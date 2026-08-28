# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom resource: stack-scoped BDA OCR project.

Provisions a per-stack Bedrock Data Automation *standard-output SYNC* project
used by the ``bda`` OCR backend, created on stack create and deleted on stack
delete. The project name is derived from the stack name so multiple stacks in
one account no longer share/interfere with a single account-global project.

The project configuration logic (standard-output config, jpeg/png->DOCUMENT
modality-routing override, routing-override repair, find/create/delete) is
**duplicated** from ``lib/idp_common_pkg/idp_common/bda/bda_ocr.py`` rather than
imported, because SAM's makefile builder runs against an isolated copy of this
function's CodeUri and cannot reach ``lib/`` at build time (and importing
``idp_common.bda`` would also pull the heavy ``bda_service`` / ``bda_invocation``
chain via ``idp_common/bda/__init__.py``). The library module is the canonical,
unit-tested source; the inlined config/sanitizer helpers must stay behaviorally
identical to it — a drift guard
(``tests/test_index.py::test_inlined_helpers_match_library``) asserts they
produce the same output. If you change one, change the other.
"""

import logging
import os
import re
import time

import boto3
import cfnresponse
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoRegionError,
    UnknownServiceError,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Suffix appended to the (sanitized) stack name to form the OCR project name.
# BDA project names must match ``^[a-zA-Z0-9-_]+$`` (max 128 chars); underscores
# are allowed, so ``<stackname>_OCR_StdOutput`` is valid.
OCR_PROJECT_NAME_SUFFIX = "_OCR_StdOutput"
_MAX_PROJECT_NAME_LEN = 128


def sanitize_ocr_project_name(stack_name):
    """Build the stack-scoped OCR project name from a stack name.

    Mirrors ``idp_common.bda.bda_ocr.sanitize_ocr_project_name``.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", stack_name or "")
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    max_stack_len = _MAX_PROJECT_NAME_LEN - len(OCR_PROJECT_NAME_SUFFIX)
    sanitized = sanitized[:max_stack_len].strip("-")
    return f"{sanitized}{OCR_PROJECT_NAME_SUFFIX}"


def build_standard_output_config():
    """Standard-output-only configuration for a pure-OCR SYNC project."""
    return {
        "document": {
            "extraction": {
                "granularity": {"types": ["PAGE", "ELEMENT", "WORD", "LINE"]},
                "boundingBox": {"state": "ENABLED"},
            },
            "generativeField": {"state": "DISABLED"},
            "outputFormat": {
                "textFormat": {"types": ["MARKDOWN"]},
                "additionalFileFormat": {"state": "DISABLED"},
            },
        }
    }


def build_override_config():
    """Force jpeg/png inputs to DOCUMENT modality (rendered page images)."""
    return {"modalityRouting": {"jpeg": "DOCUMENT", "png": "DOCUMENT"}}


def _find_project_arn_by_name(client, project_name):
    try:
        paginator = client.get_paginator("list_data_automation_projects")
    except Exception:
        paginator = None

    if paginator is not None:
        for page in paginator.paginate():
            for proj in page.get("projects", []):
                if proj.get("projectName") == project_name:
                    return proj["projectArn"]
        return None

    for proj in client.list_data_automation_projects().get("projects", []):
        if proj.get("projectName") == project_name:
            return proj["projectArn"]
    return None


def _ensure_project_routing_override(client, project_arn):
    """Repair an existing project missing the jpeg/png->DOCUMENT routing."""
    try:
        project = client.get_data_automation_project(projectArn=project_arn)["project"]
    except Exception:
        logger.warning(
            "Could not fetch BDA OCR project %s to verify routing", project_arn
        )
        return

    routing = (project.get("overrideConfiguration") or {}).get("modalityRouting") or {}
    if routing.get("jpeg") == "DOCUMENT" and routing.get("png") == "DOCUMENT":
        return

    logger.info(
        "Updating BDA OCR project %s to add jpeg/png->DOCUMENT routing", project_arn
    )
    client.update_data_automation_project(
        projectArn=project_arn,
        standardOutputConfiguration=build_standard_output_config(),
        overrideConfiguration=build_override_config(),
    )


