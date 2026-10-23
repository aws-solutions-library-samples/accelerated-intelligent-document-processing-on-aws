# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Terminal document status of a finished execution (IDP-0002)."""

import json
from unittest.mock import MagicMock

import pytest

from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import (
    POST_PROCESSING_SUPPRESSED_STATUSES,
    Document,
    Status,
    document_wrapper_status,
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
class TestDocumentWrapperStatus:
    """Where the state machine's terminal status is read from in the output."""

    @pytest.mark.parametrize(
        "output",
        [
            {"document": {"status": "REJECTED", "id": "a"}},
            {"Result": {"document": {"status": "REJECTED"}}},
            {"status": "REJECTED", "id": "a"},
        ],
        ids=["root-document", "result-document", "bare-document"],
    )
    def test_each_output_shape(self, output):
        assert document_wrapper_status(output) == "REJECTED"

    def test_a_json_string_is_parsed(self):
        output = json.dumps({"document": {"status": "HITL_IN_PROGRESS"}})
        assert document_wrapper_status(output) == "HITL_IN_PROGRESS"

    def test_the_root_document_wins_over_result(self):
        output = {
            "document": {"status": "REJECTED"},
            "Result": {"document": {"status": "COMPLETED"}},
        }
        assert document_wrapper_status(output) == "REJECTED"

    @pytest.mark.parametrize(
        "output",
        [
            "not json",
            None,
            ["document"],
            42,
            {"document": "compressed-uri"},
            {"Result": "x", "status": None},
            {"document": {"status": 3}},
            {"document": {}},
        ],
    )
    def test_anything_without_a_string_status_is_none(self, output):
        assert document_wrapper_status(output) is None

    def test_a_result_without_a_document_falls_back_to_the_root(self):
        assert document_wrapper_status({"Result": {"x": 1}, "status": "REJECTED"}) == (
            "REJECTED"
        )

    def test_composes_with_terminal_document_status(self):
        output = json.dumps({"Result": {"document": {"status": "REJECTED"}}})
        status = terminal_document_status("SUCCEEDED", document_wrapper_status(output))
        assert status is Status.REJECTED


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
