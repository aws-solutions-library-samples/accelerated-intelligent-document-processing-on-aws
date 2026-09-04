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

Six new Athena/Glue tables and one scheduled rollup Lambda; no
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
| 6 | `data_plane_lambda_hourly` | hour × function_name × component | Same rollup Lambda, writes Parquet from CloudWatch `AWS/Lambda` Duration/Invocations for `idp:plane=data` Lambdas (Lambda compute cost only — Bedrock/Textract API costs are already in `metering_hourly` per-doc, so they are omitted here to avoid double-count) |

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

**All six tables are append-only.** A partition is written once and
never rewritten. The write-time partitioning of `metering` (see §2.3)
means metering rows never land in past partitions, so no
`INSERT OVERWRITE` / trailing-window / Iceberg complexity is needed.

**Consumer tier picker.** Consumers pick the cheapest sufficient table
by requested range **and by the metric they're asking for**. Cost
(`sum_value`/`sum_cost`) is on `metering_*`; volume (`n_docs`/`sum_pages`)
is on `metering_docs_*`:

| Requested range | Cost tier | Volume tier (docs, pages) | Live tail (current partial bucket) |
|---|---|---|---|
| `< 2h` | raw `metering` (partition-pruned to hour) | raw `metering` (`COUNT(DISTINCT document_id)`, `MAX(number_of_pages)` per doc) | n/a — whole window is "current" |
| `2h – 24h` | `metering_hourly` | `metering_docs_hourly` | raw `metering` for the current hour |
| `> 24h` | `metering_daily` | `metering_docs_daily` (note: `n_docs` is a doc-hours count at day grain — see §2 for the strict-unique-doc pattern) | `metering_hourly` / `metering_docs_hourly` for the current day |

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

A query spanning the cutover mixes both semantics silently. At the
`hour` grain this affects any document whose processing crossed an
hour boundary, not just midnight — a document queued at 15:58 and
completing at 16:04 lands in the queue-derived `hour=15` bucket
before the cutover and the completion-derived `hour=16` bucket after
it. At the `date` grain the mismatch reduces to docs that crossed
midnight during processing. If you need the cutover boundary
programmatically, look at the earliest `hour_ts` in
`metering_hourly` — that's the point new-semantic rows start
appearing.

**Upgrade ordering — why the writer updates first.** The migration takes a
one-shot snapshot of the old-layout key list. Anything written at
`metering/date=X/*.parquet` *after* that listing but *before* the writer starts
emitting the new layout is unreachable by the new `date=X/hour=HH/` projection
— not an error, just silently absent from `metering` and every rollup built on
it. `SaveReportingDataFunctionV2` is the sole writer of `metering/` parquet, so
`MeteringHourMigrationCustomResource` declares
`DependsOn: SaveReportingDataFunctionV2` and the full ordering is:

```
SaveReportingDataFunctionV2  →  migration custom resource  →  MeteringTable
       (new layout live)            (relocates history)       (new projection)
```

**Residual window, and what to do about it.** Lambda finishes in-flight
invocations on the *previous* code version, so a container that started just
before the writer updated can still emit one old-layout file after the
migration listed. That shrinks the exposure from the whole stack update (tens
of minutes) to roughly one invocation duration — but it is not zero. If your
deployment ingests continuously and you care about complete cost history:

1. Quiesce ingestion for the update (stop uploading to the input bucket), **or**
2. after `UPDATE_COMPLETE`, re-run the standalone migration, which is
   idempotent and picks up exactly these stragglers:

```bash
python scripts/migrate_metering_hour_partition.py --bucket <reporting-bucket> --dry-run
python scripts/migrate_metering_hour_partition.py --bucket <reporting-bucket>
```

To check whether any straggler exists at all, list for old-layout keys — a
metering parquet whose path has no `hour=` segment:

```bash
aws s3 ls s3://<reporting-bucket>/metering/ --recursive \
  | grep '\.parquet$' | grep -v '/hour='
```

---

## Rollup Lambda

`DataMartRollupFunction` (`src/lambda/data_mart_rollup/index.py`).
Two EventBridge schedules dispatch based on the `mode` field:

- `{"mode": "hourly"}` — every hour at :05 UTC. Writes
  `metering_hourly`, `metering_docs_hourly`, and `control_plane_hourly`
  for the previous sealed hour.
- `{"mode": "daily"}` — every day at 00:15 UTC. Writes `metering_daily`
  and `metering_docs_daily` for the previous sealed day, reading from
  the corresponding hourly tables.

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

