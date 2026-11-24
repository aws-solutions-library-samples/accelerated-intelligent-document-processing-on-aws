# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Where the run that follows a change to a document starts."""

import hashlib
import json
import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys

import pytest
from pydantic import ValidationError

from idp_common.models import Document, Section
from idp_common.run_plan import restart
from idp_common.run_plan.models import RunKind, RunPlan
from idp_common.run_plan.restart import (
    CAUSE_ADDED,
    CAUSE_NO_RESULT,
    CAUSE_PAGES_CHANGED,
    MAX_LAYOUT_SECTIONS,
    SEND_BACK_START,
    RestartDecision,
    decide_restart,
    layout_of,
)
from idp_common.run_plan.steps import BDA, PIPELINE

pytestmark = pytest.mark.unit

KEY = "packet.pdf"


def _section(section_id, page_ids, *, result=True, excluded=False) -> Section:
    return Section(
        section_id=section_id,
        classification="Invoice",
        page_ids=list(page_ids),
        extraction_result_uri=(
            f"s3://b/{KEY}/sections/{section_id}/result.json" if result else None
        ),
        excluded=excluded,
    )


def _document(*sections) -> Document:
    document = Document(id=KEY, input_key=KEY)
    document.sections.extend(sections)
    return document


def _processed() -> Document:
    """Three sections, each with a result."""
    return _document(
        _section("1", ["1", "2"]), _section("2", ["3"]), _section("3", ["4", "5"])
    )


def _completed(document, mode=PIPELINE, **arguments) -> RestartDecision:
    return decide_restart(
        document, kind=RunKind.REVIEW_COMPLETED, mode=mode, **arguments
    )


class TestLayout:
    def test_every_section_has_a_digest_of_its_pages(self):
        layout = layout_of(_processed())

        assert list(layout) == ["1", "2", "3"]
        assert layout["1"] == hashlib.sha256(b"1,2").hexdigest()[:16]
        assert layout["2"] == hashlib.sha256(b"3").hexdigest()[:16]
        assert all(len(digest) == 16 for digest in layout.values())

    def test_the_digest_is_stable(self):
        """The same pages give the same digest, in whatever order and type the
        ids come, and across calls."""
        ordered = layout_of(_document(_section("1", ["2", "10", "1"])))
        shuffled = layout_of(_document(_section("1", ["10", "1", "2"])))
        numbers = layout_of(_document(_section("1", [1, 2, 10])))

        assert ordered == shuffled == numbers
        # Numbered pages are ordered by number, not by text.
        assert ordered["1"] == hashlib.sha256(b"1,2,10").hexdigest()[:16]
        assert layout_of(_processed()) == layout_of(_processed())

    def test_a_page_id_that_is_not_a_number_comes_after_the_numbered_ones(self):
        layout = layout_of(_document(_section("1", ["cover", "2", "appendix", "1"])))

        assert layout["1"] == hashlib.sha256(b"1,2,appendix,cover").hexdigest()[:16]

    def test_other_pages_give_another_digest(self):
        assert (
            layout_of(_document(_section("1", ["1", "2"])))["1"]
            != layout_of(_document(_section("1", ["1", "3"])))["1"]
        )
        assert (
            layout_of(_document(_section("1", ["1", "2"])))["1"]
            != layout_of(_document(_section("1", ["12"])))["1"]
        )

    def test_an_excluded_section_is_part_of_the_layout(self):
        layout = layout_of(
            _document(_section("1", ["1"]), _section("2", ["2"], excluded=True))
        )

        assert set(layout) == {"1", "2"}

    def test_a_section_with_no_pages_has_a_digest(self):
        assert (
            layout_of(_document(_section("1", [])))["1"]
            == (hashlib.sha256(b"").hexdigest()[:16])
        )

    def test_no_document_has_no_layout(self):
        assert layout_of(None) == {}

    def test_a_document_with_no_sections_has_an_empty_layout(self):
        assert layout_of(_document()) == {}

    def test_more_than_the_limit_of_sections_records_no_layout(self):
        at_the_limit = _document(
            *(_section(str(i), [str(i)]) for i in range(MAX_LAYOUT_SECTIONS))
        )
        over_the_limit = _document(
            *(_section(str(i), [str(i)]) for i in range(MAX_LAYOUT_SECTIONS + 1))
        )

        assert len(layout_of(at_the_limit)) == MAX_LAYOUT_SECTIONS == 500
        assert layout_of(over_the_limit) == {}


