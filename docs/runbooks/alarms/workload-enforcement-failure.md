# WorkloadEnforcementFailureAlarm

**Operations key:** `workload_enforcement_failure` · **Alarm name:** `<stack>-workload-enforcement-failure` · **Deployed only when `workloads` is configured.** · **Metric:** `WorkloadEnforcementFailure` (Sum ≥ 1 over 5 min, 1 period; missing data = not breaching) · **Emitted by:** `workload_enforcer/handler.py` when `failures` is non-empty after the two phases (reading each workload's row, then converging each role).

## What it means

The workload enforcer could not converge at least one `workload:<name>`
row's status onto its IAM principal: it failed to read the row, failed to
attach the inline `bedrock-spend-controls-workload-deny-<region>-<stack>`
policy to a blocked workload's role, or failed to remove it from a workload
that became active. The failing entries are listed in the SNS message
`WORKLOAD ENFORCEMENT FAILED` (`failures[]`: either a workload with
`workload_id` and `error`, or a role with `role_arn`, `workload_ids`, and
`error`).

A role shared by several workloads (possible only in rosters deployed before
synthesis started rejecting duplicate `role_arn`s) carries the deny while
**any** of them is blocked and loses it only when none are; a workload whose
row could not be read has `status: unknown`, and its role is left as it is
(`skipped_unknown_status`) rather than having a deny lifted on stale
information.

- **Blocked workload, attach failed:** the application keeps calling
  `bedrock-runtime` with its own credentials. There is no lease to fall back
  on — workload mode has no vend path — so **spend is unbounded until the
  attach succeeds** or the 5-minute `WorkloadEnforcementSchedule` retries.
  This is the one alarm in the system where the permission lease offers no
  bound.
- **Active workload, detach failed:** the workload is denied Bedrock although
  it is under quota. Availability impact, not spend.

The processor raises after emitting, so the invoking path records a Lambda
error; the schedule retries every 5 minutes and the users-table stream (via
the dispatcher) retries on the next row change.

## Severity guidance

**Page** for a blocked workload whose attach failed. **Next business day**
for a detach failure (availability of one internal workload).

## Likely causes

1. **`iam:PutRolePolicy` / `iam:DeleteRolePolicy` denied.** The enforcer's
   policy is scoped to exactly the `role_arn`s in the workloads config
   (`workload_role_arns` in the stack). If a role was **recreated** (same
   name, new path) or the config's `role_arn` has a typo, the ARN will not
   match. Also fails if the role has a permissions boundary that forbids
   inline policy changes, or an SCP.
2. **`NoSuchEntity` on `PutRolePolicy`** — the workload role was deleted.
3. **`unsupported IAM principal ARN`** (`ValueError` from `_principal`) —
   `role_arn` is not `arn:aws:iam::<acct>:role/...` or `:user/...`.
   Synthesis now rejects these, so this only survives from an old roster.
4. **Throttling** on IAM when many workloads flip at once.

Read the exact error:
```bash
aws logs filter-log-events --log-group-name /aws/lambda/<WorkloadEnforcerFn> \
  --filter-pattern '{ $.WorkloadEnforcementFailure > 0 }' \
  --start-time $(( $(date +%s) - 3600 ))000 --query 'events[-1].message' --output text \
  | python3 -c 'import json,sys; [print(f.get("workload_id") or f.get("role_arn"), f["error"]) for f in json.load(sys.stdin)["failures"]]'
```

## Remediation

1. Confirm the desired state for each failing workload:
   `GET /admin/user?user_id=workload:<name>` → `status`, `status_reason`,
   `enforcement_ready`.
2. Fix IAM: restore the permission on the enforcer role, correct the
   `role_arn` in `cdk/config/workloads.json` and redeploy, or remove the
   boundary/SCP conflict on the workload role.
3. Force a pass:
   ```bash
   aws lambda invoke --function-name <WorkloadEnforcerFn> \
     --payload '{"source":"aws.events"}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```
   The result JSON lists `policy_name`, `attached`, `detached`, `unchanged`
   (counts of principals), `skipped_not_ready`, `skipped_unknown_status`,
   `workloads[]` (`status` may be `unknown`), and `roles[]` (`role_arn`,
   `workload_ids`, `blocked`, `changed`, `legacy_removed`, `error`).
4. **If a blocked workload's attach cannot be fixed quickly**, attach the
   deny by hand — this is exactly the document the enforcer writes
   (`deny_policy()` in `workload_enforcer/handler.py`), so the next pass sees
   it as already applied:
   ```bash
   # The policy name is <policy_name> from the result JSON:
   # bedrock-spend-controls-workload-deny-<region>-<stack>
   aws iam put-role-policy --role-name <workload role name> \
     --policy-name bedrock-spend-controls-workload-deny-us-east-1-BedrockSpendControls \
     --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"BedrockQuotaWorkloadDeny","Effect":"Deny","Action":["bedrock:CountTokens","bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"}]}'
   ```
   Destructive to that workload's Bedrock access; reversible with
   `aws iam delete-role-policy`. The emergency stop does **not** help here —
   it acts on the vended role, not on workload roles.

## How to verify recovery

- `WorkloadEnforcementSuccess` = 1 on the next pass; `WorkloadEnforcementFailure` absent → alarm `OK`.
- For a blocked workload: `aws iam list-role-policies --role-name <role>`
  lists `bedrock-spend-controls-workload-deny-<region>-<stack>` and
  `get-role-policy` on it returns the deny. A policy with the legacy name
  `bedrock-spend-controls-workload-deny` is removed by the next pass
  (`legacy_removed: true`).
- For an active workload: `list-role-policies` shows neither name.

## Related

- Not an alarm but related: `WorkloadEnforcementSkipped` (blocked workloads
  without `role_arn`; SNS `WORKLOAD BLOCKED WITHOUT ENFORCEMENT`). Promote the
  workload by adding `role_arn` and redeploying.
- [workload-enforcer-errors.md](workload-enforcer-errors.md) — the Lambda
  `Errors` alarm that fires alongside this one, because the enforcer
  re-raises.
- Metrics (no dimensions): `WorkloadEnforcementSuccess`, `WorkloadDenyAttached`, `WorkloadDenyDetached`, `WorkloadEnforcementSkipped`, `WorkloadsBlocked`, `WorkloadEnforcementFailure`
- [enforcement-dispatch-dlq.md](enforcement-dispatch-dlq.md)
- Component: [components/workload-enforcer.md](../components/workload-enforcer.md)
