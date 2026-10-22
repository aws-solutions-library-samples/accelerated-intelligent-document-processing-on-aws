# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1361's residual: the band ``_OCR_ROW_SHORTFALL_RATIO`` left silent.

#843 installed ``extraction_rows_below_ocr_estimate`` for the case where a
Simple-mode section returns a fraction of a long list and reports success. The
bar it chose is "fewer than half the matched OCR rows", and reproduced here by
sweeping every extracted count against one 800-row / 17-page rendering, it fires
from 1 through 408 and goes silent at 409 — so a section returning 409 of the
document's 800 rows, having lost **48.9%** of it, carried no processing issue at
all and its own processing report said ``Status: SUCCESS``. A reader who knows
about #843 reads that as a clean run, which is strictly worse than the invisible
loss #843 was built for.

The arithmetic behind that: the OCR evidence for the group is 817 rows, of which
17 are the column heading reprinted once per page, and half of 817 is 408.5. The
ratio is a MULTIPLICATIVE slack standing in for an ADDITIVE imprecision, so it is
~24x the inflation it absorbs at 17 pages and the factor grows with the page
count.

``extraction_partial_row_shortfall`` closes the band against the OCR's **data**
rows — the summed evidence less exactly one heading row per matched table, which
``TestTheDeductionIsExact`` measures rather than assumes — leaving a residual
tolerance that is a row GAP rather than a ratio. The consequence of that is the
property these tests exist to hold: the silent band is bounded in ABSOLUTE rows
(29, whatever the document's size) instead of proportional to it, which
``TestTheSilentBandIsBoundedInAbsoluteRows`` asserts across five sizes.

What is deliberately NOT changed, and is pinned here in both directions:

* ``extraction_rows_below_ocr_estimate``'s own band — still exactly 1..408 on
  this rendering (``TestTheMaterialBandIsUnchanged``), because
  ``extraction.row_shortfall_action: fail`` deployments would otherwise start
  failing runs that complete today.
* the new code's severity and governance — ``warning``, unconditionally, and not
  reachable by ``row_shortfall_action`` (``TestTheActionDoesNotGovernThisCode``).
  Whether a materially short extraction should FAIL is a product decision; this
  change is detection only.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from idp_common.config.models import IDPConfig
from idp_common.extraction.service import (
    ExtractionResult,
    ExtractionService,
    SectionInfo,
)
from idp_common.models import Document, Section, Status

MATERIAL = ExtractionService.ROW_SHORTFALL_CODE
PARTIAL = ExtractionService.PARTIAL_ROW_SHORTFALL_CODE

ROW = {
    "type": "object",
    "properties": {
        "Date": {"type": "string"},
        "Description": {"type": "string"},
        "Amount": {"type": "number"},
    },
}
SCHEMA = {
    "type": "object",
    "properties": {
        "Account Number": {"type": "string"},
        "Transactions": {"type": "array", "items": ROW},
    },
}


def _repo_root() -> Path:
    """The checkout this test file belongs to.

    Asserted by ``test_the_repository_root_resolves`` rather than assumed: a
    wrong depth turns every document assertion below into a ``FileNotFoundError``
    about the wrong tree, which reads as an infrastructure problem.
    """
    return Path(__file__).resolve().parents[5]


def _table(rows: int, cols: int = 3, *, pages: int = 1, heading: str | None = None):
    """OCR-like Markdown: one ``cols``-column table reprinted across ``pages``.

    The same renderer ``test_truncation_warnings.py`` uses, so the figures in
    this module's docstring are about the rendering that module's #843 tests are
    written against.
    """
    per = max(1, -(-rows // pages))
    head = heading or "| " + " | ".join(f"H{c}" for c in range(cols)) + " |"
    sep = "|" + "---|" * cols
    blocks = []
    for p in range(pages):
        lines = [head, sep]
        for i in range(p * per, min(rows, (p + 1) * per)):
            lines.append("| " + " | ".join(f"c{c}_{i}" for c in range(cols)) + " |")
        blocks.append("\n".join(lines))
    return "\n\nPage break text.\n\n".join(blocks)


def _rows(n: int) -> list[dict]:
    return [
        {"Date": "01/01/2024", "Description": f"SEQ{i:05d}", "Amount": float(i)}
        for i in range(n)
    ]


def _svc(
    *,
    agentic: bool = False,
    schema: dict | None = None,
    row_shortfall_action: str | None = None,
) -> ExtractionService:
    extraction: dict = {
        "mode": "advanced" if agentic else "simple",
        "agentic": {"enabled": agentic},
    }
    if row_shortfall_action is not None:
        extraction["row_shortfall_action"] = row_shortfall_action
    svc = ExtractionService(config=IDPConfig(**{"extraction": extraction}))
    svc._reset_context()
    svc._class_schema = schema or SCHEMA
    return svc


def _issues(svc, fields):
    return svc._build_extraction_issues(
        extracted_fields=fields, metadata={}, section_id="1"
    )


def _codes(issues) -> list[str]:
    return [i.code for i in issues]


def _verdict(text: str, extracted: int, **svc_kw) -> str:
    """Which of the two row checks spoke, as one of three exclusive labels.

    Exclusive on purpose. Asserting "PARTIAL fires" alone would pass a mutation
    that also fires MATERIAL, and asserting "MATERIAL does not fire" alone would
    pass one that silences both — the shape of the defect under repair.
    """
    svc = _svc(**svc_kw)
    svc._document_text = text
    codes = _codes(
        _issues(svc, {"Account Number": "1", "Transactions": _rows(extracted)})
    )
    if MATERIAL in codes and PARTIAL in codes:
        return "BOTH"
    if MATERIAL in codes:
        return "MATERIAL"
    if PARTIAL in codes:
        return "PARTIAL"
    return "SILENT"


# The #1361 rendering: 800 real data rows over 17 pages, so 817 summed OCR rows.
EIGHT_HUNDRED = _table(800, pages=17)


class TestTheMeasurementThisClosed:
    """The reproduction, as figures rather than as a description of figures."""

    def test_the_rendering_is_the_one_the_arithmetic_is_about(self):
        svc = _svc()
        tables = svc._ocr_tables(EIGHT_HUNDRED)
        assert svc._expected_rows_for_width(3, tables) == 817
        assert svc._heading_rows_for_width(3, tables) == 17
        # 817 * 0.5 == 408.5, which is why 409 was the first silent count.
        assert ExtractionService._OCR_ROW_SHORTFALL_RATIO == 0.5

    def test_409_of_800_rows_is_reported(self):
        """The case the installed detector could not see."""
        assert _verdict(EIGHT_HUNDRED, 409) == "PARTIAL"

    def test_what_it_reports_there(self):
        svc = _svc()
        svc._document_text = EIGHT_HUNDRED
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
            if i.code == PARTIAL
        )
        assert issue.severity == "warning"
        assert issue.stage == "extraction"
        assert issue.section_id == "1"
        d = issue.details
        assert d["extracted_rows"] == 409
        assert d["ocr_estimated_rows"] == 800
        assert d["ocr_matched_table_rows"] == 817
        assert d["ocr_heading_rows"] == 17
        assert d["missing_rows"] == 391
        assert d["list_fields"] == ["Transactions"]
        assert d["item_property_count"] == 3
        assert d["agentic"] is False
        # The reader has to be able to see WHICH 48.9% this is about.
        assert "409 row(s)" in issue.message and "391 row(s)" in issue.message
        assert "800 data row(s)" in issue.message
        assert "Transactions" in issue.message

    def test_the_section_report_no_longer_says_success_at_that_count(self):
        """What a reader actually opens.

        ``_generate_processing_report``'s status line is driven by the issue
        severities, so at 409 of 800 the preserved artifact said
        ``Status: SUCCESS``. This is the fingerprint for "observable": a mutation
        that emits the issue at a severity nothing reads leaves this line alone.
        """
        svc = _svc()
        svc._document_text = EIGHT_HUNDRED
        issues = _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
        report = svc._generate_processing_report(
            {
                "parsing_succeeded": True,
                "processing_issues": [i.to_dict() for i in issues],
            }
        )
        assert "Status: COMPLETED WITH WARNINGS" in report
        assert "Status: SUCCESS" not in report


class TestTheBoundaryFromBothSides:
    """Every transition in the band, named, with the verdict asserted exclusively.

    The inputs are deliberately LOPSIDED — no extracted count equals half its
    evidence — so a comparison inverted by a mutation maps the case somewhere
    else rather than onto itself.
    """

    @pytest.mark.parametrize(
        "extracted,verdict,why",
        [
            (43, "MATERIAL", "the #843 case: 94.6% lost, far below the half bar"),
            (407, "MATERIAL", "two below the half bar"),
            (408, "MATERIAL", "the last count the half bar catches (408 < 408.5)"),
            (409, "PARTIAL", "48.9% lost — the count #1361 measured as silent"),
            (410, "PARTIAL", "one inside the new band"),
            (600, "PARTIAL", "mid-band, 25% lost"),
            (769, "PARTIAL", "31 rows short: one more than the gap floor"),
            (770, "PARTIAL", "30 rows short: exactly the gap floor, still reported"),
            (771, "SILENT", "29 rows short: below the gap floor"),
            (799, "SILENT", "one row short"),
            (800, "SILENT", "complete"),
        ],
    )
    def test_each_transition(self, extracted, verdict, why):
        assert _verdict(EIGHT_HUNDRED, extracted) == verdict, why

    def test_the_two_codes_are_mutually_exclusive_across_the_whole_sweep(self):
        """One group yields one verdict, at every count from 1 to complete.

        Swept rather than sampled, because "both fired" is the failure a mutation
        that drops the ``continue`` produces and it is invisible to any test that
        only asks whether its own code is present.
        """
        both = [n for n in range(1, 801) if _verdict(EIGHT_HUNDRED, n) == "BOTH"]
        assert both == [], both


class TestTheMaterialBandIsUnchanged:
    """#843's governed check keeps its exact band. This is the parked decision.

    ``extraction.row_shortfall_action: fail`` turns that code into a section
    failure, so moving its bar would start failing runs that complete today on a
    deployment that opted in for a different reason. Swept rather than spot-
    checked: the whole point is that no count changed hands.
    """

    def test_it_fires_for_exactly_1_through_408(self):
        fires = [n for n in range(1, 801) if MATERIAL in self._codes(n)]
        assert fires == list(range(1, 409)), (fires[:5], fires[-5:], len(fires))

    @staticmethod
    def _codes(n: int) -> list[str]:
        svc = _svc()
        svc._document_text = EIGHT_HUNDRED
        return _codes(_issues(svc, {"Account Number": "1", "Transactions": _rows(n)}))

    def test_the_43_row_case_still_reports_the_governed_code_only(self):
        codes = self._codes(43)
        assert MATERIAL in codes and PARTIAL not in codes

    def test_under_fail_the_governed_code_is_still_the_error(self):
        svc = _svc(row_shortfall_action="fail")
        svc._document_text = EIGHT_HUNDRED
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(43)})
            if i.code == MATERIAL
        )
        assert issue.severity == "error"


