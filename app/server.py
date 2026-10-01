"""ORGA AI consumption gateway - Anthropic Messages API over Amazon Bedrock.

A Claude Code compatible gateway. It speaks the Anthropic Messages protocol on
the front (so Claude Code can point at it with ANTHROPIC_BASE_URL) and calls
Anthropic Claude models on Amazon Bedrock at the back, streaming the answer
through unbuffered.

Technicians authenticate with a Cognito OIDC token, which the gateway validates
itself - not the ALB: ALB authenticate-oidc is a browser redirect flow and does
not fit a CLI client sending a bearer token.

Endpoints:
  GET  /health                 -> 200 {"status":"ok"}     (ALB health check, no auth)
  GET  /                       -> 200 JSON banner          (no auth)
  GET  /whoami                 -> 200 token claims / 401    (debug: prove the token flow)
  POST /v1/messages            -> Anthropic Messages, SSE or JSON  (auth required)
  GET  /v1/models              -> model discovery list      (auth required)
  HEAD /api/hello              -> 200                       (Claude Code warm-up probe)
  GET  /admin, /admin/*        -> consumption dashboard     (admin group required)

Why the gateway sits in the path at all: it is the control plane. It meters every
request (spend.py), enforces per-user budgets before calling Bedrock, and serves
the administrators' consumption dashboard (admin.py). It records spend METADATA
ONLY (token counts, rates, amounts) - never prompt or response content.

Protocol notes that the implementation depends on (per the Claude Code gateway
compatibility guide):
  * Claude Code appends /v1/messages itself, so ANTHROPIC_BASE_URL must NOT end
    in /v1. Inference requests may carry a query string (?beta=true), so paths
    are matched without it.
  * Responses must stream. A gateway that buffers makes Claude Code stall.
  * Claude Code counts every relayed byte and aborts a stream silent for 300s.
    Bedrock's event stream sends no pings of its own, so this gateway emits its
    own SSE ping events during silent gaps.
  * Upstream error wording drives Claude Code's retry/capability-downgrade
    logic, so Bedrock error messages are passed through unwrapped.
"""

from __future__ import annotations

import json
import logging
import os
import mimetypes
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
import jwt  # PyJWT[crypto]
from botocore.config import Config as BotoConfig
from botocore.credentials import (
    AssumeRoleCredentialFetcher,
    DeferredRefreshableCredentials,
)
from botocore.session import get_session
from botocore.exceptions import BotoCoreError, ClientError
from jwt import PyJWKClient

import admin
import spend

PORT = int(os.environ.get("PORT", "8080"))
APP_VERSION = os.environ.get("APP_VERSION", "dev")

# --- Cognito (token validation) ------------------------------------------------
# Injected by the ECS task definition from the Terraform `cognito` output. When
# unset, protected endpoints answer 401 rather than crashing, so /health still
# works and the container stays deployable before Cognito is wired.
COGNITO_ISSUER = os.environ.get("COGNITO_ISSUER", "")
COGNITO_JWKS_URI = os.environ.get("COGNITO_JWKS_URI", "")
# Cognito ACCESS tokens carry client_id, not aud, so this is checked manually.
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
# The dashboard's own app client. Separate from the one above because the ALB's
# authenticate-cognito action needs a CONFIDENTIAL client (one with a secret), and
# the CLI client must stay public. Tokens minted for it carry that client_id, so
# without this the dashboard's token would be rejected as "client_id does not
# match" even though it is valid.
ADMIN_CLIENT_ID = os.environ.get("ADMIN_CLIENT_ID", "")

# --- authorisation on the inference path --------------------------------------
# Group membership required to consume inference. Authentication establishes WHO
# somebody is; this establishes whether they may spend the client's Bedrock budget.
# The two are separate on purpose - a pool account is not an entitlement.
TECHNICIAN_GROUP = os.environ.get("TECHNICIAN_GROUP", "")
# Staged rollout. False = evaluate the rule and log, but do not refuse. Federated
# technicians can only be added to a pool group AFTER their first sign-in, so
# enforcing before membership is populated would refuse all of them at once.
ENFORCE_TECHNICIAN_GROUP = os.environ.get("ENFORCE_TECHNICIAN_GROUP", "").lower() in (
    "1",
    "true",
    "yes",
)

# --- Bedrock -------------------------------------------------------------------
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "eu-west-1"))
# Fallback model when the client asks for one this gateway does not map. An
# Anthropic Claude EU inference profile: inference stays in the EU (data
# residency), which is why the id is eu.-prefixed.
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
# Optional JSON object mapping the model name Claude Code sends (or an alias like
# "sonnet") to a Bedrock model id / inference profile. Example:
#   MODEL_MAP='{"sonnet":"eu.anthropic.claude-sonnet-4-5-20250929-v1:0"}'
MODEL_MAP: dict[str, str] = {}
_raw_model_map = os.environ.get("MODEL_MAP", "").strip()
if _raw_model_map:
    try:
        MODEL_MAP = json.loads(_raw_model_map)
    except json.JSONDecodeError:
        MODEL_MAP = {}

