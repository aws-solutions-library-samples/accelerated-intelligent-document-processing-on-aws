# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# src/lambda/discovery_processor/index.py
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import os
import logging
from datetime import datetime
import boto3
import requests
from aws_requests_auth.aws_auth import AWSRequestsAuth
from idp_common.discovery.classes_discovery import ClassesDiscovery
from idp_common.discovery.rules_discovery import RulesDiscovery

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))
# Get LOG_LEVEL from environment variable with INFO as default

dynamodb = boto3.resource('dynamodb')

# Initialize AWS session for AppSync authentication
session = boto3.Session()
credentials = session.get_credentials()

# Get environment variables
APPSYNC_API_URL = os.environ.get("APPSYNC_API_URL")



def handler(event, context):
    """
    Processes discovery jobs from SQS queue.

    Args:
        event (dict): SQS event containing discovery job messages
        context (object): Lambda context

    Returns:
        dict: Processing results
    """
    logger.info(f"Received event: {json.dumps(event)}")

    results = []
    status = 'SUCCESS'

    batch_item_failures = []
    sqs_batch_response = {}
    for record in event.get('Records', []):
        try:
            # Parse the SQS message
            message_body = json.loads(record['body'])
            job_id = message_body.get('jobId')
            document_key = message_body.get('documentKey')
            ground_truth_key = message_body.get('groundTruthKey')
            bucket = message_body.get('bucket')
            version = message_body.get('version')
            page_range = message_body.get('pageRange')
            class_name_hint = message_body.get('classNameHint')

            logger.info(f"Processing discovery job: {job_id} with version: {version}, page_range: {page_range}, class_name_hint: {class_name_hint}")

            # Determine discovery type: "classes" (default) or "rules"
            discovery_type = message_body.get('discoveryType', 'classes')
            logger.info(f"Discovery type: {discovery_type}")

            # Update job status to IN_PROGRESS
            update_job_status(job_id, 'IN_PROGRESS', status_message="Starting discovery processing...")

            # Route to the appropriate discovery handler
            if discovery_type == 'rules':
                result = process_rules_discovery_job(job_id, document_key, bucket, version)
            else:
                result = process_discovery_job(job_id, document_key, ground_truth_key, bucket, version, page_range, class_name_hint)
            results.append(result)

        except Exception as e:
            status = 'Failed'
            logger.error(f"Error processing record: {str(e)}")
            batch_item_failures.append({"itemIdentifier": record['messageId']})
            # Update job status to FAILED if we have a job_id
            if 'job_id' in locals():
                # Create a user-friendly error message
                error_msg = _get_user_friendly_error(str(e))
                update_job_status(job_id, 'FAILED', error_message=error_msg)
            results.append({'status': 'error', 'error': str(e)})

    
    sqs_batch_response["batchItemFailures"] = batch_item_failures
    return sqs_batch_response


def _wait_for_s3_object(bucket, key, max_wait_seconds=60, initial_delay=2):
    """
    Wait for an S3 object to become available, with exponential backoff.
    
    The SQS message is sent immediately when the upload resolver creates the job,
    but the browser's presigned POST upload to S3 may still be in progress.
    This replaces the old hardcoded time.sleep(30) with a proper poll.
    
    Args:
        bucket: S3 bucket name
        key: S3 object key
        max_wait_seconds: Maximum total time to wait (default 60s)
        initial_delay: Initial delay between checks in seconds (default 2s)
        
    Raises:
        Exception: If object doesn't appear within max_wait_seconds
    """
    import time
    s3_client = boto3.client('s3')
    delay = initial_delay
    total_waited = 0
    
    while total_waited < max_wait_seconds:
        try:
            s3_client.head_object(Bucket=bucket, Key=key)
            logger.info(f"S3 object available: s3://{bucket}/{key} (waited {total_waited:.0f}s)")
            return
        except s3_client.exceptions.ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code == '404':
                logger.info(f"Waiting for S3 object s3://{bucket}/{key}... ({total_waited:.0f}s elapsed, next check in {delay:.1f}s)")
                time.sleep(delay)
                total_waited += delay
                delay = min(delay * 1.5, 10)  # Exponential backoff, cap at 10s
            else:
                # Some other S3 error (permissions, etc.) — don't wait, re-raise
                raise
    
    raise Exception(f"Timed out waiting for document s3://{bucket}/{key} after {max_wait_seconds}s — the file upload may have failed")


