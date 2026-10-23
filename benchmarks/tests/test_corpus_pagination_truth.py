# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The pagination a corpus document CARRIES must agree with the truth it SHIPS.

Why this exists
---------------
``bank_statement.build(documents=N, paginate=True)`` emits N complete back-to-back
statements and writes ``expected_sections: N`` into the truth file. The footer used
to number pages across the whole FILE, so a 3-copy file was stamped "Page 1 of 3 /
Page 2 of 3 / Page 3 of 3" — the single most decisive "this is one 3-page document"
signal a page can carry, and the first thing the classification boundary rules
check. A live run of such a fixture returned **1** section and cited the footer as
its reason: a correct answer scored as wrong, against a truth file claiming 3.

That is the class of defect this file is about, and it is a bad one to carry in a
benchmark. A mis-instrumented fixture does not look broken — it looks like a model
failure, so the figure it produces is read as a result and conclusions are drawn
from it. Nothing else in the suite can see it, because every other metric
(``completeness_recall``, ``scalar_accuracy``) is unaffected by the footer.

What is asserted, and how
-------------------------
The invariant is derived from the **rendered PDF**, not from the generator's
parameters: the page-level pagination evidence must partition the file into exactly
``expected_sections`` statements, each numbered ``Page 1..n of n`` from its own
first page. Reading it back off the page is what makes this a check on the document
rather than a restatement of the arguments that produced it.

``benchmarks/corpus/docs/`` is gitignored and regenerated, so every test here
BUILDS its fixture into ``tmp_path``. Reading a committed one would pass on a
machine that had never regenerated the corpus and would say nothing about the
generator as it stands.

Nothing here skips, deliberately
--------------------------------
Both dependencies — ``reportlab`` to render and ``pypdfium2`` to read back — are
declared in ``lib/idp_common_pkg``'s ``test`` extra, so they are present wherever
this registered root is collected, and both are imported plainly. A missing one is
therefore an **error** naming it rather than a skip.

That is the whole point rather than a detail. ``pytest.importorskip`` is the usual
courtesy for an optional dependency and is the wrong instinct here: every
page-reading case would skip in both CIs, the invariant would be asserted nowhere
that runs, and the job log of a suite proving nothing reads exactly like the log of
one proving everything. It has happened in this directory before — see
``harness_import.py`` and GitHub #1079. The remedy is to DECLARE the dependency,
which is what the ``reportlab`` entry in the ``test`` extra records, not to
tolerate its absence.

``pypdfium2`` is the extractor the generator itself uses, so reading with it adds no
new PDF library to the tree. It extracts the pagination footer and the identity
header identically to the alternative that was tried; it collapses runs of spaces
inside a line, which neither pattern below depends on.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re

import pypdfium2
import pytest

BENCHMARKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATOR = os.path.join(BENCHMARKS, "corpus", "generators", "bank_statement.py")

# "Page 3 of 7", as `_footer` draws it.
FOOTER = re.compile(r"^Page (\d+) of (\d+)$")
ACCOUNT = re.compile(r"^Account Number: (\d+)")


