# Component: usage processor (`UsageProcessorFn`)

**Code:** `usage_processor/handler.py`. **Log group:**
`/aws/lambda/<UsageProcessorFn>`. **Invocation:** CloudWatch Logs
subscription filters on the Bedrock model-invocation log group — one for
`identity.arn` matching the vended role, one (workload mode) for
`modelId` matching an application inference profile ARN. The subscription
invokes the function asynchronously; a failed invocation is retried twice
(`retry_attempts=2`) and then delivered to `UsageProcessorDeadLetterQueue`
(14-day retention). Timeout 2 min, 256 MB.

## What it does

For each invocation-log record: attributes it (workload profile ARN first,
then vended-role session → `SESSION#<name>` map), prices every dimension the
record carries (input/output tokens, prompt-cache read/write tokens, image
count) against the resolved catalog, and in **one `TransactWriteItems`**
writes the `REQUEST#<requestId>` idempotency marker, the subject's daily
ledger row, the subject's per-model daily row
(`<subject>#model#<model_id>`), and, for subjects with rpm/tpm, the
per-minute `RATE#<subject>` counter for the minute in which the invocation
**occurred** (four items). A `TransactionCanceledException` that is not a
duplicate (hot row, conflict) is retried three times in-function with
backoff (0.05, 0.2, 0.5 s) before the batch fails. It then emits EMF
metrics and re-evaluates the subject: subject-level thresholds, rate limits
in the occurrence minute, and every model budget. A `block` threshold or
rate breach flips the row to `blocked` (automatic origin) and writes the
`REVOCATION#<subject>` sentinel in the same transaction; a subject that is
already blocked is not re-blocked or re-notified for later minutes or
windows. `warn` thresholds send one SNS message per level per window: the
per-level marker is claimed with a conditional write before the publish and
released again if the publish fails, so the retried batch re-sends it.

Prices come from the SSM parameter (15-minute cache, last-good on failure).
There is no built-in price table: if the parameter is unreadable at a cold
start every request is priced at the conservative fallback and the
`pricing_fallback` alarm fires.

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
| Env: `MODEL_FALLBACK_PRICE_JSON`, `PRICES_PARAMETER_NAME`, `WORKLOAD_PROFILES_JSON`, `DEFAULT_LIMITS_JSON`, `WARN_THRESHOLD`, `USAGE_RETENTION_DAYS`, `BEDROCK_USER_ROLE_NAME` | EMF on dimension sets `[UserId]`, `[Model]`, and none: `Requests`, `InputTokens`, `OutputTokens`, `EstimatedCostUSD`, `DetectionLagMilliseconds`, `FallbackPricedRequests`; on `[Model]` and none only: `CacheReadTokens`, `CacheWriteTokens`, `ImagesGenerated`, `UnpricedDimensionRequests`. Log properties: `PriceSource` (`snapshot`, `base-model`, `base-model-mismatch`, `fallback`), `MissingDimensions` |
| | SNS `WARNING <subject> [model <id>] <period> <pct>%`, `BLOCKED <subject> reason=...` |
| | Return value: `{processed, duplicates, ignored, unpriced, unresolved_sessions}` |

## Failure modes and where they surface

| Symptom | Where | Notes |
|---|---|---|
| `unresolved_sessions` non-empty | log `Invocation logs had unknown broker sessions` | The `SESSION#` row expired (TTL = `usage_retention_days`) or the record predates the deployment. Usage is **not** recorded. Rare unless retention is short. |
| `duplicates` > 0 | return value | Normal — at-least-once delivery. The transaction's conditional `Put` on `REQUEST#` rejects the retry. |
| Lambda `Errors` | [usage-processor-errors](../alarms/usage-processor-errors.md) | Unhandled exception; the async invocation is retried twice, then the batch lands in the DLQ ([usage-processor-dlq](../alarms/usage-processor-dlq.md)). Common: DynamoDB throttling (on-demand ramps), `TransactionCanceledException` that survives the three in-function retries (the code re-reads the marker before treating it as a duplicate, then re-raises), SNS publish failure (the warning marker is rolled back first). |
| `FallbackPricedRequests` | [pricing-fallback alarm](../alarms/pricing-fallback.md) | Model or dimension not in the catalog. |
| `DetectionLagMilliseconds` p95 climbing | dashboard, Operations tab | Bedrock log delivery or subscription backlog; the lease still bounds exposure. |
| Warning sent twice for one level | — | Should not happen: per-level markers `warning_sent_<period>_<at_bps>_window` (and `warning_sent_model_<id>_...`) are claimed with a conditional `SET` before the publish, so exactly one of two concurrent invocations sends. A marker is released when the publish fails, which is the one case a retry legitimately sends again. |
| Status write `concurrent-change` | return of `_evaluate_quota` | Three optimistic attempts lost to another writer; the next record or the workload enforcer converges it. |

## Manual operations

- **Re-drive failed batches:** a batch that failed its two async retries
  sits in `UsageProcessorDeadLetterQueue` for 14 days with the original
  subscription event in `requestPayload`; the procedure is in
  [usage-processor-dlq](../alarms/usage-processor-dlq.md). For records that
  never reached the function (subscription filter disabled, log group
  replaced), export them from the log group and re-invoke the function with
  a synthetic subscription event (`_subscription()` in
  `tests/test_usage_processor.py` shows the exact envelope). Idempotency
  markers make re-driving safe as long as their TTL has not passed.
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
