# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Schema provider for analytics agents - generates comprehensive database descriptions.
"""

import logging
from typing import Any, Dict, Generator, Optional

from idp_common.config import get_config
from idp_common.config.models import IDPConfig
from idp_common.config.schema_constants import (
    SCHEMA_DESCRIPTION,
    SCHEMA_ITEMS,
    SCHEMA_PROPERTIES,
    SCHEMA_TYPE,
    TYPE_ARRAY,
    TYPE_OBJECT,
    X_AWS_IDP_DOCUMENT_TYPE,
)

logger = logging.getLogger(__name__)


def get_metering_table_description() -> str:
    """
    Get comprehensive description of the metering table.

    Returns:
        Detailed description of metering table schema and usage patterns
    """
    return """
## Metering Table (metering)

**Purpose**: Captures detailed usage metrics and cost information for document processing operations

**Key Usage**: Always use this table for questions about:
- Volume of documents processed
- Models used and their consumption patterns  
- Units of consumption (tokens, pages) for each processing step
- Costs and spending analysis
- Processing patterns and trends

**Important**: Each document has multiple rows in this table - one for each context/service/unit combination.

### Schema:
- `document_id` (string): Unique identifier for the document
- `context` (string): Processing context (OCR, Classification, Extraction, Assessment, Summarization, Evaluation)
- `service_api` (string): Specific API or model used (e.g., textract/analyze_document, bedrock/claude-3-sonnet)
- `unit` (string): Unit of measurement (pages, inputTokens, outputTokens, totalTokens)
- `value` (double): Quantity of the unit consumed
- `number_of_pages` (int): Number of pages in the document (replicated across all rows for same document)
- `unit_cost` (double): Cost per unit in USD
- `estimated_cost` (double): Calculated total cost (value × unit_cost)
- `timestamp` (timestamp): Document COMPLETION time — i.e., when the workflow ended and
  the row was written to metering. Not queue time.
- `initial_event_time` (timestamp): Original queue time — when the document was first
  enqueued. Populated on every row for consumers who need queue-time semantics.
- `config_version` (string): Configuration version used for processing (defaults to "default" if not specified)

**Partitioned by**: date + hour (both string, `YYYY-MM-DD` / `HH`). Partition
values reflect COMPLETION time (write time), not queue time. `WHERE date = 'X'`
means "docs completed on X", not "docs queued on X" — filter on
`initial_event_time` for queue-time semantics. Add `AND hour = 'HH'` to
partition-prune tail queries scoped to a specific hour.

### Critical Aggregation Patterns:
- **For document page counts**: Use `MAX("number_of_pages")` per document (NOT SUM, as this value is replicated)
- **For total pages across documents**: Use `SUM` of per-document MAX values:
  ```sql
  SELECT SUM(max_pages) FROM (
    SELECT "document_id", MAX("number_of_pages") as max_pages 
    FROM metering 
    GROUP BY "document_id"
  )
  ```
- **For costs**: Use `SUM("estimated_cost")` for totals, `GROUP BY "context"` for breakdowns
- **For token usage**: Use `SUM("value")` when `"unit"` IN ('inputTokens', 'outputTokens', 'totalTokens')

### Sample Queries:
```sql
-- Total documents processed (last 24 h — MUST filter on date to prune)
SELECT COUNT(DISTINCT "document_id") FROM metering
WHERE date >= date_format(current_date - interval '1' day, '%Y-%m-%d')

-- Total pages processed (correct aggregation — bound with date filter)
SELECT SUM(max_pages) FROM (
  SELECT "document_id", MAX("number_of_pages") as max_pages
  FROM metering
  WHERE date >= date_format(current_date - interval '7' day, '%Y-%m-%d')
  GROUP BY "document_id"
)

-- Cost breakdown by processing context (bounded)
SELECT "context", SUM("estimated_cost") as total_cost
FROM metering
WHERE date >= date_format(current_date - interval '7' day, '%Y-%m-%d')
GROUP BY "context"
ORDER BY total_cost DESC

-- Token usage by model (bounded)
SELECT "service_api",
       SUM(CASE WHEN "unit" = 'inputTokens' THEN "value" ELSE 0 END) as input_tokens,
       SUM(CASE WHEN "unit" = 'outputTokens' THEN "value" ELSE 0 END) as output_tokens
FROM metering
WHERE "unit" IN ('inputTokens', 'outputTokens')
  AND date >= date_format(current_date - interval '7' day, '%Y-%m-%d')
GROUP BY "service_api"

-- Cost by configuration version (bounded)
SELECT "config_version",
       SUM("estimated_cost") as total_cost,
       COUNT(DISTINCT "document_id") as document_count,
       SUM("estimated_cost") / COUNT(DISTINCT "document_id") as avg_cost_per_doc
FROM metering
WHERE date >= date_format(current_date - interval '30' day, '%Y-%m-%d')
GROUP BY "config_version"
ORDER BY total_cost DESC
```

### Prefer Rollup Tables for Wide Time Ranges

For queries spanning **> 2 hours** of history, raw `metering` is the
wrong grain — the six Phase-1 rollup tables aggregate this data hourly
or daily so a "cost this month" question scans a few hundred rows
instead of millions.

**Call `get_table_info(['metering_hourly'])`** (or `_daily`, `_docs_hourly`,
`_docs_daily`, `control_plane_hourly`, `data_plane_lambda_hourly`) for
the full column list, tier picker, cost-vs-docs split rules, and
sample SQL. That single description is the canonical source — this
metering bullet does NOT duplicate it, to prevent drift.

**Fallback rule**: for the *current* hour (before the hourly rollup
fires) OR when the question needs `document_id`-grain detail (per-doc
cost, join to sections/evaluations), you MUST query raw `metering`
with a partition filter.
"""


def get_rollup_tables_description() -> str:
    """
    Detailed description of the six Phase-1 reporting rollup tables.

    Consumers should prefer these tables over raw `metering` for any query
    spanning more than the current hour — they scan hundreds of rows
    instead of millions. See `docs/reporting-sql-layer.md`.
    """
    return """
## Reporting Rollup Tables (prefer over raw `metering` for wide ranges)

The six Phase-1 rollup tables aggregate raw `metering` and CloudWatch
data on a schedule. Use them for any question that spans more than the
current hour. Raw `metering` remains the source for the current partial
hour and per-document drilldown.

### Tier picker (which table for which range)

