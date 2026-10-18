# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1489: "could not check" had the same output as "checked and found complete".

``extraction_rows_below_ocr_estimate`` and ``extraction_partial_row_shortfall``
are both grounded in the same evidence: OCR tables whose column count EQUALS the
item schema's property count, summed over the section. Below
``_OCR_ROW_ESTIMATE_MIN`` matched rows neither says anything, and at ZERO matched
rows neither is merely quiet — both are structurally unable to fire. Until
``extraction_row_count_unverified`` existed that produced exactly what a verified
complete list produces: an empty issue list and a processing report reading
``Status: SUCCESS``.

``TestTheEvidenceGapIsReal`` measures the gap rather than describing it: on a
3-property item schema, four Markdown renderings an LLM OCR backend can produce
sum to **0** rows of exact-width evidence — a GitHub-legal table written without
leading pipes, the same table carrying a 4th column the schema does not have, one
carrying an added row-number column, and a list rendered as bullets. Textractor
produces none of those shapes, which is why the three table-emitting backends do
not reach this.

What is deliberately NOT changed, and is pinned here in both directions:

* **the exact-width match itself** (``TestTheWidthMatchIsNotLoosened``). A
  width-tolerant count would let a two-column key/value block stand in for a
  three-column list on every backend, which is a false-positive change to the
  population where detection currently works. The widths that WERE found travel
  in the issue's ``details`` instead, so a 4-vs-3 mismatch is readable without
  any count being tolerated into the sum.
* **both existing codes' bands** (``TestTheExistingChecksAreUnchanged``), and the
  new code is mutually exclusive with both across the whole extracted range.
* **severity and governance** — ``info``, unconditionally, and not reachable by
  ``extraction.row_shortfall_action``
  (``TestTheActionDoesNotGovernThisCode``). It reports the ABSENCE of evidence,
  never a shortfall, and it is the correct answer on a complete extraction whose
  schema simply does not line up with the rendering — so at ``warning`` it would
  flip those runs from ``SUCCESS`` to ``COMPLETED WITH WARNINGS`` for a statement
  about the evidence rather than about the result.
