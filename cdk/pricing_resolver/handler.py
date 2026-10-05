"""Resolve standard on-demand Bedrock prices during stack deployment.

Price entries carry one rate per *dimension*. ``input_per_mtok`` and
``output_per_mtok`` are required; the resolver also emits
``cache_read_per_mtok``, ``cache_write_per_mtok``, and ``per_image`` when
the Price List publishes them for the catalog model. The usage processor
prices whichever dimensions a record carries and flags requests whose
dimensions have no rate, so adding a dimension here is additive and never
changes how an input/output-only model is priced.

Dimension names below are the exact ``inferenceType`` attribute values the
AmazonBedrock offer publishes for Nova Lite/Pro/Micro (``Prompt cache read
input tokens`` / ``Prompt cache write input tokens``, unit ``1K tokens``)
and Nova Canvas (``T2I 1024 Standard`` ..., unit ``image``). They are
matched literally, never guessed from substrings, so a renamed dimension
surfaces as a missing rate (alarmed) rather than a silently wrong price.
"""

import base64
import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

SERVICE_CODE = "AmazonBedrock"
PRICING_API_REGION = "us-east-1"

# Parameter Store caps Intelligent-Tiering / Advanced parameters at 8 KB.
# The stored value is the minimized JSON document, gzip-compressed and
# base64-encoded behind an explicit marker (``COMPRESSED_PREFIX``), so the
# shipped catalog (~9 KB of JSON) fits in about 1 KB. The resolver refuses
# to write anything at or above ``PARAMETER_VALUE_MAX_BYTES`` (a margin
# under the hard cap) so the failure is a clear deploy/refresh error rather
# than a truncated price table. The usage processor decodes the marked form
# and still accepts a legacy plain-JSON value (starting with ``{``), so an
# in-place upgrade keeps metering between the deploy and the first refresh.
PARAMETER_MAX_BYTES = 8192
PARAMETER_VALUE_MAX_BYTES = 7800
COMPRESSED_PREFIX = "gz1:"
# Length of the hex digest returned to CloudFormation in place of the full
# snapshot (the full JSON would push the custom-resource response past the
# 4,096-byte CloudFormation limit).
_DIGEST_CHARS = 16
# Inference-profile prefixes the usage processor resolves to the base model
# when a profile ID has no entry of its own; must stay in sync with
# usage_processor/handler.py _PROFILE_PREFIXES.
_PROFILE_PREFIXES = frozenset(
    {"us", "eu", "apac", "jp", "au", "ca", "sa", "global", "us-gov"}
)

# ``inferenceType`` -> price-entry key, for the token dimensions priced per
# 1K tokens in the Price List and stored here per million tokens.
_TOKEN_DIMENSIONS = {
    "input tokens": "input_per_mtok",
    "output tokens": "output_per_mtok",
    "prompt cache read input tokens": "cache_read_per_mtok",
    "prompt cache write input tokens": "cache_write_per_mtok",
}
_REQUIRED = ("input_per_mtok", "output_per_mtok")
_OPTIONAL_TOKEN = ("cache_read_per_mtok", "cache_write_per_mtok")

# Image generation prices are per image and vary by task type, size, and
# quality; the invocation log reports only a count, so the resolver keeps
# the STANDARD text-to-image rate at the smallest published size as the
# per-image estimate and lets operators pin a different one via
# ``price_overrides`` when their workload is premium/large. Matched
# literally on the observed ``inferenceType`` values.
_IMAGE_DIMENSION_PREFERENCE = (
    "t2i 512 standard",
    "t2i 1024 standard",
    "t2i 2048 standard",
)


def _unit_rate(product: dict, *, unit: str, multiplier: Decimal) -> Decimal:
    rates = set()
    for term in product.get("terms", {}).get("OnDemand", {}).values():
        for dimension in term.get("priceDimensions", {}).values():
            if dimension.get("unit") != unit:
                continue
            rates.add(Decimal(dimension["pricePerUnit"]["USD"]) * multiplier)
    if len(rates) != 1:
        sku = product.get("product", {}).get("sku")
        raise ValueError(
            f"Expected one USD rate per {unit} for SKU {sku}; "
            f"found {sorted(rates)}"
        )
    return rates.pop()


