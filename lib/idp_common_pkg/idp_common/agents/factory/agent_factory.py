# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Factory for creating IDP agents.
"""

from typing import Any, Callable, Dict, List, Optional

from ..common.idp_agent import IDPAgent


class IDPAgentFactory:
    """Factory for creating and managing IDP agents."""

    def __init__(self):
        """Initialize the factory with an empty registry."""
        self._registry: Dict[str, Dict[str, Any]] = {}

    def register_agent(
        self,
        agent_id: str,
        agent_name: str,
        agent_description: str,
        creator_func: Callable[
            ..., Any
        ],  # Now returns Strands Agent instead of IDPAgent
        sample_queries: List[str] = None,
        opt_in: Optional[str] = None,
    ) -> None:
        """
        Register an agent creator function with metadata.

        Args:
            agent_id: Unique identifier for the agent
            agent_name: Human-readable name for the agent
            agent_description: Description of what the agent does
            creator_func: Function that creates and returns a Strands Agent instance
            sample_queries: List of example queries for the agent
            opt_in: For an agent that sends its query to a service outside the
                deployment, the request flag that must opt in to it
                (``idp_common.chat_session_agents.OPT_IN_*``). Such an agent is
                left out of a chat orchestrator unless the request opts in, and
                always in a session holding Analytics or Error Analyzer results.
        """
        self._registry[agent_id] = {
            "agent_name": agent_name,
            "agent_description": agent_description,
            "creator_func": creator_func,
            "sample_queries": sample_queries or [],
            "opt_in": opt_in,
        }

    def list_available_agents(self) -> List[Dict[str, Any]]:
        """
        List all available agents with their metadata.

        Returns:
            List of dicts containing agent_id, agent_name, agent_description,
            sample_queries, and opt_in (``None`` for an agent that needs none)
        """
        return [
            {
                "agent_id": agent_id,
                "agent_name": info["agent_name"],
                "agent_description": info["agent_description"],
                "sample_queries": info["sample_queries"],
                "opt_in": info.get("opt_in"),
            }
            for agent_id, info in self._registry.items()
        ]

    def create_agent(self, agent_id: str, **kwargs) -> IDPAgent:
        """
        Create an agent instance by ID.

        Args:
            agent_id: The ID of the agent to create
            **kwargs: Arguments to pass to the agent creator function

        Returns:
            IDPAgent instance with registered metadata

        Raises:
            ValueError: If agent_id is not registered
        """
        if agent_id not in self._registry:
            raise ValueError(f"Agent ID '{agent_id}' not found in registry")

        info = self._registry[agent_id]
        creator_func = info["creator_func"]

        # Call creator function which now returns a Strands agent
        result = creator_func(**kwargs)

        # Handle different return formats from creator functions
        if isinstance(result, tuple):
            # External MCP agent returns (strands_agent, mcp_client)
            strands_agent, mcp_client = result
        else:
            # Other agents return just the strands_agent
            strands_agent = result
            mcp_client = None

        # Extract job_id and user_id from kwargs if provided
        job_id = kwargs.get("job_id")
        user_id = kwargs.get("user_id")

        # Wrap it in IDPAgent with the registered metadata
        return IDPAgent(
            agent=strands_agent,
            agent_id=agent_id,
            agent_name=info["agent_name"],
            agent_description=info["agent_description"],
            sample_queries=info["sample_queries"],
            mcp_client=mcp_client,
            job_id=job_id,
            user_id=user_id,
        )

    def create_orchestrator_agent(self, agent_ids: List[str], **kwargs) -> IDPAgent:
        """
        Create an orchestrator agent that can route queries to multiple specialized agents.

        Args:
            agent_ids: List of agent IDs to include as tools in the orchestrator
            **kwargs: Arguments to pass to the orchestrator and specialized agents

        Returns:
            IDPAgent instance configured as an orchestrator

        Raises:
            ValueError: If any agent_id is not registered
        """
        # Validate all agent IDs exist
        for agent_id in agent_ids:
            if agent_id not in self._registry:
                raise ValueError(f"Agent ID '{agent_id}' not found in registry")

        # Import orchestrator here to avoid circular imports
        from ..orchestrator.agent import create_orchestrator_agent

        # Create the orchestrator agent
        orchestrator_agent = create_orchestrator_agent(agent_ids=agent_ids, **kwargs)

        # Create orchestrator metadata
        agent_names = [self._registry[aid]["agent_name"] for aid in agent_ids]
        orchestrator_description = (
            f"Orchestrator agent that routes queries to: {', '.join(agent_names)}"
        )

        # Wrap in IDPAgent
        return IDPAgent(
            agent=orchestrator_agent,
            agent_id=f"orchestrator-{'-'.join(agent_ids)}",
            agent_name="Orchestrator Agent",
            agent_description=orchestrator_description,
            sample_queries=[],
        )

    def create_conversational_agent(
        self,
        agent_id: str,
        session_id: str,
        config: Dict[str, Any],
        session: Any,
        user_id: str = "",
        **kwargs,
    ) -> Any:
        """Create a single specialized agent as the top-level conversational agent.

        Use this instead of the orchestrator when a mode pins to exactly one
        agent (e.g. Quick Start) and that agent is itself multi-turn. Returns the
        raw Strands Agent (not wrapped in IDPAgent), matching
        create_conversational_orchestrator.

        ``user_id`` is the signed-in user the conversation belongs to (their
        Cognito ``sub``). Memory is stored per user and session, so with no
        ``user_id`` the agent is built without memory: it neither loads nor
        stores any earlier turn.
        """
        import logging
        import os

        logger = logging.getLogger(__name__)

        if agent_id not in self._registry:
            raise ValueError(f"Agent ID '{agent_id}' not found in registry")

        from ..utils.conversation_manager import DropAndSlideConversationManager
        from ..utils.memory_provider import DynamoDBMemoryHookProvider

        logger.info(
            f"Creating conversational agent '{agent_id}' for session {session_id}"
        )

        memory_table_name = os.environ.get("ID_HELPER_CHAT_MEMORY_TABLE")
        hooks = []
        if not memory_table_name:
            logger.warning(
                "ID_HELPER_CHAT_MEMORY_TABLE not set, memory will not be persisted"
            )
        elif not user_id:
            logger.warning(
                "No user for session %s; building the agent without memory",
                session_id,
            )
        else:
            hooks.append(
                DynamoDBMemoryHookProvider(
                    table_name=memory_table_name,
                    session_id=session_id,
                    user_id=user_id,
                    region_name=os.environ.get("BEDROCK_REGION", "us-east-1"),
                    max_message_size_kb=float(
                        os.environ.get("MAX_MESSAGE_SIZE_KB", "8.5")
                    ),
                    max_history_turns=int(
                        os.environ.get("MAX_CONVERSATION_TURNS", "20")
                    ),
                )
            )
            logger.info(f"Created memory provider for session {session_id}")

        info = self._registry[agent_id]
        creator_func = info["creator_func"]
        agent = creator_func(config=config, session=session, hooks=hooks, **kwargs)

        agent.conversation_manager = DropAndSlideConversationManager(
            tools_to_drop=(),
            keep_call_stub=True,
            window_size=int(os.environ.get("MAX_CONVERSATION_TURNS", "20")),
            should_truncate_results=True,
        )

        logger.info(
            f"Conversational agent '{agent_id}' created for session {session_id}"
        )
        return agent

    def create_conversational_orchestrator(
        self,
        agent_ids: List[str],
        session_id: str,
        config: Dict[str, Any],
        session: Any,
        user_id: str = "",
        **kwargs,
    ) -> Any:
        """
        Create an orchestrator agent with memory and conversation management for multi-turn chat.

        This method creates an orchestrator configured for conversational interactions:
        - Adds DynamoDB-based memory for conversation history
        - Adds conversation manager to optimize context size
        - Configures for streaming responses
        - Enables multi-turn conversations with context

        Args:
            agent_ids: List of agent IDs to include as tools in the orchestrator
            session_id: Session ID for conversation memory
            config: Configuration dictionary
            user_id: The signed-in user the conversation belongs to (their
                Cognito ``sub``). Memory is keyed on user and session; with no
                ``user_id`` the orchestrator is built without memory.
            session: Boto3 session for AWS operations
            **kwargs: Additional arguments passed to orchestrator creation

        Returns:
            Strands Agent instance configured for conversational use (not wrapped in IDPAgent)

        Raises:
            ValueError: If any agent_id is not registered

        Example:
            orchestrator = factory.create_conversational_orchestrator(
                agent_ids=["document-analysis", "analytics"],
                session_id="user-session-123",
                config=config,
                session=boto3.Session(),
                user_id="<cognito sub>",
            )

            # Use with streaming
            async for event in orchestrator.stream_async(prompt):
                if "data" in event:
                    print(event["data"])
        """
        import logging
        import os

        logger = logging.getLogger(__name__)

        # Validate all agent IDs exist
        for agent_id in agent_ids:
            if agent_id not in self._registry:
                raise ValueError(f"Agent ID '{agent_id}' not found in registry")

        # Import orchestrator and utilities here to avoid circular imports
        from ..orchestrator.agent import create_orchestrator_agent
        from ..utils.conversation_manager import DropAndSlideConversationManager
        from ..utils.memory_provider import DynamoDBMemoryHookProvider

        logger.info(
            f"Creating conversational orchestrator for session {session_id} with agents: {agent_ids}"
        )

        # Get memory table name from environment and create memory provider BEFORE agent creation
        memory_table_name = os.environ.get("ID_HELPER_CHAT_MEMORY_TABLE")
        hooks = []

        if not memory_table_name:
            logger.warning(
                "ID_HELPER_CHAT_MEMORY_TABLE not set, memory will not be persisted"
            )
        elif not user_id:
            logger.warning(
                "No user for session %s; building the orchestrator without memory",
                session_id,
            )
        else:
            # Create memory hook for conversation history
            memory_provider = DynamoDBMemoryHookProvider(
                table_name=memory_table_name,
                session_id=session_id,
                user_id=user_id,
                region_name=os.environ.get("BEDROCK_REGION", "us-east-1"),
                max_message_size_kb=float(os.environ.get("MAX_MESSAGE_SIZE_KB", "8.5")),
                max_history_turns=int(os.environ.get("MAX_CONVERSATION_TURNS", "20")),
            )
            hooks.append(memory_provider)
            logger.info(f"Created memory provider for session {session_id}")

        # Create the orchestrator agent with hooks passed during creation
        # This ensures AgentInitializedEvent fires and memory is loaded automatically
        orchestrator_agent = create_orchestrator_agent(
            agent_ids=agent_ids,
            config=config,
            session=session,
            hooks=hooks,  # Pass hooks during creation
            **kwargs,
        )

        # Add conversation manager to optimize context
        # Configure to drop verbose tool results but keep sub-agent responses
        orchestrator_agent.conversation_manager = DropAndSlideConversationManager(
            tools_to_drop=(),  # Don't drop sub-agent responses for orchestrator
            keep_call_stub=True,
            window_size=int(os.environ.get("MAX_CONVERSATION_TURNS", "20")),
            should_truncate_results=True,
        )
        logger.info("Added conversation manager with sliding window")

        logger.info(
            f"Conversational orchestrator created successfully for session {session_id}"
        )

        # Return the raw Strands agent (not wrapped in IDPAgent)
        # This is because the conversational system doesn't use the IDPAgent wrapper
        return orchestrator_agent