class TestSentBack:
    def test_a_sent_back_document_starts_at_classification(self):
        decision = decide_restart(_processed(), kind=RunKind.SENT_BACK, mode=PIPELINE)

        assert decision == RestartDecision(start="classification")
        assert SEND_BACK_START == "classification"

    def test_in_bda_mode_that_is_the_beginning(self):
        decision = decide_restart(_processed(), kind=RunKind.SENT_BACK, mode=BDA)

        assert decision == RestartDecision(start="beginning")

    def test_it_is_decided_without_the_document(self):
        assert decide_restart(None, kind=RunKind.SENT_BACK, mode=PIPELINE).start == (
            "classification"
        )

    def test_the_layout_and_the_results_do_not_change_it(self):
        document = _document(_section("9", ["1"], result=False))

        decision = decide_restart(
            document,
            kind=RunKind.SENT_BACK,
            mode=PIPELINE,
            baseline_layout={"1": "0" * 16},
        )

        assert (decision.start, decision.sections, decision.causes) == (
            "classification",
            (),
            {},
        )


class TestReviewCompleted:
    def test_corrections_to_values_only_start_at_apply_review_changes(self):
        document = _processed()

        decision = _completed(document, baseline_layout=layout_of(document))

        assert decision == RestartDecision(start="apply_review_changes")
        assert (decision.sections, decision.causes) == ((), {})

    def test_a_section_added_during_the_review_is_extracted(self):
        baseline = layout_of(_processed())
        document = _processed()
        document.sections.append(_section("4", ["6"]))

        decision = _completed(document, baseline_layout=baseline)

        assert decision == RestartDecision(
            start="extraction", sections=("4",), causes={"4": CAUSE_ADDED}
        )

    def test_a_section_whose_pages_changed_is_extracted(self):
        baseline = layout_of(_processed())
        document = _processed()
        # A page moved from section 3 to section 2: both have other pages now.
        document.sections[1].page_ids = ["3", "4"]
        document.sections[2].page_ids = ["5"]

        decision = _completed(document, baseline_layout=baseline)

        assert decision == RestartDecision(
            start="extraction",
            sections=("2", "3"),
            causes={"2": CAUSE_PAGES_CHANGED, "3": CAUSE_PAGES_CHANGED},
        )

    def test_a_section_with_no_result_is_extracted(self):
        baseline = layout_of(_processed())
        document = _processed()
        document.sections[0].extraction_result_uri = None

        decision = _completed(document, baseline_layout=baseline)

        assert decision == RestartDecision(
            start="extraction", sections=("1",), causes={"1": CAUSE_NO_RESULT}
        )

    def test_each_section_gets_the_first_cause_that_applies(self):
        """Added before pages changed before no result, in document order."""
        baseline = layout_of(_processed())
        document = _document(
            # Its pages changed and it has no result: the pages are the cause.
            _section("1", ["1"], result=False),
            _section("2", ["3"]),
            # Not in the baseline and with no result: it was added.
            _section("7", ["2"], result=False),
            _section("3", ["4", "5"], result=False),
        )

        decision = _completed(document, baseline_layout=baseline)

        assert decision.start == "extraction"
        assert decision.sections == ("1", "7", "3")
        assert decision.causes == {
            "1": CAUSE_PAGES_CHANGED,
            "7": CAUSE_ADDED,
            "3": CAUSE_NO_RESULT,
        }

    def test_the_causes(self):
        assert (CAUSE_ADDED, CAUSE_PAGES_CHANGED, CAUSE_NO_RESULT) == (
            "added",
            "pages_changed",
            "no_result",
        )

    def test_pages_in_another_order_are_the_same_pages(self):
        baseline = layout_of(_processed())
        document = _processed()
        document.sections[0].page_ids = ["2", "1"]

        assert _completed(document, baseline_layout=baseline).start == (
            "apply_review_changes"
        )

    def test_an_excluded_section_is_not_extracted(self):
        """Only processed sections count: an excluded one that was added, has
        other pages or has no result is left as it is."""
        baseline = layout_of(_processed())
        document = _processed()
        document.sections[2].page_ids = ["5"]
        document.sections[2].excluded = True
        document.sections.append(_section("4", ["4"], result=False, excluded=True))

        decision = _completed(document, baseline_layout=baseline)

        assert decision == RestartDecision(start="apply_review_changes")

    def test_an_excluded_section_is_left_out_of_the_named_sections(self):
        baseline = layout_of(_processed())
        document = _processed()
        document.sections.append(_section("4", ["6"], result=False, excluded=True))
        document.sections.append(_section("5", ["7"]))

        decision = _completed(document, baseline_layout=baseline)

        assert (decision.sections, decision.causes) == (("5",), {"5": CAUSE_ADDED})

    @pytest.mark.parametrize("baseline", [None, {}], ids=["none", "empty"])
    def test_with_no_baseline_only_a_missing_result_sends_a_section_to_extraction(
        self, baseline
    ):
        """An item placed before layouts were recorded: nothing to compare with."""
        document = _processed()
        document.sections.append(_section("4", ["6"]))
        document.sections[0].page_ids = ["9"]

        assert _completed(document, baseline_layout=baseline) == RestartDecision(
            start="apply_review_changes"
        )

        document.sections[1].extraction_result_uri = ""

        assert _completed(document, baseline_layout=baseline) == RestartDecision(
            start="extraction", sections=("2",), causes={"2": CAUSE_NO_RESULT}
        )

    def test_in_bda_mode_the_run_starts_at_apply_review_changes(self):
        """One call extracts a BDA document, so no section is extracted alone."""
        baseline = layout_of(_processed())
        document = _processed()
        document.sections.append(_section("4", ["6"], result=False))

        decision = _completed(document, mode=BDA, baseline_layout=baseline)

        assert decision == RestartDecision(start="apply_review_changes")

    def test_with_no_document_the_run_starts_at_apply_review_changes(self):
        assert _completed(None, baseline_layout={"1": "0" * 16}) == RestartDecision(
            start="apply_review_changes"
        )

    def test_a_document_too_large_for_a_layout_compares_with_nothing(self):
        """More than 500 sections: no layout was recorded at placement, so only
        a missing result sends a section to extraction."""
        count = MAX_LAYOUT_SECTIONS + 1
        document = _document(*(_section(str(i), [str(i)]) for i in range(count)))
        baseline = layout_of(document)
        assert baseline == {}
        document.sections[3].page_ids = ["900"]

        assert _completed(document, baseline_layout=baseline) == RestartDecision(
            start="apply_review_changes"
        )

        for section in document.sections:
            section.extraction_result_uri = None
        decision = _completed(document, baseline_layout=baseline)

        assert decision.start == "extraction"
        assert len(decision.sections) == count
        assert set(decision.causes.values()) == {CAUSE_NO_RESULT}
        # More than a plan can name: the caller falls back to every section.
        with pytest.raises(ValueError, match="at most 500 sections"):
            RunPlan(
                kind=RunKind.REVIEW_COMPLETED,
                start=decision.start,
                sections=list(decision.sections),
                cycle_id="01J0000000000000000000",
            )

    def test_a_document_that_grew_past_the_limit_is_still_compared(self):
        """The recorded baseline is what matters: each section is compared with
        it whatever the document's size is now."""
        baseline = layout_of(_processed())
        document = _processed()
        document.sections.extend(
            _section(f"n{i}", [str(100 + i)]) for i in range(MAX_LAYOUT_SECTIONS)
        )

        decision = _completed(document, baseline_layout=baseline)

        assert decision.start == "extraction"
        assert len(decision.sections) == MAX_LAYOUT_SECTIONS
        assert set(decision.causes.values()) == {CAUSE_ADDED}

    def test_the_decision_makes_a_plan(self):
        baseline = layout_of(_processed())
        document = _processed()
        document.sections.append(_section("4", ["6"]))

        decision = _completed(document, baseline_layout=baseline)
        plan = RunPlan(
            kind=RunKind.REVIEW_COMPLETED,
            start=decision.start,
            sections=list(decision.sections),
            cycle_id="01J0000000000000000000",
        )

        assert (plan.start, plan.sections, plan.reviewed) == (
            "extraction",
            ["4"],
            False,
        )


