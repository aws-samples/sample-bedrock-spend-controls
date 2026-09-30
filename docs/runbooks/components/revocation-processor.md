# Component: revocation processor (`RevocationProcessorFn`)

**Code:** `revocation_processor/handler.py`. **Log group:**
`/aws/lambda/<RevocationProcessorFn>`. **Invocation:** async `Invoke` from
the enforcement dispatcher (`source: enforcement-dispatch`) and the
`RevocationReconciliationSchedule` (`rate(<revocation_reconcile_minutes>)`,
default 5 min, `source: aws.events`). 256 MB, 2 min timeout, reserved
concurrency 1.

## What it does

Converges the 19 pre-attached `QuotaRevocationPolicy<n>` managed policies on
`BedrockUserRole` onto the set of currently blocked identities. Each pass:
strongly consistent **scan** of the users table for rows with
`status == blocked` and a `source_identity`; hash each identity to a shard
(`sha256(identity)[:8] mod 19`); build one deny document per shard
(`Deny bedrock:InvokeModel*/CountTokens where aws:SourceIdentity in
[...]`, or the inert sentinel `__no_blocked_quota_identity__` when empty);
compare with the current default version and `CreatePolicyVersion
--set-as-default` only when different, deleting the oldest non-default
version first when five exist. A shard whose desired document exceeds
`REVOCATION_POLICY_MAX_CHARACTERS` (6 144) is **left at its last good
version** and counted as overflow.

It never creates, attaches, or detaches policies, and cannot touch the
role, its trust policy, or the permissions boundary.

## IAM scope

- `dynamodb:Scan`/`GetItem`/`Query` (read) on the users table.
- `iam:GetPolicy`, `GetPolicyVersion`, `ListPolicyVersions`, `CreatePolicyVersion`, `DeletePolicyVersion` on exactly the 19 shard policy ARNs.
- `sns:Publish` on `QuotaAlerts`.

## Inputs / outputs

| Input | Output |
|---|---|
| Users-table scan | IAM policy versions on the shards |
| Env `REVOCATION_POLICY_ARNS_JSON`, `REVOCATION_POLICY_MAX_CHARACTERS` | EMF `RevocationSyncSuccess`, `RevocationSyncFailure`, `RevocationPolicyOverflow`, `RevokedIdentitiesDesired` |
| | SNS `REVOCATION SYNC FAILED`, `REVOCATION POLICY CAPACITY EXCEEDED` |
| | Return `{reconciled, blocked_identities, updated_shards, unchanged_shards, overflow_shards, shards[]}` |

## Failure modes

| Symptom | Alarm | Notes |
|---|---|---|
| IAM error mid-loop | [revocation-sync-failure](../alarms/revocation-sync-failure.md) | Shards before the failure were updated; the rest wait for the next pass. Raises so the schedule retries. |
| Shard too large | [revocation-policy-overflow](../alarms/revocation-policy-overflow.md) | Last-good kept; new identities in that shard rely on the lease. Does **not** raise. |
| Scan cost | — | Full users-table scan per pass. Fine at thousands of rows; at tens of thousands consider a `status` GSI (not implemented). |
| Identity blocked but never vended | — | No `source_identity` on the row → not in any shard. Harmless: there is no session to cut. |

## Manual operations

- **Force a reconcile:** `aws lambda invoke --function-name <RevocationProcessorFn> --payload '{"source":"aws.events"}' --cli-binary-format raw-in-base64-out /dev/stdout`.
- **Which shard holds an identity:**
  `python3 -c 'import hashlib;i="<source_identity>";print(int.from_bytes(hashlib.sha256(i.encode()).digest()[:8],"big")%19)'`.
- **Read a shard's live document:** see the sync-failure runbook step 1.
- **Reset a shard to inert by hand** (only if you are sure no identity in it
  should be denied): create a version with the sentinel document
  `deny_policy([])` from the handler — the same statement with
  `"aws:SourceIdentity": ["__no_blocked_quota_identity__"]`. The next pass
  will overwrite it with the correct set.
- **Never** change the shard count in place (synth refuses; see
  `revocation_policy_shards` in [configuration.md](../../configuration.md#keys)):
  rehashing live identities across a different shard count opens a window in
  which a blocked identity is in no shard.
