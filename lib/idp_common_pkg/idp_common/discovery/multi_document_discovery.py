# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Multi-document discovery orchestrator.

Coordinates the full pipeline for discovering document classes from a collection
of documents: embed → cluster → analyze → generate schemas → reflect.

This module provides both a high-level orchestrator for direct usage and
individual step methods suitable for Step Functions Lambda handlers.

Supports two document source modes:
- **S3 mode**: documents in an S3 bucket (used by Lambda handlers)
- **Local mode**: documents on local filesystem (used by CLI/SDK)
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import boto3

from idp_common.bedrock.client import BedrockClient
from idp_common.config.class_names import sanitize_class_name
from idp_common.discovery.clustering_service import ClusteringService, ClusterResult
from idp_common.discovery.discovery_agent import DiscoveredClass, DiscoveryAgent
from idp_common.discovery.embedding_service import EmbeddingResult, EmbeddingService

logger = logging.getLogger(__name__)


@dataclass
class MultiDocDiscoveryResult:
    """Complete result of multi-document discovery."""

    discovered_classes: List[Dict[str, Any]]
    """List of discovered classes as serializable dicts."""

    reflection_report: str
    """Markdown reflection report analyzing the discovered classes."""

    total_documents: int
    """Total number of documents processed."""

    num_clusters: int
    """Number of clusters found."""

    num_failed_embeddings: int
    """Number of documents that failed embedding generation."""

    num_successful_schemas: int
    """Number of clusters with successful schema generation."""

    num_failed_schemas: int
    """Number of clusters where schema generation failed."""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "discovered_classes": self.discovered_classes,
            "reflection_report": self.reflection_report,
            "total_documents": self.total_documents,
            "num_clusters": self.num_clusters,
            "num_failed_embeddings": self.num_failed_embeddings,
            "num_successful_schemas": self.num_successful_schemas,
            "num_failed_schemas": self.num_failed_schemas,
        }


