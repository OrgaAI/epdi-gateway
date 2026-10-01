"""The /admin consumption dashboard: authorisation, JSON API and static assets.

WHO GETS IN. Two layers, on purpose.

  The ALB proves IDENTITY. Its authenticate-cognito action turns away anyone who
  cannot sign in at all and forwards the validated access token in the
  x-amzn-oidc-accesstoken header. That is the right tool for a browser - it is a
  redirect flow, which is exactly why it was rejected for /v1/messages, where the
  client is a CLI holding a bearer token.

  This module decides AUTHORISATION, by checking group membership on that token.
  It has to be here rather than at the ALB: the ALB reads its claims from the OIDC
  userinfo endpoint, and Cognito does not return cognito:groups there, so a group
  condition at the ALB would silently never match and the rule would either admit
  everyone or nobody.

WHERE THE NUMBERS COME FROM. DynamoDB counters only. Not Logs Insights (an async
query API with a concurrency limit - a dashboard built on it visibly hangs) and
not Athena (seconds per query, billed per byte scanned). The counters are written
pre-aggregated by (user, day) and (user, day, model), so one Query per day in the
range returns everything for that day and the whole payload is assembled in
memory. Athena remains the tool for the invoice export, where a slow exact answer
is the right trade.

ONE ENDPOINT FOR THE WHOLE VIEW. /admin/api/overview returns summary, per-user,
per-model, timeseries and alerts together. Six endpoints would mean six identical
DynamoDB read storms for one page load.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

import pricing
import spend

log = logging.getLogger("gateway.admin")

# Group whose members may read the dashboard. Injected by Terraform; when absent
# the dashboard is off entirely rather than open.
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "")

# Where the built UI lands (see the Dockerfile's node stage).
STATIC_ROOT = os.environ.get("ADMIN_STATIC_ROOT", os.path.join(os.path.dirname(__file__), "static"))

ENABLED = bool(ADMIN_GROUP)

# Widest range the API will assemble in one call. 92 days covers "this quarter"
# and caps a single request at 92 parallel DynamoDB queries; without a ceiling a
# hand-edited URL could ask for ten years and hold a worker thread for minutes.
MAX_RANGE_DAYS = 92

# Overview responses are cached briefly. A dashboard with auto-refresh on, open on
# several screens, would otherwise re-read the same day partitions every few
# seconds for data that changes on a one-minute Firehose cadence anyway.
CACHE_TTL_SECONDS = float(os.environ.get("ADMIN_CACHE_TTL", "15"))

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

_ddb_client = None
_ddb_lock = threading.Lock()


def _ddb():
    global _ddb_client
    with _ddb_lock:
        if _ddb_client is None:
            _ddb_client = boto3.client(
                "dynamodb",
                region_name=spend.REGION,
                config=BotoConfig(
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=5,
                    read_timeout=10,
                ),
            )
    return _ddb_client


# --- authorisation -------------------------------------------------------------


def extract_token(headers) -> str:
    """Pull the credential, preferring the ALB's forwarded access token.

    x-amzn-oidc-accesstoken comes first because in normal operation the ALB has
    already completed the OIDC flow and this is the browser's real token. The
    Authorization fallback is what makes the dashboard reachable through ECS Exec
    or a port-forward, where there is no ALB in front to set the header.
    """
    forwarded = headers.get("x-amzn-oidc-accesstoken")
    if forwarded:
        return forwarded.strip()

    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer ") :].strip()
    return (headers.get("x-api-key") or "").strip()


def authorize(headers, validate) -> tuple[bool, int, dict]:
    """Validate the token and require ADMIN_GROUP membership.

    Returns (ok, status, claims_or_error). 401 means "we do not know who you are",
    403 means "we do, and you may not" - the distinction matters because the ALB
    will re-run the login flow on a 401 and would loop forever on a user who is
    authenticated but simply not an administrator.
    """
    if not ENABLED:
        return False, 404, {"error": "admin dashboard is not enabled on this server"}

    token = extract_token(headers)
    if not token:
        return False, 401, {"error": "missing credential"}

    ok, claims = validate(token)
    if not ok:
        return False, 401, claims

    groups = claims.get("cognito:groups") or []
    if ADMIN_GROUP not in groups:
        # Deliberately explicit about the reason. The most likely cause is a
        # federated admin who has signed in but has not been added to the pool
        # group yet (Terraform cannot do it before the user exists), and an opaque
        # 403 would send whoever hits it hunting through IAM instead.
        log.info("admin: %s denied, groups=%s", claims.get("sub"), groups)
        return False, 403, {
            "error": f"not a member of {ADMIN_GROUP}",
            "hint": (
                "Federated users must sign in once before they can be added to the "
                "group. Add with: aws cognito-idp admin-add-user-to-group"
            ),
            "groups": groups,
        }

    return True, 200, claims


# --- data access ---------------------------------------------------------------


def _dec(item: dict, key: str, default: str = "0") -> Decimal:
    return Decimal(item.get(key, {}).get("N", default))


def _int_of(item: dict, key: str) -> int:
    return int(Decimal(item.get(key, {}).get("N", "0")))


def _str_of(item: dict, key: str) -> str:
    return item.get(key, {}).get("S", "")


def _query_day(day: str) -> list[dict]:
    """Every counter row for one day: user totals and per-model rows together.

    Both row kinds carry the same gsi1pk, which is what lets a single query serve
    the user table and the model breakdown. Paginated because a day with many
    users and models can exceed the 1 MB query limit.
    """
    if not spend.TABLE_NAME:
        return []

    rows: list[dict] = []
    kwargs = {
        "TableName": spend.TABLE_NAME,
        "IndexName": "gsi1",
        "KeyConditionExpression": "gsi1pk = :d",
        "ExpressionAttributeValues": {":d": {"S": f"DAY#{day}"}},
    }

    try:
        while True:
            response = _ddb().query(**kwargs)
            rows.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    except (ClientError, BotoCoreError) as exc:
        # One bad day degrades that day to zero rather than failing the page. The
        # response carries a `partial` flag so the UI can say so instead of
        # presenting an under-count as fact.
        log.warning("admin: query failed for %s: %s", day, exc)
        raise

    return rows


def _date_range(start: date, end: date) -> list[str]:
    days = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(days + 1)]


def _load_days(days: list[str]) -> tuple[dict[str, list[dict]], bool]:
    """Fetch all days in parallel.

    Serially, a 30-day range is 30 round trips of ~20 ms and the page takes most
    of a second to assemble for no reason. The pool is capped so a 92-day request
    cannot open 92 sockets at once.
    """
    result: dict[str, list[dict]] = {}
    partial = False

    with ThreadPoolExecutor(max_workers=min(12, max(1, len(days)))) as pool:
        futures = {pool.submit(_query_day, day): day for day in days}
        for future, day in futures.items():
            try:
                result[day] = future.result()
            except Exception:  # noqa: BLE001
                result[day] = []
                partial = True

    return result, partial


# --- aggregation ---------------------------------------------------------------


class _Totals:
    """Mutable accumulator shared by the summary, user and model views."""

    __slots__ = (
        "requests", "errors", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "cost",
        "latency_sum", "latency_count", "ttfb_sum", "ttfb_count",
        "incomplete", "unpriced",
    )

    def __init__(self) -> None:
        for slot in self.__slots__:
            setattr(self, slot, Decimal(0) if slot == "cost" else 0)

    def add(self, item: dict) -> None:
        self.requests += _int_of(item, "requests")
        self.errors += _int_of(item, "errors")
        self.input_tokens += _int_of(item, "input_tokens")
        self.output_tokens += _int_of(item, "output_tokens")
        self.cache_read_tokens += _int_of(item, "cache_read_tokens")
        self.cache_write_tokens += _int_of(item, "cache_write_tokens")
        self.cost += _dec(item, "cost")
        self.latency_sum += _int_of(item, "latency_sum_ms")
        self.latency_count += _int_of(item, "latency_count")
        self.ttfb_sum += _int_of(item, "ttfb_sum_ms")
        self.ttfb_count += _int_of(item, "ttfb_count")
        self.incomplete += _int_of(item, "incomplete")
        self.unpriced += _int_of(item, "unpriced")

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def cache_hit_rate(self) -> float:
        """Share of billable input served from cache.

        Denominator is input + cache_read (the tokens that COULD have been a hit),
        not total tokens - including output would make the number meaningless and
        always small. Below roughly 12% caching costs more than it saves, because a
        cache write is priced above uncached input.
        """
        denominator = self.input_tokens + self.cache_read_tokens
        if denominator <= 0:
            return 0.0
        return round(self.cache_read_tokens / denominator * 100, 1)

    @property
    def avg_latency_ms(self) -> int:
        return int(self.latency_sum / self.latency_count) if self.latency_count else 0

    @property
    def avg_ttfb_ms(self) -> int:
        return int(self.ttfb_sum / self.ttfb_count) if self.ttfb_count else 0

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "errors": self.errors,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "cost": float(self.cost),
            "cache_hit_rate": self.cache_hit_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "avg_ttfb_ms": self.avg_ttfb_ms,
            "incomplete": self.incomplete,
            "unpriced": self.unpriced,
            "error_rate": round(self.errors / self.requests * 100, 1) if self.requests else 0.0,
        }


def _is_model_row(item: dict) -> bool:
    return "#MODEL#" in _str_of(item, "sk")


def build_overview(start: date, end: date) -> dict:
    days = _date_range(start, end)
    by_day, partial = _load_days(days)

    overall = _Totals()
    per_user: dict[str, _Totals] = {}
    per_model: dict[str, _Totals] = {}
    user_meta: dict[str, dict] = {}

    # date -> model -> cost, for the stacked trend
    trend: dict[str, dict[str, Decimal]] = {day: {} for day in days}
    trend_totals: dict[str, _Totals] = {day: _Totals() for day in days}

    for day, items in by_day.items():
        for item in items:
            sub = _str_of(item, "pk").removeprefix("USER#")

            if _is_model_row(item):
                model = _str_of(item, "model_id") or _str_of(item, "sk").split("#MODEL#")[-1]
                per_model.setdefault(model, _Totals()).add(item)
                trend[day][model] = trend[day].get(model, Decimal(0)) + _dec(item, "cost")
                # Model rows are NOT added to the overall or per-user totals: they
                # are a second view of the same requests, and counting both would
                # double every figure on the page.
                continue

            overall.add(item)
            trend_totals[day].add(item)
            per_user.setdefault(sub, _Totals()).add(item)
            user_meta.setdefault(
                sub,
                {
                    "username": _str_of(item, "username") or sub,
                    "email": _str_of(item, "email"),
                },
            )

    daily_budget = float(spend.DAILY_BUDGET)
    monthly_budget = float(spend.MONTHLY_BUDGET)

    users = []
    for sub, totals in per_user.items():
        row = {"sub": sub, **user_meta.get(sub, {}), **totals.as_dict()}
        # Budget percentage is only meaningful against a single day. Over a range
        # it is reported against the range's implied allowance so the column stays
        # comparable between users rather than silently changing meaning.
        allowance = daily_budget * len(days)
        row["budget"] = allowance
        row["budget_percent"] = round(row["cost"] / allowance * 100, 1) if allowance > 0 else 0.0
        users.append(row)
    users.sort(key=lambda r: (r["cost"], r["total_tokens"]), reverse=True)

    models = [{"model_id": m, **t.as_dict()} for m, t in per_model.items()]
    models.sort(key=lambda r: (r["cost"], r["total_tokens"]), reverse=True)

    timeseries = [
        {
            "date": day,
            "cost": float(trend_totals[day].cost),
            "requests": trend_totals[day].requests,
            "total_tokens": trend_totals[day].total_tokens,
            "by_model": {m: float(c) for m, c in trend[day].items()},
        }
        for day in days
    ]

    token_types = [
        {"type": "input", "tokens": overall.input_tokens},
        {"type": "output", "tokens": overall.output_tokens},
        {"type": "cache_read", "tokens": overall.cache_read_tokens},
        {"type": "cache_write", "tokens": overall.cache_write_tokens},
    ]

    # Thresholds are 75% and 90%, matching the user notifications that will be sent
    # from the same numbers. Computed here rather than in the UI so the dashboard and
    # the notifications cannot disagree about where a threshold sits - two
    # definitions of "90%" in two places is how a technician gets an email about a
    # limit the dashboard says they are under.
    #
    # 50% is deliberately NOT a tier. It was in the original offer text, but a
    # notice at half budget is not actionable - it is normal mid-month - and a
    # dashboard that always has rows in its alert panel trains people to skip it.
    alerts = []
    for row in users:
        pct = row["budget_percent"]
        if row["budget"] <= 0:
            continue
        level = "critical" if pct >= 90 else "warning" if pct >= 75 else None
        if level:
            alerts.append(
                {
                    "sub": row["sub"],
                    "username": row["username"],
                    "percent": pct,
                    "cost": row["cost"],
                    "budget": row["budget"],
                    "level": level,
                }
            )

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "range": {"from": start.isoformat(), "to": end.isoformat(), "days": len(days)},
            # True when at least one day's query failed. The UI must say so - an
            # under-count presented as a total is worse than an error.
            "partial": partial,
            "budgets": {"daily": daily_budget, "monthly": monthly_budget},
            "metering": spend.describe(),
        },
        "summary": {
            **overall.as_dict(),
            "active_users": len(per_user),
            "models_used": len(per_model),
            "avg_cost_per_request": (
                float(overall.cost / overall.requests) if overall.requests else 0.0
            ),
        },
        "users": users,
        "models": models,
        "timeseries": timeseries,
        "token_types": token_types,
        "alerts": alerts,
    }


def build_user_detail(sub: str, start: date, end: date) -> dict:
    """Per-day and per-model breakdown for one person.

    Queried by primary key with a range on sk rather than through the index: every
    row for one user on one day shares the pk, so this is a single Query per day
    boundary and does not touch other users' data at all.
    """
    days = _date_range(start, end)
    daily: list[dict] = []
    per_model: dict[str, _Totals] = {}
    overall = _Totals()
    meta = {"sub": sub, "username": sub, "email": ""}

    if not spend.TABLE_NAME:
        return {"user": meta, "summary": overall.as_dict(), "daily": [], "models": []}

    try:
        response = _ddb().query(
            TableName=spend.TABLE_NAME,
            KeyConditionExpression="pk = :p AND sk BETWEEN :lo AND :hi",
            ExpressionAttributeValues={
                ":p": {"S": f"USER#{sub}"},
                ":lo": {"S": f"DAY#{days[0]}"},
                # The high bound needs the trailing tilde: sk values for model rows
                # are DAY#<date>#MODEL#..., which sort AFTER plain DAY#<date>, so a
                # bound of DAY#<last> alone would exclude the final day's models.
                ":hi": {"S": f"DAY#{days[-1]}~"},
            },
        )
    except (ClientError, BotoCoreError) as exc:
        log.warning("admin: user detail query failed for %s: %s", sub, exc)
        return {"user": meta, "summary": overall.as_dict(), "daily": [], "models": [], "partial": True}

    per_day: dict[str, _Totals] = {day: _Totals() for day in days}

    for item in response.get("Items", []):
        sk = _str_of(item, "sk")
        day = sk.split("#")[1]
        if "#MODEL#" in sk:
            model = _str_of(item, "model_id") or sk.split("#MODEL#")[-1]
            per_model.setdefault(model, _Totals()).add(item)
            continue
        if day in per_day:
            per_day[day].add(item)
        overall.add(item)
        meta["username"] = _str_of(item, "username") or meta["username"]
        meta["email"] = _str_of(item, "email") or meta["email"]

    daily = [{"date": day, **per_day[day].as_dict()} for day in days]
    models = [{"model_id": m, **t.as_dict()} for m, t in per_model.items()]
    models.sort(key=lambda r: (r["cost"], r["total_tokens"]), reverse=True)

    return {"user": meta, "summary": overall.as_dict(), "daily": daily, "models": models}


# --- CSV export ----------------------------------------------------------------

_CSV_COLUMNS = [
    "sub", "username", "email", "requests", "errors",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "total_tokens", "cost", "currency", "pricing_version",
    "cache_hit_rate", "avg_latency_ms", "budget", "budget_percent",
]


def build_csv(overview: dict) -> str:
    """Per-user export for invoice reconciliation.

    Carries currency and pricing_version on every row, not just in a header: the
    file will be opened in Excel, filtered and pasted into something else, and a
    column of amounts with no idea which rate table produced them is not evidence.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()

    currency = pricing.CURRENCY
    version = pricing.PRICING_VERSION

    for row in overview["users"]:
        writer.writerow({**row, "currency": currency, "pricing_version": version})

    return buffer.getvalue()


