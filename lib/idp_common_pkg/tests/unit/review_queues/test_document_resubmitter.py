# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DocumentResubmitter: the legacy-review resubmit, and its idempotency token."""

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from idp_common.models import Document, Status
from idp_common.review_queues.exceptions import ResubmitError, ResubmitNotConfigured
from idp_common.review_queues.finalize import (
    RESUBMIT_EVENT_TYPE,
    DocumentResubmitter,
)
from idp_common.utils.execution_names import (
    RESUBMIT_TOKEN_ATTRIBUTE,
    RESUBMIT_TOKEN_RE,
    resubmit_token,
)

USAGE = {"OCR/textract/analyze_document": {"pages": 3}}


def _documents(document):
    service = MagicMock()
    service.get_document.return_value = document
    return service


def _document():
    return Document(
        id="a.pdf", input_key="a.pdf", status=Status.COMPLETED, metering=dict(USAGE)
    )


@pytest.mark.unit
class TestResubmit:
    def test_sends_the_document_with_the_token_and_keeps_its_usage(self):
        sqs = MagicMock()
        sqs.send_message.return_value = {"MessageId": "m-1"}
        token = resubmit_token("brj-1", "a.pdf")
        sender = DocumentResubmitter(sqs, "url", None, _documents(_document()))

        assert sender.resubmit("a.pdf", idempotency_token=token) == "m-1"

        kwargs = sqs.send_message.call_args.kwargs
        body = json.loads(kwargs["MessageBody"])
        assert body["metering"] == USAGE
        assert body["status"] == Status.QUEUED.value
        attributes = kwargs["MessageAttributes"]
        assert attributes[RESUBMIT_TOKEN_ATTRIBUTE]["StringValue"] == token
        assert attributes["EventType"]["StringValue"] == RESUBMIT_EVENT_TYPE
        assert "ReviewFinalizeToken" not in attributes

    def test_without_a_token_no_token_attribute_is_sent(self):
        sqs = MagicMock()
        sqs.send_message.return_value = {"MessageId": "m"}
        DocumentResubmitter(sqs, "url", None, _documents(_document())).resubmit("a.pdf")
        assert (
            RESUBMIT_TOKEN_ATTRIBUTE
            not in (sqs.send_message.call_args.kwargs["MessageAttributes"])
        )

    def test_a_send_failure_raises(self):
        sqs = MagicMock()
        sqs.send_message.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}}, "SendMessage"
        )
        with pytest.raises(ResubmitError, match="ClientError"):
            DocumentResubmitter(sqs, "url", None, _documents(_document())).resubmit(
                "a.pdf"
            )

    def test_a_missing_document_raises(self):
        with pytest.raises(ResubmitError):
            DocumentResubmitter(MagicMock(), "url", None, _documents(None)).resubmit(
                "a.pdf"
            )

    def test_no_queue_url_raises_not_configured(self):
        with pytest.raises(ResubmitNotConfigured):
            DocumentResubmitter(
                MagicMock(), "", None, _documents(_document())
            ).resubmit("a.pdf")

    def test_tokens_are_deterministic_and_well_formed(self):
        token = resubmit_token("brj-1", "a.pdf")
        assert token == resubmit_token("brj-1", "a.pdf")
        assert token != resubmit_token("brj-2", "a.pdf")
        assert RESUBMIT_TOKEN_RE.match(token)

    def test_a_malformed_token_is_refused(self):
        with pytest.raises(ValueError):
            DocumentResubmitter(
                MagicMock(), "url", None, _documents(_document())
            ).resubmit("a.pdf", idempotency_token="nope")
