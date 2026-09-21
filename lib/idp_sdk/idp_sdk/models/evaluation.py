# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Evaluation operation models.

The report and metrics models mirror the evaluation artifact the pipeline
actually writes, ``<document key>/evaluation/results.json`` — a document-level
``overall_metrics`` dict plus one ``section_results`` entry per section, each
carrying its own ``metrics`` and a list of per-attribute comparisons. The metric
names (``accuracy``, ``precision``, ``recall``, ``f1_score``) are the ones that
file uses, so nothing here has to be derived or guessed.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class BaselineResult:
    """Result from baseline creation."""

    document_id: str
    status: str  # "BASELINE_COPYING", "BASELINE_AVAILABLE", "BASELINE_ERROR"
    s3_location: str
    created_date: Optional[datetime] = None


@dataclass
class UseAsBaselineResult:
    """Result from promoting a processed document's output to the baseline."""

    document_id: str
    files_copied: int
    evaluation_status: str  # "BASELINE_AVAILABLE" on success
    timestamp: Optional[str] = None


@dataclass
class BaselineInfo:
    """Information about a baseline."""

    document_id: str
    s3_location: str
    #: Only populated by callers that stat the baseline objects. A prefix
    #: listing reports neither a creation date nor a size, so ``list_baselines``
    #: leaves both unset rather than inventing them.
    created_date: Optional[datetime] = None
    size_bytes: Optional[int] = None


@dataclass
class EvaluationBaselineListResult:
    """One page of baselines."""

    baselines: List[BaselineInfo]
    #: Baselines in *this* page. A prefix listing cannot report a bucket-wide
    #: total, so there is no total here that could be misread as one.
    count: int
    next_token: Optional[str] = None


@dataclass
class FieldComparison:
    """One attribute's expected-vs-actual comparison within a section."""

    attribute: str
    #: Expected and actual values are whatever the schema declared for the
    #: attribute — a scalar, a list, a nested object, or ``None`` when absent.
    expected: Any
    actual: Any
    matched: bool
    score: Optional[float]
    #: Comparator that produced ``score`` (e.g. ``"EXACT"``, ``"FUZZY"``, ``"LLM"``).
    method: Optional[str]
    reason: Optional[str]


@dataclass
class EvaluationReport:
    """Evaluation report for one section of a document."""

    document_id: str
    section_id: int
    field_comparisons: List[FieldComparison]
    document_class: Optional[str] = None
    accuracy: Optional[float] = None
    precision: Optional[float] = None
    recall: Optional[float] = None
    f1_score: Optional[float] = None
    #: The document-level ``overall_metrics`` block, for context on how this
    #: section's numbers compare with the document as a whole.
    overall_metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationMetrics:
    """Aggregate evaluation metrics across documents."""

    total_documents: int
    avg_accuracy: float
    avg_precision: float
    avg_recall: float
    avg_f1_score: float
    #: Per document class: ``{"count": int, "avg_accuracy": float}``, aggregated
    #: over sections rather than documents, because a document class is a
    #: property of a section.
    by_document_class: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: The date filters the caller supplied, echoed back so a stored result
    #: records the window it covers. ``None`` means "unfiltered".
    start_date: Optional[str] = None
    end_date: Optional[str] = None


@dataclass
class DeleteResult:
    """Result from delete operation."""

    document_id: str
    status: str  # "DELETED", "NOT_FOUND", "ERROR"
    message: Optional[str] = None
