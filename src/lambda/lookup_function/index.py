# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0


import boto3
import json
import os
from datetime import datetime, timezone
from log_sanitizer import sanitize_event_for_logging
import logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))
# Get LOG_LEVEL from environment variable with INFO as default

def calculate_durations(timestamps):
    try:
        durations = {}
        if 'QueuedTime' in timestamps and 'WorkflowStartTime' in timestamps:
            queue_time = (datetime.fromisoformat(timestamps['WorkflowStartTime']) - 
                         datetime.fromisoformat(timestamps['QueuedTime'])).total_seconds() * 1000
            durations['queue'] = int(queue_time)
            
        if 'WorkflowStartTime' in timestamps and 'CompletionTime' in timestamps:
            processing_time = (datetime.fromisoformat(timestamps['CompletionTime']) - 
                             datetime.fromisoformat(timestamps['WorkflowStartTime'])).total_seconds() * 1000
            durations['processing'] = int(processing_time)
            
        if 'InitialEventTime' in timestamps and 'CompletionTime' in timestamps:
            total_time = (datetime.fromisoformat(timestamps['CompletionTime']) - 
                         datetime.fromisoformat(timestamps['InitialEventTime'])).total_seconds() * 1000
            durations['total'] = int(total_time)
            
        return durations
    except Exception as e:
        logger.error(f"Error calculating durations: {e}", exc_info=True)
        return {}

def get_document_status(object_key, status_only=False):
    """
    Get status for a single document
    
    Args:
        object_key: Document object key
        status_only: If True, return status + timing (no Step Functions details)
    
    Returns:
        Dictionary with document status
    """
    dynamodb = boto3.resource('dynamodb')
    tracking_table = dynamodb.Table(os.environ['TRACKING_TABLE'])
    
    try:
        PK = f"doc#{object_key}"
        response = tracking_table.get_item(
            Key={'PK': PK, 'SK': "none"},
            ConsistentRead=True
        )
        
        if 'Item' not in response:
            return {'object_key': object_key, 'status': 'NOT_FOUND'}
            
        item = response['Item']
        
        # Always include status and timing
        timestamps = {
            'InitialEventTime': item.get('InitialEventTime'),
            'QueuedTime': item.get('QueuedTime'),
            'WorkflowStartTime': item.get('WorkflowStartTime'),
            'CompletionTime': item.get('CompletionTime')
        }
        
        result = {
            'object_key': object_key,
            'status': item.get('ObjectStatus', 'UNKNOWN'),
            'timing': {
                'timestamps': timestamps,
                'elapsed': calculate_durations(timestamps)
            }
        }
        
        # If status_only mode, skip expensive Step Functions queries
        if status_only:
            return result
        
        execution_arn = item.get('WorkflowExecutionArn')
        if execution_arn:
            try:
                sfn = boto3.client('stepfunctions')
                execution = sfn.describe_execution(executionArn=execution_arn)
                history = sfn.get_execution_history(
                    executionArn=execution_arn,
                    maxResults=100
                )
                
                result['processingDetail'] = {
                    'executionArn': execution_arn,
                    'execution': {k: str(v) if isinstance(v, datetime) else v 
                                for k, v in execution.items() 
                                if k != 'ResponseMetadata'},
                    'events': [{k: str(v) if isinstance(v, datetime) else v 
                              for k, v in event.items()}
                              for event in history['events']]
                }
            except Exception as e:
                logger.error(f"Error getting Step Functions details: {e}", exc_info=True)
                result['processingDetail'] = {
                    'executionArn': execution_arn,
                    'error': str(e)
                }
        
        return result
        
    except Exception as e:
        logger.error(f"Error looking up document {object_key}: {e}", exc_info=True)
        return {
            'object_key': object_key,
            'status': 'ERROR',
            'message': str(e)
        }


def handler(event, context):
    """
    Lambda handler supporting both single and batch document queries
    
    Request formats:
        Single: {'object_key': 'doc-123', 'status_only': False}
        Batch: {'object_keys': ['doc-1', 'doc-2', ...], 'status_only': True}
    
    Response formats:
        Single: {'status': 'RUNNING', 'timing': {...}, 'processingDetail': {...}}
        Batch: {'results': [{'object_key': 'doc-1', 'status': 'COMPLETED'}, ...]}
    """
    logger.info(f"Event: {json.dumps(sanitize_event_for_logging(event))}")

    # Extract request parameters
    object_keys = event.get('object_keys')  # Batch mode
    object_key = event.get('object_key')    # Single mode
    status_only = event.get('status_only', False)
    
    # Validate request
    if not object_keys and not object_key:
        return {'status': 'ERROR', 'message': 'object_key or object_keys is required'}
    
    # Handle batch request
    if object_keys:
        logger.info(f"Batch query for {len(object_keys)} documents (status_only={status_only})")
        results = []
        for key in object_keys:
            result = get_document_status(key, status_only)
            results.append(result)
        return {'results': results}
    
    # Handle single document request (backward compatible)
    logger.info(f"Single query for {object_key} (status_only={status_only})")
    result = get_document_status(object_key, status_only)
    
    # Remove object_key from response for backward compatibility
    # (old format didn't include it)
    result.pop('object_key', None)
    
    return result
