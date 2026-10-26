# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import cfnresponse
import hashlib
import json
import logging
import os
import time
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectionError as BotocoreConnectionError, HTTPClientError

# Set up logging
logger = logging.getLogger()
log_level = os.environ.get('LOG_LEVEL', 'INFO')
logger.setLevel(getattr(logging, log_level))

# Error codes from the s3vectors service model (botocore data/s3vectors). A missing
# bucket or index is NotFoundException on both DeleteVectorBucket and DeleteIndex.
# On the stack Delete's DeleteIndex, ValidationException is read the same way: the only parameters
# sent are the names, and an index whose name the service rejects cannot exist.
# On DeleteVectorBucket it is not trusted on its own, because the model does not
# say which code a non-empty bucket gets: delete_vector_bucket asks GetVectorBucket
# whether the bucket still exists before treating it as gone. ConflictException is the one conflict-class error DeleteVectorBucket declares;
# the model documents that all indexes must be deleted first but does not name the
# code for a bucket that still holds one, so treating it as "not empty" is an
# inference from the model, not a measurement against the service.
NOT_FOUND_CODES = frozenset({'NotFoundException'})
ABSENT_ON_DELETE_CODES = NOT_FOUND_CODES | {'ValidationException'}
BUCKET_NOT_EMPTY_CODES = frozenset({'ConflictException'})

# Bounded wait for a bucket to become deletable after its index is deleted:
# 2 + 4 + 8 + 16 + 30 + 30 + 30 = 120 seconds of backoff across eight attempts,
# inside the function's 600-second timeout. How long the service takes to
# release a bucket after DeleteIndex has not been measured.
BUCKET_DELETE_ATTEMPTS = 8
BUCKET_DELETE_BACKOFF_SECONDS = 2
BUCKET_DELETE_MAX_BACKOFF_SECONDS = 30

# ListIndexes returns at most 500 indexes per page (maxResults in the service
# model) and a nextToken while more remain. The page cap only guards against a
# service that never stops returning a token: 100 pages is 50,000 indexes.
INDEX_LIST_PAGE_SIZE = 500
INDEX_LIST_MAX_PAGES = 100
# Deleting the indexes stops, and the delete reports FAILED, while this much of
# the invocation remains: the bucket delete's 120 seconds of backoff plus its
# eight calls and the CloudFormation response. Retrying the stack delete
# continues where it stopped.
INDEX_DELETE_MARGIN_SECONDS = 240

# Error codes from the bedrock-agent service model. DeleteKnowledgeBase and
# GetKnowledgeBase both declare ResourceNotFoundException for a missing knowledge
# base; their only parameter is the id, so ValidationException (an id the service
# rejects) cannot name an existing one either. Conflict, throttling and internal
# errors are the transient codes DeleteKnowledgeBase declares and are retried.
KB_ABSENT_CODES = frozenset({'ResourceNotFoundException', 'ValidationException'})
KB_DELETE_RETRYABLE_CODES = frozenset(
    {'ConflictException', 'ThrottlingException', 'InternalServerException'}
)

# DeleteKnowledgeBase retries: 2 + 4 + 8 + 16 + 8 x 30 = 270 seconds of backoff
# across thirteen attempts, long enough for a knowledge base still CREATING after
# a failed Create to settle before the delete gives up. Then up to 12 polls 10
# seconds apart (120 seconds) for the asynchronous delete to finish. How long
# Bedrock takes to create or delete a knowledge base has not been measured.
#
# Those sleeps are not the whole time a delete takes, because each call can also
# wait on the network and on botocore's own retries. The Delete request's client
# is therefore built with KB_CLIENT_CONFIG, which bounds one call at about
# KB_CALL_SECONDS: two attempts, each at most a 5-second connect plus a 20-second
# wait for data, plus botocore's backoff between them. The read timeout applies
# to each read, so a peer that keeps sending a byte at a time is not bounded by it. The handler then sleeps, and so
# makes another call, only while that would still leave KB_DEADLINE_MARGIN_SECONDS
# of the invocation: room for the next call, one GetKnowledgeBase check after it,
# and the CloudFormation response.
KB_CLIENT_CONFIG = Config(
    retries={'mode': 'standard', 'total_max_attempts': 2},
    connect_timeout=5,
    read_timeout=20,
)
KB_CALL_SECONDS = 55
KB_RESPONSE_SECONDS = 20
# Errors that end a call without an answer from Bedrock: the connection failed,
# timed out or closed. They are transient, and they say nothing about whether
# the knowledge base exists.
TRANSPORT_ERRORS = (BotocoreConnectionError, HTTPClientError)
KB_DELETE_ATTEMPTS = 13
KB_DELETE_BACKOFF_SECONDS = 2
KB_DELETE_MAX_BACKOFF_SECONDS = 30
KB_DELETE_WAIT_POLLS = 12
KB_DELETE_WAIT_SECONDS = 10
# GetKnowledgeBase reads that decide a delete's outcome retry transient errors
# three times (2 + 4 seconds of backoff).
KB_STATUS_ATTEMPTS = 3
KB_DEADLINE_MARGIN_SECONDS = 2 * KB_CALL_SECONDS + KB_RESPONSE_SECONDS


def get_partition(region):
    """Resolve the AWS partition (aws, aws-us-gov, aws-cn) for a region."""
    return boto3.Session().get_partition_for_region(region)


