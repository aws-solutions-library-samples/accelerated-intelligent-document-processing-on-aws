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
_SUB = "d47cb94a-1c2e-4f3a-9b8d-0e1f2a3b4c5d"


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
    """The HOST's UsersTable, with the key schema the host actually declares.

    ``PK``/``SK`` matters and is not decoration: the scope lookup reads two key
    spaces on this table — the ``EmailIndex`` GSI, and a ``SUB#<sub>`` pointer item
    addressed by the base key. A double keyed on anything else answers the pointer
    ``GetItem`` with a ValidationException, so every ``sub``-carrying caller would
    appear to be denied for the right reason while actually being denied for the
    fixture's.
    """
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName=_USERS_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "email", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "EmailIndex",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    return ddb.Table(_USERS_TABLE)


def _put_user(email, allowed=None, *, user_id="u1", sub=None):
    """Seed one host user row, and its ``sub`` pointer when ``sub`` is given.

    The pointer carries no ``email``, which is what keeps it out of
    ``EmailIndex`` — asserted directly in
    ``test_the_sub_pointer_is_absent_from_the_email_index``.
    """
    table = boto3.resource("dynamodb", region_name="us-west-2").Table(_USERS_TABLE)
    row = {
        "PK": f"USER#{user_id}",
        "SK": f"USER#{user_id}",
        "userId": user_id,
        "email": email,
    }
    if allowed is not None:
        row["allowedConfigVersions"] = allowed
    if sub:
        row["cognitoSub"] = sub
    table.put_item(Item=row)
    if sub:
        table.put_item(
            Item={
                "PK": f"SUB#{sub}",
                "SK": f"SUB#{sub}",
                "userId": user_id,
                "cognitoSub": sub,
            }
        )
    return table


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
    _put_user("scoped@x", ["v-mine"])
    resp = _get(mod, "/report", email="scoped@x", groups="[Viewer]")
    body = json.loads(resp["body"])
    assert body["total"] == 1
    assert body["rows"][0]["documentId"] == "mine.pdf"
    assert body["totalPiiRedacted"] == 1


def test_report_list_fails_closed_on_scope_error(mod):
    """A failed UsersTable scope lookup DENIES the report list with a 403.

    Not a 200 carrying an empty row set. No rows are served either way, so both
    are fail-closed on the property that matters — but this is an audit view, and
    an empty report is the truthful answer when nothing was redacted. A 200 makes
    a missing IAM grant, a throttle or a caller with no email claim present to a
    reviewer as "the anonymizer redacted nothing": the UI assigns `rows` straight
    into state and raises its error banner only on a non-2xx. The two
    `/report/{docId}` routes return this same 403 for the identical condition.
    """
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
    assert resp["statusCode"] == 403
    body = json.loads(resp["body"])
    assert "error" in body
    # The denial must not be mistakable for a truthful empty report.
    assert "rows" not in body and "total" not in body


def test_report_list_denial_matches_the_single_row_routes(mod):
    """One condition, one answer, across all three /report routes."""
    audit = _make_table()  # users table intentionally NOT created
    _seed_mapping_doc(audit, _make_mapping_table(), "same.pdf", "secret-v1")

    statuses = {
        _get(mod, path, email="viewer@x", groups="[Viewer]")["statusCode"]
        for path in ("/report", "/report/same.pdf", "/report/same.pdf/mapping")
    }

    assert statuses == {403}


def test_a_pattern_scope_matches_the_rows_it_covers(mod):
    """Scope entries may be globs; a plain `in` test would deny every row."""
    table = _make_table()
    _make_users_table()
    for doc_id, version in (("one.pdf", "tenant-a_v1"), ("two.pdf", "other_v1")):
        table.put_item(
            Item={
                "documentId": doc_id,
                "gsiPk": "ALL",
                "createdAt": "2026-07-22T10:00:00Z",
                "piiCount": 1,
                "originalConfigVersion": version,
            }
        )
    _put_user("glob@x", ["tenant-a_*"], user_id="u9")

    body = json.loads(_get(mod, "/report", email="glob@x", groups="[Viewer]")["body"])

    assert [r["documentId"] for r in body["rows"]] == ["one.pdf"]


