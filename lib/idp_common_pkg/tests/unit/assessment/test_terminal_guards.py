# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Terminal guards for the assessment self-healing ladder (#894, #901 item 3).

Two behaviours are locked in here, both of which turn an unbounded or silent
failure into a bounded, reported one:

- **#894 oversized row.** The adaptive splitter halves a batch whose response the
  model truncated. That converges only while a smaller batch can fit. With a
  multi-instance class (``x-aws-idp-multi-instance: true``) one "row" of the outer
  list is a WHOLE document instance carrying its own long inner list, so a call
  with a single row still truncates and halving cannot help. The ladder had no
  terminal condition for that: it kept spending model calls on a batch that could
  not fit, and then reported the generic "rows could not be scored", whose remedy
  (shrink ``list_batch_size``) is the one thing that provably cannot work. It now
  stops at one row, records the cause, and reports ``assessment_row_too_large``
  naming the model, its output cap and the offending row's size, because the remedy
  is a larger-output model or a smaller list item — never a smaller batch.

  ⚠️ What these tests do NOT establish is the cause of the 900 s
  ``Sandbox.Timedout`` ×3 reported in #894. The saving here is real on multi-row
  shapes (see the call-count assertions) and the diagnosis is right on the
  single-outer-row shape #894 actually reports, but the same-model retry rung
  already stopped on no progress before this change and the wall-clock deadline
  guard was already present in the release where the timeouts were observed. #894
  stays open both for the batch sizer and for the timeout's cause.

- **#901 item 3 coverage.** ``audit_explainability`` already computes which rows
  carry no confidence, but every caller discarded that, so a section could return
  a fraction of its rows scored and still report unqualified success. Coverage
  materially short of the extracted list length now emits
  ``assessment_coverage_incomplete`` (warning, escalating to error).

Note on scope: the batch SIZER that mis-estimates a multi-instance row's output
(``cols=2 per_row~80`` for a row carrying 100 transactions) is NOT fixed here and
#894 stays open for it. These tests pin the give-up behaviour only.
"""

from __future__ import annotations

from idp_common.assessment.batching import (
    assess_results_batched,
    audit_explainability,
    build_assessment_issues,
    format_split_stats_report,
    merge_split_stats,
    split_stats_are_notable,
)
from idp_common.assessment.service import AssessmentCoreResult

NOVA_LITE = "us.amazon.nova-lite-v1:0"
CLAUDE_SONNET = "us.anthropic.claude-sonnet-4-20250514-v1:0"


def _rows(n: int) -> list[dict]:
    """Outer rows that each carry a long inner list — the #894 shape."""
    return [
        {
            "account_number": f"{1000 + i}",
            "Transactions": [
                {"date": f"2020-01-{d:02d}", "amount": f"{d}.00"} for d in range(1, 31)
            ],
        }
        for i in range(n)
    ]


class AlwaysTruncates:
    """Confidence model stand-in that truncates EVERY call, even a 1-row call.

    Mirrors the #894 log evidence: ``stopReason=max_tokens`` on every attempt, at
    every batch size the splitter tried, so shrinking recovered nothing. Records
    every call so a test can prove the ladder stopped instead of looping.
    """

    def __init__(self, list_field: str, escalation_model: str | None = None):
        self.list_field = list_field
        self.escalation_model = escalation_model
        self.primary_calls: list[int] = []
        self.escalation_calls: list[int] = []

    def assess_results(self, **kw):
        rows = kw["extraction_results"].get(self.list_field, [])
        model = kw.get("model_id_override")
        if self.escalation_model and model == self.escalation_model:
            # The stronger model scores everything it is handed cleanly.
            self.escalation_calls.append(len(rows))
            return AssessmentCoreResult(
                enhanced_assessment={
                    self.list_field: [
                        {"account_number": {"confidence": 0.93}} for _ in rows
                    ],
                },
                parsing_succeeded=True,
                truncated=False,
                duration_seconds=1.0,
                metering={},
            )
        self.primary_calls.append(len(rows))
        return AssessmentCoreResult(
            enhanced_assessment={
                k: {"confidence": 0.5, "confidence_reason": "default"}
                for k in kw["extraction_results"]
            },
            parsing_succeeded=False,
            truncated=True,
            duration_seconds=1.0,
            metering={},
        )

    def _resolve_confidence_escalation_model(self, class_label: str):
        return self.escalation_model