# --- request handling ----------------------------------------------------------


def _parse_range(params: dict) -> tuple[date, date, str | None]:
    today = datetime.now(timezone.utc).date()

    raw_from = (params.get("from") or [""])[0]
    raw_to = (params.get("to") or [""])[0]

    try:
        end = date.fromisoformat(raw_to) if raw_to else today
        start = date.fromisoformat(raw_from) if raw_from else end - timedelta(days=29)
    except ValueError:
        return today, today, "from and to must be YYYY-MM-DD"

    if start > end:
        return today, today, "from must not be after to"
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        return today, today, f"range must not exceed {MAX_RANGE_DAYS} days"

    return start, end, None


def _cached_overview(start: date, end: date) -> dict:
    key = f"{start}:{end}"
    now = time.monotonic()

    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]

    payload = build_overview(start, end)

    with _cache_lock:
        _cache[key] = (now, payload)
        # Bounded so a script walking arbitrary ranges cannot grow this forever.
        if len(_cache) > 64:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)

    return payload


def _static_path(route: str) -> str | None:
    """Resolve a /admin path to a file, refusing anything outside STATIC_ROOT.

    The realpath comparison is the actual defence: without it a request for
    /admin/../../etc/passwd would be served, because the path is attacker-supplied
    and os.path.join is happy to walk upward.
    """
    relative = route[len("/admin") :].lstrip("/")
    if not relative or relative.endswith("/"):
        relative = "index.html"

    candidate = os.path.realpath(os.path.join(STATIC_ROOT, relative))
    root = os.path.realpath(STATIC_ROOT)

    if not candidate.startswith(root + os.sep) and candidate != root:
        return None
    if os.path.isfile(candidate):
        return candidate

    # Unknown paths fall back to the shell so client-side routes (/admin/users/x)
    # survive a page reload instead of 404ing.
    index = os.path.join(root, "index.html")
    return index if os.path.isfile(index) else None


