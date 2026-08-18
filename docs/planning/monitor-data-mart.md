# IDP Monitor Data Mart — Phase 1 Query Design

**Status:** Draft
**Owner:** Taniya Mathur
**Reviewers:** (fill in)
**Home:** This doc lives in the **main IDP repo** (`docs/planning/`)
because Phase 1's implementation — the two new Athena rollup tables and
the scheduled rollup Lambda — is built here. Marketplace repo owners
consuming this mart read the same doc for the consumer-side contract
(§3–§11).

**Repo naming convention used throughout this doc:**
- **main IDP repo** — `genaiic-idp-accelerator` (this repo). Owns raw
  `metering`, the two new rollup tables (`metering_hourly`,
  `metering_daily`), the scheduled rollup Lambda, and the Athena
  workgroup. All Phase 1 implementation lands here.
- **marketplace repo** — `idp-marketplace-features`. Owns the
  `idp-monitor` feature — dashboard code, tier picker, Monitor Cost
  KPI, scheduled AI-summary agent. Pure reader of the data mart; ships
  after Phase 1.

**Related:** MR #34 (marketplace repo) established the current fetch
flow that this design supersedes.

**Assumption:** private per-user config versions are being removed in the
same release (or before). The Monitor operates on a single shared config;
all users see the same AI summary. This simplifies the scheduled agent
cache (one row per range, no per-user variants) and the config UI (no
version list, no activate button). If the private-config removal slips,
we'd re-introduce a `versionId` check on the cache read path — noted in
§11 and §12.

---

## 1. Problem

The IDP Monitor dashboard runs live Athena aggregations over the raw
`metering` table on every page load. For wide time ranges (`7d`, `30d`)
this means scanning GBs of Parquet, takes 15–90 s, and often hits API
Gateway's 29 s ceiling → `503`.

The bottleneck is very specific: **additive aggregations** (SUM/COUNT of
cost, tokens, pages, docs) over wide ranges scan the whole time window
of the raw metering table. Every other dashboard widget is either
already fast (column-scoped point queries against raw metering, or
CloudWatch / DynamoDB / X-Ray reads) or on a data source Athena doesn't
own. This design targets the additive-on-wide-range case only.

## 2. Phase 1 scope — the minimum change that fixes the bug

**Three new tables in the main IDP repo. One hourly rollup Lambda.
Nothing else.**

| # | Table | Grain | Populated by |
|---|---|---|---|
| 1 | `metering_hourly` | hour × document_class × config_version × service_api | Scheduled hourly rollup Lambda, `INSERT INTO` from raw `metering` |
| 2 | `metering_daily` | date × document_class × config_version × service_api | Same rollup Lambda, one `INSERT INTO` per hour |
| 3 | `control_plane_hourly` | hour × function_name × component × bedrock_model | Same rollup Lambda, from CloudWatch (see §10.2) |

Plus one schema extension: **`metering` is partitioned by write time**
(= document completion time, since `save_reporting_data` runs at the
end of the workflow) instead of `initial_event_time`. Every metering
row lands in the current partition — see §2.3 for the semantic
consequence.

**Aggregated columns on the two metering rollup tables:** `n_docs,
sum_cost, sum_pages, sum_input_tokens, sum_output_tokens`. No per-doc
columns, no status, no timing. Rollup dimensions are chosen to answer
every `GROUP BY` clause the additive widgets need — see §3 for the
routing matrix.

**Rollup writes are pure append-only.** Rows are written when an hour
seals. No trailing re-materialization window, no `INSERT OVERWRITE`,
no late-arrival concerns — the write-time partitioning of `metering`
means metering rows never land in past partitions.

### 2.1 Why three tables and not more (nor fewer)

**Principle we're committing to:** we build SQL tables when a specific
slow query proves that materialization is the right lever. We do not
build them because "a unified SQL layer" feels architecturally clean.
Every widget the current dashboard renders either (a) benefits from
these rollup tables via the tier picker (§3), or (b) already reads
from a data source that answers its query pattern at acceptable
latency — raw metering for column-scoped point queries, X-Ray for
per-service latency percentiles, CloudWatch for throttles and capacity,
tracking DynamoDB for live-processing state and confidence alerts.
Widget-by-widget rationale, with an explicit "why not SQL rollup" for
each non-rollup path, is in §3.

If a future widget proves that an additional rollup materialization is
the right lever — a specific slow query, measured on a live stack — the
rollup Lambda's job manifest becomes one line longer and no consumer
needs to change. Deferring costs us nothing; building speculative
tables costs us 2× the operational surface (writer, backfill,
migration, contract with consumers) to reason about for tables no
current widget queries.

### 2.3 `metering` partitioning: write time, not queue time

Today `save_reporting_data.py` partitions metering rows by
`document.initial_event_time` — the moment the document was queued.
A doc queued at 10:00 and finished at 14:30 lands its metering row in
`date=today, hour=10` at 14:30, in the past relative to wall clock.

Phase 1 changes this to write time (= completion time, since
`save_reporting_data` runs at workflow end):

- Every metering row lands in the current partition. No time-travel
  into past partitions.
- Rollups become trivially append-only.
- Dashboard time filters ("last 24h") match user intent — "docs
  completed in last 24h" — since completion = write time.
- `initial_event_time` is preserved as a column on the row for anyone
  who needs queue-time semantics.

**Semantic consequence for consumers of raw `metering`:** the
predicate `WHERE date = '2026-08-18'` shifts meaning from "docs queued
that day" to "docs completed that day". For the dashboard and the
in-tree analytics agent (only two consumers today), "completed that
day" is the more useful interpretation. Consumers who need
queue-time semantics filter on the `initial_event_time` column
explicitly.

### 2.2 Approach

Rewrite `MonitoringMetricsService` so each dashboard section picks the
cheapest sufficient tier of the pipeline's data mart:

| Requested range | Middle-of-window tier | Live tail (current partial bucket) |
|---|---|---|
| `< 2h` | `metering` (partition-pruned to hour) | n/a — whole window is "current" |
| `2h – 24h` | `metering_hourly` | `metering` for the current hour |
| `> 24h` | `metering_daily` | `metering_hourly` for the current day |

Sealed hour and day rows never change once written, so any query hitting
them benefits from Athena result reuse — a second dashboard-open within
7 days finds the same underlying SQL cached at the workgroup layer
(the workgroup is owned by the main IDP repo).

