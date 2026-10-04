import json

import pytest

from cdk.pricing_resolver import handler as resolver


def _product(
    sku: str,
    inference_type: str,
    usd_per_1k: str,
    *,
    feature: str = "",
    service_tier: str = "",
    model: str = "gpt-oss-20b",
    unit: str = "1K tokens",
) -> str:
    attributes = {
        "model": model,
        "regionCode": "us-east-1",
        "inferenceType": inference_type,
    }
    if feature:
        attributes["feature"] = feature
    if service_tier:
        attributes["service_tier"] = service_tier
    return json.dumps({
        "product": {"sku": sku, "attributes": attributes},
        "terms": {
            "OnDemand": {
                f"{sku}.term": {
                    "priceDimensions": {
                        f"{sku}.dimension": {
                            "unit": unit,
                            "pricePerUnit": {"USD": usd_per_1k},
                        }
                    }
                }
            }
        },
    })


class _FakePricing:
    def __init__(self, products):
        self.products = products
        self.calls = []

    def get_paginator(self, operation):
        assert operation == "get_products"
        return self

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return [{"PriceList": self.products}]


def test_snapshot_uses_standard_prices_and_maps_all_model_ids():
    pricing = _FakePricing([
        _product("native-in", "Input tokens", "0.0000700000",
                 feature="On-demand Inference"),
        _product("standard-in", "Input tokens", "0.0000700000",
                 service_tier="standard"),
        _product("native-out", "Output tokens", "0.0003000000",
                 feature="On-demand Inference"),
        _product("standard-out", "Output tokens", "0.0003000000",
                 service_tier="standard"),
        _product("flex", "Input tokens flex", "0.0000350000"),
        _product("priority", "Output tokens priority", "0.0005250000"),
        _product("batch", "Input tokens", "0.0000350000",
                 feature="Batch Inference"),
    ])

    snapshot = resolver.resolve_snapshot(
        pricing,
        "us-east-1",
        {
            "gpt-oss-20b": [
                "openai.gpt-oss-20b",
                "openai.gpt-oss-20b-1:0",
            ]
        },
        {
            "anthropic.claude-opus-4-7": {
                "input_per_mtok": 5,
                "output_per_mtok": 25,
            },
            "global.anthropic.claude-opus-4-7": {
                "input_per_mtok": 5,
                "output_per_mtok": 25,
            },
            "us.anthropic.claude-opus-4-7": {
                "input_per_mtok": 5.5,
                "output_per_mtok": 27.5,
            },
        },
    )

    expected = {"input_per_mtok": 0.07, "output_per_mtok": 0.3}
    assert snapshot["openai.gpt-oss-20b"] == expected
    assert snapshot["openai.gpt-oss-20b-1:0"] == expected
    assert snapshot["anthropic.claude-opus-4-7"] == {
        "input_per_mtok": 5.0,
        "output_per_mtok": 25.0,
    }
    assert snapshot["global.anthropic.claude-opus-4-7"] == {
        "input_per_mtok": 5.0,
        "output_per_mtok": 25.0,
    }
    assert snapshot["us.anthropic.claude-opus-4-7"] == {
        "input_per_mtok": 5.5,
        "output_per_mtok": 27.5,
    }
    assert pricing.calls[0]["ServiceCode"] == "AmazonBedrock"


def test_snapshot_rejects_ambiguous_standard_price():
    pricing = _FakePricing([
        _product("input-a", "Input tokens", "0.0000700000"),
        _product("input-b", "Input tokens", "0.0000800000"),
        _product("output", "Output tokens", "0.0003000000"),
    ])

    with pytest.raises(ValueError, match="one standard on-demand input price"):
        resolver.resolve_snapshot(
            pricing,
            "us-east-1",
            {"gpt-oss-20b": ["openai.gpt-oss-20b"]},
            {},
        )


def test_fallback_is_never_lower_than_known_snapshot_prices():
    assert resolver.conservative_fallback(
        {
            "cheap": {"input_per_mtok": 1, "output_per_mtok": 2},
            "premium": {"input_per_mtok": 20, "output_per_mtok": 80},
        },
        {"input_per_mtok": 15, "output_per_mtok": 100},
    ) == {
        "input_per_mtok": 20.0,
        "output_per_mtok": 100.0,
    }


