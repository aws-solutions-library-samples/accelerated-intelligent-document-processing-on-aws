# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import cfnresponse
import json
import logging
import os
import time
from botocore.exceptions import ClientError

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
    vector_dimension = properties.get('VectorDimension', 1024)
    distance_metric = properties.get('DistanceMetric', 'cosine')
    metadata_configuration = properties.get('MetadataConfiguration', {
        'nonFilterableMetadataKeys': ['AMAZON_BEDROCK_METADATA', 'AMAZON_BEDROCK_TEXT']
    })
    
    logger.info(f"Raw bucket name: {raw_bucket_name}, Sanitized bucket name: {bucket_name}")
    logger.info(f"Index properties - Dimension: {vector_dimension}, Metric: {distance_metric}, Metadata: {metadata_configuration}")
    
    # Initialize S3 Vectors client
    s3vectors_client = boto3.client('s3vectors', region_name=region)
    
    # Create index config for passing to functions
    index_config = {
        'dimension': vector_dimension,
        'distance_metric': distance_metric,
        'metadata_configuration': metadata_configuration
    }
    
    if request_type == 'Create':
        logger.info(f"Creating S3 Vector bucket: {bucket_name}")
        return create_s3_vector_resources(s3vectors_client, bucket_name, index_name, embedding_model, kms_key_arn, index_config)
        
    elif request_type == 'Update':
        logger.info(f"Updating S3 Vector bucket: {bucket_name}")
        old_properties = event.get('OldResourceProperties', {})
        old_raw_bucket_name = old_properties.get('BucketName', '')
        old_bucket_name = sanitize_bucket_name(old_raw_bucket_name)
        old_index_name = old_properties.get('IndexName', '')
        
        # If bucket or index name changed, delete old and create new
        if old_bucket_name != bucket_name or old_index_name != index_name:
            if old_bucket_name and old_index_name:
                delete_s3_vector_resources(
                    s3vectors_client, old_bucket_name, old_index_name, strict=False
                )
            return create_s3_vector_resources(s3vectors_client, bucket_name, index_name, embedding_model, kms_key_arn, index_config)
        else:
            # Names haven't changed - update existing resources (recreate index)
            return update_s3_vector_info(s3vectors_client, bucket_name, index_name, index_config)
            
    elif request_type == 'Delete':
        logger.info(f"Deleting S3 Vector bucket: {bucket_name}")
        delete_s3_vector_resources(s3vectors_client, bucket_name, index_name)
        return {'Status': 'Deleted'}


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
    bedrock_agent_client = boto3.client('bedrock-agent', region_name=region)
    
    if request_type == 'Create':
        logger.info(f"Creating Knowledge Base with S3 Vectors: {kb_name}")
        return create_knowledge_base_s3_vectors(
            bedrock_agent_client, kb_name, description, role_arn, 
            embedding_model_arn, index_arn, kms_key_arn
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
            embedding_model_arn, index_arn, kms_key_arn
        )
            
    elif request_type == 'Delete':
        logger.info(f"Deleting Knowledge Base: {kb_name}")
        physical_resource_id = event.get('PhysicalResourceId', '')
        if physical_resource_id:
            kb_id = extract_knowledge_base_id_from_physical_id(physical_resource_id)
            if kb_id:
                delete_knowledge_base(bedrock_agent_client, kb_id)
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


def create_knowledge_base_s3_vectors(bedrock_agent_client, name, description, role_arn, 
                                   embedding_model_arn, index_arn, kms_key_arn=None):
    """Create Knowledge Base with S3 Vectors using Console-proven approach."""
    try:
        logger.info(f"Creating Knowledge Base: {name} with S3 Vectors")
        logger.info(f"Using index ARN: {index_arn}")
        
        # Use the working Console payload structure
        import time
        
        response = bedrock_agent_client.create_knowledge_base(
            clientToken=f"cfn-{int(time.time())}-{'a' * 20}",  # 33+ chars required
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


def delete_knowledge_base(bedrock_agent_client, kb_id):
    """Delete Knowledge Base using Bedrock Agent API."""
    try:
        logger.info(f"Deleting Knowledge Base: {kb_id}")
        bedrock_agent_client.delete_knowledge_base(knowledgeBaseId=kb_id)
        logger.info(f"Deleted Knowledge Base: {kb_id}")
        
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            logger.warning(f"Error deleting Knowledge Base: {e}")


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
    silently leaked the bucket and its index (issue #1589). Once this
    resource's own index is gone, the expected cause is another index, which the
    handler's role is not permitted to list or delete, so the message says how to
    clear it.
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
        f"{BUCKET_DELETE_ATTEMPTS} delete attempts ({last_error}). The index this "
        f"stack created was deleted or not found, so the bucket most likely holds "
        f"another index: one this stack did not create, one left under an earlier "
        f"IndexName, or one whose deletion has not yet taken effect. Delete the bucket's remaining indexes "
        f"(aws s3vectors list-indexes --vector-bucket-name {bucket_name}), then "
        f"retry the stack delete."
    )


def delete_s3_vector_resources(s3vectors_client, bucket_name, index_name, strict=True):
    """Delete the vector index this resource created, then its vector bucket.

    S3 Vectors will not delete a bucket that still holds an index, so the index
    goes first. A bucket or index that is already gone counts as deleted.

    With ``strict`` (the stack Delete), any failure is raised, so the handler
    reports FAILED instead of SUCCESS while the bucket still exists. Without it
    (cleaning up the previous bucket and index during an Update that renamed
    them) a failure is logged and the update proceeds: the role's DeleteIndex
    grant names the index from the *current* template, so deleting an index
    under its old name is denied, and failing the update over that would block a
    rename that otherwise works.
    """
    if not bucket_name:
        return
    try:
        if index_name:
            delete_vector_index(
                s3vectors_client, bucket_name, index_name, ABSENT_ON_DELETE_CODES
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


def update_s3_vector_info(s3vectors_client, bucket_name, index_name, index_config):
    """Update existing S3 Vector resources by recreating the index."""
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

        # For updates, always recreate the index to ensure fresh configuration
        logger.info(f"Recreating vector index for update: {index_name}")
        recreate_vector_index(s3vectors_client, bucket_name, index_name, index_config)

        # Construct index ARN (required for Knowledge Base configuration)
        index_arn = f"arn:{partition}:s3vectors:{region}:{account_id}:bucket/{bucket_name}/index/{index_name}"
        
        logger.info(f"Vector bucket ARN: {bucket_arn}")
        logger.info(f"Vector index ARN: {index_arn}")
        
        return {
            'BucketName': bucket_name,
            'BucketArn': bucket_arn,
            'IndexName': index_name,
            'IndexArn': index_arn,
            'Status': 'Updated'
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
