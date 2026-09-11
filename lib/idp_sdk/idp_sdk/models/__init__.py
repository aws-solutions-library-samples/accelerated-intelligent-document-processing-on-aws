# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""IDP SDK Models - Pydantic models for typed responses."""

from .assessment import (
    AssessmentConfidenceResult,
    AssessmentFieldConfidence,
    AssessmentFieldGeometry,
    AssessmentGeometryResult,
    AssessmentMetrics,
)
from .base import DocumentState, Pattern, RerunStep, StackState
from .batch import (
    BatchDeletionResult,
    BatchDownloadResult,
    BatchInfo,
    BatchListResult,
    BatchProcessResult,
    BatchReprocessResult,
    BatchRerunResult,
    BatchResult,
    BatchStatus,
)
from .chat import ChatResponse
from .config import (
    ConfigActivateResult,
    ConfigCreateResult,
    ConfigDeleteResult,
    ConfigDownloadResult,
    ConfigListResult,
    ConfigRevisionInfo,
    ConfigRevisionListResult,
    ConfigSyncBdaResult,
    ConfigUploadResult,
    ConfigValidationResult,
    ConfigVersionInfo,
)
from .discovery import (
    AutoDetectResult,
    AutoDetectSection,
    DiscoveredClassResult,
    DiscoveryBatchResult,
    DiscoveryResult,
    MultiDocDiscoveryResult,
)
from .document import (
    DocumentDeletionResult,
    DocumentDownloadResult,
    DocumentInfo,
    DocumentListResult,
    DocumentMetadata,
    DocumentReprocessResult,
    DocumentRerunResult,
    DocumentStatus,
    DocumentUploadResult,
)
from .evaluation import (
    BaselineInfo,
    BaselineResult,
    DeleteResult,
    EvaluationBaselineListResult,
    EvaluationMetrics,
    EvaluationReport,
    FieldComparison,
    UseAsBaselineResult,
)
from .manifest import ManifestDocument, ManifestResult, ManifestValidationResult
from .publish import PublishResult, TemplateTransformResult
from .search import SearchCitation, SearchDocumentReference, SearchResult
from .stack import (
    BucketInfo,
    CancelUpdateResult,
    FailureAnalysis,
    FailureCause,
    OrphanedResourceCleanupResult,
    StackDeletionResult,
    StackDeploymentResult,
    StackMonitorResult,
    StackOperationInProgress,
    StackResources,
    StackStableStateResult,
)
from .testing import (
    DocumentsAbortedResult,
    ExecutionsStoppedResult,
    LoadTestResult,
    StopWorkflowsResult,
    TestComparisonResult,
    TestRunResult,
)

__all__ = [
    # Enums
    "StackState",
    "DocumentState",
    "Pattern",
    "RerunStep",
    # Stack models
    "StackDeploymentResult",
    "StackDeletionResult",
    "StackResources",
    "StackOperationInProgress",
    "StackMonitorResult",
    "StackStableStateResult",
    "FailureCause",
    "FailureAnalysis",
    "BucketInfo",
    "CancelUpdateResult",
    "OrphanedResourceCleanupResult",
    # Batch models
    "BatchResult",
    "BatchProcessResult",
    "BatchStatus",
    "BatchInfo",
    "BatchListResult",
    "BatchRerunResult",
    # Chat models
    "ChatResponse",
    "BatchReprocessResult",
    "BatchDownloadResult",
    "BatchDeletionResult",
    # Document models
    "DocumentStatus",
    "DocumentUploadResult",
    "DocumentDownloadResult",
    "DocumentRerunResult",
    "DocumentReprocessResult",
    "DocumentDeletionResult",
    "DocumentMetadata",
    "DocumentInfo",
    "DocumentListResult",
    # Config models
    "ConfigCreateResult",
    "ConfigValidationResult",
    "ConfigDownloadResult",
    "ConfigUploadResult",
    "ConfigActivateResult",
    "ConfigVersionInfo",
    "ConfigListResult",
    "ConfigRevisionInfo",
    "ConfigRevisionListResult",
    "ConfigDeleteResult",
    "ConfigSyncBdaResult",
    # Discovery models
    "DiscoveryResult",
    "DiscoveryBatchResult",
    "AutoDetectResult",
    "AutoDetectSection",
    "DiscoveredClassResult",
    "MultiDocDiscoveryResult",
    # Manifest models
    "ManifestDocument",
    "ManifestResult",
    "ManifestValidationResult",
    # Testing models
    "StopWorkflowsResult",
    "ExecutionsStoppedResult",
    "DocumentsAbortedResult",
    "LoadTestResult",
    "TestRunResult",
    "TestComparisonResult",
    # Search models
    "SearchResult",
    "SearchCitation",
    "SearchDocumentReference",
    # Evaluation models
    "BaselineResult",
    "BaselineInfo",
    "EvaluationBaselineListResult",
    "EvaluationReport",
    "FieldComparison",
    "EvaluationMetrics",
    "UseAsBaselineResult",
    "DeleteResult",
    # Publish models
    "PublishResult",
    "TemplateTransformResult",
    # Assessment models
    "AssessmentConfidenceResult",
    "AssessmentFieldConfidence",
    "AssessmentGeometryResult",
    "AssessmentFieldGeometry",
    "AssessmentMetrics",
]
