# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Running a document again: ``idp_common.run_plan.reprocess.ReprocessService``.

The service holds what ``reprocess_document_resolver`` did itself before run
plans: the malware refusal, the configuration a reprocess runs under (the scope
pin and the prefix mappings), withdrawing a live review, purging the output and
queueing the document. The cases that covered those in the resolver's suite are
here, with the assertions they had there. The group and scope checks on the
caller stay with the resolver.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec

import boto3
import pytest
from moto import mock_aws

from idp_common import document_attributes
from idp_common.models import (
    Document,
    Page,
    ProcessingIssue,
    RuleValidationResult,
    Section,
    Status,
    attributes_dropped_issue,
)
from idp_common.review_queues.exceptions import ReviewConflict
from idp_common.review_queues.service import ReviewQueueService
from idp_common.run_plan import reprocess as reprocess_module
from idp_common.run_plan.models import (
    RUN_PLAN_ATTRIBUTE,
    RunKind,
    RunPlanError,
    decode,
)
from idp_common.run_plan.reprocess import (
    FAILED,
    QUEUED,
    REFUSED,
    ReprocessOrigin,
    ReprocessService,
)

pytestmark = pytest.mark.unit

IN_BUCKET = "test-in"
OUT_BUCKET = "test-out"
WORKING_BUCKET = "test-working"
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/1/test-queue"
KEY = "acme/statement.pdf"


# ----- doubles ---------------------------------------------------------------


class _Documents:
    """The tracking table: what is read, created, updated and removed."""

    def __init__(self, events=None):
        self.rows = {}
        self.unreadable = set()
        self.reads = []
        self.asked_for_policy_result = []
        self.created = []
        self.updated = []
        self.removed = []
        self._events = events if events is not None else []

    def add(self, document):
        self.rows[document.input_key] = document
        return document

    def get_document(self, key, *, with_policy_rule_validation=False):
        # The service asks for the stored policy result; see TestThePolicyResult.
        self.asked_for_policy_result.append(with_policy_rule_validation)
        self.reads.append(key)
        if key in self.unreadable:
            raise RuntimeError("ProvisionedThroughputExceededException")
        return self.rows.get(key)

    def create_document(self, document, expires_after=None):
        self._events.append("create")
        self.created.append((document, expires_after))
        return document.input_key

    def update_document(self, document):
        self._events.append("update")
        self.updated.append(document)
        return document

    def remove_attributes(self, key, names):
        self._events.append("remove")
        self.removed.append((key, tuple(names)))


class _Queue:
    def __init__(self, events=None):
        self.sent = []
        self.fail_for = set()
        self._events = events if events is not None else []

    def send_message(self, **message):
        key = message["MessageAttributes"]["ObjectKey"]["StringValue"]
        if key in self.fail_for:
            raise RuntimeError("AWS.SimpleQueueService.NonExistentQueue: secret-arn")
        self._events.append("send_message")
        self.sent.append(message)
        return {"MessageId": f"msg-{len(self.sent)}"}

    def keys(self):
        return [m["MessageAttributes"]["ObjectKey"]["StringValue"] for m in self.sent]

    def plan(self, index=0):
        return decode(
            self.sent[index]["MessageAttributes"][RUN_PLAN_ATTRIBUTE]["StringValue"]
        )


class _Manager:
    """A ConfigurationManager: the mode of a revision, and what is published."""

    def __init__(self, *, bda=(), hooks=(), published=None, active="default"):
        self._bda = set(bda)  # (profile, revision) pairs in BDA mode
        self._hooks = set(hooks)  # (profile, revision) pairs with a hook
        self._published = published or {}
        self._active = active
        self.config_reads = []
        self.table = MagicMock()
        self.table.get_item.return_value = {"Item": {"Configuration": "x"}}

    def get_merged_configuration(self, version, revision=None):
        self.config_reads.append((version, revision))
        pair = (version, revision)
        return SimpleNamespace(
            use_bda=pair in self._bda,
            preprocessing=SimpleNamespace(enabled=True, arn="arn:hook")
            if pair in self._hooks
            else None,
            rule_validation=SimpleNamespace(enabled=True),
            policy_classes=[{"$id": "p"}],
        )

    def get_bda_project_arn(self, version):
        return "arn:bda:project"

    def resolve_active_version(self):
        return self._active

    def resolve_published_revision(self, profile):
        return self._published.get(profile)


def _document(key=KEY, status=Status.COMPLETED, profile="tenant-a", revision=3, **more):
    """A fully processed two-section document, as the tracking row has it."""
    document = Document(
        id=key,
        input_key=key,
        status=status,
        config_version=profile,
        config_revision=revision,
        config_source="metadata",
        start_time="2026-01-01T00:00:00+00:00",
        completion_time="2026-01-01T00:05:00+00:00",
        workflow_execution_arn="arn:execution:old",
    )
    for page_id in ("1", "2"):
        document.pages[page_id] = Page(
            page_id=page_id,
            image_uri=f"s3://{OUT_BUCKET}/{key}/pages/{page_id}/image.jpg",
            parsed_text_uri=f"s3://{OUT_BUCKET}/{key}/pages/{page_id}/result.json",
            classification="Invoice",
        )
    for section_id in ("1", "2"):
        document.sections.append(
            Section(
                section_id=section_id,
                classification="Invoice",
                page_ids=[section_id],
                extraction_result_uri=(
                    f"s3://{OUT_BUCKET}/{key}/sections/{section_id}/result.json"
                ),
                confidence_threshold_alerts=[{"attribute_name": "Total"}],
            )
        )
    document.num_pages = 2
    document.rule_validation_result = RuleValidationResult(
        request_id="r", output_uri=f"s3://{OUT_BUCKET}/{key}/rule_validation/s.md"
    )
    document.summary_report_uri = f"s3://{OUT_BUCKET}/{key}/summary/summary.md"
    document.evaluation_report_uri = f"s3://{OUT_BUCKET}/{key}/evaluation/report.md"
    document.evaluation_status = "COMPLETED"
    for name, value in more.items():
        setattr(document, name, value)
    return document


class _Harness:
    """A service over doubles, with everything it touched in one place."""

    def __init__(self, *, manager=None, working_bucket=WORKING_BUCKET, review=None):
        self.events = []
        self.documents = _Documents(self.events)
        self.queue = _Queue(self.events)
        self.s3 = MagicMock()
        self.s3.head_object.side_effect = self._head
        self.missing_inputs = set()
        #: The user metadata ``head_object`` reports for the input object.
        self.metadata = {}
        self.manager = manager
        self.review = review
        self.metrics = []
        self.service = ReprocessService(
            document_service=self.documents,
            sqs_client=self.queue,
            queue_url=QUEUE_URL,
            s3=self.s3,
            input_bucket=IN_BUCKET,
            output_bucket=OUT_BUCKET,
            working_bucket=working_bucket,
            config_manager_factory=lambda: self.manager,
            review_service_factory=lambda: self.review,
            metrics=lambda name, dimensions=None: self.metrics.append(
                (name, dimensions or {})
            ),
            retention_days=30,
        )

    def _head(self, **kwargs):
        self.events.append("head_object")
        if kwargs["Key"] in self.missing_inputs:
            raise RuntimeError("An error occurred (404) when calling HeadObject")
        return {"ContentLength": 1, "Metadata": dict(self.metadata)}

    def mappings(self, monkeypatch, *entries):
        monkeypatch.setattr(self.service, "_prefix_mappings", lambda _m: list(entries))

    def created(self):
        """(key, profile, source) of every recreated row, as the resolver queued."""
        return [
            (d.input_key, d.config_version, d.config_source)
            for d, _ in self.documents.created
        ]


@pytest.fixture
def purge(monkeypatch):
    """The output purge, recorded and not run."""
    recorded = MagicMock(return_value=0)
    monkeypatch.setattr(reprocess_module, "delete_current_output_objects", recorded)
    return recorded


@pytest.fixture
def harness(purge, monkeypatch):
    # A later start hands its document to the working bucket; that write is the
    # sender's, covered in test_sender.py.
    monkeypatch.setattr(
        Document,
        "serialize_document",
        lambda self, bucket, prefix, *a, **k: {
            "compressed": True,
            "document_id": self.id,
            "bucket": bucket,
            "prefix": prefix,
        },
    )
    h = _Harness()
    purge.side_effect = lambda *a, **k: h.events.append("purge") or 0
    return h


# ----- the malware gate (moved from the resolver's suite) --------------------


