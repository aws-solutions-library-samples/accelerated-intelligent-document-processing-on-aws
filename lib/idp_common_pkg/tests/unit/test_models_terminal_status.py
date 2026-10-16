# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Terminal document status of a finished execution (IDP-0002)."""

from unittest.mock import MagicMock

import pytest

from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import (
    POST_PROCESSING_SUPPRESSED_STATUSES,
    Document,
    Status,
    terminal_document_status,
)


@pytest.mark.unit
class TestTerminalDocumentStatus:
    @pytest.mark.parametrize(
        ("workflow", "wrapper", "expected"),
        [
            ("SUCCEEDED", None, Status.COMPLETED),
            ("SUCCEEDED", "REDACTED_SUPERSEDED", Status.REDACTED_SUPERSEDED),
            ("SUCCEEDED", "HITL_IN_PROGRESS", Status.HITL_IN_PROGRESS),
            ("SUCCEEDED", "REJECTED", Status.REJECTED),
            ("SUCCEEDED", "COMPLETED", Status.COMPLETED),
            ("SUCCEEDED", "FAILED", Status.COMPLETED),
            ("SUCCEEDED", "garbage", Status.COMPLETED),
            ("ABORTED", None, Status.ABORTED),
            ("ABORTED", "REJECTED", Status.ABORTED),
            ("FAILED", None, Status.FAILED),
            ("FAILED", "HITL_IN_PROGRESS", Status.FAILED),
            ("TIMED_OUT", None, Status.FAILED),
        ],
    )
    def test_table(self, workflow, wrapper, expected):
        assert terminal_document_status(workflow, wrapper) is expected

    def test_suppressed_statuses(self):
        assert POST_PROCESSING_SUPPRESSED_STATUSES == {
            Status.REDACTED_SUPERSEDED,
            Status.HITL_IN_PROGRESS,
            Status.REJECTED,
        }


@pytest.mark.unit
class TestWorkflowStatusMapping:
    """The tracking row's WorkflowStatus written by ``update_document``."""

    @staticmethod
    def _workflow_status(status):
        client = MagicMock()
        client.update_item.return_value = {"Attributes": {}}
        service = DocumentDynamoDBService(dynamodb_client=client)
        service.update_document(Document(id="a.pdf", input_key="a.pdf", status=status))
        kwargs = client.update_item.call_args.kwargs
        return kwargs["expression_attribute_values"][":WorkflowStatus"]

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (Status.REJECTED, "SUCCEEDED"),
            (Status.COMPLETED, "SUCCEEDED"),
            (Status.HITL_IN_PROGRESS, "RUNNING"),
            (Status.FAILED, "FAILED"),
        ],
    )
    def test_mapping(self, status, expected):
        assert self._workflow_status(status) == expected