# ---------------------------------------------------------------------------
# Multi-dimension pricing: cache read/write and per-image rates
# ---------------------------------------------------------------------------


def test_snapshot_includes_cache_dimensions_when_the_catalog_publishes_them():
    """Observed Price List shape for Nova Lite in us-east-1: cache rows use
    inferenceType 'Prompt cache read/write input tokens', unit '1K tokens',
    feature 'On-demand Inference'."""
    pricing = _FakePricing([
        _product("in", "Input tokens", "0.0000600000",
                 feature="On-demand Inference", model="Nova Lite"),
        _product("out", "Output tokens", "0.0002400000",
                 feature="On-demand Inference", model="Nova Lite"),
        _product("cr", "Prompt cache read input tokens", "0.0000150000",
                 feature="On-demand Inference", model="Nova Lite"),
        _product("cw", "Prompt cache write input tokens", "0.0000000000",
                 feature="On-demand Inference", model="Nova Lite"),
        # Customization / batch / flex rows must be ignored.
        _product("cr-custom", "Prompt cache read input tokens", "0.0000150000",
                 feature="Model Customization", model="Nova Lite"),
        _product("cr-flex", "Prompt cache read input tokens flex", "0.0000100000",
                 feature="On-demand Inference", model="Nova Lite"),
    ])

    snapshot = resolver.resolve_snapshot(
        pricing, "us-east-1", {"Nova Lite": ["amazon.nova-lite-v1:0"]}, {}
    )

    assert snapshot["amazon.nova-lite-v1:0"] == {
        "input_per_mtok": 0.06,
        "output_per_mtok": 0.24,
        "cache_read_per_mtok": 0.015,
        "cache_write_per_mtok": 0.0,
    }


def test_snapshot_omits_cache_dimensions_when_absent():
    pricing = _FakePricing([
        _product("in", "Input tokens", "0.0000700000"),
        _product("out", "Output tokens", "0.0003000000"),
    ])
    snapshot = resolver.resolve_snapshot(
        pricing, "us-east-1", {"gpt-oss-20b": ["openai.gpt-oss-20b"]}, {}
    )
    # Exactly the input/output pair: nothing extra is invented.
    assert snapshot["openai.gpt-oss-20b"] == {
        "input_per_mtok": 0.07,
        "output_per_mtok": 0.3,
    }


def test_snapshot_rejects_ambiguous_optional_cache_price():
    pricing = _FakePricing([
        _product("in", "Input tokens", "0.0000700000"),
        _product("out", "Output tokens", "0.0003000000"),
        _product("cr-a", "Prompt cache read input tokens", "0.0000100000"),
        _product("cr-b", "Prompt cache read input tokens", "0.0000200000"),
    ])
    with pytest.raises(ValueError, match="at most one .*cache_read_per_mtok"):
        resolver.resolve_snapshot(
            pricing, "us-east-1", {"gpt-oss-20b": ["openai.gpt-oss-20b"]}, {}
        )


def test_image_model_snapshot_carries_per_image_rate():
    """Observed Nova Canvas rows: inferenceType 'T2I 1024 Standard' etc.,
    unit 'image', and NO token rows at all. The resolver zeroes the token
    pair by construction and contributes the smallest standard per-image
    rate as the estimate."""
    pricing = _FakePricing([
        _product("t2i-1024-std", "T2I 1024 Standard", "0.04",
                 feature="On-demand Inference", model="Nova Canvas",
                 unit="image"),
        _product("t2i-1024-prem", "T2I 1024 Premium", "0.06",
                 feature="On-demand Inference", model="Nova Canvas",
                 unit="image"),
        _product("t2i-2048-std", "T2I 2048 Standard", "0.06",
                 feature="On-demand Inference", model="Nova Canvas",
                 unit="image"),
        _product("i2i", "I2I 1024 Standard", "0.04",
                 feature="On-demand Inference", model="Nova Canvas",
                 unit="image"),
    ])
    snapshot = resolver.resolve_snapshot(
        pricing, "us-east-1", {"Nova Canvas": ["amazon.nova-canvas-v1:0"]}, {}
    )
    assert snapshot["amazon.nova-canvas-v1:0"] == {
        "input_per_mtok": 0.0,
        "output_per_mtok": 0.0,
        "per_image": 0.04,
    }


