# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
IDP SDK Client

Main client class for programmatic access to IDP Accelerator capabilities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Optional

from .exceptions import IDPConfigurationError, IDPStackError

if TYPE_CHECKING:
    from idp_sdk.operations import (
        AssessmentOperation,
        BatchOperation,
        ConfigOperation,
        DiscoveryOperation,
        DocumentOperation,
        EvaluationOperation,
        ManifestOperation,
        PublishOperation,
        SearchOperation,
        StackOperation,
        TestingOperation,
    )


class IDPClient:
    """
    Main entry point for the IDP (Intelligent Document Processing) SDK.

    The IDPClient provides a unified interface to interact with AWS IDP Accelerator,
    enabling automated document classification, data extraction, and quality assessment
    for various document types (invoices, forms, contracts, etc.).

    Business Context:
        The IDP Accelerator automates document processing workflows by:
        - Classifying documents into predefined types
        - Extracting structured data from unstructured documents
        - Validating extraction quality with confidence scores
        - Evaluating results against baseline expectations
        - Enabling semantic search across processed documents

    Architecture:
        The client organizes functionality into 10 operation namespaces:
        - stack: Infrastructure deployment and management
        - batch: Process multiple documents in bulk
        - document: Process and manage individual documents
        - discovery: Discover document class schemas from sample documents
        - evaluation: Compare results against baseline data
        - assessment: Analyze extraction quality and confidence
        - search: Query processed documents with natural language
        - config: Manage pipeline configuration
        - manifest: Generate and validate document manifests
        - testing: Performance and load testing

    Usage Patterns:
        1. Stack-based operations (requires deployed stack):
           >>> client = IDPClient(stack_name="my-idp-stack", region="us-west-2")
           >>> result = client.batch.run(directory="./invoices")

        2. Stack-less operations (config, manifest):
           >>> client = IDPClient()
           >>> client.config.create(features="min", output="config.yaml")

        3. Per-operation stack override:
           >>> client = IDPClient()
           >>> client.batch.run(directory="./docs", stack_name="stack-1")
           >>> client.batch.run(directory="./forms", stack_name="stack-2")

    Examples:
        Process a batch of documents:
        >>> client = IDPClient(stack_name="my-stack")
        >>> result = client.batch.run(directory="./documents")
        >>> print(f"Queued {result.queued} documents")
        >>> status = client.batch.get_status(result.batch_id)
        >>> print(f"Completed: {status.completed}/{status.total}")

        Get extracted data from a document:
        >>> metadata = client.document.get_metadata(document_id="invoice-001.pdf")
        >>> print(f"Document type: {metadata.document_class}")
        >>> print(f"Extracted fields: {metadata.fields}")

        Check extraction confidence:
        >>> confidence = client.assessment.get_confidence(document_id="invoice-001.pdf")
        >>> for field, score in confidence.attributes.items():
        ...     print(f"{field}: {score.confidence:.2%}")

        Search processed documents:
        >>> result = client.search.query("What is the total amount on invoice 12345?")
        >>> if result.answer:
        ...     print(f"Answer: {result.answer} ({result.confidence:.2%})")
        >>> for citation in result.citations:
        ...     print(f"  from {citation.document.document_id} p{citation.document.page}")

    Attributes:
        stack: StackOperation - Infrastructure management
        batch: BatchOperation - Bulk document processing
        document: DocumentOperation - Single document operations
        discovery: DiscoveryOperation - Document class schema discovery
        evaluation: EvaluationOperation - Baseline comparison and accuracy
        assessment: AssessmentOperation - Quality metrics and confidence
        search: SearchOperation - Knowledge base queries
        config: ConfigOperation - Pipeline configuration
        manifest: ManifestOperation - Document manifest management
        testing: TestingOperation - Load and performance testing
    """

    def __init__(
        self,
        stack_name: Optional[str] = None,
        region: Optional[str] = None,
    ):
        """
        Initialize IDP SDK client.

        Args:
            stack_name: CloudFormation stack name. Required for most operations
                       (batch, document, evaluation, assessment, search, testing).
                       Optional for config and manifest operations.
                       Can be overridden per-operation if needed.
            region: AWS region (e.g., 'us-east-1', 'us-west-2').
                   If not specified, uses boto3's default region resolution:
                   1. AWS_DEFAULT_REGION environment variable
                   2. AWS config file (~/.aws/config)
                   3. EC2 instance metadata (if running on EC2)

        Examples:
            Initialize with stack name:
            >>> client = IDPClient(stack_name="my-idp-stack")

            Initialize with stack and region:
            >>> client = IDPClient(stack_name="my-stack", region="us-west-2")

            Initialize without stack (for config/manifest operations):
            >>> client = IDPClient()
            >>> client.config.create(features="min", output="config.yaml")

        Raises:
            IDPConfigurationError: If stack_name is required but not provided
                                  when calling stack-dependent operations
        """
        self._stack_name = stack_name
        self._region = region
        self._resources_cache: Optional[Dict[str, str]] = None

        # Initialize operation namespaces
        from idp_sdk.operations import (
            AssessmentOperation,
            BatchOperation,
            ConfigOperation,
            DiscoveryOperation,
            DocumentOperation,
            EvaluationOperation,
            ManifestOperation,
            PublishOperation,
            SearchOperation,
            StackOperation,
            TestingOperation,
        )
        from idp_sdk.operations.chat import ChatOperation

        self.stack: StackOperation = StackOperation(self)
        self.batch: BatchOperation = BatchOperation(self)
        self.document: DocumentOperation = DocumentOperation(self)
        self.config: ConfigOperation = ConfigOperation(self)
        self.discovery: DiscoveryOperation = DiscoveryOperation(self)
        self.manifest: ManifestOperation = ManifestOperation(self)
        self.testing: TestingOperation = TestingOperation(self)
        self.search: SearchOperation = SearchOperation(self)
        self.evaluation: EvaluationOperation = EvaluationOperation(self)
        self.assessment: AssessmentOperation = AssessmentOperation(self)
        self.publish: PublishOperation = PublishOperation(self)
        self.chat: ChatOperation = ChatOperation(self)

    @property
    def stack_name(self) -> Optional[str]:
        """Current default stack name."""
        return self._stack_name

    @stack_name.setter
    def stack_name(self, value: str):
        """Set default stack name and clear resource cache."""
        self._stack_name = value
        self._resources_cache = None

    @property
    def region(self) -> Optional[str]:
        """Current AWS region."""
        return self._region

    def _require_stack(self, stack_name: Optional[str] = None) -> str:
        """
        Ensure stack_name is available.

        Args:
            stack_name: Override stack name

        Returns:
            Stack name to use

        Raises:
            IDPConfigurationError: If no stack name available
        """
        name = stack_name or self._stack_name
        if not name:
            raise IDPConfigurationError(
                "stack_name is required for this operation. "
                "Either pass it to the method or set it when creating IDPClient."
            )
        return name

    def _get_stack_resources(self, stack_name: Optional[str] = None) -> Dict[str, str]:
        """Get stack resources with caching."""
        from idp_sdk._core.stack_info import StackInfo

        name = self._require_stack(stack_name)

        # Use cache if available and stack name matches
        if self._resources_cache and stack_name is None:
            return self._resources_cache

        stack_info = StackInfo(name, self._region)
        if not stack_info.validate_stack():
            raise IDPStackError(
                f"Stack '{name}' is not in a valid state for operations"
            )

        resources = stack_info.get_resources()

        # Cache only if using default stack
        if stack_name is None:
            self._resources_cache = resources

        return resources
