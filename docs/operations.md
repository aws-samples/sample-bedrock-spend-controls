# Operating Bedrock Spend Controls

Day-to-day administration happens in the admin console or through the
SigV4 admin CLI (`examples/sigv4_gateway.py`). Both call the broker's admin
API ([admin-api.md](admin-api.md)). Alarms notify the `QuotaAlerts` SNS topic
and each has a runbook under [runbooks/](runbooks/README.md).

## Admin console

The console is a static React application served from CloudFront
(`AdminUiUrl` output) when `admin_ui: true`. It signs in with
authorization-code + PKCE against the deployment's OIDC issuer (the demo
Cognito pool, or your IdP), exchanges the ID token at a Cognito Identity Pool
for temporary AWS credentials, and SigV4-signs every request to the
`AWS_IAM` Function URL with the ID token in `X-Quota-User-Token`. Browser
administrators are authorized by `admin_jwt_claim` / `admin_jwt_value`; the
browser never receives the shared admin key, the emergency key, CloudWatch
permissions, or IAM policy ARNs. Numbers are formatted en-US
(`$1,234.56`, `31,000,000`); timestamps use the browser locale.

| Tab | What it shows | What you can do |
|---|---|---|
| **Overview** | Users and workloads as two groups with an all-subject total; daily per-model spend, requests, and token charts with 7/14/30-day ranges; top subjects by spend | Read-only. Charts come from CloudWatch EMF metrics (`GET /admin/usage/metrics`) and are observability only; the DynamoDB ledger is the quota source |
| **Users** | JWT identities, 25 per page, server-side search and status filter, period selector with highest-utilization signal | Create (conditional, never overwrites), edit calendar limits, thresholds, and rate limits, block and unblock with a reason, open the detail drawer: current daily/weekly/monthly usage, retained usage history, per-user audit, per-model budgets |
| **Workloads** | The deployed roster (name, model, inference profile, IAM role) joined with each metered row, with an enforcement pill: *Enforced*, *Metering only* (no `role_arn`), *Unregistered* (metered row no longer in the roster), *Awaiting traffic* (configured, no invocation since deploy) | Same limit and status operations as users once the row exists. A block on a metering-only workload is a *record block*: stored and alerted, not enforced |
| **Operations** | Enforcement configuration, runtime lease dial, emergency stop state, live leases, auto-block sweep card, spend reconciliation card, revocation health, detection-lag p95, CloudWatch alarm chips | Change the lease dial (audited, reason required); activate or recover the emergency stop (break-glass key + confirmation phrase + reason) |
| **Audit** | Global admin audit: create, limit, status, model-budget, and lease-dial events with actor, reason, and period qualification | Read-only, explicit refresh, own freshness state |

Independent panels keep independent freshness: a failed refresh marks only
that panel stale with its last successful timestamp. Missing telemetry and
`INSUFFICIENT_DATA` are displayed as unknown, never as healthy.

### Operations tab

`GET /admin/operations` powers the tab. Its payload:

| Key | Content |
|---|---|
| `configuration` | `mode: layered`, `credential_ttl_seconds`, `permission_lease_seconds` (effective), `permission_lease_source`, `permission_lease_default_seconds`, refresh overlap and jitter, `vend_rate_limit_per_minute`, `revocation_policy_shards`, `revocation_policy_max_characters`, `revocation_reconcile_minutes` |
| `emergency` | `state`, `desired_active`, `generation`, `applied_generation`, `requested_at`, `applied_at`, `converged` |
| `auto_block_sweep` | `schedule`, `status` (`ok`, `failed`, `never_ran`), `last_run` summary (`evaluated`, `lifted`, `still_blocked`, `admin_blocked`, `raced`, `failures`) |
| `metrics` | Detection-lag p95, revocation freshness (`last_reconciliation_at`, `reconciliation_status`: `current`, `degraded`, `unknown`), recent revocation failure and overflow counts, `revoked_identities_desired`, recent emergency failures |
| `alarms` | One entry per stack alarm with its key, name, and state |
| `cloudwatch` | `status` of the CloudWatch reads (`available`, `partial`, `unavailable`) |

