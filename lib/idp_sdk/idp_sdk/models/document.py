# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document-related models."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from idp_common.review_queues.statuses import ReviewStatus

from .base import DocumentBucket, DocumentState, RerunStep


def _whole_numbers(values: Any) -> List[int]:
    """The whole numbers of a list the lookup function returned, as ints."""
    if not isinstance(values, list):
        return []
    return [
        int(value)
        for value in values
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value // 1
    ]


class ReviewPolicyFailure(BaseModel):
    """A policy rule the document failed, as its reviewer is shown it."""

    rule_id: str = Field(default="", description="<policy type>.<rule key>")
    policy: str = Field(default="", description="The policy the rule belongs to")
    name: str = Field(
        default="", description="The rule's key: the id without its policy type"
    )
    reason: str = Field(
        default="",
        description=(
            "The explanation policy rule validation stored for the rule, shortened"
        ),
    )
    pages: List[int] = Field(
        default_factory=list, description="The page numbers the explanation cites"
    )

    @classmethod
    def from_lookup(cls, failure: Dict[str, Any]) -> "ReviewPolicyFailure":
        """Build from one entry of the lookup function's ``policyFailures``."""
        return cls(
            rule_id=str(failure.get("ruleId") or ""),
            policy=str(failure.get("policy") or ""),
            name=str(failure.get("name") or ""),
            reason=str(failure.get("reason") or ""),
            pages=_whole_numbers(failure.get("pages")),
        )


class ReviewInfo(BaseModel):
    """A document's review-queue status (HITL review queues).

    ``status`` is ``in_review`` until a reviewer finishes, then ``complete`` or
    ``rejected``; the same value as ``<document>/review/status.json``.
    """

    status: ReviewStatus = Field(description="in_review, complete or rejected")
    queue: Optional[str] = Field(default=None, description="Review queue")
    cycle_id: Optional[str] = Field(default=None, description="Review cycle id")
    status_uri: Optional[str] = Field(
        default=None, description="S3 URI of the review status object"
    )
    reviewed_by: Optional[str] = Field(default=None, description="Reviewer email")
    disposed_at: Optional[str] = Field(
        default=None, description="When the reviewer completed or rejected it"
    )
    reject_reason: Optional[str] = Field(default=None, description="Reject reason")
    queue_name: Optional[str] = Field(
        default=None, description="The review queue's name, as people see it"
    )
    routed_by: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "What placed the document in the queue: the routing rule that "
            "matched, or the person who sent it there"
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        description=(
            "What the reviewer reads: the routing rule's reason with the "
            "document's values in it. None when no reason is recorded"
        ),
    )
    policy_failures: List[ReviewPolicyFailure] = Field(
        default_factory=list,
        description=(
            "The first policy rules the document failed, whichever routing rule "
            "placed it"
        ),
    )
    policy_failure_count: int = Field(
        default=0,
        description=(
            "How many policy rules the document failed; policy_failures may list fewer"
        ),
    )

    @classmethod
    def from_lookup(cls, review: Optional[Dict[str, Any]]) -> Optional["ReviewInfo"]:
        """Build from the lookup function's ``review`` block (camelCase)."""
        if not review or not review.get("status"):
            return None
        routed = review.get("routedBy")
        routed = routed if isinstance(routed, dict) else {}
        failures = routed.get("policyFailures")
        counted = _whole_numbers([routed.get("policyFailureCount")])
        return cls(
            status=review["status"],
            queue=review.get("queue"),
            cycle_id=review.get("cycleId"),
            status_uri=review.get("statusUri"),
            reviewed_by=review.get("reviewedBy"),
            disposed_at=review.get("disposedAt"),
            reject_reason=review.get("rejectReason"),
            queue_name=review.get("queueName"),
            routed_by=review.get("routedBy"),
            reason=str(routed.get("reason") or "") or None,
            policy_failures=[
                ReviewPolicyFailure.from_lookup(failure)
                for failure in (failures if isinstance(failures, list) else [])
                if isinstance(failure, dict)
            ],
            policy_failure_count=counted[0] if counted else 0,
        )


class DocumentStatus(BaseModel):
    """Status information for a single document."""

    document_id: str = Field(description="Document identifier (S3 key)")
    status: DocumentState = Field(description="Current processing status")
    start_time: Optional[datetime] = Field(
        default=None, description="Processing start time"
    )
    end_time: Optional[datetime] = Field(
        default=None, description="Processing end time"
    )
    duration_seconds: Optional[float] = Field(
        default=None, description="Processing duration in seconds"
    )
    num_pages: Optional[int] = Field(default=None, description="Number of pages")
    num_sections: Optional[int] = Field(
        default=None, description="Number of extracted sections"
    )
    error: Optional[str] = Field(default=None, description="Error message if failed")
    #: The progress bucket the producer sorted this document into, when the
    #: producer knows something `status` alone does not say.
    #:
    #: `classify_document_state` is total over `DocumentState`, so for almost
    #: every document a consumer can derive the bucket from `status` and get the
    #: same answer. `NOT_FOUND` is the exception: within
    #: `NOT_FOUND_GRACE_SECONDS` of a batch's submission it means "the
    #: QueueSender row has not landed yet", which is `QUEUED`, and past that
    #: window it means "there will never be a row", which is `FAILED`. Only a
    #: producer holding the batch's submission time can tell those apart, so it
    #: records its verdict here rather than leaving each consumer to re-derive a
    #: bucket from a status that cannot express the difference.
    #:
    #: `None` means the producer expressed no opinion; derive the bucket from
    #: `status` in that case.
    bucket: Optional[DocumentBucket] = Field(
        default=None, description="Progress bucket assigned by the producer"
    )
    #: Review-queue status; None when the document was never routed to a queue.
    review: Optional[ReviewInfo] = Field(
        default=None, description="Review-queue status of the document"
    )
    review_status: Optional[ReviewStatus] = Field(
        default=None, description="in_review, complete or rejected (review queues)"
    )


