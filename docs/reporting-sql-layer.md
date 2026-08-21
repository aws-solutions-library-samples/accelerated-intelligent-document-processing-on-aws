---
title: "Reporting SQL Layer"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Reporting SQL Layer — rollup tables on top of `metering`

**Status:** Shipped (Phase 1).
**Owner:** Taniya Mathur.

## 1. Overview

A SQL layer on top of the raw `metering` Parquet lake that lets
consumers (idp-monitor, in-tree analytics agents, and any future
dashboard/report) answer "additive-over-wide-range" cost and volume
questions in KB-scale scans instead of GB-scale.

Three new Athena/Glue tables and one scheduled rollup Lambda; no
other infrastructure. This doc is the reference for the shape of the
tables, the partitioning contract, and the tagging model that drives
control-plane cost attribution.

Related module docs (developer tier):

- [`lib/idp_common_pkg/idp_common/reporting/README.md`](../lib/idp_common_pkg/idp_common/reporting/README.md) — the write path (`save_reporting_data.py`).
- [`docs/reporting-database.md`](reporting-database.md) — Glue database + table catalogue reference.

---

## 2. Tables

| # | Table | Grain | Populated by |
|---|---|---|---|
| 1 | `metering_hourly` | hour × config_version × service_api × unit | Scheduled hourly rollup Lambda, Athena `INSERT INTO` from raw `metering` |
| 2 | `metering_daily` | date × config_version × service_api × unit | Same rollup Lambda, `INSERT INTO` from `metering_hourly` |
| 3 | `metering_docs_hourly` | hour × config_version | Same rollup Lambda, `INSERT INTO` from raw `metering` via a MAX-per-doc subquery |
| 4 | `metering_docs_daily` | date × config_version | Same rollup Lambda, `INSERT INTO` from `metering_docs_hourly` |
| 5 | `control_plane_hourly` | hour × function_name × component × bedrock_model | Same rollup Lambda, writes Parquet directly from CloudWatch data |

**Column split — cost vs docs (Phase 1 change).** Cost columns
(`sum_value`, `sum_cost`) live on `metering_hourly` / `metering_daily`
because they aggregate cleanly per (service_api, unit). Document-level
metrics (`n_docs`, `sum_pages`) live on the separate `metering_docs_*`
tables at the coarser (hour, config_version) grain — `number_of_pages`
is stamped identically on every metering row for a given document, so
grouping by `service_api` would fan out the page count by the number
of (service_api, unit) combinations a doc touched (e.g. a 10-page doc
touching 6 service rows would report 60 pages). The `metering_docs_*`
tables aggregate via a `MAX(number_of_pages) GROUP BY document_id`
subquery to collapse the fan-out.

**Columns on `metering_hourly` / `metering_daily`** (cost per service/unit):
`hour_ts` (or `day`), `config_version`, `service_api`, `unit`,
`sum_value`, `sum_cost`.

**Columns on `metering_docs_hourly` / `metering_docs_daily`** (doc-grain
volume/pages): `hour_ts` (or `day`), `config_version`, `n_docs`,
`sum_pages`.

**Naming note — `n_docs` counts differently at hour vs day grain.**
On `metering_docs_hourly`, `n_docs = COUNT(DISTINCT document_id)`
within the hour — accurate for that hour. On `metering_docs_daily`,
`n_docs = SUM(hourly n_docs)`, which counts a document once per hour
it appeared in — a "doc-hours" count, not a cross-day unique count.
For strict cross-day unique-doc counts, query raw `metering` with
`COUNT(DISTINCT document_id)`.

**Where status/timing data actually lives** — NOT on `metering`.
`metering` carries only cost primitives: `document_id`, `context`,
`service_api`, `unit`, `value`, `number_of_pages`, `unit_cost`,
`estimated_cost`, `timestamp`, `initial_event_time`, `config_version`.
Document status (`SUCCESS`/`FAILED`/`ABORTED`), pipeline stage,
error text, and wall-clock duration are in the tracking DynamoDB
table, not in Athena. A future `document_lifecycle` table (Phase 2)
would move these into the SQL layer for KPI queries.

**All three tables are append-only.** A partition is written once and
never rewritten. The write-time partitioning of `metering` (see §2.3)
means metering rows never land in past partitions, so no
`INSERT OVERWRITE` / trailing-window / Iceberg complexity is needed.

