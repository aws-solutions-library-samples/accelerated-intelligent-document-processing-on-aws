# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""user_sync maps every persona to its own Cognito group (IDP-0002 D47).

It used to map every non-Admin persona to Reviewer, so an Author, Annotator or
Viewer created or changed in User Management could become a Reviewer. An unknown
persona now gets no group at all, and that includes the retired QueueReviewer
persona, which has no Cognito group behind it.
"""

import logging
import sys

import boto3
import pytest
from botocore.stub import Stubber

from tests.unit.lambdas._lambda_loader import REPO_ROOT, load_lambda

LAMBDA_DIR = REPO_ROOT / "src" / "lambda" / "user_sync"

sys.path.insert(0, str(LAMBDA_DIR))  # its vendored log_sanitizer
try:
    user_sync = load_lambda("user_sync_persona_index", "src/lambda/user_sync")
finally:
    sys.path.remove(str(LAMBDA_DIR))

POOL = "us-east-1_test"


def _image(persona, email="u@example.com"):
    return {
        "PK": {"S": "USER#u1"},
        "userId": {"S": "u1"},
        "email": {"S": email},
        "persona": {"S": persona},
    }


@pytest.fixture
def cognito(monkeypatch):
    client = boto3.client("cognito-idp", region_name="us-east-1")
    monkeypatch.setattr(user_sync, "cognito", client)
    monkeypatch.setattr(user_sync, "USER_POOL_ID", POOL)
    with Stubber(client) as stub:
        yield stub


def _expect_create(stub, email="u@example.com"):
    stub.add_response("admin_create_user", {"User": {"Username": email}})


@pytest.mark.unit
class TestPersonaGroups:
    @pytest.mark.parametrize(
        ("persona", "group"),
        [
            ("Annotator", "Annotator"),
            ("Viewer", "Viewer"),
            ("Author", "Author"),
            ("Admin", "Admin"),
            ("Reviewer", "Reviewer"),
        ],
    )
    def test_insert_adds_the_personas_own_group(self, cognito, persona, group):
        _expect_create(cognito)
        cognito.add_response(
            "admin_add_user_to_group",
            {},
            {"UserPoolId": POOL, "Username": "u@example.com", "GroupName": group},
        )
        user_sync.handle_user_created({"dynamodb": {"NewImage": _image(persona)}})
        cognito.assert_no_pending_responses()

    @pytest.mark.parametrize("persona", ["Ghost", "QueueReviewer"])
    def test_unknown_persona_gets_no_group(self, cognito, caplog, persona):
        _expect_create(cognito)
        with caplog.at_level(logging.WARNING):
            user_sync.handle_user_created({"dynamodb": {"NewImage": _image(persona)}})
        cognito.assert_no_pending_responses()
        assert "Unknown persona" in caplog.text

    def test_persona_change_moves_between_own_groups(self, cognito):
        cognito.add_response(
            "admin_remove_user_from_group",
            {},
            {"UserPoolId": POOL, "Username": "u@example.com", "GroupName": "Viewer"},
        )
        cognito.add_response(
            "admin_add_user_to_group",
            {},
            {"UserPoolId": POOL, "Username": "u@example.com", "GroupName": "Author"},
        )
        user_sync.handle_user_modified(
            {"dynamodb": {"OldImage": _image("Viewer"), "NewImage": _image("Author")}}
        )
        cognito.assert_no_pending_responses()
