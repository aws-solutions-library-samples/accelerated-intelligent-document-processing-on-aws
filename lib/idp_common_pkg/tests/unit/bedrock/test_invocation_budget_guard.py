# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No ladder in ``idp_common.bedrock`` begins an attempt its invocation cannot hold.

The defect (#1362): the non-streamed ``converse`` ladder retried a read timeout up to
seven times at ``CONFIDENCE_READ_TIMEOUT_SECONDS`` each, inside a 900 s Lambda, with
nothing checking the remaining invocation time before starting an attempt. Three
attempts fill the function exactly, so a request that needs longer than one read
timeout cannot succeed *and* cannot fail cleanly: the Lambda is killed on the wall
clock, Step Functions calls that deterministic (``Sandbox.Timedout``, ``MaxAttempts:
1`` since #917), and ``ProcessSections`` discards the document. Measured on a live
stack: 900 s of invocation, then a 918 s replay, for a document that was never going
to complete.

**What is asserted here, and what instrument each claim uses.** A pre-attempt budget
gate is a guard, and a guard whose refusal looks exactly like its downstream no-op
cannot be tested behaviourally. This one is observable three ways, so the behavioural
form is available and is what is used:

* **the number of Bedrock calls** — the honest instrument, and the one that cannot be
  satisfied by accident. The fixed ladder makes 2 ``converse`` calls where the broken
  one makes 8.
* **a distinct metric** — ``BedrockRetryAbandonedOutOfTime`` rather than
  ``BedrockMaxRetriesExceeded``. Giving up early and exhausting the ladder are
  different operational events and the dashboards can tell them apart.
* **a distinct log record**, from the gate itself.

What is deliberately NOT distinct is the **exception**: the gate re-raises the
whatever the ladder's own exhausted path would have raised, so the caller classifies
the failure exactly as it would have after the last attempt. On the read-timeout paths
that is ``raise_if_transient`` -> ``TransientError`` ->
``ExtractionStep``/``ShardExtractionStep``, eight retries against a 21,600 s
state-machine budget. On two of them — the OpenAI Responses HTTP-status branch and the
LambdaHook function-error branch — the ladder raises a ``RuntimeError`` that
classification does not read as transient, and the gate raises the same thing rather
than inventing a name of its own; there the gain is the freed invocation and the
released concurrency slot. A distinct exception type would read as a cleaner signal and
would be worse, because it would be classified deterministic everywhere.

**The claim is about capability, not about a spelling.** "No attempt begins whose cost
exceeds the remaining budget" is the rule; "the delay must not be 1800" is a spelling.
So the tests below parametrise over the ladder's own error vocabulary and over its own
exception tuple, both read out of the source rather than transcribed, and the
closure tests derive the set of retry sites instead of listing them — a fifth ladder,
or a fifth refusal site that forgets its metric, fails here.

**The bound is "no attempt begins that the remaining invocation cannot hold, given
what the ladder has already measured"**, not "no attempt overruns". The gate is asked
about the COSTLIEST attempt measured so far rather than the last one, because a
ladder's costs are not monotonic and a cheap failure between two slow ones resets a
last-attempt comparison —
``test_a_cheap_failure_does_not_re_license_an_expensive_attempt`` is the schedule that
exposed it. What remains unrefusable is an attempt more expensive than anything
measured, including every ladder's first:
``test_the_first_attempt_cannot_be_refused_and_that_is_the_stated_bound`` states that
half. Estimating from the client's configured read timeout would close it and would
refuse requests that were going to complete, since a read timeout is an upper bound on
an attempt rather than its expected cost.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from urllib3.exceptions import ReadTimeoutError as Urllib3ReadTimeoutError

from idp_common import lambda_deadline
from idp_common.bedrock import client as client_mod
from idp_common.bedrock.client import DEFAULT_MAX_RETRIES, BedrockClient
from idp_common.lambda_deadline import set_lambda_deadline_epoch
from idp_common.timeout_budget import (
    CONFIDENCE_READ_TIMEOUT_SECONDS,
    LAMBDA_MAX_TIMEOUT_SECONDS,
)

BEDROCK_SOURCE_DIR = Path(client_mod.__file__).parent

#: One instance reused where the exception identity is not what is being tested.
_READ_TIMEOUT = ReadTimeoutError(endpoint_url="https://bedrock.local")

IDP_COMMON_ROOT = Path(client_mod.__file__).resolve().parents[1]


def _ladder_sources() -> list[Path]:
    """Every module in ``idp_common`` that participates in the budget machinery.

    ⚠️ **Membership is "mentions one of the two gate names", and the earlier version
    of this was a tripwire twice over.** It began as the literal list
    ``("client.py", "openai_responses.py")``, which a new
    ``idp_common/bedrock/newladder.py`` walked straight past. Replacing that with
    "mentions ``_calculate_backoff`` or ``_backoff_within_invocation``" was no better
    measured than it looked: it selected *exactly those two files* and excluded
    ``utils/bedrock_utils.py``, which holds the streamed ladder's three sleep sites —
    so the module this test file separately asserts shares the predicate was outside
    the closure that checks how it is used.

    Keying on ``clamp_sleep_to_budgets``/``attempt_cannot_finish`` instead brings in
    every module that reaches the gate. It is still a name test and still has a reach:
    a ladder that mentions neither is not seen here, which is what
    :func:`test_every_sleeping_bedrock_caller_is_enforced_or_registered` exists to
    catch from the other direction.
    """
    found: list[Path] = []
    for path in sorted(IDP_COMMON_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "clamp_sleep_to_budgets" in text or "attempt_cannot_finish" in text:
            found.append(path)
    assert found, "no ladder modules found — the derivation is broken, not the code"
    return found


# ---------------------------------------------------------------------------
# A clock the tests own, because a ladder must never be tested with real sleeps
# ---------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock the test drives, standing in for the ``time`` module.

    Patched over the module-global ``time`` in ``bedrock.client`` AND in
    ``lambda_deadline``, because the gate's arithmetic and the ladder's measurement
    have to agree about what time it is. Patching only one of them would make every
    assertion below meaningless, so :func:`fake_clock` asserts the pairing.

    Real sleeps are not an option: this ladder's nominal worst case is around 2,650
    seconds of attempts and sleeps, which would simply look like a hung test run.
    """

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> Generator[FakeClock]:
    clock = FakeClock()
    monkeypatch.setattr(client_mod, "time", clock)
    monkeypatch.setattr(lambda_deadline, "time", clock)
    # Jitter is 0..1s and would make the attempt arithmetic below approximate.
    monkeypatch.setattr(client_mod.random, "random", lambda: 0.0)
    assert client_mod.time is clock and lambda_deadline.time is clock
    yield clock
    set_lambda_deadline_epoch(None)


@pytest.fixture(autouse=True)
def _no_deadline_leaks():
    """A ContextVar set in one test must not decide another one's verdict."""
    set_lambda_deadline_epoch(None)
    yield
    set_lambda_deadline_epoch(None)


@pytest.fixture
def recorded_metrics():
    """Capture ``_put_metric`` names through the real method, not around it."""
    with patch("idp_common.metrics.put_metric") as put_metric:
        yield lambda: [call.args[0] for call in put_metric.call_args_list]


def _converse_params() -> dict[str, Any]:
    """The five keys ``_invoke_with_retry``'s own logging indexes unconditionally."""
    return {
        "modelId": "us.anthropic.claude-sonnet-4-6",
        "messages": [{"role": "user", "content": [{"text": "extract this"}]}],
        "system": [{"text": "you extract"}],
        "inferenceConfig": {"maxTokens": 4096},
        "additionalModelRequestFields": {},
    }


def _client_with(side_effect: list[Any] | Callable[..., Any]) -> BedrockClient:
    """A ``BedrockClient`` whose ``converse`` fails on a schedule.

    ``side_effect`` is always a sequence or callable, never a bare ``return_value``:
    on a ``MagicMock`` every attribute read is auto-created and truthy, so a ladder
    keyed on one never terminates.
    """
    client = BedrockClient(region="us-east-1")
    client._client = MagicMock()
    client._client.converse.side_effect = side_effect
    return client


def _stalling_converse(clock: FakeClock, stall_seconds: float, error: BaseException):
    """A ``converse`` that occupies ``stall_seconds`` of the clock, then fails."""

    def _call(**_kwargs):
        clock.advance(stall_seconds)
        raise error

    return _call


# ---------------------------------------------------------------------------
# The reported failure, and the number of calls it costs
# ---------------------------------------------------------------------------


def _timeout_error_instances() -> dict[str, BaseException]:
    """One instance of every exception type the timeout branch catches.

    Universe closure, not a sample: :func:`test_every_caught_timeout_type_is_covered`
    reads the ``except`` tuple out of the source and fails if this mapping does not
    account for all of it. A hand-picked subset is how a new exception type gets
    added to production and tested by nothing.
    """
    return {
        "ReadTimeoutError": ReadTimeoutError(endpoint_url="https://bedrock.local"),
        "ConnectTimeoutError": ConnectTimeoutError(
            endpoint_url="https://bedrock.local"
        ),
        "EndpointConnectionError": EndpointConnectionError(
            endpoint_url="https://bedrock.local"
        ),
        "ConnectionClosedError": ConnectionClosedError(
            endpoint_url="https://bedrock.local"
        ),
        "Urllib3ReadTimeoutError": Urllib3ReadTimeoutError(
            None, "https://bedrock.local", "Read timed out."
        ),
        "RequestsReadTimeout": client_mod.RequestsReadTimeout("Read timed out."),
        "RequestsConnectTimeout": client_mod.RequestsConnectTimeout("Connect timeout"),
    }


@pytest.mark.unit
@pytest.mark.parametrize("error_name", sorted(_timeout_error_instances()))
def test_a_stalling_ladder_stops_before_the_attempt_that_cannot_finish(
    fake_clock: FakeClock, recorded_metrics, error_name: str
):
    """The #1362 trace, as arithmetic: 300 s attempts inside a 900 s invocation.

    Attempt 1 ends at 300 s and attempt 2 at ~602 s, leaving ~298 s — less than the
    ~304 s another attempt plus its backoff needs. The third attempt is the one the
    invocation cannot hold, and it is not made. Broken, the ladder makes all eight.
    """
    error = _timeout_error_instances()[error_name]
    client = _client_with(
        _stalling_converse(fake_clock, CONFIDENCE_READ_TIMEOUT_SECONDS, error)
    )
    set_lambda_deadline_epoch(fake_clock.now + LAMBDA_MAX_TIMEOUT_SECONDS)

    with pytest.raises(type(error)):
        client._invoke_with_retry(
            model_id="us.anthropic.claude-sonnet-4-6",
            converse_params=_converse_params(),
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=fake_clock.now,
        )

    assert client._client.converse.call_count == 2, (
        "the ladder made an attempt the invocation could not hold "
        f"({client._client.converse.call_count} attempts)"
    )
    names = recorded_metrics()
    assert names.count("BedrockRetryAbandonedOutOfTime") == 1, names
    assert names.count("BedrockRequestsFailed") == 1, names
    assert "BedrockMaxRetriesExceeded" not in names, (
        "giving up for want of time was reported as exhausting the ladder"
    )
    deadline = lambda_deadline.get_lambda_deadline_epoch()
    assert deadline is not None
    assert fake_clock.now < deadline, "the ladder ran past the deadline it was given"


@pytest.mark.unit
def test_the_attempt_it_refuses_is_the_one_that_filled_the_invocation(
    fake_clock: FakeClock,
):
    """Without the gate, the same input spends the whole invocation and overruns it.

    This is the pre-fix measurement, kept as a test so the number in the commit
    message is reproducible: eight 300 s attempts plus the jitter-free floor of their
    backoff is 2,654 s of a 900 s function.
    """
    nominal = (DEFAULT_MAX_RETRIES + 1) * CONFIDENCE_READ_TIMEOUT_SECONDS + sum(
        min(client_mod.DEFAULT_MAX_BACKOFF, client_mod.DEFAULT_INITIAL_BACKOFF * 2**n)
        for n in range(DEFAULT_MAX_RETRIES)
    )
    assert nominal == pytest.approx(2654.0), nominal
    assert nominal > 2 * LAMBDA_MAX_TIMEOUT_SECONDS

    # And with the gate, the same ladder gives up having spent one invocation's worth
    # of attempts rather than three.
    client = _client_with(
        _stalling_converse(
            fake_clock,
            CONFIDENCE_READ_TIMEOUT_SECONDS,
            ReadTimeoutError(endpoint_url="https://bedrock.local"),
        )
    )
    started = fake_clock.now
    set_lambda_deadline_epoch(started + LAMBDA_MAX_TIMEOUT_SECONDS)
    with pytest.raises(ReadTimeoutError):
        client._invoke_with_retry(
            model_id="m",
            converse_params=_converse_params(),
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=started,
        )
    spent = fake_clock.now - started
    assert spent < LAMBDA_MAX_TIMEOUT_SECONDS, spent


@pytest.mark.unit
def test_the_backoff_counts_against_the_budget_and_can_be_what_refuses(
    fake_clock: FakeClock,
):
    """The sleep comes out of the same budget, so it is part of the attempt's cost.

    This is where the port differs from the streamed ladder it came from, which asks
    the question before computing its backoff. Here the cost passed to the gate is the
    measured attempt PLUS the delay about to be slept, and the difference is a real
    case rather than tidiness: with 140 s left, a 100 s attempt fits and a 64 s sleep
    followed by a 100 s attempt does not. Counting only the attempt lets the ladder
    sleep itself into a position where the next attempt cannot return.
    """
    client = _client_with(_stalling_converse(fake_clock, 100.0, _READ_TIMEOUT))
    set_lambda_deadline_epoch(fake_clock.now + 240.0)

    with pytest.raises(ReadTimeoutError):
        client._invoke_with_retry(
            model_id="m",
            converse_params=_converse_params(),
            retry_count=5,  # a 64s backoff, which is what tips this over
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=fake_clock.now,
        )

    assert client._client.converse.call_count == 1, (
        "the ladder slept into a window too small for the attempt that followed"
    )


@pytest.mark.unit
def test_the_remaining_time_helper_reports_a_number_and_not_unknown(
    fake_clock: FakeClock,
):
    """It feeds the clamp's log line, which is how an operator sees the clamp at all.

    Tested directly because the only consumer is a log message: a version that
    returned ``None`` whenever a deadline WAS set would print "unknown" forever and
    make the clamp invisible, while every behavioural assertion above still passed.
    Negative is a real answer — a ladder past its deadline — so the sign is asserted
    rather than clamped at zero.
    """
    assert lambda_deadline.remaining_invocation_seconds() is None
    set_lambda_deadline_epoch(fake_clock.now + 120.0)
    assert lambda_deadline.remaining_invocation_seconds() == pytest.approx(120.0)
    fake_clock.advance(200.0)
    assert lambda_deadline.remaining_invocation_seconds() == pytest.approx(-80.0)


@pytest.mark.unit
def test_an_attempt_that_exactly_fits_is_begun(fake_clock: FakeClock):
    """The boundary, stated: ``remaining == cost`` means it fits.

    A direct test on the predicate because no realistic ladder lands on exact
    equality, so nothing above distinguishes ``>=`` from ``>``. The rule is "cannot
    finish", and an attempt with exactly its own duration left can.
    """
    set_lambda_deadline_epoch(fake_clock.now + 100.0)
    assert lambda_deadline.attempt_cannot_finish(100.0, "probe") is False
    assert lambda_deadline.attempt_cannot_finish(100.001, "probe") is True


@pytest.mark.unit
def test_an_unmeasured_attempt_is_never_refused(fake_clock: FakeClock):
    """With nothing measured there is nothing to compare, so the answer is "begin it".

    ⚠️ This is a **contract** test on the predicate, not a behavioural one: no call
    site in either ladder reaches it with ``None``, because every call sits at a retry
    decision, which by construction follows a measured attempt. So the branch is
    defensive, and it is asserted here rather than left to be removed as dead — a
    future caller that asks before its first attempt must get False, or the gate would
    refuse every ladder's opening request and fail every document.
    """
    set_lambda_deadline_epoch(fake_clock.now - 10_000.0)  # long past the deadline
    assert lambda_deadline.attempt_cannot_finish(None, "probe") is False


@pytest.mark.unit
def test_the_first_attempt_cannot_be_refused_and_that_is_the_stated_bound(
    fake_clock: FakeClock,
):
    """What "given what the ladder has already measured" costs, stated as a test.

    Entered with 10 seconds left, the ladder still makes one 300 s attempt: nothing has
    been measured yet, so there is nothing to compare the remaining time against.
    Closing this would mean estimating the cost from the client's configured read
    timeout, which refuses requests that were going to complete — see the gate's
    docstring. What the gate guarantees is that there is no second attempt at a cost it
    has now measured; it does not guarantee that no attempt overruns.
    """
    client = _client_with(
        _stalling_converse(
            fake_clock,
            CONFIDENCE_READ_TIMEOUT_SECONDS,
            ReadTimeoutError(endpoint_url="https://bedrock.local"),
        )
    )
    set_lambda_deadline_epoch(fake_clock.now + 10.0)
    with pytest.raises(ReadTimeoutError):
        client._invoke_with_retry(
            model_id="m",
            converse_params=_converse_params(),
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=fake_clock.now,
        )
    assert client._client.converse.call_count == 1


# ---------------------------------------------------------------------------
# What the gate must NOT do: stop a ladder of cheap attempts near the wall
# ---------------------------------------------------------------------------


def _retryable_codes_by_ladder() -> dict[str, list[str]]:
    """Each ladder's own ``retryable_errors`` list, keyed by its function name.

    Derived rather than transcribed, and asserted non-empty, because an empty derived
    list collects as a pytest *skip* rather than a failure — which silently voids the
    guarantee it was supposed to carry. Keyed per ladder because the three lists are
    genuinely different: ``EC2ThrottledException`` is retryable for a LambdaHook
    invocation and non-retryable for ``converse``, so one merged list makes the
    parametrised test below assert the opposite of the truth for five codes.
    """
    tree = ast.parse((BEDROCK_SOURCE_DIR / "client.py").read_text(encoding="utf-8"))
    by_ladder: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for assign in ast.walk(node):
            if (
                isinstance(assign, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "retryable_errors"
                    for t in assign.targets
                )
                and isinstance(assign.value, ast.List)
            ):
                by_ladder.setdefault(node.name, []).extend(
                    e.value
                    for e in assign.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                )
    assert by_ladder, "could not read any retryable-code list from client.py"
    return by_ladder


@pytest.mark.unit
def test_every_retryable_vocabulary_belongs_to_a_ladder_with_a_cheap_attempt_test():
    """Universe closure over the per-ladder error lists.

    The cheap-attempt tests below parametrise over one ladder's vocabulary each. This
    is what fails if a FOURTH ``retryable_errors`` list appears in ``client.py``,
    rather than that list going untested while every other test here stays green.
    """
    found = set(_retryable_codes_by_ladder())
    covered = {
        "_invoke_with_retry",
        "_generate_embedding_with_retry",
        "_invoke_lambda_hook_with_retry",
    }
    assert found == covered, (
        f"client.py has retryable-code lists in {sorted(found - covered)} that no "
        f"test exercises, and the tests name {sorted(covered - found)} which is gone"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "error_code", _retryable_codes_by_ladder()["_invoke_with_retry"]
)
def test_a_cheap_attempt_is_never_refused_however_close_the_wall_is(
    fake_clock: FakeClock, recorded_metrics, error_code: str
):
    """A throttle refused in milliseconds still gets every retry, with 5 s left.

    This is the half of the rule that a guard written as "refuse when the remaining
    time is under the read timeout" would get wrong, and it is the behaviour the
    wall-clock bound is documented to have everywhere in this codebase: near the wall
    the attempts themselves are what consume the clock, and an attempt that costs
    nothing always fits. Parametrised over the ladder's whole vocabulary, because the
    rule is about what an attempt COSTS and not about which code named it.
    """
    error = ClientError(
        {"Error": {"Code": error_code, "Message": "slow down"}}, "Converse"
    )
    client = _client_with([error] * (DEFAULT_MAX_RETRIES + 1))
    set_lambda_deadline_epoch(fake_clock.now + 5.0)

    with pytest.raises(ClientError):
        client._invoke_with_retry(
            model_id="m",
            converse_params=_converse_params(),
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=fake_clock.now,
        )

    assert client._client.converse.call_count == DEFAULT_MAX_RETRIES + 1
    names = recorded_metrics()
    assert "BedrockRetryAbandonedOutOfTime" not in names
    assert "BedrockMaxRetriesExceeded" in names


@pytest.mark.unit
def test_with_no_deadline_published_the_ladder_is_exactly_what_it_was(
    fake_clock: FakeClock,
):
    """Outside Lambda, and in any handler that publishes no deadline, this is a no-op.

    Most handlers that reach this client publish nothing, so "unchanged" is the
    behaviour the majority of callers see, and it has to be unchanged in both halves:
    the attempt count AND the time spent asleep.
    """
    client = _client_with(
        _stalling_converse(
            fake_clock,
            CONFIDENCE_READ_TIMEOUT_SECONDS,
            ReadTimeoutError(endpoint_url="https://bedrock.local"),
        )
    )
    assert lambda_deadline.get_lambda_deadline_epoch() is None
    with pytest.raises(ReadTimeoutError):
        client._invoke_with_retry(
            model_id="m",
            converse_params=_converse_params(),
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=fake_clock.now,
        )
    assert client._client.converse.call_count == DEFAULT_MAX_RETRIES + 1
    expected_sleeps = [
        min(client_mod.DEFAULT_MAX_BACKOFF, client_mod.DEFAULT_INITIAL_BACKOFF * 2**n)
        for n in range(DEFAULT_MAX_RETRIES)
    ]
    assert fake_clock.sleeps == pytest.approx(expected_sleeps)


# ---------------------------------------------------------------------------
# The other half of the rule: a sleep may not outlive the invocation either
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_backoff_sleeps_past_the_end_of_the_invocation(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
):
    """The gate is unreachable if the ladder can be killed during a sleep.

    ``max_backoff`` is 300 s, so deep in the ladder one sleep alone can exceed what
    remains. Every sleep is therefore clamped, and the clamp is what makes the
    pre-attempt check something the invocation actually lives to perform.
    """
    client = _client_with(
        [ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "C")]
        * (DEFAULT_MAX_RETRIES + 1)
    )
    deadline = fake_clock.now + 40.0
    set_lambda_deadline_epoch(deadline)

    with caplog.at_level("WARNING", logger="idp_common.bedrock.client"):
        with pytest.raises(ClientError):
            client._invoke_with_retry(
                model_id="m",
                converse_params=_converse_params(),
                retry_count=5,  # _calculate_backoff(5) is 64s, more than the 40s left
                max_retries=DEFAULT_MAX_RETRIES,
                request_start_time=fake_clock.now,
            )

    assert fake_clock.sleeps, "the ladder did not back off at all"
    assert max(fake_clock.sleeps) <= 40.0 - lambda_deadline.DEADLINE_RESERVE_SECONDS
    assert fake_clock.now <= deadline
    # The clamp says so in the log, because a backoff that silently shrinks is a
    # latency change an operator has no way to see.
    shortened = [
        r.getMessage() for r in caplog.records if "Shortening" in r.getMessage()
    ]
    assert shortened, caplog.text
    assert "64.0s to 10.0s" in shortened[0], shortened[0]


@pytest.mark.unit
def test_the_clamp_leaves_an_ordinary_backoff_alone(fake_clock: FakeClock):
    """With room to spare the sleep is the sleep the ladder asked for.

    A clamp that shortened every backoff would pass the test above and turn the whole
    ladder into an unbacked-off burst against a service asking us to slow down.
    """
    client = BedrockClient(region="us-east-1")
    set_lambda_deadline_epoch(fake_clock.now + LAMBDA_MAX_TIMEOUT_SECONDS)
    assert client._backoff_within_invocation(5) == pytest.approx(64.0)
    set_lambda_deadline_epoch(None)
    assert client._backoff_within_invocation(5) == pytest.approx(64.0)


# ---------------------------------------------------------------------------
# One implementation of the rule, and every ladder reaching it
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_streamed_and_non_streamed_paths_share_one_predicate():
    """Not two predicates that agree today.

    The streamed agentic ladder got this gate first (#1310); #1362 was the same defect
    on the non-streamed one. A copied predicate is how the next ladder gets a subtly
    different rule, so both paths resolve to the same function object.
    """
    from idp_common.utils import bedrock_utils

    assert bedrock_utils.attempt_cannot_finish is lambda_deadline.attempt_cannot_finish
    assert (
        bedrock_utils.clamp_sleep_to_budgets is lambda_deadline.clamp_sleep_to_budgets
    )
    assert (
        bedrock_utils.get_lambda_deadline_epoch
        is lambda_deadline.get_lambda_deadline_epoch
    )


def _statement_blocks(tree: ast.AST):
    """Every list of statements in ``tree``, with the node that owns it."""
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if (
                isinstance(block, list)
                and block
                and all(isinstance(stmt, ast.stmt) for stmt in block)
            ):
                yield node, block


def _sleep_calls(tree: ast.AST) -> list[ast.Call]:
    """Every call to something named ``sleep``, by any spelling the AST can see."""
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "sleep") or (
            isinstance(func, ast.Name) and func.id == "sleep"
        ):
            found.append(node)
    return found


def _sleep_sites() -> list[tuple[str, int, list[ast.stmt], ast.Call, str]]:
    """Every sleep in a ladder module, with its block, its call and its function.

    ``await asyncio.sleep(x)`` is an ``Expr`` wrapping an ``Await`` wrapping the call,
    so the unwrap matters: without it the streamed decorator's two sleeps are invisible
    and the module looks compliant because it looks empty.
    """
    sites: list[tuple[str, int, list[ast.stmt], ast.Call, str]] = []
    for path in _ladder_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        enclosing = _enclosing_functions(tree)
        for _owner, block in _statement_blocks(tree):
            for stmt in block:
                if not isinstance(stmt, ast.Expr):
                    continue
                value = stmt.value
                if isinstance(value, ast.Await):
                    value = value.value
                if not isinstance(value, ast.Call):
                    continue
                func = value.func
                named_sleep = (
                    isinstance(func, ast.Attribute) and func.attr == "sleep"
                ) or (isinstance(func, ast.Name) and func.id == "sleep")
                if named_sleep:
                    sites.append(
                        (
                            path.name,
                            stmt.lineno,
                            block,
                            value,
                            enclosing.get(stmt.lineno, "<module>"),
                        )
                    )
    return sites


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Line number -> name of the innermost function containing it."""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                owner[line] = node.name
    return owner


def _function_bodies() -> dict[str, ast.AST]:
    """Every function in the ladder modules, by name. Names are unique enough here."""
    bodies: dict[str, ast.AST] = {}
    for path in _ladder_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bodies[node.name] = node
    return bodies


def _reaches(
    name: str, target: str, bodies: dict[str, ast.AST], depth: int = 3
) -> bool:
    """Whether function ``name`` calls ``target``, following up to ``depth`` hops."""
    if depth == 0 or name not in bodies:
        return False
    called: set[str] = set()
    for node in ast.walk(bodies[name]):
        if isinstance(node, ast.Call):
            func = node.func
            callee = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if callee == target:
                return True
            if callee:
                called.add(callee)
    return any(_reaches(c, target, bodies, depth - 1) for c in called)


def _delay_sources() -> tuple[set[str], set[str]]:
    """(functions whose result is clamped, those that also consult the gate).

    Derived rather than listed, and the distinction is what lets one rule cover two
    shapes: ``_backoff_within_invocation`` clamps but leaves the gate to its caller,
    while ``_backoff_or_none`` does both and signals the stop by returning ``None``.
    """
    bodies = _function_bodies()
    clamped = {n for n in bodies if _reaches(n, "clamp_sleep_to_budgets", bodies)}
    self_gating = {n for n in clamped if _reaches(n, "attempt_cannot_finish", bodies)}
    assert clamped, "no clamping delay source found — the derivation is broken"
    assert self_gating, "no self-gating delay source found — ditto"
    return clamped, self_gating


def _function_node(
    filename: str, lineno: int
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The innermost function containing ``lineno`` in ``filename``.

    By line rather than by name: ``bedrock_utils`` defines two functions called
    ``wrapper``, one per decorator, and a name lookup returns whichever comes first —
    so the async decorator's two retry branches were attributed to the sync one and
    read as a reassignment.
    """
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for path in _ladder_sources():
        if path.name != filename:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                if best is None or node.lineno > best.lineno:
                    best = node
    return best


def _assigned_from(block: list[ast.stmt], name: str) -> list[ast.expr]:
    """Every value assigned to ``name`` by a statement at any depth under ``block``."""
    values: list[ast.expr] = []
    for stmt in block:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                values.append(node.value)
    return values


def _is_call_to(node: ast.expr, name: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == name
    return isinstance(func, ast.Attribute) and func.attr == name


def _callee_name(node: ast.expr) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _block_chain(tree: ast.AST, lineno: int) -> list[list[ast.stmt]]:
    """The statement blocks containing ``lineno``, innermost first."""
    chain: list[tuple[int, list[ast.stmt]]] = []
    for _owner, block in _statement_blocks(tree):
        first = min(stmt.lineno for stmt in block)
        last = max(stmt.end_lineno or stmt.lineno for stmt in block)
        if first <= lineno <= last:
            chain.append((last - first, block))
    return [block for _span, block in sorted(chain)]


def _guard_sites() -> list[tuple[str, int, ast.If, list[ast.stmt]]]:
    """Every ``if attempt_cannot_finish(...)``, with the block it sits in."""
    sites: list[tuple[str, int, ast.If, list[ast.stmt]]] = []
    for path in _ladder_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for _owner, block in _statement_blocks(tree):
            for node in block:
                if isinstance(node, ast.If) and _is_call_to(
                    node.test, "attempt_cannot_finish"
                ):
                    sites.append((path.name, node.lineno, node, block))
    return sites


def _names_in(node: ast.expr) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _resolve_cost_names(block: list[ast.stmt], cost: ast.expr) -> set[str]:
    """Names the cost expression reaches, following one level of local assignment.

    ``attempt_cannot_finish(worst + backoff, ...)`` mentions two locals; what the
    assertion is about is which *values* they carry, so each name assigned in the same
    block is replaced by the names of the expression assigned to it. One level is
    enough for every site and keeps this readable; a deeper chain would make the
    assertion itself the thing needing a test.
    """
    reached = set(_names_in(cost))
    for name in list(reached):
        for value in _assigned_from(block, name):
            reached |= _names_in(value)
    return reached


def _guard_tests(block: list[ast.stmt]) -> list[ast.If]:
    """The ``if attempt_cannot_finish(...)`` statements directly in ``block``."""
    return [
        stmt
        for stmt in block
        if isinstance(stmt, ast.If) and _is_call_to(stmt.test, "attempt_cannot_finish")
    ]


#: How many of each there were when this was audited. Count pinning: a new retry site
#: inside an already-covered module still fails here rather than inheriting the
#: verdict, which is what makes the two closure tests below worth anything.
#: Counts as audited. Count pinning: a new retry site inside an already-covered
#: module fails here rather than inheriting the verdict, and a LOST site fails too —
#: which is a different remedy, so the messages say which direction moved.
AUDITED_SLEEP_SITES = 11
AUDITED_RAISING_GATES = 8
AUDITED_RETURNING_GATES = 1

#: The exact metric names a refusal may emit, per ladder. Matched exactly rather than
#: by suffix: a rename is invisible to an ``endswith`` check, which accepted
#: ``TypoRequestsFailed``, and the full string is what a dashboard or alarm would have
#: to be pointed at. Of the six, only ``BedrockRequestsFailed`` is plotted today
#: (``patterns/unified/template.yaml``); the three ``*RetryAbandonedOutOfTime`` names
#: are new in #1362 and nothing reads them yet.
EXPECTED_REFUSAL_METRICS = frozenset(
    {
        "BedrockRetryAbandonedOutOfTime",
        "BedrockEmbeddingRetryAbandonedOutOfTime",
        "LambdaHookRetryAbandonedOutOfTime",
    }
)
EXPECTED_FAILURE_METRICS = frozenset(
    {
        "BedrockRequestsFailed",
        "BedrockEmbeddingRequestsFailed",
        "LambdaHookRequestsFailed",
    }
)


@pytest.mark.unit
def test_every_retry_sleep_takes_a_gated_delay():
    """Derived closure over the ladders, so a fifth one cannot arrive unguarded.

    The repeated defect in this repository is a fix applied to the instance and not to
    the class: #1310 fixed the streamed ladder and #1362 found the same hole in the
    non-streamed one, with two more in the same file and a fourth in
    ``openai_responses``. So the rule is read off the source rather than listed, and it
    has to cover **two shapes**, which is the part a list of sites hides:

    * ``bedrock/client.py`` and ``openai_responses.py`` compute a clamped delay with
      ``_backoff_within_invocation`` and ask the gate separately in the same block,
      raising when it refuses;
    * ``utils/bedrock_utils.py``'s decorators get both from ``_backoff_or_none``,
      which clamps AND consults the gate and signals the refusal by returning ``None``.

    Expressed as: the delay must come from a function that reaches
    ``clamp_sleep_to_budgets``, assigned exactly once in the enclosing function; and if
    that function does not itself reach ``attempt_cannot_finish``, the sleep's own block
    must. Both halves are derived, so a new helper that clamps and gates is accepted
    without being named here and one that does neither is not.
    """
    sites = _sleep_sites()
    clamped, self_gating = _delay_sources()
    assert sites, "no retry sleeps found — the derivation is broken, not the code"
    assert len(sites) == AUDITED_SLEEP_SITES, (
        f"{len(sites)} retry sleeps across {[p.name for p in _ladder_sources()]} "
        f"against {AUDITED_SLEEP_SITES} audited — a site was "
        f"{'added: cover it' if len(sites) > AUDITED_SLEEP_SITES else 'removed: check nothing else needed it'}"
        ", then move the count."
    )
    for filename, lineno, block, call, function in sites:
        where = f"{filename}:{lineno} ({function})"
        assert len(call.args) == 1 and isinstance(call.args[0], ast.Name), (
            f"{where} sleeps on something other than a local name, so this test "
            "cannot see where the delay came from"
        )
        delay = call.args[0].id
        # Resolved up the block chain, innermost first, rather than in the sleep's own
        # block or over the whole function. Block-only red-lines ordinary code —
        # `if backoff > 0: time.sleep(backoff)` assigns one level up; function-wide
        # red-lines a function with two independent retry branches, which
        # `_invoke_with_retry` is. Exactly one assignment at the level that defines it,
        # so a nested reassignment — including an un-clamping
        # `backoff = min(backoff * 2, 600)` — still fails.
        owner = _function_node(filename, lineno)
        assert owner is not None, where
        chain = _block_chain(owner, lineno) + [owner.body]
        sources: list[ast.expr] = []
        for level in chain:
            sources = _assigned_from(level, delay)
            if sources:
                break
        assert len(sources) == 1, (
            f"{where} sleeps on {delay!r}, which is assigned {len(sources)} times at "
            "the innermost level that defines it — this test can only follow a single "
            "assignment, and a reassignment is how an unclamped delay reaches a sleep"
        )
        source = _callee_name(sources[0])
        assert source in clamped, (
            f"{where} sleeps on {delay!r} from {source!r}, which does not reach "
            f"clamp_sleep_to_budgets. Clamping sources: {sorted(clamped)}"
        )
        if source not in self_gating:
            guards = _guard_tests(block)
            assert guards, (
                f"{where} takes its delay from {source!r}, which clamps but does not "
                "consult the gate, and its own block does not either"
            )
            assert all(
                any(isinstance(stmt, ast.Raise) for stmt in guard.body)
                for guard in guards
            ), f"{where} has a budget gate whose body does not raise"


@pytest.mark.unit
def test_every_sleep_in_a_ladder_module_is_one_this_test_can_see():
    """Closure over the *spellings*, because the collector matches one shape.

    ``_sleep_sites`` collects a statement-level call to something named ``sleep``,
    unwrapping ``await``. Four other spellings execute identically and would be
    invisible: a bare ``sleep(x)`` after ``from time import sleep``, an assigned
    ``_ = time.sleep(x)``, a ``getattr(time, "sleep")(x)``, and a sleep nested inside
    an expression. This fails on any of them rather than letting the closure above
    silently stop covering a module.
    """
    for path in _ladder_sources():
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        calls = _sleep_calls(tree)
        collected = [
            lineno for name, lineno, _b, _c, _f in _sleep_sites() if name == path.name
        ]
        assert len(calls) == len(collected), (
            f"{path.name} has {len(calls)} sleep call(s) and the closure collected "
            f"{len(collected)}: a spelling is escaping it"
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {"time", "asyncio"}:
                assert all(a.name != "sleep" for a in node.names), (
                    f"{path.name} imports sleep directly, which the collector's "
                    "attribute match does not see"
                )
            if _is_call_to(node, "getattr") and any(
                isinstance(a, ast.Constant) and a.value == "sleep" for a in node.args
            ):
                raise AssertionError(f"{path.name} reaches sleep through getattr")


@pytest.mark.unit
def test_the_unclamped_backoff_has_exactly_one_caller():
    """``_calculate_backoff`` is reachable only through the clamping wrapper.

    Otherwise the closure above is satisfied by a block that computes a clamped delay
    and then sleeps on an unclamped one.
    """
    callers: list[str] = []
    for path in _ladder_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Every callable form, not only ``def``: an ``async def`` or a lambda calling
        # the unclamped helper used to pass this unnoticed.
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            ):
                continue
            name = getattr(node, "name", "<lambda>")
            for inner in ast.walk(node):
                if _is_call_to(inner, "_calculate_backoff"):
                    callers.append(f"{path.name}:{name}")
    assert sorted(set(callers)) == ["client.py:_backoff_within_invocation"], callers


@pytest.mark.unit
def test_every_refusal_is_observable_and_ends_its_ladder():
    """A refusal that is silent is indistinguishable from the ladder working.

    That is the specific weakness a budget gate has: skip the attempt, hand back what
    the attempt would have handed back, and no input tells the fixed code from the
    broken one. So every gate site has to end its ladder, and the sites that end it by
    raising have to say so under a name something can read.

    **The sites come in two kinds and the rule differs, so both are enumerated and
    neither is allowed to be absent.** The eight in ``bedrock/client.py`` and
    ``openai_responses.py`` raise, and those record a metric; the one in
    ``utils/bedrock_utils._backoff_or_none`` returns ``None``, which its two callers
    turn into a raise, and the decorator has no metric surface of its own. A site in
    neither category fails here rather than being read as compliant.
    """
    sites = _guard_sites()
    assert sites, "no budget gate found in the ladder modules"
    emitted: set[str] = set()
    raising: list[tuple[str, int, ast.If, list[ast.stmt]]] = []
    returning: list[tuple[str, int, ast.If, list[ast.stmt]]] = []
    for site in sites:
        _filename, _lineno, node, _block = site
        if any(isinstance(stmt, ast.Raise) for stmt in node.body):
            raising.append(site)
        elif any(isinstance(stmt, ast.Return) for stmt in node.body):
            returning.append(site)
        else:
            raise AssertionError(
                f"{_filename}:{_lineno} refuses an attempt and then carries on — a "
                "gate that neither raises nor returns is the silent case this test "
                "exists for"
            )
    assert len(raising) == AUDITED_RAISING_GATES, (
        f"{len(raising)} raising gates against {AUDITED_RAISING_GATES} audited — a "
        f"site was {'added: give it a metric' if len(raising) > AUDITED_RAISING_GATES else 'removed: check the ladder is still bounded'}"
        ", then move the count."
    )
    assert len(returning) == AUDITED_RETURNING_GATES, (
        f"{len(returning)} returning gates against {AUDITED_RETURNING_GATES} audited."
    )

    for filename, lineno, node, block in sites:
        where = f"{filename}:{lineno}"
        # The cost this site asks about has to carry BOTH terms. Checked per site
        # because the behavioural tests exercise the rule at some sites and not all,
        # and dropping either term at any one of them is invisible to every other test.
        assert node.test.args, f"{where} calls the gate with no cost at all"
        reached = _resolve_cost_names(block, node.test.args[0])
        assert "worst_attempt_seconds" in reached, (
            f"{where} asks about the last attempt rather than the costliest one, so a "
            f"cheap failure here re-licenses an expensive attempt: {sorted(reached)}"
        )

    for filename, lineno, node, block in raising:
        where = f"{filename}:{lineno}"
        reached = _resolve_cost_names(block, node.test.args[0])
        assert "backoff" in reached, (
            f"{where} omits the backoff it is about to sleep from the cost, so the "
            f"sleep can consume the margin the gate just approved: {sorted(reached)}"
        )
        metric_names = [
            arg.value
            for stmt in node.body
            for call in ast.walk(stmt)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "_put_metric"
            for arg in call.args[:1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        assert EXPECTED_REFUSAL_METRICS & set(metric_names), (
            f"{where} refuses an attempt without recording that it did under one of "
            f"{sorted(EXPECTED_REFUSAL_METRICS)}: {metric_names}"
        )
        # Closure in the other direction: every name a refusal site emits belongs to
        # one of the two sets. A name in neither is unaccounted for — nothing reads
        # it, so it is either a typo or a metric nobody wired to a dashboard.
        unaccounted = (
            set(metric_names) - EXPECTED_REFUSAL_METRICS - EXPECTED_FAILURE_METRICS
        )
        assert not unaccounted, (
            f"{where} emits {sorted(unaccounted)}, which is in neither "
            "EXPECTED_REFUSAL_METRICS nor EXPECTED_FAILURE_METRICS, so no dashboard "
            "or alarm in this solution reads it"
        )
        # And nothing may fall through on the way to the raise: a conditional early
        # ``return`` above it is a silent refusal with a loud one underneath.
        assert not any(isinstance(stmt, ast.Return) for stmt in ast.walk(node)), (
            f"{where} can return from its gate body instead of raising"
        )
        emitted |= set(metric_names)

    for filename, lineno, node, _block in returning:
        where = f"{filename}:{lineno}"
        returns = [stmt for stmt in node.body if isinstance(stmt, ast.Return)]
        assert all(
            isinstance(r.value, ast.Constant) and r.value.value is None for r in returns
        ), f"{where} signals the refusal with something other than None"

    # Non-vacuity, the direction the two-way emission closure above cannot see. A
    # member of either set that no site emits is dead, and a dead allowed-name is not
    # inert: it pre-approves whatever next uses that spelling, which is exactly how
    # the ``endswith`` match this replaced became dangerous.
    dead = (EXPECTED_REFUSAL_METRICS | EXPECTED_FAILURE_METRICS) - emitted
    assert not dead, (
        f"{sorted(dead)} are allowed metric names that no gate site emits. Delete "
        "them rather than leaving a name the closure will wave through."
    )


@pytest.mark.unit
def test_the_attempt_timer_is_bound_before_the_try_that_reads_it():
    """A static invariant, because no input can reach the case it protects.

    The gate's cost comes from ``time.time() - attempt_start_time`` inside an
    ``except``. If the only binding sits *inside* the ``try``, an exception raised
    before it — today the logging and payload-serialisation lines above the API call —
    reaches the handler with the name unbound, and the ``UnboundLocalError`` replaces
    the exception the caller needed to see. Nothing today raises there, which is
    exactly why this is asserted on the source rather than behaviourally: a
    behavioural test for it would require a fixture that cannot arise, and a
    defensive binding with no assertion at all is a line the next cleanup deletes.
    """
    tree = ast.parse((BEDROCK_SOURCE_DIR / "client.py").read_text(encoding="utf-8"))
    functions_needing_it: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        reads_in_handler = any(
            isinstance(name, ast.Name) and name.id == "attempt_start_time"
            for handler in ast.walk(node)
            if isinstance(handler, ast.ExceptHandler)
            for name in ast.walk(handler)
        )
        if not reads_in_handler:
            continue
        functions_needing_it.append(node.name)
        # The binding must come before the first Try in the function body.
        first_try = next(
            (i for i, stmt in enumerate(node.body) if isinstance(stmt, ast.Try)), None
        )
        assert first_try is not None, node.name
        bound_before = any(
            isinstance(stmt, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "attempt_start_time"
                for t in stmt.targets
            )
            for stmt in node.body[:first_try]
        )
        assert bound_before, (
            f"{node.name} reads attempt_start_time in an except handler but binds it "
            "only inside the try, so an early failure raises UnboundLocalError "
            "instead of the error the caller has to classify"
        )
    assert len(functions_needing_it) == 3, functions_needing_it


@pytest.mark.unit
def test_every_caught_timeout_type_is_covered():
    """Universe closure over the ``except`` tuple, so a new type is not untested.

    ``_timeout_error_instances`` is the parametrisation for the headline test above.
    If production starts catching a seventh exception type, the parametrised test
    would quietly keep passing on six; this is what fails instead.
    """
    tree = ast.parse((BEDROCK_SOURCE_DIR / "client.py").read_text(encoding="utf-8"))
    caught: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_invoke_with_retry":
            continue
        for handler in ast.walk(node):
            if isinstance(handler, ast.ExceptHandler) and isinstance(
                handler.type, ast.Tuple
            ):
                caught |= {e.id for e in handler.type.elts if isinstance(e, ast.Name)}
    assert caught, "could not read the timeout branch's exception tuple"
    covered = set(_timeout_error_instances())
    assert caught == covered, (
        f"the ladder catches {sorted(caught - covered)} that no test exercises, "
        f"and the tests build {sorted(covered - caught)} it no longer catches"
    )


@pytest.mark.unit
def test_the_shard_handlers_publish_the_deadline_the_gate_needs():
    """The gate is inert unless a handler publishes the deadline.

    A control that exists and is never consulted is the other recurring defect here:
    ``AssessmentFunction`` computed its deadline and threaded it only into the
    escalation ladder, so the Bedrock client ladder inside the same invocation was
    unguarded.

    ⚠️ **Three call sites publish, and they are not "every handler that runs this
    client".** Classification, summarization, evaluation, OCR and the two
    rule-validation functions all reach it and publish nothing, so the gate is a no-op
    there and their ladders behave exactly as before. Publishing in each changes that
    function's failure mode — whether an early transient error beats a wall-clock one
    depends on that task's own retry policy in the state machine — so it is a decision
    per function rather than an omission. What this test holds is that the three which
    do publish keep doing so, as real statements rather than as text.
    """
    repo_root = Path(client_mod.__file__).resolve().parents[4]
    handlers = (
        "patterns/unified/src/extraction_function/index.py",
        "patterns/unified/src/extraction_function/sfn_runtime_handler.py",
        "patterns/unified/src/assessment_function/index.py",
    )
    for relative in handlers:
        source = (repo_root / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        # The name has to be BOUND as well as called: a handler whose import went away
        # still reads correctly here and raises ``NameError`` on every invocation.
        assert "set_lambda_deadline_epoch" in imported, (
            f"{relative} calls the publisher without importing it"
        )
        # And the call has to be a real statement. A substring check is satisfied by a
        # commented-out call, which is exactly how a publisher would stop publishing.
        published = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "set_lambda_deadline_epoch"
        ]
        assert published, f"{relative} does not actually call the publisher"
        assert any(
            isinstance(arg, ast.Name) and arg.id == "deadline_epoch"
            for call in published
            for arg in call.args
        ), f"{relative} publishes something other than the deadline it computed"
        assert "get_remaining_time_in_millis" in source, relative


# ---------------------------------------------------------------------------
# The LambdaHook ladder: the same gate, on the most expensive attempt here
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_lambda_hook_ladder_will_not_start_a_second_840_second_wait(
    fake_clock: FakeClock, recorded_metrics
):
    """``lambda_client`` waits 840 s for one hook, so two exceed a 900 s caller.

    And every attempt re-runs whatever the hook charges for, which is why this ladder
    gets the gate even though nobody reported it: the cost of an extra attempt here is
    paid work, not just time. The recursive call on this branch sits inside the
    method's own ``try``, so the refusal is raised as the ``RuntimeError`` the
    exhausted path raises — the one exception this method does not retry.
    """
    client = BedrockClient(region="us-east-1")
    client._lambda_client = MagicMock()

    def _stall(**_kwargs):
        fake_clock.advance(840.0)
        raise ReadTimeoutError(endpoint_url="https://lambda.local")

    client._lambda_client.invoke.side_effect = _stall
    set_lambda_deadline_epoch(fake_clock.now + LAMBDA_MAX_TIMEOUT_SECONDS)

    with pytest.raises(ReadTimeoutError):
        client._invoke_lambda_hook_with_retry(
            lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:hook",
            lambda_payload={"messages": []},
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=fake_clock.now,
        )

    assert client._lambda_client.invoke.call_count == 1
    assert "LambdaHookRetryAbandonedOutOfTime" in recorded_metrics()


@pytest.mark.unit
def test_the_lambda_hook_ladder_still_retries_a_quick_failure(
    fake_clock: FakeClock, recorded_metrics
):
    """A hook that fails instantly is retried the full number of times."""
    client = BedrockClient(region="us-east-1")
    client._lambda_client = MagicMock()
    client._lambda_client.invoke.side_effect = [
        ConnectTimeoutError(endpoint_url="https://lambda.local")
    ] * (DEFAULT_MAX_RETRIES + 1)
    set_lambda_deadline_epoch(fake_clock.now + LAMBDA_MAX_TIMEOUT_SECONDS)

    with pytest.raises(ConnectTimeoutError):
        client._invoke_lambda_hook_with_retry(
            lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:hook",
            lambda_payload={"messages": []},
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=fake_clock.now,
        )

    assert client._lambda_client.invoke.call_count == DEFAULT_MAX_RETRIES + 1
    assert "LambdaHookRetryAbandonedOutOfTime" not in recorded_metrics()


# ---------------------------------------------------------------------------
# The embedding ladder
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_embedding_ladder_sleeps_inside_its_invocation(fake_clock: FakeClock):
    """Embedding attempts are cheap, so the sleep is what bites: 7 retries x 300 s."""
    client = BedrockClient(region="us-east-1")
    client._client = MagicMock()
    client._client.invoke_model.side_effect = [
        ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "IM")
    ] * (DEFAULT_MAX_RETRIES + 1)
    deadline = fake_clock.now + 100.0
    set_lambda_deadline_epoch(deadline)

    with pytest.raises(ClientError):
        client._generate_embedding_with_retry(
            model_id="amazon.titan-embed-text-v2:0",
            request_body=json.dumps({"inputText": "x"}),
            normalized_text="x",
            retry_count=4,  # 32s nominal, then 64s which does not fit
            max_retries=DEFAULT_MAX_RETRIES,
        )

    assert fake_clock.now <= deadline, "the embedding ladder outlived its invocation"


# ---------------------------------------------------------------------------
# The OpenAI Responses ladder: the same gate over a REST endpoint
# ---------------------------------------------------------------------------


class _HttpResponse:
    """The two attributes the Responses ladder reads off a botocore HTTP response."""

    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _mantle_ladder(
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    outcome,
):
    """Drive ``invoke_responses_api`` with ``_sign_and_send`` replaced.

    ``oar.time`` is patched as well as the two the fixture covers: this module reads
    the clock itself, and leaving it real would measure a 300 s stall as zero.
    """
    from idp_common.bedrock import openai_responses as oar

    monkeypatch.setattr(oar, "time", fake_clock)
    sent = MagicMock(side_effect=outcome)
    monkeypatch.setattr(oar, "_sign_and_send", sent)
    client = BedrockClient(region="us-east-1")
    set_lambda_deadline_epoch(fake_clock.now + LAMBDA_MAX_TIMEOUT_SECONDS)
    return oar, client, sent


@pytest.mark.unit
def test_the_mantle_ladder_stops_before_an_attempt_it_cannot_hold(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch, recorded_metrics
):
    """A throttled GPT-5.x request that takes a read timeout to be refused.

    This ladder reuses the client's backoff and metric helpers, which is how it came
    to share the defect; it shares the fix the same way. Two 300 s attempts leave
    under 300 s, so the third is not made.
    """

    def _slow_429(*_args, **_kwargs):
        fake_clock.advance(CONFIDENCE_READ_TIMEOUT_SECONDS)
        return _HttpResponse(429, "Too Many Requests")

    oar, client, sent = _mantle_ladder(fake_clock, monkeypatch, _slow_429)

    with pytest.raises(RuntimeError, match="bedrock-mantle request failed"):
        oar.invoke_responses_api(
            client=client,
            model_id="openai.gpt-5.4",
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=100,
            max_retries=DEFAULT_MAX_RETRIES,
            context="Extraction",
        )

    assert sent.call_count == 2, sent.call_count
    names = recorded_metrics()
    assert names.count("BedrockRetryAbandonedOutOfTime") == 1, names
    # The ladder's ordinary failure counter too: that is what the dashboards and the
    # BedrockServiceOutageAlarm family count, so a refusal that skipped it would make
    # the failure rate fall by exactly the number of documents the gate saved.
    assert names.count("BedrockRequestsFailed") == 1, names


@pytest.mark.unit
def test_the_mantle_refusal_keeps_the_cause_that_makes_it_transient(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch, recorded_metrics
):
    """``raise ... from e`` is load-bearing, not decoration.

    This ladder wraps every failure in a ``RuntimeError``, which carries no error code
    of its own, and ``is_transient_error`` follows ``__cause__`` and never
    ``__context__``. Without the explicit chain the refusal would be classified
    deterministic and retried once — strictly worse than the wall-clock death it
    replaced, which is the shape of mistake worth a test of its own.
    """
    from idp_common.utils.transient_errors import is_transient_error

    def _slow_timeout(*_args, **_kwargs):
        fake_clock.advance(CONFIDENCE_READ_TIMEOUT_SECONDS)
        raise ReadTimeoutError(endpoint_url="https://mantle.local")

    oar, client, sent = _mantle_ladder(fake_clock, monkeypatch, _slow_timeout)

    with pytest.raises(RuntimeError) as caught:
        oar.invoke_responses_api(
            client=client,
            model_id="openai.gpt-5.4",
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=100,
            max_retries=DEFAULT_MAX_RETRIES,
            context="Extraction",
        )

    assert sent.call_count == 2, sent.call_count
    names = recorded_metrics()
    assert names.count("BedrockRetryAbandonedOutOfTime") == 1, names
    assert names.count("BedrockRequestsFailed") == 1, names
    assert isinstance(caught.value.__cause__, ReadTimeoutError)
    assert is_transient_error(caught.value), (
        "the refusal is not classifiable as transient, so the state machine would "
        "not retry it"
    )


# ---------------------------------------------------------------------------
# The gate must survive the thread boundary, because the ladders cross one
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_gate_fires_inside_a_thread_pool_worker(monkeypatch: pytest.MonkeyPatch):
    """A fan-out worker gets the same verdict as the calling thread.

    This is where the gate was inert, and it is the case worth a behavioural test
    rather than an accessor test: two of this library's Bedrock fan-outs are raw
    ``ThreadPoolExecutor``s — ``assessment.batching`` maps concurrent confidence
    batches over one, and ``generate_embeddings_batch`` submits to one — so a
    deadline visible only in the calling context would leave every retry in a
    concurrent confidence batch unbounded, on exactly the 300-second request this
    whole change is about.

    Driven through ``generate_embeddings_batch`` because that pool is in the module
    under change. A real clock and a real (tiny) stall are used here instead of the
    shared ``FakeClock``: the fixture's clock is mutated by whichever thread runs,
    and a racing counter would make the assertion depend on the scheduler.

    ⚠️ **The stall has to be held before the backoff is stubbed out, and the two
    cannot both go through ``time.sleep``.** ``client_mod.time`` is not a per-module
    alias — it *is* the stdlib ``time`` module — so stubbing ``sleep`` on it replaces
    the function process-wide, including the one this stall calls. With the stall a
    no-op, the only real time an attempt spent was the latency of three CloudWatch
    ``PutMetricData`` round trips that nothing here mocks, and those were what carried
    the elapsed cost past the deadline. The assertion held, for a reason this
    docstring denies and no reader would look for; it inverted the moment an autouse
    refusal made an unmocked AWS call fail in-process instead of over a socket
    (#1398). ``_stall`` keeps the real function, so the cost the comment claims is the
    cost the gate measures.
    """
    client = BedrockClient(region="us-east-1")
    client._client = MagicMock()
    seen: list[float | None] = []
    _stall = time.sleep

    def _slow_throttle(**_kwargs):
        seen.append(lambda_deadline.get_lambda_deadline_epoch())
        _stall(0.05)
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "InvokeModel",
        )

    client._client.invoke_model.side_effect = _slow_throttle
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)
    # 0.01s left, against attempts that measurably cost ~0.05s: every retry after the
    # first is refused, so one attempt per item and no more.
    set_lambda_deadline_epoch(time.time() + 0.01)
    try:
        results = client.generate_embeddings_batch(
            items=[{"text": "a"}, {"text": "b"}, {"text": "c"}],
            model_id="amazon.titan-embed-text-v2:0",
            max_concurrent=3,
        )
    finally:
        set_lambda_deadline_epoch(None)

    assert results == [None, None, None]
    assert seen and all(d is not None for d in seen), (
        "a pool worker could not see the invocation deadline, so the gate is inert "
        "in every concurrent Bedrock fan-out in this library"
    )
    assert client._client.invoke_model.call_count == 3, (
        "each item retried past the deadline: "
        f"{client._client.invoke_model.call_count} calls for 3 items"
    )


# ---------------------------------------------------------------------------
# The costs are not monotonic, and the gate must not forget
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_cheap_failure_does_not_re_license_an_expensive_attempt(
    fake_clock: FakeClock, recorded_metrics
):
    """The schedule that overran a 900 s invocation by 192 s while every gate said yes.

    Comparing against the attempt that *just* failed is the obvious reading of "an
    attempt the size of the last one" and it is wrong, because a ladder's costs are
    not monotonic. Driving the real ladder with
    ``[290 s, 290 s, 150 s, 0.1 s, 0.1 s, 300 s]`` — all six retryable, the two cheap
    ones ordinary throttles — the two cheap failures reset the comparison to nothing,
    the sixth attempt began at T=792 s and the invocation was killed 192 s past the
    wall. The gate is asked about the costliest attempt measured instead, so this
    ladder stops while there is still time to return.
    """
    schedule = [290.0, 290.0, 150.0, 0.1, 0.1, 300.0]
    throttle = ClientError(
        {"Error": {"Code": "InternalServerException", "Message": "server"}}, "Converse"
    )

    def _by_schedule(**_kwargs):
        index = client._client.converse.call_count - 1
        fake_clock.advance(schedule[min(index, len(schedule) - 1)])
        raise throttle

    client = _client_with(_by_schedule)
    started = fake_clock.now
    set_lambda_deadline_epoch(started + LAMBDA_MAX_TIMEOUT_SECONDS)

    with pytest.raises(ClientError):
        client._invoke_with_retry(
            model_id="m",
            converse_params=_converse_params(),
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=started,
        )

    spent = fake_clock.now - started
    assert spent <= LAMBDA_MAX_TIMEOUT_SECONDS, (
        f"the ladder ran {spent:.1f}s into a {LAMBDA_MAX_TIMEOUT_SECONDS}s invocation"
    )
    assert client._client.converse.call_count == 3, client._client.converse.call_count
    assert "BedrockRetryAbandonedOutOfTime" in recorded_metrics()


@pytest.mark.unit
def test_the_high_water_mark_survives_the_recursion(fake_clock: FakeClock):
    """Specifically that the figure is threaded, not recomputed per frame.

    ``_invoke_with_retry`` is recursive, so the costliest attempt has to be carried
    into the next frame as an argument. A version that recomputed it from the current
    frame alone would pass the test above by accident whenever the expensive attempts
    happen to be adjacent; here the expensive one is first and is followed only by
    cheap failures, so nothing but a threaded figure can refuse.
    """
    schedule = [400.0, 0.1, 0.1, 0.1, 0.1, 0.1]
    throttle = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "Converse"
    )

    def _by_schedule(**_kwargs):
        index = client._client.converse.call_count - 1
        fake_clock.advance(schedule[min(index, len(schedule) - 1)])
        raise throttle

    client = _client_with(_by_schedule)
    started = fake_clock.now
    # 600s budget: after the 400s attempt, 200s remain and no further attempt may be
    # assumed cheaper than the one already measured.
    set_lambda_deadline_epoch(started + 600.0)

    with pytest.raises(ClientError):
        client._invoke_with_retry(
            model_id="m",
            converse_params=_converse_params(),
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=started,
        )

    assert client._client.converse.call_count == 1, client._client.converse.call_count


