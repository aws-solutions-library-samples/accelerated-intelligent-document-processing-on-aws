---
title: "Monitoring and Logging"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Monitoring and Logging

The GenAIIDP solution provides comprehensive monitoring through Amazon CloudWatch to give you visibility into the document processing pipeline.

## CloudWatch Dashboard

The solution automatically creates an integrated dashboard that displays:

### Latency Metrics

- **End-to-End Processing Time**: Total time from document upload to completion
- **Step Function Execution Duration**: Time spent in workflow orchestration
- **Lambda Function Latency**: Processing time per function (OCR, Classification, Extraction)
- **Queue Wait Time**: Time documents spend in processing queues
- **Model Inference Time**: Bedrock model response latencies

![Latency Metrics Dashboard](../images/Dashboard1.png)

### Throughput Metrics

- **Documents Processed per Hour**: Overall system throughput
- **Pages Processed per Minute**: OCR processing rate
- **Classification Requests per Second**: Page classification throughput
- **Extraction Completions per Hour**: Field extraction processing rate
- **Queue Message Rate**: SQS message processing velocity

![Throughput Metrics Dashboard](../images/Dashboard2.png)

### Error Tracking

- **Workflow Failures**: Step Function execution failures with error categorization
- **Lambda Timeouts**: Function timeout events and duration analysis
- **Model Throttling**: Bedrock throttling events and retry patterns
- **Dead Letter Queue Messages**: Failed messages requiring manual intervention
- **Validation Errors**: Data validation failures and format issues

![Error Tracking Dashboard](../images/Dashboard3.png)

### Document Queue Backlog and Dead-Letter Queue Depth

Two widgets cover the SQS layer between upload and execution, which is where a
document can be lost or stuck without any Step Functions metric noticing:

- **Document Queue Backlog** — messages waiting, messages in flight, and the
  **age of the oldest message** on the right axis, with a horizontal annotation
  at `QueueStalledAgeThresholdSeconds`.
- **Dead-Letter Queue Depth** — the document, queue-sender, and workflow-tracker
  DLQs on one graph. Every line has its own alarm, and any non-zero value means a
  document that needs a human.

