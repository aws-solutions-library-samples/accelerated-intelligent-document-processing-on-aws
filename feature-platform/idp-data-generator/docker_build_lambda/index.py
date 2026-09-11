# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Custom resource Lambda: triggers CodeBuild and waits for completion.

Generator-agnostic — copied verbatim from the host's
nested/synthesis-runtime/docker_build_lambda. Starts the DockerBuildProject on
Create/Update and polls until the image build SUCCEEDS (or fails), so the
AgentCore runtime custom resource only runs once the image is in ECR.
"""

import json
import logging
import time
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

codebuild = boto3.client("codebuild")


def handler(event, context):
    """Handle CloudFormation custom resource events to trigger CodeBuild."""
    request_type = event["RequestType"]
    physical_resource_id = event.get("PhysicalResourceId", "docker-build-run")

    try:
        if request_type == "Delete":
            send_response(event, context, "SUCCESS", physical_resource_id)
            return

        project_name = event["ResourceProperties"]["ProjectName"]
        logger.info("Starting CodeBuild project: %s", project_name)

        build = codebuild.start_build(projectName=project_name)
        build_id = build["build"]["id"]
        logger.info("Build started: %s", build_id)

        # Poll for completion (max ~14 minutes to stay within Lambda 15-min limit)
        max_wait = 840
        poll_interval = 15
        elapsed = 0

        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            result = codebuild.batch_get_builds(ids=[build_id])
            build_status = result["builds"][0]["buildStatus"]
            logger.info("Build status: %s (elapsed: %ds)", build_status, elapsed)

            if build_status == "SUCCEEDED":
                send_response(
                    event,
                    context,
                    "SUCCESS",
                    physical_resource_id,
                    data={"BuildId": build_id},
                )
                return
            elif build_status in ("FAILED", "FAULT", "TIMED_OUT", "STOPPED"):
                reason = f"CodeBuild failed with status: {build_status}"
                logger.error(reason)
                send_response(
                    event, context, "FAILED", physical_resource_id, reason=reason
                )
                return

        reason = f"Build {build_id} did not complete within {max_wait}s"
        logger.error(reason)
        send_response(event, context, "FAILED", physical_resource_id, reason=reason)

    except Exception as e:
        logger.exception("Error in custom resource handler")
        send_response(event, context, "FAILED", physical_resource_id, reason=str(e))


def send_response(event, context, status, physical_resource_id, data=None, reason=""):
    """Send response to CloudFormation."""
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
            "PhysicalResourceId": physical_resource_id,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "Data": data or {},
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        method="PUT",
    )
    urllib.request.urlopen(req)  # noqa: S310  # nosec B310 - CloudFormation-supplied custom-resource callback URL (https), not user input
