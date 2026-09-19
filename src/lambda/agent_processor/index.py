# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function to process agent queries using Strands agents.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from io import StringIO  # Used for capturing stdout during agent execution

import boto3
from botocore.exceptions import ClientError

# Import the analytics agent and configuration utilities from idp_common
from idp_common.agents.analytics import get_analytics_config, parse_agent_response
from idp_common.agents.common.config import configure_logging
from idp_common.agents.factory import agent_factory
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

# Configure logging for both application and Strands framework
# This will respect both LOG_LEVEL and STRANDS_LOG_LEVEL environment variables
configure_logging()

# Get logger for this module
logger = logging.getLogger(__name__)

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
session = boto3.Session()

# Get environment variables
AGENT_TABLE = os.environ.get("AGENT_TABLE")


def validate_job_ownership(table, user_id, job_id):
    """
    Validate that the job belongs to the specified user.
    
    Args:
        table: DynamoDB table resource
        user_id: The user ID to validate against
        job_id: The job ID to validate
        
    Returns:
        The job record if valid
        
    Raises:
        ValueError: If the job doesn't exist or doesn't belong to the user
    """
    try:
        response = table.get_item(
            Key={
                "PK": f"agent#{user_id}",
                "SK": job_id
            }
        )
        
        job_record = response.get("Item")
        if not job_record:
            error_msg = f"Job not found: {job_id} for user: {user_id}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        logger.info(f"Job ownership validated for job: {job_id}, user: {user_id}")
        return job_record
        
    except ClientError as e:
        error_msg = f"Error validating job ownership: {str(e)}"
        logger.error(error_msg)
        raise ValueError(error_msg)


def process_agent_query(query: str, agent_ids: list, job_id: str = None, user_id: str = None) -> dict:
    """
    Process an agent query using either a single agent or orchestrator.
    
    Args:
        query: The natural language query to process
        agent_ids: List of agent IDs to use
        job_id: Agent job ID for monitoring (optional)
        user_id: User ID for monitoring (optional)
        
    Returns:
        Dict containing the agent result
    """
    
    try:
        # Validate agent_ids is not empty
        if not agent_ids:
            raise ValueError("At least one agent ID is required")
            
        logger.info(f"Processing query with agent IDs: {agent_ids}")
        
        # Get analytics configuration
        config = get_analytics_config()
        logger.info("Analytics configuration loaded successfully")
        
        # Determine whether to use single agent or orchestrator
        if len(agent_ids) == 1:
            # Single agent - use directly
            agent_id = agent_ids[0]
            logger.info(f"Using single agent: {agent_id}")
            
            agent = agent_factory.create_agent(
                agent_id=agent_id,
                config=config,
                session=session,
                job_id=job_id,
                user_id=user_id
            )
            logger.info("Single agent created successfully")
        else:
            # Multiple agents - use orchestrator
            logger.info(f"Using orchestrator with {len(agent_ids)} agents: {agent_ids}")
            
            agent = agent_factory.create_orchestrator_agent(
                agent_ids=agent_ids,
                config=config,
                session=session,
                job_id=job_id,
                user_id=user_id
            )
            logger.info("Orchestrator agent created successfully")
        
        # Note: DynamoDB message tracker is automatically added by IDPAgent
        # if job_id and user_id are provided in kwargs
        
        # Process the query using context manager for MCP agents
        logger.info(f"Processing query: {query}")

        # The Strands framework seem to output complete LLM responses directly to stdout
        # statements or str() conversions, bypassing the logging system. This causes large
        # unformatted text blocks to appear in CloudWatch logs without proper log prefixes.
        # 
        # Temporarily redirect stdout during agent execution to capture and suppress
        # these unwanted outputs while preserving stderr for the logging system.
        old_stdout = sys.stdout
        captured_output = StringIO()
        
        try:
            # Redirect stdout to capture any print statements
            sys.stdout = captured_output
            
            with agent:
                response = agent(query)
                
        finally:
            # Restore stdout
            sys.stdout = old_stdout
            
        # Log any captured stdout content
        captured_text = captured_output.getvalue()
        if captured_text.strip():
            logger.info(f"Result from agent execution: {captured_text[:200]}...")
            
        logger.info("Query processed successfully")
        
        # Parse the response using the new parsing function
        try:
            result = parse_agent_response(response)
            logger.info(f"Parsed response with type: {result.get('responseType', 'unknown')}")
            return result
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Failed to parse agent response: {e}")
            logger.error(f"Raw response: {response}")
            # Return a text response with the raw output
            return {
                "responseType": "text",
                "content": f"Error parsing response: {response}",
            }
            
    except Exception as e:
        logger.exception(f"Error processing agent query: {str(e)}")
        # Re-raise the exception so the retry logic can handle it properly
        raise


