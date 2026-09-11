# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Discovery-related models."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DiscoveryResult(BaseModel):
    """Result of a single discovery operation."""

    status: str = Field(description="Discovery status ('SUCCESS' or 'FAILED')")
    document_class: Optional[str] = Field(
        default=None, description="Discovered document class name (from $id)"
    )
    json_schema: Optional[Dict[str, Any]] = Field(
        default=None, description="Generated JSON Schema for the document class"
    )
    config_version: Optional[str] = Field(
        default=None,
        description="Configuration profile the schema was saved to",
    )
    document_path: Optional[str] = Field(
        default=None, description="Path to the source document"
    )
    page_range: Optional[str] = Field(
        default=None, description="Page range that was discovered (e.g., '1-3')"
    )
    error: Optional[str] = Field(
        default=None, description="Error message if discovery failed"
    )


class DiscoveryBatchResult(BaseModel):
    """Result of a batch discovery operation."""

    total: int = Field(description="Total number of documents processed")
    succeeded: int = Field(description="Number of successful discoveries")
    failed: int = Field(description="Number of failed discoveries")
    results: List[DiscoveryResult] = Field(
        default_factory=list, description="Individual discovery results"
    )


class AutoDetectSection(BaseModel):
    """A detected document section boundary."""

    start: int = Field(description="Start page number (1-based)")
    end: int = Field(description="End page number (1-based)")
    type: Optional[str] = Field(
        default=None, description="Detected document type label"
    )


class AutoDetectResult(BaseModel):
    """Result of auto-detecting document sections."""

    status: str = Field(description="Detection status ('SUCCESS' or 'FAILED')")
    sections: List[AutoDetectSection] = Field(
        default_factory=list, description="Detected section boundaries"
    )
    document_path: Optional[str] = Field(
        default=None, description="Path to the source document"
    )
    error: Optional[str] = Field(
        default=None, description="Error message if detection failed"
    )


# ---- Multi-document discovery models ----


class DiscoveredClassResult(BaseModel):
    """A single document class discovered from a cluster of similar documents."""

    cluster_id: int = Field(description="Cluster ID this class was discovered from")
    classification: Optional[str] = Field(
        default=None, description="Discovered document class name"
    )
    json_schema: Optional[Dict[str, Any]] = Field(
        default=None, description="Generated JSON Schema for this document class"
    )
    document_count: int = Field(
        default=0, description="Number of documents in this cluster"
    )
    sample_doc_ids: List[str] = Field(
        default_factory=list,
        description="Representative document identifiers from this cluster",
    )
    error: Optional[str] = Field(
        default=None, description="Error message if analysis failed for this cluster"
    )


class MultiDocDiscoveryResult(BaseModel):
    """Result of multi-document discovery across a collection of documents."""

    status: str = Field(
        description="Overall status ('SUCCESS', 'PARTIAL', or 'FAILED')"
    )
    discovered_classes: List[DiscoveredClassResult] = Field(
        default_factory=list, description="Discovered document classes"
    )
    reflection_report: Optional[str] = Field(
        default=None,
        description="Markdown reflection report analyzing the discovered classes",
    )
    total_documents: int = Field(
        default=0, description="Total number of documents processed"
    )
    total_clusters: int = Field(default=0, description="Number of clusters found")
    noise_documents: int = Field(
        default=0,
        description="Number of documents that couldn't be clustered (noise/outliers)",
    )
    config_version: Optional[str] = Field(
        default=None,
        description="Configuration profile schemas were saved to, if any",
    )
    error: Optional[str] = Field(
        default=None, description="Error message if discovery failed"
    )
