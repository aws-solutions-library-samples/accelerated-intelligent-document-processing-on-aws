# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import concurrent.futures
import json
import logging
import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from idp_common import document_scope
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))
# Get LOG_LEVEL from environment variable with INFO as default

# Fixed number of workers for I/O bound operations
# For S3 operations, this is more effective than dynamically calculating based on CPU
MAX_WORKERS = 20

# Minimum files per batch to avoid excessive batches with too few files
MIN_BATCH_SIZE = 5

def copy_s3_object(source_bucket, destination_bucket, object_key):
    """
    Copy a single S3 object from source to destination bucket
    """
    # Use the same S3 client for all operations in this function
    s3_client = boto3.client('s3')
    copy_source = {
        'Bucket': source_bucket,
        'Key': object_key
    }
    
    try:
        s3_client.copy_object(
            CopySource=copy_source,
            Bucket=destination_bucket,
            Key=object_key
        )
        return True
    except Exception as e:
        logger.error(f"Error copying object {object_key}: {str(e)}")
        return False

def batch_copy_s3_objects(source_bucket, destination_bucket, object_keys):
    """
    Copy a batch of S3 objects from source to destination bucket
    
    Args:
        source_bucket: Source S3 bucket
        destination_bucket: Destination S3 bucket
        object_keys: List of object keys to copy
        
    Returns:
        Tuple of (successful_copies, failed_copies)
    """
    # Create a single client for all operations in this batch
    s3_client = boto3.client('s3', config=Config(
        max_pool_connections=min(len(object_keys), 50),
        retries={'max_attempts': 3}
    ))
    
    successful = []
    failed = []
    
    for key in object_keys:
        copy_source = {
            'Bucket': source_bucket,
            'Key': key
        }
        
        try:
            s3_client.copy_object(
                CopySource=copy_source,
                Bucket=destination_bucket,
                Key=key
            )
            successful.append(key)
        except Exception as e:
            logger.error(f"Error copying object {key}: {str(e)}")
            failed.append(key)
    
    return successful, failed

