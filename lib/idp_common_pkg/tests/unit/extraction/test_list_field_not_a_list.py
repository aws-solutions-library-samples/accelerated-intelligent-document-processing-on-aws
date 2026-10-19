# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1505: a declared list that came back as a sentence lost 400 rows in silence.

The shape is a model declining *inside the field* rather than failing. Measured on
the v0.6.12 benchmark grid, ``Transactions`` came back as the string

    'The statement contains 400 transactions (SEQ00000 to SEQ00399). They are too
    many to list in full here without risking errors, so this field is not
    populated.'

on a 400-row document, and the run reported ``COMPLETED``. The three real refusal
sentences recorded on that grid are in :data:`LIVE_REFUSALS` and are the inputs
this suite is driven from, rather than a paraphrase of them.

Why the existing signals do not cover it, which is what
``TestNothingElseSpeaksOnThisShape`` measures rather than asserts:

* ``extraction_incomplete`` tests for an absent key, ``None``, or an empty list. A
  string is none of the three, so the key being present and non-empty is enough to
  silence it.
* ``extraction_rows_below_ocr_estimate``, ``extraction_partial_row_shortfall`` and
  ``extraction_row_count_unverified`` all compare a row COUNT against OCR table
  evidence. There is no list, so there is no count, with full exact-width evidence
  present and 400 rows of it.
* ``confidence_coverage`` reports no denominator — correctly, since there is no
  array to walk. ``TestTheCoverageWalkIsRight`` pins that, because the opposite
  reading (a coverage-walk defect) is the one the benchmark suite's carve-out
  comment recorded before this issue was diagnosed.
* ``extraction_validation_failed`` DOES report it — but only when
  ``extraction.validation.enabled`` is true, and at ``warning``. On the grid, the
  one affected run that recorded no issue of any kind was the one with enforcement
  off. ``TestNeitherToggleGovernsDetection`` runs this check with validation and
  coercion both off, which is that run's configuration.

What is pinned about the issue itself: ``error`` severity for a value carrying
content, that ``extraction.row_shortfall_action: fail`` neither raises nor lowers
it and does not fail the section (``COMPLETED WITH ERRORS``, not ``FAILED``), and
that the offending value travels in ``details`` truncated — the refusal sentence
is the only record of *why* the model declined, and 300 characters is a
readability bound rather than the DynamoDB one, which
``ProcessingIssue._bound_details`` already applies.

Two scope decisions have their own classes, because both are places where a silent
report must not be read as "the shape did not occur":

* ``TestABlankIsNotALostList`` — ``""`` and whitespace carry what ``[]`` carries,
  so they are routed to ``extraction_incomplete`` at ``warning`` rather than
  reported as an error. The ``error`` rationale is that a declared type and a
  returned value leave nothing to estimate, and that is true of a refusal sentence
  and false of a blank.
* ``TestItReachesANestedList`` — the traversal matches ``_object_list_targets``
  (top level plus one level into an instance array), because the nested shape is in
  shipped configuration: ``checkpoints[].items``, ``glossarySections[].terms`` and
  ``employees[].shifts`` in both ``ocr-benchmark`` presets.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from idp_common.assessment.batching import confidence_coverage
from idp_common.config.models import IDPConfig
from idp_common.extraction.service import ExtractionService


def _repo_root() -> Path:
    """The checkout this test file belongs to.

    Asserted by ``test_the_repository_root_resolves`` rather than assumed: a wrong
    depth turns every document assertion into a ``FileNotFoundError`` about the
    wrong tree, which reads as an infrastructure problem.
    """
    return Path(__file__).resolve().parents[5]


NOT_A_LIST = ExtractionService.LIST_FIELD_NOT_A_LIST_CODE
INCOMPLETE = "extraction_incomplete"
MATERIAL = ExtractionService.ROW_SHORTFALL_CODE
PARTIAL = ExtractionService.PARTIAL_ROW_SHORTFALL_CODE
UNVERIFIED = ExtractionService.NO_OCR_EVIDENCE_CODE
ROW_CODES = {MATERIAL, PARTIAL, UNVERIFIED}
PREVIEW = ExtractionService._NOT_A_LIST_PREVIEW_CHARS

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

