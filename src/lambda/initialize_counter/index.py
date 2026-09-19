# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import cfnresponse
import logging
from botocore.exceptions import ClientError
from log_sanitizer import sanitize_event_for_logging
import os

# Initialize logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))
# Get LOG_LEVEL from environment variable with INFO as default

# DynamoDB resource
dynamodb = boto3.resource('dynamodb')
CONCURRENCY_TABLE = os.environ.get('CONCURRENCY_TABLE')
concurrency_table = dynamodb.Table(CONCURRENCY_TABLE)

COUNTER_ID = "workflow_counter"

def handler(event, context):
    logger.info(f"Event received: {sanitize_event_for_logging(event)}")
    try:
        # Handle CloudFormation CREATE events
        if event['RequestType'] == 'Create':
            concurrency_table.put_item(
                Item={
                    'counter_id': COUNTER_ID,
                    'active_count': 0
                },
                ConditionExpression='attribute_not_exists(counter_id)'
            )
            logger.info("Counter initialized")

        elif event['RequestType'] == 'Delete':
            # Handle CloudFormation DELETE events
            concurrency_table.delete_item(
                Key={
                    'counter_id': COUNTER_ID
                }
            )
            logger.info("Counter deleted")

        # Send a success response to CloudFormation
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {})

    except ClientError as e:
        logger.error(f"Error in DynamoDB operation: {e}")
        # Send a failure response to CloudFormation
        cfnresponse.send(event, context, cfnresponse.FAILED, {"Error": str(e)}, reason=str(e))
        raise

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        # Send a failure response to CloudFormation
        cfnresponse.send(event, context, cfnresponse.FAILED, {"Error": str(e)}, reason=str(e))
        raise