# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A retried agent invocation must resume the conversation, not re-send the prompt.

``Agent.invoke_async`` appends its ``input`` to ``agent.messages`` before calling
the model and leaves it there when that call fails, so the retry ladder around
``invoke_agent_with_retry`` used to add one more copy of the whole prompt per
attempt. The prompt ends in a ``cachePoint``, so the third copy put the request at
five ``cache_control`` blocks against Bedrock's limit of four and the section
failed with a non-retryable ``ValidationException``; one copy short of that it
silently re-sent the document text and every page image at full price.

Every test here is offline. The first two drive the REAL strands ``Agent`` and the
REAL ``BedrockModel.format_request``, because the defect lives in the interaction
between the two: strands' append-on-failure behavior, its treatment of a
``list[ContentBlock]`` input as the caller's own list (which is what
``_has_an_unanswered_attempt`` reads), and its rendering of
``cache_prompt``/``cache_tools`` plus our message cachePoint into one request.
Asserting on a hand-written fake would have missed all three — and if a future
strands starts copying the content list, or stops leaving a failed turn behind,
these are the tests that notice.
"""

import asyncio
import time
from typing import Any

import pytest

pytest.importorskip("strands", reason="agentic extras not installed")

from pydantic import BaseModel  # noqa: E402

from idp_common.extraction import agentic_idp  # noqa: E402

# Bedrock's hard cap, quoted by the error this test exists to prevent:
# "A maximum of 4 blocks with cache_control may be provided."
MAX_CACHE_CONTROL_BLOCKS = 4

# What this path legitimately sends: one for cache_prompt (system), one for
# cache_tools (toolConfig), one trailing block on the prompt itself. Pinned as an
# equality, not just against the cap: a strands release that stopped rendering
# either model-level cache point would leave a `<= 4` assertion passing while it
# no longer measured anything.
EXPECTED_CACHE_POINTS = 3

CLAUDE_MODEL = "us.anthropic.claude-sonnet-4-6"


class _Throttled(Exception):
    """A transient error the ladder's vocabulary matches by name and by message."""

    def __init__(self) -> None:
        super().__init__("ThrottlingException: Too many requests")


_Throttled.__name__ = "ThrottlingException"


def _count_cache_points(request: dict[str, Any]) -> int:
    """Total ``cachePoint`` blocks in an assembled Converse request.

    Bedrock counts these additively across the three places they may appear —
    measured against live ``us.anthropic.claude-sonnet-4-6``, one system block
    plus one toolConfig block plus N message blocks reports ``Found N+2``.
    """
    return (
        sum(1 for block in request.get("system", []) if "cachePoint" in block)
        + sum(
            1
            for tool in request.get("toolConfig", {}).get("tools", [])
            if "cachePoint" in tool
        )
        + sum(
            1
            for message in request["messages"]
            for block in message["content"]
            if "cachePoint" in block
        )
    )


def _a_claude_agent():
    """A real strands Agent on the model config this path builds for Claude.

    The two cache flags come from ``_build_model_config`` rather than being
    hardcoded here, so a change to which models get prompt or tool caching moves
    this test with it instead of leaving it asserting about a config the product
    no longer produces.
    """
    from strands import Agent, tool
    from strands.models.bedrock import BedrockModel

    model_config = agentic_idp._build_model_config(
        model_id=CLAUDE_MODEL,
        max_tokens=256,
        max_retries=1,
        connect_timeout=10,
        read_timeout=60,
    )
    assert model_config.get("cache_prompt") == "default", (
        "this test is about the cache points _build_model_config sets for a Claude "
        "model, and it no longer sets the system one"
    )
    assert model_config.get("cache_tools") == "default", (
        "this test is about the cache points _build_model_config sets for a Claude "
        "model, and it no longer sets the toolConfig one"
    )
    model_config.pop("boto_session", None)

    @tool
    def record_row(value: str) -> str:
        """Record one row, so the request carries a toolConfig and its cachePoint."""
        return f"recorded {value}"

    model = BedrockModel(region_name="us-west-2", **model_config)
    return Agent(model=model, tools=[record_row], system_prompt="system"), model


