# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Annotations are postponed so the type-stub-only imports below (guarded by
# TYPE_CHECKING) are never evaluated at runtime.
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, Unpack

import botocore.exceptions
import urllib3.exceptions

from idp_common.failed_attempt import drop_failed_attempt

# The invocation deadline, the sleep clamp and the pre-attempt budget gate. They live
# in a standard-library-only leaf module and are re-exported from here, because the
# ladders that need them sit on both sides of an import boundary: the decorators
# below, and the four ladders in ``bedrock/client.py`` / ``bedrock/openai_responses.py``,
# which take the rule from the leaf rather than from here because importing this
# package costs that cold start ``idp_common.config`` (measured in
# ``idp_common.lambda_deadline``). One implementation is the point — the defect has
# now appeared on four ladders, and a copied predicate is how the next one gets
# missed. These names stay importable from here: handlers and tests already reach for
# ``utils.bedrock_utils.set_lambda_deadline_epoch``. Two of the five are not used in
# this module and carry a ``noqa`` rather than living in an ``__all__``: adding one to
# a module that has never had one silently narrows ``import *`` to whatever that list
# happens to hold on the day, and the next public name added here would be missing
# from it.
from idp_common.lambda_deadline import (
    attempt_cannot_finish,
    clamp_sleep_to_budgets,
    get_lambda_deadline_epoch,
    remaining_invocation_seconds,  # noqa: F401 - re-exported, unused here
    set_lambda_deadline_epoch,  # noqa: F401 - re-exported, unused here
)

# Type stubs only. ``mypy-boto3-bedrock-runtime`` ships in the ``test`` and
# ``agentic-extraction`` extras, NOT in lean Lambda extras like ``assessment`` —
# so importing it at runtime makes this module unimportable from those functions
# (Runtime.ImportModuleError at cold start, before the handler body runs). Guarded
# per the project convention in .claude/skills/backend-lambda.md.
if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient
    from mypy_boto3_bedrock_runtime.type_defs import (
        ConverseRequestTypeDef,
        ConverseResponseTypeDef,
        ConverseStreamRequestTypeDef,
        ConverseStreamResponseTypeDef,
        InvokeModelRequestTypeDef,
        InvokeModelResponseTypeDef,
    )

# Optional import for strands-agents (may not be installed in all environments)
try:
    from strands.types.exceptions import ModelThrottledException

    _STRANDS_AVAILABLE = True
except ImportError:
    _STRANDS_AVAILABLE = False
    # Create a placeholder exception class that will never match
    ModelThrottledException = type("ModelThrottledException", (Exception,), {})  # type: ignore[misc, assignment]

# Configure logger
logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# --- Lambda wall-clock deadline -----------------------------------------------
# Why the decorators need it: a backoff ladder that does not know when its own
# function will be killed can schedule a sleep longer than the whole invocation.
# ``invoke_agent_with_retry`` was configured with ``max_delay=1800`` inside a
# 900-second Lambda, so one transient ``Read timed out`` could spend the entire
# invocation asleep and achieve nothing.
#
# The deadline CLAMPS a sleep; it never converts one into a failure. That is
# deliberate, and it is the half of the policy that differs from the cumulative
# allowance, which DOES end the ladder (see ``_backoff_or_none``). Near the wall the
# attempts themselves are what consume the clock, so continuing can end in a real
# Lambda timeout — and those attempts may still succeed, which is worth more than a
# marginally earlier raise. The one exception is an attempt whose own measured cost
# no longer fits, which ``attempt_cannot_finish`` refuses to begin.
#
# Which failure mode is preferable is a property of the state machine, and it has
# moved: ``Sandbox.Timedout``/``Lambda.Unknown``/``States.Timeout`` are down to
# ``MaxAttempts: 1`` on every Lambda task since #917, because a document that needs
# more time than the function has fails identically on every attempt. A raised
# transient error does better: the handlers wrap one as ``TransientError`` via
# ``utils.transient_errors.raise_if_transient``, and ``ExtractionStep`` and
# ``ShardExtractionStep`` list that with ``MaxAttempts: 8`` in
# ``patterns/unified/statemachine/workflow.asl.json``. Completed shards are skipped
# on resume either way. So the underlying error name is no longer the thing that
# decides this — what decides it is whether the remaining time is better spent on
# attempts (the deadline case) or handed to a ladder with more of it (the allowance
# case).
#
# The shard invocation's time budget — how long one Bedrock request may stall,
# how much backoff this ladder may spend, and the function's Lambda ceiling —
# lives in ``idp_common.timeout_budget``, because those numbers are only correct
# relative to each other and ``extraction.runtime`` needs them at import time
# without pulling in this package (#1014). The decorators below enforce the
# backoff half of it; see that module for the whole inequality.


def _clamped_or_log(
    sleep_time: float,
    total_slept: float,
    max_total_delay: float | None,
    func_name: str,
) -> float:
    """:func:`clamp_sleep_to_budgets` plus a log line when it actually shortened."""
    allowed = clamp_sleep_to_budgets(sleep_time, total_slept, max_total_delay)
    if allowed < sleep_time:
        deadline = get_lambda_deadline_epoch()
        logger.warning(
            "Shortening %s retry backoff from %.1fs to %.1fs to stay inside the "
            "retry budget (slept %.1fs of %s) and this Lambda invocation (%s left). "
            "%s",
            func_name,
            sleep_time,
            allowed,
            total_slept,
            f"{max_total_delay:.0f}s" if max_total_delay is not None else "unbounded",
            f"{deadline - time.time():.1f}s" if deadline is not None else "unknown",
            # Which bound bit decides what happens next, and saying the wrong one
            # here put two contradictory lines next to each other in the log.
            "This was the last backoff the allowance permits; the attempt after it "
            "is not made."
            if max_total_delay is not None and total_slept + allowed >= max_total_delay
            else "The time goes to another attempt rather than to sleeping; if it "
            "runs out the invocation times out, which the caller retries.",
        )
    return allowed


