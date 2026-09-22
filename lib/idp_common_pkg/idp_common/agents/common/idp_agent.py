# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
IDPAgent base class that extends Strands Agent with IDP-specific metadata and monitoring.

This module provides the IDPAgent base class which wraps Strands Agent instances with:
1. Metadata (name, description, ID) for agent identification and factory management
2. Automatic monitoring setup when job_id and user_id are provided
3. DynamoDB message persistence for agent conversation tracking
4. Consistent observability across all IDP agents

The monitoring system automatically logs agent conversations to DynamoDB for:
- Real-time progress tracking in the UI
- Debugging and troubleshooting agent behavior
- Audit trails for agent interactions
- Performance monitoring and optimization
"""

import logging
import os
from typing import Any, Optional

from strands import Agent

logger = logging.getLogger(__name__)


class IDPAgent(Agent):
    """
    IDP agent that extends Strands Agent with metadata and automatic monitoring.

    This class wraps existing Strands Agent instances and adds:
    - Agent metadata (name, description, unique ID) for factory management
    - Automatic DynamoDB monitoring when job_id and user_id are provided
    - Consistent observability patterns across all IDP agents
    - Optional MCP client context management for agents using external MCP servers

    The monitoring system enables:
    - Real-time agent conversation tracking in the UI
    - Message persistence for debugging and audit purposes
    - Progress updates during long-running agent operations
    - Consistent logging patterns across different agent types

    Example:
        # Create agent with monitoring (typical Lambda usage)
        agent = IDPAgent(
            agent_name="My Agent",
            agent_description="Does something useful",
            agent_id="my-agent-v1",
            agent=strands_agent,
            job_id="job-123",      # Enables monitoring
            user_id="user-456"     # Required for monitoring
        )

        # Create agent without monitoring (testing/development)
        agent = IDPAgent(
            agent_name="My Agent",
            agent_description="Does something useful",
            agent_id="my-agent-v1",
            agent=strands_agent
        )
    """

    def __init__(
        self,
        agent_name: str,
        agent_description: str,
        agent_id: str,
        agent: Agent,
        sample_queries: Optional[list[str]] = None,
        job_id: Optional[str] = None,
        user_id: Optional[str] = None,
        enable_monitoring: Optional[bool] = None,
        mcp_client: Optional[Any] = None,
    ):
        """
        Initialize IDPAgent with metadata and automatic monitoring setup.

        Args:
            agent_name: Human-readable name for the agent (e.g., "Analytics Agent")
            agent_description: Description of what the agent does
            agent_id: Unique identifier for the agent (e.g., "analytics-20250813-v0")
            agent: Existing Strands Agent instance to wrap (required)
            sample_queries: List of example queries that demonstrate the agent's capabilities
            job_id: Job ID for monitoring purposes. When provided with user_id, enables
                   automatic DynamoDB message tracking for real-time UI updates and
                   debugging. This should be the same job_id used in the Lambda
                   processing pipeline.
            user_id: User ID for monitoring purposes. Required along with job_id to
                    enable monitoring. Used for data isolation and access control.
            enable_monitoring: Whether to enable monitoring. If None, defaults to
                             environment variable ENABLE_AGENT_MONITORING or True
                             when job_id/user_id are provided.
            mcp_client: Optional MCP client for context management

        Note:
            Monitoring requires both job_id and user_id to be provided. The monitoring
            system uses DynamoDB to persist agent conversations, enabling:
            - Real-time progress updates in the UI
            - Message history for debugging
            - Audit trails for compliance
            - Performance monitoring

            If monitoring setup fails, agent creation continues without monitoring
            to ensure robustness in production environments.
        """
        # Initialize as empty Agent first, then copy all attributes from the provided agent
        super().__init__(tools=[], system_prompt="", model=None)
        # Copy all attributes from the existing agent
        for attr, value in agent.__dict__.items():
            setattr(self, attr, value)

        # Set our metadata attributes after copying (to ensure they don't get overwritten)
        self.agent_name = agent_name
        self.agent_description = agent_description
        self.agent_id = agent_id
        self.sample_queries = sample_queries or []
        self.mcp_client = mcp_client
        # The tracker _setup_monitoring registers, kept so that __exit__ can drain its
        # write pool. The registry does keep the tracker alive -- it stores the bound
        # method `on_message_added`, whose `__self__` is the tracker -- but reaching it
        # that way means walking a private callback table and matching on `__self__`,
        # which is not an interface to build teardown on. A named attribute is.
        self.message_tracker: Optional[Any] = None

        # Set up automatic monitoring if job_id and user_id are provided
        self._setup_monitoring(job_id, user_id, enable_monitoring)

    def _setup_monitoring(
        self,
        job_id: Optional[str],
        user_id: Optional[str],
        enable_monitoring: Optional[bool],
    ) -> None:
        """
        Set up DynamoDB monitoring for agent conversations.

        This method automatically configures DynamoDB message tracking when:
        1. job_id and user_id are both provided
        2. enable_monitoring is True (or defaults to True)
        3. AGENT_TABLE environment variable is set

        The monitoring system logs all agent messages (user inputs, agent responses,
        tool calls, etc.) to DynamoDB for real-time UI updates and debugging.

        Args:
            job_id: Job identifier for tracking this agent session
            user_id: User identifier for data isolation and access control
            enable_monitoring: Whether to enable monitoring (None = auto-detect)

        Note:
            If monitoring setup fails, the error is logged but agent creation
            continues to ensure production robustness. The UI will fall back
            to polling-based status updates if monitoring is unavailable.
        """
        # Determine if monitoring should be enabled
        if enable_monitoring is None:
            # Default to environment variable, or True if job_id/user_id provided
            enable_monitoring = (
                os.environ.get("ENABLE_AGENT_MONITORING", "true").lower() == "true"
            )
            if job_id and user_id:
                enable_monitoring = True

        if not enable_monitoring or not job_id or not user_id:
            if enable_monitoring and (not job_id or not user_id):
                logger.warning(
                    "Agent monitoring enabled but job_id or user_id not provided"
                )
            return

        try:
            from .dynamodb_logger import DynamoDBMessageTracker

            # Add DynamoDB message tracker for persistence
            # This enables real-time UI updates and conversation history
            message_tracker = DynamoDBMessageTracker(
                job_id=job_id,
                user_id=user_id,
                enabled=enable_monitoring,
            )
            self.hooks.add_hook(message_tracker)
            self.message_tracker = message_tracker
            logger.info(f"Agent monitoring enabled for job: {job_id}, user: {user_id}")

        except Exception as e:
            logger.error(f"Failed to set up agent monitoring: {e}")
            # Don't fail agent creation if monitoring setup fails - this ensures
            # production robustness even if monitoring infrastructure has issues

    def __enter__(self):
        """Context manager entry - manages MCP client lifecycle if present."""
        if self.mcp_client:
            self.mcp_client.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Context manager exit - drains transcript writes, then closes the MCP client.

        This is the boundary at which the agent is finished with, and it is where a
        queued transcript write is waited for. The tracker's messages are written on a
        thread pool, and a Lambda invocation ends by the execution environment being
        **frozen** rather than shut down: the process is not signalled and does not
        exit, so an unfinished write is simply suspended and no interpreter-shutdown
        hook runs. See ``DynamoDBMessageLogger.shutdown`` for the full account, for why
        the drain is bounded rather than unconditional, and for why this is the right
        boundary rather than the registry's own ``AfterInvocationEvent``.

        Every tracker built in production belongs to an agent that passes through here,
        and today that is the analytics path only: ``agent_processor``'s top-level agent
        inside its ``with agent:``, and the sub-agents its orchestrator runs, each
        inside a ``with specialized_agent:``. Agent chat builds no tracker at all -- it
        passes neither ``job_id`` nor ``user_id``, and its functions have no
        ``AGENT_TABLE`` -- so nothing there is waiting to be drained.

        Draining before closing the MCP client, because the two are unrelated and MCP
        teardown is the half that has been seen to raise.
        """
        if self.message_tracker:
            try:
                self.message_tracker.shutdown()
            except Exception as e:
                logger.warning(f"Error draining agent transcript writes: {e}")
                # Don't propagate monitoring cleanup errors: monitoring is
                # best-effort and must not turn a completed agent run into a failure.

        if self.mcp_client:
            try:
                self.mcp_client.__exit__(exc_type, exc_val, exc_tb)
            except Exception as e:
                logger.warning(f"Error closing MCP client: {e}")
                # Don't propagate MCP cleanup errors
