# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""CodeBuild starter and ECR cleanup Lambda function used by Pattern 2 deployments."""
import logging
from os import getenv
import json
from typing import List

import boto3
from botocore.config import Config as BotoCoreConfig
from botocore.exceptions import ClientError
from crhelper import CfnResource  # type: ignore[import-untyped]
from log_sanitizer import sanitize_event_for_logging


LOGGER = logging.getLogger(__name__)
LOG_LEVEL = getenv("LOG_LEVEL", "INFO")
HELPER = CfnResource(
    json_logging=True,
    log_level=LOG_LEVEL,
)

# global init code goes here so that it can pass failure in case
# of an exception
try:
    # boto3 client
    CLIENT_CONFIG = BotoCoreConfig(
        retries={"mode": "adaptive", "max_attempts": 5},
    )
    CODEBUILD_CLIENT = boto3.client("codebuild", config=CLIENT_CONFIG)
    ECR_CLIENT = boto3.client("ecr", config=CLIENT_CONFIG)
except Exception as init_exception:  # pylint: disable=broad-except
    HELPER.init_failure(init_exception)


@HELPER.create
@HELPER.update
def create_or_update(event, _):
    """Create or Update Resource"""
    resource_type = event["ResourceType"]
    resource_properties = event["ResourceProperties"]

    if resource_type == "Custom::CodeBuildRun":
        try:
            project_name = resource_properties["BuildProjectName"]
            response = CODEBUILD_CLIENT.start_build(projectName=project_name)
            build_id = response["build"]["id"]
            HELPER.Data["build_id"] = build_id
        except Exception as exception:  # pylint: disable=broad-except
            LOGGER.error("failed to start build - exception: %s", exception)
            raise

        return

    if resource_type == "Custom::ECRRepositoryCleanup":
        repository_name = resource_properties["RepositoryName"]
        LOGGER.info("registered ECR cleanup resource for repository %s", repository_name)
        HELPER.Data["repository_name"] = repository_name
        return

    raise ValueError(f"invalid resource type: {resource_type}")


def _verify_ecr_images_available(ecr_uri: str, image_version: str, expected_images: List[str] = None) -> bool:
    """Verify all required Lambda images exist in ECR and are pullable.
    
    Args:
        ecr_uri: ECR repository URI (e.g., 123456789012.dkr.ecr.us-east-1.amazonaws.com/repo-name)
        image_version: Image version tag (e.g., "latest" or "0.3.19")
        expected_images: List of base image names (without version suffix). If not provided, defaults to Pattern-2 images.
    
    Returns:
        True if all images are available and scannable, False otherwise
    """
    try:
        repository_name = ecr_uri.split("/")[-1]
        
        # If expected_images not provided, fall back to Pattern-2 images for backward compatibility
        if expected_images is None:
            expected_images = [
                "ocr-function",
                "classification-function",
                "extraction-function",
                "assessment-function",
                "processresults-function",
                "summarization-function",
                "evaluation-function",
                "hitl-wait-function",
                "hitl-status-update-function",
                "hitl-process-function",
            ]
        
        # Append version to each base image name
        required_images = [f"{img}-{image_version}" for img in expected_images]
        
        LOGGER.info(
            "verifying %d images in repository %s with version %s",
            len(required_images),
            repository_name,
            image_version,
        )
        
        # Check each image
        for image_tag in required_images:
            try:
                response = ECR_CLIENT.describe_images(
                    repositoryName=repository_name,
                    imageIds=[{"imageTag": image_tag}]
                )
                
                images = response.get("imageDetails", [])
                if not images:
                    LOGGER.warning("image %s not found in ECR", image_tag)
                    return False
                
                # Check if image scan is complete (repository has ScanOnPush enabled)
                image = images[0]
                scan_status = image.get("imageScanStatus", {}).get("status")
                
                if scan_status == "IN_PROGRESS":
                    LOGGER.info("image %s scan still in progress", image_tag)
                    return False
                
                LOGGER.info("image %s verified (scan status: %s)", image_tag, scan_status)
                    
            except ClientError as error:
                error_code = error.response["Error"]["Code"]
                
                # Retriable condition - image just doesn't exist yet, keep polling
                if error_code == "ImageNotFoundException":
                    LOGGER.warning("image %s not found: %s", image_tag, error)
                    return False  # Continue polling
                
                # Fatal errors - permissions, validation, repository not found, etc.
                # Fail immediately instead of polling forever
                LOGGER.error(
                    "fatal error checking image %s (error code: %s): %s",
                    image_tag,
                    error_code,
                    error
                )
                raise  # Fail custom resource immediately
        
        LOGGER.info("all %d required images are available in ECR", len(required_images))
        return True
        
    except Exception as exception:  # pylint: disable=broad-except
        # Any non-ClientError exception is unexpected and fatal
        LOGGER.error("unexpected fatal error verifying ECR images: %s", exception)
        raise  # Fail custom resource immediately instead of polling forever