def lambda_handler(event, context):
    """
    Custom Resource handler for S3 Vectors and Knowledge Base management.
    Handles CREATE, UPDATE, and DELETE operations for S3 Vector buckets, indices, and Knowledge Base.
    
    This function is designed to ALWAYS send a CloudFormation response, even on catastrophic failures.
    """
    response_sent = False
    
    try:
        # Safe event logging (handle potential JSON serialization issues)
        try:
            logger.info(f"Event: {json.dumps(event)}")
        except Exception as log_error:
            logger.info(f"Event received (logging error: {str(log_error)})")
        
        # Validate required event structure
        if 'RequestType' not in event:
            raise ValueError("Missing required RequestType in event")
        if 'ResourceProperties' not in event:
            raise ValueError("Missing required ResourceProperties in event")
            
        # Get request type and properties
        request_type = event['RequestType']
        properties = event['ResourceProperties']
        
        resource_type = properties.get('ResourceType', 'S3VectorBucketAndIndex')
        
        # Route to appropriate handler
        if resource_type == 'S3VectorBucketAndIndex':
            response_data = handle_s3_vector_resources(event, context, properties)
        elif resource_type == 'S3VectorsKnowledgeBase':
            response_data = handle_knowledge_base_resources(event, context, properties)
        else:
            raise ValueError(f"Unknown ResourceType: {resource_type}")
        
        # Success - send positive response with proper PhysicalResourceId
        physical_id = get_physical_resource_id(event, properties, response_data)
        logger.info(f"Operation completed successfully: {response_data}")
        logger.info(f"Using PhysicalResourceId: {physical_id}")
        
        cfnresponse.send(event, context, cfnresponse.SUCCESS, response_data, physicalResourceId=physical_id)
        response_sent = True
        
    except Exception as e:
        # Comprehensive error handling with multiple fallback mechanisms
        try:
            error_msg = f"Error in S3 Vectors management: {str(e)}"
            logger.error(error_msg)
            logger.error(f"Exception type: {type(e).__name__}")
            
            # Get appropriate PhysicalResourceId for failed resource
            physical_id = get_physical_resource_id_for_failure(event, properties)
            logger.info(f"Using PhysicalResourceId for failure: {physical_id}")
            
            # Send failure response with PhysicalResourceId
            cfnresponse.send(event, context, cfnresponse.FAILED, {}, 
                           physicalResourceId=physical_id, reason=error_msg[:1024])
            response_sent = True
            
        except Exception as cfn_error:
            # Last resort error handling if even cfnresponse fails
            logger.error(f"CRITICAL: Failed to send CFN response: {str(cfn_error)}")
            logger.error(f"Original error was: {str(e)}")
            
            # Attempt basic cfnresponse with fallback PhysicalResourceId
            try:
                fallback_physical_id = event.get('PhysicalResourceId', context.log_stream_name)
                cfnresponse.send(event, context, cfnresponse.FAILED, {}, physicalResourceId=fallback_physical_id)
                response_sent = True
            except Exception as final_error:
                logger.error(f"FINAL FAILURE: Cannot send basic CFN response: {str(final_error)}")
                # CloudFormation will timeout and handle this as a failure
    
    # Final safety check
    if not response_sent:
        logger.error("WARNING: No CloudFormation response was sent - stack may hang")
        
    return {"statusCode": 200}


def handle_s3_vector_resources(event, context, properties):
    """Handle S3 Vector bucket and index operations."""
    request_type = event['RequestType']
    
    raw_bucket_name = properties.get('BucketName', '')
    bucket_name = sanitize_bucket_name(raw_bucket_name)
    index_name = properties.get('IndexName', '')
    embedding_model = properties.get('EmbeddingModel', '')
    region = properties.get('Region', '')
    kms_key_arn = properties.get('KmsKeyArn', '')
    
    # Extract configurable index properties
    index_config = index_config_from_properties(properties)
    
    logger.info(f"Raw bucket name: {raw_bucket_name}, Sanitized bucket name: {bucket_name}")
    logger.info(f"Index properties - Dimension: {index_config['dimension']}, Metric: {index_config['distance_metric']}, Metadata: {index_config['metadata_configuration']}")
    
    # Initialize S3 Vectors client
    s3vectors_client = boto3.client('s3vectors', region_name=region)
    
    if request_type == 'Create':
        logger.info(f"Creating S3 Vector bucket: {bucket_name}")
        return create_s3_vector_resources(s3vectors_client, bucket_name, index_name, embedding_model, kms_key_arn, index_config)
        
    elif request_type == 'Update':
        logger.info(f"Updating S3 Vector bucket: {bucket_name}")
        old_properties = event.get('OldResourceProperties', {})
        old_raw_bucket_name = old_properties.get('BucketName', '')
        old_bucket_name = sanitize_bucket_name(old_raw_bucket_name)
        old_index_name = old_properties.get('IndexName', '')
        
        # If the bucket name changed, delete the old bucket and its indexes and
        # create the new one. If only the index name changed, keep the bucket
        # and the old index, and create the new index in it. The old index is
        # not deleted here: the knowledge base keeps pointing at it, and if the
        # stack update rolls back, the rollback returns to it with its vectors.
        # The stack Delete deletes it with every other index in the bucket.
        if old_bucket_name != bucket_name:
            if old_bucket_name:
                delete_s3_vector_resources(
                    s3vectors_client, old_bucket_name, old_index_name, strict=False
                )
            return create_s3_vector_resources(s3vectors_client, bucket_name, index_name, embedding_model, kms_key_arn, index_config)
        if old_index_name != index_name:
            return create_s3_vector_resources(s3vectors_client, bucket_name, index_name, embedding_model, kms_key_arn, index_config)
        else:
            # Names haven't changed. S3 Vectors has no UpdateIndex, so a changed
            # dimension, distance metric or metadata configuration can only be
            # applied by recreating the index, which deletes its vectors. Any
            # other property change (embedding model, KMS key) keeps the index,
            # recreating it only if it is missing.
            old_index_config = index_config_from_properties(old_properties)
            recreate = index_config_differs(old_index_config, index_config)
            return update_s3_vector_info(
                s3vectors_client, bucket_name, index_name, index_config, recreate_index=recreate
            )
            
    elif request_type == 'Delete':
        logger.info(f"Deleting S3 Vector bucket: {bucket_name}")
        delete_s3_vector_resources(s3vectors_client, bucket_name, index_name, context=context)
        return {'Status': 'Deleted'}


DEFAULT_METADATA_CONFIGURATION = {
    'nonFilterableMetadataKeys': ['AMAZON_BEDROCK_METADATA', 'AMAZON_BEDROCK_TEXT']
}


def index_config_from_properties(properties):
    """The CreateIndex settings a resource's properties ask for, with the defaults applied."""
    return {
        'dimension': properties.get('VectorDimension', 1024),
        'distance_metric': properties.get('DistanceMetric', 'cosine'),
        'metadata_configuration': properties.get(
            'MetadataConfiguration', DEFAULT_METADATA_CONFIGURATION
        ),
    }


