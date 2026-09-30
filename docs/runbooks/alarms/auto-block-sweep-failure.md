# AutoBlockSweepFailureAlarm

**Operations key:** `auto_block_sweep_failure` · **Always deployed.** · **Metric:** `AutoBlockSweepFailure` (Sum ≥ 1 over 1 day, 1 period; missing data = not breaching) · **Emitted by:** `auto_block_sweeper/handler.py` when the nightly pass hits an unexpected DynamoDB error (scan, lift transaction, or state-row write).

## What it means

The nightly sweep that lifts automatic blocks for JWT users who never came
back did not complete cleanly. The failing rows are listed in the SNS
message `AUTO-BLOCK SWEEP FAILED` (`failures[].user_id`,
`failures[].error`) and in the `failures` field of `CONFIG#AUTO_BLOCK_SWEEP`,
which the console's "Auto-block sweep" card renders as **Failed**.

Rows that were lifted before the error stay lifted (each lift is its own
transaction). Rows after it remain `blocked` until the next night or a
manual pass. The impact is availability for those users (they are under
quota but still denied) plus the slow accumulation of stale identities in
the Deny shards — **not** under-enforcement. Nothing here lets anyone spend
more than their quota.

The one-day alarm period is deliberate: a failure at 00:05 UTC must stay
visible during the working day instead of clearing after five minutes.

## Severity guidance

**Next business day.** Spend is still bounded; the users affected can be
unblocked by a manual pass at any time.

## Likely causes

1. **Throttling** (`ProvisionedThroughputExceededException`) on a large
   blocked set — the sweep scans every blocked row and runs one transaction
   per lift. Both tables are on-demand, so this is rare, but a burst of
   several hundred lifts right after a monthly reset can hit it.
2. **`TransactionCanceledException` for a reason other than a lost race.**
   Conditional failures are counted as `raced`, never as failures; anything
   else (item size, a second sentinel writer racing in the same
   transaction) surfaces here.
3. **IAM drift on the sweeper role** (`AccessDeniedException`) after a
   manual policy edit.

Read the exact error:
```bash
aws logs filter-log-events --log-group-name /aws/lambda/<AutoBlockSweeperFn> \
  --filter-pattern '{ $.AutoBlockSweepFailure > 0 }' \
  --start-time $(( $(date +%s) - 86400 ))000 --query 'events[-1].message' --output text \
  | python3 -c 'import json,sys; [print(f["user_id"], f["error"]) for f in json.load(sys.stdin)["failures"]]'
```

## Remediation

1. Fix the cause (restore the role policy; for throttling nothing to change,
   the next pass retries).
2. Force a pass — first a dry run to see the candidates, then the real one:
   ```bash
   aws lambda invoke --function-name <AutoBlockSweeperFn> \
     --payload '{"source":"manual","dry_run":true}' --cli-binary-format raw-in-base64-out /dev/stdout
   aws lambda invoke --function-name <AutoBlockSweeperFn> \
     --payload '{"source":"manual"}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```
   The result JSON lists `lifted`, `still_blocked`, `raced` and `failures`.
3. If one user must be unblocked before that, use the admin API — but note
   that makes the status admin-origin, and admin blocks never auto-lift.

## How to verify recovery

- `AutoBlockSweepSuccess` = 1 on the manual or next nightly pass and no new
  `AutoBlockSweepFailure` sample → the alarm returns to `OK` after the
  one-day period.
- `GET /admin/operations` → `auto_block_sweep.status` is `ok` and
  `last_run.failures` is empty. The console card shows **Alarm
  clearing** (amber) while the one-day alarm period is still running after
  a repaired failure, then **Ran** (green) once the alarm returns to `OK`.
  The card trusts the state row, not the alarm, so a repaired pass is never
  labelled Failed.
- `RevokedIdentitiesDesired` (revocation card) drops by the number lifted
  once the revocation processor converges.

## Related

- Component: [components/auto-block-sweeper.md](../components/auto-block-sweeper.md)
- [revocation-policy-overflow.md](revocation-policy-overflow.md) — the failure mode the sweep prevents.
- [revocation-sync-failure.md](revocation-sync-failure.md) — if lifted users are still denied.
