# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for IDPAgent's context-manager boundary.

The subject here is narrow and is the reason this file exists: `__exit__` is where a
queued agent-transcript write is waited for. The writes go to a thread pool so a
DynamoDB round trip stays off the agent's critical path, and a Lambda invocation ends
with the execution environment being **frozen** rather than shut down -- the process is
not signalled and does not exit, so no interpreter shutdown hook runs and an unfinished
write is simply suspended.

Two things about the boundary, since both are easy to get wrong from the outside. The
registry *does* keep the tracker alive -- it stores the bound method
`on_message_added`, whose `__self__` is the tracker -- so the defect was not a dead
object but the absence of any named route to it, which is what
`test_the_kept_tracker_is_the_one_that_was_registered_as_a_hook` pins by finding it the
hard way. And the registry *does* offer a teardown event, `AfterInvocationEvent`;
draining there would be wrong rather than unavailable, because `shutdown` closes the
pool and a twice-invoked agent would stop transcribing after its first call.

The assertions therefore go all the way to the logger rather than stopping at
`tracker.shutdown`. A mutation anywhere along `__exit__` -> tracker -> logger has to
fail one of these.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from strands import Agent

from idp_common.agents.common.idp_agent import IDPAgent

LOGGER_MODULE = "idp_common.agents.common.dynamodb_logger"


def _agent(*, monitored: bool = True, mcp_client=None) -> IDPAgent:
    """An IDPAgent whose tracker holds a stand-in logger.

    `DynamoDBMessageLogger` is the only part stubbed: the tracker itself is real, so
    the chain from `__exit__` through to the drain is the production one.
    """
    inner = Agent(tools=[], system_prompt="x", model=None)
    extra = {"job_id": "job-1", "user_id": "user-1"} if monitored else {}
    with (
        patch.dict(os.environ, {"AGENT_TABLE": "agent-table"}),
        patch(f"{LOGGER_MODULE}.DynamoDBMessageLogger"),
    ):
        return IDPAgent(
            agent_name="n",
            agent_description="d",
            agent_id="i",
            agent=inner,
            mcp_client=mcp_client,
            **extra,
        )


@pytest.mark.unit
class TestMonitoringIsReachable:
    """The reference without which nothing can drain the pool."""

    def test_the_registered_tracker_is_kept_on_the_instance(self):
        agent = _agent()
        assert agent.message_tracker is not None

    def test_the_kept_tracker_is_the_one_that_was_registered_as_a_hook(self):
        # Two trackers -- one registered, one referenced -- would drain a pool that
        # no message was ever written to, and would satisfy a test that only checked
        # a reference exists. Read off the registry's own table because Strands'
        # HookRegistry exposes callbacks and no list of the providers that added them,
        # which is the same absence that left the tracker unreachable in the first
        # place.
        agent = _agent()
        registered = [
            callback
            for callbacks in agent.hooks._registered_callbacks.values()
            for callback in callbacks
        ]
        assert any(
            getattr(callback, "__self__", None) is agent.message_tracker
            for callback in registered
        )

    def test_an_unmonitored_agent_has_no_tracker(self):
        assert _agent(monitored=False).message_tracker is None


@pytest.mark.unit
class TestExitDrainsTranscriptWrites:
    """__exit__: the invocation/sub-agent boundary."""

    def test_exit_drains_the_write_pool(self):
        agent = _agent()
        agent.__exit__(None, None, None)
        agent.message_tracker.db_logger.shutdown.assert_called_once()

    def test_the_drain_is_bounded(self):
        # An unbounded wait here holds a sub-agent's exit open behind a stuck write
        # while the user is waiting on the orchestrator's answer.
        agent = _agent()
        agent.__exit__(None, None, None)
        kwargs = agent.message_tracker.db_logger.shutdown.call_args.kwargs
        assert kwargs["timeout"] is not None

    def test_the_pool_is_drained_even_when_the_body_raised(self):
        # The failure case is the one that matters most: agent_processor retries the
        # whole workflow, and an attempt that leaves its pool undrained contends with
        # the next attempt's loggers on the same record.
        agent = _agent()
        agent.__exit__(RuntimeError, RuntimeError("boom"), None)
        agent.message_tracker.db_logger.shutdown.assert_called_once()

    def test_an_unmonitored_agent_exits_without_error(self):
        assert _agent(monitored=False).__exit__(None, None, None) is None

    def test_a_drain_failure_does_not_propagate(self):
        # Monitoring is best-effort; a telemetry failure must not turn a completed
        # agent run into a failed one.
        agent = _agent()
        agent.message_tracker.db_logger.shutdown.side_effect = RuntimeError("boom")
        assert agent.__exit__(None, None, None) is None


@pytest.mark.unit
class TestExitOrderingWithTheMcpClient:
    """The two halves of teardown are independent and must not block each other."""

    def test_the_drain_happens_before_the_mcp_client_is_closed(self):
        order: list[str] = []
        mcp_client = MagicMock()
        mcp_client.__exit__.side_effect = lambda *_: order.append("mcp")
        agent = _agent(mcp_client=mcp_client)
        agent.message_tracker.db_logger.shutdown.side_effect = lambda **_: order.append(
            "drain"
        )
        agent.__exit__(None, None, None)
        assert order == ["drain", "mcp"]

    def test_a_failing_mcp_close_does_not_skip_the_drain(self):
        mcp_client = MagicMock()
        mcp_client.__exit__.side_effect = RuntimeError("mcp down")
        agent = _agent(mcp_client=mcp_client)
        assert agent.__exit__(None, None, None) is None
        agent.message_tracker.db_logger.shutdown.assert_called_once()

    def test_a_failing_drain_does_not_skip_the_mcp_close(self):
        # Leaking an MCP client because monitoring failed would trade a best-effort
        # subsystem's failure for a real resource leak.
        mcp_client = MagicMock()
        agent = _agent(mcp_client=mcp_client)
        agent.message_tracker.db_logger.shutdown.side_effect = RuntimeError("boom")
        agent.__exit__(None, None, None)
        mcp_client.__exit__.assert_called_once()
