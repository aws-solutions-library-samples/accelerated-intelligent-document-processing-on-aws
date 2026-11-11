# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Custom resource Lambda: starts the generator image build and waits for it.

Starts the DockerBuildProject on Create/Update and reports success once the
build has SUCCEEDED, so the AgentCore runtime custom resource only runs once the
image is in ECR.

The wait is spread over polls rather than held inside one invocation (#1824).
crhelper re-invokes this function from an EventBridge rule every two minutes
until the build settles, so the build has CloudFormation's full hour for a custom
resource instead of Lambda's 15 minutes. Waiting inside one invocation is what
failed an upgrade when CodeBuild was slow to start builds, and then failed its
rollback the same way.

This is the feature's own handler rather than the host's
``src/lambda/start_codebuild``: a feature is versioned and published as its own
artifact, so its code has to live inside the feature directory, where a change
to it is a change to the feature.

There is no rollback shortcut here, unlike the host's handler. The image is
tagged ``latest``, and the AgentCore runtime re-pulls ``latest`` on every update,
rollbacks included, so a rollback that skipped the rebuild would leave the
runtime on whatever image ``latest`` holds, which can be the newer version's.
"""

import logging
from os import getenv

import boto3
from crhelper import CfnResource  # type: ignore[import-untyped]

LOGGER = logging.getLogger(__name__)

HELPER = CfnResource(json_logging=True, log_level=getenv("LOG_LEVEL", "INFO"))

try:
    CODEBUILD = boto3.client("codebuild")
except Exception as init_exception:  # pylint: disable=broad-except
    HELPER.init_failure(init_exception)

#: Kept from the handler this replaces, so a resource already deployed keeps its
#: physical id and an update is not reported as a replacement.
DEFAULT_PHYSICAL_ID = "docker-build-run"


@HELPER.create
@HELPER.update
def start_build(event, _context):
    """Start the image build; the poller below waits for it."""
    project_name = event["ResourceProperties"]["ProjectName"]
    build_id = CODEBUILD.start_build(projectName=project_name)["build"]["id"]
    LOGGER.info("started build %s of project %s", build_id, project_name)
    HELPER.Data["build_id"] = build_id


@HELPER.poll_create
@HELPER.poll_update
def poll_build(event, _context):
    """Return the physical id once the build succeeds, None to poll again.

    A build that ends in any other state fails the resource, with a reason that
    names the status and the build so the CodeBuild log can be found.
    """
    build_id = event["CrHelperData"]["build_id"]
    builds = CODEBUILD.batch_get_builds(ids=[build_id])["builds"]
    if not builds:
        raise RuntimeError(f"could not find build {build_id}")

    status = builds[0]["buildStatus"]
    LOGGER.info("build %s status: %s", build_id, status)
    if status == "SUCCEEDED":
        return event.get("PhysicalResourceId", DEFAULT_PHYSICAL_ID)
    if status == "IN_PROGRESS":
        return None
    raise RuntimeError(
        f"CodeBuild failed with status: {status} (build {build_id}). The root "
        f"cause is in that build's CloudWatch log stream."
    )


@HELPER.delete
def delete(_event, _context):
    """Nothing to clean up: the image is removed with the ECR repository."""


def handler(event, context):
    """Lambda entry point."""
    HELPER(event, context)
