# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Confidence coverage for POPULATED NON-LIST fields (#1474).

The measured defect: the assessment stage recorded confidence for 3 of a form
section's 6 populated fields while every status indicator reported success, and
nothing said which fields were skipped. Because one per-field object carries
confidence, threshold AND geometry together, all three went missing at once — the
UI showed a value with no score and no bounding box, and a field with no
confidence cannot breach a threshold, so it raises no alert and never reaches HITL
review. The safeguard was absent exactly where assessment had failed.

**The cause was not truncation**, which matters because the existing salvage and
retry machinery is all keyed to truncation. Read off the real artifact
(``.../sections/16/result.json``): no ``assessment_batch_split_stats`` in the
metadata, so ``truncated_calls == 0`` and the response never hit the output
ceiling; ``json.loads`` therefore succeeded on the model's own output, meaning
``nova-lite`` returned a syntactically valid object carrying only the first three
keys and stopped. Section 20 of the same packet — same class, same configuration,
same model, same single pass — returned all ten. So this is a small model
answering short, not a budget problem.

Three gaps are pinned here, each independently able to re-open the defect:

1. ``reconcile_assessment_to_data`` padded list rows only, so an omitted scalar
   got no placeholder and stayed ungroundable.
2. There was **no recovery rung at all** for this shape. Both call sites gate the
   row ladder on ``if all_list_fields:``, so a section whose attributes are all
   scalars reached no rung — the ladder that exists to rescue unscored data was
   never offered the one loss it was not written for.
3. ``audit_explainability``'s scalar branch range-checked the leaves it *found*,
   and an omitted field yields none, so absence was silent. The coverage rung
   below it could not reach the section either: its denominator counts list rows
   and it is gated on that being non-zero, so a form with no list attribute has a
   zero denominator and no share of it is ever unscored enough to report.

