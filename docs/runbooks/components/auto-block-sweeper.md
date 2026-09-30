# Component: auto-block sweeper (`AutoBlockSweeperFn`)

**Always deployed.** **Code:** `auto_block_sweeper/handler.py` (shares the
row re-evaluation helpers in
`quota_periods_layer/python/bedrock_spend_controls/row_enforcement.py` with
the workload enforcer). **Log group:** `/aws/lambda/<AutoBlockSweeperFn>`.
**Invocation:** `AutoBlockSweepSchedule` (`cron(5 0 * * ? *)`, 00:05 UTC,
right after the daily/weekly/monthly windows roll over). 256 MB, 2 min
timeout, reserved concurrency 1.

## Why it exists

The broker lifts an automatic block *lazily*: at the user's next
`POST /v1/credentials` it re-evaluates the current windows and, if the
subject is under quota again, flips the row to `active`. A JWT user who is
blocked and never comes back therefore stays `blocked` indefinitely and
their `source_identity` keeps a slot in the per-user IAM Deny shards (about
40–60 identities per shard, 19 shards). Enough of those and a shard
overflows, which silently stops *new* blocks in that shard from being
enforced — see [revocation-policy-overflow](../alarms/revocation-policy-overflow.md).

The sweep is the scheduled counterpart of the vend-time check. It never
introduces a "block TTL": a subject that exhausted a monthly budget stays
blocked until the month rolls over.

## What it does

1. `Scan` (consistent, paginated, `status = blocked`) the users table and
   keep JWT-user rows only: reserved bookkeeping prefixes (`SESSION#`,
   `VEND#`, `REVOCATION#`, `CONFIG#`, …) and `workload:` rows are skipped —
   the latter belong to the [workload enforcer](workload-enforcer.md).
2. For each row that the automatic path owns (`status_origin: automatic`)
   re-evaluate every enabled calendar
   period, the current rate minute, and every per-model budget with the
   broker's own criterion. Rows with `status_origin: admin` are counted as
   `admin_blocked` and never touched.
3. If nothing is breached, one `TransactWriteItems`:
   `Update` the row to `active` (`status_reason: "auto: current calendar
   periods are under quota"`, `status_origin: automatic`, `version + 1`),
   conditional on the observed `version`, `status` and `status_reason`; plus
   `Put REVOCATION#<user_id>` with `desired_status: active`. The sentinel is
   what the users-table stream → enforcement dispatcher → revocation
   processor fast path keys on, so the Deny shards converge within seconds.
   A conditional failure means another writer (vend, admin, usage
   processor) changed the row first; it is counted as `raced`, not
   retried.
4. Write the run summary to `CONFIG#AUTO_BLOCK_SWEEP` (no TTL) so
   `GET /admin/operations` → `auto_block_sweep` and the console's
   "Auto-block sweep" card show that the job ran and what it did.
5. Emit EMF counters; on any unexpected DynamoDB error publish
   `AUTO-BLOCK SWEEP FAILED` and raise so the Lambda error is recorded.

`{"dry_run": true}` in the event evaluates and reports without writing rows
or the state row.

## IAM scope

- Users table read/write (row flip + sentinel; state row).
- Usage table read.
- `sns:Publish` on `QuotaAlerts`.
- **No `iam:*`.** The sweeper never touches policy documents; the revocation
  processor owns the shards.

## Inputs / outputs

| Input | Output |
|---|---|
| `USERS_TABLE`, `USAGE_TABLE`, `WARN_THRESHOLD`, `USAGE_RETENTION_DAYS` (sentinel TTL) | Row status flip (`blocked` → `active`) + `REVOCATION#<user>` sentinel, atomically |
| Blocked user rows; usage rows `<user>`, `<user>#model#<id>`, `RATE#<user>` | `CONFIG#AUTO_BLOCK_SWEEP` `{ran_at, dry_run, evaluated, lifted, still_blocked, admin_blocked, raced, lifted_users[], failures[]}` |
| Event `{"source": "aws.events"}` or `{"source": "manual", "dry_run": bool}` | EMF `AutoBlockSweepSuccess`, `AutoBlockSweepEvaluated`, `AutoBlockSweepLifted`, `AutoBlockSweepStillBlocked`, `AutoBlockSweepRaced`, `AutoBlockSweepFailure` |
| | SNS `AUTO-BLOCK SWEEP FAILED` (failures only; lifts are not notified) |
| | Return: the same summary as the state row |

## Failure modes

| Symptom | Alarm | Notes |
|---|---|---|
| DynamoDB error during scan / transaction / state write | [auto-block-sweep-failure](../alarms/auto-block-sweep-failure.md) | Rows already lifted in the pass stay lifted; the rest wait for the next night or a manual pass. |
| `raced` > 0 | — | Expected under concurrent vends or admin changes; the winner already re-evaluated. |
| Card says "Never ran" days after deploy | Lambda `Errors` / `Invocations` | The schedule did not fire or the function failed before writing the state row. Check the log group. |
| Lifted user still denied by IAM | [revocation-sync-failure](../alarms/revocation-sync-failure.md) | The sentinel was written; the revocation processor has not converged. The 5-minute repair schedule also picks it up. |

## Manual operations

- **Dry run (what would lift tonight):**
  `aws lambda invoke --function-name <AutoBlockSweeperFn> --payload '{"source":"manual","dry_run":true}' --cli-binary-format raw-in-base64-out /dev/stdout`
  → `lifted_users[]` lists the candidates; nothing is written.
- **Force a real pass** (after raising a limit for many users, or after a
  failed night): same command without `dry_run`. Safe to repeat; a second
  pass finds nothing to lift.
- **Lift one user now:** the admin API (`PUT /admin/user/status` with
  `{"status":"active"}`) — but that makes the status admin-origin, so a
  later breach re-blocks it automatically and the *next* reset will **not**
  auto-lift it. Prefer waiting for the sweep or raising the limit.
- **Check the last run without the console:**
  `aws dynamodb get-item --table-name <UsersTable> --key '{"user_id":{"S":"CONFIG#AUTO_BLOCK_SWEEP"}}'`.

## Related

- [gateway.md](gateway.md) — `refresh_auto_status` (the vend-time lift this mirrors).
- [revocation-processor.md](revocation-processor.md) — consumes the sentinel.
- [workload-enforcer.md](workload-enforcer.md) — the same lift for `workload:` rows, every 5 minutes.
