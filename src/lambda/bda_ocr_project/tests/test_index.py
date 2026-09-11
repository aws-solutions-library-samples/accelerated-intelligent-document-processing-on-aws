# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the bda_ocr_project custom-resource Lambda handler.

Covers: Create returns SUCCESS + ProjectArn; Delete is best-effort SUCCESS;
a region without Bedrock Data Automation (a call-time EndpointConnectionError,
not a client-construction failure) degrades to SUCCESS + empty ProjectArn on
both Create and Delete; a rename Delete targets the old project via the physical
id; a real create failure reports FAILED; and the inlined helpers stay in sync
with the canonical library module.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError


@pytest.fixture(autouse=True)
def _reload_handler():
    """Import a fresh ``index`` module per test so patches stick."""
    if "index" in sys.modules:
        del sys.modules["index"]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    import index  # noqa: F401

    yield
    if "index" in sys.modules:
        del sys.modules["index"]


def _event(request_type="Create", stack_name="mystack"):
    return {
        "RequestType": request_type,
        "ResourceProperties": {"StackName": stack_name},
        "ResponseURL": "https://example.com/cfn",
        "StackId": "stack-id",
        "RequestId": "req-id",
        "LogicalResourceId": "BDAOCRProject",
    }


class _Conflict(Exception):
    pass


def _mock_bda_client(existing=None, new_arn=None):
    client = MagicMock()
    client.get_paginator.side_effect = Exception("no paginator")
    # A real exception class so `except client.exceptions.ConflictException`
    # is valid (a bare MagicMock is not a catchable type).
    client.exceptions.ConflictException = _Conflict
    client.list_data_automation_projects.return_value = {"projects": existing or []}
    if new_arn:
        client.create_data_automation_project.return_value = {"projectArn": new_arn}
    client.get_data_automation_project.return_value = {
        "project": {"status": "COMPLETED"}
    }
    return client


def test_create_returns_success_with_project_arn():
    import index

    arn = "arn:aws:bedrock:us-east-1:111122223333:data-automation-project/new"
    client = _mock_bda_client(new_arn=arn)
    with patch.object(index.boto3, "client", return_value=client):
        index.handler(_event("Create"), MagicMock())

    args = index.cfnresponse.send.call_args
    assert args.args[2] == index.cfnresponse.SUCCESS
    assert args.args[3] == {"ProjectArn": arn}
    kwargs = client.create_data_automation_project.call_args.kwargs
    assert kwargs["projectName"] == "mystack_OCR_StdOutput"
    assert kwargs["projectType"] == "SYNC"


def test_delete_is_best_effort_success():
    import index

    arn = "arn:aws:bedrock:us-east-1:111122223333:data-automation-project/abc"
    client = _mock_bda_client(
        existing=[{"projectName": "mystack_OCR_StdOutput", "projectArn": arn}]
    )
    with patch.object(index.boto3, "client", return_value=client):
        index.handler(_event("Delete"), MagicMock())

    client.delete_data_automation_project.assert_called_once_with(projectArn=arn)
    args = index.cfnresponse.send.call_args
    assert args.args[2] == index.cfnresponse.SUCCESS
    assert args.args[3] == {"ProjectArn": ""}


def test_delete_swallows_error_and_still_succeeds():
    import index

    arn = "arn:aws:bedrock:us-east-1:111122223333:data-automation-project/abc"
    client = _mock_bda_client(
        existing=[{"projectName": "mystack_OCR_StdOutput", "projectArn": arn}]
    )
    client.delete_data_automation_project.side_effect = Exception("boom")
    with patch.object(index.boto3, "client", return_value=client):
        index.handler(_event("Delete"), MagicMock())

    args = index.cfnresponse.send.call_args
    assert args.args[2] == index.cfnresponse.SUCCESS


def _endpoint_error():
    # botocore builds a client fine in a region without BDA; the failure only
    # surfaces on the first API *call* as an EndpointConnectionError.
    return EndpointConnectionError(endpoint_url="https://bda.example")


