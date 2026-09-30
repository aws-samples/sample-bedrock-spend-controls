# SpendReconciliationDeltaAlarm

**Deployed only when `reconciliation_enabled: true`.** **Operations key:**
`reconciliation_delta` · **Metric:** `ReconciliationDeltaPercent` (Maximum >
`reconciliation_alarm_percent`, default 10, over 1 day; **2 consecutive
periods**; missing data = not breaching) · **Emitted by:**
`reconciliation_processor/handler.py` — the **absolute** percent difference
between the ledger's estimated USD and Cost Explorer's Bedrock spend for the
reconciled day, aggregate scope (no `Workload` dimension).

## What it means

For two days in a row the DynamoDB ledger and the bill disagreed by more than
the configured percent for the same UTC day. The sign is in the stored run
(`GET /admin/reconciliation` → `aggregate.delta_percent`, positive = bill
higher than ledger) and in the EMF property `SignedDeltaPercent`; the metric
itself is unsigned so one alarm catches both directions. The percent is
relative to the larger of the two figures and therefore bounded to ±100 %;
`±100` means one side saw nothing at all.

The two directions mean different things:

| Direction | Ledger vs bill | What it usually is |
|---|---|---|
| **Bill > ledger** (positive) | Spend the ledger never saw | Bedrock calls from principals this stack does not meter — anything not going through the vended role or a workload profile (console Playground, other roles, `bedrock-mantle` bearer-token paths, direct calls in the same account). Or invocation logging dropped records. |
| **Bill < ledger** (negative) | Ledger counted spend the bill did not | Catalog prices above the real rates (fallback-priced models, a pinned override that is too high, a cross-Region profile priced at the base model's rate when the bill uses a cheaper one), credits/private pricing on the account, or CE data still settling. |

A **positive delta is expected** in any account where other principals call
Bedrock: the ledger holds only metered subjects while CE returns the whole
account for the Region. Set `reconciliation_alarm_percent` above that
structural floor once you know it, or route the other principals through the
gateway/workload profiles so they are metered too.

## Severity guidance

**Next business day.** Nothing here blocks or unblocks anyone; enforcement is
unaffected. Escalate to same day only if the delta is *negative* and large
(the USD quota is not protecting the bill because it under-counts), or if it
coincides with a `pricing_fallback` alarm.

## Likely causes

1. **Unmetered principals** (positive). Confirm with Cost Explorer grouped by
   linked account / usage type, or with CloudTrail `InvokeModel*` events whose
   `userIdentity` is not the vended role or a workload role.
2. **Catalog gap** (either direction). Check
   [pricing-fallback](pricing-fallback.md) and `tools/unpriced_usage.py`.
3. **Cross-Region inference profile** priced at base-model rates when the
   account's bill uses a different Region's rate. Pin the profile ID in
   `price_overrides`.
4. **Cost Explorer lag.** If the newest run's `billed_usd` is much lower than
   the ledger and the day is D-1 or D-2, CE may still be settling. Raise
   `reconcile_lag_days` (max 14) rather than the alarm threshold.
5. **Credits / private pricing.** CE `UnblendedCost` reflects them; the
   ledger prices at list. The delta is then persistent and structural —
   tune the threshold or pin the negotiated rates in `price_overrides`.
   Example: an account whose Bedrock usage types are billed at a zero rate
   shows `delta_percent ≈ -90 %` while the ledger prices the same calls at
   list — correct behaviour, not a bug. An account billed entirely in
   credits sits at `-100 %` every day; raise `reconciliation_alarm_percent`
   or accept the alarm as the signal that the ledger, not the bill, is the
   number to manage against.
6. **Invocation logging dropped records** (positive). Check the delivery log
   group / S3 bucket and the `usage_processor` `Errors` metric.
7. **Wrong service names.** The Lambda filters CE on `Amazon Bedrock` and
   `Amazon Bedrock Service`. If your bill shows
   Bedrock spend under another `SERVICE` value, set `CE_SERVICE_NAMES_JSON`
   on the Lambda and open an issue.

Inspect the runs:
```bash
# The Function URL is AWS_IAM: sign the request and pass the routine admin key.
awscurl --service lambda --region "$AWS_REGION" -H "X-Quota-Admin-Key: $ADMIN_KEY" \
  "$BROKER_API_URL/admin/reconciliation?limit=7" | jq '.runs[] | {day, aggregate, tag_inactive_workloads}'
# What CE says Bedrock cost that day, by usage type:
aws ce get-cost-and-usage --time-period Start=2026-09-12,End=2026-09-13 --granularity DAILY \
  --metrics UnblendedCost --group-by Type=DIMENSION,Key=USAGE_TYPE \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon Bedrock","Amazon Bedrock Service"]}}'
```

## Remediation

1. Classify the direction and the cause using the table above.
2. Fix catalog gaps (`cdk/config/model-pricing.json`, redeploy) or route the
   unmetered callers through the gateway / a workload profile.
3. If the remaining delta is structural (credits, other principals you accept
   as unmetered), raise `reconciliation_alarm_percent` to sit above it, or
   accept the alarm as informational and document why.
4. Historical ledger rows are never repriced; the reconciliation is a
   trend signal, not a correction mechanism.

## How to verify recovery

- The next two stored runs have `|delta_percent|` under the threshold →
  the alarm returns to `OK` after two daily periods.
- `ReconciliationRuns` = 1 per day (the schedule is firing) and no
  `ReconciliationFailure` metric.

## Related

- [configuration.md § Reconciliation](../../configuration.md#reconciliation) and [operations.md § Reading the reconciliation card](../../operations.md#reading-the-reconciliation-card)
- Component: [components/reconciliation-processor.md](../components/reconciliation-processor.md)
- Alarm: [pricing-fallback.md](pricing-fallback.md)
- Metrics: `ReconciliationEstimatedUSD`, `ReconciliationBilledUSD`, `ReconciliationDeltaUSD`, `ReconciliationDeltaPercent`, `ReconciliationTagInactive`, `ReconciliationFailure`