@pytest.mark.unit
def test_the_streamed_decorators_use_the_same_high_water_rule(fake_clock: FakeClock):
    """The agentic ladder had the identical hole, so it got the identical fix.

    Asserted on the decorator rather than on the client, because this is the other
    half of "one rule, one implementation": a high-water mark in ``bedrock/client.py``
    and a last-attempt comparison in ``utils.bedrock_utils`` would be two gates with
    the same name and different behaviour.

    ⚠️ The schedule is the load-bearing part, and an obvious one does not work. The
    two rules can only disagree where the costliest attempt is NOT the most recent
    one *and* the slack is smaller than that cost — with an expensive attempt last,
    both rules refuse identically, which is why a ``[400, 0.1, …]`` schedule left
    every mutation of this line alive. ``[300, 1, …]`` against a 601-second budget
    leaves 301 s after the first attempt (enough, under either rule) and 300 s after
    the second, which only the high-water rule reads as too little.
    """
    from idp_common.utils.bedrock_utils import exponential_backoff_retry

    calls: list[int] = []
    schedule = [300.0, 1.0, 1.0, 1.0, 1.0]

    @exponential_backoff_retry(max_retries=5, initial_delay=0.1, max_delay=0.1)
    def _flaky():
        fake_clock.advance(schedule[min(len(calls), len(schedule) - 1)])
        calls.append(1)
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "Converse"
        )

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr("idp_common.utils.bedrock_utils.time", fake_clock)
        set_lambda_deadline_epoch(fake_clock.now + 601.0)
        with pytest.raises(ClientError):
            _flaky()
    finally:
        monkey.undo()

    assert len(calls) == 2, len(calls)


