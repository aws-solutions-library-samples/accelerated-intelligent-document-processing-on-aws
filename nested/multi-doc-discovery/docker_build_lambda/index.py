"""Custom resource Lambda: triggers CodeBuild and waits for completion."""

import json
import logging
import time
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

codebuild = boto3.client("codebuild")

POLL_INTERVAL = 15

# Reserve enough of the Lambda's 15-minute budget to still send a response to
# CloudFormation. Without a response CloudFormation waits the full hour before
# declaring the resource failed.
RESPONSE_RESERVE_SECS = 45

# A failed image build is usually environmental rather than a real defect in the
# Dockerfile: PyPI/ECR connection drops mid-wheel-download surface as a hard pip
# `exit 2`, which fails the build, this custom resource, the nested stack, and
# finally the entire deploy. One rebuild turns a ~26-minute pipeline failure
# into a couple of minutes of delay. STOPPED is excluded - that means a human
# (or an automated teardown) cancelled the build deliberately.
MAX_BUILD_ATTEMPTS = 2
RETRYABLE_BUILD_STATUSES = ("FAILED", "FAULT")
TERMINAL_BUILD_STATUSES = ("FAILED", "FAULT", "TIMED_OUT", "STOPPED")


def _time_left(context):
    """Seconds remaining before we must respond to CloudFormation."""
    return (context.get_remaining_time_in_millis() / 1000.0) - RESPONSE_RESERVE_SECS


def run_build(project_name, context):
    """Start one build and poll it to a terminal state.

    Returns (status, build_id). status is a CodeBuild buildStatus, or "TIMEOUT"
    if we ran out of Lambda budget while the build was still in progress.
    """
    build = codebuild.start_build(projectName=project_name)
    build_id = build["build"]["id"]
    logger.info("Build started: %s", build_id)

    while _time_left(context) > POLL_INTERVAL:
        time.sleep(POLL_INTERVAL)

        result = codebuild.batch_get_builds(ids=[build_id])
        build_status = result["builds"][0]["buildStatus"]
        logger.info(
            "Build status: %s (%.0fs of budget left)", build_status, _time_left(context)
        )

        if build_status == "SUCCEEDED":
            return "SUCCEEDED", build_id
        if build_status in TERMINAL_BUILD_STATUSES:
            return build_status, build_id

    return "TIMEOUT", build_id


def handler(event, context):
    """Handle CloudFormation custom resource events to trigger CodeBuild."""
    request_type = event["RequestType"]
    physical_resource_id = event.get("PhysicalResourceId", "docker-build-run")

    try:
        if request_type == "Delete":
            send_response(event, context, "SUCCESS", physical_resource_id)
            return

        project_name = event["ResourceProperties"]["ProjectName"]

        status = None
        build_id = None
        for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
            logger.info(
                "Starting CodeBuild project: %s (attempt %s/%s)",
                project_name,
                attempt,
                MAX_BUILD_ATTEMPTS,
            )
            status, build_id = run_build(project_name, context)

            if status == "SUCCEEDED":
                send_response(
                    event,
                    context,
                    "SUCCESS",
                    physical_resource_id,
                    data={"BuildId": build_id},
                )
                return

            retryable = (
                status in RETRYABLE_BUILD_STATUSES
                and attempt < MAX_BUILD_ATTEMPTS
                # Only worth retrying if there is budget for another build; the
                # first build's duration is a fair estimate of the second's.
                and _time_left(context) > POLL_INTERVAL * 2
            )
            if not retryable:
                break
            logger.warning(
                "Build %s ended %s; retrying once (%.0fs of budget left). "
                "See the CodeBuild logs for that build for the real cause.",
                build_id,
                status,
                _time_left(context),
            )

        if status == "TIMEOUT":
            reason = (
                f"Build {build_id} did not complete within the Lambda budget. "
                f"Check CodeBuild project {project_name}."
            )
        else:
            reason = (
                f"CodeBuild failed with status: {status} (build {build_id}, "
                f"project {project_name}). The root cause is in that build's "
                f"CloudWatch log stream, not here."
            )
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