def test_model_with_neither_token_nor_image_rows_still_fails():
    pricing = _FakePricing([
        _product("ptu", "", "55", feature="Provisioned Throughput Inference - 1 month",
                 model="Nova Reel", unit="hour"),
    ])
    with pytest.raises(ValueError, match="one standard on-demand input price"):
        resolver.resolve_snapshot(
            pricing, "us-east-1", {"Nova Reel": ["amazon.nova-reel-v1:0"]}, {}
        )


def test_pinned_prices_keep_optional_dimensions():
    snapshot = resolver.resolve_snapshot(
        _FakePricing([]),
        "us-east-1",
        {},
        {
            "amazon.nova-canvas-v1:0": {
                "input_per_mtok": 0,
                "output_per_mtok": 0,
                "per_image": 0.08,
            },
            "anthropic.claude-haiku-4-5-20251001-v1:0": {
                "input_per_mtok": 1.0,
                "output_per_mtok": 5.0,
                "cache_read_per_mtok": 0.1,
                "cache_write_per_mtok": 1.25,
            },
        },
    )
    assert snapshot["amazon.nova-canvas-v1:0"]["per_image"] == 0.08
    assert snapshot["anthropic.claude-haiku-4-5-20251001-v1:0"] == {
        "input_per_mtok": 1.0,
        "output_per_mtok": 5.0,
        "cache_read_per_mtok": 0.1,
        "cache_write_per_mtok": 1.25,
    }


def test_conservative_fallback_covers_every_known_dimension_at_the_max_rate():
    fallback = resolver.conservative_fallback(
        {
            "a": {
                "input_per_mtok": 1,
                "output_per_mtok": 2,
                "cache_read_per_mtok": 0.1,
            },
            "b": {
                "input_per_mtok": 20,
                "output_per_mtok": 80,
                "cache_read_per_mtok": 2.0,
                "cache_write_per_mtok": 25.0,
            },
            "img": {"input_per_mtok": 0, "output_per_mtok": 0, "per_image": 0.08},
        },
        {"input_per_mtok": 15, "output_per_mtok": 100},
    )
    assert fallback == {
        "cache_read_per_mtok": 2.0,
        "cache_write_per_mtok": 25.0,
        "input_per_mtok": 20.0,
        "output_per_mtok": 100.0,
        "per_image": 0.08,
    }


class _FakeSsm:
    def __init__(self, *, missing_on_delete: bool = False):
        self.puts = []
        self.deletes = []
        self.missing_on_delete = missing_on_delete

    def put_parameter(self, **kwargs):
        self.puts.append(kwargs)
        return {"Version": len(self.puts)}

    def delete_parameter(self, **kwargs):
        self.deletes.append(kwargs)
        if self.missing_on_delete:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "gone"}},
                "DeleteParameter",
            )
        return {}


_PROPERTIES = {
    "RegionCode": "us-east-1",
    "ParameterName": "/bedrock-spend-controls/TestStack/model-prices",
    "CatalogModels": {"gpt-oss-20b": ["openai.gpt-oss-20b"]},
    "PinnedPrices": {
        "us.anthropic.claude-opus-4-7": {
            "input_per_mtok": 5.5,
            "output_per_mtok": 27.5,
        }
    },
    "FallbackPrice": {"input_per_mtok": 15.0, "output_per_mtok": 75.0},
}


def test_delete_removes_the_parameter_and_does_not_query_pricing(monkeypatch):
    monkeypatch.setattr(
        resolver.boto3,
        "client",
        lambda *args, **kwargs: pytest.fail("Pricing must not run on delete"),
    )
    for ssm in (_FakeSsm(), _FakeSsm(missing_on_delete=True)):
        result = resolver.handler(
            {"RequestType": "Delete", "ResourceProperties": _PROPERTIES},
            None,
            ssm_client=ssm,
        )
        assert result == {"PhysicalResourceId": "bedrock-model-prices-us-east-1"}
        assert ssm.deletes == [{"Name": _PROPERTIES["ParameterName"]}]


