---
title: "Circuit Breaker"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Circuit Breaker

Protects the IDP pipeline from cascading failures when Amazon Bedrock is degraded or unavailable. When Bedrock starts returning errors at a configurable rate, the circuit breaker **opens** and new workflows stop starting. Messages stay in SQS instead of fanning out into Lambda retries that would eventually time out or burn through the Step Functions retry budget. Once Bedrock recovers, the breaker transitions through a **half-open** probe state back to **closed** and normal processing resumes.

https://github.com/user-attachments/assets/86aefe92-d86d-4e38-862d-5e8047a63c80

## Why it exists

Without the circuit breaker, a Bedrock outage produces this chain:

1. Workflows start normally.
2. Every Bedrock call hits the in-client retry loop (up to 7 attempts, exponential backoff up to 5 minutes).
3. Step Functions retries another 8 times on failure.
4. Individual executions hang for up to 15 minutes before failing.
5. Meanwhile new documents keep getting pulled from SQS and starting more doomed workflows, which waste Lambda concurrency and inflate cost.

The circuit breaker short-circuits that cycle by refusing to start new workflows while Bedrock is unhealthy, so messages stay in the queue and process cleanly after recovery.

## States

| State | Behavior |
|-------|----------|
| **CLOSED** | Normal operation. All requests processed. |
| **OPEN** | Bedrock unavailable. Queue Processor returns messages to SQS for retry. |
| **HALF_OPEN** | Testing recovery. Limited traffic allowed through. First successful workflow closes the breaker; first new alarm reopens it. |

### When the state cannot be read

The Queue Processor reads the state from DynamoDB once per message. If that read
fails, what it does next depends on whether a retry could fix the failure, because
the two halves have opposite best answers:

| Read failure | Admission | Why |
|---|---|---|
| **Transient** — throttling, read timeout, dropped connection, DynamoDB 5xx | **Blocked**, as if the breaker were OPEN | Refusing costs almost nothing: the message returns to `DocumentQueue` and has `maxReceiveCount` 500 against a 60 s visibility timeout, roughly 8 hours of retries, before anything reaches the dead-letter queue. Admitting would mean a DynamoDB blip coinciding with a Bedrock outage switches the breaker off. |
| **Terminal** — access denied, table missing, malformed request | **Allowed**, with an `ERROR` log and a metric | A retry cannot fix it, so blocking would block every message on every poll until someone intervened, draining the entire backlog into the dead-letter queue while the pipeline is otherwise healthy. The breaker bounds spend and throttling during a Bedrock outage, so losing that protection is the smaller harm. |

