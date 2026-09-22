# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `RuleValidationService`'s text chunkers.

These split a document into the pieces each fact-extraction model call sees, so a
chunker that drops a region produces a confident "Information Not Found" for a rule
whose evidence was in the part that went missing. Nothing downstream can distinguish
that from the evidence genuinely being absent.

## Why every chunker call in this module runs under a memory ceiling

`_chunk_text_with_overlap` advances by `chunk_size_chars - overlap_chars` per pass and
stops as soon as a chunk reaches the end of the text. Both halves of that are
load-bearing and neither is locally obvious:

- **The stop has to be decided before `start` moves.** `end` is clamped to
  `len(text)`, so a `start` recomputed from a clamped `end` lands back inside the text
  for any non-zero overlap, and the same tail slice is emitted indefinitely. That was
  [#1090](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1090).
- **The stride has to be positive.** `overlap_percentage` may be 100, which asks for a
  stride of zero.

A loop of that shape fails by **exhausting memory rather than time** — it appends a
slice per pass — so no timeout can catch it: `pytest-timeout`'s thread method cannot
interrupt a tight C-level loop, and a thread cannot be killed. Nothing here is
therefore allowed to call a chunker without a ceiling, by either of two mechanisms:

- `_run_isolated` forks a **subprocess** under `RLIMIT_AS` with a wall-clock timeout.
  That is how a case can assert termination without the caller having to survive the
  answer being no. It names *which* non-answer it got — see `_OUTCOME_MEANINGS` —
  because "the loop is not advancing" and "a correct run wanted more memory than the
  ceiling allows" are different diagnoses and only one of them is a defect.
- `capped_address_space` is autouse over this module, so **this** process's address
  space is bounded around each test and an in-process regression raises `MemoryError`
  instead of consuming the machine.

⚠️ **Both ceilings are headroom over what is already mapped, computed in the process
they bound, not a flat figure.** That is not a refinement. `RLIMIT_AS` bounds the
whole mapping and `fork` copies it, so a flat 256 MiB in a child of a pytest process
that has already mapped ~300 MiB sets a limit **below current usage** — and
`setrlimit` succeeds when doing that, which is why nothing complained. What the
chunker then gets is whatever slack happens to exist inside already-mapped `pymalloc`
arenas: 9 MiB measured on one host, 1 MiB on another, varying with import order. An
incidental, irreproducible budget is worse than a wrong constant, and the first case
needing one fresh arena fails in a way that reads as #1090 regressing.

The page chunker is capped too, and the reason is worth stating because its own
inputs look safe: `_chunk_pages_with_overlap` falls through to the character chunker
whenever the page parser finds **no** pages, so what keeps these tests away from that
path is a property of the production code rather than of the text they pass. Its own
loop terminates for all inputs — every branch either advances the page index or
empties the pending chunk so the next pass does.

## What the page chunker is asserted on

**Nothing is lost.** Every page's content must appear in some chunk. Asserted as
content coverage rather than an exact string, because overlap deliberately repeats
material.

**Page markers stay attached to their own page.** `supporting_pages` in the published
report is derived from these markers, and a wrong page citation in a compliance report
is worse than no citation.

**Overlap follows the documented strategy.** If the previous chunk held several
complete pages the whole last page is repeated, and `overlap_percentage` is not
consulted at all; if it held one page, that percentage of it is. The single-page
branch is the one that runs on documents whose pages are large relative to the chunk
budget, so it is also the only page-chunker branch the setting reaches.

`overlap_percentage` is an integer percentage (0-100), not a fraction: the shipped
default in `base-rule-validation.yaml` is `10` and the Pydantic field declares `int`
with `ge=0, le=100`. Both chunkers divide by 100. Tests state the value they pass so
that contract is visible. The character chunker additionally bounds it at half a
chunk, because terminating is not the same as being affordable — a one-character
stride emits one model call per character.
"""

from __future__ import annotations

import mmap
import multiprocessing
import os
import re
import signal
import sys
import time

import pytest

from idp_common.rule_validation.service import RuleValidationService

#: POSIX only. `RLIMIT_AS` and the `fork` start method are both unavailable on
#: Windows, and no test in this module may run without a memory ceiling, so the
#: module reports itself skipped there rather than failing to collect.
resource = pytest.importorskip("resource", reason="RLIMIT_AS is POSIX only")

#: How much **fresh** address space a chunking call is allowed, over whatever the
#: calling process has already mapped. 256 MiB is far more than any correct chunking
#: of these inputs needs — their working set is a few kilobytes, so this is generous
#: by four orders of magnitude — and small enough that a runaway stops quickly.
#: Always applied as headroom: see the warning in the module docstring for why a flat
#: figure is not the same thing and silently leaves a child with none.
_CHUNKER_MEMORY_BYTES = 256 * 1024 * 1024
_ISOLATED_TIMEOUT_SECONDS = 20


def _address_space_in_use() -> int | None:
    """Bytes of virtual address space this process has mapped, or None if unknown.

    `RLIMIT_AS` bounds the whole mapping rather than what one call allocates, so the
    figure already in use is what a ceiling has to be expressed relative to. Read
    from /proc; None where that is unavailable, which is the one state in which no
    ceiling can be computed and so nothing here may run.
    """
    try:
        with open("/proc/self/statm") as handle:
            pages = int(handle.read().split()[0])
    except (OSError, IndexError, ValueError):
        return None
    return pages * os.sysconf("SC_PAGE_SIZE")


def _cap_address_space(in_use: int) -> tuple[int, int, int]:
    """Bound this process to `_CHUNKER_MEMORY_BYTES` over `in_use`.

    Returns `(ceiling, original_soft, hard)` so the caller can assert which ceiling
    was applied and restore what was there before.
    """
    original_soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    ceiling = in_use + _CHUNKER_MEMORY_BYTES
    if hard != resource.RLIM_INFINITY:
        ceiling = min(ceiling, hard)
    resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
    return ceiling, original_soft, hard


#: The ceiling `capped_address_space` most recently applied. A test that only checks
#: "some finite limit tighter than the budget is in force" is satisfied by an ambient
#: `ulimit -v` or a container limit with the fixture absent, so the guard tests below
#: compare against this instead of against a bound they recompute.
_applied_ceiling: int | None = None


@pytest.fixture(autouse=True)
def capped_address_space():
    """Bound this process's address space around each test in this module.

    Function-scoped and autouse: applied and lifted per test, over every test here,
    so a case added later cannot call a chunker uncapped by forgetting to ask.

    Skips rather than running uncapped when the usage figure cannot be read, which is
    the honest answer — a chunker whose failure mode is unbounded allocation is not
    something to call on trust.
    """
    global _applied_ceiling
    in_use = _address_space_in_use()
    if in_use is None:
        pytest.skip(
            "cannot read this process's address-space usage, so a chunking call "
            "cannot be given a ceiling"
        )
    ceiling, original_soft, hard = _cap_address_space(in_use)
    _applied_ceiling = ceiling
    try:
        yield ceiling
    finally:
        _applied_ceiling = None
        resource.setrlimit(resource.RLIMIT_AS, (original_soft, hard))


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

    ⚠️ **The ceiling is computed here, after the fork, and not passed in.** `fork`
    copies the parent's whole mapping, and the parent's figure moves — importing the
    service module alone adds ~45 MiB — so a parent-side number is stale by the time
    it is used. It has to be read in the process it will bound.

    A flat `_CHUNKER_MEMORY_BYTES` is not a smaller version of this; it is **below**
    what the child already has mapped. `setrlimit` **succeeds** when lowering the
    soft limit below current usage, which is why nothing complained about it: the
    chunker was left with whatever slack happened to exist inside already-mapped
    `pymalloc` arenas — measured at 9 MiB on one host and 1 MiB on another, varying
    with import order and with whatever the parent did beforehand. So the effective
    budget was neither 256 MiB nor zero but incidental, which is worse than a wrong
    constant because it is not reproducible.

    The chunk count is written to a file rather than put on a multiprocessing.Queue:
    a Queue spawns a feeder thread on first use, and under a tight RLIMIT_AS that
    allocation is itself what fails, so a runaway child could not report anything.
    A plain write needs no new thread.
    """
    in_use = _address_space_in_use()
    assert in_use is not None, "the parent skips when the usage cannot be read"
    _cap_address_space(in_use)
    chunks = _service()._chunk_text_with_overlap(
        text, max_chunk_size, token_size, overlap_percentage
    )
    with open(result_path, "w") as handle:
        handle.write(str(len(chunks)))


#: What each non-returning outcome means, so a failure names its cause. Collapsing
#: them into one verdict made a correct chunker that merely needed more memory
#: indistinguishable from the loop that does not advance, and the message then sent
#: the reader to a fixed issue.
_OUTCOME_MEANINGS = {
    "timed-out": (
        "the call was still running at the wall-clock budget, so the loop is not "
        "advancing — this is the shape #1090 had"
    ),
    "died": (
        "the child hit its address-space ceiling and raised. Either the loop is not "
        "advancing, or the ceiling is too low for a correct run of this input — "
        "check the chunk count the same input produces at a smaller size before "
        "concluding the chunker regressed"
    ),
    "killed": (
        "the child was killed by a signal, which for this harness means the kernel "
        "or a cgroup reclaimed it rather than the chunker failing an allocation"
    ),
    "no-result": (
        "the child exited cleanly and wrote nothing, so the harness is at fault "
        "rather than the chunker"
    ),
}


def _explain(outcome: str) -> str:
    return _OUTCOME_MEANINGS.get(outcome, f"unrecognised outcome {outcome!r}")


def _run_isolated(text, max_chunk_size, token_size, overlap_percentage):
    """Call the character chunker in a killable subprocess.

    Returns `("returned", chunk_count)` only when the child exited cleanly AND wrote
    a result. Otherwise the first element names **why** — `"timed-out"`, `"died"`,
    `"killed"` or `"no-result"`, described in `_OUTCOME_MEANINGS` — because those do
    not have the same diagnosis and one of them is not the chunker's fault. Nothing
    here can affect this process either way.
    """
    import tempfile

    if _address_space_in_use() is None:
        pytest.skip(
            "cannot read the address-space usage the child's ceiling is relative "
            "to, so the child cannot be capped"
        )

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
            return ("timed-out", None)
        # A negative exitcode is -signal: the kernel or a cgroup ended the process,
        # which is a different diagnosis from the child raising MemoryError itself.
        if process.exitcode is not None and process.exitcode < 0:
            return ("killed", None)
        if process.exitcode != 0:
            return ("died", None)
        with open(result_path) as read_handle:
            content = read_handle.read().strip()
        return ("returned", int(content)) if content else ("no-result", None)
    finally:
        os.unlink(result_path)


def _positional_text(length: int) -> str:
    """Text in which no substring of eight or more characters repeats.

    The character chunker returns slices carrying no record of where they came from,
    so their offsets have to be recovered by searching. Repeated filler would make
    that ambiguous, and the offsets are what the geometry tests are about.
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
    """That `capped_address_space` is in force, and is the ceiling *it* set.

    Every in-process chunker call in this module is safe to make only because it is.
    A fixture that silently stopped applying — a rename, a lost `autouse` — would
    leave all of them able to consume the machine, and nothing else here would fail.
    """

    def test_an_allocation_past_the_cap_is_refused(self):
        # mmap reserves address space without committing pages, so with the cap
        # absent this reserves rather than fills 2.5 GiB. A bytearray of that size
        # would zero-fill it, i.e. do the thing this module exists to prevent.
        # RLIMIT_AS refuses the reservation either way, as ENOMEM.
        with pytest.raises(OSError):
            mmap.mmap(-1, 10 * _CHUNKER_MEMORY_BYTES)

    def test_the_limit_in_force_is_the_one_the_fixture_applied(self):
        # Not "some finite limit tighter than the budget": an ambient `ulimit -v` in
        # a shell profile, or a container memory limit, satisfies that with the
        # fixture absent. Comparing against the value the fixture recorded is what
        # makes this test about the fixture.
        soft, _ = resource.getrlimit(resource.RLIMIT_AS)
        assert _applied_ceiling is not None, "the fixture did not record a ceiling"
        assert soft == _applied_ceiling

    def test_the_ceiling_is_the_documented_headroom(self):
        # The budget is headroom over what is mapped, so it has to be read against a
        # current usage figure rather than against a constant.
        in_use = _address_space_in_use()
        assert in_use is not None
        assert _applied_ceiling is not None
        assert _applied_ceiling <= in_use + _CHUNKER_MEMORY_BYTES


@pytest.mark.unit
class TestChunkTextContent:
    """_chunk_text_with_overlap: what comes back, asserted in-process.

    Safe to call directly because `capped_address_space` bounds this process around
    every test here. Termination is asserted separately, on a harness that survives
    the answer being no — see `TestChunkTextTermination`.
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
class TestTheIsolationHarness:
    """That `_run_isolated` reports *why* a child did not answer.

    Four causes reach it and they do not share a diagnosis: a loop that is not
    advancing, a correct run that wanted more memory than the ceiling allows, a
    process the kernel reclaimed, and a broken harness. One verdict for all four sent
    the reader to a fixed issue, so each is provoked here deliberately.

    `fork` inherits the parent's module state, so patching this module before the
    call is what reaches the child.
    """

    def _patch_worker_service(self, monkeypatch, replacement):
        monkeypatch.setattr(sys.modules[__name__], "_service", replacement)

    def test_a_child_that_raises_is_reported_as_died(self, monkeypatch):
        # What a correct chunker hitting the ceiling looks like — MemoryError is
        # raised in the child and the process exits non-zero.
        def raising_service():
            raise MemoryError("simulated ceiling")

        self._patch_worker_service(monkeypatch, raising_service)
        assert _run_isolated("x" * 400, 100, 4, 10) == ("died", None)

    def test_a_child_that_never_finishes_is_reported_as_timed_out(self, monkeypatch):
        # The #1090 shape. The budget is shortened so the test costs a second rather
        # than the full wall-clock allowance.
        class _Sleeper:
            def _chunk_text_with_overlap(self, *_args):
                time.sleep(30)

        monkeypatch.setattr(sys.modules[__name__], "_ISOLATED_TIMEOUT_SECONDS", 1)
        self._patch_worker_service(monkeypatch, _Sleeper)
        assert _run_isolated("x" * 400, 100, 4, 10) == ("timed-out", None)

    def test_a_child_killed_by_a_signal_is_not_reported_as_the_chunker_failing(
        self, monkeypatch
    ):
        # An out-of-memory kill by the kernel or a cgroup, which is what happens in a
        # Lambda rather than MemoryError being raised.
        class _SelfKiller:
            def _chunk_text_with_overlap(self, *_args):
                os.kill(os.getpid(), signal.SIGKILL)

        self._patch_worker_service(monkeypatch, _SelfKiller)
        assert _run_isolated("x" * 400, 100, 4, 10) == ("killed", None)

    def test_a_child_that_writes_nothing_is_reported_as_a_harness_fault(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            sys.modules[__name__], "_chunk_text_worker", lambda *_args: None
        )
        assert _run_isolated("x" * 400, 100, 4, 10) == ("no-result", None)

    def test_every_outcome_the_harness_can_return_is_explained(self):
        # A verdict with no entry would print "unrecognised outcome", which is the
        # same dead end the single verdict was.
        assert set(_OUTCOME_MEANINGS) == {"timed-out", "died", "killed", "no-result"}
        for outcome in _OUTCOME_MEANINGS:
            assert "unrecognised" not in _explain(outcome)


@pytest.mark.unit
class TestChunkTextTermination:
    """_chunk_text_with_overlap returns, for ragged lengths and extreme overlaps.

    Asserted from a forked child under its own address-space ceiling and a wall-clock
    timeout. A caller cannot assert "this returns" directly, because the interesting
    failure is that it does not: `_run_isolated` names why the child did not answer,
    and this process is unaffected either way.
    """

    def test_zero_overlap_returns_promptly_in_isolation(self):
        # Establishes that the harness itself works, so a non-returning outcome from
        # any case below is the function's behaviour and not a broken fixture.
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
            # overlap_percentage is documented as 0-100, and 100 asks for the whole
            # chunk back — a stride of zero before it is bounded below.
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
        assert outcome == "returned", f"{case}: {_explain(outcome)}"
        assert count is not None and count >= 1, f"{case}: returned no chunks at all"

    def test_an_exact_multiple_of_the_chunk_size_also_returns(self):
        # 4000 characters is exactly 100 chunks of 40, and that is not a special
        # case: a 10% overlap makes the stride 36, so `end` clamps to len(text) here
        # just as it does on a ragged length. Pinned because "only ragged lengths are
        # at risk" is the intuitive and wrong reading.
        outcome, _ = _run_isolated("x" * 4000, 10, 4, 10)
        assert outcome == "returned", _explain(outcome)

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
    assertions cannot read: with overlap the chunks deliberately do not join up, so
    "nothing was lost" has to be asked of the offsets instead of the text.
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

    @pytest.mark.parametrize("overlap", [51, 75, 90, 100])
    def test_an_overlap_over_half_a_chunk_is_bounded_to_half(self, overlap):
        # Terminating is not enough on its own. A one-character stride terminates and
        # still emits one chunk per character, each a model call, so the overlap is
        # bounded at half a chunk and the chunk count at twice the no-overlap count.
        text = _positional_text(4096)
        chunks = _service()._chunk_text_with_overlap(text, 100, 4, overlap)
        no_overlap = _service()._chunk_text_with_overlap(text, 100, 4, 0)
        assert len(chunks) <= 2 * len(no_overlap)
        offsets = _slice_offsets(text, chunks)
        for (previous_start, _), (start, _) in zip(offsets, offsets[1:]):
            assert start - previous_start >= 200, "the stride must stay at half a chunk"

    def test_an_overlap_at_the_bound_is_left_alone(self):
        # 50% is exactly the bound, so it must pass through unreduced — a bound that
        # also clipped the largest legitimate value would be off by one.
        text = _positional_text(4096)
        offsets = _slice_offsets(
            text, _service()._chunk_text_with_overlap(text, 100, 4, 50)
        )
        for (previous_start, _), (start, _) in zip(offsets, offsets[1:]):
            assert start - previous_start == 200

    @pytest.mark.parametrize("overlap", [0, 10, 50, 100])
    def test_no_chunk_exceeds_the_budget_at_any_overlap(self, overlap):
        text = _positional_text(4096)
        chunks = _service()._chunk_text_with_overlap(text, 100, 4, overlap)
        assert all(len(chunk) <= 400 for chunk in chunks)

    def test_consecutive_chunks_share_the_requested_share_of_a_chunk(self):
        # 10% of a 400-character chunk is 40 characters, so each chunk begins 40
        # characters before the previous one ended. That repeated region is the point
        # of the setting: a fact split across a boundary stays readable.
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

    @pytest.mark.parametrize("length", [405, 500, 601, 799, 4000, 4096, 4399])
    def test_the_final_chunk_is_never_shorter_than_the_overlap(self, length):
        # Not obvious, and it bounds the ragged case: the pass before the last
        # satisfied `start + chunk_size_chars < len(text)`, which forces the
        # remaining tail to exceed the overlap. So "the final chunk is shorter than
        # the overlap" is not a shape this loop can produce, however ragged the
        # length — and the bound is tight rather than comfortable. At length 601 the
        # final chunk is 201 characters against an overlap of 200: a margin of one.
        chunks = _service()._chunk_text_with_overlap(
            _positional_text(length), 100, 4, 50
        )
        assert len(chunks) >= 2, "reaching the loop at all means at least two chunks"
        assert len(chunks[-1]) > 200


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
        #
        # The budget has to sit BELOW the whole input's estimated token count or the
        # skipping code never runs: `_chunk_pages_with_overlap` returns `[text]`
        # unchanged when the document already fits, and this input is 63 characters,
        # so at `token_size` 4 it estimates 15 tokens. The earlier budget of 1000 took
        # that early return, and `== [text]` was then satisfied by the text simply
        # coming back untouched — the assertion held without the behaviour it names
        # ever being exercised. 10 forces the real path.
        text = "<page-number>1</page-number>\n\n<page-number>2</page-number>\nreal"
        chunks = _service()._chunk_pages_with_overlap(text, 10, 4, 10)
        assert chunks != [text], "expected the real chunking path, not the early return"
        joined = "".join(chunks)
        assert "1" not in _markers(joined), (
            f"page 1 is empty and should not be emitted, got {chunks!r}"
        )
        assert "real" in joined, "page 2's content must survive"


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
        # the previous page cannot express it — page_content[-0:] is page_content[0:],
        # the whole page — so the branch returns no overlap page at all rather than
        # an empty one.
        text = _paged(*[(n, f"PAGE{n}" + "z" * 1200) for n in range(1, 4)])
        chunks = _service()._chunk_pages_with_overlap(text, 200, 4, 0)
        assert _markers(chunks[1])[0] != _markers(chunks[0])[0]
        assert "PAGE1" not in chunks[1]

    def test_the_repeated_share_grows_monotonically_from_zero(self):
        # Zero is on the same curve as everything else, which is the behaviour worth
        # pinning: a percentage that repeats less must produce a smaller chunk, with
        # no discontinuity at the bottom of the range.
        text = _paged(*[(n, f"PAGE{n}" + "z" * 1200) for n in range(1, 4)])
        lengths = [
            len(_service()._chunk_pages_with_overlap(text, 200, 4, percentage)[1])
            for percentage in (0, 1, 10, 50, 100)
        ]
        assert lengths == sorted(lengths)
        assert lengths[0] < lengths[1], "zero overlap must repeat less than 1% does"

    @pytest.mark.parametrize("percentage", [1, 10, 25, 50, 100])
    def test_the_repeated_span_is_the_tail_of_the_previous_page(self, percentage):
        # How much is repeated, not merely that more is repeated for a larger
        # percentage. Uniform filler makes a 120-character tail indistinguishable
        # from any other 120 characters, so a bug halving every overlap — or taking
        # the head instead of the tail — would pass a length-ordering assertion.
        # Positional content pins both the amount and which end it came from.
        pages = [(n, f"P{n}-" + _positional_text(1200)) for n in (1, 2, 3)]
        chunks = _service()._chunk_pages_with_overlap(
            _paged(*pages), 200, 4, percentage
        )
        assert len(_markers(chunks[0])) == 1, "expected the single-page branch"

        first_page_content = pages[0][1]
        expected_size = len(first_page_content) * percentage // 100
        expected_tail = first_page_content[len(first_page_content) - expected_size :]
        # The repeated span carries page 1's marker, then exactly that tail, then
        # page 2's marker — so this pins the amount, which end it came from, and
        # that nothing else sits in between.
        expected_prefix = (
            f"<page-number>1</page-number>\n\n{expected_tail}"
            f"\n\n<page-number>2</page-number>"
        )
        assert chunks[1].startswith(expected_prefix), (
            f"{percentage}% should repeat the last {expected_size} characters of "
            f"page 1 and nothing more; got {chunks[1][:80]!r}"
        )

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
