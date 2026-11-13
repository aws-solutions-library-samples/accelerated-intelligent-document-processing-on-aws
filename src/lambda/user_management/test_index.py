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
from typing import cast
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.dirname(__file__))

pytestmark = pytest.mark.unit

USERS_TABLE = "test-users-table"
CONFIG_TABLE = "test-config-table"


@pytest.fixture(autouse=True)
def env():
    with patch.dict(
        os.environ,
        {
            "USERS_TABLE_NAME": USERS_TABLE,
            "CONFIGURATION_TABLE_NAME": CONFIG_TABLE,
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
        # The tenant index lives here; with no item, no tenants are declared.
        ddb.create_table(
            TableName=CONFIG_TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
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
            result = index.update_user({"userId": "u-1", "allowedTestSets": ["set-b"]})

        assert result["allowedTestSets"] == ["set-b"]
        assert result["allowedConfigVersions"] == ["v3"]

    def test_explicit_null_clears_that_axis(self, users_table):
        index = _load_index()
        users_table.put_item(
            Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["v3"]),
        )

        with patch.object(index, "sync_user_to_cognito"):
            result = index.update_user({"userId": "u-1", "allowedTestSets": None})

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


class TestUnknownPersona:
    """Only the five Cognito groups are personas: anything else can be neither
    granted nor inferred by User Management."""

    def test_the_personas_are_exactly_the_five_groups(self):
        index = _load_index()
        assert set(index.VALID_PERSONAS) == {
            "Admin",
            "Author",
            "Reviewer",
            "Annotator",
            "Viewer",
        }

    def test_creating_an_unknown_persona_is_refused_before_any_write(self, users_table):
        index = _load_index()
        with (
            patch.object(index, "sync_user_to_cognito") as sync,
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
            with pytest.raises(ValueError, match="Invalid persona: Ghost"):
                index.create_user({"email": "ghost@example.com", "persona": "Ghost"})
        sync.assert_not_called()
        assert users_table.scan()["Items"] == []

    def test_a_stray_unknown_group_confers_no_persona(self):
        index = _load_index()
        pick = index._determine_persona_from_cognito_groups
        assert pick(["Annotator", "Ghost"]) == "Annotator"
        assert pick(["Ghost", "Reviewer"]) == "Reviewer"
        objects = [{"GroupName": "Annotator"}, {"GroupName": "Ghost"}]
        assert index._determine_persona_from_groups(objects) == "Annotator"


class TestMissingCognitoSync:
    def test_create_rolls_back_dynamodb_when_cognito_fails(self, users_table):
        index = _load_index()
        with (
            patch.object(
                index, "sync_user_to_cognito", side_effect=RuntimeError("boom")
            ),
            pytest.raises(RuntimeError),
        ):
            index.create_user({"email": "doomed@example.com", "persona": "Annotator"})

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
        assert profile["allowedTestSets"] == ["w2-synth-freelance-misclassified"]

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
    on what it carries, it has to stay invisible to the ``email`` query, and it has
    to exist **only** for a row that carries a restriction. These run against a
    **real** moto table with the real ``EmailIndex``, which is the only way the
    middle one can actually be observed rather than argued.
    """

    @staticmethod
    def _pointer(users_table):
        return users_table.get_item(Key={"PK": f"SUB#{_SUB}", "SK": f"SUB#{_SUB}"})

    def test_create_user_records_the_sub_and_writes_its_pointer(self, users_table):
        index = _load_index()
        with patch.object(index, "cognito", _cognito_double()):
            index.create_user(
                {
                    "email": "someone@example.com",
                    "persona": "Viewer",
                    "allowedConfigVersions": ["tenant-a"],
                }
            )

        rows = users_table.scan()["Items"]
        row = next(r for r in rows if r["PK"].startswith("USER#"))
        assert row["cognitoSub"] == _SUB
        assert self._pointer(users_table)["Item"]["userId"] == row["userId"]

    def test_an_unrestricted_user_records_the_sub_but_gets_no_pointer(
        self, users_table
    ):
        """The invariant: a pointer exists only for a row carrying a restriction.

        A pointer is read *before* the email join, so it decides the answer. One at
        an unrestricted row pins "unrestricted" ahead of whatever the email join
        would have found — and the Cognito sync creates exactly such rows. The
        ``sub`` is still recorded, because that is what lets the next sync recognise
        the row whatever the address then says.
        """
        index = _load_index()
        with patch.object(index, "cognito", _cognito_double()):
            index.create_user({"email": "someone@example.com", "persona": "Viewer"})

        row = next(
            r for r in users_table.scan()["Items"] if r["PK"].startswith("USER#")
        )
        assert row["cognitoSub"] == _SUB
        assert "Item" not in self._pointer(users_table)

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
            index.create_user(
                {
                    "email": "someone@example.com",
                    "persona": "Viewer",
                    "allowedConfigVersions": ["tenant-a"],
                }
            )
        # The pointer must exist, or this asserts nothing.
        assert "Item" in self._pointer(users_table)

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

    def test_the_sub_is_not_recorded_on_a_row_that_does_not_exist(self, users_table):
        """``update_item`` UPSERTS, so the write must be conditional.

        The back-fill passes a ``userId`` from a scan that can be seconds old, so a
        concurrent ``deleteUser`` is enough to make an unconditional write mint a
        partial row — no ``email``, no ``userId``. Both the sync's scan loop and
        ``list_users`` index those keys directly, and both are on the ``listUsers``
        path, which is the only route into User Management *and* the only thing that
        runs the back-fill. One such item would brick the page for every Admin.
        """
        index = _load_index()

        assert (
            index._record_cognito_sub(users_table, "u-gone", _SUB, scoped=True) is False
        )

        assert "Item" not in users_table.get_item(
            Key={"PK": "USER#u-gone", "SK": "USER#u-gone"}
        )
        assert "Item" not in self._pointer(users_table)
        # And the reader that would have raised on a partial row still works.
        with patch.object(index, "sync_cognito_users_to_dynamodb"):
            assert index.list_users(
                {"identity": {"claims": {"cognito:groups": ["Admin"]}}}
            ) == {"users": []}

    def test_delete_user_removes_the_pointer(self, users_table):
        index = _load_index()
        users_table.put_item(
            Item={
                "PK": "USER#u-9",
                "SK": "USER#u-9",
                "userId": "u-9",
                "email": "gone@example.com",
                "persona": "Viewer",
                "allowedConfigVersions": ["tenant-a"],
                "cognitoSub": _SUB,
            }
        )
        index._record_cognito_sub(users_table, "u-9", _SUB, scoped=True)
        assert "Item" in self._pointer(users_table)

        with patch.object(index, "sync_user_to_cognito"):
            index.delete_user({"userId": "u-9"})

        assert "Item" not in self._pointer(users_table)


class TestUpdateUserMaintainsThePointer:
    """Setting a scope is what makes a pointer worth having; removing it is not."""

    @staticmethod
    def _seed(users_table, **extra):
        item = {
            "PK": "USER#u-1",
            "SK": "USER#u-1",
            "userId": "u-1",
            "email": "scoped@example.com",
            "persona": "Author",
            "cognitoSub": _SUB,
        }
        item.update(extra)
        users_table.put_item(Item=item)

    @staticmethod
    def _pointer(users_table):
        return users_table.get_item(Key={"PK": f"SUB#{_SUB}", "SK": f"SUB#{_SUB}"})

    def test_setting_a_scope_writes_the_pointer(self, users_table):
        index = _load_index()
        self._seed(users_table)
        assert "Item" not in self._pointer(users_table)

        index.update_user({"userId": "u-1", "allowedConfigVersions": ["tenant-a"]})

        assert self._pointer(users_table)["Item"]["userId"] == "u-1"

    def test_removing_a_scope_removes_the_pointer(self, users_table):
        """A pointer left at a now-unrestricted row would pin 'unrestricted'."""
        index = _load_index()
        self._seed(users_table, allowedConfigVersions=["tenant-a"])
        index._record_cognito_sub(users_table, "u-1", _SUB, scoped=True)
        assert "Item" in self._pointer(users_table)

        index.update_user({"userId": "u-1", "allowedConfigVersions": None})

        assert "Item" not in self._pointer(users_table)

    def test_a_row_deleted_between_the_read_and_the_write_is_not_recreated(
        self, users_table
    ):
        """``update_item`` UPSERTS, and the existence check above it is a separate read.

        A concurrent ``deleteUser`` landing between the two would otherwise *recreate*
        the row with only the attributes the update names — no ``email``, no
        ``userId``. Both ``list_users`` and the Cognito sync's scan loop index those
        keys directly, and both are on the ``listUsers`` path, which is the only route
        into User Management *and* the only thing that runs the sub back-fill. The
        caller sees the same "not found" the read would have raised.
        """
        index = _load_index()
        self._seed(users_table)
        real_get_item = users_table.get_item

        def _delete_after_the_read(**kwargs):
            result = real_get_item(**kwargs)
            users_table.delete_item(Key={"PK": "USER#u-1", "SK": "USER#u-1"})
            return result

        with patch.object(index, "dynamodb") as ddb:
            table = MagicMock(wraps=users_table)
            table.get_item.side_effect = _delete_after_the_read
            # By name: the scope write also reads the tenant index from the
            # configuration table, which must not be served by the users table.
            config_table = boto3.resource("dynamodb", region_name="us-east-1").Table(
                CONFIG_TABLE
            )
            ddb.Table.side_effect = lambda name: (
                table if name == USERS_TABLE else config_table
            )
            with pytest.raises(ValueError, match="not found"):
                index.update_user(
                    {"userId": "u-1", "allowedConfigVersions": ["tenant-a"]}
                )

        assert "Item" not in users_table.get_item(
            Key={"PK": "USER#u-1", "SK": "USER#u-1"}
        )
        # And the readers that index `email`/`userId` directly still work.
        with patch.object(index, "sync_cognito_users_to_dynamodb"):
            assert index.list_users(
                {"identity": {"claims": {"cognito:groups": ["Admin"]}}}
            ) == {"users": []}

    def test_a_row_recording_no_sub_gets_no_pointer(self, users_table):
        """Self-healing rather than raising: the next Cognito sync records the sub."""
        index = _load_index()
        self._seed(users_table)
        users_table.update_item(
            Key={"PK": "USER#u-1", "SK": "USER#u-1"},
            UpdateExpression="REMOVE cognitoSub",
        )

        index.update_user({"userId": "u-1", "allowedConfigVersions": ["tenant-a"]})

        assert "Item" not in self._pointer(users_table)


class TestTheBackFill:
    """``listUsers``' Cognito sync is what gives a pre-existing row its pointer.

    That matters for the transition: an administrator cannot *set* a
    config-version scope without going through User Management, which runs this —
    so by the time a scope can be applied to a row written before pointers
    existed, this has already given that row one.

    It also has to match a Cognito account to the **right** row. Matching on the
    address alone is what duplicates a row whose address has changed, and a
    duplicate carries no scope.
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

    @staticmethod
    def _rows(users_table):
        return [r for r in users_table.scan()["Items"] if r["PK"].startswith("USER#")]

    @staticmethod
    def _pointer(users_table, sub=_SUB):
        return users_table.get_item(Key={"PK": f"SUB#{sub}", "SK": f"SUB#{sub}"})

    def _run(self, index, email, sub=_SUB):
        pages = self._page(email, sub)
        with patch.object(index, "cognito", _cognito_double(list_users_pages=pages)):
            index.sync_cognito_users_to_dynamodb()

    def test_a_scoped_row_with_no_sub_gains_one_and_a_pointer(self, users_table):
        index = _load_index()
        users_table.put_item(
            Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["tenant-a"])
        )

        self._run(index, ANNOTATOR_ITEM["email"])

        row = users_table.get_item(Key={"PK": "USER#u-1", "SK": "USER#u-1"})["Item"]
        assert row["cognitoSub"] == _SUB
        assert self._pointer(users_table)["Item"]["userId"] == "u-1"
        assert len(self._rows(users_table)) == 1

    def test_an_unscoped_row_gains_the_sub_and_no_pointer(self, users_table):
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))

        self._run(index, ANNOTATOR_ITEM["email"])

        row = users_table.get_item(Key={"PK": "USER#u-1", "SK": "USER#u-1"})["Item"]
        assert row["cognitoSub"] == _SUB
        assert "Item" not in self._pointer(users_table)

    def test_a_row_whose_address_differs_only_in_case_is_matched_not_duplicated(
        self, users_table
    ):
        """The case this fix exists for, and the commonest cause of divergence.

        DynamoDB matching is exact and mail systems are case-insensitive, so a row
        stored as ``Alice@…`` and a Cognito account reporting ``alice@…`` are the
        same user. Matching on the exact address alone created a **second** row,
        with no scope — and since a pointer is read first, attaching one to the
        duplicate made the lost restriction permanent, so even correcting the
        address no longer restored it.
        """
        index = _load_index()
        users_table.put_item(
            Item=dict(
                ANNOTATOR_ITEM,
                email="Alice@corp.com",
                allowedConfigVersions=["tenant-a_prod"],
            )
        )

        self._run(index, "alice@corp.com")

        rows = self._rows(users_table)
        assert len(rows) == 1, f"a duplicate row was created: {rows}"
        assert rows[0]["userId"] == "u-1"
        assert rows[0]["cognitoSub"] == _SUB
        assert self._pointer(users_table)["Item"]["userId"] == "u-1"

    def test_a_row_diverged_beyond_case_keeps_its_scope_recoverable(self, users_table):
        """A duplicate is still created — but it must not take the pointer.

        When no key matches, the sync cannot tell a renamed user from a new one and
        writes a fresh row, as it always has. What must not happen is that the
        unscoped duplicate gets the pointer: a pointer is read before the email
        join, so correcting the address would no longer restore the scope.
        """
        index = _load_index()
        users_table.put_item(
            Item=dict(
                ANNOTATOR_ITEM,
                email="old.name@corp.com",
                allowedConfigVersions=["tenant-a_prod"],
            )
        )

        self._run(index, "new.name@corp.com")

        assert len(self._rows(users_table)) == 2
        assert "Item" not in self._pointer(users_table), (
            "the pointer must not resolve to the unscoped duplicate, or the lost "
            "restriction becomes permanent"
        )
        # So the original row is still reachable by its own address.
        from boto3.dynamodb.conditions import Key

        page = users_table.query(
            IndexName="EmailIndex",
            KeyConditionExpression=Key("email").eq("old.name@corp.com"),
        )["Items"]
        assert page[0]["allowedConfigVersions"] == ["tenant-a_prod"]

    @pytest.mark.parametrize("first_sub_listed", ["native", "federated"])
    def test_two_live_accounts_on_one_address_settle_the_same_way_either_order(
        self, users_table, first_sub_listed
    ):
        """A native and a federated Cognito account can share one address, both live.

        Both match the same row, so one pass writes a pointer for each sub. The row can
        record only one, so exactly one pointer must survive — and **which** one must
        not depend on the order Cognito happened to list them in. The row snapshot is
        taken before the loop and shared by both, so leaving it stale made a later pass
        read the pre-loop value, believe its own write was superseded, and re-create the
        pointer it had just deleted.
        """
        index = _load_index()
        native, federated = "11111111-aaaa-bbbb-cccc-000000000001", _SUB
        order = (
            [native, federated] if first_sub_listed == "native" else [federated, native]
        )
        users_table.put_item(
            Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["tenant-a"])
        )
        pages = [
            {
                "Users": [
                    {
                        "Username": ANNOTATOR_ITEM["email"],
                        "Attributes": [
                            {"Name": "sub", "Value": sub},
                            {"Name": "email", "Value": ANNOTATOR_ITEM["email"]},
                        ],
                    }
                    for sub in order
                ]
            }
        ]
        with patch.object(index, "cognito", _cognito_double(list_users_pages=pages)):
            index.sync_cognito_users_to_dynamodb()

        row = users_table.get_item(Key={"PK": "USER#u-1", "SK": "USER#u-1"})["Item"]
        surviving = [
            s for s in (native, federated) if "Item" in self._pointer(users_table, s)
        ]
        assert surviving == [row["cognitoSub"]], (
            "exactly one pointer must survive, and it must be the one the row records"
        )

    def test_two_rows_differing_only_in_case_are_reported(self, users_table, caplog):
        """The collision the case-insensitive match resolves by scan order.

        Which row wins then decides which one gets an immutable ``sub`` and its
        pointer, and nothing about that is visible in the result. The pool is created
        with ``CaseSensitive: false``, so this needs rows written outside user
        management — but "it needs an unusual precondition" is not a reason for it to
        be silent.
        """
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM, email="Alice@corp.com"))
        users_table.put_item(
            Item=dict(
                ANNOTATOR_ITEM,
                PK="USER#u-2",
                SK="USER#u-2",
                userId="u-2",
                email="alice@corp.com",
            )
        )

        with caplog.at_level("WARNING"):
            self._run(index, "ALICE@corp.com")

        assert any(
            "differing only in case" in r.getMessage() for r in caplog.records
        ), caplog.text

    def test_a_row_is_matched_by_its_recorded_sub_whatever_the_address_says(
        self, users_table
    ):
        """Once a row records a sub, no later rename can orphan it."""
        index = _load_index()
        users_table.put_item(
            Item=dict(
                ANNOTATOR_ITEM,
                email="old.name@corp.com",
                cognitoSub=_SUB,
                allowedConfigVersions=["tenant-a_prod"],
            )
        )

        self._run(index, "brand.new@corp.com")

        rows = self._rows(users_table)
        assert len(rows) == 1, f"a duplicate row was created: {rows}"
        assert self._pointer(users_table)["Item"]["userId"] == "u-1"

    def test_a_superseded_pointer_is_deleted_when_the_recorded_sub_changes(
        self, users_table
    ):
        """A row records one sub, so the previous one's pointer must not survive.

        A native and a federated Cognito account can share one address and both be
        live, so "the old sub can never authenticate again" does not hold. A pointer
        the old sub still resolves would keep naming this row after the row stopped
        recording it — a pointer out of step with the row it names, which is the
        precondition for the reader's stale-pointer path.
        """
        index = _load_index()
        old_sub = "11111111-2222-3333-4444-555555555555"
        users_table.put_item(
            Item=dict(
                ANNOTATOR_ITEM,
                cognitoSub=old_sub,
                allowedConfigVersions=["tenant-a"],
            )
        )
        index._record_cognito_sub(users_table, "u-1", old_sub, scoped=True)
        assert "Item" in self._pointer(users_table, old_sub)

        self._run(index, ANNOTATOR_ITEM["email"])

        assert "Item" not in self._pointer(users_table, old_sub)
        assert self._pointer(users_table)["Item"]["userId"] == "u-1"

    def test_an_unchanged_unscoped_row_costs_no_writes(self, users_table):
        """The first ``listUsers`` after an upgrade must not write twice per row.

        A row that already records this sub and carries no restriction has nothing to
        change: neither the attribute nor the (absent) pointer would move. This runs
        inside a 30s / 256MB Lambda over every user in the pool.
        """
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM, cognitoSub=_SUB))
        pages = self._page(ANNOTATOR_ITEM["email"], _SUB)

        with patch.object(index, "cognito", _cognito_double(list_users_pages=pages)):
            with patch.object(index, "dynamodb") as ddb:
                table = MagicMock(wraps=users_table)
                ddb.Table.return_value = table
                index.sync_cognito_users_to_dynamodb()

        table.update_item.assert_not_called()
        table.put_item.assert_not_called()
        table.delete_item.assert_not_called()

    def test_back_filling_an_unscoped_row_costs_one_write_not_two(self, users_table):
        """The first ``listUsers`` after an upgrade, where every row is un-backfilled.

        Recording the sub needs one ``update_item``. Deleting a pointer would be a
        second call for an item that cannot exist yet — no pointer has ever been
        written for a row that records no sub — and this runs once per user in the pool
        inside a 30s / 256MB Lambda.
        """
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))  # unscoped, records no sub
        pages = self._page(ANNOTATOR_ITEM["email"], _SUB)

        with patch.object(index, "cognito", _cognito_double(list_users_pages=pages)):
            with patch.object(index, "dynamodb") as ddb:
                table = MagicMock(wraps=users_table)
                ddb.Table.return_value = table
                index.sync_cognito_users_to_dynamodb()

        assert table.update_item.call_count == 1
        table.delete_item.assert_not_called()

    def test_a_row_another_live_account_records_stays_bound_to_it(self, users_table):
        """Two accounts in the pool share an address, and the row records the first.
        Cognito does not make ``email`` unique, and a row belongs to the account it
        records, so the sync leaves it bound to that account whichever order the
        pool lists them in, and creates no second row."""
        index = _load_index()
        first, second = "sub-first-account", "sub-second-account"
        users_table.put_item(
            Item=dict(
                ANNOTATOR_ITEM, cognitoSub=first, allowedConfigVersions=["tenant-a"]
            )
        )
        index._record_cognito_sub(users_table, "u-1", first, scoped=True)
        email = ANNOTATOR_ITEM["email"]
        for order in ((second, first), (first, second)):
            pages = [
                {"Users": [self._page(email, sub)[0]["Users"][0] for sub in order]}
            ]
            with patch.object(
                index, "cognito", _cognito_double(list_users_pages=pages)
            ):
                index.sync_cognito_users_to_dynamodb()
            (row,) = self._rows(users_table)
            assert row["cognitoSub"] == first
            assert self._pointer(users_table, first)["Item"]["userId"] == "u-1"
            assert "Item" not in self._pointer(users_table, second)

    def test_a_row_that_already_records_its_sub_is_not_rewritten(self, users_table):
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM, cognitoSub=_SUB))
        pages = self._page(ANNOTATOR_ITEM["email"], _SUB)

        with patch.object(index, "cognito", _cognito_double(list_users_pages=pages)):
            with patch.object(index, "_record_cognito_sub") as record:
                index.sync_cognito_users_to_dynamodb()

        record.assert_not_called()

    def test_a_newly_synced_user_records_the_sub_and_gets_no_pointer(self, users_table):
        index = _load_index()

        self._run(index, "fresh@example.com")

        rows = self._rows(users_table)
        assert len(rows) == 1
        assert rows[0]["cognitoSub"] == _SUB
        assert "Item" not in self._pointer(users_table)


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
        index._record_cognito_sub(users_table, "u-1", _SUB, scoped=True)

        profile = index.get_my_profile(
            self._event({"email": "Renamed.User@example.com", "sub": _SUB})
        )

        assert profile["userId"] == "u-1"
        assert profile["allowedConfigVersions"] == ["tenant-a"]

    def test_a_stale_pointer_at_an_unscoped_row_falls_back_to_email(self, users_table):
        """The same invariant every other reader of the pointer enforces.

        A pointer at an unscoped row means the writer's invariant is broken. Believing
        it here would show a scoped user an unscoped profile, so the UI would offer
        them every Configuration Profile — a display defect rather than a permission
        one, because the server decides separately, but the same class.
        """
        index = _load_index()
        # The email join finds the SCOPED row; the pointer finds an unscoped one.
        users_table.put_item(
            Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["tenant-a"])
        )
        users_table.put_item(
            Item={
                "PK": "USER#u-unscoped",
                "SK": "USER#u-unscoped",
                "userId": "u-unscoped",
                "email": "someone.else@example.com",
                "persona": "Viewer",
            }
        )
        users_table.put_item(
            Item={
                "PK": f"SUB#{_SUB}",
                "SK": f"SUB#{_SUB}",
                "userId": "u-unscoped",
                "cognitoSub": _SUB,
            }
        )

        profile = index.get_my_profile(
            self._event({"email": ANNOTATOR_ITEM["email"], "sub": _SUB})
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


# ---------------------------------------------------------------------------
# A scope must stay inside one tenant
# ---------------------------------------------------------------------------

TENANTS = [
    {"tenantId": "acme", "prefixes": ["clients/acme/"], "profilePattern": "acme-*"},
    {
        "tenantId": "globex",
        "prefixes": ["clients/globex/"],
        "profilePattern": "globex-*",
    },
]


def _declare_tenants():
    from idp_common.config.tenants import TenantStore

    store = TenantStore(
        boto3.resource("dynamodb", region_name="us-east-1").Table(CONFIG_TABLE)
    )
    for tenant in TENANTS:
        store.put(tenant["tenantId"], tenant["prefixes"], tenant["profilePattern"])


class TestScopeConfinedToOneTenant:
    """``createUser``/``updateUser`` refuse a scope reaching beyond one tenant.

    These are the only two writers of ``allowedConfigVersions``, and nothing
    re-checks a scope after it is stored, so a refusal here is the whole control.
    """

    @pytest.mark.parametrize(
        "scope",
        [
            ["acme-*", "globex-*"],  # two tenants
            ["acme-invoices", "lending"],  # a tenant plus an untenanted profile
            ["a*"],  # outside every tenant but able to match acme's profiles
        ],
    )
    def test_update_refuses_a_scope_crossing_tenants(self, users_table, scope):
        index = _load_index()
        _declare_tenants()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))
        with patch.object(index, "sync_user_to_cognito"):
            with pytest.raises(ValueError, match="tenant"):
                index.update_user({"userId": "u-1", "allowedConfigVersions": scope})
        stored = users_table.get_item(Key={"PK": "USER#u-1", "SK": "USER#u-1"})["Item"]
        assert "allowedConfigVersions" not in stored

    @pytest.mark.parametrize(
        "scope", [["acme-*"], ["acme-invoices", "acme-eu-*"], ["lending", "rvl-*"]]
    )
    def test_update_accepts_a_scope_inside_one_tenant_or_none(self, users_table, scope):
        index = _load_index()
        _declare_tenants()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))
        with patch.object(index, "sync_user_to_cognito"):
            result = index.update_user(
                {"userId": "u-1", "allowedConfigVersions": scope}
            )
        assert result["allowedConfigVersions"] == scope

    def test_create_refuses_before_any_write(self, users_table):
        index = _load_index()
        _declare_tenants()
        with patch.object(index, "sync_user_to_cognito") as sync:
            with pytest.raises(ValueError, match="spans tenants"):
                index.create_user(
                    {
                        "email": "x@example.com",
                        "persona": "Viewer",
                        "allowedConfigVersions": ["acme-*", "globex-*"],
                    }
                )
        sync.assert_not_called()
        assert users_table.scan()["Items"] == []

    def test_clearing_a_scope_is_not_checked(self, users_table):
        index = _load_index()
        _declare_tenants()
        users_table.put_item(
            Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["acme-*"])
        )
        with patch.object(index, "sync_user_to_cognito"):
            result = index.update_user({"userId": "u-1", "allowedConfigVersions": None})
        assert "allowedConfigVersions" not in result

    def test_an_unreadable_tenant_index_refuses_the_write(self, users_table):
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))
        broken = MagicMock()
        broken.return_value.list.side_effect = RuntimeError("throttled")
        with patch.object(index, "TenantStore", broken):
            with pytest.raises(ValueError, match="could not be read"):
                index.update_user(
                    {"userId": "u-1", "allowedConfigVersions": ["acme-*"]}
                )
        stored = users_table.get_item(Key={"PK": "USER#u-1", "SK": "USER#u-1"})["Item"]
        assert "allowedConfigVersions" not in stored

    def test_an_unwired_configuration_table_refuses_the_write(
        self, users_table, monkeypatch
    ):
        index = _load_index()
        monkeypatch.setattr(index, "CONFIGURATION_TABLE_NAME", "")
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))
        with pytest.raises(ValueError, match="not configured"):
            index.update_user({"userId": "u-1", "allowedConfigVersions": ["acme-*"]})


