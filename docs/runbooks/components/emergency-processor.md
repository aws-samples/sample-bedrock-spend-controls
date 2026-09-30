# Component: emergency processor (`EmergencyStopProcessorFn`)

**Code:** `emergency_processor/handler.py`. **Log group:**
`/aws/lambda/<EmergencyStopProcessorFn>`. **Invocation:** users-table stream
(`batch_size=10`, 1 s window, bisect, 10 retries, DLQ
`EmergencyStopDeadLetterQueue`) filtered to key `CONFIG#EMERGENCY_STOP`, and
`EmergencyStopReconciliationSchedule` (`rate(1 minute)`). 256 MB, 2 min
timeout, reserved concurrency 1.

## What it does

Reads `CONFIG#EMERGENCY_STOP` (strongly consistent) and converges the single
`EmergencyBedrockDenyPolicy` managed policy on `BedrockUserRole`:
`desired_active: true` → an **unconditional** deny of
`bedrock:InvokeModel*`/`CountTokens` (every vended session, regardless of
`SourceIdentity`); `false` → the same statement gated on
`aws:SourceIdentity == "__emergency_stop_inactive__"`, which matches no real
session (inert). After the IAM write it conditionally updates the control
row to `state: active|inactive`, `applied_generation = generation`. Up to
three passes handle a desired state that flips during propagation.

The broker separately refuses vends whenever `state != inactive` — so
activation closes new access **before** IAM cuts existing sessions, and
recovery keeps vending closed **until** the deny is inert again.

## IAM scope

- `dynamodb:GetItem`, `UpdateItem` on the users table **restricted by
  `dynamodb:LeadingKeys` to `CONFIG#EMERGENCY_STOP`**.
- `iam:GetPolicy`, `GetPolicyVersion`, `ListPolicyVersions`, `CreatePolicyVersion`, `DeletePolicyVersion` on the one emergency policy ARN.
- `sns:Publish` on `QuotaAlerts`.

## Inputs / outputs

| Input | Output |
|---|---|
| `CONFIG#EMERGENCY_STOP` row (`desired_active`, `generation`, `request_id`, `actor`, `reason`) | Emergency policy default version; row `state`, `applied_at`, `applied_generation` |
| | EMF `EmergencyStopActivated`, `EmergencyStopRecovered`, `EmergencyStopFailure` |
| | SNS `EMERGENCY STOP ACTIVE` / `RECOVERED` / `APPLY FAILED` |

## Failure modes

| Symptom | Alarm | Notes |
|---|---|---|
| IAM error | [emergency-stop-failure](../alarms/emergency-stop-failure.md) | State stuck in `activating`/`recovering`; vending closed either way. |
| Stream batch lost | [emergency-stop-dlq](../alarms/emergency-stop-dlq.md) | Schedule converges within a minute if the cause cleared. |
| `already-applied` returned | — | Normal on the schedule when nothing changed. |
| `state-not-found` | — | No emergency has ever been requested. Normal. |

## Manual operations

- **Read state:** `GET /admin/emergency-stop` or the DynamoDB row.
- **Request activate/recover:** only via `POST /admin/emergency-stop` with
  the break-glass key and confirmation phrase (`examples/sigv4_gateway.py
  emergency-stop|emergency-recover --reason ...`). Do not edit the control row
  by hand — the row's immutable `EMERGENCY_AUDIT#` companion is written by the
  API and is the audit record.
- **Force a convergence pass:** `aws lambda invoke ... --payload '{"source":"aws.events"}'`.
- **Apply/remove the deny by hand** when the processor is broken: see the
  failure runbook; use exactly `emergency_policy(True|False)` from the handler
  so the processor recognises the document and marks the state.
- **Verify the policy is inert** after recovery:
  `aws iam get-policy-version ... --query 'PolicyVersion.Document.Statement[0].Condition'`
  must show the `__emergency_stop_inactive__` sentinel.
