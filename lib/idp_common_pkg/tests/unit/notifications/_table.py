# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The NotificationsTable definition shared by the moto fixtures.

Kept outside ``conftest.py`` so suites in other folders (the resolver tests
under ``tests/unit/lambdas``) build the same table. ``test_table_parity.py``
pins it to the parent template.
"""

import boto3

TABLE_NAME = "notifications-test-table"
REGION = "us-east-1"

#: The table exactly as the parent template declares NotificationsTable.
TABLE_ATTRIBUTE_DEFINITIONS = [
    {"AttributeName": "PK", "AttributeType": "S"},
    {"AttributeName": "SK", "AttributeType": "S"},
    {"AttributeName": "UnreadPK", "AttributeType": "S"},
]
TABLE_KEY_SCHEMA = [
    {"AttributeName": "PK", "KeyType": "HASH"},
    {"AttributeName": "SK", "KeyType": "RANGE"},
]
TABLE_GSIS = [
    {
        "IndexName": "UnreadIndex",
        "KeySchema": [
            {"AttributeName": "UnreadPK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        "Projection": {
            "ProjectionType": "INCLUDE",
            "NonKeyAttributes": ["ExpiresAfter", "Type", "Severity"],
        },
    }
]


def create_notifications_table(
    table_name: str = TABLE_NAME, region: str = REGION
) -> str:
    """Create the table (inside an active moto ``mock_aws``); return its name."""
    boto3.resource("dynamodb", region_name=region).create_table(
        TableName=table_name,
        AttributeDefinitions=TABLE_ATTRIBUTE_DEFINITIONS,
        KeySchema=TABLE_KEY_SCHEMA,
        GlobalSecondaryIndexes=TABLE_GSIS,
        BillingMode="PAY_PER_REQUEST",
    )
    return table_name