@HELPER.poll_create
@HELPER.poll_update
def poll_create_or_update(event, _):
    """Create or Update Poller"""
    resource_type = event["ResourceType"]
    helper_data = event["CrHelperData"]

    if resource_type == "Custom::CodeBuildRun":
        try:
            build_id = helper_data["build_id"]
            response = CODEBUILD_CLIENT.batch_get_builds(ids=[build_id])
            LOGGER.info(response)

            builds = response["builds"]
            if not builds:
                raise RuntimeError("could not find build")

            build = builds[0]
            build_status = build["buildStatus"]
            LOGGER.info("build status: [%s]", build_status)

            if build_status == "SUCCEEDED":
                # Verify ECR images are available before returning success
                # This prevents Lambda functions from being created before images are pullable
                env_vars = build.get("environment", {}).get("environmentVariables", [])
                
                # Extract ECR URI, image version, and expected images from build/resource properties
                ecr_uri = next((v["value"] for v in env_vars if v["name"] == "ECR_URI"), None)
                image_version = next((v["value"] for v in env_vars if v["name"] == "IMAGE_VERSION"), None)
                
                # Get expected images from resource properties (optional)
                resource_properties = event.get("ResourceProperties", {})
                expected_images = resource_properties.get("ExpectedImages")
                
                if ecr_uri and image_version:
                    LOGGER.info("verifying ECR images are available and pullable...")
                    if _verify_ecr_images_available(ecr_uri, image_version, expected_images):
                        LOGGER.info("ECR image verification complete - returning True")
                        return True
                    
                    LOGGER.info("ECR images not yet available - returning None to poll again")
                    return None
                
                # Fallback: if we can't extract variables, proceed without verification
                LOGGER.warning(
                    "could not extract ECR_URI or IMAGE_VERSION from build environment, "
                    "proceeding without ECR verification"
                )
                return True

            if build_status == "IN_PROGRESS":
                LOGGER.info("returning None")
                return None

            raise RuntimeError(f"build did not complete - status: [{build_status}]")

        except Exception as exception:  # pylint: disable=broad-except
            LOGGER.error("build poller - exception: %s", exception)
            raise

    if resource_type == "Custom::ECRRepositoryCleanup":
        LOGGER.info("ECR cleanup resource create/update completed")
        return True

    raise RuntimeError(f"Invalid resource type: {resource_type}")


@HELPER.delete
def delete_resource(event, _):
    """Delete Resource"""
    resource_type = event["ResourceType"]

    if resource_type == "Custom::CodeBuildRun":
        LOGGER.info(
            "delete event ignored for CodeBuild custom resource: %s",
            sanitize_event_for_logging(event),
        )
        return

    if resource_type == "Custom::ECRRepositoryCleanup":
        repository_name = event["ResourceProperties"]["RepositoryName"]
        LOGGER.info("starting cleanup for repository %s", repository_name)
        try:
            _delete_all_ecr_images(repository_name)
        except ClientError as error:
            if error.response["Error"]["Code"] == "RepositoryNotFoundException":
                LOGGER.info("repository %s already deleted", repository_name)
                return
            LOGGER.error(
                "failed to purge repository %s - error: %s",
                repository_name,
                error,
            )
            raise
        except Exception as unknown_exception:  # pylint: disable=broad-except
            LOGGER.error(
                "unexpected error while cleaning repository %s - error: %s",
                repository_name,
                unknown_exception,
            )
            raise

        LOGGER.info("cleanup for repository %s completed", repository_name)
        return

    LOGGER.warning("received delete for unsupported resource type: %s", resource_type)


def _delete_all_ecr_images(repository_name: str) -> None:
    """Delete every image (tagged and untagged) from the ECR repository."""
    paginator = ECR_CLIENT.get_paginator("list_images")
    images_to_delete: List[dict] = []

    for page in paginator.paginate(repositoryName=repository_name):
        image_ids = page.get("imageIds", [])
        if not image_ids:
            continue
        images_to_delete.extend(image_ids)
        LOGGER.info(
            "queued %s images for deletion from repository %s",
            len(image_ids),
            repository_name,
        )

    if not images_to_delete:
        LOGGER.info("no images found in repository %s", repository_name)
        return

    for chunk_start in range(0, len(images_to_delete), 100):
        chunk = images_to_delete[chunk_start : chunk_start + 100]
        LOGGER.info(
            "deleting %s images from repository %s",
            len(chunk),
            repository_name,
        )
        ECR_CLIENT.batch_delete_image(repositoryName=repository_name, imageIds=chunk)


def handler(event, context):
    """Lambda Handler"""
    LOGGER.info("Received event: %s", json.dumps(sanitize_event_for_logging(event)))
    HELPER(event, context)