def _get_user_friendly_error(error_str):
    """
    Convert technical error messages into user-friendly messages.
    
    Args:
        error_str: Raw error string
        
    Returns:
        str: User-friendly error message
    """
    error_lower = error_str.lower()
    
    if "timed out waiting for document" in error_lower:
        return f"Upload timeout — the document file was not found in S3. The browser upload may have failed or been interrupted. Please try again. Details: {error_str}"
    elif "throttl" in error_lower:
        return f"Service throttled — please retry in a few minutes. Details: {error_str}"
    elif "timeout" in error_lower:
        return f"Processing timed out — the document may be too large or complex. Details: {error_str}"
    elif "access denied" in error_lower or "forbidden" in error_lower:
        return f"Access denied — check IAM permissions for the discovery processor. Details: {error_str}"
    elif "failed to load configuration" in error_lower:
        return f"Configuration error — unable to load IDP configuration. Details: {error_str}"
    elif "failed to extract data" in error_lower or "failed to process document" in error_lower:
        return f"AI analysis failed — the model could not extract a valid schema from this document. Try a clearer document or different model. Details: {error_str}"
    elif "json" in error_lower and ("parse" in error_lower or "decode" in error_lower):
        return f"Invalid response format — the AI model returned malformed output. Please retry. Details: {error_str}"
    else:
        return error_str