class TestTheSilentBandIsBoundedInAbsoluteRows:
    """The defect class, stated as a property rather than as one measurement.

    Before: the silent band was a FRACTION of the document, so the worst
    undetected loss was 48.9% at every size and the number of silent counts grew
    without bound (1,567 of them at 3,200 rows). After: it is 30 counts at every
    size, because the residual tolerance is a row gap.

    A test at one size would pass on a mutation that restores a ratio with a
    different constant. This one would not.
    """

    SIZES = [(100, 2), (400, 9), (800, 17), (1600, 34), (3200, 68)]

    @staticmethod
    def _first_silent(body: int, pages: int) -> int:
        text = _table(body, pages=pages)
        # Walk down from complete: the first count that is NOT silent ends the band.
        for n in range(body, 0, -1):
            if _verdict(text, n) != "SILENT":
                return n + 1
        return 1

    @pytest.mark.parametrize("body,pages", SIZES)
    def test_the_band_is_the_gap_floor_minus_one_row_whatever_the_size(
        self, body, pages
    ):
        gap = ExtractionService._OCR_ROW_GAP_MIN
        assert self._first_silent(body, pages) == body - (gap - 1)

    @pytest.mark.parametrize("body,pages", SIZES)
    def test_the_worst_undetected_loss_shrinks_as_the_document_grows(self, body, pages):
        """It used to be 48.9% at every size, which is the thing being fixed."""
        worst = (body - self._first_silent(body, pages)) / body
        assert worst < 0.30
        assert worst <= ExtractionService._OCR_ROW_GAP_MIN / body

    def test_the_worst_undetected_loss_is_strictly_decreasing_in_size(self):
        losses = [
            (body - self._first_silent(body, pages)) / body
            for body, pages in self.SIZES
        ]
        assert losses == sorted(losses, reverse=True), losses
        assert all(a > b for a, b in zip(losses, losses[1:])), losses


