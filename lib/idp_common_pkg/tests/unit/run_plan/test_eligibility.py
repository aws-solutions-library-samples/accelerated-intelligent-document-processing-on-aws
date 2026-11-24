# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Whether a document can be run again from a step: one case per refusal code."""

from types import SimpleNamespace

import pytest

from idp_common.models import (
    Document,
    Page,
    ProcessingIssue,
    RuleValidationResult,
    Section,
    Status,
)
from idp_common.run_plan import eligibility
from idp_common.run_plan.eligibility import (
    IN_PROGRESS_STATUSES,
    MALWARE_REFUSED_STATUSES,
    EligibilityContext,
    ReprocessRequest,
    check,
    preprocessing_hook_enabled,
    resolve_resume,
    reviewed_for,
)
from idp_common.run_plan.mode import ProcessingMode
from idp_common.run_plan.models import RunKind
from idp_common.run_plan.steps import BDA, PIPELINE

pytestmark = pytest.mark.unit

KEY = "packet.pdf"
PROFILE = "lending"

_POLICIES = {
    "rule_validation": {"enabled": True},
    "policy_classes": [{"$id": "p", "rule_properties": {}}],
}


def _document(status=Status.COMPLETED, **overrides) -> Document:
    document = Document(
        id=KEY,
        input_key=KEY,
        status=status,
        config_version=PROFILE,
        config_revision=3,
    )
    for page_id in ("1", "2"):
        document.pages[page_id] = Page(
            page_id=page_id,
            image_uri=f"s3://b/{KEY}/pages/{page_id}/image.jpg",
            parsed_text_uri=f"s3://b/{KEY}/pages/{page_id}/result.json",
            classification="Invoice",
        )
    for section_id in ("1", "2"):
        document.sections.append(
            Section(
                section_id=section_id,
                classification="Invoice",
                page_ids=[section_id],
                extraction_result_uri=f"s3://b/{KEY}/sections/{section_id}/result.json",
            )
        )
    document.rule_validation_result = RuleValidationResult(
        request_id="r", output_uri="s3://b/summary.md"
    )
    document.summary_report_uri = "s3://b/summary/summary.md"
    for name, value in overrides.items():
        setattr(document, name, value)
    return document


def _context(
    document=None,
    *,
    modes=None,
    published=None,
    config=None,
    load=None,
) -> EligibilityContext:
    """A context over one document. ``modes`` maps a revision to its mode."""
    modes = modes or {}
    base = config if config is not None else dict(_POLICIES)

    def mode_for(profile, revision):
        mode, with_hook = modes.get(revision, (PIPELINE, False))
        revision_config = dict(base)
        if with_hook:
            revision_config["preprocessing"] = {"enabled": True, "arn": "arn:hook"}
        return ProcessingMode(mode, None, revision_config, False)

    return EligibilityContext(
        load_document=load or (lambda key: document),
        mode_for=mode_for,
        published_revision=lambda profile: published,
    )


def _check(document, start="beginning", context=None, **request):
    request.setdefault("kind", RunKind.REPROCESS)
    return check(
        ReprocessRequest(object_key=KEY, start=start, **request),
        context or _context(document),
    )


class TestAccepted:
    @pytest.mark.parametrize(
        "start",
        [
            "beginning",
            "ocr",
            "classification",
            "extraction",
            "assessment",
            "policy_rule_validation",
            "summarization",
            "evaluation",
        ],
    )
    def test_a_processed_pipeline_document_can_start_anywhere(self, start):
        answer = _check(_document(), start)

        assert answer.ok, answer
        assert answer.code == ""
        assert answer.start == ("beginning" if start == "ocr" else start)
        assert answer.mode == PIPELINE

    def test_the_document_is_returned_for_the_caller_to_queue(self):
        document = _document()

        assert _check(document).document is document

    def test_a_document_with_no_row_can_start_at_the_beginning(self):
        answer = _check(None)

        assert answer.ok
        assert answer.document is None

    def test_a_valid_skip_list_is_accepted(self):
        assert _check(_document(), skip=("evaluation", "summarization")).ok

    def test_sections_are_kept_only_for_extraction_and_assessment(self):
        document = _document()

        assert _check(document, "extraction", sections=("2",)).sections == ("2",)
        assert _check(document, "summarization", sections=("2",)).sections == ()