def process_discovery_job(job_id, document_key, ground_truth_key, bucket, version, page_range=None, class_name_hint=None):
    """
    Process a single discovery job using ClassesDiscovery.

    Args:
        job_id (str): Unique job identifier
        document_key (str): S3 key for the document file
        ground_truth_key (str): S3 key for the ground truth file
        bucket (str): S3 bucket name
        version (str): Configuration version to save to
        page_range (str, optional): Page range string (e.g., "1-3") for multi-section discovery

    Returns:
        dict: Processing result
    """
    try:
        range_label = f", page_range={page_range}" if page_range else ""
        logger.info(f"Processing discovery job {job_id}: document={document_key}, ground_truth={ground_truth_key}{range_label}")

        # Get required environment variables
        region = os.environ.get("AWS_REGION")
        
        # Wait for document to be available in S3 (browser upload via presigned POST may still be in progress)
        update_job_status(job_id, 'IN_PROGRESS', status_message="Waiting for document upload to complete...")
        _wait_for_s3_object(bucket, document_key)
        
        # Progress: Loading configuration
        update_job_status(job_id, 'IN_PROGRESS', status_message="Loading configuration and initializing AI model...")
        
        # Initialize ClassesDiscovery
        classes_discovery = ClassesDiscovery(
            input_bucket=bucket,
            input_prefix=document_key,
            region=region,
            version=version
        )

        # Progress: Analyzing document
        page_label = f" (pages {page_range})" if page_range else ""
        if ground_truth_key:
            update_job_status(job_id, 'IN_PROGRESS', status_message=f"Analyzing document{page_label} with ground truth reference...")
        else:
            update_job_status(job_id, 'IN_PROGRESS', status_message=f"Analyzing document structure{page_label} with AI...")

        # Process the discovery job based on whether ground truth is provided
        if ground_truth_key:
            logger.info(f"Processing with ground truth: {ground_truth_key}, page_range: {page_range}")
            result = classes_discovery.discovery_classes_with_document_and_ground_truth(
                input_bucket=bucket,
                input_prefix=document_key,
                ground_truth_key=ground_truth_key,
                page_range=page_range
            )
        else:
            logger.info(f"Processing without ground truth, page_range: {page_range}")
            result = classes_discovery.discovery_classes_with_document(
                input_bucket=bucket,
                input_prefix=document_key,
                page_range=page_range,
                class_name_hint=class_name_hint
            )

        # Extract the discovered class name from the result schema
        discovered_class_name = None
        if result and result.get("schema"):
            schema = result["schema"]
            discovered_class_name = (
                schema.get("$id") 
                or schema.get("x-aws-idp-document-type") 
                or schema.get("title")
                or "Unknown"
            )

        # #765: the sample appeared to hold several records of the class. Carried as a
        # suggestion on the job (JSON string); discovery never sets the class flag.
        multi_instance_hint = (result or {}).get("multi_instance_hint")
        multi_instance_hint_json = json.dumps(multi_instance_hint) if multi_instance_hint else None

        # Progress: Saving to configuration (already done inside ClassesDiscovery)
        update_job_status(job_id, 'IN_PROGRESS', status_message=f"Discovered class '{discovered_class_name}' — saving to configuration...")

        # Update job status to COMPLETED with discovered class name
        success_message = f"Discovery complete. Added document class '{discovered_class_name}'"
        if multi_instance_hint:
            success_message += (
                f". The sample appears to contain {multi_instance_hint.get('instance_count')} "
                f"records of this class — open the job details to review the "
                f"several-documents-per-section suggestion"
            )
        optimization_triggered = False

        # Trigger async blueprint optimization if conditions are met:
        # 1. use_bda is enabled in config
        # 2. enable_blueprint_optimization is true in config
        # 3. Ground truth file was provided
        # 4. Document is a PDF
        use_bda = getattr(classes_discovery.config, "use_bda", False)
        optimization_enabled = getattr(
            classes_discovery.config, "enable_blueprint_optimization", False
        )
        if use_bda and optimization_enabled and ground_truth_key and document_key.lower().endswith(".pdf"):
            try:
                optimization_function = os.environ.get(
                    "BLUEPRINT_OPTIMIZATION_FUNCTION_NAME"
                )
                if optimization_function:
                    lambda_client = boto3.client("lambda")
                    lambda_client.invoke(
                        FunctionName=optimization_function,
                        InvocationType="Event",
                        Payload=json.dumps(
                            {
                                "jobId": job_id,
                                "classSchema": result["schema"],
                                "documentKey": document_key,
                                "groundTruthKey": ground_truth_key,
                                "bucket": bucket,
                                "version": version,
                                "discoveredClassName": discovered_class_name,
                            }
                        ),
                    )
                    optimization_triggered = True
                    success_message = (
                        f"Discovery complete. Added document class"
                        f" '{discovered_class_name}'."
                        f" Blueprint optimization in progress..."
                    )
                    logger.info(
                        f"Invoked blueprint optimization Lambda"
                        f" for job {job_id}"
                    )
                else:
                    logger.warning(
                        "BLUEPRINT_OPTIMIZATION_FUNCTION_NAME"
                        " not configured, skipping optimization"
                    )
            except Exception as e:
                logger.warning(
                    f"Failed to invoke blueprint optimization"
                    f" for job {job_id}: {e}"
                )

        if optimization_triggered:
            update_job_status(
                job_id,
                'OPTIMIZATION_IN_PROGRESS',
                discovered_class_name=discovered_class_name,
                status_message=success_message,
                multi_instance_hint=multi_instance_hint_json,
            )
        else:
            update_job_status(
                job_id,
                'COMPLETED',
                discovered_class_name=discovered_class_name,
                status_message=success_message,
                multi_instance_hint=multi_instance_hint_json,
            )

        logger.info(f"Successfully processed discovery job: {job_id}, discovered class: {discovered_class_name}")

        return {
            "status": result["status"],
            "jobId": job_id,
            "discoveredClassName": discovered_class_name,
            "message": success_message
        }

    except Exception as e:
        logger.error(f"Error processing discovery job {job_id}: {str(e)}")
        error_msg = _get_user_friendly_error(str(e))
        update_job_status(job_id, 'FAILED', error_message=error_msg)
        raise