def handle(handler, validate) -> bool:
    """Serve a /admin request. Returns False if the route is not ours.

    `handler` is the BaseHTTPRequestHandler; `validate` is server.py's token
    validator, passed in rather than imported to keep this module free of a
    circular import back into the server.
    """
    parsed = urlparse(handler.path)
    route = parsed.path

    if route != "/admin" and not route.startswith("/admin/"):
        return False

    if not ENABLED:
        handler.send_json(404, {"error": "admin dashboard is not enabled"})
        return True

    is_api = route.startswith("/admin/api/")

    ok, status, claims = authorize(handler.headers, validate)
    if not ok:
        if is_api:
            handler.send_json(status, claims)
        else:
            # A browser gets HTML, not a JSON blob it cannot read. 403 is the
            # interesting case: the ALB already logged them in, so the page has to
            # explain that the missing piece is group membership.
            handler.send_html(status, _denied_page(status, claims))
        return True

    params = parse_qs(parsed.query)

    if route == "/admin/api/meta":
        handler.send_json(
            200,
            {
                "viewer": {
                    "sub": claims.get("sub"),
                    "username": claims.get("username") or claims.get("cognito:username"),
                    "email": claims.get("email"),
                    "groups": claims.get("cognito:groups") or [],
                },
                "metering": spend.describe(),
                "max_range_days": MAX_RANGE_DAYS,
            },
        )
        return True

    if route == "/admin/api/overview":
        start, end, error = _parse_range(params)
        if error:
            handler.send_json(400, {"error": error})
            return True
        handler.send_json(200, _cached_overview(start, end))
        return True

    if route.startswith("/admin/api/users/"):
        sub = route[len("/admin/api/users/") :]
        if not sub:
            handler.send_json(400, {"error": "user id required"})
            return True
        start, end, error = _parse_range(params)
        if error:
            handler.send_json(400, {"error": error})
            return True
        handler.send_json(200, build_user_detail(sub, start, end))
        return True

    if route == "/admin/api/export.csv":
        start, end, error = _parse_range(params)
        if error:
            handler.send_json(400, {"error": error})
            return True
        body = build_csv(_cached_overview(start, end)).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/csv; charset=utf-8")
        handler.send_header(
            "Content-Disposition",
            f'attachment; filename="consumption-{start}-to-{end}.csv"',
        )
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True

    if is_api:
        handler.send_json(404, {"error": f"unknown admin endpoint: {route}"})
        return True

    path = _static_path(route)
    if path is None:
        handler.send_html(
            503,
            _message_page(
                "Dashboard assets missing",
                "The container was built without the UI bundle. Rebuild the image so "
                "the Node stage produces /app/static.",
            ),
        )
        return True

    handler.send_file(path)
    return True


