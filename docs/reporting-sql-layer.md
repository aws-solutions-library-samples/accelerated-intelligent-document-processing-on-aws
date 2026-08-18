# Reporting SQL Layer — rollup tables on top of `metering`

**Status:** Shipped (Phase 1).
**Owner:** Taniya Mathur.

A SQL layer on top of the raw `metering` Parquet lake that lets
consumers (idp-monitor, in-tree analytics agents, and any future
dashboard/report) answer "additive-over-wide-range" cost and volume
questions in KB-scale scans instead of GB-scale.

Three new Athena/Glue tables and one scheduled rollup Lambda; no
other infrastructure. This doc is the reference for the shape of the
tables, the partitioning contract, and the tagging model that drives
control-plane cost attribution.

---

## 2. Tables

| # | Table | Grain | Populated by |
|---|---|---|---|
| 1 | `metering_hourly` | hour × document_class × config_version × service_api | Scheduled hourly rollup Lambda, Athena `INSERT INTO` from raw `metering` |
| 2 | `metering_daily` | date × document_class × config_version × service_api | Same rollup Lambda, `INSERT INTO` from `metering_hourly` |
| 3 | `control_plane_hourly` | hour × function_name × component × bedrock_model | Same rollup Lambda, writes Parquet directly from CloudWatch data |

**Aggregated columns on the two metering rollup tables:**
`n_doc_events, sum_value, sum_cost, sum_pages`. No per-doc columns,
no status, no timing — those queries stay on raw `metering`
(column-scoped scan is already cheap).

**Naming note:** `n_doc_events` (not `n_docs`) — a document
reprocessed across multiple hours produces one metering row per
hour it lands in, and `COUNT(DISTINCT document_id)` at hour grain
de-dupes only within the hour. Consumers who need cross-hour or
cross-day *unique* document counts must query raw `metering` with
`COUNT(DISTINCT document_id)`. Cost/token/page sums are accurate at
every grain because the underlying values are additive.

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

Sealed hour and day rows never change once written, so any consumer
`SELECT` that hits them benefits from Athena result reuse — a second
dashboard-open within the workgroup's result-reuse TTL returns
instantly. (Result reuse applies to consumer reads, not to the
rollup Lambda's `INSERT`.)

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

---

## Rollup Lambda

`DataMartRollupFunction` (`patterns/unified/src/data_mart_rollup_function/index.py`).
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
| `IDPControlPlane/AthenaBytesScanned` (dim `Component`) | Any control-plane Lambda that runs an Athena query | Emit at end of `athena.get_query_execution`, one `PutMetricData` call with `bytes_scanned` value. |
| `IDPControlPlane/BedrockInputTokens` / `BedrockOutputTokens` (dim `Component`, `Model`) | Any control-plane Lambda that calls Bedrock | Emit at end of every `converse` / `invoke_model` response with the token counts from the response envelope. |

Both custom metrics are one-line calls to a shared helper —
`idp_common.metrics.emit_control_plane_cost_metric(...)` — that
callers get for free via `idp_common`. Fire-and-forget; logs a
warning (never raises) on CloudWatch failure so the calling Lambda's
business logic keeps working.

---

## Consumer contract

Consumers of these tables (idp-monitor, in-tree analytics agents,
future dashboards):

- **Read-only.** The rollup Lambda is the sole writer. Never `INSERT`
  from a consumer.
- **Pick the tier** by requested range (see §2). Live tail from raw
  `metering` for the current hour/day is expected.
- **Assume append-only.** A partition, once written, never changes.
  Athena result reuse is safe for sealed partitions.
- **Freshness:** hourly rollup for hour N lands at N+1:05 UTC. Daily
  rollup for day D lands at D+1 00:15 UTC. Consumers can display "as
  of HH:05" for hourly and "as of 00:15" for daily.
- **Schema drift:** columns may be added; existing columns never
  removed or retyped without a version bump on the table name.
