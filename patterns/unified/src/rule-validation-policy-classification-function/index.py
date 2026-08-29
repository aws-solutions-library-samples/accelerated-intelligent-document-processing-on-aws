# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function to classify which policy classes apply to a document using the PolicyClassificationService from idp_common.
"""

import json
import os
import logging
import time

# Import the PolicyClassificationService from idp_common
from idp_common import get_config, rule_validation, s3
from idp_common.models import Document, RuleValidationResult, Status
from idp_common.docs_service import create_document_service
from idp_common.utils import calculate_lambda_metering, merge_metering_data

# X-Ray tracing
from aws_xray_sdk.core import xray_recorder

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


@xray_recorder.capture("policy_classification_handler")
def handler(event, context):
    """
    Lambda handler for policy classification.

    Returns:
        - If match found: proceed to rule validation Map state
        - If no match: save consolidated result and skip Map state
    """
    start_time = time.time()
    logger.info(f"Event: {json.dumps(event)}")

    # Get buckets
    working_bucket = os.environ.get("WORKING_BUCKET")
    output_bucket = os.environ.get("OUTPUT_BUCKET")

    # Load document from ProcessResults output
    base_document_data = event.get("Result", {}).get("document", {})
    document = Document.load_document(base_document_data, working_bucket, logger)

    # Load configuration - use document's version if specified, otherwise use active version
    config_version = getattr(document, "config_version", None)
    config_revision = getattr(document, "config_revision", None)
    config = get_config(as_model=True, version=config_version, revision=config_revision)
    logger.info(f"Config: {json.dumps(config.model_dump(), default=str)}")

    # Log loaded document for troubleshooting
    logger.info(f"Loaded document - ID: {document.id}, input_key: {document.input_key}")
    logger.info(
        f"Document buckets - input_bucket: {document.input_bucket}, output_bucket: {document.output_bucket}"
    )
    logger.info(f"Document status: {document.status}, num_pages: {document.num_pages}")

    # X-Ray annotations
    xray_recorder.put_annotation("document_id", document.id)
    xray_recorder.put_annotation("processing_stage", "policy_classification")

    # Intelligent Policy Classification detection: Skip if document already has matched_policy_types
    if document.rule_validation_result and (
        document.rule_validation_result.matched_policy_types
        or document.rule_validation_result.output_uri
    ):
        has_match = bool(document.rule_validation_result.matched_policy_types)
        logger.info(
            f"Skipping policy classification - already processed. matched_policy_types: {document.rule_validation_result.matched_policy_types}"
        )

        try:
            lambda_metering = calculate_lambda_metering(
                "PolicyClassification", context, start_time
            )
            document.metering = merge_metering_data(document.metering, lambda_metering)
        except Exception as e:
            logger.warning(f"Failed to add Lambda metering: {str(e)}")

        response = {
            "document": document.serialize_document(
                working_bucket, "policy_classification_skip", logger
            ),
            "matched_policy_types": document.rule_validation_result.matched_policy_types
            or [],
            "matched_page_ids": document.rule_validation_result.matched_page_ids or {},
            "skip_rule_validation": not has_match,
        }

        logger.info(
            f"Policy classification skipped - Response: {json.dumps(response, default=str)}"
        )
        return response

    # Clean up old rule validation files before starting new processing
    _cleanup_rule_validation_files(output_bucket, document.input_key)

    # Update document status
    document.status = Status.RULE_VALIDATION_POLICY_CLASSIFICATION

    # Initialize service
    service = rule_validation.PolicyClassificationService(config=config)

    # No policy classes configured
    if not service.policy_classes:
        logger.info("No policy classes configured, skipping rule validation")
        result_data = service.create_no_policy_classes_result(document)
        return _save_result_and_skip(
            document,
            result_data,
            [],
            {},
            output_bucket,
            working_bucket,
            context,
            start_time,
            service,
        )

    # Classify document
    result = service.classify_document(document)

    if result.matched_policy_types:
        logger.info(
            f"Policy match found: {result.matched_policy_types}, matched_page_ids: {result.matched_page_ids}"
        )

        # Preserve existing section_results if present (for HITL continuation)
        existing_section_results = None
        if (
            document.rule_validation_result
            and document.rule_validation_result.section_results
        ):
            existing_section_results = document.rule_validation_result.section_results
            logger.info(
                f"Preserving {len(existing_section_results)} existing section results"
            )

        # Overwrite rule_validation_result with new policy classification
        document.rule_validation_result = RuleValidationResult(
            request_id=document.id,
            matched_policy_types=result.matched_policy_types,
            matched_page_ids=result.matched_page_ids,
            section_results=existing_section_results,
        )

        # Add Lambda metering
        try:
            lambda_metering = calculate_lambda_metering(
                "PolicyClassification", context, start_time
            )
            document.metering = merge_metering_data(document.metering, lambda_metering)
        except Exception as e:
            logger.warning(f"Failed to add Lambda metering: {str(e)}")

        response = {
            "document": document.serialize_document(
                working_bucket, "policy_classification", logger
            ),
            "matched_policy_types": result.matched_policy_types,
            "matched_page_ids": result.matched_page_ids,
            "skip_rule_validation": False,
        }

        logger.info(f"Response: {json.dumps(response, default=str)}")
        return response
    else:
        logger.info("No policy match found, skipping rule validation")
        result_data = service.create_no_match_result(document)
        return _save_result_and_skip(
            document,
            result_data,
            [],
            {},
            output_bucket,
            working_bucket,
            context,
            start_time,
            service,
        )


def _save_result_and_skip(
    document,
    result_data,
    matched_policy_types,
    matched_page_ids,
    output_bucket,
    working_bucket,
    context,
    start_time,
    service,
):
    """Save consolidated result to S3 and return skip response."""
    # Use same path as orchestrator
    output_key = (
        f"{document.input_key}/rule_validation/consolidated/consolidated_summary.json"
    )

    # Save to S3 using s3.write_content (same as orchestrator)
    s3.write_content(
        result_data,
        output_bucket,
        output_key,
        content_type="application/json",
    )
    output_uri = f"s3://{output_bucket}/{output_key}"
    logger.info(f"Saved consolidated result to: {output_uri}")

    # Generate and save markdown version for UI display using service method
    markdown_content = service.format_skip_result_as_markdown(result_data)
    markdown_output_key = (
        f"{document.input_key}/rule_validation/consolidated/consolidated_summary.md"
    )
    s3.write_content(
        markdown_content,
        output_bucket,
        markdown_output_key,
        content_type="text/markdown",
    )
    markdown_output_uri = f"s3://{output_bucket}/{markdown_output_key}"
    logger.info(f"Saved markdown summary to: {markdown_output_uri}")

    # Update document rule_validation_result
    document.rule_validation_result = RuleValidationResult(
        request_id=document.id,
        output_uri=markdown_output_uri,
        summary=result_data,
        matched_policy_types=matched_policy_types,
        matched_page_ids=matched_page_ids,
    )

    # Add Lambda metering
    try:
        lambda_metering = calculate_lambda_metering(
            "PolicyClassification", context, start_time
        )
        document.metering = merge_metering_data(document.metering, lambda_metering)
    except Exception as e:
        logger.warning(f"Failed to add Lambda metering: {str(e)}")

    # Update document in DynamoDB
    docs_service = create_document_service()
    docs_service.update_document(document)

    # Save to reporting bucket (same as orchestrator)
    reporting_bucket = os.environ.get("REPORTING_BUCKET")
    save_reporting_function = os.environ.get("SAVE_REPORTING_FUNCTION_NAME")

    if reporting_bucket and save_reporting_function and document.rule_validation_result:
        try:
            import boto3

            logger.info(
                f"Saving rule validation results to {reporting_bucket} via {save_reporting_function}"
            )
            lambda_client = boto3.client("lambda")
            lambda_response = lambda_client.invoke(
                FunctionName=save_reporting_function,
                InvocationType="RequestResponse",
                Payload=json.dumps(
                    {
                        "document": document.to_dict(),
                        "reporting_bucket": reporting_bucket,
                        "data_to_save": ["rule_validation_results"],
                    }
                ),
            )

            response_payload = json.loads(
                lambda_response["Payload"].read().decode("utf-8")
            )
            if response_payload.get("statusCode") != 200:
                logger.warning(
                    f"SaveReportingData returned non-200 status: {response_payload}"
                )
            else:
                logger.info("SaveReportingData executed successfully")
        except Exception as e:
            logger.error(f"Error invoking SaveReportingData: {str(e)}")

    response = {
        "document": document.serialize_document(
            working_bucket, "policy_classification", logger
        ),
        "matched_policy_types": matched_policy_types,
        "matched_page_ids": matched_page_ids,
        "skip_rule_validation": True,
    }

    logger.info(f"Response (skip): {json.dumps(response, default=str)}")
    return response


def _cleanup_rule_validation_files(bucket, input_key):
    """Clean up old rule validation files from S3 before starting new processing."""
    try:
        import boto3

        s3_client = boto3.client("s3")

        prefix = f"{input_key}/rule_validation/"

        # List all objects with the prefix
        response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)

        if "Contents" not in response:
            logger.info(f"No rule validation files found at {prefix}")
            return

        # Delete all objects
        objects_to_delete = [{"Key": obj["Key"]} for obj in response["Contents"]]

        if objects_to_delete:
            s3_client.delete_objects(
                Bucket=bucket, Delete={"Objects": objects_to_delete}
            )
            logger.info(
                f"Cleared {len(objects_to_delete)} rule validation files from {prefix}"
            )

    except Exception as e:
        logger.warning(
            f"Failed to clear rule validation files for {input_key}: {str(e)}"
        )
