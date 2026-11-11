# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``idp_common.chat_session_owner``: one owner per chat session id.

Measured against **moto**'s DynamoDB, not DynamoDB itself. The property this
module rests on is a conditional ``PutItem`` (``attribute_not_exists``) and a
conditional ``UpdateItem`` (``#owner = :owner``), and moto evaluates condition
expressions with its own engine. The race test below injects the lost write
rather than relying on that engine to produce one, so it does not depend on
moto's evaluation order.
"""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_common import chat_session_owner as owner_mod
from idp_common.chat_session_owner import (
    claim_chat_session,
    has_stored_messages,
    holds_session_row,
    recorded_owner,
)

REGION = "us-east-1"


@pytest.fixture
def tables():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        owners = ddb.create_table(
            TableName="owners",
            KeySchema=[{"AttributeName": "sessionId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "sessionId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        messages = ddb.create_table(
            TableName="messages",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        sessions = ddb.create_table(
            TableName="sessions",
            KeySchema=[
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "sessionId", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "sessionId", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield owners, messages, sessions


def _claim(owners, messages, sessions, sid, sub, user_id, *, allow_new, exp=100):
    return claim_chat_session(
        owners,
        sid,
        sub,
        expires_after=exp,
        allow_new=allow_new,
        has_stored_messages=lambda: has_stored_messages(messages, sid),
        holds_earlier_record=lambda: holds_session_row(sessions, user_id, sid),
    )


@pytest.mark.unit
class TestWritePath:
    def test_the_first_user_registers_an_unused_id(self, tables):
        owners, messages, sessions = tables
        assert _claim(owners, messages, sessions, "s1", "sub-a", "a@x", allow_new=True)
        assert recorded_owner(owners, "s1") == "sub-a"

    def test_a_second_user_cannot_use_a_registered_id(self, tables):
        owners, messages, sessions = tables
        assert _claim(owners, messages, sessions, "s1", "sub-a", "a@x", allow_new=True)
        assert not _claim(
            owners, messages, sessions, "s1", "sub-b", "b@x", allow_new=True
        )
        assert recorded_owner(owners, "s1") == "sub-a"

    def test_the_owner_keeps_using_it_and_its_expiry_moves_forward(self, tables):
        owners, messages, sessions = tables
        _claim(
            owners, messages, sessions, "s1", "sub-a", "a@x", allow_new=True, exp=100
        )
        assert _claim(
            owners, messages, sessions, "s1", "sub-a", "a@x", allow_new=True, exp=500
        )
        item = owners.get_item(Key={"sessionId": "s1"})["Item"]
        assert item["ExpiresAfter"] == 500

    def test_an_unowned_id_with_history_is_not_free(self, tables):
        # History with no owner is a session written before the registry existed.
        # Somebody else's session row is not evidence for this caller.
        owners, messages, sessions = tables
        messages.put_item(Item={"PK": "old", "SK": "t1", "content": "A's history"})
        sessions.put_item(Item={"userId": "a@x", "sessionId": "old"})
        assert not _claim(
            owners, messages, sessions, "old", "sub-b", "b@x", allow_new=True
        )
        assert recorded_owner(owners, "old") is None

    def test_the_user_holding_the_earlier_row_registers_it(self, tables):
        owners, messages, sessions = tables
        messages.put_item(Item={"PK": "old", "SK": "t1", "content": "A's history"})
        sessions.put_item(Item={"userId": "a@x", "sessionId": "old"})
        assert _claim(owners, messages, sessions, "old", "sub-a", "a@x", allow_new=True)
        assert recorded_owner(owners, "old") == "sub-a"

    def test_no_owner_sub_owns_nothing(self, tables):
        owners, messages, sessions = tables
        assert not _claim(owners, messages, sessions, "s1", "", "a@x", allow_new=True)
        assert recorded_owner(owners, "s1") is None


@pytest.mark.unit
class TestReadPath:
    def test_reading_an_unused_id_does_not_reserve_it(self, tables):
        owners, messages, sessions = tables
        assert not _claim(
            owners, messages, sessions, "s1", "sub-b", "b@x", allow_new=False
        )
        assert recorded_owner(owners, "s1") is None

    def test_the_row_a_message_used_to_create_is_not_ownership(self, tables):
        # Sending one message under A's id used to create B's own session row,
        # and the history read accepted that row. With A registered, B's row is
        # never consulted.
        owners, messages, sessions = tables
        _claim(owners, messages, sessions, "s1", "sub-a", "a@x", allow_new=True)
        sessions.put_item(Item={"userId": "b@x", "sessionId": "s1"})
        assert not _claim(
            owners, messages, sessions, "s1", "sub-b", "b@x", allow_new=False
        )

    def test_a_session_from_before_the_registry_is_registered_on_first_read(
        self, tables
    ):
        owners, messages, sessions = tables
        messages.put_item(Item={"PK": "old", "SK": "t1"})
        sessions.put_item(Item={"userId": "a@x", "sessionId": "old"})
        assert _claim(
            owners, messages, sessions, "old", "sub-a", "a@x", allow_new=False
        )
        assert recorded_owner(owners, "old") == "sub-a"


@pytest.mark.unit
class TestEdgeCases:
    def test_a_row_without_an_owner_belongs_to_nobody_who_asks(self, tables):
        owners, messages, sessions = tables
        owners.put_item(Item={"sessionId": "s1"})
        assert recorded_owner(owners, "s1") == ""
        assert not _claim(
            owners, messages, sessions, "s1", "sub-a", "a@x", allow_new=True
        )

    def test_losing_the_registration_race_to_another_user_is_a_refusal(self, tables):
        owners, messages, sessions = tables

        class _Racing:
            """Reports the id as free, then finds B's registration on write."""

            def __init__(self, inner):
                self.inner = inner
                self.reads = 0

            def get_item(self, **kw):
                self.reads += 1
                if self.reads == 1:
                    return {}
                return self.inner.get_item(**kw)

            def put_item(self, **kw):
                self.inner.put_item(Item={"sessionId": "s1", "ownerSub": "sub-b"})
                return self.inner.put_item(**kw)

        racing = _Racing(owners)
        assert not claim_chat_session(
            racing,
            "s1",
            "sub-a",
            expires_after=1,
            allow_new=True,
            has_stored_messages=lambda: False,
            holds_earlier_record=lambda: False,
        )
        assert recorded_owner(owners, "s1") == "sub-b"

    def test_a_dynamodb_error_is_raised_not_read_as_yes(self, tables):
        owners, messages, sessions = tables

        class _Failing:
            def get_item(self, **kw):
                raise ClientError(
                    {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                    "GetItem",
                )

        with pytest.raises(ClientError):
            claim_chat_session(
                _Failing(),
                "s1",
                "sub-a",
                expires_after=1,
                allow_new=True,
                has_stored_messages=lambda: False,
                holds_earlier_record=lambda: False,
            )

    def test_the_owner_attribute_is_the_one_every_copy_reads(self):
        assert owner_mod.OWNER_ATTRIBUTE == "ownerSub"
