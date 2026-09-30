"""Event-driven metering for direct Amazon Bedrock Runtime calls.

CloudWatch Logs invokes this Lambda through a subscription on the account's
Bedrock model-invocation log group. Each record is attributed to a quota
subject (a broker-vended user via the RoleSessionName, or a workload via its
application-inference-profile ARN), priced per dimension from the model
price SSM parameter, and applied in one transaction to the subject's UTC-day
ledger row and its per-model ledger row. Subjects with rate limits also get
a per-minute counter. Every applied record re-evaluates the subject's quotas
and blocks or warns through the automatic path.

CloudWatch Logs delivery is at-least-once. A DynamoDB transaction creates a
request-id marker and increments the aggregates together, so retries cannot
double-charge a subject or lose an increment between separate writes.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from bedrock_spend_controls.quota_periods import (
    PERIODS,
    RATE_PERIOD,
    aggregate_daily_rows,
    calendar_windows,
    default_thresholds,
    evaluate_limits,
    evaluate_model_budgets,
    limits_from_item as _layer_limits_from_item,
    merge_evaluations,
    model_budgets_from_item,
    model_ledger_subject,
    quota_reason,
    rate_limits_enabled,
    rate_limits_from_item,
    rate_row_key,
    rate_usage_from_item,
)
from bedrock_spend_controls.row_enforcement import automatic_owned

MICRO = 1_000_000
METRICS_NAMESPACE = os.environ.get(
    "METRICS_NAMESPACE", "BedrockSpendControls"
)
_ASSUMED_ROLE_ARN = re.compile(
    r":assumed-role/(?P<role>[^/]+)/(?P<session>[\w+=,.@-]+)$"
)
_SERIALIZER = TypeSerializer()

# Price dimensions. Token dimensions are USD per million tokens; ``image``
# is USD per generated image. The names are the price-catalog vocabulary
# shared with cdk/config/model-pricing.json, the pricing resolver, and the
# SSM parameter, whose entries spell them as the ``*_per_mtok`` /
# ``per_image`` keys below.
TOKEN_DIMENSIONS = ("input", "output", "cache_read", "cache_write")
UNIT_DIMENSIONS = ("image",)
PRICE_DIMENSIONS = TOKEN_DIMENSIONS + UNIT_DIMENSIONS
_RATE_KEYS = {
    "input": "input_per_mtok",
    "output": "output_per_mtok",
    "cache_read": "cache_read_per_mtok",
    "cache_write": "cache_write_per_mtok",
    "image": "per_image",
}

# No built-in model prices: the SSM parameter written at deploy time is the
# source. A cold start that cannot read it (and has no ``MODEL_PRICES_JSON``
# test override) prices every model at the conservative fallback, which the
# FallbackPricedRequests alarm surfaces.
_DEFAULT_PRICES: dict[str, dict[str, float]] = {}
_DEFAULT_FALLBACK_PRICE: dict[str, float] = {"input": 15.00, "output": 75.00}


@dataclass(frozen=True)
class InvocationUsage:
    request_id: str
    session_name: str | None
    model_id: str
    input_tokens: int
    output_tokens: int
    occurred_at: datetime
    workload_id: str | None = None
    workload_name: str | None = None
    # ``cache_read_tokens`` and ``cache_write_tokens`` come straight from the
    # invocation-log record (``input.cacheReadInputTokenCount`` /
    # ``input.cacheWriteInputTokenCount``). ``images`` is the number of
    # generated images when the record carries it.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    images: int = 0

    @property
    def window(self) -> str:
        return self.occurred_at.strftime("%Y-%m-%d")

    @property
    def dimension_counts(self) -> dict[str, int]:
        """Non-zero usage per price dimension, in catalog vocabulary."""
        counts = {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_write": self.cache_write_tokens,
            "image": self.images,
        }
        return {name: count for name, count in counts.items() if count > 0}


@dataclass(frozen=True)
class PricedInvocation:
    """Outcome of pricing one invocation against the catalog."""

    cost_micro: int
    price_source: str  # snapshot | base-model | fallback
    # Dimensions the record carried but the resolved price had no rate for.
    # Non-empty means the fallback rate was used for that dimension (when the
    # fallback has one) or the dimension went unpriced; either way the
    # request is flagged for repair rather than silently under-counted.
    missing_dimensions: tuple[str, ...] = ()

    @property
    def unpriced(self) -> bool:
        return bool(self.missing_dimensions) or self.price_source == "fallback"


_dynamodb_resource = None
_dynamodb_client = None
_sns_client = None
_ssm_client = None


def _resources():
    global _dynamodb_resource, _dynamodb_client, _sns_client, _ssm_client
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    if _dynamodb_client is None:
        _dynamodb_client = boto3.client("dynamodb")
    if _sns_client is None:
        _sns_client = boto3.client("sns")
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _dynamodb_resource, _dynamodb_client, _sns_client, _ssm_client


def _decode_subscription(event: dict) -> list[dict]:
    encoded = event.get("awslogs", {}).get("data")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("expected a CloudWatch Logs subscription event")
    payload = json.loads(gzip.decompress(base64.b64decode(encoded)))
    if payload.get("messageType") == "CONTROL_MESSAGE":
        return []
    return list(payload.get("logEvents", []))


def _non_negative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _timestamp(record: dict, log_event: dict) -> datetime:
    raw = record.get("timestamp")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except ValueError:
            pass
    millis = _non_negative_int(log_event.get("timestamp"))
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)


def _workload_profiles() -> dict[str, dict[str, str]]:
    """Attribution map: application-inference-profile ARN -> workload.

    Built from WORKLOAD_PROFILES_JSON ({name: {workload_id, profile_arn,
    model}}). The invocation-log ``modelId`` field carries the profile ARN,
    which makes it the attribution key for apps calling bedrock-runtime with
    their own credentials.
    """
    raw = os.environ.get("WORKLOAD_PROFILES_JSON")
    if not raw:
        return {}
    by_arn: dict[str, dict[str, str]] = {}
    for name, entry in json.loads(raw).items():
        profile_arn = str(entry.get("profile_arn", ""))
        if not profile_arn:
            continue
        by_arn[profile_arn] = {
            "workload_id": str(entry.get("workload_id") or f"workload:{name}"),
            "model": str(entry.get("model", "")),
            "name": str(name),
        }
    return by_arn


def _image_count(record: dict, output_data: dict) -> int:
    """Generated-image count from an invocation-log record, when present.

    Image models do not report token counts. With image data delivery
    disabled (this stack's managed configuration) the record has no
    ``outputBodyJson`` either, so the count is only available when the
    account enables image delivery or Bedrock adds an explicit counter.
    Checked in order: an explicit ``output.outputImageCount``, then the
    length of ``output.outputBodyJson.images`` (the Nova Canvas / Stability
    response shape). Absence yields 0 and the request is flagged as
    unpriced downstream instead of being silently free.
    """
    explicit = output_data.get("outputImageCount")
    if explicit is not None:
        return _non_negative_int(explicit)
    body = output_data.get("outputBodyJson")
    if isinstance(body, dict):
        images = body.get("images")
        if isinstance(images, list):
            return len(images)
    return 0


def _is_image_model(model_id: str) -> bool:
    """Heuristic for models whose on-demand price is per image.

    Used only to decide whether a record with no token counts is a
    generation we must still meter (and flag) rather than a metadata-less
    duplicate we should skip. Pricing itself never uses this: it is driven
    by the dimensions present in the catalog entry.
    """
    tail = model_id.rsplit("/", 1)[-1].lower()
    return any(
        marker in tail
        for marker in (
            "canvas", "titan-image", "stability.", "sd3", "stable-",
        )
    )


def _parse_invocation(
    log_event: dict,
    expected_role_name: str,
    workload_profiles: dict[str, dict[str, str]] | None = None,
) -> InvocationUsage | None:
    try:
        record = json.loads(log_event.get("message", ""))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None

    input_data = record.get("input")
    output_data = record.get("output")
    input_data = input_data if isinstance(input_data, dict) else {}
    output_data = output_data if isinstance(output_data, dict) else {}
    model_id = str(record.get("modelId") or "unknown")
    has_token_metadata = (
        "inputTokenCount" in input_data
        or "inputBodyTokenCount" in input_data
        or "outputTokenCount" in output_data
        or "outputBodyTokenCount" in output_data
    )
    images = _image_count(record, output_data)
    if not has_token_metadata and images == 0 and not _is_image_model(model_id):
        # The Responses API logs a second, metadata-less record for the
        # same invocation alongside the token-bearing one. A record with
        # no token metadata on either side has nothing to meter; skipping
        # it also keeps request counts honest. Image models are the
        # exception: their records carry no token counts, so they are
        # metered as one image-generation request and flagged for repair.
        return None
    input_tokens = _non_negative_int(
        input_data.get(
            "inputTokenCount", input_data.get("inputBodyTokenCount", 0)
        )
    )
    output_tokens = _non_negative_int(
        output_data.get(
            "outputTokenCount", output_data.get("outputBodyTokenCount", 0)
        )
    )
    # Prompt-cache counters live beside inputTokenCount on the input side;
    # a top-level placement is accepted as well.
    cache_read_tokens = _non_negative_int(
        input_data.get(
            "cacheReadInputTokenCount", record.get("cacheReadInputTokenCount", 0)
        )
    )
    cache_write_tokens = _non_negative_int(
        input_data.get(
            "cacheWriteInputTokenCount",
            record.get("cacheWriteInputTokenCount", 0),
        )
    )
    request_id = str(record.get("requestId") or log_event.get("id") or "")
    if not request_id:
        request_id = hashlib.sha256(
            str(log_event.get("message", "")).encode("utf-8")
        ).hexdigest()

    # Workload attribution comes first: profile-routed traffic authenticates
    # with the customer's own principal, so the vended-role check below can
    # never claim it, and a vended session invoking a workload profile is
    # deterministically attributed to the workload that pays for it.
    workload = (workload_profiles or {}).get(model_id)
    if workload is not None:
        return InvocationUsage(
            request_id=request_id,
            session_name=None,
            # Price and report by the profile's underlying model: the
            # profile ARN itself has no price entry.
            model_id=workload["model"] or model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            occurred_at=_timestamp(record, log_event),
            workload_id=workload["workload_id"],
            workload_name=workload["name"],
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            images=images,
        )

    identity = record.get("identity")
    arn = identity.get("arn", "") if isinstance(identity, dict) else ""
    match = _ASSUMED_ROLE_ARN.search(str(arn))
    if not match or match.group("role") != expected_role_name:
        return None

    return InvocationUsage(
        request_id=request_id,
        session_name=match.group("session"),
        model_id=_normalized_model_id(model_id),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        occurred_at=_timestamp(record, log_event),
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        images=images,
    )


def _normalized_model_id(model_id: str) -> str:
    """Reduce a Bedrock resource ARN in ``modelId`` to its trailing ID.

    Converse and InvokeModel log whatever identifier the caller passed,
    but the Responses API logs the resolved system inference-profile ARN
    (``arn:...:inference-profile/us.openai....``). Application inference
    profiles are attributed to workloads before this runs, so only system
    profiles and foundation-model ARNs reach here; reducing them to the
    trailing ID lets pricing and per-model reporting reuse the plain-ID
    paths (including the cross-region ``us.``/``eu.`` base-model lookup).
    """
    if model_id.startswith("arn:") and "/" in model_id:
        return model_id.rsplit("/", 1)[1]
    return model_id


def _rates_from_entry(price: dict) -> dict[str, float]:
    """Normalize one catalog/SSM price entry into ``{dimension: rate}``.

    Entries carry ``input_per_mtok`` and ``output_per_mtok`` (required) and
    optionally ``cache_read_per_mtok`` / ``cache_write_per_mtok`` /
    ``per_image``. Unknown keys are ignored.
    """
    rates: dict[str, float] = {}
    for name, key in _RATE_KEYS.items():
        if price.get(key) is not None:
            rates[name] = float(price[key])
    if "input" not in rates or "output" not in rates:
        raise ValueError("price entry needs input and output token rates")
    return rates


def _prices() -> dict[str, dict[str, float]]:
    # MODEL_PRICES_JSON is a test/local override; the stack never sets it.
    raw = os.environ.get("MODEL_PRICES_JSON")
    if not raw:
        return {model: dict(rates) for model, rates in _DEFAULT_PRICES.items()}
    return {
        model_id: _rates_from_entry(price)
        for model_id, price in json.loads(raw).items()
    }


def _fallback_price() -> dict[str, float]:
    raw = os.environ.get("MODEL_FALLBACK_PRICE_JSON")
    if not raw:
        return dict(_DEFAULT_FALLBACK_PRICE)
    return _rates_from_entry(json.loads(raw))


# Prices live in an SSM parameter written at deploy time and rewritten by
# the scheduled price resolver. The last good parameter value is kept
# across failed refreshes, so the fallback price (or the ``MODEL_PRICES_JSON``
# test/local override) is only used before the first successful read: a
# Parameter Store outage can never stop metering, and it is alarmed through
# ``FallbackPricedRequests`` because every model is then fallback-priced.
_PRICE_CACHE_TTL_SECONDS = 900
_PRICE_RETRY_SECONDS = 60
_price_cache: dict[str, Any] = {"next_attempt_at": 0.0, "value": None}


def _parameter_prices(
    ssm, now_epoch: float
) -> tuple[dict[str, dict[str, float]], dict[str, float]] | None:
    parameter_name = os.environ.get("PRICES_PARAMETER_NAME")
    if not parameter_name or ssm is None:
        return None
    cache = _price_cache
    if now_epoch >= cache["next_attempt_at"]:
        try:
            value = json.loads(
                ssm.get_parameter(Name=parameter_name)["Parameter"]["Value"]
            )
            models = {
                model_id: _rates_from_entry(price)
                for model_id, price in value["models"].items()
            }
            fallback = _rates_from_entry(value["fallback"])
            cache["value"] = (models, fallback)
            cache["next_attempt_at"] = now_epoch + _PRICE_CACHE_TTL_SECONDS
        except Exception as exc:  # noqa: BLE001 - availability over freshness
            cache["next_attempt_at"] = now_epoch + _PRICE_RETRY_SECONDS
            print(
                json.dumps(
                    {
                        "level": "warning",
                        "message": (
                            "Model price parameter unavailable; using the "
                            "last good value"
                            if cache["value"] is not None
                            else "Model price parameter unavailable; using "
                            "the conservative fallback price"
                        ),
                        "parameter": parameter_name,
                        "error": type(exc).__name__,
                    }
                )
            )
    return cache["value"]


# Cross-Region/geographic inference profiles log profile IDs such as
# "us.anthropic....". The Pricing API catalogs base model names, so an
# unmatched profile ID resolves to its base model before the conservative
# fallback. An explicit profile entry (for example a geographic uplift)
# always wins over this derivation.
_PROFILE_PREFIXES = frozenset(
    {"us", "eu", "apac", "jp", "au", "ca", "sa", "global", "us-gov"}
)


def _price_for(
    prices: dict[str, dict[str, float]],
    fallback: dict[str, float],
    model_id: str,
) -> tuple[dict[str, float], str]:
    """Return ({dimension: rate}, price_source) for one model ID."""
    exact = prices.get(model_id)
    if exact is not None:
        return exact, "snapshot"
    prefix, separator, base_model = model_id.partition(".")
    if separator and prefix in _PROFILE_PREFIXES:
        base = prices.get(base_model)
        if base is not None:
            return base, "base-model"
    return fallback, "fallback"


def _round_up_micro(usd: float) -> int:
    micro = int(usd * MICRO)
    return micro + 1 if usd * MICRO > micro else micro


def _price_invocation(
    prices: dict[str, dict[str, float]],
    fallback: dict[str, float],
    usage: InvocationUsage,
) -> PricedInvocation:
    """Price every dimension the record carries.

    ``cost = sum(count * rate) / 1e6`` for token dimensions plus
    ``count * rate`` for per-unit dimensions, rounded up to micro-USD. A
    dimension present in the log with no rate in the resolved entry is
    priced at the fallback's rate for that dimension when one exists
    (conservative, like an unknown model) and recorded in
    ``missing_dimensions`` either way so the ledger can be repaired later.
    Nothing is ever silently priced at zero.
    """
    rates, price_source = _price_for(prices, fallback, usage.model_id)
    usd = 0.0
    missing: list[str] = []
    for name, count in usage.dimension_counts.items():
        rate = rates.get(name)
        if rate is None:
            missing.append(name)
            rate = fallback.get(name)
            if rate is None:
                continue
        if name in TOKEN_DIMENSIONS:
            usd += count * rate / 1_000_000
        else:
            usd += count * rate
    return PricedInvocation(
        cost_micro=_round_up_micro(usd),
        price_source=price_source,
        missing_dimensions=tuple(missing),
    )


def _av(value: Any) -> dict:
    return _SERIALIZER.serialize(value)


def _ttl_epoch(occurred_at: datetime) -> int:
    keep_days = int(os.environ.get("USAGE_RETENTION_DAYS", "35"))
    # Late CloudWatch delivery must not create an already-expired request
    # marker or daily row. Retain from processing time when it is later.
    processed_at = datetime.now(timezone.utc)
    anchor = max(occurred_at.astimezone(timezone.utc), processed_at)
    return int(anchor.timestamp()) + keep_days * 86400


def _deployment_thresholds() -> list[dict]:
    """Thresholds a row without its own list resolves to (deploy default)."""
    return default_thresholds(float(os.environ.get("WARN_THRESHOLD", "0.8")))


def limits_from_item(item: dict) -> dict:
    return _layer_limits_from_item(
        item, default_thresholds_list=_deployment_thresholds()
    )


def _apply_rate_usage(
    usage_table,
    user_id: str,
    usage: InvocationUsage,
) -> dict[str, object]:
    """Increment the subject's per-minute counter and return the new totals.

    Rows live in the usage table under ``RATE#<user>`` / ``<UTC minute>``
    with a short TTL. Counted per *occurrence* minute, so a late-delivered
    record increments the minute it happened in, never the current one.
    Only subjects with a positive rpm/tpm pay for this write; the increment
    itself is not part of the idempotent transaction because a duplicate
    delivery is already detected before this runs.
    """
    key = rate_row_key(user_id, usage.occurred_at)
    tokens = usage.input_tokens + usage.output_tokens
    response = usage_table.update_item(
        Key=key,
        UpdateExpression=(
            "ADD requests :one, tokens :t "
            "SET expires_at = if_not_exists(expires_at, :ttl)"
        ),
        ExpressionAttributeValues={
            ":one": 1,
            ":t": tokens,
            # Counter rows only matter for the minute they describe plus a
            # grace period for late evaluation; keep them briefly.
            ":ttl": int(usage.occurred_at.timestamp()) + 15 * 60,
        },
        ReturnValues="ALL_NEW",
    )
    return rate_usage_from_item(response.get("Attributes"), usage.occurred_at)


def _apply_usage(
    client,
    usage_table_name: str,
    user_id: str,
    usage: InvocationUsage,
    cost_micro: int,
    *,
    missing_dimensions: tuple[str, ...] = (),
) -> bool:
    """Apply one invocation exactly once. Returns False for a duplicate.

    One transaction writes the ``REQUEST#<id>`` idempotency marker, the
    subject's daily row, and the subject's per-model daily row
    (``<subject>#model#<model_id>``). The per-model row feeds model-scoped
    budgets and is written unconditionally (not only when a model budget
    exists) so a budget added mid-period includes usage already recorded,
    matching how weekly/monthly limits behave. This doubles ledger writes per
    request; see DEPLOYMENT.md for the cost note.

    Beyond the quota-bearing counters, the daily rows accumulate the
    non-limited priced dimensions (cache tokens, images) and an
    ``unpriced_requests`` count plus a ``missing_dimensions`` string set so
    an operator can find days whose USD figure is known to be incomplete
    and repair them later ("flag now, repair later"). Aggregates are never
    repriced automatically.
    """
    ttl = _ttl_epoch(usage.occurred_at)
    marker_key = {
        "user_id": _av(f"REQUEST#{usage.request_id}"),
        "window": _av("EVENT"),
    }
    update_expression = (
        "ADD cost_micro :c, input_tokens :i, output_tokens :o, "
        "requests :one, cache_read_tokens :cr, cache_write_tokens :cw, "
        "images :img, unpriced_requests :unpriced"
    )
    values = {
        ":c": _av(cost_micro),
        ":i": _av(usage.input_tokens),
        ":o": _av(usage.output_tokens),
        ":one": _av(1),
        ":cr": _av(usage.cache_read_tokens),
        ":cw": _av(usage.cache_write_tokens),
        ":img": _av(usage.images),
        ":unpriced": _av(1 if missing_dimensions else 0),
        ":ttl": _av(ttl),
    }
    if missing_dimensions:
        # DynamoDB string set: ADD on a set unions the members.
        update_expression += ", missing_dimensions :md"
        values[":md"] = {"SS": sorted(set(missing_dimensions))}
    update_expression += " SET expires_at = if_not_exists(expires_at, :ttl)"

    def ledger_update(subject: str) -> dict:
        return {
            "Update": {
                "TableName": usage_table_name,
                "Key": {
                    "user_id": _av(subject),
                    "window": _av(usage.window),
                },
                "UpdateExpression": update_expression,
                "ExpressionAttributeValues": values,
            }
        }

    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": usage_table_name,
                        "Item": {
                            **marker_key,
                            "expires_at": _av(ttl),
                        },
                        "ConditionExpression": "attribute_not_exists(user_id)",
                    }
                },
                ledger_update(user_id),
                ledger_update(model_ledger_subject(user_id, usage.model_id)),
            ]
        )
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in {
            "TransactionCanceledException",
            "ConditionalCheckFailedException",
        }:
            raise
        # TransactionCanceledException also represents conflicts, capacity
        # failures, and validation errors. Confirm the request marker before
        # classifying the delivery as a duplicate; otherwise let the Logs
        # subscription retry instead of silently dropping usage.
        marker = client.get_item(
            TableName=usage_table_name,
            Key=marker_key,
            ConsistentRead=True,
        )
        if marker.get("Item"):
            return False
        raise


def _notify(sns, subject: str, payload: dict) -> None:
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    if topic_arn:
        sns.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],
            Message=json.dumps(payload, indent=2, default=str),
        )


def _current_usage(table, user_id: str, now: datetime) -> dict:
    """Current calendar totals for one ledger subject (user or user#model#id)."""
    windows = calendar_windows(now)
    start = min(window.start for window in windows.values()).date().isoformat()
    end = windows["daily"].start.date().isoformat()
    response = table.query(
        KeyConditionExpression=(
            "user_id = :user_id AND #window BETWEEN :start AND :end"
        ),
        ExpressionAttributeNames={"#window": "window"},
        ExpressionAttributeValues={
            ":user_id": user_id,
            ":start": start,
            ":end": end,
        },
        ConsistentRead=True,
    )
    rows = list(response.get("Items", []))
    while response.get("LastEvaluatedKey"):
        response = table.query(
            KeyConditionExpression=(
                "user_id = :user_id AND #window BETWEEN :start AND :end"
            ),
            ExpressionAttributeNames={"#window": "window"},
            ExpressionAttributeValues={
                ":user_id": user_id,
                ":start": start,
                ":end": end,
            },
            ConsistentRead=True,
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        rows.extend(response.get("Items", []))
    return aggregate_daily_rows(rows, now)


def _model_budget_evaluation(usage_table, user: dict, user_id: str, now: datetime):
    """Evaluate every model-scoped budget on ``user`` against its ledger.

    One strongly consistent range query per configured model budget (only
    subjects with model budgets pay for it). Returns an empty evaluation
    when none are configured.
    """
    budgets = model_budgets_from_item(
        user, default_thresholds_list=_deployment_thresholds()
    )
    if not budgets:
        return evaluate_model_budgets({}, {}, now)
    usage_by_model = {
        model_id: _current_usage(
            usage_table, model_ledger_subject(user_id, model_id), now
        )
        for model_id in budgets
    }
    return evaluate_model_budgets(budgets, usage_by_model, now)


def _current_rate_usage(usage_table, user_id: str, now: datetime) -> dict:
    item = usage_table.get_item(
        Key=rate_row_key(user_id, now), ConsistentRead=True
    ).get("Item")
    return rate_usage_from_item(item, now)


def _evaluate_quota(
    client,
    users_table,
    usage_table,
    sns,
    user_id: str,
    *,
    _attempt: int = 0,
) -> str:
    """Block or warn from all current UTC calendar quota periods.

    Each enabled period carries an ordered thresholds list. Every ``warn``
    threshold the period's peak utilization has reached sends one SNS
    warning per calendar window (idempotency marker per threshold, so
    crossing 50 % then 80 % sends two distinct messages, and a duplicate
    delivery at 81 % sends none). Only a ``block`` threshold transitions
    the row to blocked; a period without one is alert-only and can never
    auto-block, however far over 100 % it runs. Rate-limit (rpm/tpm)
    breaches use the same blocking path with a distinct reason and, being
    automatic, lift on their own once the minute is under the limit.
    """
    now = datetime.now(timezone.utc)
    user = users_table.get_item(
        Key={"user_id": user_id}, ConsistentRead=True
    ).get("Item")
    if not user:
        return "missing-user"
    current_usage = _current_usage(usage_table, user_id, now)
    rate_limits = rate_limits_from_item(user)
    rate_usage = (
        _current_rate_usage(usage_table, user_id, now)
        if rate_limits_enabled(rate_limits)
        else None
    )
    evaluation = merge_evaluations(
        evaluate_limits(
            limits_from_item(user),
            current_usage,
            now,
            rate_limits=rate_limits,
            rate_usage=rate_usage,
        ),
        # Model-scoped budgets: ANY block breach blocks the subject, because
        # the deny primitives (SourceIdentity shards, role inline deny) are
        # subject-wide. Documented limitation; never made model-selective.
        _model_budget_evaluation(usage_table, user, user_id, now),
    )
    status = str(user.get("status", "active"))
    reason = str(user.get("status_reason", ""))
    # Ownership comes from ``status_origin`` alone (admin | automatic; a
    # missing attribute is admin). Admin blocks are never rewritten here.
    machine_owned = automatic_owned(user)

    if evaluation.over_budget:
        if status == "blocked" and not machine_owned:
            return "manually-blocked"
        auto_reason = quota_reason(evaluation)
        if status != "blocked" or reason != auto_reason:
            changed_at = datetime.now(timezone.utc).isoformat()
            observed_version = int(user.get("version", 0))
            values = {
                ":s": "blocked",
                ":r": auto_reason,
                ":t": changed_at,
                ":origin": "automatic",
                ":zero": 0,
                ":one": 1,
                ":observed_version": observed_version,
                ":observed_status": status,
                ":observed_reason": reason,
            }
            version_condition = (
                "(attribute_not_exists(#version) OR "
                "#version = :observed_version)"
                if observed_version == 0
                else "#version = :observed_version"
            )
            reason_condition = (
                "(attribute_not_exists(status_reason) OR "
                "status_reason = :observed_reason)"
                if reason == ""
                else "status_reason = :observed_reason"
            )
            try:
                client.transact_write_items(
                    TransactItems=[
                        {
                            "Update": {
                                "TableName": users_table.name,
                                "Key": {"user_id": _av(user_id)},
                                "UpdateExpression": (
                                    "SET #s = :s, status_reason = :r, "
                                    "status_changed_at = :t, updated_at = :t, "
                                    "status_origin = :origin, "
                                    "#version = if_not_exists(#version, :zero) + :one"
                                ),
                                "ConditionExpression": (
                                    f"{version_condition} AND #s = :observed_status AND "
                                    f"{reason_condition}"
                                ),
                                "ExpressionAttributeNames": {
                                    "#s": "status",
                                    "#version": "version",
                                },
                                "ExpressionAttributeValues": {
                                    key: _av(value) for key, value in values.items()
                                },
                            }
                        },
                        {
                            "Put": {
                                "TableName": users_table.name,
                                "Item": {
                                    "user_id": _av(f"REVOCATION#{user_id}"),
                                    "maps_to": _av(user_id),
                                    "desired_status": _av("blocked"),
                                    "source_identity": _av(
                                        str(user.get("source_identity", ""))
                                    ),
                                    "updated_at": _av(changed_at),
                                    "expires_at": _av(
                                        _ttl_epoch(datetime.now(timezone.utc))
                                    ),
                                },
                            }
                        },
                    ]
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in {
                    "ConditionalCheckFailedException",
                    "TransactionCanceledException",
                }:
                    raise
                if _attempt < 2:
                    return _evaluate_quota(
                        client,
                        users_table,
                        usage_table,
                        sns,
                        user_id,
                        _attempt=_attempt + 1,
                    )
                return "concurrent-change"
            # User state and the revocation sentinel committed atomically.
            # Notification remains best-effort and cannot create an
            # authorization gap.
            first = evaluation.breaches[0]
            if first.period == RATE_PERIOD:
                subject_reason = f"reason={first.dimension}"
            elif first.model_id is not None:
                subject_reason = (
                    f"reason=model:{first.model_id}:{first.period}-{first.dimension}"
                )
            else:
                subject_reason = f"reason={first.period}-{first.dimension}"
            _notify(
                sns,
                f"[bedrock-spend-controls] BLOCKED {user_id} {subject_reason}",
                {
                    "user_id": user_id,
                    "reason": auto_reason,
                    "breaches": [
                        {
                            "period": breach.period,
                            "dimension": breach.dimension,
                            "usage": breach.usage,
                            "limit": breach.limit,
                            "at": breach.at_bps / 10_000,
                            "window_start": breach.window.start.isoformat(),
                            "resets_at": breach.window.end.isoformat(),
                            **(
                                {"model_id": breach.model_id}
                                if breach.model_id is not None
                                else {}
                            ),
                        }
                        for breach in evaluation.breaches
                    ],
                    "current_usage": current_usage,
                    **({"rate_usage": rate_usage} if rate_usage else {}),
                },
            )
        return "blocked"

    warned = False
    for crossing in evaluation.warnings:
        marker = crossing.marker
        if user.get(marker) == crossing.window.key:
            continue
        users_table.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET #m = :w",
            ExpressionAttributeNames={"#m": marker},
            ExpressionAttributeValues={":w": crossing.window.key},
        )
        # Keep the marker set warm locally so two crossings in one pass
        # (50 % and 80 % reached by the same request) each send exactly once.
        user[marker] = crossing.window.key
        scope = f" model {crossing.model_id}" if crossing.model_id else ""
        _notify(
            sns,
            f"[bedrock-spend-controls] WARNING {user_id}{scope} "
            f"{crossing.period} {crossing.at_bps / 100:g}%",
            {
                "user_id": user_id,
                "period": crossing.period,
                "threshold": crossing.at_bps / 10_000,
                "window_start": crossing.window.start.isoformat(),
                "resets_at": crossing.window.end.isoformat(),
                "utilization": crossing.ratio,
                **(
                    {"model_id": crossing.model_id}
                    if crossing.model_id is not None
                    else {"usage": current_usage[crossing.period]}
                ),
            },
        )
        warned = True
    if warned:
        return "warned"
    return "within-budget"


def _emit_emf(
    user_id: str,
    usage: InvocationUsage,
    cost_micro: int,
    price_source: str,
    missing_dimensions: tuple[str, ...] = (),
) -> None:
    processed_at = datetime.now(timezone.utc)
    detection_lag_ms = max(
        0,
        int((processed_at - usage.occurred_at).total_seconds() * 1_000),
    )
    # A request is "unpriced" when it used the synthetic fallback rate for
    # the whole model OR when one of its dimensions had no catalog rate.
    # Both are operational events (missing snapshot/profile/dimension
    # mapping) and are never silently priced. FallbackPricedRequests drives
    # the alarm; UnpricedDimensionRequests is the narrower "known model,
    # missing dimension" signal.
    fallback_priced = price_source == "fallback" or bool(missing_dimensions)
    record = {
        "_aws": {
            "Timestamp": int(processed_at.timestamp() * 1000),
            # Two metric groups with different dimension sets. CloudWatch
            # bills per distinct (metric, dimension-set) stream, and the
            # UserId dimension multiplies each metric by the active-user
            # count — it is the dominant line in docs/cost-estimate.md. Only
            # the quota-bearing metrics need per-user resolution; the pricing
            # observability metrics are per model / service-wide.
            "CloudWatchMetrics": [
                {
                    "Namespace": METRICS_NAMESPACE,
                    "Dimensions": [["UserId"], ["Model"], []],
                    "Metrics": [
                        {"Name": "Requests", "Unit": "Count"},
                        {"Name": "InputTokens", "Unit": "Count"},
                        {"Name": "OutputTokens", "Unit": "Count"},
                        {"Name": "EstimatedCostUSD", "Unit": "None"},
                        {
                            "Name": "DetectionLagMilliseconds",
                            "Unit": "Milliseconds",
                        },
                        {
                            "Name": "FallbackPricedRequests",
                            "Unit": "Count",
                        },
                    ],
                },
                {
                    "Namespace": METRICS_NAMESPACE,
                    "Dimensions": [["Model"], []],
                    "Metrics": [
                        {"Name": "CacheReadTokens", "Unit": "Count"},
                        {"Name": "CacheWriteTokens", "Unit": "Count"},
                        {"Name": "ImagesGenerated", "Unit": "Count"},
                        {
                            "Name": "UnpricedDimensionRequests",
                            "Unit": "Count",
                        },
                    ],
                },
            ],
        },
        "UserId": user_id,
        "Model": usage.model_id,
        "RequestId": usage.request_id,
        "PriceSource": price_source,
        "MissingDimensions": list(missing_dimensions),
        "InvocationOccurredAt": usage.occurred_at.isoformat(),
        "ProcessedAt": processed_at.isoformat(),
        "DetectionLagMilliseconds": detection_lag_ms,
        "Requests": 1,
        "InputTokens": usage.input_tokens,
        "OutputTokens": usage.output_tokens,
        "CacheReadTokens": usage.cache_read_tokens,
        "CacheWriteTokens": usage.cache_write_tokens,
        "ImagesGenerated": usage.images,
        "EstimatedCostUSD": round(cost_micro / MICRO, 8),
        "FallbackPricedRequests": 1 if fallback_priced else 0,
        "UnpricedDimensionRequests": 1 if missing_dimensions else 0,
    }
    print(json.dumps(record))


def _default_limit_attributes() -> dict[str, object]:
    """Deploy-configured defaults, mirroring the gateway's storage shape."""
    raw = json.loads(
        os.environ.get(
            "DEFAULT_LIMITS_JSON",
            '{"daily":{"usd":1.0,"input_tokens":1000000,'
            '"output_tokens":200000},"weekly":null,"monthly":null}',
        )
    )
    attributes: dict[str, object] = {}
    defaults = _deployment_thresholds()
    for period in PERIODS:
        value = raw.get(period)
        attributes[f"{period}_limits_enabled"] = value is not None
        usd = float(value.get("usd", 0)) if value else 0
        usd_micro = int(round(usd * MICRO))
        if usd > 0 and usd_micro == 0:
            usd_micro = 1
        attributes[f"{period}_usd_micro"] = usd_micro
        attributes[f"{period}_input_tokens"] = (
            int(value.get("input_tokens", 0)) if value else 0
        )
        attributes[f"{period}_output_tokens"] = (
            int(value.get("output_tokens", 0)) if value else 0
        )
        if value:
            configured = value.get("thresholds")
            attributes[f"{period}_thresholds"] = (
                [
                    {"at_bps": int(entry["at_bps"]), "action": str(entry["action"])}
                    for entry in configured
                ]
                if configured
                else [dict(entry) for entry in defaults]
            )
        else:
            attributes[f"{period}_thresholds"] = []
    rate = raw.get("rate") or {}
    attributes["rpm"] = int(rate.get("rpm", 0) or 0)
    attributes["tpm"] = int(rate.get("tpm", 0) or 0)
    return attributes


def _ensure_workload_user(users_table, workload_id: str, name: str) -> None:
    """Create the workload's quota row on first metered usage.

    Same item shape and defaults as the gateway's auto-provision path so
    admin limit/status mutations apply identically. Losing the race to a
    concurrent create (admin or another processor invocation) is fine.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        users_table.put_item(
            Item={
                "user_id": workload_id,
                "name": name,
                "status": "active",
                "status_reason": "",
                **_default_limit_attributes(),
                "version": 1,
                "created_at": now,
                "updated_at": now,
                "status_origin": "automatic",
            },
            ConditionExpression="attribute_not_exists(user_id)",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code != "ConditionalCheckFailedException":
            raise


def handler(
    event,
    context,
    *,
    dynamodb=None,
    dynamodb_client=None,
    sns=None,
    ssm=None,
) -> dict:
    needs_ssm = ssm is None and bool(os.environ.get("PRICES_PARAMETER_NAME"))
    if dynamodb is None or dynamodb_client is None or sns is None or needs_ssm:
        resource, default_client, sns_client, ssm_client = _resources()
        dynamodb = dynamodb or resource
        dynamodb_client = dynamodb_client or default_client
        sns = sns or sns_client
        ssm = ssm or ssm_client
    client = dynamodb_client
    users_table = dynamodb.Table(os.environ["USERS_TABLE"])
    usage_table = dynamodb.Table(os.environ["USAGE_TABLE"])
    usage_table_name = os.environ["USAGE_TABLE"]
    expected_role = os.environ["BEDROCK_USER_ROLE_NAME"]
    refreshed = _parameter_prices(
        ssm, datetime.now(timezone.utc).timestamp()
    )
    if refreshed is not None:
        prices, fallback = refreshed
    else:
        prices, fallback = _prices(), _fallback_price()

    result = {
        "processed": 0,
        "duplicates": 0,
        "unresolved_sessions": [],
        "ignored": 0,
        "unpriced": 0,
    }
    unresolved: set[str] = set()
    workload_profiles = _workload_profiles()
    for log_event in _decode_subscription(event):
        usage = _parse_invocation(
            log_event, expected_role, workload_profiles
        )
        if usage is None:
            result["ignored"] += 1
            continue
        if usage.workload_id is not None:
            user_id = usage.workload_id
            _ensure_workload_user(
                users_table, user_id, usage.workload_name or user_id
            )
        else:
            assert usage.session_name is not None
            mapping = users_table.get_item(
                Key={"user_id": f"SESSION#{usage.session_name}"},
                ConsistentRead=True,
            ).get("Item")
            if not mapping:
                unresolved.add(usage.session_name)
                continue
            user_id = str(mapping["maps_to"])
        priced = _price_invocation(prices, fallback, usage)
        applied = _apply_usage(
            client,
            usage_table_name,
            user_id,
            usage,
            priced.cost_micro,
            missing_dimensions=priced.missing_dimensions,
        )
        if not applied:
            result["duplicates"] += 1
        else:
            result["processed"] += 1
            if priced.missing_dimensions:
                result["unpriced"] += 1
            _emit_emf(
                user_id,
                usage,
                priced.cost_micro,
                price_source=priced.price_source,
                missing_dimensions=priced.missing_dimensions,
            )
            # Per-minute rate counters only for subjects that configured a
            # rate limit; read the row once (strongly consistent) to decide.
            subject = users_table.get_item(
                Key={"user_id": user_id}, ConsistentRead=True
            ).get("Item") or {}
            if rate_limits_enabled(rate_limits_from_item(subject)):
                _apply_rate_usage(usage_table, user_id, usage)
        # A duplicate delivery may be retrying after accounting committed but
        # status convergence failed. Re-evaluate without incrementing or
        # re-emitting usage so the retry repairs enforcement.
        _evaluate_quota(
            client, users_table, usage_table, sns, user_id
        )

    result["unresolved_sessions"] = sorted(unresolved)
    if unresolved:
        print(
            json.dumps(
                {
                    "level": "warning",
                    "message": "Invocation logs had unknown broker sessions",
                    "sessions": sorted(unresolved),
                }
            )
        )
    return result