1. Discover control-plane Lambda ARNs via a two-stage walk:
   first `cloudformation:ListStackResources` on the root stack and
   its nested-stack children (`_enumerate_stack_tree` — needed
   because nested-stack Lambdas carry the nested stack's
   `aws:cloudformation:stack-name` tag, not the root's, and a
   pure-tag query on the root name would miss most of the fleet),
   then `resourcegroupstaggingapi:GetResources` filtered on the
   discovered stack names (`_get_resources_by_tag`). Every Lambda
   in the stack tree that does *not* carry `idp:plane=data` (see
   §10.3) is treated as control-plane.
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
| `chat-orchestrator` | Chat companion's top-level orchestrator agent (routes user prompts to specialised sub-agents) | User chat |
| `error-analyzer` | Chat companion's error-analysis agent | User question about a failed doc |
| `code-intelligence` | Chat companion's code-intelligence agent (DeepWiki MCP) | User question about the codebase |
| `quick-start` | Chat companion's quick-start / bootstrap agent | User setup flow |
| `external-mcp` | Chat companion's user-registered external MCP agent | User invokes an installed MCP server |
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
| `agent-core` | AgentCore MCP handler + gateway manager | Agent runtime invocation |
| `blueprint-optimization` | LLM-driven blueprint (schema) optimizer | Admin action |
| `circuit-breaker` | Bedrock throttle backpressure manager | Throttle event |
| `version-check` | Version-check resolver | Every UI page load |
| `multi-doc-discovery` | Multi-doc discovery orchestration | Admin batch tool |
| `rollup-lambda` | The rollup Lambda itself | EventBridge cron |
| `other-control` | Fallback for Lambdas that don't match a category | — |

**Data-plane component labels** — round-24 UI polish. Data-plane rows
in `data_plane_lambda_hourly` are labeled with the pipeline stage
they belong to (the mapping matches on the stage name embedded in
the CFN-generated Lambda ID):

| Category | Stage / Lambda |
|---|---|
| `ocr` | Textract OCR stage |
| `classification` | Document classifier |
| `extraction` | Field extraction (traditional or agentic) |
| `assessment` | Extraction confidence assessment |
| `summarization` | Document summarizer |
| `evaluation` | Per-doc evaluation against baseline |
| `rule-validation` | Rule-based validation + policy classification |
| `process-results` | Post-processing / results aggregation |
| `shard-runtime` | Shard runtime (agentic extraction fan-out) |
| `save-reporting` | `SaveReportingDataFunctionV2` |
| `workflow-tracker` | Step-Functions workflow state tracker |
| `queue-processor` | Doc-arrival queue processor |
| `queue-sender` | S3-event → SQS forwarder |
| `pipeline-hooks` | Pipeline-hooks dispatcher |
| `bda` | Bedrock Data Automation invocation, completion, project-setup |
| `batch-ingest` | Jobs API batch ingest (zip extract → input bucket) |
| `job-tracker` | Per-doc status-change events on the Jobs API path |
| `post-processing` | Dispatcher for the user-supplied custom post-processor |
| `hitl-review` | HITL section-review completion callback |

The mapping is heuristic (substring match on function name) in
`_component_for_function()`. Anything unmatched lands under
`other-control`.

For a **data-plane** row, `other-control` is always a bug — the Lambda carries
`idp:plane=data`, so a label saying "control" contradicts the table it sits in.
`scripts/tests/test_data_plane_component_labels.py` pins the mapping against
`DATA_PLANE_ALLOWLIST` (see the Component column in §10.4) so a new pipeline
stage cannot be added without a label, and a rule reordering cannot silently
move a stage between buckets. Rule **order** matters — first match wins, so a
broader pattern placed above a narrower one steals the label; the BDA and
`rule-validation` rules sit above the pipeline stages for exactly that reason
(`BDAProcessResultsFunction` contains `processresultsfunction`, and
`RuleValidationPolicyClassificationFunction` contains
`classificationfunction`).

### 10.3 Tagging convention + controls

**Rule (allowlist model):** only per-doc-arrival Lambdas carry
`idp:plane=data`. Everything else is *implicitly* control plane.

This inverts the naive "tag everything" approach so the maintenance
surface is tiny — data plane is a stable set; adding a new
control-plane feature (autotune, hooks, agents, etc.) requires zero
tagging work.