class TestTheGuardRefuses:
    @pytest.mark.parametrize("status", [Status.QUARANTINED, Status.MALWARE_SCAN_FAILED])
    def test_a_gate_refused_document_cannot_be_reprocessed(self, harness, status):
        harness.documents.add(_document("acme/infected.pdf", status))

        (outcome,) = harness.service.reprocess(["acme/infected.pdf"])

        assert (outcome.status, outcome.reason) == (REFUSED, "MALWARE_GATE")
        assert "malware-scanning gate" in outcome.message

    def test_a_document_still_being_scanned_is_refused_as_a_wait(self, harness):
        """No verdict exists yet, and a run does not wait for one: it would start
        the workflow immediately, so the file would reach OCR and Bedrock with
        nothing having decided it is safe.

        Worded as a wait rather than a refusal because that is what it is, and an
        operator who reads "cannot be reprocessed" on a document that becomes
        reprocessable two minutes later will stop trusting the message.
        """
        harness.documents.add(_document("acme/scanning.pdf", Status.MALWARE_SCANNING))

        (outcome,) = harness.service.reprocess(["acme/scanning.pdf"])

        assert outcome.status == REFUSED
        assert "still being scanned" in outcome.message
        assert "once the scan verdict arrives" in outcome.message
        # Not the terminal-refusal wording, which tells the reader to delete it.
        assert "Delete it" not in outcome.message

    def test_the_refusal_says_what_to_do_next(self, harness):
        """A bare "cannot be reprocessed" leaves a reviewer with no next step, and
        the two legitimate next steps differ (delete it, or re-upload an
        unprotected copy of a password-protected file)."""
        harness.documents.add(_document("acme/locked.pdf", Status.MALWARE_SCAN_FAILED))

        (outcome,) = harness.service.reprocess(["acme/locked.pdf"])

        assert "re-upload" in outcome.message
        assert "Delete it" in outcome.message

    def test_the_check_runs_before_anything_is_queued(self, harness):
        """Ordering is the whole control: a refusal after the SQS send would have
        already handed the file to the pipeline."""
        harness.documents.add(_document("acme/infected.pdf", Status.QUARANTINED))

        harness.service.reprocess(["acme/infected.pdf"])

        assert harness.queue.sent == []
        assert harness.events == []

    def test_it_fails_closed_on_a_read_error(self, harness):
        """A run does not go back through the ingest gate, so "a DynamoDB blip is
        fine, the gate will see it again" is wrong here. The right posture is to
        refuse with a transient-sounding message that invites a retry."""
        harness.documents.unreadable.add("acme/any.pdf")

        (outcome,) = harness.service.reprocess(["acme/any.pdf"])

        assert (outcome.status, outcome.reason) == (REFUSED, "MALWARE_GATE")
        assert "Could not verify" in outcome.message
        assert harness.queue.sent == []


class TestTheGuardAllows:
    @pytest.mark.parametrize(
        "status", [Status.COMPLETED, Status.FAILED, Status.ABORTED]
    )
    def test_a_document_no_run_is_working_on_is_reprocessed(self, harness, status):
        """Reprocess is legitimate from most statuses: that is what makes it
        useful on a FAILED or half-processed document."""
        harness.documents.add(_document("acme/ok.pdf", status))

        (outcome,) = harness.service.reprocess(["acme/ok.pdf"])

        assert outcome.status == QUEUED

    @pytest.mark.parametrize("status", [Status.OCR, Status.QUEUED])
    def test_a_document_a_run_is_working_on_is_refused_for_that_reason(
        self, harness, status
    ):
        """Not the malware gate's refusal: the gate leaves these statuses alone.
        A second run beside the first is what is refused."""
        harness.documents.add(_document("acme/busy.pdf", status))

        (outcome,) = harness.service.reprocess(["acme/busy.pdf"])

        assert (outcome.status, outcome.reason) == (REFUSED, "RUN_IN_PROGRESS")
        assert "malware" not in outcome.message

    def test_a_document_with_no_tracking_row_is_allowed(self, harness):
        (outcome,) = harness.service.reprocess(["acme/new.pdf"])

        assert outcome.status == QUEUED
        assert harness.queue.keys() == ["acme/new.pdf"]


# ----- caller-supplied attributes are carried forward (#1851) ----------------


class TestCallerAttributesAreCarriedForward:
    """A reprocess from the beginning writes a fresh tracking row over the old
    one, so anything it does not copy is lost. The attributes are the intake
    snapshot on the old row; only when there is no row are they read from the
    object again, by the rules intake uses."""

    ATTRIBUTES = {"account_id": "ACME-0042", "amount": 3}

    @staticmethod
    def _sent(harness):
        return json.loads(harness.queue.sent[0]["MessageBody"])

    def test_the_rows_attributes_and_drop_warning_are_carried_forward(self, harness):
        dropped = attributes_dropped_issue([("Bad", "key rule")])
        other = ProcessingIssue(
            stage="ingest", severity="warning", code="other", message="m"
        )
        harness.documents.add(
            _document(
                attributes=dict(self.ATTRIBUTES), processing_issues=[dropped, other]
            )
        )
        # The object's metadata now says something else; the row's snapshot wins.
        harness.metadata = {"idp-attr-account-id": "CHANGED"}

        (outcome,) = harness.service.reprocess([KEY])

        assert outcome.status == QUEUED
        ((created, _expires),) = harness.documents.created
        assert created.attributes == self.ATTRIBUTES
        assert [issue.code for issue in created.processing_issues] == [
            document_attributes.ISSUE_ATTRIBUTES_DROPPED
        ]
        assert self._sent(harness)["attributes"] == self.ATTRIBUTES

    def test_without_a_row_they_are_read_from_the_object_by_the_intake_rules(
        self, harness
    ):
        harness.metadata = {
            "idp-attr-account-id": "ACME-0042",
            "idp-attr-bad-": "x" * 2000,
        }

        (outcome,) = harness.service.reprocess(["acme/new.pdf"])

        assert outcome.status == QUEUED
        ((created, _expires),) = harness.documents.created
        assert created.attributes == {"account_id": "ACME-0042"}
        (issue,) = created.processing_issues
        assert issue.code == document_attributes.ISSUE_ATTRIBUTES_DROPPED

    def test_a_document_without_attributes_reprocesses_exactly_as_before(self, harness):
        harness.documents.add(_document())

        harness.service.reprocess([KEY])

        ((created, _expires),) = harness.documents.created
        assert created.attributes == {}
        assert created.processing_issues == []
        sent = self._sent(harness)
        assert "attributes" not in sent and "processing_issues" not in sent

    def test_a_later_start_keeps_the_row_and_so_its_attributes(self, harness):
        harness.documents.add(_document(attributes=dict(self.ATTRIBUTES)))

        (outcome,) = harness.service.reprocess([KEY], start="summarization")

        assert outcome.status == QUEUED
        assert harness.documents.created == []
        (updated,) = harness.documents.updated
        assert updated.attributes == self.ATTRIBUTES


# ----- the review is withdrawn first (moved) ---------------------------------


def _review_service():
    """A stand-in with the review service's own signatures, so a call the real
    ``withdraw`` would refuse (an argument it does not have) fails here too."""
    return create_autospec(ReviewQueueService, instance=True)