"""

from __future__ import annotations

from idp_common.config.models import IDPConfig
from idp_common.extraction.service import ExtractionService

MATERIAL = ExtractionService.ROW_SHORTFALL_CODE
PARTIAL = ExtractionService.PARTIAL_ROW_SHORTFALL_CODE
UNVERIFIED = ExtractionService.NO_OCR_EVIDENCE_CODE
FLOOR = ExtractionService._OCR_ROW_ESTIMATE_MIN

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


def _body(fmt: str, n: int) -> str:
    return "\n".join(fmt.format(i=i) for i in range(n))


#: The rendering the checks were built against: textractor's GitHub table form,
#: leading pipe on every line, one separator, three columns.
def textractor(n: int = 40) -> str:
    return "| Date | Description | Amount |\n|---|---|---|\n" + _body(
        "| d{i} | SEQ{i:05d} | 1.00 |", n
    )


#: A GitHub-legal pipe table written WITHOUT leading pipes. Common LLM output;
#: ``_ocr_tables`` requires a leading pipe, so it finds no table at all.
def no_leading_pipe(n: int = 40) -> str:
    return "Date | Description | Amount\n--- | --- | ---\n" + _body(
        "d{i} | SEQ{i:05d} | 1.00", n
    )


#: The same table with the 4th visual column (Balance) rendered, against an item
#: schema that carries three properties. A table IS found; the width rejects it.
def four_columns(n: int = 40) -> str:
    return "| Date | Description | Amount | Balance |\n|---|---|---|---|\n" + _body(
        "| d{i} | SEQ{i:05d} | 1.00 | 9.00 |", n
    )


#: An added row-number column — same arithmetic as ``four_columns``, different
#: cause, and the one a reader is most likely to mistake for a match.
def row_numbered(n: int = 40) -> str:
    return "| # | Date | Description | Amount |\n|---|---|---|---|\n" + _body(
        "| {i} | d{i} | SEQ{i:05d} | 1.00 |", n
    )


#: Not a table at all: the model answered the "preserve the layout" instruction
#: with a bulleted list.
def bullets(n: int = 40) -> str:
    return _body("- d{i}: SEQ{i:05d} 1.00", n)


LLM_SHAPED_ZERO_EVIDENCE = {
    "no leading pipe": no_leading_pipe,
    "4 columns rendered vs 3 schema properties": four_columns,
    "an added row-number column": row_numbered,
    "rendered as a bulleted list": bullets,
}


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


def _rows(n: int) -> list[dict]:
    return [
        {"Date": f"d{i}", "Description": f"SEQ{i:05d}", "Amount": float(i)}
        for i in range(n)
    ]


def _issues(text: str, extracted: int, **svc_kw):
    svc = _svc(**svc_kw)
    svc._document_text = text
    return svc, svc._build_extraction_issues(
        extracted_fields={"Account Number": "1", "Transactions": _rows(extracted)},
        metadata={},
        section_id="1",
    )


def _verdict(text: str, extracted: int, **svc_kw) -> str:
    """Which row check spoke, as one of five EXCLUSIVE labels.

    Exclusive on purpose, for the reason ``test_partial_row_shortfall.py`` gives:
    asserting only that the new code is present would pass a mutation that also
    fires one of the other two, and asserting only that the others are absent
    would pass one that silences everything — the state this change repaired.
    """
    _svc_obj, issues = _issues(text, extracted, **svc_kw)
    codes = {i.code for i in issues}
    row_codes = sorted(codes & {MATERIAL, PARTIAL, UNVERIFIED})
    if len(row_codes) > 1:
        return "+".join(row_codes)
    if row_codes:
        return {MATERIAL: "MATERIAL", PARTIAL: "PARTIAL", UNVERIFIED: "UNVERIFIED"}[
            row_codes[0]
        ]
    return "SILENT"


def _unverified(text: str, extracted: int, **svc_kw):
    _svc_obj, issues = _issues(text, extracted, **svc_kw)
    return next(i for i in issues if i.code == UNVERIFIED)


class TestTheEvidenceGapIsReal:
    """The mechanism, as figures rather than as a description of figures.

    The exact-width match is only the SECOND of two ways the evidence reaches
    zero, and the distinction matters for anybody reading the issue: for
    ``four_columns`` and ``row_numbered`` a table is found and the width rejects
    it, while for ``no_leading_pipe`` and ``bullets`` ``_ocr_tables`` recognises
    no table at all, upstream of any width question. Both are asserted here
    because both produce the same silence and the same remedy.
    """

    def test_the_baseline_rendering_has_ample_evidence(self):
        tables = ExtractionService._ocr_tables(textractor(40))
        assert tables == [{"rows": 41, "cols": 3}]
        assert ExtractionService._expected_rows_for_width(3, tables) == 41
        assert ExtractionService._heading_rows_for_width(3, tables) == 1

    def test_the_width_match_rejects_a_table_it_did_find(self):
        for shape in (four_columns, row_numbered):
            tables = ExtractionService._ocr_tables(shape(40))
            assert tables == [{"rows": 41, "cols": 4}], shape.__name__
            assert ExtractionService._expected_rows_for_width(3, tables) == 0
            # A 4-column list schema WOULD be compared against the same text, so
            # the evidence is there and it is the width question that rejects it.
            assert ExtractionService._expected_rows_for_width(4, tables) == 41

    def test_no_table_is_recognised_at_all_in_the_other_two(self):
        for shape in (no_leading_pipe, bullets):
            assert ExtractionService._ocr_tables(shape(40)) == [], shape.__name__

    def test_every_llm_shape_sums_to_zero_exact_width_evidence(self):
        for name, shape in LLM_SHAPED_ZERO_EVIDENCE.items():
            tables = ExtractionService._ocr_tables(shape(400))
            assert ExtractionService._expected_rows_for_width(3, tables) == 0, name

    def test_zero_evidence_puts_both_checks_below_their_floor_unconditionally(self):
        """Why "quiet" is the wrong word for what the other two codes do here.

        Their floor is ``expected < _OCR_ROW_ESTIMATE_MIN``, and ``expected`` is
        the summed evidence (or less, under a ``maxItems`` cap). At zero that
        comparison holds for every extracted count and every document size, so no
        input reaches either check — which is what makes a report of the ABSENCE
        the only available signal.
        """
        assert FLOOR > 0
        for extracted in (FLOOR, 400, 3200):
            assert _verdict(bullets(400), extracted) == "UNVERIFIED"


class TestWhatItReports:
    def test_the_issue_a_wrong_width_produces(self):
        issue = _unverified(four_columns(400), 362)
        assert issue.code == "extraction_row_count_unverified"
        assert issue.severity == "info"
        assert issue.stage == "extraction"
        assert issue.section_id == "1"
        d = issue.details
        assert d["list_fields"] == ["Transactions"]
        assert d["item_property_count"] == 3
        assert d["extracted_rows"] == 362
        assert d["ocr_matched_table_rows"] == 0
        # The diagnostic the width match otherwise throws away.
        assert d["ocr_table_widths"] == [4]
        assert d["ocr_evidence_floor"] == FLOOR
        assert d["declared_max_items"] is None
        assert d["agentic"] is False

    def test_the_message_says_unverified_and_not_short(self):
        """The one thing this code must never be read as.

        It fires on complete extractions too, by construction, so a message a
        reader could take for a shortfall would be worse than the silence it
        replaced.
        """
        issue = _unverified(four_columns(400), 362)
        assert "UNVERIFIED" in issue.message
        assert "NOT a report of a shortfall" in issue.message
        assert "equally consistent with a complete list" in issue.message
        # And it names both checks that did not run, so the reader does not have
        # to know which codes exist to understand what is missing.
        assert MATERIAL in issue.message and PARTIAL in issue.message
        assert "362 row(s)" in issue.message and "Transactions" in issue.message

    def test_absent_evidence_and_wrong_shaped_evidence_read_differently(self):
        """The two causes need different remedies, so they must be told apart."""
        wrong_shape = _unverified(four_columns(400), 362)
        assert wrong_shape.details["ocr_table_widths"] == [4]
        assert "the table(s) found are 4 columns wide" in wrong_shape.message

        absent = _unverified(bullets(400), 362)
        assert absent.details["ocr_table_widths"] == []
        assert "no Markdown table was found" in absent.message

    def test_thin_evidence_reports_the_count_it_had(self):
        """Not only the zero case: 0 < evidence < floor is the same silence.

        One rendering that produces it is a multi-page table whose separator is
        not reprinted on continuation pages — ``_ocr_tables`` keeps the first
        page and drops the rest.
        """
        page1 = textractor(20)
        page2 = _body("| d{i} | SEQ{i:05d} | 1.00 |", 40)
        issue = _unverified(page1 + "\n\nPage 2 of 2\n\n" + page2, 40)
        assert issue.details["ocr_matched_table_rows"] == 21
        assert issue.details["ocr_table_widths"] == [3]
        assert "only 21 row(s)" in issue.message

    def test_the_root_cause_names_the_floor_and_the_mode(self):
        issue = _unverified(bullets(400), 362, agentic=True)
        assert "agentic extraction" in issue.root_cause
        assert f"{FLOOR}-row floor" in issue.root_cause
        assert issue.details["agentic"] is True

    def test_the_report_lists_it_and_still_says_success(self):
        """What a reader actually opens, and the reason this is ``info``.

        ``_generate_processing_report``'s status line is driven by the issue
        severities, so an ``info`` issue appears in the body with the status line
        unchanged. That is the whole trade: distinguishability without flipping
        every schema-vs-rendering mismatch to ``COMPLETED WITH WARNINGS``.
        """
        svc, issues = _issues(four_columns(400), 362)
        report = svc._generate_processing_report(
            {
                "parsing_succeeded": True,
                "processing_issues": [i.to_dict() for i in issues],
            }
        )
        assert "Status: SUCCESS" in report
        assert "COMPLETED WITH WARNINGS" not in report
        assert UNVERIFIED in report


class TestTheExtractedSideFloor:
    """The gate is on the extracted count, because the OCR count is what is gone.

    Both sides of the transition, and the residual it leaves, which is stated
    rather than hidden: a list that came back with fewer rows than the floor and
    has no evidence either reports nothing here.
    """

    def test_one_below_the_floor_is_silent(self):
        assert _verdict(bullets(400), FLOOR - 1) == "SILENT"

    def test_exactly_the_floor_is_reported(self):
        assert _verdict(bullets(400), FLOOR) == "UNVERIFIED"

    def test_the_floor_is_the_same_one_the_checks_use(self):
        assert _unverified(bullets(400), FLOOR).details["ocr_evidence_floor"] == FLOOR

    def test_an_empty_list_is_still_the_empty_list_check(self):
        """No evidence plus no rows is ``extraction_incomplete``, not this."""
        svc = _svc()
        svc._document_text = bullets(400)
        codes = {
            i.code
            for i in svc._build_extraction_issues(
                extracted_fields={"Account Number": "1", "Transactions": []},
                metadata={},
                section_id="1",
            )
        }
        assert UNVERIFIED not in codes
        assert "extraction_incomplete" in codes


class TestTheWidthMatchIsNotLoosened:
    """The parked decision: exact width stays exact, on every backend.

    A tolerance reachable only when the exact-width evidence is empty would still
    be reachable on the backends where detection works today, because "empty" is
    the ordinary state for a schema with a derived property. These assertions are
    what a future tolerance would have to break deliberately.
    """

    def test_a_two_column_key_value_block_does_not_count_against_a_three_column_list(
        self,
    ):
        kv = "| Field | Value |\n|---|---|\n" + _body("| k{i} | v{i} |", 60)
        tables = ExtractionService._ocr_tables(kv)
        assert tables == [{"rows": 61, "cols": 2}]
        assert ExtractionService._expected_rows_for_width(3, tables) == 0
        # And it reports "unverified", never a shortfall, at any extracted count.
        assert _verdict(kv, 40) == "UNVERIFIED"
        assert _verdict(kv, 1) == "SILENT"

    def test_a_wider_table_does_not_count_either(self):
        tables = ExtractionService._ocr_tables(four_columns(400))
        assert ExtractionService._expected_rows_for_width(3, tables) == 0
        assert ExtractionService._expected_rows_for_width(2, tables) == 0
        assert ExtractionService._expected_rows_for_width(5, tables) == 0

    def test_a_mismatched_width_never_produces_a_shortfall_at_any_count(self):
        """Swept, because one false shortfall here is the cost this avoided.

        The 444-run ``textract_tables`` population is at 48-of-49 detection; a
        tolerance that reported a shortfall against the wrong table would trade
        that for noise. No extracted count against a 400-row 4-column rendering
        may produce either shortfall code.
        """
        text = four_columns(400)
        bad = [
            n
            for n in range(0, 401, 7)
            if _verdict(text, n) not in ("SILENT", "UNVERIFIED")
        ]
        assert bad == [], bad


class TestTheExistingChecksAreUnchanged:
    """Neither existing band moved, and no group yields two row verdicts."""

    def test_the_material_band_still_fires_on_the_matched_rendering(self):
        # 41 summed rows, half is 20.5 -> 20 is material, 21 is not.
        assert _verdict(textractor(40), 20) == "MATERIAL"

    def test_a_complete_well_matched_extraction_is_still_silent(self):
        """The no-new-noise property: ample matched evidence, nothing reported."""
        assert _verdict(textractor(400), 400) == "SILENT"

    def test_the_three_codes_are_mutually_exclusive_across_the_whole_sweep(self):
        """One group, one verdict, at every count, on all three renderings.

        ``BOTH``-style labels are what a mutation dropping the ``continue`` after
        the new issue produces, and no test asserting only its own code can see
        it.
        """
        for name, text in (
            ("matched 800-row rendering", textractor(800)),
            ("wrong-width rendering", four_columns(800)),
            ("no table at all", bullets(800)),
        ):
            multi = [n for n in range(1, 801, 11) if "+" in _verdict(text, n)]
            assert multi == [], (name, multi)


class TestTheActionDoesNotGovernThisCode:
    """``extraction.row_shortfall_action: fail`` must not reach this code.

    A deployment that opted into ``fail`` did so for a materially short
    extraction. This code fires on complete extractions whose schema does not
    line up with the rendering, so widening the knob to cover it would fail runs
    that complete today — the same reasoning that keeps
    ``extraction_output_truncated`` and ``extraction_partial_row_shortfall`` out
    of it.
    """

    def test_severity_is_info_under_both_actions(self):
        for action in ("warn", "fail"):
            issue = _unverified(four_columns(400), 362, row_shortfall_action=action)
            assert issue.severity == "info", action

    def test_the_section_is_not_failed_under_fail(self):
        """Driven through the real gate, not through its precondition.

        ``_fail_on_row_shortfall`` reads the severities back off the PERSISTED
        section, so the issues are put there and the gate is called the way
        ``_save_results`` calls it. Asserting only that the severity is ``info``
        would leave a gate that keys on the code alone passing.
        """
        from idp_common.models import Document, Section, Status

        svc, issues = _issues(four_columns(400), 362, row_shortfall_action="fail")
        assert [i.code for i in issues] == [UNVERIFIED]
        document = Document(
            id="d", input_bucket="in", input_key="k.pdf", status=Status.EXTRACTING
        )
        section = Section(
            section_id="1", classification="bank-statement", page_ids=["1"]
        )
        section.processing_issues = issues
        svc._fail_on_row_shortfall(document, section, "1")
        assert document.errors == []

    def test_the_report_status_is_unchanged_under_fail(self):
        svc, issues = _issues(four_columns(400), 362, row_shortfall_action="fail")
        report = svc._generate_processing_report(
            {
                "parsing_succeeded": True,
                "processing_issues": [i.to_dict() for i in issues],
            }
        )
        assert "Status: SUCCESS" in report
        assert "FAILED" not in report
