# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
The agent chat stream must not send the model's reasoning to the browser, and
must send the answer whole, whatever the chunk boundaries are.

A ``{"data": ...}`` event carries one Bedrock ``contentBlockDelta`` verbatim and
unbuffered, so the split point is chosen by the service and can land anywhere,
including inside ``<thinking>`` or ``</thinking>``. Deciding what to show by
re-running a regex over the accumulated buffer could not cope with that: with no
closing tag in the buffer yet the block matched nothing, so the reasoning was
streamed to the browser, and when the closing tag arrived the cleaned text became
*shorter* than the character count already emitted, so the answer was dropped or
sent from the middle.

**Both halves are asserted independently at every split point**, because the two
symptoms have one cause and each partial fix passes a one-sided test: a change
that repairs only the cursor still leaks the reasoning, and one that only clears
state on the closing tag hides the answer.

Every test here runs against **both** copies of the processor -- the deployed
source and the committed ``vendored/`` copy this package builds from -- so the
vendored copy is covered by behaviour and not only by the byte-identity check in
``test_vendored_in_sync.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import os
import sys
import types

import pytest

pytestmark = pytest.mark.unit

_PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LAMBDA = os.path.dirname(_PKG)

# The two copies a change has to keep in step. Named by path so a missing file is
# a collection error rather than a silently smaller parametrisation.
_COPIES = {
    "source": os.path.join(_LAMBDA, "agent_chat_processor", "index.py"),
    "vendored": os.path.join(_PKG, "vendored", "agent_chat_processor.py"),
}

# The response every sweep below is cut up. Short on purpose: the point is to walk
# *all* the boundaries, and the interesting ones are inside the two tags.
RESPONSE = "<thinking>reasoning</thinking>Answer"
ANSWER = "Answer"
REASONING = "reasoning"


# --- loading ----------------------------------------------------------------
# The processor imports idp_common at module scope, and importing the real
# package runs agent registration that reaches Secrets Manager. Stubs keep the
# import pure, and -- more importantly -- keep this suite from being SKIPPED when
# idp_common is not installed. A skipped test cannot fail, and this is the suite
# that has to fail if the filter regresses.


def _stub_modules() -> dict[str, types.ModuleType]:
    """The idp_common surface the processor touches at import time."""
    made: dict[str, types.ModuleType] = {}

    def mod(name: str, **attrs) -> types.ModuleType:
        m = types.ModuleType(name)
        m.__path__ = []  # so `from a.b import c` resolves through it
        for k, v in attrs.items():
            setattr(m, k, v)
        made[name] = m
        return m

    class _ErrorHandler:
        @staticmethod
        def format_error_for_frontend(exc):
            return {"errorType": "test", "message": str(exc)}

    mod("idp_common")
    mod("idp_common.agents")
    mod("idp_common.agents.analytics", get_analytics_config=lambda *a, **k: {})
    mod("idp_common.agents.common")
    mod("idp_common.agents.common.config", configure_logging=lambda *a, **k: None)
    mod(
        "idp_common.agents.common.bedrock_error_messages",
        BedrockErrorMessageHandler=_ErrorHandler,
    )
    mod("idp_common.agents.factory", agent_factory=types.SimpleNamespace())
    mod("idp_common.utils")
    mod("idp_common.utils.log_sanitizer", sanitize_event_for_logging=lambda e, *a: e)
    return made


def _load(path: str, name: str):
    """Import `path` under `name` with idp_common stubbed, restoring sys.modules."""
    stubs = _stub_modules()
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader, path
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    finally:
        # Put back exactly what was there, so a sibling test that wants the real
        # idp_common still gets it.
        for key, previous in saved.items():
            if previous is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = previous
    return module


@pytest.fixture(scope="module", params=sorted(_COPIES))
def processor(request):
    """The processor module, once per copy, shared by every test in this file."""
    name = f"_acp_under_test_{request.param}"
    module = _load(_COPIES[request.param], name)
    yield module
    sys.modules.pop(name, None)


# --- driving the stream ------------------------------------------------------


class _FakeOrchestrator:
    """Yields the given chunks as `{"data": ...}` events, then a result event."""

    def __init__(self, chunks, with_result: bool = True):
        self._chunks = list(chunks)
        self._with_result = with_result

    def stream_async(self, prompt):
        async def _generate():
            for chunk in self._chunks:
                yield {"data": chunk}
            if self._with_result:
                yield {"result": {"stop_reason": "end_turn"}}

        return _generate()