class TestTheGapFloorFromBothSides:
    """The one residual tolerance, tested at its own boundary on a small document.

    Separate from the 800-row sweep so the floor is exercised where it is the
    BINDING constraint rather than incidentally. The renderings are lopsided:
    extracted is never half of the evidence.
    """

    @pytest.mark.parametrize(
        "body,extracted,verdict",
        [
            # 60 data rows, one page: evidence 61 summed, 60 data, half bar 30.5.
            # The two bars MEET here — see test_on_a_small_document_the_two_bars_meet.
            (60, 29, "MATERIAL"),  # under the half bar
            (60, 30, "MATERIAL"),  # still under it (30 < 30.5)
            (60, 31, "SILENT"),  # 29 short — inside the gap floor
            (60, 59, "SILENT"),
            (60, 60, "SILENT"),
            # 90 data rows: half bar 45.5, gap floor binds at 60 extracted, so the
            # band is 46..60 and this is the smallest rendering that HAS one.
            (90, 45, "MATERIAL"),
            (90, 46, "PARTIAL"),
            (90, 59, "PARTIAL"),  # 31 short
            (90, 60, "PARTIAL"),  # 30 short — exactly the floor
            (90, 61, "SILENT"),  # 29 short — inside it
            (90, 90, "SILENT"),
        ],
    )
    def test_each_side(self, body, extracted, verdict):
        assert _verdict(_table(body), extracted) == verdict

    def test_on_a_small_document_the_two_bars_meet_and_the_band_is_empty(self):
        """At 60 data rows, half the evidence IS the gap floor, so nothing is new.

        Worth pinning rather than leaving as a gap in the parametrisation above:
        the new code is for documents large enough that half of them exceeds 30
        rows, and below that the governed check already reached the gap floor.
        A reader looking for the band on a small document will not find one.
        """
        text = _table(60)
        assert {_verdict(text, n) for n in range(1, 61)} == {"MATERIAL", "SILENT"}

    def test_the_floor_is_the_evidence_floor_rather_than_a_second_constant(self):
        """Pinned because the alternative was a number chosen out of the air.

        ``_OCR_ROW_GAP_MIN`` is defined AS ``_OCR_ROW_ESTIMATE_MIN``: the gap the
        check will speak about is the smallest table it will speak about at all.
        A later change that gives it an independent value has to come here and
        say why.
        """
        assert (
            ExtractionService._OCR_ROW_GAP_MIN
            == ExtractionService._OCR_ROW_ESTIMATE_MIN
        )

    def test_the_evidence_floor_still_silences_a_tiny_table(self):
        """Below 30 rows of evidence neither check speaks, as before."""
        assert _verdict(_table(28), 1) == "SILENT"


