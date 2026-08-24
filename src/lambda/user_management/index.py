# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lambda function for user management operations with DynamoDB storage and Cognito sync.

Supports five roles (Cognito groups): Admin, Author, Reviewer, Annotator, Viewer.
Users can optionally have allowedConfigVersions for config-version scoping.

Annotator is a least-privilege role for ground-truth annotation, scoped by
``allowedTestSets``. Because access is gated by the scoped Cognito session, a
shared annotation-queue link only deep-links and is useless on its own.

``allowedTestSets`` and ``allowedConfigVersions`` are independent axes — which
test sets a user may annotate versus which config versions' documents they may
see. A user can carry both.
"""

import logging
import os
import re
import uuid
from datetime import datetime

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")
cognito = boto3.client("cognito-idp")

USERS_TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
AUTHOR_GROUP = os.environ.get("AUTHOR_GROUP", "Author")
REVIEWER_GROUP = os.environ.get("REVIEWER_GROUP", "Reviewer")
ANNOTATOR_GROUP = os.environ.get("ANNOTATOR_GROUP", "Annotator")
VIEWER_GROUP = os.environ.get("VIEWER_GROUP", "Viewer")
ALLOWED_SIGNUP_EMAIL_DOMAINS = os.environ.get("ALLOWED_SIGNUP_EMAIL_DOMAINS", "")

# Valid personas map to Cognito group names
VALID_PERSONAS = {
    "Admin": ADMIN_GROUP,
    "Author": AUTHOR_GROUP,
    "Reviewer": REVIEWER_GROUP,
    "Annotator": ANNOTATOR_GROUP,
    "Viewer": VIEWER_GROUP,
}


def _get_caller_identity(event):
    """Extract caller's Cognito groups and email from AppSync event identity."""
    identity = event.get("identity", {})
    claims = identity.get("claims", {})
    groups = claims.get("cognito:groups", [])
    username = claims.get("cognito:username", "") or claims.get("sub", "")
    # Email: try claims.email first, then identity.username (AppSync sets this to Cognito username = email)
    email = claims.get("email", "") or identity.get("username", "") or username

    if isinstance(groups, str):
        groups = [groups]

    return {
        "groups": groups,
        "username": username,
        "email": email,
        "is_admin": "Admin" in groups,
    }


def handler(event, context):
    """Handle user management operations from AppSync."""
    logger.info(f"Received event: {event}")

    field = event.get("info", {}).get("fieldName", "")
    arguments = event.get("arguments", {})

    # Defense-in-depth authorization. The GraphQL schema restricts these
    # operations to the Admin group via @aws_cognito_user_pools(cognito_groups),
    # but the REST dispatcher's Cognito authorizer only authenticates — it does
    # not enforce the group — so we also enforce it server-side. listUsers is
    # included because it exposes every user's email + role; it must be Admin-only
    # (closes GAP-04, where any authenticated user — incl. Viewer/Reviewer — could
    # enumerate all users). getMyProfile stays open (a caller reads only itself).
    if field in ("createUser", "updateUser", "deleteUser", "listUsers"):
        caller = _get_caller_identity(event)
        if not caller["is_admin"]:
            logger.warning(
                f"Forbidden: caller {caller['email']} (groups={caller['groups']}) "
                f"attempted Admin-only operation '{field}'"
            )
            raise Exception("Unauthorized: Admin group membership required")

    if field == "createUser":
        return create_user(arguments)
    elif field == "updateUser":
        return update_user(arguments)
    elif field == "deleteUser":
        return delete_user(arguments)
    elif field == "listUsers":
        return list_users(event)
    elif field == "getMyProfile":
        return get_my_profile(event)

    raise ValueError(f"Unknown operation: {field}")