The broker reads CloudWatch server-side with `GetMetricData`,
`DescribeAlarms`, and `ListMetrics`. If CloudWatch is denied or empty the
endpoint returns local state with `unavailable` / `unknown` markers and user
administration keeps working.

**Live leases.** The tab polls `GET /admin/users?include_usage=false` every
five seconds and renders the first page of subjects with an outstanding
logical lease: lease generation, deadline, and refresh window. The lease ID
itself is never exposed on read paths.

## Runtime lease dial

Enforcement is layered and always on; the one operational knob is the
permission-lease window.

```bash
# Read the effective window, its source, and the valid values (ETag = generation)
GET /admin/enforcement

# Change it: send the GET ETag as If-Match and a fresh Idempotency-Key
PUT /admin/enforcement
{"permission_lease_seconds": 60, "reason": "incident response"}
```

The change applies to new vends immediately; outstanding credentials keep
their issued deadline, and the revocation layer keeps cutting blocked
identities regardless of the dial. Every change writes an immutable
`CONFIG#ENFORCEMENT_AUDIT` row and emits `EnforcementDialChanged`. A stale
`If-Match` returns `409 version_conflict`; a reused `Idempotency-Key` with a
different body returns `409 idempotency_conflict`.

Use 60 seconds during an incident when the revocation layer is degraded
(see the revocation runbooks); return to 300 or 900 seconds afterwards, since
a shorter lease multiplies broker vends.

## Emergency stop

The emergency stop is an operator break-glass action, never an automatic
quota reaction. It closes credential vending for every user and applies an
unconditional Bedrock deny to the vended role. It does not act on workload
roles.

State machine (`GET /admin/emergency-stop`, also in `GET /admin/operations`
→ `emergency`):

```text
inactive --activate--> activating --(deny applied)--> active
active   --recover---> recovering --(deny inert)----> inactive
```

- `activating`: the broker already returns `503 emergency_stop` on every
  vend (strongly consistent DynamoDB read). Already-issued sessions keep
  working until the emergency processor's deny propagates or their lease
  deadline passes, whichever is first.
- `recovering`: vending stays closed until the deny has been replaced by its
  inert version; then the state becomes `inactive` and vending reopens.
- `converged: true` means the applied generation equals the requested one.
  A stuck transition raises `EmergencyStopFailureAlarm`
  ([runbook](runbooks/alarms/emergency-stop-failure.md)).

Authorization is separate from routine administration: the request must
carry the break-glass key from `EmergencyKeySecretArn` in
`X-Quota-Emergency-Key`, the action-specific confirmation phrase
(`STOP_ALL_BEDROCK_SESSIONS` or `RESTORE_ALL_BEDROCK_SESSIONS`), and a
non-empty reason. Routine admin keys and admin JWTs cannot invoke it. The
console holds the key only in the dialog state for that one request.

```bash
export EMERGENCY_ADMIN_KEY=$(aws secretsmanager get-secret-value \
  --secret-id "$(aws cloudformation describe-stacks --stack-name BedrockSpendControls \
      --query "Stacks[0].Outputs[?OutputKey=='EmergencyKeySecretArn'].OutputValue | [0]" --output text)" \
  --query SecretString --output text)

python examples/sigv4_gateway.py --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --emergency-key "$EMERGENCY_ADMIN_KEY" \
  emergency-stop --reason "security incident"

python examples/sigv4_gateway.py --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --emergency-key "$EMERGENCY_ADMIN_KEY" \
  emergency-recover --reason "incident resolved"
```

Both commands supply the confirmation phrase. Activation interrupts every
vended session after IAM propagation; recovery is also eventually
consistent. Check the alarms and the emergency state before and after either
action.

## Qualifying the enforcement bound

The overspend bound `metering lag + min(lease remainder, deny propagation)`
depends on IAM propagation and invocation-log delivery latency in your
account. Before relying on a specific number, measure it with the guarded
probes and record the evidence in
[../qualification/QUALIFICATION.md](../qualification/QUALIFICATION.md). The
probe is dry-run by default:

