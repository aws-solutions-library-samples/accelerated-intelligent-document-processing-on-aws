# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one sender of the document queue: what a queued message carries."""

import json

import boto3
import pytest
from moto import mock_aws

from idp_common.models import Document, Page, Section, Status
from idp_common.run_plan.models import (
    MAX_RUN_PLAN_BYTES,
    RUN_PLAN_ATTRIBUTE,
    RunKind,
    RunPlan,
    RunPlanError,
    decode,
    encode,
)
from idp_common.run_plan.sender import (
    EVENT_DOCUMENT_QUEUED,
    EVENT_DOCUMENT_REPROCESSED,
    EVENT_DOCUMENT_RUN_REQUESTED,
    EVENT_REVIEW_FINALIZE,
    EVENT_REVIEW_SKIP_RESUBMIT,
    EVENT_TYPES,
    build_run_message,
    send_run,
)

pytestmark = pytest.mark.unit

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/document-queue"
WORKING_BUCKET = "sender-working"
KEY = "claims/packet.pdf"


class _Queue:
    """Records what is sent."""

    def __init__(self):
        self.sent = []

    def send_message(self, **message):
        self.sent.append(message)
        return {"MessageId": f"message-{len(self.sent)}"}


def _document() -> Document:
    document = Document(
        id=KEY,
        input_key=KEY,
        input_bucket="in",
        output_bucket="out",
        status=Status.QUEUED,
        config_version="lending",
        config_revision=3,
    )
    document.pages["1"] = Page(page_id="1", classification="Invoice")
    document.sections.append(
        Section(section_id="1", classification="Invoice", page_ids=["1"])
    )
    return document


def _plan(**fields) -> RunPlan:
    fields.setdefault("kind", RunKind.REPROCESS)
    fields.setdefault("start", "summarization")
    return RunPlan(**fields)


def _values(message) -> dict:
    return {
        name: attribute["StringValue"]
        for name, attribute in message["MessageAttributes"].items()
    }


@pytest.fixture
def working_bucket(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=WORKING_BUCKET)
        yield client


class TestTheMessage:
    def test_the_attributes_are_the_event_the_key_and_the_plan(self):
        queue = _Queue()
        plan = _plan(skip=["evaluation"])

        message_id = send_run(
            queue,
            QUEUE_URL,
            document=_document(),
            plan=plan,
            event_type=EVENT_DOCUMENT_REPROCESSED,
        )

        assert message_id == "message-1"
        (message,) = queue.sent
        assert message["QueueUrl"] == QUEUE_URL
        assert _values(message) == {
            "EventType": "DocumentReprocessed",
            "ObjectKey": KEY,
            RUN_PLAN_ATTRIBUTE: encode(plan),
        }
        assert decode(_values(message)[RUN_PLAN_ATTRIBUTE]) == plan

    def test_every_attribute_is_a_string_attribute(self):
        queue = _Queue()

        send_run(
            queue,
            QUEUE_URL,
            document=_document(),
            plan=_plan(),
            event_type=EVENT_DOCUMENT_REPROCESSED,
            extra_attributes={"ReviewFinalizeToken": "abc"},
        )

        attributes = queue.sent[0]["MessageAttributes"]
        assert {a["DataType"] for a in attributes.values()} == {"String"}
        assert all(set(a) == {"DataType", "StringValue"} for a in attributes.values())

    def test_extra_attributes_are_added(self):
        queue = _Queue()

        send_run(
            queue,
            QUEUE_URL,
            document=_document(),
            plan=_plan(
                kind=RunKind.REVIEW_COMPLETED,
                start="apply_review_changes",
                cycle_id="c1",
                reviewed=True,
            ),
            event_type=EVENT_REVIEW_FINALIZE,
            extra_attributes={"ReviewFinalizeToken": "token-1"},
        )

        values = _values(queue.sent[0])
        assert values["ReviewFinalizeToken"] == "token-1"
        assert values["EventType"] == "ReviewFinalize"

    def test_sending_twice_gives_the_same_plan_attribute(self):
        queue = _Queue()
        for _ in range(2):
            send_run(
                queue,
                QUEUE_URL,
                document=_document(),
                plan=_plan(),
                event_type=EVENT_DOCUMENT_REPROCESSED,
            )

        first, second = (_values(m)[RUN_PLAN_ATTRIBUTE] for m in queue.sent)
        assert first == second

    def test_a_real_queue_accepts_the_message(self, working_bucket):
        sqs = boto3.client("sqs", region_name="us-east-1")
        url = sqs.create_queue(QueueName="document-queue")["QueueUrl"]
        plan = _plan()

        message_id = send_run(
            sqs,
            url,
            document=_document(),
            plan=plan,
            event_type=EVENT_DOCUMENT_REPROCESSED,
        )

        received = sqs.receive_message(QueueUrl=url, MessageAttributeNames=["All"])[
            "Messages"
        ][0]
        assert received["MessageId"] == message_id
        assert (
            decode(received["MessageAttributes"][RUN_PLAN_ATTRIBUTE]["StringValue"])
            == plan
        )