| Requested range | Cost tier | Volume tier (docs, pages) | Live tail |
|---|---|---|---|
| `< 2h` | raw `metering` (with `date`/`hour` filter) | raw `metering` (`MAX(number_of_pages)` per doc) | n/a |
| `2h – 24h` | `metering_hourly` | `metering_docs_hourly` | raw `metering` for current hour |
| `> 24h` | `metering_daily` | `metering_docs_daily` (see fan-out note below) | `metering_hourly` / `metering_docs_hourly` for current day |

### Cost-vs-docs column split (Phase-1)

Cost columns live on `metering_hourly` / `metering_daily`. Document
volume columns (`n_docs`, `sum_pages`) live on the SEPARATE
`metering_docs_hourly` / `metering_docs_daily` tables — grouping by
`service_api` on the volume grain would fan `number_of_pages` out by the
number of (service, unit) rows a doc touched.

- **NEVER** `SELECT n_docs FROM metering_hourly` — column does not exist.
- **NEVER** `SELECT sum_pages FROM metering_hourly` — column does not exist.
- **NEVER** `SELECT sum_cost FROM metering_docs_hourly` — column does not exist.
- **NEVER** `GROUP BY service_api` or `unit` on `metering_docs_*` — those columns don't exist.

### 1. `metering_hourly`
- **Grain**: (hour, config_version, service_api, unit)
- **Columns**: `hour_ts` (TIMESTAMP), `config_version`, `service_api`, `unit`, `sum_value`, `sum_cost`, plus partition keys `date` (VARCHAR YYYY-MM-DD) and `hour` (VARCHAR HH)
- **Meaning**: `sum_value` is a **quantity** (tokens/pages/seconds — read `unit` for the denominator). `sum_cost` is USD. ⚠️ Do NOT sum `sum_value` as dollars.
- **Partitioned by**: `date`, `hour`. **Always add a `date` (or `date` + `hour`) filter** so Athena partition-projects instead of listing every partition.
- **Freshness**: sealed hour N is written at N+1:05 UTC. **The most recent complete clock-hour is NOT yet sealed** — safe cut-off is `hour_ts < date_trunc('hour', current_timestamp) - interval '1' hour` (skip current + previous).

### 2. `metering_daily`
- **Grain**: (day, config_version, service_api, unit)
- **Columns**: `day` (DATE), `config_version`, `service_api`, `unit`, `sum_value`, `sum_cost`, plus partition key `date` (VARCHAR YYYY-MM-DD; equals `CAST(day AS VARCHAR)`)
- ⚠️ `hour_ts` does NOT exist on this table. Query `day` instead.
- **Partitioned by**: `date`. Filter on `date` (VARCHAR) for partition pruning, NOT on `day` alone.
- **Freshness**: sealed day D is written at D+1 00:15 UTC.

### 3. `metering_docs_hourly`
- **Grain**: (hour, config_version)
- **Columns**: `hour_ts` (TIMESTAMP), `config_version`, `n_docs`, `sum_pages`, plus partition keys `date` (VARCHAR YYYY-MM-DD) and `hour` (VARCHAR HH)
- **Meaning**: `n_docs = COUNT(DISTINCT document_id)` inside the hour.
- **Partitioned by**: `date`, `hour`. Same partition-filter rule as `metering_hourly`.

### 4. `metering_docs_daily`
- **Grain**: (day, config_version)
- **Columns**: `day` (DATE), `config_version`, `n_docs`, `sum_pages`, plus partition key `date` (VARCHAR YYYY-MM-DD)
- **⚠️ `n_docs` is a doc-hours count, NOT a cross-day unique-doc count** — it's
  `SUM(hourly n_docs)`, so a doc that appears in 3 hours during the day counts as 3.
  For strict unique docs across days, query raw `metering` with `COUNT(DISTINCT document_id)`.
- **Partitioned by**: `date`.

### 5. `control_plane_hourly`
- **Grain**: (hour, function_name, component, bedrock_model)
- **Columns**: `hour_ts`, `function_name`, `component`, `bedrock_model` (nullable),
  `invocations`, `duration_ms_sum`, `athena_bytes_sum`, `bedrock_tokens_in`,
  `bedrock_tokens_out`, `est_lambda_cost`, `est_athena_cost`, `est_bedrock_cost`, plus partition keys `date` (VARCHAR YYYY-MM-DD) and `hour` (VARCHAR HH)
- **Use for**: "What is IDP infrastructure costing when I'm not processing docs?"
  Broken down by `component` (`monitor-dashboard`, `monitor-agent`, `analytics-agent`,
  `doc-chat`, `test-runner`, `config-mgmt`, `rollup-lambda`, etc.).
- **Partitioned by**: `date`, `hour`.

### 6. `data_plane_lambda_hourly`
- **Grain**: (hour, function_name, component)
- **Columns**: `hour_ts`, `function_name`, `component`, `invocations`,
  `duration_ms_sum`, `est_lambda_cost`, plus partition keys `date` (VARCHAR YYYY-MM-DD) and `hour` (VARCHAR HH)
- **Use for**: Lambda compute cost of pipeline Lambdas (OCR/Classification/Extraction/etc.).
  Bedrock and Textract API cost for these Lambdas is already in `metering_hourly` per doc,
  so this table intentionally omits those to avoid double-count.
- **Combine with `control_plane_hourly` for total Lambda compute:**
  `SUM(est_lambda_cost)` over both tables.
- **Partitioned by**: `date`, `hour`.

### Note on result reuse (operator context)

Sealed rollup partitions never change, so queries against them are
safe candidates for Athena's per-query result cache — but that's a
boto3 `StartQueryExecution` parameter set by whichever consumer runs
the query, not something you (the SQL-writing agent) control. Assume
your SQL runs against fresh data; ignore this note. See
`docs/reporting-sql-layer.md` §2 for the operator/consumer side.

### Sample queries