class TestMalwareGate:
    @pytest.mark.parametrize("status", sorted(MALWARE_REFUSED_STATUSES))
    def test_a_document_refused_at_the_gate_is_refused(self, status):
        answer = _check(_document(status=Status(status)))

        assert (answer.ok, answer.code) == (False, "MALWARE_GATE")

    def test_every_malware_status_is_refused(self):
        """Derived from ``Status``, so a gate status added later is covered."""
        gate_statuses = {
            s.value
            for s in Status
            if s.value.startswith("MALWARE_") or s.value.startswith("QUARANTIN")
        }

        assert gate_statuses
        assert gate_statuses == set(MALWARE_REFUSED_STATUSES)

    def test_a_document_still_being_scanned_is_asked_to_wait(self):
        answer = _check(_document(status=Status.MALWARE_SCANNING))

        assert answer.code == "MALWARE_GATE"
        assert "still being scanned" in answer.message

    def test_a_quarantined_document_is_told_it_cannot_be_reprocessed(self):
        answer = _check(_document(status=Status.QUARANTINED))

        assert "cannot be reprocessed" in answer.message
        assert "QUARANTINED" in answer.message

    def test_a_status_that_cannot_be_read_fails_closed(self):
        def _unreadable(key):
            raise RuntimeError("throttled")

        answer = _check(None, context=_context(load=_unreadable))

        assert (answer.ok, answer.code) == (False, "MALWARE_GATE")
        assert "retry" in answer.message

    def test_the_gate_applies_to_every_kind(self):
        document = _document(status=Status.QUARANTINED)

        for kind in RunKind:
            answer = check(
                ReprocessRequest(object_key=KEY, kind=kind), _context(document)
            )
            assert answer.code == "MALWARE_GATE", kind


class TestRunInProgress:
    @pytest.mark.parametrize("status", sorted(IN_PROGRESS_STATUSES))
    def test_run_in_progress(self, status):
        answer = _check(_document(status=Status(status)))

        assert (answer.ok, answer.code) == (False, "RUN_IN_PROGRESS")
        assert status in answer.message

    @pytest.mark.parametrize(
        "kind",
        [
            RunKind.REPROCESS,
            RunKind.SENT_BACK,
            RunKind.RESTART_FAILED,
            RunKind.SAVED_EDITS,
        ],
    )
    def test_it_is_refused_for_the_kinds_a_person_starts(self, kind):
        answer = _check(_document(status=Status.EXTRACTING), kind=kind)

        assert answer.code == "RUN_IN_PROGRESS"

    @pytest.mark.parametrize(
        "kind",
        [
            RunKind.NEW_UPLOAD,
            RunKind.REVIEW_COMPLETED,
            RunKind.REVIEW_REJECTED,
            RunKind.REVIEW_SKIPPED,
            RunKind.LEGACY_REVIEW,
        ],
    )
    def test_it_is_allowed_for_an_upload_and_for_review_runs(self, kind):
        answer = _check(_document(status=Status.QUEUED), kind=kind)

        assert answer.code != "RUN_IN_PROGRESS"

    @pytest.mark.parametrize(
        "status",
        [Status.COMPLETED, Status.FAILED, Status.ABORTED, Status.HITL_IN_PROGRESS],
    )
    def test_a_document_that_is_not_running_is_not_refused(self, status):
        assert _check(_document(status=status)).ok


class TestSteps:
    def test_an_unknown_start(self):
        answer = _check(_document(), "ocrr")

        assert answer.code == "UNKNOWN_STEP"
        assert "'ocrr'" in answer.message

    def test_an_unknown_skip_value(self):
        answer = _check(_document(), skip=("extraction",))

        assert answer.code == "UNKNOWN_STEP"
        assert "extraction cannot be skipped" in answer.message

    @pytest.mark.parametrize("start", ["apply_review_changes", "rejected"])
    def test_an_internal_start_cannot_be_asked_for(self, start):
        assert _check(_document(), start).code == "UNKNOWN_STEP"

    def test_assessment_does_not_exist_in_bda_mode(self):
        context = _context(_document(), modes={3: (BDA, False)})

        answer = _check(_document(), "assessment", context=context)

        assert answer.code == "STEP_NOT_IN_MODE"
        assert answer.mode == BDA

    @pytest.mark.parametrize("start", ["classification", "extraction"])
    def test_in_bda_mode_classification_and_extraction_are_the_beginning(self, start):
        context = _context(_document(), modes={3: (BDA, False)})

        answer = _check(_document(), start, context=context)

        assert (answer.ok, answer.start) == (True, "beginning")