class TestWithdrawReview:
    def test_the_item_is_withdrawn_as_reprocessed(self):
        review = MagicMock()
        service = _Harness(review=review).service

        service._withdraw_review("claims/a.pdf")

        review.withdraw.assert_called_once_with("claims/a.pdf", "reprocessed")

    def test_without_a_service_nothing_is_withdrawn(self, monkeypatch):
        service = _Harness(review=None).service
        error = MagicMock()
        monkeypatch.setattr(reprocess_module.logger, "error", error)

        service._withdraw_review("claims/a.pdf")

        error.assert_not_called()

    def test_without_a_factory_nothing_is_withdrawn(self):
        service = ReprocessService(
            document_service=_Documents(),
            sqs_client=_Queue(),
            queue_url=QUEUE_URL,
            s3=MagicMock(),
            input_bucket=IN_BUCKET,
            output_bucket=OUT_BUCKET,
        )

        service._withdraw_review("claims/a.pdf")  # must not raise

    def test_a_failed_withdrawal_is_logged_and_not_raised(self, monkeypatch):
        review = MagicMock()
        review.withdraw.side_effect = RuntimeError("hitl table throttled")
        service = _Harness(review=review).service
        error = MagicMock()
        monkeypatch.setattr(reprocess_module.logger, "error", error)

        service._withdraw_review("claims/a.pdf")  # must not raise

        error.assert_called_once_with(
            "Failed to withdraw the review item before reprocess", exc_info=True
        )

    def test_a_send_back_is_recorded_as_sent_back_with_who_where_and_why(self):
        """The review's history gets a ``sent_back`` entry, not ``withdrawn``:
        the reviewer, the step the run starts at and the note. The call is
        checked against the review service's own signature."""
        review = _review_service()
        service = _Harness(review=review).service

        service._withdraw_review(
            "claims/a.pdf",
            ReprocessOrigin(
                kind=RunKind.SENT_BACK,
                actor="rev@example.com",
                cycle_id="c1",
                note="Wrong class",
            ),
            "classification",
        )

        review.withdraw.assert_called_once_with(
            "claims/a.pdf",
            "sent_back",
            actor="rev@example.com",
            expected_cycle="c1",
            history_event="sent_back",
            details={"start_step": "classification", "note": "Wrong class"},
        )

    def test_a_send_back_with_no_actor_or_cycle_sends_none_for_them(self):
        review = _review_service()
        service = _Harness(review=review).service

        service._withdraw_review(
            "claims/a.pdf", ReprocessOrigin(kind=RunKind.SENT_BACK), "ocr"
        )

        call = review.withdraw.call_args
        assert (call.kwargs["actor"], call.kwargs["expected_cycle"]) == (None, None)
        assert call.kwargs["details"] == {"start_step": "ocr", "note": ""}

    def test_a_send_back_of_a_replaced_review_raises(self, monkeypatch):
        """The reviewer acted on a review that has been replaced: the conflict
        reaches the caller, which refuses the document. It is not logged as a
        failure and swallowed, as any other error of a withdrawal is."""
        review = _review_service()
        review.withdraw.side_effect = ReviewConflict(
            "CYCLE_CHANGED", "the document has a newer review cycle"
        )
        service = _Harness(review=review).service
        error = MagicMock()
        monkeypatch.setattr(reprocess_module.logger, "error", error)

        with pytest.raises(ReviewConflict, match="CYCLE_CHANGED"):
            service._withdraw_review(
                "claims/a.pdf",
                ReprocessOrigin(kind=RunKind.SENT_BACK, cycle_id="c1"),
                "extraction",
            )

        error.assert_not_called()

    def test_another_failure_of_a_send_back_withdrawal_is_logged_and_not_raised(
        self, monkeypatch
    ):
        review = _review_service()
        review.withdraw.side_effect = RuntimeError("throttled")
        service = _Harness(review=review).service
        error = MagicMock()
        monkeypatch.setattr(reprocess_module.logger, "error", error)

        service._withdraw_review(
            "claims/a.pdf",
            ReprocessOrigin(kind=RunKind.SENT_BACK, cycle_id="c1"),
            "extraction",
        )  # must not raise

        error.assert_called_once_with(
            "Failed to withdraw the review item before reprocess", exc_info=True
        )

    def test_a_plain_reprocess_names_no_cycle_and_no_event(self):
        review = _review_service()
        service = _Harness(review=review).service

        service._withdraw_review(
            "claims/a.pdf", ReprocessOrigin(kind=RunKind.REPROCESS), "extraction"
        )

        review.withdraw.assert_called_once_with("claims/a.pdf", "reprocessed")


class TestReprocessWithdrawsFirst:
    def test_the_review_is_withdrawn_before_outputs_are_purged_and_requeued(
        self, harness
    ):
        review = MagicMock()
        review.withdraw.side_effect = lambda *a: harness.events.append("withdraw")
        harness.review = review

        (outcome,) = harness.service.reprocess(["claims/a.pdf"], version="v2")

        assert outcome.status == QUEUED
        review.withdraw.assert_called_once_with("claims/a.pdf", "reprocessed")
        names = harness.events
        assert names.index("head_object") < names.index("withdraw")
        assert names.index("withdraw") < names.index("purge")
        assert names.index("purge") < names.index("create")
        assert names.index("create") < names.index("send_message")
        (sent,) = harness.queue.sent
        assert json.loads(sent["MessageBody"])["id"] == "claims/a.pdf"
        assert sent["MessageAttributes"]["EventType"]["StringValue"] == (
            "DocumentReprocessed"
        )
        assert harness.documents.created[0][0].config_version == "v2"

    def test_a_missing_input_object_withdraws_nothing(self, harness):
        review = MagicMock()
        harness.review = review
        harness.missing_inputs.add("claims/a.pdf")

        (outcome,) = harness.service.reprocess(["claims/a.pdf"])

        assert (outcome.status, outcome.reason) == (FAILED, "InputObjectMissing")
        assert "not found in S3 bucket" in outcome.message
        review.withdraw.assert_not_called()
        assert harness.queue.sent == []
        assert "purge" not in harness.events


# ----- the output purge (moved) ----------------------------------------------


class TestDeleteOutputDataArgPassing:
    def test_calls_helper_with_broad_purge_semantics(self, purge):
        """A reprocess from the beginning must call the helper with the input key
        positional and NO ``subprefixes`` argument, so the default (broad purge)
        applies. Passing ``subprefixes=("pages/",)`` would narrow the purge:
        sections/, summary/ and evaluation/ would survive a "start over"."""
        purge.return_value = 3
        service = _Harness().service

        service._delete_output_data("doc.pdf")

        assert purge.call_count == 1
        call = purge.call_args
        # Positional: the S3 client, the output bucket, the input key.
        assert call.args[1] == "test-out"
        assert call.args[2] == "doc.pdf"
        assert "subprefixes" not in call.kwargs

    def test_failure_is_non_fatal(self, purge):
        purge.side_effect = RuntimeError("simulated S3 outage")

        _Harness().service._delete_output_data("doc.pdf")  # must not raise

    def test_failure_emits_alarmable_metric(self, purge):
        """On a purge failure the service publishes ``StaleOutputPurgeFailed``, so
        an operator can alarm on it without scraping logs."""
        purge.side_effect = RuntimeError("partial-purge S3 error")
        h = _Harness()

        h.service._delete_output_data("doc.pdf")

        assert h.metrics == [("StaleOutputPurgeFailed", {})]

    def test_a_clean_purge_publishes_nothing(self, purge):
        h = _Harness()

        h.service._delete_output_data("doc.pdf")

        assert h.metrics == []

    def test_metric_emit_failure_is_swallowed(self, purge):
        """Telemetry must not affect the reprocess."""
        purge.side_effect = RuntimeError("purge failed")

        def _throttled(name, dimensions=None):
            raise RuntimeError("CW throttle")

        service = ReprocessService(
            document_service=_Documents(),
            sqs_client=_Queue(),
            queue_url=QUEUE_URL,
            s3=MagicMock(),
            input_bucket=IN_BUCKET,
            output_bucket=OUT_BUCKET,
            metrics=_throttled,
        )

        service._delete_output_data("doc.pdf")  # must not raise

    def test_a_purge_failure_does_not_stop_the_reprocess(self, harness, purge):
        purge.side_effect = RuntimeError("simulated S3 outage")

        (outcome,) = harness.service.reprocess([KEY])

        assert outcome.status == QUEUED
        assert ("StaleOutputPurgeFailed", {}) in harness.metrics


# ----- the forward direction stays in scope (moved) --------------------------


class TestTheForwardDirectionStaysInScope:
    """Omitting ``version`` must not move a document OUT of the caller's scope.

    An unpinned reprocess reaches ``queue_processor`` with no ``config_version``,
    which resolves the **globally active** profile, a value nothing scope-checks.
    A scoped caller's reprocess is therefore pinned to the document's own profile.
    """

    def test_a_scoped_caller_is_pinned_to_the_documents_own_profile(self, harness):
        harness.documents.add(_document(profile="tenant-a"))

        harness.service.reprocess([KEY], allowed_versions=["tenant-a"])

        assert harness.created() == [(KEY, "tenant-a", "document-pin")]

    def test_an_explicit_version_still_wins(self, harness):
        """It has already been scope-checked, so the caller's choice stands."""
        harness.documents.add(_document(profile="tenant-a"))

        harness.service.reprocess(
            [KEY], version="tenant-a2", allowed_versions=["tenant-a", "tenant-a2"]
        )

        assert harness.created() == [(KEY, "tenant-a2", "explicit-request")]

    @pytest.mark.parametrize("allowed", [None, []])
    def test_an_unscoped_caller_is_not_pinned(self, harness, allowed):
        """No pin, so the active profile is resolved when the run starts."""
        harness.documents.add(_document(profile="tenant-a"))

        harness.service.reprocess([KEY], allowed_versions=allowed)

        assert harness.created() == [(KEY, None, "active-profile")]

    def test_each_document_is_pinned_to_its_own_profile(self, harness):
        """A batch spanning two in-scope profiles keeps each document where it is."""
        harness.documents.add(_document("a.pdf", profile="tenant-a"))
        harness.documents.add(_document("b.pdf", profile="tenant-b"))

        harness.service.reprocess(["a.pdf", "b.pdf"], allowed_versions=["tenant-*"])

        assert harness.created() == [
            ("a.pdf", "tenant-a", "document-pin"),
            ("b.pdf", "tenant-b", "document-pin"),
        ]

    def test_a_revision_argument_applies_to_the_pinned_profile(self, harness):
        harness.documents.add(_document(profile="tenant-a"))

        harness.service.reprocess([KEY], revision=7, allowed_versions=["tenant-a"])

        document, _ = harness.documents.created[0]
        assert (document.config_version, document.config_revision) == ("tenant-a", 7)