def _dimension_rate(product: dict) -> Decimal:
    """USD per million tokens for a ``1K tokens`` SKU."""
    return _unit_rate(product, unit="1K tokens", multiplier=Decimal(1000))


def _catalog_price(pricing_client, region_code: str, catalog_model: str) -> dict:
    filters = [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region_code},
        {"Type": "TERM_MATCH", "Field": "model", "Value": catalog_model},
    ]
    rates: dict[str, set[Decimal]] = {key: set() for key in _TOKEN_DIMENSIONS.values()}
    image_rates: dict[str, set[Decimal]] = {}
    paginator = pricing_client.get_paginator("get_products")
    for page in paginator.paginate(ServiceCode=SERVICE_CODE, Filters=filters):
        for raw_product in page.get("PriceList", []):
            product = json.loads(raw_product)
            attributes = product["product"]["attributes"]
            feature = attributes.get("feature", "").lower()
            tier = attributes.get("service_tier", "").lower()
            if feature not in ("", "on-demand inference"):
                continue
            if tier not in ("", "standard"):
                continue

            inference_type = attributes.get("inferenceType", "").lower()
            token_key = _TOKEN_DIMENSIONS.get(inference_type)
            if token_key is not None:
                rates[token_key].add(_dimension_rate(product))
            elif inference_type in _IMAGE_DIMENSION_PREFERENCE:
                image_rates.setdefault(inference_type, set()).add(
                    _unit_rate(product, unit="image", multiplier=Decimal(1))
                )

    per_image: float | None = None
    for inference_type in _IMAGE_DIMENSION_PREFERENCE:
        candidates = image_rates.get(inference_type)
        if not candidates:
            continue
        if len(candidates) != 1:
            raise UnresolvedPrice(
                f"Expected one {inference_type} image price for "
                f"{catalog_model} in {region_code}; found {sorted(candidates)}"
            )
        per_image = float(candidates.pop())
        break

    price: dict[str, float] = {}
    for key in _REQUIRED:
        if len(rates[key]) == 1:
            price[key] = float(rates[key].pop())
        elif not rates[key] and per_image is not None:
            # Image-only model (observed: Nova Canvas publishes no token
            # rows). The token pair is zero by construction, not by guess.
            price[key] = 0.0
        else:
            token_type = key.split("_per_")[0]
            raise UnresolvedPrice(
                f"Expected one standard on-demand {token_type} price for "
                f"{catalog_model} in {region_code}; found {sorted(rates[key])}"
            )
    for key in _OPTIONAL_TOKEN:
        candidates = rates[key]
        if len(candidates) == 1:
            price[key] = float(candidates.pop())
        elif len(candidates) > 1:
            # Ambiguity is a catalog defect, not something to average away.
            raise UnresolvedPrice(
                f"Expected at most one standard on-demand {key} price for "
                f"{catalog_model} in {region_code}; found {sorted(candidates)}"
            )
    if per_image is not None:
        price["per_image"] = per_image
    return price


class UnresolvedPrice(ValueError):
    """The Price List has no single standard on-demand price for a catalog
    model in the target Region: the model is not offered there, or the rows
    are ambiguous. Deploy-time resolution skips such models (the usage
    processor prices them with the conservative fallback and counts
    FallbackPricedRequests) instead of failing the whole stack."""


def _pinned_price(price: dict) -> dict:
    """Copy a hand-pinned entry, keeping only known rate keys."""
    resolved = {key: float(price[key]) for key in _REQUIRED}
    for key in (*_OPTIONAL_TOKEN, "per_image"):
        if price.get(key) is not None:
            resolved[key] = float(price[key])
    return resolved