def _backoff_or_none(
    sleep_time: float,
    total_slept: float,
    max_total_delay: float | None,
    func_name: str,
    worst_attempt_seconds: float | None = None,
) -> float | None:
    """Seconds to sleep before the next attempt, or ``None`` to stop retrying.

    ``None`` is returned for two conditions, both of which mean the next attempt is
    not worth making: the **cumulative** allowance ``max_total_delay`` is spent, or
    the remaining invocation cannot accommodate another attempt the size of the
    costliest one measured (:func:`~idp_common.lambda_deadline.attempt_cannot_finish`).

    On the allowance: past that point every remaining attempt goes out
    with no delay at all, and an unbacked-off retry against a service that is asking
    us to slow down is the shape that makes congestion worse. Measured on the agentic
    ladder's own numbers (``max_retries=50``, 90s allowance) over 200 jitter seeds:
    all 50 attempts were always made, and **43-45 of them (mean 44)** carried no
    backoff — so the ladder sent roughly **eight times** the requests it needed to.
    The burst does not buy the time it looks like it buys either, because a refused
    call returns in well under a second. With the stop, the same measurement gives
    **5-7 attempts (typically 6)** and no zero-backoff send at all, with the 90s
    allowance still spent in full.

    Stopping hands the decision to the caller, which on this path is a better place
    for it. ``ExtractionStep``/``ShardExtractionStep`` retry ``TransientError`` eight
    times at ``IntervalSeconds: 10`` with ``BackoffRate`` 2.5/2.0 — minutes to hours
    of backoff, against a state-machine budget of 21,600s — and the handlers wrap a
    transient failure under that name (``raise_if_transient``). It also releases the
    Lambda concurrency slot at once instead of holding it to spin.

    Two things about the objection this overrides, because it was a deliberate
    decision and its stated reason was that failing fast "would convert a
    recoverable timeout into an unrecoverable task failure". Neither half survives
    for THIS bound. Spinning after a spent allowance did not end in a Lambda
    timeout: measured, it raised the same exception class this now raises, about ten
    seconds later. And being killed by the timeout is no longer the better outcome —
    ``Sandbox.Timedout`` is down to ``MaxAttempts: 1`` since #917, against eight for
    ``TransientError``.

    The **wall-clock** bound remains asymmetric with the allowance, and the axis is
    what an attempt COSTS rather than how close the wall is. A deadline alone still
    only shortens sleeps and keeps trying: near the wall the attempts themselves are
    what consume the clock, and a cheap attempt — a throttle refused in
    milliseconds — may still succeed, which is worth more than a marginally earlier
    raise. ``test_{sync,async}_retry_shortens_the_sleep_and_keeps_retrying`` and
    ``test_the_deadline_still_only_shortens_and_does_not_stop`` pin that, and they
    keep passing because their attempts cost nothing. What DOES stop the ladder is an
    attempt whose own measured duration no longer fits in what remains, where
    "continuing may still succeed" is not true: it cannot even return. That is the
    one case :func:`~idp_common.lambda_deadline.attempt_cannot_finish` removes,
    and only that one.
    """
    if attempt_cannot_finish(worst_attempt_seconds, func_name):
        return None
    if max_total_delay is not None and total_slept >= max_total_delay:
        logger.warning(
            "Stopping %s retries: its %.0fs backoff allowance is spent, so every "
            "further attempt would be sent with no delay at all. Raising instead. A "
            "transient failure is retried by the caller with a longer backoff than "
            "fits in one invocation; one it classifies as deterministic is not, and "
            "either way this invocation is freed now.",
            func_name,
            max_total_delay,
        )
        return None
    return _clamped_or_log(sleep_time, total_slept, max_total_delay, func_name)