class TestTheServiceKeepsACallerInScope:
    """The resolver checks scope first; the service refuses on its own read too."""

    def test_a_document_outside_scope_refuses_the_request(self, harness):
        harness.documents.add(_document("ok.pdf", profile="tenant-a"))
        harness.documents.add(_document("nope.pdf", profile="tenant-b"))

        with pytest.raises(PermissionError, match="outside your allowed"):
            harness.service.reprocess(
                ["ok.pdf", "nope.pdf"], allowed_versions=["tenant-a"]
            )

        assert harness.queue.sent == []
        assert harness.events == []

    def test_an_unstamped_document_is_refused(self, harness):
        harness.documents.add(_document(profile=None))

        with pytest.raises(PermissionError):
            harness.service.reprocess([KEY], allowed_versions=["tenant-a"])

    def test_an_unreadable_tracking_row_refuses(self, harness):
        harness.documents.unreadable.add(KEY)

        with pytest.raises(PermissionError):
            harness.service.reprocess([KEY], allowed_versions=["tenant-a"])

    def test_a_profile_outside_scope_is_refused(self, harness):
        harness.documents.add(_document(profile="tenant-a"))

        with pytest.raises(PermissionError, match="tenant-b"):
            harness.service.reprocess(
                [KEY], version="tenant-b", allowed_versions=["tenant-a"]
            )

        assert harness.queue.sent == []

    def test_options_are_refused_the_same_way(self, harness):
        harness.documents.add(_document(profile="tenant-b"))

        with pytest.raises(PermissionError):
            harness.service.options([KEY], allowed_versions=["tenant-a"])


# ----- prefix mappings on reprocess (moved) ----------------------------------


class TestConfigPrefixMappingsOnReprocess:
    """Moving a mapping and reprocessing is how a backlog is re-run under a new
    profile, but the mapping must never override the scope pin.

    That ordering is the easy thing to get backwards: the pin exists for exactly
    the case "no explicit profile was requested", which is also the case a mapping
    wants to fill.
    """

    def test_an_unscoped_caller_picks_up_the_mapping(self, harness, monkeypatch):
        harness.documents.add(_document(profile="tenant-a"))
        harness.mappings(
            monkeypatch, {"prefix": "acme/", "configProfile": "mapped-profile"}
        )

        harness.service.reprocess([KEY])

        assert harness.created() == [(KEY, "mapped-profile", "prefix-mapping")]

    def test_the_scope_pin_beats_the_mapping(self, harness, monkeypatch):
        """The control this feature must not weaken."""
        harness.documents.add(_document(profile="tenant-a"))
        harness.mappings(
            monkeypatch, {"prefix": "acme/", "configProfile": "somewhere-else"}
        )

        harness.service.reprocess([KEY], allowed_versions=["tenant-a"])

        assert harness.created() == [(KEY, "tenant-a", "document-pin")]

    def test_a_mapping_wins_over_an_explicit_reprocess_request(
        self, harness, monkeypatch
    ):
        """``mapping`` precedence must not mean "the mapping wins, except on
        reprocess". A reprocess request is the same kind of claim as a profile
        named in upload metadata, so the same conflict mode adjudicates it."""
        harness.documents.add(_document(profile="tenant-a"))
        harness.mappings(
            monkeypatch,
            {
                "prefix": "acme/",
                "configProfile": "mapped-profile",
                "metadataPrecedence": "mapping",
            },
        )

        harness.service.reprocess([KEY], version="chosen")

        assert harness.created() == [(KEY, "mapped-profile", "prefix-mapping")]

    def test_a_deferring_mapping_still_honours_an_explicit_request(
        self, harness, monkeypatch
    ):
        harness.documents.add(_document(profile="tenant-a"))
        harness.mappings(
            monkeypatch,
            {
                "prefix": "acme/",
                "configProfile": "mapped-profile",
                "metadataPrecedence": "metadata",
            },
        )

        harness.service.reprocess([KEY], version="chosen")

        assert harness.created() == [(KEY, "chosen", "explicit-request")]

    def test_a_refusing_mapping_refuses_the_reprocess(self, harness, monkeypatch):
        """``reject`` must not mean "refuse the upload, then accept the same
        conflict on a re-run". Nothing is queued."""
        harness.documents.add(_document(profile="tenant-a"))
        harness.mappings(
            monkeypatch,
            {
                "prefix": "acme/",
                "configProfile": "mapped-profile",
                "metadataPrecedence": "reject",
            },
        )

        # The message must describe a REPROCESS, not upload metadata: the
        # conflicting profile came from this request, not from the object.
        with pytest.raises(ValueError, match="Refused") as excinfo:
            harness.service.reprocess([KEY], version="chosen")

        assert harness.created() == []
        assert harness.queue.sent == []
        message = str(excinfo.value)
        assert "acme/" in message, "the refusal must name the mapping to edit"
        assert "upload metadata" not in message
        assert "Reprocess without naming a profile" in message

    def test_a_refusal_for_one_document_queues_none_of_the_batch(
        self, harness, monkeypatch
    ):
        harness.documents.add(_document("other/fine.pdf", profile="tenant-a"))
        harness.documents.add(_document(profile="tenant-a"))
        harness.mappings(
            monkeypatch,
            {
                "prefix": "acme/",
                "configProfile": "mapped-profile",
                "metadataPrecedence": "reject",
            },
        )

        with pytest.raises(ValueError, match="Refused"):
            harness.service.reprocess(["other/fine.pdf", KEY], version="chosen")

        assert harness.queue.sent == []
        assert harness.events == []

    def test_agreement_on_reprocess_is_not_published_as_a_conflict(
        self, harness, monkeypatch
    ):
        """Naming the profile the mapping already assigns is agreement."""
        harness.documents.add(_document(profile="tenant-a"))
        harness.mappings(
            monkeypatch,
            {
                "prefix": "acme/",
                "configProfile": "mapped-profile",
                "metadataPrecedence": "mapping",
            },
        )

        harness.service.reprocess([KEY], version="mapped-profile")

        names = [name for name, _ in harness.metrics]
        assert "PrefixMappingApplied" in names
        assert "PrefixMappingConflict" not in names, (
            "the request named the profile the mapping assigns, which is agreement "
            f"rather than a conflict; published {names}"
        )

    def test_applied_means_matched_here_as_it_does_at_ingest(
        self, harness, monkeypatch
    ):
        """One metric, one meaning across both emitters: ``Applied`` whenever a
        mapping MATCHED the key, whoever won."""
        harness.documents.add(_document(profile="tenant-a"))
        harness.mappings(
            monkeypatch,
            {
                "prefix": "acme/",
                "configProfile": "mapped-profile",
                # The mapping DEFERS, so it loses, and must still count as matched.
                "metadataPrecedence": "metadata",
            },
        )

        harness.service.reprocess([KEY], version="chosen")

        applied = [d for name, d in harness.metrics if name == "PrefixMappingApplied"]
        assert applied == [{"Prefix": "acme/"}], (
            "a mapping that matched but lost must still publish Applied, with the "
            f"prefix dimension the dashboard filters on; published {harness.metrics}"
        )
        # And this one IS a conflict, with the winner recorded.
        assert ("PrefixMappingConflict", {"Winner": "metadata"}) in harness.metrics

    def test_a_refused_reprocess_publishes_rejected(self, harness, monkeypatch):
        harness.documents.add(_document(profile="tenant-a"))
        harness.mappings(
            monkeypatch,
            {
                "prefix": "acme/",
                "configProfile": "mapped-profile",
                "metadataPrecedence": "reject",
            },
        )

        with pytest.raises(ValueError):
            harness.service.reprocess([KEY], version="chosen")

        assert "PrefixMappingRejected" in [name for name, _ in harness.metrics]

    @pytest.mark.parametrize("requested", [None, "chosen"])
    def test_a_mapping_to_a_profile_that_does_not_exist_refuses_the_reprocess(
        self, harness, monkeypatch, requested
    ):
        """#597: a mapping naming a profile that does not exist fails the request,
        rather than reprocessing under the caller's profile or the active one."""
        harness.manager = _Manager(active="active")
        monkeypatch.setattr(
            reprocess_module,
            "_profile_head_exists",
            lambda _manager, profile: profile != "deleted",
        )
        # No pin: with no profile requested, the mapping is what decides.
        harness.documents.add(_document(profile=None, revision=None))
        harness.mappings(monkeypatch, {"prefix": "acme/", "configProfile": "deleted"})

        with pytest.raises(ValueError, match="does not exist") as refused:
            harness.service.reprocess([KEY], version=requested)

        assert "'acme/'" in str(refused.value)
        assert harness.queue.sent == []
        assert harness.documents.created == []
        assert "PrefixMappingUnresolvable" in [name for name, _ in harness.metrics]

    def test_an_explicit_request_stands_where_no_mapping_governs_the_key(
        self, harness, monkeypatch
    ):
        harness.documents.add(_document(profile="tenant-a"))
        harness.mappings(
            monkeypatch, {"prefix": "elsewhere/", "configProfile": "mapped-profile"}
        )

        harness.service.reprocess([KEY], version="chosen")

        assert harness.created() == [(KEY, "chosen", "explicit-request")]

    def test_a_mapping_outside_a_callers_scope_is_not_applied(
        self, harness, monkeypatch
    ):
        """Belt and braces: the pin already covers a scoped caller, but the
        resolved profile is scope-checked too, so the rule holds if that ever
        stops producing a pin."""
        harness.mappings(
            monkeypatch, {"prefix": "acme/", "configProfile": "finance-prod"}
        )

        version, _, source, prefix = harness.service._version_for_document(
            None,
            None,
            KEY,
            None,
            ["tenant-a"],
            mappings=harness.service._prefix_mappings(None),
        )

        # No profile pinned, and `active-profile` as the SOURCE, not an empty
        # one. The scope denial is in the log and deliberately NOT in the source.
        assert (version, source, prefix) == (None, "active-profile", None)

    def test_no_mapping_leaves_the_reprocess_unpinned_as_before(
        self, harness, monkeypatch
    ):
        harness.documents.add(_document(profile="tenant-a"))
        harness.mappings(monkeypatch)

        harness.service.reprocess([KEY])

        # Unpinned, with the provenance recorded.
        assert harness.created() == [(KEY, None, "active-profile")]

    def test_the_mapping_fields_reach_the_recreated_row(self, harness, monkeypatch):
        harness.documents.add(_document(profile="tenant-a"))
        harness.mappings(
            monkeypatch, {"prefix": "acme/", "configProfile": "mapped-profile"}
        )

        harness.service.reprocess([KEY])

        document, _ = harness.documents.created[0]
        assert document.config_mapping_prefix == "acme/"

    def test_an_unreadable_mapping_table_does_not_refuse_the_reprocess(self):
        """Fails open, like queue_sender: reprocessing under the active profile is
        a far better outcome than refusing because a ROUTING table was
        unreadable."""
        h = _Harness()

        class Boom:
            @property
            def table(self):
                raise RuntimeError("throttled")

        assert h.service._prefix_mappings(Boom()) == []
        assert h.metrics == [("PrefixMappingLookupFailed", {})]

    def test_no_configuration_table_wired_is_not_an_error(self):
        h = _Harness()

        assert h.service._prefix_mappings(None) == []
        assert h.metrics == [("PrefixMappingLookupFailed", {})]

    def test_the_mappings_are_read_once_for_a_batch(self, harness, monkeypatch):
        reads = []
        monkeypatch.setattr(
            harness.service, "_prefix_mappings", lambda _m: reads.append(1) or []
        )

        harness.service.reprocess(["a.pdf", "b.pdf", "c.pdf"])

        assert reads == [1]


