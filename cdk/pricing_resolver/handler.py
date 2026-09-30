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

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3

SERVICE_CODE = "AmazonBedrock"
PRICING_API_REGION = "us-east-1"

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
            raise ValueError(
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
            raise ValueError(
                f"Expected one standard on-demand {token_type} price for "
                f"{catalog_model} in {region_code}; found {sorted(rates[key])}"
            )
    for key in _OPTIONAL_TOKEN:
        candidates = rates[key]
        if len(candidates) == 1:
            price[key] = float(candidates.pop())
        elif len(candidates) > 1:
            # Ambiguity is a catalog defect, not something to average away.
            raise ValueError(
                f"Expected at most one standard on-demand {key} price for "
                f"{catalog_model} in {region_code}; found {sorted(candidates)}"
            )
    if per_image is not None:
        price["per_image"] = per_image
    return price


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
) -> dict:
    snapshot = {
        model_id: _pinned_price(price)
        for model_id, price in pinned_prices.items()
    }
    for catalog_model, model_ids in catalog_models.items():
        price = _catalog_price(pricing_client, region_code, catalog_model)
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


def _resolve(pricing_client, properties: dict) -> tuple[dict, dict]:
    """Resolve (snapshot, fallback) from a CatalogModels/PinnedPrices dict.

    The same shape arrives as CloudFormation ResourceProperties at deploy
    time and as the EventBridge rule input on scheduled refreshes.
    """
    snapshot = resolve_snapshot(
        pricing_client,
        properties["RegionCode"],
        properties["CatalogModels"],
        properties.get("PinnedPrices", {}),
    )
    fallback = conservative_fallback(
        snapshot,
        properties.get(
            "FallbackPrice",
            {"input_per_mtok": 15.0, "output_per_mtok": 75.0},
        ),
    )
    return snapshot, fallback


def handler(event, _context):
    properties = event["ResourceProperties"]
    physical_id = f"bedrock-model-prices-{properties['RegionCode']}"
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": physical_id}

    client = boto3.client("pricing", region_name=PRICING_API_REGION)
    snapshot, fallback = _resolve(client, properties)
    return {
        "PhysicalResourceId": physical_id,
        "Data": {
            "ModelPricesJson": json.dumps(
                snapshot, sort_keys=True, separators=(",", ":")
            ),
            "FallbackPriceJson": json.dumps(
                fallback, sort_keys=True, separators=(",", ":")
            ),
        },
    }


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
    snapshot, fallback = _resolve(pricing, event)
    resolved_at = datetime.now(timezone.utc).isoformat()
    ssm.put_parameter(
        Name=parameter_name,
        Value=json.dumps(
            {
                "models": snapshot,
                "fallback": fallback,
                "resolved_at": resolved_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        Type="String",
        Overwrite=True,
    )
    print(
        json.dumps(
            {
                "level": "info",
                "message": "Refreshed Bedrock model price parameter",
                "parameter": parameter_name,
                "models": len(snapshot),
                "resolved_at": resolved_at,
            }
        )
    )
    return {"models": len(snapshot), "resolved_at": resolved_at}