class DocumentUploadResult(BaseModel):
    """Result of uploading a single document."""

    document_id: str = Field(description="Document identifier (S3 key)")
    status: str = Field(description="Initial status (typically 'queued')")
    timestamp: datetime = Field(description="Upload timestamp")


class DocumentDownloadResult(BaseModel):
    """Result of downloading a single document's results."""

    document_id: str = Field(description="Document identifier")
    files_downloaded: int = Field(description="Number of files downloaded")
    output_dir: str = Field(description="Local output directory path")


class DocumentReprocessResult(BaseModel):
    """Result of reprocessing a single document."""

    document_id: str = Field(description="Document identifier")
    step: RerunStep = Field(description="The step that was asked for")
    queued: bool = Field(description="Whether document was successfully queued")
    status: str = Field(
        default="",
        description=(
            "QUEUED, REFUSED (the document cannot be processed again as asked) "
            "or FAILED (an error while queuing it)"
        ),
    )
    start_step: Optional[str] = Field(
        default=None,
        description="The step the run starts at, after aliases and 'resume'",
    )
    reason: Optional[str] = Field(
        default=None,
        description="The code that explains a refusal, or the error of a failure",
    )
    message: Optional[str] = Field(
        default=None, description="The refusal or the failure, in words"
    )


class ReprocessStepOption(BaseModel):
    """One step of a document's processing mode, and whether a run can start at it."""

    id: str = Field(description="Step id")
    available: bool = Field(description="Whether a reprocess can start at this step")
    skippable: bool = Field(description="Whether a run can leave this step out")
    reason: Optional[str] = Field(
        default=None, description="The code that says why the step is unavailable"
    )


class ReprocessOption(BaseModel):
    """What one document can be reprocessed from."""

    document_id: str = Field(description="Document identifier")
    mode: str = Field(description="Processing mode: 'pipeline' or 'bda'")
    default_step: str = Field(description="The step a reprocess starts at by default")
    steps: List[ReprocessStepOption] = Field(
        default_factory=list,
        description="Every step of the document's mode, in pipeline order",
    )


# Backward compatibility alias
DocumentRerunResult = DocumentReprocessResult


class DocumentDeletionResult(BaseModel):
    """Result of deleting a single document."""

    success: bool = Field(description="Whether deletion succeeded")
    object_key: str = Field(description="Document object key (S3 path)")
    deleted: Dict[str, Any] = Field(
        default_factory=dict,
        description="Details of deleted items (input_file, output_files, list_entries, document_record)",
    )
    errors: List[str] = Field(default_factory=list, description="Error messages if any")


class DocumentMetadata(BaseModel):
    """Extracted metadata and fields for a document section."""

    document_id: str = Field(description="Document identifier")
    section_id: int = Field(description="Section number")
    document_class: Optional[str] = Field(
        default=None, description="Classified document type"
    )
    fields: Dict[str, Any] = Field(
        default_factory=dict, description="Extracted fields and values"
    )
    confidence: Optional[Dict[str, float]] = Field(
        default=None, description="Confidence scores per field (if available)"
    )
    page_count: Optional[int] = Field(default=None, description="Number of pages")
    metadata: Optional[Dict[str, Any]] = Field(
        default=None, description="Additional metadata"
    )


class DocumentInfo(BaseModel):
    """Basic document information for listing.

    Exactly the attributes ``DocumentProcessor.list_documents`` reads off each
    tracking-table item. Richer per-document detail (page counts, classified
    type) comes from ``document.get_status()`` and ``document.get_metadata()``,
    which read the full record.
    """

    document_id: str = Field(description="Document identifier")
    status: DocumentState = Field(description="Processing status")
    timestamp: Optional[datetime] = Field(default=None, description="Upload timestamp")
    batch_id: Optional[str] = Field(
        default=None, description="Batch this document was submitted with, if any"
    )


class DocumentListResult(BaseModel):
    """One page of documents."""

    documents: List[DocumentInfo] = Field(description="List of documents")
    count: int = Field(
        description=(
            "Number of documents in this page. The tracking table is paginated "
            "with a DynamoDB scan, which reports no table-wide total, so this is "
            "a page size rather than a grand total."
        )
    )
    next_token: Optional[str] = Field(
        default=None, description="Continuation token for next page"
    )