def _run(svc, *, rows, max_retries, batch_size=4, **kw):
    return assess_results_batched(
        svc,
        class_label="mi-wrapped",
        extraction_results={"statements": rows},
        document_text="...",
        page_images=[],
        batch_size=batch_size,
        confidence_model_id=NOVA_LITE,
        geometry_mode="ocr_only",
        max_retries=max_retries,
        **kw,
    )


# --------------------------------------------------------------------------- #
# #894 — the ladder gives up at one row instead of retrying
# --------------------------------------------------------------------------- #
def test_single_row_truncation_stops_the_retry_rung():
    """A batch that still truncates at size 1 makes NO further model calls than a
    run configured with zero retries — i.e. the retry rung is skipped rather than
    burning ``max_retries`` more rounds of the same impossible call."""
    rows = _rows(8)

    no_retries = AlwaysTruncates("statements")
    baseline = _run(no_retries, rows=rows, max_retries=0, escalation_enabled=False)

    with_retries = AlwaysTruncates("statements")
    guarded = _run(with_retries, rows=rows, max_retries=2, escalation_enabled=False)

    # The guard, not the retry budget, decided when to stop: identical call counts.
    assert len(with_retries.primary_calls) == len(no_retries.primary_calls)
    # ... and the calls really did bisect down to a single row.
    assert 1 in with_retries.primary_calls
    assert guarded["split_stats"]["rows_recovered_by_retry"] == 0
    assert baseline["split_stats"]["unrecoverable_rows"] == len(rows)
    assert guarded["split_stats"]["unrecoverable_rows"] == len(rows)

    stats = guarded["split_stats"]
    assert stats["oversized_row_fields"] == ["statements"]
    assert stats["oversized_row_model"] == NOVA_LITE
    # Nova Lite's output cap and the offending row's serialized size are recorded
    # so the reported message can be actionable.
    assert stats["oversized_row_output_cap"]
    assert stats["oversized_row_chars"] > 0
    assert stats["oversized_row_class"] == "mi-wrapped"
    assert split_stats_are_notable(stats)


def test_single_outer_row_with_a_larger_batch_size_is_diagnosed_not_shortened():
    """The shape #894 actually reports: ONE multi-instance outer row, sizer-derived
    batch of 12.

    This pins an asymmetry worth being explicit about. With ``len(rows) <=
    batch_size`` there is no list field large enough to batch, so
    ``assess_results_batched`` takes its ``if not list_fields:`` branch and calls the
    model directly — ``_assess_slice_adaptive`` (where the terminal condition is
    detected) is not reached on the first pass, so the *pre-rung* skip cannot fire.
    The condition is instead detected inside the first retry round, which then stops
    the rung.

    So on this shape the guard saves **no model calls** (2 with it, 2 without: the
    retry rung already stopped on "no progress"). What it changes is the diagnosis:
    the section now reports ``assessment_row_too_large`` naming the model, its
    output cap and the row size, instead of ``assessment_incomplete``, whose remedy
    ("shrink the batch") cannot work. The call-count saving is real only on
    multi-row shapes, where the first pass bisects — see
    ``test_single_row_truncation_stops_the_retry_rung``.
    """
    svc = AlwaysTruncates("statements")
    result = _run(
        svc, rows=_rows(1), max_retries=2, batch_size=12, escalation_enabled=False
    )
    stats = result["split_stats"]

    # One direct call (1 row <= batch 12, so no batching) + one retry round that
    # detects the terminal condition and stops the rung. NOT max_retries rounds.
    assert svc.primary_calls == [1, 1]
    assert stats["oversized_row_fields"] == ["statements"]
    assert stats["oversized_row_model"] == NOVA_LITE
    assert stats["oversized_row_output_cap"]
    assert stats["oversized_row_class"] == "mi-wrapped"
    assert stats["unrecoverable_rows"] == 1
    assert stats["rows_recovered_by_retry"] == 0

    issue = build_assessment_issues(stats, section_id="1", confidence_model=NOVA_LITE)[
        0
    ]
    assert issue.code == "assessment_row_too_large"
    assert issue.severity == "error"
    assert NOVA_LITE in issue.message
    assert "list_batch_size cannot help" in issue.message