```sql
-- Cost this week by service (fast — 7 daily rows per service)
SELECT "service_api", SUM("sum_cost") AS total_cost
FROM metering_daily
WHERE "date" >= date_format(current_date - interval '7' day, '%Y-%m-%d')
GROUP BY "service_api"
ORDER BY total_cost DESC

-- Doc-hours and page-hours by config version this month.
-- ⚠ On metering_docs_daily, n_docs is a doc-HOURS count (SUM of hourly
-- n_docs) — a doc appearing in 3 hours of a day counts as 3. For a
-- strict cross-day unique-doc count, query raw metering with
-- COUNT(DISTINCT document_id) and accept the wider scan.
SELECT "config_version", SUM("n_docs") AS doc_hours, SUM("sum_pages") AS page_hours
FROM metering_docs_daily
WHERE "date" >= date_format(current_date - interval '30' day, '%Y-%m-%d')
GROUP BY "config_version"
ORDER BY doc_hours DESC

-- Hour-of-day cost pattern (24h). Partition-pruned to yesterday+today
-- so Athena only lists the 2 date partitions this window can touch.
-- The freshness cut-off (`hour_ts < date_trunc('hour', current_timestamp)
-- - interval '1' hour`) excludes the two hours that may still be
-- unsealed: hour N-1 (rollup writes at N+1:05, so 0-5 min after each
-- clock hour it isn't yet in the table) and the current partial hour N.
-- Copying the template WITHOUT this cut-off silently returns partial
-- data during the HH:00-HH:04 window.
SELECT hour("hour_ts") AS hod, SUM("sum_cost") AS cost
FROM metering_hourly
WHERE "date" IN (
    date_format(current_date, '%Y-%m-%d'),
    date_format(current_date - interval '1' day, '%Y-%m-%d')
)
  AND "hour_ts" >= date_trunc('hour', date_add('hour', -24, current_timestamp))
  AND "hour_ts" <  date_add('hour', -1, date_trunc('hour', current_timestamp))
GROUP BY hour("hour_ts")
ORDER BY hod

-- Cost 24h — sealed hours from metering_hourly + live tail from raw metering.
-- Four subtleties baked in:
--  1. The `date` partition filter is REQUIRED on every arm, or Athena
--     enumerates every partition dir instead of pruning.
--  2. Sealed hour N is written at N+1:05 UTC — the just-completed clock
--     hour is NOT yet in metering_hourly during HH:00-HH:04. The sealed
--     CTE therefore stops one full hour before `date_trunc(hour, now)`,
--     and the raw-metering tail picks up BOTH the current partial hour
--     AND the previous (potentially-still-unsealed) hour.
--  3. Sealed upper bound and tail lower bound MUST use the same
--     expression (`date_add('hour', -1, date_trunc('hour', current_timestamp))`)
--     so the seam is exact — no overlap, no gap. Using
--     `date_add('hour', -2, current_timestamp)` for the tail creates
--     a 0-60 minute overlap with sealed (double-counts up to an hour
--     of cost). Using `date_trunc('hour', current_timestamp)` for the
--     tail creates a 0-60 minute gap during the HH:00-HH:04 window
--     when hour H-1 isn't yet in metering_hourly.
--  4. SUM over an empty scan returns NULL, and NULL + anything = NULL —
--     COALESCE both arms to 0 or the whole query returns NULL when
--     either half has no rows.
WITH sealed AS (
  SELECT COALESCE(SUM("sum_cost"), 0) AS cost
  FROM metering_hourly
  WHERE "date" IN (
      date_format(current_date, '%Y-%m-%d'),
      date_format(current_date - interval '1' day, '%Y-%m-%d')
  )
    -- Floor the lower bound to the hour so a bucket like 03:00 (which
    -- represents 03:00-04:00) isn't excluded just because
    -- date_add('hour', -24, current_timestamp) landed at 03:30. Comparing
    -- an hour-start against a non-hour-aligned time silently dropped up to
    -- 59 minutes of data at the trailing edge.
    AND "hour_ts" >= date_trunc('hour', date_add('hour', -24, current_timestamp))
    AND "hour_ts" <  date_add('hour', -1, date_trunc('hour', current_timestamp))
),
tail AS (
  -- Prune to the exact 2 `(date, hour)` partitions we need. `date IN (...)`
  -- alone would enumerate 48 hour subdirs (2 dates × 24). Trino's
  -- tuple-IN pushes both partition columns into the projection, so
  -- Athena reads at most 2 partition dirs (~80 MB) instead of ~1.9 GB.
  -- The row-level `timestamp` filter is still needed for correctness at
  -- the seam (the previous hour partition contains rows before AND
  -- after the seal boundary).
  SELECT COALESCE(SUM(CAST("estimated_cost" AS DOUBLE)), 0) AS cost
  FROM metering
  WHERE ("date", "hour") IN (
      (
          date_format(current_timestamp, '%Y-%m-%d'),
          date_format(current_timestamp, '%H')
      ),
      (
          date_format(date_add('hour', -1, current_timestamp), '%Y-%m-%d'),
          date_format(date_add('hour', -1, current_timestamp), '%H')
      )
  )
    AND "timestamp" >= date_add('hour', -1, date_trunc('hour', current_timestamp))
)
SELECT (SELECT cost FROM sealed) + (SELECT cost FROM tail) AS cost_24h

-- Control-plane cost by component last week.
-- Sum each cost column separately (not `SUM(a + b + c)`): with the
-- combined form, if any single column is NULL in a row the whole row's
-- contribution becomes NULL and that row drops out of the aggregate.
-- Three separate SUMs are NULL-safe by default.
SELECT "component",
       COALESCE(SUM("est_lambda_cost"), 0)
         + COALESCE(SUM("est_athena_cost"), 0)
         + COALESCE(SUM("est_bedrock_cost"), 0) AS total_cost
FROM control_plane_hourly
WHERE "date" >= date_format(current_date - interval '7' day, '%Y-%m-%d')
GROUP BY "component"
ORDER BY total_cost DESC

-- Total Lambda compute (data plane + control plane) last 7 days
SELECT SUM("est_lambda_cost") AS lambda_cost
FROM (
  SELECT "est_lambda_cost" FROM control_plane_hourly
  WHERE "date" >= date_format(current_date - interval '7' day, '%Y-%m-%d')
  UNION ALL
  SELECT "est_lambda_cost" FROM data_plane_lambda_hourly
  WHERE "date" >= date_format(current_date - interval '7' day, '%Y-%m-%d')
)
```
"""


def get_evaluation_tables_description() -> str:
    """
    Get comprehensive description of the evaluation tables.

    Returns:
        Detailed description of evaluation table schemas and relationships
    """
    return """
## Evaluation Tables

**Purpose**: Store accuracy metrics from comparing extracted document data against ground truth baselines

**Key Usage**: Always use these tables for questions about accuracy for documents that have ground truth data

**Important**: These tables are typically empty unless users have run separate evaluation jobs (not run by default)

### Document Evaluations Table (document_evaluations)

**Purpose**: Document-level evaluation metrics and overall accuracy scores

