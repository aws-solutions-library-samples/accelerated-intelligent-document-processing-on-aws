# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`DynamoDBClient.update_item` must be able to express a ConditionExpression.

`put_item` on the same class has accepted one since it was written; `update_item`
did not, and a caller cannot supply one out of band because the method builds its
own parameter dict. So every read-modify-write through this wrapper was
unconditional by construction: two callers that read the same state both succeed,
and the loser's contribution is gone with nothing raising. Issue #1111 catalogues
the sites; several of them cannot be made safe without this parameter.

The interesting assertion is not that the keyword is accepted but that DynamoDB
actually evaluates it, so these tests drive moto rather than a mock — a double that
ignores `ConditionExpression` would pass a keyword-only test while proving nothing
about the write it is supposed to refuse.
"""

import boto3
import pytest
from moto import mock_aws

from idp_common.dynamodb.client import DynamoDBClient, DynamoDBError

TABLE = "condition-test-table"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        ).wait_until_exists()
        yield DynamoDBClient(table_name=TABLE, region="us-east-1")


def test_a_satisfied_condition_lets_the_update_through(client):
    client.put_item({"PK": "doc#1", "Version": 1, "Body": "first"})

    result = client.update_item(
        key={"PK": "doc#1"},
        update_expression="SET Body = :b, Version = :next",
        expression_attribute_values={":b": "second", ":next": 2, ":seen": 1},
        condition_expression="Version = :seen",
    )

    assert result["Attributes"]["Body"] == "second"


def test_a_failed_condition_refuses_the_update_and_names_the_error(client):
    """The case the missing parameter made unreachable.

    A caller that read `Version = 1`, lost the race, and wrote anyway would have
    overwritten the winner's body. With the condition, the loser is told — and the
    error code is the part that matters, because retrying is only correct in
    response to this specific failure and not to a throttle or a validation error.
    """
    client.put_item({"PK": "doc#1", "Version": 1, "Body": "first"})
    # Somebody else commits Version 2 between our read and our write.
    client.update_item(
        key={"PK": "doc#1"},
        update_expression="SET Body = :b, Version = :next",
        expression_attribute_values={":b": "theirs", ":next": 2},
    )

    with pytest.raises(DynamoDBError) as excinfo:
        client.update_item(
            key={"PK": "doc#1"},
            update_expression="SET Body = :b, Version = :next",
            expression_attribute_values={":b": "ours", ":next": 2, ":seen": 1},
            condition_expression="Version = :seen",
        )

    assert excinfo.value.error_code == "ConditionalCheckFailedException"
    # And the winner's write stands.
    assert client.get_item({"PK": "doc#1"})["Body"] == "theirs"


def test_omitting_the_condition_leaves_the_update_unconditional(client):
    """The default has to stay unconditional, or every existing caller changes.

    An absent condition always holds, exactly as in DynamoDB — which is also why the
    parameter is the only way for a caller to detect a lost update.
    """
    client.put_item({"PK": "doc#1", "Version": 1})

    result = client.update_item(
        key={"PK": "doc#1"},
        update_expression="SET Body = :b",
        expression_attribute_values={":b": "written"},
    )

    assert result["Attributes"]["Body"] == "written"


def test_a_condition_can_require_the_item_to_exist(client):
    """`attribute_exists` is what stops an update resurrecting a deleted item.

    DynamoDB's `update_item` is an upsert: with no condition it CREATES the item,
    so a row deleted between a read and the write reappears holding only the
    attributes the expression happens to set. This is the form
    `register_feature_hooks` uses on the configuration profile head.
    """
    with pytest.raises(DynamoDBError) as excinfo:
        client.update_item(
            key={"PK": "doc#absent"},
            update_expression="SET Body = :b",
            expression_attribute_values={":b": "resurrected"},
            condition_expression="attribute_exists(PK)",
        )

    assert excinfo.value.error_code == "ConditionalCheckFailedException"
    assert client.get_item({"PK": "doc#absent"}) is None
