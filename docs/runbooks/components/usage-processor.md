# Component: usage processor (`UsageProcessorFn`)

**Code:** `usage_processor/handler.py`. **Log group:**
`/aws/lambda/<UsageProcessorFn>`. **Invocation:** CloudWatch Logs
subscription filters on the Bedrock model-invocation log group — one for
`identity.arn` matching the vended role, one (workload mode) for
`modelId` matching an application inference profile ARN. Timeout 2 min,
256 MB.

## What it does

For each invocation-log record: attributes it (workload profile ARN first,
then vended-role session → `SESSION#<name>` map), prices every dimension the
record carries (input/output tokens, prompt-cache read/write tokens, image
count) against the resolved catalog, and in **one `TransactWriteItems`**
writes the `REQUEST#<requestId>` idempotency marker, the subject's daily
ledger row, and the subject's per-model daily row
(`<subject>#model#<model_id>`). It then increments the per-minute rate
counter if the subject has rpm/tpm, emits EMF metrics, and re-evaluates the
subject: subject-level thresholds, rate limits, and every model budget. A
`block` threshold or rate breach flips the row to `blocked` (automatic
origin) and writes the `REVOCATION#<subject>` sentinel in the same
transaction; `warn` thresholds send one SNS message per level per window.

Prices come from the SSM parameter (15-minute cache, last-good on failure)
with the processor's built-in conservative defaults as the cold-start fallback.

## IAM scope

- Users table read/write (`GetItem`, `PutItem`, `UpdateItem`, `TransactWriteItems`; the sentinel is a `Put`).
- Usage table read/write.
- `sns:Publish` on `QuotaAlerts`.
- `ssm:GetParameter` on the model prices parameter.
- No IAM, no Bedrock.

## Inputs / outputs

| Input | Output |
|---|---|
| Gzipped CloudWatch Logs subscription payload | DynamoDB: `REQUEST#`, `<subject>/<date>`, `<subject>#model#<id>/<date>`, optional `RATE#<subject>/<minute>`; users row status/markers; `REVOCATION#` sentinel |
| Env: `MODEL_FALLBACK_PRICE_JSON`, `PRICES_PARAMETER_NAME`, `WORKLOAD_PROFILES_JSON`, `DEFAULT_LIMITS_JSON`, `WARN_THRESHOLD`, `USAGE_RETENTION_DAYS`, `BEDROCK_USER_ROLE_NAME` | EMF: `Requests`, `InputTokens`, `OutputTokens`, `CacheReadTokens`, `CacheWriteTokens`, `ImagesGenerated`, `EstimatedCostUSD`, `DetectionLagMilliseconds`, `FallbackPricedRequests`, `UnpricedDimensionRequests` (dimensions `UserId`, `Model`) |
| | SNS `WARNING <subject> [model <id>] <period> <pct>%`, `BLOCKED <subject> reason=...` |
| | Return value: `{processed, duplicates, ignored, unpriced, unresolved_sessions}` |

## Failure modes and where they surface

| Symptom | Where | Notes |
|---|---|---|
| `unresolved_sessions` non-empty | log `Invocation logs had unknown broker sessions` | The `SESSION#` row expired (TTL = `usage_retention_days`) or the record predates the deployment. Usage is **not** recorded. Rare unless retention is short. |
| `duplicates` > 0 | return value | Normal — at-least-once delivery. The transaction's conditional `Put` on `REQUEST#` rejects the retry. |
| Lambda `Errors` | Lambda metrics | Unhandled exception; CloudWatch Logs retries the batch. Common: DynamoDB throttling (on-demand ramps), `TransactionCanceledException` for a non-conditional reason (the code re-reads the marker before treating it as a duplicate, then re-raises). |
| `FallbackPricedRequests` | [pricing-fallback alarm](../alarms/pricing-fallback.md) | Model or dimension not in the catalog. |
| `DetectionLagMilliseconds` p95 climbing | dashboard, Operations tab | Bedrock log delivery or subscription backlog; the lease still bounds exposure. |
| Warning sent twice for one level | — | Should not happen: per-level markers `warning_sent_<period>_<at_bps>_window` (and `warning_sent_model_<id>_...`) are checked and set locally within a pass. |
| Status write `concurrent-change` | return of `_evaluate_quota` | Three optimistic attempts lost to another writer; the next record or the workload enforcer converges it. |

## Manual operations

- **Re-drive a lost window from the invocation logs** (e.g. after a long
  outage): CloudWatch Logs subscription retries for up to 24 h. Beyond that,
  export the records you need and re-invoke the function with a synthetic
  subscription event (`_subscription()` in `tests/test_usage_processor.py`
  shows the exact envelope). Idempotency markers make re-driving safe as
  long as their TTL has not passed.
- **Check what a subject was charged for a day:**
  `aws dynamodb get-item --table-name <UsageTableName> --key '{"user_id":{"S":"<subject>"},"window":{"S":"YYYY-MM-DD"}}'`.
  Per model: key `<subject>#model#<model_id>`. Flagged rows:
  `tools/unpriced_usage.py`.
- **Force price refresh visibility:** the parameter cache is per container
  and 15 minutes; a redeploy or waiting is the only lever.
- **Inspect a record the processor ignored:** run
  `handler._parse_invocation(log_event, role_name, workload_profiles)` locally
  against the raw message; `None` means no token metadata and not an image
  model, wrong role, or an unmanaged application profile.