def _a_prompt() -> list[dict[str, Any]]:
    """The shape _prepare_prompt_content produces: content, then a cachePoint."""
    return [
        {"text": "Extract every row of this table."},
        {"cachePoint": {"type": "default"}},
    ]


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.agentic
def test_the_assembled_request_stays_within_bedrocks_cache_block_limit():
    """Every attempt assembles the same request, with the same cache-point count.

    The model is failed on every attempt, so what is asserted is the count the real
    ``format_request`` produces on each of them.
    """
    from strands.models.bedrock import BedrockModel

    agent, _model = _a_claude_agent()
    prompt = _a_prompt()
    counts: list[int] = []
    tool_specs = [
        {
            "name": "record_row",
            "description": "record one row",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    ]

    def _stream(self, *_args, **_kwargs):
        # Assemble the request the way the model would, then fail transiently. A
        # plain function, not an async generator: strands calls ``stream(...)``
        # and only then iterates it, so raising here raises at the call — which
        # keeps the failure in the right place without an unreachable ``yield``.
        counts.append(
            _count_cache_points(
                self.format_request(agent.messages, tool_specs, "system")
            )
        )
        raise _Throttled()

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(BedrockModel, "stream", _stream)
        patched.setattr(asyncio, "sleep", _no_sleep)
        patched.setattr(time, "time", lambda: 1_000_000.0)

        with pytest.raises(Exception, match="ThrottlingException"):
            asyncio.run(agentic_idp.invoke_agent_with_retry(input=prompt, agent=agent))

    assert len(counts) >= 3, (
        f"the ladder made {len(counts)} attempt(s); at least three are needed for "
        "this assertion to be about accumulation rather than a single request"
    )
    assert set(counts) == {EXPECTED_CACHE_POINTS}, (
        f"cachePoint counts across attempts were {counts}, not a constant "
        f"{EXPECTED_CACHE_POINTS} (system + toolConfig + one on the prompt). A "
        "climbing count means a retry appended another copy of the prompt, and "
        f"Bedrock rejects more than {MAX_CACHE_CONTROL_BLOCKS} cache_control "
        "blocks outright"
    )
    copies = [m for m in agent.messages if m.get("content") is prompt]
    assert len(copies) == 1, (
        f"the conversation holds {len(copies)} copies of the prompt after "
        f"{len(counts)} attempts; a retry must resume, not re-send"
    )


@pytest.mark.agentic
def test_a_retry_keeps_the_turns_the_failed_attempt_completed():
    """The failed attempt's tool round survives, because its state does too.

    This is the property that keeps the fix from trading a loud failure for a
    silent wrong answer. ``map_table_to_schema`` *accumulates* into
    ``agent.state["mapped_table_rows"]`` across calls, by design, for chunked
    tables, and nothing clears it. Discarding the conversation while keeping that
    state would leave the agent with no record of the chunks it had already mapped
    and a prompt that does not mention them, so it would map them again and
    ``finalize_table_extraction`` would emit every row twice.
    """
    from strands.models.bedrock import BedrockModel

    agent, _model = _a_claude_agent()
    prompt = _a_prompt()

    # A completed tool round, exactly as strands leaves it, then a failure.
    agent.messages.append({"role": "user", "content": prompt})
    agent.messages.append(
        {
            "role": "assistant",
            "content": [
                {"toolUse": {"toolUseId": "t1", "name": "record_row", "input": {}}}
            ],
        }
    )
    agent.messages.append(
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "t1",
                        "content": [{"text": "recorded A"}],
                        "status": "success",
                    }
                }
            ],
        }
    )
    agent.state.set("mapped_table_rows", {"mapped_rows": [{"a": 1}], "row_count": 1})
    before = list(agent.messages)

    def _stream(self, *_args, **_kwargs):
        raise _Throttled()

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(BedrockModel, "stream", _stream)
        patched.setattr(asyncio, "sleep", _no_sleep)
        patched.setattr(time, "time", lambda: 1_000_000.0)

        with pytest.raises(Exception, match="ThrottlingException"):
            asyncio.run(agentic_idp.invoke_agent_with_retry(input=prompt, agent=agent))

    assert agent.messages[: len(before)] == before, (
        "a retry discarded turns the failed attempt had completed; the tool "
        "results and agent.state must keep describing the same work, or the "
        "accumulating table state is re-added on top of itself"
    )
    assert agent.state.get("mapped_table_rows") == {
        "mapped_rows": [{"a": 1}],
        "row_count": 1,
    }