def test_a_blank_scope_entry_does_not_become_a_rule(mod):
    """A stray empty string must read as unrestricted, not as "matches nothing"."""
    table = _make_table()
    _make_users_table()
    table.put_item(
        Item={
            "documentId": "one.pdf",
            "gsiPk": "ALL",
            "createdAt": "2026-07-22T10:00:00Z",
            "piiCount": 1,
            "originalConfigVersion": "tenant-a",
        }
    )
    _put_user("blank@x", ["", "  "], user_id="u8")

    body = json.loads(_get(mod, "/report", email="blank@x", groups="[Viewer]")["body"])

    assert body["total"] == 1


def test_an_unstamped_row_is_denied_to_a_scoped_caller(mod):
    """Fails closed: a row naming no config version cannot be proven in scope."""
    table = _make_table()
    _make_users_table()
    table.put_item(
        Item={
            "documentId": "one.pdf",
            "gsiPk": "ALL",
            "createdAt": "2026-07-22T10:00:00Z",
            "piiCount": 1,
        }
    )
    _put_user("scoped@x", ["tenant-a"], user_id="u7")

    body = json.loads(_get(mod, "/report", email="scoped@x", groups="[Viewer]")["body"])

    assert body["total"] == 0


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
    """A scoped non-admin cannot read a row for a version outside their scope.

    Refused as **404**, identical to an id with no record at all. Answering 403 here
    and 404 there would tell a scoped caller which documentIds the audit table holds,
    one bit per request — and that table is an inventory of every document the
    anonymizer touched. A caller whose scope cannot be *evaluated* still gets a 403,
    because that says something about the caller and nothing about the resource.
    """
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
    _put_user("viewer@x", ["other-v1"])
    resp = _get(mod, "/report/doc.pdf", email="viewer@x", groups="[Viewer]")
    assert resp["statusCode"] == 404


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
    """Refused as 404 — see test_report_detail_rbac_denied for why not 403."""
    audit = _make_table()
    _make_users_table()
    _seed_mapping_doc(audit, _make_mapping_table(), "doc1.pdf", "secret-v1")
    # user scoped to a DIFFERENT version
    _put_user("viewer@x", ["other-v1"])
    resp = _get(mod, "/report/doc1.pdf/mapping", email="viewer@x", groups="[Viewer]")
    assert resp["statusCode"] == 404


def test_mapping_allowed_for_in_scope_user(mod):
    audit = _make_table()
    _make_users_table()
    _seed_mapping_doc(audit, _make_mapping_table(), "doc2.pdf", "secret-v1")
    _put_user("ok@x", ["secret-v1"], user_id="u2")
    resp = _get(mod, "/report/doc2.pdf/mapping", email="ok@x", groups="[Viewer]")
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["mapping"]["John Smith"] == "Jane Doe"


def test_mapping_allowed_for_admin(mod):
    audit = _make_table()
    _make_users_table()
    _seed_mapping_doc(audit, _make_mapping_table(), "doc3.pdf", "secret-v1")
    # Admin with a restrictive scope still passes (admin override)
    _put_user("admin@x", ["other-v1"], user_id="a1")
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


# ---- The scope lookup key comes from the `email` claim, and nothing else ----
#
# The lookup is a UsersTable EmailIndex query, and email is the only identifier
# that joins a Cognito principal to a row there. Substituting another identifier
# when the claim is absent looks harmless and is not: an identifier that is not an
# email matches no row, an empty page means "this user has no restriction", and so
# an *unresolvable* caller becomes an *unrestricted* one — on the route that
# reveals the re-identification mapping, and with no AWS fault required.
#
# `test_mapping_fails_closed_on_scope_error` above covers the other half (a lookup
# that errors). An empty page is still deliberately unrestricted, which
# `test_report_list_and_aggregate` relies on.


def _get_with_claims(mod, path, claims):
    event = {
        "rawPath": path,
        "queryStringParameters": {},
        "requestContext": {
            "http": {"method": "GET"},
            "authorizer": {"jwt": {"claims": claims}},
        },
    }
    return mod.lambda_handler(event, None)


