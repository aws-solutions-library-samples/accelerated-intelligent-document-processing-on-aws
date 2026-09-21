# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import boto3
import os
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from botocore.exceptions import ClientError
from idp_common import metrics, utils
from idp_common.models import Document
from idp_common.bda.bda_service import BdaService
from idp_common.utils.s3util import S3Util

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))
# Get LOG_LEVEL from environment variable with INFO as default

# Retry configuration
MAX_RETRIES = 7
INITIAL_BACKOFF = 2  # seconds
MAX_BACKOFF = 300   # 5 minutes

# Initialize client
bda_client = boto3.client('bedrock-data-automation-runtime')
dynamodb = boto3.resource('dynamodb')
tracking_table = dynamodb.Table(os.environ['TRACKING_TABLE'])

# def invoke_data_automation(payload: Dict[str, Any]) -> Dict[str, Any]:
def invoke_data_automation(data_project_arn: str, input_s3_uri: str, output_s3_uri: str) -> Dict[str, Any]:  # pyright: ignore[reportReturnType] - every loop exit returns or re-raises; the implicit fall-through is unreachable
    retry_count = 0
    last_exception = None
    request_start_time = time.time()

    bda_service = BdaService(
        dataAutomationProjectArn=data_project_arn,
        output_s3_uri=output_s3_uri
    )

    metrics.put_metric('BDARequestsTotal', 1)

    while retry_count < MAX_RETRIES:
        try:
            logger.info(f"BDA API request attempt {retry_count + 1}/{MAX_RETRIES}")
            
            attempt_start_time = time.time()
            response = bda_service.invoke_data_automation_async(input_s3_uri=input_s3_uri)
            duration = time.time() - attempt_start_time
            
            logger.info(f"BDA API request successful after {retry_count + 1} attempts. "
                       f"Duration: {duration:.2f}s")

            metrics.put_metric('BDARequestsSucceeded', 1)
            metrics.put_metric('BDARequestsLatency', duration * 1000, 'Milliseconds')
            
            if retry_count > 0:
                metrics.put_metric('BDARequestsRetrySuccess', 1)

            total_duration = time.time() - request_start_time
            metrics.put_metric('BDARequestsTotalLatency', total_duration * 1000, 'Milliseconds')

            return response

        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_message = e.response['Error']['Message']
            
            retryable_errors = [
                'ThrottlingException',
                'ServiceQuotaExceededException',
                'RequestLimitExceeded',
                'TooManyRequestsException',
                'InternalServerException'
            ]
            
            if error_code in retryable_errors:
                retry_count += 1
                metrics.put_metric('BDARequestsThrottles', 1)
                
                if retry_count == MAX_RETRIES:
                    logger.error(f"Max retries ({MAX_RETRIES}) exceeded. Last error: {error_message}")
                    metrics.put_metric('BDARequestsFailed', 1)
                    metrics.put_metric('BDARequestsMaxRetriesExceeded', 1)
                    raise
                
                backoff = utils.calculate_backoff(retry_count, INITIAL_BACKOFF, MAX_BACKOFF)
                logger.warning(f"BDA API throttling occurred (attempt {retry_count}/{MAX_RETRIES}). "
                             f"Error: {error_message}. "
                             f"Backing off for {backoff:.2f}s")
                
                time.sleep(backoff)  # semgrep-ignore: arbitrary-sleep - Intentional delay backoff/retry. Duration is algorithmic and not user-controlled.
                last_exception = e
            else:
                logger.error(f"Non-retryable BDA API error: {error_code} - {error_message}")
                metrics.put_metric('BDARequestsFailed', 1)
                metrics.put_metric('BDARequestsNonRetryableErrors', 1)
                raise

        except Exception as e:
            logger.error(f"Unexpected error invoking BDA API: {str(e)}", exc_info=True)
            metrics.put_metric('BDARequestsFailed', 1)
            metrics.put_metric('BDARequestsUnexpectedErrors', 1)
            raise
        
    if last_exception:
        raise last_exception
    
def track_task_token(object_key: str, task_token: str) -> None:
    try:
        # Record in DynamoDB
        tracking_item = {
            'PK': f"tasktoken#{object_key}",
            'SK': 'none',
            'TaskToken': task_token,
            'TaskTokenTime': datetime.now(timezone.utc).isoformat(),
            'ExpiresAfter': int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
        }
        logger.info(f"Recording tasktoken entry: {tracking_item}")
        tracking_table.put_item(Item=tracking_item)

    except Exception as e:
        logger.error(f"Error recording tasktoken record: {e}")
        raise

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    try:
        logger.info(f"Received event: {json.dumps(event)}")

        # Get document from event using new utility method
        working_bucket = event['working_bucket']
        document = Document.load_document(event["document"], working_bucket, logger)
        input_bucket = document.input_bucket
        object_key = document.input_key
        data_project_arn = event['BDAProjectArn']
        task_token = event['taskToken']
        
        # Intelligent skip: If document already has sections with extraction data, skip BDA
        # This supports HITL reprocessing where we only need to re-run Summarization/Evaluation
        if document.sections and len(document.sections) > 0:
            has_extraction_data = any(
                section.extraction_result_uri for section in document.sections
            )
            if has_extraction_data:
                logger.info(f"Skipping BDA for document {object_key} - already has {len(document.sections)} sections with extraction data")
                # Send task success immediately to continue workflow
                sfn_client = boto3.client('stepfunctions')
                sfn_client.send_task_success(
                    taskToken=task_token,
                    output=json.dumps({
                        "metadata": {
                            "input_bucket": input_bucket,
                            "object_key": object_key,
                            "working_bucket": working_bucket,
                            "output_prefix": object_key,
                            "skipped": True
                        },
                        "document": document.serialize_document(working_bucket, "bda_skip", logger)
                    })
                )
                return {"skipped": True, "object_key": object_key}
        
        track_task_token(object_key, task_token)

        input_s3_uri = S3Util.bucket_key_to_s3_uri(input_bucket, object_key)
        output_s3_uri = S3Util.bucket_key_to_s3_uri(working_bucket, f"{object_key}/bda_responses")
        bda_response = invoke_data_automation(data_project_arn=data_project_arn, 
                                              input_s3_uri=input_s3_uri, 
                                              output_s3_uri=output_s3_uri)

        response = {
            "metadata": {
                "input_bucket": input_bucket, 
                "object_key": object_key,
                "working_bucket": working_bucket,
                "output_prefix": object_key, 
            },
            "bda_response": bda_response
        }
        logger.info(f"API invocation successful. Response: {json.dumps(bda_response, default=str)}")
        return response

    except Exception as e:
        logger.error(f"Error processing request: {str(e)}", exc_info=True)
        raise