#: The three refusal sentences recorded verbatim in
#: ``benchmarks/results/v0.6.12/coresynth__extraction-model-sonnet55/summary.json``
#: under ``processing_issue_detail[].details.errors[].message``. Driving the suite
#: from the real strings is what keeps it a test about the observed defect: each is
#: a different length and a different phrasing, and the fourth affected run
#: recorded no text at all because validation was off there.
LIVE_REFUSALS = {
    "declines and says so": (
        "Not output: the statement contains 400 transactions (SEQ00000 through "
        "SEQ00399), and I did not list them here."
    ),
    "explains at length": (
        "The statement contains 400 transactions (SEQ00000 through SEQ00399) "
        "across 9 pages. They are not listed individually here because the full "
        "list is too long to reproduce reliably in this response, so I have not "
        "extracted the individual transactions. Returning a partial list would "
        "misrepresent the document."
    ),
    "cites the risk of error": (
        "The statement contains 400 transactions (SEQ00000 to SEQ00399). They are "
        "too many to list in full here without risking errors, so this field is "
        "not populated."
    ),
}

#: Every non-list a declared array field can come back as CARRYING CONTENT. The
#: scalars are the measured shape; the dict is the other one that reaches here (a
#: model keying an array by index), and it is included because the consequence is
#: identical — nothing downstream finds rows to count or to score.
RETURNED_INSTEAD_OF_A_LIST = {
    "a refusal sentence": LIVE_REFUSALS["cites the risk of error"],
    "a number": 400,
    "a bool": False,
    "an object keyed by index": {"0": {"Date": "d0"}, "1": {"Date": "d1"}},
}

#: Blank values, which the service routes to the empty-list signal instead. ``""``
#: carries the same information as ``[]`` — there were no rows — and a genuinely
#: empty section is legitimate, which is the reason ``extraction_incomplete`` is a
#: ``warning``. The ``error`` rationale ("a declared type and a returned value
#: leave nothing to estimate") holds for a refusal sentence and fails for a blank.
#: Each of these is asserted to produce a reported issue, at the milder severity —
#: see ``TestABlankIsNotALostList``.
BLANK = {
    "an empty string": "",
    "whitespace only": "   ",
    "a newline": "\n",
}


def _svc(
    *,
    agentic: bool = False,
    row_shortfall_action: str | None = None,
    validation: bool | None = None,
    coercion: bool | None = None,
) -> ExtractionService:
    extraction: dict = {
        "mode": "advanced" if agentic else "simple",
        "agentic": {"enabled": agentic},
    }
    if row_shortfall_action is not None:
        extraction["row_shortfall_action"] = row_shortfall_action
    if validation is not None:
        extraction["validation"] = {"enabled": validation}
    if coercion is not None:
        extraction["coercion"] = {"enabled": coercion}
    svc = ExtractionService(config=IDPConfig(**{"extraction": extraction}))
    svc._reset_context()
    svc._class_schema = SCHEMA
    return svc


def textractor(n: int = 400) -> str:
    """Full exact-width OCR evidence: the rendering the row checks were built for.

    Supplied on every call deliberately. The row checks are then not silent for
    want of evidence — they are silent because there is no list to compare it
    against, which is the whole point of ``TestNothingElseSpeaksOnThisShape``.
    """
    body = "\n".join(f"| d{i} | SEQ{i:05d} | 1.00 |" for i in range(n))
    return "| Date | Description | Amount |\n|---|---|---|\n" + body


def _issues(value, *, ocr: str | None = None, metadata: dict | None = None, **svc_kw):
    svc = _svc(**svc_kw)
    svc._document_text = ocr if ocr is not None else textractor()
    fields = {"Account Number": "1"}
    if value is not ABSENT:
        fields["Transactions"] = value
    return svc, svc._build_extraction_issues(
        extracted_fields=fields,
        metadata=metadata if metadata is not None else {},
        section_id="1",
    )


ABSENT = object()


