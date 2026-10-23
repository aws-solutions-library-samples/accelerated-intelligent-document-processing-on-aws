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
"""

from __future__ import annotations

import importlib.util
import json
import os
import re

import pytest

BENCHMARKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATOR = os.path.join(BENCHMARKS, "corpus", "generators", "bank_statement.py")

# "Page 3 of 7", as `_footer` draws it.
FOOTER = re.compile(r"^Page (\d+) of (\d+)$")
ACCOUNT = re.compile(r"^Account Number: (\d+)")


def _generator():
    """Import the corpus generator, distinguishing an absent file from an absent dep.

    The same three-outcome rule ``harness_import`` uses, for the same reason: a
    generator that is not on disk or will not import is the finding, while
    ``reportlab`` missing from a bare checkout is a genuine skip. Collapsing the
    first case into a skip is how a suite reports ``1 skipped`` with a green exit
    and nobody learns the file moved.
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
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        missing = getattr(exc, "name", None) or "a dependency"
        if missing.split(".")[0] in ("reportlab", "pypdfium2"):
            pytest.skip(f"the corpus generator needs {missing}, not installed here")
        raise
    return module


@pytest.fixture(scope="module")
def bank_statement():
    return _generator()


@pytest.fixture(scope="module")
def pdf_pages():
    """``(path) -> [page text]``. Skips only if no PDF text extractor is installed."""
    pypdf = pytest.importorskip("pypdf", reason="reading the rendered page needs pypdf")

    def _read(path):
        return [page.extract_text() or "" for page in pypdf.PdfReader(path).pages]

    return _read


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
    bank_statement, pdf_pages, tmp_path, label, kwargs
):
    out = str(tmp_path / "fixture.pdf")
    bank_statement.build(out=out, paginate=True, **kwargs)
    truth = json.loads((tmp_path / "fixture.pdf.truth.json").read_text())

    asserted = sections_the_pagination_asserts(pdf_pages(out))

    assert asserted == truth["expected_sections"], (
        f"{label}: the pagination this document carries says {asserted} "
        f"statement(s) while its truth file claims "
        f"{truth['expected_sections']}. A classifier reading the page is right "
        f"and the benchmark would score it wrong."
    )
    assert truth["expected_sections"] == kwargs.get("documents", 1)


@pytest.mark.unit
def test_each_statement_restarts_its_own_page_numbering(
    bank_statement, pdf_pages, tmp_path
):
    """The mechanism, not only the count: two 3-page statements, each "Page k of 3".

    The file-wide numbering this replaced would read 1..6 of 6, which partitions
    into one statement and is exactly what
    :func:`sections_the_pagination_asserts` is written to notice.
    """
    out = str(tmp_path / "packet.pdf")
    bank_statement.build(out=out, rows=100, documents=2, paginate=True)
    pages = pdf_pages(out)

    assert len(pages) == 6
    assert _footers(pages) == [(1, 3), (2, 3), (3, 3), (1, 3), (2, 3), (3, 3)]


@pytest.mark.unit
def test_a_single_statement_still_numbers_across_the_whole_file(
    bank_statement, pdf_pages, tmp_path
):
    """``documents=1`` is unchanged — "Page 2 of 3" is the signal that fixture is for.

    Scoping pagination to the copy must not turn the one shipped paginated fixture
    into three one-page documents, which would delete the boundary evidence it
    exists to supply.
    """
    out = str(tmp_path / "single.pdf")
    bank_statement.build(out=out, rows=100, paginate=True)
    pages = pdf_pages(out)

    assert len(pages) == 3
    assert _footers(pages) == [(1, 3), (2, 3), (3, 3)]


@pytest.mark.unit
def test_each_statement_carries_its_own_account_number(
    bank_statement, pdf_pages, tmp_path
):
    """Including on a reprinted running header, which is the other page-level signal.

    A running header is drawn on every page after the first, so with more than one
    statement it would otherwise reprint the FIRST statement's account number over
    the second statement's pages — the same contradiction as the footer, in the
    other piece of identity evidence the boundary rules read.
    """
    out = str(tmp_path / "hdr.pdf")
    bank_statement.build(
        out=out, rows=60, documents=2, paginate=True, repeat_header=True
    )
    pages = pdf_pages(out)
    numbers = _account_numbers(pages)

    first, second = bank_statement._account_number(0), bank_statement._account_number(1)
    assert first != second
    midpoint = len(pages) // 2
    for index, page_numbers in enumerate(numbers):
        expected = first if index < midpoint else second
        assert page_numbers == [expected], (
            f"page {index + 1} carries account numbers {page_numbers}, "
            f"expected only {expected}"
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
