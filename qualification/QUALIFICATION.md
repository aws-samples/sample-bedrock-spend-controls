# Enforcement qualification playbook

This sample ships with three always-on enforcement layers (permission lease,
active-session revocation, emergency stop) whose *timing* guarantees depend on
behavior AWS does not contractually document — IAM policy propagation and
invocation-log delivery latency. Before you rely on a specific overspend bound
in production, measure it in your own account and record the evidence here.
This file is a template: the results table ships empty, and every "Pending"
row is yours to fill in after running the probes.

The bound to validate is:

```text
overspend <= metering lag + min(lease remainder, deny propagation)
```

Unit, API, policy, and CDK tests in `tests/` cover the logical behavior
(conditional lease reservation, non-extending same-ID retries, refresh windows,
deterministic policy sharding, capacity overflow handling, emergency
state-machine ordering, and reconciliation). They do not measure your account's
propagation or log-delivery latency — that is what the live probes are for.

## Live probe prerequisites

Record all of these before execution:

- Dedicated non-production AWS profile:
- Account ID:
- Region:
- Dedicated sandbox role ARN:
- CountTokens-compatible model ID:
- Role tags proving non-production:
- Approver and approval timestamp:

The sandbox role must trust the selected caller for `sts:AssumeRole` and
`sts:SetSourceIdentity`, allow the tested Bedrock Runtime actions, and have a
dedicated pre-attached managed deny policy safe for temporary
`CreatePolicyVersion`/default-version changes. Never use an untagged shared or
production role.

## Required live measurements

Run `qualification/lease_revocation_probe.py` against the sandbox role, attach its
JSON output, and fill in:

| Check | Samples | p50 | p95 | Maximum | Pass criterion | Result |
|---|---:|---:|---:|---:|---|---|
| 60s lease: denial after deadline |  |  |  |  | New calls denied at the deadline | Pending |
| 300s lease: denial after deadline |  |  |  |  | New calls denied at the deadline | Pending |
| 900s lease: denial after deadline |  |  |  |  | New calls denied at the deadline | Pending |
| Targeted deny propagation (block) |  |  |  |  | Under 300s; a second, unblocked user unaffected | Pending |
| Deny removal propagation (recovery) |  |  |  |  | Under 300s | Pending |
| One-hour role-chained session |  |  |  |  | Succeeds | Pending |
| 3,601-second role-chained session |  |  |  |  | Rejected by STS | Pending |
| In-flight streaming response at cutoff |  |  |  |  | Behavior documented; completion allowed | Pending |

Also measure metering lag separately with the `DetectionLagMilliseconds`
metric (invocation-log delivery plus processing). Do not fold it into the IAM
or lease numbers and present the total as one service guarantee — report the
formula's terms individually.

## Production promotion gates

### Runtime dial values (60 / 300 / 900 seconds)

Before allowing a dial value in production, all must be true for that value:

1. The deadline probe denies new Bedrock calls at that lease window.
2. A load test at your expected concurrently active user count passes with
   refresh jitter and no synchronized refresh spike (the shorter the lease,
   the higher the refresh rate: budget roughly `active_users / lease_seconds`
   vends per second against broker Lambda, DynamoDB, and the shared regional
   STS quota).
3. The operational runbook distinguishes `expiration` (Bedrock permission
   deadline) from `sts_expiration` (key lifetime).

### Revocation layer timing claims

The deny shards deploy with the stack either way; before quoting a
mid-session cutoff time, all must be true:

1. The targeted deny never affects a control (unblocked) identity.
2. Every observed propagation sample is below five minutes.
3. Your worst-case simultaneous blocked-identity count fits the immutable
   19-shard managed-policy set by actual serialized size, and the account
   supports the resulting 20 role policy attachments including the emergency
   stop.
4. The IAM throttling, overflow, DLQ, stale-state, and reconciliation alarms
   are owned and exercised.
5. Your security team has reviewed and accepted the narrowly scoped
   managed-policy versioning permissions (`iam:CreatePolicyVersion` on the
   designated deny policies only).

### Emergency stop

Exercise one full activate/recover cycle in the sandbox and record both
propagation times before granting break-glass credentials to operators.

## Evidence log

Append one dated entry per qualification run:

```markdown
### YYYY-MM-DD — <region>, <account alias>
- Probe output: <link or attached JSON>
- Results table rows updated: <which>
- Decision: <which dial values / claims are now approved>
- Approver:
```
