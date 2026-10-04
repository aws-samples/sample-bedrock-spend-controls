# AutoBlockSweeperErrorsAlarm

**Operations key:** `auto_block_sweeper_errors` · **Alarm name:** `<stack>-auto-block-sweeper-errors` · **Metric:** Lambda `Errors` on `AutoBlockSweeperFn` (Sum ≥ 1 over 5 min, 1 period; missing data = not breaching) · **Source:** the function's own error count. `auto_block_sweeper/handler.py` emits `AutoBlockSweepFailure` and then **re-raises**, so this alarm normally fires together with [auto-block-sweep-failure](auto-block-sweep-failure.md); it also catches a timeout (2 min) or an init failure.

## What it means

The nightly sweep (00:05 UTC) that lifts stale automatic blocks did not
finish. The impact is availability, not under-enforcement: users who are
under quota again stay `blocked` until the next night or a manual pass, and
their identities keep occupying revocation-shard capacity. Nothing here lets
anyone spend above quota.

Unlike the one-day `AutoBlockSweepFailureAlarm`, this alarm uses a 5-minute
period and returns to `OK` on its own; the failure alarm is the one that
stays visible through the working day.

## Severity guidance

**Next business day.**

## Likely causes

1. **Everything in [auto-block-sweep-failure](auto-block-sweep-failure.md)**:
   a DynamoDB error on the scan, a lift transaction cancelled for a reason
   other than a lost conditional check, IAM drift on the sweeper role.
2. **Timeout** (2 min) on a very large blocked set: the sweep re-evaluates
   every automatic-origin blocked row with a strongly consistent ledger
   query each. No `AutoBlockSweepFailure` metric is emitted in this case,
   so this alarm is the only signal.
3. **Missing environment** after a manual edit (`USERS_TABLE`, `USAGE_TABLE`).

```bash
aws logs filter-log-events --log-group-name /aws/lambda/<AutoBlockSweeperFn> \
  --filter-pattern '?Traceback ?"Task timed out"' \
  --start-time $(( $(date +%s) - 86400 ))000 --query 'events[].message' --output text | tail -20
```

## Remediation

1. If the failure alarm is also firing, follow
   [auto-block-sweep-failure](auto-block-sweep-failure.md).
2. For a timeout, run the sweep by hand in dry-run mode to size the backlog,
   then for real; each pass lifts what it reaches before the timeout and
   later passes continue:
   ```bash
   aws lambda invoke --function-name <AutoBlockSweeperFn> \
     --payload '{"source":"manual","dry_run":true}' --cli-binary-format raw-in-base64-out /dev/stdout
   aws lambda invoke --function-name <AutoBlockSweeperFn> \
     --payload '{"source":"manual"}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```
   If the backlog is structural, raise the function timeout in
   `spend_controls_stack.py` and redeploy.

## How to verify recovery

- `Errors` Sum = 0 for one 5-minute period → alarm `OK`.
- `AutoBlockSweepSuccess` = 1 on the manual or next nightly pass;
  `GET /admin/operations` → `auto_block_sweep.status: ok`.

## Related

- [auto-block-sweep-failure.md](auto-block-sweep-failure.md)
- [revocation-policy-overflow.md](revocation-policy-overflow.md) — what accumulates when the sweep keeps failing
- Component: [components/auto-block-sweeper.md](../components/auto-block-sweeper.md)
- Metrics: Lambda `Errors`, `Duration` (`AWS/Lambda`); `AutoBlockSweepSuccess`, `AutoBlockSweepFailure`, `AutoBlockSweepLifted`, `AutoBlockSweepRaced` (no dimensions)
