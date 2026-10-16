# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Moto table shared by the DynamoDBClient extension tests: PK/SK plus a sparse
``UnreadIndex`` GSI with an INCLUDE projection."""

import boto3

TABLE = "client-ext-test-table"
REGION = "us-east-1"


def make_table() -> None:
    boto3.resource("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "UnreadPK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
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
        ],
        BillingMode="PAY_PER_REQUEST",
    )
