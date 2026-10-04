# UsageProcessorDlqAlarm

**Operations key:** `usage_processor_dlq` · **Alarm name:** `<stack>-usage-processor-dlq` · **Metric:** SQS `ApproximateNumberOfMessagesVisible` on `UsageProcessorDeadLetterQueue` (≥ 1 over 5 min, 1 period; missing data = not breaching) · **Source:** the asynchronous-invocation on-failure destination of `UsageProcessorFn` (`retry_attempts=2`, `on_failure=SqsDestination`); messages are retained for 14 days.

## What it means

A metering batch failed its initial invocation and both Lambda retries, and
the whole subscription event was delivered to the DLQ. **The spend in that
batch is not in the ledger** until you redrive it: the affected subjects'
daily rows under-count, a block threshold they crossed may not have fired,
and the broker keeps vending to them. The permission lease still bounds each
session, so this is delayed metering, not an open door, but the delay lasts
until the redrive, not until the next pass.

The message body is the Lambda failure-destination envelope:
`requestPayload` is the original CloudWatch Logs subscription event
(`{"awslogs": {"data": "<base64 gzip>"}}`), `requestContext.condition` is
`RetryAttemptsExhausted`, and `responsePayload` carries the last error.

## Severity guidance

**Same day.** Nothing is lost while the message is in the queue (14 days),
but every hour it sits there is an hour in which the subjects involved are
metered low. **Page** if messages keep arriving: the processor is failing on
every batch and [usage-processor-errors](usage-processor-errors.md) is in
`ALARM` as well.

## Likely causes

Whatever made the function fail three times in a row; see
[usage-processor-errors](usage-processor-errors.md): sustained DynamoDB
unavailability, an IAM change (`dynamodb:TransactWriteItems`, `sns:Publish`),
a record shape the parser rejects, or a timeout on a very large batch.

Inspect without consuming:
```bash
DLQ_URL=$(aws cloudformation describe-stack-resources --stack-name BedrockSpendControls \
  --query "StackResources[?starts_with(LogicalResourceId,'UsageProcessorDeadLetterQueue')].PhysicalResourceId | [0]" --output text)
aws sqs receive-message --queue-url "$DLQ_URL" --max-number-of-messages 1 --visibility-timeout 30 \
  --query 'Messages[0].Body' --output text | python3 -c '
import json,sys; m=json.load(sys.stdin)
print(m["requestContext"]["condition"]); print(json.dumps(m.get("responsePayload"), indent=1)[:800])'
```

## Remediation

1. **Fix the cause first** (see the errors runbook); redriving into a
   function that still fails only cycles the message back.
2. **Redrive every message** by re-invoking the function with its original
   event. The `REQUEST#<requestId>` idempotency markers (TTL
   `usage_retention_days`) make this safe even if part of a batch had been
   applied:
   ```bash
   while true; do
     MSG=$(aws sqs receive-message --queue-url "$DLQ_URL" --max-number-of-messages 1 --visibility-timeout 120 --output json)
     [ -z "$MSG" ] && break
     echo "$MSG" | python3 -c 'import json,sys; m=json.load(sys.stdin)["Messages"][0]; print(json.dumps(json.loads(m["Body"])["requestPayload"]))' > "$TMPDIR/event.json"
     aws lambda invoke --function-name <UsageProcessorFn> --payload "file://$TMPDIR/event.json" \
       --cli-binary-format raw-in-base64-out "$TMPDIR/out.json" && cat "$TMPDIR/out.json"
     RECEIPT=$(echo "$MSG" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Messages"][0]["ReceiptHandle"])')
     aws sqs delete-message --queue-url "$DLQ_URL" --receipt-handle "$RECEIPT"
   done
   ```
   A successful invoke returns `{processed, duplicates, ignored, unpriced,
   unresolved_sessions}`; `duplicates > 0` is normal when part of the batch
   had already been applied. Delete a message only after its invoke
   succeeded.
3. **Re-evaluate the affected subjects.** The redrive applies thresholds as
   it goes, so a subject that crossed a block level is blocked at that
   moment; confirm with `GET /admin/user?user_id=...`.
4. If a message's `requestPayload` cannot be processed by any version of the
   code (a record shape you have decided not to support), record the
   decision and delete it; the spend stays unmetered and reconciliation will
   show it as a positive delta.

## How to verify recovery

- `ApproximateNumberOfMessagesVisible` = 0 → alarm `OK` within 5 minutes.
- Each redriven invoke returned `processed > 0` or `duplicates > 0` without
  an error; `UsageProcessorErrorsAlarm` is `OK`.
- The affected subjects' `GET /admin/user/usage` rows show the expected
  request counts.

## Related

- [usage-processor-errors.md](usage-processor-errors.md)
- [reconciliation-delta.md](reconciliation-delta.md) — how permanently unmetered spend shows up later
- Component: [components/usage-processor.md](../components/usage-processor.md)
- Metrics: SQS `ApproximateNumberOfMessagesVisible` (`AWS/SQS`), Lambda `Errors` (`AWS/Lambda`)