def _codes(value, **kw) -> set[str]:
    _svc_obj, issues = _issues(value, **kw)
    return {i.code for i in issues}


def _issue(value, **kw):
    _svc_obj, issues = _issues(value, **kw)
    found = [i for i in issues if i.code == NOT_A_LIST]
    assert len(found) == 1, f"expected exactly one {NOT_A_LIST}, got {len(found)}"
    return found[0]


class TestItFires:
    """The detection, over every non-list a field can come back as."""

    @pytest.mark.parametrize("label", sorted(RETURNED_INSTEAD_OF_A_LIST))
    def test_every_non_list_is_reported(self, label):
        assert NOT_A_LIST in _codes(RETURNED_INSTEAD_OF_A_LIST[label])

    @pytest.mark.parametrize("label", sorted(LIVE_REFUSALS))
    def test_every_refusal_recorded_on_the_grid_is_reported(self, label):
        assert NOT_A_LIST in _codes(LIVE_REFUSALS[label])

    def test_the_severity_is_error(self):
        assert (
            _issue(RETURNED_INSTEAD_OF_A_LIST["a refusal sentence"]).severity == "error"
        )

    def test_the_field_is_named_in_the_message(self):
        assert (
            "Transactions"
            in _issue(RETURNED_INSTEAD_OF_A_LIST["a refusal sentence"]).message
        )

    def test_the_returned_type_is_recorded(self):
        issue = _issue(RETURNED_INSTEAD_OF_A_LIST["a refusal sentence"])
        assert issue.details["returned_types"] == {"Transactions": "str"}
        assert issue.details["not_a_list_fields"] == ["Transactions"]

    def test_several_lost_fields_are_reported_in_one_issue(self):
        svc = _svc()
        svc._class_schema = {
            "type": "object",
            "properties": {
                "Transactions": {"type": "array", "items": ROW},
                "Fees": {"type": "array", "items": ROW},
            },
        }
        svc._document_text = textractor()
        issues = svc._build_extraction_issues(
            extracted_fields={"Transactions": "too many", "Fees": 7},
            metadata={},
            section_id="1",
        )
        issue = next(i for i in issues if i.code == NOT_A_LIST)
        assert issue.details["returned_types"] == {
            "Transactions": "str",
            "Fees": "int",
        }


class TestItDoesNotFireOnAnythingElse:
    """A list is a list, and the empty cases stay with ``extraction_incomplete``.

    Exclusivity in both directions, because asserting only that the new code is
    present would pass a mutation that fires it on every list, and asserting only
    that it is absent on a good list would pass one that never fires at all.
    """

    def test_a_populated_list_is_silent(self):
        rows = [{"Date": f"d{i}", "Description": f"SEQ{i:05d}"} for i in range(400)]
        assert NOT_A_LIST not in _codes(rows)

    @pytest.mark.parametrize(
        "value", [[], None, ABSENT], ids=["empty", "null", "absent"]
    )
    def test_the_empty_cases_report_incomplete_and_not_this(self, value):
        codes = _codes(value)
        assert NOT_A_LIST not in codes
        assert INCOMPLETE in codes, (
            "the empty/absent cases must keep their own code; this check is "
            "disjoint from them by construction and must not have absorbed them."
        )

    def test_a_non_list_does_not_also_report_incomplete(self):
        codes = _codes(RETURNED_INSTEAD_OF_A_LIST["a refusal sentence"])
        assert codes & {NOT_A_LIST, INCOMPLETE} == {NOT_A_LIST}, (
            "a lost list is reported once. Reporting both would double-count one "
            "loss and put two codes on the section for one cause."
        )

    def test_a_scalar_in_a_scalar_field_is_silent(self):
        """Only ARRAY-declared fields are read, which is the obvious mutation."""
        svc = _svc()
        svc._document_text = textractor()
        issues = svc._build_extraction_issues(
            extracted_fields={
                "Account Number": "1",
                "Transactions": [{"Date": "d0"}],
            },
            metadata={},
            section_id="1",
        )
        assert NOT_A_LIST not in {i.code for i in issues}


