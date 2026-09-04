# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the conversational orchestrator creation.

Tests the create_conversational_orchestrator() method to ensure:
- Memory hooks are properly attached
- Conversation manager is configured
- Environment variables are read correctly
- Agent creation works with valid inputs
"""

# ruff: noqa: E402, I001
# The above line disables E402 (module level import not at top of file) and I001 (import block sorting) for this file

import os
from unittest.mock import MagicMock, patch

import pytest

# `strands` and `bedrock_agentcore` are stubbed by tests/conftest.py, and ONLY
# when genuinely absent. This module used to force MagicMocks into `sys.modules`
# here instead, unconditionally and without ever restoring them — which replaced
# a real installed `strands` for every test module imported after this one, so
# whether the agentic tests ran against the real library depended on collection
# order. See the note in tests/conftest.py.


@pytest.fixture
def setup_env():
    """Set up environment variables for testing."""
    env_vars = {
        "ID_HELPER_CHAT_MEMORY_TABLE": "test-memory-table",
        "BEDROCK_REGION": "us-west-2",
        "MAX_MESSAGE_SIZE_KB": "8.5",
        "MAX_CONVERSATION_TURNS": "20",
        "DOCUMENT_ANALYSIS_AGENT_MODEL_ID": "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
    }
    with patch.dict(os.environ, env_vars, clear=True):
        yield


@pytest.fixture
def test_agent():
    """Register a test agent and clean up after."""
    from idp_common.agents.factory import agent_factory

    def create_test_agent(**kwargs):
        mock_agent = MagicMock()
        mock_agent.invoke = MagicMock(return_value="Test response")
        return mock_agent

    agent_factory.register_agent(
        agent_id="test-agent",
        agent_name="Test Agent",
        agent_description="A test agent for unit testing",
        creator_func=create_test_agent,
        sample_queries=["test query"],
    )

    yield

    # Clean up
    if "test-agent" in agent_factory._registry:
        del agent_factory._registry["test-agent"]


@pytest.mark.unit
class TestConversationalOrchestrator:
    """Test cases for conversational orchestrator creation."""

    @patch(
        "idp_common.agents.utils.conversation_manager.DropAndSlideConversationManager"
    )
    @patch("idp_common.agents.utils.memory_provider.DynamoDBMemoryHookProvider")
    @patch("idp_common.agents.orchestrator.agent.create_orchestrator_agent")
    def test_create_conversational_orchestrator_basic(
        self,
        mock_create_orchestrator,
        mock_memory_provider,
        mock_conversation_manager,
        setup_env,
        test_agent,
    ):
        """Test basic conversational orchestrator creation."""
        from idp_common.agents.factory import agent_factory

        # Mock the base orchestrator
        mock_orchestrator = MagicMock()
        mock_create_orchestrator.return_value = mock_orchestrator

        # Mock memory provider and conversation manager
        mock_memory_instance = MagicMock()
        mock_memory_provider.return_value = mock_memory_instance
        mock_conv_manager_instance = MagicMock()
        mock_conversation_manager.return_value = mock_conv_manager_instance

        # Create conversational orchestrator
        config = {"test": "config"}
        session = MagicMock()
        session_id = "test-session-123"

        result = agent_factory.create_conversational_orchestrator(
            agent_ids=["test-agent"],
            session_id=session_id,
            config=config,
            session=session,
        )

        # Verify orchestrator was created
        assert result is not None
        mock_create_orchestrator.assert_called_once()

        # Verify hooks were passed during orchestrator creation (not added afterwards)
        call_args = mock_create_orchestrator.call_args
        assert "hooks" in call_args.kwargs
        hooks_passed = call_args.kwargs["hooks"]
        assert len(hooks_passed) == 1  # Should have memory hook
        assert hooks_passed[0] == mock_memory_instance

        # Verify conversation manager was set
        assert mock_orchestrator.conversation_manager == mock_conv_manager_instance

    @patch(
        "idp_common.agents.utils.conversation_manager.DropAndSlideConversationManager"
    )
    @patch("idp_common.agents.utils.memory_provider.DynamoDBMemoryHookProvider")
    @patch("idp_common.agents.orchestrator.agent.create_orchestrator_agent")
    def test_memory_provider_configuration(
        self,
        mock_create_orchestrator,
        mock_memory_provider,
        mock_conversation_manager,
        setup_env,
        test_agent,
    ):
        """Test that memory provider is configured with correct parameters."""
        from idp_common.agents.factory import agent_factory

        mock_orchestrator = MagicMock()
        mock_orchestrator.hooks = MagicMock()
        mock_create_orchestrator.return_value = mock_orchestrator

        mock_memory_instance = MagicMock()
        mock_memory_provider.return_value = mock_memory_instance
        mock_conv_manager_instance = MagicMock()
        mock_conversation_manager.return_value = mock_conv_manager_instance

        config = {"test": "config"}
        session = MagicMock()
        session_id = "test-session-456"

        agent_factory.create_conversational_orchestrator(
            agent_ids=["test-agent"],
            session_id=session_id,
            config=config,
            session=session,
        )

        # Verify memory provider was created with correct parameters
        mock_memory_provider.assert_called_once_with(
            table_name="test-memory-table",
            session_id=session_id,
            region_name="us-west-2",
            max_message_size_kb=8.5,
            max_history_turns=20,
        )

    @patch(
        "idp_common.agents.utils.conversation_manager.DropAndSlideConversationManager"
    )
    @patch("idp_common.agents.utils.memory_provider.DynamoDBMemoryHookProvider")
    @patch("idp_common.agents.orchestrator.agent.create_orchestrator_agent")
    def test_conversation_manager_configuration(
        self,
        mock_create_orchestrator,
        mock_memory_provider,
        mock_conversation_manager,
        setup_env,
        test_agent,
    ):
        """Test that conversation manager is configured correctly."""
        from idp_common.agents.factory import agent_factory

        mock_orchestrator = MagicMock()
        mock_orchestrator.hooks = MagicMock()
        mock_create_orchestrator.return_value = mock_orchestrator

        mock_memory_instance = MagicMock()
        mock_memory_provider.return_value = mock_memory_instance
        mock_conv_manager_instance = MagicMock()
        mock_conversation_manager.return_value = mock_conv_manager_instance

        config = {"test": "config"}
        session = MagicMock()
        session_id = "test-session-789"

        result = agent_factory.create_conversational_orchestrator(
            agent_ids=["test-agent"],
            session_id=session_id,
            config=config,
            session=session,
        )

        # Verify conversation manager was created
        mock_conversation_manager.assert_called_once()

        # Verify conversation manager was set on orchestrator
        assert result.conversation_manager == mock_conv_manager_instance

    def test_invalid_agent_id(self, setup_env, test_agent):
        """Test that invalid agent IDs raise ValueError."""
        from idp_common.agents.factory import agent_factory

        config = {"test": "config"}
        session = MagicMock()
        session_id = "test-session-invalid"

        with pytest.raises(ValueError) as exc_info:
            agent_factory.create_conversational_orchestrator(
                agent_ids=["non-existent-agent"],
                session_id=session_id,
                config=config,
                session=session,
            )

        assert "not found in registry" in str(exc_info.value)

    @patch(
        "idp_common.agents.utils.conversation_manager.DropAndSlideConversationManager"
    )
    @patch("idp_common.agents.orchestrator.agent.create_orchestrator_agent")
    def test_missing_memory_table_env_var(
        self, mock_create_orchestrator, mock_conversation_manager, test_agent
    ):
        """Test graceful handling when memory table env var is missing."""
        from idp_common.agents.factory import agent_factory

        # Set up environment without memory table
        env_vars = {
            "BEDROCK_REGION": "us-west-2",
            "MAX_MESSAGE_SIZE_KB": "8.5",
            "MAX_CONVERSATION_TURNS": "20",
            "DOCUMENT_ANALYSIS_AGENT_MODEL_ID": "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        }

        with patch.dict(os.environ, env_vars, clear=True):
            mock_orchestrator = MagicMock()
            mock_orchestrator.hooks = MagicMock()
            mock_create_orchestrator.return_value = mock_orchestrator

            mock_conv_manager_instance = MagicMock()
            mock_conversation_manager.return_value = mock_conv_manager_instance

            config = {"test": "config"}
            session = MagicMock()
            session_id = "test-session-no-memory"

            # Should not raise an exception, just log a warning
            result = agent_factory.create_conversational_orchestrator(
                agent_ids=["test-agent"],
                session_id=session_id,
                config=config,
                session=session,
            )

            # Should still return an orchestrator
            assert result is not None

    @patch(
        "idp_common.agents.utils.conversation_manager.DropAndSlideConversationManager"
    )
    @patch("idp_common.agents.utils.memory_provider.DynamoDBMemoryHookProvider")
    @patch("idp_common.agents.orchestrator.agent.create_orchestrator_agent")
    def test_multiple_agents(
        self,
        mock_create_orchestrator,
        mock_memory_provider,
        mock_conversation_manager,
        setup_env,
        test_agent,
    ):
        """Test creating orchestrator with multiple agents."""
        from idp_common.agents.factory import agent_factory

        # Register another test agent
        def create_test_agent2(**kwargs):
            mock_agent = MagicMock()
            return mock_agent

        agent_factory.register_agent(
            agent_id="test-agent-2",
            agent_name="Test Agent 2",
            agent_description="Another test agent",
            creator_func=create_test_agent2,
        )

        try:
            mock_orchestrator = MagicMock()
            mock_orchestrator.hooks = MagicMock()
            mock_create_orchestrator.return_value = mock_orchestrator

            mock_memory_instance = MagicMock()
            mock_memory_provider.return_value = mock_memory_instance
            mock_conv_manager_instance = MagicMock()
            mock_conversation_manager.return_value = mock_conv_manager_instance

            config = {"test": "config"}
            session = MagicMock()
            session_id = "test-session-multi"

            result = agent_factory.create_conversational_orchestrator(
                agent_ids=["test-agent", "test-agent-2"],
                session_id=session_id,
                config=config,
                session=session,
            )

            # Verify orchestrator was created with both agents
            assert result is not None
            call_args = mock_create_orchestrator.call_args
            assert call_args[1]["agent_ids"] == ["test-agent", "test-agent-2"]

        finally:
            # Clean up
            if "test-agent-2" in agent_factory._registry:
                del agent_factory._registry["test-agent-2"]

    @patch(
        "idp_common.agents.utils.conversation_manager.DropAndSlideConversationManager"
    )
    @patch("idp_common.agents.utils.memory_provider.DynamoDBMemoryHookProvider")
    @patch("idp_common.agents.orchestrator.agent.create_orchestrator_agent")
    def test_returns_raw_strands_agent(
        self,
        mock_create_orchestrator,
        mock_memory_provider,
        mock_conversation_manager,
        setup_env,
        test_agent,
    ):
        """Test that the method returns a raw Strands agent, not IDPAgent wrapper."""
        from idp_common.agents.factory import agent_factory

        mock_orchestrator = MagicMock()
        mock_orchestrator.hooks = MagicMock()
        mock_create_orchestrator.return_value = mock_orchestrator

        mock_memory_instance = MagicMock()
        mock_memory_provider.return_value = mock_memory_instance
        mock_conv_manager_instance = MagicMock()
        mock_conversation_manager.return_value = mock_conv_manager_instance

        config = {"test": "config"}
        session = MagicMock()
        session_id = "test-session-raw"

        result = agent_factory.create_conversational_orchestrator(
            agent_ids=["test-agent"],
            session_id=session_id,
            config=config,
            session=session,
        )

        # Should return the mock orchestrator directly, not wrapped
        assert result == mock_orchestrator

        # Verify it's not wrapped by checking it's the exact same object
        assert result is mock_orchestrator
