# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Multi-Document Discovery: Embedding Generation handler.

Step Functions step 2: Generates multimodal embeddings for all documents
using the configured Bedrock embedding model. Saves embeddings to S3
(too large for Step Functions state).
"""

import json
import logging
import os

import boto3
import numpy as np

from idp_common.bedrock.client import BedrockClient
from idp_common.config import ConfigurationReader
from idp_common.discovery.embedding_service import (
    EmbeddingService,
    default_embedding_model_id,
)

from appsync_status import update_status

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

DISCOVERY_BUCKET = os.environ.get("DISCOVERY_BUCKET", "")


def handler(event, context):
    """
    Embedding step: generate multimodal embeddings for all documents.

    Input:
        jobId: str
        bucket: str
        s3Keys: list[str]
        configVersion: str

    Returns:
        embeddingsS3Key: str - S3 key where embeddings numpy array is stored
        validKeys: list[str] - keys that were successfully embedded
        failedKeys: list[str] - keys that failed
        embeddingDim: int
    """
    job_id = event["jobId"]
    bucket = event["bucket"]
    s3_keys = event["s3Keys"]
    config_version = event.get("configVersion")

    logger.info(f"Generating embeddings for {len(s3_keys)} documents, job {job_id}")

    # Notify UI: EMBEDDING status with total document count
    update_status(
        job_id, "EMBEDDING",
        current_step="Generating embeddings",
        total_documents=len(s3_keys),
    )

    # Load multi_document config
    config = _get_multi_doc_config(config_version)

    # Initialize embedding service
    bedrock_client = BedrockClient(region=os.environ.get("AWS_REGION"))
    # _get_multi_doc_config returns {} when the config cannot be read, so the
    # fallback must be callable from this Region, not the US default.
    embedding_model_id = config.get("embedding_model_id") or default_embedding_model_id(
        os.environ.get("AWS_REGION")
    )
    max_concurrent = int(config.get("max_concurrent_embeddings", 5))

    embedding_service = EmbeddingService(
        bedrock_client=bedrock_client,
        model_id=embedding_model_id,
        max_concurrent=max_concurrent,
        input_type="clustering",  # Use clustering input type for discovery embeddings
    )

    # Generate embeddings
    result = embedding_service.embed_document_images(
        bucket=bucket,
        s3_keys=s3_keys,
    )

    # Save embeddings to S3 (too large for Step Functions state - 256KB limit)
    embeddings_key = f"multi-doc-discovery/{job_id}/embeddings.npy"
    s3_client = boto3.client("s3")

    # Save as numpy binary
    import io

    buf = io.BytesIO()
    np.save(buf, result.embeddings)
    buf.seek(0)
    s3_client.put_object(
        Bucket=DISCOVERY_BUCKET,
        Key=embeddings_key,
        Body=buf.getvalue(),
    )

    logger.info(
        f"Saved embeddings ({result.embeddings.shape}) to s3://{DISCOVERY_BUCKET}/{embeddings_key}"
    )

    return {
        "embeddingsS3Key": embeddings_key,
        "validKeys": result.valid_keys,
        "failedKeys": result.failed_keys,
        "embeddingDim": result.embedding_dim,
        "numValid": len(result.valid_keys),
        "numFailed": len(result.failed_keys),
    }


def _get_multi_doc_config(config_version):
    """Load multi_document discovery config from DynamoDB merged with system defaults."""
    try:
        config_reader = ConfigurationReader()
        full_config = config_reader.get_merged_configuration(
            version=config_version if config_version else None
        )
        multi_doc_config = full_config.get("discovery", {}).get("multi_document", {})
        logger.info(f"Loaded multi_document config: embedding_model_id={multi_doc_config.get('embedding_model_id')}")
        return multi_doc_config
    except Exception as e:
        logger.warning(f"Failed to load config, using defaults: {e}")
        return {}