def copy_files_async(object_key, source_bucket, destination_bucket):
    """
    Copy files asynchronously from source bucket to destination bucket
    
    Args:
        object_key: The object key prefix to copy
        source_bucket: Source S3 bucket
        destination_bucket: Destination S3 bucket
        
    Returns:
        Dictionary with copy operation results
    """
    start_time = time.time()
    
    try:
        # Create S3 client with optimized configuration
        s3_client = boto3.client('s3', config=Config(
            max_pool_connections=MAX_WORKERS*2,
            retries={'max_attempts': 3, 'mode': 'adaptive'}
        ))
        
        # List all objects under the prefix
        paginator = s3_client.get_paginator('list_objects_v2')
        operation_parameters = {
            'Bucket': source_bucket,
            'Prefix': object_key
        }

        # Collect all objects to copy
        objects_to_copy = []
        has_contents = False
        
        for page in paginator.paginate(**operation_parameters):
            if 'Contents' in page:
                has_contents = True
                for obj in page['Contents']:
                    objects_to_copy.append(obj['Key'])
        
        total_objects = len(objects_to_copy)
        logger.info(f"Found {total_objects} objects to copy under prefix {object_key}")
        
        if total_objects == 0:
            logger.warning(f"No objects found to copy under prefix: {object_key}")
            if not has_contents:
                logger.warning(f"Prefix {object_key} may not exist in bucket {source_bucket}")
                
            return {
                'success': True,
                'message': f'No objects found under prefix {object_key}',
                'copied': 0,
                'failed': 0,
                'elapsed_seconds': 0
            }
        
        # Determine optimal batch size based on total files and available workers
        workers_to_use = min(MAX_WORKERS, total_objects)
        
        # Calculate batch size - divide total files evenly among workers
        # but ensure at least MIN_BATCH_SIZE files per batch
        calculated_batch_size = max(total_objects // workers_to_use, MIN_BATCH_SIZE)
        
        # Prepare batches of files for workers to process
        batches = []
        for i in range(0, total_objects, calculated_batch_size):
            batch = objects_to_copy[i:i + calculated_batch_size]
            batches.append(batch)
        
        avg_batch_size = total_objects / len(batches) if batches else 0
        logger.info(f"Created {len(batches)} batches with average {avg_batch_size:.1f} files per batch")
        
        # Process batches in parallel with a fixed number of workers
        copied_count = 0
        failed_count = 0
        
        # Use only as many workers as we have batches, up to MAX_WORKERS
        max_workers = min(MAX_WORKERS, len(batches))
        logger.info(f"Using {max_workers} workers to process {len(batches)} batches ({total_objects} total files)")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit batches to thread pool
            future_to_batch = {
                executor.submit(batch_copy_s3_objects, source_bucket, destination_bucket, batch): batch 
                for batch in batches
            }
            
            # Process results as they complete
            for future in concurrent.futures.as_completed(future_to_batch):
                try:
                    successful, failed = future.result()
                    copied_count += len(successful)
                    failed_count += len(failed)
                    
                    # Log progress periodically
                    total_processed = copied_count + failed_count
                    if total_processed % 100 == 0 or total_processed == total_objects:
                        logger.info(f"Progress: {total_processed}/{total_objects} files processed")
                        
                except Exception as e:
                    logger.error(f"Exception in batch processing: {str(e)}")
                    # Assume all files in this batch failed
                    batch = future_to_batch[future]
                    failed_count += len(batch)

        total_count = copied_count + failed_count
        elapsed_time = time.time() - start_time
        logger.info(f'Copied {copied_count}/{total_count} files in {elapsed_time:.2f} seconds '
                   f'({copied_count/elapsed_time:.2f} files/sec)')
        
        if failed_count > 0:
            message = f'Copied {copied_count}/{total_count} files. {failed_count} files failed to copy.'
            success = False
        else:
            message = f'Successfully copied {copied_count} files under prefix {object_key} to baseline bucket'
            success = True
            
        return {
            'success': success,
            'message': message,
            'copied': copied_count,
            'failed': failed_count,
            'elapsed_seconds': elapsed_time
        }
        
    except Exception as e:
        logger.error(f'Error in async copy operation: {str(e)}')
        return {
            'success': False,
            'message': f'Error in async copy operation: {str(e)}',
            'copied': 0,
            'failed': 0,
            'elapsed_seconds': time.time() - start_time
        }

# Per-container cache of callers' configuration scopes (see config_scope).
_user_scope_cache: dict = {}


def update_document_copy_status(object_key, evaluation_status=None):
    """
    Update the document with copy operation status and evaluation status
    
    Args:
        object_key: The document key
        status_data: The copy operation status data
        evaluation_status: Optional status to set in EvaluationStatus field
    """
    logger.info(f"Updating document {object_key} with status: {evaluation_status}")
    
    try:
        # Import at function level to avoid circular imports
        from idp_common.docs_service import create_document_service
        from idp_common.models import Document, Status
        
        logger.info("Imported required modules")
        
        # Create a minimal document for update
        document = Document(
            id=object_key,
            input_key=object_key,
            status=Status.COMPLETED
        )
        logger.info(f"Created document object with ID: {object_key}")
        
        # Set the evaluation status if provided
        if evaluation_status:
            document.evaluation_status = evaluation_status
            logger.info(f"Setting evaluation status to {evaluation_status}")
        
        # Add baseline copy metrics to the document metering for tracking
        if not document.metering:
            document.metering = {}
        
        # Update the document in document service
        logger.info("Creating document service")
        document_service = create_document_service()
        
        # Check if APPSYNC_API_URL is set in environment
        from os import environ
        logger.info(f"APPSYNC_API_URL from environment: {environ.get('APPSYNC_API_URL')}")
        
        logger.info("Calling update_document on document service")
        result = document_service.update_document(document)
        logger.info(f"Document update result: {result}")
        
        logger.info(f"Successfully updated document {object_key} with baseline copy status")
        return True
        
    except Exception as e:
        import traceback
        stack_trace = traceback.format_exc()
        logger.error(f"Failed to update document with copy status: {str(e)}\nStack trace: {stack_trace}")
        return False

def start_async_copy(object_key, source_bucket, destination_bucket):
    """
    Start asynchronous copy operation by invoking this Lambda asynchronously
    
    Args:
        object_key: The object key prefix to copy
        source_bucket: Source S3 bucket
        destination_bucket: Destination S3 bucket
    """
    # Create a Lambda client
    lambda_client = boto3.client('lambda')
    
    # Get this Lambda's function name from the environment
    function_name = os.environ.get('AWS_LAMBDA_FUNCTION_NAME')
    logger.info(f"Lambda function name from environment: {function_name}")
    
    if not function_name:
        logger.error("Could not determine Lambda function name")
        return False
    
    # Create the payload for the async invocation
    payload = {
        'async_operation': 'copy_files',
        'object_key': object_key,
        'source_bucket': source_bucket,
        'destination_bucket': destination_bucket
    }
    logger.info(f"Prepared async invocation payload: {payload}")
    
    try:
        # First, update the document status to indicate copying is in progress
        logger.info(f"Updating document status to BASELINE_COPYING for {object_key}")
        
        # Set the document's evaluation status to BASELINE_COPYING
        try:
            update_document_copy_status(object_key, "BASELINE_COPYING")
            logger.info("Successfully updated document status to BASELINE_COPYING")
        except Exception as update_err:
            logger.error(f"Failed to update initial document status: {str(update_err)}")
            # Continue anyway to attempt the async invocation
        
        # Invoke the Lambda asynchronously (Event invocation type)
        logger.info(f"Invoking Lambda function {function_name} asynchronously")
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType='Event',
            Payload=json.dumps(payload)
        )
        
        logger.info(f"Lambda invoke response: {response}")
        
        # Check if the invocation was accepted
        if response['StatusCode'] == 202:  # 202 Accepted
            logger.info(f"Successfully started async copy operation for {object_key}")
            return True
        else:
            logger.error(f"Failed to start async operation. Status: {response['StatusCode']}")
            update_document_copy_status(object_key, "BASELINE_ERROR")
            return False
            
    except Exception as e:
        import traceback
        stack_trace = traceback.format_exc()
        logger.error(f"Error invoking Lambda asynchronously: {str(e)}\nStack trace: {stack_trace}")
        
        try:
            update_document_copy_status(object_key, "BASELINE_ERROR")
        except Exception as update_err:
            logger.error(f"Failed to update error status: {str(update_err)}")
            
        return False