# Default max_tokens when a client omits it. Claude Code always sends its own, so
# this is not a cap: capping would silently truncate the client's intent. A real
# spend ceiling belongs in the budget logic, not here.
DEFAULT_MAX_TOKENS = int(os.environ.get("DEFAULT_MAX_TOKENS", "4096"))

# The Bedrock dialect string for the Anthropic Messages body. Note this is NOT
# the value of the anthropic-version HTTP header (2023-06-01).
BEDROCK_ANTHROPIC_VERSION = os.environ.get("BEDROCK_ANTHROPIC_VERSION", "bedrock-2023-05-31")

# Body fields that must not reach Bedrock's InvokeModel.
#   model, stream      - Bedrock takes these in the URL / API call, not the body
#   context_management,
#   output_config      - Anthropic-endpoint capabilities Bedrock rejects with
#                        "Extra inputs are not permitted" (hard 400)
# Bridging this schema gap is the gateway's job, per the compatibility guide.
DROP_BODY_FIELDS = {"model", "stream", "context_management", "output_config", "metadata"}

# Claude Code sends capability requests in the anthropic-beta header; the Bedrock
# dialect expects an anthropic_beta body field. Unknown values make Bedrock 400,
# so forwarding is opt-in.
FORWARD_ANTHROPIC_BETA = os.environ.get("FORWARD_ANTHROPIC_BETA", "").lower() in ("1", "true")

# Interval between SSE keep-alive pings during upstream silence. Must stay well
# under Claude Code's 300s byte watchdog.
PING_INTERVAL_SECONDS = float(os.environ.get("PING_INTERVAL_SECONDS", "15"))

# CROSS-ACCOUNT BEDROCK. Empty (the normal case) means the task role calls Bedrock
# in this account directly.
#
# Set to a role ARN to invoke Bedrock in a DIFFERENT account instead. This exists
# because AWS applied every Bedrock quota for the newest Claude models as zero on
# the demo account - an account-level eligibility restriction that accepting the
# Marketplace agreements did not lift - while a sibling account has the same quotas
# at their defaults.
#
# Residency is unaffected: the model ids are still eu.* inference profiles, so
# inference stays inside the EU whichever account pays for it. What DOES move is
# the bill and the CloudTrail record, so this is a deliberate, reversible switch
# and not a default.
BEDROCK_ASSUME_ROLE_ARN = os.environ.get("BEDROCK_ASSUME_ROLE_ARN", "").strip()

# sts:ExternalId the target trust policy requires. Anti-confused-deputy value, not
# a credential on its own: the trust policy also pins the calling principal's ARN.
BEDROCK_ASSUME_EXTERNAL_ID = os.environ.get("BEDROCK_ASSUME_EXTERNAL_ID", "").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gateway")

_jwks_client: PyJWKClient | None = None
_bedrock_client = None
_client_lock = threading.Lock()


def _get_jwks_client() -> PyJWKClient | None:
    """Cached JWKS client: fetches Cognito's signing keys and picks one per token
    via the kid header. Lazy so a missing config does not break startup."""
    global _jwks_client
    if not COGNITO_JWKS_URI:
        return None
    with _client_lock:
        if _jwks_client is None:
            _jwks_client = PyJWKClient(COGNITO_JWKS_URI)
    return _jwks_client


def _assumed_role_session() -> boto3.Session:
    """A boto3 session whose credentials come from another account's role, and
    which RENEWS them on its own.

    WHY THIS IS NOT sts.assume_role(). That call returns a static credential set
    that expires (an hour by default). Handing it to boto3.client() produces a
    gateway that works perfectly for the length of a test and then answers
    ExpiredTokenException for every request until the task is replaced - the worst
    possible failure shape, because it passes review and breaks in front of users.

    DeferredRefreshableCredentials fetches on first use and re-fetches shortly
    before each expiry, so a task that runs for days keeps working. "Deferred"
    also means a missing or broken trust policy surfaces on the first inference
    request rather than at import time, which keeps /health honest.
    """
    fetcher = AssumeRoleCredentialFetcher(
        client_creator=get_session().create_client,
        source_credentials=get_session().get_credentials(),
        role_arn=BEDROCK_ASSUME_ROLE_ARN,
        extra_args={
            "RoleSessionName": "epdi-ai-gateway",
            # Only sent when configured: AWS rejects an ExternalId that the trust
            # policy does not ask for, so an empty value must be omitted entirely
            # rather than passed as "".
            **(
                {"ExternalId": BEDROCK_ASSUME_EXTERNAL_ID}
                if BEDROCK_ASSUME_EXTERNAL_ID
                else {}
            ),
        },
    )

    botocore_session = get_session()
    botocore_session._credentials = DeferredRefreshableCredentials(
        method="assume-role",
        refresh_using=fetcher.fetch_credentials,
    )
    return boto3.Session(botocore_session=botocore_session)


