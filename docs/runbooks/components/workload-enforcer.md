# Component: workload enforcer (`WorkloadEnforcerFn`)

**Deployed only when `workloads` is configured.** **Code:**
`workload_enforcer/handler.py`. **Log group:**
`/aws/lambda/<WorkloadEnforcerFn>`. **Invocation:** async `Invoke` from the
enforcement dispatcher and `WorkloadEnforcementSchedule` (`rate(5 minutes)`).
256 MB, 2 min timeout, reserved concurrency 1.

## What it does

For every configured workload (`WORKLOADS_JSON`: `{workload_id: {name,
profile_arn, role_arn}}`), reads the `workload:<name>` users row and:

1. If the row is `blocked` with automatic origin **and** re-evaluation shows
   every enabled period, rate limit, and model budget under quota (window
   reset, limit raised), flips it back to `active` with an optimistic
   conditional write (`version` guard). Manual admin blocks are never lifted.
2. Converges the IAM inline policy `bedrock-spend-controls-workload-deny` on
   the workload's `role_arn`: `PutRolePolicy` (idempotent upsert) when
   blocked, `DeleteRolePolicy` (tolerates `NoSuchEntity`) when active.
   Workloads without `role_arn` are skipped and counted as
   `skipped_not_ready`; blocked-but-unenforceable ones raise the
   `WorkloadEnforcementSkipped` metric and an SNS message.

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
| `WORKLOADS_JSON`, `DENY_POLICY_NAME`, `WARN_THRESHOLD` | Inline role policy present/absent |
| `workload:*` rows; usage rows `workload:<name>`, `workload:<name>#model#<id>`, `RATE#workload:<name>` | Row status flip (automatic unblock) |
| | EMF `WorkloadEnforcementSuccess`, `WorkloadDenyAttached`, `WorkloadDenyDetached`, `WorkloadEnforcementSkipped`, `WorkloadsBlocked`, `WorkloadEnforcementFailure` |
| | SNS `WORKLOAD ENFORCEMENT FAILED`, `WORKLOAD BLOCKED WITHOUT ENFORCEMENT` |
| | Return `{enforced, attached, detached, unchanged, unblocked, skipped_not_ready, blocked_workloads, workloads[]}` |

## Failure modes

| Symptom | Alarm | Notes |
|---|---|---|
| IAM attach/detach error | [workload-enforcement-failure](../alarms/workload-enforcement-failure.md) | The only place in the system where the lease offers no bound: a blocked workload keeps spending until the attach succeeds. |
| `skipped_not_ready` > 0 | SNS only (`WorkloadEnforcementSkipped` metric, no alarm) | Add `role_arn` and redeploy. |
| Unblock conditional failure | — | Another writer changed the row; next pass re-evaluates. |

## Manual operations

- **Force a pass:** `aws lambda invoke --function-name <WorkloadEnforcerFn> --payload '{"source":"aws.events"}' --cli-binary-format raw-in-base64-out /dev/stdout`.
- **Check the deny on a role:** `aws iam get-role-policy --role-name <role> --policy-name bedrock-spend-controls-workload-deny`.
- **Attach/detach by hand:** see the failure runbook; use the exact
  `deny_policy()` document so the enforcer treats it as `unchanged`.
- **Repair a stuck automatic block:** `PUT /admin/user/status?user_id=workload:<name>`
  with `{"status":"active","reason":"..."}` — but note that makes the status
  admin-origin; a later over-budget evaluation will block it again
  automatically, and the next window will **not** auto-lift an admin block.
  Prefer raising the limit if the block is legitimate.
- **Promote a metering-only workload:** add `role_arn` to
  `cdk/config/workloads.json`, redeploy (the stack attaches the invoke policy
  and widens the enforcer's IAM scope to the new ARN).