class Streamed:
    """What the browser and the chat history table would have received."""

    def __init__(self, deltas, processing_flags, final, returned):
        self.deltas = deltas
        # `is_processing` per delta. Kept because it decides how the delta is
        # *rendered*, not merely whether a spinner shows -- see
        # test_every_streamed_delta_is_marked_still_processing.
        self.processing_flags = processing_flags
        self.final = final
        self.returned = returned

    @property
    def streamed(self) -> str:
        """The in-flight bubble's text. `use-agent-chat.ts` *appends* each delta."""
        return "".join(self.deltas)


def drive(processor, chunks, with_result: bool = True) -> Streamed:
    events = []

    processor.set_sink(lambda **kwargs: events.append(kwargs))
    try:
        returned = asyncio.run(
            processor.stream_agent_response(
                None, _FakeOrchestrator(chunks, with_result), "prompt", "session-1"
            )
        )
    finally:
        processor.set_sink(None)

    streaming = [e for e in events if e["method"] == "assistant_stream"]
    return Streamed(
        [e["content"] for e in streaming],
        [e["is_processing"] for e in streaming],
        next(
            (
                e["content"]
                for e in reversed(events)
                if e["method"] == "assistant_final_response"
            ),
            None,
        ),
        returned,
    )


def splits(text: str, pieces: int):
    """Every way of cutting `text` into exactly `pieces` non-empty chunks."""
    for cuts in itertools.combinations(range(1, len(text)), pieces - 1):
        bounds = (0, *cuts, len(text))
        yield [text[bounds[i] : bounds[i + 1]] for i in range(len(bounds) - 1)]


# --- the sweep --------------------------------------------------------------


@pytest.mark.parametrize("split", range(1, len(RESPONSE)))
def test_no_two_chunk_split_leaks_the_reasoning(processor, split):
    """
    The reasoning is absent from the stream at every two-chunk boundary.

    Asserted on its own, with no claim about the answer, because the half-fix that
    clears filter state on the closing tag makes this pass while hiding the answer
    entirely -- measured -- so a test that checked both in one breath would accept
    it. The test below is the other half and fails on that mutation.

    What this cannot do is go red *while the test below stays green*, and the
    reason is arithmetic rather than a gap: the text streamed either equals the
    answer or it does not, so nothing can leak reasoning and still satisfy the
    equality there. It is kept as a separately named statement of the property that
    matters most -- reasoning intended for the model must not reach the browser --
    so that relaxing the equality assertion later cannot quietly remove it.
    """
    result = drive(processor, [RESPONSE[:split], RESPONSE[split:]])

    assert REASONING not in result.streamed
    assert "<think" not in result.streamed
    assert "thinking" not in result.streamed


@pytest.mark.parametrize("split", range(1, len(RESPONSE)))
def test_the_answer_arrives_whole_from_its_first_character(processor, split):
    """
    The stream carries exactly the answer at every two-chunk boundary.

    The other half, asserted on its own: a fix that repairs only the emitted-text
    cursor passes the leak test above and fails here, because a boundary inside the
    opening tag used to render the answer from its seventh character -- which still
    looks like a plausible answer, and is how that shape escapes review.

    Compared against the *joined* deltas rather than the first one, because a
    boundary inside the answer legitimately splits it across two events and the UI
    appends them.
    """
    result = drive(processor, [RESPONSE[:split], RESPONSE[split:]])

    assert result.streamed == ANSWER
    assert result.final == ANSWER
    assert result.returned == ANSWER


def test_no_split_of_the_response_into_up_to_three_chunks_misbehaves(processor):
    """
    Every 1-, 2- and 3-chunk split at once: 631 chunkings, both halves each.

    Two chunks reach every single boundary; three reach every *pair* of them, which
    is what puts a boundary inside both tags of the same response and inside a tag
    twice. Collected into one report rather than asserted per case so a regression
    names the shape it broke on instead of the first index that happens to fail.
    """
    bad = []
    total = 0
    for pieces in (1, 2, 3):
        for chunks in splits(RESPONSE, pieces):
            total += 1
            result = drive(processor, chunks)
            # No separate leak term: `streamed != ANSWER` already covers it, since
            # text that leaks reasoning or a tag fragment cannot also equal the
            # answer. A `leaked` disjunct here would never be the reason this
            # fails, which is the shape of apparent coverage worth not having.
            if result.streamed != ANSWER or result.returned != ANSWER:
                bad.append((chunks, result.deltas, result.returned))

    assert total == 631, total
    assert not bad, f"{len(bad)} of {total} chunkings misbehaved, first 5: {bad[:5]}"


def test_a_response_delivered_one_character_at_a_time(processor):
    """
    The degenerate chunking: every character its own event.

    Nothing forbids a one-character text delta, and at this size every tag in the
    response is split at every position at once, so a boundary rule that is right
    only for some split lengths cannot survive it.
    """
    result = drive(processor, list(RESPONSE))

    assert REASONING not in result.streamed
    assert result.streamed == ANSWER


