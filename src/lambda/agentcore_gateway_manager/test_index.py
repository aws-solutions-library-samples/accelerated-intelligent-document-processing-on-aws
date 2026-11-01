# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for what the AgentCore gateway custom resource leaves behind (#1731).

``bedrock_agentcore_starter_toolkit``'s ``GatewayClient.create_mcp_gateway``
enables "observability" by default: it creates the log group
``/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/<gatewayId>`` and
CloudWatch Logs delivery sources and destinations named after the gateway
id. None of it belongs to CloudFormation, and the custom resource's Delete
removed only the gateway, so every deleted stack left all five behind.

Two halves are pinned here:

* **Create** turns that default off, on both CreateGateway attempts (the
  semantic-search one and the GovCloud fallback without it).
* **Delete** removes the five resources named after the id of the gateway this
  stack owns, which is the cleanup for stacks created by earlier versions. It
  never derives a name from a prefix, so a stack whose gateway is gone deletes
  nothing rather than guessing.

The CloudWatch Logs client is a real botocore client behind a ``Stubber``, so an
operation name or parameter the service model does not have fails the test, and
``ResourceNotFoundException`` is the service's own exception class. The toolkit
is not a test dependency of this repository and is replaced by a recording fake.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.stub import Stubber

_HERE = Path(__file__).resolve().parent
_MODULE_NAME = "agentcore_gateway_manager_index_under_test"

STACK = "IDPLog1101"
GATEWAY_NAME = f"{STACK}-analytics-gateway"
GATEWAY_ID = f"{GATEWAY_NAME.lower()}-abcde12345"
REGION = "us-west-2"
LOG_GROUP = f"/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/{GATEWAY_ID}"

#: The five resources the toolkit creates for a gateway, in deletion order, each
#: as (operation, expected params). Derived from the toolkit's naming, not from
#: the module under test.
OBSERVABILITY_DELETES = [
    ("delete_delivery_source", {"name": f"{GATEWAY_ID}-logs-source"}),
    ("delete_delivery_destination", {"name": f"{GATEWAY_ID}-logs-destination"}),
    ("delete_delivery_source", {"name": f"{GATEWAY_ID}-traces-source"}),
    ("delete_delivery_destination", {"name": f"{GATEWAY_ID}-traces-destination"}),
    ("delete_log_group", {"logGroupName": LOG_GROUP}),
]


class _RecordingGatewayClient:
    """Stand-in for the toolkit's GatewayClient that records create kwargs."""

    calls: list = []
    fail_semantic = False

    def __init__(self, region_name=None):
        self.region_name = region_name

    # The real toolkit's parameter list (0.2.0 through 0.3.14), so a keyword the
    # toolkit does not accept raises TypeError here as it would in Lambda.
    def create_mcp_gateway(
        self,
        name=None,
        role_arn=None,
        authorizer_config=None,
        enable_semantic_search=True,
        enable_observability=True,
        policy_engine_config=None,
    ):
        kwargs = {
            "name": name,
            "role_arn": role_arn,
            "authorizer_config": authorizer_config,
            "enable_semantic_search": enable_semantic_search,
            "enable_observability": enable_observability,
        }
        type(self).calls.append(kwargs)
        if type(self).fail_semantic and kwargs.get("enable_semantic_search"):
            raise RuntimeError(
                "ValidationException: SEMANTIC search type is not supported"
            )
        return {
            "gatewayId": GATEWAY_ID,
            "gatewayUrl": "https://gateway.example/mcp",
            "gatewayArn": f"arn:aws:bedrock-agentcore:{REGION}:111122223333:gateway/{GATEWAY_ID}",
        }

    def create_mcp_gateway_target(self, **kwargs):
        return {}


@pytest.fixture
def index(monkeypatch):
    toolkit = types.ModuleType("bedrock_agentcore_starter_toolkit")
    client_mod = types.ModuleType(
        "bedrock_agentcore_starter_toolkit.operations.gateway.client"
    )
    client_mod.GatewayClient = _RecordingGatewayClient
    for name, module in {
        "bedrock_agentcore_starter_toolkit": toolkit,
        "bedrock_agentcore_starter_toolkit.operations": types.ModuleType("x"),
        "bedrock_agentcore_starter_toolkit.operations.gateway": types.ModuleType("x"),
        "bedrock_agentcore_starter_toolkit.operations.gateway.client": client_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.syspath_prepend(str(_HERE))
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    _RecordingGatewayClient.calls = []
    _RecordingGatewayClient.fail_semantic = False
    return module


def _control_client(gateways):
    """A bedrock-agentcore-control fake whose paginator terminates."""
    control = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"items": gateways}]
    control.get_paginator.return_value = paginator
    control.list_gateway_targets.return_value = {"items": [{"targetId": "t1"}]}
    return control


@pytest.fixture
def logs_stub():
    client = boto3.client(
        "logs",
        region_name=REGION,
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="1234abcd" * 5,
    )
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def _route(monkeypatch, index, control, logs_client):
    def fake_client(service, region_name=None):
        if service == "logs":
            return logs_client
        if service == "bedrock-agentcore-control":
            return control
        raise AssertionError(f"unexpected client {service}")

    monkeypatch.setattr(index.boto3, "client", fake_client)