# ----- from the beginning ----------------------------------------------------


class TestFromTheBeginning:
    def test_the_row_is_recreated_and_the_plan_says_beginning(self, harness):
        harness.documents.add(_document())

        (outcome,) = harness.service.reprocess([KEY])

        assert (outcome.status, outcome.start_step) == (QUEUED, "beginning")
        (document, expires_after) = harness.documents.created[0]
        assert document.status == Status.QUEUED
        assert (document.pages, document.sections) == ({}, [])
        assert (document.input_bucket, document.output_bucket) == (
            IN_BUCKET,
            OUT_BUCKET,
        )
        assert document.queued_time == document.initial_event_time
        assert expires_after > 0
        assert harness.documents.updated == []
        plan = harness.queue.plan()
        assert (plan.kind, plan.start, plan.skip) == (
            RunKind.REPROCESS,
            "beginning",
            [],
        )

    def test_the_body_is_the_fresh_document_itself(self, harness):
        """No working-bucket object for a document with no pages and no sections."""
        harness.documents.add(_document())

        harness.service.reprocess([KEY])

        body = json.loads(harness.queue.sent[0]["MessageBody"])
        assert body.get("compressed") is not True
        assert body["input_key"] == KEY

    def test_test_studio_markers_are_kept_on_the_fresh_document(self, harness):
        harness.documents.add(
            _document(submission_source="test-studio", test_set_id="set-7")
        )

        harness.service.reprocess([KEY])

        document, _ = harness.documents.created[0]
        assert document.submission_source == "test-studio"
        assert document.test_set_id == "set-7"
        body = json.loads(harness.queue.sent[0]["MessageBody"])
        assert body["submission_source"] == "test-studio"

    def test_an_ordinary_document_gets_no_markers(self, harness):
        harness.documents.add(_document())

        harness.service.reprocess([KEY])

        document, _ = harness.documents.created[0]
        assert (document.submission_source, document.test_set_id) == (None, None)

    def test_ocr_and_the_skip_list(self, harness):
        harness.documents.add(_document())

        (outcome,) = harness.service.reprocess(
            [KEY], start="ocr", skip=["summarization", "evaluation", "evaluation"]
        )

        assert outcome.start_step == "beginning"
        assert harness.queue.plan().skip == ["evaluation", "summarization"]

    def test_the_retention_sets_the_rows_expiry(self, harness):
        import time

        harness.service.reprocess([KEY])

        _, expires_after = harness.documents.created[0]
        thirty_days = 30 * 24 * 3600
        assert abs(expires_after - (time.time() + thirty_days)) < 120


# ----- from a later step -----------------------------------------------------


class TestFromALaterStep:
    def test_the_row_is_kept_and_cleared_from_the_start_on(self, harness):
        document = harness.documents.add(_document())

        (outcome,) = harness.service.reprocess([KEY], start="summarization")

        assert (outcome.status, outcome.start_step) == (QUEUED, "summarization")
        assert harness.documents.created == []
        assert harness.documents.updated == [document]
        # Kept: pages, sections, their results and the policy result.
        assert len(document.pages) == 2
        assert all(s.extraction_result_uri for s in document.sections)
        assert document.rule_validation_result is not None
        # Cleared: the summary and the evaluation, on the object and on the row.
        assert document.summary_report_uri is None
        assert document.evaluation_report_uri is None
        assert harness.documents.removed == [
            (KEY, ("SummaryReportUri", "EvaluationReportUri", "EvaluationStatus"))
        ]
        # No purge of the output prefix, and nothing recreated.
        assert "purge" not in harness.events
        assert harness.events == ["head_object", "update", "remove", "send_message"]

    @pytest.mark.parametrize(
        "origin",
        [
            ReprocessOrigin(),
            ReprocessOrigin(kind=RunKind.SENT_BACK, actor="rev", cycle_id="c1"),
        ],
        ids=["reprocess", "send-back"],
    )
    def test_a_withdrawn_review_does_not_leave_its_status_on_the_row(
        self, harness, origin
    ):
        """The withdrawal removes the review status from the tracking row. The
        document was read before it, and the row and the run are written from the
        document: its copy must not put the status back, or a run that is not
        held again ends ``COMPLETED`` and still shown as in review."""
        document = harness.documents.add(
            _document(status=Status.HITL_IN_PROGRESS, hitl_status="in_review")
        )
        review = _review_service()
        review.withdraw.return_value = True
        harness.review = review

        (outcome,) = harness.service.reprocess([KEY], start="extraction", origin=origin)

        assert outcome.status == QUEUED
        # What is written to the row and what the run is queued with.
        (written,) = harness.documents.updated
        assert written is document and written.hitl_status is None

    @pytest.mark.parametrize(
        "withdrew",
        [False, RuntimeError("throttled"), None],
        ids=["no-live-review", "withdrawal-failed", "no-review-service"],
    )
    def test_a_review_that_was_not_withdrawn_keeps_its_status(self, harness, withdrew):
        """A finished review has no live item: nothing is withdrawn, the row keeps
        the status, and so does the document. The same holds for a withdrawal that
        failed, which left the row as it was, and for a stack side with no review
        service."""
        document = harness.documents.add(_document(hitl_status="complete"))
        if withdrew is not None:
            review = _review_service()
            if isinstance(withdrew, Exception):
                review.withdraw.side_effect = withdrew
            else:
                review.withdraw.return_value = withdrew
            harness.review = review

        (outcome,) = harness.service.reprocess([KEY], start="summarization")

        assert outcome.status == QUEUED
        assert document.hitl_status == "complete"

    def test_the_document_is_requeued(self, harness):
        document = harness.documents.add(_document(status=Status.FAILED))

        harness.service.reprocess([KEY], start="extraction")

        assert document.status == Status.QUEUED
        assert document.start_time is None
        assert document.completion_time is None
        assert document.workflow_execution_arn is None
        assert (document.input_bucket, document.output_bucket) == (
            IN_BUCKET,
            OUT_BUCKET,
        )

    def test_extraction_clears_the_sections_results(self, harness):
        document = harness.documents.add(_document())

        harness.service.reprocess([KEY], start="extraction")

        assert [s.extraction_result_uri for s in document.sections] == [None, None]
        assert [s.section_id for s in document.sections] == ["1", "2"]
        assert document.rule_validation_result is None
        assert harness.documents.removed == [
            (
                KEY,
                (
                    "RuleValidationResult",
                    "RuleValidationResultUri",
                    "SummaryReportUri",
                    "EvaluationReportUri",
                    "EvaluationStatus",
                ),
            )
        ]

    def test_classification_removes_the_sections_from_the_row(self, harness):
        document = harness.documents.add(_document())

        harness.service.reprocess([KEY], start="classification")

        assert document.sections == []
        assert [p.classification for p in document.pages.values()] == [None, None]
        assert harness.documents.removed[0][1][0] == "Sections"

    def test_the_document_goes_through_the_working_bucket(self, harness):
        harness.documents.add(_document())

        harness.service.reprocess([KEY], start="summarization")

        body = json.loads(harness.queue.sent[0]["MessageBody"])
        assert body == {
            "compressed": True,
            "document_id": KEY,
            "bucket": WORKING_BUCKET,
            "prefix": "reprocess",
        }

    def test_without_a_working_bucket_the_message_carries_the_document(self, purge):
        h = _Harness(working_bucket=None)
        h.documents.add(_document())

        h.service.reprocess([KEY], start="summarization")

        body = json.loads(h.queue.sent[0]["MessageBody"])
        assert len(body["sections"]) == 2
        assert body["summary_report_uri"] is None

    def test_the_plan_names_the_step_and_the_skips(self, harness):
        harness.documents.add(_document())

        harness.service.reprocess([KEY], start="extraction", skip=["evaluation"])

        plan = harness.queue.plan()
        assert (plan.kind, plan.start, plan.skip, plan.sections, plan.reviewed) == (
            RunKind.REPROCESS,
            "extraction",
            ["evaluation"],
            [],
            False,
        )

    def test_a_step_the_document_cannot_start_at_is_refused(self, harness):
        document = harness.documents.add(_document())
        document.sections[1].extraction_result_uri = None

        (outcome,) = harness.service.reprocess([KEY], start="summarization")

        assert (outcome.status, outcome.reason) == (REFUSED, "OUTPUT_MISSING")
        assert harness.events == []

    def test_a_document_with_no_row_cannot_start_later(self, harness):
        (outcome,) = harness.service.reprocess([KEY], start="extraction")

        assert (outcome.status, outcome.reason) == (REFUSED, "OUTPUT_MISSING")

    def test_an_unknown_step_is_refused(self, harness):
        harness.documents.add(_document())

        (outcome,) = harness.service.reprocess([KEY], start="ocrr")

        assert (outcome.status, outcome.reason) == (REFUSED, "UNKNOWN_STEP")

    def test_a_plan_that_cannot_be_sent_changes_nothing(self, harness, monkeypatch):
        """The plan is built before the review is withdrawn or the row written."""
        harness.documents.add(_document())
        review = MagicMock()
        harness.review = review

        def _too_large(plan):
            raise RunPlanError("The run plan is 5000 bytes; the limit is 4096 bytes")

        monkeypatch.setattr(reprocess_module, "encode", _too_large)

        (outcome,) = harness.service.reprocess([KEY], start="summarization")

        assert (outcome.status, outcome.reason) == (FAILED, "RunPlanError")
        assert harness.events == []
        review.withdraw.assert_not_called()


