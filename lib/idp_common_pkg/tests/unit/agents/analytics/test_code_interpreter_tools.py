# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The sandbox tool writes only the result of the query the agent ran last.

``write_query_results_to_code_sandbox`` takes no location from the model. It reads
the object Athena reported for the agent's most recent successful query, and nothing
when there is none.
"""

import inspect
from typing import Any, Tuple
from unittest.mock import MagicMock, patch

import pytest

RESULT_URI = "s3://results-bucket/athena-results/q-1.csv"


def _tools() -> Tuple[Any, MagicMock, MagicMock]:
    # Typed Any: the type checker reads a Strands tool method's signature with
    # `self` unbound, although the descriptor binds it at run time.
    from idp_common.agents.analytics.tools.code_interpreter_tools import (
        CodeInterpreterTools,
    )

    session = MagicMock()
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"a,b\n1,2\n")}
    session.client.return_value = s3
    tools = CodeInterpreterTools(session, region="us-east-1")
    tools._invoke_code_interpreter_tool = MagicMock(return_value={})
    return tools, session, s3


@pytest.mark.unit
class TestWriteQueryResultsToCodeSandbox:
    def test_the_tool_takes_no_arguments(self):
        """The model cannot name what the tool reads. Checked on the undecorated
        function and on the tool spec the model is shown."""
        from idp_common.agents.analytics.tools.code_interpreter_tools import (
            CodeInterpreterTools,
        )

        tool = CodeInterpreterTools.write_query_results_to_code_sandbox
        func = getattr(tool, "_tool_func", None) or getattr(tool, "original_function")
        params = [p for p in inspect.signature(func).parameters if p != "self"]
        assert params == []

        tools, _, _ = _tools()
        spec = tools.write_query_results_to_code_sandbox.tool_spec
        properties = spec["inputSchema"]["json"].get("properties", {})
        assert properties == {}

    def test_refuses_when_no_query_has_run(self):
        tools, session, s3 = _tools()

        message = tools.write_query_results_to_code_sandbox()

        assert "No query results are available" in message
        session.client.assert_not_called()
        s3.get_object.assert_not_called()
        tools._invoke_code_interpreter_tool.assert_not_called()

    def test_reads_the_recorded_result_location(self):
        tools, _, s3 = _tools()
        tools.record_query_result(RESULT_URI, "SELECT 42")

        message = tools.write_query_results_to_code_sandbox()

        assert message.endswith("SELECT 42")
        s3.get_object.assert_called_once_with(
            Bucket="results-bucket", Key="athena-results/q-1.csv"
        )
        written = tools._invoke_code_interpreter_tool.call_args_list[0]
        assert written.args[0] == "writeFiles"
        assert written.args[1]["content"] == [
            {"path": "query_results.csv", "text": "a,b\n1,2\n"}
        ]

    def test_a_cleared_result_is_not_read(self):
        tools, _, s3 = _tools()
        tools.record_query_result(RESULT_URI)
        tools.record_query_result(None)

        message = tools.write_query_results_to_code_sandbox()

        assert "No query results are available" in message
        s3.get_object.assert_not_called()


def _athena_success(uri):
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "q-1"}
    athena.get_query_execution.return_value = {
        "QueryExecution": {
            "Status": {"State": "SUCCEEDED"},
            "ResultConfiguration": {"OutputLocation": uri},
            "Statistics": {},
        }
    }
    athena.get_query_results.return_value = {
        "ResultSet": {"ResultSetMetadata": {"ColumnInfo": []}, "Rows": []}
    }
    return athena


@pytest.mark.unit
class TestAgentWiresTheQueryResultToTheSandboxTool:
    """``create_analytics_agent`` records each query's outcome on the sandbox tool."""

    def _agent_tools(self):
        from idp_common.agents.analytics import agent as agent_module

        session = MagicMock()
        session.region_name = "us-east-1"
        config = {
            "aws_region": "us-east-1",
            "athena_database": "db",
            "athena_output_location": "s3://results-bucket/athena-results/",
        }
        with (
            patch.object(agent_module.strands, "Agent") as agent_cls,
            patch.object(agent_module, "_get_database_overview", return_value=""),
            patch.object(agent_module, "create_strands_bedrock_model"),
            patch.object(agent_module, "register_code_interpreter_tools"),
        ):
            agent_module.create_analytics_agent(config, session)
        tools = agent_cls.call_args.kwargs["tools"]
        by_name = {
            getattr(t, "tool_name", getattr(t, "__name__", "")): t for t in tools
        }
        return by_name

    def test_a_successful_query_is_recorded_and_a_refused_one_clears_it(self):
        tools = self._agent_tools()
        run = tools["run_athena_query_with_config"]
        sandbox_tool = tools["write_query_results_to_code_sandbox"]
        code_tools = sandbox_tool._tool_func.__self__

        with patch("idp_common.agents.analytics.tools.athena_tool.boto3") as mock_boto3:
            mock_boto3.client.return_value = _athena_success(RESULT_URI)
            assert run("SELECT 1")["success"] is True
        assert code_tools._last_query_result_uri == RESULT_URI
        assert code_tools._last_query == "SELECT 1"

        refused = run("DROP TABLE metering")
        assert refused["success"] is False
        assert code_tools._last_query_result_uri is None