class _RecordingState:
    def __init__(self, value: dict[str, Any]):
        self._value = value

    def get(self, key: str):
        return self._value if key == "current_extraction" else None


class _StrandsLikeAgent:
    """Mimics the one strands behavior this fix turns on: append, then fail.

    ``invoke_async(None)`` adds no message, which is how a resumed attempt is told
    apart from a re-sent one.
    """

    def __init__(self, value: dict[str, Any], failures: int):
        self.state = _RecordingState(value)
        self.messages: list[dict[str, Any]] = []
        self._failures = failures
        self.calls = 0
        self.resumed = 0

    async def invoke_async(self, input):
        self.calls += 1
        if input is None:
            self.resumed += 1
        else:
            self.messages.append({"role": "user", "content": input})
        if self._failures > 0:
            self._failures -= 1
            raise _Throttled()
        self.messages.append({"role": "assistant", "content": [{"text": "done"}]})
        return "response"


class _Doc(BaseModel):
    status: str


@pytest.mark.agentic
def test_the_extraction_loop_reaches_the_guard_so_two_throttles_leave_one_copy():
    """The production call site is covered, not merely the helper.

    A guard the one production caller never reached would leave the defect in
    place with the tests above still green, so this drives
    ``_invoke_agent_for_extraction`` rather than the ladder directly.
    """
    agent = _StrandsLikeAgent({"status": "paid"}, failures=2)
    prompt_content = _a_prompt()

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(asyncio, "sleep", _no_sleep)
        response, result = asyncio.run(
            agentic_idp._invoke_agent_for_extraction(
                agent=agent,
                prompt_content=prompt_content,
                data_format=_Doc,
                max_extraction_retries=1,
            )
        )

    assert agent.calls == 3, (
        f"the agent was invoked {agent.calls} time(s); two transient failures "
        "must be retried, not raised"
    )
    assert agent.resumed == 2, (
        f"{agent.resumed} of the retries resumed the conversation; both must, or "
        "the prompt is being sent again"
    )
    assert result is not None and result.status == "paid"
    assert response == "response"

    copies = [m for m in agent.messages if m.get("content") is prompt_content]
    assert len(copies) == 1, (
        f"the conversation holds {len(copies)} copies of the prompt after two "
        "retries; each retry must resume the conversation instead"
    )


@pytest.mark.agentic
def test_a_feedback_round_is_sent_rather_than_resumed():
    """A new round is not a retry, and is told apart by building a new list.

    ``_invoke_agent_for_extraction`` deliberately adds a feedback turn between
    extraction attempts. Treating that as a resume would drop the correction and
    the agent would never see it.
    """
    agent = _StrandsLikeAgent({"status": "paid"}, failures=0)
    first_prompt = _a_prompt()
    verdicts = iter([(False, "status must be lowercase"), (True, "ok")])

    response, result = asyncio.run(
        agentic_idp._invoke_agent_for_extraction(
            agent=agent,
            prompt_content=first_prompt,
            data_format=_Doc,
            max_extraction_retries=2,
            schema_validator=lambda _data: next(verdicts),
        )
    )

    assert result is not None and response == "response"
    assert agent.calls == 2, "the failed validation must trigger one more round"
    assert agent.resumed == 0, (
        "a feedback round was resumed instead of sent, so the agent never saw the "
        "correction"
    )
    assert any(m.get("content") is first_prompt for m in agent.messages), (
        "the original prompt turn is missing from the conversation"
    )
    feedback = [
        m
        for m in agent.messages
        if m["role"] == "user"
        and m.get("content") is not first_prompt
        and any("status must be lowercase" in b.get("text", "") for b in m["content"])
    ]
    assert len(feedback) == 1, (
        "the validator's feedback did not reach the conversation as its own turn"
    )


