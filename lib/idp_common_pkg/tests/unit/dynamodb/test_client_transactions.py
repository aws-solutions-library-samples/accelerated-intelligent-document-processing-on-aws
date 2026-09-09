# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`transact_write_items` must not write into the caller's payload.

The method copies each transact item with a shallow `item.copy()`, so the nested
`Put`/`Update`/`Delete` dicts stay shared with the caller — and
`processed_item["Put"]["TableName"] = self.table_name` therefore writes the table
name into the caller's own dict. The payload a caller builds, logs, or reuses
gains keys it never put there.

The nested `Item` values are safe today for a narrower reason worth pinning:
boto3's DynamoDB high-level resource registers `copy_dynamodb_params` on
`provide-client-params.dynamodb`, which deep-copies the parameters before the
`TransformationInjector` serializes them to wire format. Only that handler stands
between a retried payload and double serialization
(`{"M": {"S": {"S": "doc#1"}}}` where `"doc#1"` belonged) — switching this call
to a low-level client, or serializing manually, would lose it. The retry test
locks the observable contract either way.
"""

from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from idp_common.dynamodb.client import DynamoDBClient

TABLE = "tracking-test-table"


def _make_table():
    boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName=TABLE,
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


def _payload():
    return [
        {
            "Put": {
                "Item": {
                    "PK": "doc#1",
                    "SK": "none",
                    "Pages": Decimal(3),
                }
            }
        }
    ]


@pytest.mark.unit
@mock_aws
def test_transact_write_items_does_not_mutate_the_callers_payload(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    _make_table()
    client = DynamoDBClient(table_name=TABLE, region="us-east-1")

    payload = _payload()
    client.transact_write_items(payload)

    assert "TableName" not in payload[0]["Put"], (
        "the table name was injected into the caller's own Put dict"
    )
    assert payload == _payload(), "the transaction altered the caller's payload"


@pytest.mark.unit
@mock_aws
def test_a_retried_transaction_stores_the_same_item(monkeypatch):
    """A caller that retries the same payload after a `DynamoDBError` must write
    the item it meant to write — not a re-serialization of the first attempt's
    wire format."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    _make_table()
    client = DynamoDBClient(table_name=TABLE, region="us-east-1")

    payload = _payload()
    client.transact_write_items(payload)
    client.transact_write_items(payload)  # the retry

    stored = client.table.get_item(Key={"PK": "doc#1", "SK": "none"})["Item"]
    assert stored == {"PK": "doc#1", "SK": "none", "Pages": Decimal(3)}