# ---------------------------------------------------------------------------
# The remaining two claimed observables
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_refusal_writes_a_log_record_naming_both_numbers(
    fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
):
    """The third claimed observable, asserted rather than assumed.

    A refusal is distinguishable three ways — the attempt count, the metric, and this
    record — and the log line is the one an operator reads first. It was claimed and
    untested: deleting it left the suite green.
    """
    client = _client_with(
        _stalling_converse(fake_clock, CONFIDENCE_READ_TIMEOUT_SECONDS, _READ_TIMEOUT)
    )
    set_lambda_deadline_epoch(fake_clock.now + 400.0)
    with caplog.at_level("WARNING", logger="idp_common.lambda_deadline"):
        with pytest.raises(ReadTimeoutError):
            client._invoke_with_retry(
                model_id="m",
                converse_params=_converse_params(),
                retry_count=0,
                max_retries=DEFAULT_MAX_RETRIES,
                request_start_time=fake_clock.now,
            )
    records = [r.getMessage() for r in caplog.records if "Stopping" in r.getMessage()]
    assert len(records) == 1, caplog.text
    # 302 rather than 300: the budgeted cost is the 300 s attempt plus the 2 s backoff
    # that would precede it, so this also pins that the log and the gate agree.
    assert "302s" in records[0] and "100s" in records[0], records[0]


