# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""user_sync deletes the Cognito account a removed user record belongs to.

The UsersTable stream delivers every removal, including the one User Management's
``deleteUser`` makes, so this handler resolves the account the same way: by the
``sub`` the record names, or by address only when the record names none and exactly
one account has it. Run against moto's Cognito, so which account is gone afterwards
is observed.
"""

import sys

import boto3
import pytest
from moto import mock_aws

from tests.unit.lambdas._lambda_loader import REPO_ROOT, load_lambda

LAMBDA_DIR = REPO_ROOT / "src" / "lambda" / "user_sync"

sys.path.insert(0, str(LAMBDA_DIR))  # its vendored log_sanitizer
try:
    user_sync = load_lambda("user_sync_delete_index", "src/lambda/user_sync")
finally:
    sys.path.remove(str(LAMBDA_DIR))

ADDRESS = "shared@example.com"


@pytest.fixture
def pool(monkeypatch):
    with mock_aws():
        client = boto3.client("cognito-idp", region_name="us-east-1")
        pool_id = client.create_user_pool(PoolName="users")["UserPool"]["Id"]
        monkeypatch.setattr(user_sync, "cognito", client)
        monkeypatch.setattr(user_sync, "USER_POOL_ID", pool_id)
        yield client, pool_id


def _account(client, pool_id, username, email=ADDRESS):
    user = client.admin_create_user(
        UserPoolId=pool_id,
        Username=username,
        UserAttributes=[{"Name": "email", "Value": email}],
        MessageAction="SUPPRESS",
    )["User"]
    return next(a["Value"] for a in user["Attributes"] if a["Name"] == "sub")


def _usernames(client, pool_id):
    return sorted(u["Username"] for u in client.list_users(UserPoolId=pool_id)["Users"])


def _removal(sub=None, email=ADDRESS):
    image = {
        "PK": {"S": "USER#u1"},
        "userId": {"S": "u1"},
        "email": {"S": email},
        "persona": {"S": "Viewer"},
    }
    if sub:
        image["cognitoSub"] = {"S": sub}
    return {"eventName": "REMOVE", "dynamodb": {"OldImage": image}}


@pytest.mark.unit
class TestRemovalDeletesTheRecordsOwnAccount:
    def test_a_federated_record_deletes_the_federated_account_only(self, pool):
        client, pool_id = pool
        _account(client, pool_id, ADDRESS)
        federated_sub = _account(client, pool_id, "Okta_00u1")

        user_sync.process_record(_removal(sub=federated_sub))

        assert _usernames(client, pool_id) == [ADDRESS]

    def test_a_record_whose_account_is_gone_deletes_no_other_account(self, pool):
        client, pool_id = pool
        _account(client, pool_id, ADDRESS)

        user_sync.process_record(_removal(sub="0f0e0d0c-aaaa-bbbb-cccc-000000000000"))

        assert _usernames(client, pool_id) == [ADDRESS]

    def test_a_record_with_no_sub_deletes_the_only_account_with_its_address(self, pool):
        client, pool_id = pool
        _account(client, pool_id, ADDRESS)
        _account(client, pool_id, "other@example.com", "other@example.com")

        user_sync.process_record(_removal())

        assert _usernames(client, pool_id) == ["other@example.com"]

    def test_a_record_with_no_sub_and_two_accounts_on_its_address_deletes_none(
        self, pool
    ):
        client, pool_id = pool
        _account(client, pool_id, ADDRESS)
        _account(client, pool_id, "Okta_00u1")

        user_sync.process_record(_removal())

        assert _usernames(client, pool_id) == ["Okta_00u1", ADDRESS]