#### Schema:
- `document_id` (string): Unique identifier for the document
- `input_key` (string): S3 key of the input document  
- `evaluation_date` (timestamp): When the evaluation was performed
- `accuracy` (double): Overall accuracy score (0-1)
- `precision` (double): Precision score (0-1)
- `recall` (double): Recall score (0-1)
- `f1_score` (double): F1 score (0-1)
- `false_alarm_rate` (double): False alarm rate (0-1)
- `false_discovery_rate` (double): False discovery rate (0-1)
- `weighted_overall_score` (double): Weighted overall score (0-1)
- `execution_time` (double): Time taken to evaluate (seconds)
- `page_level_accuracy` (double): Page-level classification accuracy (0-1)
- `split_accuracy_without_order` (double): Document split accuracy without considering order (0-1)
- `split_accuracy_with_order` (double): Document split accuracy with order considered (0-1)
- `total_pages` (int): Total number of pages in the document
- `total_splits` (int): Total number of document splits/sections
- `correctly_classified_pages` (int): Number of pages correctly classified
- `correctly_split_without_order` (int): Number of correctly split sections (unordered)
- `correctly_split_with_order` (int): Number of correctly split sections (ordered)

**Partitioned by**: date (YYYY-MM-DD format)

### Section Evaluations Table (section_evaluations)

**Purpose**: Section-level evaluation metrics grouped by document type/classification

#### Schema:
- `document_id` (string): Unique identifier for the document
- `section_id` (string): Identifier for the section
- `section_type` (string): Type/class of the section (e.g., 'invoice', 'receipt', 'w2')
- `accuracy` (double): Section accuracy score (0-1)
- `precision` (double): Section precision score (0-1)
- `recall` (double): Section recall score (0-1)
- `f1_score` (double): Section F1 score (0-1)
- `false_alarm_rate` (double): Section false alarm rate (0-1)
- `false_discovery_rate` (double): Section false discovery rate (0-1)
- `weighted_overall_score` (double): Weighted overall score (0-1)
- `evaluation_date` (timestamp): When the evaluation was performed

**Partitioned by**: date (YYYY-MM-DD format)

### Attribute Evaluations Table (attribute_evaluations)

**Purpose**: Detailed attribute-level comparison results showing expected vs actual extracted values

#### Schema:
- `document_id` (string): Unique identifier for the document
- `section_id` (string): Identifier for the section
- `section_type` (string): Type/class of the section
- `attribute_name` (string): Name of the extracted attribute
- `expected` (string): Expected (ground truth) value
- `actual` (string): Actual extracted value
- `matched` (boolean): Whether the values matched according to evaluation method
- `score` (double): Match score (0-1)
- `reason` (string): Explanation for the match result
- `evaluation_method` (string): Method used for comparison (EXACT, FUZZY, SEMANTIC, etc.)
- `confidence` (string): Confidence score from extraction process
- `confidence_threshold` (string): Confidence threshold used for evaluation
- `weight` (double): Weight assigned to this attribute in the evaluation
- `evaluation_date` (timestamp): When the evaluation was performed

**Partitioned by**: date (YYYY-MM-DD format)

### Relationships:
- Use `document_id` to join between all three tables
- Use `section_id` and `document_id` to join section and attribute evaluations
- Join with metering table on `document_id` for cost vs accuracy analysis
- `config_version` is available directly in all evaluation tables (no join needed)

### Sample Queries:
```sql
-- Overall accuracy by document type
SELECT "section_type", 
       AVG("accuracy") as avg_accuracy,
       COUNT(*) as document_count
FROM section_evaluations
GROUP BY "section_type"
ORDER BY avg_accuracy DESC

-- Confidence vs accuracy correlation
SELECT 
  CASE 
    WHEN CAST("confidence" AS double) < 0.7 THEN 'Low (<0.7)'
    WHEN CAST("confidence" AS double) < 0.9 THEN 'Medium (0.7-0.9)'
    ELSE 'High (>0.9)'
  END as confidence_band,
  AVG(CASE WHEN "matched" THEN 1.0 ELSE 0.0 END) as accuracy_rate,
  COUNT(*) as attribute_count
FROM attribute_evaluations
WHERE "confidence" IS NOT NULL
GROUP BY confidence_band

-- Cost per accuracy point by document type
-- ⚠️ Cross-table date semantics: `metering.date` is COMPLETION time
-- (Phase-1 change); `section_evaluations.date` is QUEUE time. Joining
-- on `document_id` is safe, but do NOT filter both by the same `date`
-- literal — a doc queued at 23:59Z on day D and completed at 00:01Z
-- on D+1 lands in DIFFERENT date partitions on the two tables. If you
-- need a date filter, apply it on ONE side only (usually
-- `section_evaluations` since it's queue-time and matches the user's
-- intuition about "docs from yesterday"), or accept under-reporting
-- of cross-midnight docs.
-- Note the date filter is on ONE side (`se`) only — filtering `m.date`
-- too would drop cross-midnight docs due to the completion-vs-queue
-- time difference documented above.
SELECT se."section_type",
       AVG(se."accuracy") as avg_accuracy,
       SUM(m."estimated_cost") / COUNT(DISTINCT m."document_id") as avg_cost_per_doc
FROM section_evaluations se
JOIN metering m ON se."document_id" = m."document_id"
WHERE se."date" >= date_format(current_date - interval '7' day, '%Y-%m-%d')
GROUP BY se."section_type"

-- Filter by config_version (available directly in evaluation tables)
SELECT "document_id",
       "accuracy",
       "f1_score",
       "config_version"
FROM document_evaluations
WHERE "config_version" = 'your-config-version'

-- Compare accuracy across configuration versions
SELECT "config_version",
       AVG("accuracy") as avg_accuracy,
       AVG("f1_score") as avg_f1_score,
       COUNT(DISTINCT "document_id") as document_count
FROM document_evaluations
GROUP BY "config_version"
ORDER BY avg_f1_score DESC

-- Cost vs quality analysis by config version
SELECT e."config_version",
       AVG(e."weighted_overall_score") as avg_quality_score,
       SUM(m."estimated_cost") as total_cost,
       SUM(m."estimated_cost") / AVG(e."weighted_overall_score") as cost_per_quality_point
FROM document_evaluations e
JOIN metering m ON e."document_id" = m."document_id"
GROUP BY e."config_version"
ORDER BY avg_quality_score DESC
```
"""


def get_rule_validation_tables_description() -> str:
    """
    Get comprehensive description of the rule validation tables.

    Returns:
        Detailed description of rule validation table schemas and usage patterns
    """
    return """
## Rule Validation Tables

**Purpose**: Store business rule validation results showing compliance and policy adherence

**Key Usage**: Use these tables for questions about rule compliance, policy violations, and validation results

### Rule Validation Summary Table (rule_validation_summary)

**Purpose**: Document-level summary of rule validation results

#### Schema:
- `document_id` (string): Unique identifier for the document
- `input_key` (string): S3 key of the input document
- `validation_date` (timestamp): When the validation was performed
- `overall_status` (string): Overall validation status (COMPLETE, FAILED, etc.)
- `total_policy_types` (int): Number of policy types evaluated
- `total_rules` (int): Total number of rules evaluated
- `pass_count` (int): Number of rules that passed
- `fail_count` (int): Number of rules that failed
- `information_not_found_count` (int): Number of rules where information was not found