**Consumer tier picker.** Consumers pick the cheapest sufficient table
by requested range:

| Requested range | Middle-of-window tier | Live tail (current partial bucket) |
|---|---|---|
| `< 2h` | raw `metering` (partition-pruned to hour) | n/a — whole window is "current" |
| `2h – 24h` | `metering_hourly` | raw `metering` for the current hour |
| `> 24h` | `metering_daily` | `metering_hourly` for the current day |

Sealed hour and day rows never change once written, so consumer
`SELECT`s against them are safe candidates for Athena result reuse.
**Result reuse is NOT enabled automatically.** Athena's per-query
`ResultReuseConfiguration.ResultReuseByAgeConfiguration.Enabled` is
off by default and the `primary` workgroup this pipeline uses has no
default reuse TTL set. Consumers who want it must set it explicitly
on each `StartQueryExecution` (e.g. `MaxAgeInMinutes=60` for a
one-hour cache). The rollup Lambda's own `INSERT`s never benefit
from reuse — Athena caches SELECT results only.

### 2.3 `metering` partitioning: write time, not queue time

`save_reporting_data.py` partitions metering rows by write time
(= document completion time, since the writer runs at workflow end):

- Every metering row lands in the current partition. No time-travel
  into past partitions.
- Rollups become trivially append-only — no re-materialization window.
- Dashboard time filters ("last 24h") match user intent — "docs
  completed in the last 24h" — since completion = write time.
- `initial_event_time` is preserved as a column on the row for
  consumers that need queue-time semantics.

`metering` also gains an `hour` partition key so the current-hour tail
query in the tier picker partition-prunes to ~40 MB instead of scanning
the whole day.

**Semantic consequence for consumers of raw `metering`:** the
predicate `WHERE date = '2026-08-18'` shifts meaning from "docs
queued that day" to "docs completed that day". Consumers who need
queue-time semantics filter on the `initial_event_time` column
explicitly.

**Upgrade cutover — partition semantics differ before and after.**
The migration custom resource that relocates historical
`date=X/*.parquet` files into `date=X/hour=HH/` subdirs infers the
hour from each file's own `timestamp` column. Before Phase 1, that
`timestamp` was queue-derived; after Phase 1, new writes are
completion-derived. So on any given stack:

- **New writes (post-upgrade)** — `date`/`hour` partition = completion time.
- **Historical rows (pre-upgrade, migrated in place)** — `date`/`hour`
  partition = queue time (whatever the `timestamp` column said at the
  time). Dates aren't relocated by the migration — only hours are added.

A query spanning the cutover mixes both semantics silently. In
practice this only affects docs that crossed midnight during
processing (the two interpretations agree for everything else). If
you need the cutover boundary programmatically, look at the earliest
`hour_ts` in `metering_hourly` — that's the point new-semantic rows
start appearing.

---

## Rollup Lambda

`DataMartRollupFunction` (`src/lambda/data_mart_rollup/index.py`).
Two EventBridge schedules dispatch based on the `mode` field:

- `{"mode": "hourly"}` — every hour at :05 UTC. Writes
  `metering_hourly` and `control_plane_hourly` for the previous sealed
  hour.
- `{"mode": "daily"}` — every day at 00:15 UTC. Writes `metering_daily`
  for the previous sealed day, reading from `metering_hourly`.

**Idempotency:** the handler checks whether the target partition
already has data before writing (Athena `LIMIT 1` for the metering
tables; S3 HEAD for `control_plane_hourly`). Duplicate EventBridge
fires are safe — the second run skips.

**Ad-hoc invocations** default to `hourly` mode (the more common case).

---

## 10. Cost observability — data plane vs control plane

The dashboard surfaces two independent cost KPIs so operators can
distinguish "what did processing documents cost me" from "what is the
IDP infrastructure itself costing me while idle or serving my UI".

### 10.1 Data plane cost

**Definition:** any AWS spend attributable to processing a specific
document. Lambda invocations of the per-doc pipeline (OCR /
Classification / Extraction / Assessment / Summarization / Evaluation /
BDA path / Rule Validation / ingest + tracking / pipeline hooks).
Bedrock tokens on the extraction path. Textract calls.

