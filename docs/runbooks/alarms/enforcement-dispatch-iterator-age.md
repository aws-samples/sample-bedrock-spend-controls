# EnforcementDispatchIteratorAgeAlarm

**Operations key:** `enforcement_dispatch_iterator_age` · **Metric:** Lambda `IteratorAge` on `EnforcementDispatcherFn`, Maximum ≥ 300 000 ms (5 min) over 5 min, 1 period.

## What it means

The dispatcher is reading users-table stream records more than five minutes
after they were written. The enforcement **fast path is lagging**: a block
written by metering is not being fanned out to the revocation processor and
workload enforcer promptly. Their repair schedules (5 min) still run, so the
worst case is that IAM cutoff arrives on the schedule's cadence instead of
within seconds — the same bound as a dispatch DLQ, but for *every* record
rather than one lost batch.

Because the dispatcher has `reserved_concurrent_executions=1` and processes
one shard at a time, iterator age grows when the function is slow, erroring
(each error retries the batch up to ten times before bisecting), or
throttled.

## Severity guidance

**Next business day**, unless paired with `EnforcementDispatchDlqAlarm` or a
downstream failure alarm. The permission lease still bounds vended sessions.
Note that the alarm also fires benignly right after the stream is recreated
(e.g. a table replacement) while the function catches up from `LATEST`.

## Likely causes

1. **Repeated batch failures** — see
   [enforcement-dispatch-dlq.md](enforcement-dispatch-dlq.md); ten retries ×
   batch runtime accumulate age before the DLQ receives the batch.
2. **Downstream `Invoke` slow** — the dispatcher waits for the `Invoke`
   API call (asynchronous *execution*, but the call itself must be
   accepted). Lambda API throttling on `Invoke` shows as long durations.
   Check `Duration` p99 on the dispatcher.
3. **Stream write burst** — a bulk admin operation writing thousands of
   `REVOCATION#` sentinels (e.g. a scripted mass block) at once. The stream
   has two consumers; with `batch_size=100` the dispatcher clears ~100
   records per invocation.
4. **Function paused/disabled** or event-source mapping disabled
   (`aws lambda list-event-source-mappings --function-name <fn>` → `State`).

## Remediation

1. Check the event-source mapping state and last processing result:
   ```bash
   aws lambda list-event-source-mappings --function-name <EnforcementDispatcherFn> \
     --query 'EventSourceMappings[].{state:State,result:LastProcessingResult,batch:BatchSize}'
   ```
   `State` must be `Enabled`. Re-enable with
   `aws lambda update-event-source-mapping --uuid <uuid> --enabled`.
2. Look for errors that keep a batch retrying:
   ```bash
   aws logs filter-log-events --log-group-name /aws/lambda/<EnforcementDispatcherFn> \
     --filter-pattern 'ERROR' --start-time $(( $(date +%s) - 1800 ))000 --query 'events[].message' --output text | tail -5
   ```
   Fix the invoke permission / downstream availability as in the DLQ runbook.
3. If the backlog is a legitimate burst, no action beyond waiting; the
   schedules keep enforcement correct meanwhile. Force a downstream pass if
   you need convergence now (see the DLQ runbook step 3).
4. Do not raise `batch_size` or `reserved_concurrent_executions` in
   production without re-reading the stream-consumer constraint in
   `spend_controls_stack.py` (two consumers per shard).

## How to verify recovery

- `IteratorAge` Maximum falls below 300 000 ms → `OK`.
- `LastProcessingResult` is `OK`; dispatcher `Errors` = 0.

## Related

- [enforcement-dispatch-dlq.md](enforcement-dispatch-dlq.md)
- Component: [components/enforcement-dispatcher.md](../components/enforcement-dispatcher.md)
