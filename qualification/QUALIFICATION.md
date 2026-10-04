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
- Dedicated sandbox managed deny policy ARN:
- CountTokens-compatible model ID:
- Role and policy tags proving non-production:
- Approver and approval timestamp:

The sandbox role must trust the selected caller for `sts:AssumeRole` and
`sts:SetSourceIdentity`, allow the tested Bedrock Runtime actions, and have a
dedicated pre-attached managed deny policy safe for temporary
`CreatePolicyVersion`/default-version changes. Never use an untagged shared or
production role.

The probe enforces that, and refuses to run live unless all of the following
hold (it checks them read-only before any mutation):

- Both the role and the managed policy carry the tag
  `bedrock-spend-controls:qualification=true`.
- The managed policy has no `aws:cloudformation:*` tags (it must be created
  by hand for the probe, not by a stack, so it can never be a production
  deny shard).
- The managed policy's default version is the probe placeholder document,
  printed by the dry run under `prerequisites.placeholder_policy_document`
  (a deny that matches only the source identity
  `bedrock-spend-controls-qualification-placeholder`, so it affects nobody).
- The probe only deletes policy versions it created in the current run. If
  the policy already has five versions, remove old ones yourself first.

Run the dry run first and keep its JSON with the evidence:

```bash
python qualification/lease_revocation_probe.py \
  --role-arn arn:aws:iam::<account>:role/<sandbox-role> \
  --managed-policy-arn arn:aws:iam::<account>:policy/<sandbox-probe-policy> \
  --model-id <count-tokens-capable-model> \
  --lease-seconds 60 300 900
```

Live mode adds `--execute` plus the three confirmation arguments the dry run
prints under `live_confirmation`. A 900 s lease waits the full 15 minutes
before checking the denial; the probe asks STS for a 1,200 s session for that
lease so the keys outlive the deadline and the denial is a lease decision,
not an expired token.

## Required live measurements

Run `qualification/lease_revocation_probe.py` against the sandbox role with
`--lease-seconds 60 300 900` (or the subset of dial values you intend to
allow), attach its JSON output, and fill in:

| Check | Samples | p50 | p95 | Maximum | Pass criterion | Result |
|---|---:|---:|---:|---:|---|---|
| 60s lease: denial after deadline (`--lease-seconds 60`) |  |  |  |  | New calls denied at the deadline | Pending |
| 300s lease: denial after deadline (`--lease-seconds 300`) |  |  |  |  | New calls denied at the deadline | Pending |
| 900s lease: denial after deadline (`--lease-seconds 900`) |  |  |  |  | New calls denied at the deadline | Pending |
| Targeted deny propagation (block) |  |  |  |  | Under 300s; a second, unblocked user unaffected | Pending |
| Deny removal propagation (recovery) |  |  |  |  | Under 300s | Pending |
| One-hour role-chained session |  |  |  |  | Succeeds | Pending |
| 3,601-second role-chained session |  |  |  |  | Rejected by STS | Pending |
| In-flight streaming response at cutoff |  |  |  |  | Behavior documented; completion allowed | Pending |

Also measure metering lag separately with the `DetectionLagMilliseconds`
metric (invocation-log delivery plus processing). Do not fold it into the IAM
or lease numbers and present the total as one service guarantee — report the
formula's terms individually.

### End-to-end lease check on a deployed stack

`qualification/lease_stack_qualification.py` exercises the broker itself:
Cognito sign-in, SigV4 vend, CountTokens before the deadline, AccessDenied
after it. Run it from the repository root; the default is a dry run that
prints the plan, and `--live` executes it:

```bash
python qualification/lease_stack_qualification.py --stack-name <stack> --lease-seconds 300
python qualification/lease_stack_qualification.py --profile <sandbox> \
  --stack-name <stack> --lease-seconds 300 --live
```

`--lease-seconds` must match the stack's current lease dial (60, 300, or
900); the script checks the vended `expiration` against that window. Clean-up:
the script deletes the Cognito user it created (`--keep-user` keeps it; a
pre-existing user is never deleted). The quota row the broker auto-provisions
for that user (`quota-lease-qualification` by default) stays in the users
table because the admin API has no delete; block it from the console or CLI
if you do not want it to remain vendable.

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