def handler(event, context):
    """
    Lambda handler for both synchronous API calls and asynchronous processing
    
    Handles two types of invocations:
    1. API Gateway/AppSync invocation (normal GraphQL mutation)
    2. Asynchronous invocation from another Lambda instance
    """
    logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")
    logger.info(f"Lambda context: {context.function_name}, remaining time: {context.get_remaining_time_in_millis()}ms")
    logger.info(f"Environment variables: OUTPUT_BUCKET={os.environ.get('OUTPUT_BUCKET')}, EVALUATION_BASELINE_BUCKET={os.environ.get('EVALUATION_BASELINE_BUCKET')}")

    # Check if this is an asynchronous operation
    if 'async_operation' in event and event['async_operation'] == 'copy_files':
        logger.info("Executing async copy operation")
        # This is an asynchronous copy operation invoked by another Lambda
        object_key = event['object_key']
        source_bucket = event['source_bucket']
        destination_bucket = event['destination_bucket']
        
        logger.info(f"Async operation parameters: object_key={object_key}, source_bucket={source_bucket}, destination_bucket={destination_bucket}")
        
        try:
            # Perform the actual file copying
            logger.info("Starting file copy operation")
            result = copy_files_async(object_key, source_bucket, destination_bucket)
            logger.info(f"Copy operation completed with result: {result}")
            
            # Determine final status based on success/failure
            if result['success']:
                # Successful copy operation
                evaluation_status = "BASELINE_AVAILABLE"
            else:
                # Failed copy operation
                evaluation_status = "BASELINE_ERROR"
            
            logger.info(f"Setting evaluation status to: {evaluation_status}")
            
            # Update the document with the result and new status
            update_document_copy_status(object_key, evaluation_status)
            
            # Async Lambda invocations don't need a return value
            return {
                'success': result['success'],
                'message': result['message']
            }
            
        except Exception as e:
            import traceback
            stack_trace = traceback.format_exc()
            logger.error(f"Error in async operation: {str(e)}\nStack trace: {stack_trace}")
            
            try:
                update_document_copy_status(object_key, "BASELINE_ERROR")
            except Exception as update_err:
                logger.error(f"Failed to update document status: {str(update_err)}")
            
            # Return error but don't raise exception for async invocations
            return {
                'success': False,
                'message': f'Error in async operation: {str(e)}'
            }
    
    # Normal GraphQL mutation handling
    else:
        logger.info("Executing normal GraphQL mutation handler")
        try:
            # Defense-in-depth: copyToBaseline is an Admin+Author operation.
            # (Only applied to the AppSync path; the async self-invocation above
            # has no Cognito identity and is reached only from this Lambda.)
            groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
            if isinstance(groups, str):
                groups = [groups]
            if not ({"Admin", "Author"}.intersection(groups)):
                logger.warning(
                    f"Forbidden: caller (groups={groups}) attempted copyToBaseline"
                )
                # Raise (not return) so the HTTP API dispatcher maps this to a
                # 403 with errorType "Unauthorized" (the UI keys on that);
                # returning a 200 dict would read as success.
                raise PermissionError(
                    "Unauthorized: copyToBaseline requires Admin or Author group"
                )

            # Extract parameters from the GraphQL event
            object_key = event['arguments']['objectKey']
            logger.info(f"GraphQL mutation parameters: object_key={object_key}")

            # The copy reads every output object under the document's key, so the
            # document must be in the caller's configuration scope. Admins skip the
            # lookup. (The async self-invocation above is reached only from here,
            # after this check.)
            dynamodb = boto3.resource('dynamodb')
            document_scope.require_document_scope(
                event,
                [object_key],
                users_table_name=os.environ.get('USERS_TABLE_NAME', ''),
                dynamodb=dynamodb,
                tracking_table=lambda: dynamodb.Table(os.environ['TRACKING_TABLE']),
                cache=_user_scope_cache,
            )
            
            # Get bucket names from environment variables
            source_bucket = os.environ['OUTPUT_BUCKET']
            destination_bucket = os.environ['EVALUATION_BASELINE_BUCKET']
            logger.info(f"Using buckets: source={source_bucket}, destination={destination_bucket}")
            
            # For prefix-based operations, we don't check for object existence
            # since we're working with a prefix (folder) not a specific object
            logger.info(f"Will copy all objects under prefix: {source_bucket}/{object_key}/")
            
            # Check if the prefix exists by listing objects
            s3_client = boto3.client('s3')
            try:
                # List objects with a limit of 1 to see if the prefix exists
                response = s3_client.list_objects_v2(
                    Bucket=source_bucket,
                    Prefix=object_key,
                    MaxKeys=1
                )
                
                # If the prefix doesn't exist or is empty, response won't have 'Contents'
                if 'Contents' not in response or len(response['Contents']) == 0:
                    logger.warning(f"No objects found under prefix: {source_bucket}/{object_key}/")
                    # We'll continue anyway, as the copy operation will just be a no-op if no files exist
                else:
                    logger.info(f"Found objects under prefix: {source_bucket}/{object_key}/")
            except Exception as e:
                logger.warning(f"Error checking prefix: {str(e)}")
                # Continue anyway, as the copy operation will handle errors
            
            # Start asynchronous copy operation
            logger.info("Starting async copy operation")
            result = start_async_copy(object_key, source_bucket, destination_bucket)
            logger.info(f"Async copy start result: {result}")
            
            if result:
                # Return immediate success response
                logger.info("Successfully started async copy operation")
                return {
                    'success': True,
                    'message': f'Copy operation started for {object_key}. The process will continue in the background.'
                }
            else:
                logger.error("Failed to start async copy operation")
                return {
                    'success': False,
                    'message': f'Failed to start copy operation for {object_key}'
                }
            
        except PermissionError:
            # RBAC denial must propagate so the dispatcher returns 403; never
            # swallow it into a 200 success/failure dict.
            raise
        except ClientError as e:
            error_message = str(e)
            logger.error(f'Failed to initialize copy operation: {error_message}')
            return {
                'success': False,
                'message': f'Failed to initialize copy operation: {error_message}'
            }
        except Exception as e:
            import traceback
            stack_trace = traceback.format_exc()
            logger.error(f'Unexpected error: {str(e)}\nStack trace: {stack_trace}')
            return {
                'success': False,
                'message': f'Unexpected error: {str(e)}'
            }
