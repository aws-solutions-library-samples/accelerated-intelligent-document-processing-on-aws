# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The custom resource that records the stack-created MCP client in the credential
registry (``src/lambda/mcp_default_credential``)."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.unit

HANDLER = Path(__file__).resolve().parent / "index.py"


@pytest.fixture
def world(monkeypatch):
    sent = []
    cfn = SimpleNamespace(
        SUCCESS="SUCCESS",
        FAILED="FAILED",
        send=lambda event, context, status, data, physical, reason=None: sent.append(
            (status, physical, reason)
        ),
    )
    monkeypatch.setitem(sys.modules, "cfnresponse", cfn)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="creds",
            KeySchema=[{"AttributeName": "clientId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "clientId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        spec = importlib.util.spec_from_file_location("mcp_default_index", HANDLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield SimpleNamespace(module=module, table=table, sent=sent)


def event(kind, mode="Off", source="Manual"):
    return {
        "RequestType": kind,
        "ResourceProperties": {
            "TableName": "creds",
            "ClientId": "stack-client",
            "SecretArn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:x",
            "TenantIsolationMode": mode,
            "TenantGrantSource": source,
        },
    }


def row(world):
    return world.table.get_item(Key={"clientId": "stack-client"}).get("Item")


def test_create_registers_the_client_enabled_when_isolation_is_off(world):
    world.module.handler(event("Create"), None)
    assert world.sent == [("SUCCESS", "stack-client|mcp-default-credential", None)]
    assert row(world)["enabled"] is True and row(world)["role"] == "Author"


def test_create_under_strict_registers_it_disabled(world):
    world.module.handler(event("Create", mode="Strict"), None)
    assert row(world)["enabled"] is False


def test_delete_removes_the_row(world):
    world.module.handler(event("Create"), None)
    world.module.handler(event("Delete"), None)
    assert row(world) is None and world.sent[-1][0] == "SUCCESS"


def test_a_failed_registration_fails_the_stack_operation(world, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(world.module.mcp_credentials, "register_stack_default", boom)
    world.module.handler(event("Create"), None)
    assert world.sent[0][0] == "FAILED" and "AccessDenied" in world.sent[0][2]


def test_an_isolation_mode_that_reached_nowhere_registers_it_disabled(
    world, monkeypatch
):
    monkeypatch.delenv("TENANT_ISOLATION_MODE", raising=False)
    e = event("Create")
    del e["ResourceProperties"]["TenantIsolationMode"]
    world.module.handler(e, None)
    assert row(world)["enabled"] is False


def test_the_function_environment_is_used_when_the_property_is_absent(
    world, monkeypatch
):
    monkeypatch.setenv("TENANT_ISOLATION_MODE", "Off")
    e = event("Create")
    del e["ResourceProperties"]["TenantIsolationMode"]
    world.module.handler(e, None)
    assert row(world)["enabled"] is True


def test_a_failed_row_removal_still_lets_the_delete_finish(world, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("table gone")

    monkeypatch.setattr(world.module.mcp_credentials, "remove_stack_default", boom)
    world.module.handler(event("Delete"), None)
    assert world.sent == [("SUCCESS", "stack-client|mcp-default-credential", None)]
