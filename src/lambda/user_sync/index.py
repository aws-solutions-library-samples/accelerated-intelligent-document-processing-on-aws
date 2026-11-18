# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lambda function to sync DynamoDB user changes to Cognito."""

import os
import json
import logging
import boto3
from idp_common.config_scope import USERS_TABLE_SCOPE_KEY, USERS_TABLE_SUB_ATTRIBUTE
from idp_common.user_scope import cognito_username_for_row
from log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

cognito = boto3.client("cognito-idp")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
AUTHOR_GROUP = os.environ.get("AUTHOR_GROUP", "Author")
REVIEWER_GROUP = os.environ.get("REVIEWER_GROUP", "Reviewer")
ANNOTATOR_GROUP = os.environ.get("ANNOTATOR_GROUP", "Annotator")
VIEWER_GROUP = os.environ.get("VIEWER_GROUP", "Viewer")

#: Persona (case-insensitive) -> the Cognito group it grants. Every persona maps
#: to its own group; an unknown persona grants none (never a fallback group).
PERSONA_GROUPS = {
    "admin": ADMIN_GROUP,
    "author": AUTHOR_GROUP,
    "reviewer": REVIEWER_GROUP,
    "annotator": ANNOTATOR_GROUP,
    "viewer": VIEWER_GROUP,
}


def group_for_persona(persona):
    """The Cognito group for ``persona``, or ``None`` (logged) when unknown."""
    group = PERSONA_GROUPS.get(str(persona or "").strip().lower())
    if group is None:
        logger.warning(f"Unknown persona {persona!r}: no Cognito group assigned")
    return group


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
    # A record written FROM an existing Cognito account (User Management's sync, or
    # assigning HITL work to a user added in Cognito directly) has nothing to create:
    # with a username other than the email, creating one would add a second account
    # and email it an invitation.
    if get_attribute_value(new_image, "source") == "cognito":
        logger.info("Skipping a user record created from an existing Cognito account")
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

        # Add to the persona's group
        group_name = group_for_persona(persona)
        if group_name:
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
    """Handle user deletion in DynamoDB.

    Deletes the Cognito account the removed record belonged to, resolved the same
    way User Management's ``deleteUser`` resolves it: the account with the ``sub``
    the record names, or, for a record that names none, the only account with the
    record's address. A record that matches several accounts deletes none.
    """
    old_image = record.get("dynamodb", {}).get("OldImage", {})

    if not is_user_record(old_image):
        return

    user_id = get_attribute_value(old_image, "userId")
    row = {
        USERS_TABLE_SCOPE_KEY: get_attribute_value(old_image, USERS_TABLE_SCOPE_KEY),
        USERS_TABLE_SUB_ATTRIBUTE: get_attribute_value(
            old_image, USERS_TABLE_SUB_ATTRIBUTE
        ),
    }

    try:
        username = cognito_username_for_row(cognito, USER_POOL_ID, row)
    except ValueError as e:
        logger.error(f"Not deleting a Cognito account for user {user_id}: {e}")
        return
    except Exception as e:
        logger.error(f"Could not look up the Cognito account of user {user_id}: {e}")
        return

    if username is None:
        logger.info(f"No Cognito account belongs to user {user_id}; nothing to delete")
        return

    logger.info(f"Deleting the Cognito account of user {user_id}")

    try:
        cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
        logger.info(f"Deleted the Cognito account of user {user_id}")
    except cognito.exceptions.UserNotFoundException:
        logger.warning(f"The Cognito account of user {user_id} was already gone")
    except Exception as e:
        logger.error(f"Failed to delete the Cognito account of user {user_id}: {e}")
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
            old_group = group_for_persona(old_persona)
            if old_group:
                try:
                    cognito.admin_remove_user_from_group(
                        UserPoolId=USER_POOL_ID,
                        Username=email,
                        GroupName=old_group
                    )
                except Exception as e:
                    logger.warning(f"Failed to remove user {email} from group {old_group}: {e}")
            
            # Add to new group
            new_group = group_for_persona(new_persona)
            if new_group:
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