def test_oversized_row_issue_names_model_cap_and_cause():
    """The surfaced issue must name the model, its output cap, the field and class,
    the row size, and say that a smaller batch cannot help — the operator's next
    action differs completely from the generic 'rows could not be scored'."""
    svc = AlwaysTruncates("statements")
    result = _run(svc, rows=_rows(4), max_retries=2, escalation_enabled=False)
    stats = result["split_stats"]

    issues = build_assessment_issues(
        stats,
        section_id="1",
        confidence_model=NOVA_LITE,
        geometry_mode="ocr_only",
    )
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "assessment_row_too_large"
    assert issue.severity == "error"
    assert issue.stage == "assessment"
    assert NOVA_LITE in issue.message
    assert str(stats["oversized_row_output_cap"]) in issue.message
    assert "statements" in issue.message
    assert "mi-wrapped" in issue.message
    assert str(stats["oversized_row_chars"]) in issue.message
    # The remedy: bigger output budget / smaller list item, NOT a smaller batch.
    assert "larger output budget" in issue.message
    assert "list_batch_size cannot help" in issue.message
    # And the human-readable report block says the same thing.
    report = format_split_stats_report(stats)
    assert "Row too large to score" in report
    assert NOVA_LITE in report


def test_oversized_row_still_allows_one_escalation_round():
    """Giving up on the retry rung must NOT block the one remedy that can work: a
    model with a bigger output cap. Escalation still runs (and here recovers every
    row), while the futile same-model retries stay skipped."""
    svc = AlwaysTruncates("statements", escalation_model=CLAUDE_SONNET)
    result = _run(
        svc,
        rows=_rows(6),
        max_retries=2,
        escalation_enabled=True,
        escalation_model=CLAUDE_SONNET,
        max_escalation_rounds=2,
    )
    stats = result["split_stats"]

    assert svc.escalation_calls, "the stronger model must still be tried"
    assert stats["escalation_rounds"] == 1
    assert stats["rows_recovered_by_retry"] == 0
    assert stats["rows_recovered_by_escalation"] == 6
    assert stats["unrecoverable_rows"] == 0
    # Recovered, so the error rung does not fire — the run is reported as
    # self-healed (info) even though the terminal condition was hit.
    codes = [i.code for i in build_assessment_issues(stats, confidence_model=NOVA_LITE)]
    assert codes == ["assessment_recovered_with_retries"]


def test_oversized_row_flag_survives_shard_merge():
    """Two shards' stats merge into one: the oversized flag, the largest cap seen
    and the biggest offending row must all survive (a shard-local give-up that
    vanished in the merge would be reported as a generic incomplete)."""
    a = {
        "oversized_row_fields": ["statements"],
        "oversized_row_model": NOVA_LITE,
        "oversized_row_output_cap": 10000,
        "oversized_row_chars": 4000,
        "oversized_row_class": "mi-wrapped",
        "unrecoverable_rows": 3,
    }
    b = {
        "oversized_row_fields": ["statements", "other"],
        "oversized_row_model": CLAUDE_SONNET,
        "oversized_row_output_cap": 64000,
        "oversized_row_chars": 9000,
        "unrecoverable_rows": 2,
    }
    merged = merge_split_stats(a, b)
    assert merged is not None
    assert merged["oversized_row_fields"] == ["statements", "other"]
    # The largest cap that still truncated is the most damning evidence.
    assert merged["oversized_row_model"] == CLAUDE_SONNET
    assert merged["oversized_row_output_cap"] == 64000
    assert merged["oversized_row_chars"] == 9000
    assert merged["oversized_row_class"] == "mi-wrapped"
    assert merged["unrecoverable_rows"] == 5


# --------------------------------------------------------------------------- #
# #901 item 3 — partial confidence coverage is reported, not silent
# --------------------------------------------------------------------------- #
def _coverage_case(total: int, scored: int) -> tuple[dict, dict]:
    """Extraction with ``total`` rows whose first ``scored`` rows have confidence."""
    data = {"transactions": [{"amount": f"{i}.00"} for i in range(total)]}
    assessed = [
        {"amount": {"confidence": 0.9}}
        if i < scored
        else {"amount": {"confidence": None}}
        for i in range(total)
    ]
    return {"transactions": assessed}, data


def _coverage_issue(total: int, scored: int, ladder_issues=None):
    assessment, data = _coverage_case(total, scored)
    _gaps, issues = audit_explainability(
        assessment,
        data,
        geometry_mode="off",
        section_id="1",
        ladder_issues=ladder_issues,
    )
    found = [i for i in issues if i.code == "assessment_coverage_incomplete"]
    return found[0] if found else None


def test_full_coverage_reports_no_coverage_issue():
    """A fully-scored section must stay silent — the guard cannot become noise on
    the healthy path (which is every well-behaved document)."""
    assert _coverage_issue(100, 100) is None
    # Just under the 5% "materially short" line: still the ladder's business, not
    # a document-level alarm.
    assert _coverage_issue(100, 96) is None


