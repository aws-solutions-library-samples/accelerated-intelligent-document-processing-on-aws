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

This module is also the **writer** for the ``sub`` join every scope consumer reads.
A scope row is found from a verified token either on the immutable Cognito ``sub``,
via a ``SUB#<sub>`` pointer item in this table, or on the ``email`` claim via
``EmailIndex``. Email alone is not a safe join — an address can diverge from the
row it should match, and "no row" deliberately means *unrestricted* — so every
write path here records the ``sub`` and its pointer, and ``listUsers``' Cognito
sync back-fills rows that predate them. The canonical statement of the rule and of
both key spaces is ``idp_common.config_scope``; the constants below restate it
because this function ships with no ``idp_common`` layer (it vendors
``log_sanitizer`` for the same reason).
"""

import logging
import os
import re
import uuid
from datetime import datetime

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from log_sanitizer import sanitize_event_for_logging

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

# The two key spaces a caller's scope row is found on, and the claims each is read
# from. Restated from ``idp_common.config_scope`` — see the module docstring — and
# held to it by ``scripts/tests/test_scope_lookup_fail_closed.py``.
USERS_TABLE_SCOPE_INDEX = "EmailIndex"
USERS_TABLE_SCOPE_KEY = "email"
USERS_TABLE_SUB_POINTER_PREFIX = "SUB#"
USERS_TABLE_USER_KEY_PREFIX = "USER#"
USERS_TABLE_SUB_ATTRIBUTE = "cognitoSub"
SCOPE_KEY_CLAIM = "email"
SCOPE_SUB_CLAIM = "sub"

# Valid personas map to Cognito group names
VALID_PERSONAS = {
    "Admin": ADMIN_GROUP,
    "Author": AUTHOR_GROUP,
    "Reviewer": REVIEWER_GROUP,
    "Annotator": ANNOTATOR_GROUP,
    "Viewer": VIEWER_GROUP,
}


def _get_caller_identity(event):
    """The caller's Cognito groups and both row-lookup keys, from verified claims.

    ``get_my_profile`` finds the caller's own row on the immutable Cognito ``sub``
    where the row records one, and on the ``email`` claim otherwise. The two are
    **not** a fallback chain over one key: each identifier goes only to the key
    space that indexes it — ``sub`` to a ``SUB#<sub>`` pointer item, ``email`` to
    the ``EmailIndex`` GSI — and neither is ever substituted for the other.

    ⚠️ **Neither key falls back to another claim.** Putting a
    ``cognito:username``, a ``sub`` or the adapter's ``identity.username`` to an
    email-keyed index does not find the row by another route: it either matches
    nothing, or — where the substituted value happens to be some *other* account's
    address — matches the wrong row. ``username`` keeps its own fallback chain
    because it is not a lookup key; it is the display id.

    The same rules are stated canonically as ``caller_email_from_claims`` and
    ``caller_sub_from_claims`` in ``idp_common.config_scope``. They are restated
    here because this function has no ``idp_common`` layer, and
    ``scripts/tests/test_scope_lookup_fail_closed.py`` covers this file so the two
    cannot drift.
    """
    identity = event.get("identity", {})
    claims = identity.get("claims", {})
    groups = claims.get("cognito:groups", [])
    username = claims.get("cognito:username", "") or claims.get("sub", "")
    email = str(claims.get(SCOPE_KEY_CLAIM) or "").strip()
    caller_sub = str(claims.get(SCOPE_SUB_CLAIM) or "").strip()

    if isinstance(groups, str):
        groups = [groups]

    return {
        "groups": groups,
        "username": username,
        "email": email,
        "sub": caller_sub,
        "is_admin": "Admin" in groups,
    }


def _sub_pointer_key(caller_sub):
    """The UsersTable key of the pointer item for one Cognito ``sub``.

    One function so every writer and reader here builds the same key. A pointer
    that disagrees with its readers by one character is a silent "no row", and
    "no row" means unrestricted.
    """
    key = f"{USERS_TABLE_SUB_POINTER_PREFIX}{caller_sub}"
    return {"PK": key, "SK": key}


def _user_row_key(user_id):
    """The UsersTable key of the row holding one user's scope."""
    key = f"{USERS_TABLE_USER_KEY_PREFIX}{user_id}"
    return {"PK": key, "SK": key}


