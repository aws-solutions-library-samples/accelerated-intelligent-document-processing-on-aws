# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Robust list deletion utilities for DynamoDB document tracking.

This module provides robust deletion functions that handle timestamp mismatches
between document records and list entries, preventing orphaned list entries.
"""

import logging
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)


def calculate_shard(timestamp: str) -> tuple[str, str]:
    """
    Calculate shard information from timestamp.
    
    Args:
        timestamp: ISO timestamp string (e.g., "2025-09-10T12:03:27.256164+00:00")
        
    Returns:
        tuple: (date_part, shard_str) where shard_str is 2-digit padded
        
    Raises:
        ValueError: If timestamp format is invalid
    """
    if not timestamp or not isinstance(timestamp, str):
        raise ValueError(f"Invalid timestamp: must be a non-empty string, got {type(timestamp)}")
    
    if 'T' not in timestamp:
        raise ValueError(f"Invalid timestamp format: missing 'T' separator, got {timestamp}")
    
    try:
        date_part = timestamp.split('T')[0]  # e.g., 2025-09-10
        time_part = timestamp.split('T')[1]
        
        if ':' not in time_part:
            raise ValueError(f"Invalid time format: missing ':' separator, got {time_part}")
            
        hour_part = int(time_part.split(':')[0])  # e.g., 12
        
        # Validate hour range
        if not 0 <= hour_part <= 23:
            raise ValueError(f"Invalid hour: must be 0-23, got {hour_part}")
        
        # Calculate shard (6 shards per day = 4 hours each)
        hours_in_shard = 24 / 6
        shard = int(hour_part / hours_in_shard)
        shard_str = f"{shard:02d}"  # Format with leading zero
        
        return date_part, shard_str
        
    except (ValueError, IndexError) as e:
        if "Invalid" in str(e):
            raise  # Re-raise our custom validation errors
        raise ValueError(f"Invalid timestamp format: {timestamp}, error: {str(e)}")


def try_exact_list_deletion(tracking_table, list_pk: str, list_sk: str, object_key: str) -> bool:
    """
    Attempt to delete list entry with exact timestamp match.
    
    Args:
        tracking_table: DynamoDB table resource
        list_pk: Primary key of list entry
        list_sk: Sort key of list entry
        object_key: Document object key for logging
        
    Returns:
        bool: True if successful, False if not found
    """
    try:
        logger.info(f"Trying exact deletion - PK={list_pk}, SK={list_sk}")
        result = tracking_table.delete_item(
            Key={
                'PK': list_pk,
                'SK': list_sk
            },
            ReturnValues='ALL_OLD'
        )
        
        if 'Attributes' in result:
            logger.info(f"Successfully deleted list entry with exact match: {result['Attributes']}")
            return True
        else:
            logger.warning(f"No list entry found with exact match: PK={list_pk}, SK={list_sk}")
            return False
    except Exception as e:
        logger.error(f"Error in exact list deletion for {object_key}: {str(e)}")
        return False


def list_entry_names_document(entry: Dict[str, Any], object_key: str) -> bool:
    """Does this shard row belong to the document at ``object_key``?

    Two shapes exist in the table and both have to match, which is why this is an OR.
    The ``ObjectKey`` attribute is the primary route; some rows instead encode the key
    in the sort key as ``ts#<timestamp>#id#<object_key>``, which is the only route to a
    row whose ``ObjectKey`` attribute was never written.

    ⚠️ **The sort-key arm parses the key out and compares it whole; every looser test is
    wrong here because this feeds a delete.** A substring test matched a *different*
    document whose key merely starts with this one — ``invoices/report.pdf`` selected
    the rows for ``invoices/report.pdf.bak`` and ``invoices/report.pdf (1).pdf``;
    anchoring at the end still matched one whose own key *contains* ``#id#`` and ends
    with the target, which ``#`` being legal in an S3 key makes reachable by upload.
    Every row this returns is deleted by key, so the sibling would vanish from the
    document list while its tracking record survived. Splitting on the FIRST ``#id#``
    is what this repository's own reader of the same sort key does.

    ⚠️ **This is a client-side predicate, and it has to be: the sort-key half cannot be
    a ``FilterExpression``.** DynamoDB refuses a key attribute in a ``Query`` filter —
    "A ``FilterExpression`` does not allow key attributes. You cannot define a filter
    expression based on a partition key or a sort key" — so
    ``contains(SK, :obj_id)`` made this shard query raise ``ValidationException`` on
    every call (#1366). Nothing is lost by moving it here: a ``FilterExpression`` is
    applied after the items are read and consumes no extra read capacity, so the
    server-side form only ever saved response bytes, and the query below asks for just
    the three attributes that are read.

    Kept in step with ``idp_common.delete_documents.list_entry_names_document``. This
    Lambda has no ``idp_common`` layer, so the predicate is duplicated rather than
    imported; ``test_robust_list_deletion.py`` fails if the two disagree.
    """
    if entry.get('ObjectKey') == object_key:
        return True
    sort_key = entry.get('SK')
    if not isinstance(sort_key, str) or '#id#' not in sort_key:
        return False
    return sort_key.split('#id#', 1)[1] == object_key


def query_shard_for_object_key(tracking_table, list_pk: str, object_key: str) -> List[Dict[str, Any]]:
    """
    Query a shard for any list entries naming the specified object key.

    Args:
        tracking_table: DynamoDB table resource
        list_pk: Primary key of the shard
        object_key: Document object key to search for

    Returns:
        List[Dict]: List of matching DynamoDB items, each carrying PK, SK and (where
        it exists) ObjectKey. Selection is ``list_entry_names_document``, evaluated
        client-side — see that function for why.
    """
    try:
        logger.info(f"Querying shard {list_pk} for entries naming ObjectKey: {object_key}")

        # Only the three attributes the predicate and the delete read. The selection
        # itself is client-side; see list_entry_names_document.
        query_kwargs: Dict[str, Any] = {
            'KeyConditionExpression': Key('PK').eq(list_pk),
            'ProjectionExpression': 'PK, SK, ObjectKey',
        }
        response = tracking_table.query(**query_kwargs)

        matching_items = []
        for item in response.get('Items', []):
            if list_entry_names_document(item, object_key):
                matching_items.append(item)
                logger.info(f"Found matching list entry: PK={item.get('PK')}, SK={item.get('SK')}")

        # Paginate. The shard partition is read in full now that the server applies no
        # filter, so this loop carries the whole selection rather than being rare.
        while 'LastEvaluatedKey' in response:
            logger.info(f"Handling pagination for shard query, continuing from: {response['LastEvaluatedKey']}")
            response = tracking_table.query(
                **query_kwargs,
                ExclusiveStartKey=response['LastEvaluatedKey'],
            )

            for item in response.get('Items', []):
                if list_entry_names_document(item, object_key):
                    matching_items.append(item)
                    logger.info(f"Found matching list entry (paginated): PK={item.get('PK')}, SK={item.get('SK')}")

        logger.info(f"Found {len(matching_items)} matching entries in shard {list_pk}")
        return matching_items

    except Exception as e:
        logger.error(f"Error querying shard for object key {object_key}: {str(e)}")
        return []


def get_adjacent_shards(date_part: str, shard_str: str) -> List[str]:
    """
    Get adjacent shard identifiers for edge case handling.
    
    Args:
        date_part: Date string (e.g., "2025-09-10")
        shard_str: Shard string (e.g., "03")
        
    Returns:
        List[str]: List of adjacent shard PKs
    """
    try:
        current_shard = int(shard_str)
        adjacent_shards = []
        
        # Previous shard
        if current_shard > 0:
            prev_shard = f"{current_shard - 1:02d}"
            adjacent_shards.append(f"list#{date_part}#s#{prev_shard}")
        
        # Next shard  
        if current_shard < 5:  # Max shard is 05
            next_shard = f"{current_shard + 1:02d}"
            adjacent_shards.append(f"list#{date_part}#s#{next_shard}")
        
        logger.info(f"Adjacent shards for {date_part}#s#{shard_str}: {adjacent_shards}")
        return adjacent_shards
    
    except Exception as e:
        logger.error(f"Error calculating adjacent shards: {str(e)}")
        return []


def delete_list_entries_robust(tracking_table, object_key: str, document_metadata: Optional[Dict[str, Any]]) -> bool:
    """
    Robustly delete list entries for the given object key.
    Uses multiple strategies: exact match, shard query, adjacent shard search.
    
    Args:
        tracking_table: DynamoDB table resource
        object_key: Document object key
        document_metadata: Optional document metadata containing timestamp info
        
    Returns:
        bool: True if any entries were deleted
    """
    deleted_any = False
    
    # Strategy 1: Try exact timestamp match if we have document metadata
    if document_metadata:
        event_time = None
        if 'QueuedTime' in document_metadata and document_metadata['QueuedTime']:
            event_time = document_metadata['QueuedTime']
            logger.info(f"Using QueuedTime for exact match: {event_time}")
        elif 'InitialEventTime' in document_metadata and document_metadata['InitialEventTime']:
            event_time = document_metadata['InitialEventTime']
            logger.info(f"Using InitialEventTime for exact match: {event_time}")
        
        if event_time:
            try:
                date_part, shard_str = calculate_shard(event_time)
                list_pk = f"list#{date_part}#s#{shard_str}"
                list_sk = f"ts#{event_time}#id#{object_key}"
                
                if try_exact_list_deletion(tracking_table, list_pk, list_sk, object_key):
                    deleted_any = True
                    logger.info("Successfully deleted list entry with exact timestamp match")
                    return deleted_any  # Success, no need for fallback
                else:
                    logger.warning("Exact timestamp match failed, proceeding with fallback strategies")
            except Exception as e:
                logger.error(f"Error in exact timestamp deletion: {str(e)}")
    
    # Strategy 2: Query calculated shard for any entries with matching ObjectKey
    if document_metadata:
        try:
            # Use the same timestamp for shard calculation
            event_time = document_metadata.get('QueuedTime') or document_metadata.get('InitialEventTime')
            if event_time:
                date_part, shard_str = calculate_shard(event_time)
                list_pk = f"list#{date_part}#s#{shard_str}"
                
                matching_entries = query_shard_for_object_key(tracking_table, list_pk, object_key)
                for entry in matching_entries:
                    try:
                        result = tracking_table.delete_item(
                            Key={
                                'PK': entry['PK'],
                                'SK': entry['SK']
                            },
                            ReturnValues='ALL_OLD'
                        )
                        if 'Attributes' in result:
                            logger.info(f"Successfully deleted list entry via shard query: {entry['SK']}")
                            deleted_any = True
                    except Exception as e:
                        logger.error(f"Error deleting found list entry: {str(e)}")
                
                # Strategy 3: Check adjacent shards for edge cases
                if not deleted_any:
                    logger.info("No entries found in calculated shard, checking adjacent shards")
                    adjacent_shards = get_adjacent_shards(date_part, shard_str)
                    
                    for adj_pk in adjacent_shards:
                        matching_entries = query_shard_for_object_key(tracking_table, adj_pk, object_key)
                        for entry in matching_entries:
                            try:
                                result = tracking_table.delete_item(
                                    Key={
                                        'PK': entry['PK'],
                                        'SK': entry['SK']
                                    },
                                    ReturnValues='ALL_OLD'
                                )
                                if 'Attributes' in result:
                                    logger.info(f"Successfully deleted list entry via adjacent shard: {entry['SK']}")
                                    deleted_any = True
                            except Exception as e:
                                logger.error(f"Error deleting adjacent shard entry: {str(e)}")
        except Exception as e:
            logger.error(f"Error in shard query strategies: {str(e)}")
    
    # Strategy 4: Last resort - search recent dates if no metadata available
    if not deleted_any and not document_metadata:
        logger.warning(f"No document metadata available for {object_key}, attempting recent date search")
        # This could be expanded to search recent dates/shards as a last resort
        # For now, just log the limitation
        logger.warning("Cannot delete list entries without any timestamp information")
    
    if deleted_any:
        logger.info(f"Successfully deleted list entries for {object_key}")
    else:
        logger.warning(f"No list entries found or deleted for {object_key}")
    
    return deleted_any
