# Component: pricing resolver and refresher (`PriceResolverFn`, `PriceRefreshFn`)

**Code:** `cdk/pricing_resolver/handler.py` — `handler` (CloudFormation
custom resource `Custom::BedrockModelPriceSnapshot`, deploy-time) and
`scheduled_handler` (`ModelPriceRefreshSchedule`, `rate(1 day)`). Both
256 MB, 1 min timeout. **Log groups:** `/aws/lambda/<PriceResolverFn>`,
`/aws/lambda/<PriceRefreshFn>`.

## What it does

Resolves the deployment's `catalog_models` against the AWS Price List
(`pricing:GetProducts`, `ServiceCode=AmazonBedrock`, always queried in
`us-east-1`, filtered by `regionCode` and `model`), keeping only
`feature` ∈ {`""`, `On-demand Inference`} and `service_tier` ∈ {`""`,
`standard`} rows. Per model it extracts the token pair (`Input tokens`,
`Output tokens`, unit `1K tokens` → USD/MTok), optional
`Prompt cache read input tokens` / `Prompt cache write input tokens`, and
for image models the smallest standard text-to-image `image`-unit row
(`T2I 512|1024|2048 Standard`) as `per_image` with a zeroed token pair.
A catalog model with no single standard on-demand rate in the Region (not
offered there, or two different rates for one dimension) is **skipped**,
never averaged: it is recorded as *unresolved* (`UnresolvedPrice` in the
code), one warning line is logged per model, and its invocations are priced
at the fallback and alarmed. `price_overrides` are copied through verbatim.
The conservative fallback is raised per dimension to the maximum known rate.

- **Deploy-time:** the custom resource writes the resolved table itself to
  the SSM parameter `/bedrock-spend-controls/<stack>/model-prices`
  (Intelligent-Tiering; value `gz1:` + base64 of the gzipped JSON, see
  `encode_parameter_value`; the document carries `models`, `fallback`,
  `resolved_at`, and, when any, `unresolved`) and returns only
  `ParameterName`, `SnapshotDigest`, `ModelCount`, `ResolvedAt`,
  `FallbackPriceJson`, `UnresolvedCount`, and `UnresolvedModels` as
  CloudFormation attributes. The fallback seeds the usage processor's env;
  the digest, count, and unresolved count appear in the `ModelPriceSnapshot`
  output (`... models=<count> unresolved=<n>`).
- **Daily:** `scheduled_handler` re-resolves with the same properties (the
  EventBridge rule input mirrors the custom resource properties) and
  overwrites the SSM parameter. Before writing it compares the unresolved
  models with the live parameter (`_refuse_regression`): a model that was
  never priced is skipped quietly, as at deploy time, but if the Price List
  has stopped pricing a model the live table carries, the function raises
  (`Refusing to refresh the price parameter: ...`) and the previous value
  stays in place. The usage processor reads the parameter with a 15-minute
  cache and keeps the last good value across failed reads; it has no
  built-in price table, so an unreadable parameter at a cold start means
  fallback pricing (alarmed).

## IAM scope

- `pricing:GetProducts` (`*` — the Pricing API has no resource-level permissions).
- `PriceResolverFn`: `ssm:PutParameter` and `ssm:DeleteParameter` on the one
  prices parameter.
- `PriceRefreshFn`: `ssm:PutParameter` and `ssm:GetParameter` on the same
  parameter (the read is for the regression check above).

## Failure modes

| Symptom | Where | Notes |
|---|---|---|
| `ModelPriceSnapshot` ends with `unresolved=<n>`, n > 0 | Stack outputs; `PriceResolverFn` log (`Catalog model not priced in this Region`) | A `catalog_models` entry has zero or ambiguous standard on-demand rows in the target Region. The deploy succeeds without it; its invocations are priced at the fallback and raise `pricing_fallback`. Fix the catalog name (see `tools/bedrock_price_catalog.py --region <r>`), pin the model in `price_overrides`, or keep it out of `allowed_model_arns`. |
| Daily refresh `Errors` = 1 | `PriceRefreshFn` Lambda `Errors` (no alarm configured) | Pricing API throttling, or the refusal above: the Price List stopped pricing a model the live table carries (`Refusing to refresh the price parameter: the Price List no longer prices <ids> ...`). Parameter left unchanged; metering continues on the previous value. Decide whether to pin the model or drop it from `catalog_models`, then redeploy. **Add a CloudWatch alarm on this function's `Errors` if a stale catalog matters to you.** |
| Prices look stale in metering | usage processor log `Model price parameter unavailable` | SSM read failing; the processor keeps its last good value, or prices at the fallback on a cold start. |
| New dimension not priced | [pricing-fallback alarm](../alarms/pricing-fallback.md) | The Price List does not publish it for that model (Marketplace models) — pin it. |

## Manual operations

- **See what the resolver would produce** without deploying (needs
  `pricing:GetProducts`):
  ```bash
  python - <<'PY'
  import json, sys, boto3
  sys.path.insert(0, "cdk")
  from pricing_resolver import handler as r
  cfg = json.load(open("cdk/config/model-pricing.json"))
  pinned = {k: {kk: vv for kk, vv in v.items() if kk != "reason"} for k, v in cfg["price_overrides"].items()}
  unresolved = []  # omit the keyword to get the strict mode that raises UnresolvedPrice instead
  snap = r.resolve_snapshot(boto3.client("pricing", region_name="us-east-1"), "us-east-1", cfg["catalog_models"], pinned, unresolved=unresolved)
  print(json.dumps(snap, indent=1)); print("fallback", r.conservative_fallback(snap, cfg["fallback_price"]))
  print("unresolved", json.dumps(unresolved, indent=1))
  PY
  ```
- **Force a refresh now:** `aws lambda invoke --function-name <PriceRefreshFn>
  --payload "$(aws events list-targets-by-rule --rule <ModelPriceRefreshSchedule rule name> --query 'Targets[0].Input' --output text)" --cli-binary-format raw-in-base64-out /dev/stdout`.
- **Read the live parameter:** `aws ssm get-parameter --name <ModelPricesParameterName> --query Parameter.Value --output text | cut -c5- | base64 -d | gunzip | python3 -m json.tool` (`resolved_at` shows the last successful refresh).
- **Discover Price List rows for a model/Region:**
  `python3 tools/bedrock_price_catalog.py --region eu-west-1 --format csv --output "$TMPDIR/prices.csv"`.
