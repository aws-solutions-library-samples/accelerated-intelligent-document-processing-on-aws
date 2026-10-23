# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No Markdown table in this repository publishes a confidence above 1.0 (#1411).

``docs/benchmarking/config-guidance.md`` published four — 1.625, 1.293, 1.622 and
1.626 in the ``mean conf`` column of the v0.6.8 OmniAI-OCR-Benchmark table — for three
releases, under a footnote that was wrong about both the cause and the disposition. A
confidence is a probability, so each of those is not a wrong figure but no figure at
all. The cause is settled (#1395): one grader leaf answered on a 0-100 scale where the
scale is 0.0-1.0, and the row mean over fifteen leaves carries it. The aggregation was
correct; the input was out of range.

**This suite is over the documents, not over the committed grids, and that is the
point.** The grids are the primary record and four of them hold 21 such rows, so an
assertion over them would be red on arrival on data that no fix touches — and a gate
that is red on arrival gets turned off rather than obeyed. What a reader is exposed to
is the *published* figure, which is prose this repository controls and can keep clean
from the moment it is clean. ``test_confidence_range.py`` holds the other half: the
harness can no longer produce such a leaf in the first place.

Two further halves are elsewhere, because they are about different artifacts:

* ``test_reference_corpus_scoring.py::TestTheReferenceTableRenderer`` pins what
  ``aggregate.py --reference-tables`` prints when it re-renders one of those four
  committed grids — a word, not a number — so regenerating the published table cannot
  reintroduce the figure this suite forbids.
* the two spellings a cell may carry instead are the renderer's own
  (:data:`aggregate.UNSCORED_LABEL`, :data:`aggregate.OUT_OF_RANGE_LABEL`), asserted
  here against the page, so renaming one in the harness fails this suite until the
  page is updated with it.

**What is claimed, and what is not.** A column is recognised as holding confidence
*values* from its header text, by :data:`CONFIDENCE_VALUE_HEADER`, and that has a
reach: it matches an optional statistic word plus the word confidence, optionally
followed by an ``on …`` clause — ``mean conf``, ``Mean confidence``, ``Confidence``,
``mean confidence on that cell``. It deliberately does not match a header naming
something *about* confidence rather than a confidence: ``confidence model`` and
``Confidence grader`` hold model names, ``ECE (mean-conf)`` holds a calibration error,
and ``Confidence, no joinable cell`` and ``No confidence at all`` hold counts, which
legitimately exceed 1. A table that spells its confidence column some fourth way is
not covered, so :func:`test_the_harness_column_label_is_matched` pins the one spelling
the harness itself emits, which is the spelling every published reference table uses.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest
from harness_import import REPO, harness_module

aggregate = harness_module("aggregate")


# An optional statistic word, the word confidence, and optionally a trailing `on ...`
# clause. Anchored at both ends: the anchoring is what keeps a count column out.
CONFIDENCE_VALUE_HEADER = re.compile(
    r"^(?:(?:mean|median|min|minimum|max|maximum|avg|average)\s+)?"
    r"conf(?:idence)?"
    r"(?:\s+on\s+.+)?$"
)

# A cell holding one bare number, with the emphasis markers a Markdown table uses
# around a figure stripped off. Anything else — prose, a range, an em dash, a named
# non-value — is not a figure and is not this suite's subject.
FIGURE_CELL = re.compile(r"^[*_`]*\s*([0-9]+(?:\.[0-9]+)?)\s*[*_`]*$")

# The page the defect was found on, named so the scan's reading coverage of it is
# asserted rather than assumed.
GUIDANCE = os.path.join("docs", "benchmarking", "config-guidance.md")

# The cells of that page's v0.6.8 OmniAI-OCR-Benchmark control table whose roll-up is
# contaminated by an out-of-range leaf, and the one whose arm produced no confidence
# at all. Pinned by name: a re-transcription that puts a number back in any of them
# fails here.
CONTAMINATED_CELLS = (
    "core-tl-simple-sep",
    "core-bda-simple-sep",
    "enforce-escalate",
    "split-disabled",
)
UNSCORED_CELL = "core-tt-simple-off"


def tracked_markdown(root=REPO):
    """Every tracked ``.md`` path, read from the index rather than globbed.

    ``-z`` because this tree holds Markdown filenames containing spaces, which
    ``split()`` on the default output joins into paths that do not exist.
    """
    out = subprocess.run(
        ["git", "-C", root, "ls-files", "-z", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def _normalise_header(text):
    return re.sub(r"\s+", " ", text.replace("`", "").replace("*", "").strip()).lower()


def _row_cells(line):
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    return [c.strip() for c in stripped.strip("|").split("|")]


def confidence_column_cells(text):
    """Every cell of every confidence-value column of every table in one document.

    Yields ``(line_number, header, row_label, cell)``. Fenced blocks are skipped: a
    page may show example renderer output, and that is an illustration rather than a
    published figure.
    """
    header = None
    columns = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            header, columns = None, []
            continue
        if in_fence:
            continue
        cells = _row_cells(line)
        if cells is None:
            header, columns = None, []
            continue
        if header is None:
            header = cells
            columns = [
                i
                for i, text_ in enumerate(cells)
                if CONFIDENCE_VALUE_HEADER.match(_normalise_header(text_))
            ]
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        for i in columns:
            if i < len(cells):
                yield number, header[i], cells[0], cells[i]


def impossible_figures(paths=None, root=REPO):
    """Published confidence cells carrying a number above 1.0, as readable findings."""
    findings = []
    for path in paths if paths is not None else tracked_markdown(root):
        full = os.path.join(root, path)
        if not os.path.isfile(full):
            continue
        with open(full, encoding="utf-8") as handle:
            text = handle.read()
        if "|" not in text:
            continue
        for number, header, label, cell in confidence_column_cells(text):
            figure = FIGURE_CELL.match(cell)
            if figure and float(figure.group(1)) > 1.0:
                findings.append(f"{path}:{number}: {header!r} / {label} = {cell}")
    return findings


# --------------------------------------------------------------------------- #
# The check
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestNoPublishedConfidenceExceedsOne:
    def test_no_tracked_document_publishes_a_confidence_above_one(self):
        findings = impossible_figures()
        assert not findings, (
            "a confidence is a probability, and these published cells are above 1.0. "
            "If the figure comes from a committed grid holding an out-of-range leaf "
            "(#1395), name the cell with "
            f"`{aggregate.OUT_OF_RANGE_LABEL}` rather than quoting the roll-up:\n  "
            + "\n  ".join(findings)
        )

    def test_the_scan_fires_on_an_injected_figure(self, tmp_path):
        """The check's own red proof, so it cannot pass by reading nothing.

        Each probe is one published table with one impossible cell, in a spelling the
        tree actually uses; a probe that stops being reported means the collector lost
        a column family rather than that the tree got cleaner.
        """
        for header in ("mean conf", "Mean confidence", "Confidence"):
            page = tmp_path / "page.md"
            page.write_text(
                f"| cell | {header} | cost/doc |\n"
                "|---|---|---|\n"
                "| `core-tl-simple-sep` | 1.5 | $0.0223 |\n"
                "| `core-tt-simple-sep` | 0.960 | $0.0334 |\n",
                encoding="utf-8",
            )
            findings = impossible_figures(paths=["page.md"], root=str(tmp_path))
            assert len(findings) == 1, (header, findings)
            assert "core-tl-simple-sep" in findings[0]
            assert "1.5" in findings[0]

    def test_a_count_column_is_not_read_as_a_confidence(self, tmp_path):
        """``No confidence at all`` and ``Confidence, no joinable cell`` are cell
        counts in the calibration study and run into the tens; ``confidence model``
        holds a model name. Reading any of them as a probability would make this check
        red on correct prose, which is how a check gets deleted."""
        page = tmp_path / "page.md"
        page.write_text(
            "| arm | No confidence at all | Confidence, no joinable cell |\n"
            "|---|---|---|\n"
            "| `separate` | 6 | 22 |\n"
            "\n"
            "| configuration | confidence model | ECE (mean-conf) |\n"
            "|---|---|---|\n"
            "| `core` | Nova Lite | 2.5 |\n",
            encoding="utf-8",
        )
        assert impossible_figures(paths=["page.md"], root=str(tmp_path)) == []

    def test_the_scan_reads_the_whole_tracked_tree(self):
        """Universe closure, because an empty scan is a green scan.

        The narrow failure this covers is a discovery change — a path filter, a
        directory glob, a subprocess that returns nothing — which leaves every
        assertion above passing while reading none of the tree. Both halves are pinned:
        the page the defect was published on, and a count far enough above the number
        of Markdown files in ``docs/`` alone that narrowing to it would fail.
        """
        paths = tracked_markdown()
        assert GUIDANCE in paths
        assert len(paths) > 300, len(paths)
        assert sum(1 for p in paths if not p.startswith("docs/")) > 100

    def test_a_fenced_block_is_not_read_as_a_published_table(self, tmp_path):
        page = tmp_path / "page.md"
        page.write_text(
            "Example output:\n\n"
            "```\n"
            "| cell | mean conf |\n"
            "|---|---|\n"
            "| `c` | 1.5 |\n"
            "```\n",
            encoding="utf-8",
        )
        assert impossible_figures(paths=["page.md"], root=str(tmp_path)) == []


# --------------------------------------------------------------------------- #
# Reading coverage, and the coupling to the renderer's spellings
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestTheGuidancePageIsActuallyRead:
    def test_the_harness_column_label_is_matched(self):
        """The reach claim, pinned against the harness rather than against a literal.

        Every published reference table is a transcription of
        ``aggregate.py --reference-tables``, so the header this check must recognise is
        the one that renderer emits for ``mean_confidence``. Renaming the column there
        without widening the matcher here would take this check out silently.
        """
        labels = [
            header
            for header, metric in aggregate.REFERENCE_TABLE_COLUMNS
            if metric == "mean_confidence"
        ]
        assert labels, "the reference table no longer renders a confidence column"
        for label in labels:
            assert CONFIDENCE_VALUE_HEADER.match(_normalise_header(label)), label

    def test_the_guidance_page_contributes_confidence_cells_to_the_scan(self):
        """Non-vacuity on the page the defect was published on: if a table edit moves
        the ``mean conf`` column out of the collector's reach, this fails rather than
        the check going quietly green over nothing."""
        with open(os.path.join(REPO, GUIDANCE), encoding="utf-8") as handle:
            cells = list(confidence_column_cells(handle.read()))
        assert len(cells) > 50, len(cells)
        headers = {header for _n, header, _l, _c in cells}
        # Three spellings on this page, all of them genuine confidence values: the
        # reference tables' `mean conf` and two prose tables in the confidence-model
        # comparison. Asserted as a closed set so a fourth spelling arriving on the
        # page is a decision taken here rather than a column that quietly stops being
        # read.
        assert headers == {
            "mean conf",
            "mean confidence",
            "mean confidence on that cell",
        }, headers

    def test_the_contaminated_cells_name_a_non_value_in_the_harness_spelling(self):
        """The four cells this issue is about, and the one that produced no confidence
        at all, read as the renderer's own two words — so the page stays reproducible
        from the committed artifact, and renaming either word in the harness fails
        here until the page is updated too."""
        with open(os.path.join(REPO, GUIDANCE), encoding="utf-8") as handle:
            cells = list(confidence_column_cells(handle.read()))
        by_label = {}
        for _number, _header, label, cell in cells:
            by_label.setdefault(label.strip("`"), []).append(cell)
        for name in CONTAMINATED_CELLS:
            assert aggregate.OUT_OF_RANGE_LABEL in by_label[name], (
                name,
                by_label.get(name),
            )
        assert by_label[UNSCORED_CELL] == [aggregate.UNSCORED_LABEL] * 2, by_label[
            UNSCORED_CELL
        ]

    def test_the_two_spellings_are_distinguishable(self):
        """They mean different things — nothing produced, versus produced and off
        scale — so one must not be a rewording of the other."""
        assert aggregate.UNSCORED_LABEL != aggregate.OUT_OF_RANGE_LABEL
        assert not FIGURE_CELL.match(aggregate.UNSCORED_LABEL)
        assert not FIGURE_CELL.match(aggregate.OUT_OF_RANGE_LABEL)