def _normalized_index_config(index_config):
    """Compare as CreateIndex would receive them: CloudFormation sends numbers as
    strings, so ``"1024"`` and ``1024`` are the same dimension, and the order of
    the non-filterable metadata keys does not change the index."""
    metadata = dict(index_config['metadata_configuration'] or {})
    keys = metadata.get('nonFilterableMetadataKeys')
    if isinstance(keys, list):
        metadata['nonFilterableMetadataKeys'] = sorted(keys)
    return (int(index_config['dimension']), str(index_config['distance_metric']), metadata)


def index_config_differs(old_index_config, new_index_config):
    """True when an Update changes a setting that only recreating the index can apply."""
    return _normalized_index_config(old_index_config) != _normalized_index_config(new_index_config)


def sanitize_bucket_name(bucket_name):
    """
    Sanitize bucket name to comply with S3 bucket naming rules:
    - Must be lowercase letters, numbers, and hyphens only
    - Must be between 3 and 63 characters long
    - Must not start or end with a hyphen
    - Must not contain consecutive hyphens
    """
    if not bucket_name:
        return 'default-s3-vectors'
    
    # Convert to lowercase
    sanitized = bucket_name.lower()
    
    # Replace invalid characters with hyphens
    import re
    sanitized = re.sub(r'[^a-z0-9\-]', '-', sanitized)
    
    # Remove consecutive hyphens
    sanitized = re.sub(r'-+', '-', sanitized)
    
    # Remove leading and trailing hyphens
    sanitized = sanitized.strip('-')
    
    # Ensure minimum length
    if len(sanitized) < 3:
        sanitized = f"s3vectors-{sanitized}"
    
    # Ensure maximum length (S3 limit is 63 characters)
    if len(sanitized) > 63:
        sanitized = sanitized[:60] + "-kb"
    
    # Ensure it doesn't start with hyphen (redundant but safe)
    if sanitized.startswith('-'):
        sanitized = 's3' + sanitized
    
    # Ensure it doesn't end with hyphen (redundant but safe) 
    if sanitized.endswith('-'):
        sanitized = sanitized[:-1] + 'kb'
    
    logger.info(f"Sanitized bucket name: {bucket_name} → {sanitized}")
    return sanitized


def handle_knowledge_base_resources(event, context, properties):
    """Handle Knowledge Base creation with S3 Vectors using Bedrock API."""
    request_type = event['RequestType']
    
    kb_name = properties.get('Name', '')
    description = properties.get('Description', '')
    role_arn = properties.get('RoleArn', '')
    embedding_model_arn = properties.get('EmbeddingModelArn', '')
    index_arn = properties.get('IndexArn', '')  # Now expects IndexArn directly
    region = properties.get('Region', '')
    kms_key_arn = properties.get('KmsKeyArn', '')
    
    # Initialize Bedrock Agent client
    # Only Delete gets the bounded client: its own retries and deadline sit on
    # top of it. Create and Update have no retries of their own, so they keep
    # botocore's default retry budget.
    client_kwargs = {'config': KB_CLIENT_CONFIG} if request_type == 'Delete' else {}
    bedrock_agent_client = boto3.client('bedrock-agent', region_name=region, **client_kwargs)
    
    if request_type == 'Create':
        logger.info(f"Creating Knowledge Base with S3 Vectors: {kb_name}")
        return create_knowledge_base_s3_vectors(
            bedrock_agent_client, kb_name, description, role_arn, 
            embedding_model_arn, index_arn, kms_key_arn,
            client_token=knowledge_base_client_token(event)
        )
        
    elif request_type == 'Update':
        logger.info(f"Updating Knowledge Base: {kb_name}")
        # For updates, extract KB ID from PhysicalResourceId
        physical_resource_id = event.get('PhysicalResourceId', '')
        if physical_resource_id:
            kb_id = extract_knowledge_base_id_from_physical_id(physical_resource_id)
            if kb_id:
                return get_knowledge_base_info(bedrock_agent_client, kb_id)
        
        # Fallback: create new if we can't find existing
        return create_knowledge_base_s3_vectors(
            bedrock_agent_client, kb_name, description, role_arn, 
            embedding_model_arn, index_arn, kms_key_arn,
            client_token=knowledge_base_client_token(event)
        )
            
    elif request_type == 'Delete':
        logger.info(f"Deleting Knowledge Base: {kb_name}")
        physical_resource_id = event.get('PhysicalResourceId', '')
        if physical_resource_id:
            kb_id = extract_knowledge_base_id_from_physical_id(physical_resource_id)
            if kb_id:
                delete_knowledge_base(bedrock_agent_client, kb_id, context)
        return {'Status': 'Deleted'}


def extract_knowledge_base_id_from_physical_id(physical_id):
    """Extract Knowledge Base ID from PhysicalResourceId."""
    try:
        # PhysicalResourceId format: "bedrock-kb://kb-id" or just "kb-id"
        if physical_id.startswith('bedrock-kb://'):
            return physical_id.replace('bedrock-kb://', '')
        elif physical_id.startswith('bedrock-kb-failed://'):
            # Handle failed resource IDs - don't try to delete these
            return None
        else:
            # Assume it's a direct KB ID
            return physical_id
    except Exception as e:
        logger.warning(f"Could not extract KB ID from PhysicalResourceId {physical_id}: {e}")
        return None


