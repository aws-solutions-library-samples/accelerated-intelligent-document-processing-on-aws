# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for document run (version) records in the DynamoDB service."""

import json
from decimal import Decimal
from unittest.mock import Mock

import pytest

from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, RuleValidationResult, Section, Status


@pytest.mark.unit
class TestDocumentRuns:
    def setup_method(self):
        self.mock_client = Mock()
        self.service = DocumentDynamoDBService(dynamodb_client=self.mock_client)

    def _completed_document(self):
        doc = Document(
            id="loan-12345/package.pdf",
            input_key="loan-12345/package.pdf",
            status=Status.COMPLETED,
            queued_time="2025-07-07T14:00:00.000Z",
            start_time="2025-07-07T14:01:00.000Z",
            completion_time="2025-07-07T14:15:30.000Z",
            initial_event_time="2025-07-07T13:59:00.000Z",
            workflow_execution_arn=(
                "arn:aws:states:us-east-1:123456789012:execution:sm:exec-abc"
            ),
            config_version="v3",
            num_pages=6,
        )
        doc.sections.append(
            Section(
                section_id="1",
                classification="W2",
                page_ids=["1", "2"],
                extraction_result_uri="s3://out/loan-12345/package.pdf/sections/1/result.json",
            )
        )
        doc.metering = {"tokens": 123, "cost": 0.05}
        return doc

    def test_create_document_run_writes_run_item(self):
        doc = self._completed_document()
        run_id = self.service.create_document_run(
            doc,
            run_id="20250707T141530Z-exec-abc",
            manifest_uri="s3://out/loan-12345/package.pdf/runs/20250707T141530Z-exec-abc/manifest.json",
            file_count=14,
            expires_after=1234567890,
        )

        assert run_id == "20250707T141530Z-exec-abc"
        item = self.mock_client.put_item.call_args[0][0]
        assert item["PK"] == "doc#loan-12345/package.pdf"
        assert item["SK"] == "run#20250707T141530Z-exec-abc"
        assert item["RecordType"] == "run"
        assert item["FileCount"] == 14
        assert item["ConfigVersion"] == "v3"
        assert item["ExpiresAfter"] == 1234567890
        assert item["Sections"][0]["Id"] == "1"
        assert item["Sections"][0]["PageIds"] == [1, 2]
        # Run items must stay out of the TypeDateIndex GSI: no ItemType, and
        # no InitialEventTime (the snapshot uses RunInitialEventTime instead).
        assert "ItemType" not in item
        assert "InitialEventTime" not in item
        assert item["RunInitialEventTime"] == "2025-07-07T13:59:00.000Z"

    def test_create_document_run_snapshots_section_quality_data(self):
        """The run snapshot must carry the same per-section confidence alerts and
        processing issues update_document writes to the live doc item — the UI
        derives "Low Confidence Fields" and section Status from them, and once a
        later run overwrites the outputs there is no other source."""
        from idp_common.models import ProcessingIssue

        doc = self._completed_document()
        doc.sections[0].confidence_threshold_alerts = [
            {
                "attribute_name": "WagesTips",
                "confidence": 0.42,
                "confidence_threshold": 0.8,
            }
        ]
        doc.sections[0].processing_issues = [
            ProcessingIssue(
                stage="assessment",
                severity="warning",
                code="assessment_incomplete",
                message="some rows unscored",
                root_cause="truncation",
                details={"batches": 3},
            )
        ]

        self.service.create_document_run(
            doc, run_id="r1", manifest_uri="s3://m", file_count=1
        )
        section = self.mock_client.put_item.call_args[0][0]["Sections"][0]
        alert = section["ConfidenceThresholdAlerts"][0]
        assert alert["attributeName"] == "WagesTips"
        # Floats must be Decimal for DynamoDB. NB: create_document_run also runs
        # convert_floats_to_decimal over the whole item, so these assertions do
        # NOT pin the shared helper's own conversion — the guard for that is
        # test_service_section_updates.py (update_document_section has no
        # whole-item pass, so it relies on the helper).
        assert alert["confidence"] == Decimal("0.42")
        assert alert["confidenceThreshold"] == Decimal("0.8")
        issue = section["ProcessingIssues"][0]
        assert issue["code"] == "assessment_incomplete"
        assert issue["rootCause"] == "truncation"
        # Details are JSON-stringified so a large blob can't explode the item.
        assert json.loads(issue["details"]) == {"batches": 3}

    def test_create_document_run_omits_empty_quality_data(self):
        """Sections with no alerts/issues stay compact (attributes absent)."""
        doc = self._completed_document()
        self.service.create_document_run(
            doc, run_id="r1", manifest_uri="s3://m", file_count=1
        )
        section = self.mock_client.put_item.call_args[0][0]["Sections"][0]
        assert "ConfidenceThresholdAlerts" not in section
        assert "ProcessingIssues" not in section

    def test_create_document_run_snapshots_classification_signals(self):
        """A historical run must explain its own classification.

        Class confidence, the model's reason, and the boundary signal are what
        make "why was this page classified this way, and why did these two
        documents end up in one section?" answerable after a later run has
        overwritten the outputs (GitHub #673, #565).
        """
        from idp_common.models import Page

        doc = self._completed_document()
        doc.sections[0].confidence = 0.55
        doc.pages["1"] = Page(
            page_id="1",
            classification="W2",
            confidence=0.55,
            classification_reason="Box 1 wage labels",
            document_boundary="start",
        )
        doc.pages["2"] = Page(page_id="2", classification="W2")

        self.service.create_document_run(
            doc, run_id="r1", manifest_uri="s3://m", file_count=1
        )
        item = self.mock_client.put_item.call_args[0][0]
        scored, unscored = item["Pages"]

        assert scored["ClassConfidence"] == Decimal("0.55")
        assert scored["ClassReason"] == "Box 1 wage labels"
        assert scored["Boundary"] == "start"
        assert item["Sections"][0]["Confidence"] == Decimal("0.55")
        # An unscored page adds nothing, keeping items byte-identical to before.
        assert "ClassConfidence" not in unscored
        assert "ClassReason" not in unscored
        assert "Boundary" not in unscored

    def test_rule_validation_result_uri_persisted_as_flat_scalar(self):
        """The schema/UI read the flat RuleValidationResultUri; both the
        update_document write and the run-record snapshot must carry it,
        derived from rule_validation_result.output_uri. (Regression: the flat
        field was dropped in the AppSync->DynamoDB write migration, so the
        Rule Validation tab never rendered despite the nested result being
        stored.)"""
        summary_md = (
            "s3://out/loan-12345/package.pdf/rule_validation/"
            "consolidated/consolidated_summary.md"
        )
        doc = self._completed_document()
        doc.rule_validation_result = RuleValidationResult(
            request_id="loan-12345/package.pdf",
            output_uri=summary_md,
            matched_policy_types=["global_periods"],
        )

        # 1. update_document write expression (call the pure builder directly;
        # update_document() itself does a Mock read-back that isn't relevant here)
        _expr, names, values = self.service._document_to_update_expressions(doc)
        assert names["#RuleValidationResultUri"] == "RuleValidationResultUri"
        assert values[":RuleValidationResultUri"] == summary_md

        # 2. run-record snapshot path
        self.service.create_document_run(doc, run_id="r1", manifest_uri="s3://m")
        item = self.mock_client.put_item.call_args[0][0]
        assert item["RuleValidationResultUri"] == summary_md

    def test_create_document_run_increments_version_count(self):
        doc = self._completed_document()
        self.service.create_document_run(
            doc, run_id="r1", manifest_uri="s3://m", file_count=1
        )
        update_call = self.mock_client.update_item.call_args
        assert update_call.kwargs["key"] == {
            "PK": "doc#loan-12345/package.pdf",
            "SK": "none",
        }
        assert "ADD #VersionCount" in update_call.kwargs["update_expression"]

    def test_create_document_run_survives_counter_failure(self):
        doc = self._completed_document()
        self.mock_client.update_item.side_effect = Exception("throttled")
        run_id = self.service.create_document_run(
            doc, run_id="r1", manifest_uri="s3://m", file_count=1
        )
        assert run_id == "r1"  # run record still created

    def test_create_document_run_is_idempotent(self):
        """A redelivered event (same run_id) must not double-count VersionCount."""
        from idp_common.dynamodb.client import DynamoDBError

        doc = self._completed_document()
        # Second write: ConditionExpression attribute_not_exists(PK) fails.
        self.mock_client.put_item.side_effect = DynamoDBError(
            "exists", error_code="ConditionalCheckFailedException"
        )
        run_id = self.service.create_document_run(
            doc, run_id="r1", manifest_uri="s3://m", file_count=1
        )
        assert run_id == "r1"
        # VersionCount must NOT be incremented on the duplicate.
        self.mock_client.update_item.assert_not_called()

    def test_create_document_run_uses_conditional_put(self):
        doc = self._completed_document()
        self.service.create_document_run(
            doc, run_id="r1", manifest_uri="s3://m", file_count=1
        )
        # Idempotent create-if-absent guard.
        assert (
            self.mock_client.put_item.call_args.kwargs["condition_expression"]
            == "attribute_not_exists(PK)"
        )

    def test_list_document_runs_sorted_newest_first(self):
        self.mock_client.query.return_value = {
            "Items": [
                {
                    "PK": "doc#k",
                    "SK": "run#20250101T000000Z-a",
                    "FileCount": Decimal(3),
                },
                {
                    "PK": "doc#k",
                    "SK": "run#20250707T141530Z-b",
                    "FileCount": Decimal(5),
                },
            ]
        }
        runs = self.service.list_document_runs("k")
        assert [r["SK"] for r in runs] == [
            "run#20250707T141530Z-b",
            "run#20250101T000000Z-a",
        ]
        # Decimals converted to native ints for JSON-friendly API returns
        assert runs[0]["FileCount"] == 5
        query_kwargs = self.mock_client.query.call_args.kwargs
        assert query_kwargs["expression_attribute_values"][":run"] == "run#"

    def test_list_document_runs_paginates(self):
        self.mock_client.query.side_effect = [
            {"Items": [{"SK": "run#a"}], "LastEvaluatedKey": {"PK": "doc#k"}},
            {"Items": [{"SK": "run#b"}]},
        ]
        runs = self.service.list_document_runs("k")
        assert len(runs) == 2
        assert self.mock_client.query.call_count == 2

    def test_get_document_run(self):
        self.mock_client.get_item.return_value = {
            "PK": "doc#k",
            "SK": "run#r1",
            "PageCount": Decimal(6),
        }
        run = self.service.get_document_run("k", "r1")
        assert run["PageCount"] == 6
        self.mock_client.get_item.assert_called_once_with(
            {"PK": "doc#k", "SK": "run#r1"}
        )

    def test_get_document_run_missing(self):
        self.mock_client.get_item.return_value = None
        assert self.service.get_document_run("k", "nope") is None

    def test_delete_document_run(self):
        assert self.service.delete_document_run("k", "r1") is True
        self.mock_client.delete_item.assert_called_once_with(
            {"PK": "doc#k", "SK": "run#r1"}
        )
        update_call = self.mock_client.update_item.call_args
        assert update_call.kwargs["expression_attribute_values"][":neg"] == -1
