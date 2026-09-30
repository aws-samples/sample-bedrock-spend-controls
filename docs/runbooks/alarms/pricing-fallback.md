# PricingFallbackAlarm

**Operations key:** `pricing_fallback` · **Metric:** `FallbackPricedRequests` (Sum ≥ 1 over 5 min, 1 period; missing data = not breaching) · **Emitted by:** `usage_processor/handler.py` `_emit_emf` — `1` when `price_source == "fallback"` **or** `missing_dimensions` is non-empty.

## What it means

At least one metered invocation was priced with the synthetic conservative
rate instead of a resolved catalog price. Two distinct situations set the
metric:

1. **Unknown model** (`PriceSource: fallback`). The `modelId` in the
   invocation log has no exact entry, no base-model entry after stripping a
   cross-Region prefix (`us.`, `eu.`, `global.`…), so the whole request was
   priced at the fallback (`fallback_price` in `cdk/config/model-pricing.json`,
   raised at deploy to at least the highest known rate). This
   **over-estimates** and can block a
   subject earlier than the bill would justify.
2. **Known model, missing dimension** (`PriceSource: snapshot` or
   `base-model`, `MissingDimensions: [...]`, `UnpricedDimensionRequests: 1`).
   The record carried prompt-cache tokens or an image count that the model's
   catalog entry has no rate for. That dimension was priced at the fallback's
   rate for it when one exists (the resolver builds the fallback per
   dimension), otherwise at zero — and the subject's daily row was flagged
   (`unpriced_requests`, `missing_dimensions`). This may **under-estimate**.

Neither is silent mispricing; both are catalog gaps to close. The typical
trigger is a model billed through AWS Marketplace (for example Anthropic
models): it has no Price List row, so unless it is pinned in
`price_overrides` every request is priced at the conservative fallback.

## Severity guidance

**Next business day** for a trickle. **Same day** if the affected subject is
close to a block threshold (over-estimation could block them) or if a
high-volume production model is unpriced (under-estimation means the USD
quota is not protecting the bill). Never a page — enforcement still works;
only the USD figure is imprecise.

## Likely causes

1. **A model was added to `allowed_model_arns` without a catalog entry.**
   The SSM price parameter lacks it.
2. **A new inference-profile ID** (`us.`, `global.` prefix) whose base model
   is *also* missing, or a **geographic profile with a different price** that
   should be pinned explicitly (the base-model derivation would under-count).
3. **Prompt caching enabled on a Marketplace model** (Anthropic) whose
   `price_overrides` entry has only the token pair —
   `MissingDimensions: ["cache_read"]` / `["cache_write"]`.
4. **Image generation** against an entry with no `per_image` rate, or
   image-data delivery is disabled so the record has no image count (then
   `images: 0` and cost `$0` with **no** flag — see the caveats in
   [pricing.md](../../pricing.md#priced-dimensions); the alarm does not catch
   that case).
5. **Price refresh failing**, so the SSM parameter is stale. Check the
   `PriceRefreshFn` `Errors` metric — it is not alarmed separately.

Find which models and dimensions:
```bash
aws logs start-query --log-group-name /aws/lambda/<UsageProcessorFn> \
  --start-time $(( $(date +%s) - 86400 )) --end-time $(date +%s) \
  --query-string 'filter FallbackPricedRequests = 1 | stats count() as requests, sum(EstimatedCostUSD) as usd by Model, PriceSource, MissingDimensions | sort requests desc'
# then aws logs get-query-results --query-id <id>
```
Ledger rows already flagged: `tools/unpriced_usage.py --table <UsageTableName>`.

## Remediation

1. **Add the price.** For a Price List model, add it to
   `catalog_models` in `cdk/config/model-pricing.json`; for a Marketplace or
   otherwise unlisted model, add a `price_overrides` entry with `input_per_mtok`,
   `output_per_mtok`, and — if the alarm named them — `cache_read_per_mtok`,
   `cache_write_per_mtok`, `per_image`, plus a `reason`. Redeploy; the daily
   refresh also picks up catalog-driven changes without a redeploy.
2. **Verify the resolver can price it** before deploying:
   `cdk/.venv/bin/python tools/bedrock_price_catalog.py --region <region>
   --metering-compatible` lists the rows the Price List publishes.
3. **Decide on repair.** Historical daily aggregates are never repriced
   automatically. If the fallback *over*-estimated and blocked a subject,
   the automatic block lifts on the next window; you may unblock early with
   a reasoned `PUT /admin/user/status`. If it *under*-estimated, the
   `unpriced_requests` / `missing_dimensions` attributes tell you which rows
   to correct by hand if the bill matters.
4. If the model should not be callable at all, remove it from
   `allowed_model_arns` instead of pricing it.

## How to verify recovery

- New invocations of the model log `PriceSource: snapshot` (or
  `base-model`) with `MissingDimensions: []`.
- `FallbackPricedRequests` Sum = 0 over a 5-minute period → `OK`
  (`treat_missing_data=NOT_BREACHING`, so a quiet period clears it too).

## Related

- [pricing.md](../../pricing.md): priced dimensions, unpriced dimensions, and the catalog file
- Component: [components/pricing-resolver.md](../components/pricing-resolver.md), [components/usage-processor.md](../components/usage-processor.md)
- Metrics: `FallbackPricedRequests`, `UnpricedDimensionRequests`
