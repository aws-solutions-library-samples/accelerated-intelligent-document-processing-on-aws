# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DynamoDB client for direct table operations.
"""

import copy
import json
import logging
import os
import random
import time
from typing import Any, Dict, Iterator, List, Literal, Optional, Sequence

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class DynamoDBError(Exception):
    """Custom exception for DynamoDB errors"""

    def __init__(self, message: str, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


#: Error code DynamoDB returns when a ConditionExpression evaluates false. Callers
#: that use conditions as control flow (create-if-absent, optimistic concurrency)
#: expect it, so the client logs it at DEBUG rather than ERROR.
CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"

#: Transaction cancellation reason codes that mean "a condition you wrote was
#: false" or "this item was not the cause" — expected control flow, not a fault.
_EXPECTED_CANCELLATION_CODES = frozenset({"None", "ConditionalCheckFailed"})

#: DynamoDB service limits for the batch APIs.
_BATCH_GET_CHUNK = 100
_BATCH_WRITE_CHUNK = 25

QuerySelect = Literal[
    "ALL_ATTRIBUTES", "ALL_PROJECTED_ATTRIBUTES", "SPECIFIC_ATTRIBUTES", "COUNT"
]


class DynamoDBTransactionCanceledError(DynamoDBError):
    """A TransactWriteItems call was cancelled.

    ``cancellation_reasons`` holds one entry per transact item, in request order,
    each a dict with at least ``Code`` (``"None"`` for items that did not cause
    the cancellation, ``"ConditionalCheckFailed"`` for a false condition, ...).
    """

    def __init__(
        self,
        message: str,
        error_code: str = "TransactionCanceledException",
        cancellation_reasons: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message, error_code)
        self.cancellation_reasons: list[dict[str, Any]] = list(
            cancellation_reasons or []
        )

    def reason_code(self, index: int) -> str:
        """Cancellation reason code for the transact item at ``index``.

        Returns ``"None"`` when the index is out of range or carries no code.
        """
        if index < 0 or index >= len(self.cancellation_reasons):
            return "None"
        reason = self.cancellation_reasons[index] or {}
        return str(reason.get("Code") or "None")


def _backoff_sleep(attempt: int) -> None:
    """Exponential backoff with full jitter, capped at one second."""
    time.sleep(random.uniform(0, min(1.0, 0.05 * 2**attempt)))  # nosec B311


def _key_identity(key: Dict[str, Any]) -> str:
    return json.dumps(key, sort_keys=True, default=str)


def _log_client_error(operation: str, error_code: str, error_message: str) -> None:
    """Log a ClientError: DEBUG for an expected condition failure, else ERROR."""
    if error_code == CONDITIONAL_CHECK_FAILED:
        logger.debug(
            f"DynamoDB {operation} condition not met: {error_code} - {error_message}"
        )
    else:
        logger.error(f"DynamoDB {operation} failed: {error_code} - {error_message}")


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
        self,
        item: Dict[str, Any],
        condition_expression: Optional[str] = None,
        *,
        expression_attribute_names: Optional[Dict[str, str]] = None,
        expression_attribute_values: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Put an item into the DynamoDB table.

        Args:
            item: The item to put into the table
            condition_expression: Optional ConditionExpression (e.g.
                ``attribute_not_exists(PK)`` for idempotent create-if-absent).
                Raises DynamoDBError with error_code
                ``ConditionalCheckFailedException`` if the condition fails
                (logged at DEBUG, since conditions are routine control flow).
            expression_attribute_names: Optional names map for the condition.
            expression_attribute_values: Optional values map for the condition.

        Returns:
            Dict containing the response from DynamoDB

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        try:
            kwargs: Dict[str, Any] = {"Item": item}
            if condition_expression:
                kwargs["ConditionExpression"] = condition_expression
            if expression_attribute_names:
                kwargs["ExpressionAttributeNames"] = expression_attribute_names
            if expression_attribute_values:
                kwargs["ExpressionAttributeValues"] = expression_attribute_values
            response = self.table.put_item(**kwargs)
            logger.debug(f"Successfully put item with PK: {item.get('PK')}")
            return response
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]
            _log_client_error("put_item", error_code, error_message)
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
        condition_expression: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update an item in the DynamoDB table.

        Args:
            key: The primary key of the item to update
            update_expression: The update expression
            expression_attribute_names: Optional attribute name mappings
            expression_attribute_values: Optional attribute value mappings
            return_values: What to return after the update
            condition_expression: Optional ConditionExpression, evaluated by
                DynamoDB against the item as it exists at write time. This is the
                only way a read-modify-write through this client can detect that
                the state it read has since changed: without one, every writer
                succeeds and the loser's data is gone with nothing reporting it.
                Raises DynamoDBError with error_code
                ``ConditionalCheckFailedException`` if the condition fails, which
                is the caller's signal to re-read and retry.

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

            if condition_expression:
                update_params["ConditionExpression"] = condition_expression

            response = self.table.update_item(**update_params)
            logger.debug(f"Successfully updated item with key: {key}")
            return response
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]
            _log_client_error("update_item", error_code, error_message)
            raise DynamoDBError(f"Update item failed: {error_message}", error_code)
        except BotoCoreError as e:
            logger.error(f"BotoCore error during update_item: {str(e)}")
            raise DynamoDBError(f"BotoCore error: {str(e)}")

    def delete_item(
        self,
        key: Dict[str, Any],
        *,
        condition_expression: Optional[str] = None,
        expression_attribute_names: Optional[Dict[str, str]] = None,
        expression_attribute_values: Optional[Dict[str, Any]] = None,
        return_values: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Delete an item from the DynamoDB table.

        Args:
            key: The primary key of the item to delete
            condition_expression: Optional ConditionExpression. A false condition
                raises DynamoDBError with error_code
                ``ConditionalCheckFailedException`` (logged at DEBUG) and leaves
                the item in place.
            expression_attribute_names: Optional names map for the condition.
            expression_attribute_values: Optional values map for the condition.
            return_values: Optional ReturnValues (e.g. ``ALL_OLD``).

        Returns:
            Dict containing the delete response

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        try:
            kwargs: Dict[str, Any] = {"Key": key}
            if condition_expression:
                kwargs["ConditionExpression"] = condition_expression
            if expression_attribute_names:
                kwargs["ExpressionAttributeNames"] = expression_attribute_names
            if expression_attribute_values:
                kwargs["ExpressionAttributeValues"] = expression_attribute_values
            if return_values:
                kwargs["ReturnValues"] = return_values
            response = self.table.delete_item(**kwargs)
            logger.debug(f"Successfully deleted item with key: {key}")
            return response
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]
            _log_client_error("delete_item", error_code, error_message)
            raise DynamoDBError(f"Delete failed: {error_message}", error_code)
        except BotoCoreError as e:
            logger.error(f"BotoCore error during delete_item: {str(e)}")
            raise DynamoDBError(f"BotoCore error: {str(e)}")

    def get_item(
        self,
        key: Dict[str, Any],
        *,
        consistent_read: bool = False,
        projection_expression: Optional[str] = None,
        expression_attribute_names: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get an item from the DynamoDB table.

        Args:
            key: The primary key of the item to retrieve
            consistent_read: Use a strongly consistent read.
            projection_expression: Optional ProjectionExpression.
            expression_attribute_names: Optional names map for the projection.

        Returns:
            The item if found, None otherwise

        Raises:
            DynamoDBError: If the DynamoDB operation fails
        """
        try:
            kwargs: Dict[str, Any] = {"Key": key}
            if consistent_read:
                kwargs["ConsistentRead"] = True
            if projection_expression:
                kwargs["ProjectionExpression"] = projection_expression
            if expression_attribute_names:
                kwargs["ExpressionAttributeNames"] = expression_attribute_names
            response = self.table.get_item(**kwargs)
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

    def batch_get_items(
        self,
        keys: List[Dict[str, Any]],
        *,
        consistent_read: bool = False,
        projection_expression: Optional[str] = None,
        expression_attribute_names: Optional[Dict[str, str]] = None,
        max_attempts: int = 6,
    ) -> List[Dict[str, Any]]:
        """Batch get items from the DynamoDB table.

        Keys are de-duplicated and sent in chunks of 100. ``UnprocessedKeys`` are
        re-submitted with exponential backoff and full jitter; if keys remain
        unprocessed after ``max_attempts`` the call raises rather than returning a
        partial result.

        Returns:
            The items found, in no particular order. Missing keys are absent.

        Raises:
            DynamoDBError: On a service error, or with error_code
                ``UnprocessedKeys`` when retries are exhausted.
        """
        if not keys:
            return []
        unique: Dict[str, Dict[str, Any]] = {}
        for key in keys:
            unique.setdefault(_key_identity(key), key)
        deduped = list(unique.values())

        table_name = self.table.name
        results: List[Dict[str, Any]] = []
        for start in range(0, len(deduped), _BATCH_GET_CHUNK):
            request: Dict[str, Any] = {
                "Keys": deduped[start : start + _BATCH_GET_CHUNK]
            }
            if consistent_read:
                request["ConsistentRead"] = True
            if projection_expression:
                request["ProjectionExpression"] = projection_expression
            if expression_attribute_names:
                request["ExpressionAttributeNames"] = expression_attribute_names
            pending: Dict[str, Any] = {table_name: request}
            attempt = 0
            while pending:
                try:
                    response = self.dynamodb.batch_get_item(RequestItems=pending)
                except (ClientError, BotoCoreError) as e:
                    logger.error(f"DynamoDB batch_get_item failed: {e}")
                    raise DynamoDBError(f"Batch get failed: {e}")
                results.extend(response.get("Responses", {}).get(table_name, []))
                pending = response.get("UnprocessedKeys") or {}
                if not pending:
                    break
                attempt += 1
                if attempt >= max_attempts:
                    remaining = len(pending.get(table_name, {}).get("Keys", []))
                    logger.error(
                        f"DynamoDB batch_get_item: {remaining} unprocessed keys "
                        f"remain after {max_attempts} attempts"
                    )
                    raise DynamoDBError(
                        f"Batch get failed: {remaining} unprocessed keys remain",
                        "UnprocessedKeys",
                    )
                _backoff_sleep(attempt)
        return results

    def batch_write_items(
        self,
        *,
        put_items: Sequence[Dict[str, Any]] = (),
        delete_keys: Sequence[Dict[str, Any]] = (),
        max_attempts: int = 6,
    ) -> None:
        """Batch put and/or delete items, 25 requests per call.

        Delete keys are de-duplicated (DynamoDB rejects a batch naming one key
        twice). ``UnprocessedItems`` are retried with exponential backoff and
        full jitter. Empty input is a no-op.

        Raises:
            DynamoDBError: On a service error, or with error_code
                ``UnprocessedItems`` when retries are exhausted.
        """
        requests: List[Dict[str, Any]] = [
            {"PutRequest": {"Item": item}} for item in put_items
        ]
        seen: set[str] = set()
        for key in delete_keys:
            identity = _key_identity(key)
            if identity in seen:
                continue
            seen.add(identity)
            requests.append({"DeleteRequest": {"Key": key}})
        if not requests:
            return

        table_name = self.table.name
        for start in range(0, len(requests), _BATCH_WRITE_CHUNK):
            pending: Dict[str, Any] = {
                table_name: requests[start : start + _BATCH_WRITE_CHUNK]
            }
            attempt = 0
            while pending:
                try:
                    response = self.dynamodb.batch_write_item(RequestItems=pending)
                except (ClientError, BotoCoreError) as e:
                    logger.error(f"DynamoDB batch_write_item failed: {e}")
                    raise DynamoDBError(f"Batch write failed: {e}")
                pending = response.get("UnprocessedItems") or {}
                if not pending:
                    break
                attempt += 1
                if attempt >= max_attempts:
                    remaining = len(pending.get(table_name, []))
                    logger.error(
                        f"DynamoDB batch_write_item: {remaining} unprocessed items "
                        f"remain after {max_attempts} attempts"
                    )
                    raise DynamoDBError(
                        f"Batch write failed: {remaining} unprocessed items remain",
                        "UnprocessedItems",
                    )
                _backoff_sleep(attempt)

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
            DynamoDBTransactionCanceledError: If the transaction was cancelled
                (inspect ``reason_code(i)`` per transact item). Logged at DEBUG
                when every reason is ``None`` or ``ConditionalCheckFailed``.
            DynamoDBError: If the DynamoDB operation fails otherwise
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
                elif "ConditionCheck" in processed_item:
                    processed_item["ConditionCheck"]["TableName"] = self.table_name
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
            if error_code == "TransactionCanceledException":
                reasons = e.response.get("CancellationReasons") or []
                codes = {str((r or {}).get("Code") or "None") for r in reasons}
                if codes <= _EXPECTED_CANCELLATION_CODES:
                    logger.debug(
                        "DynamoDB transact_write_items cancelled by a condition: "
                        f"{error_message}"
                    )
                else:
                    logger.error(
                        "DynamoDB transact_write_items failed: "
                        f"{error_code} - {error_message}"
                    )
                raise DynamoDBTransactionCanceledError(
                    f"Transaction failed: {error_message}",
                    cancellation_reasons=reasons,
                )
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
        *,
        index_name: Optional[str] = None,
        scan_index_forward: Optional[bool] = None,
        filter_expression: Optional[str] = None,
        projection_expression: Optional[str] = None,
        select: Optional[QuerySelect] = None,
        consistent_read: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Query the DynamoDB table (one page).

        Args:
            key_condition_expression: The key condition expression
            expression_attribute_names: Optional attribute name mappings
            expression_attribute_values: Optional attribute value mappings
            limit: Optional limit on number of items to examine
            exclusive_start_key: Optional key to start querying from
            index_name: Optional secondary index to query
            scan_index_forward: ``False`` for descending sort-key order
            filter_expression: Optional FilterExpression. ``Limit`` bounds the
                items *examined*, so a filtered query must follow
                ``LastEvaluatedKey`` (use ``query_pages``).
            projection_expression: Optional ProjectionExpression
            select: Optional Select (``COUNT`` returns ``Count`` and no items)
            consistent_read: Optional ConsistentRead (base table and LSIs only)

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

            if index_name is not None:
                query_params["IndexName"] = index_name

            if scan_index_forward is not None:
                query_params["ScanIndexForward"] = scan_index_forward

            if filter_expression is not None:
                query_params["FilterExpression"] = filter_expression

            if projection_expression is not None:
                query_params["ProjectionExpression"] = projection_expression

            if select is not None:
                query_params["Select"] = select

            if consistent_read is not None:
                query_params["ConsistentRead"] = consistent_read

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

    def query_pages(
        self,
        key_condition_expression: str,
        *,
        expression_attribute_names: Optional[Dict[str, str]] = None,
        expression_attribute_values: Optional[Dict[str, Any]] = None,
        index_name: Optional[str] = None,
        scan_index_forward: Optional[bool] = None,
        filter_expression: Optional[str] = None,
        projection_expression: Optional[str] = None,
        select: Optional[QuerySelect] = None,
        consistent_read: Optional[bool] = None,
        page_size: Optional[int] = None,
        exclusive_start_key: Optional[Dict[str, Any]] = None,
        max_pages: Optional[int] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield raw query responses, following ``LastEvaluatedKey``.

        Stops when a response has no ``LastEvaluatedKey`` or after ``max_pages``
        responses. ``page_size`` is passed as ``Limit`` on each request.
        """
        start_key = exclusive_start_key
        pages = 0
        while True:
            response = self.query(
                key_condition_expression,
                expression_attribute_names,
                expression_attribute_values,
                page_size,
                start_key,
                index_name=index_name,
                scan_index_forward=scan_index_forward,
                filter_expression=filter_expression,
                projection_expression=projection_expression,
                select=select,
                consistent_read=consistent_read,
            )
            pages += 1
            yield response
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return
            if max_pages is not None and pages >= max_pages:
                return

    def query_count(
        self,
        key_condition_expression: str,
        *,
        expression_attribute_names: Optional[Dict[str, str]] = None,
        expression_attribute_values: Optional[Dict[str, Any]] = None,
        index_name: Optional[str] = None,
        filter_expression: Optional[str] = None,
        consistent_read: Optional[bool] = None,
    ) -> int:
        """Count matching items across all pages (``Select=COUNT``)."""
        total = 0
        for response in self.query_pages(
            key_condition_expression,
            expression_attribute_names=expression_attribute_names,
            expression_attribute_values=expression_attribute_values,
            index_name=index_name,
            filter_expression=filter_expression,
            select="COUNT",
            consistent_read=consistent_read,
        ):
            total += int(response.get("Count", 0))
        return total