def find_or_create_ocr_project(client, project_name):
    """Find or create the stack-scoped OCR project; wait for COMPLETED."""
    existing_arn = _find_project_arn_by_name(client, project_name)
    if existing_arn:
        _ensure_project_routing_override(client, existing_arn)
        logger.info("Reusing BDA OCR project %s", existing_arn)
        return existing_arn

    logger.info("Creating BDA OCR project %s", project_name)
    try:
        resp = client.create_data_automation_project(
            projectName=project_name,
            projectDescription="GenAIIDP stack-scoped pure-OCR standard-output project",
            projectStage="LIVE",
            projectType="SYNC",
            standardOutputConfiguration=build_standard_output_config(),
            overrideConfiguration=build_override_config(),
        )
        project_arn = resp["projectArn"]
    except client.exceptions.ConflictException:
        logger.info("BDA OCR project already created concurrently; re-fetching")
        existing_arn = _find_project_arn_by_name(client, project_name)
        if existing_arn:
            _ensure_project_routing_override(client, existing_arn)
            return existing_arn
        raise
    except ClientError as e:
        # Scoped deliberately to the CREATE call: this region/partition will not
        # accept the project shape at all, so there is nothing to preserve. A
        # failure anywhere else (notably the update/repair path above) must keep
        # failing the stack rather than degrade — see UnsupportedProjectShape.
        if _is_unsupported_capability_error(e):
            raise UnsupportedProjectShape(str(e)) from e
        raise

    # A freshly created project is IN_PROGRESS until provisioned (~seconds).
    status = None
    for _ in range(60):
        status = client.get_data_automation_project(projectArn=project_arn)["project"][
            "status"
        ]
        if status == "COMPLETED":
            break
        time.sleep(2)
    else:
        logger.warning(
            "BDA OCR project %s not COMPLETED after ~120s (last status: %s)",
            project_arn,
            status,
        )
    return project_arn


def delete_ocr_project_by_name(client, project_name):
    """Best-effort delete by name; never raise (stack deletion must not fail).

    Both the lookup and the delete are guarded: a project we can't find or
    reach during teardown (including a region where BDA is unavailable) must
    not block stack deletion.
    """
    try:
        arn = _find_project_arn_by_name(client, project_name)
    except Exception:
        logger.warning(
            "Could not list BDA OCR projects to delete %s; skipping",
            project_name,
            exc_info=True,
        )
        return None
    if not arn:
        logger.info("BDA OCR project %s not found; nothing to delete", project_name)
        return None
    try:
        client.delete_data_automation_project(projectArn=arn)
        logger.info("Deleted BDA OCR project %s", arn)
        return arn
    except Exception:
        logger.warning("Failed to delete BDA OCR project %s", arn, exc_info=True)
        return None


# Prefix for the custom resource's physical id; the project name follows it.
_PHYSICAL_ID_PREFIX = "bda-ocr-project/"

# botocore errors that mean "Bedrock Data Automation is not usable in this
# region/partition" rather than a real, fixable failure. Note this is detected
# on the first API *call*, not at client construction: botocore happily builds
# a client (and a synthetic endpoint URL) for a region that has no BDA, and the
# failure only surfaces when the endpoint can't be reached.
_BDA_UNAVAILABLE_ERRORS = (EndpointConnectionError, UnknownServiceError, NoRegionError)

# A reachable BDA endpoint can still refuse to CREATE this project shape. The
# project is a SYNC project whose standardOutputConfiguration carries a
# ``document`` block; GovCloud accepts BDA projects but not that combination:
#
#   ValidationException: Sync project does not support video/audio/document
#   modality in Standard Output Configuration
#
# That is a capability gap, not a fixable misconfiguration, so it is treated
# exactly like "BDA unavailable here" — SUCCESS with an empty ProjectArn —
# rather than failing the nested stack and rolling back a whole deployment that
# may not even use the ``bda`` OCR backend.
#
# SCOPE. The template's ShouldCreateBDAOCRProject condition gates this Lambda
# itself to the ``aws`` partition, so this path can only ever run in a
# COMMERCIAL region — it does not, and cannot, act as a fallback for GovCloud.
# What it covers is a commercial region where BDA is reachable but rejects the
# project shape.
#
# Because commercial is also where a rejection most likely means WE broke the
# project config (rather than the region lacking a capability), the tolerance is
# doubly narrowed:
#   1. It is raised only from the create_data_automation_project call site — not
#      from the update/repair path, where degrading would replace a working
#      project's ARN with "" and silently break a stack that was fine.
#   2. BOTH a "not supported" phrase AND a marker naming the unsupported
#      *capability* must appear, so an ordinary validation error (e.g. a
#      malformed project name) still fails the stack loudly.
_UNSUPPORTED_PHRASES = ("does not support", "is not supported", "not supported")
_UNSUPPORTED_CAPABILITY_MARKERS = (
    "modality",
    "standard output configuration",
    "standardoutputconfiguration",
    "sync project",
    "projecttype",
)


