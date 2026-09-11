# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Retry backoff must fit inside its own Lambda invocation.

`invoke_agent_with_retry` was decorated with ``max_delay=1800`` — thirty minutes of
backoff — inside a function Lambda kills at 900 seconds, so one transient
``Read timed out`` could spend the whole invocation asleep and achieve nothing.

The fix CLAMPS a sleep to the time available; it never turns one into a failure.
That distinction is the important one and is tested here. Raising early would
surface the underlying error name (``ReadTimeoutError``, ``EventLoopException``,
``ModelThrottledException``), none of which appear in ``ExtractionStep``'s or
``AssessmentStep``'s ``ErrorEquals`` in ``workflow.asl.json`` — so failing fast
would convert a retryable ``Sandbox.Timedout`` into an unrecoverable task failure.

Every test patches its sleep function. Without that, a regression of the clamp
makes these tests HANG (50 x 1800s) and burn the CI job timeout instead of failing.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import botocore.exceptions
import pytest

from idp_common.utils.bedrock_utils import (
    async_exponential_backoff_retry,
    clamp_sleep_to_budgets,
    exponential_backoff_retry,
    get_lambda_deadline_epoch,
    set_lambda_deadline_epoch,
)


@pytest.fixture(autouse=True)
def _clear_deadline():
    """The deadline is a ContextVar; leaking it would couple these tests."""
    set_lambda_deadline_epoch(None)
    yield
    set_lambda_deadline_epoch(None)


def _throttle() -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse"
    )


# ---------------------------------------------------------------------------
# clamp_sleep_to_budgets
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_deadline_and_no_budget_leaves_the_sleep_alone():
    """Outside Lambda the deadline is unknown and behaviour must be unchanged."""
    assert get_lambda_deadline_epoch() is None
    assert clamp_sleep_to_budgets(1800) == 1800


@pytest.mark.unit
def test_sleep_that_fits_is_not_shortened():
    set_lambda_deadline_epoch(time.time() + 600)
    assert clamp_sleep_to_budgets(60) == pytest.approx(60, abs=1)


@pytest.mark.unit
def test_sleep_longer_than_the_invocation_is_shortened_not_refused():
    """The headline case: 1800s of backoff with 100s left becomes a short sleep,
    NOT an exception. Returning a positive number keeps the retry loop alive."""
    set_lambda_deadline_epoch(time.time() + 100)
    allowed = clamp_sleep_to_budgets(1800)
    assert 0 < allowed <= 70
    assert allowed == pytest.approx(70, abs=1)  # 100 - 30 reserve


@pytest.mark.unit
def test_no_time_left_yields_zero_not_a_negative_sleep():
    """0.0 means "try again immediately". A negative would crash asyncio.sleep."""
    set_lambda_deadline_epoch(time.time() + 5)
    assert clamp_sleep_to_budgets(60) == 0.0
    set_lambda_deadline_epoch(time.time() - 100)  # already past
    assert clamp_sleep_to_budgets(60) == 0.0


@pytest.mark.unit
def test_cumulative_budget_shortens_the_last_sleep_rather_than_dropping_it():
    """With 10s of a 300s budget left, a 60s backoff becomes 10s — the remaining
    budget is spent, not discarded."""
    assert clamp_sleep_to_budgets(60, total_slept=290, max_total_delay=300) == 10
    assert clamp_sleep_to_budgets(60, total_slept=300, max_total_delay=300) == 0.0


@pytest.mark.unit
def test_the_tighter_of_the_two_bounds_wins():
    set_lambda_deadline_epoch(time.time() + 1000)
    assert clamp_sleep_to_budgets(60, total_slept=280, max_total_delay=300) == 20
    set_lambda_deadline_epoch(time.time() + 45)
    assert clamp_sleep_to_budgets(60, total_slept=0, max_total_delay=300) == (
        pytest.approx(15, abs=1)
    )