class TestABlankIsNotALostList:
    """``""`` carries ``[]``'s information, so it gets ``[]``'s severity.

    An `error` here would put a benign section — a model emitting `""` for a table
    the document genuinely does not have — at ``COMPLETED WITH ERRORS`` with an
    **Incomplete** badge, where the same answer spelled ``[]`` produces a warning.
    The asymmetry is the finding, not the blank.
    """

    @pytest.mark.parametrize("label", sorted(BLANK))
    def test_a_blank_reports_incomplete_at_warning(self, label):
        _svc_obj, issues = _issues(BLANK[label])
        codes = {i.code for i in issues}
        assert NOT_A_LIST not in codes
        assert INCOMPLETE in codes
        incomplete = next(i for i in issues if i.code == INCOMPLETE)
        assert incomplete.severity == "warning"

    @pytest.mark.parametrize("label", sorted(BLANK))
    def test_the_field_is_named_in_the_empty_list_issue(self, label):
        """It must not merely be silent — the loss is still reported, just milder."""
        _svc_obj, issues = _issues(BLANK[label])
        incomplete = next(i for i in issues if i.code == INCOMPLETE)
        assert "Transactions" in incomplete.message
        assert "Transactions" in incomplete.details["empty_list_fields"]

    def test_a_blank_and_an_empty_list_are_reported_identically(self):
        """The whole claim of this class, read off both outputs rather than stated."""
        blank = {(i.code, i.severity) for i in _issues("")[1]}
        empty = {(i.code, i.severity) for i in _issues([])[1]}
        assert blank == empty

    def test_a_string_with_content_is_still_an_error(self):
        """Non-vacuity: the blank routing has not silenced the code itself."""
        codes = _codes("N/A — but here is a sentence about it")
        assert NOT_A_LIST in codes


class TestItReachesANestedList:
    """Scope matches ``_object_list_targets``: one level into an instance array.

    The nested shape is shipped configuration, not hypothetical — both
    ``ocr-benchmark`` presets declare ``checkpoints[].items``,
    ``glossarySections[].terms`` and ``employees[].shifts`` — so a model declining
    inside one of those would be exactly as silent as the top-level case this code
    was written for if the traversal stopped at top-level ``properties``.
    """

    NESTED = {
        "type": "object",
        "properties": {
            "employees": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "shifts": {"type": "array", "items": ROW},
                    },
                },
            }
        },
    }

    def _nested_issues(self, shifts_value):
        svc = _svc()
        svc._class_schema = self.NESTED
        svc._document_text = textractor()
        return svc._build_extraction_issues(
            extracted_fields={
                "employees": [{"name": "A", "shifts": shifts_value}],
            },
            metadata={},
            section_id="1",
        )

    def test_a_nested_refusal_is_reported(self):
        issues = self._nested_issues(LIVE_REFUSALS["cites the risk of error"])
        issue = next(i for i in issues if i.code == NOT_A_LIST)
        assert issue.details["returned_types"] == {"employees[].shifts": "str"}

    def test_the_label_names_the_path(self):
        issues = self._nested_issues(LIVE_REFUSALS["declines and says so"])
        issue = next(i for i in issues if i.code == NOT_A_LIST)
        assert "employees[].shifts" in issue.message

    def test_a_populated_nested_list_is_silent(self):
        issues = self._nested_issues([{"Date": "d0", "Description": "SEQ00000"}])
        assert NOT_A_LIST not in {i.code for i in issues}

    def test_the_outer_list_being_a_refusal_is_reported_as_the_outer_field(self):
        """The outer loss shadows the inner, which cannot be read through it."""
        svc = _svc()
        svc._class_schema = self.NESTED
        svc._document_text = textractor()
        issues = svc._build_extraction_issues(
            extracted_fields={"employees": LIVE_REFUSALS["explains at length"]},
            metadata={},
            section_id="1",
        )
        issue = next(i for i in issues if i.code == NOT_A_LIST)
        assert issue.details["returned_types"] == {"employees": "str"}

    def test_the_traversal_matches_the_shipped_presets(self):
        """The three shipped nested paths resolve through the real walker.

        Read from the schema shape the presets use rather than from a copy of one,
        so this fails if the walker stops descending — which is the mutation that
        would make the scope claim in both doc tiers false.
        """
        from idp_common.extraction.service import ExtractionService

        for outer, inner in (
            ("checkpoints", "items"),
            ("glossarySections", "terms"),
            ("employees", "shifts"),
        ):
            schema = {
                "type": "object",
                "properties": {
                    outer: {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {inner: {"type": "array", "items": ROW}},
                        },
                    }
                },
            }
            found = ExtractionService._declared_array_non_lists(
                schema, {outer: [{inner: "a sentence"}]}
            )
            assert list(found) == [f"{outer}[].{inner}"]

    def test_a_ref_in_the_items_is_resolved(self):
        """``$ref``/``$defs`` is how every shipped preset declares its rows."""
        from idp_common.extraction.service import ExtractionService

        schema = {
            "type": "object",
            "$defs": {
                "Emp": {
                    "type": "object",
                    "properties": {"shifts": {"type": "array", "items": ROW}},
                }
            },
            "properties": {
                "employees": {"type": "array", "items": {"$ref": "#/$defs/Emp"}}
            },
        }
        found = ExtractionService._declared_array_non_lists(
            schema, {"employees": [{"shifts": "a sentence"}]}
        )
        assert list(found) == ["employees[].shifts"]