@pytest.mark.agentic
def test_a_compacted_conversation_is_sent_again_rather_than_resumed():
    """A prompt the summarizing manager removed has to be stated again.

    ``SummarizingConversationManager.reduce_context`` replaces ``agent.messages``
    with a summary plus the recent tail, so the prompt can be gone by the time a
    retry runs. Resuming then would ask the model to continue a task nobody stated,
    and re-sending it costs one message cache point — the count the limit allows.
    """
    agent = _StrandsLikeAgent({"status": "paid"}, failures=0)
    prompt = _a_prompt()
    assert not agentic_idp._has_an_unanswered_attempt(prompt, agent)

    agent.messages.append({"role": "user", "content": prompt})
    assert agentic_idp._has_an_unanswered_attempt(prompt, agent)

    # What reduce_context does: the prompt turn is gone, a summary stands in.
    agent.messages[:] = [{"role": "user", "content": [{"text": "## Summary ..."}]}]
    assert not agentic_idp._has_an_unanswered_attempt(prompt, agent)


@pytest.mark.agentic
def test_reusing_one_content_list_for_two_turns_is_not_mistaken_for_a_retry():
    """Identity alone would make behaviour depend on an invisible property.

    ``_invoke_agent_for_extraction`` builds a fresh list per round, so the
    production path never does this — but a future caller that passes one list
    twice must get two turns, not one turn and one resume, and nothing at a call
    site would show which it got. The second condition is what tells them apart:
    a completed turn ends in an assistant message, an outstanding attempt does not.
    """
    agent = _StrandsLikeAgent({"status": "paid"}, failures=0)
    prompt = _a_prompt()

    # A failed attempt: the prompt is on the conversation, unanswered.
    agent.messages.append({"role": "user", "content": prompt})
    assert agentic_idp._has_an_unanswered_attempt(prompt, agent), (
        "an attempt the model never answered must be resumed"
    )

    # A mid-loop failure: a tool round completed, then the model call failed. The
    # last turn is the user's toolResult, so this is still one outstanding attempt.
    agent.messages.append(
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1"}}]}
    )
    agent.messages.append(
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1"}}]}
    )
    assert agentic_idp._has_an_unanswered_attempt(prompt, agent), (
        "a mid-loop failure must still resume, or the completed tool round is lost"
    )

    # The turn completes. Submitting the SAME list again is a new logical turn.
    agent.messages.append({"role": "assistant", "content": [{"text": "done"}]})
    assert not agentic_idp._has_an_unanswered_attempt(prompt, agent), (
        "a list reused after a completed turn was read as a retry; the caller "
        "asked for a second turn and would silently have got a resume"
    )


@pytest.mark.agentic
def test_an_unreadable_message_shape_falls_back_to_sending():
    """The guard chooses the always-valid request when it cannot read the history.

    Sending is a valid Converse request from any state; resuming is not. So an
    ``agent.messages`` this cannot inspect must not raise out of the retried body
    and fail the section.
    """
    agent = _StrandsLikeAgent({"status": "paid"}, failures=0)
    prompt = _a_prompt()
    agent.messages.append({"role": "user", "content": prompt})
    agent.messages.append("not a message")  # type: ignore[arg-type]
    assert not agentic_idp._has_an_unanswered_attempt(prompt, agent)