**Source:** raw `metering` per-doc rows, rolled up into
`metering_hourly` and `metering_daily`.

### 10.2 Control plane cost

**Definition:** every other IDP AWS spend that runs regardless of
whether documents are processing — dashboard resolvers, scheduled
AI-summary agents, natural-language agents, test-set polling,
test-run aggregation, config resolvers, capacity planners, discovery,
fine-tuning, agent chat, the rollup Lambda itself.

**Classifier:** *what triggered the invocation*, not what it queried.
Doc-arrived → data plane. User-triggered / scheduled / admin →
control plane.

**Storage:** `control_plane_hourly` table:

```sql
CREATE EXTERNAL TABLE control_plane_hourly (
  hour_ts             timestamp,
  function_name       string,
  component           string,     -- 'monitor-dashboard', 'monitor-agent',
                                  -- 'test-runner', 'test-results',
                                  -- 'analytics-agent', 'rollup-lambda', etc.
  bedrock_model       string,     -- nullable; only for Bedrock invocations
  invocations         bigint,
  duration_ms_sum     bigint,
  athena_bytes_sum    bigint,
  bedrock_tokens_in   bigint,
  bedrock_tokens_out  bigint,
  est_lambda_cost     double,
  est_athena_cost     double,
  est_bedrock_cost    double
)
PARTITIONED BY (date string, hour string)
```

Cardinality: ~20 control-plane Lambdas × few components × few models
= ~50-100 rows/hour, ~40K rows/month. No `control_plane_daily` — the
hourly table is small enough that a daily rollup on top would be
pure overhead.

**Population — same rollup Lambda that writes the metering rollups.**
Every hour, for the sealed hour N-1:

1. Discover control-plane Lambda ARNs via
   `resourcegroupstaggingapi:GetResources` — every Lambda in the
   stack that does *not* carry `idp:plane=data` (see §10.3).
2. For each ARN, call CloudWatch `GetMetricData`:
   - `AWS/Lambda/Duration` and `Invocations` (native, all Lambdas)
   - `IDPControlPlane/AthenaBytesScanned` (custom, emitted by
     control-plane Lambdas that hit Athena)
   - `IDPControlPlane/BedrockInputTokens` / `BedrockOutputTokens`
     (custom, dimensioned by Model)
3. Multiply by pricing constants → `est_*_cost` columns.
4. Write one Parquet row per (function, component, model) for the
   sealed hour under
   `s3://reporting/control_plane_hourly/date=…/hour=…/`. Append-only.

**Component labels** — values of the `component` column:

| Category | What runs here | Typical trigger |
|---|---|---|
| `monitor-dashboard` | Dashboard resolver + Athena reads it issues | Page open in IDP Monitor UI |
| `monitor-agent` | Scheduled AI-summary agent | EventBridge hourly cron |
| `analytics-agent` | Natural-language → SQL agent, agent-chat processor | User question / chat |
| `doc-chat` | Chat-with-document + streaming chat | User chat about a specific doc |
| `test-set-mgmt` | Test set CRUD + S3 polling | Test Studio UI + on-open scan |
| `test-runner` | Kick off test runs + copy files | User clicks "Run test" |
| `test-results` | Aggregate test runs + serve dashboard reads | End-of-run + results page open |
| `config-mgmt` | Config CRUD, apply-preset custom resource | Config UI + feature installs |
| `capacity-planner` | Capacity calculation tool | User action |
| `policy-discovery` | Policy Discovery API + async processor | Admin action |
| `finetuning` | Fine-tuning job management | Admin action |
| `user-mgmt` | Cognito user CRUD + directory sync | Admin action |
| `api-dispatch` | Main HTTP API dispatcher + document status lookup | Every UI page load |
| `rollup-lambda` | The rollup Lambda itself | EventBridge cron |
| `other-control` | Fallback for Lambdas that don't match a category | — |

The mapping is heuristic (substring match on function name) in
`_component_for_function()`. Anything unmatched lands under
`other-control` — that's a signal to either extend the mapping or
recognize a new category.

### 10.3 Tagging convention + controls

**Rule (whitelist model):** only per-doc-arrival Lambdas carry
`idp:plane=data`. Everything else is *implicitly* control plane.