def _get_bedrock_client():
    """Cached bedrock-runtime client. read_timeout is generous: a long Claude
    answer streams for minutes and must not be cut off by the SDK.

    Two credential paths. Normally the ECS task role calls Bedrock in this
    account. When BEDROCK_ASSUME_ROLE_ARN is set, the client instead runs on
    credentials assumed in another account - the escape hatch for the case where
    AWS has applied every Bedrock quota for a model as zero on this account.
    Unset means the previous behaviour exactly, so the switch is reversible by
    clearing one SSM parameter.

    Caching the client is still correct in the assumed-role case: the credential
    object refreshes itself underneath the client, so the client does not need
    rebuilding.
    """
    global _bedrock_client
    with _client_lock:
        if _bedrock_client is None:
            config = BotoConfig(
                retries={"max_attempts": 2, "mode": "standard"},
                connect_timeout=10,
                read_timeout=900,
            )
            if BEDROCK_ASSUME_ROLE_ARN:
                log.info(
                    "bedrock: assuming %s for inference", BEDROCK_ASSUME_ROLE_ARN
                )
                _bedrock_client = _assumed_role_session().client(
                    "bedrock-runtime",
                    region_name=BEDROCK_REGION,
                    config=config,
                )
            else:
                _bedrock_client = boto3.client(
                    "bedrock-runtime",
                    region_name=BEDROCK_REGION,
                    config=config,
                )
    return _bedrock_client


def _bearer_token(headers) -> str:
    """Extract the credential. Claude Code sends it as Authorization: Bearer and/or
    x-api-key depending on which variable the developer set, so accept both."""
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer ") :].strip()
    return (headers.get("x-api-key") or "").strip()


def _validate_raw_token(token: str) -> tuple[bool, dict]:
    """Validate a bare JWT string. Returns (ok, claims) or (False, error dict).

    Split out from _validate_token because the dashboard's credential does not
    arrive in the Authorization header: the ALB completes the OIDC flow and
    forwards the access token in x-amzn-oidc-accesstoken. Both paths must apply
    exactly the same checks, so they share this one function rather than growing a
    second, subtly different validator.
    """
    client = _get_jwks_client()
    if client is None or not COGNITO_ISSUER:
        return False, {"error": "auth not configured on this server"}

    try:
        signing_key = client.get_signing_key_from_jwt(token)
        # Access tokens have no 'aud' claim, so audience checking is off and
        # client_id is verified below instead.
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=COGNITO_ISSUER,
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as exc:
        return False, {"error": f"invalid token: {exc}"}

    # The dashboard uses a SECOND app client (confidential, for the ALB), so its
    # tokens carry a different client_id than the CLI's. Accept either, since both
    # are clients of the same user pool and the issuer check above is what
    # establishes the token's origin.
    allowed_clients = {c for c in (COGNITO_CLIENT_ID, ADMIN_CLIENT_ID) if c}
    if allowed_clients and claims.get("client_id") not in (None, *allowed_clients):
        return False, {"error": "token client_id does not match"}

    return True, claims


def _validate_token(headers) -> tuple[bool, dict]:
    """Validate the Cognito JWT from request headers."""
    token = _bearer_token(headers)
    if not token:
        return False, {"error": "missing credential (Authorization: Bearer or x-api-key)"}
    return _validate_raw_token(token)


def _principal(claims: dict) -> str:
    """Who the request is billed to. Federated users arrive as the SAML subject."""
    return (
        claims.get("username")
        or claims.get("cognito:username")
        or claims.get("sub")
        or "unknown"
    )


def _may_consume_inference(claims: dict) -> tuple[bool, str]:
    """Is this identity entitled to spend on inference?

    Returns (allowed, reason). Being authenticated is deliberately not sufficient:
    without this, every account in the user pool can consume Bedrock, and the only
    thing standing between a stranger and the client's budget is Cognito's
    self-registration setting.

    Two soft edges, both intentional:

      * No TECHNICIAN_GROUP configured -> allow. The check is opt-in so the gateway
        behaves exactly as before on an environment that has not adopted it.
      * ENFORCE off -> allow, but say who would have been refused. Federated users
        cannot be placed in a pool group until they have signed in once, so
        enforcing before membership is populated locks out precisely the people the
        platform exists for. This is how the blast radius is measured first.
    """
    if not TECHNICIAN_GROUP:
        return True, ""

    groups = claims.get("cognito:groups") or []
    if TECHNICIAN_GROUP in groups:
        return True, ""

    reason = f"not a member of {TECHNICIAN_GROUP}"

    if not ENFORCE_TECHNICIAN_GROUP:
        # Warning, not info: this line is the entire point of the observation mode,
        # and it is what someone greps to build the membership list.
        log.warning(
            "would deny (enforcement off) user=%s sub=%s groups=%s reason=%s",
            _principal(claims),
            claims.get("sub"),
            groups,
            reason,
        )
        return True, ""

    return False, reason


