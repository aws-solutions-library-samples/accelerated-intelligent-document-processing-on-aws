# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Multi-Document Discovery: Save Results handler.

Step Functions step 5: Generates reflection report, saves discovered
classes to config version, and returns final results.
"""

import json
import logging
import os

import boto3

from idp_common.bedrock.client import BedrockClient
from idp_common.discovery.discovered_text import clean_text
from idp_common.discovery.discovery_agent import DiscoveredClass, DiscoveryAgent

from appsync_status import update_status

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

DISCOVERY_BUCKET = os.environ.get("DISCOVERY_BUCKET", "")


def handler(event, context):
    """
    Save results: reflection + save to config version.

    Input:
        jobId: str
        bucket: str
        configVersion: str
        analyzeResults: list[dict] - results from Map state
        prefix: str
        embeddingsS3Key: str
        clusterDataS3Key: str

    Returns:
        discoveredClassesJson: str - JSON string of discovered classes
        reflectionReport: str - Markdown reflection report
        savedClasses: list[str] - class names saved to config
    """
    job_id = event["jobId"]
    bucket = event["bucket"]
    config_version = event.get("configVersion")
    analyze_results = event["analyzeResults"]
    prefix = event.get("prefix", "")

    logger.info(
        f"Saving results for job {job_id}: "
        f"{len(analyze_results)} cluster results"
    )

    # Convert analyze results to DiscoveredClass objects
    discovered_classes = []
    for result in analyze_results:
        dc = DiscoveredClass(
            cluster_id=result["clusterId"],
            classification=result["classification"],
            json_schema=result["jsonSchema"],
            document_count=result["documentCount"],
            sample_doc_ids=result.get("sampleDocIds", []),
            error=result.get("error"),
        )
        discovered_classes.append(dc)

    # Generate reflection report
    bedrock_client = BedrockClient(region=os.environ.get("AWS_REGION"))
    agent = DiscoveryAgent(
        bedrock_client=bedrock_client,
        region=os.environ.get("AWS_REGION"),
    )

    try:
        reflection_report = agent.reflect(discovered_classes)
    except Exception as e:
        logger.error(f"Reflection failed: {e}")
        # The error text can quote the model's response, and the report is shown
        # in the UI, so it is cleaned like the report it replaces.
        reflection_report = clean_text(f"Reflection failed: {e}")

    # Save to config version if specified
    saved_classes = []
    if config_version:
        from idp_common.discovery.multi_document_discovery import (
            MultiDocumentDiscovery,
        )

        try:
            discovery = MultiDocumentDiscovery(
                region=os.environ.get("AWS_REGION"),
            )
            saved_classes = discovery.save_to_config(
                discovered_classes=discovered_classes,
                config_version=config_version,
                input_bucket=bucket,
                input_prefix=prefix,
            )
            logger.info(f"Saved {len(saved_classes)} classes to config version {config_version}")
        except Exception as e:
            logger.error(f"Failed to save to config: {e}")

    # Save full results to S3 for reference
    results_key = f"multi-doc-discovery/{job_id}/results.json"
    s3_client = boto3.client("s3")

    full_results = {
        "jobId": job_id,
        "discoveredClasses": [dc.to_dict() for dc in discovered_classes],
        "reflectionReport": reflection_report,
        "savedClasses": saved_classes,
        "configVersion": config_version,
    }

    s3_client.put_object(
        Bucket=DISCOVERY_BUCKET,
        Key=results_key,
        Body=json.dumps(full_results, indent=2, default=str),
        ContentType="application/json",
    )

    # Clean up intermediate files
    _cleanup_intermediate_files(s3_client, event)

    discovered_classes_json = json.dumps(
        [dc.to_dict() for dc in discovered_classes], default=str
    )

    # Notify UI: COMPLETED status with final results
    update_status(
        job_id, "COMPLETED",
        current_step="Complete",
        discovered_classes=discovered_classes_json,
        reflection_report=reflection_report,
    )

    return {
        "discoveredClassesJson": discovered_classes_json,
        "reflectionReport": reflection_report,
        "savedClasses": saved_classes,
        "resultsS3Key": results_key,
    }


def _cleanup_intermediate_files(s3_client, event):
    """Clean up intermediate S3 files (embeddings, cluster data)."""
    keys_to_delete = [
        event.get("embeddingsS3Key"),
        event.get("clusterDataS3Key"),
    ]

    for key in keys_to_delete:
        if key:
            try:
                s3_client.delete_object(Bucket=DISCOVERY_BUCKET, Key=key)
                logger.info(f"Cleaned up: {key}")
            except Exception as e:
                logger.warning(f"Failed to clean up {key}: {e}")