def test_bda_unavailable_region_degrades_to_empty_arn_on_create():
    import index

    client = MagicMock()
    client.get_paginator.side_effect = Exception("no paginator")
    client.exceptions.ConflictException = _Conflict
    # The first list call (find-or-create's lookup) hits the dead endpoint.
    client.list_data_automation_projects.side_effect = _endpoint_error()
    with patch.object(index.boto3, "client", return_value=client):
        index.handler(_event("Create"), MagicMock())

    args = index.cfnresponse.send.call_args
    assert args.args[2] == index.cfnresponse.SUCCESS
    assert args.args[3] == {"ProjectArn": ""}
    client.create_data_automation_project.assert_not_called()


def test_bda_unavailable_region_delete_still_succeeds():
    import index

    client = MagicMock()
    client.get_paginator.side_effect = Exception("no paginator")
    client.list_data_automation_projects.side_effect = _endpoint_error()
    with patch.object(index.boto3, "client", return_value=client):
        index.handler(_event("Delete"), MagicMock())

    args = index.cfnresponse.send.call_args
    assert args.args[2] == index.cfnresponse.SUCCESS
    client.delete_data_automation_project.assert_not_called()


# --- Unsupported project shape (GovCloud) -----------------------------------
#
# BDA exists in us-gov-west-1 and CreateDataAutomationProject is reachable, but
# it refuses a SYNC project carrying a `document` standard-output block. This is
# the verbatim message from issue #676. It must degrade like "BDA unavailable"
# instead of failing the nested stack and rolling back the whole deployment.

_GOVCLOUD_SYNC_DOC_MESSAGE = (
    "Sync project does not support video/audio/document modality in "
    "Standard Output Configuration"
)


def _validation_error(message, code="ValidationException"):
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "CreateDataAutomationProject",
    )


def test_govcloud_unsupported_project_shape_degrades_to_empty_arn():
    import index

    client = _mock_bda_client()
    client.create_data_automation_project.side_effect = _validation_error(
        _GOVCLOUD_SYNC_DOC_MESSAGE
    )
    with patch.object(index.boto3, "client", return_value=client):
        # Must NOT raise — a raise fails the custom resource and rolls the stack back.
        index.handler(_event("Create"), MagicMock())

    args = index.cfnresponse.send.call_args
    assert args.args[2] == index.cfnresponse.SUCCESS
    assert args.args[3] == {"ProjectArn": ""}


def test_unsupported_capability_predicate_matches_govcloud_message():
    import index

    assert index._is_unsupported_capability_error(
        _validation_error(_GOVCLOUD_SYNC_DOC_MESSAGE)
    )


@pytest.mark.parametrize(
    "message",
    [
        # Same capability gap, plausible wording variants.
        "Standard Output Configuration is not supported for SYNC projects",
        "projectType SYNC does not support document modality",
    ],
)
def test_unsupported_capability_predicate_matches_variants(message):
    import index

    assert index._is_unsupported_capability_error(_validation_error(message))


@pytest.mark.parametrize(
    ("message", "code"),
    [
        # A genuine misconfiguration must still fail the stack loudly: it names
        # no unsupported capability.
        ("projectName failed to satisfy constraint: ^[a-zA-Z0-9-_]+$",
         "ValidationException"),
        ("1 validation error detected: value at 'projectStage' is invalid",
         "ValidationException"),
        # Right message shape, wrong error code — not a capability gap.
        (_GOVCLOUD_SYNC_DOC_MESSAGE, "AccessDeniedException"),
    ],
)
def test_unsupported_capability_predicate_rejects_real_errors(message, code):
    import index

    assert not index._is_unsupported_capability_error(_validation_error(message, code))


