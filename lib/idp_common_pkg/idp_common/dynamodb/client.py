# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DynamoDB client for direct table operations.
"""

import copy
import logging
import os
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class DynamoDBError(Exception):
    """Custom exception for DynamoDB errors"""

    def __init__(self, message: str, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


class DynamoDBClient:
    """
    Client for executing DynamoDB operations directly against the TrackingTable.

    This client handles authentication, error handling, and provides methods
    for document CRUD operations without going through AppSync.
    """

    def __init__(self, table_name: Optional[str] = None, region: Optional[str] = None):
        """
        Initialize the DynamoDB client.

        Args:
            table_name: Optional DynamoDB table name. If not provided, will be read from TRACKING_TABLE env var.
            region: Optional AWS region. If not provided, will be read from AWS_REGION env var.
        """
        self.table_name = table_name or os.environ.get("TRACKING_TABLE")
        self.region = (
            region or os.environ.get("AWS_REGION") or boto3.Session().region_name
        )

        if not self.table_name:
            raise ValueError(
                "DynamoDB table name must be provided or set in TRACKING_TABLE environment variable"
            )

        if not self.region:
            raise ValueError(
                "AWS region must be provided, set in AWS_REGION environment variable, "
                "or configured in AWS CLI (~/.aws/config)"
            )

        try:
            self.dynamodb = boto3.resource("dynamodb", region_name=self.region)
            self.table = self.dynamodb.Table(self.table_name)  # type: ignore[attr-defined]
        except Exception as e:
            logger.error(f"Failed to initialize DynamoDB client: {str(e)}")
            raise DynamoDBError(f"Failed to initialize DynamoDB client: {str(e)}")

    def put_item(
        self, item: Dict[str, Any], condition_expression: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Put an item into the DynamoDB table.

        Args:
            item: The item to put into the table
            condition_expression: Optional ConditionExpression (e.g.
                ``attribute_not_exists(PK)`` for idempotent create-if-absent).
                Raises DynamoDBError with error_code
                ``ConditionalCheckFailedException`` if the condition fails.

        Returns:
            Dict containing the response from DynamoDB

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        try:
            kwargs: Dict[str, Any] = {"Item": item}
            if condition_expression:
                kwargs["ConditionExpression"] = condition_expression
            response = self.table.put_item(**kwargs)
            logger.debug(f"Successfully put item with PK: {item.get('PK')}")
            return response
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]
            logger.error(f"DynamoDB put_item failed: {error_code} - {error_message}")
            raise DynamoDBError(f"Put item failed: {error_message}", error_code)
        except BotoCoreError as e:
            logger.error(f"BotoCore error during put_item: {str(e)}")
            raise DynamoDBError(f"BotoCore error: {str(e)}")

    def update_item(
        self,
        key: Dict[str, Any],
        update_expression: str,
        expression_attribute_names: Optional[Dict[str, str]] = None,
        expression_attribute_values: Optional[Dict[str, Any]] = None,
        return_values: str = "ALL_NEW",
    ) -> Dict[str, Any]:
        """
        Update an item in the DynamoDB table.

        Args:
            key: The primary key of the item to update
            update_expression: The update expression
            expression_attribute_names: Optional attribute name mappings
            expression_attribute_values: Optional attribute value mappings
            return_values: What to return after the update

        Returns:
            Dict containing the response from DynamoDB

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        try:
            update_params = {
                "Key": key,
                "UpdateExpression": update_expression,
                "ReturnValues": return_values,
            }

            if expression_attribute_names:
                update_params["ExpressionAttributeNames"] = expression_attribute_names

            if expression_attribute_values:
                update_params["ExpressionAttributeValues"] = expression_attribute_values

            response = self.table.update_item(**update_params)
            logger.debug(f"Successfully updated item with key: {key}")
            return response
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]
            logger.error(f"DynamoDB update_item failed: {error_code} - {error_message}")
            raise DynamoDBError(f"Update item failed: {error_message}", error_code)
        except BotoCoreError as e:
            logger.error(f"BotoCore error during update_item: {str(e)}")
            raise DynamoDBError(f"BotoCore error: {str(e)}")

    def delete_item(
        self, key: Dict[str, Any], return_values: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Delete an item from the DynamoDB table.

        Args:
            key: The primary key of the item to delete
            return_values: Optional DynamoDB ReturnValues (e.g. "ALL_OLD" to get the
                deleted item back, which is how a caller learns whether anything
                was actually deleted — DeleteItem on a missing key succeeds silently)

        Returns:
            Dict containing the delete response

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        try:
            delete_params: Dict[str, Any] = {"Key": key}
            if return_values:
                delete_params["ReturnValues"] = return_values
            response = self.table.delete_item(**delete_params)
            logger.debug(f"Successfully deleted item with key: {key}")
            return response
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]
            logger.error(f"DynamoDB delete_item failed: {error_code} - {error_message}")
            raise DynamoDBError(f"Delete failed: {error_message}", error_code)
        except BotoCoreError as e:
            logger.error(f"BotoCore error during delete_item: {str(e)}")
            raise DynamoDBError(f"BotoCore error: {str(e)}")

    def get_item(self, key: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Get an item from the DynamoDB table.

        Args:
            key: The primary key of the item to retrieve

        Returns:
            The item if found, None otherwise

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        try:
            response = self.table.get_item(Key=key)
            item = response.get("Item")
            if item:
                logger.debug(f"Successfully retrieved item with key: {key}")
            else:
                logger.debug(f"Item not found with key: {key}")
            return item
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]
            logger.error(f"DynamoDB get_item failed: {error_code} - {error_message}")
            raise DynamoDBError(f"Get item failed: {error_message}", error_code)
        except BotoCoreError as e:
            logger.error(f"BotoCore error during get_item: {str(e)}")
            raise DynamoDBError(f"BotoCore error: {str(e)}")

    def batch_get_items(self, keys: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Batch get items from the DynamoDB table (max 100 keys)."""
        if not keys:
            return []
        try:
            response = self.dynamodb.batch_get_item(
                RequestItems={self.table.name: {"Keys": keys}}
            )
            return response.get("Responses", {}).get(self.table.name, [])
        except (ClientError, BotoCoreError) as e:
            logger.error(f"DynamoDB batch_get_item failed: {e}")
            raise DynamoDBError(f"Batch get failed: {e}")

    def transact_write_items(
        self, transact_items: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Execute a transaction with multiple write operations.

        Args:
            transact_items: List of transaction items

        Returns:
            Dict containing the response from DynamoDB

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        try:
            # Convert table references to use the table name. Deep-copied:
            # a shallow copy shares the nested Put/Update/Delete dicts with
            # the caller, so injecting TableName below would write into the
            # caller's own payload.
            processed_items = []
            for item in transact_items:
                processed_item = copy.deepcopy(item)
                if "Put" in processed_item:
                    processed_item["Put"]["TableName"] = self.table_name
                elif "Update" in processed_item:
                    processed_item["Update"]["TableName"] = self.table_name
                elif "Delete" in processed_item:
                    processed_item["Delete"]["TableName"] = self.table_name
                processed_items.append(processed_item)

            response = self.dynamodb.meta.client.transact_write_items(
                TransactItems=processed_items
            )
            logger.debug(
                f"Successfully executed transaction with {len(transact_items)} items"
            )
            return response
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]
            logger.error(
                f"DynamoDB transact_write_items failed: {error_code} - {error_message}"
            )
            raise DynamoDBError(f"Transaction failed: {error_message}", error_code)
        except BotoCoreError as e:
            logger.error(f"BotoCore error during transact_write_items: {str(e)}")
            raise DynamoDBError(f"BotoCore error: {str(e)}")

    def scan(
        self,
        filter_expression: Optional[str] = None,
        expression_attribute_names: Optional[Dict[str, str]] = None,
        expression_attribute_values: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        exclusive_start_key: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Scan the DynamoDB table.

        Args:
            filter_expression: Optional filter expression
            expression_attribute_names: Optional attribute name mappings
            expression_attribute_values: Optional attribute value mappings
            limit: Optional limit on number of items to return
            exclusive_start_key: Optional key to start scanning from

        Returns:
            Dict containing the scan results

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        try:
            scan_params = {}

            if filter_expression:
                scan_params["FilterExpression"] = filter_expression

            if expression_attribute_names:
                scan_params["ExpressionAttributeNames"] = expression_attribute_names

            if expression_attribute_values:
                scan_params["ExpressionAttributeValues"] = expression_attribute_values

            if limit:
                scan_params["Limit"] = limit

            if exclusive_start_key:
                scan_params["ExclusiveStartKey"] = exclusive_start_key

            response = self.table.scan(**scan_params)
            logger.debug(
                f"Successfully scanned table, returned {len(response.get('Items', []))} items"
            )
            return response
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]
            logger.error(f"DynamoDB scan failed: {error_code} - {error_message}")
            raise DynamoDBError(f"Scan failed: {error_message}", error_code)
        except BotoCoreError as e:
            logger.error(f"BotoCore error during scan: {str(e)}")
            raise DynamoDBError(f"BotoCore error: {str(e)}")

    def scan_all(
        self,
        filter_expression: Optional[str] = None,
        expression_attribute_names: Optional[Dict[str, str]] = None,
        expression_attribute_values: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Scan the DynamoDB table with automatic pagination to retrieve all items.

        Args:
            filter_expression: Optional filter expression
            expression_attribute_names: Optional attribute name mappings
            expression_attribute_values: Optional attribute value mappings

        Returns:
            List of all items matching the filter

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        items = []
        last_evaluated_key = None

        while True:
            scan_params = {}

            if filter_expression:
                scan_params["FilterExpression"] = filter_expression

            if expression_attribute_names:
                scan_params["ExpressionAttributeNames"] = expression_attribute_names

            if expression_attribute_values:
                scan_params["ExpressionAttributeValues"] = expression_attribute_values

            if last_evaluated_key:
                scan_params["ExclusiveStartKey"] = last_evaluated_key

            try:
                response = self.table.scan(**scan_params)
                items.extend(response.get("Items", []))

                last_evaluated_key = response.get("LastEvaluatedKey")
                if not last_evaluated_key:
                    break

            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                error_message = e.response["Error"]["Message"]
                logger.error(
                    f"DynamoDB scan_all failed: {error_code} - {error_message}"
                )
                raise DynamoDBError(f"Scan failed: {error_message}", error_code)
            except BotoCoreError as e:
                logger.error(f"BotoCore error during scan_all: {str(e)}")
                raise DynamoDBError(f"BotoCore error: {str(e)}")

        logger.debug(
            f"Successfully scanned all items, returned {len(items)} total items"
        )
        return items

    def query(
        self,
        key_condition_expression: str,
        expression_attribute_names: Optional[Dict[str, str]] = None,
        expression_attribute_values: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        exclusive_start_key: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Query the DynamoDB table.

        Args:
            key_condition_expression: The key condition expression
            expression_attribute_names: Optional attribute name mappings
            expression_attribute_values: Optional attribute value mappings
            limit: Optional limit on number of items to return
            exclusive_start_key: Optional key to start querying from

        Returns:
            Dict containing the query results

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        try:
            query_params: Dict[str, Any] = {
                "KeyConditionExpression": key_condition_expression,
            }

            if expression_attribute_names:
                query_params["ExpressionAttributeNames"] = expression_attribute_names

            if expression_attribute_values:
                query_params["ExpressionAttributeValues"] = expression_attribute_values

            if limit:
                query_params["Limit"] = limit

            if exclusive_start_key:
                query_params["ExclusiveStartKey"] = exclusive_start_key

            response = self.table.query(**query_params)
            logger.debug(
                f"Successfully queried table, returned {len(response.get('Items', []))} items"
            )
            return response
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]
            logger.error(f"DynamoDB query failed: {error_code} - {error_message}")
            raise DynamoDBError(f"Query failed: {error_message}", error_code)
        except BotoCoreError as e:
            logger.error(f"BotoCore error during query: {str(e)}")
            raise DynamoDBError(f"BotoCore error: {str(e)}")
