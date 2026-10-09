# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""CodeBuild starter and ECR cleanup Lambda function used by Pattern 2 deployments."""
import logging
from os import getenv
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

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


# --------------------------------------------------------------------------- #
# The budget the image-scan wait has to live inside.
#
# CloudFormation gives a custom resource one hour to answer, and the
# Custom::CodeBuildRun resource spends that hour on two things in sequence: the
# CodeBuild run, and then the ECR image verification below. So the scan wait gets
# whatever the build left over, and it must end before the hour does. Running past
# it is what produces "CloudFormation did not receive a response from your Custom
# Resource", which names the cfn-response path and says nothing about scanning.
CUSTOM_RESOURCE_BUDGET_SECONDS = 3600

# crhelper polls by scheduling itself on a CloudWatch Events rule; HELPER above
# does not override polling_interval, so the rule is rate(2 minutes).
#
# The Lambda timeout is CodeBuildTrigger's (patterns/unified/template.yaml), which
# is the only function that reaches this wait. The root template also deploys this
# code, as StartUICodeBuild, but its CodeBuildRun supplies no ECR_URI, so the
# poller takes the "could not extract ECR_URI" branch and never gets here. Both
# are Timeout: 60 today; only the first one is load-bearing here.
POLL_INTERVAL_SECONDS = 120
LAMBDA_TIMEOUT_SECONDS = 60

# Reserve two poll cycles plus one Lambda timeout (= 300s): the poll that notices
# the deadline still has to run, return success, and have crhelper's response
# reach CloudFormation, and the poll before it may have started just too early to
# see the deadline.
SCAN_WAIT_RESERVE_SECONDS = 2 * POLL_INTERVAL_SECONDS + LAMBDA_TIMEOUT_SECONDS

# 3600 - 300 = 3300s, measured from the moment the build started rather than from
# the first scan check. start_build() runs in the same invocation that answers
# CloudFormation's CREATE/UPDATE request, so the build's own startTime is within
# seconds of the start of the custom resource's hour. Deriving the deadline from
# it, instead of fixing an attempt count, means a slow build shortens the scan
# wait rather than pushing the pair of them past the hour: a 10-minute build
# leaves ~45 minutes of scan wait, and a build that ran to CodeBuild's own
# 55-minute TimeoutInMinutes leaves none, which is the correct answer.
SCAN_WAIT_BUDGET_SECONDS = CUSTOM_RESOURCE_BUDGET_SECONDS - SCAN_WAIT_RESERVE_SECONDS

# ECR has two scanning modes and they report different statuses, so a wait that
# knows only one of them orders nothing in the other.
#
#   basic (the repository's own ImageScanningConfiguration.ScanOnPush, which is
#   all EnableECRImageScanning sets):   IN_PROGRESS -> COMPLETE
#   enhanced (an Inspector setting at registry scope, which overrides the
#   repository's ScanOnPush):            PENDING     -> ACTIVE
#
# So both pairs are named. IN_PROGRESS and PENDING are the two ways ECR says
# "queued or running, not scanned yet"; COMPLETE and ACTIVE are the two ways it
# says "scanned". Neither of the latter says anything about what the scan found;
# nothing here reads findings.
SCAN_RUNNING_STATUSES = ("IN_PROGRESS", "PENDING")
SCAN_FINISHED_STATUSES = ("COMPLETE", "ACTIVE")

# Prefixes on every log line that reports a scan-status decision, and on every
# line where this path gives up. The whole cost of issue #1336 was a terminal
# error that named the cfn-response path instead of image scanning, so the
# subsystem is named in the log and in the failure reason, every time.
SCAN_LOG_PREFIX = "ECR IMAGE SCANNING"
VERIFY_LOG_PREFIX = "ECR IMAGE VERIFICATION"

# The phrase that has to reach CloudFormation. crhelper's `_send` truncates an
# over-long Reason by keeping the TAIL -- `"ERROR: (truncated) " +
# str(self.Reason)[len - 240:]`, resource_helper.py -- and `self.Reason` is
# `str(exception)`. So the identifier goes at the END of every message raised from
# here: a realistic AccessDeniedException is well over 256 characters on its own,
# and a message that leads with the identifier loses exactly the identifier.
VERIFY_FAILURE_SUFFIX = "ECR image verification failed"


class EcrImageVerificationError(RuntimeError):
    """ECR image verification could not complete.

    Carried to CloudFormation as the custom resource's failure reason. Every
    message ends with VERIFY_FAILURE_SUFFIX and the image tag rather than leading
    with them, because crhelper truncates a long reason from the front.
    """


def _scan_wait_remaining_seconds(
    build_start_time: Optional[datetime],
) -> Optional[float]:
    """Seconds of the custom resource's hour still available for scan waiting.

    Returns None when ``build_start_time`` is unknown, which means there is no
    clock to bound the wait against at all.
    """
    if build_start_time is None:
        return None

    if build_start_time.tzinfo is None:
        build_start_time = build_start_time.replace(tzinfo=timezone.utc)

    deadline = build_start_time + timedelta(seconds=SCAN_WAIT_BUDGET_SECONDS)
    return (deadline - datetime.now(timezone.utc)).total_seconds()


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


