# Runbooks

Operational runbooks for Bedrock Spend Controls. Every CloudWatch alarm the
stack creates has a runbook; every Lambda component has a component
runbook describing its inputs, IAM scope, failure modes, and how to re-run
or reconcile it by hand. All alarms notify the `QuotaAlerts` SNS topic
(`AlertTopicArn` stack output) and are surfaced as state chips on the admin
console Operations tab via `GET /admin/operations`.

Alarm names are fixed: `<StackName>-<operations key with hyphens>`, for
example `BedrockSpendControls-revocation-failure`. The headings below use
the alarm's CDK **construct ID** (`RevocationSyncFailureAlarm`); the
CloudFormation logical ID is that construct ID plus a hash suffix. List the
deployed alarms with:

```bash
aws cloudwatch describe-alarms --alarm-name-prefix BedrockSpendControls- \
  --query 'MetricAlarms[].{name:AlarmName,state:StateValue,metric:MetricName}' \
  --output table
```

Lambda functions, queues, and policies also have hashed logical IDs. Resolve
a construct ID to its physical name with a prefix match (the `<Fn>`
placeholders in the runbooks are the physical function names this returns):

```bash
aws cloudformation describe-stack-resources --stack-name BedrockSpendControls \
  --query "StackResources[?starts_with(LogicalResourceId,'RevocationProcessorFn')].{id:LogicalResourceId,name:PhysicalResourceId}"
```

All metrics below live in the `BedrockSpendControls` namespace unless noted.
Every deployment has 15 alarms; `workloads` adds 2 and
`reconciliation_enabled` adds 3, for 20 in all.

## Alarm runbooks

| Alarm (construct ID) | Operations key | Metric | Threshold | Runbook |
|---|---|---|---|---|
| `BrokerApiErrorsAlarm` | `broker_api_errors` | Lambda `Errors` Sum ≥ 1 / 5 min, missing = not breaching | 1 period | [broker-api-errors.md](alarms/broker-api-errors.md) |
| `UsageProcessorErrorsAlarm` | `usage_processor_errors` | Lambda `Errors` Sum ≥ 1 / 5 min, missing = not breaching | 1 period | [usage-processor-errors.md](alarms/usage-processor-errors.md) |
| `UsageProcessorDlqAlarm` | `usage_processor_dlq` | SQS `ApproximateNumberOfMessagesVisible` on `UsageProcessorDeadLetterQueue` ≥ 1 / 5 min, missing = not breaching | 1 period | [usage-processor-dlq.md](alarms/usage-processor-dlq.md) |
| `EmergencyStopFailureAlarm` | `emergency_failure` | `EmergencyStopFailure` Sum ≥ 1 / 5 min | 1 period | [emergency-stop-failure.md](alarms/emergency-stop-failure.md) |
| `EmergencyStopDlqAlarm` | `emergency_dlq` | SQS `ApproximateNumberOfMessagesVisible` ≥ 1 / 5 min | 1 period | [emergency-stop-dlq.md](alarms/emergency-stop-dlq.md) |
| `EmergencyStopProcessorErrorsAlarm` | `emergency_processor_errors` | Lambda `Errors` Sum ≥ 1 / 5 min, missing = not breaching | 1 period | [emergency-stop-processor-errors.md](alarms/emergency-stop-processor-errors.md) |
| `RevocationSyncFailureAlarm` | `revocation_failure` | `RevocationSyncFailure` Sum ≥ 1 / 5 min | 1 period | [revocation-sync-failure.md](alarms/revocation-sync-failure.md) |
| `RevocationPolicyOverflowAlarm` | `revocation_overflow` | `RevocationPolicyOverflow` Sum ≥ 1 / 5 min | 1 period | [revocation-policy-overflow.md](alarms/revocation-policy-overflow.md) |
| `RevocationProcessorErrorsAlarm` | `revocation_processor_errors` | Lambda `Errors` Sum ≥ 1 / 5 min, missing = not breaching | 1 period | [revocation-processor-errors.md](alarms/revocation-processor-errors.md) |
| `AutoBlockSweepFailureAlarm` | `auto_block_sweep_failure` | `AutoBlockSweepFailure` Sum ≥ 1 / 1 day, missing = not breaching | 1 period | [auto-block-sweep-failure.md](alarms/auto-block-sweep-failure.md) |
| `AutoBlockSweeperErrorsAlarm` | `auto_block_sweeper_errors` | Lambda `Errors` Sum ≥ 1 / 5 min, missing = not breaching | 1 period | [auto-block-sweeper-errors.md](alarms/auto-block-sweeper-errors.md) |
| `EnforcementDispatchDlqAlarm` | `enforcement_dispatch_dlq` | SQS `ApproximateNumberOfMessagesVisible` ≥ 1 / 5 min | 1 period | [enforcement-dispatch-dlq.md](alarms/enforcement-dispatch-dlq.md) |
| `EnforcementDispatchIteratorAgeAlarm` | `enforcement_dispatch_iterator_age` | Lambda `IteratorAge` Maximum ≥ 300 000 ms / 5 min | 1 period | [enforcement-dispatch-iterator-age.md](alarms/enforcement-dispatch-iterator-age.md) |
| `EnforcementDispatcherErrorsAlarm` | `enforcement_dispatcher_errors` | Lambda `Errors` Sum ≥ 1 / 5 min, missing = not breaching | 1 period | [enforcement-dispatcher-errors.md](alarms/enforcement-dispatcher-errors.md) |
| `PricingFallbackAlarm` | `pricing_fallback` | `FallbackPricedRequests` Sum ≥ 1 / 5 min, missing = not breaching | 1 period | [pricing-fallback.md](alarms/pricing-fallback.md) |
| `WorkloadEnforcementFailureAlarm` (workload mode only) | `workload_enforcement_failure` | `WorkloadEnforcementFailure` Sum ≥ 1 / 5 min, missing = not breaching | 1 period | [workload-enforcement-failure.md](alarms/workload-enforcement-failure.md) |
| `WorkloadEnforcerErrorsAlarm` (workload mode only) | `workload_enforcer_errors` | Lambda `Errors` Sum ≥ 1 / 5 min, missing = not breaching | 1 period | [workload-enforcer-errors.md](alarms/workload-enforcer-errors.md) |
| `SpendReconciliationDeltaAlarm` (`reconciliation_enabled` only) | `reconciliation_delta` | `ReconciliationDeltaPercent` Maximum > `reconciliation_alarm_percent` / 1 day | 2 periods | [reconciliation-delta.md](alarms/reconciliation-delta.md) |
| `SpendReconciliationErrorsAlarm` (`reconciliation_enabled` only) | `reconciliation_errors` | Lambda `Errors` Sum ≥ 1 / 5 min, missing = not breaching | 1 period | [spend-reconciliation-errors.md](alarms/spend-reconciliation-errors.md) |
| `ReconciliationTimeoutAlarm` (`reconciliation_enabled` only) | `reconciliation_timeout` | `ReconciliationStarted − ReconciliationRuns − ReconciliationFailure` (Sums, `FILL(…, 0)`) > 0 / 1 day, missing = not breaching | 1 period | [reconciliation-timeout.md](alarms/reconciliation-timeout.md) |