def test_capability_gap_on_update_path_does_not_clobber_a_working_arn():
    """The degrade path must NOT be reachable from the routing-override repair.

    An existing, working project takes the early-return branch of
    find_or_create_ocr_project, which calls update_data_automation_project to add
    the jpeg/png routing override. If a capability-gap ValidationException there
    degraded to SUCCESS + empty ARN, a previously-healthy stack would silently
    lose its BDA_OCR_PROJECT_ARN while the project still existed — strictly worse
    than the "unavailable" case the tolerance models. It must keep failing loudly.
    """
    import index

    arn = "arn:aws:bedrock:us-east-1:111122223333:data-automation-project/live"
    client = _mock_bda_client(
        existing=[{"projectName": "mystack_OCR_StdOutput", "projectArn": arn}]
    )
    # No routing override present, so the repair path runs...
    client.get_data_automation_project.return_value = {
        "project": {"status": "COMPLETED", "overrideConfiguration": {}}
    }
    # ...and the repair is what fails, with a message the matcher would accept.
    client.update_data_automation_project.side_effect = _validation_error(
        _GOVCLOUD_SYNC_DOC_MESSAGE
    )
    with patch.object(index.boto3, "client", return_value=client):
        with pytest.raises(ClientError):
            index.handler(_event("Create"), MagicMock())

    args = index.cfnresponse.send.call_args
    assert args.args[2] == index.cfnresponse.FAILED
    # Crucially: never reported an empty ARN for a project that exists.
    assert args.args[3] != {"ProjectArn": ""}


def test_unsupported_project_shape_is_raised_only_from_the_create_call():
    """The dedicated exception is what carries the degrade signal."""
    import index

    client = _mock_bda_client()
    client.create_data_automation_project.side_effect = _validation_error(
        _GOVCLOUD_SYNC_DOC_MESSAGE
    )
    with pytest.raises(index.UnsupportedProjectShape):
        index.find_or_create_ocr_project(client, "mystack_OCR_StdOutput")


def test_ordinary_validation_error_still_reports_failed():
    """Narrowness guard: a non-capability ValidationException fails the stack."""
    import index

    client = _mock_bda_client()
    client.create_data_automation_project.side_effect = _validation_error(
        "projectName failed to satisfy constraint: ^[a-zA-Z0-9-_]+$"
    )
    with patch.object(index.boto3, "client", return_value=client):
        with pytest.raises(ClientError):
            index.handler(_event("Create"), MagicMock())

    args = index.cfnresponse.send.call_args
    assert args.args[2] == index.cfnresponse.FAILED


def test_delete_targets_old_project_from_physical_id_on_rename():
    """A rename-triggered Delete must delete the OLD project (from the physical
    id), not recompute the new name from StackName."""
    import index

    old_arn = "arn:aws:bedrock:us-east-1:111122223333:data-automation-project/old"
    client = _mock_bda_client(
        existing=[{"projectName": "oldstack_OCR_StdOutput", "projectArn": old_arn}]
    )
    event = _event("Delete", stack_name="newstack")
    event["PhysicalResourceId"] = "bda-ocr-project/oldstack_OCR_StdOutput"
    with patch.object(index.boto3, "client", return_value=client):
        index.handler(event, MagicMock())

    # Deleted the old project named in the physical id, not newstack_*.
    client.delete_data_automation_project.assert_called_once_with(projectArn=old_arn)


def test_create_failure_reports_failed():
    import index

    client = MagicMock()
    client.get_paginator.side_effect = Exception("no paginator")
    client.exceptions.ConflictException = _Conflict
    client.list_data_automation_projects.return_value = {"projects": []}
    client.create_data_automation_project.side_effect = Exception("kaboom")
    with patch.object(index.boto3, "client", return_value=client):
        with pytest.raises(Exception, match="kaboom"):
            index.handler(_event("Create"), MagicMock())

    args = index.cfnresponse.send.call_args
    assert args.args[2] == index.cfnresponse.FAILED


def test_inlined_helpers_match_library():
    """The inlined config/sanitizer helpers must match the canonical library.

    The Lambda duplicates this logic (SAM can't reach lib/ at build time); this
    guards against the two drifting.
    """
    import index
    from idp_common.bda import bda_ocr

    assert (
        index.build_standard_output_config()
        == bda_ocr.build_ocr_project_standard_output_config()
    )
    assert index.build_override_config() == bda_ocr.build_ocr_project_override_config()
    for stack in ("mystack", "My-Stack-123", "weird name.with/chars", "a" * 200):
        assert index.sanitize_ocr_project_name(
            stack
        ) == bda_ocr.sanitize_ocr_project_name(stack)
    assert index.OCR_PROJECT_NAME_SUFFIX == bda_ocr.OCR_PROJECT_NAME_SUFFIX