class TestTheDeductionIsExact:
    """The denominator counts DATA rows, and that is measured, not assumed.

    A recall figure taken against the wrong denominator is a known defect shape
    in this repository, so the identity is checked against renderings whose true
    body-row count is known by construction.
    """

    @pytest.mark.parametrize(
        "body,cols,pages",
        [(800, 3, 17), (400, 3, 9), (100, 3, 1), (60, 5, 1), (5, 2, 1), (333, 4, 7)],
    )
    def test_summed_evidence_less_one_heading_per_table_is_the_body_count(
        self, body, cols, pages
    ):
        tables = ExtractionService._ocr_tables(_table(body, cols, pages=pages))
        summed = ExtractionService._expected_rows_for_width(cols, tables)
        headings = ExtractionService._heading_rows_for_width(cols, tables)
        assert headings == sum(1 for t in tables if t["cols"] == cols)
        assert summed - headings == body

    def test_the_deduction_counts_only_the_matched_width(self):
        """A second table of ANOTHER width must not contribute heading rows.

        Found by mutation: every other rendering in this module holds one width,
        so ``sum(1 for tb in tables)`` — dropping the ``cols == n_props`` filter —
        survived all of them. Here the section also carries a 2-column table
        reprinted over 20 pages, so the unfiltered count is 29 rather than 9,
        which lowers the bar by 20 and SUPPRESSES a firing. The figures are
        chosen so that suppression is the outcome: 370 extracted is 30 short of
        the true 400 and only 10 short of the inflated 380.
        """
        text = (
            _table(400, cols=3, pages=9)
            + "\n\n\n\n\n\n\n\n"
            + _table(300, cols=2, pages=20, heading="| Date | Balance |")
        )
        svc = _svc()
        tables = svc._ocr_tables(text)
        assert svc._expected_rows_for_width(3, tables) == 409
        assert svc._heading_rows_for_width(3, tables) == 9
        assert svc._heading_rows_for_width(2, tables) == 20

        svc._document_text = text
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(370)})
            if i.code == PARTIAL
        )
        assert issue.details["ocr_heading_rows"] == 9
        assert issue.details["ocr_estimated_rows"] == 400
        assert issue.details["missing_rows"] == 30
        # and one row better is inside the gap floor, from the other side
        assert _verdict(text, 371) == "SILENT"

    def test_a_headless_table_errs_downward_rather_than_upward(self):
        """A separator on the first line has no heading row to deduct.

        Subtracting one anyway under-states the body by one, which lowers the bar
        and can only SUPPRESS a firing. The safe direction, pinned so a later
        "fix" that raises the bar instead has to argue for it.
        """
        text = "|---|---|---|\n" + "\n".join("| a | b | c |" for _ in range(40))
        tables = ExtractionService._ocr_tables(text)
        summed = ExtractionService._expected_rows_for_width(3, tables)
        headings = ExtractionService._heading_rows_for_width(3, tables)
        assert summed == 40  # every row is data
        assert summed - headings == 39  # one less than the truth, not one more

    def test_the_reported_figures_reconcile(self):
        """``extracted + missing == estimated`` and ``ratio == extracted/estimated``.

        Both halves matter: a ratio taken against ``ocr_matched_table_rows``
        instead would read 0.501 here rather than 0.511, and nothing else in the
        issue would look wrong.
        """
        svc = _svc()
        svc._document_text = EIGHT_HUNDRED
        d = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
            if i.code == PARTIAL
        ).details
        assert d["extracted_rows"] + d["missing_rows"] == d["ocr_estimated_rows"]
        assert (
            d["ocr_matched_table_rows"] - d["ocr_heading_rows"]
            == (d["ocr_estimated_rows"])
        )
        assert d["ratio"] == round(d["extracted_rows"] / d["ocr_estimated_rows"], 3)
        # and the inflated count is NOT the denominator
        assert d["ocr_estimated_rows"] != d["ocr_matched_table_rows"]
        assert d["ratio"] != round(d["extracted_rows"] / d["ocr_matched_table_rows"], 3)

    def test_the_ratio_is_above_the_material_bar_by_construction(self):
        """Which is the whole reason this code exists, so it is stated as data."""
        svc = _svc()
        svc._document_text = EIGHT_HUNDRED
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
            if i.code == PARTIAL
        )
        assert issue.details["material_shortfall_ratio"] == 0.5
        # against the INFLATED evidence the material check uses, not the body bar
        assert 409 / 817 >= issue.details["material_shortfall_ratio"]


