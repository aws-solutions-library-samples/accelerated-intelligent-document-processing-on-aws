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
| **HALF_OPEN** | Testing recovery. A bounded number of probes is admitted — 5 workflows per 60 seconds — and every other message is returned to SQS for retry. The first successful workflow closes the breaker; if none closes it within the probe window (15 minutes by default) the breaker returns to OPEN and the recovery timeout is re-timed. |

### How much traffic HALF_OPEN admits

Five workflows per rolling 60-second window, and the rest are refused with the
message left on `DocumentQueue` to retry on its ordinary 60 s visibility
timeout. The budget is claimed with a conditional DynamoDB write on the breaker
record, so concurrent Queue Processor invocations cannot collectively exceed it.

The two numbers are module constants in `src/lambda/queue_processor/index.py`
(`HALF_OPEN_PROBE_BUDGET`, `HALF_OPEN_PROBE_WINDOW_SECONDS`) rather than stack
parameters. One probe per window would be enough to detect recovery — a single
successful workflow closes the breaker — but it would make that detection
hostage to one document, so a handful per window gives recovery several
independent chances a minute while still holding admitted work far below
`MaxConcurrentWorkflows`.

**The budget is per rolling window, not per half-open episode**, and that
matters for what happens during a long outage. The trickle is what notices the
service coming back — the first probe that completes successfully closes the
breaker — so it has to keep running for as long as the breaker is half-open. A
budget spent once per episode would stop it: nothing in the half-open state
reports a probe *failure*, so once the budget were spent with no success the
breaker would refuse every document until the episode ended, and the service
could recover with nothing left to notice.

### How long HALF_OPEN lasts

A half-open episode is bounded in time as well as in admitted traffic. If no
probe closes the breaker within **15 minutes** of entering HALF_OPEN, the
scheduled health check returns the breaker to OPEN, re-times the recovery
timeout from that moment, and the cycle begins again: a quiet OPEN pause,
followed by a fresh bounded probe episode. A sustained outage therefore
alternates between the two states rather than settling in either.

