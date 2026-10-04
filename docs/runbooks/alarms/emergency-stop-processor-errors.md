# EmergencyStopProcessorErrorsAlarm

**Operations key:** `emergency_processor_errors` · **Alarm name:** `<stack>-emergency-processor-errors` · **Metric:** Lambda `Errors` on `EmergencyStopProcessorFn` (Sum ≥ 1 over 5 min, 1 period; missing data = not breaching) · **Source:** the function's own error count. `emergency_processor/handler.py` emits `EmergencyStopFailure` and then **re-raises**, so this alarm normally fires together with [emergency-stop-failure](emergency-stop-failure.md); it also catches failures that happen before the handler's own error path (init, environment, the control-row read).

## What it means

The emergency processor did not finish a pass. If the control row is in
`activating` or `recovering`, the operator's request has not converged: read
[emergency-stop-failure](emergency-stop-failure.md), which describes the
state the system is in (fail-closed for new vends either way). If the state
is `active` or `inactive` and `converged: true`, the failing pass was a
routine 1-minute schedule tick that found nothing to do and still crashed,
which points at an infrastructure or configuration problem rather than a
stuck transition.

The 1-minute `EmergencyStopReconciliationSchedule` and the users-table
stream retry automatically; a single error often self-heals.

## Severity guidance

**Page** when `GET /admin/emergency-stop` shows `activating` or `recovering`
or when `EmergencyStopFailureAlarm` is also in `ALARM`. **Next business day**
when the state is stable and converged.

## Likely causes

1. **Everything listed in [emergency-stop-failure](emergency-stop-failure.md)**:
   IAM `CreatePolicyVersion`/`DeletePolicyVersion` denied or throttled,
   desired state flip-flopping.
2. **Control-row read failure**: `dynamodb:GetItem` on `CONFIG#EMERGENCY_STOP`
   denied (the role is restricted with `dynamodb:LeadingKeys`; a manual
   policy edit that drops the condition's key breaks it) or a DynamoDB
   outage.
3. **Missing environment** after a manual edit (`EMERGENCY_POLICY_ARN`,
   `USERS_TABLE`, `SNS_TOPIC_ARN`).
4. **Timeout** (2 min) while IAM is slow.

```bash
aws logs filter-log-events --log-group-name /aws/lambda/<EmergencyStopProcessorFn> \
  --filter-pattern '?Traceback ?"Task timed out" ?EmergencyStopFailure' \
  --start-time $(( $(date +%s) - 3600 ))000 --query 'events[].message' --output text | tail -20
```

## Remediation

1. Read the state: `GET /admin/emergency-stop` (or the DynamoDB row, see
   the failure runbook). If a transition is pending, follow
   [emergency-stop-failure](emergency-stop-failure.md) including its manual
   IAM fallback.
2. Otherwise fix the infrastructure cause (restore the role policy from the
   template by redeploying, wait out throttling) and force a pass:
   ```bash
   aws lambda invoke --function-name <EmergencyStopProcessorFn> \
     --payload '{"source":"aws.events"}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```
   A healthy idle pass returns `{"applied": false, "reason": "already-applied"}`
   (or `state-not-found` if no emergency was ever requested).

## How to verify recovery

- `Errors` Sum = 0 for one 5-minute period → alarm `OK`.
- `GET /admin/operations` → `emergency.converged: true` and
  `metrics.recent_emergency_failures: 0`.

## Related

- [emergency-stop-failure.md](emergency-stop-failure.md)
- [emergency-stop-dlq.md](emergency-stop-dlq.md)
- Component: [components/emergency-processor.md](../components/emergency-processor.md)
- Metrics: Lambda `Errors` (`AWS/Lambda`); `EmergencyStopFailure`, `EmergencyStopActivated`, `EmergencyStopRecovered` (no dimensions)