ADMIN_CALLER = {
    "is_admin": True,
    "email": "admin@example.com",
    "username": "admin",
    "groups": ["Admin"],
}


class TestExternalGrantSourcesOwnTheScope:
    """Under ``TenantGrantSource`` IdpClaim or Api, an Admin cannot edit the scope.

    The claim or the entitlement system is the source of truth; an edit here would
    be overwritten at the next grant, or stand in for a grant that was never made.
    """

    @pytest.mark.parametrize("source", ["IdpClaim", "Api"])
    def test_update_refuses_a_config_scope_edit(self, users_table, monkeypatch, source):
        monkeypatch.setenv("TENANT_GRANT_SOURCE", source)
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["v3"]))
        for value in (["v4"], None):
            with pytest.raises(ValueError, match=f"TenantGrantSource={source}"):
                index.update_user({"userId": "u-1", "allowedConfigVersions": value})
        stored = users_table.get_item(Key={"PK": "USER#u-1", "SK": "USER#u-1"})["Item"]
        assert stored["allowedConfigVersions"] == ["v3"]

    @pytest.mark.parametrize("source", ["IdpClaim", "Api"])
    def test_update_still_edits_test_sets(self, users_table, monkeypatch, source):
        monkeypatch.setenv("TENANT_GRANT_SOURCE", source)
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["v3"]))
        with patch.object(index, "sync_user_to_cognito"):
            result = index.update_user({"userId": "u-1", "allowedTestSets": ["s"]})
        assert result["allowedTestSets"] == ["s"]
        assert result["allowedConfigVersions"] == ["v3"]

    def test_create_refuses_a_config_scope_before_any_write(
        self, users_table, monkeypatch
    ):
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "Api")
        index = _load_index()
        with patch.object(index, "sync_user_to_cognito") as sync:
            with pytest.raises(ValueError, match="TenantGrantSource=Api"):
                index.create_user(
                    {
                        "email": "x@example.com",
                        "persona": "Viewer",
                        "allowedConfigVersions": ["v3"],
                    }
                )
        sync.assert_not_called()
        assert users_table.scan()["Items"] == []

    def test_create_without_a_config_scope_is_allowed(self, users_table, monkeypatch):
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "IdpClaim")
        index = _load_index()
        with patch.object(index, "sync_user_to_cognito", return_value=None):
            result = index.create_user({"email": "x@example.com", "persona": "Viewer"})
        assert "allowedConfigVersions" not in result

    def test_manual_still_edits(self, users_table, monkeypatch):
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "Manual")
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))
        with patch.object(index, "sync_user_to_cognito"):
            result = index.update_user(
                {"userId": "u-1", "allowedConfigVersions": ["v3"]}
            )
        assert result["allowedConfigVersions"] == ["v3"]

    def test_a_manual_edit_drops_a_tenant_a_grant_source_recorded(
        self, users_table, monkeypatch
    ):
        """After the stack returns to Manual, the tenant an earlier grant recorded
        no longer describes a scope an Admin edits by hand."""
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "Manual")
        index = _load_index()
        users_table.put_item(
            Item=dict(
                ANNOTATOR_ITEM,
                allowedConfigVersions=["acme-*"],
                scopeTenant="acme",
                scopeSource="idp",
            )
        )
        with patch.object(index, "sync_user_to_cognito"):
            result = index.update_user(
                {"userId": "u-1", "allowedConfigVersions": ["v3"]}
            )
        assert result["allowedConfigVersions"] == ["v3"]
        stored = users_table.get_item(Key={"PK": "USER#u-1", "SK": "USER#u-1"})["Item"]
        assert "scopeTenant" not in stored and "scopeSource" not in stored