Live processing status, throttling counts, HITL, and confidence alerts
stay on their current data sources (tracking DDB and CloudWatch). Those
are sub-second-live signals where Athena's cold-query latency isn't the
right fit. Widget-by-widget rationale in §3.

## 3. Widget-to-data-source routing (and why)

Every dashboard widget lands in one of four buckets. The rollup tables
serve the *first* bucket only; everything else keeps its current data
source. The rationale column is the answer to "why isn't this on the
new SQL rollup?" for each non-rollup widget — write it down here so
the "unified SQL layer" question doesn't come back per widget in
future reviews.

### 3.1 Widgets served by the new SQL rollup

Additive aggregations (SUM/COUNT) over long time ranges. These are
the queries that time out today.

| Widget | Query shape | Tier hit |
|---|---|---|
| KPI Cards (total cost, tokens, pages, docs) | `SUM(sum_cost), SUM(sum_pages), SUM(n_docs)` | tier picker (§2.2) |
| Processing Volume (time series) | `SELECT hour_ts, SUM(n_docs) GROUP BY hour_ts` | tier picker |
| Document Types (distribution) | `GROUP BY document_class` | tier picker |
| Config Versions (distribution) | `GROUP BY config_version` | tier picker |
| Cost by Pipeline Stage | `GROUP BY service_api` | tier picker |

**Why SQL rollup:** all five follow the same pattern — additive
aggregation over an arbitrarily wide time window. Materializing hourly
+ daily sums makes a 30-day query touch ~100 K rollup rows instead of
~30 GB of raw metering.

### 3.2 Widgets that stay on raw `metering`

Non-additive queries — either point-in-time lookups or per-doc
rankings. Column-scoped scans against raw `metering` are already cheap
because Athena skips the columns the query doesn't reference.

| Widget | Query shape | Why not rollup |
|---|---|---|
| Recent Failures list | `SELECT … ORDER BY ts DESC LIMIT N` | Rollup is by definition an aggregate; it discards the per-doc rows needed to list individual failures. Raw metering with `LIMIT N` is O(N), not O(range). |
| Top-N Expensive Docs | `GROUP BY document_id ORDER BY SUM(cost) DESC LIMIT N` | The `GROUP BY document_id` grain is what the rollup tables deliberately drop; adding a per-doc rollup would 100×–1000× the row count for a query that's already column-scoped and finishes in <5 s. Revisit only if measurement proves this is a hot spot. |

**Why not SQL rollup:** these queries need per-document grain. Any
rollup that preserved per-doc grain would essentially be a copy of the
raw table with a partition change — not a materialization win.

### 3.3 Widgets on non-Athena sources (unchanged)

Data sources Athena doesn't own, and where moving to SQL would either
lose fidelity or add infrastructure for no query-side win.

| Widget | Source | Why not SQL |
|---|---|---|
| Processing Speed (p50/p90/p99 latency) | X-Ray + tracking DDB fallback | X-Ray gives per-service latency breakdown (Bedrock vs. Textract vs. States) which SQL rollup over end-to-end doc timing can't reproduce. Falling back to per-doc metering blobs in the tracking DDB when X-Ray has no data (`_supplement_latency_from_metering`, 500-doc sample cap). Cold-query Athena latency (~1 s) is worse than either. |
| Service Performance (throttles) | CloudWatch `GetMetricData` | CloudWatch is authoritative for throttle counts, retains 15 months by default, and answers in ~200 ms. Mirroring to S3 via Metric Streams + Firehose would add a paid AWS pipeline for data we can query where it lives. |
| Workflow Capacity (concurrency) | Tracking DDB | Concurrency is *now* state — the count of in-flight documents at this instant. A rollup would give the count as-of-last-hour, which is wrong for the widget's purpose. DDB point-read: <20 ms. |
| Human Review (HITL) queue | Tracking DDB | Same as above — it's a live queue, not a historical aggregate. |
| Confidence Alerts | Document sections DDB GSI | Current widget lists currently-open alerts; that's a point query the GSI answers in <50 ms. Historical aggregation over confidence data is not a query anything asks today. |
| Live Processing (currently-running docs) | Tracking DDB | Live state. Same rationale as concurrency. |

### 3.4 AI Summary widget

| Widget | Data path | Notes |
|---|---|---|
| AI Summary | Composes §3.1 widgets + scheduled agent cache (§11) | Underlying data comes from the tier picker for the common precomputed ranges (24h, 7d). Cached by an hourly agent as prose text so first-open renders in ~50 ms from DDB. Uncommon ranges fall back to on-demand Bedrock generation. |

### 3.5 Framing

The rollup tables solve **exactly one class of problem**: additive
aggregation over wide time windows. Every widget in §3.2–§3.4 has a
data source that is already the right fit for its query pattern.
Building rollup tables to consolidate them into "one SQL layer" would
move fast queries off their optimal source and onto Athena's cold-query
latency floor.

The "unified SQL data layer" direction is worth revisiting
**per-widget, on evidence**: if any widget in §3.2–§3.4 shows measurable
dashboard-tail latency in Phase 1's observability metrics (§10), that
widget graduates to a rollup in Phase 2. Speculative migration ahead
of that evidence is what §2.1 rejects.

## 4. Tier selection

One shared helper. Every section calls it — no per-section policy
divergence.

```python
# idp_common_ext_pkg/idp_common_ext/monitoring/tier_selection.py

TIER_HOURLY_HOURS = 2        # windows below this stay on raw metering
TIER_DAILY_HOURS = 24        # windows above this move to daily rollup

def pick_tier(time_range: TimeRange) -> Tier:
    hours = time_range.width_hours()
    if hours < TIER_HOURLY_HOURS:
        return Tier.METERING
    if hours < TIER_DAILY_HOURS:
        return Tier.METERING_HOURLY
    return Tier.METERING_DAILY
```

Both thresholds are code constants for v1. Post-launch telemetry can
inform whether to expose them as config or leave hardcoded. Not
user-facing knobs.

## 5. Query pattern per section

Every additive section follows the same pattern: **middle from the
chosen tier, live tail from the next-finer source.**

### Example — `get_volume_metrics` for `7d`

