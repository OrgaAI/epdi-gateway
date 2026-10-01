"""Rate table and cost arithmetic for the consumption gateway.

The gateway prices every request itself rather than reading a price at query
time, and stores the RATE IT USED alongside the amount. That is the whole point:
an invoice justification has to reproduce what was charged when the request ran,
so a later AWS price change must never retroactively rewrite history.

WHERE THE RATES COME FROM. Terraform passes them in as SPEND_PRICING, a JSON
object keyed by the RESOLVED Bedrock model id (the inference profile the gateway
actually called, not the alias the client asked for). There is no Bedrock API that
returns model prices; the general AWS Price List API does carry them but answers
in SKUs and usageTypes that do not map onto inference-profile ids without a
hand-written table. So this is a deliberate manual input, versioned by
SPEND_PRICING_VERSION.

A MODEL WITH NO RATE IS NOT FREE. When a model is absent from the table the cost
comes back as zero with rate_missing = True, and every consumer is expected to
surface that separately rather than folding it into a total. A visible gap is a
bug report; a plausible wrong number is a billing dispute.

DECIMAL, NOT FLOAT. Rates are fractions of a cent per token and get summed across
hundreds of thousands of requests. Decimal keeps that exact and, just as
importantly, serialises to the string form DynamoDB's atomic ADD expects - float
would introduce representation error on the way in and again on every read.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

log = logging.getLogger("gateway.pricing")

# Rates are quoted per million tokens, which is how every model provider
# publishes them. Dividing here keeps the tfvars readable.
TOKENS_PER_UNIT = Decimal(1_000_000)

# Ten decimal places. A single cheap request can cost ~1e-7, so rounding at cent
# precision would floor most individual requests to zero and lose the total.
MONEY_PLACES = Decimal("0.0000000001")

ZERO = Decimal(0)

PRICING_VERSION = os.environ.get("SPEND_PRICING_VERSION", "unset")
CURRENCY = os.environ.get("SPEND_CURRENCY", "USD")


@dataclass(frozen=True)
class Rates:
    """Per-million-token rates for one model, split by token type.

    Five rates rather than two because prompt caching is priced asymmetrically:
    a cache read is a fraction of the normal input rate, while a cache write
    costs MORE than uncached input. Claude Code caches aggressively, so
    collapsing these into a single input rate does not lose precision - it makes
    the amount wrong.

    cache_write vs cache_write_1h is the same argument one level down. Anthropic
    prices the two cache TTLs differently - the 1-hour write is 1.6x the
    5-minute one on every Claude model in the rate card - and Bedrock reports
    them separately in usage.cache_creation. Pricing both at the 5-minute rate
    is a silent undercount on exactly the workload this gateway exists to meter,
    since a coding session is long enough for the 1-hour TTL to be the sensible
    choice for a client.
    """

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal
    cache_write_1h: Decimal


@dataclass(frozen=True)
class Cost:
    """The money half of a spend record: rates applied, amounts produced."""

    rate_missing: bool
    rate_input: Decimal
    rate_output: Decimal
    rate_cache_read: Decimal
    rate_cache_write: Decimal
    rate_cache_write_1h: Decimal
    cost_input: Decimal
    cost_output: Decimal
    cost_cache_read: Decimal
    cost_cache_write: Decimal
    cost_cache_write_1h: Decimal
    cost_total: Decimal

    def as_record_fields(self) -> dict:
        """Flatten into the spend-record keys, as floats for JSON transport.

        float is safe HERE and only here: these values are already final, and
        json has no Decimal type. Accumulation happens in DynamoDB, in decimal.
        """
        return {
            "pricing_version": PRICING_VERSION,
            "currency": CURRENCY,
            "rate_missing": self.rate_missing,
            "rate_input": float(self.rate_input),
            "rate_output": float(self.rate_output),
            "rate_cache_read": float(self.rate_cache_read),
            "rate_cache_write": float(self.rate_cache_write),
            "rate_cache_write_1h": float(self.rate_cache_write_1h),
            "cost_input": float(self.cost_input),
            "cost_output": float(self.cost_output),
            "cost_cache_read": float(self.cost_cache_read),
            "cost_cache_write": float(self.cost_cache_write),
            "cost_cache_write_1h": float(self.cost_cache_write_1h),
            "cost_total": float(self.cost_total),
        }


def _to_decimal(value, field: str, model: str) -> Decimal:
    """Coerce a JSON number to Decimal via str().

    Decimal(str(x)) rather than Decimal(x): the latter faithfully reproduces the
    float's binary error, so a rate of 3.0 arrives as
    3.00000000000000266453525910037569701671600341796875 and then every derived
    amount carries that noise into the stored record.
    """
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        log.warning("pricing: ignoring non-numeric %s for %s: %r", field, model, value)
        return ZERO


def _load_table() -> dict[str, Rates]:
    """Parse SPEND_PRICING. An unset or malformed table is not fatal.

    Metering must keep working when pricing is missing - token accounting is the
    part that cannot be reconstructed after the fact, while a rate can be applied
    retroactively from the stored token counts. So a bad table degrades to
    rate_missing on every record instead of taking the gateway down.
    """
    raw = os.environ.get("SPEND_PRICING", "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("pricing: SPEND_PRICING is not valid JSON, treating as empty: %s", exc)
        return {}

    if not isinstance(parsed, dict):
        log.error("pricing: SPEND_PRICING must be a JSON object, got %s", type(parsed).__name__)
        return {}

    table: dict[str, Rates] = {}
    for model, rates in parsed.items():
        if not isinstance(rates, dict):
            log.warning("pricing: skipping %s, expected an object of rates", model)
            continue
        cache_write = _to_decimal(rates.get("cache_write", 0), "cache_write", model)
        # Falls back to the 5-minute rate rather than to zero. Zero would price
        # 1-hour cache writes as free, which is a worse error than pricing them
        # too low: free looks like a working total, whereas the 5m rate at least
        # errs in a bounded, explainable direction.
        raw_1h = rates.get("cache_write_1h")
        cache_write_1h = (
            _to_decimal(raw_1h, "cache_write_1h", model)
            if raw_1h is not None
            else cache_write
        )
        table[model] = Rates(
            input=_to_decimal(rates.get("input", 0), "input", model),
            output=_to_decimal(rates.get("output", 0), "output", model),
            cache_read=_to_decimal(rates.get("cache_read", 0), "cache_read", model),
            cache_write=cache_write,
            cache_write_1h=cache_write_1h,
        )
    return table


TABLE: dict[str, Rates] = _load_table()

# True when no rates were supplied at all, which is the expected state before
# someone fills in the table. Distinct from "this one model is missing" so the
# dashboard can explain the difference: unconfigured vs an unmapped model.
UNCONFIGURED = not TABLE


def _money(tokens: int, rate: Decimal) -> Decimal:
    if not tokens or rate == ZERO:
        return ZERO
    return ((Decimal(tokens) / TOKENS_PER_UNIT) * rate).quantize(MONEY_PLACES)


def compute(
    model_id: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> Cost:
    """Price one request. Never raises - metering must not break inference.

    cache_write_tokens is the TOTAL cache write reported for the request, and
    cache_write_1h_tokens is the 1-hour-TTL SUBSET of it. Keeping the total
    rather than the 5-minute remainder is deliberate: it is the number Bedrock
    reports directly (cacheWriteInputTokenCount), so a record stays reconcilable
    against AWS even when the subset is unavailable, which is the case for any
    stream that ended before message_start was parsed.
    """
    rates = TABLE.get(model_id)

    if rates is None:
        # Logged at debug, not warning: with an empty table this would otherwise
        # fire on every single request and drown the log. The dashboard reports
        # the condition from the records themselves, which is where an operator
        # will actually notice it.
        log.debug("pricing: no rates for %s, recording zero cost", model_id)
        return Cost(
            rate_missing=True,
            rate_input=ZERO,
            rate_output=ZERO,
            rate_cache_read=ZERO,
            rate_cache_write=ZERO,
            rate_cache_write_1h=ZERO,
            cost_input=ZERO,
            cost_output=ZERO,
            cost_cache_read=ZERO,
            cost_cache_write=ZERO,
            cost_cache_write_1h=ZERO,
            cost_total=ZERO,
        )

    # Split the total into the two TTLs. Clamped at zero because the subset
    # arrives from a different event than the total on a streamed call, and a
    # truncated stream can leave the pair inconsistent - which must not turn into
    # a negative charge.
    tokens_1h = max(0, min(cache_write_1h_tokens, cache_write_tokens))
    tokens_5m = max(0, cache_write_tokens - tokens_1h)

    cost_input = _money(input_tokens, rates.input)
    cost_output = _money(output_tokens, rates.output)
    cost_cache_read = _money(cache_read_tokens, rates.cache_read)
    cost_cache_write = _money(tokens_5m, rates.cache_write)
    cost_cache_write_1h = _money(tokens_1h, rates.cache_write_1h)

    return Cost(
        rate_missing=False,
        rate_input=rates.input,
        rate_output=rates.output,
        rate_cache_read=rates.cache_read,
        rate_cache_write=rates.cache_write,
        rate_cache_write_1h=rates.cache_write_1h,
        cost_input=cost_input,
        cost_output=cost_output,
        cost_cache_read=cost_cache_read,
        cost_cache_write=cost_cache_write,
        cost_cache_write_1h=cost_cache_write_1h,
        cost_total=(
            cost_input
            + cost_output
            + cost_cache_read
            + cost_cache_write
            + cost_cache_write_1h
        ),
    )


def known_models() -> list[str]:
    """Models the table can price. Shown in the dashboard's configuration panel."""
    return sorted(TABLE)


def describe() -> dict:
    """Pricing configuration, for the dashboard's meta payload."""
    return {
        "version": PRICING_VERSION,
        "currency": CURRENCY,
        "configured": not UNCONFIGURED,
        "models": known_models(),
    }
