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
            # Both row-lookup keys, as _get_caller_identity always supplies: this
            # row predates the sub pointer, so only the email route finds it.
            "sub": "",
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


class TestOwnProfileLookupKey:
    """`getMyProfile` finds the caller's row by the `email` claim, and only that.

    The row's key is a `uuid4` minted in `create_user`, so email is the only
    identifier that joins a Cognito principal to it. A substituted identifier does
    not find the row by another route: it matches nothing, or — where the
    substitute happens to be another account's address — the wrong row. So a
    claims set with no email is answered from the verified Cognito groups, with no
    query issued and no scope attributed.
    """

    def _event(self, claims, username=None):
        identity = {"claims": claims}
        if username is not None:
            identity["username"] = username
        return {"info": {"fieldName": "getMyProfile"}, "identity": identity}

    def test_the_email_claim_is_the_lookup_key(self, users_table):
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))

        profile = index.get_my_profile(
            self._event({"email": "annotator@example.com", "sub": "irrelevant"})
        )

        assert profile["userId"] == "u-1"
        assert profile["allowedTestSets"] == [
            "w2-synth-freelance-misclassified"
        ]

    @pytest.mark.parametrize(
        "claims,username",
        [
            ({"sub": "abc-123", "cognito:username": "annotator@example.com"}, None),
            ({"sub": "abc-123"}, "annotator@example.com"),
            ({"email": ""}, "annotator@example.com"),
        ],
    )
    def test_a_substitute_identifier_is_not_used_as_the_key(
        self, users_table, claims, username
    ):
        """Each of these would have matched the stored row via the old chain."""
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))

        profile = index.get_my_profile(self._event(claims, username))

        assert profile["userId"] != "u-1"
        assert "allowedTestSets" not in profile
        assert "allowedConfigVersions" not in profile


def test_module_imports_without_aws(monkeypatch):
    """Cold-start safety: import must not require live AWS."""
    monkeypatch.setattr(boto3, "resource", MagicMock())
    monkeypatch.setattr(boto3, "client", MagicMock())
    assert _load_index() is not None


_SUB = "d47cb94a-1c2e-4f3a-9b8d-0e1f2a3b4c5d"


def _cognito_double(sub=_SUB, list_users_pages=None):
    """A Cognito client double that assigns ``sub`` on create and can be listed."""
    cognito = MagicMock()
    cognito.admin_create_user.return_value = {
        "User": {
            "Username": "someone@example.com",
            "Attributes": [
                {"Name": "sub", "Value": sub},
                {"Name": "email", "Value": "someone@example.com"},
            ],
        }
    }
    cognito.get_paginator.return_value.paginate.return_value = list_users_pages or []
    cognito.admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": "Viewer"}]
    }
    return cognito


class TestTheSubPointerWriter:
    """The writer for the ``sub`` join every scope consumer reads.

    A pointer this module writes and a consumer reads has to agree on its key and
    on what it carries, and it has to stay invisible to the ``email`` query. These
    run against a **real** moto table with the real ``EmailIndex``, which is the
    only way the last of those can actually be observed rather than argued.
    """

    def test_create_user_records_the_sub_and_writes_its_pointer(self, users_table):
        index = _load_index()
        with patch.object(index, "cognito", _cognito_double()):
            index.create_user({"email": "someone@example.com", "persona": "Viewer"})

        rows = users_table.scan()["Items"]
        row = next(r for r in rows if r["PK"].startswith("USER#"))
        assert row["cognitoSub"] == _SUB
        pointer = users_table.get_item(
            Key={"PK": f"SUB#{_SUB}", "SK": f"SUB#{_SUB}"}
        )["Item"]
        assert pointer["userId"] == row["userId"]

    def test_the_pointer_is_absent_from_the_email_index(self, users_table):
        """The one property that keeps the pointer from breaking the email join.

        ``EmailIndex`` is keyed on ``email``, so an item without that attribute is
        not indexed at all. Give a pointer an ``email`` and a ``Limit=1`` email
        query could return the pointer instead of the row — and the pointer carries
        no ``allowedConfigVersions``, so the scope would silently lift. Asserted
        against the real index rather than by reading the writer, because that is
        the thing that would actually go wrong.
        """
        index = _load_index()
        with patch.object(index, "cognito", _cognito_double()):
            index.create_user({"email": "someone@example.com", "persona": "Viewer"})

        from boto3.dynamodb.conditions import Key

        page = users_table.query(
            IndexName="EmailIndex",
            KeyConditionExpression=Key("email").eq("someone@example.com"),
        )["Items"]

        assert len(page) == 1
        assert page[0]["PK"].startswith("USER#")

    def test_a_pointer_write_failure_does_not_fail_the_create(self, users_table):
        """The pointer is an additional route, not a required one."""
        index = _load_index()
        with (
            patch.object(index, "cognito", _cognito_double()),
            patch.object(index, "_record_cognito_sub", return_value=False),
        ):
            created = index.create_user(
                {"email": "someone@example.com", "persona": "Viewer"}
            )

        assert created["email"] == "someone@example.com"

    def test_delete_user_removes_the_pointer(self, users_table):
        index = _load_index()
        users_table.put_item(
            Item={
                "PK": "USER#u-9",
                "SK": "USER#u-9",
                "userId": "u-9",
                "email": "gone@example.com",
                "persona": "Viewer",
                "cognitoSub": _SUB,
            }
        )
        index._record_cognito_sub(users_table, "u-9", _SUB)

        with patch.object(index, "sync_user_to_cognito"):
            index.delete_user({"userId": "u-9"})

        assert "Item" not in users_table.get_item(
            Key={"PK": f"SUB#{_SUB}", "SK": f"SUB#{_SUB}"}
        )


