"""Spend metering: records what every request cost, and enforces budgets.

THREE DESTINATIONS, ONE RECORD. Each request produces a single metadata record
that goes three places, for three different reasons:

  CloudWatch Logs   an EMF line, so CloudWatch derives metrics from the log event
                    itself (no PutMetricData) and Logs Insights can drill into an
                    individual request.
  Firehose -> S3    the same record as NDJSON, buffered and gzipped for Athena.
                    This is the invoice evidence.
  DynamoDB          atomic counters. The only destination read back in the request
                    path, because the 100% block has to be decided BEFORE Bedrock
                    is called.

OFF THE HOT PATH. record() only puts a dict on a queue; a single background
worker does all three writes. Inference never waits on metering, and a metering
outage degrades to lost records rather than failed requests. The trade is that a
hard kill loses whatever is still queued, which is why shutdown() exists and why
server.py calls it on SIGTERM.

NO PROMPT OR RESPONSE CONTENT. Everything here is metadata: identity, model,
token counts, rates, amounts, latency. The offer forbids persisting request
bodies, and nothing in this module has access to one.

WHY THE GATEWAY WRITES TO FIREHOSE ITSELF rather than letting a CloudWatch Logs
subscription filter forward the log group: a subscription filter does not forward
raw log lines, it forwards CloudWatch's own gzipped envelope with a logEvents
array. Athena cannot read that, so that route needs an unwrapping Lambda. One
extra API call from a worker thread is cheaper than a Lambda in the path.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

import pricing

log = logging.getLogger("gateway.spend")

# --- configuration (injected by Terraform, see gateway-config.tf) --------------
LOG_GROUP = os.environ.get("SPEND_LOG_GROUP", "")
FIREHOSE_STREAM = os.environ.get("SPEND_STREAM", "")
TABLE_NAME = os.environ.get("SPEND_TABLE", "")

REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", ""))
APP_VERSION = os.environ.get("APP_VERSION", "dev")

# Metering is on when Terraform supplied its targets. Absent = the toggle is off,
# and the gateway keeps serving inference with the plain text usage line only.
ENABLED = bool(LOG_GROUP or FIREHOSE_STREAM or TABLE_NAME)


def _env_decimal(name: str, default: str = "0") -> Decimal:
    raw = (os.environ.get(name) or "").strip()
    try:
        return Decimal(raw) if raw else Decimal(default)
    except Exception:
        log.warning("spend: %s=%r is not a number, treating as 0", name, raw)
        return Decimal(default)


DAILY_BUDGET = _env_decimal("SPEND_DAILY_BUDGET")
MONTHLY_BUDGET = _env_decimal("SPEND_MONTHLY_BUDGET")
BUDGETS_ACTIVE = DAILY_BUDGET > 0 or MONTHLY_BUDGET > 0

# Counters expire on their own so the table needs no cleanup job. Defaults to the
# same 90 days the log group and bucket use; Terraform passes the real value so
# the three stay in step when retention changes.
COUNTER_TTL_DAYS = int(os.environ.get("SPEND_RETENTION_DAYS", "90") or 90)

# CloudWatch namespace for the derived metrics. Budget alarms watch these.
METRIC_NAMESPACE = os.environ.get("SPEND_METRIC_NAMESPACE", "OrgaAI/Gateway")
ENVIRONMENT = os.environ.get("SPEND_ENVIRONMENT", "gateway")

# Queue is bounded: an unbounded one turns a downstream outage into the container
# running out of memory. When full, records are dropped and counted - losing
# metering is bad, taking inference down with it is worse.
QUEUE_MAX = int(os.environ.get("SPEND_QUEUE_MAX", "10000"))

# How many records one flush cycle handles. Also the aggregation window: records
# for the same user/day inside a batch collapse into one DynamoDB update.
BATCH_MAX = int(os.environ.get("SPEND_BATCH_MAX", "200"))

# How long the worker waits to accumulate a batch before writing.
BATCH_LINGER_SECONDS = float(os.environ.get("SPEND_BATCH_LINGER", "1.0"))

_clients_lock = threading.Lock()
_logs_client = None
_firehose_client = None
_dynamodb_client = None

_boto_config = BotoConfig(
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
)


def _logs():
    global _logs_client
    with _clients_lock:
        if _logs_client is None:
            _logs_client = boto3.client("logs", region_name=REGION, config=_boto_config)
    return _logs_client


def _firehose():
    global _firehose_client
    with _clients_lock:
        if _firehose_client is None:
            _firehose_client = boto3.client("firehose", region_name=REGION, config=_boto_config)
    return _firehose_client


def _dynamodb():
    global _dynamodb_client
    with _clients_lock:
        if _dynamodb_client is None:
            _dynamodb_client = boto3.client("dynamodb", region_name=REGION, config=_boto_config)
    return _dynamodb_client


# --- record construction -------------------------------------------------------


def _principal(claims: dict) -> str:
    return (
        claims.get("username")
        or claims.get("cognito:username")
        or claims.get("sub")
        or "unknown"
    )


def _int(value) -> int:
    """Token counts arrive as ints, None, or occasionally strings. Never trust
    them enough to let a TypeError escape into the request path."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_record(
    *,
    claims: dict,
    requested_model: str | None,
    model_id: str,
    fallback_used: bool,
    streamed: bool,
    usage: dict,
    usage_complete: bool,
    usage_source: str,
    outcome: str,
    error_type: str = "",
    http_status: int = 200,
    stop_reason: str = "",
    request_id: str = "",
    session_id: str = "",
    invocation_latency_ms: int = 0,
    first_byte_latency_ms: int = 0,
) -> dict:
    """Assemble one spend record.

    Keys here must match local.spend_columns in the Terraform (spend-metering.tf),
    which generates the Glue table from the same list. A key added here without
    the column being added there lands in S3 but is invisible to Athena.
    """
    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    cache_read = _int(usage.get("cache_read_input_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))

    # The two cache TTLs are priced differently (1h is 1.6x 5m on every Claude
    # model), and Anthropic reports the breakdown in a nested object rather than
    # alongside the flat counters:
    #
    #   "cache_creation": {"ephemeral_5m_input_tokens": N,
    #                      "ephemeral_1h_input_tokens": M}
    #
    # Present on both paths - the non-streaming response body, and message_start
    # on a streamed call, which server.py copies wholesale into usage. Absent
    # when a stream is abandoned before message_start is parsed, or when Bedrock
    # only supplied its own cacheWriteInputTokenCount; in that case the request
    # is priced entirely at the 5m rate, which is the previous behaviour and
    # errs low rather than inventing a split.
    cache_creation = usage.get("cache_creation")
    cache_write_1h = (
        _int(cache_creation.get("ephemeral_1h_input_tokens"))
        if isinstance(cache_creation, dict)
        else 0
    )

    cost = pricing.compute(
        model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cache_write_1h_tokens=cache_write_1h,
    )

    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "request_id": request_id,
        "session_id": session_id or "",
        # sub is the billing key. username and email are for display only - both
        # can change, sub cannot, so aggregates must never be keyed on them.
        "sub": claims.get("sub") or "unknown",
        "username": _principal(claims),
        "email": claims.get("email") or "",
        # Empty for federated users until Identity Center group passthrough is
        # mapped as a custom attribute (see identity.md). The offer's per-group
        # breakdown is blocked on that, not on this field.
        "groups": claims.get("cognito:groups") or [],
        "requested_model": requested_model or "",
        "model_id": model_id,
        # True when the client asked for a model the gateway could not map and the
        # request silently ran on the default instead - a different price than the
        # one asked for. Invisible without this flag.
        "fallback_used": bool(fallback_used),
        "region": REGION,
        "streamed": bool(streamed),
        "outcome": outcome,
        "error_type": error_type or "",
        "http_status": int(http_status),
        "stop_reason": stop_reason or "",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        # TOTAL cache writes, matching Bedrock's cacheWriteInputTokenCount.
        "cache_write_tokens": cache_write,
        # The 1-hour-TTL SUBSET of the line above, not an addition to it. Kept as
        # a subset so total_tokens stays a straight sum and cannot double-count.
        "cache_write_1h_tokens": min(cache_write_1h, cache_write),
        "total_tokens": input_tokens + output_tokens + cache_read + cache_write,
        # False marks a stream the client abandoned before Bedrock sent its final
        # metrics. Such a record UNDERSTATES spend; totals that mix them silently
        # drift, so the dashboard reports them separately.
        "usage_complete": bool(usage_complete),
        "usage_source": usage_source,
        "invocation_latency_ms": _int(invocation_latency_ms),
        "first_byte_latency_ms": _int(first_byte_latency_ms),
        "app_version": APP_VERSION,
    }
    record.update(cost.as_record_fields())
    return record


# --- EMF -----------------------------------------------------------------------
#
# CARDINALITY IS THE TRAP HERE, so the dimension sets are chosen deliberately.
#
# CloudWatch bills roughly $0.30 per custom metric per month, and a metric exists
# for every combination of dimension values. Putting the USER in a dimension
# would mean 60 users x ~13 models x 10 metrics = ~7,800 metrics, about $2,300 a
# month, to show something DynamoDB already answers for free.
#
# So: no user dimension, ever. Per-user aggregates come from the counters table,
# per-user detail from Logs Insights over the same records. The metrics exist for
# one job - alarms - and alarms fire on totals and on models, not on people.
#
# Two metric blocks rather than one, because a single block applies every metric
# to every dimension set. The aggregate gets the full set; the per-model view gets
# only the four that are worth alarming on. ~10 + 13x4 = ~62 metrics, under $20.

_METRICS_AGGREGATE = [
    ("Requests", "Count"),
    ("Errors", "Count"),
    ("BlockedRequests", "Count"),
    ("InputTokens", "Count"),
    ("OutputTokens", "Count"),
    ("CacheReadTokens", "Count"),
    ("CacheWriteTokens", "Count"),
    ("Cost", "None"),
    ("InvocationLatency", "Milliseconds"),
    ("FirstByteLatency", "Milliseconds"),
]

_METRICS_PER_MODEL = [
    ("Requests", "Count"),
    ("Errors", "Count"),
    ("InputTokens", "Count"),
    ("OutputTokens", "Count"),
    ("Cost", "None"),
]


def _emf_event(record: dict) -> str:
    """Wrap a record as an EMF log event.

    The metric VALUES sit at the root next to the ordinary properties; CloudWatch
    reads the ones named in _aws.CloudWatchMetrics and leaves the rest as
    searchable log fields. That is what makes one write serve both metrics and
    Logs Insights.
    """
    is_error = record["outcome"] != "ok"

    payload = dict(record)

    # groups is a list; EMF properties may hold lists, but Logs Insights is far
    # easier to query against a scalar. Keep both: the array for Athena (which has
    # a real array<string> column) and a joined string for Insights.
    payload["groups_joined"] = ",".join(record.get("groups") or [])

    payload.update(
        {
            "Environment": ENVIRONMENT,
            "ModelId": record["model_id"],
            "Requests": 1,
            "Errors": 1 if is_error else 0,
            "BlockedRequests": 1 if record["outcome"] == "blocked" else 0,
            "InputTokens": record["input_tokens"],
            "OutputTokens": record["output_tokens"],
            "CacheReadTokens": record["cache_read_tokens"],
            "CacheWriteTokens": record["cache_write_tokens"],
            "Cost": record["cost_total"],
            "InvocationLatency": record["invocation_latency_ms"],
            "FirstByteLatency": record["first_byte_latency_ms"],
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": METRIC_NAMESPACE,
                        "Dimensions": [["Environment"]],
                        "Metrics": [{"Name": n, "Unit": u} for n, u in _METRICS_AGGREGATE],
                    },
                    {
                        "Namespace": METRIC_NAMESPACE,
                        "Dimensions": [["Environment", "ModelId"]],
                        "Metrics": [{"Name": n, "Unit": u} for n, u in _METRICS_PER_MODEL],
                    },
                ],
            },
        }
    )
    return json.dumps(payload, separators=(",", ":"))