_AGENT_TERMINAL_STATUSES = {"COMPLETED", "FAILED"}


def update_job_status(job_id, status, user_id, result=None, error=None):
    """
    Persist the job status/result to the Agent DynamoDB table.

    This is the authoritative status write: the UI reads job status by polling
    getAgentJobStatus, which reads this DynamoDB item. (AppSync — previously the
    only status-persistence path — was removed as part of the AppSync->REST
    migration.)

    Args:
        job_id: The ID of the job to update.
        status: The new status (PROCESSING / COMPLETED / FAILED).
        user_id: The user who owns the job (part of the DynamoDB key).
        result: The result payload (JSON string or serializable object), optional.
        error: The error message, optional.

    Returns:
        True if the DynamoDB write succeeded, else False.
    """
    try:
        table = dynamodb.Table(AGENT_TABLE)

        names = {"#status": "status"}
        values = {":status": status}
        expr = "SET #status = :status"

        if result is not None:
            names["#result"] = "result"
            values[":result"] = result if isinstance(result, str) else json.dumps(result)
            expr += ", #result = :result"
        if error is not None:
            names["#error"] = "error"
            values[":error"] = error if isinstance(error, str) else str(error)
            expr += ", #error = :error"
        if status in _AGENT_TERMINAL_STATUSES:
            names["#completedAt"] = "completedAt"
            values[":completedAt"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            expr += ", #completedAt = :completedAt"

        table.update_item(
            Key={"PK": f"agent#{user_id}", "SK": job_id},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        logger.info(f"Persisted job status to DynamoDB: {job_id} -> {status}")
        return True
    except Exception as e:
        logger.error(f"Failed to persist job status to DynamoDB for job {job_id}: {str(e)}")
        import traceback
        logger.error(f"Error traceback: {traceback.format_exc()}")
        return False


def handler(event, context):
    """
    Process agent queries.
    
    Args:
        event: The event dict with userId and jobId
        context: The Lambda context
        
    Returns:
        The updated job record
    """
    # Redacted: the event carries the caller's question and identity attributes.
    logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")

    try:
        # Extract user ID and job ID from the event
        user_id = event.get("userId")
        job_id = event.get("jobId")
        
        if not user_id or not job_id:
            error_msg = "userId and jobId are required"
            logger.error(error_msg)
            return {
                "statusCode": 400,
                "body": error_msg
            }
        
        # Get the DynamoDB table
        table = dynamodb.Table(AGENT_TABLE)
        
        # Validate job ownership
        try:
            job_record = validate_job_ownership(table, user_id, job_id)
        except ValueError as e:
            return {
                "statusCode": 403,
                "body": str(e)
            }
        
        # Update the job status to PROCESSING
        update_job_status(job_id, "PROCESSING", user_id)
        logger.info(f"Updated job status to PROCESSING: {job_id}")
        
        # Process the agent query using the agent with retry logic
        # This retries the entire workflow -- if something dies, it restarts
        # from scratch with the initial query.
        max_retries = 3
        retry_delay = 10  # seconds
        result = None
        processing_error = None
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Processing agent query (attempt {attempt + 1}/{max_retries}): {job_id}")
                
                # Parse agentIds from JSON string
                agent_ids_str = job_record.get("agentIds", "[]")
                try:
                    agent_ids = json.loads(agent_ids_str) if isinstance(agent_ids_str, str) else agent_ids_str
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON in agentIds for job {job_id}, using empty list")
                    agent_ids = []
                
                # Validate that agentIds are provided
                if not agent_ids:
                    raise ValueError("No agentIds provided - at least one agent ID is required")
                
                # Process the query using the agent
                result = process_agent_query(
                    job_record.get("query"), 
                    agent_ids, 
                    job_id, 
                    user_id
                )
                logger.info(f"Successfully processed agent query on attempt {attempt + 1}: {job_id}")
                break  # Success, exit retry loop
                
            except Exception as e:
                logger.warning(f"Agent query processing failed on attempt {attempt + 1}/{max_retries} for job {job_id}: {str(e)}")
                processing_error = e
                
                if attempt < max_retries - 1:  # Not the last attempt
                    logger.info(f"Waiting {retry_delay} seconds before retry {attempt + 2}/{max_retries} for job {job_id}")
                    time.sleep(retry_delay) # semgrep-ignore: arbitrary-sleep - Intentional delay. Duration is hardcoded and not user-controlled.
                else:
                    # Last attempt failed
                    logger.error(f"All {max_retries} attempts failed for agent query processing, job {job_id}: {str(e)}")
        
        # Check if processing was successful
        if result is not None:
            # Success path - process the result
            try:
                # Prepare the result with metadata
                analytics_result = {
                    "responseType": result.get("responseType", "text"),
                    "metadata": {
                        "generatedAt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                        "query": job_record.get("query")
                    }
                }
                
                # Add the appropriate data field based on response type
                if result.get("responseType") == "plotData":
                    analytics_result["plotData"] = [result]  # Wrap in array for consistency
                elif result.get("responseType") == "table":
                    analytics_result["tableData"] = result
                else:  # text or fallback
                    analytics_result["content"] = result.get("content", "No content available")
                
                # Serialize the analytics_result to a JSON string
                analytics_result_json = json.dumps(analytics_result)
                
                # Update the job status to COMPLETED with result. Writes to
                # DynamoDB (authoritative, polled by the UI) with completedAt,
                # and best-effort notifies AppSync if still configured.
                success = update_job_status(
                    job_id=job_id,
                    status="COMPLETED",
                    user_id=user_id,
                    result=analytics_result_json
                )

                if success:
                    logger.info(f"Successfully updated job status to COMPLETED with result: {job_id}")
                else:
                    logger.error(f"Failed to update job status for job: {job_id}")
                
                # Return the updated job record (without exposing userId)
                return {
                    "jobId": job_id,
                    "status": "COMPLETED",
                    "query": job_record.get("query"),
                    "createdAt": job_record.get("createdAt"),
                    "result": analytics_result
                }
                
            except Exception as e:
                # Handle result processing error (this is different from query processing error)
                error_msg = f"Error processing analytics result: {str(e)}"
                logger.error(error_msg)
                processing_error = e
        
        # Failure path - all retries failed or result processing failed
        if processing_error:
            error_msg = f"Agent query processing failed: {str(processing_error)}"
            logger.error(error_msg)
            
            # Update the job status to FAILED. Writes to DynamoDB (authoritative,
            # polled by the UI) with completedAt, and best-effort notifies AppSync
            # if still configured.
            success = update_job_status(
                job_id=job_id,
                status="FAILED",
                user_id=user_id,
                error=error_msg
            )

            if success:
                logger.info(f"Successfully updated job status to FAILED: {job_id}")
            else:
                logger.error(f"Failed to update job status for job: {job_id}")
            
            return {
                "statusCode": 500,
                "body": error_msg
            }
        
    except ClientError as e:
        logger.error(f"DynamoDB error: {str(e)}")
        return {
            "statusCode": 500,
            "body": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        return {
            "statusCode": 500,
            "body": f"Error processing request: {str(e)}"
        }