def _resolve_model(requested: str | None) -> tuple[str, bool]:
    """Map the client's model name onto a Bedrock model id / inference profile.

    Order: explicit MODEL_MAP entry -> already a Bedrock id (passed through) ->
    configured default.

    Returns (model_id, fallback_used). The flag matters for metering: falling back
    means the client is billed at a DIFFERENT model's rate than it asked for, and
    without recording that, an unmapped model name shows up in the invoice as an
    unexplained discrepancy rather than a configuration gap.
    """
    if not requested:
        return BEDROCK_MODEL_ID, False
    if requested in MODEL_MAP:
        return MODEL_MAP[requested], False
    # Already a Bedrock model id or inference profile (e.g. eu.anthropic.claude-…)
    if "anthropic." in requested:
        return requested, False
    return BEDROCK_MODEL_ID, True


def _to_bedrock_body(payload: dict, headers) -> dict:
    """Translate an Anthropic Messages request body into a Bedrock InvokeModel body.

    Everything not explicitly dropped is forwarded unchanged - the compatibility
    guide is explicit that body fields are an open list, and allowlisting breaks
    each new Claude Code capability on release. In particular cache_control
    markers and block-form system content are preserved as-is, or prompt caching
    silently stops working.
    """
    body = {k: v for k, v in payload.items() if k not in DROP_BODY_FIELDS}
    body["anthropic_version"] = BEDROCK_ANTHROPIC_VERSION

    if "max_tokens" not in body:
        body["max_tokens"] = DEFAULT_MAX_TOKENS

    if FORWARD_ANTHROPIC_BETA:
        beta = headers.get("anthropic-beta")
        if beta:
            body["anthropic_beta"] = [v.strip() for v in beta.split(",") if v.strip()]

    return body


def _bedrock_error_response(exc: Exception) -> tuple[int, dict]:
    """Map a boto exception to (status, Anthropic-shaped error body).

    The upstream message is preserved verbatim: Claude Code matches on error
    wording to decide whether to retry and drop a capability, so wrapping or
    rewording it breaks that recovery path.
    """
    status = 502
    message = str(exc)
    if isinstance(exc, ClientError):
        meta = exc.response.get("ResponseMetadata", {})
        status = meta.get("HTTPStatusCode", 502) or 502
        message = exc.response.get("Error", {}).get("Message", message)
        code = exc.response.get("Error", {}).get("Code", "")
        # Bedrock's throttling maps onto the Anthropic rate-limit status Claude
        # Code already knows how to back off from.
        if code in ("ThrottlingException", "TooManyRequestsException"):
            status = 429
    return status, {"type": "error", "error": {"type": "api_error", "message": message}}


@dataclass
class _RequestContext:
    """Everything metering needs about one in-flight request.

    Exists so the recording call site is one method instead of a dozen keyword
    arguments repeated at each of the five places a request can end (success,
    upstream error, malformed response, mid-stream failure, quota block). Those
    five paths drifting apart is exactly how spend records end up inconsistent.
    """

    claims: dict
    requested_model: str | None
    model_id: str
    fallback_used: bool
    session_id: str
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def finish(
        self,
        *,
        streamed: bool,
        usage: dict | None = None,
        usage_complete: bool = True,
        usage_source: str = "response",
        outcome: str = "ok",
        error_type: str = "",
        http_status: int = 200,
        stop_reason: str = "",
        request_id: str = "",
        invocation_latency_ms: int = 0,
        first_byte_latency_ms: int = 0,
    ) -> None:
        usage = usage or {}

        spend.record(
            claims=self.claims,
            requested_model=self.requested_model,
            model_id=self.model_id,
            fallback_used=self.fallback_used,
            streamed=streamed,
            usage=usage,
            usage_complete=usage_complete,
            usage_source=usage_source,
            outcome=outcome,
            error_type=error_type,
            http_status=http_status,
            stop_reason=stop_reason,
            request_id=request_id,
            session_id=self.session_id,
            # Bedrock's own measurement when it gave us one, otherwise the
            # gateway's wall clock. The two differ - ours includes the relay - so
            # usage_source records which is which.
            invocation_latency_ms=invocation_latency_ms or self.elapsed_ms,
            first_byte_latency_ms=first_byte_latency_ms,
        )

        # The human-readable line stays alongside the structured record: it is what
        # someone tailing the application log actually reads during an incident.
        log.info(
            "usage user=%s model=%s outcome=%s in=%s out=%s cache_r=%s cache_w=%s "
            "streamed=%s complete=%s session=%s",
            _principal(self.claims),
            self.model_id,
            outcome,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_read_input_tokens"),
            usage.get("cache_creation_input_tokens"),
            streamed,
            usage_complete,
            self.session_id or "-",
        )


def _error_type_of(exc: Exception) -> str:
    """Short, stable label for grouping errors on the dashboard."""
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "ClientError")
    return type(exc).__name__