class TestSavedEdits:
    def _saved(self, document, **arguments) -> RestartDecision:
        return decide_restart(
            document, kind=RunKind.SAVED_EDITS, mode=PIPELINE, **arguments
        )

    def test_a_class_reset_starts_at_classification(self):
        assert self._saved(_processed(), class_reset=True) == RestartDecision(
            start="classification"
        )

    def test_a_class_reset_wins_over_a_section_with_no_result(self):
        document = _processed()
        document.sections[0].extraction_result_uri = None

        decision = self._saved(document, class_reset=True)

        assert (decision.start, decision.sections) == ("classification", ())

    def test_the_sections_with_no_result_are_extracted(self):
        document = _processed()
        document.sections[0].extraction_result_uri = None
        document.sections[2].extraction_result_uri = ""

        assert self._saved(document) == RestartDecision(
            start="extraction",
            sections=("1", "3"),
            causes={"1": CAUSE_NO_RESULT, "3": CAUSE_NO_RESULT},
        )

    def test_every_section_without_a_result_is_a_plain_extraction_start(self):
        document = _document(
            _section("1", ["1"], result=False), _section("2", ["2"], result=False)
        )

        assert self._saved(document) == RestartDecision(start="extraction")

    def test_an_excluded_section_with_no_result_is_not_extracted(self):
        document = _processed()
        document.sections[1].extraction_result_uri = None
        document.sections[1].excluded = True

        assert self._saved(document) == RestartDecision(start="policy_rule_validation")

    def test_an_excluded_section_still_counts_as_a_section_of_the_document(self):
        """The one processed section needs extraction and the excluded one does
        not, so the section is named."""
        document = _document(
            _section("1", ["1"], result=False),
            _section("2", ["2"], result=False, excluded=True),
        )

        assert self._saved(document).sections == ("1",)

    def test_nothing_to_extract_starts_at_policy_rule_validation(self):
        assert self._saved(_processed()) == RestartDecision(
            start="policy_rule_validation"
        )

    def test_a_document_with_no_sections_starts_at_policy_rule_validation(self):
        assert self._saved(_document()).start == "policy_rule_validation"

    def test_the_layout_is_not_read(self):
        """Saved edits clear the result of a section they change, so the missing
        result is the whole rule."""
        document = _processed()
        document.sections[0].page_ids = ["9"]

        assert self._saved(document, baseline_layout=layout_of(_processed())) == (
            RestartDecision(start="policy_rule_validation")
        )

    def test_the_mode_does_not_change_the_start(self):
        """The eligibility check maps the start to the mode afterwards."""
        document = _processed()
        document.sections[0].extraction_result_uri = None

        decision = decide_restart(document, kind=RunKind.SAVED_EDITS, mode=BDA)

        assert (decision.start, decision.sections) == ("extraction", ("1",))

    def test_it_needs_the_document(self):
        with pytest.raises(ValueError, match="document"):
            self._saved(None)