```bash
cdk/.venv/bin/python qualification/lease_revocation_probe.py \
  --profile YOUR_SANDBOX_PROFILE \
  --role-arn arn:aws:iam::111122223333:role/YOUR_DEDICATED_SANDBOX_ROLE \
  --managed-policy-arn arn:aws:iam::111122223333:policy/YOUR_PREATTACHED_SANDBOX_DENY \
  --region us-east-1 \
  --model-id YOUR_COUNT_TOKENS_MODEL
```

Live mode versions IAM policy and invokes Bedrock; run it only against
dedicated non-production resources after reviewing the printed account and
role.

## Alarms and notifications

The stack creates eight alarms in every deployment and two more when the
matching feature is configured. All notify the `QuotaAlerts` SNS topic
(`AlertTopicArn` output; `alert_email` adds an email subscription) and
appear as chips on the Operations tab.

| Alarm | Condition | Runbook |
|---|---|---|
| `EmergencyStopFailureAlarm` | Emergency processor failed to converge | [emergency-stop-failure](runbooks/alarms/emergency-stop-failure.md) |
| `EmergencyStopDlqAlarm` | Emergency stream batch dead-lettered | [emergency-stop-dlq](runbooks/alarms/emergency-stop-dlq.md) |
| `RevocationSyncFailureAlarm` | Deny shards could not be updated | [revocation-sync-failure](runbooks/alarms/revocation-sync-failure.md) |
| `RevocationPolicyOverflowAlarm` | A deny shard is over its 6,144-character capacity | [revocation-policy-overflow](runbooks/alarms/revocation-policy-overflow.md) |
| `AutoBlockSweepFailureAlarm` | Nightly sweep did not complete | [auto-block-sweep-failure](runbooks/alarms/auto-block-sweep-failure.md) |
| `EnforcementDispatchDlqAlarm` | Enforcement stream batch dead-lettered | [enforcement-dispatch-dlq](runbooks/alarms/enforcement-dispatch-dlq.md) |
| `EnforcementDispatchIteratorAgeAlarm` | Enforcement fan-out lagging over 5 minutes | [enforcement-dispatch-iterator-age](runbooks/alarms/enforcement-dispatch-iterator-age.md) |
| `PricingFallbackAlarm` | A request was priced at the fallback or with a missing dimension | [pricing-fallback](runbooks/alarms/pricing-fallback.md) |
| `WorkloadEnforcementFailureAlarm` (workloads configured) | Workload deny could not be attached or removed | [workload-enforcement-failure](runbooks/alarms/workload-enforcement-failure.md) |
| `SpendReconciliationDeltaAlarm` (`reconciliation_enabled`) | Ledger and bill disagree two days running | [reconciliation-delta](runbooks/alarms/reconciliation-delta.md) |

The same topic carries quota notifications: `WARNING <subject> [model <id>]
<period> <pct>%` at each `warn` threshold, `BLOCKED <subject> reason=...` on
every automatic block, and the operational messages listed in the component
runbooks (`EMERGENCY STOP ACTIVE`, `REVOCATION SYNC FAILED`, `WORKLOAD
BLOCKED WITHOUT ENFORCEMENT`, ...). Subscribe an on-call channel in
production.

## Reading the reconciliation card

With `reconciliation_enabled: true` the Operations tab shows **Spend
reconciliation**: the reconciled day, the ledger estimate, the Cost Explorer
figure, the delta, per-workload rows, and the number of stored runs. `GET
/admin/reconciliation?limit=N` returns the same data; with the feature off
both say so explicitly.

`delta = billed - estimated`; `delta_percent` is relative to the larger of
the two, so it stays within ±100 % (`null` only when both are zero).

| Sign | Meaning | Typical cause |
|---|---|---|
| Positive (bill > ledger) | Spend the ledger never saw | Other principals in the account calling Bedrock (console playground, other roles, `bedrock-mantle`); dropped invocation-log records. Expected in any account with unmetered callers: set `reconciliation_alarm_percent` above that floor or route those callers through the broker or a workload profile |
| Negative (bill < ledger) | The ledger counted spend the bill did not | Catalog over-pricing (fallback-priced models, a high override, a cross-Region profile priced at the base rate), credits or private pricing, Cost Explorer still settling |

