# Component: spend reconciliation processor (`SpendReconciliationFn`)

**Deployed only when `reconciliation_enabled: true`.** **Code:**
`reconciliation_processor/handler.py`. **Log group:**
`/aws/lambda/<SpendReconciliationFn>`. **Invocation:**
`SpendReconciliationSchedule` — `cron(0 6 * * ? *)` (06:00 UTC daily, after
Cost Explorer's refresh). 256 MB, 2 min timeout.

## What it does

Once a day, compares what the ledger *thinks* Bedrock cost with what Cost
Explorer *says* it cost, for one settled UTC day `D-<reconcile_lag_days>`
(default D-2):

1. **Aggregate.** Scans the usage table for every subject-level daily row
   with `window = <day>` (users and `workload:*`; `<subject>#model#<id>` rows
   are skipped so per-model ledgers do not double count; `REQUEST#`,
   `RATE#`, `RECONCILE#` rows are not ledgers), sums `cost_micro`, and
   calls `ce:GetCostAndUsage` for that day filtered to `SERVICE ∈ {Amazon
   Bedrock, Amazon Bedrock Service}` and `REGION = <stack region>`.
2. **Per workload.** For each configured workload, compares its
   `workload:<name>` row with CE filtered additionally on the cost-allocation
   tag `bedrock-spend-controls-workload = <name>` (the tag the stack stamps
   on each application inference profile). If the ledger shows spend and CE
   returns `0` for the tag, the tag is almost certainly **not activated** in
   Billing → Cost allocation tags; the run records `tag_inactive: true`,
   emits `ReconciliationTagInactive = 1`, and sends one SNS message.
3. **Stores** the result as `RECONCILE#<day>` / `window=<day>` in the
   **usage table** with `expires_at` = now + `usage_retention_days`. The
   usage table is used because the row shares its retention, its key-prefix
   convention (`REQUEST#`, `RATE#`), and its existing broker read grant; the
   admin-audit table is reserved for operator actions.
4. **Emits** EMF metrics and returns the result document.

Cost Explorer is billed at $0.01 per `GetCostAndUsage` request: one run makes
`1 + number_of_workloads` requests, so ≈ $0.30 + $0.30 × workloads per month.
The broker's `GET /admin/reconciliation` reads the stored rows only and never
calls CE.

**What it cannot do.** JWT-vended users share one IAM role and therefore one
line in the bill; there is no per-user CE dimension. Aggregate is the finest
grain for users. Workloads are reconcilable only because each has its own
tagged inference profile.

## IAM scope

- Usage table read/write (scan for the day, `PutItem` of the result row).
- `ce:GetCostAndUsage` on `*` — Cost Explorer has no resource-level
  permissions; this is the only `ce:` verb granted.
- `sns:Publish` on `QuotaAlerts`.
- Nothing on the users or admin-audit tables.

## Inputs / outputs

| Input | Output |
|---|---|
| `USAGE_TABLE`, `RECONCILE_REGION`, `RECONCILE_LAG_DAYS`, `USAGE_RETENTION_DAYS`, `WORKLOADS_JSON`, `WORKLOAD_TAG_KEY`, `SNS_TOPIC_ARN`, optional `CE_SERVICE_NAMES_JSON` | `RECONCILE#<day>` row: `{run_at, result{day, region, service_names, aggregate{estimated_usd, billed_usd, delta_usd, delta_percent}, workloads[], tag_inactive_workloads[]}, expires_at}` |
| Event `{"day": "YYYY-MM-DD"}` (optional; overrides D-lag for a manual re-run) | EMF (aggregate, no dimension): `ReconciliationEstimatedUSD`, `ReconciliationBilledUSD`, `ReconciliationDeltaUSD`, `ReconciliationDeltaPercent` (absolute; signed value in the `SignedDeltaPercent` property), `ReconciliationRuns` |
| | EMF (`Workload` dimension): the same four plus `ReconciliationTagInactive` |
| | EMF on CE error: `ReconciliationFailure` |
| | SNS `RECONCILIATION FAILED`, `RECONCILIATION: cost-allocation tag not active` |

`delta_percent` is the difference relative to `max(estimated, billed)`, so
it is bounded to ±100 %: a metered day the bill priced at `$0` is `-100`
(credits, inactive tag, or over-pricing) and **does** trip the alarm, which
is the intended reading — the two sources disagree completely. It is `null`
only when both sides are `$0` (nothing happened); the metric then emits `0`
so an empty day never alarms.

## Failure modes

| Symptom | Alarm | Notes |
|---|---|---|
| `\|delta_percent\|` over threshold two days running | [reconciliation-delta](../alarms/reconciliation-delta.md) | Direction tells you whether spend is unmetered (positive) or over-priced (negative). |
| `ReconciliationFailure` = 1 | none (SNS message) | CE `DataUnavailableException` (lag too short), throttling, or missing `ce:GetCostAndUsage` when Cost Explorer is not yet enabled on the account. Enable CE in the console once; the first API call can take up to 24 h to succeed. |
| `ReconciliationTagInactive` = 1 | none (SNS message) | Activate the cost-allocation tag **in the payer account**; CE starts attributing ~24 h after activation and does not backfill. |
| No `ReconciliationRuns` for a day | none | Schedule disabled or Lambda erroring before the CE call; check the log group. |

## Manual operations

- **Re-run for a specific day:**
  `aws lambda invoke --function-name <SpendReconciliationFn> --payload '{"day":"2026-09-12"}' --cli-binary-format raw-in-base64-out /dev/stdout`.
  The result row for that day is overwritten.
- **Read the stored runs:** `GET /admin/reconciliation?limit=14` (SigV4-signed,
  with the `X-Quota-Admin-Key` header; the Operations tab shows the same data) or `aws dynamodb query --table-name <UsageTable> --key-condition-expression 'user_id = :k' --expression-attribute-values '{":k":{"S":"RECONCILE#2026-09-12"}}'`.
- **Activate the cost-allocation tag:** Billing and Cost Management →
  *Cost allocation tags* → select `bedrock-spend-controls-workload` →
  *Activate*; or
  `aws ce update-cost-allocation-tags-status --cost-allocation-tags-status TagKey=bedrock-spend-controls-workload,Status=Active`.
  Must run in the **management (payer) account**; from a linked account
  `list-cost-allocation-tags` / `update-cost-allocation-tags-status` fail
  with `AccessDeniedException: Linked account doesn't have access to cost
  allocation tags`. Until the payer activates it every
  workload with spend reports `tag_inactive: true`.
- **Check what CE calls Bedrock in this account:**
  `aws ce get-dimension-values --dimension SERVICE --search-string Bedrock --time-period Start=<D-30>,End=<today>`.
  If a value other than the two defaults appears, set
  `CE_SERVICE_NAMES_JSON` on the function.

## Related

- [configuration.md § Reconciliation](../../configuration.md#reconciliation): setup and payer-account tag activation
- [alarms/reconciliation-delta.md](../alarms/reconciliation-delta.md)
- [components/usage-processor.md](usage-processor.md) (writes the ledger rows this component reads)