# Bedrock rejects a request whose IMAGES are wrong — too many pixels per side, too
# many bytes — with a ValidationException, the same code it uses for a context
# overflow, and with overlapping vocabulary ("exceeds", "too large"). The remedies
# are opposite: an oversized image is fixed only by rendering pages smaller, never
# by sending fewer pages or switching extraction mode. So the image case is matched
# separately and checked FIRST, both to give it its own explanation and so it can
# never be absorbed by the overflow branch as the wording changes (#994).
#
# For the record, measured against today's wording: is_input_token_overflow does
# NOT claim "image exceed max allowed size for many-image requests: 2000 pixels" —
# it requires "input"/"context"/"prompt" vocabulary that string does not carry. The
# misdiagnosis reported in #994 came from the OTHER direction: Bedrock reports an
# oversized request PAYLOAD as "Input is too long for requested model", which is a
# genuine overflow match, and the token estimate then in use over-stated the page
# images 2.4x, so the message compared an inflated estimate against a window it
# had not actually exceeded. That half is fixed in bedrock.model_utils, not here.
# Every marker names an image limit explicitly. Two earlier candidates — "image
# size" and "invalid image" — were dropped: they are generic enough to appear in
# an unrelated message, and this verdict is DETERMINISTIC (it raises a
# non-retryable error and short-circuits the retry ladder), so a false positive
# turns a transient fault into a permanent failure. The error-code guard in
# is_image_request_rejection catches that only when the error carries a definite
# code, which a Strands-wrapped exception does not — hence the narrower list.
#
# Be clear about what a MISS costs, because it is more than a worse message. An
# image rejection worded outside this list is still a ValidationException, which is
# in DEFAULT_RETRYABLE_ERRORS and matched by substring in the generic branch below,
# so it goes back to being retried before failing the way it failed first time. On
# the agentic path that is bounded by AGENT_MAX_TOTAL_BACKOFF_SECONDS rather than by
# max_retries: spending the allowance now ends the ladder (see _backoff_or_none), so
# the stall is a handful of attempts rather than the fifty max_retries permits. That
# stall is the pathology the short-circuit exists to prevent, and it is smaller than
# it was. The trade is still the right way round —
# retrying a transient error costs time, permanently failing a document costs the
# document — but a marker added later should be judged on both sides of it.
_IMAGE_REJECTION_MARKERS = (
    "many-image request",
    "image exceeds",
    "images exceed",
    "image dimensions exceed",
    "image dimension",
    "too many images",
    "image is too large",
    # Claude Haiku 4.5's wording for the per-request image cap, which the other
    # Claude models word as "too many images and documents: 101 + 0 > 100".
    # Measured against Bedrock Converse, 2026-11-01: "Too much media: 0 document
    # pages + 101 images > 100".
    "too much media",
)


def is_image_request_rejection(error: BaseException) -> bool:
    """True if ``error`` is Bedrock rejecting an image's dimensions or bytes.

    Distinct from :func:`is_input_token_overflow`: no amount of shrinking the
    text or reducing pages per shard fixes it — the images themselves must be
    downscaled (see ``idp_common.image.fit_images_in_request``).

    Judged by error code first, exactly as the overflow matcher is, because the
    verdict here is DETERMINISTIC — it turns into a non-retryable
    ``ExtractionImageRejected`` and short-circuits the retry ladder, so a false
    positive converts a transient fault into a permanent failure. An image
    rejection is always a ``ValidationException``, so a definite code that is
    anything else settles it. A Strands-wrapped exception carries no code at all,
    which is why ``_IMAGE_REJECTION_MARKERS`` is kept narrow enough to stand on
    its own.
    """
    code = ""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
    if code and code != "ValidationException":
        return False
    msg = str(error).lower()
    return any(marker in msg for marker in _IMAGE_REJECTION_MARKERS)


#: The subset of ``_IMAGE_REJECTION_MARKERS`` whose limit depends on how MANY
#: images one request carries rather than on any single image: the cap on images
#: per request, and the tighter per-image dimension cap that applies only above a
#: count threshold ("... for many-image requests"). Sending the same pages in two
#: smaller requests fixes these; it does nothing for an image that is too large on
#: its own, which is why the per-image markers are not here.
_IMAGE_COUNT_REJECTION_MARKERS = (
    "too many images",
    "many-image request",
    "too much media",
)


def is_image_count_rejection(error: BaseException) -> bool:
    """True if ``error`` is an image rejection that fewer images per request fixes.

    Narrower than :func:`is_image_request_rejection`, and judged through it first,
    so the error-code guard applies unchanged: a throttle that happens to say "too
    many images" is not a rejection of the request's shape.
    """
    if not is_image_request_rejection(error):
        return False
    msg = str(error).lower()
    return any(marker in msg for marker in _IMAGE_COUNT_REJECTION_MARKERS)


def is_input_token_overflow(error: BaseException) -> bool:
    """True if ``error`` is a Bedrock input/context overflow.

    Bedrock phrases it several ways — "Input is too long for requested model",
    "Input Tokens Exceeded", "input token count ... exceeds the maximum" — so the
    match is loose. Shared by summarization (which degrades to a stub) and
    extraction (which explains the failure); keep the one matcher.

    An image dimension/byte rejection is NOT an overflow even though the wording
    overlaps, so it is excluded first (#994).
    """
    if is_image_request_rejection(error):
        return False
    code = ""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
    if code and code != "ValidationException":
        # A ClientError with a definite code is judged by the code: a throttle that
        # mentions "input tokens per minute" is not an overflow.
        return False
    msg = str(error).lower()
    if "too long" in msg and "input" in msg:
        return True
    if "input token" in msg or "input tokens" in msg:
        return True
    if "context" in msg and ("exceed" in msg or "too long" in msg):
        return True
    if "prompt is too long" in msg:  # Anthropic-native phrasing forwarded by Bedrock
        return True
    return False


#: ``stopReason`` values that mean the model stopped because it ran out of OUTPUT
#: budget rather than because it finished answering.
#:
#: Two of the three are reachable today and the third is defensive; be precise
#: about which, because the set decides what counts as data loss:
#:
#: * ``max_tokens`` — Bedrock Converse.
#: * ``incomplete`` — what ``bedrock.openai_responses.translate_response`` puts in
#:   ``stopReason``, which is the Responses API's ``status`` verbatim.
#: * ``max_output_tokens`` — the Responses API's own ``incomplete_details.reason``
#:   spelling for the same condition. **Nothing in this repository writes it into
#:   ``stopReason``**: the adapter maps ``status``, and the reason is read
#:   separately by ``_incomplete_reason``. It is kept so a response that surfaces
#:   the reason directly is matched rather than read as a clean stop, which is the
#:   safe direction for a completeness signal — but it is not evidence that the
#:   adapter maps it, and no test can drive it end to end.
OUTPUT_TRUNCATION_STOP_REASONS = ("max_tokens", "max_output_tokens", "incomplete")


