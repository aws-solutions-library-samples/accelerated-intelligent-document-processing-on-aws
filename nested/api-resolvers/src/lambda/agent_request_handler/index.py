# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function to handle agent query requests.
This function creates a job record in DynamoDB and invokes the agent processor Lambda.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime

import boto3
from botocore.exceptions import ClientError
from log_sanitizer import sanitize_event_for_logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")

# Get environment variables
AGENT_TABLE = os.environ.get("AGENT_TABLE")
AGENT_PROCESSOR_FUNCTION = os.environ.get("AGENT_PROCESSOR_FUNCTION")
DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "30"))


def validate_user_identity(identity):
    """
    Validate and extract user identity from the event context.
    
    Args:
        identity: The identity object from the event context
        
    Returns:
        The validated user ID
        
    Raises:
        ValueError: If no valid user identity is found
    """
    user_id = identity.get("username") or identity.get("sub")
    
    if not user_id:
        logger.warning("No valid user identity found in request")
        # For security, we'll still use "anonymous" but log the warning
        user_id = "anonymous"
    
    logger.info(f"Request authenticated for user: {user_id}")
    return user_id


def handler(event, context):
    """
    Handle agent query requests from AppSync.
    
    Args:
        event: The event dict from AppSync
        context: The Lambda context
        
    Returns:
        The job record with jobId
    """
    logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")
    
    try:
        # Extract the query and agent IDs from the event
        arguments = event.get("arguments", {})
        query = arguments.get("query")
        agent_ids = arguments.get("agentIds", [])
        
        if not query:
            error_msg = "Query parameter is required"
            logger.error(error_msg)
            raise Exception(error_msg)
        
        if len(query) > 100000:
            error_msg = "Query exceeds maximum length of 100000 characters"
            logger.error(f"{error_msg}. Query length: {len(query)}")
            raise Exception(error_msg)
        
        if not agent_ids:
            error_msg = "At least one agent ID is required"
            logger.error(error_msg)
            raise Exception(error_msg)
        
        # Extract and validate user ID from the identity context
        identity = event.get("identity", {})
        try:
            user_id = validate_user_identity(identity)
        except ValueError as e:
            return {
                "statusCode": 401,
                "body": str(e)
            }

        # Defense-in-depth RBAC: submitAgentQuery is Admin/Author/Viewer only
        # (Reviewer excluded) in the AppSync schema. The REST dispatcher's
        # Cognito authorizer only authenticates, so re-enforce the group check
        # here (dispatcher maps PermissionError -> 403).
        claims = identity.get("claims") or {}
        groups = claims.get("cognito:groups") or []
        if isinstance(groups, str):
            groups = [groups]
        if not ({"Admin", "Author", "Viewer"}.intersection(groups)):
            logger.warning(
                "Forbidden: caller %s (groups=%s) attempted submitAgentQuery",
                user_id, list(groups),
            )
            raise PermissionError(
                "Unauthorized: submitAgentQuery requires Admin, Author, or Viewer group"
            )

        # Generate a unique job ID
        job_id = str(uuid.uuid4())
        
        # Calculate expiration time (TTL)
        current_time = int(time.time())
        expires_after = current_time + (DATA_RETENTION_DAYS * 24 * 60 * 60)
        
        # Create a timestamp for job creation - format as ISO-8601 with Z suffix for UTC
        created_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        
        # Create the job record in DynamoDB
        # Use a composite key with PK = "agent#{userId}" and SK = jobId
        # This follows the pattern used in the existing application
        table = dynamodb.Table(AGENT_TABLE)
        job_record = {
            "PK": f"agent#{user_id}",
            "SK": job_id,
            "query": query,
            "agentIds": json.dumps(agent_ids),  # Store as JSON string
            "status": "PENDING",
            "createdAt": created_at,
            "expiresAfter": expires_after
        }
        
        table.put_item(Item=job_record)
        logger.info(f"Created job record: {job_id} for user: {user_id}")
        
        # Invoke the agent processor Lambda asynchronously
        lambda_client.invoke(
            FunctionName=AGENT_PROCESSOR_FUNCTION,
            InvocationType="Event",
            Payload=json.dumps({
                "userId": user_id,
                "jobId": job_id
            })
        )
        logger.info(f"Invoked agent processor for job: {job_id}")
        
        # Return the job record to the client (without exposing userId)
        return {
            "jobId": job_id,
            "status": "PENDING",
            "query": query,
            "createdAt": created_at
        }
        
    except ClientError as e:
        error_msg = f"DynamoDB error: {str(e)}"
        logger.error(error_msg)
        return {
            "statusCode": 500,
            "body": error_msg
        }
    except PermissionError:
        # RBAC denial — must propagate so the dispatcher maps it to 403 (not a
        # 500 body). Never swallow into the generic handler below.
        raise
    except Exception as e:
        # Check if this is a validation error that should propagate as GraphQL error
        error_str = str(e)
        if any(msg in error_str for msg in [
            "Query parameter is required",
            "Query exceeds maximum length",
            "At least one agent ID is required"
        ]):
            # Re-raise validation errors so they become GraphQL errors
            raise e
        
        # Handle other unexpected errors
        error_msg = f"Error processing request: {error_str}"
        logger.error(error_msg)
        return {
            "statusCode": 500,
            "body": error_msg
        }
