# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The run plan of the run that follows a review, and of a legacy resubmit.

A finalize message tells the run where to start: a completed review at applying
the review's changes, or at extraction when the review decided that sections are
extracted again; a rejected one at the rejected end; and a removed or skipped one
at policy rule validation. The plan is a function of the item, so a message the
sweeper sends again carries the same bytes.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from idp_common.models import Document, RuleValidationResult, Section, Status
from idp_common.review_queues.finalize import (
    FINALIZE_EVENT_TYPE,
    RESUBMIT_EVENT_TYPE,
    TOKEN_ATTRIBUTE,
    DocumentResubmitter,
    FinalizeSender,
    finalize_token,
)
from idp_common.review_queues.models import ReviewItemRecord
from idp_common.review_queues.statuses import QueueItemState
from idp_common.run_plan.models import RUN_PLAN_ATTRIBUTE, RunKind, RunPlan, decode
from idp_common.utils.execution_names import RESUBMIT_TOKEN_ATTRIBUTE, resubmit_token

pytestmark = pytest.mark.unit

KEY = "claim.pdf"
CYCLE = "cyc-0001"
TOKEN = finalize_token(KEY, CYCLE, 1)


def _item(disposition, **overrides) -> ReviewItemRecord:
    fields = dict(
        doc_key=KEY,
        cycle_id=CYCLE,
        profile="default",
        queue="claims",
        state=QueueItemState.FINALIZING.value,
        enqueued_at="2026-10-16T00:00:00.000Z",
        enqueued_at_ms=1,
        sections=["1"],
        version=1,
        disposition=disposition,
        finalize_token=TOKEN,
        finalize_attempt=1,
    )
    fields.update(overrides)
    return ReviewItemRecord(**fields)


def _document() -> Document:
    """A processed document, as the tracking row has it."""
    document = Document(
        id=KEY,
        input_key=KEY,
        status=Status.COMPLETED,
        metering={"OCR/textract/analyze_document": {"pages": 3}},
    )
    document.sections.append(
        Section(
            section_id="1",
            classification="Claim",
            page_ids=["1"],
            extraction_result_uri="s3://out/claim.pdf/sections/1/result.json",
        )
    )
    document.rule_validation_result = RuleValidationResult(
        request_id="r", output_uri="s3://out/claim.pdf/rule_validation/summary.md"
    )
    document.summary_report_uri = "s3://out/claim.pdf/summary/summary.md"
    document.evaluation_report_uri = "s3://out/claim.pdf/evaluation/report.md"
    document.evaluation_status = "COMPLETED"
    return document


def _two_sections() -> Document:
    """The processed document with a second section, each with an alert."""
    document = _document()
    document.sections.append(
        Section(
            section_id="2",
            classification="Claim",
            page_ids=["2"],
            extraction_result_uri="s3://out/claim.pdf/sections/2/result.json",
        )
    )
    for section in document.sections:
        section.attributes = {"Total": 1}
        section.confidence_threshold_alerts = [{"attribute_name": "Total"}]
        section.missing_required_fields = [{"attribute_name": "Vendor"}]
    document.confidence_alert_count = 2
    document.missing_required_field_count = 2
    return document


def _extraction_item(*sections, **overrides) -> ReviewItemRecord:
    """A completed review that decided the run starts at extraction."""
    return _item(
        "complete",
        restart_step="extraction",
        restart_sections=list(sections),
        **overrides,
    )


def _sender(document=None):
    documents = MagicMock()
    documents.get_document.side_effect = lambda key: (
        _document() if document is None else document
    )
    sqs = MagicMock()
    sqs.send_message.return_value = {"MessageId": "m-1"}
    return FinalizeSender(sqs, "queue-url", None, documents), sqs


def _values(attributes) -> dict:
    return {name: value["StringValue"] for name, value in attributes.items()}


