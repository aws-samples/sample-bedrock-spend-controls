# Pricing and USD estimates

The USD figure in the ledger is an **estimate**: every metered invocation is
priced from a catalog that the stack resolves from the AWS Price List at
deployment and refreshes daily. Token quotas do not depend on it. To check
that the estimate tracks the bill, enable reconciliation
([configuration.md](configuration.md#reconciliation)).

## Where prices come from

| Source | Used for | Refresh |
|---|---|---|
| AWS Price List (`pricing:GetProducts`) | Every model listed in `catalog_models` | Resolved at `cdk deploy` by `PriceResolverFn`; re-resolved daily (`rate(1 day)`) by `PriceRefreshFn` into the SSM parameter named by the `ModelPricesParameterName` output (`/bedrock-spend-controls/<stack>/model-prices`) |
| `price_overrides` in `cdk/config/model-pricing.json` | Models the Price List does not publish (models billed through AWS Marketplace, geographic inference profiles with a different rate) | Redeploy |
| `fallback_price` | Any model ID not resolved by the two sources above | Redeploy; raised at deploy time to at least the highest known rate per dimension |

The SSM parameter is Intelligent-Tiering and holds the JSON table compressed:
the value is `gz1:` followed by base64-encoded gzip (`encode_parameter_value`
in `cdk/pricing_resolver/handler.py`), so the shipped catalog fits well below
the 8 KB advanced-parameter limit. Read it with
`aws ssm get-parameter --name <ModelPricesParameterName> --query Parameter.Value --output text | cut -c5- | base64 -d | gunzip`.
The `ModelPriceSnapshot` output is a digest only
(`ssm:<name> sha256:<digest> models=<count> unresolved=<n>`); the custom
resource returns `ParameterName`, `SnapshotDigest`, `ModelCount`,
`ResolvedAt`, `FallbackPriceJson`, `UnresolvedCount`, and
`UnresolvedModels`, never the table itself.

The usage processor reads the parameter with a 15-minute cache and keeps
the last good value if a read fails. It has no built-in price table: if the
parameter is unreadable at a cold start every request is priced at the
fallback and the `pricing_fallback` alarm fires. Updating prices affects
future invocations only: existing daily aggregates and blocked statuses are
not repriced.

Resolution order for one invocation-log record (`PriceSource` in the EMF
log line):

1. Exact match on the `modelId` the log carries (`snapshot`): a pinned
   profile ID such as `us.<model>` wins over its base model.
2. The base foundation model after stripping a geographic or cross-Region
   prefix (`us.`, `eu.`, `apac.`, `jp.`, `au.`, `global.`, ...)
   (`base-model`). When a *sibling* geographic pin for the same base model
   is priced differently from the base (for example `eu.<model>` is pinned
   but the record says `apac.<model>`), the base rate is probably wrong for
   this profile too: the request is still priced at the base rate but
   reported as `base-model-mismatch`, which counts in `FallbackPricedRequests`
   so the missing geography gets pinned.
3. `fallback_price` (`fallback`). Requests priced this way emit
   `FallbackPricedRequests` and raise the `pricing_fallback` alarm
   ([runbooks/alarms/pricing-fallback.md](runbooks/alarms/pricing-fallback.md)).

### Models the Region does not price

A `catalog_models` entry the Price List cannot price in the target Region
(the model is not offered there, or its standard on-demand rows carry two
different rates for one dimension) does not fail the deploy. The resolver
skips it, logs one warning per model (`Catalog model not priced in this
Region; its invocations use the fallback price`), lists the skipped Price
List names under `unresolved` in the parameter document, and reports them
to CloudFormation as `UnresolvedCount` and `UnresolvedModels`, so the
`ModelPriceSnapshot` output ends with `unresolved=<n>`. An invocation of
such a model is priced at the fallback (`PriceSource: fallback`), emits
`FallbackPricedRequests`, and raises the `pricing_fallback` alarm: a skipped
model that is actually called is never silent. Pin it in `price_overrides`
with its published rate, or keep it out of `allowed_model_arns`.

The daily refresh applies the same rule to a model that was never priced,
but refuses to demote one: when the Price List stops answering for a model
the live parameter already prices, `PriceRefreshFn` raises instead of
writing a table without it (`_refuse_regression` in
`cdk/pricing_resolver/handler.py`), the previous value stays in place, and
the failure shows in the function's `Errors` metric
([runbook](runbooks/components/pricing-resolver.md)). The refresher reads
the live parameter for that comparison, which is why it holds
`ssm:GetParameter` next to `ssm:PutParameter`.

## `cdk/config/model-pricing.json`

```json
{
  "catalog_models": {
    "<Price List model name>": ["<runtime model ID>", "..."]
  },
  "price_overrides": {
    "<runtime model or profile ID>": {
      "input_per_mtok": 0.0,
      "output_per_mtok": 0.0,
      "cache_read_per_mtok": 0.0,
      "cache_write_per_mtok": 0.0,
      "per_image": 0.0,
      "reason": "why the Price List cannot price this ID"
    }
  },
  "fallback_price": {"input_per_mtok": 0.0, "output_per_mtok": 0.0}
}
```

Validation at synthesis:

- Every price entry requires `input_per_mtok` and `output_per_mtok` (USD per
  million tokens). `cache_read_per_mtok`, `cache_write_per_mtok` (USD per
  million tokens) and `per_image` (USD per generated image) are optional.
  Unknown keys fail synthesis.
- The token pair must be positive, except for an image model (`per_image >
  0`) whose token rates are pinned at `0`. `cache_write_per_mtok` may be `0`
  where the catalog publishes a zero rate.
- `fallback_price` must carry a positive token pair.
- Every `price_overrides` entry must carry a `reason`.
- Model and inference-profile IDs are matched literally. A profile that has
  its own price must appear with its exact ID; prefixes are not inferred for
  pinned entries.
- An ambiguous catalog (two different rates for one dimension of one model)
  is never averaged: at deploy and refresh time the model is treated as
  unpriced in that Region ([above](#models-the-region-does-not-price)); the
  strict resolver that tests and tooling use raises `UnresolvedPrice`.

The shipped file is the reference for current values; do not copy prices
from documentation. It carries 54 pins. Every Claude pin, including the
`us.`, `eu.`, `apac.`, `jp.`, `au.`, and `global.` inference-profile IDs,
is pinned explicitly with its own token pair (`global.` at the base rate,
the regional profiles 10 % above it), `cache_read_per_mtok` (10 % of the
input rate) and `cache_write_per_mtok` (125 % of the input rate), so
geographic profiles are never derived from the base model; Nova 2 Lite
carries its published cache rates the same way. Review the fallback whenever you allow
a more expensive model or a modality that is not billed by input/output
tokens: it is conservative, not a universal upper bound.

## Priced dimensions

The usage processor prices every dimension an invocation-log record carries
against the resolved entry for its model.

| Dimension | Log field | Catalog key | Unit | Status |
|---|---|---|---|---|
| Input tokens | `input.inputTokenCount` | `input_per_mtok` | USD / MTok | Priced (required) |
| Output tokens | `output.outputTokenCount` | `output_per_mtok` | USD / MTok | Priced (required) |
| Prompt-cache read | `input.cacheReadInputTokenCount` | `cache_read_per_mtok` | USD / MTok | Priced when the entry has the rate |
| Prompt-cache write | `input.cacheWriteInputTokenCount` | `cache_write_per_mtok` | USD / MTok | Priced when the entry has the rate |
| Generated images | `output.outputBodyJson.images[]` length (or `output.outputImageCount`) | `per_image` | USD / image | Priced when the count is present; see caveats |
| Video / audio seconds, embeddings | not present in the invocation log | — | — | **Not priced** |

The Price List resolver reads `Prompt cache read input tokens` / `Prompt
cache write input tokens` rows for catalog models that publish them and the
smallest standard text-to-image `image` row for image models. Models billed
through AWS Marketplace have no Price List rows, so their cache rates must be
pinned in `price_overrides` next to the token pair.

Token quotas count `inputTokenCount` (uncached) plus `outputTokenCount`.
Cached tokens accumulate in separate `cache_read_tokens` /
`cache_write_tokens` ledger counters and in the USD estimate, never against
the input-token limit.

### Caveats

- **Image generation with image delivery disabled.** The stack-managed
  logging configuration sets `imageDataDeliveryEnabled: false`. An image
  model record then carries no token counts and no body, so the processor
  cannot know how many images were generated: it assumes **one** image,
  prices it at the entry's `per_image` rate, counts `images: 1`, flags the
  row (`unpriced_requests`, `missing_dimensions: ["image"]`), and emits
  `FallbackPricedRequests` so the `pricing_fallback` alarm fires. A request
  that produced several images is under-counted. Enable image data delivery
  on the invocation-logging configuration, or keep image models out of
  `allowed_model_arns` if their spend must be enforced.
- **Image size and quality tiers.** The log reports a count only. The
  resolver uses the smallest standard text-to-image rate; premium or larger
  generations are under-counted unless you pin the higher rate in
  `price_overrides`.
- **Cache write at `$0`.** Some catalog models publish a `$0` cache-write
  rate; the processor records the tokens and prices them at zero as
  published.
- **Video, audio, and embeddings** are unmetered: no invocation-log field
  carries duration or embedding counts, and `StartAsyncInvoke` is not logged
  at all. Do not include such models in `allowed_model_arns` if strict
  accounting matters.
- **Service tiers** (flex, priority), provisioned throughput, batch
  inference, and separately billed tools (web search, code execution, and
  similar tool calls billed per use) are outside the estimate: the
  invocation log carries no field for them, so their spend never reaches the
  ledger and is never flagged as unpriced. Reconciliation is the only place
  it shows up.

## Unpriced dimensions

When a record carries a dimension its resolved entry has no rate for (a
cache-enabled call to a model whose pin lacks `cache_*_per_mtok`, an image
count against an entry without `per_image`, an image record without a
count), the processor:

1. prices that dimension at the fallback's rate for it (the fallback carries
   every dimension any known model prices, at the highest known rate);
2. increments `unpriced_requests` on the subject's daily row and adds the
   dimension name to the row's `missing_dimensions` set;
3. emits `FallbackPricedRequests` and `UnpricedDimensionRequests`, which
   raise the `pricing_fallback` alarm.

Nothing is silently priced at zero. The admin API returns
`unpriced_requests` on every usage response, the console shows an amber
warning on the subject when it is above zero, and `tools/unpriced_usage.py`
lists the affected rows, one line per subject and day with a `models`
column naming the models involved (per-model ledger rows are folded in, not
listed separately):

```bash
python tools/unpriced_usage.py \
  --table "$(aws cloudformation describe-stacks --stack-name BedrockSpendControls \
      --query "Stacks[0].Outputs[?OutputKey=='UsageTableName'].OutputValue | [0]" --output text)" \
  --since 2026-09-01
```

Add the missing rate to `cdk/config/model-pricing.json` for future events;
historical aggregates are never repriced automatically, so decide on a
manual repair for past rows if the bill matters.

## Exporting the regional price catalog

`tools/bedrock_price_catalog.py` downloads the public AWS Price List bulk
offer for Amazon Bedrock and exports every model-related price dimension,
grouped by Region. It needs no AWS credentials and preserves token, image,
video, request, hourly, and model-month units.

```bash
# All model prices in all published Regions
python3 tools/bedrock_price_catalog.py --output "$TMPDIR/bedrock-prices.json"

# One Region, as CSV
python3 tools/bedrock_price_catalog.py --region eu-south-2 --format csv \
  --output "$TMPDIR/bedrock-prices-eu-south-2.csv"

# Only rows with a standard on-demand input/output token rate
python3 tools/bedrock_price_catalog.py --region us-east-1 --metering-compatible \
  --output "$TMPDIR/bedrock-metering-prices-us-east-1.json"
```

The export records the catalog version and publication timestamp.
`--metering-compatible` selects rows with a standard on-demand USD
input/output token rate and computes `price_per_million_tokens`; it does not
map Price List model names to Bedrock Runtime IDs. Keep those aliases
explicit in `catalog_models`. Do not paste the whole export into the price
configuration: it spans many Regions and units and would exceed the 8 KB
SSM parameter limit even compressed. Use it for discovery, then map only the
exact Runtime IDs the deployment allows.

References: [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/),
[global cross-Region inference pricing](https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html).