# --- DynamoDB counters ---------------------------------------------------------


@dataclass
class _Bucket:
    """In-memory accumulator for one counter item, filled during a flush.

    Aggregating before writing is not a micro-optimisation: a busy technician can
    produce dozens of records per second, and each would otherwise be its own
    UpdateItem against the same item - the same write capacity spent many times to
    reach the same total, plus contention on one hot key.
    """

    requests: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: Decimal = field(default_factory=lambda: Decimal(0))

    # Latency is accumulated as a SUM plus a COUNT rather than an average, because
    # averages cannot be merged: adding today's average to tomorrow's and halving
    # is only correct when both days had identical request counts. Sum/count
    # divides correctly at read time over any range.
    #
    # Kept in the counters table deliberately. The alternative - reading latency
    # back from CloudWatch metrics - would need cloudwatch:GetMetricData on the
    # task role and a second data source in the dashboard, to answer something the
    # write path already knows.
    latency_sum_ms: int = 0
    latency_count: int = 0
    ttfb_sum_ms: int = 0
    ttfb_count: int = 0

    # Records whose token counts are known to be incomplete (a stream the client
    # abandoned) and records priced at zero because the model had no rate. Both
    # make a total misleading, so both are counted and shown, never folded in.
    incomplete: int = 0
    unpriced: int = 0

    username: str = ""
    email: str = ""
    model_id: str = ""


