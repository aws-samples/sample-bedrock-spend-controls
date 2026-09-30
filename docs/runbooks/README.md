# Runbooks

Operational runbooks for Bedrock Spend Controls. Every CloudWatch alarm the
stack creates has a runbook; every Lambda component has a component
runbook describing its inputs, IAM scope, failure modes, and how to re-run
or reconcile it by hand. All alarms notify the `QuotaAlerts` SNS topic
(`AlertTopicArn` stack output) and are surfaced as state chips on the admin
console Operations tab via `GET /admin/operations`.

Alarm names are CloudFormation-generated (`<StackName>-<LogicalId>-<hash>`);
find the exact name with:

```bash
aws cloudwatch describe-alarms --alarm-name-prefix BedrockSpendControls \
  --query 'MetricAlarms[].{name:AlarmName,state:StateValue,metric:MetricName}' \
  --output table
```

All metrics below live in the `BedrockSpendControls` namespace unless noted.

## Alarm runbooks

| Alarm (logical ID) | Operations key | Metric | Threshold | Runbook |
|---|---|---|---|---|
| `EmergencyStopFailureAlarm` | `emergency_failure` | `EmergencyStopFailure` Sum ≥ 1 / 5 min | 1 period | [emergency-stop-failure.md](alarms/emergency-stop-failure.md) |
| `EmergencyStopDlqAlarm` | `emergency_dlq` | SQS `ApproximateNumberOfMessagesVisible` ≥ 1 / 5 min | 1 period | [emergency-stop-dlq.md](alarms/emergency-stop-dlq.md) |
| `RevocationSyncFailureAlarm` | `revocation_failure` | `RevocationSyncFailure` Sum ≥ 1 / 5 min | 1 period | [revocation-sync-failure.md](alarms/revocation-sync-failure.md) |
| `RevocationPolicyOverflowAlarm` | `revocation_overflow` | `RevocationPolicyOverflow` Sum ≥ 1 / 5 min | 1 period | [revocation-policy-overflow.md](alarms/revocation-policy-overflow.md) |
| `WorkloadEnforcementFailureAlarm` (workload mode only) | `workload_enforcement_failure` | `WorkloadEnforcementFailure` Sum ≥ 1 / 5 min, missing = not breaching | 1 period | [workload-enforcement-failure.md](alarms/workload-enforcement-failure.md) |
| `AutoBlockSweepFailureAlarm` | `auto_block_sweep_failure` | `AutoBlockSweepFailure` Sum ≥ 1 / 1 day, missing = not breaching | 1 period | [auto-block-sweep-failure.md](alarms/auto-block-sweep-failure.md) |
| `EnforcementDispatchDlqAlarm` | `enforcement_dispatch_dlq` | SQS `ApproximateNumberOfMessagesVisible` ≥ 1 / 5 min | 1 period | [enforcement-dispatch-dlq.md](alarms/enforcement-dispatch-dlq.md) |
| `EnforcementDispatchIteratorAgeAlarm` | `enforcement_dispatch_iterator_age` | Lambda `IteratorAge` Maximum ≥ 300 000 ms / 5 min | 1 period | [enforcement-dispatch-iterator-age.md](alarms/enforcement-dispatch-iterator-age.md) |
| `PricingFallbackAlarm` | `pricing_fallback` | `FallbackPricedRequests` Sum ≥ 1 / 5 min, missing = not breaching | 1 period | [pricing-fallback.md](alarms/pricing-fallback.md) |
| `SpendReconciliationDeltaAlarm` (`reconciliation_enabled` only) | `reconciliation_delta` | `ReconciliationDeltaPercent` Maximum > `reconciliation_alarm_percent` / 1 day | 2 periods | [reconciliation-delta.md](alarms/reconciliation-delta.md) |

Not alarmed but worth knowing: every Lambda's own `Errors` metric (a failing
price refresh, for example, surfaces there and nowhere else), the
`UnpricedDimensionRequests` metric (a narrower sibling of
`FallbackPricedRequests` that is included in the pricing-fallback alarm), and
`WorkloadEnforcementSkipped` (blocked workloads without a `role_arn`, sent
as an SNS message by the enforcer rather than a CloudWatch alarm).

## Component runbooks

| Component | Lambda logical ID | Runbook |
|---|---|---|
| Broker / admin API | `BrokerApiFn` | [gateway.md](components/gateway.md) |
| Usage processor (metering) | `UsageProcessorFn` | [usage-processor.md](components/usage-processor.md) |
| Enforcement dispatcher | `EnforcementDispatcherFn` | [enforcement-dispatcher.md](components/enforcement-dispatcher.md) |
| Revocation processor | `RevocationProcessorFn` | [revocation-processor.md](components/revocation-processor.md) |
| Emergency processor | `EmergencyStopProcessorFn` | [emergency-processor.md](components/emergency-processor.md) |
| Workload enforcer | `WorkloadEnforcerFn` | [workload-enforcer.md](components/workload-enforcer.md) |
| Auto-block sweeper | `AutoBlockSweeperFn` | [auto-block-sweeper.md](components/auto-block-sweeper.md) |
| Pricing resolver / refresher | `PriceResolverFn`, `PriceRefreshFn` | [pricing-resolver.md](components/pricing-resolver.md) |
| Reconciliation processor | `SpendReconciliationFn` (`reconciliation_enabled` only) | [reconciliation-processor.md](components/reconciliation-processor.md) |

## Conventions used in the runbooks

- **Severity.** *Page* means wake someone: spend may be under-enforced right
  now, or vending is failing for everyone. *Next business day* means the
  system is degraded but the permission lease still bounds every session
  (`metering lag + min(lease remainder, deny propagation)`), so exposure is
  known and bounded.
- **Log group names** are `/aws/lambda/<function name>`. Find function names
  with `aws cloudformation describe-stack-resources --stack-name
  BedrockSpendControls --query "StackResources[?ResourceType=='AWS::Lambda::Function'].{id:LogicalResourceId,name:PhysicalResourceId}"`.
- **Logs Insights queries** below assume the Lambda writes one JSON object per
  line (every component does). Replace `<fn>` with the function name.
- **Emergency stop** is an operator break-glass action, never a remediation
  step a runbook takes for you. Where a runbook says "consider the emergency
  stop", it means: if you cannot restore enforcement quickly and the
  exposure is unacceptable, follow the procedure in
  [operations.md § Emergency stop](../operations.md#emergency-stop).