class TestPlanFor:
    @pytest.mark.parametrize(
        ("disposition", "kind", "start", "reviewed"),
        [
            ("complete", RunKind.REVIEW_COMPLETED, "apply_review_changes", True),
            ("rejected", RunKind.REVIEW_REJECTED, "rejected", False),
            ("removed", RunKind.REVIEW_SKIPPED, "policy_rule_validation", False),
            ("skipped", RunKind.REVIEW_SKIPPED, "policy_rule_validation", False),
            # An unknown disposition is treated as rejected.
            ("something-new", RunKind.REVIEW_REJECTED, "rejected", False),
            (None, RunKind.REVIEW_REJECTED, "rejected", False),
        ],
    )
    def test_each_disposition_gives_its_kind_and_start(
        self, disposition, kind, start, reviewed
    ):
        sender, _ = _sender()

        plan = sender.plan_for(_item(disposition))

        assert plan == RunPlan(
            kind=kind, start=start, cycle_id=CYCLE, reviewed=reviewed
        )
        assert (plan.skip, plan.sections) == ([], [])

    def test_a_completed_review_that_extracts_starts_at_extraction(self):
        """The start and the sections are the ones stored on the item when the
        review was completed. The run extracts again, so the document does not
        count as reviewed."""
        sender, _ = _sender()

        plan = sender.plan_for(_extraction_item("1", "3"))

        assert plan == RunPlan(
            kind=RunKind.REVIEW_COMPLETED,
            start="extraction",
            sections=["1", "3"],
            cycle_id=CYCLE,
            reviewed=False,
        )

    def test_no_section_named_means_every_section(self):
        sender, _ = _sender()

        plan = sender.plan_for(_extraction_item())

        assert (plan.start, plan.sections, plan.reviewed) == ("extraction", [], False)

    def test_a_stored_start_at_applying_the_changes_is_the_plan_as_before(self):
        sender, _ = _sender()

        plan = sender.plan_for(_item("complete", restart_step="apply_review_changes"))

        assert plan == RunPlan(
            kind=RunKind.REVIEW_COMPLETED,
            start="apply_review_changes",
            cycle_id=CYCLE,
            reviewed=True,
        )

    def test_an_item_completed_before_the_start_was_recorded_applies_the_changes(
        self,
    ):
        item = _item("complete")
        assert item.restart_step is None
        sender, _ = _sender()

        assert sender.plan_for(item).start == "apply_review_changes"

    @pytest.mark.parametrize("disposition", ["rejected", "removed", "skipped"])
    def test_only_a_completed_review_reads_the_stored_start(self, disposition):
        sender, _ = _sender()
        plain = sender.plan_for(_item(disposition))

        plan = sender.plan_for(
            _item(disposition, restart_step="extraction", restart_sections=["1"])
        )

        assert plan == plain
        assert plan.start != "extraction"


class TestTheFinalizeMessage:
    @pytest.mark.parametrize(
        "disposition", ["complete", "rejected", "removed", "skipped"]
    )
    def test_the_message_carries_the_plan_and_the_token(self, disposition):
        sender, sqs = _sender()
        item = _item(disposition)

        assert sender.send(item) == "m-1"

        sent = sqs.send_message.call_args.kwargs
        assert sent["QueueUrl"] == "queue-url"
        values = _values(sent["MessageAttributes"])
        assert set(values) == {
            "EventType",
            "ObjectKey",
            TOKEN_ATTRIBUTE,
            RUN_PLAN_ATTRIBUTE,
        }
        assert values["EventType"] == FINALIZE_EVENT_TYPE == "ReviewFinalize"
        assert values["ObjectKey"] == KEY
        # The token attribute is what names the execution; it is unchanged.
        assert values[TOKEN_ATTRIBUTE] == TOKEN
        assert decode(values[RUN_PLAN_ATTRIBUTE]) == sender.plan_for(item)

    def test_a_resend_of_the_same_item_carries_the_same_plan_bytes(self):
        sender, sqs = _sender()
        item = _item("complete")

        sender.send(item)
        sender.send(item)

        first, second = (
            call.kwargs["MessageAttributes"][RUN_PLAN_ATTRIBUTE]["StringValue"]
            for call in sqs.send_message.call_args_list
        )
        assert first == second
        assert first.encode("utf-8") == second.encode("utf-8")

    def test_a_resend_of_an_extraction_item_carries_the_same_plan_bytes(self):
        """The sweeper sends the item again: the plan is read from the item, so
        it is the one the first message carried."""
        sender, sqs = _sender()
        item = _extraction_item("2", "1")

        sender.send(item)
        sender.send(item)

        first, second = (
            call.kwargs["MessageAttributes"][RUN_PLAN_ATTRIBUTE]["StringValue"]
            for call in sqs.send_message.call_args_list
        )
        assert first == second
        assert decode(first) == RunPlan(
            kind=RunKind.REVIEW_COMPLETED,
            start="extraction",
            sections=["2", "1"],
            cycle_id=CYCLE,
        )

    def test_build_message_is_what_send_sends(self):
        sender, sqs = _sender()
        item = _item("complete")

        body, attributes = sender.build_message(item)
        sender.send(item)

        sent = sqs.send_message.call_args.kwargs
        # queued_time is reset to "now" on each build, so compare without it.
        built, queued = json.loads(body), json.loads(sent["MessageBody"])
        built.pop("queued_time"), queued.pop("queued_time")
        assert built == queued
        assert attributes == sent["MessageAttributes"]

    def test_an_item_without_a_valid_token_is_not_sent(self):
        sender, sqs = _sender()

        with pytest.raises(ValueError, match="finalize token"):
            sender.send(_item("complete", finalize_token="nope"))

        sqs.send_message.assert_not_called()

    def test_a_missing_tracking_row_names_the_cycle(self):
        documents = MagicMock()
        documents.get_document.return_value = None
        sender = FinalizeSender(MagicMock(), "queue-url", None, documents)

        with pytest.raises(LookupError, match=CYCLE):
            sender.send(_item("complete"))


