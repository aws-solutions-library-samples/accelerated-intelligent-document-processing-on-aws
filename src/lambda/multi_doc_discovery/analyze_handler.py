# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Multi-Document Discovery: Cluster Analysis handler.

Step Functions step 4 (Map iteration): Analyzes a single cluster using
a Strands agent to generate a JSON Schema for the document type.
"""

import io
import json
import logging
import os

import boto3
import numpy as np
from scipy.spatial import KDTree

from idp_common.bedrock.client import BedrockClient
from idp_common.config import ConfigurationReader
from idp_common.discovery.clustering_service import ClusteringService, ClusterResult
from idp_common.discovery.discovery_agent import DiscoveryAgent

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

DISCOVERY_BUCKET = os.environ.get("DISCOVERY_BUCKET", "")


def handler(event, context):
    """
    Analyze a single cluster using the Strands discovery agent.

    Input (from Map state):
        jobId: str
        bucket: str
        clusterId: int
        embeddingsS3Key: str
        clusterDataS3Key: str
        validKeys: list[str]
        configVersion: str

    Returns:
        clusterId: int
        classification: str
        jsonSchema: dict
        documentCount: int
        error: str or None
    """
    job_id = event["jobId"]
    bucket = event["bucket"]
    cluster_id = event["clusterId"]
    embeddings_key = event["embeddingsS3Key"]
    cluster_data_key = event["clusterDataS3Key"]
    valid_keys = event["validKeys"]
    config_version = event.get("configVersion")

    logger.info(f"Analyzing cluster {cluster_id} for job {job_id}")

    s3_client = boto3.client("s3")

    # Load embeddings
    response = s3_client.get_object(Bucket=DISCOVERY_BUCKET, Key=embeddings_key)
    embeddings = np.load(io.BytesIO(response["Body"].read()))

    # Load cluster data. Written as JSON by cluster_handler — deliberately not
    # pickle, which would make this load an arbitrary-code-execution sink.
    response = s3_client.get_object(Bucket=DISCOVERY_BUCKET, Key=cluster_data_key)
    cluster_data = json.loads(response["Body"].read().decode("utf-8"))
    cluster_result = ClusterResult.from_artifact(
        cluster_data, embeddings=embeddings, kdtree=KDTree(embeddings)
    )

    # Load config
    config = _get_multi_doc_config(config_version)

    # Load images for this cluster's documents
    cluster_indices = cluster_result.get_cluster_indices(cluster_id)
    images = _load_cluster_images(s3_client, bucket, valid_keys, cluster_indices)

    # Create and run discovery agent
    bedrock_client = BedrockClient(region=os.environ.get("AWS_REGION"))
    analysis_model_id = config.get(
        "analysis_model_id", "us.anthropic.claude-sonnet-4-6"
    )
    max_sample_size = int(config.get("max_sample_size", 5))

    clustering_service = ClusteringService(
        min_cluster_size=2,
        num_sample_documents=int(config.get("num_sample_documents", 3)),
    )

    agent = DiscoveryAgent(
        analysis_model_id=analysis_model_id,
        bedrock_client=bedrock_client,
        max_sample_size=max_sample_size,
        system_prompt=config.get("system_prompt") or None,
        region=os.environ.get("AWS_REGION"),
    )

    discovered = agent.analyze_cluster(
        cluster_id=cluster_id,
        cluster_result=cluster_result,
        images=images,
        clustering_service=clustering_service,
    )

    result = {
        "clusterId": discovered.cluster_id,
        "classification": discovered.classification,
        "jsonSchema": discovered.json_schema,
        "documentCount": discovered.document_count,
        "sampleDocIds": discovered.sample_doc_ids,
        "error": discovered.error,
    }

    logger.info(
        f"Cluster {cluster_id} analysis complete: "
        f"classification={discovered.classification}, "
        f"error={discovered.error}"
    )

    return result


def _load_cluster_images(s3_client, bucket, valid_keys, cluster_indices):
    """Load and compress images for documents in a specific cluster.

    Images are compressed to fit within Bedrock Converse API limits
    (max ~3.75MB, max 8000x8000 pixels for Claude).
    """
    from idp_common.discovery.embedding_service import EmbeddingService

    images = {}
    # We need a temporary EmbeddingService for PDF rendering and image compression
    bedrock_client = BedrockClient(region=os.environ.get("AWS_REGION"))
    embed_service = EmbeddingService(bedrock_client=bedrock_client)

    for idx in cluster_indices:
        if idx >= len(valid_keys):
            continue
        key = valid_keys[idx]
        try:
            response = s3_client.get_object(Bucket=bucket, Key=key)
            raw_bytes = response["Body"].read()

            if key.lower().endswith(".pdf"):
                rendered = embed_service._render_pdf_first_page(raw_bytes)
                if rendered:
                    images[idx] = rendered
            else:
                # Compress images to fit Converse API limits
                compressed = embed_service._compress_image_bytes(raw_bytes)
                if compressed:
                    images[idx] = compressed
                else:
                    images[idx] = raw_bytes
        except Exception as e:
            logger.warning(f"Failed to load image for doc {idx} ({key}): {e}")

    logger.info(f"Loaded {len(images)} images for cluster analysis")
    return images


def _get_multi_doc_config(config_version):
    """Load multi_document discovery config from DynamoDB merged with system defaults."""
    try:
        config_reader = ConfigurationReader()
        full_config = config_reader.get_merged_configuration(
            version=config_version if config_version else None
        )
        multi_doc_config = full_config.get("discovery", {}).get("multi_document", {})
        logger.info(f"Loaded multi_document config: analysis_model_id={multi_doc_config.get('analysis_model_id')}")
        return multi_doc_config
    except Exception as e:
        logger.warning(f"Failed to load config, using defaults: {e}")
        return {}
