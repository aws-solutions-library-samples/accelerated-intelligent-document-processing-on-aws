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


@pytest.mark.unit
class TestAbortDuringPolicyRuleValidation:
    """Policy rule validation runs before review routing, so a document can sit in
    one of its three statuses for as long as the stage takes."""

    @pytest.mark.parametrize(
        "status",
        [
            Status.RULE_VALIDATION_POLICY_CLASSIFICATION,
            Status.RULE_VALIDATION,
            Status.RULE_VALIDATION_ORCHESTRATOR,
        ],
    )
    def test_a_document_in_policy_rule_validation_can_be_aborted(
        self, doc_service, status
    ):
        doc_service.get_document.return_value = _doc(status)

        result = abort_index.handler(_event(["a.pdf"]), None)

        assert result["abortedCount"] == 1
        assert not result["errors"]
        assert doc_service.update_document.call_args.args[0].status is Status.ABORTED

    def test_every_status_a_running_document_can_have_is_abortable(self):
        """A status a step sets while the workflow runs, and that is not abortable,
        leaves a document nobody can stop."""
        running = {
            Status.QUEUED,
            Status.RUNNING,
            Status.PREPROCESSING,
            Status.OCR,
            Status.CLASSIFYING,
            Status.EXTRACTING,
            Status.ASSESSING,
            Status.POSTPROCESSING,
            Status.RULE_VALIDATION_POLICY_CLASSIFICATION,
            Status.RULE_VALIDATION,
            Status.RULE_VALIDATION_ORCHESTRATOR,
            Status.HITL_IN_PROGRESS,
            Status.SUMMARIZING,
            Status.EVALUATING,
        }

        assert running <= abort_index.ABORTABLE_STATUSES
