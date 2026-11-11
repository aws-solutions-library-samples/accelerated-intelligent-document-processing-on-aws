# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Code interpreter tools for analytics agents.
"""

import json
import logging
import time
from typing import Optional

import boto3
from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter
from strands import tool

from ..analytics_logger import analytics_logger
from ..sandbox_code_checks import SandboxCodeNotAllowedError, check_sandbox_code

logger = logging.getLogger(__name__)


class CodeInterpreterTools:
    """Tools for managing code interpreter operations."""

    def __init__(self, session: boto3.Session, region: str = "us-west-2"):
        """
        Initialize the code interpreter tools.

        Args:
            session: Boto3 session for AWS operations
            region: AWS region for code interpreter
        """
        self.session = session
        self.region = region
        self._code_client = None
        # Where the result of the last successful Athena query this agent ran is
        # stored, as reported by Athena. This is the only object
        # write_query_results_to_code_sandbox reads.
        self._last_query_result_uri: Optional[str] = None
        self._last_query: Optional[str] = None

    def record_query_result(
        self, s3_uri: Optional[str], query: Optional[str] = None
    ) -> None:
        """Remember the result location of the query that just ran.

        Called by the agent's Athena tool after every query: with the location
        Athena reported for a successful query, and with ``None`` before a query runs
        so that a failed or refused query leaves no earlier result in place.
        """
        self._last_query_result_uri = s3_uri or None
        self._last_query = query if s3_uri else None

    def _get_code_interpreter_client(self):
        """Get or create the code interpreter client."""
        if self._code_client is None:
            self._code_client = CodeInterpreter(self.region)
            self._code_client.start()
            logger.info(f"Started code interpreter client in region {self.region}")
        return self._code_client

    def _invoke_code_interpreter_tool(self, tool_name: str, arguments: dict) -> dict:
        """Invoke a code interpreter tool and return the result."""
        client = self._get_code_interpreter_client()
        response = client.invoke(tool_name, arguments)
        for event in response["stream"]:
            return json.loads(json.dumps(event["result"], indent=2))
        return {}

    def cleanup(self):
        """Clean up the code interpreter session."""
        if self._code_client:
            logger.info("Cleaning up code interpreter session...")
            self._code_client.stop()
            self._code_client = None

    @tool
    def write_query_results_to_code_sandbox(self) -> str:
        """
        Write the results of the last successful Athena query to the code
        interpreter environment as "query_results.csv".
        Must always be called before any execute_python functions can be called.

        Takes no arguments: the file is always the result of the most recent
        successful run_athena_query call. Run the query first.

        Returns:
            Success message indicating the file was written to the sandbox
        """
        start_time = time.time()
        try:
            filename = "query_results.csv"
            s3_uri = self._last_query_result_uri
            if not s3_uri:
                result = (
                    "No query results are available to write. Run a query with "
                    "run_athena_query first; this tool writes the results of the "
                    "most recent successful query."
                )
                analytics_logger.log_content(
                    "write_query_results_to_code_sandbox", result
                )
                return result

            logger.info(
                f"Writing the last query result to code interpreter: {filename}"
            )

            # The location came from Athena's own GetQueryExecution response for
            # the query this agent ran, never from the model.
            if not s3_uri.startswith("s3://") or "/" not in s3_uri[5:]:
                raise ValueError("Athena reported an unexpected result location")
            bucket_name, key = s3_uri[5:].split("/", 1)

            logger.debug(f"Bucket: {bucket_name}, Key: {key}")

            # Download from S3 using boto3
            s3_client = self.session.client("s3")

            # Download the CSV content
            response = s3_client.get_object(Bucket=bucket_name, Key=key)
            csv_content = response["Body"].read().decode("utf-8")

            logger.debug(f"Successfully downloaded CSV ({len(csv_content)} characters)")

            # Prepare files to create in code interpreter
            files_to_create = [{"path": filename, "text": csv_content}]

            # Write the file to the code interpreter environment
            writing_files = self._invoke_code_interpreter_tool(
                "writeFiles", {"content": files_to_create}
            )
            logger.debug(f"Writing files result: {writing_files}")

            # List files to verify
            listing_files = self._invoke_code_interpreter_tool(
                "listFiles", {"path": ""}
            )
            logger.debug(f"Files in code interpreter: {listing_files}")

            result = (
                f"CSV file '{filename}' successfully written to code interpreter "
                "environment. It holds the results of this query; if that is not "
                "the query the answer needs, run that query again and call this tool "
                f"again: {(self._last_query or '')[:2000]}"
            )
            analytics_logger.log_content("write_query_results_to_code_sandbox", result)
            return result

        except Exception as e:
            logger.error(
                f"Error downloading from S3 or writing to code interpreter: {str(e)}"
            )
            raise
        finally:
            analytics_logger.log_event(
                "write_query_results_to_code_sandbox", time.time() - start_time
            )

    @tool
    def execute_python(self, code: str, description: str = "") -> str:
        """
        Execute Python code in the code interpreter sandbox.

        The code may read only "query_results.csv" and import only data, charting
        and standard-library computation modules; code that does otherwise is
        returned with an error instead of being run.

        Args:
            code: Python code to execute
            description: Optional description of what the code does

        Returns:
            JSON string containing the execution result
        """
        start_time = time.time()
        try:
            if description:
                code = f"# {description}\n{code}"

            # Checked after the description is prepended, so the text checked is
            # exactly the text that runs.
            try:
                check_sandbox_code(code)
            except SandboxCodeNotAllowedError as e:
                logger.warning(f"Code not run in the sandbox: {e}")
                return json.dumps({"isError": True, "error": str(e)}, indent=2)

            logger.debug(f"Executing Python code: {description}")
            logger.debug(f"Code:\n{code}")

            # Execute code using the invoke_code_interpreter_tool helper
            result = self._invoke_code_interpreter_tool(
                "executeCode",
                {"code": code, "language": "python", "clearContext": False},
            )

            result_json = json.dumps(result, indent=2)
            # analytics_logger.log_content("execute_python", result_json)
            return result_json

        except Exception as e:
            logger.error(f"Error executing code: {str(e)}")
            raise
        finally:
            analytics_logger.log_event("execute_python", time.time() - start_time)