class TestNothingElseSpeaksOnThisShape:
    """The gap, measured. Every other signal is silent with evidence in hand.

    Each check below is given 400 rows of exact-width OCR evidence — the rendering
    the row checks were written for — so none of them is quiet for want of
    evidence. They are quiet because there is no list to count.
    """

    @pytest.mark.parametrize("label", sorted(LIVE_REFUSALS))
    def test_no_row_check_fires(self, label):
        codes = _codes(LIVE_REFUSALS[label])
        assert codes & ROW_CODES == set(), (
            "a row check spoke about a field that holds no rows; the counts it "
            "would be comparing do not exist."
        )

    def test_the_same_evidence_does_fire_a_row_check_on_a_short_list(self):
        """Non-vacuity for the check above: the evidence is live, not inert.

        If ``textractor(400)`` produced no usable evidence the assertions above
        would hold for a reason unrelated to the defect, so the same OCR text is
        shown to drive ``extraction_rows_below_ocr_estimate`` on a list of 10 —
        the 10-of-400 shape recorded on the same grid, which detection DOES cover.
        """
        rows = [{"Date": f"d{i}", "Description": f"SEQ{i:05d}"} for i in range(10)]
        assert MATERIAL in _codes(rows)

    def test_only_the_new_code_speaks(self):
        assert _codes(LIVE_REFUSALS["cites the risk of error"]) == {NOT_A_LIST}


class TestTheCoverageWalkIsRight:
    """``conf_rows_expected == 0`` is the correct answer on this shape.

    The benchmark suite's carve-out for these runs recorded the opposite reading —
    that the coverage walk was missing rows the extraction produced. It was not:
    there is no array to walk. Pinned here so the wrong diagnosis cannot come
    back, and because it is the reason this check had to be added in extraction
    rather than in the coverage walk.
    """

    @pytest.mark.parametrize("label", sorted(LIVE_REFUSALS))
    def test_no_denominator_is_reported(self, label):
        coverage = confidence_coverage(
            {"Transactions": {}}, {"Transactions": LIVE_REFUSALS[label]}
        )
        assert coverage["expected_rows"] == 0
        assert coverage["expected_rows_by_field"] == {}

    def test_a_real_list_does_produce_a_denominator(self):
        """Non-vacuity: the walk counts rows when there are rows to count.

        Also pins the key names. ``expected_rows`` is the field, not
        ``rows_expected`` — the benchmark summaries spell it the other way round
        (``conf_rows_expected``), and reading the wrong key off this dict gives
        ``None``, which is falsy, so a "no denominator" assertion written against
        it passes whatever the walk returns.
        """
        rows = [{"Amount": f"{i}.00"} for i in range(5)]
        coverage = confidence_coverage(
            {"Transactions": {"rows": []}}, {"Transactions": rows}
        )
        assert coverage["expected_rows"] == 5
        assert coverage["expected_rows_by_field"] == {"Transactions": 5}