def test_two_blocks_with_the_second_one_split(processor):
    """A second block must be recognised after the first has been closed."""
    result = drive(
        processor,
        [
            "<thinking>first</thinking>Part one. <thin",
            "king>second</thin",
            "king>Part two.",
        ],
    )

    assert "first" not in result.streamed
    assert "second" not in result.streamed
    assert result.streamed == "Part one. Part two."
    assert result.returned == "Part one. Part two."


# --- the released-text-is-final property ------------------------------------


def test_what_has_been_streamed_is_always_a_prefix_of_the_final_answer(processor):
    """
    Nothing sent to the browser is ever revised, at any point in any chunking.

    This is the property that makes the defect structurally impossible rather than
    handled: the UI *appends* each ``assistant_stream`` delta, so it has no way to
    take one back, and the old code's "displayed" marker was a character count into
    a buffer that got shorter when a closing tag arrived. Asserting after every
    individual delta, not just at the end, is what distinguishes "only grows" from
    "ends up correct".
    """
    for pieces in (1, 2, 3):
        for chunks in splits(RESPONSE, pieces):
            result = drive(processor, chunks)
            accumulated = ""
            for delta in result.deltas:
                accumulated += delta
                assert ANSWER.startswith(accumulated), (chunks, result.deltas)
            assert accumulated == result.final


# --- the no-block path, and text that merely looks like a tag ---------------


@pytest.mark.parametrize(
    "text",
    [
        "The answer is 42, and here is why.",
        "  5 < 6, use <b>bold</b> and <thin ice>.  ",
        "The opener is <think",
        "Ends with a lone <",
        "a\n\n<thinking not a tag\n\nb",
    ],
)
def test_a_response_with_no_block_is_delivered_unchanged(processor, text):
    """
    A block-free response reaches the browser exactly as the old code sent it.

    The closed form is `text.strip()`: the old code emitted the growth of
    `re.sub(...).strip()` over the accumulated buffer, and with no block to remove
    those increments concatenate to the stripped whole. So asserting against
    `text.strip()` at every chunking *is* the differential against the previous
    behaviour, expressed without keeping a copy of it.

    The four inputs after the first are the ones that could regress while ordinary
    prose stayed fine, because withholding a possible tag prefix is what the fix
    adds: text containing `<`, an unrelated `<b>` tag, a `<thin`/`<thinking`
    lookalike, and a response *ending* mid-lookalike, whose held characters no
    chunk will complete and which must therefore still arrive.

    (Measured offline against the pre-fix module itself over 562 chunkings of the
    first input and 1508 of the rest: zero differences in the concatenated deltas,
    the final message or the persisted return value.)
    """
    expected = text.strip()
    for pieces in (1, 2, 3):
        for chunks in splits(text, pieces):
            result = drive(processor, chunks)
            assert result.streamed == expected, chunks
            assert result.final == expected, chunks
            assert result.returned == expected, chunks


@pytest.mark.parametrize(
    "reasoning",
    [
        "the stream died here",
        # Ends on a proper prefix of the closing tag, which is the *only* input
        # shape that reaches the `self._inside` guard in `close`. With reasoning
        # ending on any other character the filter is holding nothing by then, so
        # the guard is never consulted and deleting it changes no output -- that
        # was measured, with the case above as the sole input, and the deletion
        # survived the whole suite green. What it would release is bounded (at most
        # ten characters, always a proper prefix of `</thinking>`) but it is
        # reasoning the model did not intend to show, and it is the decision this
        # test claims to hold.
        "secret plan</th",
    ],
)
def test_an_unterminated_block_is_never_streamed(processor, reasoning):
    """
    A stream that stops inside a block shows the text before it and nothing more.

    A decision rather than a consequence: an agent that stops mid-thought has no
    answer to show, so the choice is between showing the reasoning and showing
    nothing, and showing the reasoning is the failure being fixed.
    """
    result = drive(processor, ["Checking. ", f"<thinking>{reasoning}"])

    assert reasoning not in result.streamed
    assert "</th" not in result.streamed
    assert result.streamed == "Checking."
    assert result.returned == "Checking."


# --- the stream that never delivers a result event --------------------------


@pytest.mark.parametrize("split", range(1, len(RESPONSE)))
def test_a_stream_that_ends_without_a_result_event_still_persists_the_answer(
    processor, split
):
    """
    The durable half of the defect: what gets written to the chat history table.

    `handler` passes this function's return value to `_persist_chat_turn`, and the
    `force_stop` branch `continue`s, so the generator can run out without a
    "result" event ever arriving -- in which case nothing re-derived the text and
    the leaked value was stored. Measured on the pre-fix module: 564 of the 631
    one-, two- and three-chunk splits persisted something other than the answer.

    It also pins that the held tail is released on this path, which is a separate
    line of the fix from the release in the "result" branch.
    """
    result = drive(processor, [RESPONSE[:split], RESPONSE[split:]], with_result=False)

    assert REASONING not in result.returned
    assert result.returned == ANSWER
    assert result.final is None