class TestOutputMissing:
    def test_classification_needs_page_text_and_images(self):
        document = _document()
        document.pages["2"].parsed_text_uri = None

        answer = _check(document, "classification")

        assert answer.code == "OUTPUT_MISSING"
        assert "page text and images" in answer.message

    @pytest.mark.parametrize("start", ["extraction", "assessment"])
    def test_extraction_and_assessment_need_classes_and_sections(self, start):
        document = _document()
        document.pages["1"].classification = None

        assert _check(document, start).code == "OUTPUT_MISSING"

    def test_extraction_needs_sections(self):
        assert _check(_document(sections=[]), "extraction").code == "OUTPUT_MISSING"

    @pytest.mark.parametrize(
        "start", ["assessment", "policy_rule_validation", "summarization", "evaluation"]
    )
    def test_assessment_and_later_need_every_sections_result(self, start):
        document = _document()
        document.sections[1].extraction_result_uri = None

        answer = _check(document, start)

        assert answer.code == "OUTPUT_MISSING"
        assert "extraction result" in answer.message

    def test_an_excluded_section_needs_no_result(self):
        document = _document()
        document.sections[1].extraction_result_uri = None
        document.sections[1].excluded = True

        assert _check(document, "policy_rule_validation").ok

    def test_extraction_itself_does_not_need_results(self):
        document = _document()
        document.sections[1].extraction_result_uri = None

        assert _check(document, "extraction").ok

    def test_a_document_with_no_row_cannot_start_later(self):
        assert _check(None, "extraction").code == "OUTPUT_MISSING"

    def test_the_beginning_needs_nothing(self):
        assert _check(_document(pages={}, sections=[])).ok

    def test_in_bda_mode_a_page_without_a_class_is_not_missing_output(self):
        document = _document()
        document.pages["1"].classification = None
        context = _context(document, modes={3: (BDA, False)})

        assert _check(document, "policy_rule_validation", context=context).ok


class TestProfileAndRevision:
    def test_profile_and_revision_rules(self):
        document = _document()
        # r3 is pinned; r4 is plain; r5 has a preprocessing hook; r6 is BDA mode.
        context = _context(
            document, modes={5: (PIPELINE, True), 6: (BDA, False)}, published=3
        )

        def answer(start, **request):
            return _check(document, start, context=context, **request)

        # Another profile: the beginning only.
        assert answer("beginning", version="claims").ok
        assert answer("extraction", version="claims").code == (
            "OTHER_PROFILE_BEGINNING_ONLY"
        )
        # Another revision of the same profile, with no hook: any step.
        assert answer("extraction", revision=4).ok
        assert answer("extraction", version=PROFILE, revision=4).ok
        # A revision with a preprocessing hook, or another mode: refused later...
        assert answer("extraction", revision=5).code == (
            "REVISION_HAS_PREPROCESSING_HOOK"
        )
        assert answer("policy_rule_validation", revision=6).code == (
            "REVISION_CHANGES_MODE"
        )
        # ...and accepted at the beginning.
        assert answer("beginning", revision=5).ok
        assert answer("beginning", revision=6).ok
        # The pinned revision always passes.
        assert answer("extraction", revision=3).ok
        assert answer("extraction").ok

    def test_no_revision_named_means_the_published_one(self):
        document = _document()
        context = _context(document, modes={5: (PIPELINE, True)}, published=5)

        answer = _check(document, "extraction", context=context)

        assert answer.code == "REVISION_HAS_PREPROCESSING_HOOK"
        assert "r5" in answer.message

    def test_a_run_that_keeps_the_pinned_revision_is_not_checked(self):
        """Saved edits, a send-back and a restart keep the document's revision."""
        document = _document()
        context = _context(document, modes={5: (PIPELINE, True)}, published=5)

        for kind in (RunKind.SAVED_EDITS, RunKind.SENT_BACK, RunKind.RESTART_FAILED):
            start = "resume" if kind is RunKind.RESTART_FAILED else "extraction"
            failed = _document(status=Status.FAILED)
            answer = check(
                ReprocessRequest(object_key=KEY, start=start, kind=kind),
                _context(failed, modes={5: (PIPELINE, True)}, published=5),
            )
            assert answer.ok, (kind, answer)
        assert context is not None

    def test_a_document_with_no_pin_and_no_published_revision_passes(self):
        document = _document(config_revision=None)

        assert _check(document, "extraction", context=_context(document)).ok