class TestALaterStartClearsS3:
    """With a real bucket: what a later start deletes, and what it keeps."""

    @pytest.fixture
    def bucket(self, monkeypatch):
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            monkeypatch.setenv(name, "testing")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            for name in (IN_BUCKET, OUT_BUCKET, WORKING_BUCKET):
                client.create_bucket(Bucket=name)
            client.put_object(Bucket=IN_BUCKET, Key=KEY, Body=b"%PDF")
            for key in (
                f"{KEY}/pages/1/result.json",
                f"{KEY}/sections/1/result.json",
                f"{KEY}/sections/2/result.json",
                f"{KEY}/rule_validation/consolidated/consolidated_summary.json",
                f"{KEY}/summary/summary.md",
                f"{KEY}/runs/2026/manifest.json",
            ):
                client.put_object(Bucket=OUT_BUCKET, Key=key, Body=b"{}")
            yield client

    def _service(self, client, documents, queue):
        return ReprocessService(
            document_service=documents,
            sqs_client=queue,
            queue_url=QUEUE_URL,
            s3=client,
            input_bucket=IN_BUCKET,
            output_bucket=OUT_BUCKET,
            working_bucket=WORKING_BUCKET,
        )

    def _keys(self, client, bucket=OUT_BUCKET):
        listed = client.list_objects_v2(Bucket=bucket).get("Contents", [])
        return {item["Key"] for item in listed}

    def test_extraction_deletes_results_and_policy_files_and_keeps_ocr(self, bucket):
        documents, queue = _Documents(), _Queue()
        documents.add(_document())

        (outcome,) = self._service(bucket, documents, queue).reprocess(
            [KEY], start="extraction"
        )

        assert outcome.status == QUEUED
        assert self._keys(bucket) == {
            f"{KEY}/pages/1/result.json",
            f"{KEY}/summary/summary.md",
            f"{KEY}/runs/2026/manifest.json",
        }
        # The document itself went to the working bucket.
        body = json.loads(queue.sent[0]["MessageBody"])
        assert body["compressed"] is True
        assert len(self._keys(bucket, WORKING_BUCKET)) == 1

    def test_the_beginning_purges_everything_but_the_run_records(self, bucket):
        documents, queue = _Documents(), _Queue()
        documents.add(_document())

        self._service(bucket, documents, queue).reprocess([KEY])

        assert self._keys(bucket) == {f"{KEY}/runs/2026/manifest.json"}
        assert self._keys(bucket, WORKING_BUCKET) == set()

    def test_a_missing_input_object_is_a_failure(self, bucket):
        documents, queue = _Documents(), _Queue()

        (outcome,) = self._service(bucket, documents, queue).reprocess(["gone.pdf"])

        assert (outcome.status, outcome.reason) == (FAILED, "InputObjectMissing")
        assert outcome.message == "Document gone.pdf not found in S3 bucket test-in"


# ----- the revision a run is pinned to ---------------------------------------


class TestTheRevisionOfALaterStart:
    def test_no_revision_named_means_the_published_one(self, harness):
        harness.manager = _Manager(published={"tenant-a": 5})
        document = harness.documents.add(_document(revision=3))

        (outcome,) = harness.service.reprocess([KEY], start="extraction")

        assert outcome.status == QUEUED
        assert (document.config_version, document.config_revision) == ("tenant-a", 5)

    def test_a_named_revision_is_used(self, harness):
        harness.manager = _Manager(published={"tenant-a": 5})
        document = harness.documents.add(_document(revision=3))

        harness.service.reprocess([KEY], start="extraction", revision=4)

        assert document.config_revision == 4

    def test_no_published_revision_keeps_the_pin(self, harness):
        harness.manager = _Manager()
        document = harness.documents.add(_document(revision=3))

        harness.service.reprocess([KEY], start="extraction")

        assert document.config_revision == 3

    def test_a_revision_with_a_preprocessing_hook_is_refused(self, harness):
        harness.manager = _Manager(published={"tenant-a": 5}, hooks={("tenant-a", 5)})
        harness.documents.add(_document(revision=3))

        (outcome,) = harness.service.reprocess([KEY], start="extraction")

        assert (outcome.status, outcome.reason) == (
            REFUSED,
            "REVISION_HAS_PREPROCESSING_HOOK",
        )
        assert harness.events == []

    def test_a_revision_in_the_other_mode_is_refused(self, harness):
        harness.manager = _Manager(published={"tenant-a": 5}, bda={("tenant-a", 5)})
        harness.documents.add(_document(revision=3))

        (outcome,) = harness.service.reprocess([KEY], start="policy_rule_validation")

        assert (outcome.status, outcome.reason) == (REFUSED, "REVISION_CHANGES_MODE")

    def test_another_profile_starts_at_the_beginning_only(self, harness):
        harness.manager = _Manager()
        harness.documents.add(_document(profile="tenant-a"))

        (later,) = harness.service.reprocess([KEY], start="extraction", version="other")
        (beginning,) = harness.service.reprocess([KEY], version="other")

        assert (later.status, later.reason) == (REFUSED, "OTHER_PROFILE_BEGINNING_ONLY")
        assert beginning.status == QUEUED
        assert harness.created() == [(KEY, "other", "explicit-request")]

    def test_a_later_start_does_not_consult_the_prefix_mappings(
        self, harness, monkeypatch
    ):
        """A mapping assigns a profile, and a later start keeps the document's."""
        document = harness.documents.add(_document(profile="tenant-a"))
        reads = []
        monkeypatch.setattr(
            harness.service,
            "_prefix_mappings",
            lambda _m: (
                reads.append(1)
                or [{"prefix": "acme/", "configProfile": "mapped-profile"}]
            ),
        )

        (outcome,) = harness.service.reprocess([KEY], start="summarization")

        assert outcome.status == QUEUED
        assert reads == []
        assert (document.config_version, document.config_source) == (
            "tenant-a",
            "metadata",
        )

    def test_in_bda_mode_extraction_is_the_beginning(self, harness):
        harness.manager = _Manager(bda={("tenant-a", 3)})
        harness.documents.add(_document())

        (outcome,) = harness.service.reprocess([KEY], start="extraction")

        assert (outcome.status, outcome.start_step) == (QUEUED, "beginning")
        assert len(harness.documents.created) == 1
        assert harness.queue.plan().start == "beginning"

    def test_one_configuration_read_serves_a_batch(self, harness):
        harness.manager = _Manager()
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            harness.documents.add(_document(name))

        harness.service.reprocess(["a.pdf", "b.pdf", "c.pdf"])

        assert harness.manager.config_reads == [("tenant-a", 3)]
        # And one tracking-row read per document.
        assert sorted(harness.documents.reads) == ["a.pdf", "b.pdf", "c.pdf"]