class MultiDocumentDiscovery:
    """
    Orchestrates multi-document discovery: embed → cluster → analyze → reflect.

    Can be used either as a high-level orchestrator (run_full_pipeline) or
    step-by-step for integration with Step Functions.

    Example (high-level):
        >>> discovery = MultiDocumentDiscovery(region="us-east-1")
        >>> result = discovery.run_full_pipeline(
        ...     bucket="my-bucket",
        ...     prefix="documents/",
        ... )

    Example (step-by-step for Lambda handlers):
        >>> discovery = MultiDocumentDiscovery(region="us-east-1")
        >>> keys = discovery.list_documents(bucket, prefix)
        >>> embedding_result = discovery.generate_embeddings(bucket, keys)
        >>> cluster_result = discovery.cluster_documents(embedding_result)
        >>> for cluster_id in cluster_result.get_cluster_ids():
        ...     discovered = discovery.analyze_cluster(cluster_id, ...)
    """

    # Supported document file extensions
    SUPPORTED_EXTENSIONS = {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".tiff",
        ".tif",
        ".webp",
    }

    def __init__(
        self,
        region: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        bedrock_client: Optional[BedrockClient] = None,
    ):
        """
        Initialize multi-document discovery.

        Args:
            region: AWS region
            config: Discovery configuration dict (from IDPConfig.discovery.multi_document)
            bedrock_client: Optional pre-configured BedrockClient
        """
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.config = config or {}
        self.bedrock_client = bedrock_client or BedrockClient(region=self.region)

        # Extract config values with defaults
        self.embedding_model_id = self.config.get(
            "embedding_model_id", "us.cohere.embed-v4:0"
        )
        self.analysis_model_id = self.config.get(
            "analysis_model_id", "us.anthropic.claude-sonnet-4-6"
        )
        self.max_documents = self.config.get("max_documents", 500)
        self.min_cluster_size = self.config.get("min_cluster_size", 2)
        self.num_sample_documents = self.config.get("num_sample_documents", 3)
        self.max_concurrent_embeddings = self.config.get("max_concurrent_embeddings", 5)
        self.max_concurrent_clusters = self.config.get("max_concurrent_clusters", 3)
        self.max_sample_size = self.config.get("max_sample_size", 5)

        # Initialize services
        self.embedding_service = EmbeddingService(
            bedrock_client=self.bedrock_client,
            model_id=self.embedding_model_id,
            max_concurrent=self.max_concurrent_embeddings,
        )
        self.clustering_service = ClusteringService(
            min_cluster_size=self.min_cluster_size,
            num_sample_documents=self.num_sample_documents,
        )
        self.discovery_agent = DiscoveryAgent(
            analysis_model_id=self.analysis_model_id,
            bedrock_client=self.bedrock_client,
            max_sample_size=self.max_sample_size,
            max_workers=self.max_concurrent_clusters,
            system_prompt=self.config.get("system_prompt"),
            region=self.region,
        )

    # ---- Step 1: List Documents ----

    def list_documents(
        self,
        bucket: str,
        prefix: str,
        max_documents: Optional[int] = None,
    ) -> List[str]:
        """
        List document files in an S3 location.

        Args:
            bucket: S3 bucket name
            prefix: S3 key prefix
            max_documents: Maximum documents to return (safety limit)

        Returns:
            List of S3 keys for supported document files

        Raises:
            ValueError: If no supported documents found or max exceeded
        """
        max_docs = max_documents or self.max_documents

        s3_client = boto3.client("s3", region_name=self.region)
        keys: List[str] = []

        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                ext = os.path.splitext(key.lower())[1]
                if ext in self.SUPPORTED_EXTENSIONS:
                    keys.append(key)
                    if len(keys) > max_docs:
                        raise ValueError(
                            f"Too many documents ({len(keys)}+). "
                            f"Maximum is {max_docs}. "
                            f"Use a more specific prefix to narrow the scope."
                        )

        if not keys:
            raise ValueError(
                f"No supported documents found in s3://{bucket}/{prefix}. "
                f"Supported file types: {', '.join(sorted(self.SUPPORTED_EXTENSIONS))}"
            )

        logger.info(f"Found {len(keys)} documents in s3://{bucket}/{prefix}")
        return keys

    # ---- Step 2: Generate Embeddings ----

    def generate_embeddings(
        self,
        bucket: str,
        s3_keys: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> EmbeddingResult:
        """
        Generate embeddings for all documents.

        Args:
            bucket: S3 bucket name
            s3_keys: List of S3 keys
            progress_callback: Optional progress callback

        Returns:
            EmbeddingResult with embeddings matrix and metadata
        """
        return self.embedding_service.embed_document_images(
            bucket=bucket,
            s3_keys=s3_keys,
            progress_callback=progress_callback,
        )

    # ---- Step 3: Cluster Documents ----

    def cluster_documents(
        self,
        embedding_result: EmbeddingResult,
    ) -> ClusterResult:
        """
        Cluster documents based on embeddings.

        Args:
            embedding_result: Result from generate_embeddings()

        Returns:
            ClusterResult with cluster assignments
        """
        return self.clustering_service.cluster(embedding_result.embeddings)

    # ---- Step 4: Analyze Cluster (one per Step Functions Map iteration) ----

    def analyze_cluster(
        self,
        cluster_id: int,
        cluster_result: ClusterResult,
        images: Dict[int, bytes],
    ) -> DiscoveredClass:
        """
        Analyze a single cluster to generate a JSON Schema.

        Suitable for a Step Functions Map state iteration.

        Args:
            cluster_id: Cluster to analyze
            cluster_result: Full clustering result
            images: Mapping of document index -> image bytes

        Returns:
            DiscoveredClass with the generated schema
        """
        return self.discovery_agent.analyze_cluster(
            cluster_id=cluster_id,
            cluster_result=cluster_result,
            images=images,
            clustering_service=self.clustering_service,
        )

    # ---- Step 5: Reflect ----

    def reflect(
        self,
        discovered_classes: List[DiscoveredClass],
    ) -> str:
        """
        Generate a reflection report on the discovered classes.

        Args:
            discovered_classes: List of DiscoveredClass from analysis

        Returns:
            Markdown reflection report
        """
        return self.discovery_agent.reflect(discovered_classes)

    # ---- Step 6: Save Results ----

    def save_to_config(
        self,
        discovered_classes: List[DiscoveredClass],
        config_version: str,
        input_bucket: str,
        input_prefix: str,
    ) -> List[str]:
        """
        Save discovered classes to a configuration version.

        Uses the existing ClassesDiscovery._merge_and_save_class pattern
        to merge schemas into the config version in DynamoDB.

        Args:
            discovered_classes: Classes to save
            config_version: Target configuration version
            input_bucket: S3 bucket (for ClassesDiscovery initialization)
            input_prefix: S3 prefix (for ClassesDiscovery initialization)

        Returns:
            List of saved class names
        """
        from idp_common.discovery.classes_discovery import ClassesDiscovery

        saved_classes = []

        for dc in discovered_classes:
            if dc.error:
                logger.warning(f"Skipping cluster {dc.cluster_id} (error: {dc.error})")
                continue

            try:
                # Use existing ClassesDiscovery for config saving
                classes_discovery = ClassesDiscovery(
                    input_bucket=input_bucket,
                    input_prefix=input_prefix,
                    region=self.region,
                    version=config_version,
                )

                # Merge the discovered class into the config version
                classes_discovery._merge_and_save_class(dc.json_schema)
                # Report the id that was actually written: the merge normalizes
                # the schema in place, while dc.classification is the raw LLM
                # label — preferring it would name a class the config does not
                # contain ("Bank Statement" vs the saved "Bank-Statement").
                class_name = (
                    dc.json_schema.get("$id")
                    or dc.json_schema.get("x-aws-idp-document-type")
                    or sanitize_class_name(dc.classification)
                    or f"cluster-{dc.cluster_id}"
                )
                saved_classes.append(class_name)
                logger.info(f"Saved class '{class_name}' from cluster {dc.cluster_id}")

            except Exception as e:
                logger.error(f"Failed to save class from cluster {dc.cluster_id}: {e}")

        return saved_classes

    # ---- Full Pipeline ----

    def run_full_pipeline(
        self,
        bucket: str,
        prefix: str,
        config_version: Optional[str] = None,
        progress_callback: Optional[Callable[[str, Any], None]] = None,
    ) -> MultiDocDiscoveryResult:
        """
        Run the complete multi-document discovery pipeline.

        Pipeline steps:
        1. List documents in S3
        2. Generate embeddings for all documents
        3. Cluster documents by similarity
        4. Analyze each cluster with Strands agent
        5. Generate reflection report
        6. Optionally save to config version

        Args:
            bucket: S3 bucket name
            prefix: S3 key prefix containing documents
            config_version: Optional config version to save results to
            progress_callback: Optional callable(step_name, step_data) for status updates

        Returns:
            MultiDocDiscoveryResult with all outputs
        """

        def _update(step: str, data: Any = None):
            if progress_callback:
                try:
                    progress_callback(step, data)
                except Exception:
                    pass

        # Step 1: List documents
        _update("listing_documents", {"bucket": bucket, "prefix": prefix})
        s3_keys = self.list_documents(bucket, prefix)
        _update("documents_found", {"count": len(s3_keys)})

        # Step 2: Generate embeddings
        _update("generating_embeddings", {"total": len(s3_keys)})
        embedding_result = self.generate_embeddings(
            bucket=bucket,
            s3_keys=s3_keys,
            progress_callback=lambda done, total: _update(
                "embedding_progress", {"done": done, "total": total}
            ),
        )
        _update("embeddings_complete", embedding_result.to_serializable())

        # Step 3: Cluster
        _update("clustering", {"num_documents": len(embedding_result.valid_keys)})
        cluster_result = self.cluster_documents(embedding_result)
        _update("clustering_complete", cluster_result.to_serializable())

        # Step 4: Prepare images for agent analysis
        _update("preparing_images")
        images = self._load_images_for_analysis(bucket, embedding_result.valid_keys)

        # Step 5: Analyze clusters
        _update(
            "analyzing_clusters",
            {"total": cluster_result.num_clusters},
        )
        discovered_classes = self.discovery_agent.analyze_clusters(
            cluster_result=cluster_result,
            images=images,
            clustering_service=self.clustering_service,
            progress_callback=lambda done, total, cls: _update(
                "cluster_analysis_progress",
                {"done": done, "total": total, "classification": cls},
            ),
        )
        _update(
            "analysis_complete",
            {"classes": [dc.to_dict() for dc in discovered_classes]},
        )

        # Step 6: Reflect
        _update("reflecting")
        reflection_report = self.reflect(discovered_classes)
        _update("reflection_complete")

        # Step 7: Optionally save to config
        if config_version:
            _update("saving_to_config", {"version": config_version})
            self.save_to_config(discovered_classes, config_version, bucket, prefix)
            _update("save_complete")

        # Build result
        num_successful = sum(1 for dc in discovered_classes if not dc.error)
        num_failed = sum(1 for dc in discovered_classes if dc.error)

        result = MultiDocDiscoveryResult(
            discovered_classes=[dc.to_dict() for dc in discovered_classes],
            reflection_report=reflection_report,
            total_documents=len(s3_keys),
            num_clusters=cluster_result.num_clusters,
            num_failed_embeddings=len(embedding_result.failed_keys),
            num_successful_schemas=num_successful,
            num_failed_schemas=num_failed,
        )

        _update("pipeline_complete", result.to_dict())
        return result

    # ---- Local file operations ----

    def list_local_documents(
        self,
        document_dir: Optional[str] = None,
        document_paths: Optional[List[str]] = None,
        max_documents: Optional[int] = None,
    ) -> List[str]:
        """
        List document files from a local directory or explicit paths.

        Args:
            document_dir: Directory to scan for documents (recursive)
            document_paths: Explicit list of file paths
            max_documents: Maximum documents to return (safety limit)

        Returns:
            List of absolute file paths for supported documents

        Raises:
            ValueError: If no supported documents found, or neither dir/paths given
        """
        max_docs = max_documents or self.max_documents
        paths: List[str] = []

        if document_paths:
            for p in document_paths:
                fp = Path(p).resolve()
                if not fp.exists():
                    logger.warning(f"File not found, skipping: {p}")
                    continue
                if fp.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                    paths.append(str(fp))
                else:
                    logger.warning(f"Unsupported file type, skipping: {p}")

        elif document_dir:
            dir_path = Path(document_dir).resolve()
            if not dir_path.is_dir():
                raise ValueError(f"Not a directory: {document_dir}")
            for fp in sorted(dir_path.rglob("*")):
                if fp.is_file() and fp.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                    paths.append(str(fp))
                    if len(paths) > max_docs:
                        raise ValueError(
                            f"Too many documents ({len(paths)}+). "
                            f"Maximum is {max_docs}. "
                            f"Use a more specific directory or explicit paths."
                        )
        else:
            raise ValueError("Either document_dir or document_paths must be provided.")

        if not paths:
            raise ValueError(
                f"No supported documents found. "
                f"Supported file types: {', '.join(sorted(self.SUPPORTED_EXTENSIONS))}"
            )

        logger.info(f"Found {len(paths)} local documents")
        return paths

    def generate_embeddings_local(
        self,
        file_paths: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> EmbeddingResult:
        """
        Generate embeddings for local document files.

        Reads each file, renders PDFs to images, and generates embeddings
        using the configured Bedrock model.

        Args:
            file_paths: List of local file paths
            progress_callback: Optional progress callback

        Returns:
            EmbeddingResult with embeddings matrix and metadata.
            valid_keys contains the file paths that succeeded.
        """
        images: List[bytes] = []
        path_mapping: List[str] = []

        for fp in file_paths:
            try:
                raw_bytes = Path(fp).read_bytes()
                if fp.lower().endswith(".pdf"):
                    rendered = self.embedding_service._render_pdf_first_page(raw_bytes)
                    if rendered:
                        images.append(rendered)
                        path_mapping.append(fp)
                    else:
                        logger.warning(f"Failed to render PDF: {fp}")
                else:
                    compressed = self.embedding_service._compress_image_bytes(raw_bytes)
                    if compressed:
                        images.append(compressed)
                        path_mapping.append(fp)
                    else:
                        logger.warning(f"Failed to compress image: {fp}")
            except Exception as e:
                logger.warning(f"Failed to read file {fp}: {e}")

        if not images:
            import numpy as np

            return EmbeddingResult(
                embeddings=np.array([]),
                valid_keys=[],
                failed_keys=list(file_paths),
                embedding_dim=0,
            )

        logger.info(f"Prepared {len(images)} local images for embedding")

        embeddings_array, valid_indices = (
            self.embedding_service.embed_images_from_bytes(
                images=images,
                progress_callback=progress_callback,
            )
        )

        valid_keys = [path_mapping[i] for i in valid_indices]
        failed_set = set(valid_keys)
        failed_keys = [fp for fp in file_paths if fp not in failed_set]

        import numpy as np

        embedding_dim = embeddings_array.shape[1] if embeddings_array.size > 0 else 0

        return EmbeddingResult(
            embeddings=embeddings_array,
            valid_keys=valid_keys,
            failed_keys=failed_keys,
            embedding_dim=embedding_dim,
        )

    def _load_local_images(
        self,
        file_paths: List[str],
    ) -> Dict[int, bytes]:
        """
        Load local document images into memory for agent analysis.

        Args:
            file_paths: Local file paths (in same order as embeddings)

        Returns:
            Mapping of document index -> image bytes
        """
        images: Dict[int, bytes] = {}

        for idx, fp in enumerate(file_paths):
            try:
                raw_bytes = Path(fp).read_bytes()
                if fp.lower().endswith(".pdf"):
                    rendered = self.embedding_service._render_pdf_first_page(raw_bytes)
                    if rendered:
                        images[idx] = rendered
                else:
                    images[idx] = raw_bytes
            except Exception as e:
                logger.warning(f"Failed to load image for doc {idx} ({fp}): {e}")

        logger.info(f"Loaded {len(images)} local images for analysis")
        return images

    def run_local_pipeline(
        self,
        document_dir: Optional[str] = None,
        document_paths: Optional[List[str]] = None,
        config_version: Optional[str] = None,
        progress_callback: Optional[Callable[[str, Any], None]] = None,
    ) -> MultiDocDiscoveryResult:
        """
        Run the complete multi-document discovery pipeline on local files.

        Pipeline steps:
        1. List local documents
        2. Generate embeddings for all documents
        3. Cluster documents by similarity
        4. Analyze each cluster with Strands agent
        5. Generate reflection report
        6. Optionally save to config version

        Args:
            document_dir: Directory containing documents
            document_paths: Explicit list of document file paths
            config_version: Optional config version to save results to
            progress_callback: Optional callable(step_name, step_data) for status updates

        Returns:
            MultiDocDiscoveryResult with all outputs
        """

        def _update(step: str, data: Any = None):
            if progress_callback:
                try:
                    progress_callback(step, data)
                except Exception:
                    pass

        # Step 1: List local documents
        _update("listing_documents", {"dir": document_dir, "paths": document_paths})
        file_paths = self.list_local_documents(
            document_dir=document_dir,
            document_paths=document_paths,
        )
        _update("documents_found", {"count": len(file_paths)})

        # Step 2: Generate embeddings from local files
        _update("generating_embeddings", {"total": len(file_paths)})
        embedding_result = self.generate_embeddings_local(
            file_paths=file_paths,
            progress_callback=lambda done, total: _update(
                "embedding_progress", {"done": done, "total": total}
            ),
        )
        _update("embeddings_complete", embedding_result.to_serializable())

        # Step 3: Cluster
        _update("clustering", {"num_documents": len(embedding_result.valid_keys)})
        cluster_result = self.cluster_documents(embedding_result)
        _update("clustering_complete", cluster_result.to_serializable())

        # Step 4: Prepare images for agent analysis
        _update("preparing_images")
        images = self._load_local_images(embedding_result.valid_keys)

        # Step 5: Analyze clusters
        _update(
            "analyzing_clusters",
            {"total": cluster_result.num_clusters},
        )
        discovered_classes = self.discovery_agent.analyze_clusters(
            cluster_result=cluster_result,
            images=images,
            clustering_service=self.clustering_service,
            progress_callback=lambda done, total, cls: _update(
                "cluster_analysis_progress",
                {"done": done, "total": total, "classification": cls},
            ),
        )
        _update(
            "analysis_complete",
            {"classes": [dc.to_dict() for dc in discovered_classes]},
        )

        # Step 6: Reflect
        _update("reflecting")
        reflection_report = self.reflect(discovered_classes)
        _update("reflection_complete")

        # Step 7: Optionally save to config
        if config_version:
            _update("saving_to_config", {"version": config_version})
            self.save_to_config(discovered_classes, config_version, "local", "local")
            _update("save_complete")

        # Build result
        num_successful = sum(1 for dc in discovered_classes if not dc.error)
        num_failed = sum(1 for dc in discovered_classes if dc.error)

        result = MultiDocDiscoveryResult(
            discovered_classes=[dc.to_dict() for dc in discovered_classes],
            reflection_report=reflection_report,
            total_documents=len(file_paths),
            num_clusters=cluster_result.num_clusters,
            num_failed_embeddings=len(embedding_result.failed_keys),
            num_successful_schemas=num_successful,
            num_failed_schemas=num_failed,
        )

        _update("pipeline_complete", result.to_dict())
        return result

    # ---- S3 image loading (for Lambda handlers) ----

    def _load_images_for_analysis(
        self,
        bucket: str,
        s3_keys: List[str],
    ) -> Dict[int, bytes]:
        """
        Load document images from S3 into memory for agent analysis.

        Args:
            bucket: S3 bucket
            s3_keys: S3 keys (in same order as embeddings)

        Returns:
            Mapping of document index -> image bytes
        """
        s3_client = boto3.client("s3", region_name=self.region)
        images: Dict[int, bytes] = {}

        for idx, key in enumerate(s3_keys):
            try:
                response = s3_client.get_object(Bucket=bucket, Key=key)
                raw_bytes = response["Body"].read()

                # For PDFs, render first page
                if key.lower().endswith(".pdf"):
                    rendered = self.embedding_service._render_pdf_first_page(raw_bytes)
                    if rendered:
                        images[idx] = rendered
                else:
                    images[idx] = raw_bytes

            except Exception as e:
                logger.warning(f"Failed to load image for doc {idx} ({key}): {e}")

        logger.info(f"Loaded {len(images)} images for analysis")
        return images
