# Component: pricing resolver and refresher (`PriceResolverFn`, `PriceRefreshFn`)

**Code:** `cdk/pricing_resolver/handler.py` — `handler` (CloudFormation
custom resource `Custom::BedrockModelPriceSnapshot`, deploy-time) and
`scheduled_handler` (`ModelPriceRefreshSchedule`, `rate(24 hours)`). Both
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
Ambiguity (two different rates for one dimension) **fails** the resolve.
`price_overrides` are copied through verbatim. The conservative fallback is
raised per dimension to the maximum known rate.

- **Deploy-time:** the custom resource returns `ModelPricesJson` and
  `FallbackPriceJson` as CloudFormation attributes; they seed the usage
  processor's env and the SSM parameter `ModelPricesParameter`.
- **Daily:** `scheduled_handler` re-resolves with the same properties (the
  EventBridge rule input mirrors the custom resource properties) and
  overwrites the SSM parameter. The usage processor reads the parameter with
  a 15-minute cache and keeps the last good value across failed reads.

## IAM scope

- `pricing:GetProducts` (`*` — the Pricing API has no resource-level permissions).
- Refresher: `ssm:PutParameter` on the one prices parameter.

## Failure modes

| Symptom | Where | Notes |
|---|---|---|
| `cdk deploy` fails at `BedrockModelPriceSnapshot` | CloudFormation events | A `catalog_models` entry has zero or ambiguous standard on-demand rows in the target Region. Fix the catalog name (see `tools/bedrock_price_catalog.py --region <r>`) or pin the model in `price_overrides`. |
| Daily refresh `Errors` = 1 | `PriceRefreshFn` Lambda `Errors` (no alarm configured) | Same ambiguity, or Pricing API throttling. Parameter left unchanged; metering continues on the previous value. **Add a CloudWatch alarm on this function's `Errors` if a stale catalog matters to you.** |
| Prices look stale in metering | usage processor log `Model price parameter unavailable` | SSM read failing; processor uses last good value / built-in defaults. |
| New dimension not priced | [pricing-fallback alarm](../alarms/pricing-fallback.md) | The Price List does not publish it for that model (Marketplace models) — pin it. |

## Manual operations

- **See what the resolver would produce** without deploying (needs
  `pricing:GetProducts`):
  ```bash
  cdk/.venv/bin/python - <<'PY'
  import json, sys, boto3
  sys.path.insert(0, "cdk")
  from pricing_resolver import handler as r
  cfg = json.load(open("cdk/config/model-pricing.json"))
  pinned = {k: {kk: vv for kk, vv in v.items() if kk != "reason"} for k, v in cfg["price_overrides"].items()}
  snap = r.resolve_snapshot(boto3.client("pricing", region_name="us-east-1"), "us-east-1", cfg["catalog_models"], pinned)
  print(json.dumps(snap, indent=1)); print("fallback", r.conservative_fallback(snap, cfg["fallback_price"]))
  PY
  ```
- **Force a refresh now:** `aws lambda invoke --function-name <PriceRefreshFn>
  --payload "$(aws events list-targets-by-rule --rule <ModelPriceRefreshSchedule rule name> --query 'Targets[0].Input' --output text)" --cli-binary-format raw-in-base64-out /dev/stdout`.
- **Read the live parameter:** `aws ssm get-parameter --name <parameter> --query Parameter.Value --output text | python3 -m json.tool` (`resolved_at` shows the last successful refresh).
- **Discover Price List rows for a model/Region:**
  `cdk/.venv/bin/python tools/bedrock_price_catalog.py --region eu-west-1 --format csv --output /tmp/prices.csv`.
