# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ProcessingIssue persistence + read-back in the DynamoDB service (Phase 2.1).

Issues are written per-section (camelCase, details JSON-stringified), a top-level
ProcessingIssueCount is written whenever the document carries the sections that
count is derived from — including 0 — and a HasProcessingIssues attribute is set
only when issues exist, in the shape a sparse index would need (it has no reader
today; ProcessingIssueCount is what the document list reads).

The "whenever it carries sections" part is load-bearing and has its own test
below: a sectionless Document reports 0 by absence rather than by measurement, and
writing that 0 erased a count a processing Lambda had just recorded.
"""

from unittest.mock import Mock

import pytest

from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, ProcessingIssue, Section, Status


@pytest.mark.unit
class TestProcessingIssuePersistence:
    def setup_method(self):
        self.service = DocumentDynamoDBService(dynamodb_client=Mock())

    def _doc_with_issue(self):
        section = Section(
            section_id="1",
            classification="bank-statement",
            processing_issues=[
                ProcessingIssue(
                    stage="assessment",
                    severity="error",
                    code="assessment_incomplete",
                    message="5 rows unscored",
                    root_cause="Nova Lite output cap exceeded",
                    section_id="1",
                    details={"unrecoverable_rows": 5},
                )
            ],
        )
        return Document(id="d", input_key="d.pdf", sections=[section])

    def test_issue_count_and_sparse_gsi_set_when_issues_present(self):
        _, names, values = self.service._document_to_update_expressions(
            self._doc_with_issue()
        )
        assert values[":ProcessingIssueCount"] == 1
        assert "HasProcessingIssues" in names.values()
        assert values[":HasProcessingIssues"] == "true"

    def test_sparse_gsi_absent_when_no_issues(self):
        doc = Document(
            id="d",
            input_key="d.pdf",
            sections=[Section(section_id="1", classification="x")],
        )
        _, _, values = self.service._document_to_update_expressions(doc)
        # The document carries a section, so 0 is a measurement; the sparse flag is
        # simply not set.
        assert values[":ProcessingIssueCount"] == 0
        assert ":HasProcessingIssues" not in values

    def test_a_sectionless_document_does_not_stamp_the_count_back_to_zero(self):
        """A bare Document has no information about the count, so it must not write
        one.

        `processing_issue_count` is derived from the sections plus the
        document-level issues, so a Document with no sections reports 0 by absence.
        The caller that does this is `workflow_tracker`: on a FAILED execution it
        builds a Document carrying only a status and a completion time — its
        sections branch is gated on SUCCEEDED — and calls `update_document`. The
        `#Sections` attribute survives that write because it is gated the same way,
        so a section-level issue written moments earlier by the extraction Lambda
        kept its entry while this counter was stamped back to 0. The UI prefers the
        stored value whenever it is merely non-null, so the document list's badge
        read a green 0 for a document whose own section said it had failed.
        """
        bare = Document(id="d", input_key="d.pdf", status=Status.FAILED)
        _, names, values = self.service._document_to_update_expressions(bare)

        assert ":ProcessingIssueCount" not in values, (
            "a sectionless document wrote ProcessingIssueCount, which erases the "
            "count a section-level write had just recorded"
        )
        assert "ProcessingIssueCount" not in names.values()
        assert ":HasProcessingIssues" not in values
        # The status write itself must still happen — this is the tracker's job.
        assert values[":ObjectStatus"] == Status.FAILED.value

    def test_a_document_level_issue_is_still_counted_when_sections_exist(self):
        """The gate is on `sections`, not on where the issues came from, so a
        document-level issue alongside a section still reaches the count."""
        doc = self._doc_with_issue()
        doc.processing_issues = [
            ProcessingIssue(
                stage="ocr",
                severity="warning",
                code="ocr_low_confidence",
                message="blurry",
            )
        ]
        _, _, values = self.service._document_to_update_expressions(doc)
        assert values[":ProcessingIssueCount"] == 2

    def test_section_issues_serialized_camelcase_with_json_details(self):
        _, _, values = self.service._document_to_update_expressions(
            self._doc_with_issue()
        )
        sections = values[":Sections"]
        issues = sections[0]["ProcessingIssues"]
        assert issues[0]["code"] == "assessment_incomplete"
        assert issues[0]["severity"] == "error"
        assert issues[0]["rootCause"] == "Nova Lite output cap exceeded"
        # details is JSON-stringified to avoid nested-map bloat
        assert isinstance(issues[0]["details"], str)
        assert "unrecoverable_rows" in issues[0]["details"]

    def test_round_trip_through_item_to_document(self):
        # Build the persisted item shape, then read it back.
        _, names, values = self.service._document_to_update_expressions(
            self._doc_with_issue()
        )
        item = {
            "ObjectKey": "d.pdf",
            "Sections": values[":Sections"],
            "ProcessingIssueCount": values[":ProcessingIssueCount"],
        }
        doc = self.service._dynamodb_item_to_document(item)
        assert len(doc.sections) == 1
        issues = doc.sections[0].processing_issues
        assert len(issues) == 1
        assert issues[0].code == "assessment_incomplete"
        assert issues[0].severity == "error"
        assert issues[0].root_cause == "Nova Lite output cap exceeded"
        # details JSON round-tripped back to a dict
        assert issues[0].details == {"unrecoverable_rows": 5}
        # rollup property reflects the restored issue
        assert doc.processing_issue_count == 1
        assert doc.has_processing_issues is True