def test_create_writes_the_parameter_and_returns_only_a_digest():
    """H-2: CloudFormation caps custom-resource responses at 4,096 bytes;
    the snapshot goes to Parameter Store and only a digest comes back."""
    pricing = _FakePricing([
        _product("in", "Input tokens", "0.0000700000"),
        _product("out", "Output tokens", "0.0003000000"),
    ])
    ssm = _FakeSsm()
    result = resolver.handler(
        {"RequestType": "Create", "ResourceProperties": _PROPERTIES},
        None,
        pricing_client=pricing,
        ssm_client=ssm,
    )
    assert result["PhysicalResourceId"] == "bedrock-model-prices-us-east-1"
    data = result["Data"]
    assert set(data) == {
        "ParameterName", "SnapshotDigest", "ModelCount", "ResolvedAt",
        "FallbackPriceJson",
    }
    assert data["ParameterName"] == _PROPERTIES["ParameterName"]
    assert len(data["SnapshotDigest"]) == 16
    assert data["ModelCount"] == "2"
    assert json.loads(data["FallbackPriceJson"]) == {
        "input_per_mtok": 15.0,
        "output_per_mtok": 75.0,
    }
    assert "ModelPricesJson" not in data
    (put,) = ssm.puts
    assert put["Name"] == _PROPERTIES["ParameterName"]
    assert put["Tier"] == "Intelligent-Tiering"
    assert put["Overwrite"] is True
    assert put["Value"].startswith(resolver.COMPRESSED_PREFIX)
    value = resolver.decode_parameter_value(put["Value"])
    assert set(value) == {"models", "fallback", "resolved_at"}
    assert value["models"]["openai.gpt-oss-20b"] == {
        "input_per_mtok": 0.07,
        "output_per_mtok": 0.3,
    }
    assert value["resolved_at"] == data["ResolvedAt"]


def _shipped_catalog_pricing(catalog_models: dict) -> _FakePricing:
    """Price List rows for every shipped catalog model, shaped like the
    observed offer: Nova text models publish cache read/write rows, Nova
    Canvas publishes image rows only, every other family publishes the
    input/output pair. Rates are realistic 4-6 significant-digit values so
    the measured document size matches a real deployment."""
    products = []
    for catalog_model in catalog_models:
        if catalog_model == "Nova Canvas":
            products.append(
                _product(
                    f"{catalog_model}-t2i", "T2I 1024 Standard", "0.04",
                    feature="On-demand Inference", model=catalog_model,
                    unit="image",
                )
            )
            continue
        rows = [
            ("in", "Input tokens", "0.0003500000"),
            ("out", "Output tokens", "0.0014000000"),
        ]
        if catalog_model.startswith("Nova"):
            rows += [
                ("cr", "Prompt cache read input tokens", "0.0000875000"),
                ("cw", "Prompt cache write input tokens", "0.0000000000"),
            ]
        for sku, inference_type, rate in rows:
            products.append(
                _product(
                    f"{catalog_model}-{sku}", inference_type, rate,
                    feature="On-demand Inference", model=catalog_model,
                )
            )
    return _FakePricing(products)


def _shipped_model_config() -> dict:
    from pathlib import Path

    return json.loads(
        (Path(__file__).resolve().parents[1] / "cdk" / "config" / "model-pricing.json")
        .read_text(encoding="utf-8")
    )