```python
def get_volume_metrics(time_range: TimeRange) -> dict:
    tier = pick_tier(time_range)         # → METERING_DAILY

    middle_end = floor_to_boundary(now, tier)     # midnight today
    tail_start = middle_end                        # cover today so far

    middle = _athena("""
        SELECT SUM(n_docs) AS docs, SUM(n_pages) AS pages, SUM(n_failures) AS fails
        FROM metering_daily
        WHERE date BETWEEN :start AND :middle_end_date
    """, ...)

    tail = _athena("""
        SELECT SUM(n_docs), SUM(n_pages), SUM(n_failures)
        FROM metering_hourly
        WHERE hour_ts >= :tail_start
    """, ...)

    return {
        "totalDocuments": middle.docs + tail.docs,
        "totalPages":     middle.pages + tail.pages,
        "failedDocuments": middle.fails + tail.fails,
    }
```

### Example — `get_volume_metrics` for `4h`

```python
def get_volume_metrics(time_range: TimeRange) -> dict:
    tier = pick_tier(time_range)         # → METERING_HOURLY

    middle_end = floor_to_boundary(now, tier)     # top of current hour
    tail_start = middle_end

    middle = _athena("""
        SELECT SUM(n_docs), SUM(n_pages), SUM(n_failures)
        FROM metering_hourly
        WHERE hour_ts BETWEEN :start AND :middle_end
    """, ...)

    tail = _athena("""
        SELECT COUNT(DISTINCT document_id) AS docs,
               SUM(CASE WHEN unit='pages' THEN value END) AS pages,
               SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS fails
        FROM metering
        WHERE date = :today AND hour = :current_hour
    """, ...)

    return { ... sum ... }
```

The tail scan is bounded by the tier granularity — max one hour of raw
`metering` for the 2-24h path, max one day of `_hourly` for the
>24h path. Both are cheap regardless of the requested window's width.

### Result reuse hits the middle

Sealed hour rows and sealed day rows never change. Result reuse on the
Athena workgroup (owned by the main IDP repo) caches the middle
query's result for up to 7 days. Two users opening `7d` five minutes
apart both hit the same cached middle result — the second query pays
only for the tail scan (~a few MB).

## 6. Sliding window — why this is enough

Every 5 minutes, a new completion may land in the current hour. That
completion invalidates every range's total. In the tiered design:

- **Middle query is unchanged** — sealed hour/day rows don't move.
  Result reuse cache stays valid across the invalidation.
- **Tail query re-runs each dashboard load** — it always covers the
  current partial hour (2-24h path) or current partial day (>24h
  path). Fresh every load.

No "add x, remove y" bookkeeping in the marketplace repo. The sliding
boundary is a query-time bucket selection over immutable tier data.

## 7. Freshness display

Freshness is now a property of the tail's coverage. Every dashboard
response includes:

- `dataAsOf`: wall-clock timestamp of the query, or the pipeline's
  most-recent-write timestamp for the tail source (whichever the tail
  query used).

Banner:

> Data as of **`<dataAsOf>`**

For 2-24h ranges the tail hits raw `metering`, so freshness is bounded
by how quickly the pipeline commits metering rows (seconds). For >24h
ranges the tail hits `_hourly`, so freshness is bounded by the
pipeline's write cadence for that table (should also be seconds since
it's write-time per doc, but coordinate with the main IDP repo on the
actual SLO).

## 8. Component-level changes

Only marketplace repo changes. Changes in the main IDP repo (schema,
writers, partitions, workgroup) are covered separately in that repo's
scope.

| Component | File(s) | Change |
|---|---|---|
| Tier picker | `idp_common_ext_pkg/…/tier_selection.py` (new) | Single helper `pick_tier(time_range)`. Two code constants for thresholds. |
| Metrics service | `idp_common_ext_pkg/…/monitoring_metrics_service.py` and section-level services | Rewrite `get_volume_metrics`, `get_cost_metrics`, `get_cost_by_version`, `get_cost_by_document_type`, `get_per_stage_cost`, `get_doc_type_distribution`, `get_volume_over_time`, `get_config_version_distribution` to use middle+tail against the chosen tier. Latency + top-N + failures + throttles paths unchanged. |
| Query window helper | `idp_common_ext_pkg/…/query_windows.py` (new) | `floor_to_boundary(dt, tier) → dt`, `split_window(range, tier) → (middle, tail)`. |
| Athena client | `idp_common_ext_pkg/…/analytics_athena_service.py` | Verify queries reach the shared workgroup so result reuse applies. Add `WorkGroup` and `ResultReuseConfiguration` if not already set. |
| Response schema | UI + backend | Add `dataAsOf` field to the dashboard response. |
| Freshness banner | `feature-ui/src/components/monitoring/MonitoringLayout.tsx` | Show "Data as of `<dataAsOf>`" in the layout header. |
| Monitor cost KPI | `feature-ui/…/KPICardsWidget.tsx` + backend `_compute_monitor_cost` | New KPI card, $ amount hyperlinked to Cost Explorer, pre-filtered by resource tag + date range. See §10. |
| Metric emission | `feature-api/handler.py` | Emit `AthenaBytesScanned` (with `Source=dashboard-read` dimension), `BedrockInputTokens`, `BedrockOutputTokens` after every relevant SDK call. |
| Tests | `tests/test_tier_selection.py`, `tests/test_query_windows.py`, `tests/test_metering_service.py` | Tier picker returns expected tier per range. Split window covers window fully. Section-level services produce correct results against mocked tiered queries. |

**No new AWS resources in the marketplace repo.** No cache table, no
new tables, no scheduled Lambda for data. The main IDP repo owns all
of that. The marketplace repo ships pure code changes plus two new KPI
cards (data plane + control plane cost).

## 9. Sequences

### 9.1 User request — `7d` at `t = 14:23`

```
handler → parse timeRange="7d"
       → tier = METERING_DAILY
       → middle_end = midnight today (00:00)
       → tail_start = 00:00 today

per additive section:
   middle: SUM over metering_daily WHERE date BETWEEN t-7d AND midnight today
   tail:   SUM over metering_hourly WHERE hour_ts >= 00:00 today

per non-additive section:
   raw column-scoped scan against metering (unchanged from today)

response.dataAsOf = now
freshness banner: "Data as of 14:23"
```