That matters for how long the queue can hold documents. A refused document in
OPEN has its SQS visibility pushed out to
`CircuitBreakerRecoveryTimeoutSeconds` (300 s by default), while a refused
probe in HALF_OPEN keeps `DocumentQueue`'s ordinary 60-second visibility — see
the table under [When the state cannot be read](#when-the-state-cannot-be-read)
for why the two are priced differently. Against `maxReceiveCount` 500 that is
roughly 41 hours of redrive budget versus roughly 8, so a breaker that settled
in HALF_OPEN would move documents to the dead-letter queue about five times
sooner than one held in OPEN.

Alternating between the two states lands between those figures rather than at
the better one, and it is worth doing the arithmetic before relying on it. With
the defaults a cycle is one 15-to-20-minute probe episode followed by a
5-to-10-minute pause (both ends decided by the same 5-minute schedule), during
which a refused message takes 15 to 20 deliveries at 60 seconds and one or two
at 300 — so 500 deliveries last roughly **10 to 12 hours**. The large number
belongs to a breaker that spends the outage paused, which is not what a probing
breaker does. Raising `CircuitBreakerRecoveryTimeoutSeconds` lengthens both
phases and buys more of it.

The window is the module constant `HALF_OPEN_TIMEOUT_SECONDS` in
`src/lambda/circuit_breaker_manager/index.py`, defined as
`max(900, RECOVERY_TIMEOUT_SECONDS)` — so raising
`CircuitBreakerRecoveryTimeoutSeconds` for a deliberately patient breaker
lengthens the probe window with it, and there is no second parameter to keep
consistent with the first. It is not shorter than 15 minutes for a reason worth
knowing before tuning it: a successful probe closes the breaker only if the
state is **still** HALF_OPEN when that workflow finishes, so a window shorter
than a document takes to process could never close the breaker at all. The
15-minute floor is a whole Lambda step's worth of room for a probe admitted at
the start of an episode. The health check evaluates the window on its 5-minute
schedule, so an episode ends between 15 and 20 minutes in.

### When the state cannot be read

The Queue Processor reads the state from DynamoDB once per message. If that read
fails, what it does next depends on whether a retry could fix the failure, because
the two halves have opposite best answers:

| Read failure | Admission | Why |
|---|---|---|
| **Transient** — throttling, read timeout, dropped connection, DynamoDB 5xx | **Blocked**, as if the breaker were OPEN | Refusing costs almost nothing: the message returns to `DocumentQueue` and has `maxReceiveCount` 500 against a 60 s visibility timeout, roughly 8 hours of retries, before anything reaches the dead-letter queue. Admitting would mean a DynamoDB blip coinciding with a Bedrock outage switches the breaker off. |
| **Terminal** — access denied, table missing, malformed request | **Allowed**, with an `ERROR` log and a metric | A retry cannot fix it, so blocking would block every message on every poll until someone intervened, and that is the wrong trade for a control that bounds spend rather than correctness. |

An error the classifier does not recognise is treated as terminal. Either outcome
emits `CircuitBreakerCheckFailed` (see [Observability](#observability)); alarm on
the `TERMINAL` dimension if you rely on the breaker, because that is the state in
which it is protecting nothing.

**What "allowed" does and does not mean on the terminal branch.** For the two
faults named above — access denied, and a missing table — documents do *not* get
through. The Queue Processor increments the same ConcurrencyTable counter a few
lines later, that write fails for the same reason, and the message is returned to
the queue there instead; if the fault persists, the backlog reaches the
dead-letter queue either way and needs a redrive. So the terminal branch is not a
promise that work keeps flowing. What it buys is the narrower case: a fault
specific to *reading* the breaker record — a permissions boundary or a
scoped-down policy that allows `UpdateItem` but not `GetItem` — where the
pipeline is genuinely healthy and only the breaker is broken. Blocking in that
case would stall a working stack to protect nothing.

Two consequences for reading an incident. The ~8-hour retry budget is the **same**
500 deliveries a message spends waiting at the concurrency gate, and this path
leaves visibility at 60 s rather than pushing it to
`CircuitBreakerRecoveryTimeoutSeconds` the way an OPEN pause does, so it consumes
that budget roughly five times faster. And the classifier errs toward admitting:
some real transport failures (TLS errors, an opaque HTTP client error, and
throttling codes spelled differently from DynamoDB's own) are judged terminal, so
they take the allowing branch and are then caught by the counter write described
above.

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
              │   OPEN   │◄──── No probe closed the breaker within the
              └────┬─────┘      probe window (scheduled health check)
                   │             OR alarm fires during HALF_OPEN
                   │             OR manual Pause (admin)
                   │
                   │ Recovery timeout elapsed (scheduled health check)
                   │ OR alarm returns to OK
                   │ OR manual Probe (admin)
                   ▼
              ┌───────────┐
              │ HALF_OPEN │──── Probe window elapsed with no success,
              └───────────┘     or a new alarm (both back to OPEN)
```

The probe window is the transition that keeps HALF_OPEN a window rather than a
resting state; see [How long HALF_OPEN lasts](#how-long-half_open-lasts). An
alarm cannot be relied on for it: the alarm handler needs a fresh transition
*into* `ALARM`, and CloudWatch does not deliver one while the alarm is already
in `ALARM`, which is exactly the case during the outage that opened the breaker.

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
                                   │ if CLOSED, or HALF_OPEN with
                                   │ a probe slot free
                                   ▼
                           ┌─────────────────┐
                           │  Step Functions │
                           │    Workflow     │
                           └─────────────────┘
```

- **BedrockServiceOutageAlarm**: CloudWatch MetricMath alarm on Bedrock error metrics (see [Alarm threshold](#alarm-threshold)).
- **CircuitBreakerManager**: Lambda triggered by the alarm's SNS topic and by a 5-minute EventBridge schedule. Manages state transitions and publishes notifications.
- **ConcurrencyTable**: Existing DynamoDB table — circuit breaker state is stored on the `circuit_breaker` partition key.
- **QueueProcessor**: Reads state before starting workflows. If OPEN, it returns without starting the Step Functions execution, leaving the message in SQS. If HALF_OPEN, it tries to claim one of the bounded probe slots and does the same when none is free (see [How much traffic HALF_OPEN admits](#how-much-traffic-half_open-admits)).
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

- `CircuitBreakerOpened` — incremented each time the breaker transitions to
  OPEN, including a return to OPEN from a half-open episode that no probe
  closed. During a sustained outage this one alternates with
  `CircuitBreakerHalfOpen` rather than firing once, so a repeating pair is the
  signature of an outage that has not recovered, not of a flapping breaker
- `CircuitBreakerHalfOpen` — incremented on transition to HALF_OPEN
- `CircuitBreakerClosed` — incremented on transition to CLOSED
- `CircuitBreakerCheckFailed` — the Queue Processor could not read the breaker's
  state, dimension `Classification` = `TRANSIENT` (admission was blocked) or
  `TERMINAL` (admission was allowed and the breaker is protecting nothing until
  the cause is fixed). No other signal reports this: the manager Lambda publishes
  only on transitions it makes, and a read that never reached the table is not a
  transition.

The `AlertsTopic` receives SNS notifications on every state transition so operators can subscribe email, SMS, or PagerDuty endpoints.