**Partitioned by**: date (YYYY-MM-DD format)

### Rule Validation Details Table (rule_validation_details)

**Purpose**: Individual rule-level validation results with recommendations and reasoning

#### Schema:
- `document_id` (string): Unique identifier for the document
- `policy_type` (string): Category/type of the policy class
- `rule` (string): Description of the specific rule being validated
- `recommendation` (string): Validation result (Pass, Fail, Information Not Found)
- `reasoning` (string): Explanation for the recommendation
- `supporting_pages` (string): JSON array of page numbers
- `validation_date` (timestamp): When the validation was performed

**Partitioned by**: date (YYYY-MM-DD format)

### Sample Queries:
```sql
-- Overall pass/fail rates
SELECT SUM("pass_count") as total_pass, SUM("fail_count") as total_fail
FROM rule_validation_summary

-- Documents with failed rules
SELECT "document_id", "fail_count"
FROM rule_validation_summary WHERE "fail_count" > 0

-- Most common failures
SELECT "policy_type", "rule", COUNT(*) as count
FROM rule_validation_details WHERE "recommendation" = 'Fail'
GROUP BY "policy_type", "rule" ORDER BY count DESC LIMIT 10
```
"""


def get_dynamic_document_sections_description(
    config: Optional[IDPConfig] = None,
) -> str:
    """
    Generate deployment-specific description of document sections tables based on actual configuration.

    Args:
        config: Optional configuration dictionary. If None, loads from environment.

    Returns:
        Deployment-specific description with exact table names and column schemas, or error-aware fallback
    """
    try:
        if config is None:
            config = get_config(as_model=True)

        # Get document classes from config
        classes = config.classes

        if not classes:
            logger.warning("No classes found in configuration")
            return _get_error_aware_fallback(
                error_type="CONFIGURATION_ISSUE",
                error_message="No document classes found in configuration. The 'classes' field is missing or empty.",
                troubleshooting="Verify your configuration contains a 'classes' array with document type definitions.",
            )

        description = """
## Document Sections Tables (Configuration-Based)

**Purpose**: Store actual extracted data from document sections in structured format for analytics

**Key Usage**: Use these tables to query the actual extracted content and attributes from processed documents

**IMPORTANT**: Based on your current configuration, the following tables DEFINITELY exist. Do NOT use discovery queries (SHOW TABLES, DESCRIBE) for these - use them directly.

"""

        # Generate table list
        table_names = []
        for doc_class in classes:
            class_name = doc_class.get(X_AWS_IDP_DOCUMENT_TYPE, "Unknown")
            # Apply exact table name transformation logic
            table_name = f"document_sections_{_get_table_suffix(class_name)}"
            table_names.append(table_name)

        description += "### Known Document Sections Tables:\n\n"
        for table_name in table_names:
            description += f"- `{table_name}`\n"

        description += "\n### Complete Table Schemas:\n\n"
        description += "Each table has the following structure:\n\n"

        # Generate detailed schema for each table
        for schema in classes:
            class_name = schema.get(X_AWS_IDP_DOCUMENT_TYPE, "Unknown")
            class_desc = schema.get("description", "No description available")
            table_name = f"document_sections_{_get_table_suffix(class_name)}"
            properties = schema.get(SCHEMA_PROPERTIES, {})
            # Get $defs for resolving $ref references
            defs = schema.get("$defs", {})

            description += f'**`{table_name}`** (Class: "{class_name}"):\n'
            description += f"- **Description**: {class_desc}\n"

            # Standard columns always present
            description += "- **Standard Columns**:\n"
            description += (
                "  - `document_class.type` (string): Document classification type\n"
            )
            description += (
                "  - `document_id` (string): Unique identifier for the document\n"
            )
            description += (
                "  - `section_id` (string): Unique identifier for the section\n"
            )
            description += (
                "  - `section_classification` (string): Type/class of the section\n"
            )
            description += "  - `section_confidence` (string): Confidence in the section's classification; NULL when not scored\n"
            description += "  - `explainability_info` (string): JSON containing explanation of extraction decisions\n"
            description += (
                "  - `timestamp` (timestamp): When the document was processed\n"
            )
            description += "  - `date` (string): Partition key in YYYY-MM-DD format\n"
            description += "  - `config_version` (string): Configuration version used for processing\n"
            description += (
                "  - Various `metadata.*` columns (strings): Processing metadata\n"
            )

            # Multi-instance classes (#715) fan out to ONE ROW PER DOCUMENT
            # INSTANCE rather than one row per section, so the attribute columns
            # below are unchanged but a row is no longer uniquely identified by
            # (document_id, section_id). Without this the agent writes COUNT(*)
            # and per-section joins that silently multiply by the instance count.
            from idp_common.schema.multi_instance import is_multi_instance

            if is_multi_instance(schema):
                description += (
                    "  - `record_index` (int): **This class is multi-instance.** One "
                    "section can contain several separate documents of this class, and "
                    "this table stores ONE ROW PER DOCUMENT, numbered from 0 within "
                    "the section. So (`document_id`, `section_id`) is NOT unique here "
                    "— use (`document_id`, `section_id`, `record_index`). The "
                    "`inference_result.*` columns below hold that one document's "
                    "values, exactly as for a single-record class.\n"
                )

            # Configuration-specific columns - reset column count for each table
            if properties:
                description += "- **Configuration-Specific Columns**:\n"
                column_count = 0  # Reset for each table
                prop_list = list(properties.keys())
                for prop_index, (prop_desc_text, columns_added) in enumerate(
                    _walk_properties_for_columns(properties, defs=defs)
                ):
                    description += prop_desc_text
                    column_count += columns_added
                    # Limit columns within this individual table only
                    if column_count > 20:  # Reasonable per-table limit
                        remaining_props = len(prop_list) - prop_index - 1
                        if remaining_props > 0:
                            description += f"  - ... and {remaining_props} more properties from configuration\n"
                        break
            else:
                description += "- **Configuration-Specific Columns**: None configured\n"

        description += """### Column Naming Patterns:
- **Simple attributes**: `inference_result.{attribute_name_lowercase}` (all strings)
- **Group attributes**: `inference_result.{group_name_lowercase}.{sub_attribute_lowercase}` (all strings)
- **List attributes**: `inference_result.{list_name_lowercase}` (JSON string containing array data)

