# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for main Error Analyzer Agent.
"""

# ruff: noqa: E402, I001
# The above line disables E402 (module level import not at top of file) and I001 (import block sorting) for this file

from unittest.mock import patch

import pytest
from idp_common.agents.error_analyzer import tools as error_analyzer_tools
from idp_common.config.models import IDPConfig


def _exported_tools() -> dict:
    """The tool objects the tools package publishes, keyed by exported name.

    This is the independent expectation for what the factory must wire up. The
    count the factory passes is deliberately NOT restated as a literal here:
    a literal is what let the previous version of these tests assert 7 while
    production passed 9 (#1129). Identity comparison is used rather than a
    name derived from the object, so the assertion holds whether or not the
    strands ``@tool`` decorator is the real one (conftest stubs the package
    only when it is genuinely absent).
    """
    return {
        name: getattr(error_analyzer_tools, name)
        for name in error_analyzer_tools.__all__
    }


def _tools_passed_to(mock_agent_class) -> list:
    mock_agent_class.assert_called_once()
    kwargs = mock_agent_class.call_args.kwargs
    assert "tools" in kwargs, "the factory must pass its tools by keyword"
    return list(kwargs["tools"])


@pytest.mark.unit
class TestErrorAnalyzerAgent:
    """Test main error analyzer agent."""

    @patch("idp_common.agents.error_analyzer.agent.strands.Agent")
    @patch("boto3.Session")
    @patch("idp_common.agents.error_analyzer.agent.get_config")
    def test_agent_is_given_every_published_tool(
        self, mock_get_config, mock_session, mock_agent_class
    ):
        """Every tool the tools package publishes is wired into the agent.

        The predicate is over the recorded call to ``strands.Agent``, not over
        the object the patched constructor hands back -- that object is the
        test's own fixture, so anything read off it is whatever the test put
        there.
        """
        from idp_common.agents.error_analyzer.agent import create_error_analyzer_agent

        mock_get_config.return_value = IDPConfig()

        create_error_analyzer_agent(session=mock_session.return_value)

        passed = _tools_passed_to(mock_agent_class)
        exported = _exported_tools()

        wired = sorted(
            name
            for name, obj in exported.items()
            if any(obj is tool for tool in passed)
        )
        assert wired == sorted(exported), (
            "tools published by idp_common.agents.error_analyzer.tools but not "
            f"passed to the agent: {sorted(set(exported) - set(wired))}"
        )
        assert len(passed) == len(exported), (
            f"the agent was passed {len(passed)} tools but the tools package "
            f"publishes {len(exported)}: an unpublished or duplicated tool is "
            "in the list"
        )

    @patch("idp_common.agents.error_analyzer.agent.strands.Agent")
    @patch("boto3.Session")
    @patch("idp_common.agents.error_analyzer.agent.get_config")
    def test_a_supplied_session_is_used_rather_than_a_new_one(
        self, mock_get_config, mock_session, mock_agent_class
    ):
        """A caller-supplied session is honoured, and the Agent is returned."""
        from idp_common.agents.error_analyzer.agent import create_error_analyzer_agent

        mock_get_config.return_value = IDPConfig()
        supplied = mock_session.return_value

        agent = create_error_analyzer_agent(session=supplied)

        mock_session.assert_not_called()
        assert agent is mock_agent_class.return_value

    @patch(
        "idp_common.agents.error_analyzer.agent.create_strands_bedrock_model",
    )
    @patch("idp_common.agents.error_analyzer.agent.strands.Agent")
    @patch("boto3.Session")
    @patch("idp_common.agents.error_analyzer.agent.get_config")
    def test_a_session_is_created_when_none_is_supplied(
        self, mock_get_config, mock_session, mock_agent_class, mock_model
    ):
        """With no session argument the factory builds one and threads it on.

        Asserting only that a session was constructed would leave the branch
        half-covered: the interesting part is that the new session reaches the
        Bedrock model, since a session built and then dropped would still
        satisfy the construction call.
        """
        from idp_common.agents.error_analyzer.agent import create_error_analyzer_agent

        mock_get_config.return_value = IDPConfig()

        create_error_analyzer_agent()

        mock_session.assert_called_once_with()
        assert mock_model.call_args.kwargs["boto_session"] is mock_session.return_value
        assert mock_agent_class.call_args.kwargs["model"] is mock_model.return_value

    @patch("idp_common.agents.error_analyzer.agent.strands.Agent")
    @patch("boto3.Session")
    @patch("idp_common.agents.error_analyzer.agent.get_config")
    def test_agent_system_prompt_format(
        self, mock_get_config, mock_session, mock_agent_class
    ):
        """Test that agent is created with correct system prompt format."""
        from idp_common.agents.error_analyzer.agent import create_error_analyzer_agent

        mock_get_config.return_value = IDPConfig()

        create_error_analyzer_agent()

        # Verify strands.Agent was called with correct parameters
        mock_agent_class.assert_called_once()
        call_args = mock_agent_class.call_args

        assert "tools" in call_args.kwargs
        assert "system_prompt" in call_args.kwargs
        assert "model" in call_args.kwargs

        # Check system prompt contains required sections
        system_prompt = call_args.kwargs["system_prompt"]
        assert "Root Cause" in system_prompt
        assert "Recommendations" in system_prompt
        assert "Do NOT include" in system_prompt

    def test_specific_tools_import(self):
        """Test that specific tools can be imported correctly."""
        from idp_common.agents.error_analyzer.tools import (
            analyze_workflow_execution,
            fetch_document_record,
            search_cloudwatch_logs,
        )

        assert search_cloudwatch_logs is not None
        assert callable(search_cloudwatch_logs)
        assert fetch_document_record is not None
        assert callable(fetch_document_record)
        assert analyze_workflow_execution is not None
        assert callable(analyze_workflow_execution)