def is_output_token_truncation(response: object) -> bool:
    """True if ``response`` says the model hit its OUTPUT token ceiling.

    The opposite direction from :func:`is_input_token_overflow`, which reads an
    *exception*: this reads a **successful** response that is nonetheless short.
    That is the dangerous shape, because nothing raises — the call returns 200, the
    text is well formed as far as it goes, and a list that stopped in the middle is
    indistinguishable from a list that was always that long.

    Accepts either the metering-wrapped envelope ``idp_common.bedrock.invoke_model``
    returns (``{"response": ..., "metering": ...}``) or the bare Converse response,
    because callers hold one or the other and a matcher that silently answers
    ``False`` for the wrong shape is worse than no matcher.

    Against the three inline copies this replaced it is a **strict widening**, not
    an exact equivalent, on one shape: ``{"response": None, "stopReason": ...}``
    answered ``False`` there (the ``isinstance`` guard rejected ``None`` and the
    comparison never reached the envelope) and answers ``True`` here. ``invoke_model``
    does not produce that shape, so nothing changes in practice; it is called a
    widening rather than a no-op because the direction matters — the new answer is
    the one that reports a truncation.

    ⚠️ A ``False`` here is **not** evidence that the output is complete. A model
    that abbreviates a long list on its own stops with ``end_turn`` and a
    syntactically complete answer; measured on a 17-page, 800-row statement, the
    short runs used ~2.3k output tokens where the complete runs on the same model
    used ~38.5k, so no ceiling was involved (#1361). Row-level completeness is
    judged separately — see ``ExtractionService._build_extraction_issues``.
    """
    if not isinstance(response, dict):
        return False
    raw = response.get("response", response)
    if not isinstance(raw, dict):
        raw = response
    return raw.get("stopReason") in OUTPUT_TRUNCATION_STOP_REASONS


def is_max_tokens_agent_error(error: BaseException) -> bool:
    """True if ``error`` is Strands' hard failure on an output-truncated turn.

    The *exception* counterpart of :func:`is_output_token_truncation`, which reads a
    response. Strands raises ``MaxTokensReachedException`` when a model stops with
    ``stopReason == "max_tokens"`` mid-agent-loop, and deliberately lets it bubble
    up rather than wrapping it — so every caller that runs an agent loop has to
    recognise it, and a copied predicate is how one of them gets missed.

    Matched on type name and message text rather than by importing the Strands
    exception type, mirroring ``extraction.agentic_idp._is_context_overflow_error``:
    this module is imported by Lambda functions whose extras do not include the
    agentic stack, so an ``import strands`` here would make it unimportable there.
    """
    if "maxtokensreached" in type(error).__name__.lower():
        return True
    msg = str(error).lower()
    return "max_tokens limit" in msg or "due to max_tokens" in msg


# Default retryable error codes (matched against ClientError codes and exception
# messages).
#
# Matching is CASE-INSENSITIVE: Bedrock's streaming APIs report the same condition
# with a lower-cased first letter (ConverseStream raises
# "internalServerException" where Converse raises "InternalServerException"), so a
# case-sensitive set silently fails to retry transient streaming errors. List one
# spelling per condition here; both decorators fold case before comparing.
DEFAULT_RETRYABLE_ERRORS = {
    "ThrottlingException",
    "ModelThrottledException",  # Strands wrapper for throttling
    "ModelErrorException",
    "ValidationException",
    "ServiceQuotaExceededException",
    "RequestLimitExceeded",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalServerException",  # Transient Bedrock server-side errors
    "InternalServerError",  # Variant of InternalServerException
    "RequestTimeout",
    "RequestTimeoutException",
    # Transient network/read timeouts to Bedrock, matched by exception name and as
    # message substrings in the generic-Exception branch — NOT the generic
    # "EventLoopException" wrapper name (which also wraps non-retryable failures),
    # so a slow/oversized request retries with backoff instead of failing the whole
    # section after the full read timeout. The durable fix for oversized requests is
    # image capping + sharding (below); this just prevents a hard fail on a
    # transient blip. botocore's own transport failures, raw or wrapped by Strands,
    # are recognised by TYPE instead: see _RETRYABLE_TRANSPORT_EXCEPTION_TYPES.
    "ReadTimeoutError",
    "ConnectTimeoutError",
    "Read timed out",
    "AWSHTTPSConnectionPool",
}

# Pre-folded for case-insensitive comparison.
_DEFAULT_RETRYABLE_ERRORS_LOWER = {err.lower() for err in DEFAULT_RETRYABLE_ERRORS}

