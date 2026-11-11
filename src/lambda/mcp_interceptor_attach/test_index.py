# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The custom resource that attaches the MCP caller interceptor to the gateway, and
detaches it on Delete, so a rollback or a downgrade never leaves the gateway naming
a deleted function. The gateway is the same model-validated fake the
``idp_common.mcp_gateway`` suite uses: moto has no AgentCore Gateway."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ParamValidationError
from botocore.validate import ParamValidator

from idp_common import mcp_gateway

pytestmark = pytest.mark.unit

REGION = "us-east-1"
ARN = "arn:aws:lambda:us-east-1:111122223333:function:interceptor"
ATTACH = Path(__file__).resolve().parent / "index.py"


class FakeGateway:
    """Keeps one configuration; UpdateGateway replaces it (measured behaviour)."""

    def __init__(self):
        real = boto3.client("bedrock-agentcore-control", region_name=REGION)
        self.meta = real.meta
        self.exceptions = real.exceptions
        self._model = real.meta.service_model.operation_model("UpdateGateway")
        self.state = {
            "gatewayId": "gw-1",
            "status": "READY",
            "name": "gw",
            "roleArn": "arn:aws:iam::111122223333:role/r",
            "protocolType": "MCP",
            "protocolConfiguration": {"mcp": {"searchType": "SEMANTIC"}},
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": {
                "customJWTAuthorizer": {
                    "discoveryUrl": "https://example/.well-known/openid-configuration",
                    "allowedClients": ["a"],
                }
            },
        }
        self.updates = []

    def get_gateway(self, gatewayIdentifier):
        return json.loads(json.dumps(self.state))

    def update_gateway(self, **request):
        report = ParamValidator().validate(request, self._model.input_shape)
        if report.has_errors():
            raise ParamValidationError(report=report.generate_report())
        keep = {k: self.state[k] for k in ("gatewayId", "status")}
        self.state = {
            **keep,
            **{k: v for k, v in request.items() if k != "gatewayIdentifier"},
        }
        self.updates.append(request)
        return {}


@pytest.fixture
def attach(monkeypatch):
    sent = []
    cfn = SimpleNamespace(
        SUCCESS="SUCCESS",
        FAILED="FAILED",
        send=lambda event, context, status, data, physical, reason=None: sent.append(
            (status, physical, reason)
        ),
    )
    monkeypatch.setitem(sys.modules, "cfnresponse", cfn)
    spec = importlib.util.spec_from_file_location("mcp_attach_index", ATTACH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    gw = FakeGateway()
    monkeypatch.setattr(module, "_control", lambda _props: gw)
    return SimpleNamespace(module=module, gw=gw, sent=sent)


def event(kind, gateway_id="gw-1", arn=ARN):
    return {
        "RequestType": kind,
        "ResourceProperties": {"GatewayId": gateway_id, "InterceptorArn": arn},
    }


@pytest.mark.parametrize("kind", ["Create", "Update"])
def test_create_and_update_attach(attach, kind):
    attach.module.handler(event(kind), None)
    assert attach.sent == [("SUCCESS", "gw-1|interceptor", None)]
    assert attach.gw.state[
        "interceptorConfigurations"
    ] == mcp_gateway.desired_interceptors(ARN)


def test_a_failed_attach_fails_the_stack_operation(attach, monkeypatch):
    def boom(*_a):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(attach.module.mcp_gateway, "ensure_interceptor", boom)
    attach.module.handler(event("Update"), None)
    assert attach.sent[0][0] == "FAILED" and "AccessDenied" in attach.sent[0][2]


def test_delete_detaches(attach):
    attach.gw.state["interceptorConfigurations"] = mcp_gateway.desired_interceptors(ARN)
    attach.module.handler(event("Delete"), None)
    assert attach.sent[0][0] == "SUCCESS"
    assert "interceptorConfigurations" not in attach.gw.state


def test_a_failed_detach_never_blocks_a_delete_or_rollback(attach, monkeypatch):
    def boom(*_a):
        raise RuntimeError("throttled")

    monkeypatch.setattr(attach.module.mcp_gateway, "detach_interceptor", boom)
    attach.module.handler(event("Delete"), None)
    assert attach.sent[0][0] == "SUCCESS"


def test_no_gateway_in_the_region_has_nothing_to_attach(attach):
    attach.module.handler(event("Create", gateway_id="N/A"), None)
    assert attach.sent[0][0] == "SUCCESS" and attach.gw.updates == []
