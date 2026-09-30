#!/usr/bin/env python3
"""Export every model-related Amazon Bedrock price by AWS Region.

The exporter reads the official public AWS Price List bulk offer. It preserves
all price units and dimensions instead of assuming every Bedrock model is
priced with input and output tokens.
"""

from __future__ import annotations

import argparse
import csv
import json
import ssl
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, TextIO
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SERVICE_CODE = "AmazonBedrock"
DEFAULT_SOURCE_URL = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/"
    "AmazonBedrock/current/index.json"
)
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MODEL_ATTRIBUTE_NAMES = (
    "model",
    "titanModel",
    "modelUnit",
    "titanModelUnit",
    "architectureName",
)

CSV_FIELDS = (
    "source_url",
    "catalog_version",
    "publication_date",
    "region_code",
    "location",
    "model",
    "model_attribute",
    "provider",
    "sku",
    "product_family",
    "feature",
    "service_tier",
    "inference_type",
    "token_type",
    "usage_type",
    "operation",
    "routing",
    "api_path",
    "term_type",
    "offer_term_code",
    "effective_date",
    "dimension_code",
    "description",
    "begin_range",
    "end_range",
    "applies_to",
    "unit",
    "currency",
    "price_per_unit",
    "price_per_million_tokens",
    "metering_compatible",
    "term_attributes",
    "product_attributes",
)


