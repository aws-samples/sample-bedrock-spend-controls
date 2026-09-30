# Component: enforcement dispatcher (`EnforcementDispatcherFn`)

**Code:** `enforcement_dispatcher/handler.py`. **Log group:**
`/aws/lambda/<EnforcementDispatcherFn>`. **Invocation:** users-table
DynamoDB stream (`LATEST`, `batch_size=100`, 5 s batching window, bisect on
error, 10 retries, DLQ `EnforcementDispatchDeadLetterQueue`), filtered to
keys with prefix `REVOCATION#` or `workload:`. 128 MB, 30 s timeout,
reserved concurrency 1.

## What it does

Exists because DynamoDB Streams supports two simultaneous consumers per
shard and the emergency processor already holds one. It is the single
enforcement consumer: for each batch it notes whether any `REVOCATION#`
sentinel or `workload:` row changed and asynchronously invokes
(`InvocationType="Event"`, payload `{"source": "enforcement-dispatch"}`)
the revocation processor and/or the workload enforcer. It carries no state
and no per-record content — downstream processors re-scan DynamoDB.

## IAM scope

- `lambda:InvokeFunction` on `RevocationProcessorFn` and (workload mode) `WorkloadEnforcerFn`.
- Stream read permissions granted by the event-source mapping.
- Nothing else.

## Inputs / outputs

| Input | Output |
|---|---|
| Stream batch (`Records[].dynamodb.Keys.user_id.S`) | Up to two `Invoke` calls; return `{records, dispatched: ["revocation"?, "workload"?]}` |

## Failure modes

| Symptom | Alarm | Notes |
|---|---|---|
| Batch exhausted retries | [enforcement-dispatch-dlq](../alarms/enforcement-dispatch-dlq.md) | Invoke denied or downstream unavailable. Enforcement converges via schedules. |
| Falling behind | [enforcement-dispatch-iterator-age](../alarms/enforcement-dispatch-iterator-age.md) | Slow `Invoke`, disabled mapping, or write burst. |

There is no failure mode in which a lost dispatch causes *wrong* enforcement;
only later enforcement. Both downstream schedules (`revocation_reconcile_minutes`,
5 min; workload enforcer 5 min) are the correctness path.

## Manual operations

- **Force the fan-out by hand** (equivalent to one dispatched batch):
  ```bash
  aws lambda invoke --function-name <RevocationProcessorFn> --payload '{"source":"enforcement-dispatch"}' --cli-binary-format raw-in-base64-out /dev/stdout
  aws lambda invoke --function-name <WorkloadEnforcerFn>    --payload '{"source":"enforcement-dispatch"}' --cli-binary-format raw-in-base64-out /dev/stdout
  ```
- **Pause/resume the stream consumer:**
  `aws lambda update-event-source-mapping --uuid <uuid> --no-enabled` /
  `--enabled`. While paused, `IteratorAge` grows; on resume the function
  drains the backlog.
- **Check the two-consumer constraint** before adding any other stream
  consumer: `aws lambda list-event-source-mappings --event-source-arn <users table stream ARN>` must list exactly this function and the emergency processor.
