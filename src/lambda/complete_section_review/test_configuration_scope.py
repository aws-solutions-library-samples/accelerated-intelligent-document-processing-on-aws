# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Production HITL review is confined to the caller's configuration scope.

Real (moto) UsersTable and TrackingTable, and the real scope lookup: this suite
opts out of the conftest default that treats every caller as unscoped.

What each case asserts is the refusal itself — the shared non-disclosing message,
raised before the document is touched — or, for an allowed caller, that whatever
happens next is not that refusal.
"""

from __future__ import annotations

import importlib
import os
import sys

import boto3
import pytest
from moto import mock_aws

REAL_CONFIGURATION_SCOPE = True

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
TRACKING = "scope-review-tracking"
USERS = "scope-review-users"
IN_SCOPE = "acme/doc.pdf"
OUT_OF_SCOPE = "globex/doc.pdf"
TEST_SET_DOC = "run-1/doc.pdf"
REFUSAL = "^Unauthorized: you are not permitted to act on that document"

FIELDS = [
    ("claimReview", {}),
    ("releaseReview", {}),
    ("skipAllSectionsReview", {"reason": "NOT_NEEDED"}),
    ("saveSectionReviewEdits", {"sectionId": "1", "editedData": "{}"}),
    ("completeSectionReview", {"sectionId": "1", "editedData": "{}"}),
]


def _table(ddb, name, gsi=False):
    attrs = [
        {"AttributeName": "PK", "AttributeType": "S"},
        {"AttributeName": "SK", "AttributeType": "S"},
    ]
    kwargs = {}
    if gsi:
        attrs.append({"AttributeName": "email", "AttributeType": "S"})
        kwargs["GlobalSecondaryIndexes"] = [
            {
                "IndexName": "EmailIndex",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ]
    return ddb.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=attrs,
        BillingMode="PAY_PER_REQUEST",
        **kwargs,
    )


@pytest.fixture
def mod(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - dummy moto credential
        "AWS_SESSION_TOKEN": "testing",  # nosec B105 - dummy moto credential
        "TRACKING_TABLE": TRACKING,
        "TRACKING_TABLE_NAME": TRACKING,
        "USERS_TABLE_NAME": USERS,
        "OUTPUT_BUCKET": "scope-review-output",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TENANT_ISOLATION_MODE", raising=False)
    for name in ("QUEUE_URL", "WORKING_BUCKET", "INPUT_BUCKET", "TEST_SET_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    with mock_aws():
        if MODULE_DIR not in sys.path:
            sys.path.insert(0, MODULE_DIR)
        sys.modules.pop("index", None)
        module = importlib.import_module("index")
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        users = _table(ddb, USERS, gsi=True)
        users.put_item(
            Item={
                "PK": "USER#r",
                "SK": "USER#r",
                "email": "reviewer@example.com",
                "allowedConfigVersions": ["acme-*"],
            }
        )
        users.put_item(
            Item={"PK": "USER#o", "SK": "USER#o", "email": "open@example.com"}
        )
        tracking = _table(ddb, TRACKING)
        for key, extra in (
            (IN_SCOPE, {"ConfigVersion": "acme-invoices"}),
            (OUT_OF_SCOPE, {"ConfigVersion": "globex-w2"}),
            (TEST_SET_DOC, {"ConfigVersion": "globex-w2", "TestSetId": "set-1"}),
        ):
            tracking.put_item(Item={"PK": f"doc#{key}", "SK": "none", **extra})
        module.dynamodb = ddb
        module._user_scope_cache.clear()
        yield module
        sys.modules.pop("index", None)


def _event(field, key, email, groups, **arguments):
    return {
        "info": {"fieldName": field},
        "arguments": {"objectKey": key, **arguments},
        "identity": {
            "username": email,
            "claims": {"cognito:groups": list(groups), "email": email},
        },
    }


@pytest.mark.unit
@pytest.mark.parametrize("field,args", FIELDS)
def test_a_scoped_reviewer_is_refused_another_profiles_document(mod, field, args):
    with pytest.raises(PermissionError, match=REFUSAL):
        mod.handler(
            _event(field, OUT_OF_SCOPE, "reviewer@example.com", ["Reviewer"], **args),
            None,
        )


@pytest.mark.unit
@pytest.mark.parametrize("field,args", FIELDS)
def test_a_scoped_reviewer_passes_the_scope_check_on_their_own_document(
    mod, field, args
):
    _not_the_scope_refusal(
        mod, _event(field, IN_SCOPE, "reviewer@example.com", ["Reviewer"], **args)
    )


@pytest.mark.unit
def test_an_untracked_key_is_refused_to_a_scoped_reviewer(mod):
    with pytest.raises(PermissionError, match=REFUSAL):
        mod.handler(
            _event("claimReview", "nope.pdf", "reviewer@example.com", ["Reviewer"]),
            None,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "email,groups",
    [("admin@example.com", ["Admin"]), ("open@example.com", ["Reviewer"])],
)
def test_an_admin_and_an_unscoped_reviewer_are_unaffected(mod, email, groups):
    _not_the_scope_refusal(mod, _event("claimReview", OUT_OF_SCOPE, email, groups))


@pytest.mark.unit
def test_a_test_set_document_is_left_to_the_annotator_scope(mod):
    # Outside the Reviewer's configuration scope, but a Test Studio document: the
    # configuration check does not apply to it.
    _not_the_scope_refusal(
        mod, _event("claimReview", TEST_SET_DOC, "reviewer@example.com", ["Reviewer"])
    )


@pytest.mark.unit
def test_strict_mode_refuses_an_unscoped_reviewer(mod, monkeypatch):
    monkeypatch.setenv("TENANT_ISOLATION_MODE", "Strict")
    with pytest.raises(PermissionError, match=REFUSAL):
        mod.handler(
            _event("claimReview", IN_SCOPE, "open@example.com", ["Reviewer"]), None
        )
    _not_the_scope_refusal(
        mod, _event("claimReview", IN_SCOPE, "admin@example.com", ["Admin"])
    )


def _not_the_scope_refusal(mod, event):
    """Run the handler; anything may happen next except the scope refusal."""
    try:
        mod.handler(event, None)
    except PermissionError as e:
        assert not str(e).startswith("Unauthorized: you are not permitted to act"), e
    except Exception:  # noqa: BLE001 - later steps need fixtures this suite omits
        pass