@pytest.mark.unit
def test_a_refusal_counts_one_failed_request_not_two(
    fake_clock: FakeClock, recorded_metrics
):
    """One refusal, one failed request.

    The LambdaHook ``FunctionError`` branch raises inside its own ``try``, so the
    method's ``except Exception`` catches the refusal on the way out and emits the
    ladder's failure counter there. Emitting it at the gate as well counted one failed
    request twice, which is the opposite of the reason the counter is emitted at all.
    """
    client = BedrockClient(region="us-east-1")
    client._lambda_client = MagicMock()
    payload = MagicMock()
    payload.read.return_value = b'{"errorMessage": "hook blew up"}'

    def _slow_unhandled(**_kwargs):
        fake_clock.advance(840.0)
        return {"FunctionError": "Unhandled", "Payload": payload}

    client._lambda_client.invoke.side_effect = _slow_unhandled
    set_lambda_deadline_epoch(fake_clock.now + LAMBDA_MAX_TIMEOUT_SECONDS)

    with pytest.raises(RuntimeError, match="LambdaHook function error"):
        client._invoke_lambda_hook_with_retry(
            lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:hook",
            lambda_payload={"messages": []},
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=fake_clock.now,
        )

    names = recorded_metrics()
    assert client._lambda_client.invoke.call_count == 1
    assert names.count("LambdaHookRetryAbandonedOutOfTime") == 1, names
    assert names.count("LambdaHookRequestsFailed") == 1, names