This inverts the naive "tag everything" approach so the maintenance
surface is tiny — data plane is a stable set; adding a new
control-plane feature (autotune, hooks, agents, etc.) requires zero
tagging work.

**Enforcement — `scripts/check_data_plane_tags.py`, wired into `make
lint` / `fastlint` / `lint-cicd`:** the linter checks that every
Lambda in the `DATA_PLANE_WHITELIST` list exists in its template AND
carries `Properties.Tags: idp:plane: data`. A rename, removal, or
missing tag fails the build. This turns a silent misattribution (the
Lambda's cost quietly falls into `other-control`) into a loud CI
failure.

**When adding a new pipeline stage:** add the Lambda's logical ID to
`DATA_PLANE_WHITELIST` in `scripts/check_data_plane_tags.py` **and**
add `Tags: idp:plane: data` in the CFN block. If the Lambda is
control plane (user/schedule/admin-triggered, not per-doc-arrival),
don't touch either.

**Stack scoping** uses the CloudFormation-native
`aws:cloudformation:stack-name` tag (present on every stack-created
resource — no custom tagging work required). No custom `idp:stack`
tag is defined or maintained.

**Untagged Lambda default at runtime:** treated as control plane —
the safe default, since we track control plane cost. If a data-plane
Lambda slips through without a tag, its cost is *misattributed* to
`other-control`, not lost — and the linter catches this in CI first.

### 10.4 Data-plane Lambda whitelist

Applied classifier: *what triggered the invocation*. If cost scales
with production doc arrival, it's data plane.

**Data-plane Lambdas** (23 total, all in `DATA_PLANE_WHITELIST`):

| Lambda | Template | Trigger |
|---|---|---|
| `OCRFunction` | `patterns/unified/template.yaml` | Doc arrival (Step Functions) |
| `ClassificationFunction` | `patterns/unified/template.yaml` | Doc arrival |
| `ExtractionFunction` | `patterns/unified/template.yaml` | Doc arrival |
| `AssessmentFunction` | `patterns/unified/template.yaml` | Doc arrival |
| `SummarizationFunction` | `patterns/unified/template.yaml` | Doc arrival |
| `EvaluationFunction` | `patterns/unified/template.yaml` | Doc arrival |
| `ProcessResultsFunction` | `patterns/unified/template.yaml` | Per-doc pipeline result stitching |
| `PipelineHooksDispatcherFunction` | `patterns/unified/template.yaml` | Sync-invoked per doc (PII, etc.) |
| `ShardRuntimeFunction` | `patterns/unified/template.yaml` | Per-doc/per-shard Bedrock batch runtime |
| `InvokeBDAFunction` | `patterns/unified/template.yaml` | Per-doc BDA invocation (BDA mode) |
| `BDAProcessResultsFunction` | `patterns/unified/template.yaml` | Per-doc BDA result parsing |
| `BDACompletionFunction` | `patterns/unified/template.yaml` | Per-doc BDA completion |
| `RuleValidationFunction` | `patterns/unified/template.yaml` | Per-doc rule validation |
| `RuleValidationOrchestrationFunction` | `patterns/unified/template.yaml` | Per-doc orchestration |
| `RuleValidationPolicyClassificationFunction` | `patterns/unified/template.yaml` | Per-doc policy classification |
| `WorkflowTracker` | `template.yaml` | SF state change per doc |
| `QueueSender` | `template.yaml` | S3 upload event per doc |
| `QueueProcessor` | `template.yaml` | SQS batch trigger from doc queue |
| `BatchPreProcessorFunction` | `template.yaml` | Jobs API batch ingest |
| `JobTracker` | `template.yaml` | SQS per-doc status-change events |
| `SaveReportingDataFunctionV2` | `template.yaml` | Async per doc (Evaluation / RuleValidation) |
| `PostProcessingDecompressor` | `template.yaml` | SQS per-doc custom post-processor dispatcher |
| `CompleteSectionReviewFunction` | `template.yaml` | HITL callback — resumes paused Step Function once per doc that needs review |

**Explicitly NOT data plane (in the same templates):**
`TestExecutionAggregationFunction` (post-run orchestration),
`MLflowLoggerFunction` (per-run write-up),
`CodeBuildTrigger` / `BDAOCRProjectFunction` (one-shot CFN custom
resources), `TestFileCopierFunction` (test-run seeding — scales with
test volume, not prod docs — `test-runner` component),
`CircuitBreakerManagerFunction` (alarm/health-check),
`BackfillWorkerFunction` (admin one-shot),
`FinetuningProcessDocumentFunction` (training-set processing),
`DataMartRollupFunction` (this rollup itself), and all API resolvers /
chat / auth / admin functions.

**Test-run cost split:** kickoff, file-copy, aggregation, and MLflow
write-up are control plane (`test-runner` + `test-results`
components). The actual document processing that runs against test
docs — OCR, Extraction, etc. — goes through the same data-plane
Lambdas as production docs and appears under Data Plane Cost.

### 10.5 Custom-metric emission — what each component measures

The rollup Lambda queries CloudWatch for three metric types per
control-plane Lambda:

| Metric | Emitted by | How |
|---|---|---|
| `AWS/Lambda/Duration` + `Invocations` | Native (all Lambdas) | Zero code — CloudWatch emits automatically. |
| `IDPControlPlane/AthenaBytesScanned` (dims: `Component`, `FunctionName`) | Any control-plane Lambda that runs an Athena query | Emit at end of `athena.get_query_execution`, one `PutMetricData` call with `bytes_scanned` value. |
| `IDPControlPlane/BedrockInputTokens` / `BedrockOutputTokens` (dims: `Component`, `FunctionName`, `Model`) | Any control-plane Lambda that calls Bedrock | Emit at end of every `converse` / `invoke_model` response with the token counts from the response envelope. |

`FunctionName` is load-bearing — the rollup Lambda scopes its
`GetMetricData` calls on `FunctionName` alone (not `Component`) so a
Lambda whose emitter-side hardcoded component label differs from the
rollup-side derived label (e.g. a Lambda that vendors the analytics
agent — emitted as `analytics-agent` but mapped to a different
component by `_COMPONENT_RULES`) still has its metrics found. The
`Component` and `Model` dims are informational — useful for direct
CloudWatch queries by an operator but not required by the rollup.

Both custom metrics are one-line calls to a shared helper —
`idp_common.metrics.emit_control_plane_cost_metric(...)` — that
callers get for free via `idp_common`. Fire-and-forget; logs a
warning (never raises) on CloudWatch failure so the calling Lambda's
business logic keeps working. `FunctionName` is auto-populated from
`AWS_LAMBDA_FUNCTION_NAME` — callers pass only `component` (and
`bedrock_model` for Bedrock metrics).

**Phase 1 emitter coverage.** As of the initial Phase 1 landing, the
only in-repo caller of the helper is the analytics agent's Athena tool
(`lib/idp_common_pkg/idp_common/agents/analytics/tools/athena_tool.py`),
which emits `AthenaBytesScanned`. **`BedrockInputTokens` /
`BedrockOutputTokens` are not yet emitted by any in-repo Lambda** —
the rollup Lambda reads them if present, but until the agent-chat /
monitor-agent / test-runner Lambdas start emitting, the corresponding
columns in `control_plane_hourly` will be `0` and `est_bedrock_cost`
will always be `$0` for control-plane rows. Wiring Bedrock emission
is a Phase 2 task tracked separately.

---

## Consumer contract

Consumers of these tables (idp-monitor, in-tree analytics agents,
future dashboards):

- **Read-only.** The rollup Lambda is the sole writer. Never `INSERT`
  from a consumer.
- **Pick the tier** by requested range (see §2). Live tail from raw
  `metering` for the current hour/day is expected.
- **Assume append-only.** A partition, once written, never changes.
  Athena result reuse is safe for sealed partitions — enable it
  explicitly on each `StartQueryExecution` via
  `ResultReuseConfiguration.ResultReuseByAgeConfiguration` (it's off
  by default on the `primary` workgroup this pipeline uses).
- **Freshness:** hourly rollup for hour N lands at N+1:05 UTC. Daily
  rollup for day D lands at D+1 00:15 UTC. Consumers can display "as
  of HH:05" for hourly and "as of 00:15" for daily.
- **Schema drift:** columns may be added; existing columns never
  removed or retyped without a version bump on the table name.
