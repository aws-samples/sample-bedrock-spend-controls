# RevocationSyncFailureAlarm

**Operations key:** `revocation_failure` · **Metric:** `RevocationSyncFailure` (Sum ≥ 1 over 5 min, 1 period) · **Emitted by:** `revocation_processor/handler.py` in the `except (ClientError, RuntimeError, ValueError)` around the shard loop.

## What it means

The revocation processor could not bring one or more of the 19
`QuotaRevocationPolicy<n>` deny shards in line with the set of blocked
`source_identity` values. **Spend may be under-enforced:** an identity that
metering blocked keeps its already-issued STS session until the permission
lease deadline (60/300/900 s, runtime dial) instead of being cut within IAM
propagation time. New vends for that identity are still refused (the broker
gate is the DynamoDB row, not IAM), so exposure is bounded by
`metering lag + lease remainder`, not unbounded.

The processor raises after emitting the metric, so the invoking path (stream
dispatch or the `RevocationReconciliationSchedule`) records a Lambda error
and, for the schedule, retries on the next tick
(`revocation_reconcile_minutes`, default 5).

## Severity guidance

**Page when the runtime lease dial is 900 s and blocked identities exist**
(`RevokedIdentitiesDesired` > 0): the fallback bound is 15 minutes of
overspend per blocked identity per refresh. **Next business day when the dial
is 60 s** or no identities are currently blocked. Check both in
`GET /admin/operations` (`configuration.permission_lease_seconds`,
`metrics.revoked_identities_desired`).

## Likely causes

1. **IAM API failure** — `AccessDenied` (role policy edited), `Throttling`
   (19 shards × `GetPolicy` + `GetPolicyVersion` per pass, plus a
   `CreatePolicyVersion` per changed shard), or `LimitExceeded`. The error
   string is in the SNS message `REVOCATION SYNC FAILED` and in the log:
   ```bash
   aws logs filter-log-events --log-group-name /aws/lambda/<RevocationProcessorFn> \
     --filter-pattern '{ $.RevocationSyncFailure = 1 }' \
     --start-time $(( $(date +%s) - 3600 ))000 --query 'events[].message' --output text | tail -3
   ```
2. **`managed policy has no removable non-default version`** — a shard has
   five versions and none but the default can be deleted. Only possible if
   someone set a default by hand; fix with `aws iam delete-policy-version` on
   a non-default version.
3. **Users-table scan failure** (`_blocked_identities` does a
   `ConsistentRead` scan) — `ProvisionedThroughputExceeded` is impossible on
   the on-demand table, but a DynamoDB outage surfaces here.
4. **`at least one revocation policy ARN is required`** — the
   `REVOCATION_POLICY_ARNS_JSON` env is empty. Deployment defect; redeploy.

## Remediation

1. Identify which shards are stale. The success log line lists every shard
   with `changed`, `overflow`, and character counts; the failure line stops at
   the shard that raised. Compare the desired deny set with what IAM holds:
   ```bash
   # Blocked identities the processor wants denied
   aws dynamodb scan --table-name <UsersTableName> \
     --filter-expression '#s = :b AND attribute_exists(source_identity)' \
     --expression-attribute-names '{"#s":"status"}' --expression-attribute-values '{":b":{"S":"blocked"}}' \
     --projection-expression 'user_id, source_identity' --output table
   # What shard 0 currently denies
   ARN=$(aws cloudformation describe-stack-resources --stack-name BedrockSpendControls \
     --logical-resource-id QuotaRevocationPolicy0 --query 'StackResources[0].PhysicalResourceId' --output text)
   aws iam get-policy-version --policy-arn "$ARN" \
     --version-id "$(aws iam get-policy --policy-arn "$ARN" --query Policy.DefaultVersionId --output text)" \
     --query PolicyVersion.Document
   ```
2. Fix the root cause (restore IAM permissions, wait out throttling, delete
   a stray policy version).
3. Force a reconcile pass rather than waiting for the schedule:
   ```bash
   aws lambda invoke --function-name <RevocationProcessorFn> \
     --payload '{"source":"aws.events"}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```
   The result JSON reports `updated_shards`, `unchanged_shards`,
   `overflow_shards`.
4. **If IAM cannot be repaired quickly and blocked identities are actively
   spending**, shorten the exposure with the runtime dial:
   `PUT /admin/enforcement {"permission_lease_seconds": 60, "reason": "..."}`.
   This applies to new vends only; outstanding leases keep their deadline.
   The emergency stop is the last resort and affects every session.

## How to verify recovery

- `RevocationSyncSuccess` emitted (visible as
  `metrics.last_reconciliation_at` in `/admin/operations`,
  `reconciliation_status: current`).
- `RevocationSyncFailure` Sum = 0 for one period → alarm `OK`.
- A blocked identity's `source_identity` appears in the expected shard
  (`shard_for(identity, 19)` in `revocation_processor/handler.py` is
  `sha256(identity)[:8] mod 19`).

## Related

- [revocation-policy-overflow.md](revocation-policy-overflow.md)
- [enforcement-dispatch-dlq.md](enforcement-dispatch-dlq.md)
- Component: [components/revocation-processor.md](../components/revocation-processor.md)
- Metrics: `RevocationSyncSuccess`, `RevocationSyncFailure`, `RevocationPolicyOverflow`, `RevokedIdentitiesDesired`
