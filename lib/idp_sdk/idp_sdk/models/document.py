# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document-related models."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import DocumentState, RerunStep


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
    step: RerunStep = Field(description="Pipeline step being reprocessed")
    queued: bool = Field(description="Whether document was successfully queued")


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