def create_user(args):
    """Create user in DynamoDB and sync to Cognito."""
    email = args["email"]
    persona = args["persona"]
    allowed_config_versions = args.get("allowedConfigVersions")
    allowed_test_sets = args.get("allowedTestSets")
    user_id = str(uuid.uuid4())

    # Validate email format
    email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    if not re.match(email_pattern, email):
        raise ValueError(f"Invalid email format: {email}")

    # Validate email domain if restrictions are configured
    if ALLOWED_SIGNUP_EMAIL_DOMAINS and ALLOWED_SIGNUP_EMAIL_DOMAINS.strip():
        allowed_domains = [
            d.strip().lower()
            for d in ALLOWED_SIGNUP_EMAIL_DOMAINS.split(",")
            if d.strip()
        ]
        if allowed_domains:  # Only validate if there are actual domains configured
            if "@" not in email:
                raise ValueError(f"Invalid email format: {email}")
            email_domain = email.split("@")[1].lower()
            if email_domain not in allowed_domains:
                raise ValueError(
                    f"Email domain '{email_domain}' is not allowed. "
                    f"Allowed domains: {', '.join(allowed_domains)}"
                )

    # Validate persona - support all five roles
    if persona not in VALID_PERSONAS:
        raise ValueError(
            f"Invalid persona: {persona}. Must be one of: {', '.join(VALID_PERSONAS.keys())}"
        )

    logger.info(f"Creating user with email {email} and persona {persona}")

    table = dynamodb.Table(USERS_TABLE_NAME)

    # Check if user already exists
    existing_users = table.query(
        IndexName="EmailIndex", KeyConditionExpression=Key("email").eq(email)
    )

    if existing_users.get("Items"):
        raise ValueError(f"User with email {email} already exists")

    # Create user record in DynamoDB
    user_record = {
        "PK": f"USER#{user_id}",
        "SK": f"USER#{user_id}",
        "userId": user_id,
        "email": email,
        "persona": persona,
        "status": "active",
        "createdAt": datetime.utcnow().isoformat() + "Z",
        "updatedAt": datetime.utcnow().isoformat() + "Z",
    }

    # Store allowedConfigVersions if provided
    if allowed_config_versions is not None:
        user_record["allowedConfigVersions"] = allowed_config_versions
    if allowed_test_sets is not None:
        user_record["allowedTestSets"] = allowed_test_sets

    table.put_item(Item=user_record)

    # Sync to Cognito
    try:
        sync_user_to_cognito(user_id, email, persona, "create")
    except Exception as e:
        logger.error(f"Failed to sync user to Cognito: {e}")
        # Rollback DynamoDB record
        table.delete_item(Key={"PK": f"USER#{user_id}", "SK": f"USER#{user_id}"})
        raise e

    logger.info(f"User {email} created successfully")
    return user_response_from_item(user_record)


def update_user(args):
    """Update a user's scope (config versions and/or test sets). Admin-only.

    Each axis is only touched when ``args`` mentions it, so updating one does not
    clear the other. Presence matters, not truthiness: an explicit ``null``/empty
    list means "remove the restriction", which a plain ``.get()`` could not
    distinguish from omitting the axis.
    """
    user_id = args["userId"]
    config_versions_given = "allowedConfigVersions" in args
    test_sets_given = "allowedTestSets" in args
    allowed_config_versions = args.get("allowedConfigVersions")
    allowed_test_sets = args.get("allowedTestSets")

    logger.info(
        f"Updating user {user_id} scope: configVersions={allowed_config_versions} "
        f"testSets={allowed_test_sets}"
    )

    table = dynamodb.Table(USERS_TABLE_NAME)

    # Get existing user record
    response = table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"USER#{user_id}"})
    if not response.get("Item"):
        raise ValueError(f"User {user_id} not found")

    user_record = response["Item"]

    # Admin is unscoped on every axis (config versions and test sets), so scoping
    # one would read as a restriction the authorizers do not enforce.
    if user_record.get("persona") == "Admin":
        raise ValueError("Cannot set access scope for Admin users")

    set_parts = ["updatedAt = :now"]
    remove_parts = []
    expr_values = {":now": datetime.utcnow().isoformat() + "Z"}

    if config_versions_given:
        if allowed_config_versions:
            set_parts.append("allowedConfigVersions = :acv")
            expr_values[":acv"] = allowed_config_versions
        else:
            # null or [] = remove the restriction (unrestricted access).
            remove_parts.append("allowedConfigVersions")

    if test_sets_given:
        if allowed_test_sets:
            set_parts.append("allowedTestSets = :ats")
            expr_values[":ats"] = allowed_test_sets
        else:
            remove_parts.append("allowedTestSets")

    update_expr = f"SET {', '.join(set_parts)}"
    if remove_parts:
        update_expr += f" REMOVE {', '.join(remove_parts)}"

    table.update_item(
        Key={"PK": f"USER#{user_id}", "SK": f"USER#{user_id}"},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_values,
    )

    # Return updated user
    updated = table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"USER#{user_id}"})
    return user_response_from_item(updated["Item"])