What is deliberately NOT changed, and is asserted as such below so a reader does
not over-read a green suite: the coverage denominator still counts list rows only.
``confidence_coverage`` feeds the published benchmark ``conf_coverage`` column,
defined over list rows, and the committed grids were measured that way, so
redefining it is a separate decision. ``assessment_fields_unscored`` makes the
shortfall visible without touching that number.
"""

from __future__ import annotations

import time

from idp_common.assessment.batching import (
    _UNASSESSED_OMITTED_FIELD_REASON,
    _is_populated_value,
    _missing_scalar_fields,
    assess_results_batched,
    audit_explainability,
    confidence_coverage,
    merge_split_stats,
    reconcile_assessment_to_data,
    split_stats_are_notable,
)
from idp_common.assessment.service import AssessmentCoreResult

NOVA_LITE = "us.amazon.nova-lite-v1:0"

#: The real section 16 shape: ten defined fields, six populated, four absent.
#: Field order is the emission order from the artifact, because WHICH fields the
#: model dropped (a trailing run) is part of the evidence.
SECTION_16 = {
    "FormId": "1046835",
    "FormType": "NOTIFICATION RE: AUTOMATED TRAFFIC LAW ENFORCEMENT",
    "SubmitterName": "Ruth Wedster",
    "SubmitterId": None,
    "SubmissionDate": "27 Sep 2011",
    "Department": "Chicago Police Department",
    "ProcessedBy": None,
    "ProcessingDate": None,
    "ApprovalStatus": None,
    "Comments": "R/Cpt is completing this in lieu of the officer",
}

#: What the model actually returned for it — the first three keys only.
SECTION_16_ASSESSED = {
    "FormId": {"confidence": 0.98, "confidence_reason": "clear"},
    "FormType": {"confidence": 0.95, "confidence_reason": "clear"},
    "SubmitterName": {"confidence": 0.91, "confidence_reason": "legible"},
}

#: The three populated fields that got nothing.
SECTION_16_DROPPED = ["SubmissionDate", "Department", "Comments"]


def _leaf(conf=0.9, reason="scored"):
    return {"confidence": conf, "confidence_reason": reason}


# --------------------------------------------------------------------------- #
# _is_populated_value / _missing_scalar_fields — the shared predicate
# --------------------------------------------------------------------------- #


def test_absent_values_in_every_spelling_are_not_populated():
    """An absent value needs no confidence, and demanding one would make every
    sparse form report a shortfall. Section 16 defines ten fields and populates
    six; the four empty ones are correctly unscored, so the predicate has to
    agree with extraction's own idea of "not found on the page" — which includes
    the blank string and the empty container, not just ``None``."""
    for absent in (None, "", "   ", "\n\t", {}, []):
        assert _is_populated_value(absent) is False, absent
    for present in ("x", 0, False, 0.0, {"k": "v"}, [1], "0"):
        assert _is_populated_value(present) is True, present


def test_zero_and_false_are_populated_values():
    """Regression guard on the obvious implementation. A truthiness test would
    call ``0``, ``0.0`` and ``False`` absent, and those are real extracted values
    — an amount of 0 or a checkbox read as false is exactly the kind of field a
    reviewer wants a confidence on."""
    assert _is_populated_value(0) is True
    assert _is_populated_value(False) is True
    assert _is_populated_value(0.0) is True


def test_missing_scalar_fields_reports_omitted_fields_in_extraction_order():
    missing = _missing_scalar_fields(SECTION_16_ASSESSED, SECTION_16)
    assert missing == SECTION_16_DROPPED


def test_missing_scalar_fields_counts_a_null_confidence_as_unscored():
    """The same predicate ``_row_confidence_missing`` applies per row: a leaf with
    ``confidence is None`` is not a score. Both halves are needed — absence is the
    model omitting the key, null is the placeholder reconciliation leaves in its
    place, and a recovery rung must still treat that as work to do."""
    assessment = {"name": {"confidence": None, "confidence_reason": "x"}}
    assert _missing_scalar_fields(assessment, {"name": "Ruth"}) == ["name"]


def test_missing_scalar_fields_ignores_absent_values_and_list_fields():
    data = {"name": None, "blank": "", "txns": [{"a": 1}], "kept": "v"}
    assert _missing_scalar_fields({}, data) == ["kept"]


def test_missing_scalar_fields_tolerates_non_dict_inputs():
    assert _missing_scalar_fields(None, SECTION_16) == []
    assert _missing_scalar_fields(SECTION_16_ASSESSED, None) == []
    assert _missing_scalar_fields("not a dict", "neither") == []


# --------------------------------------------------------------------------- #
# reconcile_assessment_to_data — the placeholder (gap 1)
# --------------------------------------------------------------------------- #


def test_an_omitted_populated_scalar_gets_a_null_placeholder():
    out = reconcile_assessment_to_data(dict(SECTION_16_ASSESSED), SECTION_16)
    for field in SECTION_16_DROPPED:
        assert field in out, field
        assert out[field]["confidence"] is None
        assert out[field]["confidence_reason"] == _UNASSESSED_OMITTED_FIELD_REASON


def test_the_placeholder_reason_does_not_claim_a_short_or_undispatched_response():
    """The reason text is what an operator reads per field in the UI, and the
    remedy differs by cause. "returned fewer items than were extracted" is the
    wrong sentence here — nothing was enumerated short — and the deadline reason
    would be false, since the call was made and answered."""
    reason = _UNASSESSED_OMITTED_FIELD_REASON.lower()
    assert "omitted" in reason
    assert "fewer items" not in reason
    assert "time budget" not in reason


def test_an_absent_value_is_not_padded():
    """The four empty fields of section 16 must stay out of the assessment: there
    is nothing to be confident about, and padding them would invent a shortfall
    on every sparsely-populated form."""
    out = reconcile_assessment_to_data(dict(SECTION_16_ASSESSED), SECTION_16)
    for field in ("SubmitterId", "ProcessedBy", "ProcessingDate", "ApprovalStatus"):
        assert field not in out, field


def test_a_blank_string_is_not_padded():
    out = reconcile_assessment_to_data({}, {"blank": "", "spaces": "   "})
    assert out == {}


def test_an_existing_entry_is_never_overwritten_even_under_an_unknown_shape():
    """The data-loss guard. Testing "carries no ``confidence`` leaf" instead of
    "is empty" also fires on an entry whose leaf sits under a different key, and
    padding would then DESTROY a real assessment in order to report it missing —
    turning a reporting gap into data loss, which is strictly worse than the bug.
    Whether such an entry leaves the field effectively unscored is the audit's
    question; it reports without overwriting."""
    assessment = {"name": {"c": 0.9}, "group": {"k": {"c": 0.8}}}
    out = reconcile_assessment_to_data(assessment, {"name": "x", "group": {"k": "v"}})
    assert out["name"] == {"c": 0.9}
    assert out["group"] == {"k": {"c": 0.8}}


def test_a_model_emitted_null_confidence_keeps_its_own_reason():
    """A null the model chose to emit is its statement about the field, not an
    absence to paper over, so the placeholder must not replace it."""
    assessment = {"name": {"confidence": None, "confidence_reason": "illegible"}}
    out = reconcile_assessment_to_data(assessment, {"name": "Ruth"})
    assert out["name"]["confidence_reason"] == "illegible"


def test_an_empty_dict_entry_is_treated_as_absent_and_padded():
    out = reconcile_assessment_to_data({"name": {}}, {"name": "Ruth"})
    assert out["name"]["confidence"] is None
    assert out["name"]["confidence_reason"] == _UNASSESSED_OMITTED_FIELD_REASON


def test_an_omitted_group_gets_a_leaf_per_populated_sub_field():
    """Geometry grounding attaches a box per confidence leaf by matching the
    leaf's own value, so a group needs one leaf per populated sub-field to be
    locatable — the same reason ``_row_placeholder`` mirrors a list row."""
    data = {"Employee": {"name": "Ruth", "id": "7", "middle": None}}
    out = reconcile_assessment_to_data({}, data)
    assert set(out["Employee"].keys()) == {"name", "id"}
    assert out["Employee"]["name"]["confidence"] is None


def test_list_reconciliation_is_unchanged():
    """The padding runs after the list loop and must not disturb it."""
    data = {"txns": [{"a": "1"}, {"a": "2"}, {"a": "3"}], "name": "Ruth"}
    assessment = {"txns": [{"a": _leaf()}], "name": _leaf()}
    out = reconcile_assessment_to_data(assessment, data)
    assert len(out["txns"]) == 3
    assert out["txns"][0]["a"]["confidence"] == 0.9
    assert out["txns"][2]["a"]["confidence"] is None
    assert out["name"]["confidence"] == 0.9


def test_reconcile_is_idempotent_over_the_padding():
    """A second pass must not re-pad what it already padded, or re-report it."""
    once = reconcile_assessment_to_data(dict(SECTION_16_ASSESSED), SECTION_16)
    twice = reconcile_assessment_to_data(dict(once), SECTION_16)
    assert twice == once


# --------------------------------------------------------------------------- #
# The recovery rung (gap 2)
# --------------------------------------------------------------------------- #


class ShortThenComplete:
    """Confidence model stand-in reproducing the measured #1474 behaviour.

    The first call returns a VALID object carrying only the first
    ``first_pass_fields`` keys — not a truncation, which is the whole point:
    ``truncated`` is False and ``parsing_succeeded`` is True, so none of the
    existing salvage or retry machinery is triggered. A later call answers about
    everything it is handed, which is not a hypothesis: the next section of the
    same packet, same class and same model, returned all ten fields in one pass.
    """

    def __init__(self, first_pass_fields: int = 3, answer_retry: bool = True):
        self.first_pass_fields = first_pass_fields
        self.answer_retry = answer_retry
        self.calls: list[list[str]] = []

    def assess_results(self, **kw):
        results = kw["extraction_results"]
        scalars = [k for k, v in results.items() if not isinstance(v, list)]
        self.calls.append(list(results.keys()))
        first = len(self.calls) == 1
        if first:
            answered = scalars[: self.first_pass_fields]
        elif self.answer_retry:
            answered = scalars
        else:
            answered = []
        assessment = {k: _leaf(0.97) for k in answered}
        for k, v in results.items():
            if isinstance(v, list):
                assessment[k] = [{"a": _leaf(0.9)} for _ in v]
        return AssessmentCoreResult(
            enhanced_assessment=assessment,
            parsing_succeeded=True,
            truncated=False,
            duration_seconds=0.5,
            metering={"calls": 1},
        )

    def _resolve_confidence_escalation_model(self, class_label: str):
        return None


def _run(svc, results, **kw):
    kw.setdefault("batch_size", 4)
    kw.setdefault("max_retries", 2)
    return assess_results_batched(
        svc,
        class_label="form",
        extraction_results=results,
        document_text="...",
        page_images=[],
        confidence_model_id=NOVA_LITE,
        geometry_mode="ocr_only",
        **kw,
    )


def test_a_section_with_no_list_field_reaches_the_recovery_rung_at_all():
    """The core regression guard for gap 2. Both recovery call sites gate the row
    ladder on ``if all_list_fields:``; section 16 has no list attribute, so before
    this it made exactly ONE model call and the three dropped fields were
    unrecoverable by construction."""
    svc = ShortThenComplete()
    out = _run(svc, dict(SECTION_16))
    assert len(svc.calls) == 2, "no second call means no recovery rung ran"
    assert _missing_scalar_fields(out["assessment"], SECTION_16) == []
    for field in SECTION_16_DROPPED:
        assert out["assessment"][field]["confidence"] == 0.97


def test_the_retry_asks_about_only_the_missing_fields():
    """The same narrowing ``_splice_missing_rows`` applies to rows: the prompt
    shrinks as fields are recovered, so a model that omitted keys under a long
    field list is not handed the same long list again."""
    svc = ShortThenComplete()
    _run(svc, dict(SECTION_16))
    assert svc.calls[1] == SECTION_16_DROPPED


def test_recovery_is_recorded_in_split_stats():
    svc = ShortThenComplete()
    out = _run(svc, dict(SECTION_16))
    stats = out["split_stats"]
    assert stats["scalar_retry_calls"] == 1
    assert stats["fields_recovered_by_retry"] == 3
    assert stats["unrecoverable_fields"] == 0


def test_a_retry_that_recovers_nothing_stops_and_counts_the_residual():
    """Bounded, and honest about the outcome: the placeholders stay, the rung does
    not spend its remaining rounds, and ``unrecoverable_fields`` is what a future
    escalation step would be justified by."""
    svc = ShortThenComplete(answer_retry=False)
    out = _run(svc, dict(SECTION_16), max_retries=3)
    assert len(svc.calls) == 2, "a round recovering nothing must stop the rung"
    stats = out["split_stats"]
    assert stats["fields_recovered_by_retry"] == 0
    assert stats["unrecoverable_fields"] == 3
    for field in SECTION_16_DROPPED:
        assert out["assessment"][field]["confidence"] is None


def test_a_retry_answering_with_a_null_leaf_is_not_counted_as_recovery():
    """Overwriting the placeholder with a null leaf would replace a placeholder
    that says WHY the field is unscored with one that says nothing, and would
    count as a recovery while recovering nothing."""

    class NullOnRetry(ShortThenComplete):
        def assess_results(self, **kw):
            results = kw["extraction_results"]
            self.calls.append(list(results.keys()))
            if len(self.calls) == 1:
                answered = {k: _leaf(0.97) for k in list(results)[:3]}
            else:
                answered = {k: {"confidence": None} for k in results}
            return AssessmentCoreResult(
                enhanced_assessment=answered,
                parsing_succeeded=True,
                truncated=False,
                duration_seconds=0.1,
            )

    svc = NullOnRetry()
    out = _run(svc, dict(SECTION_16))
    assert out["split_stats"]["fields_recovered_by_retry"] == 0
    assert out["split_stats"]["unrecoverable_fields"] == 3
    for field in SECTION_16_DROPPED:
        assert (
            out["assessment"][field]["confidence_reason"]
            == _UNASSESSED_OMITTED_FIELD_REASON
        )


def test_a_failing_recovery_call_keeps_the_placeholders():
    """Best-effort, like every other rung: a raised exception must not fail the
    section, and the placeholder is the honest residual."""

    class RaisesOnRetry(ShortThenComplete):
        def assess_results(self, **kw):
            self.calls.append(list(kw["extraction_results"].keys()))
            if len(self.calls) > 1:
                raise RuntimeError("bedrock unavailable")
            return AssessmentCoreResult(
                enhanced_assessment={
                    k: _leaf(0.97) for k in list(kw["extraction_results"])[:3]
                },
                parsing_succeeded=True,
                truncated=False,
                duration_seconds=0.1,
            )

    svc = RaisesOnRetry()
    out = _run(svc, dict(SECTION_16))
    assert out["split_stats"]["unrecoverable_fields"] == 3
    for field in SECTION_16_DROPPED:
        assert out["assessment"][field]["confidence"] is None


def test_an_exhausted_time_budget_does_not_start_a_recovery_call():
    """Wall-clock guarded on the same terms as every other rung (#958): one call
    is priced, and a round that will not fit is not started."""
    svc = ShortThenComplete()
    out = _run(svc, dict(SECTION_16), deadline_epoch=time.time() + 1.0)
    assert len(svc.calls) == 1, "no budget should mean no recovery call"
    assert out["split_stats"]["deadline_reached"] is True
    assert out["split_stats"]["unrecoverable_fields"] == 3


def test_a_fully_scored_section_makes_no_recovery_call():
    """Cost guard: the rung is unconditional, so it must be free when there is
    nothing to recover."""
    svc = ShortThenComplete(first_pass_fields=10)
    out = _run(svc, dict(SECTION_16))
    assert len(svc.calls) == 1
    assert out["split_stats"]["scalar_retry_calls"] == 0
    assert split_stats_are_notable(out["split_stats"]) is False


def test_a_first_batch_that_omits_a_scalar_does_not_shadow_a_later_batch_score():
    """The regression the padding itself introduces if it runs per chunk.

    The batched path takes the FIRST slice whose ``scalars`` is non-empty —
    ``left["scalars"] or right["scalars"]`` in the bisection merge, ``if not
    merged_assessment`` in the chunk loop. Padding inside the per-chunk reconcile
    makes every slice's ``scalars`` non-empty by construction, so a first chunk
    that omitted a scalar would shadow a later chunk's REAL score with a null
    placeholder: data the pipeline already had, lost to the fix for data it did
    not. Hence ``_reconcile_rows_only`` for intermediate slices, and padding only
    at the final assembly.

    The recovery rung would re-ask and usually paper over this, so the assertion
    that matters is the call count: the real score must arrive with no extra call.
    """

    class FirstChunkOmitsScalar:
        def __init__(self):
            self.calls: list[list[str]] = []

        def assess_results(self, **kw):
            results = kw["extraction_results"]
            self.calls.append(list(results.keys()))
            rows = results.get("txns", [])
            assessment = {"txns": [{"a": _leaf(0.9)} for _ in rows]}
            # Only the SECOND chunk answers about the scalar.
            if len(self.calls) > 1:
                assessment["Department"] = _leaf(0.88)
            return AssessmentCoreResult(
                enhanced_assessment=assessment,
                parsing_succeeded=True,
                truncated=False,
                duration_seconds=0.1,
            )

        def _resolve_confidence_escalation_model(self, class_label: str):
            return None

    svc = FirstChunkOmitsScalar()
    results = {
        "Department": "Chicago Police Department",
        "txns": [{"a": str(i)} for i in range(8)],
    }
    out = _run(svc, results, batch_size=4)
    assert out["assessment"]["Department"]["confidence"] == 0.88
    assert out["split_stats"]["scalar_retry_calls"] == 0, (
        "the real score was shadowed by a placeholder and then re-fetched"
    )
    assert out["split_stats"]["fields_recovered_by_retry"] == 0


def test_the_batched_path_recovers_an_omitted_scalar_too():
    """Batching slices the big LIST while the scalars ride every batch, so a
    response that omits a scalar key omits it on the batch the merged scalars came
    from. Both call sites therefore need the rung."""
    svc = ShortThenComplete(first_pass_fields=1)
    results = {
        "FormId": "1046835",
        "Department": "Chicago Police Department",
        "txns": [{"a": str(i)} for i in range(9)],
    }
    out = _run(svc, results, batch_size=4)
    assert _missing_scalar_fields(out["assessment"], results) == []
    assert out["split_stats"]["fields_recovered_by_retry"] >= 1
    assert len(out["assessment"]["txns"]) == 9


# --------------------------------------------------------------------------- #
# audit_explainability — the completeness gate (gap 3)
# --------------------------------------------------------------------------- #


def test_an_omitted_populated_field_is_reported_as_a_warning():
    gaps, issues = audit_explainability(SECTION_16_ASSESSED, SECTION_16)
    codes = [i.code for i in issues]
    assert "assessment_fields_unscored" in codes
    issue = next(i for i in issues if i.code == "assessment_fields_unscored")
    assert issue.severity == "warning"
    assert issue.details["unscored_fields"] == SECTION_16_DROPPED
    assert issue.details["populated_fields"] == 6
    assert issue.details["scored_fields"] == 3
    assert "3 of 6" in issue.message
    assert gaps == {}


def test_no_populated_field_scored_at_all_is_an_error():
    """The limit case rather than a partial shortfall, the same distinction the
    coverage rung draws: there is no confidence surface over the section's fields,
    so no threshold applies to any of them."""
    _gaps, issues = audit_explainability({}, SECTION_16)
    issue = next(i for i in issues if i.code == "assessment_fields_unscored")
    assert issue.severity == "error"
    assert issue.details["scored_fields"] == 0
    assert "None of the 6 populated field(s)" in issue.message


def test_a_fully_scored_section_reports_nothing():
    assessment = {k: _leaf() for k, v in SECTION_16.items() if v is not None}
    _gaps, issues = audit_explainability(assessment, SECTION_16)
    assert [i.code for i in issues] == []


def test_the_message_names_at_most_five_fields_and_counts_the_rest():
    data = {f"F{i}": f"v{i}" for i in range(9)}
    _gaps, issues = audit_explainability({"F0": _leaf()}, data)
    issue = next(i for i in issues if i.code == "assessment_fields_unscored")
    assert "+3 more" in issue.message
    assert len(issue.details["unscored_fields"]) == 8


def test_scalar_findings_stay_out_of_gaps_and_cannot_corrupt_coverage():
    """The correctness guard on the reporting choice. ``gaps`` is keyed field ->
    row INDEX and its lengths are the numerator ``coverage_from_gaps`` divides by
    a denominator counting list rows only. A scalar entry there makes
    ``unscored_rows`` exceed ``expected_rows`` and drives ``scored_rows``
    negative — a corrupted measurement shipped as a fixed bug."""
    gaps, _issues = audit_explainability(SECTION_16_ASSESSED, SECTION_16)
    assert gaps == {}
    coverage = confidence_coverage(SECTION_16_ASSESSED, SECTION_16)
    assert coverage["expected_rows"] == 0
    assert coverage["unscored_rows"] == 0
    assert coverage["scored_rows"] == 0
    assert coverage["scored_fraction"] is None


def test_a_mixed_section_keeps_the_two_numerators_separate():
    """With a list present, the row gaps and the field findings must not bleed
    into one another: coverage counts the rows, the issue counts the fields."""
    data = {"name": "Ruth", "txns": [{"a": "1"}, {"a": "2"}]}
    assessment = {"txns": [{"a": _leaf()}, {"a": {"confidence": None}}]}
    gaps, issues = audit_explainability(assessment, data)
    assert gaps == {"txns": [1]}
    coverage = confidence_coverage(assessment, data)
    assert (coverage["expected_rows"], coverage["unscored_rows"]) == (2, 1)
    assert coverage["scored_rows"] == 1
    issue = next(i for i in issues if i.code == "assessment_fields_unscored")
    assert issue.details["unscored_fields"] == ["name"]


def test_the_coverage_rung_still_cannot_reach_a_section_with_no_list_field():
    """Asserted so a green suite is not over-read: the coverage DENOMINATOR is
    unchanged and still counts list rows only, because ``confidence_coverage``
    feeds the published benchmark ``conf_coverage`` column and the committed grids
    were measured that way. The new issue is what makes this section's shortfall
    visible; ``assessment_coverage_incomplete`` remains unreachable here."""
    _gaps, issues = audit_explainability({}, SECTION_16)
    codes = [i.code for i in issues]
    assert "assessment_coverage_incomplete" not in codes
    assert "assessment_fields_unscored" in codes


def test_the_field_issue_is_not_suppressed_by_a_ladder_error():
    """Unlike the coverage rung, which a ladder error covers with a cause
    attached. A ladder error is about list ROWS; these are different values, so
    reporting both is two findings rather than one counted twice."""
    from idp_common.models import ProcessingIssue

    ladder = [
        ProcessingIssue(
            stage="assessment",
            severity="error",
            code="assessment_incomplete",
            message="rows unscored",
        )
    ]
    _gaps, issues = audit_explainability(
        SECTION_16_ASSESSED, SECTION_16, ladder_issues=ladder
    )
    assert "assessment_fields_unscored" in [i.code for i in issues]


def test_an_absent_value_is_never_reported_as_unscored():
    """The four empty fields of section 16 must not inflate the finding."""
    _gaps, issues = audit_explainability({}, SECTION_16)
    issue = next(i for i in issues if i.code == "assessment_fields_unscored")
    for field in ("SubmitterId", "ProcessedBy", "ProcessingDate", "ApprovalStatus"):
        assert field not in issue.details["unscored_fields"]


# --------------------------------------------------------------------------- #
# split_stats plumbing
# --------------------------------------------------------------------------- #


def test_the_scalar_counters_make_a_run_notable():
    """The section that prompted this had no list attribute, so every row-shaped
    term was 0 and the metadata block was omitted entirely — which is how a run
    that dropped three of six populated fields left no trace in its own artifact
    beyond the missing keys."""
    assert split_stats_are_notable({"unrecoverable_fields": 1}) is True
    assert split_stats_are_notable({"scalar_retry_calls": 1}) is True
    assert split_stats_are_notable({"fields_recovered_by_retry": 1}) is True
    assert split_stats_are_notable({}) is False


def test_merge_split_stats_sums_the_scalar_counters():
    """A sharded section asks each shard about its own fields, so the omissions
    and the recoveries are disjoint across shards and add up."""
    merged = merge_split_stats(
        {"scalar_retry_calls": 1, "fields_recovered_by_retry": 2},
        {"scalar_retry_calls": 2, "fields_recovered_by_retry": 1},
    )
    assert merged is not None
    assert merged["scalar_retry_calls"] == 3
    assert merged["fields_recovered_by_retry"] == 3
