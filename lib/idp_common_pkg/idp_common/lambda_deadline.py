# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The current invocation's wall clock, and the two rules every retry ladder owes it.

A retry ladder that does not know when its own Lambda will be killed can schedule a
sleep longer than the whole invocation, and can begin an attempt the invocation
cannot hold. Both have happened here, on different ladders, with the same outcome:
the function dies on the wall clock, Step Functions classifies that as deterministic
(``Sandbox.Timedout`` is ``MaxAttempts: 1`` on every Lambda task since #917), and a
Map state that tolerates no member failure discards the siblings that had already
succeeded. A raised transient error does far better — the handlers wrap one as
``TransientError`` via ``utils.transient_errors.raise_if_transient``, and
``ExtractionStep``/``ShardExtractionStep`` retry that eight times against a
state-machine budget of 21,600 s.

So the rules are:

* :func:`clamp_sleep_to_budgets` — a backoff never sleeps past the end of the
  invocation. It only ever SHORTENS a sleep; it does not convert one into a failure.
* :func:`attempt_cannot_finish` — an attempt the size of the one that just failed is
  not BEGUN when the remaining invocation cannot hold it. This one does end the
  ladder, by telling the caller to raise the underlying error.

**Why these live in a leaf module that imports only the standard library.** Both are
needed by ladders on two sides of an import boundary. ``utils.bedrock_utils`` holds
the decorators the streamed agentic path uses; ``bedrock/client.py`` holds the
non-streamed ``converse``, embedding, LambdaHook and OpenAI Responses ladders. The
second cannot reach the first cheaply: ``utils/__init__`` imports
``idp_common.config.models``, whose package ``__init__`` comes back through
``configuration_manager`` -> ``merge_utils`` -> ``idp_common.bedrock``. Measured,
taking the rule from ``utils.bedrock_utils`` instead of from here moves
``import idp_common.bedrock`` from 526 modules in 0.15 s to 968 in 1.00 s and makes it
require ``idp_common.config`` — a cold-start cost a lean handler pays before its
handler body runs, on one of the two chains
``tests/unit/test_import_surface_region_free.py`` exists to keep thin.
``idp_common.timeout_budget`` is a leaf for the same reason and states the rest of
that history.

One implementation rather than one per ladder is the point. The defect this module
addresses has now been found on five separate ladders in this library, and four of
them were found by looking for the first one's shape rather than by being reported —
see ``tests/unit/bedrock/test_invocation_budget_guard.py``, which derives the set of
ladders it covers instead of listing it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# Absolute epoch seconds by which the current Lambda invocation must finish, as a
# ContextVar so retry ladders can see it without every call site threading it
# through. Set once per invocation from ``context.get_remaining_time_in_millis()``;
# None outside Lambda (local, tests), where every check here is a no-op and
# behaviour is unchanged.
_LAMBDA_DEADLINE_EPOCH: ContextVar[float | None] = ContextVar(
    "idp_lambda_deadline_epoch", default=None
)

# The same value, outside the context. ⚠️ **This is what makes the gate work in a
# thread, and the gate is useless here without it.** A ContextVar survives
# ``asyncio.run``, ``gather``/``create_task`` and ``asyncio.to_thread``, and does NOT
# survive ``ThreadPoolExecutor``/``loop.run_in_executor`` — and this library reaches
# Bedrock from a raw pool on the paths that matter most to this gate:
# ``assessment.batching`` fans concurrent confidence batches out with ``pool.map``,
# and ``bedrock.client.generate_embeddings_batch`` with ``executor.submit``. Both run
# inside the invocation whose deadline this is, both reach ladders that back off and
# retry, and in both the ContextVar reads None in the worker. Relying on every fan-out
# site — present and future — to remember ``contextvars.copy_context().run`` is the
# shape of defect this module exists to close, so the value is readable without it.
#
# The cost is that this is process-wide mutable state, and the one way it can be
# wrong is staleness: a value left behind by an invocation that has ended. That is
# closed by :func:`lambda_deadline_scope`, which every publishing handler runs its
# whole invocation inside and which puts this back, with the ContextVar, when the
# invocation ends — so neither the next warm invocation nor a later test in the same
# process inherits it. A stale value would fail in the safe direction, because a
# deadline already past produces an early transient error the state machine retries
# rather than silent inaction. The ContextVar stays primary so a caller that scopes a
# different deadline to one async context still gets it.
_FALLBACK_DEADLINE_EPOCH: float | None = None

# Seconds of the remaining budget left unspent when clamping a sleep, so a sleep does
# not end exactly at the wall with no room for the attempt that follows it. This is a
# floor on usefulness, not a guarantee: one non-streamed ``converse`` can legitimately
# occupy ``timeout_budget.CONFIDENCE_READ_TIMEOUT_SECONDS``, so no reserve can promise
# the next attempt completes.
DEADLINE_RESERVE_SECONDS = 30.0


def set_lambda_deadline_epoch(deadline_epoch: float | None) -> None:
    """Record when the current Lambda invocation must finish (epoch seconds).

    ⚠️ **The contract is: call this inside :func:`lambda_deadline_scope`, which the
    handler wraps around its whole invocation.** It writes process-wide state as well
    as the ContextVar, and the scope is what takes both back when the invocation ends.
    Called outside one, the value outlives the invocation that published it, and a
    warm container — or a later test in the same process — reads a deadline belonging
    to nothing still running; see the note on ``_FALLBACK_DEADLINE_EPOCH``. Passing
    ``None`` clears both and restores the unbounded behaviour, which is what every
    test teardown here does.

    The value is readable from a ``ThreadPoolExecutor`` worker, which a ContextVar
    alone is not. ``contextvars.copy_context().run`` is therefore no longer needed to
    make the budget gate work in a pool, though it remains the right tool for
    propagating anything else.
    """
    global _FALLBACK_DEADLINE_EPOCH
    _LAMBDA_DEADLINE_EPOCH.set(deadline_epoch)
    _FALLBACK_DEADLINE_EPOCH = deadline_epoch


@contextmanager
def lambda_deadline_scope() -> Iterator[None]:
    """Give back, when the block ends, any deadline published inside it.

    Every handler that calls :func:`set_lambda_deadline_epoch` runs its whole
    invocation inside this. On exit, by return or by raise, both stores hold what
    they held on entry: the ContextVar through the token of a ``set`` made here, the
    process-wide value by assignment. Without it a deadline outlives the invocation
    that published it, and whatever runs next in the same process reads it — the
    next warm invocation until it publishes its own, or a later test in the same
    pytest run. Once that deadline has passed, :func:`attempt_cannot_finish` refuses
    every attempt, so a retry ladder there gives up after its first failure.

    Restored rather than cleared, so a scope nested inside another, or a caller with
    a deadline of its own in force, gets that deadline back.
    """
    global _FALLBACK_DEADLINE_EPOCH
    previous_fallback = _FALLBACK_DEADLINE_EPOCH
    token = _LAMBDA_DEADLINE_EPOCH.set(_LAMBDA_DEADLINE_EPOCH.get())
    try:
        yield
    finally:
        _LAMBDA_DEADLINE_EPOCH.reset(token)
        _FALLBACK_DEADLINE_EPOCH = previous_fallback


def get_lambda_deadline_epoch() -> float | None:
    """The current invocation's deadline, or None when unknown.

    The ContextVar first, so a deadline scoped to one async context wins, then the
    process-wide value, which is what a thread-pool worker sees.
    """
    scoped = _LAMBDA_DEADLINE_EPOCH.get()
    if scoped is not None:
        return scoped
    return _FALLBACK_DEADLINE_EPOCH


def remaining_invocation_seconds() -> float | None:
    """Seconds left in this invocation, or None when the deadline is unknown.

    May be negative, which means the function is already past the deadline it was
    given. Callers log it rather than branching on the sign: a ladder that has run
    past the wall is about to be killed either way, and the useful thing is that the
    log says so.
    """
    deadline = get_lambda_deadline_epoch()
    if deadline is None:
        return None
    return deadline - time.time()


def clamp_sleep_to_budgets(
    sleep_time: float,
    total_slept: float = 0.0,
    max_total_delay: float | None = None,
    reserve: float | None = None,
) -> float:
    """Shorten ``sleep_time`` to fit the cumulative and wall-clock budgets.

    Returns the seconds to actually sleep, never negative and never longer than
    requested. Both bounds only ever SHORTEN a sleep:

    - **cumulative** — ``max_total_delay`` caps total time spent asleep across all
      attempts, because a per-sleep cap alone still permits 50 x 60s.
    - **wall-clock** — what remains of this Lambda invocation, minus ``reserve``.

    A return of 0.0 means "no time left to back off". Nothing here raises; what the
    caller does with it depends on WHICH bound produced it, and
    ``utils.bedrock_utils._backoff_or_none`` is where that is decided for the
    decorators — a spent cumulative allowance ends the ladder, a tight deadline keeps
    trying with no sleep.
    """
    if reserve is None:
        reserve = DEADLINE_RESERVE_SECONDS
    allowed = sleep_time
    if max_total_delay is not None:
        allowed = min(allowed, max_total_delay - total_slept)
    deadline = get_lambda_deadline_epoch()
    if deadline is not None:
        allowed = min(allowed, deadline - time.time() - reserve)
    return max(0.0, min(sleep_time, allowed))


def attempt_cannot_finish(
    attempt_cost_seconds: float | None,
    func_name: str,
) -> bool:
    """Whether an attempt costing ``attempt_cost_seconds`` cannot finish in time.

    ⚠️ **Callers pass the WORST attempt this ladder has measured, not the last one**,
    plus any backoff they are about to sleep. Passing the last one is the obvious
    reading and it is wrong, because a ladder's costs are not monotonic: measured on
    the real ``converse`` ladder with an injected clock, the schedule
    ``[290 s, 290 s, 150 s, 0.1 s, 0.1 s, 300 s]`` — all six retryable, the two cheap
    ones ordinary throttles — has every gate decision answer "allowed", begins its
    sixth attempt at T=792 s of a 900 s invocation and runs 192 s past the wall. Two
    cheap failures had reset the comparison to nothing. A high-water mark is what makes
    the gate's claim hold across a mixed schedule, and it costs a ladder of
    *consistently* cheap attempts nothing at all, which is the case the wall-clock
    bound deliberately keeps retrying.

    The cost is measured rather than taken from a constant, which is what makes this
    self-calibrating: the caller does not have to tell this module how long a request
    may take, and the figure covers whatever an attempt actually spends — a stalled
    stream waiting out its read timeout, the non-streamed ``converse`` that bounds a
    whole response, a synchronous LambdaHook invocation that may run for 840 s, or all
    of them inside one invocation.

    What it prevents is the one shape that cannot end well. An attempt that has just
    spent 300 seconds stalling, started again with 289 seconds of the invocation
    left, cannot succeed and cannot even fail cleanly: the Lambda is killed first,
    Step Functions reports ``Sandbox.Timedout``, and since #917 that is
    ``MaxAttempts: 1`` on every Lambda task — so the blip a retry would have cleared
    becomes the failure that is NOT retried, and a Map state that tolerates no member
    failure discards the members that had already succeeded. Returning in time is
    therefore worth more than one more attempt that provably cannot complete.

    ⚠️ **Raising early is better than being killed, but it is not automatically
    retryable, and the difference is the caller's to make.** On the paths where the
    error that surfaces is a read timeout, the handlers wrap it as ``TransientError``
    (``raise_if_transient``) and ``ExtractionStep``/``ShardExtractionStep`` retry eight
    times against a state-machine budget of 21,600 s. Two of the refusal sites do not
    produce such an error, because the ladder they belong to does not: the
    OpenAI Responses HTTP-status branch raises a ``RuntimeError`` carrying no cause,
    and the LambdaHook ``FunctionError`` branch raises the ``RuntimeError`` its own
    exhausted path raises. Each of those is classified the same way the exhausted
    ladder's failure would have been, which is the property worth having — the gate
    changes how many attempts are made and not how the failure is named — but it means
    the gain at those sites is the freed invocation and the released concurrency slot
    rather than a retry. ``is_transient_error`` follows ``__cause__`` only, so a
    ``raise ... from`` is what carries transience where one is available.

    ⚠️ ``DEADLINE_RESERVE_SECONDS`` is deliberately NOT subtracted here, and that is
    the difference between this and :func:`clamp_sleep_to_budgets`. The reserve exists
    so a SLEEP does not end exactly at the wall; applying it to this comparison would
    make a zero-cost attempt "not fit" in the last 30 seconds of an invocation and so
    stop the ladder on proximity after all — which is exactly the behaviour the
    wall-clock bound is documented not to have, and what
    ``test_the_deadline_still_only_shortens_and_does_not_stop`` pins. An attempt that
    costs nothing always fits, **as long as the deadline is in the future** — past it
    every cost is refused, which is correct for an invocation that really is over and
    is the one way the process-wide fallback can be wrong when it is stale. The margin
    for the unwind comes from the size of the
    attempt being refused: what makes this fire is an attempt expensive enough that
    skipping it leaves real time behind, which is the same property that makes it
    unaffordable.

    ⚠️ **What this cannot refuse is an attempt more expensive than anything measured**,
    and that is a limit of measuring rather than an oversight. A ladder's first attempt
    is unmeasured, so it is never refused; and a ladder whose attempts have all been
    cheap will begin one expensive attempt before the figure reflects it — on the
    LambdaHook ladder, where one attempt may occupy 840 s of a 900 s caller, seven
    cheap throttles followed by one real stall still ends 195 s past the wall. The
    alternative — estimating from the client's configured read timeout — was rejected
    because a read timeout is an upper bound on an attempt, not its expected cost: a
    hook that normally returns in 20 s would be refused in all but the first 60 s of
    every invocation, which is the feature rather than the defect.

    So the bound is **"no attempt begins that the remaining invocation cannot hold,
    given what this ladder has already measured"**, and the residual it leaves is one
    attempt per cost level that the ladder has not seen before. The gate's job is to
    stop the ladder repeating an expense it has already proved, which is the shape
    #1362 and #1310 both reported.

    ⚠️ **A high-water mark is pessimistic once a ladder has been expensive, and that
    costs retries that would have been cheap.** Measured over 21 cost schedules on the
    streamed decorator with the agentic ladder's own settings, one changes: a 300 s
    stall followed by 50 ms throttles, entered 300 s into the invocation, makes two
    attempts where the last-attempt comparison made six, and gives up with 295 s of
    the invocation unused. Nothing was avoided in that cell — the declined attempts
    were throttles refused in milliseconds — so the trade is real and it is accepted
    deliberately: the alternative is the measured 192 s overrun on the non-streamed
    ladder, where the same shape ends in a wall-clock kill that Step Functions does
    not retry. Giving up early raises a transient error the caller retries with hours
    of backoff; giving up late loses the document.

    This is what lets the read timeouts be sized for the work rather than for the
    arithmetic. Making the constants sum to fit inside one invocation forced the
    agentic read timeout below a gap that real generations exhibit — the Nuveen
    agentic extraction stalls longer than 180s between stream events, so every
    attempt timed out and the ladder spent the whole 900s making five of them
    (#1310). Bounding the LADDER instead leaves one stall's worth of margin and
    still ends the invocation with a retryable name.
    """
    if attempt_cost_seconds is None:
        return False
    deadline = get_lambda_deadline_epoch()
    if deadline is None:
        return False
    remaining = deadline - time.time()
    if remaining >= attempt_cost_seconds:
        return False
    # The figure is the COST the caller asked about, not a measured duration: on the
    # client ladders it is the costliest attempt plus the backoff about to be slept.
    # Wording it as "the last attempt took Ns" made the log disagree with the number
    # beside it by exactly one backoff.
    logger.warning(
        "Stopping %s retries: the next attempt is budgeted at %.0fs and only %.0fs "
        "of this invocation remain, so it cannot finish. Raising now instead of being "
        "killed on the wall clock, which Step Functions classifies as deterministic "
        "and does not retry; what the caller does with this error depends on how it "
        "classifies it.",
        func_name,
        attempt_cost_seconds,
        max(0.0, remaining),
    )
    return True