### CRITICAL: Dot-Notation Column Names
**These are SINGLE column identifiers containing dots, NOT table.column references:**
- ✅ **CORRECT**: `"document_class.type"` (single column name containing a dot)
- ❌ **WRONG**: `"document_class"."type"` (table.column syntax - this will FAIL)
- ✅ **CORRECT**: `"inference_result.ytdnetpay"` (single column name containing dots)
- ❌ **WRONG**: `"inference_result"."ytdnetpay"` (table.column syntax - this will FAIL)

### Important Querying Notes:
- **All `inference_result.*` columns are string type** - even numeric data is stored as strings
- **Always use double quotes** around column names: `"inference_result.companyaddress.state"`
- **Dot notation columns**: Names like `document_class.type` are SINGLE column names with dots inside quotes
- **List data is stored as JSON strings** - use JSON parsing functions to extract array elements
- **Case sensitivity**: Column names are lowercase, use LOWER() for string comparisons
- **Partitioning**: `document_sections_*` tables partitioned by `date` (YYYY-MM-DD).
  The `metering` table is partitioned by `date` + `hour` (YYYY-MM-DD / HH) reflecting
  completion time — `WHERE "date" = 'X'` means "docs completed on X", not "queued on X";
  filter on `initial_event_time` for queue-time semantics.

### Sample Queries:
```sql
-- CORRECT: Filter by document type using dot-notation column name
SELECT COUNT(DISTINCT "document_id") as w2_count
FROM document_sections_w2
WHERE "document_class.type" = 'W2'
AND date >= '2024-01-01'

-- CORRECT: Query specific attributes (example for Payslip)
SELECT "document_id", 
       "document_class.type",
       "inference_result.ytdnetpay",
       "inference_result.employeename.firstname",
       "inference_result.companyaddress.state"
FROM document_sections_payslip
WHERE date >= '2024-01-01'
AND "document_class.type" = 'Payslip'

-- CORRECT: Parse JSON list data (example for FederalTaxes)  
SELECT "document_id",
       "document_class.type",
       json_extract_scalar(tax_item, '$.ItemDescription') as tax_type,
       json_extract_scalar(tax_item, '$.YTD') as ytd_amount
FROM document_sections_payslip
CROSS JOIN UNNEST(json_parse("inference_result.federaltaxes")) as t(tax_item)
WHERE "document_class.type" = 'Payslip'

-- CORRECT: Join with metering for cost analysis
SELECT ds."section_classification",
       ds."document_class.type",
       COUNT(DISTINCT ds."document_id") as document_count,
       AVG(CAST(m."estimated_cost" AS double)) as avg_processing_cost
FROM document_sections_w2 ds
JOIN metering m ON ds."document_id" = m."document_id"
WHERE ds."document_class.type" = 'W2'
GROUP BY ds."section_classification", ds."document_class.type"

-- CORRECT: Filter by configuration version
SELECT "document_id",
       "document_class.type",
       "config_version",
       "timestamp"
FROM document_sections_w2
WHERE "config_version" = 'fake_w2'
AND date >= '2024-01-01'
ORDER BY "timestamp" DESC
```

**This schema information is generated from your actual configuration and shows exactly what tables and columns exist in your deployment.**
"""

        return description

    except Exception as e:
        # Determine the type of error and provide appropriate error-aware fallback
        error_message = str(e)
        logger.error(f"Error generating dynamic sections description: {e}")

        if "Configuration table name not provided" in error_message:
            return _get_error_aware_fallback(
                error_type="MISSING_CONFIGURATION",
                error_message="Configuration table name not provided. The CONFIGURATION_TABLE_NAME environment variable is not set.",
                troubleshooting="Set the CONFIGURATION_TABLE_NAME environment variable to point to your configuration DynamoDB table.",
            )
        elif "ClientError" in str(type(e)) or "DynamoDB" in error_message:
            return _get_error_aware_fallback(
                error_type="DYNAMODB_ACCESS_ERROR",
                error_message=f"Cannot access configuration table: {error_message}",
                troubleshooting="Check that the DynamoDB table exists, you have proper permissions, and AWS credentials are configured.",
            )
        elif "Default configuration not found" in error_message:
            return _get_error_aware_fallback(
                error_type="MISSING_DEFAULT_CONFIG",
                error_message="Default configuration not found in the configuration table.",
                troubleshooting="Ensure your configuration table contains a record with Configuration= Config#default.",
            )
        else:
            return _get_error_aware_fallback(
                error_type="UNKNOWN_ERROR",
                error_message=f"Unexpected error loading configuration: {error_message}",
                troubleshooting="Check logs for detailed error information and verify your deployment configuration.",
            )


def _get_table_suffix(class_name: str) -> str:
    """
    Convert class name to table suffix using exact transformation rules.

    Args:
        class_name: The class name from configuration

    Returns:
        Table suffix for use in document_sections_{suffix}
    """
    return class_name.lower().replace("-", "_").replace(" ", "_")


def _walk_properties_for_columns(
    properties: Dict[str, Any],
    parent_path: str = "inference_result",
    indent: str = "  ",
    defs: Optional[Dict[str, Any]] = None,
) -> Generator[tuple[str, int], None, None]:
    """
    Walk JSON Schema properties and yield (column_description, count) tuples.

    Args:
        properties: JSON Schema properties dict
        parent_path: Parent column path
        indent: Indentation for formatting
        defs: Schema definitions for resolving $ref references

    Yields:
        Tuples of (description_text, columns_added_count)
    """
    for prop_name, prop_schema in properties.items():
        # Handle $ref by resolving to the actual definition
        if "$ref" in prop_schema and defs:
            ref_path = prop_schema["$ref"]
            # Extract the definition name from the reference (e.g., "#/$defs/employer_info")
            if ref_path.startswith("#/$defs/"):
                def_name = ref_path.replace("#/$defs/", "")
                if def_name in defs:
                    # Merge the referenced definition with any override fields
                    resolved_schema = {**defs[def_name], **prop_schema}
                    # Remove $ref from the resolved schema
                    resolved_schema.pop("$ref", None)
                    prop_schema = resolved_schema

        prop_type = prop_schema.get(SCHEMA_TYPE)
        prop_desc = prop_schema.get(SCHEMA_DESCRIPTION, "")
        column_path = f"{parent_path}.{prop_name.lower()}"

        if prop_type == TYPE_OBJECT:
            # Group - recurse to get leaf columns only (no group header)
            # Groups don't become columns themselves - only leaf attributes do
            nested_props = prop_schema.get(SCHEMA_PROPERTIES, {})
            yield from _walk_properties_for_columns(
                nested_props, column_path, indent, defs
            )

        elif prop_type == TYPE_ARRAY:
            # List - single array column
            items_schema = prop_schema.get(SCHEMA_ITEMS, {})
            item_props = items_schema.get(SCHEMA_PROPERTIES, {})
            item_names = list(item_props.keys())
            desc = f'{indent}- `"{column_path}"` (string): {prop_desc}\n'
            if item_names:
                desc += f"{indent}  - JSON array containing items with: {', '.join(item_names)}\n"
            yield (desc, 1)

        else:
            # Simple - single column
            desc = f'{indent}- `"{column_path}"` (string): {prop_desc}\n'
            yield (desc, 1)


def _get_error_aware_fallback(
    error_type: str, error_message: str, troubleshooting: str
) -> str:
    """
    Get error-aware fallback description that surfaces configuration problems prominently.

    Args:
        error_type: Type of error encountered
        error_message: Detailed error message
        troubleshooting: Troubleshooting guidance for the user

    Returns:
        Error-aware description that includes the error details
    """
    return f"""