@pytest.mark.parametrize(
    "claims",
    [
        # Every Cognito identifier EXCEPT an email.
        {
            "sub": "11111111-2222-3333-4444-555555555555",
            "cognito:username": "viewer",
            "username": "viewer",
            "cognito:groups": "[Viewer]",
        },
        {"email": "", "cognito:groups": "[Viewer]"},
        {"cognito:groups": "[Viewer]"},
    ],
)
def test_mapping_denied_when_the_claims_carry_no_email(mod, claims):
    audit = _make_table()
    _make_users_table()
    _seed_mapping_doc(audit, _make_mapping_table(), "doc6.pdf", "secret-v1")

    resp = _get_with_claims(mod, "/report/doc6.pdf/mapping", claims)

    assert resp["statusCode"] == 403


def test_the_scope_key_is_the_email_claim_alone(mod):
    """No substitute identifier is accepted, whatever else the claims carry."""
    event_claims = {
        "email": "a@example.com",
        "cognito:username": "other",
        "sub": "11111111-2222-3333-4444-555555555555",
    }
    event = {"requestContext": {"authorizer": {"jwt": {"claims": event_claims}}}}

    assert mod._caller_email(event) == "a@example.com"

    del event_claims["email"]
    assert mod._caller_email(event) == ""


def test_the_sub_key_is_the_sub_claim_alone(mod):
    """A body-supplied identifier is never the pointer key."""
    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {"claims": {"sub": _SUB, "cognito:username": "other"}}
            }
        },
        "body": '{"callerSub": "someone-else"}',
    }

    assert mod._caller_sub(event) == _SUB
    assert mod._caller_sub({"body": '{"callerSub": "someone-else"}'}) == ""


# ---- The routes must not report which document ids exist ---------------------
#
# Both single-record routes used to resolve the record BEFORE the caller's scope,
# so a caller whose scope could not be resolved still learnt whether a redaction
# record existed: 404 meant no, 403 meant yes. One bit of the audit table per
# request, to a caller entitled to none of it.


@pytest.mark.parametrize("path", ["/report/{}", "/report/{}/mapping"])
def test_an_unresolvable_caller_cannot_tell_which_ids_exist(mod, path):
    """One answer for both, so the response carries no information about the id."""
    audit = (
        _make_table()
    )  # users table intentionally NOT created → scope cannot resolve
    _seed_mapping_doc(audit, _make_mapping_table(), "present.pdf", "secret-v1")

    present = _get(mod, path.format("present.pdf"), email="viewer@x", groups="[Viewer]")
    absent = _get(mod, path.format("absent.pdf"), email="viewer@x", groups="[Viewer]")

    assert present["statusCode"] == 403
    assert absent["statusCode"] == 403
    assert present["body"] == absent["body"]


@pytest.mark.parametrize("path", ["/report/{}", "/report/{}/mapping"])
def test_a_scoped_caller_cannot_tell_which_out_of_scope_ids_exist(mod, path):
    """The realistic population, and the one the *ordering* fix alone did not cover.

    Resolving the scope before the record closes the oracle for a caller whose scope
    cannot be **evaluated**. A caller whose scope resolves fine but does not cover
    the document is a different and much commoner case — any scoped Viewer or Author
    — and distinguishing "out of scope" from "no such record" hands them the same one
    bit per request over the audit table, which is an inventory of every document the
    anonymizer has touched. Both answer 404, with the same body.
    """
    audit = _make_table()
    _make_users_table()
    _seed_mapping_doc(audit, _make_mapping_table(), "present.pdf", "secret-v1")
    _put_user("viewer@x", ["other-v1"])  # scope resolves, and excludes secret-v1

    present = _get(mod, path.format("present.pdf"), email="viewer@x", groups="[Viewer]")
    absent = _get(mod, path.format("absent.pdf"), email="viewer@x", groups="[Viewer]")

    assert present["statusCode"] == 404
    assert absent["statusCode"] == 404
    assert present["body"] == absent["body"]
    # And the body must not echo the id back either, which would identify the probe
    # in a log or a proxy even where the status does not.
    assert "present.pdf" not in present["body"]

    # An Admin still gets the record, so this is not a blanket refusal.
    assert (
        _get(mod, path.format("present.pdf"), email="admin@x", groups="[Admin]")[
            "statusCode"
        ]
        == 200
    )