class TestWhatTheQueuedDocumentKeeps:
    def _queued(self, disposition) -> dict:
        sender, sqs = _sender()
        sender.send(_item(disposition))
        return json.loads(sqs.send_message.call_args.kwargs["MessageBody"])

    @pytest.mark.parametrize("disposition", ["complete", "removed", "skipped"])
    def test_a_run_that_goes_on_clears_what_it_produces_again(self, disposition):
        queued = self._queued(disposition)

        # The run validates the policies, summarises and evaluates again.
        assert queued.get("rule_validation_result") is None
        assert queued.get("summary_report_uri") is None
        assert queued.get("evaluation_report_uri") is None
        assert queued.get("evaluation_status") is None
        # It starts after extraction, so the sections and their results stay.
        assert queued["sections"][0]["extraction_result_uri"].endswith("result.json")
        assert queued["status"] == Status.QUEUED.value

    def test_a_completed_review_clears_the_policy_result_on_the_document(self):
        document = _document()
        sender, _ = _sender(document)

        sender.send(_item("complete"))

        assert document.rule_validation_result is None

    def test_a_rejected_document_keeps_its_results(self):
        queued = self._queued("rejected")

        assert queued["rule_validation_result"] is not None
        assert queued["summary_report_uri"].endswith("summary.md")
        assert queued["evaluation_report_uri"].endswith("report.md")

    def test_the_finalize_run_counts_only_its_own_usage(self):
        assert self._queued("complete")["metering"] == {}