# botocore's transport failures for a Bedrock call that got no response: a read
# timeout, a connect timeout, and a connection closed before the response arrived
# (``ConnectionClosedError``, which botocore raises for urllib3's ``ProtocolError``).
# The synchronous ladder in ``bedrock/client.py`` retries all three among its
# transport failures (#1706).
#
# Matched by type, and anywhere on the explicit ``raise ... from`` chain, because
# Strands 1.48 delivers them in two forms (measured with a real Agent and a real
# botocore client, only urllib3's ``urlopen`` replaced, on both ``converse`` and
# ``converse_stream``): raw from the first model
# call of an invocation, and wrapped in ``EventLoopException`` — ``__cause__`` the
# botocore error, message its ``str()`` — from the model call of every cycle after a
# tool use, which on the agentic extraction path is most of them. Neither form
# carries a name or a message the substring vocabulary above matches: before this,
# a dropped connection was never retried, and a read timeout was retried only on the
# first cycle, because botocore words it "Read timeout on endpoint URL" (#1730).
#
# A connection reset PARTWAY through a streamed response (``converse_stream``, after
# botocore has returned the event stream) is not rewrapped by botocore at all: it
# reaches here as urllib3's own ``ProtocolError``, raw or wrapped in the same way
# (#1745). Retrying it is safe for the same reason it is safe before the response:
# Strands 1.48 appends the assistant message, and starts any tool the model asked
# for, only after the stream has been read to its end (``process_stream`` yields
# its result after the loop over chunks; ``_handle_model_execution`` appends it
# after that). Measured with hand-built event streams cut after a complete
# ``toolUse`` block and ``messageStop``: the tool ran zero times and
# ``agent.messages`` was unchanged, so the retry resumes the same conversation an
# unanswered attempt leaves. A mid-stream read STALL is urllib3's
# ``ReadTimeoutError``, retried by name above. The one visible side effect is
# that the chunks already streamed were delivered to the agent's callback handler.
_RETRYABLE_TRANSPORT_EXCEPTION_TYPES: tuple[type[Exception], ...] = (
    botocore.exceptions.ReadTimeoutError,
    botocore.exceptions.ConnectTimeoutError,
    botocore.exceptions.ConnectionClosedError,
    urllib3.exceptions.ProtocolError,
)

# Default retryable exception types, matched by isinstance against the exception and
# its ``__cause__`` chain (see :func:`_raised_from`). ModelThrottledException is
# included only if strands is available.
DEFAULT_RETRYABLE_EXCEPTION_TYPES: tuple[type[Exception], ...] = (
    (ModelThrottledException,) if _STRANDS_AVAILABLE else ()
) + _RETRYABLE_TRANSPORT_EXCEPTION_TYPES

# Bounds the ``__cause__`` walk; a real chain here is two or three links.
_MAX_CAUSE_CHAIN = 16


def _raised_from(error: BaseException, types: tuple[type[BaseException], ...]) -> bool:
    """Whether ``error``, or an exception it was explicitly raised ``from``, is one
    of ``types``.

    Only ``__cause__`` is followed, never ``__context__``: Python sets the context on
    any exception raised while another is being handled, so a deterministic failure
    raised inside a caller's ``except`` would inherit the transience of whatever the
    caller was handling. ``transient_errors`` follows the same rule for the same
    reason. Cycle-safe and bounded.
    """
    seen: set[int] = set()
    node: BaseException | None = error
    while node is not None and id(node) not in seen and len(seen) < _MAX_CAUSE_CHAIN:
        if isinstance(node, types):
            return True
        seen.add(id(node))
        node = node.__cause__
    return False