class TestTheActionDoesNotGovernThisCode:
    """The parked product decision, held as an assertion.

    ``extraction.row_shortfall_action: fail`` exists and some deployment has
    opted into it. Extending it to this band would fail documents that complete
    today, which is a decision for the maintainer rather than for a detection
    fix. So: severity is ``warning`` under both settings, and the failure path
    does not see this code.
    """

    @pytest.mark.parametrize("action", ["warn", "fail"])
    def test_severity_is_warning_under_both_settings(self, action):
        svc = _svc(row_shortfall_action=action)
        svc._document_text = EIGHT_HUNDRED
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
            if i.code == PARTIAL
        )
        assert issue.severity == "warning"

    @pytest.mark.parametrize("action", ["warn", "fail"])
    def test_the_band_does_not_move_with_the_action(self, action):
        """Detection and consequence stay separate concerns, as for #843."""
        for n, verdict in [(408, "MATERIAL"), (409, "PARTIAL"), (771, "SILENT")]:
            assert _verdict(EIGHT_HUNDRED, n, row_shortfall_action=action) == verdict, (
                n,
                action,
            )

    def test_the_failure_path_does_not_raise_on_this_code_alone(self):
        svc = _svc(row_shortfall_action="fail")
        svc._document_text = EIGHT_HUNDRED
        issues = _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
        assert _codes(issues) == [PARTIAL], _codes(issues)
        # No raise: _fail_on_row_shortfall reads ROW_SHORTFALL_CODE at error
        # severity and nothing else.
        svc._fail_on_row_shortfall(
            Document(
                id="d",
                input_key="d.pdf",
                input_bucket="in",
                output_bucket="out",
                status=Status.EXTRACTING,
            ),
            SimpleNamespace(processing_issues=issues),
            "1",
        )

    def test_the_section_completes_and_the_issue_is_persisted(self):
        """Through the real ``_save_results`` tail, not by calling the builder.

        What the band needed was not a failure but a RECORD, so the contract is
        that the section completes AND the diagnosis is durable. A mutation that
        moves the check after the result is written leaves this red.
        """
        svc = _svc()
        svc._document_text = EIGHT_HUNDRED
        doc = Document(
            id="d",
            input_key="d.pdf",
            input_bucket="in",
            output_bucket="out",
            status=Status.EXTRACTING,
        )
        section = Section(section_id="1", classification="Statement", page_ids=["1"])
        doc.sections = [section]
        info = SectionInfo(
            class_label="Statement",
            sorted_page_ids=["1"],
            page_indices=[0],
            output_bucket="out",
            output_key="d.pdf/sections/1/result.json",
            output_uri="s3://out/d.pdf/sections/1/result.json",
            start_page=1,
            end_page=1,
        )
        result = ExtractionResult(
            extracted_fields={"Account Number": "1", "Transactions": _rows(409)},
            metering={},
            parsing_succeeded=True,
            total_duration=1.0,
        )
        with patch("idp_common.extraction.service.s3.write_content") as write:
            # completes: no ExtractionOutputIncomplete
            svc._save_results(doc, section, result, info, "1", 0.0)
        written = write.call_args.args[0]
        assert len(written["inference_result"]["Transactions"]) == 409
        persisted = written["metadata"]["processing_issues"]
        assert [
            i for i in persisted if i["code"] == PARTIAL and i["severity"] == "warning"
        ], persisted
        assert "Status: COMPLETED WITH WARNINGS" in written["processing_report"]
        assert [i.code for i in section.processing_issues] == [PARTIAL]


