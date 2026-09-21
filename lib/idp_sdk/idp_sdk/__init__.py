# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
IDP SDK - Python SDK for IDP Accelerator

Provides programmatic access to document processing capabilities.

Example:
    >>> from idp_sdk import IDPClient
    >>>
    >>> # Stack operations
    >>> client = IDPClient()
    >>> client.stack.deploy(stack_name="my-stack", pattern="pattern-2")
    >>>
    >>> # Batch operations
    >>> client = IDPClient(stack_name="my-stack", region="us-west-2")
    >>> result = client.batch.run(source="./documents/")
    >>> status = client.batch.get_status(batch_id=result.batch_id)
    >>>
    >>> # Config operations (no stack required)
    >>> client = IDPClient()
    >>> client.config.create(features="min", output="config.yaml")
"""

from .client import IDPClient
from .exceptions import (
    IDPConfigurationError,
    IDPError,
    IDPProcessingError,
    IDPResourceNotFoundError,
    IDPStackError,
    IDPTimeoutError,
    IDPValidationError,
)
from .models import (
    # Assessment models
    AssessmentConfidenceResult,
    AssessmentFieldConfidence,
    AssessmentFieldGeometry,
    AssessmentGeometryResult,
    AssessmentMetrics,
    # Discovery models
    AutoDetectResult,
    AutoDetectSection,
    BaselineInfo,
    BaselineResult,
    # Batch models
    BatchDeletionResult,
    BatchDownloadResult,
    BatchInfo,
    BatchListResult,
    BatchProcessResult,
    BatchReprocessResult,
    BatchRerunResult,
    BatchResult,
    BatchStatus,
    BucketInfo,
    CancelUpdateResult,
    # Chat models
    ChatResponse,
    # Config models
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
    DeleteResult,
    DiscoveredClassResult,
    DiscoveryBatchResult,
    DiscoveryResult,
    # Document models
    DocumentDeletionResult,
    DocumentDownloadResult,
    DocumentInfo,
    DocumentListResult,
    DocumentMetadata,
    DocumentReprocessResult,
    DocumentRerunResult,
    # Testing models
    DocumentsAbortedResult,
    DocumentState,
    DocumentStatus,
    DocumentUploadResult,
    # Evaluation models
    EvaluationBaselineListResult,
    EvaluationMetrics,
    EvaluationReport,
    ExecutionsStoppedResult,
    FailureAnalysis,
    FailureCause,
    FieldComparison,
    # Manifest models
    LoadTestResult,
    ManifestDocument,
    ManifestResult,
    ManifestValidationResult,
    MultiDocDiscoveryResult,
    # Stack models
    OrphanedResourceCleanupResult,
    # Enums
    Pattern,
    # Publish models
    PublishResult,
    RerunStep,
    # Search models
    SearchCitation,
    SearchDocumentReference,
    SearchResult,
    StackDeletionResult,
    StackDeploymentResult,
    StackMonitorResult,
    StackOperationInProgress,
    StackResources,
    StackStableStateResult,
    StackState,
    StopWorkflowsResult,
    TemplateTransformResult,
    TestComparisonResult,
    TestRunResult,
    UseAsBaselineResult,
)

__version__ = "0.6.9"

__all__ = [
    # Client
    "IDPClient",
    # Exceptions
    "IDPError",
    "IDPConfigurationError",
    "IDPStackError",
    "IDPProcessingError",
    "IDPValidationError",
    "IDPResourceNotFoundError",
    "IDPTimeoutError",
    # Enums
    "StackState",
    "DocumentState",
    "Pattern",
    "RerunStep",
    # Publish models
    "PublishResult",
    "TemplateTransformResult",
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
    "BatchReprocessResult",
    "BatchDownloadResult",
    "BatchDeletionResult",
    # Chat models
    "ChatResponse",
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
    # Evaluation models
    "EvaluationReport",
    "EvaluationMetrics",
    "EvaluationBaselineListResult",
    "BaselineResult",
    "BaselineInfo",
    "FieldComparison",
    "DeleteResult",
    "UseAsBaselineResult",
    # Assessment models
    "AssessmentConfidenceResult",
    "AssessmentFieldConfidence",
    "AssessmentGeometryResult",
    "AssessmentFieldGeometry",
    "AssessmentMetrics",
    # Search models
    "SearchResult",
    "SearchCitation",
    "SearchDocumentReference",
    # Config models
    "ConfigCreateResult",
    "ConfigValidationResult",
    "ConfigDownloadResult",
    "ConfigUploadResult",
    "ConfigActivateResult",
    "ConfigVersionInfo",
    "ConfigListResult",
    "ConfigDeleteResult",
    "ConfigSyncBdaResult",
    "ConfigRevisionInfo",
    "ConfigRevisionListResult",
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
]