def knowledge_base_client_token(event):
    """Derive CreateKnowledgeBase's idempotency token from the CloudFormation request.

    A retry of the same request (Lambda retrying the invocation, or the handler
    re-running after a timeout) carries the same StackId, LogicalResourceId and
    RequestId, so it sends the same token and Bedrock returns the knowledge base
    the first attempt created instead of creating a second one. A different
    request gets a different token. The service model requires 33 to 256
    characters matching ``[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}``; ``cfn-`` plus a
    64-character hex digest is 68 characters of that alphabet.
    """
    identity = '|'.join(
        str(event.get(key, '')) for key in ('StackId', 'LogicalResourceId', 'RequestId')
    )
    return f"cfn-{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def create_knowledge_base_s3_vectors(bedrock_agent_client, name, description, role_arn, 
                                   embedding_model_arn, index_arn, kms_key_arn=None,
                                   client_token=None):
    """Create Knowledge Base with S3 Vectors using Console-proven approach.

    ``client_token`` is the idempotency token (see ``knowledge_base_client_token``);
    the handler always passes one.
    """
    try:
        logger.info(f"Creating Knowledge Base: {name} with S3 Vectors")
        logger.info(f"Using index ARN: {index_arn}")
        
        # Use the working Console payload structure
        create_params = {}
        if client_token:
            create_params['clientToken'] = client_token
        response = bedrock_agent_client.create_knowledge_base(
            **create_params,
            name=name,
            description=description,
            roleArn=role_arn,
            knowledgeBaseConfiguration={
                'type': 'VECTOR',
                'vectorKnowledgeBaseConfiguration': {
                    'embeddingModelConfiguration': {
                        'bedrockEmbeddingModelConfiguration': {
                            'dimensions': 1024,  # All embedding models in picklist output 1024 
                            'embeddingDataType': 'FLOAT32'
                        }
                    },
                    'embeddingModelArn': embedding_model_arn
                }
            },
            storageConfiguration={
                'type': 'S3_VECTORS',
                's3VectorsConfiguration': {
                    'indexArn': index_arn  # Use indexArn approach (Console-proven)
                }
            }
        )
        
        kb_id = response['knowledgeBase']['knowledgeBaseId']
        logger.info(f"Created Knowledge Base with ID: {kb_id}")
        
        if kms_key_arn:
            logger.info(f"KMS encryption configured at bucket level with key: {kms_key_arn}")
        
        return {
            'KnowledgeBaseId': kb_id,
            'KnowledgeBaseName': name,
            'Status': 'Created'
        }
        
    except ClientError as e:
        logger.error(f"Error creating Knowledge Base: {e}")
        raise


def knowledge_base_status(bedrock_agent_client, kb_id, attempts=KB_STATUS_ATTEMPTS, context=None):
    """Return ``(status, failure_reasons)`` for a knowledge base, or None if it is gone.

    ResourceNotFoundException means the knowledge base does not exist, and so
    does ValidationException: the only parameter is the id, so an id the service
    rejects cannot name a knowledge base. Throttling, internal and transport
    errors are retried up to ``attempts`` times while the invocation has time
    (see ``has_time_for``), so a transient read error is not what decides whether
    a delete fails; the deletion wait polls on its own schedule and passes 1.
    The last such error, and any other error, is raised.
    """
    for attempt in range(1, attempts + 1):
        try:
            kb = bedrock_agent_client.get_knowledge_base(knowledgeBaseId=kb_id)['knowledgeBase']
        except ClientError as e:
            code = e.response['Error']['Code']
            if code in KB_ABSENT_CODES:
                return None
            if code not in KB_DELETE_RETRYABLE_CODES:
                raise
            error = e
        except TRANSPORT_ERRORS as e:
            error = e
        else:
            return kb['status'], kb.get('failureReasons') or []
        delay = KB_DELETE_BACKOFF_SECONDS * attempt
        if attempt == attempts or not has_time_for(context, delay):
            raise error
        logger.warning(f"GetKnowledgeBase for {kb_id} failed ({error}); retrying in {delay}s")
        time.sleep(delay)


def has_time_for(context, seconds):
    """True if sleeping ``seconds`` would still leave KB_DEADLINE_MARGIN_SECONDS of the invocation.

    The margin is what the call after the sleep, one GetKnowledgeBase check and
    the CloudFormation response can take. A context without
    ``get_remaining_time_in_millis`` has no deadline to keep.
    """
    remaining = getattr(context, 'get_remaining_time_in_millis', None)
    left_ms = remaining() if callable(remaining) else None
    if not isinstance(left_ms, (int, float)):
        return True
    return left_ms / 1000 - seconds >= KB_DEADLINE_MARGIN_SECONDS


def wait_for_knowledge_base_deletion(bedrock_agent_client, kb_id, context=None):
    """Poll GetKnowledgeBase a bounded time after DeleteKnowledgeBase was accepted.

    DeleteKnowledgeBase is asynchronous: it answers with status DELETING and the
    knowledge base disappears later. Returns when it is gone. Raises
    ``RuntimeError`` only on DELETE_UNSUCCESSFUL, the status in which the service
    reports that the deletion failed and the knowledge base still exists. If it
    is still DELETING when the wait runs out, or GetKnowledgeBase keeps erroring,
    the delete has been accepted and is in progress, so this returns rather than
    leaving the stack in DELETE_FAILED for a transient state.
    """
    for poll in range(1, KB_DELETE_WAIT_POLLS + 1):
        if not has_time_for(context, KB_DELETE_WAIT_SECONDS):
            logger.warning(f"Stopping the wait for Knowledge Base {kb_id} to keep within the function timeout")
            break
        time.sleep(KB_DELETE_WAIT_SECONDS)
        try:
            state = knowledge_base_status(bedrock_agent_client, kb_id, attempts=1)
        except (ClientError, *TRANSPORT_ERRORS) as e:
            logger.warning(f"Could not read Knowledge Base {kb_id} while it deletes (poll {poll}): {e}")
            continue
        if state is None:
            logger.info(f"Deleted Knowledge Base: {kb_id}")
            return
        status, failure_reasons = state
        if status == 'DELETE_UNSUCCESSFUL':
            raise RuntimeError(
                f"Deleting Knowledge Base {kb_id} failed (status DELETE_UNSUCCESSFUL: "
                f"{'; '.join(failure_reasons) or 'no reason given'}). The knowledge base "
                f"still exists; resolve the reason above, then retry the stack delete."
            )
        logger.info(f"Knowledge Base {kb_id} is {status} (poll {poll})")
    logger.warning(
        f"Knowledge Base {kb_id} was still being deleted when the wait ended; the "
        f"delete was accepted and continues in Bedrock"
    )