class TestTheSeverityIsOneAReaderSees:
    """ "Emitted at a severity nothing reads" is a way for this fix to be inert.

    The UI reducer and the section report are two different consumers in two
    different languages, so both are checked — the report by behaviour above,
    the reducer by reading the committed source, since a TypeScript literal
    cannot be imported here.
    """

    def test_the_ui_reducer_handles_warning_severity(self):
        src = (
            _repo_root() / "src/ui/src/components/common/processing-issues-utils.ts"
        ).read_text(encoding="utf-8")
        assert "'warning'" in src, (
            "the severity this code emits is not one getSectionIssueStatus reads; "
            "the issue would reach the document but render as 'OK'"
        )
        assert "severities.has('warning')" in src

    def test_info_would_have_been_the_wrong_choice(self):
        """Pinned as a reason rather than left as a preference.

        ``info`` is the severity that keeps the processing report's status line at
        SUCCESS, so a section that lost rows would read as a clean run. That is
        the argument, and it holds whatever the sections panel calls an info note.

        ⚠️ Do **not** pin this on the string "Auto-recovered" appearing in the
        panel's source. That label is reserved for codes reporting an actual repair
        (#1521), so the string survives on the recovery branch and such an
        assertion passes whether or not a lost-rows note would carry it — green for
        a reason that is not the reason. Assert against ``RECOVERY_CODES``, which is
        what decides the label.
        """
        src = (
            _repo_root() / "src/ui/src/components/common/processing-issues-utils.ts"
        ).read_text(encoding="utf-8")
        # An info note is NOT described as a recovery unless its code says so.
        assert "RECOVERY_CODES" in src
        assert "assessment_recovered_with_retries" in src
        svc = _svc()
        svc._document_text = EIGHT_HUNDRED
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
            if i.code == PARTIAL
        )
        assert issue.severity != "info"


