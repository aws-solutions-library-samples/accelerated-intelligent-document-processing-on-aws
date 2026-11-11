# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Changes to the MCP gateway that keep the rest of its configuration
(``idp_common.mcp_gateway``), and the custom resource that attaches the caller
interceptor (``src/lambda/mcp_interceptor_attach``).

moto has no AgentCore Gateway, so the gateway is a fake that keeps one
configuration, validates every ``UpdateGateway`` request against the real botocore
service model, and REPLACES the configuration with the request, which is what the
live API was measured to do (an update omitting the interceptor list removed it).
"""

import json

import boto3
import pytest
from botocore.exceptions import ParamValidationError
from botocore.validate import ParamValidator

from idp_common import mcp_gateway

pytestmark = pytest.mark.unit

REGION = "us-east-1"
ARN = "arn:aws:lambda:us-east-1:111122223333:function:interceptor"


class FakeGateway:
    def __init__(self, interceptors=None, missing=False):
        real = boto3.client("bedrock-agentcore-control", region_name=REGION)
        self.meta = real.meta
        self.exceptions = real.exceptions
        self._model = real.meta.service_model.operation_model("UpdateGateway")
        self.missing = missing
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
                    "allowedClients": ["a", "b"],
                }
            },
            "description": "d",
        }
        if interceptors is not None:
            self.state["interceptorConfigurations"] = interceptors
        self.updates = []

    def get_gateway(self, gatewayIdentifier):
        if self.missing:
            raise self.exceptions.ResourceNotFoundException(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "x"}},
                "GetGateway",
            )
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


def test_attaching_keeps_every_other_field():
    gw = FakeGateway()
    before = dict(gw.state)
    assert mcp_gateway.ensure_interceptor(gw, "gw-1", ARN) is True
    after = gw.state
    for field in ("protocolConfiguration", "authorizerConfiguration", "description"):
        assert after[field] == before[field]
    assert after["interceptorConfigurations"] == mcp_gateway.desired_interceptors(ARN)


def test_an_attached_interceptor_is_left_alone():
    gw = FakeGateway(interceptors=mcp_gateway.desired_interceptors(ARN))
    assert mcp_gateway.ensure_interceptor(gw, "gw-1", ARN) is False
    assert gw.updates == []


def test_the_interceptor_passes_request_headers_at_both_points():
    (config,) = mcp_gateway.desired_interceptors(ARN)
    assert config["inputConfiguration"] == {"passRequestHeaders": True}
    assert sorted(config["interceptionPoints"]) == ["REQUEST", "RESPONSE"]


def test_detaching_removes_only_this_interceptor_and_keeps_the_rest():
    other = {
        "interceptor": {"lambda": {"arn": "arn:aws:lambda:us-east-1:1:function:other"}},
        "interceptionPoints": ["REQUEST"],
        "inputConfiguration": {"passRequestHeaders": False},
    }
    gw = FakeGateway(interceptors=mcp_gateway.desired_interceptors(ARN) + [other])
    assert mcp_gateway.detach_interceptor(gw, "gw-1", ARN) is True
    assert gw.state["interceptorConfigurations"] == [other]
    assert gw.state["protocolConfiguration"] == {"mcp": {"searchType": "SEMANTIC"}}


def test_detaching_the_last_interceptor_omits_the_list():
    gw = FakeGateway(interceptors=mcp_gateway.desired_interceptors(ARN))
    assert mcp_gateway.detach_interceptor(gw, "gw-1", ARN) is True
    assert "interceptorConfigurations" not in gw.state
    assert gw.state["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"]


def test_detaching_from_a_gateway_that_is_gone_is_a_no_op():
    assert (
        mcp_gateway.detach_interceptor(FakeGateway(missing=True), "gw-1", ARN) is False
    )


def test_the_update_fields_come_from_the_service_model():
    fields = set(
        mcp_gateway.gateway_update_fields(
            boto3.client("bedrock-agentcore-control", region_name=REGION)
        )
    )
    assert {
        "authorizerConfiguration",
        "protocolConfiguration",
        "interceptorConfigurations",
    } <= fields
    assert "gatewayIdentifier" not in fields


def test_waiting_gives_up_on_a_failed_gateway(monkeypatch):
    gw = FakeGateway()
    gw.state["status"] = "FAILED"
    with pytest.raises(RuntimeError, match="did not become READY"):
        mcp_gateway.wait_until_ready(gw, "gw-1", attempts=2, delay=0)
