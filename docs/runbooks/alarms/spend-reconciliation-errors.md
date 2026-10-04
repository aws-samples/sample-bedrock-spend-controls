# SpendReconciliationErrorsAlarm

**Operations key:** `reconciliation_errors` · **Alarm name:** `<stack>-reconciliation-errors` · **Deployed only when `reconciliation_enabled: true`.** · **Metric:** Lambda `Errors` on `SpendReconciliationFn` (Sum ≥ 1 over 5 min, 1 period; missing data = not breaching) · **Source:** the function's own error count. `reconciliation_processor/handler.py` wraps the whole run, emits `ReconciliationFailure`, publishes `RECONCILIATION FAILED`, and **re-raises**, so this alarm fires for every failed run; a timeout is the one failure it catches that the functional metric cannot (see [reconciliation-timeout](reconciliation-timeout.md)).

## What it means

Today's reconciliation run (06:00 UTC) did not store a result. Nothing is
blocked, unblocked, or repriced by this component, so **enforcement is
unaffected**; what you lose is the day's drift signal between the ledger and
the bill. The `RECONCILE#<day>` row for that day is missing until a manual
re-run.

## Severity guidance

**Next business day.** The run is idempotent and can be repeated for any day
inside `usage_retention_days`.

## Likely causes

1. **Cost Explorer not enabled** on the payer account, or enabled less than
   24 hours ago: `AccessDeniedException` or `DataUnavailableException` on
   `ce:GetCostAndUsage`. Open Billing and Cost Management → Cost Explorer
   once; the API starts answering within a day.
2. **`ce:GetCostAndUsage` denied** after a manual policy edit on the function
   role or an SCP that blocks `ce:*` in member accounts.
3. **Cost Explorer throttling** (the run makes `1 + workloads` requests).
4. **Usage-table scan failure** or `dynamodb:PutItem` denied for the
   `RECONCILE#` row.
5. **Configuration**: `reconciliation_service_names` empty after a manual
   environment edit (`RECONCILE_SERVICE_NAMES_JSON must list at least one
   service`), or `RECONCILE_REGION` missing.
6. **Timeout** (2 min): see [reconciliation-timeout](reconciliation-timeout.md).

The SNS message `RECONCILIATION FAILED` and the log carry `error` and
`error_type`:
```bash
aws logs filter-log-events --log-group-name /aws/lambda/<SpendReconciliationFn> \
  --filter-pattern '{ $.ReconciliationFailure = 1 }' \
  --start-time $(( $(date +%s) - 86400 ))000 --query 'events[-1].message' --output text \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["day"], r["error_type"], r["error"])'
```

## Remediation

1. Fix the cause (enable Cost Explorer, restore the permission, wait out
   throttling, redeploy to restore the environment).
2. Re-run for the missed day; the result row is written or overwritten:
   ```bash
   aws lambda invoke --function-name <SpendReconciliationFn> \
     --payload '{"day":"2026-09-12"}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```
3. Check the stored run on the Operations tab or with
   `GET /admin/reconciliation?limit=7`.

## How to verify recovery

- `Errors` Sum = 0 for one 5-minute period → alarm `OK`.
- `ReconciliationRuns` = 1 for the re-run (and each following day), paired
  with one `ReconciliationStarted`; no `ReconciliationFailure`.
- `GET /admin/reconciliation` → `latest.day` is the expected day.

## Related

- [reconciliation-delta.md](reconciliation-delta.md)
- [reconciliation-timeout.md](reconciliation-timeout.md)
- [configuration.md § Reconciliation](../../configuration.md#reconciliation) — enabling Cost Explorer and the cost-allocation tag
- Component: [components/reconciliation-processor.md](../components/reconciliation-processor.md)
- Metrics: Lambda `Errors` (`AWS/Lambda`); `ReconciliationStarted`, `ReconciliationRuns`, `ReconciliationFailure` (no dimensions)