class TestTheBody:
    def test_without_a_working_bucket_the_body_is_the_document(self):
        queue = _Queue()
        document = _document()

        send_run(
            queue,
            QUEUE_URL,
            document=document,
            plan=_plan(kind=RunKind.NEW_UPLOAD, start="beginning"),
            event_type=EVENT_DOCUMENT_QUEUED,
        )

        body = queue.sent[0]["MessageBody"]
        assert body == document.to_json()
        assert Document.from_json(body).sections[0].section_id == "1"

    def test_with_a_working_bucket_the_body_is_a_reference(self, working_bucket):
        queue = _Queue()
        document = _document()

        send_run(
            queue,
            QUEUE_URL,
            document=document,
            plan=_plan(),
            event_type=EVENT_DOCUMENT_REPROCESSED,
            working_bucket=WORKING_BUCKET,
            prefix="reprocess",
        )

        body = json.loads(queue.sent[0]["MessageBody"])
        assert body["compressed"] is True
        assert body["document_id"] == KEY
        assert body["sections"] == ["1"]
        assert "pages" not in body
        assert "_reprocess_state.json" in body["s3_uri"]
        # The stored object is the whole document.
        restored = Document.load_document(body, WORKING_BUCKET)
        assert restored.pages["1"].classification == "Invoice"
        assert restored.config_revision == 3


class TestBuildRunMessage:
    def test_it_is_the_message_send_run_sends(self):
        queue = _Queue()
        arguments = dict(
            document=_document(),
            plan=_plan(skip=["evaluation"]),
            event_type=EVENT_DOCUMENT_REPROCESSED,
            extra_attributes={"ReviewFinalizeToken": "abc"},
        )

        body, attributes = build_run_message(**arguments)
        send_run(queue, QUEUE_URL, **arguments)

        assert queue.sent == [
            {
                "QueueUrl": QUEUE_URL,
                "MessageBody": body,
                "MessageAttributes": attributes,
            }
        ]

    def test_it_sends_nothing(self):
        body, attributes = build_run_message(
            document=_document(), plan=_plan(), event_type=EVENT_DOCUMENT_REPROCESSED
        )

        assert json.loads(body)["input_key"] == KEY
        assert set(attributes) == {"EventType", "ObjectKey", RUN_PLAN_ATTRIBUTE}

    def test_it_refuses_what_send_run_refuses(self):
        with pytest.raises(ValueError, match="Unknown event type"):
            build_run_message(document=_document(), plan=_plan(), event_type="Nope")


class TestRefusedBeforeAnythingIsSent:
    def _oversized(self) -> RunPlan:
        plan = _plan(start="extraction", sections=[f"{i:064d}" for i in range(500)])
        assert len(json.dumps(plan.model_dump(mode="json"))) > MAX_RUN_PLAN_BYTES
        return plan

    def test_an_oversized_plan_raises_before_any_send(self, working_bucket):
        queue = _Queue()

        with pytest.raises(RunPlanError, match="limit"):
            send_run(
                queue,
                QUEUE_URL,
                document=_document(),
                plan=self._oversized(),
                event_type=EVENT_DOCUMENT_REPROCESSED,
                working_bucket=WORKING_BUCKET,
            )

        assert queue.sent == []
        # And before the document is stored.
        assert "Contents" not in working_bucket.list_objects_v2(Bucket=WORKING_BUCKET)

    def test_a_plan_that_still_says_resume_is_refused(self):
        queue = _Queue()

        with pytest.raises(RunPlanError, match="resume"):
            send_run(
                queue,
                QUEUE_URL,
                document=_document(),
                plan=_plan(kind=RunKind.RESTART_FAILED, start="resume"),
                event_type=EVENT_DOCUMENT_REPROCESSED,
            )

        assert queue.sent == []

    def test_an_unknown_event_type_is_refused(self):
        queue = _Queue()

        with pytest.raises(ValueError, match="Unknown event type"):
            send_run(
                queue,
                QUEUE_URL,
                document=_document(),
                plan=_plan(),
                event_type="SomethingElse",
            )

        assert queue.sent == []

    @pytest.mark.parametrize("name", ["EventType", "ObjectKey", RUN_PLAN_ATTRIBUTE])
    def test_an_extra_attribute_cannot_replace_one_the_sender_sets(self, name):
        queue = _Queue()

        with pytest.raises(ValueError, match="cannot be an extra attribute"):
            send_run(
                queue,
                QUEUE_URL,
                document=_document(),
                plan=_plan(),
                event_type=EVENT_DOCUMENT_REPROCESSED,
                extra_attributes={name: "x"},
            )

        assert queue.sent == []


def test_the_event_types():
    """The strings the senders used before run plans, and one for the rest."""
    assert EVENT_TYPES == (
        "DocumentQueued",
        "DocumentReprocessed",
        "ReviewFinalize",
        "ReviewSkipResubmit",
        "DocumentRunRequested",
    )
    assert EVENT_TYPES == (
        EVENT_DOCUMENT_QUEUED,
        EVENT_DOCUMENT_REPROCESSED,
        EVENT_REVIEW_FINALIZE,
        EVENT_REVIEW_SKIP_RESUBMIT,
        EVENT_DOCUMENT_RUN_REQUESTED,
    )