def _verify_ecr_images_available(
    ecr_uri: str,
    image_version: str,
    expected_images: Optional[List[str]] = None,
    build_start_time: Optional[datetime] = None,
) -> bool:
    """Verify all required Lambda images exist in ECR before Lambdas reference them.

    Two separate things happen here, and they are worth keeping apart.

    **Presence.** Every expected tag must exist in the repository. A tag that is
    absent returns False so the caller polls again, because the build has only
    just reported success and the push may not have settled.

    **Scan ordering, and only ordering.** When scanning is on, this waits while
    ECR reports a tag's scan as queued or running -- ``IN_PROGRESS`` under basic
    scan-on-push, ``PENDING`` under enhanced (Inspector) scanning, see
    SCAN_RUNNING_STATUSES -- so that an image is scanned before the Lambda
    functions that pull it are created. That is the whole of it. Nothing here
    calls describe_image_scan_findings or reads findingSeverityCounts, so the
    scan's *result* is not consulted and no severity gates the deploy: an image
    whose scan reports critical findings is deployed exactly as one with a clean
    scan is. Every other status -- including FAILED and UNSUPPORTED_IMAGE -- is
    logged and deployed.

    The wait is bounded by SCAN_WAIT_BUDGET_SECONDS measured from
    ``build_start_time``. Once that is spent the image is treated as available and
    deployed with its scan still running, which degrades a slow scan to added
    latency instead of failing the stack operation at CloudFormation's one-hour
    custom-resource limit.

    Args:
        ecr_uri: ECR repository URI (e.g., 123456789012.dkr.ecr.us-east-1.amazonaws.com/repo-name)
        image_version: Image version tag (e.g., "latest" or "0.3.19")
        expected_images: Base image names, without the version suffix. Required:
            there is no default image set, because a wrong one is a permanent
            poll. See the raise below.
        build_start_time: When the CodeBuild run started, used as the origin for
            the scan-wait budget. None disables waiting on scan status, since
            without it the wait cannot be bounded.

    Returns:
        True if every expected image is present (and no scan wait is outstanding),
        False if the caller should poll again.

    Raises:
        EcrImageVerificationError: verification cannot complete -- no
            ``ExpectedImages``, or a permissions, validation or
            missing-repository error from ECR. Fails the custom resource
            immediately rather than polling out the hour.
    """
    try:
        repository_name = ecr_uri.split("/")[-1]

        # No default image set. There used to be one, naming three `hitl-*` images
        # no buildspec in this repository builds and omitting every `bda-*`,
        # `rule-validation-*` and `mlflow-logger-*` image that it does -- so had
        # anything ever reached it, the result was a wait for tags that would never
        # appear: a permanent poll ending in the same misleading custom-resource
        # timeout this function was changed to prevent. Nothing reaches it today
        # (DockerBuildRun always passes ExpectedImages, and the root template's
        # CodeBuildRun has no ECR_URI so it never gets here), so failing loudly
        # names the missing property instead of silently verifying the wrong set.
        if not expected_images:
            raise EcrImageVerificationError(
                "no ExpectedImages were supplied for repository "
                f"{repository_name}, so there is no image set to verify and "
                "waiting would never end -- "
                f"{VERIFY_FAILURE_SUFFIX}"
            )

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
                    # Deliberately unbounded: the build has just reported success,
                    # so a tag that is absent is the shape of issue #1310 (a build
                    # that claimed success without pushing), which is prevented at
                    # source by errexit in the buildspec loops.
                    LOGGER.warning("image %s not found in ECR", image_tag)
                    return False

                # Scan status, when the repository has ScanOnPush enabled. This
                # orders scanning before the image goes live; it does not read
                # what the scan found.
                image = images[0]
                scan_status = image.get("imageScanStatus", {}).get("status")

                if scan_status in SCAN_RUNNING_STATUSES:
                    if not _proceed_despite_running_scan(
                        image_tag, scan_status, build_start_time
                    ):
                        return False
                elif scan_status is None:
                    # Deliberately not waited on, and the reason is that an absent
                    # scan record cannot be told apart from scanning being switched
                    # off: EnableECRImageScanning defaults to false at the root, and
                    # with ScanOnPush off no scan record ever appears, so waiting
                    # here would spend the whole budget on every deploy. It does
                    # also occur transiently with scanning ON, in the window
                    # between the push and ECR attaching a scan record, and in that
                    # window the ordering is skipped.
                    LOGGER.info(
                        "%s: image %s is present and ECR reports no scan status "
                        "for it yet. Not waiting -- an absent scan record is "
                        "indistinguishable from scanning being switched off for "
                        "this repository, where no record would ever arrive.",
                        SCAN_LOG_PREFIX,
                        image_tag,
                    )
                elif scan_status in SCAN_FINISHED_STATUSES:
                    LOGGER.info(
                        "%s: image %s is present and its scan has finished "
                        "(status: %s). Scan findings are not read here, so a "
                        "finished scan is not a clean bill of health.",
                        SCAN_LOG_PREFIX,
                        image_tag,
                        scan_status,
                    )
                else:
                    LOGGER.warning(
                        "%s: image %s is present but its scan did not finish "
                        "(status: %s). Deploying it anyway -- this wait orders "
                        "scanning before the image goes live and does not gate on "
                        "the scan's status or its findings.",
                        SCAN_LOG_PREFIX,
                        image_tag,
                        scan_status,
                    )

            except ClientError as error:
                error_code = error.response["Error"]["Code"]

                # Retriable condition - image just doesn't exist yet, keep polling
                if error_code == "ImageNotFoundException":
                    LOGGER.warning("image %s not found: %s", image_tag, error)
                    return False  # Continue polling

                # Fatal errors - permissions, validation, repository not found, etc.
                # Fail immediately instead of polling forever
                LOGGER.error(
                    "%s: fatal error checking image %s (error code: %s): %s",
                    VERIFY_LOG_PREFIX,
                    image_tag,
                    error_code,
                    error
                )
                # Identifier last: see VERIFY_FAILURE_SUFFIX. A realistic
                # AccessDeniedException is longer than crhelper's 256-character
                # Reason limit on its own, and crhelper keeps the tail.
                raise EcrImageVerificationError(
                    f"{error_code}: {error} -- {VERIFY_FAILURE_SUFFIX} "
                    f"for image {image_tag} in repository {repository_name}"
                ) from error

        LOGGER.info("all %d required images are available in ECR", len(required_images))
        return True

    except EcrImageVerificationError:
        # Already named, already logged. Without this the broad handler below
        # describes an error it has just correctly named as "unexpected" -- a
        # second, wrong ERROR line -- and re-wraps the message, pushing the
        # identifier further from the tail crhelper keeps.
        raise
    except Exception as exception:  # pylint: disable=broad-except
        # Any non-ClientError exception is unexpected and fatal
        LOGGER.error("%s: unexpected fatal error: %s", VERIFY_LOG_PREFIX, exception)
        # Identifier last: see VERIFY_FAILURE_SUFFIX. Nothing from the try block
        # is referenced here -- `repository_name` may be unbound if the failure
        # was in computing it, and a NameError raised inside this handler would
        # lose the original error entirely.
        raise EcrImageVerificationError(
            f"{exception} -- {VERIFY_FAILURE_SUFFIX} unexpectedly"
        ) from exception