class TestNeitherToggleGovernsDetection:
    """Validation and coercion both off — the configuration of the silent run.

    ``enforce-off`` on the grid recorded no issue of any kind, because
    ``extraction_validation_failed`` is gated on ``extraction.validation.enabled``
    and coercion's refusal (correctly refused: cross-family coercion is forbidden)
    is gated on ``extraction.coercion.enabled`` and emits no issue at all. This
    check reads the class schema against the extracted fields, so it needs
    neither.
    """

    @pytest.mark.parametrize(
        ("validation", "coercion"),
        [(False, False), (False, True), (True, False), (True, True)],
    )
    def test_it_fires_in_all_four_enforcement_states(self, validation, coercion):
        codes = _codes(
            LIVE_REFUSALS["cites the risk of error"],
            validation=validation,
            coercion=coercion,
        )
        assert NOT_A_LIST in codes

    def test_the_severity_does_not_follow_the_toggles(self):
        for validation in (False, True):
            issue = _issue(
                LIVE_REFUSALS["cites the risk of error"], validation=validation
            )
            assert issue.severity == "error"


class TestTheActionDoesNotGovernThisCode:
    """``extraction.row_shortfall_action`` reaches ``ROW_SHORTFALL_CODE`` only.

    Widening it to cover this code would start FAILING sections that complete
    today on a deployment that has already opted into ``fail`` — the reason it
    governs neither ``OUTPUT_TRUNCATED_CODE`` nor
    ``PARTIAL_ROW_SHORTFALL_CODE``. Whether a lost list should fail the section is
    a product decision; detection is what this change makes.
    """

    @pytest.mark.parametrize("action", ["warn", "fail"])
    def test_the_severity_is_error_under_either_action(self, action):
        issue = _issue(
            LIVE_REFUSALS["cites the risk of error"], row_shortfall_action=action
        )
        assert issue.severity == "error"

    @pytest.mark.parametrize("action", ["warn", "fail"])
    def test_the_section_is_not_failed(self, action):
        """The report reads COMPLETED WITH ERRORS, never the row-shortfall line.

        Read off the real ``_generate_processing_report``, which is what decides
        it, rather than by restating the branch here.
        """
        svc, issues = _issues(
            LIVE_REFUSALS["cites the risk of error"], row_shortfall_action=action
        )
        report = svc._generate_processing_report(
            {
                "parsing_succeeded": True,
                "processing_issues": [i.to_dict() for i in issues],
            }
        )
        assert "Status: COMPLETED WITH ERRORS" in report
        assert "MATERIALLY INCOMPLETE" not in report


class TestTheValueTravelsWithTheIssue:
    """The refusal sentence is the diagnostic, and it is bounded."""

    @pytest.mark.parametrize("label", sorted(LIVE_REFUSALS))
    def test_the_refusal_text_is_recorded(self, label):
        """Readable either way, and one of the three really does exceed the cap.

        "explains at length" is 318 characters, so a reader of this suite is not
        left assuming the recorded value is always the whole sentence — the
        truncation is exercised by live input rather than only by a synthetic
        long string.
        """
        refusal = LIVE_REFUSALS[label]
        recorded = _issue(refusal).details["returned_values"]["Transactions"]
        if len(refusal) <= PREVIEW:
            assert recorded == refusal
        else:
            assert recorded == refusal[:PREVIEW] + "…"
            assert refusal.startswith(recorded[:-1])

    def test_a_long_value_is_truncated(self):
        issue = _issue("x" * (PREVIEW + 5000))
        recorded = issue.details["returned_values"]["Transactions"]
        assert len(recorded) == PREVIEW + 1, "the ellipsis is one character"
        assert recorded.endswith("…")

    def test_a_value_at_the_cap_is_not_truncated(self):
        issue = _issue("x" * PREVIEW)
        assert issue.details["returned_values"]["Transactions"] == "x" * PREVIEW

    def test_a_non_string_value_is_typed_but_not_previewed(self):
        """Only strings are previewed: a dict of unbounded rows is not a message.

        Its type is still recorded, so the issue names what came back either way.
        """
        issue = _issue(RETURNED_INSTEAD_OF_A_LIST["an object keyed by index"])
        assert issue.details["returned_types"] == {"Transactions": "dict"}
        assert "Transactions" not in issue.details["returned_values"]


