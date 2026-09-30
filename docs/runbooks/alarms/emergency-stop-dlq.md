# EmergencyStopDlqAlarm

**Operations key:** `emergency_dlq` · **Metric:** SQS `ApproximateNumberOfMessagesVisible` on `EmergencyStopDeadLetterQueue` (≥ 1 over 5 min, 1 period) · **Source:** the `DynamoEventSource` on `EmergencyStopProcessorFn` (`retry_attempts=10`, `bisect_batch_on_error=True`, `on_failure=SqsDlq`).

## What it means

A users-table stream batch containing a `CONFIG#EMERGENCY_STOP` change was
retried ten times by the Lambda event-source mapping and gave up. The
emergency processor's **fast path** for that transition is lost. Nothing is
lost permanently: the processor is a converger that re-reads the DynamoDB
control row on every pass, and the `EmergencyStopReconciliationSchedule`
(rate 1 minute) invokes it regardless of the stream. The practical impact is
that an emergency activate/recover may take up to ~1 minute longer than the
sub-second stream path, and only if the underlying failure has cleared.

## Severity guidance

**Page if `EmergencyStopFailureAlarm` is also in ALARM** — that combination
means the schedule path is failing too, and the state is stuck (see
[emergency-stop-failure.md](emergency-stop-failure.md)). **Next business day
otherwise:** a DLQ message with the failure alarm in `OK` means the stream
path failed transiently but the schedule converged the state.

## Likely causes

1. **Same root cause as `EmergencyStopFailureAlarm`** (IAM denied/throttled,
   version-limit corner case). Ten stream retries in quick succession all hit
   it; the message landed in the DLQ; the schedule kept retrying every minute.
2. **Lambda throttled or reserved concurrency exhausted.** The function has
   `reserved_concurrent_executions=1`; a long IAM call plus a schedule
   invocation can collide. Check `Throttles` on the function.
3. **A record the processor cannot parse.** Only key-filtered records reach
   it (`Keys.user_id.S == "CONFIG#EMERGENCY_STOP"`), so this is unlikely
   unless the table was edited by hand with a malformed item.

## Remediation

1. Inspect the DLQ message. It is the Lambda "failure destination" envelope,
   not the raw stream record:
   ```bash
   DLQ_URL=$(aws sqs get-queue-url --queue-name "$(aws sqs list-queues --queue-name-prefix BedrockSpendControls-EmergencyStopDeadLetterQueue --query 'QueueUrls[0]' --output text | awk -F/ '{print $NF}')" --query QueueUrl --output text)
   aws sqs receive-message --queue-url "$DLQ_URL" --max-number-of-messages 1 --visibility-timeout 30 \
     --query 'Messages[0].Body' --output text | python3 -m json.tool
   ```
   `DDBStreamBatchInfo.shardId` / `startSequenceNumber` identify the batch;
   `requestContext.condition` says `RetryAttemptsExhausted`.
2. Confirm the control state converged anyway:
   `GET /admin/operations` → `emergency.converged`. If **not** converged,
   follow [emergency-stop-failure.md](emergency-stop-failure.md).
3. Once converged, purge the DLQ so the alarm clears (the message has no
   replay value — the processor is stateless and re-reads the row):
   ```bash
   aws sqs purge-queue --queue-url "$DLQ_URL"
   ```
   Purge is destructive to the queue contents only; it does not touch
   DynamoDB or IAM.

## How to verify recovery

- `ApproximateNumberOfMessagesVisible` = 0 → alarm `OK` within 5 minutes.
- `GET /admin/operations` → `emergency.converged: true`.

## Related

- [emergency-stop-failure.md](emergency-stop-failure.md)
- [enforcement-dispatch-dlq.md](enforcement-dispatch-dlq.md) — the other
  stream consumer's DLQ; the users-table stream has exactly two consumers.
- Component: [components/emergency-processor.md](../components/emergency-processor.md)