@pytest.mark.unit
def test_reserve_is_configurable_and_actually_applied():
    """Guards against the reserve being frozen as a default argument, which would
    make a monkeypatched module constant silently do nothing."""
    set_lambda_deadline_epoch(time.time() + 100)
    assert clamp_sleep_to_budgets(1800, reserve=0.0) == pytest.approx(100, abs=1)
    assert clamp_sleep_to_budgets(1800, reserve=90.0) == pytest.approx(10, abs=1)


# ---------------------------------------------------------------------------
# The decorators never raise early
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sync_retry_shortens_the_sleep_and_keeps_retrying():
    """It must NOT fail fast: being killed by the Lambda timeout is retried by Step
    Functions, whereas the raised error name is not in ExtractionStep's ErrorEquals."""
    calls = {"n": 0}

    @exponential_backoff_retry(
        max_retries=4, initial_delay=1800, max_delay=1800, jitter=0.0
    )
    def always_throttled():
        calls["n"] += 1
        raise _throttle()

    set_lambda_deadline_epoch(time.time() + 100)
    with patch("time.sleep") as slept:
        with pytest.raises(botocore.exceptions.ClientError):
            always_throttled()
    assert calls["n"] == 4, "must use its full attempt budget, not bail out early"
    assert slept.call_count == 3
    for call in slept.call_args_list:
        assert 0 <= call.args[0] <= 100, f"slept {call.args[0]}s, longer than remained"


@pytest.mark.unit
def test_sync_retry_respects_the_cumulative_budget():
    calls = {"n": 0}

    @exponential_backoff_retry(
        max_retries=6,
        initial_delay=10,
        max_delay=10,
        jitter=0.0,
        max_total_delay=25,
    )
    def always_throttled():
        calls["n"] += 1
        raise _throttle()

    with patch("time.sleep") as slept:
        with pytest.raises(botocore.exceptions.ClientError):
            always_throttled()
    assert calls["n"] == 6
    total = sum(c.args[0] for c in slept.call_args_list)
    assert total == pytest.approx(25), f"slept {total}s against a 25s budget"


@pytest.mark.unit
def test_sync_retry_unchanged_with_no_deadline_and_no_budget():
    """Guards against over-correction."""
    calls = {"n": 0}

    @exponential_backoff_retry(max_retries=3, initial_delay=5, max_delay=5, jitter=0.0)
    def fails_twice():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _throttle()
        return "ok"

    with patch("time.sleep") as slept:
        assert fails_twice() == "ok"
    assert calls["n"] == 3
    assert [c.args[0] for c in slept.call_args_list] == [5, 5]


@pytest.mark.unit
def test_async_retry_shortens_the_sleep_and_keeps_retrying():
    calls = {"n": 0}

    @async_exponential_backoff_retry(
        max_retries=4, initial_delay=1800, max_delay=1800, jitter=0.0
    )
    async def read_timeout():
        calls["n"] += 1
        raise Exception("Read timed out. (read timeout=120)")

    async def run():
        set_lambda_deadline_epoch(time.time() + 100)
        with patch("asyncio.sleep", new_callable=AsyncMock) as slept:
            with pytest.raises(Exception, match="Read timed out"):
                await read_timeout()
        return slept

    slept = asyncio.run(run())
    assert calls["n"] == 4
    assert slept.await_count == 3
    for call in slept.await_args_list:
        assert 0 <= call.args[0] <= 100


@pytest.mark.unit
def test_async_retry_unchanged_when_the_deadline_is_roomy():
    calls = {"n": 0}

    @async_exponential_backoff_retry(
        max_retries=5, initial_delay=5, max_delay=5, jitter=0.0
    )
    async def fails_twice():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("Read timed out")
        return "ok"

    async def run():
        set_lambda_deadline_epoch(time.time() + 900)
        with patch("asyncio.sleep", new_callable=AsyncMock) as slept:
            assert await fails_twice() == "ok"
        return slept

    slept = asyncio.run(run())
    assert calls["n"] == 3
    assert [c.args[0] for c in slept.await_args_list] == [5, 5]