class UnsupportedProjectShape(Exception):
    """BDA refused to create this project shape in this region/partition.

    Raised only from the create call site (see ``_UNSUPPORTED_PHRASES``) so the
    degrade-to-empty-ARN path cannot be reached by a failure on the
    update/repair path, where an empty ARN would clobber a working project.
    """


def _is_unsupported_capability_error(exc):
    """True if exc is a BDA ValidationException about an unsupported capability.

    Matches the GovCloud refusal of a SYNC project carrying a ``document``
    standard-output block (see ``_UNSUPPORTED_CAPABILITY_MARKERS``). Returns
    False for every other ValidationException so genuine misconfiguration still
    fails the stack.
    """
    if not isinstance(exc, ClientError):
        return False
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    if error.get("Code") != "ValidationException":
        return False
    message = str(error.get("Message", "")).lower()
    return any(p in message for p in _UNSUPPORTED_PHRASES) and any(
        m in message for m in _UNSUPPORTED_CAPABILITY_MARKERS
    )


def _delete_project_name(event):
    """Resolve the project name to delete from the resource's physical id.

    On Delete (including the delete half of a rename-triggered replacement),
    the physical id identifies the resource that is actually going away — which
    may carry the *old* name. Prefer it over recomputing from StackName so a
    rename deletes the old project, not the new one. Falls back to StackName if
    the physical id isn't in the expected form (e.g. a legacy resource).
    """
    physical_id = event.get("PhysicalResourceId") or ""
    if physical_id.startswith(_PHYSICAL_ID_PREFIX):
        return physical_id[len(_PHYSICAL_ID_PREFIX) :]
    props = event.get("ResourceProperties", {}) or {}
    stack_name = props.get("StackName") or os.environ.get("STACK_NAME", "")
    return sanitize_ocr_project_name(stack_name)


def handler(event, context):
    logger.info("Event received: %s", event)
    request_type = event.get("RequestType")
    props = event.get("ResourceProperties", {}) or {}
    stack_name = props.get("StackName") or os.environ.get("STACK_NAME", "")
    project_name = sanitize_ocr_project_name(stack_name)
    # Stable physical id keyed on the project name: a name change (stack rename)
    # is treated by CloudFormation as a replace, and the old resource's Delete
    # carries this (old-name) physical id.
    physical_id = f"{_PHYSICAL_ID_PREFIX}{project_name}"

    client = boto3.client("bedrock-data-automation")

    try:
        if request_type == "Delete":
            # Delete the project identified by the physical id (the resource
            # going away), which may differ from StackName on a rename.
            delete_ocr_project_by_name(client, _delete_project_name(event))
            cfnresponse.send(
                event,
                context,
                cfnresponse.SUCCESS,
                {"ProjectArn": ""},
                physicalResourceId=physical_id,
            )
            return

        # Create / Update: find-or-create and return the ARN.
        arn = find_or_create_ocr_project(client, project_name)
        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            {"ProjectArn": arn},
            physicalResourceId=physical_id,
        )
    except _BDA_UNAVAILABLE_ERRORS:
        # Bedrock Data Automation is unavailable in this region/partition (e.g.
        # some GovCloud regions). Degrade gracefully so Textract-only stacks
        # still deploy; the OCR service errors only if the 'bda' backend is
        # actually selected here. This applies to Delete too — a project that
        # could never be created also never needs deleting.
        logger.warning(
            "Bedrock Data Automation unavailable in this region; "
            "returning empty ProjectArn",
            exc_info=True,
        )
        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            {"ProjectArn": ""},
            physicalResourceId=physical_id,
        )
    except UnsupportedProjectShape as e:
        # BDA is reachable but will not create this project shape here. Same
        # graceful degradation as "BDA unavailable". Logged at ERROR, not
        # WARNING: this can only fire in the commercial partition (the resource
        # is gated to `aws`), where the likeliest cause is a regression in the
        # project config rather than a regional capability gap — so it must be
        # visible even though the deploy is allowed to succeed.
        logger.error(
            "Bedrock Data Automation rejected the stack-scoped OCR project as "
            "unsupported in this region (%s); returning empty ProjectArn. The "
            "'bda' OCR backend will be unavailable in this stack.",
            e,
            exc_info=True,
        )
        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            {"ProjectArn": ""},
            physicalResourceId=physical_id,
        )
    except Exception as e:
        logger.error("Error managing BDA OCR project: %s", e, exc_info=True)
        # A real create/update failure: fail the stack op with a clear reason.
        # (Delete's own failures are swallowed inside delete_ocr_project_by_name
        # so teardown is never blocked, and never reach here.)
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {"Error": str(e)},
            physicalResourceId=physical_id,
            reason=str(e),
        )
        raise