def delete_knowledge_base(bedrock_agent_client, kb_id, context=None):
    """Delete a Knowledge Base, treating one that is already gone as deleted.

    The data sources that belong to it are separate stack resources that depend
    on it, so CloudFormation has deleted them before this runs; DeleteKnowledgeBase
    does not require anything else to be deleted first.

    Conflict, throttling, internal and transport errors are retried with bounded
    backoff.
    Any other refusal is checked with GetKnowledgeBase: if the knowledge base is
    gone the delete succeeds, and if it still exists ``RuntimeError`` names it, so
    the stack delete reports FAILED instead of reporting SUCCESS and leaking it.
    A knowledge base already DELETING counts as deleted. After a successful
    call, waits a bounded time for the deletion to finish (see
    ``wait_for_knowledge_base_deletion``). Retries and the wait both stop early
    when the invocation is running out of time (see ``has_time_for``).
    """
    last_error = None
    for attempt in range(1, KB_DELETE_ATTEMPTS + 1):
        try:
            logger.info(f"Deleting Knowledge Base: {kb_id} (attempt {attempt})")
            response = bedrock_agent_client.delete_knowledge_base(knowledgeBaseId=kb_id)
        except ClientError as e:
            code = e.response['Error']['Code']
            if code == 'ResourceNotFoundException':
                logger.info(f"Knowledge Base {kb_id} not found (already deleted)")
                return
            if code not in KB_DELETE_RETRYABLE_CODES:
                state = knowledge_base_status(bedrock_agent_client, kb_id, context=context)
                if state is None:
                    logger.info(f"Knowledge Base {kb_id} does not exist ({code} on delete)")
                    return
                if state[0] == 'DELETING':
                    logger.warning(f"Knowledge Base {kb_id} is already being deleted ({code} on delete)")
                    return
                raise RuntimeError(
                    f"Knowledge Base {kb_id} still exists (status {state[0]}) and "
                    f"DeleteKnowledgeBase was refused: {e}"
                ) from e
            last_error = e
        except TRANSPORT_ERRORS as e:
            code = type(e).__name__
            last_error = e
        else:
            logger.info(f"Knowledge Base {kb_id} delete accepted (status {response.get('status')})")
            wait_for_knowledge_base_deletion(bedrock_agent_client, kb_id, context)
            return
        delay = min(KB_DELETE_BACKOFF_SECONDS * (2 ** (attempt - 1)), KB_DELETE_MAX_BACKOFF_SECONDS)
        if attempt == KB_DELETE_ATTEMPTS or not has_time_for(context, delay):
            break
        logger.warning(f"DeleteKnowledgeBase for {kb_id} failed ({code}); retrying in {delay}s")
        time.sleep(delay)
    state = knowledge_base_status(bedrock_agent_client, kb_id, context=context)
    if state is None:
        logger.info(f"Knowledge Base {kb_id} not found (already deleted)")
        return
    if state[0] == 'DELETING':
        logger.warning(f"Knowledge Base {kb_id} is already being deleted ({last_error})")
        return
    raise RuntimeError(
        f"Knowledge Base {kb_id} still exists (status {state[0]}) after "
        f"{attempt} delete attempts ({last_error}). Retry the stack delete "
        f"once it is no longer {state[0]}."
    )


def get_knowledge_base_info(bedrock_agent_client, kb_id):
    """Get information about existing Knowledge Base."""
    try:
        response = bedrock_agent_client.get_knowledge_base(knowledgeBaseId=kb_id)
        kb = response['knowledgeBase']
        
        return {
            'KnowledgeBaseId': kb['knowledgeBaseId'],
            'KnowledgeBaseName': kb['name'],
            'Status': 'Existing'
        }
        
    except ClientError as e:
        logger.error(f"Error getting Knowledge Base info: {e}")
        raise


def delete_vector_index(s3vectors_client, bucket_name, index_name, absent_codes=NOT_FOUND_CODES):
    """Delete a vector index from an S3 Vectors bucket.

    ``absent_codes`` are the error codes read as "the index does not exist". The
    stack Delete also passes ValidationException (an index whose name the
    service rejects cannot exist; if it did, the bucket delete that follows is
    refused and the handler reports FAILED). The Update rebuild keeps the
    default, so an unexpected refusal there fails the update instead of
    skipping the rebuild.
    """
    try:
        logger.info(f"Deleting vector index: {index_name} from bucket: {bucket_name}")
        
        s3vectors_client.delete_index(
            vectorBucketName=bucket_name,
            indexName=index_name
        )
        logger.info(f"Successfully deleted vector index: {index_name}")
        return True
        
    except ClientError as e:
        if e.response['Error']['Code'] in absent_codes:
            # Index doesn't exist, which is fine for delete operations
            logger.info(f"Index {index_name} not found (already deleted or never existed)")
            return False
        else:
            logger.error(f"Error deleting vector index {index_name}: {e}")
            raise


def create_vector_index(s3vectors_client, bucket_name, index_name, dimension=1024, distance_metric="cosine", metadata_configuration=None):
    """Create a vector index with configurable settings for Bedrock Knowledge Base integration."""
    try:
        logger.info(f"Creating vector index: {index_name} in bucket: {bucket_name}")
        logger.info(f"Index configuration - Dimension: {dimension}, Distance Metric: {distance_metric}, Metadata Config: {metadata_configuration}")
        
        # Default metadata configuration if none provided
        if metadata_configuration is None:
            metadata_configuration = {
                "nonFilterableMetadataKeys": ["AMAZON_BEDROCK_METADATA", "AMAZON_BEDROCK_TEXT"]
            }
        
        index_response = s3vectors_client.create_index(
            vectorBucketName=bucket_name,
            indexName=index_name,
            dataType="float32",
            dimension=int(dimension),
            distanceMetric=distance_metric,
            metadataConfiguration=metadata_configuration
        )
        logger.info(f"Successfully created vector index: {index_name}")
        return index_response
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConflictException':
            # Index already exists or was created by another process
            logger.info(f"Index {index_name} already exists or was created by another process")
            return None
        else:
            logger.error(f"Error creating vector index {index_name}: {e}")
            raise


def recreate_vector_index(s3vectors_client, bucket_name, index_name, index_config):
    """Delete and recreate a vector index to ensure fresh configuration."""
    try:
        logger.info(f"Recreating vector index: {index_name} in bucket: {bucket_name}")
        
        # Delete existing index if it exists
        delete_vector_index(s3vectors_client, bucket_name, index_name)
        
        # Create new index with configuration
        return create_vector_index(
            s3vectors_client, 
            bucket_name, 
            index_name,
            dimension=index_config['dimension'],
            distance_metric=index_config['distance_metric'],
            metadata_configuration=index_config['metadata_configuration']
        )
        
    except Exception as e:
        logger.error(f"Error recreating vector index {index_name}: {e}")
        raise