# ---------------------------------------------------------------------------
# Re-entrancy: concurrent shards must not share a budget
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_concurrent_calls_do_not_share_the_cumulative_budget():
    """The agentic runtime fans shards out concurrently over the SAME decorated
    coroutine. If ``total_slept``/``delay`` were decorator-scoped rather than
    per-call, one shard's sleeping would eat the others' retry budget.

    Asserted on total sleep TIME, not attempt count: a shared accumulator clamps
    later sleeps to 0 without reducing the number of attempts, so counting attempts
    cannot detect it (verified by mutation).
    """
    shards, budget = 4, 25.0

    @async_exponential_backoff_retry(
        max_retries=5,
        initial_delay=10,
        max_delay=10,
        jitter=0.0,
        max_total_delay=budget,
    )
    async def always_fails(shard: int):
        raise Exception("Read timed out")

    async def run():
        with patch("asyncio.sleep", new_callable=AsyncMock) as slept:
            await asyncio.gather(
                *(always_fails(i) for i in range(shards)), return_exceptions=True
            )
        return sum(c.args[0] for c in slept.await_args_list)

    total = asyncio.run(run())
    # Each shard independently spends its own 25s budget -> 100s overall. A shared
    # accumulator would total ~25s no matter how many shards ran.
    assert total == pytest.approx(shards * budget), (
        f"expected {shards} independent {budget}s budgets ({shards * budget}s), "
        f"slept {total}s in total — the budget looks shared across calls"
    )


# ---------------------------------------------------------------------------
# ContextVar propagation, which is what makes the ContextVar approach viable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_deadline_propagates_through_asyncio_primitives():
    """The agentic fan-out uses gather/create_task and asyncio.to_thread, so the
    deadline must survive all three. It does NOT survive a raw ThreadPoolExecutor —
    that limitation is documented on set_lambda_deadline_epoch."""
    deadline = time.time() + 500

    async def run():
        set_lambda_deadline_epoch(deadline)
        direct = get_lambda_deadline_epoch()

        async def child():
            return get_lambda_deadline_epoch()

        gathered = await asyncio.gather(child(), child())
        task = await asyncio.create_task(child())
        threaded = await asyncio.to_thread(get_lambda_deadline_epoch)
        return direct, gathered, task, threaded

    direct, gathered, task, threaded = asyncio.run(run())
    assert direct == deadline
    assert gathered == [deadline, deadline]
    assert task == deadline
    assert threaded == deadline


@pytest.mark.unit
def test_thread_pool_does_not_inherit_the_deadline():
    """Pins the documented limitation so it cannot be silently relied upon."""
    from concurrent.futures import ThreadPoolExecutor

    set_lambda_deadline_epoch(time.time() + 500)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(get_lambda_deadline_epoch).result() is None
    # ...and copy_context() is the documented workaround.
    import contextvars

    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(ctx.run, get_lambda_deadline_epoch).result() is not None


# ---------------------------------------------------------------------------
# The agent decorator's own constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_backoff_constants_fit_inside_one_lambda_invocation():
    """Pins the reported bug. ``max_delay`` was 1800 — twice the whole invocation.
    The bounds are asserted tightly, so a regression to a merely-less-absurd value
    (say 200s inside a 900s function) still fails."""
    pytest.importorskip("strands", reason="agentic extras not installed")
    from idp_common.extraction.agentic_idp import (
        _AGENT_MAX_BACKOFF_SECONDS,
        _AGENT_MAX_TOTAL_BACKOFF_SECONDS,
    )

    assert _AGENT_MAX_BACKOFF_SECONDS <= 60
    assert _AGENT_MAX_TOTAL_BACKOFF_SECONDS <= 300


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