# ---------------------------------------------------------------------------
# The residual the process-wide fallback carries
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_stale_deadline_ends_a_ladder_early_rather_than_late(fake_clock: FakeClock):
    """The one way the fallback can be wrong, pinned so it is a known quantity.

    The process-wide value is what makes the gate visible in a thread-pool worker, and
    its cost is staleness: a value an ended invocation left behind would hold that
    invocation's deadline. With that deadline in the past, ``remaining`` is negative
    and every cost is refused — so a ladder of cheap throttles is cut from eight
    attempts to one, which contradicts "an attempt that costs nothing always fits" in
    every case except the one the docstring now names.

    It is the survivable direction: the ladder raises a transient error immediately
    instead of silently doing nothing, and ``lambda_deadline_scope``, which every
    publishing handler runs its invocation inside, is what keeps it unreachable.
    Asserted here so that if the direction ever reverses, this fails rather than an
    operator discovering it.
    """
    client = _client_with(
        [ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "C")]
        * (DEFAULT_MAX_RETRIES + 1)
    )
    set_lambda_deadline_epoch(fake_clock.now - 60.0)  # a previous invocation's
    with pytest.raises(ClientError):
        client._invoke_with_retry(
            model_id="m",
            converse_params=_converse_params(),
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=fake_clock.now,
        )
    assert client._client.converse.call_count == 1
    assert fake_clock.sleeps == [], "it also slept, which is strictly worse"