def _response_meta(response: dict) -> tuple[str, int]:
    """Bedrock's request id and its own invocation latency, from InvokeModel.

    The latency header is Bedrock's measurement of the model call, which excludes
    the time this gateway spends relaying. Preferring it keeps the dashboard's
    latency figure about the model rather than about our own network.
    """
    meta = response.get("ResponseMetadata", {}) or {}
    request_id = meta.get("RequestId", "") or ""
    headers = meta.get("HTTPHeaders", {}) or {}
    try:
        latency = int(headers.get("x-amzn-bedrock-invocation-latency", 0) or 0)
    except (TypeError, ValueError):
        latency = 0
    return request_id, latency


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 is required for chunked transfer encoding, which is how the SSE
    # stream is framed. Every non-streaming response sets Content-Length.
    protocol_version = "HTTP/1.1"
    server_version = f"epdi-ai-gateway/{APP_VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802
        log.info("%s - %s", self.address_string(), fmt % args)

    # --- small helpers ---------------------------------------------------------

    @property
    def route(self) -> str:
        """Path without the query string: inference posts to /v1/messages?beta=true."""
        return self.path.split("?", 1)[0]

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Public names for admin.py, which is handed this handler and must not reach
    # into underscore-prefixed internals.
    def send_json(self, status: int, payload: dict) -> None:
        self._send_json(status, payload)

    def send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # These pages carry per-person spend data. Keeping them out of shared
        # caches is cheap insurance.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: str) -> None:
        """Serve a built asset from the UI bundle."""
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return

        content_type, _ = mimetypes.guess_type(path)
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))

        # Vite fingerprints asset filenames, so they are safe to cache hard. The
        # HTML shell is not fingerprinted and must never be cached, or a deploy
        # leaves browsers loading an old shell that asks for deleted asset names.
        if "/assets/" in path.replace(os.sep, "/"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-store")

        # Defence in depth for a page rendering names and spend figures. The bundle
        # is self-contained, so a strict policy costs nothing.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict | None:
        """Parse the request body as JSON, or answer with an error and return None."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"type": "error", "error": {"message": "empty request body"}})
            return None
        # Claude Code sends whole-file context, so the limit is generous; it only
        # exists so a malformed request cannot exhaust the task's memory.
        if length > 64 * 1024 * 1024:
            self._send_json(413, {"type": "error", "error": {"message": "request too large"}})
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json(400, {"type": "error", "error": {"message": "body is not valid JSON"}})
            return None

    # --- routing ---------------------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802
        # Claude Code warms the connection with HEAD /api/hello before inference.
        # Answering it is optional but cheap, and keeps the logs clean.
        if self.route in ("/api/hello", "/health", "/"):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        route = self.route

        # Checked before anything else so /admin/api/* is never shadowed by a
        # future route, and so admin.py owns the whole subtree including its
        # client-side routes.
        if admin.handle(self, _validate_raw_token):
            return

        if route == "/health":
            # Must stay trivial: if it depended on Bedrock or Cognito, an upstream
            # blip would make ECS cycle otherwise-healthy tasks.
            self._send_json(200, {"status": "ok"})
            return

        if route == "/logout":
            # This route exists only because Cognito requires the logout_uri of
            # its /logout endpoint to be one of the app client's registered
            # sign-out URLs, and cognito.tf registers "<gateway>/logout". The
            # sign-out itself has already happened at Cognito by the time the
            # browser arrives here; this page is the landing spot, nothing more.
            #
            # Deliberately unauthenticated: the visitor has just had their session
            # cleared, so requiring a token would make it unreachable by
            # definition. It carries no data, which is why that is safe.
            #
            # Before this existed the browser landed on the 404 JSON body, which
            # reads as a failed sign-out to anyone who is not holding the spec.
            self.send_html(
                200,
                "<!doctype html><html lang=\"en\"><head>"
                "<meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                "<title>Signed out</title></head>"
                "<body style=\"margin:0;min-height:100vh;display:flex;"
                "align-items:center;justify-content:center;background:#0f1115;"
                "color:#e6e6e6;font:16px/1.6 -apple-system,BlinkMacSystemFont,"
                "'Segoe UI',system-ui,sans-serif\">"
                "<main style=\"max-width:30rem;padding:2rem;text-align:center\">"
                "<h1 style=\"margin:0 0 .75rem;font-size:1.4rem;font-weight:600\">"
                "Signed out</h1>"
                "<p style=\"margin:0 0 1.25rem;color:#9aa4b2\">"
                "Your session has been closed. You can close this tab.</p>"
                "<p style=\"margin:0;color:#9aa4b2;font-size:.9rem\">"
                "To sign in again, run "
                "<code style=\"background:#1b1f27;padding:.15rem .4rem;"
                "border-radius:.25rem;color:#e6e6e6\">orga-login login</code>"
                "</p></main></body></html>",
            )
            return

        if route == "/":
            self._send_json(
                200,
                {
                    "service": "epdi-ai-gateway",
                    "version": APP_VERSION,
                    "api": "anthropic-messages",
                    "auth_configured": bool(COGNITO_JWKS_URI and COGNITO_ISSUER),
                    "default_model": BEDROCK_MODEL_ID,
                    "region": BEDROCK_REGION,
                    # Unauthenticated, so this is config state only - no figures.
                    # Enough to answer "is metering actually on" from a curl.
                    "metering_enabled": spend.ENABLED,
                    "dashboard_enabled": admin.ENABLED,
                },
            )
            return

        if route == "/whoami":
            ok, result = _validate_token(self.headers)
            if not ok:
                self._send_json(401, result)
                return
            groups = result.get("cognito:groups", [])
            entitled, reason = _may_consume_inference(result)
            self._send_json(
                200,
                {
                    "authenticated": True,
                    "sub": result.get("sub"),
                    "username": _principal(result),
                    "email": result.get("email"),
                    # Pool groups DO appear here, including for federated users once
                    # they have been added to one. What is absent is membership
                    # inherited from Identity Center - that needs group passthrough.
                    "groups": groups,
                    "token_use": result.get("token_use"),
                    # Deliberately kept ungated and self-explanatory: this endpoint is
                    # how a technician who is being refused finds out why, without
                    # anyone having to read server logs on their behalf.
                    "entitlements": {
                        "inference": entitled,
                        "inference_reason": reason or "ok",
                        "dashboard": bool(admin.ADMIN_GROUP) and admin.ADMIN_GROUP in groups,
                        "required_group": TECHNICIAN_GROUP or None,
                        "enforcing": ENFORCE_TECHNICIAN_GROUP,
                    },
                },
            )
            return

        if route == "/v1/models":
            # Model discovery for the /model picker. Claude Code only keeps ids
            # containing "claude" or "anthropic", which the Bedrock ids satisfy.
            ok, claims = _validate_token(self.headers)
            if not ok:
                self._send_json(401, claims)
                return

            # Same entitlement gate as /v1/messages. Without it, someone outside the
            # group could still enumerate which models the platform exposes - and
            # more practically, Claude Code would populate its /model picker for a
            # user who cannot actually send anything, which reads as a broken gateway
            # rather than as a permissions problem.
            allowed, reason = _may_consume_inference(claims)
            if not allowed:
                self._send_json(
                    403,
                    {
                        "type": "error",
                        "error": {"type": "permission_error", "message": f"Not authorised: {reason}"},
                    },
                )
                return

            ids = list(dict.fromkeys(list(MODEL_MAP.values()) + [BEDROCK_MODEL_ID]))
            self._send_json(
                200,
                {
                    "data": [
                        {"type": "model", "id": mid, "display_name": mid.split(".")[-1]}
                        for mid in ids
                    ]
                },
            )
            return

        self._send_json(404, {"type": "error", "error": {"message": f"not found: {route}"}})

    def do_POST(self) -> None:  # noqa: N802
        route = self.route
        if route != "/v1/messages":
            self._send_json(404, {"type": "error", "error": {"message": f"not found: {route}"}})
            return

        ok, claims = _validate_token(self.headers)
        if not ok:
            self._send_json(401, claims)
            return

        payload = self._read_json_body()
        if payload is None:
            return

        requested_model = payload.get("model")
        model_id, fallback_used = _resolve_model(requested_model)

        # ENTITLEMENT, checked before the budget and before Bedrock. A refusal here
        # is still recorded: an attempt by someone outside the technician group is
        # exactly the event an administrator wants to see in the dashboard, and it
        # costs nothing to store since no tokens were consumed.
        allowed, reason = _may_consume_inference(claims)
        if not allowed:
            spend.record(
                claims=claims,
                requested_model=requested_model,
                model_id=model_id,
                fallback_used=fallback_used,
                streamed=bool(payload.get("stream")),
                usage={},
                usage_complete=True,
                usage_source="authz",
                outcome="denied",
                error_type="not_entitled",
                http_status=403,
                session_id=self.headers.get("x-claude-code-session-id", ""),
            )
            log.warning("denied user=%s sub=%s: %s", _principal(claims), claims.get("sub"), reason)
            # permission_error, not rate_limit_error: Claude Code retries and backs
            # off on the latter, which would turn a permanent refusal into a loop of
            # pointless requests.
            self._send_json(
                403,
                {
                    "type": "error",
                    "error": {
                        "type": "permission_error",
                        "message": (
                            f"Not authorised to use this gateway: {reason}. "
                            "Ask your administrator to add you to the group."
                        ),
                    },
                },
            )
            return

        # BUDGET CHECK BEFORE THE CALL. This is the offer's 100% block, and it only
        # works in front of Bedrock - refusing after the fact would still have spent
        # the money. Fails open by design (see spend.check_quota): this is a cost
        # control, not a security control, so a DynamoDB outage must not stop sixty
        # technicians from working.
        decision = spend.check_quota(claims.get("sub") or "")
        if not decision.allowed:
            spend.record(
                claims=claims,
                requested_model=requested_model,
                model_id=model_id,
                fallback_used=fallback_used,
                streamed=bool(payload.get("stream")),
                usage={},
                usage_complete=True,
                usage_source="quota",
                outcome="blocked",
                error_type="quota_exceeded",
                http_status=429,
                session_id=self.headers.get("x-claude-code-session-id", ""),
            )
            log.warning(
                "quota block user=%s scope=%s spent=%s budget=%s",
                _principal(claims),
                decision.scope,
                decision.spent,
                decision.budget,
            )
            # 429 in the Anthropic error shape: Claude Code already knows how to
            # surface a rate-limit message, so the technician sees the reason rather
            # than a generic failure. The wording says budget, not rate, so it is
            # not mistaken for something that will clear on its own.
            self._send_json(
                429,
                {
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "message": (
                            f"Consumption budget reached ({decision.scope}): "
                            f"{decision.spent} of {decision.budget} "
                            f"{spend.pricing.CURRENCY} used. Contact your administrator."
                        ),
                    },
                },
            )
            return

        body = _to_bedrock_body(payload, self.headers)
        streaming = bool(payload.get("stream"))

        context = _RequestContext(
            claims=claims,
            requested_model=requested_model,
            model_id=model_id,
            fallback_used=fallback_used,
            session_id=self.headers.get("x-claude-code-session-id", ""),
        )

        if streaming:
            self._messages_stream(model_id, body, context)
        else:
            self._messages_once(model_id, body, context)

    # --- Bedrock calls ---------------------------------------------------------

    def _messages_once(self, model_id: str, body: dict, ctx: _RequestContext) -> None:
        """Non-streaming InvokeModel. Bedrock already returns an Anthropic Message
        object, so the response body is relayed as-is."""
        try:
            response = _get_bedrock_client().invoke_model(
                modelId=model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )
            raw = response["body"].read()
        except (ClientError, BotoCoreError) as exc:
            status, err = _bedrock_error_response(exc)
            log.warning(
                "invoke failed user=%s model=%s: %s", _principal(ctx.claims), model_id, exc
            )
            # Failures are recorded too, with zero tokens. A throttled request costs
            # nothing but is precisely what is needed when a technician reports that
            # things stopped working - and error rate per model is only visible if
            # the failures are counted somewhere.
            ctx.finish(
                streamed=False,
                outcome="error",
                error_type=_error_type_of(exc),
                http_status=status,
                usage_source="error",
            )
            self._send_json(status, err)
            return

        request_id, upstream_latency = _response_meta(response)

        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            ctx.finish(
                streamed=False,
                outcome="error",
                error_type="MalformedUpstreamResponse",
                http_status=502,
                request_id=request_id,
                usage_source="error",
            )
            self._send_json(502, {"type": "error", "error": {"message": "malformed upstream response"}})
            return

        ctx.finish(
            streamed=False,
            usage=message.get("usage", {}),
            usage_complete=True,
            usage_source="invoke_model",
            stop_reason=message.get("stop_reason", "") or "",
            request_id=request_id,
            invocation_latency_ms=upstream_latency,
        )
        payload = json.dumps(message).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _messages_stream(self, model_id: str, body: dict, ctx: _RequestContext) -> None:
        """Streaming InvokeModelWithResponseStream relayed as Anthropic SSE.

        Bedrock emits the same event objects as the Anthropic API
        (message_start, content_block_delta, ... ), so each chunk is re-framed as
        an SSE event rather than translated.
        """
        try:
            response = _get_bedrock_client().invoke_model_with_response_stream(
                modelId=model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )
        except (ClientError, BotoCoreError) as exc:
            # Nothing has been written yet, so a normal error response is still
            # possible - and Claude Code needs the upstream wording intact.
            status, err = _bedrock_error_response(exc)
            log.warning(
                "stream failed user=%s model=%s: %s", _principal(ctx.claims), model_id, exc
            )
            ctx.finish(
                streamed=True,
                outcome="error",
                error_type=_error_type_of(exc),
                http_status=status,
                usage_source="error",
            )
            self._send_json(status, err)
            return

        request_id, _ = _response_meta(response)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        # All socket writes go through this lock: the keep-alive thread and the
        # relay loop share the connection, and interleaved writes would corrupt
        # both the chunk framing and the SSE events.
        write_lock = threading.Lock()
        stop_ping = threading.Event()
        state = {"last_write": time.monotonic(), "broken": False}

        def emit(event_type: str, data: dict) -> None:
            frame = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            with write_lock:
                if state["broken"]:
                    return
                try:
                    self._write_chunk(frame)
                    state["last_write"] = time.monotonic()
                except OSError:
                    # Client hung up (Ctrl-C in Claude Code, ALB idle timeout).
                    state["broken"] = True

        def ping_loop() -> None:
            # Bedrock's event stream has no pings of its own, so during a long
            # thinking pause there is nothing to relay. Claude Code aborts a
            # stream that goes silent for 300s, so the gateway generates them.
            while not stop_ping.wait(1.0):
                if time.monotonic() - state["last_write"] >= PING_INTERVAL_SECONDS:
                    emit("ping", {"type": "ping"})

        pinger = threading.Thread(target=ping_loop, name="sse-ping", daemon=True)
        pinger.start()

        usage: dict = {}
        stop_reason = ""
        latency_ms = 0
        ttfb_ms = 0
        error_type = ""

        # Whether Bedrock's authoritative final metrics arrived. Only then are the
        # token counts trustworthy: an abandoned stream stops mid-flight and the
        # counts gathered from the events alone UNDERSTATE what was consumed. The
        # record carries this flag so those rows can be reported separately rather
        # than quietly deflating a total.
        saw_final_metrics = False

        try:
            for event in response.get("body", []):
                chunk = event.get("chunk")
                if not chunk:
                    continue
                try:
                    data = json.loads(chunk["bytes"])
                except json.JSONDecodeError:
                    continue

                etype = data.get("type", "")
                # message_start carries the input side (including the cache
                # counters), message_delta the output side and the stop reason.
                if etype == "message_start":
                    usage.update(data.get("message", {}).get("usage", {}))
                elif etype in ("message_delta", "message_stop"):
                    usage.update(data.get("usage", {}))
                    stop_reason = (
                        data.get("delta", {}).get("stop_reason")
                        or data.get("stop_reason")
                        or stop_reason
                    )

                    # Bedrock appends its own metrics to the final event. This is the
                    # ONLY place latency and time-to-first-byte are available for a
                    # streamed call - the previous version of this loop read the
                    # token counts out of here and discarded both timings.
                    metrics = data.get("amazon-bedrock-invocationMetrics") or {}
                    if metrics:
                        saw_final_metrics = True
                        usage["input_tokens"] = metrics.get(
                            "inputTokenCount", usage.get("input_tokens")
                        )
                        usage["output_tokens"] = metrics.get(
                            "outputTokenCount", usage.get("output_tokens")
                        )
                        # The cache counters appear here under different names than
                        # in the Anthropic usage object. setdefault, not assignment:
                        # the event names are what the rest of the pipeline keys on,
                        # so they win when both are present.
                        if metrics.get("cacheReadInputTokenCount") is not None:
                            usage.setdefault(
                                "cache_read_input_tokens",
                                metrics.get("cacheReadInputTokenCount"),
                            )
                        if metrics.get("cacheWriteInputTokenCount") is not None:
                            usage.setdefault(
                                "cache_creation_input_tokens",
                                metrics.get("cacheWriteInputTokenCount"),
                            )
                        latency_ms = int(metrics.get("invocationLatency") or 0)
                        ttfb_ms = int(metrics.get("firstByteLatency") or 0)

                emit(etype or "message", data)
                if state["broken"]:
                    break
        except (ClientError, BotoCoreError) as exc:
            # Mid-stream failure: headers are already sent, so the only way to
            # report it is an SSE error event, which is what the Anthropic API
            # does too.
            log.warning(
                "stream error user=%s model=%s: %s", _principal(ctx.claims), model_id, exc
            )
            error_type = _error_type_of(exc)
            _, err = _bedrock_error_response(exc)
            emit("error", err)
        finally:
            stop_ping.set()
            pinger.join(timeout=2)
            with write_lock:
                if not state["broken"]:
                    try:
                        # Zero-length chunk terminates the chunked response.
                        self._write_chunk("")
                    except OSError:
                        pass

            if error_type:
                outcome, usage_source = "error", "error"
            elif state["broken"]:
                # The client hung up (Ctrl-C in Claude Code, or the ALB idle
                # timeout). Tokens were consumed, so this is not an error - but the
                # count is partial, which usage_complete below records.
                outcome, usage_source = "ok", "stream_aborted"
            else:
                outcome = "ok"
                usage_source = "stream_metrics" if saw_final_metrics else "stream_events"

            ctx.finish(
                streamed=True,
                usage=usage,
                usage_complete=saw_final_metrics and not state["broken"],
                usage_source=usage_source,
                outcome=outcome,
                error_type=error_type,
                stop_reason=stop_reason,
                request_id=request_id,
                invocation_latency_ms=latency_ms,
                first_byte_latency_ms=ttfb_ms,
            )

    def _write_chunk(self, text: str) -> None:
        """Write one HTTP chunked-transfer chunk and flush it.

        Flushing matters: Claude Code reads the stream as it arrives and stalls
        behind a gateway that buffers.
        """
        data = text.encode("utf-8")
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
        if data:
            self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

def _serve() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True

    # Metering writer runs in the background for the life of the process.
    spend.start()

    def _shutdown(signum, _frame) -> None:
        log.info("received signal %s, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info(
        "gateway listening on :%d (version=%s, auth=%s, default_model=%s, region=%s, "
        "metering=%s, dashboard=%s)",
        PORT,
        APP_VERSION,
        "on" if (COGNITO_JWKS_URI and COGNITO_ISSUER) else "off",
        BEDROCK_MODEL_ID,
        BEDROCK_REGION,
        "on" if spend.ENABLED else "off",
        "on" if admin.ENABLED else "off",
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        # Drain the queue BEFORE the process exits. ECS sends SIGTERM and allows 30
        # seconds; without this every deploy silently discards the spend records of
        # the requests that were in flight, which is the least visible way there is
        # to lose a client's money.
        spend.shutdown()
        log.info("stopped")


if __name__ == "__main__":
    try:
        _serve()
    except Exception as exc:  # pragma: no cover
        log.exception("fatal: %s", exc)
        sys.exit(1)