def resolve_snapshot(
    pricing_client,
    region_code: str,
    catalog_models: dict[str, list[str]],
    pinned_prices: dict[str, dict],
    *,
    unresolved: list[dict] | None = None,
) -> dict:
    """Resolve every catalog model's price in ``region_code``.

    With ``unresolved`` given, a catalog model the Price List cannot price
    in this Region (not offered there, or ambiguous rows) is skipped and
    recorded in that list as ``{"catalog_model", "model_ids", "reason"}``
    instead of raising, so one Regional gap does not block the deploy.
    Without it (strict mode, used by tests and tooling) the
    :class:`UnresolvedPrice` propagates.
    """
    snapshot = {
        model_id: _pinned_price(price)
        for model_id, price in pinned_prices.items()
    }
    for catalog_model, model_ids in catalog_models.items():
        try:
            price = _catalog_price(pricing_client, region_code, catalog_model)
        except UnresolvedPrice as exc:
            if unresolved is None:
                raise
            unresolved.append(
                {"catalog_model": catalog_model, "model_ids": list(model_ids), "reason": str(exc)}
            )
            print(
                json.dumps(
                    {
                        "level": "warning",
                        "message": "Catalog model not priced in this Region; "
                        "its invocations use the fallback price",
                        "catalog_model": catalog_model,
                        "model_ids": list(model_ids),
                        "region": region_code,
                        "reason": str(exc),
                    }
                )
            )
            continue
        for model_id in model_ids:
            if model_id in snapshot:
                raise ValueError(f"Duplicate model price mapping for {model_id}")
            snapshot[model_id] = dict(price)
    return snapshot


def conservative_fallback(snapshot: dict, configured: dict) -> dict:
    """Keep unknown models at least as expensive as every known model.

    Applies per dimension: the fallback carries every dimension any known
    model prices, at the maximum known rate, so a record whose dimension is
    missing from its own model's entry is priced conservatively rather than
    at zero. Dimensions no model prices are omitted (the processor then
    flags them as unpriceable).
    """
    fallback = {}
    keys = set(_REQUIRED) | set(configured) | {
        key for price in snapshot.values() for key in price
    }
    for field in sorted(keys):
        if field not in (*_REQUIRED, *_OPTIONAL_TOKEN, "per_image"):
            continue
        candidates = []
        if configured.get(field) is not None:
            candidates.append(float(configured[field]))
        candidates.extend(
            float(price[field])
            for price in snapshot.values()
            if price.get(field) is not None
        )
        if candidates:
            fallback[field] = max(candidates)
    return fallback


def _resolve(pricing_client, properties: dict) -> tuple[dict, dict, list[dict]]:
    """Resolve (snapshot, fallback) from a CatalogModels/PinnedPrices dict.

    The same shape arrives as CloudFormation ResourceProperties at deploy
    time and as the EventBridge rule input on scheduled refreshes.
    """
    unresolved: list[dict] = []
    snapshot = resolve_snapshot(
        pricing_client,
        properties["RegionCode"],
        properties["CatalogModels"],
        properties.get("PinnedPrices", {}),
        unresolved=unresolved,
    )
    fallback = conservative_fallback(
        snapshot,
        properties.get(
            "FallbackPrice",
            {"input_per_mtok": 15.0, "output_per_mtok": 75.0},
        ),
    )
    return snapshot, fallback, unresolved