**Enforcement — `scripts/check_data_plane_tags.py`, wired into `make
lint` / `fastlint` / `lint-cicd`:** the linter checks that every
Lambda in the `DATA_PLANE_ALLOWLIST` list exists in its template AND
carries `Properties.Tags: idp:plane: data`. A rename, removal, or
missing tag fails the build. This turns a silent misattribution (the
Lambda's cost quietly falls into `other-control`) into a loud CI
failure.

**When adding a new pipeline stage:** add the Lambda's logical ID to
`DATA_PLANE_ALLOWLIST` in `scripts/check_data_plane_tags.py` **and**
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

### 10.4 Data-plane Lambda allowlist

Applied classifier: *what triggered the invocation*. If cost scales
with production doc arrival, it's data plane.

**Data-plane Lambdas** (all in `DATA_PLANE_ALLOWLIST`). The **Component**
column is the `component` value its `data_plane_lambda_hourly` rows carry.
`scripts/tests/test_data_plane_component_labels.py` enforces the allowlist ↔
label mapping against `_component_for_function`, but it does so via its own
`EXPECTED_COMPONENT` map — **nothing parses this table**, so keep the rules, that
map, and this column in step by hand when adding a stage.

⚠️ **Rule literals must stay at or under 24 characters.** Lambda function names
cap at 64 chars and CloudFormation truncates the *logical-ID* segment to fit, so
`_component_for_function` receives a prefix of the logical ID on any stack whose
name is long enough. A longer literal matches the full logical ID — and so
passes a naive test — while matching nothing a real deployment emits.

| Lambda | Template | Component | Trigger |
|---|---|---|---|
| `OCRFunction` | `patterns/unified/template.yaml` | `ocr` | Doc arrival (Step Functions) |
| `ClassificationFunction` | `patterns/unified/template.yaml` | `classification` | Doc arrival |
| `ExtractionFunction` | `patterns/unified/template.yaml` | `extraction` | Doc arrival |
| `AssessmentFunction` | `patterns/unified/template.yaml` | `assessment` | Doc arrival |
| `SummarizationFunction` | `patterns/unified/template.yaml` | `summarization` | Doc arrival |
| `EvaluationFunction` | `patterns/unified/template.yaml` | `evaluation` | Doc arrival |
| `ProcessResultsFunction` | `patterns/unified/template.yaml` | `process-results` | Per-doc pipeline result stitching |
| `PipelineHooksDispatcherFunction` | `patterns/unified/template.yaml` | `pipeline-hooks` | Sync-invoked per doc (PII, etc.) |
| `ShardRuntimeFunction` | `patterns/unified/template.yaml` | `shard-runtime` | Per-doc/per-shard Bedrock batch runtime |
| `InvokeBDAFunction` | `patterns/unified/template.yaml` | `bda` | Per-doc BDA invocation (BDA mode) |
| `BDAProcessResultsFunction` | `patterns/unified/template.yaml` | `bda` | Per-doc BDA result parsing |
| `BDACompletionFunction` | `patterns/unified/template.yaml` | `bda` | Per-doc BDA completion |
| `RuleValidationFunction` | `patterns/unified/template.yaml` | `rule-validation` | Per-doc rule validation |
| `RuleValidationOrchestrationFunction` | `patterns/unified/template.yaml` | `rule-validation` | Per-doc orchestration |
| `RuleValidationPolicyClassificationFunction` | `patterns/unified/template.yaml` | `rule-validation` | Per-doc policy classification |
| `WorkflowTracker` | `template.yaml` | `workflow-tracker` | SF state change per doc |
| `QueueSender` | `template.yaml` | `queue-sender` | S3 upload event per doc |
| `QueueProcessor` | `template.yaml` | `queue-processor` | SQS batch trigger from doc queue |
| `BatchPreProcessorFunction` | `template.yaml` | `batch-ingest` | Jobs API batch ingest |
| `JobTracker` | `template.yaml` | `job-tracker` | SQS per-doc status-change events |
| `SaveReportingDataFunctionV2` | `template.yaml` | `save-reporting` | Async per doc (Evaluation / RuleValidation) |
| `PostProcessingDecompressor` | `template.yaml` | `post-processing` | SQS per-doc custom post-processor dispatcher |
| `CompleteSectionReviewFunction` | `template.yaml` | `hitl-review` | HITL callback — resumes paused Step Function once per doc that needs review |

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

**Phase 1 emitter coverage** (`AthenaBytesScanned` only). Three
in-repo call sites: the analytics agent's Athena tool
(`lib/idp_common_pkg/idp_common/agents/analytics/tools/athena_tool.py`),
the test-results resolver
(`nested/api-resolvers/src/lambda/test_results_resolver/index.py`),
and the rollup Lambda's own self-cost accounting
(`src/lambda/data_mart_rollup/index.py`).

**Phase 2 emitter coverage** (`BedrockInputTokens` /
`BedrockOutputTokens`). Wired via a reusable Strands
`ControlPlaneCostHook`
(`lib/idp_common_pkg/idp_common/agents/common/cost_metrics.py`) that
registers on `AfterInvocationEvent`, reads the agent's cumulative
`event_loop_metrics.accumulated_usage`, and emits the per-invocation
delta with the concrete `bedrock_model` used at inference time.
Registered on all six in-tree Strands agents through the
`with_cost_hook(hooks, component, model_id)` one-liner in the same
module — the analytics chat agent (`analytics-agent`), chat
orchestrator (`chat-orchestrator`), error analyzer (`error-analyzer`),
code-intelligence agent (`code-intelligence`), quick-start agent
(`quick-start`), and external MCP agent (`external-mcp`). The
marketplace idp-monitor's `monitor-agent` picks up the same hook via
its own repo. The hook is asymmetric-regression-safe (one counter
dropping doesn't re-emit the other from scratch), thread-safe (state
update guarded by an internal lock so concurrent `stream_async` calls
against a reused agent don't tear the delta), and zero-side-skipping
(a 0-valued CloudWatch datum is never emitted). **Latent gap**: the
hook doesn't yet read `cacheReadInputTokens` / `cacheWriteInputTokens`
— no in-repo agent has prompt caching enabled today, so the counters
stay accurate until the first one does; wiring cache tokens is a
tracked follow-up.

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

---

## Phase 2 — upcoming features planned for future work

> **Status: proposed / planned, not committed.** This section lists
> features and integrations that are **candidates for future PRs**, not
> guarantees. Scope, timing, and priority will be decided per-item as
> work is picked up. Nothing here is in flight today.

Phase 1 (this doc's subject) delivered **infrastructure and correctness**:
the five rollup tables, the scheduled Lambda that populates them, the
migration custom resource, the tagging model, write-time partitioning
on raw metering. Phase 2 is the follow-on that delivers **integration**
(consumers reading the tables) and **the data types Phase 1 deliberately
deferred**.

Tracked as four parallel workstreams. Ordering within a track is
dependency-sensitive; tracks are independent.

### Track A — new tables (data Phase 1 had nowhere to put)

- **`document_lifecycle`** — one row per document with `status`
  (`SUCCESS` / `FAILED` / `ABORTED` / `REDACTED_SUPERSEDED`), pipeline
  `stage`, `error_message`, `queued_at`, `completed_at`, `duration_ms`,
  `config_version`. Written by `WorkflowTracker` on every state
  transition (currently DynamoDB-only). Enables true success/failure
  rates, latency percentiles, and stage-level failure attribution —
  today's `get_volume_metrics` hardcodes `failed: 0, success_rate: 1.0`
  because status data doesn't exist in Athena.
- **`document_confidence`** — aggregate confidence-alert queries. Today
  the confidence data is visible only in the per-doc UI; Phase 2 makes
  "docs with confidence drops in the last week" a single query.
- **`latency_hourly`** — pre-bucketed p50/p90/p99 histograms per
  `(hour, service_api)`. Current dashboard latency comes from X-Ray +
  a 500-doc tracking-DDB sample; Phase 2 lifts that to a single-row
  scan at any range.
- **`document_class` in the metering grain** — extends
  `metering_hourly` / `metering_daily` grain from
  `(hour, config_version, service_api, unit)` to
  `(hour, document_class, config_version, service_api, unit)`. Blocked
  on the classification service emitting `document_class` into
  metering rows (write-side change).

### Track B — consumer integration (make Phase 1's tables actually pay off)

- **Analytics chat agent** — extend `schema_provider.py` to describe
  all 5 rollup tables to the LLM. Add tier-picker guidance in the
  prompt (`<2h → raw`, `2-24h → hourly`, `>24h → daily`). Add
  cost-vs-docs split guidance: never `SELECT sum_pages FROM metering_hourly`;
  always route volume/pages to `metering_docs_*`. Roughly half a day
  of prompt work + testing.
- **`analytics_cost_service.py`** — route wide-range queries through
  the tier picker rather than always scanning raw metering.
- **Marketplace `idp-monitor` dashboard** (external repo, the primary
  intended consumer) — full tier picker in the dashboard resolver,
  read volume/pages from `metering_docs_*`, read per-Lambda cost from
  `control_plane_hourly`, fall back to raw metering for the current-hour
  tail, set `ResultReuseConfiguration` explicitly on every
  `StartQueryExecution`.

### Track C — producers (metrics with no emitter today)

- ✅ **Bedrock token emission** from control-plane Lambdas — **SHIPPED**.
  Implemented via `ControlPlaneCostHook` in
  `lib/idp_common_pkg/idp_common/agents/common/cost_metrics.py`,
  wired on all six in-tree Strands agents (`analytics-agent`,
  `chat-orchestrator`, `error-analyzer`, `code-intelligence`,
  `quick-start`, `external-mcp`) plus the marketplace idp-monitor's
  `monitor-agent`. See §10.5 for the emission mechanics. Verified
  live on `idp-dev-fix`: a chat session produced `bedrock_tokens_in
  = 203,255` and `est_bedrock_cost = $0.2519` in the next `:05`
  rollup — first time this column has been non-zero on that stack.
- **`config_library/pricing.yaml` integration.** `BEDROCK_PRICING`
  is currently hardcoded in `data_mart_rollup/index.py` and drifts on
  every new Bedrock model — this is the class-of-bug that produced the
  1000× overstate reviewer #2 caught. `pricing.yaml` is the canonical
  unit-price source used by the data plane. Blocked on factoring out a
  lightweight `read_bedrock_prices()` — the current reader is coupled
  to `IDPConfig` init that the rollup Lambda doesn't hold. Once fixed,
  kills the drift by construction.

### Track D — operational hardening

- **Backfill mode for the rollup Lambda.** If the Lambda misses a
  period (like `idp-dev-qs` did after a redeploy dropped it),
  partitions for those hours are just missing — the Lambda only ever
  processes the immediately-previous hour. Phase 2 adds a
  `{"mode":"backfill","start":"…","end":"…"}` payload that iterates
  the range and calls the hourly logic per hour, using the same
  idempotency guard so reruns are safe.
- **Alarm on rollup absence.** The develop redeploy that dropped
  `DataMartRollupFunction` from `idp-dev-qs` was invisible until an
  operator noticed missing partitions. Phase 2 adds a CloudWatch alarm
  on `AWS/Lambda/Invocations == 0 for 2 hours` → page the operator.
- **Rollup-Lambda layer swap-in-place guard.** Both the rollup and the
  migration Lambdas now use `IDPCommonReportingLayer` for pyarrow +
  `idp_common`. External customers install this feature via
  CreateFunction (new resource in this release) so the layer attaches
  cleanly under the 250 MB "combined code + layers" limit. A *future*
  release that swaps the rollup Lambda's runtime shape in-place
  (bundled ↔ layer, or a much larger layer) risks the same
  update-stack size trap seen once during dev iteration — CFN's
  `UpdateFunctionConfiguration` call validates old-code + new-layer
  before `UpdateFunctionCode` runs. If Phase 2 changes the layer
  materially, deliver the swap via a custom resource that
  delete-then-recreates the function rather than an in-place
  configuration update.
- **`GetResources` tag-filter chunking.** `TagFilters[].Values` caps at
  25; the stack tree today is 6, but a deep future topology would
  throw `ValidationException` rather than degrade. Trivial fix, low
  urgency.
- **Doc updates going forward.** Any Phase 2 table added must include
  a doc-drift lint check that fails if the doc's grain description
  doesn't match the actual `PartitionKeys` and `GROUP BY` in the SQL —
  reviewer #2 caught two "doc says X, SQL says Y" mismatches.

### Suggested sequencing (indicative)

These groupings show a natural dependency order if the work is picked
up; they aren't a commitment to ship in any particular window.

- **2a — marketplace repo:** dashboard tier picker + result-reuse
  configuration. Highest-value item because it turns the KB-vs-GB scan
  win from theoretical to real for the primary consumer.
- **2b — this repo:** analytics agent integration, Bedrock emission
  wiring, `pricing.yaml` refactor.
- **2c — this repo:** `document_lifecycle` + `document_confidence`
  tables. Bigger scope, needs its own design doc before implementation.
- **2d — cross-repo:** latency histograms + `document_class` in the
  metering grain. Needs classification service change first.

### What Phase 2 explicitly does NOT do

- **Change any Phase 1 table shape.** Adding columns is allowed;
  removing or renaming is not without a version bump on the table
  name.
- **Re-aggregate historical data.** Phase 2 tables written after
  their deploy contain data from that point forward only. Backfilling
  a lifecycle table for months of pre-Phase-2 tracking-DDB history is
  out of scope.
- **Bring feature-platform extensions into the stack-tree walk.**
  Extensions deployed as separate top-level stacks stay invisible to
  `_discover_control_plane_lambdas` by design — an extension author who
  wants cost visibility must register with the reporting pipeline
  explicitly.