@pytest.mark.unit
@pytest.mark.parametrize(
    "error_code", _retryable_codes_by_ladder()["_generate_embedding_with_retry"]
)
def test_the_embedding_ladder_keeps_retrying_a_cheap_failure(
    fake_clock: FakeClock, error_code: str
):
    """Its own vocabulary, which is not the ``converse`` ladder's.

    ``ReadTimeout`` and ``TimeoutError`` are retryable here and are not names
    ``_invoke_with_retry`` recognises, which is why the lists are read per ladder.
    """
    client = BedrockClient(region="us-east-1")
    client._client = MagicMock()
    client._client.invoke_model.side_effect = [
        ClientError({"Error": {"Code": error_code, "Message": "x"}}, "InvokeModel")
    ] * (DEFAULT_MAX_RETRIES + 1)
    set_lambda_deadline_epoch(fake_clock.now + 5.0)

    with pytest.raises(ClientError):
        client._generate_embedding_with_retry(
            model_id="amazon.titan-embed-text-v2:0",
            request_body=json.dumps({"inputText": "x"}),
            normalized_text="x",
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
        )
    assert client._client.invoke_model.call_count == DEFAULT_MAX_RETRIES + 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "error_code", _retryable_codes_by_ladder()["_invoke_lambda_hook_with_retry"]
)
def test_the_lambda_hook_ladder_keeps_retrying_a_cheap_failure(
    fake_clock: FakeClock, error_code: str
):
    """Same rule, third vocabulary. These codes are Lambda's, not Bedrock's."""
    client = BedrockClient(region="us-east-1")
    client._lambda_client = MagicMock()
    client._lambda_client.invoke.side_effect = [
        ClientError({"Error": {"Code": error_code, "Message": "x"}}, "Invoke")
    ] * (DEFAULT_MAX_RETRIES + 1)
    set_lambda_deadline_epoch(fake_clock.now + 5.0)

    with pytest.raises(ClientError):
        client._invoke_lambda_hook_with_retry(
            lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:hook",
            lambda_payload={"messages": []},
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=fake_clock.now,
        )
    assert client._lambda_client.invoke.call_count == DEFAULT_MAX_RETRIES + 1