def _generator():
    """Import the corpus generator from its path, with no outcome but success.

    An absent file, a ``SyntaxError`` and a missing ``reportlab`` are all errors
    here. The first two are findings about the tree; the third is a finding about
    the environment, because the ``test`` extra declares it. ``harness_import``'s
    third outcome — a skip for a genuinely optional dependency — has no member in
    this file, so it is not offered.
    """
    if not os.path.exists(GENERATOR):
        raise RuntimeError(
            f"{GENERATOR} does not exist. This is a broken test bootstrap, not a "
            "reason to skip."
        )
    spec = importlib.util.spec_from_file_location(
        "bank_statement_under_test", GENERATOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bank_statement():
    return _generator()


def read_pages(path):
    """``[page text]`` for a rendered PDF, read with the generator's own extractor."""
    document = pypdfium2.PdfDocument(path)
    try:
        return [
            document[index].get_textpage().get_text_bounded() or ""
            for index in range(len(document))
        ]
    finally:
        document.close()


def _lines(page_text):
    return [line.strip() for line in page_text.splitlines() if line.strip()]


def _footers(pages):
    """``[(page_number_within_its_statement, that_statement_total)]``, one per page.

    ``None`` for a page carrying no footer, so a document that paginates only some
    of its pages is visible rather than silently skipped.
    """
    out = []
    for text in pages:
        found = [FOOTER.match(line) for line in _lines(text)]
        hits = [(int(m.group(1)), int(m.group(2))) for m in found if m]
        assert len(hits) <= 1, f"more than one pagination footer on a page: {hits}"
        out.append(hits[0] if hits else None)
    return out


def sections_the_pagination_asserts(pages):
    """How many separate statements the footers claim this file holds.

    A statement is a maximal run of pages numbered ``1 of n`` through ``n of n``.
    Raises on anything that is not such a partition — a run that does not start at
    1, a total that changes mid-run, or a run whose length disagrees with its own
    declared total — because each of those is a footer sequence that cannot be read
    as a document count at all, and returning a number for it would be a guess.
    """
    footers = _footers(pages)
    assert all(f is not None for f in footers), (
        f"not every page carries a pagination footer: {footers}"
    )
    runs = []
    for page, total in footers:
        if page == 1:
            runs.append([(page, total)])
            continue
        assert runs, f"pagination starts at page {page}, not 1: {footers}"
        previous_page, previous_total = runs[-1][-1]
        assert page == previous_page + 1 and total == previous_total, (
            f"pagination is not consecutive within a statement: {footers}"
        )
        runs[-1].append((page, total))
    for run in runs:
        declared = run[0][1]
        assert len(run) == declared, (
            f"a statement declares {declared} pages and the file gives it "
            f"{len(run)}: {footers}"
        )
    return len(runs)


def _statement_of_each_page(pages):
    """Which statement each page belongs to, derived from the footers it carries.

    Taken from the pagination rather than from arithmetic on the page count. The
    obvious shortcut — split the file in half — is right only while the copies
    happen to render the same number of pages, so it would silently stop testing
    the pairing the moment a fixture's statements differed in length, which is the
    case the pairing matters most for.
    """
    statements = []
    index = -1
    for page, _total in _footers(pages):
        if page == 1:
            index += 1
        statements.append(index)
    assert index >= 0, f"no page starts a statement: {_footers(pages)}"
    return statements


def _account_numbers(pages):
    """The distinct account numbers appearing on each page, in page order."""
    out = []
    for text in pages:
        found = [ACCOUNT.match(line) for line in _lines(text)]
        out.append(sorted({m.group(1) for m in found if m}))
    return out


# --------------------------------------------------------------------------- #
# The invariant, over the axis combinations that produce pagination
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        # The shipped `paginated_3pg` shape: one multi-page statement.
        ("one multi-page statement", {"rows": 100}),
        # One statement that fits on a single page.
        ("one single-page statement", {"rows": 5}),
        # The defect: three single-page statements, previously "Page 1/2/3 of 3".
        ("three single-page statements", {"rows": 12, "documents": 3}),
        # The fixture the corpus had no way to express: a per-statement "Page 1 of
        # 3" inside a 6-page packet, where the footer and the truth agree.
        ("two three-page statements", {"rows": 100, "documents": 2}),
        # Pagination composed with the running identity header (#750 shape).
        (
            "three statements, running header",
            {"rows": 60, "documents": 3, "repeat_header": True},
        ),
    ],
)
def test_pagination_agrees_with_expected_sections(
    bank_statement, tmp_path, label, kwargs
):
    out = str(tmp_path / "fixture.pdf")
    bank_statement.build(out=out, paginate=True, **kwargs)
    truth = json.loads((tmp_path / "fixture.pdf.truth.json").read_text())

    asserted = sections_the_pagination_asserts(read_pages(out))

    assert asserted == truth["expected_sections"], (
        f"{label}: the pagination this document carries says {asserted} "
        f"statement(s) while its truth file claims "
        f"{truth['expected_sections']}. A classifier reading the page is right "
        f"and the benchmark would score it wrong."
    )
    assert truth["expected_sections"] == kwargs.get("documents", 1)


@pytest.mark.unit
def test_each_statement_restarts_its_own_page_numbering(bank_statement, tmp_path):
    """The mechanism, not only the count: two 3-page statements, each "Page k of 3".

    The file-wide numbering this replaced would read 1..6 of 6, which partitions
    into one statement and is exactly what
    :func:`sections_the_pagination_asserts` is written to notice.
    """
    out = str(tmp_path / "packet.pdf")
    bank_statement.build(out=out, rows=100, documents=2, paginate=True)
    pages = read_pages(out)

    assert len(pages) == 6
    assert _footers(pages) == [(1, 3), (2, 3), (3, 3), (1, 3), (2, 3), (3, 3)]


