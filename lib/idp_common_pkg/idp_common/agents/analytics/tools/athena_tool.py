# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Athena Query Tool for executing SQL queries using Strands framework.
"""

import logging
import time
from typing import Any, Dict

import boto3
from strands import tool

from ..analytics_logger import analytics_logger

logger = logging.getLogger(__name__)

# Maximum number of rows that can be returned directly when return_full_query_results=True
MAX_ROWS_TO_RETURN_DIRECTLY = 100


@tool
def run_athena_query(
    query: str, config: Dict[str, Any], return_full_query_results: bool = False
) -> Dict[str, Any]:
    """
    Execute a SQL query on Amazon Athena.

    Uses boto3 to execute the query on Athena. Query results are stored in s3.
    Successful execution will return a dict with result_column_metadata,
        result_csv_s3_uri, number of rows_returned, and original_query.

    Args:
        query: SQL query string to execute
        config: Configuration dictionary containing Athena settings
        return_full_query_results: If True, includes the full query results as CSV string in the response.
            WARNING: This can return very large strings and should only be used for small exploratory
            queries like DESCRIBE, SHOW TABLES, or queries with LIMIT clauses. Default is False.

    Returns:
        Dict containing either s3 URI pointer to query results or error information
        Query results for a successful query include:
            result_column_metadata (information about the columns in the result)
            result_csv_s3_uri (s3 location where results are stored as a csv)
            rows_returned (number of rows returned by the query)
            original_query (the original query the user entered, for posterity)
            full_results (optional, only if return_full_query_results=True): CSV string of query results
    """
    start_time = time.time()
    try:
        # Create Athena client
        athena_client = boto3.client("athena", region_name=config.get("aws_region"))

        # Start query execution
        analytics_logger.log_query(query)
        logger.info(f"return_full_query_results: [{return_full_query_results}]")
        response = athena_client.start_query_execution(
            QueryString=query,
            QueryExecutionContext={"Database": config["athena_database"]},
            ResultConfiguration={"OutputLocation": config["athena_output_location"]},
        )

        query_execution_id = response["QueryExecutionId"]
        logger.info(f"Query execution ID: {query_execution_id}")

        # Wait for query to complete by polling its status
        max_polling_attempts = config.get("max_polling_attempts", 20)
        attempts = 0
        state = "RUNNING"  # Initialize state

        while attempts < max_polling_attempts:
            response = athena_client.get_query_execution(
                QueryExecutionId=query_execution_id
            )
            state = response["QueryExecution"]["Status"]["State"]
            if state == "SUCCEEDED":
                logger.info("Query succeeded!")
                query_output_s3_uri = response["QueryExecution"]["ResultConfiguration"][
                    "OutputLocation"
                ]
                break
            elif state in ["FAILED", "CANCELLED"]:
                logger.error(f"Query {state.lower()}.")
                break
            else:
                logger.debug(
                    f"Query state: {state}, sleeping for 2 seconds (attempt {attempts + 1}/{max_polling_attempts})"
                )
                time.sleep(  # semgrep-ignore: arbitrary-sleep - Intentional delay. Duration is hardcoded and not user-controlled.
                    2
                )  # semgrep-ignore: arbitrary-sleep - Intentional delay. Duration is hardcoded and not user-controlled.
                attempts += 1

        # Emit AthenaBytesScanned for the control-plane cost KPI —
        # the analytics agent is control plane (user-triggered SQL over
        # metering). Fire-and-forget; failures are swallowed by the helper
        # so this can't affect the caller's query result.
        try:
            data_scanned = (
                response.get("QueryExecution", {})
                .get("Statistics", {})
                .get("DataScannedInBytes")
            )
            if data_scanned is not None:
                from idp_common.metrics import emit_control_plane_cost_metric

                emit_control_plane_cost_metric(
                    component="analytics-agent",
                    athena_bytes=int(data_scanned),
                )
        except Exception as e:  # nosec — telemetry must not affect the caller
            # WARNING (not silent) so a future packaging/import regression
            # doesn't silently zero the analytics-agent's Athena spend in
            # control_plane_hourly. Round-5 review fix.
            logger.warning(f"Failed to emit analytics-agent Athena cost metric: {e!r}")

        # Check final state
        if state == "SUCCEEDED":
            # Get query results
            results = athena_client.get_query_results(
                QueryExecutionId=query_execution_id
            )

            # Extract relevant metadata to share with downstream agents
            column_metadata = [
                f"{col['Name']=}, {col['Label']=}, {col['Type']=}, {col['Precision']=}"
                for col in results["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
            ]

            # Count the number of rows returned
            # Note: For most queries, all rows in the ResultSet are data rows
            # For queries with headers (like SELECT), Athena typically includes headers in the first row
            total_rows = len(results["ResultSet"]["Rows"])

            # Check if return_full_query_results is True and we have too many rows
            if return_full_query_results and total_rows > MAX_ROWS_TO_RETURN_DIRECTLY:
                logger.warning(
                    f"Query returned {total_rows} rows, which exceeds the limit of {MAX_ROWS_TO_RETURN_DIRECTLY} "
                    f"for return_full_query_results=True"
                )
                result = {
                    "success": False,
                    "error": (
                        f"More than {MAX_ROWS_TO_RETURN_DIRECTLY} rows were retrieved when the tool was called with "
                        "`return_full_query_results` set to True. This flag should only be used for small queries "
                        "returning a few rows. Please try again with `return_full_query_results` set to False, "
                        "in which case the query results will be saved rather than returned directly."
                    ),
                    "query": query,
                    "rows_returned": total_rows,
                }
                analytics_logger.log_content("run_athena_query", result)
                logger.info(f"return_full_query_results: [{return_full_query_results}]")
                return result

            result_dict = {
                "success": True,
                "result_column_metadata": column_metadata,
                "result_csv_s3_uri": query_output_s3_uri,
                "rows_returned": total_rows,
                "query": query,
            }

            # Optionally include full query results
            if return_full_query_results:
                try:
                    # Parse S3 URI to get bucket and key
                    import re

                    s3_match = re.match(r"s3://([^/]+)/(.+)", query_output_s3_uri)
                    if s3_match:
                        bucket_name = s3_match.group(1)
                        object_key = s3_match.group(2)

                        # Create S3 client and read the CSV file
                        s3_client = boto3.client(
                            "s3", region_name=config.get("aws_region")
                        )
                        response = s3_client.get_object(
                            Bucket=bucket_name, Key=object_key
                        )
                        csv_content = response["Body"].read().decode("utf-8")

                        result_dict["full_results"] = csv_content
                        logger.info(
                            f"Included full query results ({len(csv_content)} characters)"
                        )
                    else:
                        logger.warning(f"Could not parse S3 URI: {query_output_s3_uri}")
                        result_dict["full_results_error"] = (
                            f"Could not parse S3 URI: {query_output_s3_uri}"
                        )

                except Exception as e:
                    logger.error(f"Error reading full query results from S3: {e}")
                    result_dict["full_results_error"] = (
                        f"Error reading results from S3: {str(e)}"
                    )

            analytics_logger.log_content("run_athena_query", result_dict)
            logger.info(f"return_full_query_results: [{return_full_query_results}]")
            return result_dict

        elif state == "RUNNING":
            # Query is still running after max polling attempts
            logger.warning(
                f"Query still running after {max_polling_attempts} polling attempts. Query execution ID: {query_execution_id}"
            )
            result = {
                "success": False,
                "error": f"Query timed out after {max_polling_attempts} polling attempts. The query is still running in Athena and may complete later.",
                "query": query,
                "query_execution_id": query_execution_id,
                "state": "RUNNING",
            }
            analytics_logger.log_content("run_athena_query", result)
            logger.info(f"return_full_query_results: [{return_full_query_results}]")
            return result
        else:
            # Query failed
            error_message = response["QueryExecution"]["Status"].get(
                "StateChangeReason", "Query failed with an Unknown error"
            )
            error_details = response["QueryExecution"]["Status"].get("AthenaError", {})
            logger.error(f"Query failed with state {state}. Reason: {error_message}")

            result = {
                "success": False,
                "error": error_message,
                "state": state,
                "athena_error_details": error_details,
                "query": query,
            }
            analytics_logger.log_content("run_athena_query", result)
            logger.info(f"return_full_query_results: [{return_full_query_results}]")
            return result

    except Exception as e:
        logger.exception("Error executing Athena query")
        result = {"success": False, "error": str(e), "query": query}
        analytics_logger.log_content("run_athena_query", result)
        logger.info(f"return_full_query_results: [{return_full_query_results}]")
        return result
    finally:
        analytics_logger.log_event("run_athena_query", time.time() - start_time)
