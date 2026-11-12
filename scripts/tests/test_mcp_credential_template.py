# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The template wiring behind MCP service credentials.

Two properties here are invisible to the unit suites, which exercise the handler
and library in isolation: that the registry-removal cleanup exists at all and is
ordered before the interceptor is detached, and that the manager's gateway grant
does not name the gateway's ARN, which the gateway custom resource reports as
``N/A`` where AgentCore is unavailable.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate_premises import load_template  # noqa: E402

pytestmark = pytest.mark.unit

RESOURCES = load_template("template.yaml")["Resources"]


def test_the_cleanup_is_the_managers_and_shares_its_condition():
    cleanup = RESOURCES["McpCredentialsCleanup"]
    manager = RESOURCES["McpCredentialManagerFunction"]
    assert cleanup["Type"] == "Custom::McpCredentialsCleanup"
    assert cleanup["Properties"]["ServiceToken"] == {
        "Fn::GetAtt": "McpCredentialManagerFunction.Arn"
    }
    assert cleanup["Condition"] == manager["Condition"]
    assert RESOURCES["McpCredentialsTable"]["Condition"] == manager["Condition"]


def test_the_cleanup_runs_before_the_interceptor_is_detached():
    depends = RESOURCES["McpCredentialsCleanup"].get("DependsOn")
    depends = [depends] if isinstance(depends, str) else list(depends or [])
    assert "McpGatewayInterceptorAttachment" in depends


def _statements(function):
    for policy in RESOURCES[function]["Properties"].get("Policies") or []:
        if isinstance(policy, dict):
            yield from policy.get("Statement") or []


def test_the_managers_gateway_grant_does_not_name_the_gateway_arn():
    grants = [
        st
        for st in _statements("McpCredentialManagerFunction")
        if "bedrock-agentcore:UpdateGateway" in (st.get("Action") or [])
    ]
    assert grants, "the manager has no UpdateGateway grant"
    for st in grants:
        assert "AgentCoreGateway.GatewayArn" not in str(st["Resource"])
        assert ":gateway/*" in str(st["Resource"])