@pytest.mark.unit
def test_a_single_statement_still_numbers_across_the_whole_file(
    bank_statement, tmp_path
):
    """``documents=1`` is unchanged — "Page 2 of 3" is the signal that fixture is for.

    Scoping pagination to the copy must not turn the one shipped paginated fixture
    into three one-page documents, which would delete the boundary evidence it
    exists to supply.
    """
    out = str(tmp_path / "single.pdf")
    bank_statement.build(out=out, rows=100, paginate=True)
    pages = read_pages(out)

    assert len(pages) == 3
    assert _footers(pages) == [(1, 3), (2, 3), (3, 3)]


@pytest.mark.unit
def test_each_statement_carries_its_own_account_number(bank_statement, tmp_path):
    """Including on a reprinted running header, which is the other page-level signal.

    A running header is drawn on every page after the first, so with more than one
    statement it would otherwise reprint the FIRST statement's account number over
    the second statement's pages — the same contradiction as the footer, in the
    other piece of identity evidence the boundary rules read.

    The page-to-statement pairing is read off the pagination rather than assumed
    from the page count. ``rows`` is per statement, so the generator cannot
    currently produce copies of unequal length and the two readings agree on this
    fixture; deriving it is what keeps the assertion about the pairing instead of
    about that coincidence.
    """
    out = str(tmp_path / "hdr.pdf")
    bank_statement.build(
        out=out, rows=60, documents=2, paginate=True, repeat_header=True
    )
    pages = read_pages(out)
    numbers = _account_numbers(pages)
    statements = _statement_of_each_page(pages)

    assert sections_the_pagination_asserts(pages) == 2
    assert bank_statement._account_number(0) != bank_statement._account_number(1)
    for index, page_numbers in enumerate(numbers):
        expected = bank_statement._account_number(statements[index])
        assert page_numbers == [expected], (
            f"page {index + 1} belongs to statement {statements[index] + 1} by its "
            f"pagination but carries account numbers {page_numbers}, expected only "
            f"{expected}"
        )


# --------------------------------------------------------------------------- #
# The page map refuses to guess
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestPageMapRefusesToGuess:
    """An incomplete page map must raise, not fall back to file-wide numbering.

    The map is recorded by a marker flowable during layout. If one never drew, the
    footers would quietly revert to the defect — so absence has to be an error, in
    the layer that would otherwise absorb it.
    """

    def test_a_complete_map_is_accepted(self, bank_statement):
        assert bank_statement._page_spans({0: 1, 1: 4}, 6, 2) == [(1, 3), (4, 3)]

    @pytest.mark.parametrize(
        ("starts", "total", "documents"),
        [
            ({0: 1}, 6, 2),  # a copy never recorded a start page
            ({0: 1, 1: 1}, 6, 2),  # two copies claim the same page
            ({0: 4, 1: 1}, 6, 2),  # recorded out of order
            ({}, 3, 3),  # nothing drew at all
        ],
    )
    def test_an_incomplete_map_raises(self, bank_statement, starts, total, documents):
        with pytest.raises(RuntimeError, match="page map is incomplete"):
            bank_statement._page_spans(starts, total, documents)

    def test_a_page_inside_a_span_is_located(self, bank_statement):
        spans = [(1, 3), (4, 2)]
        located = [bank_statement._locate(page, spans) for page in range(1, 6)]
        assert located == [(0, 1, 3), (0, 2, 3), (0, 3, 3), (1, 1, 2), (1, 2, 2)]

    @pytest.mark.parametrize("page", [6, 7, 99])
    def test_a_page_past_the_last_span_raises(self, bank_statement, page):
        """Clamping here would print "Page 6 of 2" — a total the file does not have.

        The two helpers have to agree. ``_page_spans`` refuses an incomplete map
        rather than guessing, and a page no span covers means the real build
        out-ran the measuring pass — the same class of disagreement, one layer
        down. Returning a number for it reinstates the defect silently, on a
        fixture whose output is read as a model result.
        """
        with pytest.raises(RuntimeError, match="outside every document copy"):
            bank_statement._locate(page, [(1, 3), (4, 2)])

    def test_page_zero_raises_rather_than_counting_backwards(self, bank_statement):
        with pytest.raises(RuntimeError, match="outside every document copy"):
            bank_statement._locate(0, [(1, 3)])