Not alarmed but worth knowing: the `PriceRefreshFn` and `PriceResolverFn`
`Errors` metrics (a failing daily price refresh surfaces there and nowhere
else), the `UnpricedDimensionRequests` metric (a narrower sibling of
`FallbackPricedRequests`, which already drives the pricing-fallback alarm),
`RevokedIdentitiesDropped` (identities an overflowing deny shard could not
hold; the overflow alarm covers the condition), and
`WorkloadEnforcementSkipped` (blocked workloads without a `role_arn`, sent
as an SNS message by the enforcer rather than a CloudWatch alarm).

## Component runbooks

| Component | Lambda construct ID | Runbook |
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
- **Log group names** are `/aws/lambda/<function name>`; the stack creates
  them explicitly with `log_retention_days` (default 90). Find function
  names with the `describe-stack-resources` prefix query above, or list them
  all with `aws cloudformation describe-stack-resources --stack-name
  BedrockSpendControls --query "StackResources[?ResourceType=='AWS::Lambda::Function'].{id:LogicalResourceId,name:PhysicalResourceId}"`.
- **Logs Insights queries** below assume the Lambda writes one JSON object per
  line (every component does). Replace `<fn>` with the function name.
- **`Errors` alarms.** Every Lambda has a fixed-name alarm on its own
  `Errors` metric (Sum ≥ 1 over 5 minutes). Each component's error runbook
  says what an unhandled exception means for that component and points at
  the functional alarm that usually fires alongside it.
- **Emergency stop** is an operator break-glass action, never a remediation
  step a runbook takes for you. Where a runbook says "consider the emergency
  stop", it means: if you cannot restore enforcement quickly and the
  exposure is unacceptable, follow the procedure in
  [operations.md § Emergency stop](../operations.md#emergency-stop).
