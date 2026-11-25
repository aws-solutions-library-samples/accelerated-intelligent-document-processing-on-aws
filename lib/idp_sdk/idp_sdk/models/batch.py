# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Batch processing models."""

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .base import RerunStep
from .document import DocumentDeletionResult, DocumentReprocessResult, DocumentStatus


class BatchDeletionResult(BaseModel):
    """Result of a batch document deletion operation."""

    success: bool = Field(description="Whether all deletions succeeded")
    deleted_count: int = Field(description="Number of documents successfully deleted")
    failed_count: int = Field(description="Number of documents that failed to delete")
    total_count: int = Field(description="Total number of documents attempted")
    dry_run: bool = Field(
        default=False, description="Whether this was a dry run (no actual deletions)"
    )
    results: List[DocumentDeletionResult] = Field(
        default_factory=list, description="Per-document deletion results"
    )


class BatchProcessResult(BaseModel):
    """Result of a batch processing operation."""

    batch_id: str = Field(description="Unique batch identifier")
    document_ids: List[str] = Field(description="List of document IDs in the batch")
    documents_queued: int = Field(
        alias="queued", description="Number of documents queued"
    )
    documents_uploaded: int = Field(
        alias="uploaded", description="Number of files uploaded"
    )
    documents_failed: int = Field(
        alias="failed", description="Number of documents that failed to queue"
    )
    baselines_uploaded: int = Field(
        default=0, description="Number of baseline files uploaded for evaluation"
    )
    source: str = Field(description="Source path (manifest, directory, or S3 URI)")
    output_prefix: str = Field(description="Output prefix for results")
    timestamp: datetime = Field(description="Batch submission timestamp")

    model_config = ConfigDict(populate_by_name=True)


# Backward compatibility alias
BatchResult = BatchProcessResult


class BatchStatus(BaseModel):
    """Status information for a batch of documents."""

    batch_id: str = Field(description="Batch identifier")
    documents: List[DocumentStatus] = Field(description="Status of each document")
    total: int = Field(description="Total number of documents")
    completed: int = Field(description="Number of completed documents")
    failed: int = Field(description="Number of failed documents")
    in_progress: int = Field(description="Number of documents in progress")
    queued: int = Field(description="Number of queued documents")
    #: Documents whose processing has stopped to wait for a human reviewer
    #: (`HITL_IN_PROGRESS`). Neither completed nor failed; they move on once a
    #: reviewer completes or rejects them in a review queue.
    awaiting_review: int = Field(
        default=0, description="Number of documents awaiting human review"
    )
    success_rate: float = Field(description="Completion success rate (0-1)")
    #: True once no document is queued or running. A document awaiting review
    #: counts as settled, because nothing more happens to it without a person, so
    #: a loop polling on this ends; read `awaiting_review` to tell a batch that
    #: finished from one that is waiting on reviewers.
    all_complete: bool = Field(
        description=(
            "Whether processing has finished: no document is queued or running "
            "(documents awaiting human review count as finished)"
        )
    )
    elapsed_seconds: Optional[float] = Field(
        default=None, description="Total elapsed time"
    )


class BatchInfo(BaseModel):
    """Information about a batch."""

    batch_id: str = Field(description="Batch identifier")
    document_ids: List[str] = Field(description="Document IDs in the batch")
    queued: int = Field(description="Number of documents queued")
    failed: int = Field(description="Number of documents failed")
    timestamp: str = Field(description="Batch creation timestamp")


class BatchReprocessResult(BaseModel):
    """Result of a batch reprocess operation."""

    documents_queued: int = Field(
        description="Number of documents queued for reprocess"
    )
    documents_failed: int = Field(
        description="Number of documents that failed to queue"
    )
    failed_documents: List[Dict[str, str]] = Field(
        default_factory=list,
        description=(
            "The documents that were not queued: object_key, error (the "
            "reason in words), status and reason"
        ),
    )
    step: RerunStep = Field(description="The step that was asked for")
    results: List[DocumentReprocessResult] = Field(
        default_factory=list,
        description="One result per document: status, start step, reason, message",
    )


# Backward compatibility alias
BatchRerunResult = BatchReprocessResult


class BatchDownloadResult(BaseModel):
    """Result of a batch download operation."""

    files_downloaded: int = Field(description="Number of files downloaded")
    documents_downloaded: int = Field(description="Number of documents with downloads")
    output_dir: str = Field(description="Local output directory path")


class BatchListResult(BaseModel):
    """Result of a batch list operation with pagination.

    This class is backward compatible - can be used as a list or access pagination.
    """

    batches: List[BatchInfo] = Field(description="List of batch information")
    count: int = Field(description="Number of batches in this response")
    next_token: Optional[str] = Field(
        default=None, description="Token for retrieving next page of results"
    )

    def __iter__(self):
        """Allow iteration over batches directly for backward compatibility."""
        return iter(self.batches)

    def __len__(self):
        """Allow len() for backward compatibility."""
        return len(self.batches)

    def __getitem__(self, index):
        """Allow indexing for backward compatibility."""
        return self.batches[index]