Typical scan cost: **middle ~7 rows per dimension in `metering_daily`**
(< 10 KB), **tail ~14 rows per dimension in `_hourly`** (< 30 KB).
Plus whatever the non-additive raw scans cost (200-500 MB column-scoped).

Result reuse: the middle query for `t-7d → midnight today` is byte-
identical to any other user's `7d` query in the same wall-clock day.
Second user pays only for the tail.

### 9.2 User request — `4h` at `t = 14:23`

```
handler → tier = METERING_HOURLY
       → middle_end = 14:00 (top of current hour)
       → tail_start = 14:00

per additive section:
   middle: SUM over metering_hourly WHERE hour_ts BETWEEN 10:00 AND 14:00
   tail:   SUM over metering WHERE date=today AND hour=14

Freshness bounded by pipeline's raw metering write cadence.
```

### 9.3 User request — `30m` at `t = 14:23`

```
handler → tier = METERING (whole window is "current")
       → no middle/tail split

per additive section:
   SUM over metering WHERE date=today AND hour=14 (partition-pruned)
```

Cost of a 30-minute query is now O(one hour of metering), not O(one day)
as today. Partition pruning on `hour` is doing the work.

## 10. Cost observability — data plane vs control plane

The dashboard surfaces two independent cost KPIs so operators can
distinguish "what did processing documents cost me" from "what is the
IDP infrastructure itself costing me while idle or serving my UI".

### 10.1 Data plane cost

**Definition:** any AWS spend attributable to processing a specific
document. Lambda invocations of OCR / Classification / Extraction /
Assessment / Summarization / Evaluation. Bedrock tokens on the
extraction path. Textract calls.

**Source:** the existing `metering` table (per-doc rows), rolled up
into `metering_hourly` and `metering_daily`. Already surfaced today by
the dashboard's cost widgets — Phase 1 does not change this beyond
making the rollup queries faster.

**Dashboard KPI:** "Data Plane Cost" (or the existing "Total Cost" tile,
renamed for clarity).

### 10.2 Control plane cost

**Definition:** every other IDP AWS spend that runs regardless of
whether documents are processing. Concretely:

- IDP Monitor dashboard resolver (per page open)
- IDP Monitor scheduled AI-summary agent (hourly Bedrock calls)
- IDP Monitor natural-language agent queries (per user question)
- Test Studio periodic scan for new test sets
- Test Studio test-run aggregation Lambda
- Config resolvers, capacity calculators, discovery processors
- The hourly rollup Lambda itself (self-consistent)
- Athena bytes scanned by any dashboard or agent
- Bedrock tokens consumed by any non-document-processing Lambda

**Classifier:** *what triggered the invocation*, not what it queried.
Doc-arrived → data plane. User-triggered / scheduled / admin → control
plane.

**Storage:** `control_plane_hourly` table:

```sql
CREATE EXTERNAL TABLE control_plane_hourly (
  hour_ts             timestamp,
  function_name       string,
  component           string,             -- 'monitor-dashboard', 'monitor-agent',
                                          -- 'test-studio-poll', 'test-run-aggregation',
                                          -- 'analytics-agent', 'rollup-lambda', etc.
  bedrock_model       string,             -- nullable; only for Bedrock-using invocations
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

Typical cardinality: ~20 control-plane Lambdas × few components ×
few models = ~50-100 rows/hour, ~40K rows/month. Cheap to scan at any
dashboard range. **No `control_plane_daily`** — hourly rollup is small
enough that a daily rollup on top would be pure overhead.

**Population — same rollup Lambda that writes the metering rollups.**
Every hour, for the sealed hour N-1:

1. Discover control-plane Lambda ARNs via `resourcegroupstaggingapi:
   GetResources` — any Lambda in the stack that does *not* carry
   `idp:plane=data` (see §10.3).
2. For each ARN, call CloudWatch `GetMetricData`:
   - `AWS/Lambda/Duration` (Sum + SampleCount)
   - `IDPControlPlane/AthenaBytesScanned` (Sum) — custom metric emitted
     by control-plane Lambdas that hit Athena.
   - `IDPControlPlane/BedrockInputTokens` / `BedrockOutputTokens`
     (Sum, dimensioned by Model) — custom metric emitted by
     control-plane Lambdas that hit Bedrock.
3. Multiply by pricing constants → `est_*_cost` columns.
4. `INSERT INTO control_plane_hourly` — one row per
   (function, component, model) for the sealed hour. Append-only.

**Custom-metric emission (in-band):** control-plane Lambdas that hit
Athena or Bedrock emit `IDPControlPlane/*` custom metrics at the end
of their invocation. Lambda `Duration` comes from CloudWatch natively.

**Cost categories (values of the `component` column on
`control_plane_hourly`):**

| Category | What runs here | Typical trigger | Expected primary cost driver |
|---|---|---|---|
| `monitor-dashboard` | Dashboard resolver + Athena reads it issues | Page open in IDP Monitor UI | Athena bytes + Lambda time |
| `monitor-agent` | Scheduled AI-summary agent (hourly Bedrock synth) | EventBridge hourly cron | Bedrock tokens |
| `analytics-agent` | Natural-language → SQL agent | User question in chat | Bedrock + Athena |
| `test-set-mgmt` | Test set CRUD + S3 polling for new sets | Test Studio UI + on-open scan | Lambda time |
| `test-runner` | Kick off test runs + copy files | User clicks "Run test" | Lambda time |
| `test-results` | Aggregate test runs + serve dashboard reads | End-of-run + results page open | Lambda + Athena |
| `config-mgmt` | Config CRUD, apply-preset custom resource | Config UI + feature installs | Lambda time |
| `capacity-planner` | Capacity calculation tool | User action | Lambda time |
| `policy-discovery` | Policy Discovery API + async processor | Admin action | Bedrock + Lambda |
| `finetuning` | Fine-tuning job management | Admin action | Lambda time |
| `agent-chat` | User chat message handling | User messages | Bedrock + Lambda |
| `rollup-lambda` | The hourly rollup Lambda itself | EventBridge cron | Athena (tiny) + Lambda |

Each category is one Component label on the rollup rows. The set is
fixed at code-time (Lambdas set their own `Component` label when they
emit metrics); new categories are added deliberately, not discovered
at runtime.

**Dashboard KPI:** "Control Plane Cost" tile beside "Data Plane Cost".
Dollar value hyperlinks to Cost Explorer, pre-filtered by
`idp:plane=control` tag + current time range (Cost Explorer needs
24-48h to index new tags — first-day tile shows "Cost breakdown
available ~24h after deployment").

**Expandable drill-down under the tile:** table sorted by cost
descending, one row per `component`. Query is a single `GROUP BY
component` against `control_plane_hourly` filtered on the current
range. Illustrative shape:

```
Control Plane Cost — $3.42 (24h)                    [Cost Explorer ↗]
──────────────────────────────────────────────────────────────────────
Component                Cost      Invocations   Duration   Breakdown
──────────────────────────────────────────────────────────────────────
monitor-agent           $2.10     24            6.2 min    Bedrock: $2.05
                                                           Lambda:  $0.05
monitor-dashboard       $0.85    142            3.1 min    Athena:  $0.68
                                                           Lambda:  $0.17
analytics-agent         $0.28     12            1.4 min    Bedrock: $0.22
                                                           Lambda:  $0.06
test-runner             $0.10      3            4.1 min    Lambda:  $0.10
test-run-aggregation    $0.05      3            0.8 min    Athena:  $0.03
                                                           Lambda:  $0.02
config-resolver         $0.02     45            0.4 min    Lambda:  $0.02
test-set-poll           $0.02    288            1.8 min    Lambda:  $0.02
rollup-lambda           <$0.01    24            0.3 min    Athena:  <$0.01
                                                           Lambda:  <$0.01
──────────────────────────────────────────────────────────────────────
```

Clicking a component row expands one further level — per-`function_name`
breakdown within that component (some components map to multiple
Lambdas, e.g. `monitor-dashboard` includes the resolver Lambda plus
downstream test-results / test-runs resolvers).

**Why `component` as the primary grouping, not `function_name`:** a
single feature usually spans multiple Lambdas — Test Studio's
per-component numbers are easier to reason about than a flat list of
15 test-related Lambda ARNs. `function_name` is the second-level
drill-down for anyone who needs to identify a specific function.

### 10.3 Tagging convention + controls

**Rule (whitelist model):** only the ~6-8 data-plane Lambdas carry
`idp:plane=data`. Everything else is *implicitly* control plane. This
inverts the naive "tag everything" approach so the maintenance surface
is tiny — data plane is a small, stable set; adding a new control-plane
feature (autotune, hooks, agents, etc.) requires zero tagging work.

**Data plane whitelist** (main IDP repo — all live in
`patterns/unified/template.yaml`):
- `OCRFunction`
- `ClassificationFunction`
- `ExtractionFunction`
- `AssessmentFunction`
- `SummarizationFunction`
- `EvaluationFunction`
- `WorkflowTrackerFunction`

Marketplace repo has zero data-plane Lambdas — the IDP Monitor is
control plane in its entirety.

**Two-tier control** for keeping the whitelist accurate as the
codebase evolves:

| Stage | Control | What it catches |
|---|---|---|
| PR opened with new Lambda in `patterns/unified/template.yaml` | `make lint` fails without the `idp:plane=data` tag | 95% of "forgot to tag data plane" cases — new pipeline stage additions land here by convention |
| Runtime (hourly) | Rollup Lambda WARN-logs any Lambda under `patterns/unified/` that lacks the tag | Drift: data-plane Lambdas added *outside* `patterns/unified/`, hotfixes / console changes bypassing CFN, or PR-time gaps |

The location-based linter (~15 lines Python, wired into `make lint`
and `make lint-cicd`) is scoped to `patterns/unified/template.yaml`
only — every `AWS::Lambda::Function` in that file must have the tag.
No linter friction anywhere else, because everywhere else defaults
correctly to control plane.

**Untagged Lambda default at runtime:** treated as control plane —
the safe default, since we track control plane cost. If a data-plane
Lambda slips through without a tag, its cost is *misattributed* to
control plane (visible as a "Control Plane Cost" anomaly), not lost.

**Cost Explorer deep links** filter on the CloudFormation-native
`aws:cloudformation:stack-name` tag (present on every stack-created
resource — no custom tagging work required) plus the `idp:plane` tag.
The rollup Lambda uses the same `aws:cloudformation:stack-name` filter
via `resourcegroupstaggingapi:GetResources` to discover its stack's
Lambdas, so no custom `idp:stack` tag is defined or maintained by this
design.

### 10.4 Concrete classification of existing Lambdas

Applied classifier: *what triggered the invocation*, not what it
queried. The full stack (both repos) classifies as follows:

| Lambda / feature area | Trigger | Plane | Component label |
|---|---|---|---|
| `OCRFunction` | Doc arrival (Step Functions) | data | *(not applicable — data plane isn't broken out by component)* |
| `ClassificationFunction` | Doc arrival | data | — |
| `ExtractionFunction` | Doc arrival | data | — |
| `AssessmentFunction` | Doc arrival | data | — |
| `SummarizationFunction` | Doc arrival | data | — |
| `EvaluationFunction` | Doc arrival | data | — |
| `ProcessResultsFunction` | Per-doc pipeline result stitching | data | — |
| `PipelineHooksDispatcherFunction` | Sync-invoked per doc from pipeline (PII, etc.) | data | — |
| `ShardRuntimeFunction` | Per-doc (or per-shard) Bedrock batch runtime | data | — |
| `InvokeBDAFunction` | Per-doc BDA invocation (BDA mode only) | data | — |
| `BDAProcessResultsFunction` | Per-doc BDA result parsing | data | — |
| `BDACompletionFunction` | Per-doc BDA completion signal | data | — |
| `RuleValidationFunction` | Per-doc rule validation | data | — |
| `RuleValidationOrchestrationFunction` | Per-doc rule-validation orchestration | data | — |
| `RuleValidationPolicyClassificationFunction` | Per-doc policy classification | data | — |
| `WorkflowTracker` | Step Functions state change per doc | data | — |
| `QueueSender` | S3 upload event per doc | data | — |
| `QueueProcessor` | SQS batch trigger from doc queue | data | — |
| `BatchPreProcessorFunction` | Jobs API batch ingest (per-file scaling) | data | — |
| `JobTracker` | SQS per-doc status-change events (Jobs API) | data | — |
| `SaveReportingDataFunctionV2` | Async per doc (from Evaluation / RuleValidation) | data | — |
| `PostProcessingDecompressor` | SQS per-doc dispatcher for custom post-processor | data | — |
| `TestSetResolverFunction` | REST from Test Studio UI (`createTestSet`, `getTestSets`, etc.) | control | `test-set-mgmt` |
| Test-set S3 polling (inside `getTestSets`) | Scheduled + on-demand from UI open | control | `test-set-mgmt` |
| `TestRunnerFunction` | User clicks "Run test" | control | `test-runner` |
| `TestFileCopierFunction` | SQS-triggered by test runner; seeds test-set → input bucket. Cost scales with **test volume**, not prod doc arrival. | control | `test-runner` |
| `TestResultsResolverFunction` | Dashboard read + SQS cache-update processing | control | `test-results` |
| `TestExecutionAggregationFunction` | Invoked at end of a test run | control | `test-results` |
| `MLflowLoggerFunction` | Async on test-run completion | control | `test-results` |
| `ConfigurationResolverFunction` | Config CRUD from Config UI | control | `config-mgmt` |
| `ApplyFeatureConfigPresetFunction` | Feature-install CFN custom resource | control | `config-mgmt` |
| `MonitoringMetricsService` (marketplace) | Dashboard read | control | `monitor-dashboard` |
| Scheduled AI-summary agent (marketplace) | EventBridge hourly cron | control | `monitor-agent` |
| Natural-language analytics agent | Dashboard user query | control | `analytics-agent` |
| `CapacityCalculatorResolverFunction` | User action | control | `capacity-planner` |
| `PolicyDiscoveryResolverFunction`, `DiscoveryProcessorFunction` | User/admin action | control | `policy-discovery` |
| `FinetuningJobsResolverFunction` | User action | control | `finetuning` |
| `AgentChatProcessorFunction` | User chat message | control | `agent-chat` |
| The hourly rollup Lambda itself | EventBridge cron | control | `rollup-lambda` |

**How a test run's cost splits under this classification:** kickoff /
file-copy / aggregation / MLflow write-up are control plane
(`test-runner` + `test-results` components). The **actual document
processing that runs against test docs** — OCR, Extraction, etc. —
goes through the same data-plane Lambdas as production docs, so it
appears under Data Plane Cost. See the "Test-doc cost separation"
open question in §13 if we ever want to break that out separately.

### 10.5 Custom-metric emission — what each component measures

The rollup Lambda queries CloudWatch for three metric types per
control-plane Lambda:

| Metric | Emitted by | How |
|---|---|---|
| `AWS/Lambda/Duration` | Native (all Lambdas) | Zero code — CloudWatch emits automatically. |
| `IDPControlPlane/AthenaBytesScanned` (dim `Component`) | Any control-plane Lambda that runs an Athena query | Emit at end of `athena.get_query_execution`, one `PutMetricData` call with `bytes_scanned` value. |
| `IDPControlPlane/BedrockInputTokens` / `BedrockOutputTokens` (dim `Component`, `Model`) | Any control-plane Lambda that calls Bedrock | Emit at end of every `converse` / `invoke_model` response with the token counts from the response envelope. |

Both custom metrics are one-line calls to a shared helper
(`idp_common.observability.emit_control_plane_cost_metric(...)`) that
callers get for free via `idp_common`. No per-Lambda glue code beyond
importing the helper.

## 11. Scheduled agent — AI output caching

Sits **on top of** the SQL data mart. Not a data layer — just persists
slow LLM outputs so users don't wait 5-90 s for Bedrock on every
dashboard open. Fits the Quick / Claude CoWork "scheduled agent tasks"
pattern: an agent runs on a schedule, writes persisted outputs, users
consume them without waiting.

### 11.1 What gets cached, and why not the raw payload

**Cache these:**
- AI Summary text for the precomputed ranges (`24h`, `7d`)
- Optional: daily briefing tile (§11.6)

**Do NOT cache the raw dashboard payload.** The tier-picked SQL layer
already returns it in 100-300 ms with result reuse. Caching that in DDB
would save maybe 50 ms and re-introduce the invalidation gymnastics we
rejected in Appendix A.

### 11.2 Storage

Reuse `MonitorConfigurationTable` with a new key prefix (same pattern
as `analyticsjob#*`, `userActive#*`). No new AWS resource.

**AI Summary cache item:**

```json
{
  "versionId": "agentOutput#summary#24h",
  "range": "24h",
  "summaryText": "...prose bullets from the LLM...",
  "generatedAt": "2026-08-11T14:00:00Z",
  "generatedFromBucketTs": "2026-08-11T13:00:00Z",
  "expiresAt": 1234567890
}
```

Overwrite each tick — history is not valuable for the summary cache.
TTL is `generatedAt + 24h` so a disabled/broken agent doesn't leave
stale rows around forever.

**Daily briefing item (if we build §11.6):**

```json
{
  "versionId": "agentOutput#briefing#2026-08-11",
  "date": "2026-08-11",
  "briefingText": "...what changed vs. previous day...",
  "highlights": [ ... structured findings ... ],
  "generatedAt": "2026-08-11T06:00:00Z",
  "expiresAt": 1234567890
}
```

One row per calendar day, TTL of 30 days. Users may look back at
yesterday's briefing.

### 11.3 Hourly agent flow

```
Every hour (EventBridge → agent Lambda):
  for range in ["24h", "7d"]:
    dashboard = MonitoringMetricsService.get_dashboard(range)   # cheap via SQL tier

    cached = get_item(f"agentOutput#summary#{range}")
    if cached and cached.generatedFromBucketTs == dashboard.lastSealedBucketTs:
      continue                                # data hasn't advanced; skip Bedrock call

    prompt = build_summary_prompt(dashboard, config.ai.summaryPrompt)
    summary = bedrock_converse(prompt, config.ai.modelId)   # ~5-15s

    put_item(f"agentOutput#summary#{range}", {
      summaryText: summary.text,
      generatedAt: now,
      generatedFromBucketTs: dashboard.lastSealedBucketTs,
    })
```

**Cost bounded.** Worst case (both ranges fully change every hour, all
day): 48 Bedrock calls/day × ~$0.03 = ~$1.50/day. Realistic hit rate on
the "data didn't advance" skip probably brings this to $0.50-1/day.

### 11.4 Dashboard-open read flow

Two parallel reads on page load:

```
User opens /dashboard?range=24h
   │
   ├─ Fetch dashboard data (SQL layer, tier-picked, ~100-300ms)
   │     → renders KPIs, charts, distributions immediately
   │
   └─ Fetch AI summary (DDB point-read, ~50ms)
        Read agentOutput#summary#24h
        │
        ├─ Exists? → render cached summary instantly
        │            banner: "AI summary refreshed 14:00 (auto)"
        │
        └─ Range not in the precomputed set (e.g. 4h, 30d)?
             → show "Generating…" progress bar
             → call Bedrock live (~5-15s, current path unchanged)
```

Everyone sees the same summary — no branching on user identity because
private config is gone.

### 11.5 Invalidation

- **Summary prompt edit** — on save, delete all `agentOutput#summary#*`
  rows. Next hourly tick regenerates with the new prompt. Users see
  "generating…" for the gap.
- **Cache older than 24h** — treated as missing at read time; falls
  through to live generation. Guards against a stuck agent leaving
  users on a day-old summary.
- **Bedrock rate-limited** — agent logs warning, skips tick. Cache row
  from last successful run remains.

### 11.6 Daily briefing (proposed for v1 or v2 — decision needed)

Different pattern from the hourly summary. Focuses on **what changed**,
not state:

```
Daily at 6am (EventBridge → briefing agent):
  today_data      = get_dashboard("24h")
  yesterday_data  = get_dashboard_for_date(yesterday)
  prev_day_data   = get_dashboard_for_date(yesterday - 1)

  prompt = build_briefing_prompt(today_data, yesterday_data, prev_day_data)
  briefing = bedrock_converse(prompt, config.ai.modelId)

  put_item(f"agentOutput#briefing#{today}", { briefingText, highlights, ... })
```

**UI:** new "Overnight brief" section at the top of the dashboard when
today's briefing exists. Dismissable. Briefing history accessible via
a "briefing history" link → simple list view of last 30 days.

**Decision needed:** ship briefing in v1 alongside summary caching, or
defer to v2? Pros of shipping now — same Lambda + DDB pattern, marginal
extra cost (~$0.50/day), compelling "scheduled agent" story. Pros of
deferring — UI complexity (new section, dismissal state, history view),
and briefing utility should be validated with usage data before
committing to it.

### 11.7 Cost envelope

- Hourly summary agent (both ranges): **~$0.50-1.50/day** Bedrock
- Daily briefing agent (if enabled): **~$0.50/day** Bedrock
- Agent Lambda compute: **negligible** (a few invocations/hour × ~30s each)

All appears in the Monitor Cost KPI card (§10) so operators see the
cost of the automation in the same tile as everything else Monitor is
spending.

## 12. Rollout

**Prerequisite:** the main IDP repo lands first with the hour-partitioned
`metering`, `metering_hourly`, `metering_daily`, and result reuse
enabled. History backfilled. Deploy sequence coordinates on that.

Marketplace repo rollout in two small stages:

**Stage 1 — SQL tier rewrite + observability**
1. Tier-selection helper and rewrite of additive section services to use
   middle+tail against the chosen tier. Non-additive sections unchanged.
2. Freshness banner and Monitor Cost KPI card.
3. Metric emission (Athena bytes with `Tier` dimension) for observability.
4. Verify on internal stack: p50/p95 dashboard latency, Athena bytes/day
   per user-view, `dataAsOf` correctly bounded per range.

**Stage 2 — Scheduled agent (AI summary cache)**
5. Agent Lambda + EventBridge hourly schedule.
6. DDB cache read path in `SummaryWidget`.
7. Invalidation hook on config save.
8. Verify: cached first-open < 100 ms, agent Bedrock spend within cost
   envelope, cache invalidation actually clears rows on prompt edit.

**Stage 3 (optional / decision) — Daily briefing tile**
9. Briefing agent + new UI section + history view. Only if we decide
   §11.6 ships in v1.

No coordinated release flag or transitional shim. By deploy time the
pipeline mart is live and populated; Monitor just queries it. Stage 2
+ 3 can be a separate MR after Stage 1 stabilises if that's easier to
review.

## 13. Trade-offs, open questions, risks

### Trade-offs

- **Freshness lag on all ranges = the rollup Lambda's hourly cron cadence.** Sealed hours are visible ~1 minute after the hour ends. The current-hour "tail" query against raw `metering` reads seconds-fresh data. If we need sub-hour rollup freshness later, the Lambda's schedule shortens; no design change.
- **Tier boundaries are hardcoded.** `<2h`, `2-24h`, `>24h`. If someone hits a range near a boundary and it doesn't feel snappy, we tune the constants — no config surface needed for v1.
- **Result reuse depends on identical SQL text.** Two dashboard loads for `7d` at 14:23 and 14:28 generate slightly different SQL (different `t-7d` start). Snapping the middle window to the tier boundary (midnight today) so all `7d` queries within the same day share bytes — worth doing, see §9.1.
- **`metering.date` semantic shift** (see §2.3). Consumers of raw `metering` who relied on queue-time bucketing get a subtly different answer. The two known consumers (dashboard and analytics agent) benefit from the write-time interpretation; any future consumer needing queue-time filters on the `initial_event_time` column directly.

### Open questions

- **Test-doc cost separation.** Under the current classification, docs processed as part of a test run consume data-plane Lambdas and show up in Data Plane Cost, indistinguishable from production processing. If we later want operators to see "how much am I spending on tests vs. production" separately, two options: (a) filter by `configVersion` on the dashboard (test runs pin a distinct version — cheapest); (b) add a `run_kind='production'|'test'` column to metering + rollups. Both are Phase 2 if/when the question comes up.
- **Top-N docs on `>24h`** — could pre-aggregate per doc from `metering_hourly` (SUM by doc_id across hours) instead of raw metering. Deferred until measurement shows raw is a hot spot.
- **Ship daily briefing in v1 or v2** (§11.6)? Ready-to-build alongside the summary cache; primary tradeoff is UI complexity and validating that users want a briefing tile before committing to the surface.
- **What if private-config removal slips?** This design assumes a single shared config. If private versions still exist at deploy time, we re-introduce a `versionId` check on the cache read path so users on private versions fall through to on-demand generation. Small conditional; noted as a fallback.

### Risks

- **Result reuse doesn't fire.** Requires the marketplace repo's Athena calls to hit the main IDP repo's workgroup and pass `ResultReuseConfiguration`. Verify via CloudWatch metric `QueryPlanningTime` — result-reuse hits show ~0ms planning.
- **Non-additive raw scans could become the new hot spot.** Top-N and failures list use column-scoped raw metering scans (~200-500 MB on wide ranges). If measurement shows they're a bottleneck, options: (a) live with it — column-scoping already trims most of the cost; (b) materialize a `top_docs_daily` companion table as a follow-up.
- **Latency percentiles are on X-Ray + tracking DDB, not Athena.** X-Ray has its own retention and sampling policies. If X-Ray is silent (sampling drops the traces we need), the tracking-DDB fallback caps at a 500-doc sample. Adjust X-Ray sampling if this becomes lossy at scale.
- **Untagged data-plane Lambdas misattribute cost to control plane.** Two-tier control (linter + runtime WARN log, §10.3) closes this, but relies on data-plane Lambdas living under `patterns/unified/template.yaml`. A data-plane Lambda added elsewhere gets caught by the WARN log (once/hour) rather than at PR time.

## 14. Success metrics

Measured on internal stack after rollout:

- **`/dashboard` p50 latency** — target > 10× reduction on `24h`, > 30× on `30d`
- **`/dashboard` 5xx rate on wide ranges** — target near-zero
- **Athena bytes/day per user-view** — target > 10× reduction
- **Result reuse hit rate** — target > 60% within same wall-clock day
- **Monitor cost as % of pipeline cost** — sanity check, target < 3%
- **AI Summary first-paint (precomputed ranges)** — target < 100 ms (DDB read path). Uncommon ranges keep today's 5-15 s live path.
- **Agent cache hit rate on `24h`/`7d` opens** — target > 90% during business hours (near-100% after warm-up)

## 15. What we're NOT doing

- **Any Athena schema, writer, backfill, or workgroup change in the
  marketplace repo.** The main IDP repo owns the three rollup tables +
  rollup Lambda + workgroup config (§2, §10.2).
- **Additional rollup tables beyond the three in §2.** Every widget the
  current dashboard renders is either served by the tier picker over
  these tables (§3.1) or by an existing data source that's already fast
  for its query pattern (§3.2, §3.3). Phase 2 materializes new tables
  only if Phase 1 observability shows a specific slow widget that a
  rollup would fix — never speculatively.
- **Trailing re-materialization window / `INSERT OVERWRITE` / Iceberg
  table format.** Not needed: `metering` is partitioned by write time
  (= completion time), so rollup rows are always append-only. Plain
  Parquet + `INSERT INTO` is trivially sufficient.
- **`control_plane_daily` rollup.** `control_plane_hourly` at typical
  cardinality (~40K rows/month) is cheap enough to scan directly at
  any dashboard range.
- **Cache table in DynamoDB for dashboard payload.** Superseded by
  tiered SQL — see Appendix A.
- **Per-user AI summary variants.** Design assumes a single shared config
  (private versions removed). If we needed to re-introduce them, cache
  would key on `configVersionId` — see §11.
- **Moving widgets currently on non-Athena sources into Athena.** X-Ray
  for per-service latency, CloudWatch for throttles, tracking DDB for
  live processing / HITL / confidence alerts — each is already the
  right fit for its query pattern (§3.3). Migrating them would move
  fast queries onto Athena's cold-query latency floor for no measurable
  win.
- **CloudWatch Metric Streams / Firehose pipeline** for throttle-metric
  mirroring to S3. CloudWatch answers these queries natively; adding a
  paid streaming pipeline is infrastructure we'd have to operate for
  zero query-side win (§3.3).
- **Tagging every Lambda in the stack.** Only the small, stable set of
  data-plane processors carries `idp:plane=data` (§10.3). Everything
  else — autotune, hooks, agents, future features — is implicitly
  control plane. Zero developer friction for the common case; a small
  location-based linter guards the whitelist.

---

## Appendix A — Rejected alternatives

### A.1 DDB-backed dashboard cache (previous version of this design)

Earlier drafts proposed a `monitorCache#dashboard#<range>` DDB row per
range, refreshed on a schedule, with a KPI probe to skip work when
unchanged. Rejected because:

- It solved the dashboard performance problem specifically, not the
  underlying "SQL queries are expensive" problem. A well-designed SQL
  data mart makes every consumer fast (dashboard, agents, MCP,
  Tableau), not just this one dashboard.
- Cache invalidation is hard by definition; the cache row lies the
  moment a completion lands. The probe detects this, forces a re-scan
  — at which point we've done the full work the design was supposed to
  avoid.
- The cache row's shape (payload keyed on range) is dashboard-specific.
  A future agent that wants "cost by hour for the last 4 hours" can't
  reuse it.

### A.2 Per-range refresh frequency

Extending config to a per-range `frequencyMinutes` map. Rejected: any
completion invalidates every range simultaneously, so per-range
frequency doesn't buy correctness — it ships some ranges known-stale
for longer. Half-measure.

### A.3 Running-total incremental cache

Maintaining a single "last-24h SUM" running total updated by
add/subtract on each new event. Rejected: every metric on the dashboard
would need its own running total; aging events out requires knowing
what to subtract; a bug in the running-total math produces
silently-wrong dashboards forever. Rollups (in the pipeline's tables)
sidestep this — each query freshly computes SUM-over-buckets from an
immutable materialized view.

### A.4 Rollup tables owned by the marketplace repo

An earlier iteration proposed the marketplace repo own the rollup
tables + writer + backfill. Rejected: the natural home is the main IDP
repo, where raw `metering` already lives. Phase 1's rollup Lambda is a
scheduled `INSERT INTO` from raw `metering` — it sits in the main IDP
repo, next to the data source it reads. The marketplace repo as a pure
reader is a cleaner boundary.

### A.5 API Gateway response caching

Cache TTL is per-endpoint, doesn't know about `timeRange`. Would need
custom header work and still doesn't help the first-user's Athena scan
cost. Result reuse at the workgroup layer covers the "duplicate concurrent
queries" case natively.

### A.6 Client-side localStorage cache

Helps a repeat visit by the same user; does nothing for cross-user
sharing or backend load. Doesn't fix the 503 problem for the first-open
case. Existing browser AI-summary cache remains for on-demand summary
regeneration on private configs; nothing else uses it.