def _n(value) -> dict:
    """DynamoDB number attribute.

    str() on a Decimal, never float(): DynamoDB stores decimal, and handing it a
    float's repr would import binary rounding error into a running money total.
    """
    return {"N": str(value)}


def _day_of(record: dict) -> str:
    # Slice the ISO timestamp rather than re-deriving from the clock: the record's
    # own timestamp is what every other destination agrees on, so the counter must
    # land in the same day even if the flush happens after midnight.
    return record["ts"][:10]


def _month_of(record: dict) -> str:
    return record["ts"][:7]


def _aggregate(records: list[dict]) -> dict[tuple[str, str], _Bucket]:
    """Collapse a batch into the counter items it should touch.

    Three items per (user, day): the day total the quota check reads, a per-model
    row for the dashboard breakdown, and a month total. The month item is written
    here rather than summed from days at read time so the monthly budget check
    stays a single GetItem.
    """
    buckets: dict[tuple[str, str], _Bucket] = defaultdict(_Bucket)

    for r in records:
        sub = r["sub"]
        day = _day_of(r)
        month = _month_of(r)
        pk = f"USER#{sub}"

        targets = [
            (pk, f"DAY#{day}"),
            (pk, f"DAY#{day}#MODEL#{r['model_id']}"),
            (pk, f"MONTH#{month}"),
        ]

        for key in targets:
            b = buckets[key]
            b.requests += 1
            b.errors += 0 if r["outcome"] == "ok" else 1
            b.input_tokens += r["input_tokens"]
            b.output_tokens += r["output_tokens"]
            b.cache_read_tokens += r["cache_read_tokens"]
            b.cache_write_tokens += r["cache_write_tokens"]
            b.cost += Decimal(str(r["cost_total"]))

            # Only count latency that was actually measured. A failed request has
            # no meaningful invocation latency, and folding a zero into the sum
            # would drag the average down and make a broken model look fast.
            if r["invocation_latency_ms"] > 0:
                b.latency_sum_ms += r["invocation_latency_ms"]
                b.latency_count += 1
            if r["first_byte_latency_ms"] > 0:
                b.ttfb_sum_ms += r["first_byte_latency_ms"]
                b.ttfb_count += 1

            if not r["usage_complete"]:
                b.incomplete += 1
            if r["rate_missing"]:
                b.unpriced += 1

            b.username = r["username"] or b.username
            b.email = r["email"] or b.email
            if key[1].startswith("DAY#") and "#MODEL#" in key[1]:
                b.model_id = r["model_id"]

    return buckets


