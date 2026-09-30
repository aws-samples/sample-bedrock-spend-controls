# EmergencyStopFailureAlarm

**Operations key:** `emergency_failure` · **Metric:** `EmergencyStopFailure` (Sum ≥ 1 over 5 min, 1 period) · **Emitted by:** `emergency_processor/handler.py` `_emit("EmergencyStopFailure", ...)` in the `except` at the bottom of `handler()`.

## What it means

The emergency processor tried to converge the `CONFIG#EMERGENCY_STOP` desired
state onto the `EmergencyBedrockDenyPolicy` managed policy and failed. The
control state in DynamoDB is still `activating` or `recovering`:

- If an operator requested **activation**, the broker is already returning
  `503 emergency_stop` to every vend (that gate is the strongly consistent
  DynamoDB read, not IAM), but **already-issued sessions have not been cut**.
  They keep working until their permission lease deadline (60/300/900 s).
- If an operator requested **recovery**, vending stays closed (the broker
  refuses while state ≠ `inactive`) and the role-wide deny is still applied.
  Nobody can call Bedrock through the vended role.

Either way the system is fail-closed for new access but has not reached the
state the operator asked for.

## Severity guidance

**Page.** An emergency stop is only ever invoked during an incident; a stuck
transition means the incident response is not doing what the operator
believes it is doing. The 1-minute reconcile schedule retries automatically,
so a single alarm evaluation may self-heal — check the state before acting.

## Likely causes

1. **IAM `CreatePolicyVersion` denied or throttled.** The Lambda role may
   only version the one emergency policy ARN. Check:
   ```bash
   aws logs filter-log-events --log-group-name /aws/lambda/<EmergencyStopProcessorFn> \
     --filter-pattern '{ $.EmergencyStopFailure = 1 }' --start-time $(( $(date +%s) - 3600 ))000 \
     --query 'events[].message' --output text | tail -5
   ```
   The payload's `error` field carries the boto3 message (`AccessDenied`,
   `LimitExceeded`, `Throttling`).
2. **Five policy versions with no removable one** (`RuntimeError: emergency
   policy has no removable version`). Should not happen — the processor
   deletes the oldest non-default version first — but a manually set default
   version can leave four non-default versions that are all newer. Inspect:
   ```bash
   aws iam list-policy-versions --policy-arn "$(aws cloudformation describe-stacks --stack-name BedrockSpendControls --query "Stacks[0].Outputs[?OutputKey=='EmergencyDenyPolicyArn'].OutputValue | [0]" --output text)"
   ```
3. **Desired state flip-flopping** (`emergency desired state changed
   repeatedly during reconciliation`): two operators issued activate/recover
   within seconds. The processor gives up after three passes and lets the
   next stream event or the 1-minute schedule retry.
4. **Stream batch stuck** — see [emergency-stop-dlq.md](emergency-stop-dlq.md);
   the two alarms usually fire together.

## Remediation

1. Read the control state and the requested action:
   ```bash
   aws dynamodb get-item --table-name "$(aws cloudformation describe-stacks --stack-name BedrockSpendControls --query "Stacks[0].Outputs[?OutputKey=='UsersTableName'].OutputValue | [0]" --output text)" \
     --key '{"user_id":{"S":"CONFIG#EMERGENCY_STOP"}}' --consistent-read
   ```
   `desired_active`, `state`, `generation`, `applied_generation`, and the
   operator's `actor`/`reason` are all there. `GET /admin/emergency-stop` and
   the Operations tab show the same row.
2. If the cause is an IAM error, fix the permission/quota. The 1-minute
   schedule (`EmergencyStopReconciliationSchedule`) retries on its own; you
   can force a pass immediately:
   ```bash
   aws lambda invoke --function-name <EmergencyStopProcessorFn> \
     --payload '{"source":"aws.events"}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```
   A successful pass returns `"applied": true` and the state flips to
   `active`/`inactive`.
3. **If activation is stuck and you must cut sessions now** (destructive,
   affects every vended session): set the emergency policy's default version
   to an unconditional deny by hand. Use exactly the document the processor
   would write (`emergency_policy(True)` in `emergency_processor/handler.py`):
   ```bash
   aws iam create-policy-version --policy-arn <EmergencyDenyPolicyArn> --set-as-default \
     --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"EmergencyStopAllBedrockSessions","Effect":"Deny","Action":["bedrock:CountTokens","bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"}]}'
   ```
   The processor will then see the document already matches and mark the
   state `active` on its next pass.
4. **If recovery is stuck**, do the inverse with `emergency_policy(False)`
   (the same statement plus
   `"Condition":{"StringEquals":{"aws:SourceIdentity":["__emergency_stop_inactive__"]}}`).
   Do not delete the policy or detach it from the role; the stack owns the
   attachment.

## How to verify recovery

- `GET /admin/operations` → `emergency.converged: true` and `state` equals
  `active` (after activate) or `inactive` (after recover).
- The alarm returns to `OK` after one 5-minute period with no failures.
- SNS delivered `EMERGENCY STOP ACTIVE` / `EMERGENCY STOP RECOVERED`.

## Related

- [emergency-stop-dlq.md](emergency-stop-dlq.md)
- Component: [components/emergency-processor.md](../components/emergency-processor.md)
- Metrics: `EmergencyStopActivated`, `EmergencyStopRecovered`, `EmergencyStopFailure`