@pytest.mark.unit
def test_the_mantle_ladder_also_remembers_its_costliest_attempt(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
):
    """Same discriminating schedule, third ladder.

    A uniform schedule cannot tell the two rules apart anywhere, so without this the
    high-water term in ``openai_responses`` was asserted structurally and by nothing
    else: a 300 s attempt, then a 1 s one, against a 603 s budget.
    """
    schedule = [300.0, 1.0, 1.0, 1.0]

    def _by_schedule(*_args, **_kwargs):
        fake_clock.advance(schedule[min(sent.call_count - 1, len(schedule) - 1)])
        return _HttpResponse(429, "Too Many Requests")

    from idp_common.bedrock import openai_responses as oar

    monkeypatch.setattr(oar, "time", fake_clock)
    sent = MagicMock(side_effect=_by_schedule)
    monkeypatch.setattr(oar, "_sign_and_send", sent)
    client = BedrockClient(region="us-east-1")
    # 603 rather than 601: this ladder's first backoff is 2 s where the decorator's is
    # 0.1 s, and the discriminating window is only as wide as the backoff.
    set_lambda_deadline_epoch(fake_clock.now + 603.0)

    with pytest.raises(RuntimeError, match="bedrock-mantle request failed"):
        oar.invoke_responses_api(
            client=client,
            model_id="openai.gpt-5.4",
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=100,
            max_retries=DEFAULT_MAX_RETRIES,
            context="Extraction",
        )

    assert sent.call_count == 2, sent.call_count


@pytest.mark.unit
def test_the_mantle_timeout_branch_remembers_too(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
):
    """The other of that ladder's two retry decisions.

    Its two branches compute the high-water mark separately — one from the response's
    measured duration, one from the clock — so a test of either leaves the other
    asserted only structurally.
    """
    schedule = [300.0, 1.0, 1.0, 1.0]

    def _by_schedule(*_args, **_kwargs):
        fake_clock.advance(schedule[min(sent.call_count - 1, len(schedule) - 1)])
        raise ReadTimeoutError(endpoint_url="https://mantle.local")

    from idp_common.bedrock import openai_responses as oar

    monkeypatch.setattr(oar, "time", fake_clock)
    sent = MagicMock(side_effect=_by_schedule)
    monkeypatch.setattr(oar, "_sign_and_send", sent)
    client = BedrockClient(region="us-east-1")
    set_lambda_deadline_epoch(fake_clock.now + 603.0)

    with pytest.raises(RuntimeError):
        oar.invoke_responses_api(
            client=client,
            model_id="openai.gpt-5.4",
            system_prompt="sys",
            content=[{"text": "hi"}],
            max_tokens=100,
            max_retries=DEFAULT_MAX_RETRIES,
            context="Extraction",
        )

    assert sent.call_count == 2, sent.call_count


@pytest.mark.unit
@pytest.mark.parametrize("branch", ["client-error", "wrapped"])
def test_the_async_decorator_remembers_its_costliest_attempt(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch, branch: str
):
    """Both of the async decorator's retry decisions, which is the streamed path.

    The async decorator judges a ``ClientError`` by its code and anything else by
    substring, and each branch computes the high-water mark on its own line. The
    streamed agentic ladder goes through this decorator, so this is where the #1310
    fix lives and where the same hole was.
    """
    from idp_common.utils import bedrock_utils

    calls: list[int] = []
    schedule = [300.0, 1.0, 1.0, 1.0, 1.0]

    if branch == "client-error":
        error: Exception = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "Converse"
        )
    else:
        # Not a ClientError, so it takes the generic branch and is judged by text.
        error = RuntimeError("ThrottlingException: slow down")

    @bedrock_utils.async_exponential_backoff_retry(
        max_retries=5, initial_delay=0.1, max_delay=0.1
    )
    async def _flaky():
        fake_clock.advance(schedule[min(len(calls), len(schedule) - 1)])
        calls.append(1)
        raise error

    async def _fake_sleep(seconds):
        fake_clock.advance(seconds)

    monkeypatch.setattr(bedrock_utils, "time", fake_clock)
    monkeypatch.setattr(bedrock_utils.asyncio, "sleep", _fake_sleep)
    set_lambda_deadline_epoch(fake_clock.now + 601.0)
    with pytest.raises(type(error)):
        asyncio.run(_flaky())

    assert len(calls) == 2, len(calls)


@pytest.mark.unit
def test_publishing_writes_both_stores_and_the_scoped_one_is_read_first():
    """White-box on purpose: the two stores *are* the mechanism.

    ``set_lambda_deadline_epoch`` writes a ContextVar and a process-wide value, and
    ``get_lambda_deadline_epoch`` prefers the ContextVar. No behavioural test can tell
    which store answered — that is the whole point of the fallback — so dropping
    either write is invisible from outside, and the ContextVar write in particular
    became unobservable the moment the fallback existed. What it buys is the documented
    guarantee that a deadline scoped to one async context wins over the process-wide
    one, which is asserted here and nowhere else.
    """
    import contextvars

    set_lambda_deadline_epoch(1000.0)
    assert lambda_deadline._LAMBDA_DEADLINE_EPOCH.get() == 1000.0
    assert lambda_deadline._FALLBACK_DEADLINE_EPOCH == 1000.0

    def _scoped() -> float | None:
        lambda_deadline._LAMBDA_DEADLINE_EPOCH.set(2000.0)
        return lambda_deadline.get_lambda_deadline_epoch()

    assert contextvars.copy_context().run(_scoped) == 2000.0
    assert lambda_deadline.get_lambda_deadline_epoch() == 1000.0, (
        "a deadline scoped to one context escaped into the process-wide value"
    )