class TestTheAdvancedRecommendation:
    """Offered in simple mode, withheld in agentic — as ``extraction_incomplete``.

    Advanced extraction completed all 18 of its runs on the same documents in the
    v0.6.12 grid, so the recommendation is the measured remedy here rather than
    boilerplate; it would be useless advice on a run that is already agentic.
    """

    def test_simple_mode_recommends_advanced(self):
        assert (
            "Advanced (agentic)" in _issue(LIVE_REFUSALS["explains at length"]).message
        )

    def test_agentic_mode_does_not(self):
        issue = _issue(LIVE_REFUSALS["explains at length"], agentic=True)
        assert "Advanced (agentic)" not in issue.message
        assert issue.details["agentic"] is True


class TestItCannotTakeDownTheSave:
    """``_save_results`` calls the builder with no enclosing ``try``.

    A schema read from YAML can carry a bare numeric key, which arrives as an
    ``int``; a ``TypeError`` out of a join would cost the section its whole
    processing-issue list. The same hazard the empty-list and truncation joins
    already guard against.
    """

    def test_a_numeric_schema_key_does_not_raise(self):
        svc = _svc()
        svc._class_schema = {
            "type": "object",
            "properties": {7: {"type": "array", "items": ROW}},
        }
        svc._document_text = textractor()
        issues = svc._build_extraction_issues(
            extracted_fields={7: "too many to list"},
            metadata={},
            section_id="1",
        )
        issue = next(i for i in issues if i.code == NOT_A_LIST)
        assert issue.details["returned_types"] == {"7": "str"}


class TestBothDocTiersCarryTheCode:
    """A ratchet on the two doc entries, as ``test_partial_row_shortfall.py`` has.

    Without it the README section that calls this file "the whole surface" for the
    code is true of the behaviour and false of the documentation: either entry
    could be deleted with every test green.
    """

    TIERS = [
        "docs/extraction-and-confidence.md",
        "lib/idp_common_pkg/idp_common/extraction/README.md",
    ]

    def test_the_repository_root_resolves(self):
        assert (_repo_root() / "CHANGELOG.md").is_file(), _repo_root()

    @pytest.mark.parametrize("rel", TIERS)
    def test_the_code_is_described(self, rel):
        assert NOT_A_LIST in (_repo_root() / rel).read_text(encoding="utf-8")

    @pytest.mark.parametrize("rel", TIERS)
    def test_each_tier_states_that_a_blank_is_not_this_code(self, rel):
        """The one thing a reader must not get backwards.

        A reader who takes this as "any non-list is an error" will expect an error
        on a legitimately empty section and read its absence as a missed
        detection, when the blank was routed to ``extraction_incomplete`` on
        purpose.
        """
        text = re.sub(
            r"[*`#\s]+", " ", (_repo_root() / rel).read_text(encoding="utf-8")
        )
        assert "blank" in text.lower(), (
            f"{rel} does not tell a reader that a blank value is deliberately not "
            f"this code"
        )

    @pytest.mark.parametrize("rel", TIERS)
    def test_each_tier_states_the_traversal_scope(self, rel):
        """A silent report must not be read as "the nested shape did not occur"."""
        text = (_repo_root() / rel).read_text(encoding="utf-8")
        assert "outer[].inner" in text or "[].shifts" in text, (
            f"{rel} does not state how deep this check looks, so a reader cannot "
            f"tell a clean report from an unexamined one"
        )

    def test_the_changelog_records_the_measurement(self):
        text = (_repo_root() / "CHANGELOG.md").read_text(encoding="utf-8")
        assert NOT_A_LIST in text
        assert "13 of 39" in text
