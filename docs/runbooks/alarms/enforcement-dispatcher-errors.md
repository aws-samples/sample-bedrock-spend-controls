# EnforcementDispatcherErrorsAlarm

**Operations key:** `enforcement_dispatcher_errors` · **Alarm name:** `<stack>-enforcement-dispatcher-errors` · **Metric:** Lambda `Errors` on `EnforcementDispatcherFn` (Sum ≥ 1 over 5 min, 1 period; missing data = not breaching) · **Source:** the function's own error count; `enforcement_dispatcher/handler.py` has no error path of its own, so any failed `lambda:Invoke` or runtime problem surfaces here.

## What it means

The dispatcher could not fan a users-table stream batch out to the
revocation processor and/or the workload enforcer. The event-source mapping
retries the batch (up to ten times, bisecting), so this alarm fires
**first**; if the retries are exhausted the batch is dead-lettered and
[enforcement-dispatch-dlq](enforcement-dispatch-dlq.md) follows, and while
the batch is being retried `IteratorAge` grows
([enforcement-dispatch-iterator-age](enforcement-dispatch-iterator-age.md)).

**Losing or delaying a dispatch degrades enforcement latency, never
correctness**: both downstream processors re-scan DynamoDB on every pass and
their schedules (`revocation_reconcile_minutes`, default 5; workload enforcer
every 5 minutes) converge the same state. A newly blocked identity is cut by
IAM up to 5 minutes later than usual, still inside the lease bound.

## Severity guidance

**Next business day.** Escalate only if `RevocationSyncFailureAlarm` or
`WorkloadEnforcementFailureAlarm` is also firing, which means the schedule
path is broken too.

## Likely causes

1. **`lambda:InvokeFunction` denied** on a downstream function after a
   manual policy edit, or the downstream function was deleted
   (`ResourceNotFoundException`).
2. **Downstream async queue rejecting** (`TooManyRequestsException`) when
   the downstream function is disabled or misconfigured; with reserved
   concurrency 1 this is rare because `InvocationType=Event` queues rather
   than executes.
3. **Missing environment** (`REVOCATION_FUNCTION_NAME`,
   `WORKLOAD_ENFORCER_FUNCTION_NAME`).
4. **Timeout** (30 s) when the `Invoke` API itself is slow or throttled.

```bash
aws logs filter-log-events --log-group-name /aws/lambda/<EnforcementDispatcherFn> \
  --filter-pattern '?Traceback ?"Task timed out"' \
  --start-time $(( $(date +%s) - 3600 ))000 --query 'events[].message' --output text | tail -20
```

## Remediation

1. Restore the invoke permission or the downstream function (a redeploy
   re-creates both from the template).
2. Confirm the downstream processors converged via their schedules:
   `GET /admin/operations` → `metrics.reconciliation_status` (`current`) and
   a recent `WorkloadEnforcementSuccess`. Or force both:
   ```bash
   aws lambda invoke --function-name <RevocationProcessorFn> --payload '{"source":"enforcement-dispatch"}' --cli-binary-format raw-in-base64-out /dev/stdout
   aws lambda invoke --function-name <WorkloadEnforcerFn>    --payload '{"source":"enforcement-dispatch"}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```
3. If batches reached the DLQ, purge it once converged
   ([enforcement-dispatch-dlq](enforcement-dispatch-dlq.md) step 4).

## How to verify recovery

- `Errors` Sum = 0 for one 5-minute period → alarm `OK`.
- `IteratorAge` back near zero; the mapping's `LastProcessingResult` is `OK`.
- A fresh block produces a `RevocationSyncSuccess` within seconds.

## Related

- [enforcement-dispatch-dlq.md](enforcement-dispatch-dlq.md)
- [enforcement-dispatch-iterator-age.md](enforcement-dispatch-iterator-age.md)
- Component: [components/enforcement-dispatcher.md](../components/enforcement-dispatcher.md)
- Metrics: Lambda `Errors`, `IteratorAge`, `Duration` (`AWS/Lambda`)