class TestResume:
    def test_resume_is_refused_for_a_document_that_did_not_fail(self):
        answer = _check(_document(status=Status.COMPLETED), "resume")

        assert answer.code == "NOT_FAILED"

    def _resume(self, document, mode=PIPELINE, config=None):
        return resolve_resume(
            document, config if config is not None else _POLICIES, mode
        )

    def test_resume_no_pages(self):
        assert self._resume(_document(pages={})) == ("beginning", [])

    def test_resume_a_page_without_text(self):
        document = _document()
        document.pages["1"].parsed_text_uri = None

        assert self._resume(document) == ("beginning", [])

    def test_resume_a_page_without_a_class(self):
        document = _document()
        document.pages["2"].classification = None

        assert self._resume(document) == ("classification", [])
        assert self._resume(document, BDA) != ("classification", [])

    def test_resume_no_sections(self):
        assert self._resume(_document(sections=[])) == ("classification", [])
        assert self._resume(_document(sections=[]), BDA) == ("beginning", [])

    def test_resume_some_sections_without_a_result(self):
        document = _document()
        document.sections[1].extraction_result_uri = None

        assert self._resume(document) == ("extraction", ["2"])
        assert self._resume(document, BDA) == ("beginning", [])

    def test_resume_every_section_without_a_result(self):
        document = _document()
        for section in document.sections:
            section.extraction_result_uri = None

        assert self._resume(document) == ("extraction", [])

    def test_resume_an_assessment_issue(self):
        document = _document()
        document.sections[0].processing_issues = [
            ProcessingIssue(stage="assessment", severity="error", code="c", message="m")
        ]

        assert self._resume(document) == ("assessment", [])
        # BDA mode has no assessment step.
        assert self._resume(document, BDA)[0] != "assessment"

    def test_resume_an_issue_of_another_stage_is_not_an_assessment_failure(self):
        document = _document()
        document.sections[0].processing_issues = [
            ProcessingIssue(
                stage="extraction", severity="warning", code="c", message="m"
            )
        ]

        assert self._resume(document)[0] != "assessment"

    def test_resume_no_policy_rule_validation_result(self):
        document = _document(rule_validation_result=None)

        assert self._resume(document) == ("policy_rule_validation", [])
        assert self._resume(document, BDA) == ("policy_rule_validation", [])

    def test_resume_skips_policy_rule_validation_when_it_is_not_enabled(self):
        document = _document(rule_validation_result=None)

        assert self._resume(
            document, config={"rule_validation": {"enabled": False}}
        ) == (
            "evaluation",
            [],
        )

    def test_resume_no_summary(self):
        assert self._resume(_document(summary_report_uri=None)) == ("summarization", [])

    def test_resume_everything_is_there(self):
        assert self._resume(_document()) == ("evaluation", [])

    def test_resume_through_check(self):
        document = _document(status=Status.FAILED)
        document.sections[1].extraction_result_uri = None

        answer = _check(document, "resume", kind=RunKind.RESTART_FAILED)

        assert (answer.ok, answer.start, answer.sections) == (
            True,
            "extraction",
            ("2",),
        )


class TestHelpers:
    @pytest.mark.parametrize(
        ("preprocessing", "expected"),
        [
            ({"enabled": True, "arn": "arn:hook"}, True),
            ({"enabled": False, "arn": "arn:hook"}, False),
            ({"enabled": True, "arn": None}, False),
            ({"enabled": True}, False),
            (None, False),
        ],
    )
    def test_preprocessing_hook_enabled(self, preprocessing, expected):
        as_dict = {} if preprocessing is None else {"preprocessing": preprocessing}
        as_model = SimpleNamespace(
            preprocessing=None
            if preprocessing is None
            else SimpleNamespace(
                enabled=preprocessing.get("enabled"), arn=preprocessing.get("arn")
            )
        )

        assert preprocessing_hook_enabled(as_dict) is expected
        assert preprocessing_hook_enabled(as_model) is expected

    @pytest.mark.parametrize(
        ("hitl_status", "start", "expected"),
        [
            ("complete", "policy_rule_validation", True),
            ("complete", "summarization", True),
            ("complete", "evaluation", True),
            ("complete", "assessment", False),
            ("complete", "extraction", False),
            ("complete", "beginning", False),
            ("in_review", "policy_rule_validation", False),
            ("rejected", "policy_rule_validation", False),
            (None, "policy_rule_validation", False),
        ],
    )
    def test_reviewed_for(self, hitl_status, start, expected):
        document = SimpleNamespace(hitl_status=hitl_status)

        assert reviewed_for(document, start) is expected

    def test_check_never_writes(self):
        """The context has only readers, and check calls nothing else."""
        calls = []
        document = _document()
        context = EligibilityContext(
            load_document=lambda key: calls.append("load") or document,
            mode_for=lambda profile, revision: (
                calls.append("mode") or ProcessingMode(PIPELINE, None, _POLICIES, False)
            ),
            published_revision=lambda profile: calls.append("published") or 3,
        )

        check(ReprocessRequest(object_key=KEY, start="extraction"), context)

        assert set(calls) <= {"load", "mode", "published"}
        assert eligibility.check.__doc__