def process_rules_discovery_job(job_id, document_key, bucket, version=None):
    """
    Process a rules discovery job using RulesDiscovery.

    Extracts business rules from a policy document and saves them as
    rule_classes in the DynamoDB configuration table. These rule_classes
    are then consumed by RuleValidationService at runtime.

    Args:
        job_id (str): Unique job identifier
        document_key (str): S3 key for the policy document
        bucket (str): S3 bucket name

    Returns:
        dict: Processing result
    """
    try:
        logger.info(
            f"Processing rules discovery job {job_id}: "
            f"document={document_key}, bucket={bucket}"
        )

        region = os.environ.get("AWS_REGION")

        # Wait for the browser-side S3 upload to complete before reading the
        # object. Without this the SQS-triggered invocation can race the upload
        # and fail with NoSuchKey.
        update_job_status(
            job_id,
            'IN_PROGRESS',
            status_message="Waiting for document upload to complete...",
        )
        _wait_for_s3_object(bucket, document_key)

        update_job_status(
            job_id,
            'IN_PROGRESS',
            status_message="Extracting policy rules from document...",
        )

        # Initialize RulesDiscovery — loads config from DynamoDB
        rules_discovery = RulesDiscovery(
            input_bucket=bucket,
            input_prefix=document_key,
            region=region,
            version=version,
        )

        # Extract rules from the policy document
        # This calls _extract_rules() (agentic or traditional based on config)
        # and _save_rules_to_config() which persists rule_classes to DynamoDB
        result = rules_discovery.discovery_rules_from_document(
            input_bucket=bucket,
            input_prefix=document_key,
        )

        rule_classes = result.get("rules", [])
        num_rule_classes = len(rule_classes)
        num_rules = sum(len(rc.get("rule_properties") or {}) for rc in rule_classes)
        saved_type = (
            rule_classes[0].get("x-aws-idp-rule-type")
            or rule_classes[0].get("x-aws-idp-policy-type")
            if rule_classes
            else None
        )

        logger.info(
            f"Successfully extracted {num_rule_classes} rule classes "
            f"({num_rules} rules total) from policy document for job {job_id}"
        )

        # Update job status to COMPLETED with a human-readable summary so the
        # UI stops displaying "Starting discovery processing..." and users can
        # jump to Configuration → Policy Schema to see the appended rules.
        saved_version = getattr(rules_discovery, "version", None) or "active version"
        success_message = (
            f"Extracted {num_rules} rules — appended to policy_classes in "
            f"version '{saved_version}'. View in Configuration → Policy Schema."
        )

        # Append any advisories the save raised (no document-matching regex on
        # any class, duplicated rule names). These describe states in which the
        # extracted rules will not fire, or will be billed repeatedly, so they
        # must reach the person who ran discovery — not just CloudWatch.
        warnings = result.get("warnings") or []
        if warnings:
            success_message = " ".join([success_message, "⚠️ ", *warnings])
            logger.warning(
                "Rules discovery job %s completed with %d advisory warning(s)",
                job_id,
                len(warnings),
            )

        update_job_status(
            job_id,
            'COMPLETED',
            discovered_class_name=saved_type,
            status_message=success_message,
        )

        return {
            "status": result["status"],
            "jobId": job_id,
            "numRuleClasses": num_rule_classes,
            "message": success_message,
        }

    except Exception as e:
        logger.error(f"Error processing rules discovery job {job_id}: {str(e)}")
        update_job_status(job_id, 'FAILED', str(e))
        raise


