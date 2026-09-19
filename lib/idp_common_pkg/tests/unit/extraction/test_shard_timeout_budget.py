# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The shard invocation's time budget has to add up, and nothing checked that.

Three numbers draw on the same 900 seconds: how long ONE Bedrock request may
stall (``AGENT_READ_TIMEOUT_SECONDS``), how much total backoff the retry ladder
may spend (``AGENT_MAX_TOTAL_BACKOFF_SECONDS``), and the shard function's Lambda
``Timeout``. At a read timeout of 600 the first two summed to exactly 900 and
left nothing for the work itself, so a single transient ``Read timed out`` ran the
invocation into the wall clock. Step Functions then read the resulting
``Sandbox.Timedout`` as deterministic — one attempt, by design (#917) — so the one
failure a retry would have cleared was the one not retried, and
``ExtractionShardMap``, which tolerates no shard failures, discarded the sibling
shards that had already succeeded (#1014).

Each number was individually defensible and the relationship between them was
stated only in a comment. These tests assert the relationship, and that the
constants are actually the ones the extraction code and the deployed function
use — a correct constant nobody reads is not a fix.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import botocore.exceptions
import pytest
from pydantic import BaseModel

from idp_common.config.models import IDPConfig
from idp_common.extraction.runtime import extract_one_shard
from idp_common.utils.bedrock_utils import (
    AGENT_MAX_BACKOFF_SECONDS,
    AGENT_MAX_TOTAL_BACKOFF_SECONDS,
    AGENT_READ_TIMEOUT_SECONDS,
    LAMBDA_MAX_TIMEOUT_SECONDS,
    set_lambda_deadline_epoch,
)
from idp_common.utils.transient_errors import (
    TransientError,
    is_transient_error,
    raise_if_transient,
)

REPO = Path(__file__).resolve().parents[5]
ASL = REPO / "patterns" / "unified" / "statemachine" / "workflow.asl.json"
TEMPLATE = REPO / "patterns" / "unified" / "template.yaml"

# ``workflow.asl.json`` carries one UNQUOTED CloudFormation placeholder
# (``"TimeoutSeconds": ${...}``), so the raw file is not valid JSON and a
# substitution is unavoidable before it can be parsed. Anchored on the key's closing
# QUOTE, never on a bare colon: the Lambda task resources are
# ``"arn:${Partition}:states:::lambda:invoke"``, where a colon sits immediately
# before ``${``, and a bare ``:\s*\$\{…\}`` rewrites all of them to
# ``"arn: 1:states:::lambda:invoke"`` — every assertion below would then be made
# against a document that is not what deploys.
# ``scripts/tests/test_asl_placeholder_substitution.py`` enumerates every
# substitution site in the tree by content and proves behaviourally that the task
# ARNs survive it, so this spelling is gated rather than merely asserted here.
_UNQUOTED_PLACEHOLDER_RE = re.compile(r'"\s*:\s*\$\{[^}]+\}')


def _asl() -> dict[str, Any]:
    raw = ASL.read_text(encoding="utf-8")
    return json.loads(_UNQUOTED_PLACEHOLDER_RE.sub('": 1', raw))


def _section_states() -> dict[str, Any]:
    """The states inside the per-section Map, where the shard Map lives."""
    process_sections = _asl()["States"]["ProcessSections"]
    scope = process_sections.get("ItemProcessor") or process_sections["Iterator"]
    return scope["States"]


# Room that must remain for the actual extraction work after one stalled request
# and the whole backoff allowance. Two further attempts at the read timeout is the
# floor worth defending: one to replace the stalled call, one for a second blip.
_MIN_WORKING_MARGIN_SECONDS = 2 * AGENT_READ_TIMEOUT_SECONDS


@pytest.mark.unit
def test_one_stalled_request_plus_all_backoff_leaves_room_to_work():
    """The budget inequality itself. This is the assertion that was missing."""
    spent_worst_case = AGENT_READ_TIMEOUT_SECONDS + AGENT_MAX_TOTAL_BACKOFF_SECONDS
    remaining = LAMBDA_MAX_TIMEOUT_SECONDS - spent_worst_case
    assert remaining >= _MIN_WORKING_MARGIN_SECONDS, (
        f"a stalled Bedrock request ({AGENT_READ_TIMEOUT_SECONDS}s) plus the full "
        f"backoff allowance ({AGENT_MAX_TOTAL_BACKOFF_SECONDS}s) leaves only "
        f"{remaining}s of the {LAMBDA_MAX_TIMEOUT_SECONDS}s invocation for the work "
        f"itself, which is under the {_MIN_WORKING_MARGIN_SECONDS}s floor. The shard "
        "will die on the wall clock instead of retrying, and Step Functions treats a "
        "timeout as deterministic. See #1014."
    )


@pytest.mark.unit
def test_a_single_sleep_cannot_outlast_the_invocation():
    """``max_delay`` was once 1800 inside a 900s function. Keep that shut."""
    assert AGENT_MAX_BACKOFF_SECONDS < LAMBDA_MAX_TIMEOUT_SECONDS
    assert AGENT_MAX_BACKOFF_SECONDS <= AGENT_MAX_TOTAL_BACKOFF_SECONDS


@pytest.mark.unit
def test_the_extraction_code_actually_defaults_to_this_read_timeout():
    """A budget constant that the callers do not use would pass the maths and
    change nothing. Both extraction modules must take their default from it, and
    no literal 600 may survive as a read timeout."""
    from idp_common.extraction import agentic_idp, runtime

    for mod in (agentic_idp, runtime):
        src = Path(mod.__file__).read_text()
        assert "read_timeout: float = 600.0" not in src, (
            f"{mod.__name__} still hardcodes a 600s read timeout; the deployed "
            "function's Lambda timeout is 900s, so one stalled request takes the "
            "invocation. See #1014."
        )
        assert "read_timeout: float = AGENT_READ_TIMEOUT_SECONDS" in src, (
            f"{mod.__name__} does not take its read timeout from the shared budget "
            "constant, so the budget assertions above do not constrain it."
        )


@pytest.mark.unit
def test_the_deployed_shard_function_timeout_matches_the_assumed_ceiling():
    """The maths above assumes the function really is capped at 900s. If someone
    lowers the function's Timeout, the budget silently stops adding up."""
    text = TEMPLATE.read_text()
    idx = text.index("ShardRuntimeFunction:")
    block = text[idx : idx + 4000]
    m = re.search(r"^\s+Timeout:\s*(\d+)\s*$", block, re.MULTILINE)
    assert m, "could not read ShardRuntimeFunction's Timeout from the template"
    assert float(m.group(1)) <= LAMBDA_MAX_TIMEOUT_SECONDS, (
        "ShardRuntimeFunction's Timeout exceeds the ceiling the budget assumes"
    )
    assert float(m.group(1)) == LAMBDA_MAX_TIMEOUT_SECONDS, (
        f"ShardRuntimeFunction's Timeout is {m.group(1)}s but the budget constants "
        f"are sized for {LAMBDA_MAX_TIMEOUT_SECONDS}s. Re-derive the constants in "
        "utils/bedrock_utils.py against the real timeout."
    )


@pytest.mark.unit
def test_the_shard_map_can_retry_so_persisted_shards_are_not_thrown_away():
    """``ExtractionShardMap`` declares no ToleratedFailurePercentage, so Step
    Functions' default of 0 applies and one failed shard fails the Map. That is
    only survivable because the Map is retried and the shards that already
    succeeded reload from S3 rather than re-inferring. Without a Map-level
    retrier, that persistence can never be used."""
    shard_map = _section_states()["ExtractionShardMap"]
    retried = {
        error
        for retrier in shard_map.get("Retry", [])
        for error in retrier["ErrorEquals"]
    }
    assert "States.ExceedToleratedFailureThreshold" in retried, (
        "ExtractionShardMap has no retrier for the error Step Functions raises when "
        "a shard fails under a zero failure tolerance, so a single transient shard "
        "failure discards the output of every shard that succeeded. See #1014."
    )
    assert "ToleratedFailurePercentage" not in shard_map, (
        "A non-zero failure tolerance would let the document complete with shards "
        "missing, which is silent data loss - worse than the failure it replaces. "
        "Recover by retrying the Map, not by accepting a partial result."
    )
    assert "ToleratedFailureCount" not in shard_map, (
        "Same reasoning as ToleratedFailurePercentage: an absolute count above zero "
        "also ships a document with shards missing."
    )


@pytest.mark.unit
def test_the_shard_map_names_the_section_and_the_cause_when_it_fails():
    """A failed shard Map must not surface as a bare error code.

    With no ``Catch``, the only thing an operator sees on the ExecutionFailed event
    is ``States.ExceedToleratedFailureThreshold``: not which section, not which
    shard, and nothing about why. The Map Run holds the per-shard failure, but it
    has to be found by hand first. The ``Catch`` routes to a ``Fail`` state whose
    ``CausePath`` names the section and carries the Map's error output, so the
    execution's own failure event is self-describing (#1014).

    ``patterns/unified/tests/test_workflow_hook_fatal_catch.py`` checks the other
    half — that what the ``CausePath`` dereferences is what its catcher actually
    provides, since an unresolvable path there reports ``States.Runtime`` and masks
    the failure it was meant to describe.
    """
    states = _section_states()
    catchers = states["ExtractionShardMap"].get("Catch") or []
    assert catchers, (
        "ExtractionShardMap has no Catch, so a shard failure reports only "
        "States.ExceedToleratedFailureThreshold - no section, no shard, no cause. "
        "See #1014."
    )
    targets = set()
    for catcher in catchers:
        assert "States.ExceedToleratedFailureThreshold" in catcher["ErrorEquals"], (
            "the Catch does not cover the error a shard failure actually raises "
            f"under a zero failure tolerance: {catcher['ErrorEquals']}"
        )
        targets.add(catcher["Next"])
    for target in targets:
        failure_state = states[target]
        assert failure_state["Type"] == "Fail", (
            f"ExtractionShardMap catches to {target}, which is a "
            f"{failure_state['Type']} state. Catching a shard failure must not let "
            "the document continue as though extraction had completed - that is the "
            "silent partial result a failure tolerance above zero would produce."
        )
        cause = failure_state.get("CausePath", "")
        assert "$.section_id" in cause, (
            f"{target} does not name the section in its Cause, which is the one "
            "identifier an operator needs to find the failing Map Run"
        )
        assert "ShardMapError" in cause, (
            f"{target} does not carry the Map's error output in its Cause, so the "
            "failure is still reported without a reason"
        )


# ---------------------------------------------------------------------------
# The behavioural half: an injected read timeout, on a simulated clock.
# ---------------------------------------------------------------------------


class _ShardModel(BaseModel):
    transactions: list | None = None


def _payload(page_start: int = 0, page_end: int = 3) -> dict[str, Any]:
    return {
        "content": [{"text": "page text"}],
        "page_start": page_start,
        "page_end": page_end,
        "total_pages": 4,
    }


@pytest.fixture(autouse=True)
def _clear_deadline():
    """The deadline is a ContextVar; leaking it would couple these tests."""
    set_lambda_deadline_epoch(None)
    yield
    set_lambda_deadline_epoch(None)


def _read_timeout() -> botocore.exceptions.ReadTimeoutError:
    """The exact exception botocore raises when a Bedrock response never arrives."""
    return botocore.exceptions.ReadTimeoutError(
        endpoint_url="https://bedrock-runtime.example.invalid/"
    )


@pytest.mark.unit
def test_one_stalled_request_leaves_the_ladder_a_full_window_to_retry_in():
    """The failure from #1014, replayed on a simulated clock.

    A stalled Bedrock request occupies the whole read timeout and then surfaces as
    ``ReadTimeoutError``. The retry ladder around the agent call is bounded by the
    Lambda deadline, so what decides whether the shard recovers is arithmetic: after
    the stall and the backoff that follows it, is there still a full read-timeout
    window left inside the invocation? At 600 s there was not — the retry began with
    roughly 295 s of a 900 s invocation left, less than half of what one more
    attempt is allowed to take, so the shard ran into the wall clock instead. Step
    Functions then read the resulting ``Sandbox.Timedout`` as deterministic and did
    not retry the one failure a retry would have cleared.

    The clock is simulated rather than real: the point is the budget, and a test
    that actually waited out a read timeout could not run in CI. What the simulation
    models is the one thing that matters — that botocore spends the entire read
    timeout before giving up.

    This asserts the retry gets a full window, not that the retry succeeds. How long
    a successful agent call takes is a property of the document and the model, and
    no static test can know it.
    """
    pytest.importorskip("strands", reason="agentic extras not installed")
    from idp_common.extraction.agentic_idp import invoke_agent_with_retry

    clock = [1_000_000.0]
    attempt_starts: list[float] = []

    class _StallsOnceAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke_async(self, _input):
            self.calls += 1
            attempt_starts.append(clock[0])
            if self.calls == 1:
                # botocore waits out the FULL read timeout before it gives up.
                clock[0] += AGENT_READ_TIMEOUT_SECONDS
                raise _read_timeout()
            return "extracted"

    async def _fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    agent = _StallsOnceAgent()
    deadline = clock[0] + LAMBDA_MAX_TIMEOUT_SECONDS

    # ``bedrock_utils`` reads the clock as ``time.time()`` and sleeps as
    # ``asyncio.sleep()``, so patching the two stdlib modules is what makes the
    # ladder's own deadline arithmetic run against the simulated clock. Neither
    # ``asyncio.run`` nor the coroutine below awaits ``asyncio.sleep`` itself.
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(time, "time", lambda: clock[0])
        patched.setattr(asyncio, "sleep", _fake_sleep)

        async def _run():
            set_lambda_deadline_epoch(deadline)
            return await invoke_agent_with_retry("prompt", agent)  # type: ignore[arg-type]

        result = asyncio.run(_run())

    assert result == "extracted"
    assert agent.calls == 2, (
        f"the ladder made {agent.calls} attempt(s); a ReadTimeoutError is in the "
        "retryable vocabulary and must be retried, not raised"
    )

    remaining_at_retry = deadline - attempt_starts[1]
    assert remaining_at_retry >= AGENT_READ_TIMEOUT_SECONDS, (
        f"after one stalled request and its backoff, the retry began with only "
        f"{remaining_at_retry:.0f}s of the {LAMBDA_MAX_TIMEOUT_SECONDS:.0f}s "
        f"invocation left — less than the {AGENT_READ_TIMEOUT_SECONDS:.0f}s a "
        "single request is allowed to take, so the retry cannot even fail cleanly "
        "before the Lambda is killed. The shard dies on the wall clock, Step "
        "Functions calls that deterministic and does not retry it. See #1014."
    )


@pytest.mark.unit
def test_a_read_timeout_leaves_the_shard_as_a_transient_error_not_a_timeout():
    """A stalled request must reach Step Functions under a name it retries.

    Two failure reports are possible for the same underlying stall, and only one of
    them is recoverable. If the shard returns, the shard handler classifies the
    ``ReadTimeoutError`` and re-raises it as ``TransientError``, which
    ``ShardExtractionStep`` lists with a real ladder. If the shard instead runs out
    of time, Step Functions reports ``Sandbox.Timedout``, which that state retries
    once and no more — deliberately, since a genuine timeout is deterministic
    (#917). So the transient-error vocabulary only helps a shard that still has time
    to return, which is what the budget above buys.

    The assertions are the whole chain: that the runner's error propagates promptly
    rather than being swallowed, that it is classified transient, and that the name
    it is re-raised under is one the shard state actually retries. A break anywhere
    along that chain turns a recoverable blip back into a lost document.
    """
    started = time.monotonic()
    calls: list[int] = []

    async def _stalling_runner(**kwargs: Any):
        calls.append(kwargs["shard_index"])
        raise _read_timeout()

    async def _run():
        set_lambda_deadline_epoch(time.time() + LAMBDA_MAX_TIMEOUT_SECONDS)
        return await extract_one_shard(
            shard_index=0,
            total_shards=2,
            payload=_payload(),
            model_id="model",
            data_format=_ShardModel,
            config=IDPConfig(),
            section_id="1",
            shard_runner=_stalling_runner,
        )

    with pytest.raises(botocore.exceptions.ReadTimeoutError) as raised:
        asyncio.run(_run())

    elapsed = time.monotonic() - started
    assert elapsed < 30, (
        f"the shard took {elapsed:.1f}s to report an injected read timeout; it has "
        "to RETURN the failure, because being killed by the Lambda timeout reports "
        "Sandbox.Timedout, which is retried once and no more"
    )
    assert calls == [0]

    assert is_transient_error(raised.value), (
        "a ReadTimeoutError from Bedrock is a transport blip and must be classified "
        "transient; otherwise the shard state has no reason to retry it"
    )
    with pytest.raises(TransientError) as surfaced:
        raise_if_transient(raised.value, where="shard runtime")

    # The name the handler raises has to be the name the state retries.
    shard_map = _section_states()["ExtractionShardMap"]
    scope = shard_map.get("ItemProcessor") or shard_map["Iterator"]
    shard_step = scope["States"]["ShardExtractionStep"]
    retried = {
        error
        for retrier in shard_step.get("Retry", [])
        for error in retrier["ErrorEquals"]
    }
    assert type(surfaced.value).__name__ in retried, (
        f"the shard handler reports a transient failure as "
        f"{type(surfaced.value).__name__}, which ShardExtractionStep does not "
        f"retry (it lists {sorted(retried)}). The classification would be correct "
        "and have no effect."
    )


@pytest.mark.unit
def test_the_retry_ladder_never_sleeps_past_the_invocation():
    """A stall that keeps recurring must give up inside the invocation.

    The ladder allows 50 attempts, so its real bound is time: the cumulative backoff
    allowance and the Lambda deadline. Neither may be exceeded, and it must not
    raise early either — ``clamp_sleep_to_budgets`` shortens a sleep rather than
    converting it into a failure, because the remaining time is better spent on
    another attempt than asleep.
    """
    pytest.importorskip("strands", reason="agentic extras not installed")
    from idp_common.extraction.agentic_idp import invoke_agent_with_retry

    clock = [2_000_000.0]
    slept: list[float] = []

    class _AlwaysStallsAgent:
        async def invoke_async(self, _input):
            clock[0] += AGENT_READ_TIMEOUT_SECONDS
            raise _read_timeout()

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    deadline = clock[0] + LAMBDA_MAX_TIMEOUT_SECONDS
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(time, "time", lambda: clock[0])
        patched.setattr(asyncio, "sleep", _fake_sleep)

        async def _run():
            set_lambda_deadline_epoch(deadline)
            return await invoke_agent_with_retry("prompt", _AlwaysStallsAgent())  # type: ignore[arg-type]

        with pytest.raises(botocore.exceptions.ReadTimeoutError):
            asyncio.run(_run())

    assert sum(slept) <= AGENT_MAX_TOTAL_BACKOFF_SECONDS, (
        f"slept {sum(slept):.0f}s against a "
        f"{AGENT_MAX_TOTAL_BACKOFF_SECONDS:.0f}s cumulative allowance"
    )
    assert max(slept) <= AGENT_MAX_BACKOFF_SECONDS, (
        f"one sleep of {max(slept):.0f}s exceeds the "
        f"{AGENT_MAX_BACKOFF_SECONDS:.0f}s per-attempt cap"
    )
    assert all(s >= 0 for s in slept), "a negative sleep would crash asyncio.sleep"
    assert 0.0 in slept, (
        "no sleep was clamped to zero even though the simulated stalls ran past the "
        "deadline. The ladder is therefore still sleeping its nominal backoff with "
        "no time left to sleep it in, which is the behaviour clamp_sleep_to_budgets "
        "exists to prevent. Note it CLAMPS rather than raising: the remaining time "
        "goes to another attempt, and if it runs out the invocation ends on the "
        "underlying error, which is the retryable failure mode."
    )