class TestTheBackFill:
    """``listUsers``' Cognito sync is what gives a pre-existing row its pointer.

    That matters for the transition: an administrator cannot *set* a
    config-version scope without going through User Management, which runs this —
    so by the time a scope can be applied to a row written before pointers
    existed, this has already given that row one.
    """

    @staticmethod
    def _page(email, sub):
        return [
            {
                "Users": [
                    {
                        "Username": email,
                        "Attributes": [
                            {"Name": "sub", "Value": sub},
                            {"Name": "email", "Value": email},
                        ],
                    }
                ]
            }
        ]

    def test_an_existing_row_with_no_sub_gains_one_and_a_pointer(self, users_table):
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))
        pages = self._page(ANNOTATOR_ITEM["email"], _SUB)

        with patch.object(index, "cognito", _cognito_double(list_users_pages=pages)):
            index.sync_cognito_users_to_dynamodb()

        row = users_table.get_item(Key={"PK": "USER#u-1", "SK": "USER#u-1"})["Item"]
        assert row["cognitoSub"] == _SUB
        pointer = users_table.get_item(
            Key={"PK": f"SUB#{_SUB}", "SK": f"SUB#{_SUB}"}
        )["Item"]
        assert pointer["userId"] == "u-1"
        # And it did not duplicate the row under a fresh uuid4.
        assert len([r for r in users_table.scan()["Items"] if r["PK"].startswith("USER#")]) == 1

    def test_a_row_that_already_records_its_sub_is_left_alone(self, users_table):
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM, cognitoSub=_SUB))
        pages = self._page(ANNOTATOR_ITEM["email"], _SUB)

        with patch.object(index, "cognito", _cognito_double(list_users_pages=pages)):
            with patch.object(index, "_record_cognito_sub") as record:
                index.sync_cognito_users_to_dynamodb()

        record.assert_not_called()

    def test_a_newly_synced_user_gets_a_pointer_too(self, users_table):
        index = _load_index()
        pages = self._page("fresh@example.com", _SUB)

        with patch.object(index, "cognito", _cognito_double(list_users_pages=pages)):
            index.sync_cognito_users_to_dynamodb()

        rows = [r for r in users_table.scan()["Items"] if r["PK"].startswith("USER#")]
        assert len(rows) == 1
        assert rows[0]["cognitoSub"] == _SUB
        assert "Item" in users_table.get_item(
            Key={"PK": f"SUB#{_SUB}", "SK": f"SUB#{_SUB}"}
        )


class TestGetMyProfileUsesBothKeys:
    """The reader side, over the three row shapes a deployment can hold."""

    @staticmethod
    def _event(claims):
        return {"identity": {"claims": claims}}

    def test_a_diverged_email_still_finds_the_row_through_the_sub(self, users_table):
        """The residual this join closes, at the profile the UI reads its scope from.

        The caller's ``email`` claim no longer matches the address on their row.
        The email query finds nothing, which alone yields a claims-only profile
        carrying no scope at all — so the UI would show an unscoped user. The
        pointer finds the row anyway.
        """
        index = _load_index()
        users_table.put_item(
            Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["tenant-a"])
        )
        index._record_cognito_sub(users_table, "u-1", _SUB)

        profile = index.get_my_profile(
            self._event({"email": "Renamed.User@example.com", "sub": _SUB})
        )

        assert profile["userId"] == "u-1"
        assert profile["allowedConfigVersions"] == ["tenant-a"]

    def test_a_row_with_no_pointer_is_still_found_by_email(self, users_table):
        """The transition case: no pointer items exist yet."""
        index = _load_index()
        users_table.put_item(
            Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["tenant-a"])
        )

        profile = index.get_my_profile(
            self._event({"email": ANNOTATOR_ITEM["email"], "sub": _SUB})
        )

        assert profile["userId"] == "u-1"
        assert profile["allowedConfigVersions"] == ["tenant-a"]

    def test_neither_claim_answers_from_the_verified_groups(self, users_table):
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))

        profile = index.get_my_profile(
            self._event({"cognito:groups": ["Viewer"], "cognito:username": "someone"})
        )

        assert profile["userId"] != "u-1"
        assert "allowedConfigVersions" not in profile
