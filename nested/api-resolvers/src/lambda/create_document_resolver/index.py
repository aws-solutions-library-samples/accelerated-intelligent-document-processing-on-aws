# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
from decimal import Decimal

import boto3
from log_sanitizer import sanitize_event_for_logging
from robust_list_deletion import calculate_shard, delete_list_entries_robust

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))
# Get LOG_LEVEL from environment variable with INFO as default

dynamodb = boto3.resource('dynamodb')

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super(DecimalEncoder, self).default(obj)

def handler(event, context):
    logger.info(f"Create document resolver invoked with event: {json.dumps(sanitize_event_for_logging(event))}")
    
    try:
        # Extract input data from full AppSync context
        input_data = event['arguments']['input']
        
        # Validate required input fields
        if not input_data:
            raise ValueError("Input data is required")
        
        object_key = input_data.get('ObjectKey')
        queued_time = input_data.get('QueuedTime')
        
        if not object_key or not isinstance(object_key, str):
            raise ValueError("ObjectKey must be a non-empty string")
        if not queued_time or not isinstance(queued_time, str):
            raise ValueError("QueuedTime must be a non-empty string")
        
        logger.info(f"Processing document: {object_key}, QueuedTime: {queued_time}")
        
        tracking_table = dynamodb.Table(os.environ['TRACKING_TABLE_NAME'])
        logger.info(f"Using tracking table: {os.environ['TRACKING_TABLE_NAME']}")
        
        # Define document key format
        doc_pk = f"doc#{object_key}"
        doc_sk = "none"
        
        # First check if document already exists
        logger.info(f"Checking if document {object_key} already exists")
        existing_doc = None
        try:
            response = tracking_table.get_item(
                Key={
                    'PK': doc_pk,
                    'SK': doc_sk
                }
            )
            if 'Item' in response:
                existing_doc = response['Item']
                logger.info(f"Found existing document metadata: {json.dumps(existing_doc, cls=DecimalEncoder)}")
        except Exception as e:
            logger.error(f"Error checking for existing document: {str(e)}")
            # Continue with creation process even if this check fails
        
        # If existing document found, delete its list entry using robust deletion
        if existing_doc:
            try:
                logger.info(f"Attempting robust deletion of list entries for existing document: {object_key}")
                deletion_success = delete_list_entries_robust(tracking_table, object_key, existing_doc)
                
                if deletion_success:
                    logger.info(f"Successfully deleted existing list entries for {object_key}")
                else:
                    logger.warning(f"No existing list entries found/deleted for {object_key}")
            except Exception as e:
                logger.error(f"Error in robust list entry deletion: {str(e)}")
                # Continue with creation process even if deletion fails
        
        # Calculate shard ID for new list entry using shared utility
        date_part, shard_str = calculate_shard(queued_time)
        list_pk = f"list#{date_part}#s#{shard_str}"
        list_sk = f"ts#{queued_time}#id#{object_key}"
        
        logger.info(f"Creating document entries with doc_pk={doc_pk}, list_pk={list_pk}")
        
        # Create both items directly using the resource interface instead of transactions
        try:
            # Create the document record
            logger.info(f"Creating document record: PK={doc_pk}, SK={doc_sk}")
            tracking_table.put_item(
                Item={
                    'PK': doc_pk,
                    'SK': doc_sk,
                    'ItemType': 'document',
                    **input_data
                }
            )
            
            # Create the list item
            logger.info(f"Creating list item: PK={list_pk}, SK={list_sk}")
            tracking_table.put_item(
                Item={
                    'PK': list_pk,
                    'SK': list_sk,
                    'ObjectKey': object_key,
                    'QueuedTime': queued_time,
                    'ExpiresAfter': input_data.get('ExpiresAfter')
                }
            )
            
            logger.info(f"Successfully created document and list entries for {object_key}")
        except Exception as e:
            logger.error(f"Error creating document entries: {str(e)}")
            raise e
        
        return {"ObjectKey": object_key}
    except Exception as e:
        logger.error(f"Error in create_document resolver: {str(e)}", exc_info=True)
        raise e