class TestOtherKinds:
    @pytest.mark.parametrize(
        "kind",
        [
            kind
            for kind in RunKind
            if kind
            not in (RunKind.SENT_BACK, RunKind.REVIEW_COMPLETED, RunKind.SAVED_EDITS)
        ],
    )
    def test_no_other_kind_is_decided_here(self, kind):
        with pytest.raises(ValueError, match=kind.value):
            decide_restart(_processed(), kind=kind, mode=PIPELINE)


class TestTheDecision:
    def test_the_defaults(self):
        decision = RestartDecision(start="apply_review_changes")

        assert (decision.sections, decision.causes) == ((), {})

    def test_it_is_frozen(self):
        decision = RestartDecision(start="extraction", sections=("1",))

        with pytest.raises(ValidationError):
            decision.start = "classification"  # type: ignore[misc]

    def test_the_public_names(self):
        assert set(restart.__all__) == {
            "CAUSE_ADDED",
            "CAUSE_NO_RESULT",
            "CAUSE_PAGES_CHANGED",
            "MAX_LAYOUT_SECTIONS",
            "SEND_BACK_START",
            "RestartDecision",
            "decide_restart",
            "layout_of",
        }


_IMPORT_PROBE = r"""
import json
import sys

before = set(sys.modules)
import idp_common.run_plan.restart

added = set(sys.modules) - before
print(json.dumps(sorted(m for m in added if m.split(".")[0] == "idp_common")))
"""


def test_the_module_loads_the_step_list_and_the_plan_model_only():
    """The decision is made without loading the review code or the models of a
    document."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    run = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    loaded = json.loads(run.stdout.strip().splitlines()[-1])

    assert loaded == [
        "idp_common",
        "idp_common.run_plan",
        "idp_common.run_plan.models",
        "idp_common.run_plan.restart",
        "idp_common.run_plan.steps",
    ]