def _deadline_a_worker_reads() -> float | None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda_deadline.get_lambda_deadline_epoch).result()


@pytest.mark.unit
@pytest.mark.parametrize("ends_by", ["return", "raise"])
def test_a_scope_takes_back_the_deadline_published_inside_it(ends_by: str):
    """What a handler owes whatever runs after it in the same process.

    Both stores are read afterwards, white-box and from a pool worker, because either
    one left behind is the leak: the ContextVar is what the next caller on this thread
    reads first, and the process-wide value is what every thread falls back to. A
    raise must take it back as surely as a return, since a failed invocation is
    followed by the next one exactly as a successful one is.
    """
    published = time.time() + 300.0

    def _invocation() -> None:
        with lambda_deadline.lambda_deadline_scope():
            set_lambda_deadline_epoch(published)
            assert lambda_deadline.get_lambda_deadline_epoch() == published
            assert _deadline_a_worker_reads() == published
            if ends_by == "raise":
                raise RuntimeError("the invocation failed")

    if ends_by == "raise":
        with pytest.raises(RuntimeError, match="the invocation failed"):
            _invocation()
    else:
        _invocation()

    assert lambda_deadline._LAMBDA_DEADLINE_EPOCH.get() is None
    assert lambda_deadline._FALLBACK_DEADLINE_EPOCH is None
    assert lambda_deadline.get_lambda_deadline_epoch() is None
    assert _deadline_a_worker_reads() is None


@pytest.mark.unit
def test_a_scope_restores_the_deadline_it_found_rather_than_clearing_it():
    """Restoring is what makes a scope safe to nest and safe to enter under a caller
    that already holds a deadline: a test that publishes one and then drives a handler
    gets its own back, which clearing would not give it."""
    outer = time.time() + 900.0
    inner = time.time() + 300.0
    set_lambda_deadline_epoch(outer)

    with lambda_deadline.lambda_deadline_scope():
        with lambda_deadline.lambda_deadline_scope():
            set_lambda_deadline_epoch(inner)
            assert lambda_deadline.get_lambda_deadline_epoch() == inner
        assert lambda_deadline.get_lambda_deadline_epoch() == outer
        assert _deadline_a_worker_reads() == outer

    assert lambda_deadline._LAMBDA_DEADLINE_EPOCH.get() == outer
    assert lambda_deadline._FALLBACK_DEADLINE_EPOCH == outer


@pytest.mark.unit
def test_the_deadline_reads_as_unknown_before_anything_publishes_one():
    """In a fresh interpreter, with nothing published, the answer is None.

    Run in a subprocess because by the time any test in this file executes, the
    autouse fixture has already published ``None`` and bound the module-level name —
    so the initialiser that keeps ``get_lambda_deadline_epoch`` from raising
    ``NameError`` on a tree that never publishes is unobservable in-process. Every
    Lambda that reaches this client without a publishing handler is in exactly that
    state, and there are more of those than there are publishers.
    """
    import subprocess

    repo_root = Path(client_mod.__file__).resolve().parents[4]
    probe = (
        "from idp_common.lambda_deadline import ("
        "get_lambda_deadline_epoch, clamp_sleep_to_budgets, attempt_cannot_finish)\n"
        "assert get_lambda_deadline_epoch() is None\n"
        "assert clamp_sleep_to_budgets(1800) == 1800\n"
        "assert attempt_cannot_finish(100000.0, 'probe') is False\n"
        "print('UNBOUNDED')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(repo_root / "lib" / "idp_common_pkg"),
            "AWS_DEFAULT_REGION": "us-east-1",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UNBOUNDED" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# The other direction: a ladder that mentions neither helper is still a ladder
# ---------------------------------------------------------------------------

#: Functions in ``idp_common`` that both sleep and invoke a Bedrock call, and are
#: deliberately NOT bounded by the invocation budget. One entry, one function, one
#: reason — the registry's rule, because a reason attached to a directory answers for
#: files it is false of.
BUDGET_UNBOUNDED_BEDROCK_LADDERS = {
    "rule_validation/z3/rule_translator.py::_invoke_bedrock": (
        "Its client is built with no botocore Config at all, so it carries the default "
        "60 s read timeout and the default retry mode rather than this solution's: "
        "nominally 2 application attempts x 5 botocore attempts x 60 s = 601 s, inside "
        "a ConfigurationResolverFunction capped at 120 s. Bounding it needs a read "
        "timeout chosen for the work, which is a product decision rather than a "
        "mechanical edit, and its callers publish no deadline, so the gate would be "
        "inert there today. Recorded as a known unbounded ladder rather than fixed "
        "quietly or left unnoticed (#1362 review)."
    ),
}


@pytest.mark.unit
def test_every_sleeping_bedrock_caller_is_enforced_or_registered():
    """Universe closure from the other side, which is what the name test cannot do.

    :func:`_ladder_sources` selects modules by mentioning a gate name, so a ladder that
    mentions neither is invisible to every closure above — and one exists. The universe
    here is derived differently and from the whole package: a function that both sleeps
    and invokes a Bedrock call. Every member is either in a module the closures cover,
    or named in :data:`BUDGET_UNBOUNDED_BEDROCK_LADDERS` with its own reason. A new
    ladder is in neither and fails here.

    The two derivations are deliberately not the same shape. A closure whose universe
    comes from the same predicate as its enforcement cannot see what the predicate
    misses, which is how "derived" ended up meaning "the two files the list named".
    """
    invocations = {
        "converse",
        "converse_stream",
        "invoke_model",
        "invoke_model_with_response_stream",
        "invoke_async",
    }
    covered = {path.name for path in _ladder_sources()}
    universe: dict[str, str] = {}
    for path in sorted(IDP_COMMON_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            callees = {
                _callee_name(node)
                for node in ast.walk(fn)
                if isinstance(node, ast.Call)
            }
            if "sleep" in callees and callees & invocations:
                key = f"{path.relative_to(IDP_COMMON_ROOT)}::{fn.name}"
                universe[key] = path.name
    assert universe, "no sleeping Bedrock caller found — the derivation is broken"

    unaccounted = {
        key
        for key, filename in universe.items()
        if filename not in covered and key not in BUDGET_UNBOUNDED_BEDROCK_LADDERS
    }
    assert not unaccounted, (
        f"these functions sleep between Bedrock calls and are in neither the enforced "
        f"set nor BUDGET_UNBOUNDED_BEDROCK_LADDERS: {sorted(unaccounted)}. Either give "
        "the ladder the budget gate, or register it with the reason it does not get one."
    )
    # And the register may not go stale: an entry naming a function that no longer
    # sleeps between Bedrock calls is pre-excusing whatever next occupies that name.
    stale = set(BUDGET_UNBOUNDED_BEDROCK_LADDERS) - set(universe)
    assert not stale, (
        f"BUDGET_UNBOUNDED_BEDROCK_LADDERS names {sorted(stale)}, which no longer "
        "sleeps between Bedrock calls. Delete the entry."
    )


# ---------------------------------------------------------------------------
# The two gate sites the structural checks were the only cover for
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_embedding_gate_refuses_an_attempt_and_says_so(
    fake_clock: FakeClock, recorded_metrics
):
    """Behavioural cover for the embedding site, which had none.

    Measured: neutering that site's cost, or replacing its ``raise`` with a silent
    ``return None`` — the exact failure the module docstring says this file exists to
    catch — left the whole suite green. The structural checks were the only thing
    holding it, and four separate evasions passed there.

    Embedding attempts are normally cheap, so the schedule has to make one expensive:
    a throttle that takes 300 s to come back is unusual but not impossible, and it is
    the only shape the gate is about.
    """
    client = BedrockClient(region="us-east-1")
    client._client = MagicMock()

    def _slow_throttle(**_kwargs):
        fake_clock.advance(300.0)
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "InvokeModel"
        )

    client._client.invoke_model.side_effect = _slow_throttle
    started = fake_clock.now
    set_lambda_deadline_epoch(started + LAMBDA_MAX_TIMEOUT_SECONDS)

    with pytest.raises(ClientError):
        client._generate_embedding_with_retry(
            model_id="amazon.titan-embed-text-v2:0",
            request_body=json.dumps({"inputText": "x"}),
            normalized_text="x",
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
        )

    assert client._client.invoke_model.call_count == 2, (
        client._client.invoke_model.call_count
    )
    names = recorded_metrics()
    assert names.count("BedrockEmbeddingRetryAbandonedOutOfTime") == 1, names
    assert names.count("BedrockEmbeddingRequestsFailed") == 1, names
    assert "BedrockEmbeddingMaxRetriesExceeded" not in names, names
    assert fake_clock.now - started < LAMBDA_MAX_TIMEOUT_SECONDS


@pytest.mark.unit
def test_the_lambda_hook_throttle_gate_refuses_an_attempt_and_says_so(
    fake_clock: FakeClock, recorded_metrics
):
    """Behavioural cover for the hook's throttle site, which also had none.

    Its sibling branches were covered and it was not, which is the asymmetry a
    structural check cannot report: all three look identical to the AST.
    """
    client = BedrockClient(region="us-east-1")
    client._lambda_client = MagicMock()

    def _slow_throttle(**_kwargs):
        fake_clock.advance(840.0)
        raise ClientError(
            {"Error": {"Code": "TooManyRequestsException", "Message": "slow"}}, "Invoke"
        )

    client._lambda_client.invoke.side_effect = _slow_throttle
    set_lambda_deadline_epoch(fake_clock.now + LAMBDA_MAX_TIMEOUT_SECONDS)

    with pytest.raises(ClientError):
        client._invoke_lambda_hook_with_retry(
            lambda_arn="arn:aws:lambda:us-east-1:123456789012:function:hook",
            lambda_payload={"messages": []},
            retry_count=0,
            max_retries=DEFAULT_MAX_RETRIES,
            request_start_time=fake_clock.now,
        )

    assert client._lambda_client.invoke.call_count == 1
    names = recorded_metrics()
    assert names.count("LambdaHookRetryAbandonedOutOfTime") == 1, names
    assert names.count("LambdaHookRequestsFailed") == 1, names