def delete_user(args):
    """Delete user from DynamoDB and sync to Cognito."""
    user_id = args["userId"]

    logger.info(f"Deleting user {user_id}")

    table = dynamodb.Table(USERS_TABLE_NAME)

    # Get user record
    response = table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"USER#{user_id}"})

    if not response.get("Item"):
        raise ValueError(f"User {user_id} not found")

    user_record = response["Item"]
    email = user_record["email"]

    # Delete from DynamoDB
    table.delete_item(Key={"PK": f"USER#{user_id}", "SK": f"USER#{user_id}"})

    # Sync to Cognito
    try:
        sync_user_to_cognito(user_id, email, user_record["persona"], "delete")
    except Exception as e:
        logger.warning(f"Failed to sync user deletion to Cognito: {e}")
        # Continue with deletion as DynamoDB is the source of truth

    logger.info(f"User {user_id} deleted successfully")
    return True


def get_my_profile(event):
    """Get the calling user's own profile including allowedConfigVersions."""
    caller = _get_caller_identity(event)
    caller_email = caller["email"]

    logger.info(f"Getting profile for caller: {caller_email}")

    table = dynamodb.Table(USERS_TABLE_NAME)

    # Look up by email using GSI
    response = table.query(
        IndexName="EmailIndex",
        KeyConditionExpression=Key("email").eq(caller_email),
    )

    items = response.get("Items", [])
    if not items:
        # User not in DynamoDB yet - return basic profile from Cognito claims
        logger.info(f"No DynamoDB record for {caller_email}, returning basic profile")
        return {
            "userId": caller["username"],
            "email": caller_email,
            "persona": _determine_persona_from_cognito_groups(caller["groups"]),
            "status": "active",
        }

    return user_response_from_item(items[0])


_SCOPE_ATTRIBUTES = ("allowedConfigVersions", "allowedTestSets")


def user_response_from_item(item):
    """Build the GraphQL ``User`` shape from a DynamoDB user item.

    Every read path goes through this so the scope axes cannot drift between them.
    Absent axes are omitted rather than returned empty.
    """
    result = {
        "userId": item["userId"],
        "email": item["email"],
        "persona": item["persona"],
        "status": item.get("status", "active"),
        "createdAt": format_datetime(item.get("createdAt")),
    }
    for attr in _SCOPE_ATTRIBUTES:
        if attr in item:
            result[attr] = item[attr]
    return result


def format_datetime(dt_str):
    """Ensure datetime string is valid ISO 8601 with Z suffix for AppSync."""
    if not dt_str:
        return None
    # Remove any existing timezone offset (+00:00) and trailing Z
    dt_str = dt_str.replace("+00:00", "").rstrip("Z")
    return dt_str + "Z"


def _determine_persona_from_groups(groups):
    """Determine persona from Cognito groups response, using highest precedence."""
    group_names = [g["GroupName"] for g in groups]
    if ADMIN_GROUP in group_names:
        return "Admin"
    if AUTHOR_GROUP in group_names:
        return "Author"
    if REVIEWER_GROUP in group_names:
        return "Reviewer"
    # Annotator outranks Viewer: it may annotate its allowed test sets, where
    # Viewer is read-only. Reversing the order hides the annotation queue.
    if ANNOTATOR_GROUP in group_names:
        return "Annotator"
    if VIEWER_GROUP in group_names:
        return "Viewer"
    return "Viewer"


def _determine_persona_from_cognito_groups(group_list):
    """Determine persona from a list of Cognito group name strings."""
    if "Admin" in group_list:
        return "Admin"
    if "Author" in group_list:
        return "Author"
    if "Reviewer" in group_list:
        return "Reviewer"
    if "Annotator" in group_list:
        return "Annotator"
    if "Viewer" in group_list:
        return "Viewer"
    return "Viewer"


