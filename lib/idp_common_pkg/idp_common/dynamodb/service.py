# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DynamoDB service for handling document operations directly.

This module provides the DocumentDynamoDBService class for managing document
storage and retrieval through direct DynamoDB operations, bypassing AppSync.
"""

import datetime
import json
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from idp_common.dynamodb.client import DynamoDBClient
from idp_common.models import (
    Document,
    Page,
    ProcessingIssue,
    Section,
    Status,
    coerce_revision,
)

logger = logging.getLogger(__name__)

# TypeDateIndex hash-key values. A document submitted by a non-production caller
# (currently only Test Studio) gets its own ItemType so it never appears in the
# production Document List, while staying efficiently queryable on its own.
ITEM_TYPE_DOCUMENT = "document"
ITEM_TYPE_TEST_DOCUMENT = "test-document"


def convert_floats_to_decimal(obj):
    """
    Recursively convert float values to Decimal for DynamoDB compatibility.

    Args:
        obj: Object that may contain float values

    Returns:
        Object with floats converted to Decimal
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {key: convert_floats_to_decimal(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_floats_to_decimal(item) for item in obj]
    else:
        return obj


def convert_decimals_to_native(obj):
    """
    Recursively convert Decimal values to int or float for JSON serialization.

    Args:
        obj: Object that may contain Decimal values (e.g. from DynamoDB)

    Returns:
        Object with Decimals converted to int (if whole) or float
    """
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    elif isinstance(obj, dict):
        return {k: convert_decimals_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [convert_decimals_to_native(i) for i in obj]
    return obj


def serialize_page_classification_signals(page: Any) -> Dict[str, Any]:
    """Serialize a Page's OPTIONAL classification signals for DynamoDB.

    Returns only the keys that have a value: ``ClassConfidence`` (the
    classifier's confidence in ``Class``, as a Decimal — DynamoDB rejects
    floats), ``ClassReason`` (the model's stated evidence) and ``Boundary``
    (the ``start``/``continue`` signal ``sectionSplitting: llm_determined``
    splits on). A page that produced none of them adds no attributes, so items
    stay byte-identical to what older code wrote.

    Named ``ClassConfidence`` rather than ``Confidence`` because a page item
    already carries ``TextConfidenceUri``, which is OCR confidence — a different
    measurement entirely. The prefix ties this one to the sibling ``Class``.

    Shared by the live doc item writer (update_document) and the run-record
    writer (create_document_run) so a version snapshot never carries fewer
    signals than the live document.
    """
    signals: Dict[str, Any] = {}
    if page.confidence is not None:
        signals["ClassConfidence"] = Decimal(str(float(page.confidence)))
    if getattr(page, "classification_reason", None):
        signals["ClassReason"] = page.classification_reason
    candidates = getattr(page, "classification_candidates", None)
    if candidates:
        # Ranked alternatives ("80% W-2, 15% 1099") from topk mode. Stored as a
        # list of maps rather than a JSON string so the UI can render it without
        # a parse step, mirroring ConfidenceThresholdAlerts.
        signals["ClassCandidates"] = [
            {
                "Class": str(candidate.get("class", "")),
                "Probability": Decimal(str(float(candidate.get("probability", 0.0)))),
            }
            for candidate in candidates
            if isinstance(candidate, dict) and candidate.get("class")
        ]
    if page.document_boundary:
        signals["Boundary"] = page.document_boundary
    return signals


def serialize_confidence_threshold_alerts(section: Section) -> List[Dict[str, Any]]:
    """
    Serialize a Section's confidence threshold alerts to the DynamoDB/GraphQL
    camelCase shape (``attributeName``/``confidence``/``confidenceThreshold``).

    Shared by the doc item writers (update_document,
    update_document_section) and the run-record writer (create_document_run) so
    a version snapshot carries the same low-confidence data the live document
    does — the UI's "Low Confidence Fields" count reads this field.
    """
    alerts_data: List[Dict[str, Any]] = []
    for alert in section.confidence_threshold_alerts or []:
        alerts_data.append(
            convert_floats_to_decimal(
                {
                    "attributeName": alert.get("attribute_name"),
                    "confidence": alert.get("confidence"),
                    "confidenceThreshold": alert.get("confidence_threshold"),
                }
            )
        )
    return alerts_data


def serialize_processing_issues(section: Section) -> List[Dict[str, Any]]:
    """
    Serialize a Section's structured processing issues to compact camelCase
    dicts mirroring the GraphQL ``ProcessingIssue`` type. ``details`` is
    JSON-stringified rather than stored as a nested map, keeping the attribute
    to one scalar regardless of the blob's shape. Note no consumer reads
    ``details`` back today (the API resolvers strip it — it is not part of the
    GraphQL type); it is written for parity with the live doc item.
    """
    issues_data: List[Dict[str, Any]] = []
    for issue in section.processing_issues or []:
        issue_item: Dict[str, Any] = {
            "stage": issue.stage,
            "severity": issue.severity,
            "code": issue.code,
            "message": issue.message,
        }
        if issue.root_cause:
            issue_item["rootCause"] = issue.root_cause
        if issue.details:
            issue_item["details"] = json.dumps(issue.details, default=str)
        issues_data.append(issue_item)
    return issues_data


class DocumentDynamoDBService:
    """
    Service for interacting directly with DynamoDB to manage Documents.

    This service provides methods to convert between Document objects and the
    DynamoDB item format, and to create and update documents directly in DynamoDB.
    """

    def __init__(
        self,
        dynamodb_client: Optional[DynamoDBClient] = None,
        table_name: Optional[str] = None,
    ):
        """
        Initialize the DocumentDynamoDBService.

        Args:
            dynamodb_client: Optional DynamoDBClient instance. If not provided, a new one will be created.
            table_name: Optional DynamoDB table name. Used only if dynamodb_client is not provided.
        """
        self.client = dynamodb_client or DynamoDBClient(table_name=table_name)

    def _generate_shard_info(self, queued_time: str) -> tuple[str, str]:
        """
        Generate shard information for list partitioning based on queued time.

        Args:
            queued_time: ISO 8601 timestamp string

        Returns:
            Tuple of (list_pk, list_sk) for the list partition
        """
        shards_in_day = 6
        shard_divider = 24 // shards_in_day

        # Extract date and hour from timestamp
        date = queued_time[:10]  # YYYY-MM-DD
        hour_string = queued_time[11:13]  # HH
        hour = int(hour_string)

        # Calculate shard
        hour_shard = hour // shard_divider
        shard_pad = f"{hour_shard:02d}"

        list_pk = f"list#{date}#s#{shard_pad}"
        list_sk = f"ts#{queued_time}#id#{queued_time}"

        return list_pk, list_sk

    def _document_to_create_item(
        self, document: Document, expires_after: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Convert a Document object to a DynamoDB item for creation.

        Args:
            document: The Document object to convert
            expires_after: Optional TTL timestamp for document expiration

        Returns:
            Dictionary compatible with DynamoDB item format
        """
        item = {
            "PK": f"doc#{document.input_key}",
            "SK": "none",
            "ObjectKey": document.input_key,
            "ObjectStatus": document.status.value,
            "InitialEventTime": document.initial_event_time,
            "QueuedTime": document.queued_time,
            # Partitioning on the index hash key, not a FilterExpression over a
            # projected attribute: DynamoDB applies FilterExpression *after* Limit,
            # so where most documents are test artifacts a page of 50 can return 1
            # with no indication the rest were dropped.
            "ItemType": ITEM_TYPE_TEST_DOCUMENT
            if document.submission_source
            else ITEM_TYPE_DOCUMENT,
        }

        if document.submission_source:
            item["SubmissionSource"] = document.submission_source
        if document.test_set_id:
            item["TestSetId"] = document.test_set_id

        if expires_after:
            item["ExpiresAfter"] = expires_after

        return item

    def _document_to_update_expressions(
        self, document: Document
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        """
        Convert a Document object to DynamoDB update expressions.

        Args:
            document: The Document object to convert

        Returns:
            Tuple of (update_expression, expression_attribute_names, expression_attribute_values)
        """
        set_expressions = []
        expression_names = {}
        expression_values = {}

        # Always update ObjectStatus
        set_expressions.append("#ObjectStatus = :ObjectStatus")
        expression_names["#ObjectStatus"] = "ObjectStatus"
        expression_values[":ObjectStatus"] = document.status.value

        # Add optional fields if they exist
        if document.queued_time:
            set_expressions.append("#QueuedTime = :QueuedTime")
            expression_names["#QueuedTime"] = "QueuedTime"
            expression_values[":QueuedTime"] = document.queued_time

        if document.start_time:
            set_expressions.append("#WorkflowStartTime = :WorkflowStartTime")
            expression_names["#WorkflowStartTime"] = "WorkflowStartTime"
            expression_values[":WorkflowStartTime"] = document.start_time

        if document.completion_time:
            set_expressions.append("#CompletionTime = :CompletionTime")
            expression_names["#CompletionTime"] = "CompletionTime"
            expression_values[":CompletionTime"] = document.completion_time

        if document.workflow_execution_arn:
            set_expressions.append("#WorkflowExecutionArn = :WorkflowExecutionArn")
            expression_names["#WorkflowExecutionArn"] = "WorkflowExecutionArn"
            expression_values[":WorkflowExecutionArn"] = document.workflow_execution_arn

        # Persist the configuration version (read from the input object's
        # `config-version` S3 metadata at queue time) so the UI/GSI can display
        # which config each document was processed with. Without this the tracking
        # item never carries ConfigVersion and the UI shows "N/A".
        if document.config_version:
            set_expressions.append("#ConfigVersion = :ConfigVersion")
            expression_names["#ConfigVersion"] = "ConfigVersion"
            expression_values[":ConfigVersion"] = document.config_version

        # The revision of that profile the document is pinned to, so a result can
        # be traced back to the exact configuration that produced it even after
        # the profile has been saved again.
        if document.config_revision is not None:
            set_expressions.append("#ConfigRevision = :ConfigRevision")
            expression_names["#ConfigRevision"] = "ConfigRevision"
            expression_values[":ConfigRevision"] = int(document.config_revision)

        # Set workflow status based on document status
        if document.status == Status.FAILED:
            workflow_status = "FAILED"
        elif document.status == Status.COMPLETED:
            workflow_status = "SUCCEEDED"
        elif document.status == Status.ABORTED:
            workflow_status = "ABORTED"
        else:
            workflow_status = "RUNNING"

        set_expressions.append("#WorkflowStatus = :WorkflowStatus")
        expression_names["#WorkflowStatus"] = "WorkflowStatus"
        expression_values[":WorkflowStatus"] = workflow_status

        if document.num_pages > 0:
            set_expressions.append("#PageCount = :PageCount")
            expression_names["#PageCount"] = "PageCount"
            expression_values[":PageCount"] = document.num_pages

        # Convert pages
        if document.pages:
            pages_data = []
            for page_id, page in document.pages.items():
                # In the DynamoDB schema, page IDs are integers
                try:
                    page_id_int = int(page_id)
                except ValueError:
                    logger.warning(f"Skipping page {page_id} - ID is not an integer")
                    continue

                page_data = {
                    "Id": page_id_int,
                    "Class": page.classification or "",
                    "ImageUri": page.image_uri or "",
                    "TextUri": page.parsed_text_uri or page.raw_text_uri or "",
                    "OcrPageDataUri": page.ocr_page_data_uri or "",
                }
                # Optional classification signals — the confidence in Class, the
                # model's reason for it, and the "start"/"continue" boundary
                # signal that sectionSplitting: llm_determined splits on.
                # Persisted so a surprising classification or section merge can
                # be audited after the fact instead of re-derived from Lambda
                # logs. Each is omitted when absent.
                page_data.update(serialize_page_classification_signals(page))
                pages_data.append(page_data)

            if pages_data:
                set_expressions.append("#Pages = :Pages")
                expression_names["#Pages"] = "Pages"
                expression_values[":Pages"] = pages_data

        # Convert sections
        if document.sections:
            sections_data = []
            for section in document.sections:
                # Convert page IDs to integers for DynamoDB
                page_ids = []
                for page_id in section.page_ids:
                    try:
                        page_ids.append(int(page_id))
                    except ValueError:
                        logger.warning(
                            f"Skipping page ID {page_id} in section {section.section_id} - not an integer"
                        )

                section_data = {
                    "Id": section.section_id,
                    "PageIds": page_ids,
                    "Class": section.classification,
                    "OutputJSONUri": section.extraction_result_uri or "",
                }

                # Confidence in the section's CLASS (aggregated from its pages),
                # distinct from the per-field ConfidenceThresholdAlerts below.
                # Omitted when not scored.
                if section.confidence is not None:
                    section_data["Confidence"] = Decimal(str(float(section.confidence)))

                # Convert confidence threshold alerts (matching current AppSync interface)
                if section.confidence_threshold_alerts:
                    section_data["ConfidenceThresholdAlerts"] = (
                        serialize_confidence_threshold_alerts(section)
                    )

                # Persist structured processing issues (self-healing observability).
                if section.processing_issues:
                    section_data["ProcessingIssues"] = serialize_processing_issues(
                        section
                    )

                # How many documents (instances) of this class the section holds.
                # Omitted when undetermined (0) so items stay byte-identical to
                # what older code wrote.
                if section.instance_count:
                    section_data["InstanceCount"] = section.instance_count

                # Exclusion flags for a section whose class carries
                # x-aws-idp-exclude-from-processing. This is the write that makes
                # the UI's "Skipped" badge possible — the badge is the only
                # explanation a user gets for why an excluded section's panels are
                # empty. Omitted when not excluded, per the convention above.
                if section.excluded:
                    section_data["Excluded"] = True
                    if section.exclusion_reason:
                        section_data["ExclusionReason"] = section.exclusion_reason

                sections_data.append(section_data)

            if sections_data:
                set_expressions.append("#Sections = :Sections")
                expression_names["#Sections"] = "Sections"
                expression_values[":Sections"] = sections_data

        # Add metering data if available
        if document.metering:
            set_expressions.append("#Metering = :Metering")
            expression_names["#Metering"] = "Metering"
            expression_values[":Metering"] = json.dumps(document.metering, default=str)

        # Add evaluation status & report if available
        if document.evaluation_status:
            set_expressions.append("#EvaluationStatus = :EvaluationStatus")
            expression_names["#EvaluationStatus"] = "EvaluationStatus"
            expression_values[":EvaluationStatus"] = document.evaluation_status

        if document.evaluation_report_uri:
            set_expressions.append("#EvaluationReportUri = :EvaluationReportUri")
            expression_names["#EvaluationReportUri"] = "EvaluationReportUri"
            expression_values[":EvaluationReportUri"] = document.evaluation_report_uri

        # Add summary report if available
        if document.summary_report_uri:
            set_expressions.append("#SummaryReportUri = :SummaryReportUri")
            expression_names["#SummaryReportUri"] = "SummaryReportUri"
            expression_values[":SummaryReportUri"] = document.summary_report_uri

        # Add rule validation result if available
        if document.rule_validation_result:
            set_expressions.append("#RuleValidationResult = :RuleValidationResult")
            expression_names["#RuleValidationResult"] = "RuleValidationResult"
            # Store as JSON string to preserve structure
            rule_validation_dict = {
                "request_id": document.rule_validation_result.request_id,
                "summary": document.rule_validation_result.summary,
                "section_results": document.rule_validation_result.section_results,
                "metadata": document.rule_validation_result.metadata,
                "output_uri": document.rule_validation_result.output_uri,
                "errors": document.rule_validation_result.errors,
                "matched_policy_types": document.rule_validation_result.matched_policy_types,
                "matched_page_ids": document.rule_validation_result.matched_page_ids,
            }
            expression_values[":RuleValidationResult"] = json.dumps(
                rule_validation_dict, default=str
            )
            # Also persist the flat URI scalar the schema/UI read directly
            # (getDocument returns RuleValidationResultUri, and the UI's
            # DocumentViewers renders the Rule Validation tab only when it is
            # present). Without this the report never appears in the doc detail
            # page even though the nested RuleValidationResult is stored. This
            # mirrors the pre-AppSync-removal behaviour, where appsync/service.py
            # set RuleValidationResultUri = output_uri "for backward
            # compatibility"; that line was lost in the move to DynamoDB writes.
            if document.rule_validation_result.output_uri:
                set_expressions.append(
                    "#RuleValidationResultUri = :RuleValidationResultUri"
                )
                expression_names["#RuleValidationResultUri"] = "RuleValidationResultUri"
                expression_values[":RuleValidationResultUri"] = (
                    document.rule_validation_result.output_uri
                )

        # Add trace_id if available
        if document.trace_id:
            set_expressions.append("#TraceId = :TraceId")
            expression_names["#TraceId"] = "TraceId"
            expression_values[":TraceId"] = document.trace_id

        # Add Review Status fields if available
        if document.hitl_status:
            set_expressions.append("#HITLStatus = :HITLStatus")
            expression_names["#HITLStatus"] = "HITLStatus"
            expression_values[":HITLStatus"] = document.hitl_status
            # Maintain sparse GSI attribute for pending review queries
            # "PendingReview" = initial trigger, "Review Pending" = after release_review,
            # "InProgress" = after claim_review
            pending_statuses = ("PendingReview", "Review Pending", "InProgress")
            if document.hitl_status in pending_statuses:
                set_expressions.append("#HITLPendingReview = :HITLPendingReview")
                expression_names["#HITLPendingReview"] = "HITLPendingReview"
                expression_values[":HITLPendingReview"] = "true"
        if document.hitl_sections_pending:
            set_expressions.append("#HITLSectionsPending = :HITLSectionsPending")
            expression_names["#HITLSectionsPending"] = "HITLSectionsPending"
            expression_values[":HITLSectionsPending"] = document.hitl_sections_pending
        if document.hitl_sections_completed:
            set_expressions.append("#HITLSectionsCompleted = :HITLSectionsCompleted")
            expression_names["#HITLSectionsCompleted"] = "HITLSectionsCompleted"
            expression_values[":HITLSectionsCompleted"] = (
                document.hitl_sections_completed
            )

        # Always persist confidence alert count (even 0) so GSI has it for listDocuments
        set_expressions.append("#ConfidenceAlertCount = :ConfidenceAlertCount")
        expression_names["#ConfidenceAlertCount"] = "ConfidenceAlertCount"
        expression_values[":ConfidenceAlertCount"] = document.confidence_alert_count

        # Always persist processing-issue count (even 0) so the document list can
        # show/filter on it, mirroring ConfidenceAlertCount (the authoritative
        # filterable source of truth).
        issue_count = document.processing_issue_count
        set_expressions.append("#ProcessingIssueCount = :ProcessingIssueCount")
        expression_names["#ProcessingIssueCount"] = "ProcessingIssueCount"
        expression_values[":ProcessingIssueCount"] = issue_count

        # Sparse GSI attribute for cheap "has processing issues" filtering — SET
        # only when there ARE issues (mirrors the HITLPendingReview sparse pattern).
        # Not proactively removed on issue-free writes: ProcessingIssueCount (always
        # written, above) is the authoritative filter source, and avoiding a REMOVE
        # here keeps the update-expression additive.
        if issue_count > 0:
            set_expressions.append("#HasProcessingIssues = :HasProcessingIssues")
            expression_names["#HasProcessingIssues"] = "HasProcessingIssues"
            expression_values[":HasProcessingIssues"] = "true"

        # Build update expression with optional REMOVE clause
        update_expression = "SET " + ", ".join(set_expressions)

        # Remove HITLPendingReview GSI attribute when review is completed/skipped
        remove_expressions = []
        if document.hitl_status:
            pending_statuses = ("PendingReview", "Review Pending", "InProgress")
            if document.hitl_status not in pending_statuses:
                remove_expressions.append("HITLPendingReview")
        if remove_expressions:
            update_expression += " REMOVE " + ", ".join(remove_expressions)

        # Convert any float values to Decimal for DynamoDB compatibility
        expression_values = convert_floats_to_decimal(expression_values)  # type: ignore[assignment]

        return update_expression, expression_names, expression_values

    def _dynamodb_item_to_document(self, item: Dict[str, Any]) -> Document:
        """
        Convert DynamoDB item data to a Document object.

        Args:
            item: The document item returned from DynamoDB

        Returns:
            Document object populated with data from DynamoDB
        """
        # Create document with basic properties
        doc = Document(
            id=item.get("ObjectKey"),
            input_key=item.get("ObjectKey"),
            num_pages=int(item.get("PageCount", 0)),  # Ensure PageCount is integer
            queued_time=item.get("QueuedTime"),
            start_time=item.get("WorkflowStartTime"),
            completion_time=item.get("CompletionTime"),
            workflow_execution_arn=item.get("WorkflowExecutionArn"),
            evaluation_report_uri=item.get("EvaluationReportUri"),
            summary_report_uri=item.get("SummaryReportUri"),
            trace_id=item.get("TraceId"),
            initial_event_time=item.get("InitialEventTime"),
            config_version=item.get("ConfigVersion"),
            config_revision=coerce_revision(item.get("ConfigRevision")),
        )

        # Convert status
        object_status = item.get("ObjectStatus")
        if object_status:
            try:
                doc.status = Status(object_status)
            except ValueError:
                logger.warning(f"Unknown status '{object_status}', using QUEUED")
                doc.status = Status.QUEUED

        # Convert metering data - handle both JSON string and native dict formats
        metering_data = item.get("Metering")
        if metering_data:
            try:
                if isinstance(metering_data, str):
                    # It's a JSON string, parse it
                    if metering_data.strip():  # Only parse non-empty strings
                        doc.metering = json.loads(metering_data)
                    else:
                        doc.metering = {}
                else:
                    # It's already a dict/object (native DynamoDB format), use it directly
                    doc.metering = metering_data
            except json.JSONDecodeError:
                logger.warning("Failed to parse metering JSON string, using empty dict")
                doc.metering = {}
            except Exception as e:
                logger.warning(f"Error processing metering data: {e}, using empty dict")
                doc.metering = {}

        # Convert pages
        pages_data = item.get("Pages", [])
        if pages_data is not None:  # Ensure pages_data is not None before iterating
            for page_data in pages_data:
                page_id = str(page_data.get("Id"))
                text_uri = page_data.get("TextUri")
                doc.pages[page_id] = Page(
                    page_id=page_id,
                    image_uri=page_data.get("ImageUri"),
                    raw_text_uri=text_uri,
                    parsed_text_uri=text_uri,  # Set both raw and parsed to same URI
                    text_confidence_uri=page_data.get("TextConfidenceUri"),
                    ocr_page_data_uri=page_data.get("OcrPageDataUri") or None,
                    classification=page_data.get("Class"),
                    # Stored as a DynamoDB Decimal; back to float for the model.
                    # Absent => not scored (None), never a presumed 1.0.
                    confidence=(
                        float(page_data["ClassConfidence"])
                        if page_data.get("ClassConfidence") is not None
                        else None
                    ),
                    classification_reason=page_data.get("ClassReason") or None,
                    classification_candidates=[
                        {
                            "class": candidate.get("Class"),
                            "probability": float(candidate["Probability"])
                            if candidate.get("Probability") is not None
                            else None,
                        }
                        for candidate in page_data.get("ClassCandidates") or []
                    ]
                    or None,
                    document_boundary=page_data.get("Boundary") or None,
                )

        # Convert sections
        sections_data = item.get("Sections", [])
        if (
            sections_data is not None
        ):  # Ensure sections_data is not None before iterating
            for section_data in sections_data:
                # Convert page IDs to strings
                page_ids = [str(page_id) for page_id in section_data.get("PageIds", [])]

                # Convert confidence threshold alerts (matching current AppSync interface)
                confidence_threshold_alerts = []
                alerts_data = section_data.get("ConfidenceThresholdAlerts", [])
                if alerts_data:
                    for alert in alerts_data:
                        confidence_threshold_alerts.append(
                            {
                                "attribute_name": alert.get("attributeName"),
                                "confidence": alert.get("confidence"),
                                "confidence_threshold": alert.get(
                                    "confidenceThreshold"
                                ),
                            }
                        )

                # Convert persisted processing issues back to ProcessingIssue.
                processing_issues = []
                issues_data = section_data.get("ProcessingIssues", [])
                if issues_data:
                    for iss in issues_data:
                        details = iss.get("details")
                        if isinstance(details, str):
                            try:
                                details = json.loads(details)
                            except (json.JSONDecodeError, TypeError):
                                details = {}
                        processing_issues.append(
                            ProcessingIssue(
                                stage=iss.get("stage", ""),
                                severity=iss.get("severity", "info"),
                                code=iss.get("code", ""),
                                message=iss.get("message", ""),
                                root_cause=iss.get("rootCause", ""),
                                section_id=section_data.get("Id"),
                                details=details or {},
                            )
                        )

                doc.sections.append(
                    Section(
                        section_id=section_data.get("Id", ""),
                        classification=section_data.get("Class", ""),
                        # Confidence in the CLASS. Absent => not scored.
                        confidence=(
                            float(section_data["Confidence"])
                            if section_data.get("Confidence") is not None
                            else None
                        ),
                        page_ids=page_ids,
                        extraction_result_uri=section_data.get("OutputJSONUri"),
                        confidence_threshold_alerts=confidence_threshold_alerts,
                        processing_issues=processing_issues,
                        instance_count=int(section_data.get("InstanceCount") or 0),
                        # Absent on items written before the flags were persisted,
                        # and on every non-excluded section.
                        excluded=bool(section_data.get("Excluded", False)),
                        exclusion_reason=section_data.get("ExclusionReason"),
                    )
                )

        # Convert Review Status fields
        doc.hitl_status = item.get("HITLStatus")
        doc.hitl_sections_pending = item.get("HITLSectionsPending", [])
        doc.hitl_sections_completed = item.get("HITLSectionsCompleted", [])

        # Convert rule validation result if present
        if item.get("RuleValidationResult"):
            try:
                from idp_common.models import RuleValidationResult

                rv_data = item.get("RuleValidationResult")
                doc.rule_validation_result = RuleValidationResult(
                    request_id=rv_data.get("request_id"),
                    summary=rv_data.get("summary"),
                    section_results=rv_data.get("section_results"),
                    metadata=rv_data.get("metadata"),
                    output_uri=rv_data.get("output_uri"),
                    errors=rv_data.get("errors"),
                    matched_policy_types=rv_data.get("matched_policy_types"),
                    matched_page_ids=rv_data.get("matched_page_ids"),
                )
            except Exception as e:
                logger.warning(f"Failed to parse RuleValidationResult: {e}")

        return doc

    def create_document(
        self, document: Document, expires_after: Optional[int] = None
    ) -> Optional[str]:
        """
        Create a new document in DynamoDB using a transaction.

        Args:
            document: The Document object to create
            expires_after: Optional TTL timestamp for document expiration

        Returns:
            The ObjectKey of the created document

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        # Create the main document item
        doc_item = self._document_to_create_item(document, expires_after)

        # Generate shard information for list partition
        list_pk, list_sk = self._generate_shard_info(document.queued_time)

        # Create list item for time-based queries
        list_item = {
            "PK": list_pk,
            "SK": list_sk,
            "ObjectKey": document.input_key,
            "QueuedTime": document.queued_time,
        }

        if expires_after:
            list_item["ExpiresAfter"] = expires_after

        # Execute transaction to create both items
        transact_items = [
            {
                "Put": {
                    "Item": doc_item,
                }
            },
            {
                "Put": {
                    "Item": list_item,
                }
            },
        ]

        self.client.transact_write_items(transact_items)
        logger.info(f"Successfully created document: {document.input_key}")

        return document.input_key

    def update_document(self, document: Document) -> Document:
        """
        Update an existing document in DynamoDB.

        Args:
            document: The Document object to update

        Returns:
            Updated Document object with any data returned from DynamoDB

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        key = {
            "PK": f"doc#{document.input_key}",
            "SK": "none",
        }

        update_expression, expression_names, expression_values = (
            self._document_to_update_expressions(document)
        )

        response = self.client.update_item(
            key=key,
            update_expression=update_expression,
            expression_attribute_names=expression_names,
            expression_attribute_values=expression_values,
            return_values="ALL_NEW",
        )

        # Convert the response back to a Document object
        updated_item = response.get("Attributes", {})
        updated_document = self._dynamodb_item_to_document(updated_item)

        logger.info(f"Successfully updated document: {document.input_key}")
        return updated_document

    def get_document(self, object_key: str) -> Optional[Document]:
        """
        Get a document from DynamoDB by its object key.

        Args:
            object_key: The object key of the document to retrieve

        Returns:
            Document object if found, None otherwise

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        key = {
            "PK": f"doc#{object_key}",
            "SK": "none",
        }

        item = self.client.get_item(key)
        if item:
            return self._dynamodb_item_to_document(item)
        return None

    def batch_get_documents(self, object_keys: List[str]) -> List[Dict[str, Any]]:
        """Batch get document records by object keys (max 100)."""
        keys = [{"PK": f"doc#{k}", "SK": "none"} for k in object_keys]
        items = self.client.batch_get_items(keys)
        return [
            {
                "document_id": item.get("PK", "").replace("doc#", ""),
                "status": item.get("ObjectStatus", ""),
            }
            for item in items
        ]

    def list_documents(
        self,
        start_date_time: Optional[str] = None,
        end_date_time: Optional[str] = None,
        limit: Optional[int] = None,
        exclusive_start_key: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        List documents with optional date filtering.

        Uses TypeDateIndex GSI when available for efficient querying.
        Falls back to scan if GSI query fails.

        Args:
            start_date_time: Optional start datetime filter (ISO 8601)
            end_date_time: Optional end datetime filter (ISO 8601)
            limit: Optional limit on number of items to return
            exclusive_start_key: Optional key to start scanning from

        Returns:
            Dict containing documents and pagination info

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        # Try GSI query first (efficient)
        try:
            return self._list_documents_via_gsi(
                start_date_time, end_date_time, limit, exclusive_start_key
            )
        except Exception as e:
            logger.warning(f"GSI query failed, falling back to scan: {e}")

        # Fallback to scan
        filter_expression = None
        expression_attribute_values = {}

        if start_date_time and end_date_time:
            filter_expression = "InitialEventTime BETWEEN :start_date AND :end_date"
            expression_attribute_values[":start_date"] = start_date_time
            expression_attribute_values[":end_date"] = end_date_time
        elif start_date_time:
            filter_expression = "InitialEventTime >= :start_date"
            expression_attribute_values[":start_date"] = start_date_time
        elif end_date_time:
            filter_expression = "InitialEventTime <= :end_date"
            expression_attribute_values[":end_date"] = end_date_time

        response = self.client.scan(
            filter_expression=filter_expression,
            expression_attribute_values=(
                expression_attribute_values if expression_attribute_values else None
            ),
            limit=limit or 50,
            exclusive_start_key=exclusive_start_key,
        )

        # Convert items to Document objects
        documents = []
        for item in response.get("Items", []):
            try:
                documents.append(self._dynamodb_item_to_document(item))
            except Exception as e:
                logger.warning(f"Failed to convert item to document: {e}")

        return {
            "Documents": documents,
            "nextToken": response.get("LastEvaluatedKey"),
        }

    def _list_documents_via_gsi(
        self,
        start_date_time: Optional[str] = None,
        end_date_time: Optional[str] = None,
        limit: Optional[int] = None,
        exclusive_start_key: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """List documents using TypeDateIndex GSI for efficient querying."""
        import boto3
        from boto3.dynamodb.conditions import Key

        table = boto3.resource("dynamodb").Table(self.client.table_name)

        query_kwargs: Dict[str, Any] = {
            "IndexName": "TypeDateIndex",
            "Limit": limit or 50,
            "ScanIndexForward": False,
        }

        if start_date_time and end_date_time:
            query_kwargs["KeyConditionExpression"] = Key("ItemType").eq(
                "document"
            ) & Key("InitialEventTime").between(start_date_time, end_date_time)
        elif start_date_time:
            query_kwargs["KeyConditionExpression"] = Key("ItemType").eq(
                "document"
            ) & Key("InitialEventTime").gte(start_date_time)
        elif end_date_time:
            query_kwargs["KeyConditionExpression"] = Key("ItemType").eq(
                "document"
            ) & Key("InitialEventTime").lte(end_date_time)
        else:
            query_kwargs["KeyConditionExpression"] = Key("ItemType").eq("document")

        if exclusive_start_key:
            query_kwargs["ExclusiveStartKey"] = exclusive_start_key

        response = table.query(**query_kwargs)

        documents = []
        for item in response.get("Items", []):
            try:
                documents.append(self._dynamodb_item_to_document(item))
            except Exception as e:
                logger.warning(f"Failed to convert GSI item to document: {e}")

        return {
            "Documents": documents,
            "nextToken": response.get("LastEvaluatedKey"),
        }

    def list_documents_date_hour(
        self, date: Optional[str] = None, hour: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        List documents for a specific date and hour using the list partition.

        Args:
            date: Date in YYYY-MM-DD format (defaults to today)
            hour: Hour in 24-hour format 0-23 (defaults to current hour)

        Returns:
            Dict containing documents and pagination info

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        shards_in_day = 6
        shard_divider = 24 // shards_in_day

        # Use current time if not provided
        now = datetime.datetime.now()
        if date is None:
            date = now.strftime("%Y-%m-%d")
        if hour is None:
            hour = now.hour

        if hour < 0 or hour > 23:
            raise ValueError(
                "Invalid hour parameter - value should be between 0 and 23"
            )

        # Calculate shard
        hour_shard = hour // shard_divider
        shard_pad = f"{hour_shard:02d}"
        hour_pad = f"{hour:02d}"

        list_pk = f"list#{date}#s#{shard_pad}"
        sk_prefix = f"ts#{date}T{hour_pad}"

        response = self.client.query(
            key_condition_expression="PK = :pk AND begins_with(SK, :sk_prefix)",
            expression_attribute_values={
                ":pk": list_pk,
                ":sk_prefix": sk_prefix,
            },
        )

        return {
            "Documents": response.get("Items", []),
            "nextToken": response.get("LastEvaluatedKey"),
        }

    def list_documents_date_shard(
        self, date: Optional[str] = None, shard: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        List documents for a specific date and shard using the list partition.

        Args:
            date: Date in YYYY-MM-DD format (defaults to today)
            shard: Shard number (defaults to current shard)

        Returns:
            Dict containing documents and pagination info

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        shards_in_day = 6
        shard_divider = 24 // shards_in_day

        # Use current time if not provided
        now = datetime.datetime.now()
        if date is None:
            date = now.strftime("%Y-%m-%d")
        if shard is None:
            shard = now.hour // shard_divider

        if shard >= shards_in_day or shard < 0:
            raise ValueError(
                f"Invalid shard parameter value - must be positive and less than {shards_in_day}"
            )

        shard_pad = f"{shard:02d}"
        list_pk = f"list#{date}#s#{shard_pad}"

        response = self.client.query(
            key_condition_expression="PK = :pk",
            expression_attribute_values={
                ":pk": list_pk,
            },
        )

        return {
            "Documents": response.get("Items", []),
            "nextToken": response.get("LastEvaluatedKey"),
        }

    def update_document_status(
        self,
        document_id: str,
        status: Status,
        workflow_execution_arn: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update only the status of a document (lightweight operation).

        This method performs a minimal update that only touches the ObjectStatus field,
        reducing DynamoDB WCU consumption from ~100KB to ~500 bytes. Use this during
        parallel Map operations where multiple Lambda functions update status concurrently.

        Args:
            document_id: The ObjectKey of the document to update
            status: The new Status to set
            workflow_execution_arn: Optional workflow execution ARN

        Returns:
            Dictionary with the updated document attributes

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        key = {
            "PK": f"doc#{document_id}",
            "SK": "none",
        }

        # Derive workflow status from document status
        if status == Status.FAILED:
            workflow_status = "FAILED"
        elif status == Status.COMPLETED:
            workflow_status = "SUCCEEDED"
        elif status == Status.ABORTED:
            workflow_status = "ABORTED"
        else:
            workflow_status = "RUNNING"

        # Build minimal update expression
        set_expressions = [
            "#ObjectStatus = :ObjectStatus",
            "#WorkflowStatus = :WorkflowStatus",
        ]
        expression_names = {
            "#ObjectStatus": "ObjectStatus",
            "#WorkflowStatus": "WorkflowStatus",
        }
        expression_values = {
            ":ObjectStatus": status.value,
            ":WorkflowStatus": workflow_status,
        }

        if workflow_execution_arn:
            set_expressions.append("#WorkflowExecutionArn = :WorkflowExecutionArn")
            expression_names["#WorkflowExecutionArn"] = "WorkflowExecutionArn"
            expression_values[":WorkflowExecutionArn"] = workflow_execution_arn

        update_expression = "SET " + ", ".join(set_expressions)

        response = self.client.update_item(
            key=key,
            update_expression=update_expression,
            expression_attribute_names=expression_names,
            expression_attribute_values=expression_values,
            return_values="ALL_NEW",
        )

        logger.info(f"Updated document status: {document_id} -> {status.value}")
        return response.get("Attributes", {})

    def update_document_section(
        self,
        document_id: str,
        section_index: int,
        section: Section,
    ) -> Dict[str, Any]:
        """
        Update a single section in a document (atomic section-level update).

        This method performs an atomic update of a single section using DynamoDB's
        SET Sections[index] = :value expression. This reduces WCU consumption from
        ~100KB to ~5KB per update and avoids read-modify-write race conditions
        during parallel Map operations.

        Args:
            document_id: The ObjectKey of the document to update
            section_index: The index position of the section in the Sections array
            section: The Section object with updated data

        Returns:
            Dictionary with the updated document attributes

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        key = {
            "PK": f"doc#{document_id}",
            "SK": "none",
        }

        # Convert page IDs to integers for DynamoDB
        page_ids = []
        for page_id in section.page_ids:
            try:
                page_ids.append(int(page_id))
            except ValueError:
                logger.warning(
                    f"Skipping page ID {page_id} in section {section.section_id} - not an integer"
                )

        section_data: Dict[str, Any] = {
            "Id": section.section_id,
            "PageIds": page_ids,
            "Class": section.classification,
            "OutputJSONUri": section.extraction_result_uri or "",
        }

        # Confidence in the section's CLASS, written here for the same reason the
        # exclusion flags below are: this is a whole-map replace, so omitting a key
        # classification already persisted ERASES it. Extraction calls this writer
        # per section, which would otherwise drop the class confidence moments
        # after it was stored.
        if section.confidence is not None:
            section_data["Confidence"] = Decimal(str(float(section.confidence)))

        # Convert confidence threshold alerts
        if section.confidence_threshold_alerts:
            section_data["ConfidenceThresholdAlerts"] = (
                serialize_confidence_threshold_alerts(section)
            )

        # Persist structured processing issues. This write REPLACES the whole
        # section map (`SET #Sections[i] = :section`), so omitting them here does
        # not merely skip them — it ERASES any issues an earlier stage wrote.
        # Extraction persists through this path for immediate UI visibility
        # (patterns/unified/src/extraction_function/index.py:367), so without
        # this the section status icon stayed blank until the collate step
        # rewrote the full document.
        #
        # The truthiness guard is what CLEARS the attribute when a re-run resolved
        # every issue: the caller already replaced its own stage's issues (see
        # ExtractionService._save_results), so an empty list here means "nothing to
        # report", and a full-map replace with the key omitted deletes the stale
        # value. Do not "fix" this into an unconditional write of `[]`.
        if section.processing_issues:
            section_data["ProcessingIssues"] = serialize_processing_issues(section)

        if section.instance_count:
            section_data["InstanceCount"] = section.instance_count

        # Exclusion flags, written for the same reason ProcessingIssues are above:
        # classification sets them, then extraction calls THIS writer for every
        # section including the excluded ones it short-circuited
        # (patterns/unified/src/extraction_function/index.py:367). Because this is
        # a whole-map replace, omitting the keys would erase the flags
        # classification had already persisted, and the "Skipped" badge would
        # disappear moments after appearing.
        if section.excluded:
            section_data["Excluded"] = True
            if section.exclusion_reason:
                section_data["ExclusionReason"] = section.exclusion_reason

        # Use SET Sections[index] = :value for atomic section update
        update_expression = f"SET #Sections[{section_index}] = :section"
        expression_names = {"#Sections": "Sections"}
        expression_values = {":section": section_data}

        response = self.client.update_item(
            key=key,
            update_expression=update_expression,
            expression_attribute_names=expression_names,
            expression_attribute_values=expression_values,
            return_values="ALL_NEW",
        )

        logger.info(
            f"Updated section {section_index} ({section.section_id}) for document: {document_id}"
        )
        return response.get("Attributes", {})

    # ------------------------------------------------------------------ #
    # Document runs (versions)
    #
    # Each successful processing run is recorded as an immutable item under
    # the document's partition: PK = doc#<key>, SK = run#<run_id>. The run_id
    # is timestamp-prefixed (see idp_common.document_versions.build_run_id)
    # so a SK-descending query returns newest-first.
    #
    # Run items intentionally carry RecordType="run" and NO ItemType /
    # InitialEventTime attributes: the TypeDateIndex GSI keys on
    # (ItemType, InitialEventTime), so omitting ItemType keeps run items out
    # of the GSI entirely — no index schema change, no re-hydration, no GSI
    # write amplification. Likewise the VersionCount counter maintained on
    # the doc item is NOT in the GSI's INCLUDE projection, so adding it never
    # touches the index definition.
    # ------------------------------------------------------------------ #

    def create_document_run(
        self,
        document: Document,
        run_id: str,
        manifest_uri: str,
        file_count: int = 0,
        expires_after: Optional[int] = None,
    ) -> str:
        """
        Record an immutable run (version) item for a completed document and
        increment the document's VersionCount.

        Args:
            document: The completed Document (post-processing state)
            run_id: Run identifier (timestamp-prefixed, unique per execution)
            manifest_uri: S3 URI of the run's output-version manifest
            file_count: Number of output objects pinned by the manifest
            expires_after: Optional TTL timestamp (should match the doc item's)

        Returns:
            The run_id of the created run item
        """
        item: Dict[str, Any] = {
            "PK": f"doc#{document.input_key}",
            "SK": f"run#{run_id}",
            "RecordType": "run",
            "RunId": run_id,
            "ObjectKey": document.input_key,
            "ManifestUri": manifest_uri,
            "FileCount": file_count,
        }

        # Snapshot of the run's metadata (mirrors the doc item's attributes so
        # the UI can render a prior version with the same code paths).
        if document.completion_time:
            item["CompletionTime"] = document.completion_time
        if document.queued_time:
            item["QueuedTime"] = document.queued_time
        if document.start_time:
            item["WorkflowStartTime"] = document.start_time
        if document.initial_event_time:
            # NB: attribute is safe on run items because ItemType is absent;
            # both GSI keys are required for an item to be indexed.
            item["RunInitialEventTime"] = document.initial_event_time
        if document.workflow_execution_arn:
            item["WorkflowExecutionArn"] = document.workflow_execution_arn
        if document.config_version:
            item["ConfigVersion"] = document.config_version
        if document.config_revision is not None:
            item["ConfigRevision"] = int(document.config_revision)
        if document.num_pages > 0:
            item["PageCount"] = document.num_pages
        if document.metering:
            item["Metering"] = json.dumps(document.metering, default=str)
        if document.summary_report_uri:
            item["SummaryReportUri"] = document.summary_report_uri
        if document.evaluation_report_uri:
            item["EvaluationReportUri"] = document.evaluation_report_uri
        if (
            document.rule_validation_result
            and document.rule_validation_result.output_uri
        ):
            item["RuleValidationResultUri"] = document.rule_validation_result.output_uri

        if document.sections:
            sections_data = []
            for section in document.sections:
                page_ids = []
                for page_id in section.page_ids:
                    try:
                        page_ids.append(int(page_id))
                    except ValueError:
                        continue
                section_data: Dict[str, Any] = {
                    "Id": section.section_id,
                    "PageIds": page_ids,
                    "Class": section.classification,
                    "OutputJSONUri": section.extraction_result_uri or "",
                }
                # Snapshot the per-section quality data alongside the structure,
                # exactly as update_document does for the live doc item. Without
                # these, a historical version renders "Low Confidence Fields: 0"
                # and an empty Status for every section, because the UI derives
                # both from these attributes (there is no other source once the
                # run's outputs have been overwritten).
                if section.confidence is not None:
                    section_data["Confidence"] = Decimal(str(float(section.confidence)))
                if section.confidence_threshold_alerts:
                    section_data["ConfidenceThresholdAlerts"] = (
                        serialize_confidence_threshold_alerts(section)
                    )
                if section.processing_issues:
                    section_data["ProcessingIssues"] = serialize_processing_issues(
                        section
                    )
                if section.instance_count:
                    section_data["InstanceCount"] = section.instance_count
                # Snapshot the exclusion flags too, so a historical version keeps
                # the "Skipped" badge instead of showing an unexplained empty
                # section.
                if section.excluded:
                    section_data["Excluded"] = True
                    if section.exclusion_reason:
                        section_data["ExclusionReason"] = section.exclusion_reason
                sections_data.append(section_data)
            item["Sections"] = sections_data

        if document.pages:
            pages_data = []
            for page_id, page in document.pages.items():
                try:
                    page_id_int = int(page_id)
                except ValueError:
                    continue
                page_snapshot: Dict[str, Any] = {
                    "Id": page_id_int,
                    "Class": page.classification or "",
                    "ImageUri": page.image_uri or "",
                    "TextUri": page.parsed_text_uri or page.raw_text_uri or "",
                    "OcrPageDataUri": page.ocr_page_data_uri or "",
                }
                # Snapshot the classification signals too (confidence, reason,
                # boundary), so a historical run can be audited for why its pages
                # were classified — and its sections split — the way they were.
                page_snapshot.update(serialize_page_classification_signals(page))
                pages_data.append(page_snapshot)
            if pages_data:
                item["Pages"] = pages_data

        if expires_after:
            item["ExpiresAfter"] = expires_after

        item = convert_floats_to_decimal(item)  # type: ignore[assignment]
        # Idempotent create: EventBridge delivers Step Functions events
        # at-least-once, so the same (stable) run_id can arrive twice. Only the
        # first write should land — and only it should bump VersionCount — so a
        # redelivery does not create a phantom duplicate version or over-count.
        try:
            self.client.put_item(item, condition_expression="attribute_not_exists(PK)")
        except Exception as e:
            error_code = getattr(e, "error_code", None)
            if error_code == "ConditionalCheckFailedException":
                logger.info(
                    f"Run {run_id} already recorded for {document.input_key}; "
                    "skipping duplicate (at-least-once redelivery)"
                )
                return run_id
            raise

        # Maintain a version counter on the doc item (detail-page display).
        # Not projected into the TypeDateIndex GSI, so this is a plain
        # attribute update with no index implications.
        try:
            self.client.update_item(
                key={"PK": f"doc#{document.input_key}", "SK": "none"},
                update_expression="ADD #VersionCount :one",
                expression_attribute_names={"#VersionCount": "VersionCount"},
                expression_attribute_values={":one": 1},
                return_values="NONE",
            )
        except Exception as e:
            logger.warning(
                f"Failed to increment VersionCount for {document.input_key}: {e}"
            )

        logger.info(
            f"Created run record for {document.input_key}: run_id={run_id}, "
            f"{file_count} files pinned"
        )
        return run_id

    def list_document_runs(self, object_key: str) -> List[Dict[str, Any]]:
        """
        List all run (version) items for a document, newest first.

        Returns:
            List of run items (native Python types)
        """
        runs: List[Dict[str, Any]] = []
        exclusive_start_key = None
        while True:
            kwargs: Dict[str, Any] = {
                "key_condition_expression": "PK = :pk AND begins_with(SK, :run)",
                "expression_attribute_values": {
                    ":pk": f"doc#{object_key}",
                    ":run": "run#",
                },
            }
            if exclusive_start_key:
                kwargs["exclusive_start_key"] = exclusive_start_key
            response = self.client.query(**kwargs)
            runs.extend(response.get("Items", []))
            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break
        # run_id is timestamp-prefixed, so SK order is chronological.
        runs.sort(key=lambda r: r.get("SK", ""), reverse=True)
        return convert_decimals_to_native(runs)  # type: ignore[return-value]

    def get_document_run(
        self, object_key: str, run_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get a single run (version) item for a document."""
        item = self.client.get_item({"PK": f"doc#{object_key}", "SK": f"run#{run_id}"})
        return convert_decimals_to_native(item) if item else None  # type: ignore[return-value]

    def delete_document_run(self, object_key: str, run_id: str) -> bool:
        """
        Delete a run (version) item and decrement the doc's VersionCount.

        S3 artifact cleanup (the pinned object versions and the manifest) is
        the caller's responsibility — see
        idp_common.document_versions.delete_run_artifacts.
        """
        self.client.delete_item({"PK": f"doc#{object_key}", "SK": f"run#{run_id}"})
        try:
            self.client.update_item(
                key={"PK": f"doc#{object_key}", "SK": "none"},
                update_expression="ADD #VersionCount :neg",
                expression_attribute_names={"#VersionCount": "VersionCount"},
                expression_attribute_values={":neg": -1},
                return_values="NONE",
            )
        except Exception as e:
            logger.warning(f"Failed to decrement VersionCount for {object_key}: {e}")
        logger.info(f"Deleted run record for {object_key}: run_id={run_id}")
        return True

    def calculate_ttl(self, days: int = 30) -> int:
        """
        Calculate a TTL timestamp for document expiration.

        Args:
            days: Number of days until expiration

        Returns:
            Unix timestamp (seconds since epoch) for the expiration date
        """
        expiration_date = datetime.datetime.now() + datetime.timedelta(days=days)
        return int(expiration_date.timestamp())
