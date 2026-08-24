# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the user_management Lambda.

Focused on the User response shape. Four read/write paths return a ``User`` and
each used to assemble the dict independently; they drifted, and ``list_users``
silently dropped ``allowedTestSets``. These tests pin the shape at every path so
adding a third scope axis cannot repeat it.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.dirname(__file__))

pytestmark = pytest.mark.unit

USERS_TABLE = "test-users-table"


@pytest.fixture(autouse=True)
def env():
    with patch.dict(
        os.environ,
        {
            "USERS_TABLE_NAME": USERS_TABLE,
            "USER_POOL_ID": "us-east-1_test",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - dummy moto credential
        },
    ):
        yield


@pytest.fixture
def users_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=USERS_TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "email", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "EmailIndex",
                    "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb.Table(USERS_TABLE)


def _load_index():
    """Import the handler fresh so it binds to the moto-backed clients."""
    sys.modules.pop("index", None)
    import index

    return index


ANNOTATOR_ITEM = {
    "PK": "USER#u-1",
    "SK": "USER#u-1",
    "userId": "u-1",
    "email": "annotator@example.com",
    "persona": "Annotator",
    "status": "active",
    "createdAt": "2026-08-03T19:16:47.962351Z",
    "allowedTestSets": ["w2-synth-freelance-misclassified"],
}


class TestUserResponseShape:
    def test_includes_both_scope_axes(self, users_table):
        index = _load_index()
        item = dict(ANNOTATOR_ITEM, allowedConfigVersions=["v3"])
        result = index.user_response_from_item(item)
        assert result["allowedTestSets"] == ["w2-synth-freelance-misclassified"]
        assert result["allowedConfigVersions"] == ["v3"]

    def test_omits_absent_axes(self, users_table):
        index = _load_index()
        result = index.user_response_from_item(
            {
                "userId": "u-2",
                "email": "admin@example.com",
                "persona": "Admin",
                "createdAt": "2026-07-06T18:47:03.509000Z",
            }
        )
        assert "allowedTestSets" not in result
        assert "allowedConfigVersions" not in result
        assert result["status"] == "active"

    def test_list_users_returns_allowed_test_sets(self, users_table):
        """Regression: the Users table showed a scoped annotator as "None assigned".

        list_users copied allowedConfigVersions out of the item but not
        allowedTestSets, so the column was always empty and the Edit scope modal
        — which prefills from this same list — opened blank. Saving that blank
        modal then sent allowedTestSets: null and really did revoke the scope,
        turning a display bug into data loss.
        """
        index = _load_index()
        users_table.put_item(Item=ANNOTATOR_ITEM)

        with (
            patch.object(index, "sync_cognito_users_to_dynamodb"),
            patch.object(
                index,
                "_get_caller_identity",
                return_value={
                    "is_admin": True,
                    "email": "admin@example.com",
                    "username": "admin",
                    "groups": ["Admin"],
                },
            ),
        ):
            result = index.list_users({})

        assert len(result["users"]) == 1
        assert result["users"][0]["allowedTestSets"] == [
            "w2-synth-freelance-misclassified"
        ]

    def test_every_read_path_agrees_on_the_shape(self, users_table):
        """list_users, get_my_profile and update_user must return the same keys.

        Divergence between them is what caused the original bug, and it is
        invisible in any single-path test.
        """
        index = _load_index()
        users_table.put_item(Item=ANNOTATOR_ITEM)
        caller = {
            "is_admin": True,
            "email": ANNOTATOR_ITEM["email"],
            "username": "annotator",
            "groups": ["Admin"],
        }

        with (
            patch.object(index, "sync_cognito_users_to_dynamodb"),
            patch.object(index, "_get_caller_identity", return_value=caller),
            patch.object(index, "sync_user_to_cognito"),
        ):
            listed = index.list_users({})["users"][0]
            profile = index.get_my_profile({})
            updated = index.update_user(
                {
                    "userId": "u-1",
                    "allowedTestSets": ["w2-synth-freelance-misclassified"],
                }
            )

        assert set(listed) == set(profile) == set(updated)
        for path in (listed, profile, updated):
            assert path["allowedTestSets"] == ["w2-synth-freelance-misclassified"]


class TestCreateUserResponse:
    def test_create_returns_the_assigned_test_sets(self, users_table):
        index = _load_index()
        with (
            patch.object(index, "sync_user_to_cognito"),
            patch.object(
                index,
                "_get_caller_identity",
                return_value={
                    "is_admin": True,
                    "email": "admin@example.com",
                    "username": "admin",
                    "groups": ["Admin"],
                },
            ),
        ):
            result = index.create_user(
                {
                    "email": "new-annotator@example.com",
                    "persona": "Annotator",
                    "allowedTestSets": ["set-a"],
                }
            )

        assert result["allowedTestSets"] == ["set-a"]
        assert result["persona"] == "Annotator"
        assert "allowedConfigVersions" not in result


class TestUpdateUserScopeAxes:
    def test_absent_argument_leaves_the_other_axis_alone(self, users_table):
        """A missing arg must mean "don't touch", not "clear".

        The UI clears an axis by sending an explicit null, so keying on
        ``.get()`` rather than presence would strand every scoped user the first
        time an admin edited the other axis.
        """
        index = _load_index()
        users_table.put_item(
            Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["v3"]),
        )

        with patch.object(index, "sync_user_to_cognito"):
            result = index.update_user(
                {"userId": "u-1", "allowedTestSets": ["set-b"]}
            )

        assert result["allowedTestSets"] == ["set-b"]
        assert result["allowedConfigVersions"] == ["v3"]

    def test_explicit_null_clears_that_axis(self, users_table):
        index = _load_index()
        users_table.put_item(
            Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["v3"]),
        )

        with patch.object(index, "sync_user_to_cognito"):
            result = index.update_user(
                {"userId": "u-1", "allowedTestSets": None}
            )

        assert "allowedTestSets" not in result
        assert result["allowedConfigVersions"] == ["v3"]


class TestPersonaPrecedence:
    def test_annotator_outranks_viewer(self):
        index = _load_index()
        assert (
            index._determine_persona_from_cognito_groups(["Viewer", "Annotator"])
            == "Annotator"
        )

    def test_annotator_recognised_from_cognito_group_objects(self):
        index = _load_index()
        groups = [{"GroupName": "Annotator"}, {"GroupName": "Viewer"}]
        assert index._determine_persona_from_groups(groups) == "Annotator"


class TestMissingCognitoSync:
    def test_create_rolls_back_dynamodb_when_cognito_fails(self, users_table):
        index = _load_index()
        with (
            patch.object(
                index, "sync_user_to_cognito", side_effect=RuntimeError("boom")
            ),
            pytest.raises(RuntimeError),
        ):
            index.create_user(
                {"email": "doomed@example.com", "persona": "Annotator"}
            )

        remaining = users_table.scan().get("Items", [])
        assert remaining == []


def test_module_imports_without_aws(monkeypatch):
    """Cold-start safety: import must not require live AWS."""
    monkeypatch.setattr(boto3, "resource", MagicMock())
    monkeypatch.setattr(boto3, "client", MagicMock())
    assert _load_index() is not None