def create_s3_vector_resources(s3vectors_client, bucket_name, index_name, embedding_model, kms_key_arn=None, index_config=None):
    """Create S3 Vector bucket and index following Console approach."""
    try:
        # Get region from client for ARN construction
        region = s3vectors_client.meta.region_name
        
        # Create vector bucket with optional encryption
        logger.info(f"Creating vector bucket: {bucket_name}")
        
        create_bucket_params = {
            'vectorBucketName': bucket_name
        }
        
        # Add KMS encryption if provided
        if kms_key_arn:
            create_bucket_params['encryptionConfiguration'] = {
                'sseType': 'aws:kms',
                'kmsKeyArn': kms_key_arn
            }
            logger.info(f"Using KMS encryption for bucket with key: {kms_key_arn}")
        
        bucket_response = s3vectors_client.create_vector_bucket(**create_bucket_params)
        logger.info(f"Created vector bucket: {bucket_name}")
        
        # Create S3 Vector Index using modular function with configuration
        if index_config:
            create_vector_index(
                s3vectors_client, 
                bucket_name, 
                index_name,
                dimension=index_config['dimension'],
                distance_metric=index_config['distance_metric'],
                metadata_configuration=index_config['metadata_configuration']
            )
        else:
            create_vector_index(s3vectors_client, bucket_name, index_name)
        
        # Construct ARNs
        sts_client = boto3.client('sts', region_name=region)
        account_id = sts_client.get_caller_identity()['Account']
        partition = get_partition(region)

        bucket_arn = f"arn:{partition}:s3vectors:{region}:{account_id}:bucket/{bucket_name}"
        index_arn = f"arn:{partition}:s3vectors:{region}:{account_id}:bucket/{bucket_name}/index/{index_name}"
        
        logger.info(f"Vector bucket ARN: {bucket_arn}")
        logger.info(f"Vector index ARN: {index_arn}")
        
        # Validate bucket name one more time before returning
        if not is_valid_s3_bucket_name(bucket_name):
            raise ValueError(f"Sanitized bucket name is still invalid: {bucket_name}")
        
        return {
            'BucketName': bucket_name,        # Return sanitized name
            'BucketArn': bucket_arn,         # Return proper bucket ARN
            'IndexName': index_name,         # Index name
            'IndexArn': index_arn,           # Index ARN for Knowledge Base
            'Status': 'Created'
        }
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code in ['BucketAlreadyExists', 'ConflictException']:
            logger.warning(f"Vector resource already exists: {e}")
            # Try to get existing resource info (need to pass index_config here too)
            return get_s3_vector_info(s3vectors_client, bucket_name, index_name, index_config)
        else:
            raise


def is_valid_s3_bucket_name(bucket_name):
    """Validate that bucket name meets S3 requirements."""
    import re
    if not bucket_name or len(bucket_name) < 3 or len(bucket_name) > 63:
        return False
    if not re.match(r'^[a-z0-9][a-z0-9\-]*[a-z0-9]$', bucket_name):
        return False
    if '--' in bucket_name:  # No consecutive hyphens
        return False
    return True


def vector_bucket_exists(s3vectors_client, bucket_name):
    """Ask GetVectorBucket whether the bucket exists.

    NotFoundException means it does not, and so does ValidationException: the
    only parameter is the name, and GetVectorBucket has no not-empty condition
    for that code to stand for. Any other error is raised.
    """
    try:
        s3vectors_client.get_vector_bucket(vectorBucketName=bucket_name)
        return True
    except ClientError as e:
        if e.response['Error']['Code'] in ABSENT_ON_DELETE_CODES:
            return False
        raise


def delete_vector_bucket(s3vectors_client, bucket_name):
    """Delete a vector bucket, waiting a bounded time for it to become empty.

    Returns True when this call deleted the bucket and False when it was already
    gone. S3 Vectors refuses to delete a bucket that still holds an index and
    answers ``ConflictException``; that is retried with backoff, because the
    index this resource owns has just been deleted and the refusal can outlast
    the ``DeleteIndex`` call briefly. ``ValidationException`` is checked with
    GetVectorBucket: if the bucket does not exist it counts as gone, and if it
    does it is retried like a refusal, so neither guess about which code the
    service uses for a non-empty bucket can report a bucket gone while it
    exists. Every other error is raised at once.

    Raises ``RuntimeError`` when the bucket is still not empty after the last
    attempt. On a stack Delete the handler then reports FAILED, which leaves the
    stack in DELETE_FAILED; that is deliberate, because reporting SUCCESS here is what
    silently leaked the bucket and its index (issue #1589). The caller has
    already deleted every index ListIndexes returned, so the expected cause is
    an index created after that listing, or a listing that failed; the message
    says how to clear it.
    """
    last_error = None
    for attempt in range(1, BUCKET_DELETE_ATTEMPTS + 1):
        try:
            logger.info(f"Deleting vector bucket: {bucket_name} (attempt {attempt})")
            s3vectors_client.delete_vector_bucket(vectorBucketName=bucket_name)
            logger.info(f"Deleted vector bucket: {bucket_name}")
            return True
        except ClientError as e:
            code = e.response['Error']['Code']
            if code in NOT_FOUND_CODES:
                logger.info(f"Vector bucket {bucket_name} not found (already deleted)")
                return False
            if code in ABSENT_ON_DELETE_CODES and not vector_bucket_exists(
                s3vectors_client, bucket_name
            ):
                logger.info(
                    f"Vector bucket {bucket_name} does not exist ({code} on delete)"
                )
                return False
            if code not in ABSENT_ON_DELETE_CODES | BUCKET_NOT_EMPTY_CODES:
                raise
            last_error = e
            if attempt < BUCKET_DELETE_ATTEMPTS:
                delay = min(
                    BUCKET_DELETE_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                    BUCKET_DELETE_MAX_BACKOFF_SECONDS,
                )
                logger.warning(
                    f"Vector bucket {bucket_name} is not empty yet ({code}); "
                    f"retrying in {delay}s"
                )
                time.sleep(delay)
    raise RuntimeError(
        f"Vector bucket {bucket_name} still holds an index after "
        f"{BUCKET_DELETE_ATTEMPTS} delete attempts ({last_error}). Every index "
        f"the handler could list was deleted or not found, so the bucket most "
        f"likely holds an index created after the listing, or one whose deletion "
        f"has not yet taken effect, or the listing failed (see the log). Delete the "
        f"bucket's remaining indexes "
        f"(aws s3vectors list-indexes --vector-bucket-name {bucket_name}), then "
        f"retry the stack delete."
    )


