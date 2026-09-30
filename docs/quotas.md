# How quotas work

This page describes what a quota is in Bedrock Spend Controls, how it is
evaluated, and what happens when it is reached. Configuration keys are in
[configuration.md](configuration.md); the admin routes that set limits are in
[admin-api.md](admin-api.md).

## Subjects

A quota belongs to a **subject**: the value of the configured JWT claim
(`jwt_user_claim`, default `sub`) for per-user or per-tenant quotas, or
`workload:<name>` for a workload that calls Bedrock with its own IAM role
through an application inference profile. Every subject is one row in the
users table with the same limits schema, status fields, and admin routes.

| Field | Values | Meaning |
|---|---|---|
| `status` | `active`, `blocked` | Whether the broker vends credentials (users) or the workload role may invoke (workloads) |
| `status_origin` | `admin`, `automatic` | Who set the status. Automatic blocks lift by themselves; admin blocks do not |
| `status_reason` | free text | Operator reason, or the generated `auto: ...` reason |
| `limits` | see below | Calendar periods, thresholds, and rate limits |
| `model_budgets` | see below | Optional per-model budgets |

## Calendar periods

Quotas are evaluated over fixed UTC calendar windows, not rolling intervals.
Every enabled period is evaluated on every credential vend and every metered
invocation; reaching a block threshold on any one of them blocks the subject.

| Period | Window | Resets |
|---|---|---|
| `daily` | 00:00 UTC to 00:00 UTC the next day | every day |
| `weekly` | Monday 00:00 UTC to the following Monday | every Monday |
| `monthly` | the 1st at 00:00 UTC to the 1st of the next month | every month |

A period is an object or `null`:

```json
{
  "daily":   {"usd": 5, "input_tokens": 1000000, "output_tokens": 200000,
              "thresholds": [{"at": 0.8, "action": "warn"}, {"at": 1.0, "action": "block"}]},
  "weekly":  {"usd": 25, "input_tokens": 0, "output_tokens": 0,
              "thresholds": [{"at": 1.0, "action": "warn"}]},
  "monthly": null
}
```

- `null` disables the period entirely.
- `0` on one dimension means **Unlimited** for that dimension of an enabled
  period. The console shows it as "Unlimited".
- `usd` is the estimated spend priced from the catalog
  ([pricing.md](pricing.md)); it is an estimate, not the bill.
- `input_tokens` counts uncached input tokens and `output_tokens` counts
  output tokens. Prompt-cache read and write tokens are recorded separately
  and count toward `usd` only.

Deployment defaults enable a finite daily period and leave weekly and monthly
disabled (`default_limits` in [configuration.md](configuration.md)).

### How weekly and monthly totals are computed

The daily ledger row per subject is the canonical record. Weekly and monthly
totals are not stored; they are derived on demand by a strongly consistent
DynamoDB range query over the retained daily rows of the current window.

The query reads at most **37 rows**: a monthly window spans at most 31 daily
rows, and a weekly window can begin up to 6 days before the first of the
month (a week that straddles the month boundary), so the union of the two
windows never exceeds 31 + 6 = 37 days. This is why `usage_retention_days`
must be at least 31.

Because totals are derived from history, enabling a weekly or monthly limit
in the middle of a window includes usage recorded before the limit existed.
The same holds for a per-model budget added mid-window. Increasing retention
does not restore rows that already expired.

## Thresholds and alert-only budgets

Each enabled period carries an ordered `thresholds` list of
`{"at": <ratio>, "action": "warn" | "block"}`:

- `at` is utilization (`1.0` = 100 %), greater than 0 and at most `10.0`.
- Entries must be strictly increasing in `at`.
- At most one `block` entry, and it must be last. It may sit above 100 %
  (`{"at": 1.2, "action": "block"}`).
- A period whose list has **no `block` entry is alert-only**: it warns at
  every configured level and never blocks, however far over 100 % it runs.
  The console flags such periods.

When a period is submitted without `thresholds`, it uses the deployment
default `[{"at": <warn_threshold>, "action": "warn"}, {"at": 1.0, "action": "block"}]`.

The usage processor sends one SNS warning per `warn` level per calendar
window (crossing 50 % then 80 % sends two messages; a duplicate log delivery
sends none) and blocks only when the `block` level is reached.

## Rate limits

A subject may carry `rate: {"rpm": N, "tpm": N}`: requests and uncached
input + output tokens per UTC minute, counted from metered invocations.
`0` disables a dimension. Cached tokens never count toward `tpm`.