A workload row with `tag_inactive: true` means the ledger has spend but Cost
Explorer sees none for the tag: activate the cost-allocation tag in the payer
account ([configuration.md](configuration.md#reconciliation)).

## Admin CLI

`examples/sigv4_gateway.py` is a SigV4-signed client for the admin API. It
reads `--gateway-url` (`BrokerApiUrl` output), `--profile`, `--region`,
`--admin-key`, and `--emergency-key` from flags or the `GATEWAY_URL`,
`AWS_PROFILE`, `AWS_REGION`, `ADMIN_KEY`, and `EMERGENCY_ADMIN_KEY`
environment variables.

Commands: `create-user`, `list-users`, `update-user`, `block-user`,
`unblock-user`, `get-usage`, `emergency-stop`, `emergency-recover`.

```bash
export GATEWAY_URL="$BROKER_API_URL"   # or pass --gateway-url on every call

# Create with daily and monthly limits, custom thresholds, and rate limits
python examples/sigv4_gateway.py create-user tenant-acme --name "ACME" \
  --daily-usd 25 --daily-input-tokens 10000000 --daily-output-tokens 2000000 \
  --daily-thresholds "50:warn,80:warn,100:block" \
  --monthly-usd 500 --monthly-input-tokens 200000000 --monthly-output-tokens 40000000 \
  --monthly-thresholds "100:warn" \
  --rpm 60 --tpm 100000

# List
python examples/sigv4_gateway.py list-users

# Update some dimensions; unspecified periods are preserved
python examples/sigv4_gateway.py update-user tenant-acme \
  --daily-usd 30 --daily-input-tokens 12000000 --daily-output-tokens 2500000 \
  --reason "Reviewed annual allocation"

# Disable a period, or remove rate limits
python examples/sigv4_gateway.py update-user tenant-acme --disable-monthly --reason "Monthly cap retired"
python examples/sigv4_gateway.py update-user tenant-acme --disable-rate --reason "Batch job needs bursts"

# Block and unblock
python examples/sigv4_gateway.py block-user tenant-acme --reason "security review"
python examples/sigv4_gateway.py unblock-user tenant-acme --reason "review closed"

# Current calendar usage (daily by default; --window YYYY-MM-DD for a past window)
python examples/sigv4_gateway.py get-usage tenant-acme --period monthly
```

`--<period>-thresholds` takes comma-separated `<percent>:<warn|block>`
entries; omit the `block` entry for an alert-only period. `--rpm` / `--tpm`
take `0` to switch one dimension off. `update-user`, `block-user`, and
`unblock-user` first `GET /admin/user?user_id=...` and send the returned ETag
as `If-Match`, so a concurrent change surfaces as `409 version_conflict`
instead of being overwritten. Workloads use the same commands with
`workload:<name>` as the identity.

## Safe routine writes

Every routine mutation follows the same rules, in the console, the CLI, and
any client you write:

1. **Create is conditional.** `POST /admin/users` returns `409
   user_already_exists` with the current user and its ETag instead of
   overwriting. Do an exact `GET /admin/user` before deciding whether to
   create, so reruns do not depend on the duplicate response.
2. **Send `Idempotency-Key`** (a UUID) on every mutation. Replaying the same
   key with the same body is safe and returns the original result; the same
   key with a different body returns `409 idempotency_conflict`. Without the
   header the server generates a key and a retry is not deduplicated.
3. **Send `If-Match`** on limit, status, and model-budget changes with the
   ETag (or integer `version`) of the user you reviewed. A stale value
   returns `409 version_conflict` with the current user in `details`.
   Without the header the write is unconditional.
4. **Carry a reason** on status and limit writes. Omitted reasons are stored
   as `not provided`.
5. **Treat every `409` as a stop.** Refresh, review, and decide; never retry
   a conflict blindly.

Successful writes return the complete canonical user, a new `ETag`, and
`X-Request-Id`. Routine audit events are retained for 365 days in the
`AdminAuditTable`; usage history is retained for `usage_retention_days`.

Temporary per-user overrides, bulk operations, user delete, and usage reset
are not provided.