# ⚠️ CONFIGURATION ERROR DETECTED

**ERROR TYPE**: {error_type}

**ERROR MESSAGE**: {error_message}

**IMPACT**: Cannot load deployment-specific table schemas.

**ACTION REQUIRED**: {troubleshooting}
"""


def get_database_overview(config: Optional[IDPConfig] = None) -> str:
    """
    Get a fast, lightweight overview of available tables with brief descriptions.
    This is the first step in the two-step progressive disclosure system.

    Args:
        config: Optional configuration dictionary for dynamic sections

    Returns:
        Concise database overview with table listings and query guidance
    """
    try:
        if config is None:
            config = get_config(as_model=True)

        # Get document classes from config
        classes = config.classes

        overview = """# Database Overview - Available Tables

### Usage metering and cost (data plane — per-document API spend)
Table name: `metering`
**Purpose**: Per-doc row-level usage — one row per (document, context, service_api, unit)
**Use for**: Individual doc drilldown, doc-grain joins, current-hour queries before hourly rollup fires
**Key columns**: `document_id`, `context`, `service_api`, `estimated_cost`, `date`, `hour`

Table name: `metering_hourly` / `metering_daily`
**Purpose**: Pre-aggregated per-hour / per-day cost, one row per (hour|day, config_version, service_api, unit).
**Use for**: Any wide time-range cost query (>2h) — scans 100× fewer rows than raw metering. Columns: `sum_value` (quantity — tokens/pages/seconds, NOT USD), `sum_cost` (USD).
**Key columns differ per table**: `metering_hourly` uses `hour_ts` (TIMESTAMP); `metering_daily` uses `day` (DATE, NOT `hour_ts` — `hour_ts` does not exist on the daily rollup). See detail section for the full anti-pattern list.

Table name: `metering_docs_hourly` / `metering_docs_daily`
**Purpose**: Doc-grain volume and pages per hour/day, one row per (hour|day, config_version).
**Use for**: "How many docs processed?", "How many pages?" Columns: `n_docs`, `sum_pages`.
**Note**: These doc-grain tables OMIT `service_api` and `unit` — those live only on `metering_hourly` / `metering_daily`. Do NOT `GROUP BY service_api` or `unit` on the docs tables (COLUMN_NOT_FOUND).

### Operational cost (Lambda compute, control-plane vs data-plane)
Table name: `control_plane_hourly`
**Purpose**: Per-hour compute + service cost for CONTROL-PLANE Lambdas (rollup Lambda, resolvers, agents).
**Key columns**: `function_name`, `component`, `bedrock_model`, `duration_ms_sum`, `athena_bytes_sum`, `bedrock_tokens_in/out`, `est_lambda_cost`, `est_athena_cost`, `est_bedrock_cost`. Partition: `date`, `hour`.

Table name: `data_plane_lambda_hourly`
**Purpose**: Per-hour Lambda compute cost for DATA-PLANE pipeline Lambdas (OCR, Classification, Extraction, Assessment, Summarization, Evaluation, etc. — anything tagged `idp:plane=data`). Bedrock/Textract API cost for these Lambdas is already in `metering_hourly` (per-doc). This table adds ONLY Lambda compute (Duration × arch-price + invocations × request-price) so total-compute queries work across both planes.
**Key columns**: `function_name`, `component`, `invocations`, `duration_ms_sum`, `est_lambda_cost`. Partition: `date`, `hour`.
**Total-compute query**: SUM(est_lambda_cost) across `control_plane_hourly` + `data_plane_lambda_hourly` gives full Lambda spend.

### Accuracy evaluations
Table name: `document_evaluations` - Overall document accuracy scores
Table name: `section_evaluations` - Section-level accuracy by document type
Table name: `attribute_evaluations` - Detailed attribute-level comparisons
**Use for**: Accuracy analysis, precision/recall metrics

### Document Sections Tables (extracted content)
"""

        if classes:
            overview += "**Configuration-based tables in your deployment:**\n"
            for schema in classes:
                class_name = schema.get(X_AWS_IDP_DOCUMENT_TYPE, "Unknown")
                class_desc = schema.get("description", "")
                table_name = f"document_sections_{_get_table_suffix(class_name)}"
                overview += f"Table name: `{table_name}` - {class_desc}\n"
        overview += """
**Use for**: Extracted document content, classification results, specific field values

## Critical Query Guidance

### Question-to-Table Mapping:
- **"How many X documents?"** → Use `document_sections_x` table
- **"What document types processed?"** → Query multiple `document_sections_*` tables
- **"Processing costs/volume?"** → Use `metering` table
- **"Document accuracy?"** → Use evaluation tables (if available)

### Key SQL Rules:
- **Always use double quotes** around column names: `"document_id"`
- **Dot-notation columns** are single identifiers: `"document_class.type"`
- **Today's data**: `WHERE "date" = CAST(CURRENT_DATE AS VARCHAR)`
- **Count documents**: `COUNT(DISTINCT "document_id")`

### Next Steps:
Use `get_table_info(['table1', 'table2'])` to get detailed schemas for specific tables you need to query.
"""
        logger.debug(f"Database Overview: {overview}")
        return overview

    except Exception as e:
        logger.error(f"Error generating database overview: {e}")
        return """# Database Overview - Error Loading Configuration"""