def test_shipped_catalog_response_stays_far_under_the_cloudformation_limit():
    """H-2 with the real cdk/config/model-pricing.json: every catalog model
    resolved plus every pinned override. The custom-resource response must
    stay well under CloudFormation's 4,096-byte cap regardless of catalog
    size, and the stored document under Parameter Store's 8 KB
    Intelligent-Tiering cap."""
    config = _shipped_model_config()
    pinned = {
        model_id: {k: v for k, v in price.items() if k != "reason"}
        for model_id, price in config["price_overrides"].items()
    }
    pricing = _shipped_catalog_pricing(config["catalog_models"])
    snapshot, fallback = resolver._resolve(
        pricing,
        {
            "RegionCode": "us-east-1",
            "CatalogModels": config["catalog_models"],
            "PinnedPrices": pinned,
            "FallbackPrice": config["fallback_price"],
        },
    )
    total_models = len(pinned) + sum(
        len(ids) for ids in config["catalog_models"].values()
    )
    assert len(snapshot) == total_models

    stored, dropped = resolver.minimize_snapshot(snapshot)
    for model_id in dropped:
        prefix, _, base = model_id.partition(".")
        assert prefix in resolver._PROFILE_PREFIXES
        assert pinned[base] == pinned[model_id]

    ssm = _FakeSsm()
    result = resolver.handler(
        {
            "RequestType": "Update",
            "ResourceProperties": {
                "RegionCode": "us-east-1",
                "ParameterName": "/bedrock-spend-controls/Stack/model-prices",
                "CatalogModels": config["catalog_models"],
                "PinnedPrices": pinned,
                "FallbackPrice": config["fallback_price"],
            },
        },
        None,
        pricing_client=pricing,
        ssm_client=ssm,
    )
    # CloudFormation wraps Data in a ~300-byte envelope; keep the payload
    # under 1 KB so even a doubled catalog cannot approach 4 KB.
    response_bytes = len(json.dumps(result).encode("utf-8"))
    assert response_bytes < 1024, response_bytes

    # The stored value is gzip+base64 behind an explicit marker. The plain
    # JSON for this catalog is over Parameter Store's 8 KB cap on its own
    # (that is why it is compressed); the encoded value must sit well under
    # the resolver's own 7,800-byte ceiling so a materially larger catalog
    # still deploys. Both sizes are reported so a regression is legible.
    (put,) = ssm.puts
    value = put["Value"]
    assert value.startswith(resolver.COMPRESSED_PREFIX)
    encoded_bytes = len(value.encode("utf-8"))
    decoded = resolver.decode_parameter_value(value)
    json_bytes = len(
        json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    assert json_bytes > resolver.PARAMETER_MAX_BYTES, (
        f"shipped catalog JSON shrank to {json_bytes} bytes; compression is "
        "no longer load-bearing, revisit this assertion"
    )
    assert encoded_bytes < resolver.PARAMETER_VALUE_MAX_BYTES // 4, (
        f"shipped catalog encodes to {encoded_bytes} bytes (from "
        f"{json_bytes} bytes of JSON, {len(stored)} stored of {total_models} "
        f"models); expected well under the {resolver.PARAMETER_VALUE_MAX_BYTES}"
        f"-byte resolver ceiling / {resolver.PARAMETER_MAX_BYTES}-byte "
        "Parameter Store cap"
    )
    assert set(decoded) == {"models", "fallback", "resolved_at"}
    assert decoded["models"] == stored
    assert decoded["resolved_at"] == result["Data"]["ResolvedAt"]
    assert int(result["Data"]["ModelCount"]) == total_models
    # Only Regional pins identical to their base model are omitted; the
    # processor derives those itself.
    for model_id in set(pinned) - set(decoded["models"]):
        prefix, _, base = model_id.partition(".")
        assert prefix in resolver._PROFILE_PREFIXES
        assert pinned[base] == pinned[model_id]


def test_parameter_value_round_trips_and_is_deterministic():
    document = json.dumps(
        {
            "models": {"a.m": {"input_per_mtok": 1, "output_per_mtok": 2.5}},
            "fallback": {"input_per_mtok": 15, "output_per_mtok": 75},
            "resolved_at": "2026-10-02T00:00:00+00:00",
        },
        sort_keys=True, separators=(",", ":"),
    )
    encoded = resolver.encode_parameter_value(document)
    assert encoded.startswith("gz1:")
    assert resolver.decode_parameter_value(encoded) == json.loads(document)
    # Same document, same bytes: no spurious parameter versions.
    assert resolver.encode_parameter_value(document) == encoded
    # Legacy plain JSON (pre-compression template versions) still decodes.
    assert resolver.decode_parameter_value(document) == json.loads(document)
    with pytest.raises(ValueError, match="Unrecognised price parameter encoding"):
        resolver.decode_parameter_value("not-a-marker")


def test_minimize_snapshot_keeps_regional_uplifts_and_compacts_rates():
    snapshot = {
        "anthropic.m": {"input_per_mtok": 5.0, "output_per_mtok": 25.0},
        # Identical to base: derived by the processor, dropped here.
        "global.anthropic.m": {"input_per_mtok": 5.0, "output_per_mtok": 25.0},
        # Regional uplift: kept.
        "us.anthropic.m": {"input_per_mtok": 5.5, "output_per_mtok": 27.5},
        # Unknown prefix: kept even if equal.
        "x.anthropic.m": {"input_per_mtok": 5.0, "output_per_mtok": 25.0},
        # No base entry: kept.
        "eu.other.m": {"input_per_mtok": 1.0, "output_per_mtok": 2.0},
    }
    stored, dropped = resolver.minimize_snapshot(snapshot)
    assert dropped == ["global.anthropic.m"]
    assert stored["anthropic.m"] == {"input_per_mtok": 5, "output_per_mtok": 25}
    assert stored["us.anthropic.m"] == {"input_per_mtok": 5.5, "output_per_mtok": 27.5}
    assert set(stored) == {"anthropic.m", "us.anthropic.m", "x.anthropic.m", "eu.other.m"}
    # Rendered ints parse back to the same float rates.
    assert float(json.loads(json.dumps(stored))["anthropic.m"]["input_per_mtok"]) == 5.0