def _compact(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _compact_rates(price: dict) -> dict:
    """Render whole-number rates as ints (``5.0`` -> ``5``).

    The usage processor coerces every rate with ``float()``, so this is
    lossless for it and saves two bytes per whole rate in a document that
    is measured against the 8 KB Parameter Store cap.
    """
    return {
        key: int(rate) if float(rate).is_integer() else float(rate)
        for key, rate in price.items()
    }


def minimize_snapshot(snapshot: dict) -> tuple[dict, list[str]]:
    """Drop profile entries the usage processor derives on its own.

    For ``<prefix>.<base-model>`` IDs with no entry, the processor prices at
    the base model's rates. An explicit entry identical to the base is
    therefore redundant and is dropped from the stored document (the
    processor reports such requests as ``base-model`` priced, never as
    fallback). Entries whose rates differ (Regional uplifts) are kept.
    Returns the minimized snapshot and the dropped IDs.
    """
    kept: dict = {}
    dropped: list[str] = []
    for model_id, price in snapshot.items():
        prefix, separator, base_model = model_id.partition(".")
        if (
            separator
            and prefix in _PROFILE_PREFIXES
            and snapshot.get(base_model) == price
        ):
            dropped.append(model_id)
            continue
        kept[model_id] = _compact_rates(price)
    return kept, dropped


def encode_parameter_value(document: str) -> str:
    """Encode the JSON document as ``gz1:<base64(gzip(document))>``.

    ``mtime=0`` and a fixed compression level make the encoding a pure
    function of the document, so identical snapshots produce identical
    parameter values (no spurious parameter versions). The marker lets the
    reader tell this form from a legacy plain-JSON value unambiguously:
    base64 output never starts with ``{``.
    """
    compressed = gzip.compress(
        document.encode("utf-8"), compresslevel=9, mtime=0
    )
    return COMPRESSED_PREFIX + base64.b64encode(compressed).decode("ascii")


def decode_parameter_value(value: str) -> dict:
    """Inverse of :func:`encode_parameter_value`; also accepts plain JSON.

    Mirrors the usage processor's reader (kept there as a private helper
    because the two Lambdas are packaged separately). Used by tests to
    pin both sides to the same contract.
    """
    if value.startswith(COMPRESSED_PREFIX):
        compressed = base64.b64decode(value[len(COMPRESSED_PREFIX):])
        return json.loads(gzip.decompress(compressed).decode("utf-8"))
    if value.startswith("{"):
        return json.loads(value)
    raise ValueError(
        "Unrecognised price parameter encoding: expected a value starting "
        f"with {COMPRESSED_PREFIX!r} or '{{'"
    )


def write_price_parameter(
    ssm, parameter_name: str, snapshot: dict, fallback: dict,
    unresolved: list[dict] | None = None,
) -> dict:
    """Write ``{"models", "fallback", "resolved_at"}`` to Parameter Store.

    This is the only writer of the parameter the usage processor reads, for
    both the deploy-time custom resource and the daily refresh. The value
    is stored in the compressed form from :func:`encode_parameter_value`;
    the parameter is Intelligent-Tiering so a value that outgrows the 4 KB
    standard tier is promoted automatically (up to 8 KB; a value at or
    above ``PARAMETER_VALUE_MAX_BYTES`` fails here with an explicit error
    rather than a truncated table).
    """
    stored, dropped = minimize_snapshot(snapshot)
    resolved_at = datetime.now(timezone.utc).isoformat()
    body = {
        "models": stored,
        "fallback": _compact_rates(fallback),
        "resolved_at": resolved_at,
    }
    if unresolved:
        # Catalog models the Price List could not price in this Region;
        # their invocations are priced with the fallback and flagged.
        body["unresolved"] = sorted(item["catalog_model"] for item in unresolved)
    document = _compact(body)
    json_size = len(document.encode("utf-8"))
    value = encode_parameter_value(document)
    size = len(value.encode("utf-8"))
    if size >= PARAMETER_VALUE_MAX_BYTES:
        raise ValueError(
            f"Encoded price parameter value is {size} bytes (from a "
            f"{json_size}-byte JSON document with {len(stored)} stored "
            f"models); the resolver requires it to stay under "
            f"{PARAMETER_VALUE_MAX_BYTES} bytes to fit Parameter Store's "
            f"{PARAMETER_MAX_BYTES}-byte cap with margin. Trim "
            "catalog_models or price_overrides (Regional profile pins "
            "identical to their base model are already omitted)."
        )
    ssm.put_parameter(
        Name=parameter_name,
        Value=value,
        Type="String",
        Tier="Intelligent-Tiering",
        Overwrite=True,
        Description=(
            "Bedrock model token prices used by quota metering; "
            "refreshed daily from the AWS Pricing API"
        ),
    )
    digest = hashlib.sha256(_compact(snapshot).encode("utf-8")).hexdigest()
    return {
        "digest": digest[:_DIGEST_CHARS],
        "models": len(snapshot),
        "stored": len(stored),
        "derived": dropped,
        "resolved_at": resolved_at,
        # Size of the value as written (the figure Parameter Store caps).
        "bytes": size,
        "json_bytes": json_size,
    }


def _delete_price_parameter(ssm, parameter_name: str) -> None:
    try:
        ssm.delete_parameter(Name=parameter_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise


def handler(event, _context, *, pricing_client=None, ssm_client=None):
    """CloudFormation custom-resource entrypoint.

    Writes the resolved snapshot straight to the SSM parameter named in
    ``ResourceProperties.ParameterName`` and returns only a short digest,
    the model count, and the (small) fallback price as attributes. The full
    snapshot never rides in the response: CloudFormation rejects
    custom-resource responses over 4,096 bytes, and the catalog alone is
    larger than that once cache and image dimensions are included.
    """
    properties = event["ResourceProperties"]
    physical_id = f"bedrock-model-prices-{properties['RegionCode']}"
    # Delete events for a resource created by an older template version
    # carry no ParameterName (that version owned no parameter).
    parameter_name = properties.get("ParameterName", "")
    if event["RequestType"] == "Delete":
        if parameter_name:
            ssm = ssm_client or boto3.client("ssm")
            _delete_price_parameter(ssm, parameter_name)
        return {"PhysicalResourceId": physical_id}
    if not parameter_name:
        raise ValueError("ResourceProperties.ParameterName is required")

    ssm = ssm_client or boto3.client("ssm")
    pricing = pricing_client or boto3.client(
        "pricing", region_name=PRICING_API_REGION
    )
    snapshot, fallback, unresolved = _resolve(pricing, properties)
    written = write_price_parameter(ssm, parameter_name, snapshot, fallback, unresolved)
    print(
        json.dumps(
            {
                "level": "info",
                "message": "Wrote Bedrock model price parameter",
                "parameter": parameter_name,
                "models": written["models"],
                "stored": written["stored"],
                "derived": written["derived"],
                "bytes": written["bytes"],
                "json_bytes": written["json_bytes"],
                "digest": written["digest"],
            }
        )
    )
    return {
        "PhysicalResourceId": physical_id,
        "Data": {
            "ParameterName": parameter_name,
            "SnapshotDigest": written["digest"],
            "ModelCount": str(written["models"]),
            "ResolvedAt": written["resolved_at"],
            "FallbackPriceJson": _compact(fallback),
            "UnresolvedCount": str(len(unresolved)),
            "UnresolvedModels": ",".join(
                sorted(item["catalog_model"] for item in unresolved)
            )[:1024],
        },
    }


def _previously_priced(ssm, parameter_name: str) -> set[str]:
    """Model IDs the live parameter prices today (empty when absent)."""
    try:
        value = ssm.get_parameter(Name=parameter_name)["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return set()
        raise
    try:
        return set(decode_parameter_value(value).get("models", {}))
    except ValueError:
        return set()


def _refuse_regression(ssm, parameter_name: str, unresolved: list[dict]) -> None:
    """A daily refresh must not silently demote a priced model to the
    fallback: if the Price List stops answering for a model the live table
    already prices, keep the previous value and fail loudly (Lambda Errors)
    instead of writing a table without it. A model that was never priced
    (not offered in this Region) is skipped quietly, as at deploy time.
    """
    live = _previously_priced(ssm, parameter_name)
    dropped = sorted(
        model_id
        for item in unresolved
        for model_id in item["model_ids"]
        if model_id in live
    )
    if dropped:
        reasons = "; ".join(item["reason"] for item in unresolved)
        raise ValueError(
            "Refusing to refresh the price parameter: the Price List no longer "
            f"prices {', '.join(dropped)}, which the live table prices today "
            f"({reasons}). The previous value stays in place."
        )


def scheduled_handler(event, _context, *, pricing_client=None, ssm_client=None):
    """Refresh the model price parameter from the live Pricing API.

    EventBridge invokes this daily with the same properties shape as the
    deploy-time custom resource, so metering follows catalog price changes
    without a redeploy. The metering Lambda keeps the last good parameter
    value (and the deployment-time env snapshot before the first read), so
    a failed refresh (this function raising) leaves the previous value in
    place and surfaces through this function's Lambda ``Errors`` metric
    instead of silently mispricing. The stack does not alarm on that metric
    by default; see docs/runbooks/components/pricing-resolver.md.
    """
    parameter_name = os.environ["PRICES_PARAMETER_NAME"]
    pricing = pricing_client or boto3.client(
        "pricing", region_name=PRICING_API_REGION
    )
    ssm = ssm_client or boto3.client("ssm")
    snapshot, fallback, unresolved = _resolve(pricing, event)
    if unresolved:
        _refuse_regression(ssm, parameter_name, unresolved)
    written = write_price_parameter(ssm, parameter_name, snapshot, fallback, unresolved)
    print(
        json.dumps(
            {
                "level": "info",
                "message": "Refreshed Bedrock model price parameter",
                "parameter": parameter_name,
                "models": written["models"],
                "stored": written["stored"],
                "bytes": written["bytes"],
                "json_bytes": written["json_bytes"],
                "digest": written["digest"],
                "resolved_at": written["resolved_at"],
            }
        )
    )
    return {"models": written["models"], "resolved_at": written["resolved_at"]}