def _write_counters(records: list[dict]) -> None:
    if not TABLE_NAME:
        return

    client = _dynamodb()
    expires_at = int(time.time()) + COUNTER_TTL_DAYS * 86400

    for (pk, sk), b in _aggregate(records).items():
        # gsi1 turns "everyone on this day" into a single Query instead of a Scan
        # over the whole table. Both the day total and the per-model rows carry it,
        # so one query per day returns everything the dashboard needs for that day.
        day = sk.split("#")[1] if sk.startswith("DAY#") else ""
        gsi1pk = f"DAY#{day}" if day else f"MONTH#{sk.split('#')[1]}"
        gsi1sk = pk if "#MODEL#" not in sk else f"{pk}#MODEL#{b.model_id}"

        # Every attribute name goes through ExpressionAttributeNames. Several of
        # these (for instance a bare `requests`) are close enough to DynamoDB's
        # reserved word list that spelling them inline is a gamble with no upside.
        names = {
            "#req": "requests",
            "#err": "errors",
            "#it": "input_tokens",
            "#ot": "output_tokens",
            "#cr": "cache_read_tokens",
            "#cw": "cache_write_tokens",
            "#cost": "cost",
            "#lsum": "latency_sum_ms",
            "#lcnt": "latency_count",
            "#tsum": "ttfb_sum_ms",
            "#tcnt": "ttfb_count",
            "#inc": "incomplete",
            "#unp": "unpriced",
            "#g1": "gsi1pk",
            "#g2": "gsi1sk",
            "#ttl": "expires_at",
            "#un": "username",
            "#em": "email",
            "#upd": "updated_at",
        }
        values = {
            ":req": _n(b.requests),
            ":err": _n(b.errors),
            ":it": _n(b.input_tokens),
            ":ot": _n(b.output_tokens),
            ":cr": _n(b.cache_read_tokens),
            ":cw": _n(b.cache_write_tokens),
            ":cost": _n(b.cost),
            ":lsum": _n(b.latency_sum_ms),
            ":lcnt": _n(b.latency_count),
            ":tsum": _n(b.ttfb_sum_ms),
            ":tcnt": _n(b.ttfb_count),
            ":inc": _n(b.incomplete),
            ":unp": _n(b.unpriced),
            ":g1": {"S": gsi1pk},
            ":g2": {"S": gsi1sk},
            ":ttl": _n(expires_at),
            ":un": {"S": b.username or "unknown"},
            ":em": {"S": b.email},
            ":upd": {"S": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        }

        update = (
            "SET #g1 = :g1, #g2 = :g2, #un = :un, #em = :em, #upd = :upd, "
            # if_not_exists so the TTL is anchored to when the counter was first
            # written. Refreshing it on every request would keep an active user's
            # counters alive indefinitely and quietly break retention.
            "#ttl = if_not_exists(#ttl, :ttl) "
            "ADD #req :req, #err :err, #it :it, #ot :ot, #cr :cr, #cw :cw, #cost :cost, "
            "#lsum :lsum, #lcnt :lcnt, #tsum :tsum, #tcnt :tcnt, #inc :inc, #unp :unp"
        )

        if "#MODEL#" in sk:
            names["#model"] = "model_id"
            values[":model"] = {"S": b.model_id}
            update = update.replace("SET ", "SET #model = :model, ", 1)

        try:
            client.update_item(
                TableName=TABLE_NAME,
                Key={"pk": {"S": pk}, "sk": {"S": sk}},
                UpdateExpression=update,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except (ClientError, BotoCoreError) as exc:
            log.warning("spend: counter update failed for %s / %s: %s", pk, sk, exc)


# --- the writer ----------------------------------------------------------------


class _Writer:
    """Single background worker draining the record queue."""

    _SENTINEL = object()

    def __init__(self) -> None:
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self.dropped = 0
        self._stream_ready = False
        self._thread: threading.Thread | None = None
        self._log_stream = f"{socket.gethostname()}/{os.getpid()}"

    def start(self) -> None:
        if not ENABLED or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="spend-writer", daemon=True)
        self._thread.start()
        log.info(
            "spend: writer started (log_group=%s stream=%s table=%s pricing=%s)",
            LOG_GROUP or "-",
            FIREHOSE_STREAM or "-",
            TABLE_NAME or "-",
            pricing.PRICING_VERSION,
        )
        if BUDGETS_ACTIVE and pricing.UNCONFIGURED:
            # Worth shouting about: budgets are configured but no rates are, so
            # every request costs 0 and the 100% block can never trigger. Silently
            # non-functional enforcement is worse than none.
            log.warning(
                "spend: budgets are set (daily=%s monthly=%s) but SPEND_PRICING is empty - "
                "every request prices at zero, so quota enforcement CANNOT trigger",
                DAILY_BUDGET,
                MONTHLY_BUDGET,
            )

    def submit(self, record: dict) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1
            # Logged sparsely: if the queue is full the last thing wanted is a log
            # line per dropped record making the pressure worse.
            if self.dropped % 100 == 1:
                log.error("spend: queue full, dropped %d record(s) so far", self.dropped)

    def shutdown(self, timeout: float = 8.0) -> None:
        """Drain before exit. ECS allows 30 s after SIGTERM, so this is well
        inside the window - and without it the last records of every deploy are
        lost, which is the least obvious way to lose a customer's money."""
        if self._thread is None:
            return
        try:
            self.queue.put_nowait(self._SENTINEL)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)
        remaining = self.queue.qsize()
        if remaining:
            log.warning("spend: shut down with %d record(s) still queued", remaining)

    # -- internals

    def _run(self) -> None:
        stopping = False
        while not stopping:
            batch: list[dict] = []
            try:
                first = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if first is self._SENTINEL:
                stopping = True
            else:
                batch.append(first)

            # Linger briefly to build a bigger batch: fewer API calls, and the
            # aggregation in _write_counters gets more to collapse.
            deadline = time.monotonic() + BATCH_LINGER_SECONDS
            while not stopping and len(batch) < BATCH_MAX:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self.queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if item is self._SENTINEL:
                    stopping = True
                    break
                batch.append(item)

            if stopping:
                # Take everything left so shutdown does not strand records.
                while len(batch) < QUEUE_MAX:
                    try:
                        item = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not self._SENTINEL:
                        batch.append(item)

            if batch:
                self._flush(batch)

    def _flush(self, batch: list[dict]) -> None:
        # Each destination is attempted independently. A CloudWatch problem must
        # not cost us the S3 evidence, and neither must cost us the counters that
        # quota enforcement reads.
        for step in (self._write_logs, self._write_firehose, _write_counters):
            try:
                step(batch)
            except Exception as exc:  # noqa: BLE001 - a writer must never die
                log.warning("spend: %s failed for %d record(s): %s", step.__name__, len(batch), exc)

    def _ensure_stream(self) -> None:
        if self._stream_ready or not LOG_GROUP:
            return
        try:
            _logs().create_log_stream(logGroupName=LOG_GROUP, logStreamName=self._log_stream)
        except ClientError as exc:
            # Already existing is the normal case after a restart, not a problem.
            if exc.response.get("Error", {}).get("Code") != "ResourceAlreadyExistsException":
                raise
        self._stream_ready = True

    def _write_logs(self, batch: list[dict]) -> None:
        if not LOG_GROUP:
            return
        self._ensure_stream()

        # PutLogEvents requires chronological order within a batch and rejects the
        # whole call otherwise. Records can arrive out of order because streaming
        # requests finish late.
        events = sorted(
            (
                {"timestamp": int(time.time() * 1000), "message": _emf_event(r)}
                for r in batch
            ),
            key=lambda e: e["timestamp"],
        )

        # No sequenceToken: AWS removed that requirement, so the writer needs no
        # per-stream state and a restart cannot get itself wedged.
        _logs().put_log_events(
            logGroupName=LOG_GROUP,
            logStreamName=self._log_stream,
            logEvents=events,
        )

    def _write_firehose(self, batch: list[dict]) -> None:
        if not FIREHOSE_STREAM:
            return

        # The trailing newline is what makes the S3 object NDJSON. Without it every
        # record in a Firehose buffer concatenates into one line and Athena sees a
        # single malformed row per object - which looks like data loss, not a
        # formatting bug.
        records = [
            {"Data": (json.dumps(r, separators=(",", ":")) + "\n").encode("utf-8")}
            for r in batch
        ]

        # PutRecordBatch caps at 500 records / 4 MB per call.
        for i in range(0, len(records), 500):
            chunk = records[i : i + 500]
            response = _firehose().put_record_batch(
                DeliveryStreamName=FIREHOSE_STREAM, Records=chunk
            )
            failed = response.get("FailedPutCount", 0)
            if failed:
                # Partial failure is reported per-record and is NOT an exception.
                # Unchecked, records vanish with a 200 response.
                log.warning("spend: firehose rejected %d of %d record(s)", failed, len(chunk))


