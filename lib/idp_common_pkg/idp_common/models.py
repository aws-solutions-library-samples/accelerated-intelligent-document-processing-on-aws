# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Simplified document data model for IDP processing.

This module defines the Document class that represents the state of a document
as it moves through the processing pipeline.
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


def coerce_revision(value: Any) -> Optional[int]:
    """
    Normalize a configuration revision number from any transport.

    The same value arrives as a str (S3 object metadata), a Decimal (DynamoDB), or
    an int (JSON), and an unparseable value must degrade to None — "no pinned
    revision, use the profile's current configuration" — rather than raising in the
    middle of a document's plumbing.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Status(Enum):
    """Document processing status."""

    PENDING_UPLOAD = "PENDING_UPLOAD"  # Pre-s3 upload state only applicable for documents uploaded via /Jobs API
    IN_PROGRESS = "IN_PROGRESS"  # Batch job file is being processed
    QUEUED = "QUEUED"  # Initial state when document is added to queue
    RUNNING = "RUNNING"  # Step function workflow has started
    PREPROCESSING = "PREPROCESSING"  # Preprocessing hook (e.g. PII redaction)
    OCR = "OCR"  # OCR processing
    CLASSIFYING = "CLASSIFYING"  # Document classification
    EXTRACTING = "EXTRACTING"  # Information extraction
    ASSESSING = "ASSESSING"  # Document assessment
    POSTPROCESSING = "POSTPROCESSING"  # Document summarization
    HITL_IN_PROGRESS = "HITL_IN_PROGRESS"  # Human-in-the-loop review in progress
    SUMMARIZING = "SUMMARIZING"  # Document summarization
    RULE_VALIDATION_POLICY_CLASSIFICATION = "RULE_VALIDATION_POLICY_CLASSIFICATION"  # Policy classification for rule validation
    RULE_VALIDATION = "RULE_VALIDATION"  # Rule validation processing
    RULE_VALIDATION_ORCHESTRATOR = "RULE_VALIDATION_ORCHESTRATOR"  # Rule validation orchestration and consolidation
    EVALUATING = "EVALUATING"  # Document evaluation
    COMPLETED = "COMPLETED"  # All processing completed
    FAILED = "FAILED"  # Processing failedy
    ABORTED = "ABORTED"  # User cancelled workflow
    REDACTED_SUPERSEDED = "REDACTED_SUPERSEDED"  # A preprocessing hook (e.g. PII
    # anonymization) produced a redacted copy that is processed as a separate
    # document; this original was intentionally not processed further.


@dataclass
class Page:
    """Represents a single page in a document."""

    page_id: str
    image_uri: Optional[str] = None
    raw_text_uri: Optional[str] = None
    parsed_text_uri: Optional[str] = None
    text_confidence_uri: Optional[str] = None
    ocr_page_data_uri: Optional[str] = None
    classification: Optional[str] = None
    confidence: Optional[float] = None
    """Confidence in this page's ``classification``, in ``[0.0, 1.0]``.

    ``None`` means NOT SCORED — no confidence signal exists for this page. That
    is the normal state: the classification prompt has to ask for a score and
    the model has to return one (see `classification.confidence` and GitHub
    #673), and several code paths never produce one at all (the SageMaker
    backend returns no score; pages beyond `maxPagesForClassification` have
    their class extrapolated rather than predicted).

    Distinguishing "not scored" from a number matters because this value reaches
    the reporting lake as `section_confidence` once aggregated: the field used
    to default to a literal ``1.0``/``0.0``, which fed a fabricated *certainty*
    into a column named confidence. ``1.0`` is now reserved for the paths that
    genuinely assert the class deterministically (a document-name regex match, a
    single-class configuration, a page-content regex match) and ``0.0`` for a
    page whose classification errored.

    Not serialized when ``None``, so pages that were never scored round-trip
    unchanged."""
    tables: List[Dict[str, Any]] = field(default_factory=list)
    forms: Dict[str, str] = field(default_factory=dict)

    classification_reason: Optional[str] = None
    """The model's stated justification for this page's ``classification``.

    The default classification prompt has always asked for a
    `classification_reason` ("Detailed reasoning including specific visual and
    textual evidence that led to this classification") — those output tokens
    were paid for on every page and then dropped on the floor, because nothing
    parsed the key back out. Persisting it makes a surprising classification
    explainable after the fact, which is the same argument that applies to
    ``document_boundary`` below.

    ``None`` when the model returned no reason (or a custom prompt does not ask
    for one), and not serialized in that case."""

    classification_candidates: Optional[List[Dict[str, Any]]] = None
    """The classifier's ranked alternative classes for this page, most likely
    first: ``[{"class": "w2", "probability": 0.8}, {"class": "1099",
    "probability": 0.15}]``.

    Produced by ``classification.confidence.mode: topk``, which asks the model to
    enumerate and rank candidates rather than self-report one number — that is
    both better calibrated and the direct answer to "what else could this page
    have been?" (the question a reviewer looking at a suspicious classification
    actually has). ``None`` when nothing asked for them, and not serialized in
    that case."""

    document_boundary: Optional[str] = None
    """The classifier's per-page boundary signal: ``"start"`` (this page begins a
    new document) or ``"continue"``. ``None`` means no signal was produced — the
    model omitted it, or the code path never asked for one.

    This is what ``sectionSplitting: llm_determined`` splits on, and it used to
    be discarded immediately after use. That made an unexpected section merge
    effectively un-auditable: section spans alone cannot distinguish "the model
    said continue" from "the model said nothing" from "the code never asked",
    and diagnosing GitHub #565 required re-deriving it from Lambda logs.
    Persisting it keeps the decision inspectable after the fact.

    ``None`` is not serialized, so documents written by older code — and page
    records that never had a boundary — round-trip unchanged."""


@dataclass
class ProcessingIssue:
    """A structured, user-surfacing record of something that went wrong (or was
    auto-healed) during processing.

    Replaces the scattered signals (``split_stats`` / ``parsing_succeeded`` /
    free-text ``document.errors``) with ONE concept that the backend produces,
    DynamoDB persists, GraphQL exposes, and the UI renders as a section status
    icon + tooltip. An issue never fails the document — it flags it.

    Fields:
        stage: Where it arose — ``"extraction"`` | ``"assessment"`` | ``"ocr"`` …
        severity: ``"error"`` (incomplete/failed), ``"warning"`` (partial /
            degraded), or ``"info"`` (auto-recovered but worth noting).
        code: Stable machine code, e.g. ``"assessment_incomplete"``,
            ``"assessment_recovered_with_retries"``,
            ``"assessment_deadline_reached"``, ``"extraction_incomplete"``.
        message: User-friendly one-liner.
        root_cause: Technical detail — model, output cap, rows affected, geometry
            mode, escalation chain tried.
        section_id: The section this pertains to (None for document-level).
        details: Structured payload (split_stats, unrecoverable indices, model
            chain) for the processing report / debugging.
    """

    stage: str
    severity: str
    code: str
    message: str
    root_cause: str = ""
    section_id: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a compact dict (omitting empty optional fields)."""
        result: Dict[str, Any] = {
            "stage": self.stage,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.root_cause:
            result["root_cause"] = self.root_cause
        if self.section_id is not None:
            result["section_id"] = self.section_id
        if self.details:
            result["details"] = self.details
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProcessingIssue":
        """Create a ProcessingIssue from its dict representation."""
        return cls(
            stage=data.get("stage", ""),
            severity=data.get("severity", "info"),
            code=data.get("code", ""),
            message=data.get("message", ""),
            root_cause=data.get("root_cause", ""),
            section_id=data.get("section_id"),
            details=data.get("details", {}) or {},
        )


@dataclass
class Section:
    """Represents a section of pages with the same classification."""

    section_id: str
    classification: str
    confidence: Optional[float] = None
    """Confidence in this section's ``classification``, in ``[0.0, 1.0]``.

    Aggregated from the section's pages by
    ``ClassificationService._aggregate_page_confidence`` — the MINIMUM across
    them, and ``None`` if any page is unscored. A mean would hide the one page
    the classifier was unsure about, which is the page a reviewer needs to see;
    and a section cannot be scored more confidently than its weakest page.

    ``None`` means NOT SCORED (see :attr:`Page.confidence`). This reaches the
    reporting lake as the `section_confidence` column, so a fabricated ``1.0``
    there is worse than a null.

    Note this is the confidence in the section's CLASS, unrelated to
    ``confidence_threshold_alerts`` below, which are per-extracted-field."""
    page_ids: List[str] = field(default_factory=list)
    extraction_result_uri: Optional[str] = None
    attributes: Optional[Dict[str, Any]] = None
    confidence_threshold_alerts: List[Dict[str, Any]] = field(default_factory=list)
    processing_issues: List["ProcessingIssue"] = field(default_factory=list)
    """Structured issues detected for this section (assessment incomplete,
    recovered-with-retries, extraction incomplete, …). Rolled up to
    ``Document.processing_issues`` and surfaced in the UI."""

    # Exclusion flags (populated by ClassificationService from class config)
    excluded: bool = False
    """True if the classification of this section is marked for exclusion
    (e.g., static instruction or legal pages). Downstream services skip
    excluded sections."""

    exclusion_reason: Optional[str] = None
    """Optional category (e.g., "instructions", "legal") carried over from
    the class configuration for UI/report display."""

    instance_count: int = 0
    """How many separate documents (instances) of this section's class the
    extraction found in this section.

    ``0`` means "not determined" — the default, so sections written by older
    code (or whose extraction failed before producing a result) read back
    unchanged. ``1`` is the normal case. ``> 1`` means the section spans
    several distinct documents of the same class, which classification did not
    split apart; the UI surfaces this so a multi-document section is visible at
    a glance instead of silently collapsing to its first record.

    Populated by ``ExtractionService`` from whichever of these applies:
    the length of a class's declared instance array, or the number of records
    recovered when the model returned a JSON array for a single-object schema.
    """

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Section":
        """Create a Section from a dictionary representation."""
        if not data:
            raise ValueError("Cannot create Section from empty data")

        return cls(
            section_id=data.get("section_id", ""),
            classification=data.get("classification", ""),
            # Absent => not scored (None), NOT a presumed 1.0.
            confidence=data.get("confidence"),
            page_ids=data.get("page_ids", []),
            extraction_result_uri=data.get("extraction_result_uri"),
            attributes=data.get("attributes"),
            confidence_threshold_alerts=data.get("confidence_threshold_alerts", []),
            processing_issues=[
                ProcessingIssue.from_dict(pi)
                for pi in data.get("processing_issues", [])
                if isinstance(pi, dict)
            ],
            excluded=bool(data.get("excluded", False)),
            exclusion_reason=data.get("exclusion_reason"),
            instance_count=int(data.get("instance_count") or 0),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert section to dictionary representation."""
        result: Dict[str, Any] = {
            "section_id": self.section_id,
            "classification": self.classification,
            "page_ids": self.page_ids,
            "extraction_result_uri": self.extraction_result_uri,
            "attributes": self.attributes,
            "confidence_threshold_alerts": self.confidence_threshold_alerts,
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
        }
        # Omitted when not scored, so an unscored section reads back as None
        # rather than as a presumed 1.0 (see the `confidence` docstring).
        if self.confidence is not None:
            result["confidence"] = self.confidence
        # Only emit when non-empty to keep output compact / back-compatible.
        if self.processing_issues:
            result["processing_issues"] = [
                pi.to_dict() for pi in self.processing_issues
            ]
        if self.instance_count:
            result["instance_count"] = self.instance_count
        return result


@dataclass
class RuleValidationResult:
    """Result of criteria validation for a document request."""

    request_id: str
    summary: Optional[Dict[str, Any]] = None
    section_results: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None
    output_uri: Optional[str] = None
    errors: Optional[List[str]] = None
    matched_policy_types: Optional[List[str]] = None
    matched_page_ids: Optional[Dict[str, str]] = None

    @classmethod
    def for_section(
        cls,
        document_id: str,
        section_uri: str,
        timing_metrics: Dict = None,
        chunking_occurred: bool = False,
        chunks_created: int = 0,
    ):
        """Create result for single section processing."""
        section_data = {
            "section_uri": section_uri,
            "timing": timing_metrics or {},
            "chunking_occurred": chunking_occurred,
            "chunks_created": chunks_created,
        }
        return cls(
            request_id=document_id,
            section_results=[section_data],
            metadata={
                "sections_processed": 1,
                "section_output_uri": section_uri,
                "chunking_occurred": chunking_occurred,
                "chunks_created": chunks_created,
            },
            output_uri=section_uri,
        )

    @classmethod
    def for_consolidation(
        cls,
        document_id: str,
        policy_type_uris: List[str],
        summary_uri: str,
        sections_processed: int = 0,
        section_results: Optional[List[Dict[str, Any]]] = None,
    ):
        """Create result for consolidation processing."""
        return cls(
            request_id=document_id,
            summary={
                "policy_type_uris": policy_type_uris,
                "consolidated_summary_uri": summary_uri,
            },
            section_results=section_results,  # Preserve section results from Map state
            metadata={
                "processing_type": "consolidation",
                "sections_processed": sections_processed,
            },
            output_uri=summary_uri,
        )


@dataclass
class HitlMetadata:
    """Represents HITL (Human-In-The-Loop) metadata for a document."""

    execution_id: Optional[str] = None
    record_number: Optional[int] = None
    bp_match: Optional[bool] = None
    extraction_bp_name: Optional[str] = None
    hitl_bp_change: Optional[str] = None
    hitl_triggered: bool = False
    page_array: List[str] = field(default_factory=list)
    review_portal_url: Optional[str] = None  # Added field for review portal URL
    hitl_completed: Optional[bool] = (
        None  # None = unknown, True = completed, False = in progress
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert HITL metadata to dictionary representation."""
        return {
            "execution_id": self.execution_id,
            "record_number": self.record_number,
            "bp_match": self.bp_match,
            "extraction_bp_name": self.extraction_bp_name,
            "hitl_bp_change": self.hitl_bp_change,
            "hitl_triggered": self.hitl_triggered,
            "page_array": self.page_array,
            "review_portal_url": self.review_portal_url,  # Include review portal URL in dict
            "hitl_completed": self.hitl_completed,  # Include completion status in dict
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HitlMetadata":
        """Create a HitlMetadata from a dictionary representation."""
        if not data:
            return cls()

        return cls(
            execution_id=data.get("execution_id"),
            record_number=data.get("record_number"),
            bp_match=data.get("bp_match"),
            extraction_bp_name=data.get("extraction_bp_name"),
            hitl_bp_change=data.get("hitl_bp_change"),
            hitl_triggered=data.get("hitl_triggered", False),
            review_portal_url=data.get(
                "review_portal_url"
            ),  # Fix: Include review portal URL
            hitl_completed=data.get("hitl_completed"),  # None if not set
            page_array=data.get("page_array", []),
        )


@dataclass
class Document:
    """
    Core document type that is passed through the processing pipeline.
    Each processing step enriches this object.

    The Document class provides comprehensive support for handling large documents
    in Step Functions workflows through automatic compression and decompression.

    Key Features:
    - Automatic compression for documents exceeding size thresholds
    - Seamless handling of compressed and uncompressed document data
    - Utility methods for Lambda function input/output processing
    - Preservation of section IDs for Step Functions Map operations

    Compression Methods:
    - compress(): Store full document in S3 and return lightweight wrapper
    - decompress(): Restore full document from S3 using compressed wrapper
    - from_compressed_or_dict(): Handle both compressed and regular document data

    Utility Methods:
    - load_document(): Process document input from Lambda events
    - serialize_document(): Prepare document output with automatic compression

    Usage Examples:
        # Handle input in Lambda functions
        document = Document.load_document(event_data, working_bucket, logger)

        # Prepare output with automatic compression
        response = {"document": document.serialize_document(working_bucket, "step_name", logger)}

        # Manual compression/decompression
        compressed_data = document.compress(working_bucket, "processing")
        restored_document = Document.decompress(working_bucket, compressed_data)
    """

    # Core identifiers
    id: Optional[str] = None  # Generated document ID
    input_bucket: Optional[str] = None  # S3 bucket containing the input document
    input_key: Optional[str] = None  # S3 key of the input document
    output_bucket: Optional[str] = None  # S3 bucket for processing outputs

    # Processing state and timing
    status: Status = Status.QUEUED
    initial_event_time: Optional[str] = None
    queued_time: Optional[str] = None
    start_time: Optional[str] = None
    completion_time: Optional[str] = None
    workflow_execution_arn: Optional[str] = None

    # Document content details
    num_pages: int = 0
    pages: Dict[str, Page] = field(default_factory=dict)
    sections: List[Section] = field(default_factory=list)
    summary_report_uri: Optional[str] = None

    # Processing metadata
    metering: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    trace_id: Optional[str] = None
    config_version: Optional[str] = None  # Configuration Profile to process under
    # Revision of that profile, pinned at queue time. Kept as its own field rather
    # than folded into config_version because config_version is equality-compared
    # against allowedConfigVersions in the RBAC scope checks — a composite value
    # there would either bypass or break scope.
    config_revision: Optional[int] = None
    submission_source: Optional[str] = None
    test_set_id: Optional[str] = None
    # Class every page must be treated as, instead of classifying. Set when a
    # reviewer corrects a misclassified document and asks for it to be
    # re-extracted: the point of that request is to run extraction under a
    # DIFFERENT class than the pipeline would choose, so leaving classification
    # to the model would re-derive the wrong one and discard the correction.
    forced_document_class: Optional[str] = None
    evaluation_status: Optional[str] = None
    evaluation_report_uri: Optional[str] = None
    evaluation_results_uri: Optional[str] = None
    rule_validation_result: Optional[RuleValidationResult] = None
    evaluation_result: Any = None  # Holds the DocumentEvaluationResult object
    summarization_result: Any = None  # Holds the DocumentSummarizationResult object
    errors: List[str] = field(default_factory=list)

    # HITL metadata
    hitl_metadata: List[HitlMetadata] = field(default_factory=list)
    hitl_status: Optional[str] = None  # PendingReview, InProgress, Completed, Skipped
    hitl_triggered: bool = False  # Whether HITL review was triggered for this document
    hitl_sections_pending: List[str] = field(default_factory=list)
    hitl_sections_completed: List[str] = field(default_factory=list)

    # Confidence alerts (top-level count for GSI projection)
    confidence_alert_count: int = 0

    # Processing issues rolled up from sections (+ any document-level issues).
    # Kept as a top-level count for cheap GSI projection / list filtering,
    # mirroring confidence_alert_count. The full issue objects live on each
    # Section (and are also listed here for document-level convenience).
    processing_issues: List["ProcessingIssue"] = field(default_factory=list)

    @property
    def all_processing_issues(self) -> List["ProcessingIssue"]:
        """All issues across sections + any document-level ones (deduped by
        identity — sections own their issues; document-level issues have no
        section_id)."""
        issues: List["ProcessingIssue"] = list(self.processing_issues)
        for section in self.sections:
            issues.extend(section.processing_issues)
        return issues

    @property
    def processing_issue_count(self) -> int:
        """Total number of processing issues (sections + document-level)."""
        return len(self.all_processing_issues)

    @property
    def has_processing_issues(self) -> bool:
        """True if any processing issue was recorded (any severity)."""
        return self.processing_issue_count > 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert document to dictionary representation."""
        # First convert basic attributes
        result = {
            "id": self.id,
            "input_bucket": self.input_bucket,
            "input_key": self.input_key,
            "output_bucket": self.output_bucket,
            "status": self.status.value,
            "initial_event_time": self.initial_event_time,
            "queued_time": self.queued_time,
            "start_time": self.start_time,
            "completion_time": self.completion_time,
            "workflow_execution_arn": self.workflow_execution_arn,
            "num_pages": self.num_pages,
            "summary_report_uri": self.summary_report_uri,
            "evaluation_status": self.evaluation_status,
            "evaluation_report_uri": self.evaluation_report_uri,
            "evaluation_results_uri": self.evaluation_results_uri,
            "errors": self.errors,
            "metering": self.metering,
            "trace_id": self.trace_id,
            "config_version": self.config_version,
            "config_revision": self.config_revision,
            "submission_source": self.submission_source,
            "test_set_id": self.test_set_id,
            # Carried across step boundaries: set before OCR, read at
            # classification. Dropping it here would silently re-enable
            # classification and lose the reviewer's correction.
            "forced_document_class": self.forced_document_class,
            "confidence_alert_count": self.confidence_alert_count,
            # We don't include evaluation_result or summarization_result in the dict since they're objects
        }

        # Processing-issue rollup (top-level count for GSI / list filtering).
        # Only emit when non-zero to keep output compact / back-compatible.
        issue_count = self.processing_issue_count
        if issue_count:
            result["processing_issue_count"] = issue_count
        if self.processing_issues:
            result["processing_issues"] = [
                pi.to_dict() for pi in self.processing_issues
            ]

        # Free-form per-document metadata. Written by producers that expect a
        # LATER stage to read it — classification stores
        # metadata["failed_page_exceptions"] there — so leaving it out of the
        # dict meant it never survived a Step Functions hop and the reader saw
        # nothing (GitHub #706).
        #
        # Round-tripped through json with default=str because callers also stash
        # live objects here (metadata["primary_exception"] holds an Exception
        # instance for in-process re-raise). serialize_document's uncompressed
        # branch returns to_dict() straight to Lambda, whose response serializer
        # has no default=str and would fail on such a value.
        #
        # Omitted when empty so payloads stay byte-identical to what older code
        # wrote.
        if self.metadata:
            result["metadata"] = json.loads(json.dumps(self.metadata, default=str))

        # Convert pages
        result["pages"] = {}
        for page_id, page in self.pages.items():
            result["pages"][page_id] = {
                "page_id": page.page_id,
                "image_uri": page.image_uri,
                "raw_text_uri": page.raw_text_uri,
                "parsed_text_uri": page.parsed_text_uri,
                "text_confidence_uri": page.text_confidence_uri,
                "ocr_page_data_uri": page.ocr_page_data_uri,
                "classification": page.classification,
                "tables": page.tables,
                "forms": page.forms,
            }
            # Confidence and reason are omitted when absent, on the same
            # convention as document_boundary below: a page nobody scored says
            # nothing about its confidence rather than claiming 0.0 or 1.0.
            if page.confidence is not None:
                result["pages"][page_id]["confidence"] = page.confidence
            if page.classification_reason:
                result["pages"][page_id]["classification_reason"] = (
                    page.classification_reason
                )
            if page.classification_candidates:
                result["pages"][page_id]["classification_candidates"] = (
                    page.classification_candidates
                )
            # Omitted when absent so payloads from before this existed — and
            # pages that genuinely produced no boundary signal — are unchanged.
            if page.document_boundary:
                result["pages"][page_id]["document_boundary"] = page.document_boundary

        # Convert sections
        result["sections"] = []
        for section in self.sections:
            section_dict = {
                "section_id": section.section_id,
                "classification": section.classification,
                "page_ids": section.page_ids,
                "extraction_result_uri": section.extraction_result_uri,
                "confidence_threshold_alerts": section.confidence_threshold_alerts,
            }
            # Same convention as the pages above: omitted when not scored.
            if section.confidence is not None:
                section_dict["confidence"] = section.confidence
            if section.attributes:
                section_dict["attributes"] = section.attributes
            if section.processing_issues:
                section_dict["processing_issues"] = [
                    pi.to_dict() for pi in section.processing_issues
                ]
            # Only emit exclusion fields when set to keep output compact
            # and backward-compatible with existing consumers.
            if section.excluded:
                section_dict["excluded"] = True
                if section.exclusion_reason:
                    section_dict["exclusion_reason"] = section.exclusion_reason
            # Same convention: omit when undetermined (0) so existing payloads
            # are byte-identical.
            if section.instance_count:
                section_dict["instance_count"] = section.instance_count
            result["sections"].append(section_dict)

        # Add rule_validation_result if present (optional)
        if self.rule_validation_result:
            result["rule_validation_result"] = {
                "request_id": self.rule_validation_result.request_id,
                "summary": self.rule_validation_result.summary,
                "section_results": self.rule_validation_result.section_results,
                "metadata": self.rule_validation_result.metadata,
                "output_uri": self.rule_validation_result.output_uri,
                "errors": self.rule_validation_result.errors,
                "matched_policy_types": self.rule_validation_result.matched_policy_types,
                "matched_page_ids": self.rule_validation_result.matched_page_ids,
            }

        # Add HITL metadata if it has any values
        if self.hitl_metadata:
            result["hitl_metadata"] = [
                metadata.to_dict() for metadata in self.hitl_metadata
            ]

        # Add Review Status fields
        if self.hitl_status:
            result["hitl_status"] = self.hitl_status
        if self.hitl_triggered:
            result["hitl_triggered"] = self.hitl_triggered
        if self.hitl_sections_pending:
            result["hitl_sections_pending"] = self.hitl_sections_pending
        if self.hitl_sections_completed:
            result["hitl_sections_completed"] = self.hitl_sections_completed

        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Document":
        """Create a Document from a dictionary representation."""
        document = cls(
            id=data.get("id", data.get("input_key")),
            input_bucket=data.get("input_bucket"),
            input_key=data.get("input_key"),
            output_bucket=data.get("output_bucket"),
            num_pages=int(data.get("num_pages", 0)),  # Ensure num_pages is integer
            initial_event_time=data.get("initial_event_time"),
            queued_time=data.get("queued_time"),
            start_time=data.get("start_time"),
            completion_time=data.get("completion_time"),
            workflow_execution_arn=data.get("workflow_execution_arn"),
            evaluation_status=data.get("evaluation_status"),
            evaluation_report_uri=data.get("evaluation_report_uri"),
            evaluation_results_uri=data.get("evaluation_results_uri"),
            summary_report_uri=data.get("summary_report_uri"),
            metering=data.get("metering", {}),
            # `or {}` so an absent key and an explicit null both read back as the
            # empty dict the field defaults to.
            metadata=data.get("metadata") or {},
            trace_id=data.get("trace_id"),
            config_version=data.get("config_version"),
            config_revision=coerce_revision(data.get("config_revision")),
            submission_source=data.get("submission_source"),
            test_set_id=data.get("test_set_id"),
            forced_document_class=data.get("forced_document_class"),
            errors=data.get("errors", []),
        )

        # Convert status from string to enum
        if "status" in data:
            try:
                document.status = Status(data["status"])
            except ValueError:
                # If the status isn't a valid enum value, use QUEUED as default
                document.status = Status.QUEUED

        # Convert pages
        pages_data = data.get("pages", {})
        for page_id, page_data in pages_data.items():
            document.pages[page_id] = Page(
                page_id=page_id,
                image_uri=page_data.get("image_uri"),
                raw_text_uri=page_data.get("raw_text_uri"),
                parsed_text_uri=page_data.get("parsed_text_uri"),
                text_confidence_uri=page_data.get("text_confidence_uri"),
                ocr_page_data_uri=page_data.get("ocr_page_data_uri"),
                classification=page_data.get("classification"),
                # Absent => not scored (None), NOT a presumed 0.0.
                confidence=page_data.get("confidence"),
                tables=page_data.get("tables", []),
                forms=page_data.get("forms", {}),
                classification_reason=page_data.get("classification_reason"),
                classification_candidates=page_data.get("classification_candidates"),
                document_boundary=page_data.get("document_boundary"),
            )

        # Convert sections
        sections_data = data.get("sections", [])
        for section_data in sections_data:
            document.sections.append(
                Section(
                    section_id=section_data.get("section_id"),
                    classification=section_data.get("classification"),
                    confidence=section_data.get("confidence"),
                    page_ids=section_data.get("page_ids", []),
                    extraction_result_uri=section_data.get("extraction_result_uri"),
                    attributes=section_data.get("attributes"),
                    confidence_threshold_alerts=section_data.get(
                        "confidence_threshold_alerts", []
                    ),
                    processing_issues=[
                        ProcessingIssue.from_dict(pi)
                        for pi in section_data.get("processing_issues", [])
                        if isinstance(pi, dict)
                    ],
                    excluded=bool(section_data.get("excluded", False)),
                    exclusion_reason=section_data.get("exclusion_reason"),
                    instance_count=int(section_data.get("instance_count") or 0),
                )
            )

        # Restore any document-level processing issues (section-level ones are
        # restored on each Section above).
        document.processing_issues = [
            ProcessingIssue.from_dict(pi)
            for pi in data.get("processing_issues", [])
            if isinstance(pi, dict)
        ]

        # Convert HITL metadata if present
        hitl_metadata_data = data.get("hitl_metadata", [])
        for metadata_item in hitl_metadata_data:
            document.hitl_metadata.append(HitlMetadata.from_dict(metadata_item))

        # Convert Review Status fields
        document.hitl_status = data.get("hitl_status")
        document.hitl_triggered = data.get("hitl_triggered", False)
        document.hitl_sections_pending = data.get("hitl_sections_pending", [])
        document.hitl_sections_completed = data.get("hitl_sections_completed", [])

        # Restore confidence alert count
        document.confidence_alert_count = int(data.get("confidence_alert_count", 0))

        # Convert rule_validation_result if present (optional)
        if "rule_validation_result" in data:
            rv_data = data["rule_validation_result"]
            document.rule_validation_result = RuleValidationResult(
                request_id=rv_data.get("request_id"),
                summary=rv_data.get("summary"),
                section_results=rv_data.get("section_results"),
                metadata=rv_data.get("metadata"),
                output_uri=rv_data.get("output_uri"),
                errors=rv_data.get("errors"),
                matched_policy_types=rv_data.get("matched_policy_types"),
                matched_page_ids=rv_data.get("matched_page_ids"),
            )

        return document

    @classmethod
    def from_s3_event(cls, event: Dict[str, Any], output_bucket: str) -> "Document":
        """Create a Document from an S3 event."""
        import logging

        logger = logging.getLogger(__name__)

        input_bucket = event.get("detail", {}).get("bucket", {}).get("name", "")
        input_key = event.get("detail", {}).get("object", {}).get("key", "")
        initial_event_time = event.get("time", "")

        # Read S3 metadata to get configuration version if available
        config_version = None
        config_revision = None
        submission_source = None
        test_set_id = None
        forced_document_class = None
        try:
            import boto3

            s3_client = boto3.client("s3")
            response = s3_client.head_object(Bucket=input_bucket, Key=input_key)
            metadata = response.get("Metadata", {})
            logger.info(f"S3 metadata for {input_key}: {metadata}")
            config_version = metadata.get("config-version")
            config_revision = coerce_revision(metadata.get("config-revision"))
            if config_version:
                logger.info(
                    f"Found config version in S3 metadata: {config_version}"
                    + (f" r{config_revision}" if config_revision else "")
                )
            else:
                logger.info(f"No config-version found in metadata for {input_key}")
            submission_source = metadata.get("submission-source")
            test_set_id = metadata.get("test-set-id")
            forced_document_class = metadata.get("document-class")
            if forced_document_class:
                logger.info(
                    f"Document {input_key} carries a forced class "
                    f"'{forced_document_class}'; classification will be skipped"
                )
            if submission_source:
                logger.info(
                    f"Document {input_key} submitted by {submission_source}"
                    + (f" for test set {test_set_id}" if test_set_id else "")
                )
        except Exception as e:
            logger.warning(f"Could not read S3 metadata for {input_key}: {e}")

        return cls(
            id=input_key,
            input_bucket=input_bucket,
            input_key=input_key,
            output_bucket=output_bucket,
            initial_event_time=initial_event_time,
            status=Status.QUEUED,
            config_version=config_version,  # Add config version to document
            config_revision=config_revision,
            submission_source=submission_source,
            test_set_id=test_set_id,
            forced_document_class=forced_document_class,
        )

    def to_json(self) -> str:
        """Convert document to JSON string."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_json(cls, json_str: str) -> "Document":
        """Create a Document from a JSON string."""
        data = json.loads(json_str)
        return cls.from_dict(data)

    @classmethod
    def from_s3(cls, bucket: str, input_key: str) -> "Document":
        """
        Create a Document from baseline results stored in S3.

        This method loads page and section result.json files from the specified
        S3 bucket with the given input_key prefix.

        Args:
            bucket: The S3 bucket containing baseline results
            input_key: The document key (used as prefix for finding baseline files)

        Returns:
            A Document instance populated with data from baseline files
        """
        import logging

        import boto3

        from idp_common.s3 import get_json_content
        from idp_common.utils import build_s3_uri

        logger = logging.getLogger(__name__)
        s3_client = boto3.client("s3")

        # Create a basic document structure
        document = cls(
            id=input_key,
            input_key=input_key,
            output_bucket=bucket,
            status=Status.COMPLETED,
        )

        # List all objects with the given prefix to find pages and sections
        prefix = f"{input_key}/"
        logger.info(f"Listing objects in {bucket} with prefix {prefix}")

        try:
            # List pages first
            pages_prefix = f"{prefix}pages/"
            paginator = s3_client.get_paginator("list_objects_v2")
            page_dirs = set()

            # Find all page directories
            for page in paginator.paginate(
                Bucket=bucket, Prefix=pages_prefix, Delimiter="/"
            ):
                for prefix_item in page.get("CommonPrefixes", []):
                    page_dir = prefix_item.get("Prefix")
                    page_id = page_dir.split("/")[-2]  # Extract page ID from path
                    page_dirs.add((page_id, page_dir))

            # Process each page directory
            for page_id, page_dir in page_dirs:
                result_key = f"{page_dir}result.json"

                try:
                    # Check if result.json exists
                    s3_client.head_object(Bucket=bucket, Key=result_key)

                    # Load page data from result.json
                    result_uri = build_s3_uri(bucket, result_key)
                    page_data = get_json_content(result_uri)

                    # Create image and raw text URIs
                    image_uri = build_s3_uri(bucket, f"{page_dir}image.jpg")
                    raw_text_uri = build_s3_uri(bucket, f"{page_dir}rawText.json")

                    # Add page to document
                    document.pages[page_id] = Page(
                        page_id=page_id,
                        image_uri=image_uri,
                        raw_text_uri=raw_text_uri,
                        parsed_text_uri=result_uri,
                        classification=page_data.get("classification"),
                        confidence=page_data.get("confidence"),
                        tables=page_data.get("tables", []),
                        forms=page_data.get("forms", {}),
                        classification_reason=page_data.get("classification_reason"),
                    )

                except Exception as e:
                    logger.warning(f"Error loading page {page_id}: {str(e)}")

            # Update document with number of pages
            document.num_pages = len(document.pages)

            # Now list sections
            sections_prefix = f"{prefix}sections/"
            section_dirs = set()

            # Find all section directories
            for section_page in paginator.paginate(
                Bucket=bucket, Prefix=sections_prefix, Delimiter="/"
            ):
                for prefix_item in section_page.get("CommonPrefixes", []):
                    section_dir = prefix_item.get("Prefix")
                    section_id = section_dir.split("/")[
                        -2
                    ]  # Extract section ID from path
                    section_dirs.add((section_id, section_dir))

            # Process each section directory
            for section_id, section_dir in section_dirs:
                result_key = f"{section_dir}result.json"

                try:
                    # Check if result.json exists
                    s3_client.head_object(Bucket=bucket, Key=result_key)

                    # Load section data from result.json
                    result_uri = build_s3_uri(bucket, result_key)
                    section_data = get_json_content(result_uri)

                    # Get section attributes if they exist in the result
                    attributes = section_data.get("attributes", section_data)

                    # Determine page IDs for this section based on classification
                    # If not available in section_data, we'll try to infer from page classifications
                    section_classification = section_data.get("classification")
                    page_ids = section_data.get("page_ids", [])

                    # If page_ids not found in section data, try to infer from pages
                    if not page_ids and section_classification:
                        for page_id, page in document.pages.items():
                            if page.classification == section_classification:
                                page_ids.append(page_id)

                    # If section_id is numeric, match it to page_id
                    if not page_ids and section_id.isdigit():
                        if section_id in document.pages:
                            page_ids = [section_id]

                    # Add section to document
                    document.sections.append(
                        Section(
                            section_id=section_id,
                            classification=section_classification,
                            confidence=section_data.get("confidence"),
                            page_ids=page_ids,
                            extraction_result_uri=result_uri,
                            attributes=attributes,
                        )
                    )

                except Exception as e:
                    logger.warning(f"Error loading section {section_id}: {str(e)}")

            return document

        except Exception as e:
            logger.error(f"Error building document from S3: {str(e)}")
            raise

    def compress(self, bucket: str, step_name: str = "processing") -> Dict[str, Any]:
        """
        Store full document in S3 and return lightweight wrapper for Step Functions.

        Args:
            bucket: S3 bucket to store the full document
            step_name: Name of the processing step (for unique S3 key)

        Returns:
            Lightweight wrapper containing essential fields and section IDs for Map step
        """
        import logging

        import boto3

        logger = logging.getLogger(__name__)
        s3_client = boto3.client("s3")

        # Generate unique S3 key with timestamp
        timestamp = str(int(time.time() * 1000))  # milliseconds for uniqueness
        s3_key = f"compressed_documents/{self.id}/{timestamp}_{step_name}_state.json"

        try:
            # Store full document in S3
            full_document_json = self.to_json()
            s3_client.put_object(
                Bucket=bucket,
                Key=s3_key,
                Body=full_document_json,
                ContentType="application/json",
            )

            s3_uri = f"s3://{bucket}/{s3_key}"
            logger.info(f"Compressed document {self.id} to {s3_uri}")

            # Create lightweight wrapper with just section IDs for Map step
            # This significantly reduces payload size for large documents
            sections_for_map = [section.section_id for section in self.sections]

            return {
                "document_id": self.id,
                "s3_uri": s3_uri,
                "timestamp": timestamp,
                "status": self.status.value,
                "num_pages": self.num_pages,
                "sections": sections_for_map,  # For Step Functions Map state
                # config_version travels in the lightweight wrapper so consumers
                # that never decompress (e.g. the pipeline-hooks dispatcher) can
                # still honor the version the document was processed under.
                "config_version": self.config_version,
                "config_revision": self.config_revision,
                "compressed": True,
            }

        except Exception as e:
            logger.error(f"Error compressing document {self.id}: {str(e)}")
            raise

    @classmethod
    def decompress(cls, bucket: str, compressed_data: Dict[str, Any]) -> "Document":
        """
        Restore full Document from S3 using compressed wrapper data.

        Args:
            bucket: S3 bucket containing the compressed document
            compressed_data: Lightweight wrapper from compress() method

        Returns:
            Full Document object with all content restored
        """
        import logging

        import boto3

        from idp_common.utils import parse_s3_uri

        logger = logging.getLogger(__name__)
        s3_client = boto3.client("s3")

        try:
            # Extract S3 key from URI
            s3_uri = compressed_data.get("s3_uri")
            if not s3_uri:
                raise ValueError("No s3_uri found in compressed data")

            _, s3_key = parse_s3_uri(s3_uri)

            # Retrieve full document from S3
            response = s3_client.get_object(Bucket=bucket, Key=s3_key)
            document_json = response["Body"].read().decode("utf-8")

            # Restore full document
            document = cls.from_json(document_json)

            logger.info(f"Decompressed document {document.id} from {s3_uri}")
            return document

        except Exception as e:
            logger.error(f"Error decompressing document: {str(e)}")
            raise

    @classmethod
    def from_compressed_or_dict(cls, data, bucket=None):
        """
        Create a Document from either compressed data or a regular dict.

        Args:
            data: Either a compressed document reference or a regular document dict
            bucket: S3 bucket name (required if data is compressed)

        Returns:
            Document: The document instance
        """
        if isinstance(data, dict) and data.get("compressed") is True:
            if not bucket:
                raise ValueError("Bucket required for decompressing document")
            return cls.decompress(bucket, data)
        else:
            return cls.from_dict(data)

    @classmethod
    def load_document(cls, event_data, working_bucket, logger=None):
        """
        Utility method to handle document input from Lambda events.
        Automatically handles both compressed and uncompressed documents.

        Args:
            event_data: The document data from the Lambda event
            working_bucket: S3 bucket for decompression
            logger: Optional logger for debug messages

        Returns:
            Document: The document instance
        """
        if isinstance(event_data, dict) and event_data.get("compressed") is True:
            if logger:
                logger.info("Decompressed document from S3")
            return cls.decompress(working_bucket, event_data)
        else:
            if logger:
                logger.info("Loaded uncompressed document")
            return cls.from_dict(event_data)

    def serialize_document(
        self, working_bucket, step_name, logger=None, size_threshold_kb=0
    ):
        """
        Utility method to prepare document output for Lambda responses.
        Automatically compresses documents and returns appropriate response format.

        Args:
            working_bucket: S3 bucket for compression
            step_name: Name of the processing step (for S3 key generation)
            logger: Optional logger for debug messages
            size_threshold_kb: Size threshold in KB for compression (default 0KB - always compress)

        Returns:
            dict: Response data with either compressed reference or document dict
        """
        document_json = json.dumps(self.to_dict(), default=str)
        document_size = len(document_json.encode("utf-8"))
        threshold_bytes = size_threshold_kb * 1024

        if logger:
            logger.info(f"Document size after {step_name}: {document_size} bytes")

        # Compress if document is larger than threshold (default 0KB means always compress)
        if working_bucket and document_size > threshold_bytes:
            if logger:
                logger.info(
                    f"Document size ({document_size} bytes) exceeds {size_threshold_kb}KB threshold, compressing to S3"
                )
            compressed_data = self.compress(working_bucket, step_name)
            return compressed_data
        else:
            if logger:
                logger.info(
                    f"Document size ({document_size} bytes) is under {size_threshold_kb}KB threshold, returning as JSON"
                )
            return self.to_dict()