def _props():
    return {
        "Region": REGION,
        "LambdaArn": "arn:aws:lambda:us-west-2:111122223333:function:f",
        "UserPoolId": "us-west-2_example",
        "ClientId": "client",
        "ExecutionRoleArn": "arn:aws:iam::111122223333:role/r",
        "StackName": STACK,
    }


@pytest.mark.unit
@pytest.mark.parametrize("semantic_supported", [True, False])
def test_create_does_not_enable_toolkit_observability(index, semantic_supported):
    _RecordingGatewayClient.fail_semantic = not semantic_supported

    index.create_gateway(_props(), GATEWAY_NAME, _RecordingGatewayClient(REGION))

    calls = _RecordingGatewayClient.calls
    assert len(calls) == (1 if semantic_supported else 2)
    for kwargs in calls:
        # `is False`, not falsy: omitting the argument takes the toolkit's
        # default, which is True.
        assert kwargs["enable_observability"] is False, kwargs


@pytest.mark.unit
def test_delete_removes_the_observability_resources_of_its_own_gateway(
    index, monkeypatch, logs_stub
):
    logs_client, stubber = logs_stub
    for operation, params in OBSERVABILITY_DELETES:
        stubber.add_response(operation, {}, params)
    control = _control_client([{"name": GATEWAY_NAME, "gatewayId": GATEWAY_ID}])
    _route(monkeypatch, index, control, logs_client)

    index.delete_gateway(_props(), GATEWAY_NAME)

    control.delete_gateway.assert_called_once_with(gatewayIdentifier=GATEWAY_ID)


@pytest.mark.unit
def test_delete_tolerates_resources_that_were_never_created(
    index, monkeypatch, logs_stub
):
    """A stack created with observability off has none of the five."""
    logs_client, stubber = logs_stub
    for operation, params in OBSERVABILITY_DELETES:
        stubber.add_client_error(
            operation,
            service_error_code="ResourceNotFoundException",
            http_status_code=400,
            expected_params=params,
        )
    control = _control_client([{"name": GATEWAY_NAME, "gatewayId": GATEWAY_ID}])
    _route(monkeypatch, index, control, logs_client)

    index.delete_gateway(_props(), GATEWAY_NAME)  # must not raise


@pytest.mark.unit
def test_a_failed_cleanup_is_reported_and_does_not_stop_the_rest(
    index, monkeypatch, logs_stub
):
    logs_client, stubber = logs_stub
    first_op, first_params = OBSERVABILITY_DELETES[0]
    stubber.add_client_error(
        first_op,
        service_error_code="AccessDeniedException",
        http_status_code=400,
        expected_params=first_params,
    )
    for operation, params in OBSERVABILITY_DELETES[1:]:
        stubber.add_response(operation, {}, params)
    control = _control_client([{"name": GATEWAY_NAME, "gatewayId": GATEWAY_ID}])
    _route(monkeypatch, index, control, logs_client)

    with pytest.raises(Exception, match=f"{GATEWAY_ID}-logs-source"):
        index.delete_gateway(_props(), GATEWAY_NAME)


@pytest.mark.unit
def test_delete_without_its_gateway_touches_no_log_resource(index, monkeypatch):
    """No prefix guessing: another stack's gateway shares this one's prefix.

    ``idp1`` and ``IDP1`` are different stacks with the same lowercased gateway
    id prefix, so with this stack's gateway gone the only safe action is none.

    The refusal is at client CREATION, not on a call: cleanup collects every
    exception a logs call raises, including a Stubber's UnStubbedResponseError,
    so an unqueued Stubber here would let a prefix-guessing cleanup pass.
    """
    other = {"name": STACK.lower() + "-analytics-gateway", "gatewayId": GATEWAY_ID}
    control = _control_client([other])

    def fake_client(service, region_name=None):
        if service == "bedrock-agentcore-control":
            return control
        raise AssertionError(f"a {service} client was created")

    monkeypatch.setattr(index.boto3, "client", fake_client)

    index.delete_gateway(_props(), GATEWAY_NAME)

    control.delete_gateway.assert_not_called()


@pytest.mark.unit
def test_the_delete_event_still_succeeds_when_cleanup_fails(
    index, monkeypatch, logs_stub
):
    """A leftover log group must never block stack deletion."""
    logs_client, stubber = logs_stub
    for operation, params in OBSERVABILITY_DELETES:
        stubber.add_client_error(
            operation,
            service_error_code="AccessDeniedException",
            http_status_code=400,
            expected_params=params,
        )
    control = _control_client([{"name": GATEWAY_NAME, "gatewayId": GATEWAY_ID}])
    _route(monkeypatch, index, control, logs_client)
    sent = []
    monkeypatch.setattr(
        index.cfnresponse, "send", lambda *a, **k: sent.append((a[2], k))
    )

    index.handler({"RequestType": "Delete", "ResourceProperties": _props()}, None)

    assert [status for status, _ in sent] == [index.cfnresponse.SUCCESS]