_writer = _Writer()


def start() -> None:
    _writer.start()


def shutdown() -> None:
    _writer.shutdown()


def record(**kwargs) -> None:
    """Build and enqueue one spend record. Never raises."""
    if not ENABLED:
        return
    try:
        _writer.submit(build_record(**kwargs))
    except Exception as exc:  # noqa: BLE001
        log.warning("spend: could not record usage: %s", exc)


# --- quota ---------------------------------------------------------------------


@dataclass
class QuotaDecision:
    allowed: bool
    reason: str = ""
    scope: str = ""
    spent: Decimal = field(default_factory=lambda: Decimal(0))
    budget: Decimal = field(default_factory=lambda: Decimal(0))

    @property
    def percent(self) -> float:
        if self.budget <= 0:
            return 0.0
        return float(self.spent / self.budget * 100)


_ALLOWED = QuotaDecision(allowed=True)


def check_quota(sub: str) -> QuotaDecision:
    """Decide whether this user may spend more, BEFORE Bedrock is called.

    FAILS OPEN. If DynamoDB is unreachable the request proceeds and the failure is
    logged. That is a deliberate choice: this is a cost control, not a security
    control, and the alternative is a DynamoDB blip stopping sixty technicians
    from working. The exposure is bounded by how long an outage lasts, and the
    records still land in CloudWatch and S3 so the spend remains visible.
    """
    if not (ENABLED and TABLE_NAME and BUDGETS_ACTIVE):
        return _ALLOWED

    now = datetime.now(timezone.utc)
    day_key = f"DAY#{now.date().isoformat()}"
    month_key = f"MONTH#{now.strftime('%Y-%m')}"
    pk = f"USER#{sub}"

    try:
        # One BatchGetItem instead of two GetItems: the day and month totals are
        # both needed on every request, and this is the request path.
        response = _dynamodb().batch_get_item(
            RequestItems={
                TABLE_NAME: {
                    "Keys": [
                        {"pk": {"S": pk}, "sk": {"S": day_key}},
                        {"pk": {"S": pk}, "sk": {"S": month_key}},
                    ],
                    "ProjectionExpression": "sk, #c",
                    "ExpressionAttributeNames": {"#c": "cost"},
                }
            }
        )
    except (ClientError, BotoCoreError) as exc:
        log.warning("spend: quota lookup failed for %s, allowing request: %s", sub, exc)
        return _ALLOWED

    spent = {"day": Decimal(0), "month": Decimal(0)}
    for item in response.get("Responses", {}).get(TABLE_NAME, []):
        sk = item.get("sk", {}).get("S", "")
        amount = Decimal(item.get("cost", {}).get("N", "0"))
        if sk == day_key:
            spent["day"] = amount
        elif sk == month_key:
            spent["month"] = amount

    if DAILY_BUDGET > 0 and spent["day"] >= DAILY_BUDGET:
        return QuotaDecision(
            allowed=False,
            reason="daily budget exhausted",
            scope="day",
            spent=spent["day"],
            budget=DAILY_BUDGET,
        )

    if MONTHLY_BUDGET > 0 and spent["month"] >= MONTHLY_BUDGET:
        return QuotaDecision(
            allowed=False,
            reason="monthly budget exhausted",
            scope="month",
            spent=spent["month"],
            budget=MONTHLY_BUDGET,
        )

    return _ALLOWED


def describe() -> dict:
    """Metering configuration, for the dashboard's meta payload and /."""
    return {
        "enabled": ENABLED,
        "log_group": LOG_GROUP,
        "stream": FIREHOSE_STREAM,
        "table": TABLE_NAME,
        "retention_days": COUNTER_TTL_DAYS,
        "dropped_records": _writer.dropped,
        "budgets": {
            "daily": float(DAILY_BUDGET),
            "monthly": float(MONTHLY_BUDGET),
            "active": BUDGETS_ACTIVE,
            # Surfaced so the dashboard can explain why a configured budget is not
            # actually enforcing anything.
            "enforceable": BUDGETS_ACTIVE and not pricing.UNCONFIGURED,
        },
        "pricing": pricing.describe(),
    }