def list_users(event):
    """List users. Admin sees all users; non-admin sees only their own profile."""
    caller = _get_caller_identity(event)

    # Non-admin users can only see their own profile
    if not caller["is_admin"]:
        logger.info(f"Non-admin caller {caller['email']}, returning self only")
        profile = get_my_profile(event)
        return {"users": [profile] if profile else []}

    logger.info("Admin listing all users")

    # First, sync Cognito users to DynamoDB
    sync_cognito_users_to_dynamodb()

    table = dynamodb.Table(USERS_TABLE_NAME)

    # Scan for all user records. Must paginate: DynamoDB applies the 1MB page
    # size to the items EXAMINED, not the items matching FilterExpression, so a
    # single call silently truncates the user list once the table outgrows one
    # page — the admin sees fewer users than exist, with no error.
    scan_kwargs = {
        "FilterExpression": "begins_with(PK, :pk_prefix)",
        "ExpressionAttributeValues": {":pk_prefix": "USER#"},
    }
    items = []
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    users = [user_response_from_item(item) for item in items]

    # Sort by creation date (newest first)
    users.sort(key=lambda x: x.get("createdAt") or "", reverse=True)

    logger.info(f"Found {len(users)} users")
    return {"users": users}


def sync_cognito_users_to_dynamodb():
    """Sync existing Cognito users to DynamoDB table."""
    logger.info("Syncing Cognito users to DynamoDB")

    table = dynamodb.Table(USERS_TABLE_NAME)

    # Get existing emails in DynamoDB for quick lookup. Must paginate: the 1MB
    # page size bounds the items EXAMINED, not the items matching
    # FilterExpression, so a single call yields an INCOMPLETE email set once the
    # table outgrows one page — and every user missing from it gets re-created
    # below under a fresh uuid4, duplicating the record. (The Cognito paginator
    # further down pages Cognito, not this scan.)
    existing_scan_kwargs = {
        "FilterExpression": "begins_with(PK, :pk_prefix)",
        "ExpressionAttributeValues": {":pk_prefix": "USER#"},
        "ProjectionExpression": "email",
    }
    existing_emails = set()
    while True:
        existing_response = table.scan(**existing_scan_kwargs)
        existing_emails.update(
            item["email"] for item in existing_response.get("Items", [])
        )
        last_key = existing_response.get("LastEvaluatedKey")
        if not last_key:
            break
        existing_scan_kwargs["ExclusiveStartKey"] = last_key

    # List all Cognito users
    paginator = cognito.get_paginator("list_users")

    for page in paginator.paginate(UserPoolId=USER_POOL_ID):
        for user in page.get("Users", []):
            username = user["Username"]

            # Get email from attributes
            email = username
            for attr in user.get("Attributes", []):
                if attr["Name"] == "email":
                    email = attr["Value"]
                    break

            # Skip if already in DynamoDB
            if email in existing_emails:
                continue

            # Get user's groups to determine persona
            try:
                groups_response = cognito.admin_list_groups_for_user(
                    Username=username, UserPoolId=USER_POOL_ID
                )
                persona = _determine_persona_from_groups(
                    groups_response.get("Groups", [])
                )
            except Exception as e:
                logger.warning(f"Could not get groups for user {username}: {e}")
                persona = "Viewer"

            # Create user record in DynamoDB
            user_id = str(uuid.uuid4())
            if user.get("UserCreateDate"):
                # Convert to UTC and format without timezone offset
                dt = user["UserCreateDate"]
                created_at = dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
            else:
                created_at = datetime.utcnow().isoformat() + "Z"

            user_record = {
                "PK": f"USER#{user_id}",
                "SK": f"USER#{user_id}",
                "userId": user_id,
                "email": email,
                "persona": persona,
                "status": "active",
                "createdAt": created_at,
                "updatedAt": datetime.utcnow().isoformat() + "Z",
            }

            table.put_item(Item=user_record)
            logger.info(f"Synced Cognito user {email} to DynamoDB")


def sync_user_to_cognito(user_id, email, persona, operation):
    """Sync user operations to Cognito."""
    if operation == "create":
        # Create user in Cognito
        cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )

        # Add to appropriate Cognito group based on persona
        group_name = VALID_PERSONAS.get(persona)
        if group_name:
            cognito.admin_add_user_to_group(
                UserPoolId=USER_POOL_ID, Username=email, GroupName=group_name
            )
            logger.info(
                f"User {email} synced to Cognito and added to group {group_name}"
            )
        else:
            logger.warning(
                f"Unknown persona '{persona}' - user created without group assignment"
            )

    elif operation == "delete":
        # Delete user from Cognito
        try:
            cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=email)
            logger.info(f"User {email} deleted from Cognito")
        except cognito.exceptions.UserNotFoundException:
            logger.warning(f"User {email} not found in Cognito during deletion")
