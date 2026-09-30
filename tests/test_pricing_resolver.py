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


def test_delete_does_not_query_pricing(monkeypatch):
    monkeypatch.setattr(
        resolver.boto3,
        "client",
        lambda *args, **kwargs: pytest.fail("Pricing must not run on delete"),
    )
    result = resolver.handler(
        {
            "RequestType": "Delete",
            "ResourceProperties": {"RegionCode": "us-east-1"},
        },
        None,
    )
    assert result == {"PhysicalResourceId": "bedrock-model-prices-us-east-1"}


class _FakeSsm:
    def __init__(self):
        self.puts = []

    def put_parameter(self, **kwargs):
        self.puts.append(kwargs)
        return {"Version": len(self.puts)}


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
    value = json.loads(put["Value"])
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
