# RevocationProcessorErrorsAlarm

**Operations key:** `revocation_processor_errors` · **Alarm name:** `<stack>-revocation-processor-errors` · **Metric:** Lambda `Errors` on `RevocationProcessorFn` (Sum ≥ 1 over 5 min, 1 period; missing data = not breaching) · **Source:** the function's own error count. `revocation_processor/handler.py` emits `RevocationSyncFailure` and then **re-raises**, so this alarm normally fires together with [revocation-sync-failure](revocation-sync-failure.md); it also catches a timeout (2 min) or an init failure, which never reach the handler's error path.

## What it means

A revocation pass did not finish. When
[revocation-sync-failure](revocation-sync-failure.md) is also in `ALARM`,
that runbook has the diagnosis and the impact statement: deny shards may be
behind the set of blocked identities, so an identity blocked by metering
keeps its already-issued session until the lease deadline instead of being
cut within IAM propagation time. New vends are refused regardless.

When this alarm fires **alone**, the pass died before it could emit the
functional metric: the function timed out (a very large users-table scan or
19 slow IAM calls) or failed to start. The impact is the same, with the
added hint that the problem is capacity rather than permissions.

The `RevocationReconciliationSchedule` (`revocation_reconcile_minutes`,
default 5) retries automatically.

## Severity guidance

As for [revocation-sync-failure](revocation-sync-failure.md): **page when
the runtime lease dial is 900 s and blocked identities exist**; **next
business day** at 60 s or with no blocked identities.

## Likely causes

1. **Everything in [revocation-sync-failure](revocation-sync-failure.md)**:
   IAM denied or throttled, a scan failure, an empty
   `REVOCATION_POLICY_ARNS_JSON`.
2. **Timeout**: `Duration` close to 120 000 ms. The pass scans every users
   row (consistent) and reads 19 policies; tens of thousands of rows or IAM
   throttling retries can exceed two minutes.
3. **Out of memory** on a very large scan (256 MB).

```bash
aws logs filter-log-events --log-group-name /aws/lambda/<RevocationProcessorFn> \
  --filter-pattern '?Traceback ?"Task timed out" ?"Runtime exited"' \
  --start-time $(( $(date +%s) - 3600 ))000 --query 'events[].message' --output text | tail -20
```

## Remediation

1. If the sync-failure alarm is also firing, follow that runbook.
2. For a timeout or memory failure, reduce the blocked set (force the
   [auto-block sweep](../components/auto-block-sweeper.md), unblock stale
   identities) and, if the users table is large, raise the function's
   memory and timeout in `spend_controls_stack.py` and redeploy. Shorten
   exposure meanwhile with the runtime dial (`PUT /admin/enforcement
   {"permission_lease_seconds": 60, ...}`).
3. Force a pass once the cause is fixed:
   ```bash
   aws lambda invoke --function-name <RevocationProcessorFn> \
     --payload '{"source":"aws.events"}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```

## How to verify recovery

- `Errors` Sum = 0 for one 5-minute period → alarm `OK`.
- `RevocationSyncSuccess` emitted; `/admin/operations` →
  `metrics.reconciliation_status: current`.

## Related

- [revocation-sync-failure.md](revocation-sync-failure.md)
- [revocation-policy-overflow.md](revocation-policy-overflow.md)
- Component: [components/revocation-processor.md](../components/revocation-processor.md)
- Metrics: Lambda `Errors`, `Duration` (`AWS/Lambda`); `RevocationSyncSuccess`, `RevocationSyncFailure`, `RevokedIdentitiesDesired` (no dimensions)