def test_oversized_parameter_document_is_refused():
    import hashlib

    # Incompressible model IDs and rates: repetitive synthetic names would
    # gzip to almost nothing and never trip the ceiling.
    huge = {}
    for index in range(600):
        digest = hashlib.sha256(str(index).encode()).hexdigest()
        huge[f"provider.{digest}"] = {
            "input_per_mtok": int(digest[:6], 16) / 1_000_000,
            "output_per_mtok": int(digest[6:12], 16) / 1_000_000,
        }
    ssm = _FakeSsm()
    with pytest.raises(ValueError, match="stay under 7800 bytes"):
        resolver.write_price_parameter(
            ssm, "/p", huge, {"input_per_mtok": 1.0, "output_per_mtok": 2.0}
        )
    assert ssm.puts == []


def test_scheduled_refresh_writes_the_price_parameter(monkeypatch):
    monkeypatch.setenv("PRICES_PARAMETER_NAME", "/quota/model-prices")
    pricing = _FakePricing([
        _product("in", "Input tokens", "0.0000700000"),
        _product("out", "Output tokens", "0.0003000000"),
    ])
    ssm = _FakeSsm()

    # EventBridge delivers the same properties shape as the deploy-time
    # custom resource.
    result = resolver.scheduled_handler(
        {
            "RegionCode": "us-east-1",
            "CatalogModels": {"gpt-oss-20b": ["openai.gpt-oss-20b"]},
            "PinnedPrices": {
                "us.anthropic.claude-opus-4-7": {
                    "input_per_mtok": 5.5,
                    "output_per_mtok": 27.5,
                }
            },
            "FallbackPrice": {
                "input_per_mtok": 15.0,
                "output_per_mtok": 75.0,
            },
        },
        None,
        pricing_client=pricing,
        ssm_client=ssm,
    )

    assert result["models"] == 2
    assert len(ssm.puts) == 1
    put = ssm.puts[0]
    assert put["Name"] == "/quota/model-prices"
    assert put["Overwrite"] is True
    assert put["Tier"] == "Intelligent-Tiering"
    # The scheduled refresh uses the same writer and therefore the same
    # compressed encoding as the deploy-time custom resource.
    assert put["Value"].startswith(resolver.COMPRESSED_PREFIX)
    value = resolver.decode_parameter_value(put["Value"])
    assert value["models"]["openai.gpt-oss-20b"] == {
        "input_per_mtok": 0.07,
        "output_per_mtok": 0.3,
    }
    assert value["models"]["us.anthropic.claude-opus-4-7"] == {
        "input_per_mtok": 5.5,
        "output_per_mtok": 27.5,
    }
    # Conservative fallback is never lower than any known price.
    assert value["fallback"] == {
        "input_per_mtok": 15.0,
        "output_per_mtok": 75.0,
    }
    assert value["resolved_at"]


def test_scheduled_refresh_failure_leaves_parameter_unwritten(monkeypatch):
    monkeypatch.setenv("PRICES_PARAMETER_NAME", "/quota/model-prices")
    # Ambiguous catalog data must fail the refresh, not write bad prices.
    pricing = _FakePricing([
        _product("in-a", "Input tokens", "0.0000700000"),
        _product("in-b", "Input tokens", "0.0000900000"),
        _product("out", "Output tokens", "0.0003000000"),
    ])
    ssm = _FakeSsm()

    with pytest.raises(ValueError):
        resolver.scheduled_handler(
            {
                "RegionCode": "us-east-1",
                "CatalogModels": {"gpt-oss-20b": ["openai.gpt-oss-20b"]},
            },
            None,
            pricing_client=pricing,
            ssm_client=ssm,
        )
    assert ssm.puts == []