# ----- the stored policy result ----------------------------------------------


class TestThePolicyResult:
    """A run that starts after policy rule validation keeps the stage's result.

    The tracking table restores that result only when asked: the runs that
    requeue a document after saved edits or a review rely on it being absent.
    """

    def test_the_service_reads_the_document_with_its_policy_result(self, harness):
        harness.documents.add(_document())

        harness.service.reprocess([KEY], start="summarization")

        assert harness.documents.asked_for_policy_result == [True]

    def test_options_read_it_the_same_way(self, harness):
        harness.documents.add(_document())

        harness.service.options([KEY])

        assert harness.documents.asked_for_policy_result == [True]

    def test_a_start_after_the_stage_queues_the_result(self, purge):
        h = _Harness(working_bucket=None)
        h.documents.add(_document())

        h.service.reprocess([KEY], start="summarization")

        body = json.loads(h.queue.sent[0]["MessageBody"])
        assert body["rule_validation_result"]["output_uri"].endswith("s.md")
        assert h.queue.plan().start == "summarization"

    def test_a_failed_document_with_the_result_resumes_after_the_stage(self, harness):
        harness.manager = _Manager()
        document = harness.documents.add(_document(status=Status.FAILED))
        document.summary_report_uri = None

        (outcome,) = harness.service.reprocess(
            [KEY], start="resume", origin=ReprocessOrigin(kind=RunKind.RESTART_FAILED)
        )

        assert outcome.start_step == "summarization"

    def test_a_failed_document_without_it_resumes_at_the_stage(self, harness):
        harness.manager = _Manager()
        document = harness.documents.add(_document(status=Status.FAILED))
        document.rule_validation_result = None
        document.summary_report_uri = None

        (outcome,) = harness.service.reprocess(
            [KEY], start="resume", origin=ReprocessOrigin(kind=RunKind.RESTART_FAILED)
        )

        assert outcome.start_step == "policy_rule_validation"


# ----- per-document outcomes -------------------------------------------------


class TestOutcomes:
    def test_a_mixed_selection_gives_per_document_outcomes(self, harness):
        harness.documents.add(_document("ok.pdf"))
        harness.documents.add(_document("infected.pdf", Status.QUARANTINED))
        harness.documents.add(_document("busy.pdf", Status.EXTRACTING))
        harness.documents.add(_document("also-ok.pdf"))

        outcomes = harness.service.reprocess(
            ["ok.pdf", "infected.pdf", "busy.pdf", "also-ok.pdf"]
        )

        assert [(o.object_key, o.status, o.reason) for o in outcomes] == [
            ("ok.pdf", QUEUED, ""),
            ("infected.pdf", REFUSED, "MALWARE_GATE"),
            ("busy.pdf", REFUSED, "RUN_IN_PROGRESS"),
            ("also-ok.pdf", QUEUED, ""),
        ]
        assert harness.queue.keys() == ["ok.pdf", "also-ok.pdf"]

    def test_a_send_failure_is_failed_and_the_next_document_is_still_queued(
        self, harness
    ):
        harness.documents.add(_document("first.pdf"))
        harness.documents.add(_document("second.pdf"))
        harness.queue.fail_for.add("first.pdf")

        first, second = harness.service.reprocess(["first.pdf", "second.pdf"])

        assert (first.status, first.reason) == (FAILED, "RuntimeError")
        assert second.status == QUEUED
        assert harness.queue.keys() == ["second.pdf"]

    def test_a_failure_message_does_not_carry_the_services_error_text(self, harness):
        harness.documents.add(_document())
        harness.queue.fail_for.add(KEY)

        (outcome,) = harness.service.reprocess([KEY])

        assert "secret-arn" not in outcome.message
        assert KEY in outcome.message

    def test_a_storage_failure_is_failed(self, harness, monkeypatch):
        harness.documents.add(_document())

        def _refused(document, expires_after=None):
            raise RuntimeError("TransactionCanceledException")

        monkeypatch.setattr(harness.documents, "create_document", _refused)

        (outcome,) = harness.service.reprocess([KEY])

        assert (outcome.status, outcome.reason) == (FAILED, "RuntimeError")
        assert harness.queue.sent == []

    def test_no_documents_is_no_outcomes(self, harness):
        assert harness.service.reprocess([]) == []


# ----- the other kinds -------------------------------------------------------


class TestOrigins:
    def test_a_send_back_keeps_the_pinned_revision(self, harness):
        harness.manager = _Manager(published={"tenant-a": 5})
        document = harness.documents.add(
            _document(revision=3, status=Status.HITL_IN_PROGRESS)
        )
        review = MagicMock()
        harness.review = review

        (outcome,) = harness.service.reprocess(
            [KEY],
            start="extraction",
            version="ignored",
            revision=9,
            origin=ReprocessOrigin(kind=RunKind.SENT_BACK, actor="rev", cycle_id="c1"),
        )

        assert outcome.status == QUEUED
        assert (document.config_version, document.config_revision) == ("tenant-a", 3)
        review.withdraw.assert_called_once_with(
            KEY,
            "sent_back",
            actor="rev",
            expected_cycle="c1",
            history_event="sent_back",
            details={"start_step": "classification", "note": ""},
        )
        plan = harness.queue.plan()
        assert (plan.kind, plan.start, plan.cycle_id) == (
            RunKind.SENT_BACK,
            "classification",
            "c1",
        )

    def test_a_send_back_of_a_replaced_review_is_refused_and_changes_nothing(
        self, harness
    ):
        """The reviewer's review was replaced by a newer cycle before the
        send-back arrived: the document is refused with the review's code, and
        no output is cleared, no row written and no run queued."""
        document = harness.documents.add(
            _document(revision=3, status=Status.HITL_IN_PROGRESS)
        )
        review = _review_service()
        review.withdraw.side_effect = ReviewConflict(
            "CYCLE_CHANGED", "the document has a newer review cycle"
        )
        harness.review = review

        (outcome,) = harness.service.reprocess(
            [KEY],
            start="extraction",
            origin=ReprocessOrigin(kind=RunKind.SENT_BACK, actor="rev", cycle_id="c1"),
        )

        assert (outcome.status, outcome.reason, outcome.start_step) == (
            REFUSED,
            "CYCLE_CHANGED",
            "classification",
        )
        assert outcome.message == "the document has a newer review cycle"
        assert harness.queue.sent == []
        assert harness.documents.updated == [] and harness.documents.created == []
        assert document.status == Status.HITL_IN_PROGRESS

    def test_a_refused_send_back_does_not_stop_the_other_documents(self, harness):
        harness.documents.add(_document(status=Status.HITL_IN_PROGRESS))
        other = "claims/b.pdf"
        harness.documents.add(_document(key=other, status=Status.HITL_IN_PROGRESS))
        review = _review_service()
        review.withdraw.side_effect = [
            ReviewConflict("CYCLE_CHANGED", "the document has a newer review cycle"),
            True,
        ]
        harness.review = review

        outcomes = harness.service.reprocess(
            [KEY, other],
            start="extraction",
            origin=ReprocessOrigin(kind=RunKind.SENT_BACK, actor="rev", cycle_id="c1"),
        )

        assert [(o.object_key, o.status) for o in outcomes] == [
            (KEY, REFUSED),
            (other, QUEUED),
        ]

    def test_a_send_back_to_the_beginning_keeps_the_pins_too(self, harness):
        """In BDA mode a send-back starts at the beginning, where the row is
        created again."""
        harness.manager = _Manager(bda={("tenant-a", 3)}, published={"tenant-a": 5})
        harness.documents.add(_document(revision=3, status=Status.HITL_IN_PROGRESS))

        harness.service.reprocess(
            [KEY], origin=ReprocessOrigin(kind=RunKind.SENT_BACK, cycle_id="c1")
        )

        document, _ = harness.documents.created[0]
        assert (document.config_version, document.config_revision) == ("tenant-a", 3)
        assert document.config_source == "metadata"

    def test_a_failed_run_restarts_where_it_has_no_output(self, harness):
        document = harness.documents.add(_document(status=Status.FAILED))
        document.sections[1].extraction_result_uri = None

        (outcome,) = harness.service.reprocess(
            [KEY], start="resume", origin=ReprocessOrigin(kind=RunKind.RESTART_FAILED)
        )

        assert (outcome.status, outcome.start_step) == (QUEUED, "extraction")
        plan = harness.queue.plan()
        assert (plan.kind, plan.start, plan.sections) == (
            RunKind.RESTART_FAILED,
            "extraction",
            ["2"],
        )
        # Only the section with no result is cleared; the other keeps its result.
        assert document.sections[0].extraction_result_uri

    def test_resume_is_refused_for_a_document_that_did_not_fail(self, harness):
        harness.documents.add(_document(status=Status.COMPLETED))

        (outcome,) = harness.service.reprocess(
            [KEY], start="resume", origin=ReprocessOrigin(kind=RunKind.RESTART_FAILED)
        )

        assert (outcome.status, outcome.reason) == (REFUSED, "NOT_FAILED")

    @pytest.mark.parametrize(
        "kind",
        sorted(
            set(RunKind)
            - {RunKind.REPROCESS, RunKind.SENT_BACK, RunKind.RESTART_FAILED},
            key=lambda k: k.value,
        ),
    )
    def test_the_other_kinds_are_not_queued_here(self, kind):
        with pytest.raises(ValueError, match="not queued through"):
            ReprocessOrigin(kind=kind)