class TestTheGatingItInheritsStillApplies:
    """Everything #843 declines to compare, this declines to compare too.

    Written out because the new branch sits inside the same group loop: a
    mutation that hoists it out of that loop would start comparing lists of
    scalars, forms and empty lists.
    """

    def test_an_empty_list_is_left_to_extraction_incomplete(self):
        svc = _svc()
        svc._document_text = EIGHT_HUNDRED
        codes = _codes(_issues(svc, {"Account Number": "1", "Transactions": []}))
        assert "extraction_incomplete" in codes
        assert PARTIAL not in codes and MATERIAL not in codes

    def test_a_list_of_scalars_is_never_compared(self):
        svc = _svc(
            schema={
                "type": "object",
                "properties": {
                    "CheckNumbers": {"type": "array", "items": {"type": "string"}}
                },
            }
        )
        svc._document_text = _table(400, cols=1, heading="| Check |")
        issues = _issues(svc, {"CheckNumbers": [str(i) for i in range(60)]})
        assert PARTIAL not in _codes(issues)

    def test_tables_of_another_width_do_not_count(self):
        """100 complete 3-column Transactions beside a 300-row 2-column table."""
        svc = _svc()
        svc._document_text = (
            _table(100, cols=3)
            + "\n\n\n\n\n\n\n\n"
            + _table(300, cols=2, heading="| Date | Balance |")
        )
        assert PARTIAL not in _codes(
            _issues(svc, {"Account Number": "1", "Transactions": _rows(100)})
        )

    def test_no_document_text_means_no_comparison(self):
        """Asserted as the full verdict, not just the absence of one code.

        "``PARTIAL`` is absent" is satisfied by a variant that fabricates
        evidence and reports the material code instead, so the assertion has to
        be that NEITHER check spoke.
        """
        assert _verdict("", 3) == "SILENT"
        assert _verdict("", 500) == "SILENT"

    def test_a_cap_below_the_evidence_floor_silences_both_checks(self):
        """The floor is read off the CAPPED figure, which is the quantity to read.

        With ``maxItems: 15`` the group's legitimate total is 15 rows, below the
        30-row evidence floor, so neither check has enough to say anything — the
        pre-existing behaviour, and the reason the floor is read off ``expected``
        rather than off the raw OCR sum. A variant reading the raw sum fires here
        on a list that returned a fifth of a ceiling the schema itself set.
        """
        schema = {
            "type": "object",
            "properties": {
                "Account Number": {"type": "string"},
                "Transactions": {"type": "array", "items": ROW, "maxItems": 15},
            },
        }
        svc = _svc(schema=schema)
        svc._document_text = EIGHT_HUNDRED
        codes = _codes(_issues(svc, {"Account Number": "1", "Transactions": _rows(3)}))
        assert PARTIAL not in codes and MATERIAL not in codes, codes

    def test_a_declared_max_items_bounds_this_evidence_too(self):
        """A correctly capped list is not a shortfall, as for #843 (#1046 item 4).

        The cap is BELOW the OCR body count here, so a bar that ignored it would
        fire on a list that did exactly what the schema asked.
        """
        schema = {
            "type": "object",
            "properties": {
                "Account Number": {"type": "string"},
                "Transactions": {"type": "array", "items": ROW, "maxItems": 40},
            },
        }
        svc = _svc(schema=schema)
        svc._document_text = EIGHT_HUNDRED
        codes = _codes(_issues(svc, {"Account Number": "1", "Transactions": _rows(40)}))
        assert PARTIAL not in codes and MATERIAL not in codes

    def test_a_cap_above_the_evidence_leaves_the_band_where_it_was(self):
        schema = {
            "type": "object",
            "properties": {
                "Account Number": {"type": "string"},
                "Transactions": {"type": "array", "items": ROW, "maxItems": 5000},
            },
        }
        svc = _svc(schema=schema)
        svc._document_text = EIGHT_HUNDRED
        codes = _codes(
            _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
        )
        assert PARTIAL in codes

    def test_a_shortfall_under_a_binding_cap_is_still_reported(self):
        """Capped at 200 of 800, returned 100: the cap explains 600, not the 100."""
        schema = {
            "type": "object",
            "properties": {
                "Account Number": {"type": "string"},
                "Transactions": {"type": "array", "items": ROW, "maxItems": 200},
            },
        }
        svc = _svc(schema=schema)
        svc._document_text = EIGHT_HUNDRED
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(120)})
            if i.code == PARTIAL
        )
        assert issue.details["ocr_estimated_rows"] == 200
        assert issue.details["declared_max_items"] == 200
        assert issue.details["missing_rows"] == 80
        assert "maxItems" in issue.message

    def test_inner_lists_of_instances_are_compared_through_the_wrapper(self):
        svc = _svc(
            schema={
                "type": "object",
                "properties": {"instances": {"type": "array", "items": SCHEMA}},
            }
        )
        svc._document_text = EIGHT_HUNDRED
        issue = next(
            i
            for i in _issues(
                svc,
                {
                    "instances": [
                        {"Account Number": "1", "Transactions": _rows(200)},
                        {"Account Number": "2", "Transactions": _rows(209)},
                    ]
                },
            )
            if i.code == PARTIAL
        )
        assert issue.details["list_fields"] == ["instances[].Transactions"]
        assert issue.details["extracted_rows"] == 409


class TestTheAdvancedModeWording:
    def test_advanced_mode_is_not_recommended_to_itself(self):
        svc = _svc(agentic=True)
        svc._document_text = EIGHT_HUNDRED
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
            if i.code == PARTIAL
        )
        assert "Advanced (agentic) extraction, which shards" not in issue.message
        assert "table-parsing tool" in issue.message
        assert issue.details["agentic"] is True

    def test_simple_mode_is_told_what_to_change(self):
        svc = _svc()
        svc._document_text = EIGHT_HUNDRED
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
            if i.code == PARTIAL
        )
        assert "Advanced (agentic) extraction, which shards" in issue.message


