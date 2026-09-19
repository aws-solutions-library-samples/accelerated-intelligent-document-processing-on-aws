# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lambda function to sync DynamoDB user changes to Cognito."""

import os
import json
import logging
import boto3
from log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

cognito = boto3.client("cognito-idp")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
REVIEWER_GROUP = os.environ.get("REVIEWER_GROUP", "Reviewer")


def handler(event, context):
    """Handle DynamoDB stream events for user table changes."""
    # Redacted before logging: the stream records are rows of the user table, so
    # each one carries the account attributes of a real person.
    logger.info(
        f"Received DynamoDB stream event: "
        f"{json.dumps(sanitize_event_for_logging(event), default=str)}"
    )

    for record in event.get("Records", []):
        try:
            process_record(record)
        except Exception as e:
            logger.error(f"Failed to process record {record.get('eventID', 'unknown')}: {e}")
            # Continue processing other records
            continue

    return {"statusCode": 200}


def process_record(record):
    """Process a single DynamoDB stream record."""
    event_name = record.get("eventName")
    
    if event_name == "INSERT":
        handle_user_created(record)
    elif event_name == "REMOVE":
        handle_user_deleted(record)
    elif event_name == "MODIFY":
        handle_user_modified(record)
    else:
        logger.info(f"Ignoring event type: {event_name}")


def handle_user_created(record):
    """Handle user creation in DynamoDB."""
    new_image = record.get("dynamodb", {}).get("NewImage", {})
    
    if not is_user_record(new_image):
        return
    
    user_id = get_attribute_value(new_image, "userId")
    email = get_attribute_value(new_image, "email")
    persona = get_attribute_value(new_image, "persona")
    
    logger.info(f"Creating user in Cognito: {email}")
    
    try:
        # Create user in Cognito
        cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:user_id", "Value": user_id}
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )

        # Add to appropriate group
        group_name = ADMIN_GROUP if persona.lower() == "admin" else REVIEWER_GROUP
        cognito.admin_add_user_to_group(
            UserPoolId=USER_POOL_ID, 
            Username=email, 
            GroupName=group_name
        )
        
        logger.info(f"User {email} created in Cognito and added to group {group_name}")
        
    except cognito.exceptions.UsernameExistsException:
        logger.warning(f"User {email} already exists in Cognito")
    except Exception as e:
        logger.error(f"Failed to create user {email} in Cognito: {e}")
        raise


def handle_user_deleted(record):
    """Handle user deletion in DynamoDB."""
    old_image = record.get("dynamodb", {}).get("OldImage", {})
    
    if not is_user_record(old_image):
        return
    
    email = get_attribute_value(old_image, "email")
    
    logger.info(f"Deleting user from Cognito: {email}")
    
    try:
        cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=email)
        logger.info(f"User {email} deleted from Cognito")
    except cognito.exceptions.UserNotFoundException:
        logger.warning(f"User {email} not found in Cognito during deletion")
    except Exception as e:
        logger.error(f"Failed to delete user {email} from Cognito: {e}")
        # Don't raise exception for deletion failures


def handle_user_modified(record):
    """Handle user modification in DynamoDB."""
    old_image = record.get("dynamodb", {}).get("OldImage", {})
    new_image = record.get("dynamodb", {}).get("NewImage", {})
    
    if not is_user_record(new_image):
        return
    
    email = get_attribute_value(new_image, "email")
    old_persona = get_attribute_value(old_image, "persona")
    new_persona = get_attribute_value(new_image, "persona")
    
    # Check if persona changed
    if old_persona != new_persona:
        logger.info(f"Updating user {email} persona from {old_persona} to {new_persona}")
        
        try:
            # Remove from old group
            old_group = ADMIN_GROUP if old_persona.lower() == "admin" else REVIEWER_GROUP
            try:
                cognito.admin_remove_user_from_group(
                    UserPoolId=USER_POOL_ID,
                    Username=email,
                    GroupName=old_group
                )
            except Exception as e:
                logger.warning(f"Failed to remove user {email} from group {old_group}: {e}")
            
            # Add to new group
            new_group = ADMIN_GROUP if new_persona.lower() == "admin" else REVIEWER_GROUP
            cognito.admin_add_user_to_group(
                UserPoolId=USER_POOL_ID,
                Username=email,
                GroupName=new_group
            )
            
            logger.info(f"User {email} moved from group {old_group} to {new_group}")
            
        except Exception as e:
            logger.error(f"Failed to update user {email} groups: {e}")
            raise


def is_user_record(image):
    """Check if the DynamoDB record is a user record."""
    pk = get_attribute_value(image, "PK")
    return pk and pk.startswith("USER#")


def get_attribute_value(image, attribute_name):
    """Extract attribute value from DynamoDB image."""
    attribute = image.get(attribute_name, {})
    
    # Handle different DynamoDB attribute types
    if "S" in attribute:
        return attribute["S"]
    elif "N" in attribute:
        return attribute["N"]
    elif "BOOL" in attribute:
        return attribute["BOOL"]
    elif "NULL" in attribute:
        return None
    
    return None