def test_a_stale_pointer_at_an_unscoped_row_does_not_widen_the_scope(mod):
    """The pointer leg is read first, so it must not be able to answer 'unrestricted'.

    The host's writer only creates a pointer for a row carrying a restriction. One at
    an unscoped row means that invariant is broken, and believing it would pin
    "unrestricted" ahead of the row the email join finds — on the route that serves a
    re-identification key. It is treated as stale instead.
    """
    audit = _make_table()
    _make_users_table()
    _seed_mapping_doc(audit, _make_mapping_table(), "doc10.pdf", "secret-v1")
    # An unscoped row with a pointer, plus the scoped row the email join should find.
    _put_user("viewer@x", None, user_id="u-unscoped", sub=_SUB)
    _put_user("viewer@x", ["other-v1"], user_id="u-scoped")

    resp = _get_with_claims(
        mod,
        "/report/doc10.pdf/mapping",
        {"email": "viewer@x", "sub": _SUB, "cognito:groups": "[Viewer]"},
    )

    assert resp["statusCode"] == 404


# ---- The sub join: a diverged address must not lift the restriction ----------


def test_a_diverged_email_still_applies_the_scope_through_the_sub(mod):
    """The residual this join closes, on the route that serves a mapping.

    The caller's address no longer matches the one on their row. The ``EmailIndex``
    query therefore finds nothing, which alone means *unrestricted* — and would
    hand this caller a re-identification key for a document outside their scope.
    The ``SUB#<sub>`` pointer finds the row anyway, so it is refused.
    """
    audit = _make_table()
    _make_users_table()
    _seed_mapping_doc(audit, _make_mapping_table(), "doc7.pdf", "secret-v1")
    _put_user("original@x", ["other-v1"], user_id="u-div", sub=_SUB)

    resp = _get_with_claims(
        mod,
        "/report/doc7.pdf/mapping",
        {"email": "Renamed.User@x", "sub": _SUB, "cognito:groups": "[Viewer]"},
    )

    assert resp["statusCode"] == 404


def test_the_sub_pointer_is_absent_from_the_email_index(mod):
    """The property that keeps the pointer from breaking the email join.

    ``EmailIndex`` is keyed on ``email``, so an item without that attribute is not
    indexed. If a pointer ever carried one, a ``Limit=1`` email query could return
    the pointer instead of the row — and a pointer holds no
    ``allowedConfigVersions``, so the scope would silently lift.
    """
    from boto3.dynamodb.conditions import Key

    _make_users_table()
    table = _put_user("shared@x", ["tenant-a"], user_id="u-ptr", sub=_SUB)

    page = table.query(
        IndexName="EmailIndex", KeyConditionExpression=Key("email").eq("shared@x")
    )["Items"]

    assert len(page) == 1
    assert page[0]["PK"] == "USER#u-ptr"


def test_a_row_with_no_pointer_is_still_found_by_email(mod):
    """The transition case: no pointer items exist on an upgraded deployment."""
    audit = _make_table()
    _make_users_table()
    _seed_mapping_doc(audit, _make_mapping_table(), "doc8.pdf", "secret-v1")
    _put_user("plain@x", ["secret-v1"], user_id="u-plain")

    resp = _get_with_claims(
        mod,
        "/report/doc8.pdf/mapping",
        {"email": "plain@x", "sub": _SUB, "cognito:groups": "[Viewer]"},
    )

    assert resp["statusCode"] == 200


def test_a_sub_only_caller_with_no_pointer_is_denied(mod):
    """Not an answer about this caller, so not read as "unrestricted"."""
    audit = _make_table()
    _make_users_table()
    _seed_mapping_doc(audit, _make_mapping_table(), "doc9.pdf", "secret-v1")

    resp = _get_with_claims(
        mod,
        "/report/doc9.pdf/mapping",
        {"sub": _SUB, "cognito:groups": "[Viewer]"},
    )

    assert resp["statusCode"] == 403
