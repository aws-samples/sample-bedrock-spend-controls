# WorkloadEnforcerErrorsAlarm

**Operations key:** `workload_enforcer_errors` · **Alarm name:** `<stack>-workload-enforcer-errors` · **Deployed only when `workloads` is configured.** · **Metric:** Lambda `Errors` on `WorkloadEnforcerFn` (Sum ≥ 1 over 5 min, 1 period; missing data = not breaching) · **Source:** the function's own error count. `workload_enforcer/handler.py` emits `WorkloadEnforcementFailure` and then **re-raises**, so this alarm normally fires together with [workload-enforcement-failure](workload-enforcement-failure.md); it also catches a timeout (2 min), an unparsable `WORKLOADS_JSON`, or an init failure.

## What it means

A workload-enforcement pass did not finish. When
[workload-enforcement-failure](workload-enforcement-failure.md) is also in
`ALARM`, read that runbook: it lists the failing workloads or roles and
explains the exposure, which is the one place in the system without a lease
bound (a blocked workload keeps spending until the inline deny lands).

When this alarm fires **alone**, the pass died before the handler's own
error path: a timeout with many workloads and slow IAM calls, a roster the
function cannot parse after a manual environment edit, or a missing table
name. No deny was attached or removed in that pass; roles keep whatever
policy they had, so a workload blocked in an earlier pass stays denied and a
workload blocked *since* is not yet denied.

## Severity guidance

**Page** if any workload is currently `blocked`
(`GET /admin/workloads` → `subject.status`) and its role does not carry the
deny. **Next business day** otherwise.

## Likely causes

1. **Everything in [workload-enforcement-failure](workload-enforcement-failure.md)**:
   `iam:PutRolePolicy` / `DeleteRolePolicy` denied, role deleted, a users
   row that cannot be read.
2. **Timeout**: each pass reads every workload row and ledger, then calls
   IAM once per role; hundreds of workloads with throttled IAM can exceed
   two minutes.
3. **Environment edited by hand** (`WORKLOADS_JSON` not valid JSON,
   `USERS_TABLE` or `USAGE_TABLE` missing). A redeploy restores the template
   values.

```bash
aws logs filter-log-events --log-group-name /aws/lambda/<WorkloadEnforcerFn> \
  --filter-pattern '?Traceback ?"Task timed out"' \
  --start-time $(( $(date +%s) - 3600 ))000 --query 'events[].message' --output text | tail -20
```

## Remediation

1. If the functional alarm is also firing, follow
   [workload-enforcement-failure](workload-enforcement-failure.md), including
   the manual `put-role-policy` fallback for a blocked workload.
2. Otherwise fix the cause (redeploy to restore the environment, raise the
   timeout for a very large roster) and force a pass:
   ```bash
   aws lambda invoke --function-name <WorkloadEnforcerFn> \
     --payload '{"source":"aws.events"}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```
   The result JSON's `roles[]` shows `blocked`, `changed`, and
   `legacy_removed` per role.

## How to verify recovery

- `Errors` Sum = 0 for one 5-minute period → alarm `OK`.
- `WorkloadEnforcementSuccess` = 1 on the next pass; for every blocked
  workload `aws iam list-role-policies --role-name <role>` lists
  `bedrock-spend-controls-workload-deny-<region>-<stack>`.

## Related

- [workload-enforcement-failure.md](workload-enforcement-failure.md)
- [enforcement-dispatch-dlq.md](enforcement-dispatch-dlq.md)
- Component: [components/workload-enforcer.md](../components/workload-enforcer.md)
- Metrics: Lambda `Errors`, `Duration` (`AWS/Lambda`); `WorkloadEnforcementSuccess`, `WorkloadEnforcementFailure`, `WorkloadsBlocked` (no dimensions)
