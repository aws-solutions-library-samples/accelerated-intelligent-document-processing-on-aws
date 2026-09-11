# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Multi-Document Discovery: Clustering handler.

Step Functions step 3: Loads embeddings from S3, clusters documents
using KMeans, and saves cluster data to S3.
"""

import io
import json
import logging
import os

import boto3
import numpy as np

from idp_common.discovery.clustering_service import ClusteringService

from appsync_status import update_status

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

DISCOVERY_BUCKET = os.environ.get("DISCOVERY_BUCKET", "")


def handler(event, context):
    """
    Clustering step: cluster documents by embedding similarity.

    Input:
        jobId: str
        bucket: str
        s3Keys: list[str]
        embeddingsS3Key: str
        validKeys: list[str]

    Returns:
        numClusters: int
        clusterIds: list[int]
        clusterSizes: dict
        clusterDataS3Key: str - S3 key with full cluster data (JSON)
    """
    job_id = event["jobId"]
    embeddings_key = event["embeddingsS3Key"]
    valid_keys = event["validKeys"]

    logger.info(f"Clustering {len(valid_keys)} documents for job {job_id}")

    # Notify UI: CLUSTERING status
    update_status(job_id, "CLUSTERING", current_step="Clustering documents")

    # Load embeddings from S3
    s3_client = boto3.client("s3")
    response = s3_client.get_object(Bucket=DISCOVERY_BUCKET, Key=embeddings_key)
    buf = io.BytesIO(response["Body"].read())
    embeddings = np.load(buf)

    logger.info(f"Loaded embeddings: shape={embeddings.shape}")

    # Cluster
    clustering_service = ClusteringService(
        min_cluster_size=2,
        num_sample_documents=3,
    )
    cluster_result = clustering_service.cluster(embeddings)

    # Save full cluster data to S3 (needed by analyze step). JSON, not pickle —
    # see ClusterResult.to_artifact() for why.
    cluster_data_key = f"multi-doc-discovery/{job_id}/cluster_data.json"
    s3_client.put_object(
        Bucket=DISCOVERY_BUCKET,
        Key=cluster_data_key,
        Body=json.dumps(cluster_result.to_artifact()).encode("utf-8"),
        ContentType="application/json",
    )

    logger.info(
        f"Clustering complete: {cluster_result.num_clusters} clusters, "
        f"saved to {cluster_data_key}"
    )

    # Notify UI: ANALYZING status with clusters found
    # (reported here rather than in analyze_handler because analyze runs in
    # parallel via Map state, and cluster_handler is the single invocation
    # that transitions into the analyze phase)
    update_status(
        job_id, "ANALYZING",
        current_step="Analyzing clusters",
        clusters_found=cluster_result.num_clusters,
    )

    return {
        "numClusters": cluster_result.num_clusters,
        "clusterIds": cluster_result.get_cluster_ids(),
        "clusterSizes": {
            str(k): v
            for k, v in cluster_result.cluster_sizes.items()
            if k >= 0
        },
        "clusterDataS3Key": cluster_data_key,
    }
