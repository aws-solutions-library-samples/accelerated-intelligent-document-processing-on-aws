# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Discovery agent for multi-document discovery.

Uses Strands agents SDK to analyze document clusters and generate JSON Schemas.
Each cluster gets a fresh agent instance with tools for exploring the cluster
(viewing documents, analyzing cluster structure) and generates a normalized
schema applicable across all documents in the cluster.

Adapted from MR #502's ClusterSchemaGenerator with full integration into
idp_common patterns and BedrockClient infrastructure.
"""

import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field

from idp_common import image
from idp_common.bedrock.client import BedrockClient
from idp_common.discovery.clustering_service import ClusteringService, ClusterResult

logger = logging.getLogger(__name__)


# --- Pydantic models for structured output ---


class ClusterSchemaOutput(BaseModel):
    """Structured output from the discovery agent for a single cluster."""

    json_schema: Dict[str, Any] = Field(
        description="JSON Schema (draft 2020-12) describing the document class"
    )
    classification: str = Field(
        description="Short document type classification (e.g., 'W2', 'Invoice', 'BankStatement')"
    )


class DiscoveredClass:
    """Represents a discovered document class from a cluster."""

    def __init__(
        self,
        cluster_id: int,
        classification: str,
        json_schema: Dict[str, Any],
        document_count: int,
        sample_doc_ids: List[int],
        error: Optional[str] = None,
    ):
        self.cluster_id = cluster_id
        self.classification = classification
        self.json_schema = json_schema
        self.document_count = document_count
        self.sample_doc_ids = sample_doc_ids
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        """Convert to serializable dict."""
        return {
            "cluster_id": self.cluster_id,
            "classification": self.classification,
            "json_schema": self.json_schema,
            "document_count": self.document_count,
            "sample_doc_ids": self.sample_doc_ids,
            "error": self.error,
        }


# --- Strands tool classes ---


class ClusterAnalysisTool:
    """Tool for the Strands agent to explore cluster structure."""

    def __init__(
        self,
        cluster_result: ClusterResult,
        clustering_service: ClusteringService,
    ):
        self.cluster_result = cluster_result
        self.clustering_service = clustering_service

    def get_cluster_docids_by_distance_to_centroid(
        self, cluster_id: int, max_docs: int = 20
    ) -> Dict[str, Any]:
        """
        Get document IDs in a cluster sorted by distance to the cluster centroid.

        Args:
            cluster_id: The cluster ID to analyze
            max_docs: Maximum number of document IDs to return

        Returns:
            Dict with cluster info and document IDs sorted by distance to centroid
        """
        docs = self.clustering_service.get_docs_by_distance_to_centroid(
            self.cluster_result, cluster_id, max_docs=max_docs
        )
        cluster_size = self.cluster_result.cluster_sizes.get(cluster_id, 0)

        return {
            "status": "success",
            "content": [
                {
                    "json": {
                        "cluster_id": cluster_id,
                        "total_documents": cluster_size,
                        "returned_documents": len(docs),
                        "documents": docs,
                    }
                }
            ],
        }


class DocumentVisualTool:
    """Tool for the Strands agent to view document images."""

    def __init__(self, images: Dict[int, bytes]):
        """
        Args:
            images: Mapping of document index to image bytes
        """
        self.images = images

    def get_document_image(self, doc_id: int) -> Any:
        """
        Get a document image for visual inspection.

        Args:
            doc_id: Document ID (index into the dataset)

        Returns:
            Image content block for the Strands agent
        """
        if doc_id not in self.images:
            return {
                "status": "error",
                "content": [{"text": f"Document {doc_id} not found"}],
            }

        image_bytes = self.images[doc_id]

        # Always compress to JPEG (ensures consistent format for Claude)
        compressed = self._compress_image(image_bytes, force_jpeg=True)
        if compressed is None:
            return {
                "status": "error",
                "content": [{"text": f"Failed to process document {doc_id}"}],
            }

        # Strands/Converse API expects raw bytes in source.bytes, not base64 string
        return {
            "status": "success",
            "content": [
                {
                    "image": {
                        "format": "jpeg",
                        "source": {"bytes": compressed},
                    }
                }
            ],
        }

    # Max pixel dimension for Claude Converse API images. 2,000 rather than the
    # old 2,048: 2,000 is the documented cap that applies once a request carries
    # more than 20 images, and discovery attaches every page of a sample document,
    # so it routinely crosses that threshold. 2,048 was 48px over and would be
    # rejected outright (#994). Sourced from idp_common.image so the two agree.
    MAX_IMAGE_DIMENSION = image.BEDROCK_MANY_IMAGE_MAX_DIMENSION

    def _compress_image(
        self,
        image_bytes: bytes,
        max_size: int = 3 * 1024 * 1024,
        force_jpeg: bool = False,
    ) -> Optional[bytes]:
        """Compress image to within size and pixel dimension limits for Claude.

        Args:
            image_bytes: Raw image bytes (any format)
            max_size: Maximum file size in bytes
            force_jpeg: If True, always convert to JPEG (required when format="jpeg" is declared)
        """
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(image_bytes))
            width, height = img.size

            # Always resize if pixel dimensions exceed limit (Claude can't process large images)
            needs_resize = (
                width > self.MAX_IMAGE_DIMENSION or height > self.MAX_IMAGE_DIMENSION
            )

            # If force_jpeg, always convert (even small images) to avoid MIME type mismatch
            if not force_jpeg and not needs_resize and len(image_bytes) <= max_size:
                return image_bytes

            if img.mode != "RGB":
                img = img.convert("RGB")

            if needs_resize:
                img.thumbnail(
                    (self.MAX_IMAGE_DIMENSION, self.MAX_IMAGE_DIMENSION),
                    Image.Resampling.LANCZOS,
                )
                logger.debug(
                    f"Resized image from {width}x{height} to {img.size[0]}x{img.size[1]}"
                )

            for quality in [70, 50, 30]:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality)
                result = buf.getvalue()
                if len(result) <= max_size:
                    return result

            # Resize further if still too large
            width, height = img.size
            for scale in [0.5, 0.25]:
                resized = img.resize((int(width * scale), int(height * scale)))
                buf = io.BytesIO()
                resized.save(buf, format="JPEG", quality=50)
                result = buf.getvalue()
                if len(result) <= max_size:
                    return result

        except Exception as e:
            logger.warning(f"Image compression failed: {e}")

        return None


class DiscoveryAgent:
    """
    Strands-based agent that analyzes document clusters to generate schemas.

    Creates a fresh Strands Agent per cluster with isolated conversation context.
    The agent uses tools to explore cluster structure (view documents, analyze
    distances) and generates a JSON Schema for each document type.

    Example:
        >>> agent = DiscoveryAgent(
        ...     analysis_model_id="us.anthropic.claude-sonnet-4-6",
        ...     bedrock_client=BedrockClient(region="us-east-1"),
        ... )
        >>> results = agent.analyze_clusters(
        ...     cluster_result=cluster_result,
        ...     images=document_images,
        ...     clustering_service=clustering_service,
        ... )
    """

    def __init__(
        self,
        analysis_model_id: str = "us.anthropic.claude-sonnet-4-6",
        bedrock_client: Optional[BedrockClient] = None,
        max_sample_size: int = 5,
        max_workers: int = 3,
        system_prompt: Optional[str] = None,
        region: Optional[str] = None,
    ):
        """
        Initialize the discovery agent.

        Args:
            analysis_model_id: Bedrock model ID for schema generation agent
            bedrock_client: Optional BedrockClient (used for region info)
            max_sample_size: Maximum documents to sample per cluster
            max_workers: Maximum parallel cluster analysis workers
            system_prompt: Optional custom system prompt (overrides template)
            region: AWS region for Strands BedrockModel
        """
        self.analysis_model_id = analysis_model_id
        self.bedrock_client = bedrock_client
        self.max_sample_size = max_sample_size
        self.max_workers = max_workers
        self.custom_system_prompt = system_prompt
        self.region = region or (bedrock_client.region if bedrock_client else None)

    def _load_system_prompt(self) -> str:
        """Load and render the extraction prompt template."""
        if self.custom_system_prompt:
            return self.custom_system_prompt

        template_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "prompts"
        )
        template_loader = FileSystemLoader(searchpath=template_dir)
        # nosec B701 - autoescape intentionally disabled.
        # These Jinja2 templates render LLM prompt text (plain text), not HTML.
        # Enabling HTML autoescape would corrupt prompt characters (&, <, >) and
        # degrade LLM behavior without providing any security benefit, since the
        # output is sent to Bedrock as a prompt, not rendered in a browser.
        # Template inputs (max_sample_size) are developer-controlled integers.
        env = Environment(loader=template_loader, autoescape=False)  # nosec B701
        template = env.get_template("extraction_prompt.jinja2")
        return template.render(max_sample_size=self.max_sample_size)

    def _create_agent(
        self,
        cluster_analysis_tool: ClusterAnalysisTool,
        document_visual_tool: DocumentVisualTool,
    ) -> Any:
        """
        Create a fresh Strands Agent instance for cluster analysis.

        Each cluster gets a new agent with clean conversation history
        to avoid context pollution between clusters.
        """
        from strands import Agent, tool
        from strands.agent.conversation_manager import NullConversationManager

        system_prompt = self._load_system_prompt()

        # Wrap tools as Strands tools using @tool decorator pattern
        @tool
        def get_cluster_docids_by_distance_to_centroid(
            cluster_id: int, max_docs: int = 20
        ) -> Dict:
            """
            Get document IDs in a cluster sorted by distance to the cluster centroid.
            Returns the most representative documents first (closest to center).

            Args:
                cluster_id: The cluster ID to analyze
                max_docs: Maximum number of document IDs to return

            Returns:
                Dict with cluster info and document IDs sorted by distance
            """
            return cluster_analysis_tool.get_cluster_docids_by_distance_to_centroid(
                cluster_id, max_docs
            )

        @tool
        def get_document_image(doc_id: int) -> Any:
            """
            Get a document image for visual inspection.
            Use this to examine the structure and fields of a document.

            Args:
                doc_id: Document ID (from get_cluster_docids_by_distance_to_centroid)

            Returns:
                The document image for visual analysis
            """
            return document_visual_tool.get_document_image(doc_id)

        # Create Strands agent with Bedrock model
        agent_kwargs: Dict[str, Any] = {
            "conversation_manager": NullConversationManager(),
            "tools": [
                get_cluster_docids_by_distance_to_centroid,
                get_document_image,
            ],
            "system_prompt": system_prompt,
        }

        # Build a BedrockModel via the shared factory so the boto session
        # honors BEDROCK_ASSUME_ROLE_ARN (cross-account hub) when configured.
        from idp_common.agents.common.strands_bedrock_model import (
            create_strands_bedrock_model,
        )

        agent_kwargs["model"] = create_strands_bedrock_model(self.analysis_model_id)

        return Agent(**agent_kwargs)

    def analyze_cluster(
        self,
        cluster_id: int,
        cluster_result: ClusterResult,
        images: Dict[int, bytes],
        clustering_service: ClusteringService,
    ) -> DiscoveredClass:
        """
        Analyze a single cluster to generate a JSON Schema.

        Args:
            cluster_id: Cluster to analyze
            cluster_result: Full clustering result
            images: Mapping of document index -> image bytes
            clustering_service: ClusteringService for sampling methods

        Returns:
            DiscoveredClass with the generated schema
        """
        cluster_size = cluster_result.cluster_sizes.get(cluster_id, 0)
        logger.info(f"Analyzing cluster {cluster_id} ({cluster_size} documents)")

        try:
            # Create tools
            cluster_tool = ClusterAnalysisTool(cluster_result, clustering_service)
            visual_tool = DocumentVisualTool(images)

            # Create fresh agent
            agent = self._create_agent(cluster_tool, visual_tool)

            # Run the agent
            message = f"Analyze documents from cluster {cluster_id}."
            response = agent(message, structured_output_model=ClusterSchemaOutput)

            result = response.structured_output.model_dump()

            sample_ids = clustering_service.sample_cluster(
                cluster_result, cluster_id, self.max_sample_size
            )

            return DiscoveredClass(
                cluster_id=cluster_id,
                classification=result.get("classification", f"Cluster_{cluster_id}"),
                json_schema=result.get("json_schema", {}),
                document_count=cluster_size,
                sample_doc_ids=sample_ids,
            )

        except Exception as e:
            logger.error(f"Failed to analyze cluster {cluster_id}: {e}", exc_info=True)
            return DiscoveredClass(
                cluster_id=cluster_id,
                classification=f"Error_Cluster_{cluster_id}",
                json_schema={"error": str(e)},
                document_count=cluster_size,
                sample_doc_ids=[],
                error=str(e),
            )

    def analyze_clusters(
        self,
        cluster_result: ClusterResult,
        images: Dict[int, bytes],
        clustering_service: ClusteringService,
        sequential: bool = False,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> List[DiscoveredClass]:
        """
        Analyze all clusters to generate schemas.

        Args:
            cluster_result: Result from ClusteringService.cluster()
            images: Mapping of document index -> image bytes
            clustering_service: ClusteringService for methods
            sequential: If True, process one at a time (for debugging)
            progress_callback: Optional callable(completed, total, cluster_classification)

        Returns:
            List of DiscoveredClass for each cluster
        """
        cluster_ids = cluster_result.get_cluster_ids()
        total = len(cluster_ids)
        logger.info(f"Analyzing {total} clusters")

        if sequential:
            return self._analyze_sequential(
                cluster_ids,
                cluster_result,
                images,
                clustering_service,
                progress_callback,
                total,
            )
        else:
            return self._analyze_parallel(
                cluster_ids,
                cluster_result,
                images,
                clustering_service,
                progress_callback,
                total,
            )

    def _analyze_sequential(
        self,
        cluster_ids: List[int],
        cluster_result: ClusterResult,
        images: Dict[int, bytes],
        clustering_service: ClusteringService,
        progress_callback: Optional[Callable],
        total: int,
    ) -> List[DiscoveredClass]:
        """Process clusters sequentially."""
        results = []
        for i, cluster_id in enumerate(cluster_ids):
            discovered = self.analyze_cluster(
                cluster_id, cluster_result, images, clustering_service
            )
            results.append(discovered)
            if progress_callback:
                progress_callback(i + 1, total, discovered.classification)
        return results

    def _analyze_parallel(
        self,
        cluster_ids: List[int],
        cluster_result: ClusterResult,
        images: Dict[int, bytes],
        clustering_service: ClusteringService,
        progress_callback: Optional[Callable],
        total: int,
    ) -> List[DiscoveredClass]:
        """Process clusters in parallel using ThreadPoolExecutor."""
        results: List[Optional[DiscoveredClass]] = [None] * total
        completed = 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_idx = {
                executor.submit(
                    self.analyze_cluster,
                    cluster_id,
                    cluster_result,
                    images,
                    clustering_service,
                ): i
                for i, cluster_id in enumerate(cluster_ids)
            }

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    discovered = future.result()
                    results[idx] = discovered
                    completed += 1
                    if progress_callback:
                        progress_callback(completed, total, discovered.classification)
                except Exception as e:
                    cluster_id = cluster_ids[idx]
                    logger.error(f"Cluster {cluster_id} analysis failed: {e}")
                    results[idx] = DiscoveredClass(
                        cluster_id=cluster_id,
                        classification=f"Error_Cluster_{cluster_id}",
                        json_schema={"error": str(e)},
                        document_count=cluster_result.cluster_sizes.get(cluster_id, 0),
                        sample_doc_ids=[],
                        error=str(e),
                    )
                    completed += 1

        return [r for r in results if r is not None]

    def reflect(
        self,
        discovered_classes: List[DiscoveredClass],
    ) -> str:
        """
        Generate a reflection report on the discovered classes.

        Uses an LLM to analyze the quality and completeness of the
        discovered schemas and provide recommendations.

        Args:
            discovered_classes: List of discovered classes from analyze_clusters()

        Returns:
            Markdown-formatted reflection report
        """
        # Build results dict for template
        results_dict = {}
        for dc in discovered_classes:
            results_dict[str(dc.cluster_id)] = {
                "classification": dc.classification,
                "json_schema": dc.json_schema,
                "document_count": dc.document_count,
            }

        # Load reflection prompt template
        template_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "prompts"
        )
        template_loader = FileSystemLoader(searchpath=template_dir)
        # nosec B701 - autoescape intentionally disabled.
        # This Jinja2 template renders LLM prompt text (plain text), not HTML.
        # Enabling HTML autoescape would corrupt prompt characters (&, <, >) and
        # degrade LLM behavior without providing any security benefit, since the
        # output is sent to Bedrock as a prompt, not rendered in a browser.
        # Template inputs (results_dict) are internally-constructed from
        # clustering results; they are not rendered as HTML anywhere downstream.
        env = Environment(loader=template_loader, autoescape=False)  # nosec B701
        template = env.get_template("reflection_prompt.jinja2")
        prompt_text = template.render(results=results_dict)

        # Use BedrockClient for reflection (not Strands - simple single call)
        if self.bedrock_client is None:
            self.bedrock_client = BedrockClient(region=self.region)

        response = self.bedrock_client.invoke_model(
            model_id=self.analysis_model_id,
            system_prompt="You are an expert document processing engineer providing quality review.",
            content=[{"text": prompt_text}],
            temperature=0.3,
            # None => Bedrock client resolves the model's max output tokens
            # (model_config_limits.yaml); Bedrock's default-when-omitted truncates.
            max_tokens=None,
            context="discovery/reflection",
        )

        return self.bedrock_client.extract_text_from_response(response)