class TestWhereASendBackStarts:
    """A send-back names no step. The service starts it at classification, which
    keeps the page text and splits the document into sections again."""

    ORIGIN = ReprocessOrigin(kind=RunKind.SENT_BACK, actor="rev", cycle_id="c1")

    def _in_review(self, harness, **more):
        return harness.documents.add(_document(status=Status.HITL_IN_PROGRESS, **more))

    @pytest.mark.parametrize(
        "asked", [None, "extraction", "summarization", "beginning", "resume", "nowhere"]
    )
    def test_it_queues_at_classification_whatever_step_is_asked_for(
        self, harness, asked
    ):
        document = self._in_review(harness)
        named = {} if asked is None else {"start": asked}

        (outcome,) = harness.service.reprocess([KEY], origin=self.ORIGIN, **named)

        assert (outcome.status, outcome.start_step) == (QUEUED, "classification")
        plan = harness.queue.plan()
        assert (plan.kind, plan.start, plan.sections) == (
            RunKind.SENT_BACK,
            "classification",
            [],
        )
        # The row is kept with its pages; the split into sections is done again.
        assert harness.documents.created == []
        assert len(document.pages) == 2 and document.sections == []
        assert "purge" not in harness.events

    def test_a_plain_reprocess_still_starts_where_it_is_asked_to(self, harness):
        harness.documents.add(_document())

        (outcome,) = harness.service.reprocess([KEY], start="extraction")

        assert outcome.start_step == "extraction"

    def test_in_bda_mode_it_starts_at_the_beginning(self, harness):
        harness.manager = _Manager(bda={("tenant-a", 3)})
        self._in_review(harness)

        (outcome,) = harness.service.reprocess(
            [KEY], start="extraction", origin=self.ORIGIN
        )

        assert (outcome.status, outcome.start_step) == (QUEUED, "beginning")
        assert harness.queue.plan().start == "beginning"
        assert len(harness.documents.created) == 1

    def test_a_document_with_no_page_text_starts_at_the_beginning(self, harness):
        """Classification reads the page text. Without it the run cannot start
        there, and a send-back is not refused for that: it starts over."""
        document = self._in_review(harness)
        document.pages["2"].parsed_text_uri = None

        (outcome,) = harness.service.reprocess([KEY], origin=self.ORIGIN)

        assert (outcome.status, outcome.reason) == (QUEUED, "")
        assert outcome.start_step == "beginning"
        plan = harness.queue.plan()
        assert (plan.kind, plan.start, plan.cycle_id) == (
            RunKind.SENT_BACK,
            "beginning",
            "c1",
        )
        assert len(harness.documents.created) == 1
        assert "purge" in harness.events

    def test_a_plain_reprocess_with_no_page_text_is_still_refused(self, harness):
        document = harness.documents.add(_document())
        document.pages["2"].parsed_text_uri = None

        (outcome,) = harness.service.reprocess([KEY], start="classification")

        assert (outcome.status, outcome.reason) == (REFUSED, "OUTPUT_MISSING")

    def test_another_refusal_is_not_turned_into_a_run_from_the_beginning(self, harness):
        harness.documents.add(_document(status=Status.EXTRACTING))

        (outcome,) = harness.service.reprocess([KEY], origin=self.ORIGIN)

        assert (outcome.status, outcome.reason) == (REFUSED, "RUN_IN_PROGRESS")
        assert harness.events == []

    @pytest.mark.parametrize(
        "no_text, started_at",
        [(False, "classification"), (True, "beginning")],
        ids=["classification", "beginning"],
    )
    def test_the_history_names_the_step_the_run_starts_at(
        self, harness, no_text, started_at
    ):
        document = self._in_review(harness)
        if no_text:
            document.pages["1"].parsed_text_uri = None
        review = _review_service()
        review.withdraw.return_value = True
        harness.review = review

        harness.service.reprocess(
            [KEY],
            start="extraction",
            origin=ReprocessOrigin(
                kind=RunKind.SENT_BACK, actor="rev", cycle_id="c1", note="split it"
            ),
        )

        review.withdraw.assert_called_once_with(
            KEY,
            "sent_back",
            actor="rev",
            expected_cycle="c1",
            history_event="sent_back",
            details={"start_step": started_at, "note": "split it"},
        )


class TestReviewed:
    def test_a_late_start_on_a_reviewed_document_keeps_the_review(self, harness):
        harness.documents.add(_document(hitl_status="Completed"))

        harness.service.reprocess([KEY], start="summarization")

        assert harness.queue.plan().reviewed is True

    def test_a_start_that_extracts_again_does_not(self, harness):
        harness.documents.add(_document(hitl_status="Completed"))

        harness.service.reprocess([KEY], start="extraction")

        assert harness.queue.plan().reviewed is False

    def test_a_document_that_was_not_reviewed_is_not_marked(self, harness):
        harness.documents.add(_document())

        harness.service.reprocess([KEY], start="summarization")

        assert harness.queue.plan().reviewed is False

    def test_a_send_back_asks_for_the_review_again(self, harness):
        harness.documents.add(_document(hitl_status="Completed"))

        harness.service.reprocess(
            [KEY],
            start="summarization",
            origin=ReprocessOrigin(kind=RunKind.SENT_BACK, cycle_id="c1"),
        )

        assert harness.queue.plan().reviewed is False


# ----- options ---------------------------------------------------------------


class TestOptions:
    def _by_id(self, option):
        return {step["id"]: step for step in option["steps"]}

    def test_pipeline_mode_lists_every_step(self, harness):
        harness.manager = _Manager()
        harness.documents.add(_document())

        (option,) = harness.service.options([KEY])

        assert (option["objectKey"], option["mode"], option["defaultStep"]) == (
            KEY,
            "pipeline",
            "beginning",
        )
        steps = self._by_id(option)
        assert list(steps) == [
            "beginning",
            "classification",
            "extraction",
            "assessment",
            "policy_rule_validation",
            "summarization",
            "evaluation",
        ]
        assert all(step["available"] for step in steps.values())
        assert all(step["reason"] is None for step in steps.values())
        assert [i for i, step in steps.items() if step["skippable"]] == [
            "assessment",
            "policy_rule_validation",
            "summarization",
            "evaluation",
        ]

    def test_bda_mode_lists_its_own_steps(self, harness):
        harness.manager = _Manager(bda={("tenant-a", 3)})
        harness.documents.add(_document())

        (option,) = harness.service.options([KEY])

        assert option["mode"] == "bda"
        assert list(self._by_id(option)) == [
            "beginning",
            "policy_rule_validation",
            "summarization",
            "evaluation",
        ]

    def test_a_step_that_cannot_start_names_its_code(self, harness):
        harness.manager = _Manager()
        document = harness.documents.add(_document())
        document.sections[1].extraction_result_uri = None

        steps = self._by_id(harness.service.options([KEY])[0])

        assert steps["extraction"]["available"] is True
        assert (
            steps["summarization"]["available"],
            steps["summarization"]["reason"],
        ) == (
            False,
            "OUTPUT_MISSING",
        )

    def test_another_profile_leaves_only_the_beginning(self, harness):
        harness.manager = _Manager()
        harness.documents.add(_document())

        steps = self._by_id(harness.service.options([KEY], version="other")[0])

        assert [i for i, step in steps.items() if step["available"]] == ["beginning"]
        assert steps["extraction"]["reason"] == "OTHER_PROFILE_BEGINNING_ONLY"

    def test_a_document_being_processed_has_no_available_step(self, harness):
        harness.manager = _Manager()
        harness.documents.add(_document(status=Status.EXTRACTING))

        steps = self._by_id(harness.service.options([KEY])[0])

        assert {step["reason"] for step in steps.values()} == {"RUN_IN_PROGRESS"}

    def test_options_change_nothing(self, harness):
        harness.manager = _Manager()
        harness.documents.add(_document())

        harness.service.options([KEY])

        assert harness.events == []
        assert harness.queue.sent == []