# --- minimal server-rendered pages --------------------------------------------
#
# Only for states the SPA can never render: not-authorised, and assets missing.
# Everything a signed-in administrator sees comes from the React bundle.

_PAGE_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: grid; place-items: center;
  background: #0d1117; color: #e6edf3; padding: 24px;
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.card {
  max-width: 34rem; background: #161b22; border: 1px solid #30363d;
  border-radius: 14px; padding: 32px 34px;
  box-shadow: 0 18px 48px rgba(0,0,0,.45);
}
.brand {
  font-size: 11px; letter-spacing: .14em; text-transform: uppercase;
  color: #d94f2b; font-weight: 700; margin-bottom: 14px;
}
h1 { font-size: 20px; margin: 0 0 12px; letter-spacing: -.01em; }
p { margin: 0 0 12px; color: #9aa7b4; }
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px;
  background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
  padding: 2px 6px; color: #e6edf3;
}
.hint { margin-top: 18px; padding-top: 16px; border-top: 1px solid #30363d; font-size: 13px; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title} - ORGA AI</title><style>{_PAGE_CSS}</style></head>"
        f"<body><main class='card'><div class='brand'>ORGA AI &middot; Consumption</div>{body}</main></body></html>"
    )


def _message_page(title: str, message: str) -> str:
    return _page(title, f"<h1>{title}</h1><p>{message}</p>")


def _denied_page(status: int, error: dict) -> str:
    if status == 401:
        return _page(
            "Sign in required",
            "<h1>Sign in required</h1>"
            "<p>This page is served behind Cognito. Reload to start the sign-in flow.</p>",
        )

    groups = ", ".join(error.get("groups") or []) or "none"
    return _page(
        "Access denied",
        "<h1>Access denied</h1>"
        f"<p>Your account is signed in but is not a member of <code>{ADMIN_GROUP}</code>.</p>"
        f"<p>Groups on your token: <code>{groups}</code></p>"
        "<div class='hint'><p>If you have just signed in for the first time, the account "
        "now exists in the user pool and can be added to the group:</p>"
        f"<p><code>aws cognito-idp admin-add-user-to-group --group-name {ADMIN_GROUP} "
        "--username &lt;your-pool-username&gt; --user-pool-id &lt;pool-id&gt;</code></p></div>",
    )