def _proceed_despite_running_scan(
    image_tag: str,
    scan_status: str,
    build_start_time: Optional[datetime],
) -> bool:
    """Decide whether a queued or running image scan should stop blocking the deploy.

    ``scan_status`` is the status that is holding the wait up -- one of
    SCAN_RUNNING_STATUSES, so ``IN_PROGRESS`` under basic scanning or ``PENDING``
    under enhanced. Returns True to stop waiting (the image is deployed with its
    scan unfinished), False to keep waiting for this poll cycle.
    """
    remaining = _scan_wait_remaining_seconds(build_start_time)

    if remaining is None:
        LOGGER.warning(
            "%s: image %s scan has not finished (status: %s) and the wait cannot "
            "be bounded -- the CodeBuild run's start time is unknown, so there is "
            "no origin to measure the custom resource's remaining hour from. "
            "Proceeding with the scan unfinished rather than polling until "
            "CloudFormation times this custom resource out.",
            SCAN_LOG_PREFIX,
            image_tag,
            scan_status,
        )
        return True

    if remaining <= 0:
        LOGGER.warning(
            "%s: wait exhausted for image %s. Its scan has not finished (status: "
            "%s) and the %ds this custom resource can spend waiting -- "
            "CloudFormation's %ds custom-resource limit less %ds reserved to "
            "answer it -- has been spent since the build started. Proceeding: the "
            "image is deployed with its scan unfinished. This is latency, not a "
            "scan result; findings are not read here in any case.",
            SCAN_LOG_PREFIX,
            image_tag,
            scan_status,
            SCAN_WAIT_BUDGET_SECONDS,
            CUSTOM_RESOURCE_BUDGET_SECONDS,
            SCAN_WAIT_RESERVE_SECONDS,
        )
        return True

    LOGGER.info(
        "%s: image %s scan still in progress (status: %s); %ds of the %ds wait "
        "budget remain before the image is deployed unscanned",
        SCAN_LOG_PREFIX,
        image_tag,
        scan_status,
        int(remaining),
        SCAN_WAIT_BUDGET_SECONDS,
    )
    return False


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
                
                # Origin for the image-scan wait budget: start_build() is called
                # in the same invocation that answers CloudFormation's request,
                # so this is within seconds of the start of the custom resource's
                # hour. See SCAN_WAIT_BUDGET_SECONDS.
                build_start_time = build.get("startTime")

                if ecr_uri and image_version:
                    LOGGER.info("verifying ECR images are available and pullable...")
                    if _verify_ecr_images_available(
                        ecr_uri, image_version, expected_images, build_start_time
                    ):
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
