# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `RuleValidationService`'s text chunkers.

These split a document into the pieces each fact-extraction model call sees, so a
chunker that drops a region produces a confident "Information Not Found" for a rule
whose evidence was in the part that went missing. Nothing downstream can distinguish
that from the evidence genuinely being absent.

## Why this module runs one function in a subprocess

`_chunk_text_with_overlap` **does not terminate** for most inputs — see
[#1090](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1090).
Once its `end` index clamps to `len(text)`, `start` is recomputed to
`len(text) - overlap_chars`, which is always less than `len(text)` when the overlap is
non-zero, so the loop is stationary and appends the same tail slice forever. An
earlier version of this file called it in-process; it allocated 81.6 GiB and stalled
the host for fourteen hours.

An in-process test cannot fail safely against that: `pytest-timeout`'s thread method
cannot interrupt a tight C-level loop, and a thread cannot be killed. So the cases
that pin the non-termination run in a **forked subprocess with an address-space
limit and a wall-clock timeout** (`_run_isolated` below). The worst outcome is a
failed assertion, not a dead machine.

The in-process tests only use inputs proven to terminate, and the condition is
narrower than it looks: `overlap_chars == 0`, which means either
`overlap_percentage == 0` or a percentage small enough that
`int(chunk_size_chars * pct / 100)` floors to zero. An exact multiple of the chunk size
does **not** help — 4,000 characters is exactly 100 chunks of 40 and still loops,
because the stride is 36 and `end` clamps regardless. `_chunk_pages_with_overlap`
terminates for all inputs — every branch of its loop either advances the page index
or empties the pending chunk so the next pass does — and is exercised directly.

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
import re

import pytest

from idp_common.rule_validation.service import RuleValidationService

#: Address-space cap and wall-clock budget for the isolated calls. 512 MiB is far
#: more than any correct chunking of these inputs needs, and small enough that a
#: runaway is killed quickly. The chunker's own working set for these inputs is
#: a few kilobytes, so this is generous by four orders of magnitude.
_ISOLATED_MEMORY_BYTES = 256 * 1024 * 1024
_ISOLATED_TIMEOUT_SECONDS = 20


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
    import resource

    resource.setrlimit(
        resource.RLIMIT_AS, (_ISOLATED_MEMORY_BYTES, _ISOLATED_MEMORY_BYTES)
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
    import os
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


def _paged(*pages: tuple[int, str]) -> str:
    """Render (page_number, content) pairs the way OCR output arrives."""
    return "\n\n".join(
        f"<page-number>{number}</page-number>\n{content}" for number, content in pages
    )


def _markers(chunk: str) -> list[str]:
    return re.findall(r"<page-number>(\d+)</page-number>", chunk)


@pytest.mark.unit
class TestChunkTextTerminatingInputs:
    """_chunk_text_with_overlap for the inputs that are known to terminate.

    These run in-process. Every case either takes the early return or has
    `overlap_chars == 0`, which are the only two shapes #1090 does not affect.
    """

    def test_text_within_the_budget_is_returned_whole(self):
        # One chunk means one model call; splitting a document that fits would double
        # the cost and add a boundary for no reason.
        assert _service()._chunk_text_with_overlap("short text", 1000, 4, 10) == [
            "short text"
        ]

    def test_text_at_exactly_the_budget_is_returned_whole(self):
        # estimated_tokens == max_chunk_size takes the early return. Pinned because
        # flipping that comparison would split every document at the boundary — and
        # send it into the non-terminating loop.
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
class TestChunkTextNonTermination:
    """The #1090 loop, pinned from a subprocess so it cannot take the host down."""

    def test_zero_overlap_returns_promptly_in_isolation(self):
        # Establishes that the harness itself works: the same call shape that hangs
        # below returns here, so a timeout in the next test is the function's
        # behaviour and not a broken fixture.
        assert _run_isolated("x" * 4000, 100, 4, 0) == ("returned", 10)

    @pytest.mark.xfail(
        strict=True,
        reason="_chunk_text_with_overlap does not terminate when the final chunk is "
        "short and the overlap is non-zero: once `end` clamps to len(text), `start` "
        "is recomputed to len(text) - overlap_chars, which never reaches len(text). "
        "The same tail slice is appended forever. See "
        "https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1090",
    )
    @pytest.mark.parametrize(
        "text_length,max_chunk_size,token_size,overlap",
        [
            (4000, 100, 4, 10),
            (8000, 100, 4, 5),
            (5000, 10, 4, 10),
        ],
    )
    def test_a_ragged_length_with_overlap_terminates(
        self, text_length, max_chunk_size, token_size, overlap
    ):
        # Run in a subprocess with a 512 MiB address-space cap: the loop allocates a
        # tail slice per iteration, so it dies on MemoryError or is killed at the
        # timeout. Either way this process is unaffected.
        outcome, _ = _run_isolated(
            "x" * text_length, max_chunk_size, token_size, overlap
        )
        assert outcome == "returned", (
            "expected the chunker to return; it did not. See issue #1090."
        )

    def test_an_exact_multiple_of_the_chunk_size_does_not_rescue_termination(self):
        # 4000 characters is exactly 100 chunks of 40, and it still does not
        # terminate: the stride is 36, so `end` clamps to len(text) anyway and
        # `start` is recomputed to len(text) - 4. Pinned because "it only breaks on
        # ragged lengths" is the intuitive and wrong reading of #1090.
        outcome, _ = _run_isolated("x" * 4000, 10, 4, 10)
        assert outcome == "did-not-return"

    def test_an_overlap_that_floors_to_zero_characters_terminates(self):
        # The precise condition is overlap_chars == 0, not overlap_percentage == 0:
        # int(40 * 1/100) is 0, so a 1% overlap on a 40-character chunk terminates
        # while a 1% overlap on the shipped 32,000-character chunk does not.
        assert _run_isolated("x" * 4000, 10, 4, 1) == ("returned", 100)


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

    @pytest.mark.xfail(
        strict=True,
        reason="overlap_percentage 0 repeats the ENTIRE previous page: overlap_size "
        "is 0 and page_content[-0:] is page_content[0:]. Zero overlap therefore "
        "produces the maximum overlap. See "
        "https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1091",
    )
    def test_zero_overlap_repeats_nothing_from_a_single_page(self):
        text = _paged(*[(n, f"PAGE{n}" + "z" * 1200) for n in range(1, 4)])
        chunks = _service()._chunk_pages_with_overlap(text, 200, 4, 0)
        assert _markers(chunks[1])[0] != _markers(chunks[0])[0]

    def test_a_one_percent_overlap_repeats_far_less_than_zero_does(self):
        # The monotonic relationship holds everywhere except at zero, which is what
        # makes #1091 surprising rather than merely wrong: 1% repeats 12 characters
        # of a 1,205-character page and 0% repeats all 1,205.
        text = _paged(*[(n, f"PAGE{n}" + "z" * 1200) for n in range(1, 4)])
        one_percent = _service()._chunk_pages_with_overlap(text, 200, 4, 1)
        zero = _service()._chunk_pages_with_overlap(text, 200, 4, 0)
        assert len(one_percent[1]) < len(zero[1])

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