def _tls_context() -> ssl.SSLContext:
    """Use certifi when present, otherwise the interpreter trust store."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def load_offer(source_url: str, timeout_seconds: int = 120) -> dict[str, Any]:
    """Download and validate the official Bedrock bulk offer."""
    parsed = urlparse(source_url)
    if parsed.scheme != "https":
        raise ValueError("The price-list source must use HTTPS")

    request = Request(
        source_url,
        headers={"User-Agent": "bedrock-spend-controls-price-exporter/1"},
    )
    with urlopen(  # nosec B310  # nosemgrep -- scheme enforced to https above and after redirects
        request,
        timeout=timeout_seconds,
        context=_tls_context(),
    ) as response:
        final_url = response.geturl()
        if urlparse(final_url).scheme != "https":
            raise ValueError(
                "The price-list source redirected to a non-HTTPS URL"
            )
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
            raise ValueError(
                "The price-list response exceeds the 512 MiB safety limit"
            )
        payload = response.read(MAX_DOWNLOAD_BYTES + 1)

    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise ValueError("The price-list response exceeds the 512 MiB safety limit")
    offer = json.loads(payload)
    if not isinstance(offer, dict):
        raise ValueError("The price-list response must be a JSON object")
    if offer.get("offerCode") != SERVICE_CODE:
        raise ValueError(
            f"Expected offerCode {SERVICE_CODE!r}; got {offer.get('offerCode')!r}"
        )
    if not isinstance(offer.get("products"), dict):
        raise ValueError("The price-list response has no products object")
    if not isinstance(offer.get("terms"), dict):
        raise ValueError("The price-list response has no terms object")
    return offer


def _model_identity(attributes: dict[str, Any]) -> tuple[str, str] | None:
    for attribute_name in MODEL_ATTRIBUTE_NAMES:
        value = attributes.get(attribute_name)
        if isinstance(value, str) and value.strip():
            return attribute_name, value.strip()

    usage_type = str(attributes.get("usagetype", ""))
    if (
        attributes.get("modality")
        and "novamultimodalembeddings" in usage_type.casefold()
    ):
        return "usagetype", "Nova Multimodal Embeddings"
    return None


def _routing(usage_type: str) -> str:
    normalized = usage_type.casefold()
    if "cross-region-global" in normalized:
        return "cross-region-global"
    if "cross-region" in normalized:
        return "cross-region"
    return "in-region"


def _api_path(usage_type: str) -> str:
    return "bedrock-mantle" if "-mantle-" in usage_type.casefold() else "bedrock-runtime"


def _price_per_million_tokens(
    unit: str, currency: str, price_per_unit: str
) -> str | None:
    if currency != "USD" or unit not in {"1K tokens", "1M tokens"}:
        return None
    try:
        price = Decimal(price_per_unit)
    except InvalidOperation:
        return None
    if unit == "1K tokens":
        price *= 1000
    return format(price, "f")


def _metering_compatible(
    *,
    term_type: str,
    unit: str,
    currency: str,
    feature: str,
    service_tier: str,
    inference_type: str,
) -> bool:
    """Whether a row has a standard on-demand USD per-1K-token input/output rate.

    Rows marked ``metering_compatible`` are the ones the deployment's price
    snapshot can carry as ``input_per_mtok`` / ``output_per_mtok``.
    """
    return (
        term_type == "OnDemand"
        and unit == "1K tokens"
        and currency == "USD"
        and feature.casefold() in {"", "on-demand inference"}
        and service_tier.casefold() in {"", "standard"}
        and inference_type.casefold() in {"input tokens", "output tokens"}
    )


def iter_model_prices(
    offer: dict[str, Any],
    *,
    regions: set[str] | None = None,
    model_query: str | None = None,
    metering_compatible_only: bool = False,
) -> Iterable[dict[str, Any]]:
    """Yield one normalized row per model price dimension and currency."""
    terms = offer["terms"]
    query = model_query.casefold() if model_query else None

    for sku, product in offer["products"].items():
        attributes = product.get("attributes", {})
        if not isinstance(attributes, dict):
            continue
        identity = _model_identity(attributes)
        if identity is None:
            continue
        model_attribute, model = identity
        if query and query not in model.casefold():
            continue

        region_code = str(attributes.get("regionCode", ""))
        if regions and region_code not in regions:
            continue

        feature = str(attributes.get("feature", ""))
        service_tier = str(attributes.get("service_tier", ""))
        inference_type = str(attributes.get("inferenceType", ""))
        usage_type = str(attributes.get("usagetype", ""))

        for term_type, terms_by_sku in terms.items():
            if not isinstance(terms_by_sku, dict):
                continue
            for offer_term_code, term in terms_by_sku.get(sku, {}).items():
                term_attributes = term.get("termAttributes", {})
                dimensions = term.get("priceDimensions", {})
                for dimension_code, dimension in dimensions.items():
                    unit = str(dimension.get("unit", ""))
                    price_per_unit = dimension.get("pricePerUnit", {})
                    if not isinstance(price_per_unit, dict):
                        continue
                    for currency, raw_price in price_per_unit.items():
                        price = str(raw_price)
                        compatible = _metering_compatible(
                            term_type=term_type,
                            unit=unit,
                            currency=currency,
                            feature=feature,
                            service_tier=service_tier,
                            inference_type=inference_type,
                        )
                        if metering_compatible_only and not compatible:
                            continue
                        yield {
                            "region_code": region_code,
                            "location": str(attributes.get("location", "")),
                            "model": model,
                            "model_attribute": model_attribute,
                            "provider": str(attributes.get("provider", "")),
                            "sku": sku,
                            "product_family": str(product.get("productFamily", "")),
                            "feature": feature,
                            "service_tier": service_tier,
                            "inference_type": inference_type,
                            "token_type": str(attributes.get("tokenType", "")),
                            "usage_type": usage_type,
                            "operation": str(attributes.get("operation", "")),
                            "routing": _routing(usage_type),
                            "api_path": _api_path(usage_type),
                            "term_type": term_type,
                            "offer_term_code": offer_term_code,
                            "effective_date": str(term.get("effectiveDate", "")),
                            "dimension_code": dimension_code,
                            "description": str(
                                dimension.get("description", "")
                            ),
                            "begin_range": str(
                                dimension.get("beginRange", "")
                            ),
                            "end_range": str(dimension.get("endRange", "")),
                            "applies_to": list(dimension.get("appliesTo", [])),
                            "unit": unit,
                            "currency": currency,
                            "price_per_unit": price,
                            "price_per_million_tokens": (
                                _price_per_million_tokens(
                                    unit, currency, price
                                )
                            ),
                            "metering_compatible": compatible,
                            "term_attributes": term_attributes,
                            "product_attributes": attributes,
                        }


def build_catalog(
    offer: dict[str, Any],
    records: Iterable[dict[str, Any]],
    *,
    source_url: str,
    requested_regions: set[str] | None,
    model_query: str | None,
    metering_compatible_only: bool,
) -> dict[str, Any]:
    """Build a deterministic Region-keyed JSON catalog."""
    sorted_records = sorted(
        records,
        key=lambda row: (
            row["region_code"],
            row["model"].casefold(),
            row["usage_type"],
            row["sku"],
            row["term_type"],
            row["dimension_code"],
            row["currency"],
        ),
    )
    region_prices: dict[str, list[dict[str, Any]]] = {}
    region_models: dict[str, set[str]] = {}
    for record in sorted_records:
        region = record["region_code"]
        region_prices.setdefault(region, []).append(record)
        region_models.setdefault(region, set()).add(record["model"])

    regions = {
        region: {
            "model_count": len(region_models[region]),
            "price_count": len(prices),
            "prices": prices,
        }
        for region, prices in sorted(region_prices.items())
    }
    return {
        "schema_version": 1,
        "service_code": SERVICE_CODE,
        "currency_note": (
            "Each row preserves the currency published in pricePerUnit; "
            "the current AWS offer normally uses USD."
        ),
        "source_url": source_url,
        "catalog_version": offer.get("version"),
        "publication_date": offer.get("publicationDate"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "regions": sorted(requested_regions or []),
            "model_query": model_query or "",
            "metering_compatible_only": metering_compatible_only,
        },
        "region_count": len(regions),
        "model_region_count": sum(len(models) for models in region_models.values()),
        "price_count": len(sorted_records),
        "regions": regions,
    }


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return str(value).lower()
    return "" if value is None else value


def write_csv(
    records: Iterable[dict[str, Any]],
    stream: TextIO,
    *,
    source_url: str,
    catalog_version: str,
    publication_date: str,
) -> None:
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    metadata = {
        "source_url": source_url,
        "catalog_version": catalog_version,
        "publication_date": publication_date,
    }
    for record in records:
        row = {**metadata, **record}
        writer.writerow({field: _csv_value(row.get(field)) for field in CSV_FIELDS})


def _open_output(path: str, overwrite: bool) -> tuple[TextIO, bool]:
    if path == "-":
        return sys.stdout, False
    output_path = Path(path)
    if not output_path.parent.is_dir():
        raise ValueError(
            f"Output directory does not exist: {output_path.parent}"
        )
    mode = "w" if overwrite else "x"
    return output_path.open(mode, encoding="utf-8", newline=""), True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export every model-related Amazon Bedrock price from the "
            "official AWS Price List bulk catalog."
        )
    )
    parser.add_argument(
        "--source-url",
        default=DEFAULT_SOURCE_URL,
        help="Official HTTPS AWS Price List offer URL.",
    )
    parser.add_argument(
        "--region",
        action="append",
        dest="regions",
        help="Include one Region; repeat for multiple Regions (default: all).",
    )
    parser.add_argument(
        "--model",
        help="Case-insensitive substring filter for the catalog model name.",
    )
    parser.add_argument(
        "--metering-compatible",
        action="store_true",
        help=(
            "Keep only rows with a standard on-demand USD per-1K-token "
            "input/output rate (the shape the price snapshot carries)."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="Output format (default: json).",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Output path, or - for stdout (default: -).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacement of an existing output file.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Download timeout in seconds (default: 120).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")

    requested_regions = set(args.regions or [])
    offer = load_offer(args.source_url, args.timeout)
    records = list(
        iter_model_prices(
            offer,
            regions=requested_regions or None,
            model_query=args.model,
            metering_compatible_only=args.metering_compatible,
        )
    )
    discovered_regions = {record["region_code"] for record in records}
    missing_regions = sorted(requested_regions - discovered_regions)
    if missing_regions:
        raise ValueError(
            "No matching model prices found for Region(s): "
            + ", ".join(missing_regions)
        )
    if not records:
        raise ValueError("No model prices matched the requested filters")

    stream, should_close = _open_output(args.output, args.overwrite)
    try:
        if args.format == "csv":
            ordered_records = sorted(
                records,
                key=lambda row: (
                    row["region_code"],
                    row["model"].casefold(),
                    row["usage_type"],
                    row["sku"],
                    row["dimension_code"],
                ),
            )
            write_csv(
                ordered_records,
                stream,
                source_url=args.source_url,
                catalog_version=str(offer.get("version", "")),
                publication_date=str(offer.get("publicationDate", "")),
            )
        else:
            catalog = build_catalog(
                offer,
                records,
                source_url=args.source_url,
                requested_regions=requested_regions or None,
                model_query=args.model,
                metering_compatible_only=args.metering_compatible,
            )
            json.dump(catalog, stream, indent=2, sort_keys=True)
            stream.write("\n")
    finally:
        if should_close:
            stream.close()

    print(
        json.dumps(
            {
                "catalog_version": offer.get("version"),
                "publication_date": offer.get("publicationDate"),
                "regions": len(discovered_regions),
                "model_regions": len(
                    {(row["region_code"], row["model"]) for row in records}
                ),
                "prices": len(records),
                "output": args.output,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
