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
    # Chat models
    ChatResponse,
    # Config models
    ConfigActivateResult,
    ConfigCreateResult,
    ConfigDeleteResult,
    ConfigDownloadResult,
    ConfigListResult,
    ConfigSyncBdaResult,
    ConfigUploadResult,
    ConfigValidationResult,
    ConfigVersionInfo,
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
    # Manifest models
    LoadTestResult,
    ManifestDocument,
    ManifestResult,
    ManifestValidationResult,
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
    StackResources,
    StackState,
    StopWorkflowsResult,
    TemplateTransformResult,
)

__version__ = "0.6.7"

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
    # Discovery models
    "DiscoveryResult",
    "DiscoveryBatchResult",
    "AutoDetectResult",
    "AutoDetectSection",
    # Manifest models
    "ManifestDocument",
    "ManifestResult",
    "ManifestValidationResult",
    # Testing models
    "StopWorkflowsResult",
    "ExecutionsStoppedResult",
    "DocumentsAbortedResult",
    "LoadTestResult",
]
