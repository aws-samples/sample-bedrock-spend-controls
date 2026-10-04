# ReconciliationTimeoutAlarm

**Operations key:** `reconciliation_timeout` · **Alarm name:** `<stack>-reconciliation-timeout` · **Deployed only when `reconciliation_enabled: true`.** · **Metric:** math expression `FILL(started, 0) - FILL(runs, 0) - FILL(failures, 0)` over the daily Sums of `ReconciliationStarted`, `ReconciliationRuns`, and `ReconciliationFailure` (> 0 over 1 day, 1 period; missing data = not breaching) · **Emitted by:** `reconciliation_processor/handler.py` emits `ReconciliationStarted` as the very first statement of a run, before any work.

## What it means

A reconciliation run started but, within the same UTC day, neither finished
(`ReconciliationRuns`) nor recorded a failure (`ReconciliationFailure`). The
handler wraps everything in a `try` that emits the failure metric, so the
only way to reach this state is a termination the code cannot catch: a
**Lambda timeout** (2 minutes), an out-of-memory kill, or a runtime crash
mid-run. No `RECONCILE#<day>` row was written and no SNS message was sent;
without this alarm the day would simply be missing.

Enforcement is unaffected; the day's drift signal is missing.

## Severity guidance

**Next business day.** A single occurrence after a usage spike is worth a
look; a recurring one means the run no longer fits its timeout.

## Likely causes

1. **Usage-table scan too large.** The aggregate step scans every daily
   ledger row for the reconciled day (users and workloads; per-model rows are
   skipped but still read). Tens of thousands of active subjects per day can
   push the scan past two minutes.
2. **Cost Explorer slow or retrying.** `1 + workloads` requests, each of
   which boto3 retries on throttling; many workloads on a throttled account
   add up.
3. **Out of memory** (256 MB) when the scan result is held before summing.

Confirm with the Lambda metrics and log:
```bash
aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Duration \
  --dimensions Name=FunctionName,Value=<SpendReconciliationFn> \
  --start-time "$(date -u -v-2d +%FT%TZ 2>/dev/null || date -u -d '2 days ago' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
  --period 86400 --statistics Maximum
aws logs filter-log-events --log-group-name /aws/lambda/<SpendReconciliationFn> \
  --filter-pattern '"Task timed out"' \
  --start-time $(( $(date +%s) - 172800 ))000 --query 'events[].message' --output text
```

## Remediation

1. Re-run for the missed day by hand once; a second timeout confirms the
   capacity problem rather than a one-off:
   ```bash
   aws lambda invoke --function-name <SpendReconciliationFn> \
     --payload '{"day":"2026-09-12"}' --cli-binary-format raw-in-base64-out /dev/stdout
   ```
2. If it recurs, raise the function's `timeout` (and `memory_size` if the
   log shows memory pressure) for `SpendReconciliationFn` in
   `cdk/stacks/spend_controls_stack.py` and redeploy. Cost Explorer calls
   are billed per request, not per second, so a longer timeout costs
   nothing extra.
3. If Cost Explorer throttling is the cause, reduce the per-run request
   count by trimming the workload roster, or accept aggregate-only
   reconciliation.

## How to verify recovery

- The next daily run emits `ReconciliationStarted` **and** `ReconciliationRuns`
  (or a `ReconciliationFailure` with a diagnosable error); the expression
  evaluates to 0 → alarm `OK` after one daily period.
- `GET /admin/reconciliation` → `latest.day` advances every day.

## Related

- [spend-reconciliation-errors.md](spend-reconciliation-errors.md) — the Lambda `Errors` alarm, which also fires on a timeout
- [reconciliation-delta.md](reconciliation-delta.md)
- Component: [components/reconciliation-processor.md](../components/reconciliation-processor.md)
- Metrics: `ReconciliationStarted`, `ReconciliationRuns`, `ReconciliationFailure` (no dimensions); Lambda `Duration`, `Errors` (`AWS/Lambda`)