An error the classifier does not recognise is treated as terminal. Either outcome
emits `CircuitBreakerCheckFailed` (see [Observability](#observability)); alarm on
the `TERMINAL` dimension if you rely on the breaker, because that is the state in
which it is protecting nothing.

Neither applies unless you set `CircuitBreakerEnabled=true`. On the default the
Queue Processor never reads the record at all.

### Transitions

```
              ┌──────────┐
              │  CLOSED  │◄──── Successful workflow in HALF_OPEN
              └────┬─────┘      (workflow_tracker)
                   │            OR manual Resume (admin)
                   │
                   │ CloudWatch alarm fires
                   │ OR manual Pause (admin)
                   ▼
              ┌──────────┐
              │   OPEN   │◄──── Alarm fires during HALF_OPEN
              └────┬─────┘      OR manual Pause (admin)
                   │
                   │ Recovery timeout elapsed (scheduled health check)
                   │ OR alarm returns to OK
                   │ OR manual Probe (admin)
                   ▼
              ┌───────────┐
              │ HALF_OPEN │──── First new alarm re-opens
              └───────────┘     (back to OPEN)
```

## Architecture

```
┌─────────────┐    SNS     ┌────────────────────┐    DynamoDB    ┌──────────────┐
│ CloudWatch  │───────────►│  Circuit Breaker   │◄──────────────►│ Concurrency  │
│   Alarm     │            │     Manager        │                │    Table     │
└─────────────┘            └────────────────────┘                └──────────────┘
                                   │                                    ▲
                                   │ SNS                                │
                                   ▼                                    │
                           ┌──────────────┐                             │
                           │ AlertsTopic  │                             │
                           │ (notify ops) │                             │
                           └──────────────┘                             │
                                                                        │
┌─────────────┐                                                         │
│    SQS      │────────────►┌─────────────────┐  check state before     │
│   Queue     │             │ Queue Processor │─────processing──────────┘
└─────────────┘             └─────────────────┘
                                   │
                                   │ if CLOSED or HALF_OPEN
                                   ▼
                           ┌─────────────────┐
                           │  Step Functions │
                           │    Workflow     │
                           └─────────────────┘
```

- **BedrockServiceOutageAlarm**: CloudWatch MetricMath alarm on Bedrock error metrics (see [Alarm threshold](#alarm-threshold)).
- **CircuitBreakerManager**: Lambda triggered by the alarm's SNS topic and by a 5-minute EventBridge schedule. Manages state transitions and publishes notifications.
- **ConcurrencyTable**: Existing DynamoDB table — circuit breaker state is stored on the `circuit_breaker` partition key.
- **QueueProcessor**: Reads state before starting workflows. If OPEN, it returns without starting the Step Functions execution, leaving the message in SQS.
- **WorkflowTracker**: On a successful workflow completion, if state is HALF_OPEN, transitions to CLOSED.

## Alarm threshold

The `BedrockServiceOutageAlarm` uses MetricMath to sum the Bedrock error categories you opt into and compares the total to `CircuitBreakerFailureThreshold`.

Expression:

```
<SU> * FILL(m1, 0) + <Thr> * FILL(m2, 0) + <QL> * FILL(m3, 0)
```

Where each coefficient is `1` if the corresponding trigger is enabled and `0` otherwise. Metrics `m1`/`m2`/`m3` are `BedrockServiceUnavailable`, `BedrockThrottling`, and `BedrockQuotaLimit` under the stack namespace.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CircuitBreakerEnabled` | `false` | Master switch. Set to `true` to provision the alarm, SNS topic, manager Lambda, and traffic gate. |
| `CircuitBreakerTriggerServiceUnavailable` | `true` | Count 503 `ServiceUnavailableException` errors toward threshold. |
| `CircuitBreakerTriggerThrottling` | `false` | Count `ThrottlingException`, `TooManyRequestsException`, `RequestLimitExceeded`. |
| `CircuitBreakerTriggerQuotaLimit` | `false` | Count `ServiceQuotaExceededException`. |
| `CircuitBreakerFailureThreshold` | `3` | Combined error count per 5-minute period to breach. |
| `CircuitBreakerEvaluationPeriods` | `1` | Consecutive periods that must breach. |
| `CircuitBreakerRecoveryTimeoutSeconds` | `300` | Seconds before automatic OPEN → HALF_OPEN transition. |
| `CircuitBreakerErrorHandlerArn` | (empty) | Optional Lambda ARN invoked on state changes for custom handling. |

**Default behavior when enabled**: 3 or more `ServiceUnavailableException` errors in a single 5-minute window open the breaker. Throttling and quota-limit errors are not counted by default because those usually indicate client-side load issues, not a Bedrock outage. Enable additional triggers to protect against sustained throttling or quota exhaustion.

## Error categories

The Bedrock client in `idp_common` emits category-specific CloudWatch metrics under the stack namespace whenever it catches a retryable error:

| Metric | Bedrock exception code(s) | Typical cause |
|--------|---------------------------|---------------|
| `BedrockServiceUnavailable` | `ServiceUnavailableException` (503) | Bedrock service degradation or regional outage |
| `BedrockThrottling` | `ThrottlingException`, `TooManyRequestsException`, `RequestLimitExceeded` | Client-side throughput limits reached |
| `BedrockQuotaLimit` | `ServiceQuotaExceededException` | Account quota exhausted for the model |

These metrics are emitted unconditionally, independent of `CircuitBreakerEnabled`, so you can observe Bedrock error rates even when the circuit breaker is disabled.

## Enabling the circuit breaker

Set `CircuitBreakerEnabled=true` at deploy time:

```bash
aws cloudformation deploy \
  --stack-name my-idp-stack \
  --template-file template.yaml \
  --parameter-overrides \
    CircuitBreakerEnabled=true \
    CircuitBreakerFailureThreshold=3 \
    CircuitBreakerTriggerThrottling=true
```

Or via the IDP CLI / console. When `CircuitBreakerEnabled=false` (the default), none of the circuit breaker resources are provisioned — no alarm, no SNS topic, no manager Lambda — and the Queue Processor skips the state check entirely.

## Tuning guidance

For GovCloud or other environments experiencing intermittent Bedrock outages:

```
CircuitBreakerFailureThreshold: 3
CircuitBreakerEvaluationPeriods: 1        # 5-minute window
CircuitBreakerRecoveryTimeoutSeconds: 300 # 5 minutes before probing
```

For stable environments where you want the breaker as a safety net only:

```
CircuitBreakerFailureThreshold: 5
CircuitBreakerEvaluationPeriods: 2         # 10-minute window
CircuitBreakerRecoveryTimeoutSeconds: 600  # 10 minutes
```

## Web UI

When `CircuitBreakerEnabled=true`, the document list header shows a live status badge that reflects the current breaker state. The UI polls the breaker's DynamoDB record roughly every 15 seconds through the REST API (`src/ui/src/hooks/use-circuit-breaker.ts`), so a transition is visible within about one interval:

| Badge | State | Meaning |
|-------|-------|---------|
| Green "Circuit: closed" | CLOSED | Normal operation |
| Blue "Circuit: recovering" | HALF_OPEN | Probing recovery |
| Red "Circuit: Bedrock outage" | OPEN (automatic) | Opened by `BedrockServiceOutageAlarm`; hover for `lastError` |
| Red "Circuit: manually paused" | OPEN (manual) | Opened via the admin **Pause processing** control; hover for the reason |

When `CircuitBreakerEnabled=false` the badge is hidden entirely.

Click the badge to open a details panel showing `state`, `openedAt`, `lastCheckedAt`, `failureCount`, `recoveryAttempts`, and `lastError`. Users in the **Admin** Cognito group additionally see three controls:

- **Pause processing** — forces OPEN (available when state is CLOSED or HALF_OPEN). Use before planned Bedrock changes or to quiesce the pipeline.
- **Resume processing** — forces CLOSED and resets failure/recovery counters. Use to clear a stuck OPEN state. Resume is unconditional: if the underlying Bedrock outage is still active, a subsequent alarm will re-open the breaker within ~5 minutes. Hold until the alarm clears before resuming.
- **Probe recovery** — forces HALF_OPEN (available when state is OPEN). Use to test recovery before the automatic timeout.

Each control requires a **reason** that is persisted to DynamoDB (`lastError` field for pause; also logged) and broadcast over the existing SNS alerts topic. All transitions — including automatic ones from CloudWatch alarms, the scheduled health check, and the `HALF_OPEN → CLOSED` transition triggered by a successful workflow completion — are written to the same DynamoDB record, so every open browser picks them up on its next poll.

Non-admins can view the panel but do not see the control buttons.

**VPC deployments.** When `ApiGatewayVisibility=PRIVATE`, both the circuit-breaker manager and the circuit-breaker resolver Lambda are automatically attached to the same VPC/subnets used by the other private-API resolvers, so their DynamoDB reads and writes succeed from inside the VPC. If you run with `DeployInVPC=true` but `ApiGatewayVisibility=GLOBAL`, the resolver still reaches DynamoDB over the public endpoint; in accounts whose SCP blocks public DynamoDB without a gateway endpoint, the badge will fail to load. Deploy with `ApiGatewayVisibility=PRIVATE` in that case.

## Manual operations

Reset the circuit breaker (force CLOSED):

```bash
aws lambda invoke --function-name <CircuitBreakerManagerFunctionName> \
  --payload '{"action": "reset"}' response.json
```

Check current state:

```bash
aws lambda invoke --function-name <CircuitBreakerManagerFunctionName> \
  --payload '{"action": "get_state"}' response.json
```

Or read state directly from DynamoDB:

```bash
aws dynamodb get-item \
  --table-name <ConcurrencyTable> \
  --key '{"counter_id": {"S": "circuit_breaker"}}'
```

## Observability

CloudWatch metrics emitted by the circuit breaker (under the stack namespace):

- `CircuitBreakerOpened` — incremented each time the breaker transitions to OPEN
- `CircuitBreakerHalfOpen` — incremented on transition to HALF_OPEN
- `CircuitBreakerClosed` — incremented on transition to CLOSED
- `CircuitBreakerCheckFailed` — the Queue Processor could not read the breaker's
  state, dimension `Classification` = `TRANSIENT` (admission was blocked) or
  `TERMINAL` (admission was allowed and the breaker is protecting nothing until
  the cause is fixed). No other signal reports this: the manager Lambda publishes
  only on transitions it makes, and a read that never reached the table is not a
  transition.

The `AlertsTopic` receives SNS notifications on every state transition so operators can subscribe email, SMS, or PagerDuty endpoints.