Rate limits are evaluated by the usage processor against a short-lived
per-minute counter that exists only for subjects with a rate limit. Reaching
either limit blocks the subject through the same automatic path as a
calendar breach, with reason `auto: rpm rate limit reached in minute ...` and
SNS subject `BLOCKED <subject> reason=rpm|tpm`. The block lifts by itself as
soon as the current minute is under the limit (see
[Blocking and unblocking](#blocking-and-unblocking)).

`vend_rate_limit_per_minute` is a different control: it bounds how often one
identity may call the credential broker and protects the broker, not Bedrock
spend.

## Per-model budgets

Next to its subject-level limits, a subject may carry zero or more budgets
keyed by model or inference-profile ID as it appears in the invocation log
(for example `us.anthropic.claude-opus-4-7`, never an ARN). Each budget has
the same `daily`/`weekly`/`monthly` + `thresholds` shape as the subject
limits: "user X gets $10/day in total but only $2/day of that may go to
model Y". Rate limits are subject-level only.

The usage processor writes a second daily ledger row per (subject, day,
model) in the same transaction as the subject row, for every model a
subject uses, so a budget added mid-window includes usage already recorded.

**Known limitation: a model budget breach blocks the whole subject.** The
enforcement primitives act on the identity (revocation shards deny
`aws:SourceIdentity` values; the workload enforcer attaches a role-wide
deny), not on the model. When any model budget reaches its `block`
threshold, the subject is blocked exactly as for a subject-level breach
(`status_reason: auto: daily USD quota exhausted for model <id> ...`,
SNS `BLOCKED <subject> reason=model:<id>:<period>-<dimension>`) and calls
to other models are refused too, until every enabled period is under quota
again or the budget is raised or removed. Use model budgets as a cost
guardrail, not as a model allowlist; `allowed_model_arns` is the allowlist.

Model budgets are not part of the credential-vend check: the model is
unknown at vend time, so the broker evaluates subject-level limits and rate
limits only. The first metered invocation that crosses a model budget's
block threshold blocks the subject.

Admin routes: `PUT` / `DELETE /admin/user/model-budget` and
`GET /admin/user/model-usage` ([admin-api.md](admin-api.md)). The console
exposes them in the user detail drawer under "Per-model budgets".

## Reasons

Every status change and limit change is written to the admin audit table
with the actor and a reason.

- The API accepts an optional `reason` string on every mutation. When it is
  omitted or blank, the audit event stores `not provided`.
- The console requires a non-empty reason when a period is enabled or
  disabled, when a positive limit becomes Unlimited, when a submitted finite
  limit is below current usage for that period, when a period becomes
  alert-only, and on every block, unblock, lease-dial change, and emergency
  action. Ordinary limit increases have an optional reason.

## Auto-provisioning

With `auto_provision_users: true` (the demo default), the first valid JWT for
an unknown claim value creates the user row with `default_limits`. With
`false` (the production default), unknown users receive `403 quota_blocked`
until an operator creates them through the console or `POST /admin/users`.

Workload rows are always created by metering: the first invocation
attributed to a workload's inference profile creates `workload:<name>` with
`default_limits`. `POST /admin/users` rejects the `workload:` prefix.

## Blocking and unblocking

**Automatic block.** When the usage processor finds a `block` threshold or a
rate limit reached, it flips the row to `blocked` / `status_origin:
automatic` and writes a `REVOCATION#<subject>` sentinel in the same
transaction. From that moment:

1. The broker refuses new credential vends for the subject (`429
   quota_exceeded` or `403 quota_blocked`).
2. The enforcement dispatcher fans the sentinel out to the revocation
   processor, which adds the identity to a deny shard on the vended role;
   in-flight sessions lose Bedrock permission after IAM propagation.
3. For workloads, the workload enforcer attaches an inline deny to the
   workload's role instead.
4. Whatever happens first, every vended credential also carries its
   permission-lease deadline, so access ends at
   `metering lag + min(lease remainder, deny propagation)`.

**Automatic lift.** An automatic block lifts without operator action once
every enabled period, the current rate minute, and every model budget are
under quota again, through whichever of these runs first:

| Path | When | Scope |
|---|---|---|
| Credential vend | the user's next `POST /v1/credentials` | that user |
| Workload enforcer | stream fast path plus a 5-minute schedule | `workload:` rows |
| Auto-block sweep | nightly at 00:05 UTC (`AutoBlockSweeperFn`) | every blocked user row with automatic origin |

The nightly sweep exists for users who never come back: without it their
identities would occupy revocation-shard capacity indefinitely. Its last run
is shown on the Operations tab and in `GET /admin/operations` →
`auto_block_sweep`; runbook:
[runbooks/components/auto-block-sweeper.md](runbooks/components/auto-block-sweeper.md).

**Admin block and unblock.** `PUT /admin/user/status` with `{"status":
"blocked" | "active", "reason": "..."}` sets `status_origin: admin`. An
admin block never lifts automatically. An admin unblock makes the row active
immediately; a later breach blocks it again through the automatic path.

**Limit changes reconcile status immediately.** Lowering a limit or adding a
model budget below current usage blocks the subject at once; raising the
binding limit or removing the binding budget lifts an automatic block at
once.

**Emergency stop** is separate from quotas: it closes vending for everyone
and applies a role-wide deny. See [operations.md](operations.md#emergency-stop).
