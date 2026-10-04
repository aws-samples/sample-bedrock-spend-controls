# UsageProcessorErrorsAlarm

**Operations key:** `usage_processor_errors` · **Alarm name:** `<stack>-usage-processor-errors` · **Metric:** Lambda `Errors` on `UsageProcessorFn` (Sum ≥ 1 over 5 min, 1 period; missing data = not breaching) · **Source:** the function's own error count; `usage_processor/handler.py` re-raises after the ledger transaction has exhausted its three in-function retries, after an SNS publish fails (the warning marker is released first), or on any other unhandled exception.

## What it means

A metering batch (one CloudWatch Logs subscription delivery, usually 1 to a
few invocation records) was not applied. The subscription invokes the
function asynchronously, so Lambda retries the same batch twice
(`retry_attempts=2`) with its own backoff; only if those fail too does the
batch land in `UsageProcessorDeadLetterQueue` and raise
[usage-processor-dlq](usage-processor-dlq.md). This alarm therefore fires
**before** the DLQ alarm and often clears on its own.

While a batch is failing, the spend it carries is not in the ledger:
detection of a block threshold is delayed by the retry time, and
`DetectionLagMilliseconds` grows. The permission lease still bounds every
session, and usage is **not lost** as long as the batch is retried or
redriven; the `REQUEST#` idempotency markers make redrives safe.

## Severity guidance

**Next business day** for a few errors that clear (a DynamoDB blip). **Same
day** if errors are continuous: every record is being delayed and the DLQ
alarm will follow. Never a page by itself; enforcement of already-metered
spend is unaffected.

## Likely causes

1. **DynamoDB throttling or outage** on the usage or users table. The
   transaction is retried three times in-function (0.05, 0.2, 0.5 s) on
   `TransactionCanceledException`, so what reaches the alarm is sustained,
   not a single hot-row conflict.
2. **SNS publish failure** for a warning or block notification
   (`sns:Publish` denied, topic deleted). The warning marker is rolled back
   so the retry re-sends; the batch is re-raised on purpose.
3. **SSM parameter unreadable** at a cold start does **not** raise: the
   processor prices at the fallback and fires `pricing_fallback` instead.
4. **A malformed or unexpected record shape** that the parser does not
   tolerate (new Bedrock log fields). The traceback names the field.
5. **Timeout** (2 min) on an unusually large batch.

Read the error:
```bash
aws logs filter-log-events --log-group-name /aws/lambda/<UsageProcessorFn> \
  --filter-pattern '?Traceback ?"Task timed out"' \
  --start-time $(( $(date +%s) - 3600 ))000 --query 'events[].message' --output text | tail -40
```

## Remediation

1. Fix the cause (restore IAM, wait out the service event, deploy a parser
   fix for a new record shape).
2. Nothing to replay while Lambda is still retrying. Once batches reach the
   DLQ, follow [usage-processor-dlq](usage-processor-dlq.md) to redrive
   them; duplicates are rejected by the `REQUEST#` markers.
3. Check whether any subject should have been blocked meanwhile:
   `GET /admin/user?user_id=...` after the redrive shows the updated ledger,
   and the next metered record or vend re-evaluates the thresholds.

## How to verify recovery

- `Errors` Sum = 0 for one 5-minute period → alarm `OK`.
- `Requests` (namespace `BedrockSpendControls`) resumes; `DetectionLagMilliseconds`
  p95 returns to its baseline on the Operations tab.
- `ApproximateNumberOfMessagesVisible` on the DLQ stays at 0.

## Related

- [usage-processor-dlq.md](usage-processor-dlq.md) — where failed batches end up
- [pricing-fallback.md](pricing-fallback.md) — the alarm that fires instead when prices cannot be read
- Component: [components/usage-processor.md](../components/usage-processor.md)
- Metrics: Lambda `Errors`, `Duration` (`AWS/Lambda`); `Requests`, `DetectionLagMilliseconds` (dimensions `UserId`, `Model`, and none)