def test_a_stream_ending_mid_lookalike_with_no_result_event_keeps_its_tail(processor):
    """
    The two release sites are separately load-bearing, and this is the second one.

    A held tail and a missing "result" event have to coincide for the release after
    the loop to matter: with a result event the branch there already drained the
    filter, and with a response that ends on a decided character there is nothing
    held. So the sweep above passes with that line deleted -- measured -- and this
    is what fails instead. The stored answer would otherwise be six characters
    short with nothing to indicate it.
    """
    result = drive(processor, ["Answer <think"], with_result=False)

    assert result.streamed == "Answer <think"
    assert result.returned == "Answer <think"


def test_the_held_tail_is_released_exactly_once(processor):
    """
    A response ending mid-lookalike emits its tail once, not twice.

    Both the "result" branch and the line after the loop release the held text --
    the loop's `break` means the normal path runs both -- so `close` being
    idempotent for the value it returns is load-bearing: a second release would
    duplicate the last few characters of every answer ending in `<think`.
    """
    result = drive(processor, ["The opener is <think"])

    assert result.streamed == "The opener is <think"
    # Two events, not three: the text and the released tail. A non-idempotent
    # `close` would add a third carrying the tail again.
    assert len(result.deltas) == 2, result.deltas
    assert result.returned == "The opener is <think"


@pytest.mark.parametrize(
    "chunks",
    [
        ["Answer"],
        # The tail release is a second publish site and takes its own arguments,
        # so it is asserted on an input that produces one.
        ["The opener is <think"],
        ["<thinking>reason", "ing</thinking>Answer"],
    ],
)
def test_every_streamed_delta_is_marked_still_processing(processor, chunks):
    """
    `is_processing` on an `assistant_stream` delta decides how it is *rendered*.

    `app.py` puts the flag on the SSE frame as `isProcessing` beside
    `role: "assistant"`, and `use-agent-chat.ts` treats `not isProcessing` on an
    assistant message as a final response -- a branch that **overwrites** the
    bubble rather than appending to it. So a delta sent with `False` does not
    merely hide a spinner: it replaces the whole rendered answer with that delta's
    text. For the tail release, whose delta is at most ten characters, that would
    leave a ten-character answer on screen.

    Every delta must therefore say the turn is still in progress, including the
    one the tail release publishes. Nothing else in this file reads the flag, and
    with it unread the tail release could be flipped to `False` with all 270 tests
    green -- measured.
    """
    result = drive(processor, chunks)

    assert result.processing_flags, "expected at least one streamed delta"
    assert all(result.processing_flags), result.processing_flags


# --- the filter in isolation -------------------------------------------------


def test_the_withheld_run_is_exactly_what_cannot_yet_be_classified(processor):
    """
    `_longest_partial_tag_suffix` holds back a proper tag prefix and nothing else.

    Asserted directly because it is where an off-by-one lives, and the two
    directions are not symmetric. Holding one character too few releases the last
    character of a tag as text, which is the leak, and every sweep above catches
    it. Holding one character too *many* -- searching up to `len(tag)` rather than
    `len(tag) - 1` -- is invisible through `feed`, because the function is only
    reached when `find(tag)` already returned -1 and so `text` cannot end with the
    whole tag. The last assertion here is the only place that direction is
    observable: it passes a text that does end with the tag, which `feed` never
    produces, and pins that a *proper* prefix is what the contract means.
    """
    assert processor._longest_partial_tag_suffix("x<think", "<thinking>") == 6
    assert processor._longest_partial_tag_suffix("x<thinking", "<thinking>") == 9
    assert processor._longest_partial_tag_suffix("x</thinking", "</thinking>") == 10
    assert processor._longest_partial_tag_suffix("x<b>", "<thinking>") == 0
    assert processor._longest_partial_tag_suffix("", "<thinking>") == 0
    assert processor._longest_partial_tag_suffix("x<thinking>", "<thinking>") == 0


def test_the_filter_is_bounded_by_the_longer_tag(processor):
    """
    Held state never exceeds `len("</thinking>") - 1`, whatever the stream does.

    The old code kept the whole response in memory to re-clean it on every chunk;
    this replaces that with a fixed bound, which is the reason a cursor into a
    shrinking buffer no longer exists.
    """
    filt = processor.ThinkingFilter()
    for chunk in ("<thinking>", "a" * 500, "</thinking>", "answer", "<thinkin"):
        filt.feed(chunk)
        assert len(filt._held) <= len("</thinking>") - 1
