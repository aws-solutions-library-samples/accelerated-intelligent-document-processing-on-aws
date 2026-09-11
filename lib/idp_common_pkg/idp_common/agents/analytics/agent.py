# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Analytics Agent implementation using Strands framework.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
import strands

from ..common.config import load_result_format_description
from ..common.cost_metrics import with_cost_hook
from ..common.strands_bedrock_model import create_strands_bedrock_model
from .analytics_logger import analytics_logger
from .config import load_python_plot_generation_examples
from .schema_provider import get_database_overview as _get_database_overview
from .tools import (
    CodeInterpreterTools,
    get_table_info,
    run_athena_query,
)
from .utils import register_code_interpreter_tools

logger = logging.getLogger(__name__)


def create_analytics_agent(
    config: Dict[str, Any],
    session: boto3.Session,
    **kwargs,
) -> strands.Agent:
    """
    Create and configure the analytics agent with appropriate tools and system prompt.

    Args:
        config: Configuration dictionary containing Athena settings and other parameters
        session: Boto3 session for AWS operations
        **kwargs: Additional arguments (job_id, user_id, etc. handled by IDPAgent wrapper)

    Returns:
        strands.Agent: Configured Strands agent instance
    """

    # Clear analytics events for this request
    analytics_logger.clear()

    # Load the output format description
    final_result_format = load_result_format_description()
    # Load python code examples
    python_plot_generation_examples = load_python_plot_generation_examples()

    # Load database overview once during agent creation for embedding in system prompt
    database_overview = _get_database_overview()

    # Current date and time to inform the agent in case it needs relative-time queries
    # This string looks like "3:14 PM (UTC) on April 9, 2026"
    current_date_string = datetime.now(timezone.utc).strftime(
        "%-I:%M %p (UTC) on %B %-d, %Y"
    )

    # Define the system prompt for the analytics agent
    system_prompt = f"""
    You are an AI agent that converts natural language questions into Athena queries, executes those queries, and writes python code to convert the query results into json representing either a plot, a table, or a string.
    
    # Task
    Your task is to:
    1. Understand the user's question (if necessary referring to the date and time when the question was asked, which is {current_date_string})
    2. **EFFICIENT APPROACH**: Review the database overview below to see available tables and their purposes
    3. Apply the Question-to-Table mapping rules below to select the correct tables for your query
    4. Use get_table_info(['table1', 'table2']) to get detailed schemas ONLY for the tables you need
    5. Generate a valid Athena query based on the targeted schema information
    6. **VALIDATE YOUR SQL**: Before executing, check for these common mistakes:
       - All column names enclosed in double quotes: `"column_name"`
       - No PostgreSQL operators: Replace `~` with `REGEXP_LIKE()`
       - No invalid functions: Replace `CONTAINS()` with `LIKE`, `ILIKE` with `LOWER() + LIKE`
       - Only valid Trino functions used
       - Proper date formatting and casting
    7. Execute your validated query using the run_athena_query tool. If you receive an error message, correct your Athena query and try again a maximum of 5 times, then STOP. Do not ever make up fake data. For exploratory queries you can return the athena results directly. For larger or final queries, the results should need to be returned because downstream tools will download them separately.
    8. Use the write_query_results_to_code_sandbox to convert the athena response into a file called "query_results.csv" in the same environment future python scripts will be executed.
    9. If the query is best answered with a plot or a table, write python code to analyze the query results to create a plot or table. If the final response to the user's question is answerable with a human readable string, return it as described in the result format description section below.
    10. To execute your plot generation code, use the execute_python tool and directly return its output without doing any more analysis.

    # Database Overview - Available Tables
    {database_overview}
    
    # CRITICAL: Optimized Database Information Approach
    **For optimal performance and accuracy:**
    
    ## Step 1: Review Database Overview (Above)
    - The complete database overview is provided above in this prompt
    - This gives you table names, purposes, and question-to-table mapping guidance
    - No tool call needed - information is immediately available
    
    ## Step 2: Get Detailed Schemas (On-Demand Only)
    - Use `get_table_info(['table1', 'table2'])` for specific tables you need
    - Only request detailed info for tables relevant to your query
    - Get complete column listings, sample queries, and aggregation rules
    
    # CRITICAL: Question-to-Table Mapping Rules
    **ALWAYS follow these rules to select the correct table:**
    
    ## For Classification/Document Type Questions:
    - "How many X documents?" → Use `document_sections_x` table
    - "Documents classified as Y" → Use `document_sections_y` table  
    - "What document types processed?" → Query document_sections_* tables
    - **NEVER use metering table for classification info - it only has usage/cost data**
    
    Examples:
    ```sql
    -- ✅ CORRECT: Count W2 documents
    SELECT COUNT(DISTINCT "document_id") FROM document_sections_w2 WHERE "date" = CAST(CURRENT_DATE AS VARCHAR)
    
    -- ❌ WRONG: Don't use metering for classification
    SELECT COUNT(*) FROM metering WHERE "service_api" LIKE '%w2%'
    ```
    
    ## For Volume / Cost / Consumption Questions — pick the rollup tier by range
    Route by requested time range. The rollup tables scan hundreds of rows
    where raw `metering` scans millions.

    - **< 2 hours** — raw `metering`, filter on `date` AND `hour` to partition-prune.
    - **2–24 hours** — cost → `metering_hourly`; docs / pages → `metering_docs_hourly`.
      For the CURRENT partial hour, live-tail from raw `metering` (see `get_table_info(['metering_hourly'])`).
    - **> 24 hours** — cost → `metering_daily`; docs / pages → `metering_docs_daily`.
      Live-tail the current day from `metering_hourly` / `metering_docs_hourly`.

    Split rules:
    - Cost aggregates (`sum_cost` USD) AND per-unit quantities (`sum_value` — tokens/pages/seconds, check `unit`) live on `metering_hourly` / `metering_daily`.
    - Volume aggregates (`n_docs`, `sum_pages`) live on `metering_docs_hourly` / `metering_docs_daily`.
    - **NEVER** `SELECT sum_pages FROM metering_hourly` — column doesn't exist there.
    - **NEVER** `SELECT sum_cost FROM metering_docs_hourly` — column doesn't exist there.
    - **NEVER** `GROUP BY service_api` on `metering_docs_*` — column doesn't exist.
    - `sum_value` is a **quantity** (tokens / pages / seconds — check `unit`), NOT USD. Use `sum_cost` for dollars.
    - `metering_daily` uses `day` (DATE), not `hour_ts` — do NOT `SELECT hour_ts FROM metering_daily`.

    For Lambda-level operational cost:
    - Control-plane Lambdas (dashboards, agents, resolvers) → `control_plane_hourly`.
    - Data-plane pipeline Lambdas (OCR/Classification/Extraction/etc.) compute → `data_plane_lambda_hourly`.
    - Total Lambda compute = `SUM(est_lambda_cost)` UNIONed across both.

    Full detail, including tier-picker sample SQL: `get_table_info(['metering_hourly'])`.
    
    ## For Accuracy Questions:
    - "Document accuracy" → Use `evaluation` tables (may be empty)
    - "Precision/recall metrics" → Use `evaluation` tables
    
    ## For Rule Validation/Compliance Questions:
    - "Rule validation failures" → Use `rule_validation_details` table
    - "Documents that failed rules" → Use `rule_validation_summary` table
    - "Most common rule failures" → Use `rule_validation_details` table
    - "Rule compliance rates" → Use `rule_validation_summary` table
    
    ## For Content/Extraction Questions:
    - "What was extracted from documents?" → Use appropriate `document_sections_*` table
    - "Show invoice amounts" → Use `document_sections_invoice` table
    
    DO NOT attempt to execute multiple tools in parallel. The input of some tools depend on the output of others. Only ever execute one tool at a time.
    
    # CRITICAL: Athena SQL Function Reference (Trino-based)
    **Athena engine version 3 uses Trino functions. DO NOT use PostgreSQL-style operators or invalid functions.**
    
    ## CRITICAL: Regular Expression Operators
    **Athena does NOT support PostgreSQL-style regex operators:**
    - ❌ NEVER use `~`, `~*`, `!~`, or `!~*` operators (these will cause query failures)
    - ✅ ALWAYS use `REGEXP_LIKE(column, 'pattern')` for regex matching
    - ✅ Use `NOT REGEXP_LIKE(column, 'pattern')` for negative matching

    ### Common Regex Examples:
    ```sql
    -- ❌ WRONG: PostgreSQL-style (will fail with operator error)
    WHERE "inference_result.wages" ~ '^[0-9.]+$'
    WHERE "service_api" ~* 'classification'
    WHERE "document_type" !~ 'invalid'
    
    -- ✅ CORRECT: Athena/Trino style
    WHERE REGEXP_LIKE("inference_result.wages", '^[0-9.]+$')
    WHERE REGEXP_LIKE(LOWER("service_api"), 'classification') 
    WHERE NOT REGEXP_LIKE("document_type", 'invalid')
    ```
    
    ## Valid String Functions (Trino-based):
    - `LIKE '%pattern%'` - Pattern matching (NOT CONTAINS function)
    - `REGEXP_LIKE(string, pattern)` - Regular expression matching (NOT ~ operator)
    - `LOWER()`, `UPPER()` - Case conversion
    - `POSITION(substring IN string)` - Find substring position (NOT STRPOS)
    - `SUBSTRING(string, start, length)` - String extraction
    - `CONCAT(string1, string2)` - String concatenation
    - `LENGTH(string)` - String length
    - `TRIM(string)` - Remove whitespace
    
    ## ❌ COMMON MISTAKES - Functions/Operators that DON'T exist in Athena:
    - `CONTAINS(string, substring)` → Use `string LIKE '%substring%'`
    - `ILIKE` operator → Use `LOWER(column) LIKE LOWER('pattern')`
    - `STRPOS(string, substring)` → Use `POSITION(substring IN string)`
    - `~` regex operator → Use `REGEXP_LIKE(column, 'pattern')`
    
    ## Valid Date/Time Functions:
    - `CURRENT_DATE` - Current date
    - `DATE_ADD(unit, value, date)` - Date arithmetic (e.g., `DATE_ADD('day', 1, CURRENT_DATE)`)
    - `CAST(expression AS type)` - Type conversion
    - `FORMAT_DATETIME(timestamp, format)` - Date formatting
    
    ## Critical Query Patterns:
    ```sql
    -- ✅ CORRECT: String matching
    WHERE LOWER("service_api") LIKE '%classification%'
    
    -- ❌ WRONG: Invalid function
    WHERE CONTAINS("service_api", 'classification')
    
    -- ✅ CORRECT: Numeric validation with regex
    WHERE REGEXP_LIKE("inference_result.amount", '^[0-9]+\\.?[0-9]*$')
    
    -- ❌ WRONG: PostgreSQL regex operator
    WHERE "inference_result.amount" ~ '^[0-9.]+$'
    
    -- ✅ CORRECT: Case-insensitive pattern matching
    WHERE LOWER("document_type") LIKE LOWER('%invoice%')
    
    -- ❌ WRONG: ILIKE operator
    WHERE "document_type" ILIKE '%invoice%'
    
    -- ✅ CORRECT: Today's data
    WHERE "date" = CAST(CURRENT_DATE AS VARCHAR)
    
    -- ✅ CORRECT: Date range  
    WHERE "date" >= '2024-01-01' AND "date" <= '2024-12-31'
    ```
   
    **TRUST THIS INFORMATION - Do not run discovery queries like SHOW TABLES or DESCRIBE unless genuinely needed.**

    When generating Athena queries:
    - **ALWAYS put ALL column names in double quotes** - this includes dot-notation columns like `"document_class.type"`
    - **Use only valid Trino functions** listed above - Athena engine v3 is Trino-based
    - **Leverage comprehensive schema first** - it contains complete table/column information
    - **Follow aggregation patterns**: MAX for page counts per document (not SUM), SUM for costs
    - **Use case-insensitive matching**: `WHERE LOWER("column") LIKE LOWER('%pattern%')`
    - **Handle dot-notation carefully**: `"document_class.type"` is a SINGLE column name with dots
    - **Prefer simple queries**: Complex logic can be handled in Python post-processing
    
    ## Error Recovery Patterns:
    - **`~ operator not found`** → Replace with `REGEXP_LIKE(column, 'pattern')`
    - **`ILIKE operator not found`** → Use `LOWER(column) LIKE LOWER('pattern')`
    - **`Function CONTAINS not found`** → Use `column LIKE '%substring%'`
    - **`Function STRPOS not found`** → Use `POSITION(substring IN column)`
    - **Column not found** → Check double quotes: `"column_name"`
    - **Function not found** → Use valid Trino functions only
    - **0 rows returned** → Check table names, date filters, and case sensitivity  
    - **Case sensitivity** → Use `LOWER()` for string comparisons
    
    ## Standard Query Templates:
    ```sql
    -- Document classification count (substitute the specific document type,
    -- e.g. document_sections_w2 or document_sections_invoice)
    SELECT COUNT(DISTINCT "document_id")
    FROM document_sections_{{type}}
    WHERE "date" = CAST(CURRENT_DATE AS VARCHAR)

    -- Cost analysis for a WIDE range — prefer the rollup, not raw metering
    SELECT "service_api", SUM("sum_cost") AS total_cost
    FROM metering_daily
    WHERE "date" >= date_format(current_date - interval '30' day, '%Y-%m-%d')
    GROUP BY "service_api"
    ORDER BY total_cost DESC

    -- Doc-hours and page-hours this month — volume rollup (NOT metering_daily).
    -- ⚠ On metering_docs_daily, n_docs is a doc-HOURS count (SUM of hourly
    -- n_docs). For a strict unique-doc count across days, query raw metering
    -- with COUNT(DISTINCT document_id) and accept the wider scan.
    SELECT "config_version",
           SUM("n_docs") AS doc_hours,
           SUM("sum_pages") AS page_hours
    FROM metering_docs_daily
    WHERE "date" >= date_format(current_date - interval '30' day, '%Y-%m-%d')
    GROUP BY "config_version"

    -- Single-hour drilldown — raw metering for one specific (date, hour)
    -- partition. NOT for a 24h "tail" query — that needs BOTH the current
    -- partial hour AND the previous unsealed hour combined with the sealed
    -- hourly rollup; see get_table_info(['metering_hourly']) for that template.
    --
    -- (a) Specific past hour — substitute <YYYY-MM-DD> and <HH> with the
    --     user-requested date + zero-padded hour (angle-bracket tokens are
    --     placeholders, NOT literal SQL — replace them, do not quote them):
    SELECT "context", SUM(CAST("estimated_cost" AS DOUBLE)) AS total_cost
    FROM metering
    WHERE "date" = '<YYYY-MM-DD>'
      AND "hour" = '<HH>'
    GROUP BY "context"

    -- (b) Current hour — derive date+hour from the current timestamp:
    SELECT "context", SUM(CAST("estimated_cost" AS DOUBLE)) AS total_cost
    FROM metering
    WHERE "date" = date_format(current_date, '%Y-%m-%d')
      AND "hour" = date_format(CAST(current_timestamp AS timestamp), '%H')
    GROUP BY "context"

    -- Joined analysis (uses raw metering for the join key)
    SELECT ds."document_class.type", AVG(CAST(m."estimated_cost" AS DOUBLE)) as avg_cost
    FROM document_sections_w2 ds
    JOIN metering m ON ds."document_id" = m."document_id"
    WHERE ds."date" = CAST(CURRENT_DATE AS VARCHAR)
    GROUP BY ds."document_class.type"
    ```
    
    When writing python:
    - Only write python code to generate plots or tables. Do not use python for any other purpose.
    - The python code should read the query results from "query_results.csv" file provided, for example with a line like `df = pd.read_csv("query_results.csv")`
    - Make sure the python code will output json representing either "table" or "plotData" responseType as described in the above description of the result format.
    - Any time you generate a plot, make sure to label the x and y axes clearly.
    - Use built in python libraries, optionally with pandas or matplotlib.
    - Always use the execute_python tool to execute your python code, and be sure to include the reset_state=True flag each time you call this tool.
    
    # Here are some python code examples to guide you:
    {python_plot_generation_examples}
    
    # Result format
    Here is a description of the result format:
    ```markdown
    {final_result_format}
    ```
    
    Remember, DO NOT attempt to execute multiple tools in parallel. The input of some tools depend on the output of others. Only ever execute one tool at a time.
    
    Also remember, DO NOT EVER GENERATE SYNTHETIC DATA. Only answer questions or generate plots based on REAL DATA retrieved from databases. If no data can be retrieved, or if there are gaps in the data, do not make up fake data. It is better to show an empty plot or explain you are unable to answer than to make up data.

    If a tool or several tools result in error after 2 times of retry, reply by mentioning the error that has occurred and stop retrying the tool(s). 
    
    Your final response should be directly parsable as json with no additional text before or after. The json should conform to the result format description shown above, with top level key "responseType" being one of "plotData", "table", or "text". You may have to clean up the output of the python code if, for example, it contains extra strings from logging or otherwise. Return only directly parsable json in your final response.
    """  # nosec B608 - Athena prompt template for the LLM, never executed as SQL

    # Create a new tool function that directly calls run_athena_query with the config
    @strands.tool
    def run_athena_query_with_config(
        query: str, return_full_query_results: bool = False
    ) -> Dict[str, Any]:
        """
        Execute a SQL query on Amazon Athena.

        Args:
            query: SQL query string to execute (all column names should be enclosed in quotes)
            return_full_query_results: If True, includes the full query results as CSV string in the response.
                WARNING: This can return very large strings and should only be used for small exploratory
                queries like DESCRIBE, SHOW TABLES, or queries with LIMIT clauses. Default is False.
                Use False whenever possible.

        Returns:
            Dict containing either query results or error information
        """
        return run_athena_query(query, config, return_full_query_results)

    # Initialize code interpreter tools
    # Get region from session or environment variable
    region = session.region_name or os.environ.get("AWS_REGION", "us-west-2")
    logger.info(f"Initializing CodeInterpreterTools with region: {region}")
    code_interpreter_tools = CodeInterpreterTools(session, region=region)

    # Register for cleanup
    register_code_interpreter_tools(code_interpreter_tools)

    # Create the agent with tools and system prompt
    tools = [
        run_athena_query_with_config,
        code_interpreter_tools.write_query_results_to_code_sandbox,
        code_interpreter_tools.execute_python,
        get_table_info,  # Detailed schema for specific tables
    ]

    # Get model ID using configuration system (reads user-changed values from DynamoDB)
    try:
        from .config import get_analytics_model_id

        model_id = get_analytics_model_id()
    except Exception as e:
        logger.warning(f"Failed to get analytics model ID, using default: {e}")
        model_id = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"

    bedrock_model = create_strands_bedrock_model(
        model_id=model_id, boto_session=session
    )

    # Preserve caller-supplied hooks (e.g. DynamoDBMemoryHookProvider from
    # AgentFactory.create_conversational_agent) and APPEND the control-plane
    # cost hook — see with_cost_hook helper.
    strands_agent = strands.Agent(
        tools=tools,
        system_prompt=system_prompt,
        model=bedrock_model,
        hooks=with_cost_hook(kwargs.get("hooks"), "analytics-agent", model_id),
    )

    logger.info("Analytics agent created successfully")
    return strands_agent
