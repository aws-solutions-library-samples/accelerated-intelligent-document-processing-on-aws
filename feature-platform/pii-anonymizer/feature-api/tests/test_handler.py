# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the PII Anonymization feature API (Redaction Report)."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from urllib.parse import quote

import boto3
import pytest
from moto import mock_aws

_HANDLER_DIR = Path(__file__).resolve().parents[1]
_AUDIT_TABLE = "TestRedactionAudit"
_MAPPING_TABLE = "TestRedactionMapping"
_USERS_TABLE = "TestUsers"


def _make_table():
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName=_AUDIT_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "documentId", "AttributeType": "S"},
            {"AttributeName": "gsiPk", "AttributeType": "S"},
            {"AttributeName": "createdAt", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "documentId", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "ByCreatedAt",
                "KeySchema": [
                    {"AttributeName": "gsiPk", "KeyType": "HASH"},
                    {"AttributeName": "createdAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    return ddb.Table(_AUDIT_TABLE)


def _make_mapping_table():
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName=_MAPPING_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "documentId", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "documentId", "KeyType": "HASH"}],
    )
    return ddb.Table(_MAPPING_TABLE)


def _make_users_table():
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName=_USERS_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "email", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "EmailIndex",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    return ddb.Table(_USERS_TABLE)


@pytest.fixture
def mod(monkeypatch):
    """The handler module, imported with moto already active.

    The mock is started HERE rather than with a `@mock_aws` decorator on each
    test, because pytest sets fixtures up *before* it enters the decorated test
    function: a decorator would leave this import — and anything it constructs —
    outside the mock. That is not hypothetical. `handler.py` used to bind
    `boto3.resource("dynamodb")` at module scope, and with an assume-role profile
    in the ambient environment botocore deferred the `sts:AssumeRole` to the first
    signed request, which happened inside the mock; moto served it, registered the
    minted `ASIA…` key in its IAM backend, and thereafter resolved the caller's
    account from that key instead of its own default. The DynamoDB query was then
    routed to a lazily created, empty backend for that account while the tables
    lived under moto's default account — so the FIRST DynamoDB test in the file
    failed with `ResourceNotFoundException` and every later one passed, because
    starting the next mock resets moto's IAM backend.

    The ambient credentials are also replaced with static fakes and `AWS_PROFILE`
    is removed, so this suite cannot resolve a real credential provider chain
    whatever the developer's shell is set to.
    """
    monkeypatch.setenv("AUDIT_TABLE_NAME", _AUDIT_TABLE)
    monkeypatch.setenv("MAPPING_TABLE_NAME", _MAPPING_TABLE)
    monkeypatch.setenv("USERS_TABLE_NAME", _USERS_TABLE)
    monkeypatch.setenv("MAIN_STACK_NAME", "IDP")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        sys.path.insert(0, str(_HANDLER_DIR))
        sys.modules.pop("handler", None)
        m = importlib.import_module("handler")
        sys.path.remove(str(_HANDLER_DIR))
        yield m
        sys.modules.pop("handler", None)


def _get(mod, path, qs=None, *, email="admin@x", groups="[Admin]"):
    event = {
        "rawPath": path,
        "queryStringParameters": qs or {},
        "requestContext": {
            "http": {"method": "GET"},
            "authorizer": {
                "jwt": {"claims": {"email": email, "cognito:groups": groups}}
            },
        },
    }
    return mod.lambda_handler(event, None)


def test_config_route(mod):
    resp = _get(mod, "/config")
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["feature"] == "pii-anonymizer"


def test_report_list_and_aggregate(mod):
    table = _make_table()
    _make_users_table()
    table.put_item(
        Item={
            "documentId": "a.pdf",
            "gsiPk": "ALL",
            "createdAt": "2026-07-22T10:00:00Z",
            "piiCount": 3,
            "mode": "redactcopy_and_stop",
        }
    )
    table.put_item(
        Item={
            "documentId": "b.pdf",
            "gsiPk": "ALL",
            "createdAt": "2026-07-22T11:00:00Z",
            "piiCount": 5,
            "mode": "redactcopy_and_continue",
        }
    )
    resp = _get(mod, "/report")
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["total"] == 2
    assert body["totalPiiRedacted"] == 8
    # newest first (ScanIndexForward=False)
    assert body["rows"][0]["documentId"] == "b.pdf"


def test_report_list_rbac_filters_scoped_user(mod):
    """A non-admin scoped to one config version sees only that version's rows."""
    table = _make_table()
    _make_users_table()
    table.put_item(
        Item={
            "documentId": "mine.pdf",
            "gsiPk": "ALL",
            "createdAt": "2026-07-22T10:00:00Z",
            "piiCount": 1,
            "originalConfigVersion": "v-mine",
        }
    )
    table.put_item(
        Item={
            "documentId": "theirs.pdf",
            "gsiPk": "ALL",
            "createdAt": "2026-07-22T11:00:00Z",
            "piiCount": 9,
            "originalConfigVersion": "v-theirs",
        }
    )
    boto3.resource("dynamodb", region_name="us-west-2").Table(_USERS_TABLE).put_item(
        Item={"id": "u1", "email": "scoped@x", "allowedConfigVersions": ["v-mine"]}
    )
    resp = _get(mod, "/report", email="scoped@x", groups="[Viewer]")
    body = json.loads(resp["body"])
    assert body["total"] == 1
    assert body["rows"][0]["documentId"] == "mine.pdf"
    assert body["totalPiiRedacted"] == 1