def update_job_status_via_appsync(job_id, status, error_message=None, discovered_class_name=None, status_message=None, multi_instance_hint=None):
    """
    Update discovery job status via AppSync GraphQL mutation to trigger subscriptions.
    
    Args:
        job_id (str): Unique job identifier
        status (str): New status
        error_message (str, optional): Error message if status is FAILED
        discovered_class_name (str, optional): Name of the discovered document class
        status_message (str, optional): Human-readable progress/result message
    """
    try:
        if not APPSYNC_API_URL:
            logger.warning("APPSYNC_API_URL not configured, falling back to direct DynamoDB update")
            update_job_status_direct(job_id, status, error_message, discovered_class_name, status_message, multi_instance_hint)
            return

        # Prepare the GraphQL mutation with all optional fields
        mutation = """
        mutation UpdateDiscoveryJobStatus($jobId: ID!, $status: String!, $errorMessage: String, $discoveredClassName: String, $statusMessage: String, $multiInstanceHint: String) {
            updateDiscoveryJobStatus(jobId: $jobId, status: $status, errorMessage: $errorMessage, discoveredClassName: $discoveredClassName, statusMessage: $statusMessage, multiInstanceHint: $multiInstanceHint) {
                jobId
                status
                errorMessage
                discoveredClassName
                statusMessage
                multiInstanceHint
            }
        }
        """
        
        logger.info(f"Updating AppSync for discovery job {job_id}, status {status}")
        
        # Prepare the variables
        variables = {
            "jobId": job_id,
            "status": status
        }
        
        if error_message:
            variables["errorMessage"] = error_message
        if discovered_class_name:
            variables["discoveredClassName"] = discovered_class_name
        if status_message:
            variables["statusMessage"] = status_message
        if multi_instance_hint:
            variables["multiInstanceHint"] = multi_instance_hint
        
        # Set up AWS authentication
        region = session.region_name or os.environ.get('AWS_REGION', 'us-east-1')
        auth = AWSRequestsAuth(
            aws_access_key=credentials.access_key,
            aws_secret_access_key=credentials.secret_key,
            aws_token=credentials.token,
            aws_host=APPSYNC_API_URL.replace('https://', '').replace('/graphql', ''),
            aws_region=region,
            aws_service='appsync'
        )
        
        # Prepare the request
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        payload = {
            'query': mutation,
            'variables': variables
        }
        
        logger.info(f"Publishing discovery job update to AppSync for job: {job_id}")
        logger.debug(f"Mutation payload: {json.dumps(payload)}")
        
        # Make the request
        response = requests.post(
            APPSYNC_API_URL,
            json=payload,
            headers=headers,
            auth=auth,
            timeout=30
        )
        
        # Check for successful response
        if response.status_code == 200:
            response_json = response.json()
            if "errors" not in response_json:
                logger.info(f"Successfully published discovery job update for: {job_id}")
                logger.debug(f"Response: {response.text}")
                return True
            else:
                logger.error(f"GraphQL errors in response: {json.dumps(response_json.get('errors'))}")
                logger.error(f"Full mutation payload: {json.dumps(payload)}")
                return False
        else:
            logger.error(f"Failed to publish discovery job update. Status: {response.status_code}, Response: {response.text}")
            return False
        
    except Exception as e:
        logger.error(f"Error updating job status via AppSync: {str(e)}")
        import traceback
        logger.error(f"Error traceback: {traceback.format_exc()}")
        # Fall back to direct DynamoDB update
        update_job_status_direct(job_id, status, error_message, discovered_class_name, status_message, multi_instance_hint)
        return False


def update_job_status_direct(job_id, status, error_message=None, discovered_class_name=None, status_message=None, multi_instance_hint=None):
    """
    Fallback method to update discovery job status directly in DynamoDB.
    Used when AppSync is not available or fails.

    Args:
        job_id (str): Unique job identifier
        status (str): New status
        error_message (str, optional): Error message if status is FAILED
        discovered_class_name (str, optional): Name of the discovered document class
        status_message (str, optional): Human-readable progress/result message
    """
    try:
        table_name = os.environ.get('DISCOVERY_TRACKING_TABLE')
        if not table_name:
            logger.warning("DISCOVERY_TRACKING_TABLE not configured, skipping status update")
            return

        table = dynamodb.Table(table_name)

        update_expression = "SET #status = :status, updatedAt = :updated_at"
        expression_attribute_names = {'#status': 'status'}
        expression_attribute_values = {
            ':status': status,
            ':updated_at': datetime.now().isoformat()
        }

        if error_message:
            update_expression += ", errorMessage = :error_message"
            expression_attribute_values[':error_message'] = error_message

        if discovered_class_name:
            update_expression += ", discoveredClassName = :discovered_class_name"
            expression_attribute_values[':discovered_class_name'] = discovered_class_name

        if status_message:
            update_expression += ", statusMessage = :status_message"
            expression_attribute_values[':status_message'] = status_message

        if multi_instance_hint:
            update_expression += ", multiInstanceHint = :multi_instance_hint"
            expression_attribute_values[':multi_instance_hint'] = multi_instance_hint

        table.update_item(
            Key={'jobId': job_id},
            UpdateExpression=update_expression,
            ExpressionAttributeNames=expression_attribute_names,
            ExpressionAttributeValues=expression_attribute_values
        )

        logger.info(f"Updated job {job_id} status to {status} (direct DynamoDB)")

    except Exception as e:
        logger.error(f"Error updating job status directly: {str(e)}")
        # Don't fail the processing if status update fails


# Keep the old function name for backward compatibility
def update_job_status(job_id, status, error_message=None, discovered_class_name=None, status_message=None, multi_instance_hint=None):
    """
    Update discovery job status. This now uses AppSync by default.
    """
    update_job_status_via_appsync(job_id, status, error_message, discovered_class_name, status_message, multi_instance_hint)