# --------------------------------------------------------------------------- #
# The two layout passes have to agree, in both directions
# --------------------------------------------------------------------------- #
def _relayout_the_written_build(bank_statement, monkeypatch, margin):
    """Make the build that WRITES the file lay out differently from the measuring one.

    The two passes agree by construction today — same story, same page size, same
    margins — so the disagreement has to be injected to be exercised at all. It is
    injected at the margins, which is the shape the real cause had, and only on the
    construction that writes the file: the generator builds three
    ``SimpleDocTemplate`` objects per paginated multi-copy run, and the third is
    the one whose output ships.

    The construction count is asserted rather than assumed. Keyed on a count, this
    helper would otherwise go silently inert the moment the generator built a
    fourth template, leaving a green test that injects nothing — the same
    absence-versus-failure shape the rest of this file is about.
    """
    import reportlab.lib.units

    real = bank_statement.SimpleDocTemplate
    calls = {"n": 0}

    def counted(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            kwargs["topMargin"] = margin * reportlab.lib.units.inch
            kwargs["bottomMargin"] = margin * reportlab.lib.units.inch
        return real(*args, **kwargs)

    monkeypatch.setattr(bank_statement, "SimpleDocTemplate", counted)
    return calls


@pytest.mark.unit
class TestTheTwoLayoutPassesMustAgree:
    """A fixture whose footers were measured against a different layout is refused.

    Both directions reach this, by different routes, and the quiet one is the
    reason the check is a comparison rather than a wider bound on ``_locate``:

    * MORE real pages than measured — a page falls outside every span, so
      ``_locate`` raises during the build.
    * FEWER real pages than measured — every page still resolves inside some span,
      so nothing raises and a later copy's first page is stamped with the previous
      copy's number and total. The file then asserts a boundary where it has none,
      which is scored as a model failure.
    """

    def test_fewer_real_pages_than_measured_is_refused(
        self, bank_statement, monkeypatch, tmp_path
    ):
        calls = _relayout_the_written_build(bank_statement, monkeypatch, margin=0.1)
        with pytest.raises(RuntimeError, match="measured layout and the real one"):
            bank_statement.build(
                out=str(tmp_path / "fewer.pdf"), rows=100, documents=2, paginate=True
            )
        assert calls["n"] == 3, (
            "the generator no longer builds three document templates, so this test "
            "injected nothing into the build that writes the file"
        )

    def test_more_real_pages_than_measured_is_refused(
        self, bank_statement, monkeypatch, tmp_path
    ):
        calls = _relayout_the_written_build(bank_statement, monkeypatch, margin=4.0)
        with pytest.raises(RuntimeError, match="outside every document copy"):
            bank_statement.build(
                out=str(tmp_path / "more.pdf"), rows=100, documents=2, paginate=True
            )
        assert calls["n"] == 3

    def test_an_agreeing_layout_is_accepted(self, bank_statement):
        spans = [(1, 3), (4, 3)]
        bank_statement._verify_measured_layout(spans, {0: 1, 1: 4}, 6, 2)

    @pytest.mark.parametrize(
        ("starts", "total", "note"),
        [
            ({0: 1, 1: 4}, 5, "the final copy came out a page shorter"),
            ({0: 1, 1: 4}, 7, "the final copy came out a page longer"),
            ({0: 1, 1: 3}, 6, "a copy started on a different page"),
            ({0: 1, 1: 5}, 6, "a copy started later than measured"),
        ],
    )
    def test_a_disagreeing_layout_is_refused(self, bank_statement, starts, total, note):
        """Driven through the comparison's own inputs, to cover maps the margin
        injection above cannot produce — a copy whose start page moves while the
        total does not, in particular."""
        with pytest.raises(RuntimeError, match="measured layout and the real one"):
            bank_statement._verify_measured_layout([(1, 3), (4, 3)], starts, total, 2)

    def test_a_single_copy_is_checked_on_its_total(self, bank_statement):
        """``documents=1`` records no markers, so the total is the whole comparison.

        It still matters: the one shipped paginated fixture is single-copy, and a
        real build a page longer than the measuring pass would stamp every page
        "of 3" on a 4-page file.
        """
        bank_statement._verify_measured_layout([(1, 3)], {}, 3, 1)
        with pytest.raises(RuntimeError, match="measured layout and the real one"):
            bank_statement._verify_measured_layout([(1, 3)], {}, 4, 1)