class TestARunThatStartsAtExtraction:
    """The queued document has the fields of what the run produces again
    cleared, and nothing is deleted in S3: the role that sends the message may
    delete only under the document's ``review/`` folder."""

    @pytest.fixture
    def s3(self, monkeypatch):
        """Every route the clearing has to S3, recorded."""
        import idp_common.document_versions as document_versions
        import idp_common.s3 as s3_module

        client = MagicMock(name="s3 client")
        monkeypatch.setattr(s3_module, "get_s3_client", lambda: client)
        purge = MagicMock(name="delete_current_output_objects", return_value=0)
        monkeypatch.setattr(document_versions, "delete_current_output_objects", purge)
        return SimpleNamespace(client=client, purge=purge)

    def _queued(self, item, document=None) -> dict:
        sender, sqs = _sender(_two_sections() if document is None else document)
        sender.send(item)
        return json.loads(sqs.send_message.call_args.kwargs["MessageBody"])

    def test_the_named_sections_are_cleared_and_the_others_kept(self, s3):
        queued = self._queued(_extraction_item("2"))

        kept, cleared = queued["sections"]
        assert (kept["section_id"], cleared["section_id"]) == ("1", "2")
        assert kept["extraction_result_uri"].endswith("sections/1/result.json")
        assert kept["attributes"] == {"Total": 1}
        assert kept["confidence_threshold_alerts"] == [{"attribute_name": "Total"}]
        assert cleared["extraction_result_uri"] is None
        assert cleared.get("attributes") is None
        assert cleared["confidence_threshold_alerts"] == []
        assert cleared.get("missing_required_fields", []) == []
        # The counts are those of the sections that keep their result.
        assert queued["confidence_alert_count"] == 1
        assert queued["missing_required_field_count"] == 1

    def test_no_section_named_clears_every_section(self, s3):
        queued = self._queued(_extraction_item())

        assert [s["extraction_result_uri"] for s in queued["sections"]] == [None, None]
        assert queued["confidence_alert_count"] == 0

    def test_what_the_later_steps_produce_again_is_cleared_too(self, s3):
        queued = self._queued(_extraction_item("2"))

        assert queued.get("rule_validation_result") is None
        assert queued.get("summary_report_uri") is None
        assert queued.get("evaluation_report_uri") is None
        assert queued.get("evaluation_status") is None
        assert queued["status"] == Status.QUEUED.value
        assert queued["metering"] == {}

    def test_nothing_is_deleted_in_s3(self, s3):
        self._queued(_extraction_item("2"))
        self._queued(_extraction_item())

        assert s3.client.mock_calls == []
        s3.purge.assert_not_called()

    def test_the_pages_and_the_sections_themselves_are_kept(self, s3):
        document = _two_sections()

        self._queued(_extraction_item(), document)

        assert [s.section_id for s in document.sections] == ["1", "2"]
        assert [s.page_ids for s in document.sections] == [["1"], ["2"]]

    def test_a_start_at_applying_the_changes_clears_no_section(self, s3):
        queued = self._queued(_item("complete", restart_step="apply_review_changes"))

        assert all(s["extraction_result_uri"] for s in queued["sections"])
        assert queued["confidence_alert_count"] == 2
        assert queued.get("rule_validation_result") is None


class TestTheLegacyResubmit:
    def _resubmitter(self):
        documents = MagicMock()
        documents.get_document.side_effect = lambda key: _document()
        sqs = MagicMock()
        sqs.send_message.return_value = {"MessageId": "m-2"}
        return DocumentResubmitter(sqs, "queue-url", None, documents), sqs

    def test_it_carries_a_legacy_review_plan_from_the_beginning(self):
        resubmitter, sqs = self._resubmitter()
        token = resubmit_token("brj-1", KEY)

        assert resubmitter.resubmit(KEY, idempotency_token=token) == "m-2"

        values = _values(sqs.send_message.call_args.kwargs["MessageAttributes"])
        assert set(values) == {
            "EventType",
            "ObjectKey",
            RESUBMIT_TOKEN_ATTRIBUTE,
            RUN_PLAN_ATTRIBUTE,
        }
        assert values["EventType"] == RESUBMIT_EVENT_TYPE == "ReviewSkipResubmit"
        assert values[RESUBMIT_TOKEN_ATTRIBUTE] == token
        assert decode(values[RUN_PLAN_ATTRIBUTE]) == RunPlan(
            kind=RunKind.LEGACY_REVIEW, start="beginning"
        )

    def test_it_clears_nothing_and_keeps_the_usage(self):
        """The run starts at the beginning and each step's own check skips what
        exists, so the document goes back as it is."""
        resubmitter, sqs = self._resubmitter()

        resubmitter.resubmit(KEY)

        queued = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
        assert queued["rule_validation_result"] is not None
        assert queued["summary_report_uri"].endswith("summary.md")
        assert queued["metering"] == {"OCR/textract/analyze_document": {"pages": 3}}

    def test_without_a_token_there_is_no_token_attribute(self):
        resubmitter, sqs = self._resubmitter()

        resubmitter.resubmit(KEY)

        values = _values(sqs.send_message.call_args.kwargs["MessageAttributes"])
        assert set(values) == {"EventType", "ObjectKey", RUN_PLAN_ATTRIBUTE}