def test_short_coverage_reports_a_warning():
    """Past 5% unscored the confidence surface stops being trustworthy as a whole,
    so the shortfall is surfaced with its counts."""
    issue = _coverage_issue(100, 90)
    assert issue is not None
    assert issue.severity == "warning"
    assert issue.details["expected_rows"] == 100
    assert issue.details["scored_rows"] == 90
    assert issue.details["unscored_rows"] == 10
    assert issue.details["unscored_rows_by_field"] == {"transactions": 10}
    assert "90 of 100" in issue.message


def test_severely_short_coverage_reports_an_error():
    """The #901 shape — roughly a quarter of rows scored — is an error, and names
    the field and counts. It deliberately makes no claim about WHY coverage fell
    (that cause was never established); it only stops the silence."""
    issue = _coverage_issue(1200, 288)  # 24% scored
    assert issue is not None
    assert issue.severity == "error"
    assert issue.details["unscored_rows"] == 912
    assert "'transactions'" in issue.message
    assert "extracted values themselves are unaffected" in issue.message


def test_error_severity_needs_an_absolute_row_floor_not_just_a_fraction():
    """A short list must not produce an ERROR, which the UI renders as a red
    "Incomplete" section.

    One unscored row in a four-row list is 25% — and short list attributes are
    ordinary (a two-entry ENDORSEMENTS array in the shipped lending-package sample),
    where a single ``None`` confidence leaf marks the whole row unscored. Error
    severity therefore also requires 10+ unscored rows in absolute terms; below that
    the shortfall is still reported, as a warning.
    """
    # Fraction well past 25%, absolute count tiny → warning, never error.
    for total, scored in ((2, 1), (3, 2), (4, 3), (5, 4), (20, 19)):
        issue = _coverage_issue(total, scored)
        assert issue is not None, (total, scored)
        assert issue.severity == "warning", (total, scored)

    # Exactly at 25% but only 9 unscored rows → still a warning (floor not met).
    nine = _coverage_issue(36, 27)
    assert nine is not None
    assert nine.details["unscored_rows"] == 9
    assert nine.severity == "warning"

    # 25% AND 10 unscored rows → error.
    ten = _coverage_issue(40, 30)
    assert ten is not None
    assert ten.details["unscored_rows"] == 10
    assert ten.severity == "error"


def test_zero_coverage_does_not_claim_partial_coverage():
    """At 0% scored, "covers only part of this section" is simply false, and this
    audit cannot vouch for the extracted data either — it only knows nothing was
    scored."""
    issue = _coverage_issue(30, 0)
    assert issue is not None
    assert issue.severity == "error"
    assert "None of the 30 extracted list row(s)" in issue.message
    assert "covers only part" not in issue.message
    assert "complete" not in issue.message


def test_coverage_issue_is_suppressed_when_the_ladder_already_reported_an_error():
    """The ladder's own error rung describes the SAME unscored rows WITH a cause.

    Emitting both doubles ``ProcessingIssueCount`` with two counts that can
    legitimately disagree (``unrecoverable_rows`` tracks only the largest list field,
    this audit counts every list field), and in the schema-mismatch case it appends
    "the extracted values themselves are unaffected" directly under a diagnosis that
    says extraction produced off-schema data.
    """
    # The #901 shape with the ladder reporting assessment_incomplete for it.
    ladder = build_assessment_issues(
        {"unrecoverable_rows": 912, "truncated_calls": 3},
        section_id="1",
        confidence_model=NOVA_LITE,
    )
    assert [i.code for i in ladder] == ["assessment_incomplete"]
    assert _coverage_issue(1200, 288, ladder_issues=ladder) is None

    # Schema mismatch keeps its "emitted alone" contract.
    mismatch = build_assessment_issues(
        {"schema_mismatch_fields": ["transactions"], "unrecoverable_rows": 1200},
        section_id="1",
        confidence_model=NOVA_LITE,
    )
    assert [i.code for i in mismatch] == ["assessment_schema_mismatch"]
    assert _coverage_issue(1200, 0, ladder_issues=mismatch) is None

    # A non-error ladder issue (info/warning) does NOT suppress it: those do not
    # claim the rows are unscored, so the coverage shortfall is still news.
    recovered = build_assessment_issues(
        {"rows_recovered_by_retry": 4, "truncated_calls": 1},
        section_id="1",
        confidence_model=NOVA_LITE,
    )
    assert [i.code for i in recovered] == ["assessment_recovered_with_retries"]
    assert _coverage_issue(1200, 288, ladder_issues=recovered) is not None
    # Plain dicts are accepted too (either composition site may pass serialized ones).
    assert _coverage_issue(1200, 288, ladder_issues=[{"severity": "error"}]) is None