def list_vector_index_names(s3vectors_client, bucket_name):
    """Return the name of every index in a vector bucket, following nextToken.

    Every error is raised; ``indexes_to_delete`` decides what a failed listing
    means. (A bucket that does not exist needs no branch of its own here: the
    fallback deletes the named index, which is then absent, and the bucket
    delete reads NotFoundException as already deleted.) Stops, with a warning, if the service repeats a
    token or returns more than INDEX_LIST_MAX_PAGES pages; the bucket delete
    that follows then fails if indexes remain, so a truncated listing cannot
    turn into a reported success.
    """
    names = []
    seen_tokens = set()
    next_token = None
    for _ in range(INDEX_LIST_MAX_PAGES):
        params = {'vectorBucketName': bucket_name, 'maxResults': INDEX_LIST_PAGE_SIZE}
        if next_token:
            params['nextToken'] = next_token
        page = s3vectors_client.list_indexes(**params)
        names.extend(index['indexName'] for index in page.get('indexes', []))
        next_token = page.get('nextToken')
        if not next_token:
            return names
        if next_token in seen_tokens:
            logger.warning(f"ListIndexes for {bucket_name} repeated a nextToken; stopping the listing")
            return names
        seen_tokens.add(next_token)
    logger.warning(
        f"ListIndexes for {bucket_name} returned more than {INDEX_LIST_MAX_PAGES} pages; "
        f"stopping the listing"
    )
    return names


def indexes_to_delete(s3vectors_client, bucket_name, index_name):
    """The indexes to delete before a vector bucket: every index it holds.

    ``index_name`` (the index named in the resource's properties) comes first
    and is always included, so a listing that has not yet caught up with a new
    index still deletes it. If ListIndexes fails with a service or transport
    error, the failure is logged and only ``index_name`` is returned: the bucket
    delete that follows is what decides the outcome, and it fails if the bucket
    still holds an index. Any other exception is a defect and is raised.
    """
    names = [index_name] if index_name else []
    try:
        listed = list_vector_index_names(s3vectors_client, bucket_name)
    except (ClientError, *TRANSPORT_ERRORS) as e:
        logger.warning(
            f"Could not list the indexes in vector bucket {bucket_name} ({e}); "
            f"deleting only {index_name or 'no index'}"
        )
        return names
    names.extend(name for name in listed if name not in names)
    return names


def seconds_left(context):
    """Seconds left in the invocation, or None if ``context`` has no deadline."""
    remaining = getattr(context, 'get_remaining_time_in_millis', None)
    left_ms = remaining() if callable(remaining) else None
    return left_ms / 1000 if isinstance(left_ms, (int, float)) else None


def delete_s3_vector_resources(s3vectors_client, bucket_name, index_name, strict=True, context=None):
    """Delete every index in the vector bucket, then the bucket.

    S3 Vectors will not delete a bucket that still holds an index, so the
    indexes go first: every index ListIndexes returns, plus ``index_name`` (see
    ``indexes_to_delete``). A bucket or index that is already gone counts as
    deleted.

    With ``strict`` (the stack Delete), any failure is raised, so the handler
    reports FAILED instead of SUCCESS while the bucket still exists. Without it
    (cleaning up the previous bucket during an Update that renamed it) a
    failure is logged and the update proceeds.

    The handler's role may list and delete every index only in the bucket the
    template names for this stack. For any other bucket the listing is denied,
    and only ``index_name`` is deleted (see ``indexes_to_delete``).

    The index deletes stop with ``RuntimeError`` while INDEX_DELETE_MARGIN_SECONDS
    of the invocation remain, so a bucket holding many indexes reports FAILED,
    and can be retried, rather than running past the function timeout.
    """
    if not bucket_name:
        return
    try:
        names = indexes_to_delete(s3vectors_client, bucket_name, index_name)
        for done, name in enumerate(names):
            left = seconds_left(context)
            if left is not None and left < INDEX_DELETE_MARGIN_SECONDS:
                raise RuntimeError(
                    f"Stopped deleting the indexes in vector bucket {bucket_name} "
                    f"to stay within the function timeout, after {done} of "
                    f"{len(names)}. Retry the stack delete to continue."
                )
            delete_vector_index(
                s3vectors_client, bucket_name, name, ABSENT_ON_DELETE_CODES
            )
        delete_vector_bucket(s3vectors_client, bucket_name)
    except Exception as e:
        if strict:
            raise
        logger.warning(
            f"Could not delete previous vector bucket {bucket_name} / index "
            f"{index_name} during update (continuing): {e}"
        )


def get_s3_vector_info(s3vectors_client, bucket_name, index_name, index_config=None):
    """Get information about existing S3 Vector bucket and ensure index exists."""
    try:
        # Get bucket info
        bucket_response = s3vectors_client.get_vector_bucket(vectorBucketName=bucket_name)
        bucket_arn = bucket_response.get('BucketArn')
        
        # Get region and account ID for ARN construction
        region = s3vectors_client.meta.region_name
        sts_client = boto3.client('sts', region_name=region)
        account_id = sts_client.get_caller_identity()['Account']
        
        # Construct bucket ARN if not returned in response
        partition = get_partition(region)
        if not bucket_arn:
            bucket_arn = f"arn:{partition}:s3vectors:{region}:{account_id}:bucket/{bucket_name}"

        logger.info(f"Found existing vector bucket ARN: {bucket_arn}")

        # Always attempt to create the index - if it exists, we'll get ConflictException
        # This is more robust than trying to check existence with potentially non-existent API methods
        logger.info(f"Ensuring vector index exists: {index_name}")
        if index_config:
            index_created = create_vector_index(
                s3vectors_client,
                bucket_name,
                index_name,
                dimension=index_config['dimension'],
                distance_metric=index_config['distance_metric'],
                metadata_configuration=index_config['metadata_configuration']
            )
        else:
            index_created = create_vector_index(s3vectors_client, bucket_name, index_name)

        # Construct index ARN (required for Knowledge Base configuration)
        index_arn = f"arn:{partition}:s3vectors:{region}:{account_id}:bucket/{bucket_name}/index/{index_name}"
        
        logger.info(f"Vector bucket ARN: {bucket_arn}")
        logger.info(f"Vector index ARN: {index_arn}")
        
        return {
            'BucketName': bucket_name,
            'BucketArn': bucket_arn,
            'IndexName': index_name,
            'IndexArn': index_arn,
            'Status': 'IndexCreated' if index_created is not None else 'Existing'
        }
        
    except ClientError as e:
        logger.error(f"Error getting S3 Vector bucket info: {e}")
        raise