def _is_scoped(row):
    """Whether a user row carries a config-version restriction."""
    raw = (row or {}).get("allowedConfigVersions")
    if not raw:
        return False
    if isinstance(raw, str):
        return bool(raw.strip())
    return any(str(entry).strip() for entry in raw)


def _record_cognito_sub(table, user_id, caller_sub, *, scoped, delete_stale_pointer=True):
    """Record a user's Cognito ``sub`` on their row, and maintain its pointer.

    ⚠️ **A pointer exists only for a row that carries a restriction**, which is what
    ``scoped`` decides. That is the invariant every reader depends on, and it is not
    an optimisation:

    * A row with no ``allowedConfigVersions`` resolves to "unrestricted" through
      either key, so a pointer for it changes no authorization answer.
    * A pointer is read **before** the email join, so it decides the answer. Pointing
      one at an unrestricted row therefore *pins* "unrestricted" ahead of any row the
      email join would have found — and this function's own caller can create such a
      row: the Cognito sync writes a fresh unscoped row whenever a Cognito address
      matches none it knows, which is what happens to a user whose address diverged
      from their (scoped) row. Before this rule, that duplicate got the pointer, and
      correcting the address no longer restored the scope. With it, resolving through
      a pointer can only ever *tighten*.

    ⚠️ The pointer item carries **no** ``email`` attribute, and must not gain one.
    A DynamoDB GSI indexes only items that have its hash key, so an item without
    ``email`` is absent from ``EmailIndex`` altogether — which is what keeps
    pointers out of an email query's result page. Give a pointer an ``email`` and a
    ``Limit=1`` email query could return the pointer instead of the row; the
    pointer holds no ``allowedConfigVersions``, so the scope would silently lift.

    The ``sub`` itself is recorded on the row unconditionally. It is not a lookup key
    on its own — it is how the next sync recognises this row as this Cognito
    account's whatever the address now says — so recording it is always safe.

    Failures are logged, not raised. The pointer is an *additional* route to a row
    that ``EmailIndex`` still finds, so a user whose pointer could not be written
    keeps working exactly as they did before pointers existed. Raising would fail
    the create or the list that triggered it, which is the worse trade.
    """
    if not caller_sub or not user_id:
        return False
    now = datetime.utcnow().isoformat() + "Z"
    try:
        # `attribute_exists(PK)` because `update_item` UPSERTS. The user_id can come
        # from a scan that is seconds old, so a concurrent deleteUser is enough to
        # make this mint a partial row — no `email`, no `userId` — which then raises
        # KeyError in both the sync's scan loop and `list_users`, and those are the
        # only route into User Management *and* the only thing that runs this
        # back-fill. One such item would brick the page for every Admin.
        table.update_item(
            Key=_user_row_key(user_id),
            UpdateExpression="SET #sub = :sub, updatedAt = :now",
            ExpressionAttributeNames={"#sub": USERS_TABLE_SUB_ATTRIBUTE},
            ExpressionAttributeValues={":sub": caller_sub, ":now": now},
            ConditionExpression="attribute_exists(PK)",
        )
        if scoped:
            pointer = _sub_pointer_key(caller_sub)
            pointer["userId"] = user_id
            pointer[USERS_TABLE_SUB_ATTRIBUTE] = caller_sub
            pointer["updatedAt"] = now
            table.put_item(Item=pointer)
        elif delete_stale_pointer:
            # Unrestricted now, so no pointer may survive: a scope removed through
            # updateUser must not leave one behind pointing at the row it used to
            # restrict. Callers that know none can exist yet pass
            # ``delete_stale_pointer=False`` rather than paying for the call.
            table.delete_item(Key=_sub_pointer_key(caller_sub))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Could not record the Cognito %s for user %s; their scope stays "
            "resolvable by %s alone: %s",
            SCOPE_SUB_CLAIM,
            user_id,
            SCOPE_KEY_CLAIM,
            e,
        )
        return False
    return True


