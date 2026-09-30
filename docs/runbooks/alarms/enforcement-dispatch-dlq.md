# EnforcementDispatchDlqAlarm

**Operations key:** `enforcement_dispatch_dlq` · **Metric:** SQS `ApproximateNumberOfMessagesVisible` on `EnforcementDispatchDeadLetterQueue` (≥ 1 over 5 min, 1 period) · **Source:** the `DynamoEventSource` on `EnforcementDispatcherFn` (`batch_size=100`, `max_batching_window=5s`, `retry_attempts=10`, `bisect_batch_on_error=True`).

## What it means

A users-table stream batch containing `REVOCATION#*` sentinels or
`workload:*` rows could not be delivered to the enforcement dispatcher after
ten retries. The dispatcher's only job is to asynchronously `Invoke` the
revocation processor and/or the workload enforcer so they react within
seconds instead of waiting for their repair schedules. **Losing a dispatch
degrades enforcement latency, never correctness**: both downstream
processors re-scan DynamoDB on every pass, and their schedules
(`revocation_reconcile_minutes`, default 5; workload enforcer every 5
minutes) will converge the same state.

Practical impact while the fast path is down: a newly blocked identity is
cut by IAM up to 5 minutes later than usual — still inside the permission
lease bound for a 300 s or 900 s dial, and only marginally later than the
60 s dial would have cut it anyway.

## Severity guidance

**Next business day.** Escalate to page only if
`RevocationSyncFailureAlarm` or `WorkloadEnforcementFailureAlarm` is also
firing — that means the schedule path is broken too.

## Likely causes

1. **`lambda:InvokeFunction` denied** on the dispatcher → downstream
   function. The stack grants `grant_invoke` explicitly; a manual policy
   edit or a deleted downstream function breaks it. Check the dispatcher's
   `Errors` metric and log:
   ```bash
   aws logs filter-log-events --log-group-name /aws/lambda/<EnforcementDispatcherFn> \
     --filter-pattern 'ERROR' --start-time $(( $(date +%s) - 3600 ))000 --query 'events[].message' --output text | tail -5
   ```
2. **Downstream function throttled** (`TooManyRequestsException` on
   `Invoke`). Both downstream functions have `reserved_concurrent_executions=1`;
   `InvocationType="Event"` queues asynchronously so this is rare, but the
   async queue can reject when the function is disabled or its async
   invocation is misconfigured.
3. **Dispatcher itself throttled** — `reserved_concurrent_executions=1` and
   a 30 s timeout; a stuck downstream `Invoke` can hold the single slot.
4. **Stream position too old** — see
   [enforcement-dispatch-iterator-age.md](enforcement-dispatch-iterator-age.md).

## Remediation

1. Read the DLQ envelope to learn the batch and failure condition:
   ```bash
   DLQ_URL=$(aws cloudformation describe-stack-resources --stack-name BedrockSpendControls \
     --logical-resource-id EnforcementDispatchDeadLetterQueue --query 'StackResources[0].PhysicalResourceId' --output text)
   aws sqs receive-message --queue-url "$DLQ_URL" --max-number-of-messages 1 --query 'Messages[0].Body' --output text | python3 -m json.tool
   ```
2. Fix the invoke permission or downstream availability.
3. Confirm the downstream processors converged via their schedules:
   `GET /admin/operations` → `metrics.reconciliation_status` (`current`) and
   `WorkloadEnforcementSuccess` recently emitted. Or force both:
   ```bash
   aws lambda invoke --function-name <RevocationProcessorFn> --payload '{"source":"enforcement-dispatch"}' --cli-binary-format raw-in-base64-out /dev/stdout
   aws lambda invoke --function-name <WorkloadEnforcerFn>    --payload '{"source":"enforcement-dispatch"}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```
4. Purge the DLQ (the message has no replay value; the processors are
   stateless convergers):
   `aws sqs purge-queue --queue-url "$DLQ_URL"`.

## How to verify recovery

- `ApproximateNumberOfMessagesVisible` = 0 → `OK`.
- Dispatcher `Errors` = 0; a fresh block shows a `RevocationSyncSuccess`
  within seconds rather than minutes.

## Related

- [enforcement-dispatch-iterator-age.md](enforcement-dispatch-iterator-age.md)
- [emergency-stop-dlq.md](emergency-stop-dlq.md) — the other stream consumer
- Component: [components/enforcement-dispatcher.md](../components/enforcement-dispatcher.md)