def update_s3_vector_info(s3vectors_client, bucket_name, index_name, index_config, recreate_index=True):
    """Return the existing S3 Vector resources, recreating the index if asked.

    Recreating the index deletes every vector in it, so the handler asks for it
    only when the index settings changed (see ``index_config_differs``).
    Otherwise the index is created only if it is missing.
    """
    try:
        # Get bucket info
        bucket_response = s3vectors_client.get_vector_bucket(vectorBucketName=bucket_name)
        bucket_arn = bucket_response.get('BucketArn')
        
        # Get region and account ID for ARN construction
        region = s3vectors_client.meta.region_name
        sts_client = boto3.client('sts', region_name=region)
        account_id = sts_client.get_caller_identity()['Account']
        
        # Construct bucket ARN if not returned in response
        partition = get_partition(region)
        if not bucket_arn:
            bucket_arn = f"arn:{partition}:s3vectors:{region}:{account_id}:bucket/{bucket_name}"

        logger.info(f"Found existing vector bucket ARN: {bucket_arn}")

        if recreate_index:
            logger.info(f"Index settings changed; recreating vector index: {index_name}")
            recreate_vector_index(s3vectors_client, bucket_name, index_name, index_config)
        else:
            # Keep the index and its vectors. CreateIndex still runs, so an index
            # deleted outside the stack comes back; an existing one answers
            # ConflictException, which create_vector_index reads as "exists".
            logger.info(f"Index settings unchanged; keeping vector index and its vectors: {index_name}")
            create_vector_index(
                s3vectors_client,
                bucket_name,
                index_name,
                dimension=index_config['dimension'],
                distance_metric=index_config['distance_metric'],
                metadata_configuration=index_config['metadata_configuration']
            )

        # Construct index ARN (required for Knowledge Base configuration)
        index_arn = f"arn:{partition}:s3vectors:{region}:{account_id}:bucket/{bucket_name}/index/{index_name}"
        
        logger.info(f"Vector bucket ARN: {bucket_arn}")
        logger.info(f"Vector index ARN: {index_arn}")
        
        return {
            'BucketName': bucket_name,
            'BucketArn': bucket_arn,
            'IndexName': index_name,
            'IndexArn': index_arn,
            'Status': 'Updated' if recreate_index else 'Existing'
        }
        
    except ClientError as e:
        logger.error(f"Error updating S3 Vector bucket info: {e}")
        raise


def get_physical_resource_id(event, properties, response_data):
    """
    Generate appropriate PhysicalResourceId for successful operations.
    This is crucial for CloudFormation to properly track resource lifecycle.
    """
    request_type = event['RequestType']
    resource_type = properties.get('ResourceType', 'S3VectorBucketAndIndex')
    
    # For CREATE operations, generate new PhysicalResourceId
    if request_type == 'Create':
        if resource_type == 'S3VectorBucketAndIndex':
            bucket_name = response_data.get('BucketName', properties.get('BucketName', ''))
            index_name = response_data.get('IndexName', properties.get('IndexName', ''))
            return f"s3vectors://{bucket_name}/{index_name}"
            
        elif resource_type == 'S3VectorsKnowledgeBase':
            kb_id = response_data.get('KnowledgeBaseId', '')
            return f"bedrock-kb://{kb_id}"
            
    # For UPDATE and DELETE operations, preserve existing PhysicalResourceId
    elif request_type in ['Update', 'Delete']:
        existing_physical_id = event.get('PhysicalResourceId', '')
        if existing_physical_id:
            return existing_physical_id
    
    # Fallback: generate based on resource type and properties
    if resource_type == 'S3VectorBucketAndIndex':
        bucket_name = properties.get('BucketName', 'unknown-bucket')
        index_name = properties.get('IndexName', 'unknown-index')
        return f"s3vectors://{bucket_name}/{index_name}"
    elif resource_type == 'S3VectorsKnowledgeBase':
        kb_name = properties.get('Name', 'unknown-kb')
        return f"bedrock-kb://{kb_name}-{event.get('RequestId', 'unknown')[:8]}"
    
    # Ultimate fallback
    return f"custom-resource-{event.get('RequestId', 'unknown')[:8]}"


def get_physical_resource_id_for_failure(event, properties):
    """
    Generate appropriate PhysicalResourceId for failed operations.
    Ensures CloudFormation can still track the resource even on failure.
    """
    request_type = event['RequestType']
    resource_type = properties.get('ResourceType', 'S3VectorBucketAndIndex')
    
    # For UPDATE and DELETE failures, always preserve existing PhysicalResourceId
    if request_type in ['Update', 'Delete']:
        existing_physical_id = event.get('PhysicalResourceId', '')
        if existing_physical_id:
            return existing_physical_id
    
    # For CREATE failures, generate a reasonable PhysicalResourceId
    # This prevents CloudFormation from getting confused about resource identity
    if resource_type == 'S3VectorBucketAndIndex':
        bucket_name = properties.get('BucketName', 'failed-bucket')
        index_name = properties.get('IndexName', 'failed-index')
        return f"s3vectors-failed://{bucket_name}/{index_name}"
        
    elif resource_type == 'S3VectorsKnowledgeBase':
        kb_name = properties.get('Name', 'failed-kb')
        return f"bedrock-kb-failed://{kb_name}-{event.get('RequestId', 'unknown')[:8]}"
    
    # Ultimate fallback
    return f"failed-custom-resource-{event.get('RequestId', 'unknown')[:8]}"
