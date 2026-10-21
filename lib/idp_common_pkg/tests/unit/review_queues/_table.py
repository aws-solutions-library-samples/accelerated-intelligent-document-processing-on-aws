# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The HITLTable definition shared by the moto fixtures.

``test_table_parity.py`` pins it to the parent template, so a fixture table can't
drift from the one that deploys.
"""

import boto3

TABLE_NAME = "review-queue-test-table"
REGION = "us-east-1"

TABLE_ATTRIBUTE_DEFINITIONS = [
    {"AttributeName": name, "AttributeType": "S"}
    for name in (
        "PK",
        "SK",
        "QueuePK",
        "QueueSK",
        "OpenPK",
        "OwnerPK",
        "OwnerSK",
        "SweepPK",
        "SweepSK",
    )
]
TABLE_KEY_SCHEMA = [
    {"AttributeName": "PK", "KeyType": "HASH"},
    {"AttributeName": "SK", "KeyType": "RANGE"},
]


def _gsi(name, hash_key, range_key):
    return {
        "IndexName": name,
        "KeySchema": [
            {"AttributeName": hash_key, "KeyType": "HASH"},
            {"AttributeName": range_key, "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    }


TABLE_GSIS = [
    _gsi("QueueIndex", "QueuePK", "QueueSK"),
    _gsi("OpenIndex", "OpenPK", "QueueSK"),
    _gsi("OwnerIndex", "OwnerPK", "OwnerSK"),
    _gsi("SweepIndex", "SweepPK", "SweepSK"),
]


def create_review_table(table_name: str = TABLE_NAME, region: str = REGION) -> str:
    """Create the table (inside an active moto ``mock_aws``); return its name."""
    boto3.resource("dynamodb", region_name=region).create_table(
        TableName=table_name,
        AttributeDefinitions=TABLE_ATTRIBUTE_DEFINITIONS,
        KeySchema=TABLE_KEY_SCHEMA,
        GlobalSecondaryIndexes=TABLE_GSIS,
        BillingMode="PAY_PER_REQUEST",
    )
    return table_name
