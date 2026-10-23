# Circuit Breaker Manager

Protects the system from cascading failures when Bedrock services are degraded or unavailable.

## State Diagram

```
                    ┌─────────────────────────────────────────┐
                    │                                         │
                    ▼                                         │
              ┌──────────┐                                    │
              │  CLOSED  │◄───────────────────────────────────┤
              │ (normal) │         Alarm OK + probe succeeds  │
              └────┬─────┘                                    │
                   │                                          │
                   │ CloudWatch Alarm                         │
                   │ (Bedrock errors)                         │
                   ▼                                          │
              ┌──────────┐                                    │
              │   OPEN   │ ◄──── Alarm during HALF_OPEN       │
              │ (reject) │ ◄──── No probe closed the breaker  │
              └────┬─────┘       within the probe window      │
                   │                                          │
                   │ Recovery timeout expires                 │
                   │ OR Alarm returns to OK                   │
                   ▼                                          │
              ┌───────────┐                                   │
              │ HALF_OPEN │───────────────────────────────────┘
              │  (probe)  │
              └───────────┘
```

## How It Works

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

## States

| State | Behavior |
|-------|----------|
| **CLOSED** | Normal operation. All requests processed. |
| **OPEN** | Bedrock unavailable. Messages return to SQS for retry. |
| **HALF_OPEN** | Testing recovery. The Queue Processor admits a bounded number of probes (5 per 60 s, claimed with a conditional write on this record) and returns everything else to SQS. See [docs/circuit-breaker.md](../../../docs/circuit-breaker.md). |

## Triggers

- **CLOSED → OPEN**: CloudWatch Alarm fires (Bedrock throttling/errors)
- **OPEN → HALF_OPEN**: Recovery timeout expires (default 5 min) or alarm clears
- **HALF_OPEN → CLOSED**: Probe traffic succeeds, alarm stays OK
- **HALF_OPEN → OPEN**: No probe closed the breaker within the probe window
  (`HALF_OPEN_TIMEOUT_SECONDS`, evaluated by the scheduled health check), or a
  new alarm arrives during recovery testing

The probe-window transition is what keeps HALF_OPEN a window rather than a
resting state. Leaving HALF_OPEN for CLOSED needs a workflow to *succeed*, and
the alarm handler needs a fresh transition into `ALARM`, which CloudWatch does
not deliver while the alarm is already in `ALARM` — so without it a sustained
outage settles in HALF_OPEN, never re-times the recovery timeout, and leaves
every refused document on the queue's 60-second visibility timeout instead of
the recovery timeout the Queue Processor pushes an OPEN refusal out to.

The episode start is recorded on the breaker record as `half_open_at` by every
transition into HALF_OPEN. A HALF_OPEN record that carries none — written
before the attribute existed — is stamped with the current time on the first
health check rather than being treated as infinitely old, so an upgrade cannot
reopen a breaker that is probing healthily; the cost is one evaluation cycle.

## Alarm Threshold

The `BedrockServiceOutageAlarm` uses MetricMath to sum the Bedrock error categories you opt in to, and compares the total to `CircuitBreakerFailureThreshold`.

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `CircuitBreakerEnabled` | false | true/false | Master switch. Set to `true` to provision the alarm, SNS topic, manager Lambda, and traffic gate. |
| `CircuitBreakerTriggerServiceUnavailable` | true | true/false | Count `BedrockServiceUnavailable` (503) toward threshold |
| `CircuitBreakerTriggerThrottling` | false | true/false | Count `BedrockThrottling` (ThrottlingException, TooManyRequestsException, RequestLimitExceeded) toward threshold |
| `CircuitBreakerTriggerQuotaLimit` | false | true/false | Count `BedrockQuotaLimit` (ServiceQuotaExceededException) toward threshold |
| `CircuitBreakerFailureThreshold` | 3 | 1-100 | Combined error count per period to breach |
| `CircuitBreakerEvaluationPeriods` | 1 | 1-10 | Consecutive 5-min periods that must breach |

**Default behavior**: The circuit breaker is **disabled by default** (`CircuitBreakerEnabled=false`). When enabled, **3+ ServiceUnavailable errors in a single 5-minute window** opens the circuit breaker. Throttling and quota-limit errors are not counted by default, since those indicate client-side load issues rather than a Bedrock outage.

Enable additional triggers when you want the breaker to also protect against sustained throttling or quota exhaustion. The metrics are summed, so a threshold of 3 with both ServiceUnavailable and Throttling enabled opens the breaker on any combination totaling 3 errors in the window.

These are CloudFormation stack parameters configurable at deployment time.

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `RECOVERY_TIMEOUT_SECONDS` | 300 | Seconds before OPEN → HALF_OPEN |
| `ERROR_HANDLER_ARN` | (none) | Optional Lambda for custom handling |
| `METRIC_NAMESPACE` | GENAIDP | CloudWatch metrics namespace |

`HALF_OPEN_TIMEOUT_SECONDS` — how long a HALF_OPEN episode may run before it
returns to OPEN — is a module constant rather than an environment variable or a
stack parameter. It is `max(900, RECOVERY_TIMEOUT_SECONDS)`, so the one knob an
operator already has for how patient the breaker should be sets this too.
**Do not shorten the 900-second floor without reading the comment at the
constant**: `workflow_tracker` only closes the breaker if the state is still
HALF_OPEN when a probe finishes, so a window shorter than a probe workflow can
never close the breaker at all.

## Manual Operations

Reset the circuit breaker:
```bash
aws lambda invoke --function-name <CircuitBreakerManager> \
  --payload '{"action": "reset"}' response.json
```

Check current state:
```bash
aws lambda invoke --function-name <CircuitBreakerManager> \
  --payload '{"action": "get_state"}' response.json
```
