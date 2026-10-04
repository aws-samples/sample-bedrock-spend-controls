# Component: workload enforcer (`WorkloadEnforcerFn`)

**Deployed only when `workloads` is configured.** **Code:**
`workload_enforcer/handler.py`. **Log group:**
`/aws/lambda/<WorkloadEnforcerFn>`. **Invocation:** async `Invoke` from the
enforcement dispatcher and `WorkloadEnforcementSchedule` (`rate(5 minutes)`).
256 MB, 2 min timeout, reserved concurrency 1 (unless
`reserve_enforcement_concurrency` is `false`).

## What it does

Two phases. **Phase 1**, for every configured workload (`WORKLOADS_JSON`:
`{workload_id: {name, profile_arn, role_arn}}`), reads the `workload:<name>`
users row and resolves its desired status: a missing row is `active`; a row
that is `blocked` with automatic origin **and** whose re-evaluation shows
every enabled period, rate limit, and model budget under quota (window
reset, limit raised) is flipped back to `active` with an optimistic
conditional write (`version` guard); a row that cannot be read is
`unknown`. Manual admin blocks are never lifted. A **blocked** workload
without `role_arn` is counted as `skipped_not_ready` (only blocked ones; an
active role-less workload is simply not enforceable and not counted) and
raises the `WorkloadEnforcementSkipped` metric plus an SNS message.

**Phase 2** converges each IAM principal once from the union of its
workloads' statuses: the inline policy
`bedrock-spend-controls-workload-deny-<region>-<stack>` is put
(`PutRolePolicy`, idempotent upsert) when **any** workload on the role is
blocked, deleted (`DeleteRolePolicy`, tolerates `NoSuchEntity`) when none
is blocked and none is unknown, and left as it is when a sibling's status is
`unknown` (`skipped_unknown_status`), so a deny is never lifted on stale
information. A policy still carrying the legacy name
`bedrock-spend-controls-workload-deny` is removed on the same pass
(`legacy_removed`). Synthesis now rejects a `role_arn` shared by two
workloads; the union rule exists for rosters deployed before that check.

Any IAM failure leaves the DynamoDB block untouched (metering keeps
accruing) and raises so the schedule retries.

## IAM scope

- Users table read/write; usage table read.
- `iam:PutRolePolicy`, `DeleteRolePolicy`, `GetRolePolicy` **on exactly the
  configured `role_arn`s** (never a wildcard).
- `sns:Publish` on `QuotaAlerts`.

## Inputs / outputs

| Input | Output |
|---|---|
| `WORKLOADS_JSON`, `WARN_THRESHOLD`, optional `DENY_POLICY_NAME` override (the default is derived from `AWS_REGION` and the stack name) | Inline role policy present/absent |
| `workload:*` rows; usage rows `workload:<name>`, `workload:<name>#model#<id>`, `RATE#workload:<name>` | Row status flip (automatic unblock) |
| | EMF (no dimensions) `WorkloadEnforcementSuccess`, `WorkloadDenyAttached`, `WorkloadDenyDetached`, `WorkloadEnforcementSkipped`, `WorkloadsBlocked`, `WorkloadEnforcementFailure` |
| | SNS `WORKLOAD ENFORCEMENT FAILED`, `WORKLOAD BLOCKED WITHOUT ENFORCEMENT` |
| | Return `{enforced, policy_name, attached, detached, unchanged, unblocked, skipped_not_ready, skipped_unknown_status, blocked_workloads, workloads[], roles[]}`; `attached`/`detached`/`unchanged` count principals; each workload has `workload_id, role_arn, status (active|blocked|unknown), enforcement_ready, error?`; each role has `role_arn, workload_ids, blocked, changed, legacy_removed, error?` |

## Failure modes

| Symptom | Alarm | Notes |
|---|---|---|
| IAM attach/detach error, or a workload row that cannot be read | [workload-enforcement-failure](../alarms/workload-enforcement-failure.md) and [workload-enforcer-errors](../alarms/workload-enforcer-errors.md) | The only place in the system where the lease offers no bound: a blocked workload keeps spending until the attach succeeds. |
| `skipped_not_ready` > 0 | SNS only (`WorkloadEnforcementSkipped` metric, no alarm) | A **blocked** workload has no `role_arn`. Add `role_arn` and redeploy. |
| `skipped_unknown_status` > 0 | — | A sibling workload's row could not be read; the role was left unchanged. Clears on the next pass. |
| Unblock conditional failure | — | Another writer changed the row; next pass re-evaluates. |

## Manual operations

- **Force a pass:** `aws lambda invoke --function-name <WorkloadEnforcerFn> --payload '{"source":"aws.events"}' --cli-binary-format raw-in-base64-out /dev/stdout`.
- **Check the deny on a role:** `aws iam list-role-policies --role-name <role>`,
  then `aws iam get-role-policy --role-name <role> --policy-name
  bedrock-spend-controls-workload-deny-<region>-<stack>` (the exact name is
  `policy_name` in the result JSON).
- **Attach/detach by hand:** see the failure runbook; use the exact
  `deny_policy()` document so the enforcer treats it as `unchanged`.
- **Repair a stuck automatic block:** `PUT /admin/user/status?user_id=workload:<name>`
  with `{"status":"active","reason":"..."}`. The unblock itself is
  admin-origin; if the workload is still over budget the next evaluation
  blocks it again with automatic origin, and that block lifts normally.
  Prefer raising the limit if the block is legitimate.
- **Promote a metering-only workload:** add `role_arn` to
  `cdk/config/workloads.json`, redeploy (the stack attaches the invoke policy
  and widens the enforcer's IAM scope to the new ARN).
- **Remove a workload:** unblock it (or restore its budget), wait one
  5-minute cycle so the deny detaches, then delete it from
  `workloads.json` and redeploy. Removing a blocked workload first leaves
  the deny on a role the enforcer no longer manages; delete it by hand with
  `aws iam delete-role-policy`.
