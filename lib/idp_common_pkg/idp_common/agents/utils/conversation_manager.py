# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Conversation Manager for Agent System

This module provides conversation management strategies for conversational agents.
Copied and adapted from code_intel system for use with the agent chat system.

Key Features:
- Drops tool results from specified tools to reduce context size
- Applies sliding window management to keep conversations within token limits
- Preserves important context while removing verbose tool outputs
- Configurable tool dropping and window size

Usage:
    from idp_common.agents.utils.conversation_manager import DropAndSlideConversationManager

    conversation_manager = DropAndSlideConversationManager(
        tools_to_drop=("read_multiple_files", "search_files"),
        keep_call_stub=True,
        window_size=20,
        should_truncate_results=True
    )

    # Add to agent
    agent.conversation_manager = conversation_manager

``MeteredSummarizingConversationManager`` is the summarizing manager to use for
any agent whose usage is metered: it is Strands' ``SummarizingConversationManager``
with the summarization call's token usage added to the agent's
``event_loop_metrics``, which Strands' own default summarization path skips.
"""

import logging
from typing import Any, AsyncIterator, Callable, Iterable

from strands.agent.conversation_manager import (
    SlidingWindowConversationManager,
    SummarizingConversationManager,
)
from strands.event_loop.streaming import extract_usage_metrics
from strands.types.content import Message
from strands.types.event_loop import Usage

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class DropAndSlideConversationManager(SlidingWindowConversationManager):
    """
    Conversation manager that drops tool results and applies sliding window.

    This manager implements a two-phase strategy for managing conversation context:

    Phase 1 - Drop Tool Results:
    - Removes verbose tool results from specified tools (e.g., file reading tools)
    - Optionally keeps a small "breadcrumb" message indicating the tool was used
    - Significantly reduces context size while preserving conversation flow

    Phase 2 - Sliding Window:
    - Applies standard sliding window management from parent class
    - Keeps only the most recent N turns in the conversation
    - Truncates long tool results if configured

    This approach is particularly useful for agents that use tools with large outputs
    (like reading multiple files) where the full output isn't needed in subsequent turns.

    Example:
        Without dropping:
        - User: "Read file1.py and file2.py"
        - Assistant: [calls read_multiple_files]
        - User: [tool result with 50KB of file contents]
        - Assistant: "I've analyzed the files..."

        With dropping:
        - User: "Read file1.py and file2.py"
        - Assistant: "[read_multiple_files used; output discarded]"
        - Assistant: "I've analyzed the files..."

        The agent still processed the files, but we don't keep the verbose output
        in the conversation history for future turns.
    """

    def __init__(
        self,
        tools_to_drop: Iterable[str] = ("read_multiple_files",),
        keep_call_stub: bool = True,
        **kwargs: Any,  # pass window_size, should_truncate_results, etc.
    ):
        """
        Initialize the DropAndSlideConversationManager.

        Args:
            tools_to_drop: Iterable of tool names whose results should be dropped.
                          Common examples: "read_multiple_files", "search_files", "list_directory"
            keep_call_stub: If True, replace dropped tool calls with a small breadcrumb message
                          indicating the tool was used. If False, remove the message entirely.
            **kwargs: Additional arguments passed to SlidingWindowConversationManager:
                     - window_size: Number of recent turns to keep (default: 20)
                     - should_truncate_results: Whether to truncate long tool results (default: True)
        """
        super().__init__(**kwargs)
        self.tools_to_drop = set(tools_to_drop)
        self.keep_call_stub = keep_call_stub

        logger.info(
            f"Conversation Manager initialized: dropping tools={list(tools_to_drop)}, "
            f"keep_stub={keep_call_stub}, window_size={kwargs.get('window_size', 'default')}"
        )

    def apply_management(self, agent, **kwargs):
        """
        Apply conversation management to the agent's message history.

        This method is called by the agent framework to manage the conversation context.
        It performs two operations in sequence:
        1. Drop tool results from specified tools
        2. Apply sliding window management

        Args:
            agent: The agent instance whose messages will be managed
            **kwargs: Additional arguments (passed to parent class)
        """
        # Phase 1: Remove results from the specified tools
        to_drop_ids = set()
        new_msgs = []

        for msg in agent.messages:
            role = msg.get("role")
            content = msg.get("content", [])

            if role == "assistant":
                # Find tool uses in assistant messages
                tool_uses = [
                    cb for cb in content if isinstance(cb, dict) and "toolUse" in cb
                ]
                # Check if any match our tools to drop
                matching = [
                    cb
                    for cb in tool_uses
                    if cb["toolUse"]["name"] in self.tools_to_drop
                ]

                if matching:
                    # Track the tool use IDs so we can drop their results
                    for cb in matching:
                        to_drop_ids.add(cb["toolUse"]["toolUseId"])

                    # Optionally keep a breadcrumb message
                    if self.keep_call_stub:
                        tool_names = {cb["toolUse"]["name"] for cb in matching}
                        new_msgs.append(
                            {
                                "role": "assistant",
                                "content": [
                                    {
                                        "text": f"[{', '.join(tool_names)} used; output discarded]"
                                    }
                                ],
                            }
                        )
                    # Skip the original assistant toolUse message
                    continue

            if role == "user":
                # Filter out tool results that match our dropped tool IDs
                kept = []
                for cb in content:
                    if isinstance(cb, dict) and "toolResult" in cb:
                        if cb["toolResult"].get("toolUseId") in to_drop_ids:
                            # Drop this toolResult block
                            logger.debug(
                                f"Dropping tool result for toolUseId: {cb['toolResult'].get('toolUseId')}"
                            )
                            continue
                    kept.append(cb)

                # Only keep the user message if it has content left
                if kept:
                    new_msgs.append({"role": "user", "content": kept})
                # If nothing left, drop the whole message
                continue

            # Keep all other messages as-is
            new_msgs.append(msg)

        # Update agent's messages with the filtered list
        agent.messages = new_msgs

        logger.debug(
            f"Dropped {len(to_drop_ids)} tool results, "
            f"reduced from {len(agent.messages)} to {len(new_msgs)} messages"
        )

        # Phase 2: Apply sliding window behavior (windowing, overflow trimming, truncation)
        super().apply_management(agent, **kwargs)

    def reduce_context(self, agent, e=None, **kwargs):
        """
        Reduce context when the conversation exceeds limits.

        This method is called by the agent framework when the conversation
        becomes too large (e.g., exceeds token limits). It delegates to the
        parent class's implementation.

        Args:
            agent: The agent instance whose context will be reduced
            e: Optional exception that triggered the context reduction
            **kwargs: Additional arguments
        """
        logger.info("Reducing conversation context due to size limits")
        super().reduce_context(agent, e, **kwargs)


class AggressiveDropConversationManager(DropAndSlideConversationManager):
    """
    More aggressive conversation manager that drops multiple tool types.

    This variant drops results from more tools to keep context even smaller.
    Useful for agents that use many different tools with large outputs.

    Example:
        manager = AggressiveDropConversationManager(
            window_size=15,  # Smaller window
            should_truncate_results=True
        )
    """

    def __init__(self, **kwargs):
        """
        Initialize with a predefined set of tools to drop.

        Drops results from:
        - read_multiple_files: Large file contents
        - search_files: Large search results
        - list_directory: Long directory listings
        - file_read: Individual file contents

        Args:
            **kwargs: Arguments passed to parent class
        """
        super().__init__(
            tools_to_drop=(
                "read_multiple_files",
                "search_files",
                "list_directory",
                "file_read",
            ),
            keep_call_stub=True,
            **kwargs,
        )
        logger.info(
            "Aggressive conversation manager initialized with extended tool drop list"
        )


class MinimalConversationManager(DropAndSlideConversationManager):
    """
    Minimal conversation manager that keeps very little history.

    This variant uses a small window size and drops many tool results.
    Useful for agents with strict token limits or when context isn't critical.

    Example:
        manager = MinimalConversationManager()
        # Uses window_size=10 and drops common tools
    """

    def __init__(self, **kwargs):
        """
        Initialize with minimal settings.

        Args:
            **kwargs: Arguments passed to parent class (can override defaults)
        """
        # Set defaults for minimal context
        kwargs.setdefault("window_size", 10)
        kwargs.setdefault("should_truncate_results", True)

        super().__init__(
            tools_to_drop=("read_multiple_files", "search_files"),
            keep_call_stub=True,
            **kwargs,
        )
        logger.info("Minimal conversation manager initialized with small window")


def _record_summarization_usage(agent: Any, usage: Usage) -> None:
    """Add one summarization call's ``usage`` to ``agent.event_loop_metrics``.

    ``update_usage`` is the method the Strands event loop itself calls after every
    model call, so the summary's tokens land in ``accumulated_usage`` (the total
    every metering reader in this package uses), in the current invocation's
    record, and in the OpenTelemetry token counters, exactly as an ordinary turn's
    would. It attributes to ``agent_invocations[-1]``, which exists whenever the
    agent loop is the caller; a direct ``reduce_context`` outside an invocation has
    no such record, so only the running total is updated then.

    Metering must never break the summary it is measuring, so a failure here is
    logged and swallowed rather than raised into the conversation manager.
    """
    try:
        metrics = agent.event_loop_metrics
        if metrics.agent_invocations:
            metrics.update_usage(usage)
            return
        total = metrics.accumulated_usage
        for key, value in usage.items():
            # Token counts only: Bedrock's usage can also carry ``cacheDetails``,
            # a list, which ``update_usage`` ignores and this must too.
            if isinstance(value, int) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
    except Exception:  # noqa: BLE001 - metering is best effort, the summary is not
        logger.warning(
            "Could not record conversation-summarization token usage", exc_info=True
        )


class _UsageReportingModel:
    """``model``, with the usage of every ``stream`` call handed to ``on_usage``.

    The usage arrives in the stream's ``metadata`` chunk and is read with Strands'
    own ``extract_usage_metrics``, the function the agent loop uses to read it from
    the same chunk, so the units and defaults are the library's. Every other
    attribute is the wrapped model's.
    """

    def __init__(self, model: Any, on_usage: Callable[[Usage], None]) -> None:
        self._model = model
        self._on_usage = on_usage

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async for chunk in self._model.stream(*args, **kwargs):
            if isinstance(chunk, dict) and "metadata" in chunk:
                usage, _metrics = extract_usage_metrics(chunk["metadata"])
                self._on_usage(usage)
            yield chunk


class _SummaryAgentView:
    """``agent`` as the summarizer sees it: the same agent, with a metered model.

    Strands' default summarization path reads only ``agent.model``. A view rather
    than swapping ``agent.model`` in place, so nothing else that holds the agent can
    observe the wrapper, and any other attribute a later Strands release reads is
    still the real agent's.
    """

    def __init__(self, agent: Any, model: Any) -> None:
        self._agent = agent
        self.model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)


class MeteredSummarizingConversationManager(SummarizingConversationManager):
    """``SummarizingConversationManager`` whose summaries are metered.

    When it has no ``summarization_agent``, Strands' manager summarizes the
    conversation by calling the agent's model directly (``model.stream()``) rather
    than through the agent loop, to avoid re-entering the agent's invocation lock.
    That call is billed like any other, but the loop is what records usage, so its
    tokens never reach ``agent.event_loop_metrics`` and every reader of
    ``accumulated_usage`` under-reports the run by the cost of each summary.

    This subclass keeps Strands' summarization exactly as it is — the same prompt,
    the same messages, the same model — and adds the call's usage to the agent's
    metrics as it arrives. The summary is generated by the agent's own model, so
    its usage is the same model's and belongs under the same metering key.

    ``summarization_agent`` is refused, at construction and on any later
    assignment: a dedicated agent records the summary's usage on its own metrics,
    which none of this package's readers look at, so accepting one would bring the
    omission back by another route.
    """

    @property
    def summarization_agent(self) -> None:
        return None

    @summarization_agent.setter
    def summarization_agent(self, value: Any) -> None:
        if value is not None:
            raise ValueError(
                "MeteredSummarizingConversationManager does not take a "
                "summarization_agent: that agent's usage would be recorded on its "
                "own metrics rather than on the agent being summarized."
            )

    def _generate_summary_with_model(
        self, messages: list[Message], agent: Any
    ) -> Message:
        model = _UsageReportingModel(
            agent.model, lambda usage: _record_summarization_usage(agent, usage)
        )
        view: Any = _SummaryAgentView(agent, model)
        return super()._generate_summary_with_model(messages, view)