def test_report_list_fails_closed_on_scope_error(mod):
    """If the UsersTable scope lookup fails, a non-admin gets an EMPTY list."""
    table = _make_table()  # users table intentionally NOT created
    table.put_item(
        Item={
            "documentId": "a.pdf",
            "gsiPk": "ALL",
            "createdAt": "2026-07-22T10:00:00Z",
            "piiCount": 3,
        }
    )
    resp = _get(mod, "/report", email="viewer@x", groups="[Viewer]")
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["total"] == 0


def test_report_detail(mod):
    table = _make_table()
    _make_users_table()
    table.put_item(
        Item={
            "documentId": "sub/dir/doc.pdf",
            "gsiPk": "ALL",
            "createdAt": "2026-07-22T10:00:00Z",
            "piiCount": 2,
            "redactedKey": "_pii_redacted/sub/dir/doc.pdf",
        }
    )
    resp = _get(mod, f"/report/{quote('sub/dir/doc.pdf', safe='')}")
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["redactedKey"] == "_pii_redacted/sub/dir/doc.pdf"


def test_report_detail_rbac_denied(mod):
    """A scoped non-admin cannot read a row for a version outside their scope."""
    table = _make_table()
    _make_users_table()
    table.put_item(
        Item={
            "documentId": "doc.pdf",
            "gsiPk": "ALL",
            "createdAt": "2026-07-22T10:00:00Z",
            "originalConfigVersion": "secret-v1",
        }
    )
    boto3.resource("dynamodb", region_name="us-west-2").Table(_USERS_TABLE).put_item(
        Item={"id": "u1", "email": "viewer@x", "allowedConfigVersions": ["other-v1"]}
    )
    resp = _get(mod, "/report/doc.pdf", email="viewer@x", groups="[Viewer]")
    assert resp["statusCode"] == 403


def test_report_detail_404(mod):
    _make_table()
    _make_users_table()
    resp = _get(mod, "/report/missing.pdf")
    assert resp["statusCode"] == 404


def test_bad_window(mod):
    _make_table()
    _make_users_table()
    resp = _get(mod, "/report", {"window": "banana"})
    assert resp["statusCode"] == 400


def test_unknown_path(mod):
    resp = _get(mod, "/nope")
    assert resp["statusCode"] == 404


# ---- RBAC-gated PII mapping view -------------------------------------------
#
# The mapping (a re-identification key) lives in a FEATURE-OWNED DynamoDB
# table — never a host-proxyable bucket — and the audit row carries only a
# `mappingStored` boolean.


def _seed_mapping_doc(audit_table, mapping_table, doc_id, original_version):
    mapping_table.put_item(
        Item={
            "documentId": doc_id,
            "originalConfigVersion": original_version,
            "createdAt": "2026-07-23T10:00:00Z",
            "mapping": {"John Smith": "Jane Doe"},
        }
    )
    audit_table.put_item(
        Item={
            "documentId": doc_id,
            "gsiPk": "ALL",
            "createdAt": "2026-07-23T10:00:00Z",
            "mappingStored": True,
            "originalConfigVersion": original_version,
        }
    )


def test_mapping_denied_for_out_of_scope_user(mod):
    audit = _make_table()
    _make_users_table()
    _seed_mapping_doc(audit, _make_mapping_table(), "doc1.pdf", "secret-v1")
    # user scoped to a DIFFERENT version
    boto3.resource("dynamodb", region_name="us-west-2").Table(_USERS_TABLE).put_item(
        Item={"id": "u1", "email": "viewer@x", "allowedConfigVersions": ["other-v1"]}
    )
    resp = _get(mod, "/report/doc1.pdf/mapping", email="viewer@x", groups="[Viewer]")
    assert resp["statusCode"] == 403


def test_mapping_allowed_for_in_scope_user(mod):
    audit = _make_table()
    _make_users_table()
    _seed_mapping_doc(audit, _make_mapping_table(), "doc2.pdf", "secret-v1")
    boto3.resource("dynamodb", region_name="us-west-2").Table(_USERS_TABLE).put_item(
        Item={"id": "u2", "email": "ok@x", "allowedConfigVersions": ["secret-v1"]}
    )
    resp = _get(mod, "/report/doc2.pdf/mapping", email="ok@x", groups="[Viewer]")
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["mapping"]["John Smith"] == "Jane Doe"


def test_mapping_allowed_for_admin(mod):
    audit = _make_table()
    _make_users_table()
    _seed_mapping_doc(audit, _make_mapping_table(), "doc3.pdf", "secret-v1")
    # Admin with a restrictive scope still passes (admin override)
    boto3.resource("dynamodb", region_name="us-west-2").Table(_USERS_TABLE).put_item(
        Item={"id": "a1", "email": "admin@x", "allowedConfigVersions": ["other-v1"]}
    )
    resp = _get(mod, "/report/doc3.pdf/mapping", email="admin@x", groups="[Admin]")
    assert resp["statusCode"] == 200


def test_mapping_fails_closed_on_scope_error(mod):
    """UsersTable lookup failure must DENY the mapping (403), never allow."""
    audit = _make_table()  # users table intentionally NOT created
    _seed_mapping_doc(audit, _make_mapping_table(), "doc4.pdf", "secret-v1")
    resp = _get(mod, "/report/doc4.pdf/mapping", email="viewer@x", groups="[Viewer]")
    assert resp["statusCode"] == 403


def test_mapping_404_when_not_stored(mod):
    audit = _make_table()
    _make_users_table()
    _make_mapping_table()
    audit.put_item(
        Item={
            "documentId": "doc5.pdf",
            "gsiPk": "ALL",
            "createdAt": "2026-07-23T10:00:00Z",
            "mappingStored": False,
        }
    )
    resp = _get(mod, "/report/doc5.pdf/mapping", email="admin@x", groups="[Admin]")
    assert resp["statusCode"] == 404