class TestSetUserTenant:
    """``setUserTenant``: an entitlement system pushing one user's tenant."""

    def _call(self, index, args, caller=ADMIN_CALLER):
        event = {"info": {"fieldName": "setUserTenant"}, "arguments": args}
        with patch.object(index, "_get_caller_identity", return_value=caller):
            # setUserTenant returns a User; the handler's return type is the union
            # over every operation.
            return cast(dict, index.handler(event, None))

    def _stored(self, users_table):
        return users_table.get_item(Key={"PK": "USER#u-1", "SK": "USER#u-1"})["Item"]

    def test_grants_exactly_the_tenants_pattern(self, users_table, monkeypatch):
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "Api")
        index = _load_index()
        _declare_tenants()
        users_table.put_item(
            Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["globex-*", "lending"])
        )
        result = self._call(
            index, {"email": ANNOTATOR_ITEM["email"], "tenantId": "acme"}
        )
        assert result["allowedConfigVersions"] == ["acme-*"]
        assert result["scopeTenant"] == "acme"
        assert result["scopeSource"] == "api"

    def test_the_response_is_read_back_consistently(self, users_table, monkeypatch):
        """The email index is eventually consistent, so a read through it right
        after the write may return the previous scope. moto's indexes are
        immediate, so the read itself is what is asserted: the base-table row,
        by primary key, strongly consistent, and no index query."""
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "Api")
        index = _load_index()
        _declare_tenants()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))
        real_table = index.dynamodb.Table
        reads = []

        def spying_table(name):
            table = real_table(name)
            if name != index.USERS_TABLE_NAME:
                return table
            get_item, query = table.get_item, table.query

            def spy_get(**kw):
                reads.append(("get", kw.get("ConsistentRead")))
                return get_item(**kw)

            def spy_query(**kw):
                reads.append(("query", kw.get("IndexName")))
                return query(**kw)

            table.get_item, table.query = spy_get, spy_query
            return table

        monkeypatch.setattr(index.dynamodb, "Table", spying_table)
        result = self._call(
            index, {"email": ANNOTATOR_ITEM["email"], "tenantId": "acme"}
        )
        assert result["allowedConfigVersions"] == ["acme-*"]
        assert reads[-1] == ("get", True), reads

    def test_a_revocation_succeeds_with_the_declarations_unreadable(
        self, users_table, monkeypatch
    ):
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "Api")
        index = _load_index()
        users_table.put_item(
            Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["acme-*"])
        )

        def unreadable():
            raise ValueError("The tenant declarations could not be read. Try again.")

        monkeypatch.setattr(index, "_read_tenants", unreadable)
        result = self._call(index, {"email": ANNOTATOR_ITEM["email"], "tenantId": None})
        assert result["allowedConfigVersions"] == ["!no-tenant-granted"]
        with pytest.raises(ValueError, match="could not be read"):
            self._call(index, {"email": ANNOTATOR_ITEM["email"], "tenantId": "acme"})

    def test_no_tenant_writes_the_deny_marker(self, users_table, monkeypatch):
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "Api")
        index = _load_index()
        _declare_tenants()
        users_table.put_item(
            Item=dict(ANNOTATOR_ITEM, allowedConfigVersions=["acme-*"])
        )
        result = self._call(index, {"email": ANNOTATOR_ITEM["email"], "tenantId": None})
        assert result["allowedConfigVersions"] == ["!no-tenant-granted"]
        assert "scopeTenant" not in result

    @pytest.mark.parametrize("source", [None, "Manual", "IdpClaim"])
    def test_refused_unless_the_source_is_api(self, users_table, monkeypatch, source):
        if source is None:
            monkeypatch.delenv("TENANT_GRANT_SOURCE", raising=False)
        else:
            monkeypatch.setenv("TENANT_GRANT_SOURCE", source)
        index = _load_index()
        _declare_tenants()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))
        with pytest.raises(ValueError, match="only when TenantGrantSource is Api"):
            self._call(index, {"email": ANNOTATOR_ITEM["email"], "tenantId": "acme"})
        assert "allowedConfigVersions" not in self._stored(users_table)

    def test_an_undeclared_tenant_is_refused(self, users_table, monkeypatch):
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "Api")
        index = _load_index()
        _declare_tenants()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))
        with pytest.raises(ValueError, match="not declared"):
            self._call(index, {"email": ANNOTATOR_ITEM["email"], "tenantId": "nope"})
        assert "allowedConfigVersions" not in self._stored(users_table)

    def test_an_unknown_user_is_refused_and_not_created(self, users_table, monkeypatch):
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "Api")
        index = _load_index()
        _declare_tenants()
        with pytest.raises(ValueError, match="not found"):
            self._call(index, {"email": "ghost@example.com", "tenantId": "acme"})
        assert users_table.scan()["Items"] == []

    def test_an_admin_is_refused(self, users_table, monkeypatch):
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "Api")
        index = _load_index()
        _declare_tenants()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM, persona="Admin"))
        with pytest.raises(ValueError, match="cross-tenant"):
            self._call(index, {"email": ANNOTATOR_ITEM["email"], "tenantId": "acme"})
        assert "allowedConfigVersions" not in self._stored(users_table)

    def test_a_non_admin_caller_is_refused(self, users_table, monkeypatch):
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "Api")
        index = _load_index()
        users_table.put_item(Item=dict(ANNOTATOR_ITEM))
        caller = dict(ADMIN_CALLER, is_admin=False, groups=["Author"])
        with pytest.raises(Exception, match="Admin group membership required"):
            self._call(
                index,
                {"email": ANNOTATOR_ITEM["email"], "tenantId": "acme"},
                caller=caller,
            )
        assert "allowedConfigVersions" not in self._stored(users_table)