**Reading the backlog widget:** depth on its own is not a problem. The queue
processor gates on the workflow-concurrency counter and *refuses* a message by
letting its 30-second visibility timeout lapse, so a bulk upload legitimately
parks thousands of messages. Rising depth with a **flat** age line is a queue
draining under load. A **climbing** age line while the SQS throughput widget
shows nothing being deleted is a stall — see
[`DocumentQueueStalledAlarm`](#documentqueuestalledalarm--why-it-is-not-a-queue-depth-alarm).

### Workflow Concurrency Counter

The stack limits in-flight workflows with a DynamoDB counter: the queue processor
increments it before `StartExecution`, and the workflow tracker decrements it on
the execution's terminal event. If a decrement is ever lost, the counter drifts
**upward** and nothing puts it back — so once it reaches
`MaxConcurrentWorkflows`, documents stop starting **permanently**.

That failure is quiet. Every other signal looks *idle* rather than broken: no
errors, no failed executions, latency graphs simply stop. The usual first symptom
is a person noticing that nothing has processed for hours.

The counter can also drift the other way. Admission is gated on
`active_count < MaxConcurrentWorkflows`, so a counter driven **below zero** raises
the effective ceiling by exactly that much and nothing errors: documents process,
queues drain, every graph looks healthy, and the stack simply spends more on
Bedrock and Textract than it was configured to. Two guards in the tracker prevent
it: the decrement is refused when the counter is already at zero, and it carries a
`dec#<executionArn>` marker written in the same DynamoDB transaction, so a
redelivered terminal event cannot release a second slot. The markers expire via
the `ConcurrencyTable` TTL attribute (`ExpiresAfter`, seven days) rather than
accumulating one item per document forever.

Unlike the upward leak, a negative counter is **not** permanent even without
intervention: every admitted document increments it, and once it climbs to the
ceiling the floored decrements absorb the excess, so it converges back on its own.
The over-admission is therefore bounded to roughly one generation of documents
rather than lasting forever — which is why the guards and the repair below matter
for cost and for predictable capacity rather than for recoverability.

Four metrics in the stack's own namespace (`<StackName>`) make all of this
visible, all on the **Workflow Concurrency Counter** widget:

- **`ConcurrencyCounterActive`** — the counter value, published on every document
  completion. Continuous, so there is a history to inspect after the fact. Its
  **Minimum** is plotted as well as its Average, because a single dip below zero
  is what matters and an average hides it.
- **`ConcurrencyCounterDrift`** — claimed slots minus executions actually
  running. Sampled only when an increment is *refused*, i.e. when drift is
  actually blocking work.
- **`ConcurrencyCounterUnderflow`** — a decrement that was refused because the
  counter was already at zero. Nothing else reports this: the counter and the
  document both end up correct, so without this metric a duplicate release is
  invisible.
- **`ConcurrencyDecrementSuppressed`** — a terminal event whose slot had already
  been released, recognised by its `dec#<executionArn>` marker and skipped. This
  is the guard working, not a fault, so it has **no alarm**: EventBridge
  redelivery is expected (the rule allows three retries, and a tracker invocation
  that fails after the decrement lands is redelivered by design), and alarming on
  correct behaviour would be noise. It is worth watching as a series, because it
  is the only signal that terminal events are being redelivered at all — a rising
  count alongside `WorkflowTrackerDLQAlarm` or tracker errors says the tracker is
  failing *after* it releases the slot.

Four alarms publish to `AlertsTopic`:

- **`ConcurrencyCounterDriftAlarm`** — sustained drift (> 0 for 15 minutes). This
  fires on the *symptom*, once slots are already being held wrongly.
- **`WorkflowTrackerDLQAlarm`** — any message in the Workflow Tracker
  dead-letter queue. This fires on the *cause*: the tracker owns the decrement,
  so an event it could not process is a slot that was never released, and it
  alarms on the first message rather than waiting for drift to accumulate.
- **`ConcurrencyCounterUnderflowAlarm`** — any refused decrement. The floor
  already prevented the damage, so this is a *correctness* signal: something
  released a slot twice, and the reason is worth finding.
- **`ConcurrencyCounterNegativeAlarm`** — the counter observed below zero. This
  should be unreachable now that the decrement is floored; if it fires, the
  counter is being written by something that bypasses the floor.

The queue processor also **self-heals**, in two different places for the two
different directions:

- **Downward correction** (the counter is too high) runs on a *refused* increment,
  reconciling against `ListExecutions` and writing conditionally on the value it
  sampled. It requires the same discrepancy in two samples at least five minutes
  apart, because lowering the counter wrongly over-admits work.
- **Upward repair** (the counter is negative) runs on the next *successful*
  increment — which is where it has to be, because a negative counter always
  satisfies `active_count < MaxConcurrentWorkflows` and so is never refused. The
  increment asks DynamoDB for the updated value, and a post-increment value of
  zero or below means it was negative before. The counter is then raised to the
  executions actually running plus the slot that increment just claimed (that
  execution does not exist yet, so `ListExecutions` cannot see it), conditionally
  on the value observed, and never to a value below zero. So a negative counter is
  corrected within one admitted document rather than needing the queue to be at
  its ceiling first.

The repair publishes the pre-repair **negative** value — not the value the counter
reads after the increment — before it writes, so `ConcurrencyCounterNegativeAlarm`
still fires on a counter that healed itself. Without that a self-healed underflow
would leave no trace at all.

**Reading the widget:** the counter tracking a busy queue is normal. The counter
sitting at or near `MaxConcurrentWorkflows` while the SQS widget shows messages
in flight and the Step Functions widget shows nothing starting is the upward
leak. The counter minimum below the zero annotation, or any
`ConcurrencyCounterUnderflow` bar, is the downward one. A
`ConcurrencyDecrementSuppressed` bar on its own is the idempotency guard doing its
job.

### Stale Output Purge on Re-upload

OCR has a retry-safe recovery path: on a Step Functions retry the document is
reloaded with `pages={}`, so before re-OCRing it scans
`s3://<OutputBucket>/<key>/pages/` and reuses any page that already has all four
of its files (`rawText.json`, `result.json`, `textConfidence.json`, `image.*`).
That is what makes a throttled OCR retry cheap — and it is also why uploading a
**different** document under an **existing** filename used to produce the
previous document's extraction ([#719](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/719)).
Two paths now purge before processing: the queue sender removes `<key>/pages/`
on every upload event, and the **Reprocess** action removes everything under
`<key>/` except `<key>/runs/` (matching its "start over" intent).

This failure is quiet in the same way the concurrency leak is: if the purge only
partly succeeds, the document processes, reports success, and silently carries
text from the old document — recovery needs just **one** surviving complete page
to skip OCR for it. Processing deliberately continues on a purge failure (a
possibly-stale extraction beats a dropped upload or a refused reprocess), so the
signal has to come from a metric rather than the document's own status.

One metric in the stack's own namespace (`<StackName>`):

- **`StaleOutputPurgeFailed`** — published (value `1`) whenever a purge raises.
  Both the ingest path and the reprocess path publish it, with no dimensions and
  into the **root** stack's namespace, so one metric and one alarm cover both.
  Published only on failure, so no data means every purge succeeded.

One alarm publishes to `AlertsTopic`:

- **`StaleOutputPurgeFailedAlarm`** — any occurrence within 5 minutes. Unlike
  concurrency drift there is no self-healing path: the stale pages sit in S3
  until someone removes them, and every later upload of that key inherits the
  same wrong results — so this alarms on the **first** failure rather than on a
  sustained trend.

Two dashboard widgets are paired on the main dashboard: **Stale Output Purge
Failures** (the count across both paths) and **Stale Output Purge Failures —
affected keys** (a Logs Insights table over the Queue Sender log group).

**Recovering:** identify the affected keys, then delete
`s3://<OutputBucket>/<key>/pages/` and re-upload or reprocess the document.
The log widget covers the ingest path; for the reprocess path, query the
`ReprocessDocumentResolverFunction` log group (in the API-resolvers nested
stack) instead. The two paths log different messages:

| Path | Log group | Message |
|---|---|---|
| Upload / re-upload | `QueueSender` | `Failed to purge previous output data for <key>` |
| Reprocess action | `ReprocessDocumentResolverFunction` | `Failed to delete previous output data for <key>` |

The most common cause is a KMS or bucket-policy change that denies
`s3:DeleteObject` to the purging role — check that before assuming a transient
S3 error.

**Note:** because the purge runs on every upload, a re-upload of a
byte-identical file no longer reuses the prior OCR cache; it re-OCRs from
scratch.

### Confidence Assessment Degraded

Confidence assessment is an *enrichment* pass: extraction has already run,
written its results and been paid for by the time it starts. So when the
confidence model fails **deterministically** — most often
`ValidationException: Input is too long for requested model.`, which a retry
would send again unchanged — the Assessment Lambda keeps the extraction and
degrades that section instead of failing the document
([#901](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/901)).
The document completes, its extracted data is intact, and the gap is recorded as
an error-severity `assessment_failed_confidence_unavailable` processing issue on
the section.

A section can also reach the Assessment step with **nothing to assess** — no
extraction result written for it, no pages listed on it, or an extraction result
whose `inference_result` is empty. The confidence model is never called, so this
is not a confidence failure and the document completes for the same reason, but
the outcome for that section is identical: no confidence scores, and therefore no
coverage by confidence-based review. It is recorded as an error-severity
`assessment_skipped_confidence_unavailable` issue on the section
([#1006](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1006)).

Not every empty result is that, though. A class with **no attributes to extract**
makes extraction skip the model deliberately and write an empty result flagged as
such, and that is an everyday occurrence rather than a gap: a page classified
`unclassified` — a blank page, a page whose classification errored, or any page in
a deployment with no document types configured — has no class in configuration and
therefore no attributes. Those sections report nothing at all, exactly as an
[excluded class](./classification.md) does, because an error indicator and an
alarm data point per blank page would make both useless.

That is the right trade for one section, and it creates a monitoring gap for the
fleet ([#996](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/996)):
a **systemic** confidence failure no longer fails documents, so it no longer
lights `WorkflowErrorsAlarm` or any DLQ alarm. Without a metric it would be
visible only in the Sections panel, one document at a time. `ProcessingIssueCount`
does not help — it is a DynamoDB attribute on the tracking record, not a
CloudWatch metric.

One metric in the stack's own namespace (`<StackName>`):

- **`AssessmentConfidenceUnavailable`** — published (value `1`) by the unified
  pattern's `AssessmentFunction`, with no dimensions, each time a section ends up
  with **no confidence scores**, whichever of the two causes above put it there.
  It reaches the **root** stack's namespace because that function's
  `METRIC_NAMESPACE` is the `StackName` the parent passes down. The metric
  deliberately does not separate a failed confidence pass from a skipped one: the
  question it exists to answer is "are sections coming back without confidence?",
  and the answer is yes either way. The section's issue code is what tells the two
  apart once you open the document, and they point at different remedies — the
  confidence model for a failure, whatever produced the section for a skip.
  Nothing is published for a section the confidence pass was never going to score:
  one whose class is **excluded**, one that extraction deliberately produced no
  fields for because its class has **no attributes to extract** (which is what a
  page classified `unclassified` gets), or any section at all when confidence
  assessment is switched off in configuration. All three are expected on healthy
  documents — a single blank page or cover sheet produces the second — so counting
  them would breach the alarm's threshold on ordinary throughput. **No data
  therefore means every section that should have been scored was scored** — give
  or take a confidence pass that failed transiently and succeeded on retry.

One alarm publishes to `AlertsTopic`:

- **`AssessmentConfidenceUnavailableAlarm`** — `ConfidenceUnavailableThreshold`
  (default `10`) or more degrades within 15 minutes. Unlike
  `StaleOutputPurgeFailedAlarm` this deliberately does **not** alarm on the first
  occurrence: a single degraded section is an expected, self-limiting outcome — one
  unusually large section against a small-context confidence model produces it with
  nothing misconfigured. A steady stream is what a systemic cause produces, because
  it degrades every section of every document. The default assumes no single
  document legitimately produces ten degrades; **raise the parameter if your
  documents split into many sections** that a small-context confidence model cannot
  fit, since one such document would otherwise fire it on its own. The
  unified-pattern dashboard draws the configured value as its annotation, so the
  graph and the trigger stay in step when you tune it.

**Diagnosing.** Start from the recorded issue's `code`, which says whether the
confidence pass failed or never ran, and its `root_cause`, which names the
underlying exception for a failure and what the section was missing for a skip.
The same line is logged at ERROR in the AssessmentFunction log group. Note
that group is `/<StackName>-PATTERNSTACK-<id>/lambda/AssessmentFunction`: the name
comes from `AWS::StackName` **inside the nested pattern template**, which is the
nested stack's CloudFormation-generated name, not the root stack's — so list on the
`/<StackName>-PATTERNSTACK` prefix rather than typing the path. A failure logs
"Deterministic (non-retryable) assessment failure"; a skip logs what the section
was missing. The four causes worth checking first:

| Symptom in the recorded issue | Likely cause | Fix |
|---|---|---|
| `ValidationException: Input is too long for requested model.` | The confidence model's input limit is smaller than the sections being assessed | Lower `extraction.confidence.list_batch_size`, or configure a confidence model with a larger context window |
| `AccessDeniedException` on `bedrock:InvokeModel` | The configured confidence model is not granted, or model access was revoked | Grant the model in Bedrock console → Model access, and check the Lambda role |
| `ValidationException` naming the model id | The model id is not available in this region | Choose a model enabled in the deployment region |
| No exception at all, and the code is `assessment_skipped_confidence_unavailable` | The section reached assessment with nothing to assess: no extraction result, no pages, or an empty `inference_result` | Look at the stage that produced the section — Extraction for a missing or empty result, Classification for a section with no pages — not at the confidence model |

**What is lost while it is firing:** the affected sections have no confidence
values, so they are not covered by confidence-based review — HITL confidence
routing and the UI threshold signals do not apply to them, and per-field scores
are absent in the UI. The extracted data itself is unaffected.

One dashboard widget on the **unified pattern** dashboard (not the main one):
**Confidence Assessment Degraded**, a 15-minute-period count with the alarm
threshold drawn as an annotation so the trend and the trigger are read together.

## Log Groups

The solution creates centralized logging across all components:

- `/aws/stepfunctions/IDPWorkflow`: Step Function execution logs
- `/aws/lambda/QueueProcessor`: Document queue processing logs
- `/aws/lambda/OCRFunction`: OCR processing logs and errors
- `/aws/lambda/ClassificationFunction`: Classification processing logs
- `/aws/lambda/ExtractionFunction`: Extraction processing logs
- `/aws/lambda/TrackingFunction`: Document tracking and status logs
- The REST API's access logs and the dispatcher Lambda's log group: Web UI API activity (the dispatcher is the single entry point for every UI query and mutation)

All logs include correlation IDs for tracing individual document processing journeys.

### `LogLevel` — what `WARN` turns off

The `LogLevel` stack parameter defaults to `WARN`. At `INFO` or `DEBUG` the
accelerator can write S3 presigned URLs, document contents and PII into
CloudWatch Logs, which is why [well-architected](./well-architected.md) has
recommended `WARN` or `ERROR` for production and why the default was changed from
`INFO` (AppSec finding #9). An **existing** stack keeps whatever value it was
deployed with — CloudFormation preserves the previous parameter value on update —
so this only affects new stacks and updates that re-specify the parameter.

Four things you may be used to seeing are absent at `WARN`, and each comes back
only by setting `LogLevel=INFO` (or `DEBUG`) and accepting the exposure above:

- **Per-stage progress lines.** The `INFO` messages many operators use to follow
  one document through OCR, classification, extraction and assessment. Errors and
  warnings are still logged, and document status is still visible in the Web UI
  and the tracking table.
- **The Web UI REST API access log.** Access logging on the API Gateway stage is
  only configured when `LogLevel` is `INFO` or `DEBUG` (finding API-GW-006). It
  records request metadata only, never bodies, but it is the only trace of
  requests that fail *before* the dispatcher Lambda — authorizer 401/403s, WAF
  blocks, CORS and gateway responses. This is deliberately coupled to `LogLevel`
  rather than given its own parameter, so emitting request metadata stays one
  visible decision.
- **The dashboard widget "Count of Workflow Executions over latency threshold".**
  It is a Logs Insights query that parses the workflow tracker's `INFO` line
  `Publishing latency metrics - ... total: <n>ms`, so at `WARN` the widget is
  always empty. It is the only dashboard element with this dependency. The
  underlying data is still published as custom metrics
  (`QueueLatencyMilliseconds`, `WorkflowLatencyMilliseconds`,
  `TotalLatencyMilliseconds` in the stack's metric namespace), which drive the
  "Queue Latency" and "Workflow Latency" widgets next to it; `SlowExecutionsAlarm`
  reads Step Functions' own `ExecutionTime` metric. Neither the metrics nor the
  alarm depend on the log level, so nothing you would *alert* on is lost — only
  that one per-minute count.
- **Installed features are not affected — they still log at `INFO`.** Each
  installable feature (for example `pii-anonymizer`) is its own stack, launched
  with the `LogLevel` pinned in its `feature.yaml`, which is `INFO` for the six
  shipped features. The host's value is not forwarded. Change the feature stack's
  `LogLevel` parameter after install if you need it at `WARN`; newly scaffolded
  features default to `WARN`.
- **`idp-cli deploy --log-level` has no CLI default.** Omit it to get the template
  default on a new stack or to preserve the current value on an update. Passing
  `--log-level INFO` is honoured; it used to be silently treated as "unset".

## X-Ray Tracing

`EnableXRayTracing` (default `true`) controls AWS X-Ray tracing. On `true` each
covered Lambda runs in `Active` mode and both state machines set
`TracingConfiguration.Enabled`; on `false` the Lambdas run in `PassThrough`, which
records nothing of their own and continues a trace only if a caller already sampled
the request, and the state machines stop tracing. X-Ray is billed per trace
recorded, so `false` is how you take that line of the bill to zero.

It covers both state machines and every traced Lambda in the three templates the
main stack deploys: `template.yaml`, `patterns/unified/template.yaml`, and the
nested `feature-platform/main-stack-extensions/template.yaml`, which receives the
parameter from the main stack the same way it receives `LogLevel` and
`LogRetentionDays`. `scripts/tests/test_xray_tracing.py` is what keeps that true —
it fails if any function in those templates hardcodes a mode, and if any function in
the nested one omits it.

⚠️ **The feature-platform resolvers are new to the traced set, and on the default
`true` they add X-Ray charges an existing deployment did not have.** Those are the
UI-facing resolvers and install hooks in the nested stack. They declared
`Tracing: Active` before, but they share one execution role that carried no
`xray:PutTraceSegments`, so no segment was ever written and nothing was billed —
tracing was on and inert. The role now carries the grant, so with
`EnableXRayTracing=true` they emit segments like every other traced function in the
deployment, and with `false` they emit nothing. If you upgrade and want the previous
X-Ray spend, set the parameter to `false`.

### Installed extensions trace unconditionally

An extension you install from the Extensions catalog — `pii-anonymizer`,
`idp-data-generator`, `confbench-testset`, `sample-feature`,
`sample-health-insurance-review`, or a stack scaffolded from `feature-template` — is
its own CloudFormation stack with its own parameters, launched by you rather than
created by the main stack. The install URL pre-fills only two host-derived values,
`MainStackName` and `FeatureBucket`, so **the main stack's `EnableXRayTracing` does
not reach it**, the same way its `LogLevel` does not (see above). Each of those
templates sets `Tracing: Active` for every function in its `Globals` block, so
setting `EnableXRayTracing=false` on the main stack leaves them tracing.

What that costs depends on the function's execution role, because SAM attaches the
X-Ray write policy only to a role it *generates*:

| Functions in the six extension templates | Traces recorded |
|---|---|
| Those with a SAM-generated execution role — the majority, and every one that serves requests | Yes — SAM attaches its X-Ray managed policy because `Tracing` is declared, so these emit segments and are billed |
| Those with an explicit `Role:` — each `UiDeployerFunction`, plus `idp-data-generator`'s `DockerBuildRunFunction` and `AgentCoreRuntimeManagerFunction` | No — their roles carry no `xray:PutTraceSegments`, so tracing is declared and produces nothing |

To see the split for the version you are running, rather than trusting a number
written down here, `scripts/tests/test_xray_tracing.py`'s `_traced_without_a_grant`
is the predicate: a function it reports has an explicit role that cannot write a
segment, and every other function in those templates emits.

The policy SAM picks depends on the partition: `AWSXrayWriteOnlyAccess` in `aws`,
and `AWSXRayDaemonWriteAccess` in China and GovCloud. Both grant
`xray:PutTraceSegments`, so the table above reads the same in every partition.

To stop the 13 from tracing today, delete the extension stack, or change
`Globals.Function.Tracing` in the extension's template to `PassThrough` and
republish it. There is no per-extension parameter yet, and adding one is the
obvious third option rather than an unavailable one: all six already declare a
`LogLevel` parameter with its own default that you set on that stack when you
install it, so an `EnableXRayTracing` beside it would follow a pattern these
templates already use. What it would not do is make the main stack's setting reach
them — the catalog install flow pre-fills only `MainStackName` and `FeatureBucket`
from the host — so it is a knob per stack, not one setting for the deployment.

`scripts/tests/test_xray_tracing.py` records these six templates exactly, in both
directions, so a seventh cannot join them silently and converting one forces its
entry to be removed.

## Pattern-Specific Monitoring

Each pattern includes additional monitoring tailored to its specific workflow:

### Pattern 1: Bedrock Data Automation (BDA)
- BDA project execution metrics
- API usage and throttling
- Media processor performance

### Pattern 2: Textract + Bedrock
- Textract OCR performance
- Bedrock model usage
- Classification confidence distribution
- Extraction completeness metrics

### Pattern 3: Textract + UDOP + Bedrock
- SageMaker endpoint performance
- UDOP model latency and throughput
- GPU utilization metrics

## Alarms the Stack Creates

Every alarm publishes to one SNS topic, `AlertsTopic`. A standard deployment
subscribes the `AdminEmail` address to it at deploy time — but **that subscription
is not live until the address confirms it**, and one address is not an on-call
rota. A `--headless` deployment subscribes **nothing**, because the transform
removes the `AdminEmail` parameter along with the UI, so there setting up delivery
is a required step you perform yourself. Read
[Who receives the alerts](#who-receives-the-alerts) before assuming these alarms
will reach anyone, in either mode.

> ⚠️ **On stacks deployed before release 0.6.7 with the circuit breaker disabled
> (the default), no alarm notification was ever delivered.** `AlertsTopic` is
> always encrypted with the stack's customer-managed key, but the key policy
> statement granting CloudWatch permission to use it was conditional on
> `CircuitBreakerEnabled=true`. With the default `false`, every alarm action
> failed with *"CloudWatch Alarms does not have authorization to access the SNS
> topic encryption key"* — visible only in each alarm's **Actions** history, since
> the alarm itself still transitioned to `ALARM` in the console. The grant is now
> unconditional. If you upgrade a stack that has been alarming silently, expect
> notifications to start arriving; to confirm, check
> `aws cloudwatch describe-alarm-history --alarm-name <name> --history-item-type Action`
> before and after.

All of them set `TreatMissingData: notBreaching`, so an idle stack reads `OK`
rather than `INSUFFICIENT_DATA`. That is deliberate: for these signals "no
documents processed" genuinely means "no failures", and leaving alarms parked in
`INSUFFICIENT_DATA` makes a **broken** alarm indistinguishable from a quiet one
(see the `WorkflowErrorsAlarm` note below).

| Alarm | Fires when | Topic | Tuned by |
|---|---|---|---|
| `WorkflowErrorsAlarm` | Failed Step Functions executions ≥ threshold in 5 min | `AlertsTopic` | `ErrorThreshold` (default `1`) |
| `SlowExecutionsAlarm` | Average execution time exceeds the threshold over 5 min | `AlertsTopic` | `ExecutionTimeThresholdMs` (default `300000`, i.e. 300 s) |
| `WorkflowTimeoutsAlarm` | Any execution ended `TIMED_OUT` by the execution-level bound in 5 min | `AlertsTopic` | Threshold is fixed (≥ 1); the bound itself is `WorkflowExecutionTimeoutSeconds` (default `21600`, i.e. 6 hours) |
| `ConcurrencyCounterDriftAlarm` | Concurrency drift > 0 sustained for 15 min | `AlertsTopic` | — |
| `ConcurrencyCounterUnderflowAlarm` | Any decrement refused because the counter was already 0 — a slot released twice | `AlertsTopic` | — |
| `ConcurrencyCounterNegativeAlarm` | Concurrency counter observed below 0 in 5 min — the ceiling is being exceeded | `AlertsTopic` | — |
| `DocumentQueueDLQAlarm` | Any message in the document DLQ — a document that failed every retry | `AlertsTopic` | — |
| `QueueSenderDLQAlarm` | Any message in the queue-sender DLQ — an upload that was never enqueued | `AlertsTopic` | — |
| `DocumentQueueStalledAlarm` | Oldest queued document older than the threshold **and** nothing left the queue, for 30 min | `AlertsTopic` | `QueueStalledAgeThresholdSeconds` (default `1800`, i.e. 30 min) |
| `QueueProcessorErrorsAlarm` | Any `QueueProcessor` invocation error in 5 min — for this function, a timeout or out-of-memory before its SQS batch finished | `AlertsTopic` | — |
| `WorkflowTrackerDLQAlarm` | Any message in the Workflow Tracker DLQ | `AlertsTopic` | — |
| `StaleOutputPurgeFailedAlarm` | Any output-purge failure within 5 min | `AlertsTopic` | — |
| `AssessmentConfidenceUnavailableAlarm` | `ConfidenceUnavailableThreshold` or more sections left with "no confidence scores" within 15 min, whether the confidence pass failed or never ran — something systemic, not a few awkward documents | `AlertsTopic` | `ConfidenceUnavailableThreshold` (default `10`) |
| `DataMartRollupDLQAlarm` | Any message in the reporting-rollup DLQ | `AlertsTopic` | — |
| `BedrockServiceOutageAlarm` | Combined Bedrock error count exceeds the circuit-breaker threshold | `CircuitBreakerTopic` | `CircuitBreakerFailureThreshold` and the `CircuitBreakerTrigger*` toggles |

`AlertsTopic` carries the display name **Workflow Alerts**.
`BedrockServiceOutageAlarm` is created only when the circuit breaker is enabled
and reports to its own topic, since it drives automated back-off rather than
human attention — the circuit-breaker manager Lambda that topic invokes then
publishes a readable notification to `AlertsTopic`, so a breaker trip still
reaches the same recipients.

### Who receives the alerts

A standard deployment creates an **email** subscription on `AlertsTopic` for the
address you passed as the `AdminEmail` parameter — the same address that receives
the temporary Cognito password. Before release 0.6.9 there was no subscription to
`AlertsTopic` — other topics in the solution had one, this one did not: the topic
ARN was emitted as the `SNSAlertsTopicARN` stack output and an
operator was tacitly expected to subscribe by hand, so a default deployment
raised alarms nobody saw ([issue #922](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/922)).

#### Headless deployments: you set up alert delivery

> ⚠️ **A `--headless` deployment creates no subscription on `AlertsTopic`, so
> alarm delivery is a required post-deployment step.** The headless transform
> removes the `AdminEmail` parameter along with the Web UI and Cognito resources,
> and a subscription cannot reference a parameter that does not exist, so the
> subscription is removed with it. The topic and every alarm are still created, and
> `SNSAlertsTopicARN` is still a stack output — what is missing is a recipient.

This is a deliberate decision rather than an oversight, and the reasoning is worth
stating because the alternative looks obviously better until you consider who
deploys this way. Headless is the API-only path: operators using it are automating,
and most of them attach a pager, a chat webhook or an existing operational topic
through their own infrastructure-as-code. An optional `AlertsEmail` parameter on the
variant whose design goal is fewer moving parts would be a parameter most of them
never set, while the ones who do want email are equally well served by one
`aws sns subscribe` call they already have to make for their other topics. What is
**not** acceptable is finding out by missing an alarm, which is why this is called
out here, in
[Headless Deployment](./headless-deployment.md#monitoring--operations) and in
[GovCloud Operations](./govcloud-operations.md#cloudwatch-alarms) rather than left
to be inferred from the template. Tracked and decided in
[issue #984](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/984).

Do it immediately after the stack completes:

```bash
TOPIC_ARN="$(aws cloudformation describe-stacks --stack-name <stack-name> \
    --query "Stacks[0].Outputs[?OutputKey=='SNSAlertsTopicARN'].OutputValue" \
    --output text)"

# Email or a distribution list — starts in PendingConfirmation, see below.
aws sns subscribe --topic-arn "$TOPIC_ARN" \
  --protocol email --notification-endpoint ops-alerts@example.com

# Or an endpoint with no confirmation step and no unsubscribe link in the payload,
# which is the better choice for an automated deployment.
aws sns subscribe --topic-arn "$TOPIC_ARN" \
  --protocol https --notification-endpoint https://example.com/hooks/idp-alerts
```

Then verify with the `list-subscriptions-by-topic` command below. Treat a topic
with zero subscriptions as a failed deployment step: every alarm will publish
successfully and notify nobody, and nothing in the stack, the console or the alarm
history will tell you.

#### You must confirm the subscription before anything is delivered

> ⚠️ **An SNS email subscription starts in `PendingConfirmation` and delivers
> nothing at all until the recipient clicks the confirmation link.** SNS sends a
> *"AWS Notification - Subscription Confirmation"* message to `AdminEmail` when
> the stack is created. Until someone opens it and follows the link, every alarm
> still publishes successfully and every notification is still dropped — which
> looks exactly like the pre-0.6.9 behaviour. A pending confirmation does **not**
> fail or delay the CloudFormation operation, so there is no deployment error to
> notice; the confirmation token is valid for about two days, after which you have
> to re-request one from the SNS console.

Check the status any time:

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn "$(aws cloudformation describe-stacks --stack-name <stack-name> \
      --query "Stacks[0].Outputs[?OutputKey=='SNSAlertsTopicARN'].OutputValue" \
      --output text)" \
  --query 'Subscriptions[].{Protocol:Protocol,Endpoint:Endpoint,Arn:SubscriptionArn}' \
  --output table
```

A `SubscriptionArn` of the literal string `PendingConfirmation` means exactly
that — unconfirmed, delivering nothing. A real ARN means the endpoint is live.

#### Alert a team, not one person

One personal mailbox is a single point of failure for every alert in the
solution. The `AdminEmail` subscription is a floor, not a design: add the
recipients you actually want to the same topic. These are ordinary SNS
subscriptions and are independent of the stack, so adding them does not conflict
with a stack update, and removing the stack removes only the subscription it
created.

There is a second reason beyond the rota. **Every SNS email carries a one-click
unsubscribe link, so any recipient — or anyone the mail is forwarded to — can
remove the subscription without telling you, and nothing in the stack notices.**
Alarms then keep publishing successfully to a topic nobody receives, which is
indistinguishable from having no subscription at all. This is inherent to
`Protocol: email` rather than something this solution introduces, and it is the
strongest argument for the options below: a chat or `https` subscription has no
unsubscribe link in the payload, and a distribution list keeps the SNS endpoint
constant no matter who leaves it. If you rely on email, re-run the
`list-subscriptions-by-topic` check above periodically.

- **A distribution list or ticket queue** — subscribe a group address rather than
  an individual, so the rota changes without a stack update:

  ```bash
  aws sns subscribe --topic-arn <alerts-topic-arn> \
    --protocol email --endpoint idp-oncall@example.com
  ```

  Every email subscription needs its own confirmation click, including this one.

- **Chat** — [AWS Chatbot](https://docs.aws.amazon.com/chatbot/latest/adminguide/getting-started.html)
  subscribes the topic to a Slack channel or Amazon Chime/Microsoft Teams room and
  renders the alarm payload legibly. No confirmation step, and the channel history
  gives you an audit trail that a mailbox does not.

- **Paging** — PagerDuty, Opsgenie and similar accept an SNS `https` subscription
  endpoint, which is confirmed automatically by the receiving service. Use this if
  an alarm needs to wake someone; email will not.

- **An existing operational topic** — if you already centralise alarms, you do not
  have to use `AlertsTopic` as the fan-out point. Subscribe your own topic's
  ingest Lambda/queue to it, or point the alarms at your topic directly by
  editing `AlarmActions` in a template you deploy yourself. A subscriber in
  another account needs `sns:Subscribe` on the topic policy. It does **not** need
  any permission on the stack's KMS key: SNS server-side encryption protects the
  message at rest and SNS decrypts it itself before delivery, so the documented
  key-policy grants are for *publishers* and for the `sns.amazonaws.com` service
  principal, not for subscribers. The KMS requirement that does exist runs the
  other way — if you subscribe an **encrypted SQS queue**, that queue's key
  policy must allow `sns.amazonaws.com` to `kms:GenerateDataKey*` and
  `kms:Decrypt`, otherwise delivery fails silently from SNS's side.

  > ⚠️ Do not grant a foreign account `kms:Decrypt` on the stack's
  > `CustomerManagedEncryptionKey` in order to receive alerts. That one key also
  > encrypts the input, output, working and evaluation buckets, the DynamoDB
  > tables and the queues, so the grant would reach the entire processed-document
  > corpus — an enormous amount of access for an alarm email, and it is not
  > required.

#### `--headless` deployments still start with no subscribers

> ⚠️ A `--headless` deployment strips the `AdminEmail` parameter along with
> Cognito, so it collects no operator address and **creates no subscription at
> all**. It keeps `AlertsTopic` and all fifteen alarms, so a headless stack still has
> the original defect: every alarm publishes successfully and nobody is notified.
> Issue #922 is closed for the standard deployment and remains open for this one.

Subscribing at least one recipient to the `SNSAlertsTopicARN` output is therefore
a required post-deploy step for headless, not an optional improvement:

```bash
aws sns subscribe --topic-arn <alerts-topic-arn> \
  --protocol email --endpoint idp-oncall@example.com
```

Whether the headless variant should gain its own optional alerts-email parameter
is an open deployment-interface question rather than a defect in the transform.

### `WorkflowErrorsAlarm` — the primary failure signal

`ErrorThreshold` defaults to `1`, so this is an **alert on any failed
execution** rather than on a rate. That matches document processing, where each
failed execution is a document that did not get processed; a percentage
threshold would stay silent on a low-volume day when everything failed. Raise
`ErrorThreshold` if you routinely submit documents you expect to fail (malformed
uploads, for instance) and only want to hear about clusters.

Note that this counts *failed executions*. A document that fails in a way the
state machine catches and handles finishes as a **successful** execution, so it
does not appear here — use the **Processing Issues** column in the Web UI and the
Processing Report for those. `ExecutionsTimedOut` is a separate metric, covered by
`WorkflowTimeoutsAlarm` (below); `ExecutionsAborted` is not covered by any alarm.

> ⚠️ **Before release 0.6.7 this alarm never fired.** It was defined against
> `ExecutionsFailedCount`, which is not a metric Step Functions publishes, so it
> received no datapoints and stayed in `INSUFFICIENT_DATA` through real failures
> ([#746](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/746)).
> If you upgrade a stack that has been failing silently, expect notifications to
> start arriving immediately — that is the fix working, not a new problem.

### `SlowExecutionsAlarm` — check the threshold against your documents

This compares the **average** execution time over 5 minutes against
`ExecutionTimeThresholdMs`. The 300-second default suits small documents; large
packets, agentic extraction, and summarization all routinely exceed it, so a
deployment that processes those should raise the parameter or the alarm will
report normal operation as a problem. Because it is an average, one slow document
in a busy 5-minute window will not trip it — this is a "the whole pipeline is
slow" signal, not a per-document one.

### The queue alarms — the gap both of the above leave

Both alarms above read the **state machine**, so neither can see a document that
never got an execution ([#761](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/761)):
a dead-lettered document emits no `ExecutionsFailed`, and a document still sitting
in the queue emits no `ExecutionTime`. Four alarms cover the queue layer.

#### `DocumentQueueDLQAlarm` and `QueueSenderDLQAlarm`

Both fire on the **first** message, with no threshold to tune, and both also
notify on recovery (`OKActions`) — DLQ depth does not decay on its own, so the
`OK` notification is how you know someone actually drained the queue.
`WorkflowTrackerDLQAlarm` now does the same, so all three DLQ lines on the
**Dead-Letter Queue Depth** widget behave alike.

| Alarm | What a message means | Recovery |
|---|---|---|
| `DocumentQueueDLQAlarm` | The document exhausted `DocumentQueue`'s redrive policy — `maxReceiveCount` 500 against a 60 s visibility timeout, roughly **8 hours** of retries — and never processed. | Read the messages for the object keys, find the cause in the QueueProcessor and state-machine logs, then redrive or re-upload. Redrive needs `kms:Decrypt` on the stack's CMK. |
| `QueueSenderDLQAlarm` | The upload event never reached the queue, so the document never entered the pipeline. | Read the messages for the S3 keys, check the QueueSender logs, then **re-upload**. See the note below on which state the document is left in — and note that SQS redrive does **not** apply to this queue. |

> **What a `QueueSenderDLQ` message means for the document, precisely.** The
> queue sender writes the tracking record *before* it enqueues
> (`src/lambda/queue_sender/index.py`), so where the invocation died decides what
> you see: a failed **enqueue** leaves a row wedged at `QUEUED` in the Web UI
> forever, while a failed **`create_document`** leaves no record at all. Look for
> the stuck `QUEUED` document first — that is the likelier case in a queue named
> for the sender. Recovery is re-upload either way: `QueueSenderDLQ` is a Lambda
> **async-invoke** DLQ, not the dead-letter queue of another SQS queue, so
> `StartMessageMoveTask` (the console's *Redrive* button) does not apply to it and
> is not offered. `DocumentQueueDLQ` *is* a true SQS DLQ, so redrive does work
> there.

#### `QueueProcessorErrorsAlarm` — a processor that cannot finish its batches

`QueueProcessor` catches every per-message error itself, so an invocation that
ends in a Lambda **error** is one that was killed from outside: it hit its
`Timeout` or ran out of memory before finishing its SQS batch. Nothing else
reports that. The messages it held are redelivered and eventually processed, so
every execution still succeeds, the DLQ stays empty and `DocumentQueueStalledAlarm`
sees a queue that is draining. Before release 0.6.9 this was how a saturated
queue silently multiplied its own work
([#904](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/904)):
the processor ran at the 128 MB default with 50-message batches and a 30 s
timeout, two thirds of its invocations under load timed out, and because Lambda
reports a batch outcome only when the function returns, each timeout handed the
whole batch back to SQS — including messages whose `StartExecution` had already
succeeded. Each redelivery then started another execution, because executions had
no name. One upload was started six times; 4,384 uploads produced 14,727
executions, all billed, with every alarm reading `OK`.

Two changes make a redelivery harmless now, whatever Lambda or SQS do: the
execution name is derived from the SQS message id (`<basename>-<message-id>`),
so a second start of the same message is refused by Step Functions and acked as a
no-op, and each message is deleted the moment its execution exists rather than at
the end of the batch. The processor also runs at 1024 MB with 10-message batches
and a 60 s timeout, so the timeouts themselves should be rare. When this alarm
does fire, compare the function's **Duration** and **Max Memory Used** against
its `Timeout` and `MemorySize`, and check **Throttles**: a processor that cannot
finish 10 messages in 60 s is undersized for the deployment's configuration (a
very large merged configuration is decompressed per message) or is being
throttled. To confirm redelivery rather than duplicate ingest for one document,
count receives of its message in the `QueueProcessor` log group — a single
`messageId` appearing more than once is a redelivery, and since 0.6.9 the same id
is the suffix of the execution name:

```
fields @timestamp, @requestId, @message
| filter @message like /Processing message/ and @message like /<object-key>/
| sort @timestamp asc
```

#### `DocumentQueueStalledAlarm` — why it is not a queue-depth alarm

"Not draining" cannot be alarmed on queue **depth**, because waiting is the
design: the queue processor refuses a message by letting its visibility timeout
lapse while workflow concurrency is saturated.

It cannot be alarmed on message **age** alone either, and that is less obvious.
Because a refused message is never deleted and re-sent,
`ApproximateAgeOfOldestMessage` measures time since the *original* send, so it
climbs monotonically for as long as concurrency stays saturated — a healthy stack
draining a large batch will pass any fixed age threshold.

So the alarm requires **both** conditions in one metric-math expression: the
oldest message is older than `QueueStalledAgeThresholdSeconds` **and** not a
single message was deleted in the period. Deep but draining stays `OK` at any
depth; a wedged consumer fires.

**Why the window is 30 minutes** (six 5-minute periods, where the other alarms
here use one to three). A message leaves `DocumentQueue` only when a workflow
slot frees up, so with concurrency saturated by long-running documents a
*healthy* stack can legitimately delete nothing for as long as its slowest
document takes. A shorter window would page on ordinary large-packet
processing — and an alarm that cries wolf gets muted, which costs more than the
detection latency. Thirty minutes of **zero** dequeues is where "slow" and
"stuck" stop being distinguishable from outside, and it deserves attention
either way: at that point it is either a wedge or a capacity shortfall.

**Expect roughly an hour of detection latency at the defaults**, not 30 minutes:
the age condition has to be met *first* (30 minutes at the default threshold) and
the 30-minute no-progress window then runs on top of it. Lower
`QueueStalledAgeThresholdSeconds` to shorten that, at the cost of firing during
saturation.

**A circuit-breaker pause trips this alarm too, by design.** A processor in the
`OPEN` state pushes a message's visibility out to
`CircuitBreakerRecoveryTimeoutSeconds` *without deleting it*, so age climbs and
deletions stay at zero — both conditions hold. That is not suppressed, because
muting the queue signal for the duration of a Bedrock outage would mute it
exactly when documents pile up. If `BedrockServiceOutageAlarm` is active on the
same topic, this alarm is reporting the same incident and clears on its own when
the breaker closes. Only relevant when the circuit breaker is enabled, which is
not the default.

**A failure to *read* the breaker's state trips it too, and looks nothing like the
pause above.** With `CircuitBreakerEnabled=true`, a transient fault on the state read
refuses admission, which holds messages without deleting them — so both conditions hold
again. The distinguishing signal is that this case emits **no Bedrock error metrics at
all**, so `BedrockServiceOutageAlarm` stays **clear** and the runbook's "if that alarm is
also active, it is the same incident" test does not apply. Check the
`CircuitBreakerCheckFailed` metric with dimension `Classification=TRANSIENT`; the cause is
the ConcurrencyTable, not the processor and not Bedrock. See
[Circuit breaker](circuit-breaker.md#when-the-state-cannot-be-read).

**One reporting caveat.** SQS stops publishing queue metrics for a queue that has
been inactive for about six hours. In the specific case where the consumer is
fully detached *and* no new documents arrive, `FILL(m1, 0)` then yields `0`, the
condition goes false, and the alarm returns to `OK` while the queue is still
stuck. It will have fired first, so the notification is not lost — but do not
read a later `OK` as "resolved" without checking the queue.

**Tuning.** Raise `QueueStalledAgeThresholdSeconds` (default `1800`) if bulk
uploads against a low `MaxConcurrentWorkflows` trip it and you consider that
normal. Because the alarm already requires zero throughput, the threshold is
"how long is too long to wait with no progress whatsoever", not a backlog limit.

**When it fires**, check in this order: the **Workflow Concurrency Counter**
widget (a counter pinned at `MaxConcurrentWorkflows` with nothing running is a
leaked slot — `ConcurrencyCounterDriftAlarm` covers that case), the
circuit-breaker state, whether the QueueProcessor event-source mapping is still
enabled, then QueueProcessor invocations, errors, and throttles. If executions
**are** running and each simply takes longer than 30 minutes, this is a capacity
signal rather than a fault: raise `MaxConcurrentWorkflows`, or raise the
threshold to accept it.

### `WorkflowExecutionTimeoutSeconds` — the execution-level bound

The state machine has a top-level `TimeoutSeconds`, sourced from the
`WorkflowExecutionTimeoutSeconds` parameter (default **21600**, 6 hours). It is the
backstop for anything the per-state guards do not cover — a `Map` branch that
stalls rather than errors, a retry policy whose cumulative backoff runs for hours,
a future state added without its own bound. Without it a Standard workflow's ceiling
is **one year**, during which the execution emits neither `ExecutionsFailed` (so
`WorkflowErrorsAlarm` cannot see it) nor `ExecutionTime` (so `SlowExecutionsAlarm`
cannot either), while holding a workflow-concurrency slot: a stack can lose capacity
with every alarm reading `OK`.

Tripping the bound ends the execution **`TIMED_OUT`**. That is a distinct Step
Functions metric, `ExecutionsTimedOut`, which `WorkflowTimeoutsAlarm` watches (any
occurrence in 5 minutes); the execution-status EventBridge rule already routes
`TIMED_OUT` to the workflow tracker, which releases the concurrency slot and marks the
document `FAILED` with `WorkflowStatus` `TIMED_OUT` (visible in the tracking table and
the Web UI), so a timeout is distinguishable from an ordinary failure. The dashboard's
**Workflow Executions** widget plots timed-out executions alongside failed ones. A
timed-out execution **discards its completed work** (OCR, extraction,
assessment, summarization already done), so the default errs generous.

**How the default was chosen** (measured 2026-09-11 on a production stack, 30 days,
2,253 executions): median execution 0.7 minutes, p90 4.6 minutes, longest
*successful* execution 37 minutes. Every execution longer than an hour — 117 of them —
was a `FAILED` run of 306–308 minutes: a single state's Lambda `Sandbox.Timedout` at
900 seconds retried eight times at 2.5× backoff. A benchmark stack's longest success
was 2.6 minutes. Six hours is therefore about ten times the longest observed success.

That particular storm can no longer happen: every Lambda task state now retries the
timeout codes at most once, so a deterministic timeout fails in about 30 minutes
rather than 5.1 hours (see
[Step Functions Retry Configuration](./configuration.md)). The measurement is kept
here because it is what sized the bound, and because the *transient* ladder is
unchanged — a state throttled through all eight attempts still spends about 2.8
hours in backoff alone.

Be precise about what the default does and does not bound. It does **not** shorten
a state that is still inside its own `Retry` budget: a full transient ladder fails
on its own inside the 6-hour bound, and cutting the bound to 3 hours or less
(`10800`) is a defensible choice for a stack whose largest documents finish well
under an hour. What the default does bound is everything the
per-state guards cannot: a `.waitForTaskToken` callback that never arrives once the
BDA bound is exceeded (see below), a state that hangs without erroring, and a storm
that compounds across two or more states (two consecutive transient ladders are
about 5.6 hours).
Not measured: multi-hundred-page packets under agentic table extraction, which are
the case most likely to approach the bound — if your `ExecutionTime` p99 for
*successful* runs is within a factor of two of the bound, raise it (up to one year,
`31536000`, which effectively disables it).

### `BDACallbackTimeoutSeconds` — why a hung BDA job needs its own bound

This one is a parameter rather than an alarm, but it belongs here because without
it **neither alarm above can see the failure it prevents.**

It must also stay **smaller than `WorkflowExecutionTimeoutSeconds`** (default 2 hours
against 6) to have any effect: if the BDA bound is the larger of the two, the
execution-level bound fires first and the document simply ends `TIMED_OUT`, instead of
the BDA step failing with a catchable `States.Timeout` that the pipeline can record.

In BDA mode (`use_bda: true`) the `BDA_InvokeDataAutomation` step uses the Step
Functions `.waitForTaskToken` integration: it blocks until something outside the
state machine returns the token. The return path is
BDA job → EventBridge (`BDAEventRule`) → `BDACompletionFunction` → task token
read from S3 → `SendTaskSuccess`. Any hop can break — a stuck BDA job, a lost
token, a dropped EventBridge delivery, the completion handler erroring before it
responds.

Before release 0.6.7 that step had no `TimeoutSeconds`, so a broken callback left
the execution waiting **indefinitely**, and that is the worst possible shape for
monitoring:

- it never fails, so it emits no `ExecutionsFailed` → `WorkflowErrorsAlarm` is blind;
- it never completes, so it emits no `ExecutionTime` → `SlowExecutionsAlarm` is blind;
- it holds a **workflow-concurrency slot** and its tracking row the whole time.

A stack could therefore lose capacity with every alarm reading `OK`. The step is
now bounded by `BDACallbackTimeoutSeconds` (default **7200**, i.e. 2 hours), and
because the step catches `States.ALL` and routes to the fail state, tripping the
bound produces a genuinely **FAILED** execution — which emits `ExecutionsFailed`,
fires `WorkflowErrorsAlarm`, and lets the workflow tracker release the
concurrency slot and mark the document `FAILED`. A stuck document becomes an
ordinary, visible failure.

**Tuning.** The asymmetry favours a generous value: too high only delays
detection of a job that is already lost, whereas too low fails healthy work and
wastes the BDA spend already incurred. Raise it if you process very large packets
and see `States.Timeout` failures on documents that were progressing normally. A
timeout is deliberately **not** retried — re-invoking would start a second BDA
job, paying twice and risking double-processing, for a callback that may still be
in flight.

> **Pipeline mode is unaffected.** Those steps are direct Lambda invocations with
> their own function timeouts plus `Retry`/`Catch`, so they cannot hang: a failure
> propagates to `ExecutionsFailed` on its own.

## Setting Up Alerts

For who receives the **built-in** alarms — and the confirmation click that has to
happen before any of them are delivered — see
[Who receives the alerts](#who-receives-the-alerts).

Beyond the built-in alarms you can add your own for metrics specific to your
deployment:

1. **Error Rate Thresholds**: Alert when error rates exceed acceptable levels
2. **Processing Time Anomalies**: Detect unusual latency spikes
3. **Concurrency Limits**: Notify when approaching service limits
4. **Cost Controls**: Alert on unusual model usage patterns

Queue backlog and DLQ depth are already covered by the built-in alarms above, and
a plain "depth > N" alarm on `DocumentQueue` is specifically **not** worth adding
— see [`DocumentQueueStalledAlarm`](#documentqueuestalledalarm--why-it-is-not-a-queue-depth-alarm)
for why depth alone is not a fault signal in this architecture.

Example alarm configuration:

```yaml
ErrorRateAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmDescription: Alert when error rate exceeds 5%
    MetricName: DocumentProcessingErrors
    Namespace: AWS/Lambda
    Statistic: Sum
    Period: 300
    EvaluationPeriods: 1
    Threshold: 5
    ComparisonOperator: GreaterThanThreshold
    TreatMissingData: notBreaching
    AlarmActions:
      - !Ref AlertSNSTopic
```

## Log Insights Queries

The solution includes predefined CloudWatch Log Insights queries for common analysis tasks:

### Error Analysis

```
filter @message like /ERROR/ or @message like /Exception/
| parse @message "Error: *" as errorMessage
| stats count(*) as errorCount by errorMessage
| sort by errorCount desc
| limit 10
```

### Processing Time Analysis

```
filter @message like /Processing complete/
| parse @message "Processing complete in * ms" as processingTime
| stats avg(processingTime) as avgTime, min(processingTime) as minTime, max(processingTime) as maxTime by bin(30m)
| sort by avgTime desc
```

### Document Volume Tracking

```
filter @message like /Document received/
| stats count(*) as documentCount by bin(1h)
| sort by bin(1h) asc
```

## Metric Dimensions

Key metrics are available with these dimensions:

- **DocumentType**: Break down metrics by document class
- **ProcessingPattern**: Compare metrics across different patterns
- **PageCount**: Analyze performance based on document complexity
- **Region**: Track regional performance differences

## Performance Benchmarks

The dashboard includes performance benchmark comparisons:

- **Current vs. Historical Performance**: Compare current metrics against previous periods
- **Pattern Comparison**: Side-by-side comparison of different processing patterns
- **Model Performance**: Comparison of different Bedrock models for similar tasks

## Operational Monitoring

The solution provides operational metrics for infrastructure health:

- **Lambda Concurrency**: Track function concurrency usage
- **Throttling Events**: Monitor service limits and throttling
- **DynamoDB Capacity**: Track consumed read/write capacity units
- **S3 Request Rates**: Monitor bucket operation rates and latency
- **Step Functions Execution Metrics**: Track state transitions and execution counts

## Cost Monitoring

Monitor resource usage and costs:

- **Bedrock Model Tokens**: Track token usage by model and operation
- **Lambda Execution Time**: Monitor function duration and memory usage
- **S3 Storage**: Track storage growth over time
- **Data Transfer**: Monitor network costs between services

## Custom Dashboard Creation

You can create custom dashboards focused on specific aspects:

1. Open the CloudWatch console
2. Go to Dashboards and select "Create dashboard"
3. Add widgets using metrics from the "GenAIIDP" namespace
4. Organize widgets logically by processing stage or metric type

## Exporting Metrics

To export metrics for external analysis:

1. Use CloudWatch Metric Streams to send metrics to:
   - Amazon Kinesis Data Firehose
   - Third-party monitoring tools
   - Custom analytics solutions

2. Configure the stream with:
   - Metrics namespace filters
   - Output format (JSON or OpenTelemetry)
   - Destination configuration