class TestTheMessageDoesNotOverclaim:
    """The attribution limit it inherits has to be stated where it is read.

    ``_expected_rows_for_width`` matches on column width only and sums over the
    section, so an unrelated same-width table inflates the evidence. That can
    only ever inflate — nothing subtracts — so the bar is upward-biased and a
    firing is a shortfall to confirm. A reader told "391 rows were lost" without
    that caveat has been over-promised.
    """

    def test_the_message_says_the_figure_is_a_shortfall_to_confirm(self):
        svc = _svc()
        svc._document_text = EIGHT_HUNDRED
        msg = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
            if i.code == PARTIAL
        ).message
        assert "column width only" in msg
        assert "confirm" in msg

    def test_the_message_arithmetic_names_the_right_quantity_in_every_slot(self):
        """Every figure in the prose, checked as prose.

        Found by mutation: the `details` identities above are tight, but the
        message and `root_cause` are separate renderings of the same numbers and
        four different swaps between them — the heading deduction printed as the
        raw sum, the bar printed as the raw sum, `extracted` printed as
        `expected` — left every other assertion in this module green while
        telling a reader "817 counted, less 817 reprinted heading rows". A figure
        a reader acts on is worth asserting at the character level.
        """
        svc = _svc()
        svc._document_text = EIGHT_HUNDRED
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
            if i.code == PARTIAL
        )
        assert "(817 counted, less 17 reprinted column-heading row(s))" in issue.message
        assert "holds 800 data row(s) in 3-column" in issue.message
        assert "409 extracted rows vs 800 matching OCR table data rows" in (
            issue.root_cause
        )

    def test_the_cap_clause_appears_only_when_the_cap_binds_the_bar(self):
        """``maxItems: 810`` is above the 800-row bar, so it bounds nothing.

        The clause's condition has to be read against the bar, not against the
        heading-inflated sum: 810 is below 817, so a variant comparing with that
        prints "bounded to 800 by the declared maxItems" over a ceiling that did
        not bind.
        """
        schema = {
            "type": "object",
            "properties": {
                "Account Number": {"type": "string"},
                "Transactions": {"type": "array", "items": ROW, "maxItems": 810},
            },
        }
        svc = _svc(schema=schema)
        svc._document_text = EIGHT_HUNDRED
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
            if i.code == PARTIAL
        )
        assert "maxItems" not in issue.message
        assert issue.details["ocr_estimated_rows"] == 800

    def test_a_binding_cap_is_named_with_the_bar_it_produced(self):
        schema = {
            "type": "object",
            "properties": {
                "Account Number": {"type": "string"},
                "Transactions": {"type": "array", "items": ROW, "maxItems": 200},
            },
        }
        svc = _svc(schema=schema)
        svc._document_text = EIGHT_HUNDRED
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(120)})
            if i.code == PARTIAL
        )
        assert "bounded to 200 by the declared maxItems" in issue.message

    def test_the_message_says_the_governed_check_did_not_fire(self):
        svc = _svc()
        svc._document_text = EIGHT_HUNDRED
        msg = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(409)})
            if i.code == PARTIAL
        ).message
        assert MATERIAL in msg
        assert "row_shortfall_action does not apply" in msg


class TestBothDocTiersCarryTheCode:
    TIERS = [
        "docs/extraction-and-confidence.md",
        "lib/idp_common_pkg/idp_common/extraction/README.md",
    ]

    def test_the_repository_root_resolves(self):
        assert (_repo_root() / "CHANGELOG.md").is_file(), _repo_root()

    @pytest.mark.parametrize("rel", TIERS)
    def test_the_code_is_described(self, rel):
        assert PARTIAL in (_repo_root() / rel).read_text(encoding="utf-8")

    @pytest.mark.parametrize("rel", TIERS)
    def test_each_tier_states_that_the_residual_band_is_a_row_gap(self, rel):
        """The one thing a reader must not get backwards.

        A reader who takes this as "every lost row is now reported" will read a
        clean section as complete when up to 29 rows are gone. The residual is
        small and bounded, and it is not zero.
        """
        text = re.sub(
            r"[*`#\s]+", " ", (_repo_root() / rel).read_text(encoding="utf-8")
        )
        assert "29 row" in text, (
            f"{rel} does not tell a reader the size of the band this check still "
            f"leaves silent"
        )

    def test_the_changelog_records_the_measurement(self):
        text = (_repo_root() / "CHANGELOG.md").read_text(encoding="utf-8")
        assert PARTIAL in text
        assert "409 of 800" in text