def _sub_from_cognito_attributes(attributes):
    """The ``sub`` from a Cognito ``UserAttributes``/``Attributes`` list."""
    for attr in attributes or []:
        if attr.get("Name") == SCOPE_SUB_CLAIM:
            return str(attr.get("Value") or "").strip()
    return ""


def handler(event, context):
    """Handle user management operations from AppSync."""
    # Redacted before logging: this is the user-administration API, so the event
    # carries the caller's `identity.claims` alongside the arguments naming the
    # account being operated on.
    logger.info(f"Received event: {sanitize_event_for_logging(event)}")

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
        **_user_row_key(user_id),
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
        caller_sub = sync_user_to_cognito(user_id, email, persona, "create")
    except Exception as e:
        logger.error(f"Failed to sync user to Cognito: {e}")
        # Rollback DynamoDB record
        table.delete_item(Key=_user_row_key(user_id))
        raise e

    # Record the sub Cognito just assigned, so this row is found on the immutable
    # identifier from the first sign-in rather than only on an address that can
    # later diverge from it. Done after the account exists because the sub does not
    # exist until then; a failure here is logged and leaves the email join intact.
    # The pointer follows only if this user is actually restricted — see
    # _record_cognito_sub for why that is the invariant and not a saving.
    if caller_sub and _record_cognito_sub(
        table, user_id, caller_sub, scoped=_is_scoped(user_record)
    ):
        user_record[USERS_TABLE_SUB_ATTRIBUTE] = caller_sub

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
    response = table.get_item(Key=_user_row_key(user_id))
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

    # `attribute_exists(PK)` because `update_item` UPSERTS, and the existence check
    # above is a separate read: a concurrent `deleteUser` landing between the two is
    # enough for this to *recreate* the row with only the attributes named here — no
    # `email`, no `userId`. The Admin sees an error either way, but a phantom row
    # persists, and both `list_users` and the Cognito sync's scan loop index those two
    # keys directly. Those are the only route into User Management *and* the only thing
    # that runs the sub back-fill, so one such item bricks the page for every Admin.
    try:
        table.update_item(
            Key=_user_row_key(user_id),
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
            ConditionExpression="attribute_exists(PK)",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        # Deleted between the read above and this write. Same answer the read gives.
        raise ValueError(f"User {user_id} not found") from e

    # Return updated user
    updated = table.get_item(Key=_user_row_key(user_id))
    updated_item = updated["Item"]

    # Bring the sub pointer into line with what the row now says. Setting a scope on
    # a row that records a sub gives it a pointer, so the restriction survives the
    # address changing; removing the scope removes the pointer, because a pointer at
    # an unrestricted row would pin "unrestricted" ahead of the email join. A row
    # that records no sub yet gets neither — the Cognito sync records it on the next
    # Admin `listUsers`, which is how an administrator reached this operation.
    recorded_sub = str(updated_item.get(USERS_TABLE_SUB_ATTRIBUTE) or "").strip()
    if recorded_sub:
        _record_cognito_sub(
            table, user_id, recorded_sub, scoped=_is_scoped(updated_item)
        )

    return user_response_from_item(updated_item)


def delete_user(args):
    """Delete user from DynamoDB and sync to Cognito."""
    user_id = args["userId"]

    logger.info(f"Deleting user {user_id}")

    table = dynamodb.Table(USERS_TABLE_NAME)

    # Get user record
    response = table.get_item(Key=_user_row_key(user_id))

    if not response.get("Item"):
        raise ValueError(f"User {user_id} not found")

    user_record = response["Item"]
    email = user_record["email"]

    # Delete from DynamoDB
    table.delete_item(Key=_user_row_key(user_id))

    # And the sub pointer, so a recreated Cognito account with the same address
    # cannot be handed the deleted row's userId. A pointer left behind names a row
    # that no longer exists, which the lookup already treats as "not found" and
    # logs — so this is tidiness rather than a control, and it warns rather than
    # failing a delete that has already happened.
    recorded_sub = str(user_record.get(USERS_TABLE_SUB_ATTRIBUTE) or "").strip()
    if recorded_sub:
        try:
            table.delete_item(Key=_sub_pointer_key(recorded_sub))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not delete the sub pointer for {user_id}: {e}")

    # Sync to Cognito
    try:
        sync_user_to_cognito(user_id, email, user_record["persona"], "delete")
    except Exception as e:
        logger.warning(f"Failed to sync user deletion to Cognito: {e}")
        # Continue with deletion as DynamoDB is the source of truth

    logger.info(f"User {user_id} deleted successfully")
    return True


def _basic_profile_from_claims(caller):
    """The profile of a caller with no stored row: Cognito groups, and no scope.

    Carries neither ``allowedConfigVersions`` nor ``allowedTestSets``, which is the
    conservative answer in both directions — it grants nothing, and it does not
    claim the caller is unrestricted on any axis.
    """
    return {
        "userId": caller["username"],
        "email": caller["email"],
        "persona": _determine_persona_from_cognito_groups(caller["groups"]),
        "status": "active",
    }


def _row_for_caller(table, caller):
    """The caller's own row, found on their Cognito ``sub`` first and email second.

    The ``sub`` route is preferred because it survives an address that has diverged
    from the row — a self-service email change, an external IdP re-applying its
    attribute mapping, a case difference. The email route is what finds a row
    written before pointers existed, and is the only route for a caller whose
    claims carry no ``sub``.

    Returns None when neither key finds a row, which the caller answers from the
    verified Cognito claims instead.

    ⚠️ The ``sub`` leg cannot return an unrestricted row — a pointer resolving one is
    treated as stale and the email join is tried instead. Every other reader of the
    pointer enforces the same invariant: ``_row_by_sub`` in ``idp_common.config_scope``
    (which both vendored ``config_scope`` copies inherit byte-for-byte), the
    Chat-with-Document processor and its vendored twin, and the pii-anonymizer feature
    API. That is what makes resolving through a pointer only ever *tighten*, and it is
    a property of the **deployment**, so one reader that disagreed would make it untrue
    everywhere rather than in one module. It matters less here than there — this decides
    what the UI displays, not what the server permits — and it is held as a class by
    rule SCOPE6 in ``scripts/tests/test_scope_lookup_fail_closed.py``, not only by the
    per-reader tests, because four per-instance tests are what let one reader spell it
    differently and one omit it entirely.
    """
    caller_sub = caller.get("sub") or ""
    if caller_sub:
        pointer = table.get_item(Key=_sub_pointer_key(caller_sub)).get("Item")
        user_id = str((pointer or {}).get("userId") or "").strip()
        if user_id:
            row = table.get_item(Key=_user_row_key(user_id)).get("Item")
            if row and _is_scoped(row):
                return row
            if row:
                logger.warning(
                    "A UsersTable %s pointer names a row carrying no "
                    "allowedConfigVersions; treating it as stale",
                    USERS_TABLE_SUB_POINTER_PREFIX,
                )

    caller_email = caller.get("email") or ""
    if not caller_email:
        return None
    response = table.query(
        IndexName=USERS_TABLE_SCOPE_INDEX,
        KeyConditionExpression=Key(USERS_TABLE_SCOPE_KEY).eq(caller_email),
    )
    items = response.get("Items", [])
    return items[0] if items else None


def get_my_profile(event):
    """Get the calling user's own profile including allowedConfigVersions."""
    caller = _get_caller_identity(event)

    if not caller.get("email") and not caller.get("sub"):
        # Neither lookup key is present, so there is no way to find this caller's
        # row. Answer from the verified Cognito groups instead of putting a
        # substituted identifier to an email-keyed index, where it matches nothing
        # at best and another account's row at worst. See _get_caller_identity.
        logger.warning(
            "No email or sub claim on the caller identity; returning a claims-only "
            "profile"
        )
        return _basic_profile_from_claims(caller)

    table = dynamodb.Table(USERS_TABLE_NAME)
    row = _row_for_caller(table, caller)
    if not row:
        # User not in DynamoDB yet - return basic profile from Cognito claims
        logger.info("No DynamoDB record for the caller, returning basic profile")
        return _basic_profile_from_claims(caller)

    return user_response_from_item(row)


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
    """Sync existing Cognito users to DynamoDB, and back-fill their ``sub`` pointers.

    This is the back-fill for the ``sub`` join. It runs on every Admin ``listUsers``,
    which is the workflow an administrator has to go through to *set* a
    config-version scope in the first place — so by the time a scope can be applied
    to a row that predates pointers, this has already given that row one. A row it
    has not reached yet stays resolvable by ``email`` exactly as before, so nothing
    regresses while the back-fill is outstanding.
    """
    logger.info("Syncing Cognito users to DynamoDB")

    table = dynamodb.Table(USERS_TABLE_NAME)

    # Get existing rows in DynamoDB for quick lookup. Must paginate: the 1MB
    # page size bounds the items EXAMINED, not the items matching
    # FilterExpression, so a single call yields an INCOMPLETE email set once the
    # table outgrows one page — and every user missing from it gets re-created
    # below under a fresh uuid4, duplicating the record. (The Cognito paginator
    # further down pages Cognito, not this scan.)
    #
    # ``userId``, the recorded sub and the scope are projected as well as ``email``
    # because the back-fill needs to know which row a Cognito account belongs to,
    # which rows have no sub recorded yet, and which of them carry a restriction (the
    # condition for writing a pointer at all — see ``_record_cognito_sub``).
    existing_scan_kwargs = {
        "FilterExpression": "begins_with(PK, :pk_prefix)",
        "ExpressionAttributeValues": {":pk_prefix": USERS_TABLE_USER_KEY_PREFIX},
        "ProjectionExpression": "#e, userId, #sub, #acv",
        "ExpressionAttributeNames": {
            "#e": USERS_TABLE_SCOPE_KEY,
            "#sub": USERS_TABLE_SUB_ATTRIBUTE,
            "#acv": "allowedConfigVersions",
        },
    }
    # Three indexes, because matching a Cognito account to its row on the address
    # ALONE is what duplicates a row whose address has changed — and a duplicate
    # carries no scope, so the user silently loses their restriction. In priority
    # order: the recorded ``sub``, which is definitive and survives any rename; the
    # exact address; then the case-folded address, which closes the commonest cause
    # of divergence on its own, since mail systems are case-insensitive and DynamoDB
    # is not.
    rows_by_sub = {}
    rows_by_email = {}
    rows_by_folded_email = {}
    while True:
        existing_response = table.scan(**existing_scan_kwargs)
        for item in existing_response.get("Items", []):
            row_email = item[USERS_TABLE_SCOPE_KEY]
            rows_by_email[row_email] = item
            folded = row_email.casefold()
            if folded in rows_by_folded_email:
                # Two rows whose addresses differ only in case. The case-insensitive
                # match below would resolve them by scan order and then bind an
                # immutable `sub` and its pointer to whichever won, invisibly. The pool
                # is created with `CaseSensitive: false`, so this needs rows written
                # outside user management, but it must not be silent.
                logger.warning(
                    "Two user rows have addresses differing only in case; a Cognito "
                    "account matching neither exactly will be bound to one of them by "
                    "scan order. Reconcile the duplicate rows."
                )
            rows_by_folded_email.setdefault(folded, item)
            recorded = str(item.get(USERS_TABLE_SUB_ATTRIBUTE) or "").strip()
            if recorded:
                rows_by_sub[recorded] = item
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
            caller_sub = _sub_from_cognito_attributes(user.get("Attributes"))

            # Which row is this Cognito account's? The sub is definitive; failing
            # that the exact address, then the case-folded one.
            existing = None
            if caller_sub:
                existing = rows_by_sub.get(caller_sub)
            if existing is None:
                existing = rows_by_email.get(email)
            if existing is None:
                existing = rows_by_folded_email.get(email.casefold())
                if existing is not None:
                    logger.info(
                        "Matched a Cognito account to an existing user row on a "
                        "case-insensitive address; not creating a duplicate"
                    )

            if existing is not None:
                recorded = str(existing.get(USERS_TABLE_SUB_ATTRIBUTE) or "").strip()
                scoped = _is_scoped(existing)
                if caller_sub and (recorded != caller_sub or scoped):
                    # A row can only record one sub. Where it already recorded a
                    # different one, that pointer is superseded and is **deleted**: a
                    # native and a federated Cognito account can share one address and
                    # both be live, so the previous sub may well still authenticate,
                    # and a pointer it still resolves would keep naming this row after
                    # the row stops recording it. Leaving it behind is what puts a
                    # pointer out of step with the row it names.
                    if recorded and recorded != caller_sub:
                        logger.warning(
                            "A user row's recorded Cognito %s changed; replacing its "
                            "pointer. If two live accounts share this address, only "
                            "the one this row now records resolves through the %s key "
                            "space.",
                            SCOPE_SUB_CLAIM,
                            USERS_TABLE_SUB_POINTER_PREFIX,
                        )
                        try:
                            table.delete_item(Key=_sub_pointer_key(recorded))
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                "Could not delete a superseded sub pointer: %s", e
                            )
                    # Skip the write entirely when there is nothing to change: the sub
                    # already matches and the row is unscoped, so neither the attribute
                    # nor the (absent) pointer would move. The first `listUsers` after
                    # an upgrade would otherwise cost two writes per row.
                    if recorded != caller_sub or scoped:
                        # Update the snapshot, not just the table. `existing` came from
                        # a scan taken before this loop and is shared by every account
                        # that matches this row — two live accounts sharing one address
                        # is exactly the case above — so leaving it stale makes the
                        # outcome depend on the order Cognito listed them in: a later
                        # pass would read the pre-loop `cognitoSub`, believe it was
                        # superseded, and re-create the pointer it had just deleted.
                        existing[USERS_TABLE_SUB_ATTRIBUTE] = caller_sub
                        rows_by_sub[caller_sub] = existing
                        _record_cognito_sub(
                            table,
                            existing.get("userId"),
                            caller_sub,
                            scoped=scoped,
                            # A pointer cannot exist yet for a row that records no sub,
                            # so there is nothing to delete — which matters on the first
                            # run after an upgrade, when that is every row.
                            delete_stale_pointer=bool(recorded),
                        )
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
                **_user_row_key(user_id),
                "userId": user_id,
                "email": email,
                "persona": persona,
                "status": "active",
                "createdAt": created_at,
                "updatedAt": datetime.utcnow().isoformat() + "Z",
            }
            if caller_sub:
                user_record[USERS_TABLE_SUB_ATTRIBUTE] = caller_sub

            # No pointer: the sub is recorded on the row above, but a row created
            # here carries no ``allowedConfigVersions``, and a pointer at an
            # unrestricted row would pin "unrestricted" ahead of the email join.
            # That matters because this is also the branch a user whose address
            # changed beyond recognition lands in — creating a row rather than
            # updating theirs. `updateUser` writes the pointer when a scope is
            # actually applied. See `_record_cognito_sub`.
            table.put_item(Item=user_record)
            logger.info(f"Synced Cognito user {email} to DynamoDB")


def sync_user_to_cognito(user_id, email, persona, operation):
    """Sync user operations to Cognito.

    Returns the Cognito ``sub`` for a create, so ``create_user`` can record the
    immutable identifier the scope lookup prefers. Cognito assigns it, so this is
    the only moment it becomes knowable; an empty string means the response did
    not carry one, and the row then stays resolvable by email alone.
    """
    if operation == "create":
        # Create user in Cognito
        created = cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        caller_sub = _sub_from_cognito_attributes(
            (created or {}).get("User", {}).get("Attributes")
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
        return caller_sub

    elif operation == "delete":
        # Delete user from Cognito
        try:
            cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=email)
            logger.info(f"User {email} deleted from Cognito")
        except cognito.exceptions.UserNotFoundException:
            logger.warning(f"User {email} not found in Cognito during deletion")
    return ""
