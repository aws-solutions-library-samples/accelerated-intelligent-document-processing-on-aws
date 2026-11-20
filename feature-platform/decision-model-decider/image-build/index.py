# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Custom resource: build the decider's container image, once per set of inputs.

The image is named by its build inputs: the upstream source commit, the model
and its Hub commit, and whether the vision tower is included. (The upstream
Dockerfile's base image and system packages are not pinned by digest, so two
builds of the same inputs can differ in those; each tag is built once.) Its tag
is derived from those inputs (:func:`image_tag`), so

* a stack update that changes no input finds the image already in ECR and does
  not rebuild it, which is a long CodeBuild run on the large ARM machine;
* a rollback after an input change finds the previous image still in ECR and
  points the function back at it without a build;
* the function's ``ImageUri`` changes exactly when an input changes, which is
  what publishes a new function version.

Both revisions must be full 40-character commit SHAs. A branch or tag name can
move, so it would build different code or weights from the same template; the
build verifies the fetched source is the named commit, and that the Hub revision
is a commit rather than a ref (see ``buildspec`` in ``template.yaml``).

The wait is spread over polls, as in ``feature-platform/idp-data-generator``:
crhelper re-invokes this function from an EventBridge rule every two minutes
until the build settles, so the build has CloudFormation's hour for a custom
resource rather than Lambda's 15 minutes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from os import getenv
from typing import Any, Dict

import boto3
from crhelper import CfnResource  # type: ignore[import-untyped]

LOGGER = logging.getLogger(__name__)

HELPER = CfnResource(json_logging=True, log_level=getenv("LOG_LEVEL", "INFO"))

try:
    CODEBUILD = boto3.client("codebuild")
    ECR = boto3.client("ecr")
except Exception as init_exception:  # pylint: disable=broad-except
    HELPER.init_failure(init_exception)

DEFAULT_PHYSICAL_ID = "decider-image-build"

#: A full, lowercase git commit SHA. Hugging Face Hub commits use the same form.
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")

#: A Hugging Face Hub model id: ``<owner>/<name>``.
HUB_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

#: The source repository the build may fetch. Fixed rather than configurable,
#: and the build accepts only a commit one of this repository's own branches or
#: tags contains, because GitHub also serves a fork's commits through the
#: parent's URL. ``DeciderSourceRevision`` still chooses which of its commits.
SOURCE_REPOSITORY = "https://github.com/strands-labs/strands-decider"

#: Bumped when the build itself changes in a way that changes the image for the
#: same inputs (the buildspec's build arguments, for example), so such a change
#: rebuilds instead of reusing an image built the old way.
RECIPE_VERSION = "1"


def build_inputs(properties: Dict[str, Any]) -> Dict[str, str]:
    """The validated build inputs named by the resource's properties.

    Raises:
        ValueError: an input is missing or not in its required form.
    """
    source_revision = str(properties.get("SourceRevision", "")).strip()
    model_id = str(properties.get("ModelId", "")).strip()
    model_revision = str(properties.get("ModelRevision", "")).strip()
    vision = str(properties.get("Vision", "")).strip().lower()

    if not COMMIT_SHA.match(source_revision):
        raise ValueError(
            f"SourceRevision {source_revision!r} is not a full 40-character commit "
            "SHA. A branch or tag can move; pin the upstream source by commit."
        )
    if not COMMIT_SHA.match(model_revision):
        raise ValueError(
            f"ModelRevision {model_revision!r} is not a full 40-character Hub commit "
            "SHA. A branch such as 'main' can move; pin the weights by commit."
        )
    if not HUB_MODEL_ID.match(model_id):
        raise ValueError(f"ModelId {model_id!r} is not a Hub model id (owner/name)")
    if vision not in ("true", "false"):
        raise ValueError(f"Vision must be 'true' or 'false', not {vision!r}")
    return {
        "SOURCE_REPOSITORY": SOURCE_REPOSITORY,
        "SOURCE_REVISION": source_revision,
        "MODEL_ID": model_id,
        "MODEL_REVISION": model_revision,
        "VISION": vision,
        "RECIPE_VERSION": RECIPE_VERSION,
    }


def image_tag(inputs: Dict[str, str]) -> str:
    """The image tag for a set of build inputs: equal inputs, equal tag."""
    digest = hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"decider-{digest[:24]}"


def image_exists(repository: str, tag: str) -> bool:
    """Whether ``repository`` already holds an image tagged ``tag``."""
    try:
        ECR.describe_images(repositoryName=repository, imageIds=[{"imageTag": tag}])
    except ECR.exceptions.ImageNotFoundException:
        return False
    return True


@HELPER.create
@HELPER.update
def start_build(event, _context):
    """Start the image build unless the image is already in ECR."""
    properties = event["ResourceProperties"]
    inputs = build_inputs(properties)
    tag = image_tag(inputs)
    HELPER.Data["ImageTag"] = tag
    repository = properties["RepositoryName"]
    if image_exists(repository, tag):
        LOGGER.info("image %s:%s is already in ECR; not rebuilding", repository, tag)
        return None
    overrides = [
        {"name": name, "value": value, "type": "PLAINTEXT"}
        for name, value in sorted({**inputs, "IMAGE_TAG": tag}.items())
    ]
    build_id = CODEBUILD.start_build(
        projectName=properties["ProjectName"],
        environmentVariablesOverride=overrides,
    )["build"]["id"]
    LOGGER.info("started build %s for image tag %s", build_id, tag)
    HELPER.Data["build_id"] = build_id
    return None


@HELPER.poll_create
@HELPER.poll_update
def poll_build(event, _context):
    """Return the physical id once the image is in ECR, None to poll again.

    A build that ends in any state other than SUCCEEDED fails the resource, with
    a reason that names the status and the build so its log can be found.
    """
    data = event.get("CrHelperData", {})
    build_id = data.get("build_id")
    physical_id = event.get("PhysicalResourceId", DEFAULT_PHYSICAL_ID)
    if not build_id:
        # The image was already in ECR when the request arrived.
        return physical_id
    builds = CODEBUILD.batch_get_builds(ids=[build_id])["builds"]
    if not builds:
        raise RuntimeError(f"could not find build {build_id}")
    status = builds[0]["buildStatus"]
    LOGGER.info("build %s status: %s", build_id, status)
    if status == "SUCCEEDED":
        return physical_id
    if status == "IN_PROGRESS":
        return None
    raise RuntimeError(
        f"the decider image build ended {status} (build {build_id}); the cause is "
        "in that build's log in the stack's CodeBuild log group"
    )


@HELPER.delete
def delete(_event, _context):
    """Nothing to clean up: the images go with the ECR repository."""


def handler(event, context):
    """Lambda entry point."""
    HELPER(event, context)
