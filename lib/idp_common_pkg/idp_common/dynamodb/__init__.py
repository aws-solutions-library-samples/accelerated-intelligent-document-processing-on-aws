# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Direct DynamoDB integration module for IDP Common package.

This module provides classes and functions for interacting directly with DynamoDB
to store and retrieve document data, bypassing AppSync GraphQL API.
"""

from idp_common.dynamodb.client import (
    DynamoDBClient,
    DynamoDBError,
    DynamoDBTransactionCanceledError,
)
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.dynamodb.ttl import calculate_ttl

__all__ = [
    "DynamoDBClient",
    "DynamoDBError",
    "DynamoDBTransactionCanceledError",
    "DocumentDynamoDBService",
    "calculate_ttl",
]