def get_table_info(table_names: list[str], config: Optional[IDPConfig] = None) -> str:
    """
    Get detailed schema information for specific tables.
    This is the second step in the two-step progressive disclosure system.

    Args:
        table_names: List of table names to get detailed information for
        config: Optional configuration dictionary for dynamic sections

    Returns:
        Detailed schema information for the requested tables
    """
    if not table_names:
        logger.error("get_table_info(): No table names provided.")
        return "No table names provided. Please specify which tables you need detailed information for."

    detailed_info = f"# Detailed Schema Information for {len(table_names)} Table(s)\n\n"

    # Track which conceptual "group" descriptions have already been
    # emitted so `get_table_info(['metering_hourly', 'metering_docs_hourly'])`
    # doesn't emit the full 6-table rollup description twice. Same for
    # the evaluation and rule-validation groups — each is a single
    # helper that already covers all tables in its group.
    emitted_groups: set[str] = set()

    for table_name in table_names:
        table_name = table_name.lower().strip()

        if table_name == "metering":
            detailed_info += get_metering_table_description()
            detailed_info += "\n---\n\n"

        elif table_name in {
            "metering_hourly",
            "metering_daily",
            "metering_docs_hourly",
            "metering_docs_daily",
            "control_plane_hourly",
            "data_plane_lambda_hourly",
        }:
            # The six rollup tables are one conceptual unit — the tier
            # picker, cost-vs-docs split rule, and sample joins are all
            # cross-cutting — so we return the full 6-table description
            # whenever any rollup is requested. Same pattern as the
            # evaluation / rule-validation helpers below.
            if "rollup" not in emitted_groups:
                detailed_info += get_rollup_tables_description()
                detailed_info += "\n---\n\n"
                emitted_groups.add("rollup")

        elif table_name.startswith("document_evaluations") or table_name in [
            "document_evaluations",
            "section_evaluations",
            "attribute_evaluations",
        ]:
            if "evaluation" not in emitted_groups:
                detailed_info += get_evaluation_tables_description()
                detailed_info += "\n---\n\n"
                emitted_groups.add("evaluation")

        elif table_name in ["rule_validation_summary", "rule_validation_details"]:
            if "rule_validation" not in emitted_groups:
                detailed_info += get_rule_validation_tables_description()
                detailed_info += "\n---\n\n"
                emitted_groups.add("rule_validation")

        elif table_name.startswith("document_sections_"):
            # Extract the class name from table name
            suffix = table_name.replace("document_sections_", "")
            detailed_info += _get_specific_document_sections_table_info(suffix, config)
            detailed_info += "\n---\n\n"

        else:
            detailed_info += f"## Unknown Table: {table_name}\n\n"
            detailed_info += "**Error**: Table name not recognized.\n"

    logger.debug(f"Table Info: {detailed_info}")
    return detailed_info


def _get_specific_document_sections_table_info(
    table_suffix: str, config: Optional[IDPConfig] = None
) -> str:
    """
    Get detailed information for a specific document sections table.

    Args:
        table_suffix: The suffix part of the table name (after document_sections_)
        config: Optional configuration dictionary

    Returns:
        Detailed schema information for the specific table
    """
    try:
        if config is None:
            config = get_config(as_model=True)

        classes = config.classes
        table_name = f"document_sections_{table_suffix}"

        # Find the matching class for this table
        matching_schema = None
        for schema in classes:
            class_name = schema.get(X_AWS_IDP_DOCUMENT_TYPE, "")
            if _get_table_suffix(class_name) == table_suffix:
                matching_schema = schema
                break

        if not matching_schema:
            msg = f"**Error**: Could not find configuration for table `{table_name}`."
            logger.error(msg)
            return msg

        class_name = matching_schema.get(X_AWS_IDP_DOCUMENT_TYPE, "Unknown")
        class_desc = matching_schema.get("description", "No description available")
        properties = matching_schema.get(SCHEMA_PROPERTIES, {})
        # Get $defs for resolving $ref references
        defs = matching_schema.get("$defs", {})

        info = f"""## Document Sections Table: {table_name}

**Class**: "{class_name}"  
**Description**: {class_desc}

### Complete Schema:

#### Standard Columns (present in all document_sections tables):
- `"document_id"` (string): Unique identifier for the document
- `"section_id"` (string): Unique identifier for the section
- `"section_classification"` (string): Type/class of the document section
- `"section_confidence"` (string): Confidence in the section's classification; NULL when not scored
- `"explainability_info"` (string): JSON with extraction field confidence scores and geometry
- `"timestamp"` (timestamp): When document was processed in YYYY-MM-DD hh:mm:ss.ms format
- `"date"` (string): Partition key in YYYY-MM-DD format
- `"config_version"` (string): Configuration version used for processing
"""

        # A multi-instance class (#715) stores ONE ROW PER DOCUMENT, not per
        # section, so (document_id, section_id) is not unique here. Without this
        # the agent writes COUNT(*) and per-section joins that silently multiply
        # by the instance count. It must be on THIS path too, not only on
        # get_dynamic_document_sections_description: this is the detailed
        # per-table description the agent reads after get_table_info, and it is
        # the one it plans queries from.
        from idp_common.schema.multi_instance import is_multi_instance

        if is_multi_instance(matching_schema):
            info += (
                '- `"record_index"` (int): **This class is multi-instance.** One '
                "section can hold several separate documents of this class, and "
                "this table stores ONE ROW PER DOCUMENT, numbered from 0 within "
                "the section. So (`document_id`, `section_id`) is NOT unique — use "
                "(`document_id`, `section_id`, `record_index`), and count documents "
                'of this class with `COUNT(*)`, not `COUNT(DISTINCT "document_id")` '
                "(one input file can contain many). The columns below hold that "
                "one document's values, exactly as for a single-record class.\n"
            )

        info += "\n#### Columns specific to this table:\n"

        if properties:
            for prop_desc_text, _ in _walk_properties_for_columns(
                properties, defs=defs
            ):
                info += prop_desc_text
        else:
            info += "No configuration-specific columns defined.\n"

        info += f"""

### Sample Queries for {table_name}:
```sql
-- Count documents of this type today
SELECT COUNT(DISTINCT "document_id") as document_count
FROM {table_name}
WHERE "date" = CAST(CURRENT_DATE AS VARCHAR)

-- Get documents with extracted data
SELECT "document_id", "section_classification"
FROM {table_name}
WHERE "date" >= '2024-01-01'
ORDER BY "timestamp" DESC
LIMIT 10

```

### Important Notes:
- All `"inference_result.*"` columns are stored as strings
- Use `LOWER()` for case-insensitive string matching
- Dot-notation column names like `"document_class.type"` are single column identifiers
- Table is partitioned by `"date"` - include date filters for better performance
"""  # nosec B608 - example SQL inside a prompt template, never executed

        return info

    except Exception as e:
        logger.error(f"Error getting table info for {table_suffix}: {e}")
        return "**Error**: Could not load detailed schema information."
