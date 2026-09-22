# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `RuleValidationService`'s text chunkers.

These split a document into the pieces each fact-extraction model call sees, so a
chunker that drops a region produces a confident "Information Not Found" for a rule
whose evidence was in the part that went missing. Nothing downstream can distinguish
that from the evidence genuinely being absent.

## Why this module caps memory, and runs one function in a subprocess

`_chunk_text_with_overlap` advances by `chunk_size_chars - overlap_chars` per pass and
stops as soon as a chunk reaches the end of the text. Both halves of that are
load-bearing and neither is locally obvious, so the termination cases are pinned
rather than assumed:

- **The stop has to be decided before `start` moves.** `end` is clamped to
  `len(text)`, so a `start` recomputed from a clamped `end` lands back inside the text
  for any non-zero overlap, and the same tail slice is emitted indefinitely. That was
  [#1090](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1090).
- **The stride has to be positive.** `overlap_percentage` is allowed to be 100, which
  would make it zero.

A run that does not terminate here is not an ordinary test failure: the loop appends a
slice per pass, so it exhausts memory rather than time, and `pytest-timeout`'s thread
method cannot interrupt a tight C-level loop. This module therefore never lets the
chunker allocate without a ceiling. Two mechanisms, for two different needs:

- `_run_isolated` forks a **subprocess** under `RLIMIT_AS` with a wall-clock timeout,
  and reports "did-not-return" for a child that dies or is killed. That is how a case
  can assert termination without the caller having to survive non-termination.
- `capped_address_space` is autouse over the whole module: it caps **this** process's
  address space for the duration of every test, so an in-process call that regresses
  raises `MemoryError` instead of consuming the machine. Previously the in-process
  tests were safe only because production keeps unmarked text out of the character
  chunker's fallback — a property of the code under test, not of the inputs, and so
  not something a test should rest on.

`_chunk_pages_with_overlap` terminates for all inputs — every branch of its loop
either advances the page index or empties the pending chunk so the next pass does —
and is exercised directly.

## What the page chunker is asserted on

**Nothing is lost.** Every page's content must appear in some chunk. Asserted as
content coverage rather than an exact string, because overlap deliberately repeats
material.

**Page markers stay attached to their own page.** `supporting_pages` in the published
report is derived from these markers, and a wrong page citation in a compliance report
is worse than no citation.

**Overlap follows the documented strategy.** If the previous chunk held several
complete pages the whole last page is repeated; if it held one page, a percentage of
it is. The single-page branch is the one that runs on documents whose pages are large
relative to the chunk budget.

`overlap_percentage` is an integer percentage (0-100), not a fraction: the shipped
default in `base-rule-validation.yaml` is `10` and the Pydantic field declares `int`
with `ge=0, le=100`. Both chunkers divide by 100. Tests state the value they pass so
that contract is visible.
"""

from __future__ import annotations

import multiprocessing
import os
import re
import resource

import pytest

from idp_common.rule_validation.service import RuleValidationService

#: Address-space budget and wall-clock budget for a chunking call. 256 MiB is far
#: more than any correct chunking of these inputs needs, and small enough that a
#: runaway is stopped quickly. The chunker's own working set for these inputs is
#: a few kilobytes, so this is generous by four orders of magnitude.
_CHUNKER_MEMORY_BYTES = 256 * 1024 * 1024
_ISOLATED_TIMEOUT_SECONDS = 20


def _address_space_in_use() -> int | None:
    """Bytes of virtual address space this process has mapped, or None if unknown.

    `RLIMIT_AS` is measured against the whole mapping, not against what a single
    call allocates, so the figure already in use is what a ceiling has to be
    expressed relative to. Read from /proc; returns None where that is unavailable.
    """
    try:
        with open("/proc/self/statm") as handle:
            pages = int(handle.read().split()[0])
    except (OSError, IndexError, ValueError):
        return None
    return pages * os.sysconf("SC_PAGE_SIZE")


@pytest.fixture(autouse=True)
def capped_address_space():
    """Bound this process's address space for every test in this module.

    The chunkers are loops that append a slice per pass, so a regression in one
    consumes memory rather than time and no timeout can catch it. An uncapped
    in-process call is therefore not a safe thing to make, whatever the input.

    The cap is `_CHUNKER_MEMORY_BYTES` of **headroom** rather than a flat ceiling:
    the interpreter running the suite has already mapped more than that, so a flat
    256 MiB would fail the next allocation of any kind. The child in `_run_isolated`
    can and does use the flat figure, because it is forked before the test body
    allocates anything. Either way the code under test gets the same 256 MiB, which
    is four orders of magnitude above its real working set here.
    """
    in_use = _address_space_in_use()
    if in_use is None:
        pytest.skip(
            "cannot read this process's address-space usage, so an in-process "
            "chunker call cannot be capped"
        )
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    ceiling = in_use + _CHUNKER_MEMORY_BYTES
    if hard != resource.RLIM_INFINITY:
        ceiling = min(ceiling, hard)
    resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


def _service() -> RuleValidationService:
    """A service instance without its constructor's config and AWS work.

    The chunkers read nothing off `self` — every knob is a parameter — so building
    the real service would load configuration and a Bedrock client for no benefit.
    """
    return RuleValidationService.__new__(RuleValidationService)


def _chunk_text_worker(
    result_path, text, max_chunk_size, token_size, overlap_percentage
):
    """Run the character chunker under an address-space limit. Child process only.

    The chunk count is written to a file rather than put on a multiprocessing.Queue:
    a Queue spawns a feeder thread on first use, and under a tight RLIMIT_AS that
    allocation is itself what fails, so a runaway child could not report anything.
    A plain write needs no new thread.
    """
    resource.setrlimit(
        resource.RLIMIT_AS, (_CHUNKER_MEMORY_BYTES, _CHUNKER_MEMORY_BYTES)
    )
    chunks = _service()._chunk_text_with_overlap(
        text, max_chunk_size, token_size, overlap_percentage
    )
    with open(result_path, "w") as handle:
        handle.write(str(len(chunks)))


def _run_isolated(text, max_chunk_size, token_size, overlap_percentage):
    """Call the character chunker in a killable subprocess.

    Returns ("returned", chunk_count) only when the child exited cleanly AND wrote a
    result. Anything else — a non-zero exit from MemoryError, or a kill at the
    timeout — is ("did-not-return", None). Both are evidence the loop never
    finished, and neither can affect this process.
    """
    import tempfile

    context = multiprocessing.get_context("fork")
    handle, result_path = tempfile.mkstemp(prefix="chunk-result-")
    os.close(handle)
    try:
        process = context.Process(
            target=_chunk_text_worker,
            args=(result_path, text, max_chunk_size, token_size, overlap_percentage),
        )
        process.start()
        process.join(_ISOLATED_TIMEOUT_SECONDS)
        if process.is_alive():
            process.kill()
            process.join()
            return ("did-not-return", None)
        if process.exitcode != 0:
            return ("did-not-return", None)
        with open(result_path) as read_handle:
            content = read_handle.read().strip()
        return ("returned", int(content)) if content else ("did-not-return", None)
    finally:
        os.unlink(result_path)


def _positional_text(length: int) -> str:
    """Text in which no substring of eight or more characters repeats.

    The character chunker returns slices with no record of where they came from, so
    the offsets have to be recovered by searching. Repeated filler would make that
    ambiguous, and the offsets are what the geometry tests are about.
    """
    records = "".join(f"{index:07d}|" for index in range((length // 8) + 1))
    return records[:length]


def _slice_offsets(text: str, chunks: list[str]) -> list[tuple[int, int]]:
    """Recover each chunk's (start, end) offset in the source text."""
    offsets = []
    search_from = 0
    for chunk in chunks:
        assert len(chunk) >= 8, (
            "a chunk this short cannot be located unambiguously; choose a text "
            "length that does not leave a tiny tail"
        )
        start = text.index(chunk, search_from)
        offsets.append((start, start + len(chunk)))
        search_from = start + 1
    return offsets


def _paged(*pages: tuple[int, str]) -> str:
    """Render (page_number, content) pairs the way OCR output arrives."""
    return "\n\n".join(
        f"<page-number>{number}</page-number>\n{content}" for number, content in pages
    )


def _markers(chunk: str) -> list[str]:
    return re.findall(r"<page-number>(\d+)</page-number>", chunk)


@pytest.mark.unit
class TestTheAddressSpaceCap:
    """That `capped_address_space` is actually in force.

    Every in-process chunker call in this module is safe to make only because it is.
    A fixture that silently stopped applying — a rename, a lost `autouse`, a platform
    where the usage cannot be read — would leave all of them able to consume the
    machine, and nothing else here would fail.
    """

    def test_an_allocation_past_the_cap_raises_rather_than_succeeding(self):
        with pytest.raises(MemoryError):
            bytearray(10 * _CHUNKER_MEMORY_BYTES)

    def test_the_cap_is_no_looser_than_the_documented_budget(self):
        # A finite limit is not enough on its own: one set far above the headroom the
        # fixture documents would pass the test above and still let a runaway run for
        # a long time. The ceiling has to be the stated 256 MiB of headroom.
        soft, _ = resource.getrlimit(resource.RLIMIT_AS)
        assert soft != resource.RLIM_INFINITY, "the cap should be in force here"
        in_use = _address_space_in_use()
        assert in_use is not None
        assert soft <= in_use + _CHUNKER_MEMORY_BYTES


@pytest.mark.unit
class TestChunkTextContent:
    """_chunk_text_with_overlap: what comes back, asserted in-process.

    Safe to call directly because `capped_address_space` bounds this process for the
    whole module, so a chunker that stopped terminating would raise `MemoryError`
    here rather than consuming the machine. That termination is asserted separately,
    on a harness that survives the answer being no — see `TestChunkTextTermination`.
    """

    def test_text_within_the_budget_is_returned_whole(self):
        # One chunk means one model call; splitting a document that fits would double
        # the cost and add a boundary for no reason.
        assert _service()._chunk_text_with_overlap("short text", 1000, 4, 10) == [
            "short text"
        ]

    def test_text_at_exactly_the_budget_is_returned_whole(self):
        # estimated_tokens == max_chunk_size takes the early return. Pinned because
        # flipping that comparison would split every document that exactly fits, for
        # no benefit and at the cost of a boundary through it.
        text = "x" * 400
        assert _service()._chunk_text_with_overlap(text, 100, 4, 10) == [text]

    def test_text_just_under_the_budget_is_returned_whole(self):
        text = "x" * 399
        assert _service()._chunk_text_with_overlap(text, 100, 4, 10) == [text]

    def test_zero_overlap_splits_into_adjacent_non_repeating_chunks(self):
        text = "".join(chr(ord("a") + i % 26) for i in range(2000))
        chunks = _service()._chunk_text_with_overlap(text, 100, 4, 0)
        assert "".join(chunks) == text
        assert len(chunks) == 5

    def test_zero_overlap_loses_no_characters_on_a_ragged_length(self):
        # 2050 is not a multiple of the 400-character chunk size, so the last chunk
        # is short. With no overlap that is still terminal.
        text = "".join(chr(ord("a") + i % 26) for i in range(2050))
        assert "".join(_service()._chunk_text_with_overlap(text, 100, 4, 0)) == text

    def test_no_chunk_exceeds_the_character_budget(self):
        # The budget is what keeps a request under the model's context limit, so an
        # oversized chunk is a failed call rather than a degraded one.
        chunks = _service()._chunk_text_with_overlap("x" * 4000, 100, 4, 0)
        assert all(len(chunk) <= 400 for chunk in chunks)

    def test_the_token_size_scales_the_character_budget(self):
        # token_size is characters-per-token, so doubling it doubles how much text
        # fits in the same token budget.
        text = "x" * 8000
        narrow = _service()._chunk_text_with_overlap(text, 100, 2, 0)
        wide = _service()._chunk_text_with_overlap(text, 100, 8, 0)
        assert len(narrow) > len(wide)

    def test_empty_text_returns_a_single_empty_chunk(self):
        # Differs from the page chunker, which returns []. Pinned because the two are
        # used interchangeably at the fallback boundary.
        assert _service()._chunk_text_with_overlap("", 100, 4, 10) == [""]


@pytest.mark.unit
class TestChunkTextTermination:
    """_chunk_text_with_overlap returns, for ragged lengths and extreme overlaps.

    Asserted from a forked child under a flat 256 MiB address-space cap and a
    wall-clock timeout. A caller cannot assert "this returns" directly, because the
    interesting failure is that it does not: `_run_isolated` answers
    "did-not-return" for a child that dies or is killed, and this process is
    unaffected either way.
    """

    def test_zero_overlap_returns_promptly_in_isolation(self):
        # Establishes that the harness itself works, so a "did-not-return" from any
        # case below is the function's behaviour and not a broken fixture.
        assert _run_isolated("x" * 4000, 100, 4, 0) == ("returned", 10)

    @pytest.mark.parametrize(
        "case,text_length,max_chunk_size,token_size,overlap",
        [
            # Ragged: the final chunk is shorter than the budget, so `end` clamps to
            # len(text) on the last pass. A `start` recomputed from a clamped `end`
            # lands back inside the text for any non-zero overlap, which is why the
            # loop decides to stop before moving `start` rather than after.
            ("ragged tail, 10% overlap", 4000, 100, 4, 10),
            ("ragged tail, 5% overlap", 8000, 100, 4, 5),
            ("ragged tail, 40-character chunks", 5000, 10, 4, 10),
            # A text barely longer than one chunk: the shortest tail the loop can
            # produce, reached on its second pass.
            ("minimal tail, 50% overlap", 405, 100, 4, 50),
            ("minimal tail, 99% overlap", 405, 100, 4, 99),
            # overlap_percentage is documented as 0-100, and 100 makes the requested
            # overlap the whole chunk — a stride of zero before it is bounded below.
            ("overlap equal to the chunk size", 4000, 100, 4, 100),
            ("overlap equal to the chunk size, minimal tail", 405, 100, 4, 100),
            # Empty input, the other end of the range: the early return.
            ("empty text", 0, 100, 4, 10),
        ],
    )
    def test_it_returns(self, case, text_length, max_chunk_size, token_size, overlap):
        outcome, count = _run_isolated(
            "x" * text_length, max_chunk_size, token_size, overlap
        )
        assert outcome == "returned", f"{case}: the chunker did not return"
        assert count is not None and count >= 1, f"{case}: returned no chunks at all"

    def test_an_exact_multiple_of_the_chunk_size_also_returns(self):
        # 4000 characters is exactly 100 chunks of 40, and that is not a special
        # case: a 10% overlap makes the stride 36, so `end` clamps to len(text) here
        # just as it does on a ragged length. Pinned because "only ragged lengths
        # are at risk" is the intuitive and wrong reading.
        outcome, _ = _run_isolated("x" * 4000, 10, 4, 10)
        assert outcome == "returned"

    def test_an_overlap_that_floors_to_zero_characters_returns(self):
        # A percentage can round down to no characters at all: int(40 * 1/100) is 0,
        # so a 1% overlap on a 40-character chunk repeats nothing while the same 1%
        # on the shipped 32,000-character chunk repeats 320 characters.
        assert _run_isolated("x" * 4000, 10, 4, 1) == ("returned", 100)


@pytest.mark.unit
class TestChunkTextOverlapGeometry:
    """Where _chunk_text_with_overlap's chunks sit in the source text.

    The chunks are contiguous slices, so their offsets can be recovered and the
    geometry stated directly — full coverage, no gap, and a start that always
    advances. These run at overlaps above zero, which the concatenation-based
    assertions above cannot read: with overlap the chunks deliberately do not join
    up, so "nothing was lost" has to be asked of the offsets instead of the text.
    """

    @pytest.mark.parametrize("overlap", [0, 10, 25, 50])
    def test_the_chunks_cover_the_whole_text_with_no_gap(self, overlap):
        # A gap is a region of the document that reaches no model call, which
        # produces a confident "Information Not Found" for any rule whose evidence
        # was in it.
        text = _positional_text(4096)
        offsets = _slice_offsets(
            text, _service()._chunk_text_with_overlap(text, 100, 4, overlap)
        )
        assert offsets[0][0] == 0, "the first chunk must start at the beginning"
        assert offsets[-1][1] == len(text), "the last chunk must reach the end"
        for (_, previous_end), (start, _) in zip(offsets, offsets[1:]):
            assert start <= previous_end, "a region of the text is in no chunk"

    @pytest.mark.parametrize("overlap", [0, 10, 25, 50, 100])
    def test_every_chunk_starts_later_than_the_one_before(self, overlap):
        # The property a stalled loop violates: it reissues one slice, so its starts
        # stop increasing. Including 100, where the requested overlap is the whole
        # chunk and the stride would be zero if it were taken literally.
        text = _positional_text(4096)
        offsets = _slice_offsets(
            text, _service()._chunk_text_with_overlap(text, 100, 4, overlap)
        )
        starts = [start for start, _ in offsets]
        assert starts == sorted(set(starts))

    @pytest.mark.parametrize("overlap", [0, 10, 50, 100])
    def test_no_chunk_exceeds_the_budget_at_any_overlap(self, overlap):
        text = _positional_text(4096)
        chunks = _service()._chunk_text_with_overlap(text, 100, 4, overlap)
        assert all(len(chunk) <= 400 for chunk in chunks)

    def test_consecutive_chunks_share_the_requested_share_of_a_chunk(self):
        # 10% of a 400-character chunk is 40 characters, so each chunk begins 40
        # characters before the previous one ended. That repeated region is the whole
        # point of the setting: a fact split across a boundary stays readable.
        text = _positional_text(4096)
        offsets = _slice_offsets(
            text, _service()._chunk_text_with_overlap(text, 100, 4, 10)
        )
        for (_, previous_end), (start, _) in zip(offsets, offsets[1:]):
            assert start == previous_end - 40

    def test_zero_overlap_repeats_nothing(self):
        # The counterpart of the page chunker's zero-overlap case: asking for no
        # overlap has to mean no repeated region, not a maximal one.
        text = _positional_text(4096)
        offsets = _slice_offsets(
            text, _service()._chunk_text_with_overlap(text, 100, 4, 0)
        )
        for (_, previous_end), (start, _) in zip(offsets, offsets[1:]):
            assert start == previous_end

    def test_the_final_chunk_is_never_shorter_than_the_overlap(self):
        # Worth stating because it is not obvious and it bounds the ragged case: the
        # pass before the last had `start + chunk_size_chars < len(text)`, so the
        # tail that remains is longer than chunk_size_chars - overlap_chars's
        # complement. A final chunk shorter than the overlap is therefore not a
        # shape this loop can produce, however ragged the length.
        for length in (405, 500, 799, 4000, 4096, 4399):
            text = _positional_text(length)
            chunks = _service()._chunk_text_with_overlap(text, 100, 4, 50)
            assert len(chunks[-1]) > 200 or len(chunks) == 1, length


@pytest.mark.unit
class TestChunkPagesEarlyReturns:
    """_chunk_pages_with_overlap: the cases that never reach page parsing."""

    def test_empty_text_returns_no_chunks(self):
        # Zero chunks means zero model calls, which is right for a document with no
        # text. The character chunker returns [""] for the same input.
        assert _service()._chunk_pages_with_overlap("", 100, 4, 10) == []

    def test_whitespace_only_text_returns_no_chunks(self):
        assert _service()._chunk_pages_with_overlap("   \n\n  ", 100, 4, 10) == []

    def test_text_within_the_budget_is_returned_whole_with_its_markers(self):
        text = _paged((1, "alpha"), (2, "beta"))
        assert _service()._chunk_pages_with_overlap(text, 1000, 4, 10) == [text]

    def test_unmarked_text_becomes_one_page_rather_than_taking_the_fallback(self):
        # This is the reachability fact behind #1090's low severity: `re.split`
        # leaves unmarked text as parts[0], and the pre-page branch assigns it page
        # "0", so `pages` is non-empty and the character fallback is NOT entered.
        # Were it otherwise, every long unmarked document would hang.
        text = "y" * 4000
        chunks = _service()._chunk_pages_with_overlap(text, 100, 4, 10)
        assert chunks, "unmarked text must still be chunked"
        assert all(_markers(chunk) == [] for chunk in chunks)

    def test_an_oversized_unmarked_document_becomes_one_chunk_over_the_budget(self):
        # Two facts combine here and the result is worth stating plainly.
        #
        # Unmarked text becomes a single page (numbered "0"), and the page chunker
        # cannot split a page — its oversized branch emits the page whole. So a
        # 40,000-character unmarked document against a 400-character budget comes
        # back as ONE chunk of 40,000 characters: `max_chunk_size` has no effect on
        # unmarked text at all.
        #
        # The character-based chunker that would split it is unreachable for this
        # input (the fallback needs `pages` to be empty), and would not terminate if
        # it were reached — #1090. The upside is that it terminates; the downside is
        # that a long unmarked document is sent to the model in one prompt.
        chunks = _service()._chunk_pages_with_overlap("y" * 40000, 100, 4, 10)
        assert len(chunks) == 1
        assert len(chunks[0]) == 40000


@pytest.mark.unit
class TestChunkPagesGrouping:
    """_chunk_pages_with_overlap: how pages are grouped into chunks."""

    def test_pages_that_fit_together_share_a_chunk(self):
        text = _paged((1, "a" * 200), (2, "b" * 200))
        chunks = _service()._chunk_pages_with_overlap(text, 200, 4, 10)
        assert len(chunks) == 1
        assert _markers(chunks[0]) == ["1", "2"]

    def test_pages_that_do_not_fit_together_are_split(self):
        text = _paged((1, "a" * 800), (2, "b" * 800))
        assert len(_service()._chunk_pages_with_overlap(text, 250, 4, 10)) >= 2

    def test_every_page_appears_in_at_least_one_chunk(self):
        # The property a dropped page violates. A rule whose evidence is on page 3
        # would be reported unevaluable with no indication why.
        text = _paged(*[(n, f"page{n}content" + "z" * 400) for n in range(1, 7)])
        chunks = _service()._chunk_pages_with_overlap(text, 200, 4, 10)
        seen = {marker for chunk in chunks for marker in _markers(chunk)}
        assert seen == {"1", "2", "3", "4", "5", "6"}

    def test_every_pages_content_survives_somewhere(self):
        text = _paged(*[(n, f"UNIQUE{n}MARKER" + "z" * 400) for n in range(1, 7)])
        joined = "".join(_service()._chunk_pages_with_overlap(text, 200, 4, 10))
        for n in range(1, 7):
            assert f"UNIQUE{n}MARKER" in joined

    def test_each_marker_precedes_its_own_content(self):
        # The marker is what `supporting_pages` is derived from downstream, so a
        # marker attached to the wrong content produces a wrong page citation.
        text = _paged(*[(n, f"CONTENT{n}" + "z" * 400) for n in range(1, 5)])
        for chunk in _service()._chunk_pages_with_overlap(text, 200, 4, 10):
            for match in re.finditer(
                r"<page-number>(\d+)</page-number>\s*(.*?)(?=<page-number>|\Z)",
                chunk,
                re.DOTALL,
            ):
                page_number, body = match.group(1), match.group(2)
                if "CONTENT" in body:
                    assert f"CONTENT{page_number}" in body

    def test_a_single_page_larger_than_the_budget_becomes_its_own_chunk(self):
        # It cannot be made to fit, and dropping it would lose the page, so an
        # oversized chunk is the least-bad outcome and must at least happen.
        text = _paged((1, "a" * 100), (2, "b" * 4000))
        chunks = _service()._chunk_pages_with_overlap(text, 100, 4, 10)
        assert any("2" in _markers(chunk) for chunk in chunks)
        assert any(len(chunk) > 400 for chunk in chunks)

    def test_content_before_the_first_marker_is_kept(self):
        # Pre-page preamble is assigned page "0" internally and emitted without a
        # marker; dropping it would silently lose a cover page.
        text = "PREAMBLE_MARKER\n\n" + _paged((1, "a" * 100))
        assert "PREAMBLE_MARKER" in "".join(
            _service()._chunk_pages_with_overlap(text, 1000, 4, 10)
        )

    def test_pre_page_content_is_not_given_a_page_marker(self):
        text = "PREAMBLE_MARKER" + "z" * 900 + "\n\n" + _paged((1, "a" * 900))
        chunks = _service()._chunk_pages_with_overlap(text, 200, 4, 10)
        preamble_chunk = next(c for c in chunks if "PREAMBLE_MARKER" in c)
        assert "0" not in _markers(preamble_chunk)

    def test_an_empty_page_is_skipped_rather_than_emitted_bare(self):
        # A marker with no content would spend prompt tokens and could read as a
        # blank page in the document.
        text = "<page-number>1</page-number>\n\n<page-number>2</page-number>\nreal"
        assert _service()._chunk_pages_with_overlap(text, 1000, 4, 10) == [text]


@pytest.mark.unit
class TestChunkPagesOverlap:
    """_chunk_pages_with_overlap: the two overlap strategies."""

    def test_a_multi_page_previous_chunk_repeats_its_whole_last_page(self):
        # Repeating the complete page keeps a fact that spans the boundary readable
        # in the next chunk; a partial page could split a table row.
        text = _paged(*[(n, f"PAGE{n}" + "z" * 300) for n in range(1, 6)])
        chunks = _service()._chunk_pages_with_overlap(text, 200, 4, 10)
        assert len(chunks) >= 2
        first_markers = _markers(chunks[0])
        assert len(first_markers) > 1
        assert first_markers[-1] == _markers(chunks[1])[0]

    def test_the_repeated_page_carries_its_full_content(self):
        text = _paged(*[(n, f"PAGE{n}" + "z" * 300) for n in range(1, 6)])
        chunks = _service()._chunk_pages_with_overlap(text, 200, 4, 10)
        assert f"PAGE{_markers(chunks[0])[-1]}" in chunks[1]

    def test_a_single_page_previous_chunk_repeats_only_a_percentage(self):
        # Pages large relative to the budget take this branch; repeating the whole
        # page would make every chunk twice the intended size.
        text = _paged(*[(n, f"PAGE{n}" + "z" * 1200) for n in range(1, 4)])
        chunks = _service()._chunk_pages_with_overlap(text, 200, 4, 10)
        assert len(chunks) >= 2
        assert len(_markers(chunks[0])) == 1
        assert len(chunks[1]) < len(chunks[0]) * 2

    def test_a_larger_overlap_percentage_repeats_more_of_a_single_page(self):
        text = _paged(*[(n, f"PAGE{n}" + "z" * 1200) for n in range(1, 4)])
        small = _service()._chunk_pages_with_overlap(text, 200, 4, 5)
        large = _service()._chunk_pages_with_overlap(text, 200, 4, 50)
        assert len(large[1]) > len(small[1])

    def test_zero_overlap_repeats_nothing_from_a_single_page(self):
        # Zero overlap has to mean no repeated page. The slice that takes the tail of
        # the previous page cannot be asked for zero characters — page_content[-0:]
        # is page_content[0:], the whole page — so the branch returns no overlap page
        # at all rather than an empty one.
        text = _paged(*[(n, f"PAGE{n}" + "z" * 1200) for n in range(1, 4)])
        chunks = _service()._chunk_pages_with_overlap(text, 200, 4, 0)
        assert _markers(chunks[1])[0] != _markers(chunks[0])[0]
        assert "PAGE1" not in chunks[1]

    def test_the_repeated_share_grows_monotonically_from_zero(self):
        # Zero is on the same curve as everything else, and that is the whole
        # behaviour worth pinning here: a percentage that repeats less must produce a
        # smaller chunk, with no discontinuity at the bottom of the range.
        text = _paged(*[(n, f"PAGE{n}" + "z" * 1200) for n in range(1, 4)])
        lengths = [
            len(_service()._chunk_pages_with_overlap(text, 200, 4, percentage)[1])
            for percentage in (0, 1, 10, 50, 100)
        ]
        assert lengths == sorted(lengths)
        assert lengths[0] < lengths[1], "zero overlap must repeat less than 1% does"

    def test_the_first_chunk_has_no_overlap_prepended(self):
        # There is no previous chunk; prepending anything would duplicate the start
        # of the document.
        text = _paged(*[(n, f"PAGE{n}" + "z" * 300) for n in range(1, 6)])
        chunks = _service()._chunk_pages_with_overlap(text, 200, 4, 10)
        assert _markers(chunks[0])[0] == "1"

    def test_the_repeated_page_still_appears_in_the_chunk_that_owns_it(self):
        text = _paged(*[(n, f"PAGE{n}" + "z" * 300) for n in range(1, 6)])
        chunks = _service()._chunk_pages_with_overlap(text, 200, 4, 10)
        assert f"PAGE{_markers(chunks[0])[-1]}" in chunks[0]


@pytest.mark.unit
class TestChunkPagesInvariants:
    """Properties that must hold across a range of shapes and budgets."""

    @pytest.mark.parametrize("page_count", [1, 2, 3, 7, 12])
    @pytest.mark.parametrize("page_size", [50, 400, 1500])
    @pytest.mark.parametrize("max_chunk_size", [100, 300])
    def test_no_page_is_ever_dropped(self, page_count, page_size, max_chunk_size):
        # A matrix rather than one case: the grouping loop has three exits (page
        # fits, chunk full, single page oversized) and which one runs depends on the
        # ratio of page size to budget, so one shape exercises only one of them.
        text = _paged(
            *[(n, f"P{n}X" + "z" * page_size) for n in range(1, page_count + 1)]
        )
        joined = "".join(
            _service()._chunk_pages_with_overlap(text, max_chunk_size, 4, 10)
        )
        for n in range(1, page_count + 1):
            assert f"P{n}X" in joined, f"page {n} was dropped"

    @pytest.mark.parametrize("page_count", [1, 3, 8])
    @pytest.mark.parametrize("overlap", [0, 10, 50])
    def test_chunks_are_never_empty(self, page_count, overlap):
        # An empty chunk is a model call with no document in it: a guaranteed
        # "Information Not Found" that costs a request.
        text = _paged(*[(n, f"P{n}" + "z" * 600) for n in range(1, page_count + 1)])
        chunks = _service()._chunk_pages_with_overlap(text, 200, 4, overlap)
        assert all(chunk.strip() for chunk in chunks)

    @pytest.mark.parametrize("page_count", [2, 5, 9])
    def test_page_markers_appear_in_ascending_order_within_a_chunk(self, page_count):
        # Out-of-order pages in one prompt make a multi-page fact unreadable, and
        # would mean the grouping loop had reordered the document.
        text = _paged(*[(n, f"P{n}" + "z" * 300) for n in range(1, page_count + 1)])
        for chunk in _service()._chunk_pages_with_overlap(text, 300, 4, 10):
            numbers = [int(m) for m in _markers(chunk)]
            assert numbers == sorted(numbers)

    def test_the_result_is_deterministic(self):
        # These boundaries decide which model call sees which evidence, so a
        # run-to-run difference would make a rule's verdict irreproducible.
        text = _paged(*[(n, f"P{n}" + "z" * 400) for n in range(1, 8)])
        assert _service()._chunk_pages_with_overlap(
            text, 200, 4, 10
        ) == _service()._chunk_pages_with_overlap(text, 200, 4, 10)
