# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""abortWorkflow and the review-queue statuses (IDP-0002).

``HITL_IN_PROGRESS`` (a document waiting in a review queue) stays abortable;
``REJECTED`` is terminal and is refused like any finished document.
"""

from unittest.mock import MagicMock

import pytest

from idp_common.models import Document, Status
from tests.unit.lambdas._lambda_loader import load_lambda

abort_index = load_lambda(
    "abort_workflow_resolver_review_index",
    "nested/api-resolvers/src/lambda/abort_workflow_resolver",
    patches={
        "idp_common.docs_service.create_document_service": lambda *a, **kw: MagicMock(),
    },
)


def _event(keys):
    return {
        "arguments": {"objectKeys": keys},
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
    }


@pytest.fixture
def doc_service(monkeypatch):
    svc = MagicMock()
    monkeypatch.setattr(abort_index, "document_service", svc)
    monkeypatch.setattr(abort_index, "sfn_client", MagicMock())
    monkeypatch.setattr(abort_index, "get_setting", lambda name: None)
    return svc


def _doc(status):
    return Document(id="a.pdf", input_key="a.pdf", status=status)


@pytest.mark.unit
class TestAbortReviewStatuses:
    def test_rejected_is_refused(self, doc_service):
        doc_service.get_document.return_value = _doc(Status.REJECTED)

        result = abort_index.handler(_event(["a.pdf"]), None)

        assert result["abortedCount"] == 0
        assert result["errors"] == ["a.pdf: Cannot abort document with status REJECTED"]
        doc_service.update_document.assert_not_called()

    def test_in_review_is_abortable(self, doc_service):
        doc_service.get_document.return_value = _doc(Status.HITL_IN_PROGRESS)

        result = abort_index.handler(_event(["a.pdf"]), None)

        assert result["abortedCount"] == 1
        assert doc_service.update_document.call_args.args[0].status is Status.ABORTED