def async_exponential_backoff_retry[T, **P](
    max_retries: int = 5,
    initial_delay: float = 1.0,
    max_delay: float = 32.0,
    exponential_base: float = 2.0,
    jitter: float = 0.1,
    retryable_errors: set[str] | None = None,
    retryable_exception_types: tuple[type[Exception], ...] | None = None,
    max_total_delay: float | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Retry with exponential backoff, bounded by cumulative delay AND by the
    Lambda deadline (see :func:`set_lambda_deadline_epoch`). The two bounds do
    different things when they bite, and the difference is deliberate: a spent
    ``max_total_delay`` **stops** the ladder and re-raises, because every further
    attempt would carry no delay at all; the deadline only **shortens** sleeps and
    keeps trying, because there is no burst to suppress that close to the wall and
    an attempt may still succeed. See :func:`_backoff_or_none`."""
    # Use defaults if not provided
    if retryable_errors is None:
        retryable_errors = DEFAULT_RETRYABLE_ERRORS
    # Fold case once per decoration rather than on every comparison.
    retryable_lower = {err.lower() for err in retryable_errors}
    if retryable_exception_types is None:
        retryable_exception_types = DEFAULT_RETRYABLE_EXCEPTION_TYPES

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            delay = initial_delay
            total_slept = 0.0

            def log_bedrock_invocation_error(error: Exception, attempt_num: int):
                """Log bedrock invocation details when an error occurs"""
                # Fallback logging if extraction fails
                logger.error(
                    "Bedrock invocation error",
                    extra={
                        "function_name": func.__name__,
                        "original_error": str(error),
                        "max_attempts": max_retries,
                        "attempt_num": attempt_num,
                    },
                )

            # The COSTLIEST attempt measured so far, so the ladder can refuse to begin
            # one that provably cannot finish inside this invocation. The worst rather
            # than the last: a cheap throttle between two 600s stalls would otherwise
            # reset the comparison and re-license an attempt that cannot return.
            # Measured rather than assumed: see lambda_deadline.attempt_cannot_finish.
            worst_attempt_seconds: float | None = None
            # Whatever the CALLER was handling when it awaited this. Not ours to
            # touch: see ``drop_failed_attempt``'s ``stop_at``.
            caller_exception = sys.exc_info()[1]

            for attempt in range(max_retries):
                attempt_started = time.time()
                try:
                    return await func(*args, **kwargs)
                except botocore.exceptions.ClientError as e:
                    worst_attempt_seconds = max(
                        worst_attempt_seconds or 0.0, time.time() - attempt_started
                    )
                    error_code = e.response.get("Error", {}).get("Code")

                    # For EventStreamError (subclass of ClientError), the error code
                    # may be in a different location or need to be extracted from the message
                    if not error_code:
                        # Try to extract error code from exception message
                        # Format: "An error occurred (errorCode) when calling..."
                        match = re.search(r"\((\w+)\)", str(e))
                        if match:
                            error_code = match.group(1)

                    # Log bedrock invocation details for all errors
                    log_bedrock_invocation_error(e, attempt + 1)

                    if (
                        error_code == "ValidationException"
                        and "Output blocked by content filtering policy"
                        not in e.response.get("Error", {}).get("Message", "")
                    ):
                        raise
                    if (
                        error_code is None
                        or error_code.lower() not in retryable_lower
                        or attempt == max_retries - 1
                    ):
                        raise

                    jitter_value = random.uniform(-jitter, jitter)  # nosec B311 - retry jitter
                    sleep_time = max(0.1, delay * (1 + jitter_value))
                    backoff = _backoff_or_none(
                        sleep_time,
                        total_slept,
                        max_total_delay,
                        func.__name__,
                        worst_attempt_seconds,
                    )
                    if backoff is None:
                        raise
                    logger.warning(
                        f"{error_code}:{e.response.get('Error', {}).get('Message', '')} encountered in {func.__name__}. Retrying in {backoff:.2f} seconds. "
                        f"Attempt {attempt + 1}/{max_retries}"
                    )
                    await asyncio.sleep(backoff)
                    total_slept += backoff
                    delay = min(delay * exponential_base, max_delay)
                except Exception as e:
                    worst_attempt_seconds = max(
                        worst_attempt_seconds or 0.0, time.time() - attempt_started
                    )
                    # An image rejection is deterministic: the same request will be
                    # rejected identically every time, so retrying it only spends
                    # the backoff budget. The ClientError branch above already
                    # raises a raw ValidationException, but Strands re-wraps Bedrock
                    # errors, and this branch judges retryability by substring —
                    # "ValidationException" is in DEFAULT_RETRYABLE_ERRORS, so a
                    # wrapped image rejection would otherwise be retried up to
                    # max_retries (50 on the agentic extraction path) before the
                    # caller ever got to classify it (#994).
                    if is_image_request_rejection(e):
                        log_bedrock_invocation_error(e, attempt + 1)
                        raise
                    # A retryable exception type (Strands' ModelThrottledException,
                    # botocore's transport failures), raw or raised ``from`` one.
                    is_retryable_type = _raised_from(e, retryable_exception_types)

                    # Also check if exception name or message contains retryable error patterns
                    exception_name = type(e).__name__
                    exception_str = str(e)
                    exception_str_lower = exception_str.lower()
                    is_retryable_name = (
                        exception_name.lower() in retryable_lower
                        or any(err in exception_str_lower for err in retryable_lower)
                    )

                    if (
                        is_retryable_type or is_retryable_name
                    ) and attempt < max_retries - 1:
                        # Log and retry
                        log_bedrock_invocation_error(e, attempt + 1)
                        jitter_value = random.uniform(-jitter, jitter)  # nosec B311 - retry jitter
                        sleep_time = max(0.1, delay * (1 + jitter_value))
                        backoff = _backoff_or_none(
                            sleep_time,
                            total_slept,
                            max_total_delay,
                            func.__name__,
                            worst_attempt_seconds,
                        )
                        if backoff is None:
                            raise
                        logger.warning(
                            f"{exception_name}: {exception_str} encountered in {func.__name__}. "
                            f"Retrying in {backoff:.2f} seconds. Attempt {attempt + 1}/{max_retries}"
                        )
                        await asyncio.sleep(backoff)
                        total_slept += backoff
                        delay = min(delay * exponential_base, max_delay)
                        # The next attempt starts after this handler exits, so the
                        # failure is unreachable by then; but a botocore timeout
                        # leaves botocore's reference cycle behind it (see
                        # ``drop_failed_attempt``), so without this the failed
                        # attempt's serialised request stayed in memory until the
                        # cyclic collector ran. Measured with a real botocore client
                        # and an 8 MB request, cyclic collector off: 8, 16, 24 and
                        # 32 MB still held at attempts 2-5 with read timeouts, and
                        # the same with each raised ``from`` a Strands exception. A
                        # botocore throttle (``ClientError``) leaves no cycle and
                        # measured flat, which is why the branch above has no call
                        # (#1706).
                        drop_failed_attempt(e, stop_at=caller_exception)
                        continue

                    # Log bedrock invocation details for non-retryable exceptions
                    log_bedrock_invocation_error(e, attempt + 1)
                    raise

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def exponential_backoff_retry[T, **P](
    max_retries: int = 5,
    initial_delay: float = 1.0,
    max_delay: float = 32.0,
    exponential_base: float = 2.0,
    jitter: float = 0.1,
    max_total_delay: float | None = None,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Retry with exponential backoff, bounded by cumulative delay AND by the
    Lambda deadline (see :func:`set_lambda_deadline_epoch`). The two bounds do
    different things when they bite, and the difference is deliberate: a spent
    ``max_total_delay`` **stops** the ladder and re-raises, because every further
    attempt would carry no delay at all; the deadline only **shortens** sleeps and
    keeps trying, because there is no burst to suppress that close to the wall and
    an attempt may still succeed. See :func:`_backoff_or_none`."""

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            delay = initial_delay
            total_slept = 0.0

            def log_bedrock_invocation_error(error: Exception, attempt_num: int):
                """Log bedrock invocation details when an error occurs"""
                try:
                    # Check for invoke_model API (has 'body' parameter)
                    if "body" in kwargs:
                        logger.error(
                            "Bedrock invoke_model failed",
                            extra={
                                "attempt_number": attempt_num,
                                "max_retries": max_retries,
                                "function_name": func.__name__,
                                "error": str(error),
                                "body": kwargs["body"],
                            },
                        )
                    # Check for converse API (has structured parameters)
                    elif any(
                        key in kwargs
                        for key in [
                            "messages",
                            "inferenceConfig",
                            "system",
                            "toolConfig",
                        ]
                    ):
                        # Log converse API parameters
                        converse_data = {
                            k: v
                            for k, v in kwargs.items()
                            if k
                            in [
                                "messages",
                                "inferenceConfig",
                                "system",
                                "toolConfig",
                                "additionalModelRequestFields",
                                "guardrailConfig",
                                "performanceConfig",
                                "promptVariables",
                                "requestMetadata",
                            ]
                        }
                        logger.error(
                            "Bedrock converse failed",
                            extra={
                                "attempt_number": attempt_num,
                                "max_retries": max_retries,
                                "function_name": func.__name__,
                                "error": str(error),
                                "parameters": json.dumps(converse_data, default=str),
                            },
                        )
                    else:
                        # Generic bedrock error logging
                        logger.error(
                            "Bedrock invocation failed",
                            extra={
                                "attempt_number": attempt_num,
                                "max_retries": max_retries,
                                "function_name": func.__name__,
                                "error": str(error),
                            },
                        )

                except Exception as log_error:
                    # Fallback logging if extraction fails
                    logger.error(
                        "Failed to log bedrock invocation details",
                        extra={
                            "function_name": func.__name__,
                            "log_error": str(log_error),
                            "original_error": str(error),
                        },
                    )

            # See the async decorator: the ladder refuses to begin an attempt the
            # size of the costliest one measured when the invocation cannot hold it.
            worst_attempt_seconds: float | None = None

            for attempt in range(max_retries):
                attempt_started = time.time()
                try:
                    return func(*args, **kwargs)
                except botocore.exceptions.ClientError as e:
                    worst_attempt_seconds = max(
                        worst_attempt_seconds or 0.0, time.time() - attempt_started
                    )
                    error_code = e.response.get("Error", {}).get("Code")

                    # Log bedrock invocation details for all errors
                    log_bedrock_invocation_error(e, attempt + 1)

                    if (
                        error_code == "ValidationException"
                        and "Output blocked by content filtering policy"
                        not in e.response.get("Error", {}).get("Message", "")
                    ):
                        raise
                    # Shares DEFAULT_RETRYABLE_ERRORS with the async decorator so
                    # both paths retry the same conditions; converse_stream is
                    # wrapped here, so any streaming-only spelling omitted from
                    # that set fails the caller on the first attempt.
                    if (
                        error_code is None
                        or error_code.lower() not in _DEFAULT_RETRYABLE_ERRORS_LOWER
                        or attempt == max_retries - 1
                    ):
                        raise

                    jitter_value = random.uniform(-jitter, jitter)  # nosec B311 - retry jitter
                    sleep_time = max(0.1, delay * (1 + jitter_value))
                    backoff = _backoff_or_none(
                        sleep_time,
                        total_slept,
                        max_total_delay,
                        func.__name__,
                        worst_attempt_seconds,
                    )
                    if backoff is None:
                        raise
                    logger.warning(
                        f"{error_code}:{e.response.get('Error', {}).get('Message', '')} encountered in {func.__name__}. Retrying in {backoff:.2f} seconds. "
                        f"Attempt {attempt + 1}/{max_retries}"
                    )
                    time.sleep(backoff)
                    total_slept += backoff
                    delay = min(delay * exponential_base, max_delay)
                except Exception as e:
                    # Log bedrock invocation details for non-ClientError exceptions too
                    log_bedrock_invocation_error(e, attempt + 1)
                    raise

            return func(*args, **kwargs)

        return wrapper

    return decorator


class BedrockClientWrapper:
    """
    A wrapper around AWS Bedrock Runtime Client that provides automatic retry logic
    with exponential backoff for handling transient errors and rate limiting.

    This wrapper automatically retries failed requests for specific error types:
    - ThrottlingException: When API rate limits are exceeded
    - ModelErrorException: When the model encounters temporary errors
    - ValidationException: When content filtering blocks output (retryable case)

    The retry mechanism uses exponential backoff with jitter to avoid thundering herd
    problems when multiple clients retry simultaneously.

    Attributes:
        client (BedrockRuntimeClient): The underlying AWS Bedrock Runtime client
        max_retries (int): Maximum number of retry attempts
        initial_delay (float): Initial delay between retries in seconds
        max_delay (float): Maximum delay between retries in seconds
        exponential_base (float): Base for exponential backoff calculation
        jitter (float): Random jitter factor to add variance to retry delays
        invoke_model: Wrapped invoke_model method with retry logic
        converse: Wrapped converse method with retry logic

    Example:
        >>> import boto3
        >>> from mypy_boto3_bedrock_runtime import BedrockRuntimeClient
        >>> bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
        >>> wrapper = BedrockClientWrapper(bedrock_client, max_retries=3)
        >>> # Use invoke_model with automatic retries
        >>> response = wrapper.invoke_model(
        ...     modelId="us.anthropic.claude-sonnet-4-6",
        ...     body=json.dumps(
        ...         {
        ...             "messages": [{"role": "user", "content": "Hello"}],
        ...             "max_tokens": 100,
        ...         }
        ...     ),
        ... )
        >>> # Use converse API with automatic retries
        >>> response = wrapper.converse(
        ...     modelId="us.anthropic.claude-sonnet-4-6",
        ...     messages=[{"role": "user", "content": [{"text": "Hello"}]}],
        ... )
    """

    def __init__(
        self,
        bedrock_client: BedrockRuntimeClient,
        max_retries: int = 5,
        initial_delay: float = 1.0,
        max_delay: float = 32.0,
        exponential_base: float = 2.0,
        jitter: float = 0.1,
    ):
        """
        Initialize the BedrockClientWrapper with retry configuration.

        Args:
            bedrock_client (BedrockRuntimeClient): The AWS Bedrock Runtime client to wrap
            max_retries (int, optional): Maximum number of retry attempts. Defaults to 5.
            initial_delay (float, optional): Initial delay between retries in seconds. Defaults to 1.0.
            max_delay (float, optional): Maximum delay between retries in seconds. Defaults to 32.0.
            exponential_base (float, optional): Base for exponential backoff calculation. Defaults to 2.0.
            jitter (float, optional): Random jitter factor (0.0-1.0) to add variance to retry delays. Defaults to 0.1.

        Raises:
            TypeError: If bedrock_client is not a BedrockRuntimeClient instance
            ValueError: If retry parameters are invalid (negative values, jitter > 1.0, etc.)
        """
        self.client = bedrock_client

        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter

        # Apply decorator directly to client methods
        self._decorated_invoke_model = exponential_backoff_retry(
            max_retries=max_retries,
            initial_delay=initial_delay,
            max_delay=max_delay,
            exponential_base=exponential_base,
            jitter=jitter,
        )(self.client.invoke_model)

        self._decorated_converse = exponential_backoff_retry(
            max_retries=max_retries,
            initial_delay=initial_delay,
            max_delay=max_delay,
            exponential_base=exponential_base,
            jitter=jitter,
        )(self.client.converse)

        self._decorated_converse_stream_async = exponential_backoff_retry(
            max_retries=max_retries,
            initial_delay=initial_delay,
            max_delay=max_delay,
            exponential_base=exponential_base,
            jitter=jitter,
        )(self.client.converse_stream)

    def invoke_model(
        self, **kwargs: Unpack[InvokeModelRequestTypeDef]
    ) -> InvokeModelResponseTypeDef:
        """
        Invoke a model with automatic retry logic.

        This method has the same signature as BedrockRuntimeClient.invoke_model()
        but includes automatic retry logic with exponential backoff.

        Args:
            modelId: The ID or ARN of the model to invoke
            body: The input data to send to the model
            contentType: The MIME type of the input data
            accept: The desired MIME type of the response
            **kwargs: Additional arguments passed to the underlying API

        Returns:
            InvokeModelResponseTypeDef: The response from the model invocation

        Raises:
            botocore.exceptions.ClientError: For non-retryable errors or after max retries
        """
        return self._decorated_invoke_model(**kwargs)

    def converse(
        self,
        **kwargs: Unpack[ConverseRequestTypeDef],
    ) -> ConverseResponseTypeDef:
        """
        Converse with a model using the conversation API with automatic retry logic.

        This method has the same signature as BedrockRuntimeClient.converse()
        but includes automatic retry logic with exponential backoff.

        Args:
            modelId: The ID or ARN of the model to invoke
            messages: The conversation messages
            system: System prompts to provide context
            inferenceConfig: Configuration for model inference parameters
            toolConfig: Configuration for tool use
            guardrailConfig: Configuration for content filtering
            additionalModelRequestFields: Additional model-specific request fields
            promptVariables: Variables to substitute in prompts
            additionalModelResponseFieldPaths: Additional response field paths
            performanceConfig: Performance optimization configuration
            requestMetadata: Metadata for the request
            **kwargs: Additional arguments passed to the underlying API

        Returns:
            ConverseResponseTypeDef: The response from the conversation

        Raises:
            botocore.exceptions.ClientError: For non-retryable errors or after max retries
        """
        return self._decorated_converse(**kwargs)

    def converse_stream(
        self, **kwargs: Unpack[ConverseStreamRequestTypeDef]
    ) -> ConverseStreamResponseTypeDef:
        """
        Async version of converse_stream with automatic retry logic.

        This method has the same signature as BedrockRuntimeClient.converse_stream()
        but runs asynchronously with automatic retry logic and exponential backoff.

        Args:
            **kwargs: All arguments passed to the underlying converse_stream API

        Returns:
            The streaming response from the conversation

        Raises:
            botocore.exceptions.ClientError: For non-retryable errors or after max retries
        """
        return self._decorated_converse_stream_async(**kwargs)
