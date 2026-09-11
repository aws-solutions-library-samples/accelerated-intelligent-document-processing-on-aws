# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import logging
import os
import time
from typing import Any, Dict

import boto3
from boto3.dynamodb.conditions import Key as DDBKey
from idp_common.bda.bda_blueprint_service import (
    BdaBlueprintService,  # type: ignore[import-untyped]
)
from idp_common.config import ConfigurationManager
from idp_common.config_scope import scope_allows

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("idp_common.bedrock.client").setLevel(
    os.environ.get("BEDROCK_LOG_LEVEL", "INFO")
)


# ----- Caller-scope enforcement for multi-user deployments ---------------
# When a customer deploys this stack with multiple Cognito users and
# restricts authors via `allowedConfigVersions` stored in the
# UsersTable (EmailIndex), syncBdaIdp MUST NOT let those authors
# mutate BDA projects tied to versions outside their scope. This
# mirrors the same check now performed in configuration_resolver.
_dynamodb = boto3.resource("dynamodb")
_user_scope_cache: dict = {}
_USER_SCOPE_CACHE_TTL = 60  # seconds


def _get_caller_info(event: Dict[str, Any]) -> Dict[str, Any]:
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    username = claims.get("cognito:username") or claims.get("sub") or ""
    email = claims.get("email") or identity.get("username") or username
    return {
        "email": email,
        "username": username,
        "groups": groups,
        "is_admin": "Admin" in groups,
    }


def _get_user_allowed_config_versions(caller_email: str):
    """Look up the caller's `allowedConfigVersions` with a per-container TTL cache."""
    users_table_name = os.environ.get("USERS_TABLE_NAME", "")
    if not users_table_name or not caller_email:
        return None
    now = time.time()
    cached = _user_scope_cache.get(caller_email)
    if cached and (now - cached["timestamp"]) < _USER_SCOPE_CACHE_TTL:
        return cached["scope"]
    try:
        users_table = _dynamodb.Table(users_table_name)
        resp = users_table.query(
            IndexName="EmailIndex",
            KeyConditionExpression=DDBKey("email").eq(caller_email),
        )
        items = resp.get("Items", [])
        if items:
            scope = items[0].get("allowedConfigVersions")
            result = list(scope) if scope and len(scope) > 0 else None
        else:
            result = None
    except Exception as e:
        logger.warning(f"Failed to look up user scope for {caller_email}: {e}")
        result = None
    _user_scope_cache[caller_email] = {"scope": result, "timestamp": now}
    return result


def handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """
    Synchronous BDA/IDP sync resolver with bidirectional support.

    BDA project ARN resolution order:
    1. Explicit bdaProjectArn from UI arguments (user-provided)
    2. Version tracking table (previously linked project)
    3. Auto-create new project (for idp_to_bda direction)

    Supports four sync directions:
    - "bda_to_idp": Sync from BDA blueprints to IDP classes (read BDA, update IDP)
    - "idp_to_bda": Sync from IDP classes to BDA blueprints (read IDP, update BDA)
    - "bidirectional": Sync both directions (default for backward compatibility)
    - "cleanup_orphaned": Delete orphaned BDA blueprints not in current IDP config
    """
    try:
        logger.info("Starting BDA/IDP sync")
        # NOTE: do NOT log full event — it contains identity.claims which
        # carries Cognito PII. Log only the resolver-specific arguments.
        logger.info(
            "syncBdaIdp invoked by operation=%s",
            (event.get("info") or {}).get("fieldName", "unknown"),
        )

        # Get arguments
        arguments = event.get("arguments", {})
        sync_direction = arguments.get("direction", "bidirectional")
        sync_mode = arguments.get("syncMode", "replace")  # 'replace' or 'merge'
        versionName = arguments.get("versionName", "default")
        explicit_bda_arn = arguments.get("bdaProjectArn")  # Optional: user-provided ARN
        save_arn = arguments.get(
            "saveArn", True
        )  # Whether to save the ARN to version tracking

        # Defense-in-depth RBAC: syncBdaIdp is an Admin+Author operation. The
        # schema enforces this via @aws_cognito_user_pools(cognito_groups), but
        # we also gate it server-side so a Viewer can never reach it even if the
        # schema directive is missing/misconfigured.
        caller = _get_caller_info(event)
        if not ({"Admin", "Author"}.intersection(caller["groups"])):
            logger.warning(
                "Forbidden: caller %s (groups=%s) attempted syncBdaIdp",
                caller["email"],
                caller["groups"],
            )
            return {
                "success": False,
                "error": {
                    "type": "Unauthorized",
                    "message": "syncBdaIdp requires Admin or Author group",
                },
            }

        # RBAC: scope-enforce for non-admins. An Author with restricted
        # `allowedConfigVersions` must not be able to invoke syncBdaIdp
        # against a version (and its linked BDA project) outside their
        # scope. Admins are unrestricted.
        if not caller["is_admin"]:
            allowed_versions = _get_user_allowed_config_versions(caller["email"])
            if not scope_allows(allowed_versions, versionName):
                logger.warning(
                    "Rejecting syncBdaIdp: caller %s is scoped to %s but requested "
                    "versionName=%r",
                    caller["email"],
                    sorted(allowed_versions),
                    versionName,
                )
                return {
                    "success": False,
                    "error": {
                        "type": "Unauthorized",
                        "message": (
                            f"Access denied: version '{versionName}' is not in "
                            "your allowed scope"
                        ),
                    },
                    "processedClasses": [],
                    "direction": sync_direction,
                }

        logger.info(
            f"Sync direction: {sync_direction}, mode: {sync_mode}, "
            f"version: {versionName}, explicit ARN: {explicit_bda_arn}"
        )

        # Initialize ConfigurationManager for BDA project tracking
        config_table = os.environ.get("CONFIGURATION_TABLE_NAME")
        manager = (
            ConfigurationManager(table_name=config_table) if config_table else None
        )

        # Resolve BDA project ARN using priority chain
        bda_project_arn = None
        arn_source = None

        # Check for CREATE_NEW sentinel — user explicitly wants a new BDA project
        force_create_new = explicit_bda_arn == "CREATE_NEW"
        if force_create_new:
            explicit_bda_arn = None  # Clear sentinel so it's not used as an ARN
            logger.info(
                "User requested CREATE_NEW — will force-create a new BDA project"
            )

        # Priority 1: Explicit ARN from UI (skip if CREATE_NEW was requested)
        if explicit_bda_arn and not force_create_new:
            bda_project_arn = explicit_bda_arn
            arn_source = "user-provided"
            logger.info(f"Using user-provided BDA project ARN: {bda_project_arn}")

        # Priority 2: Version tracking table (skip if CREATE_NEW was requested)
        if not bda_project_arn and not force_create_new and manager:
            tracked_arn = manager.get_bda_project_arn(versionName)
            if tracked_arn:
                bda_project_arn = tracked_arn
                arn_source = "version-tracking"
                logger.info(
                    f"Using tracked BDA project ARN for version '{versionName}': {bda_project_arn}"
                )

        # Priority 3: Auto-create for idp_to_bda or bidirectional (or when CREATE_NEW forced)
        if not bda_project_arn and (
            force_create_new or sync_direction in ("idp_to_bda", "bidirectional")
        ):
            logger.info(
                f"No BDA project found, auto-creating for version '{versionName}'"
            )
            if manager:
                manager.set_bda_sync_status(versionName, "creating")
            try:
                bda_service = BdaBlueprintService()
                bda_project_arn = bda_service.get_or_create_project_for_version(
                    versionName
                )
                arn_source = "auto-created"
                logger.info(
                    f"Auto-created BDA project for version '{versionName}': {bda_project_arn}"
                )
            except Exception as e:
                logger.error(f"Failed to auto-create BDA project: {e}")
                if manager:
                    manager.set_bda_sync_status(versionName, "error")
                return {
                    "success": False,
                    "error": {
                        "type": "CONFIGURATION_ERROR",
                        "message": f"Failed to create BDA project for version '{versionName}': {str(e)}",
                    },
                    "processedClasses": [],
                    "direction": sync_direction,
                }

        # No ARN available for bda_to_idp — need user to provide one
        if not bda_project_arn:
            return {
                "success": False,
                "error": {
                    "type": "CONFIGURATION_ERROR",
                    "message": f"No BDA project linked to version '{versionName}'. "
                    f"Please provide a BDA Project ARN or sync to BDA first to create one.",
                },
                "processedClasses": [],
                "direction": sync_direction,
            }

        # Initialize BDA service with the resolved project ARN
        bda_service = BdaBlueprintService(dataAutomationProjectArn=bda_project_arn)
        bda_service.dataAutomationProjectArn = bda_project_arn

        # Handle cleanup_orphaned direction separately
        if sync_direction == "cleanup_orphaned":
            logger.info("Executing orphaned blueprint cleanup")
            cleanup_result = bda_service.cleanup_orphaned_blueprints(
                version=versionName
            )

            # Update tracking
            if manager and save_arn:
                manager.set_bda_project_arn(versionName, bda_project_arn, "synced")

            return {
                "success": cleanup_result.get("success", False),
                "message": cleanup_result.get("message", ""),
                "processedClasses": [],
                "direction": sync_direction,
                "bdaProjectArn": bda_project_arn,
                "bdaSyncStatus": "synced",
                "cleanupDetails": {
                    "deletedCount": cleanup_result.get("deleted_count", 0),
                    "failedCount": cleanup_result.get("failed_count", 0),
                    "details": cleanup_result.get("details", []),
                },
            }

        # Execute the sync operation with direction and mode parameters
        result = bda_service.create_blueprints_from_custom_configuration(
            sync_direction=sync_direction, version=versionName, sync_mode=sync_mode
        )

        logger.info(f"BDA Service results: {result}")

        # Extract processed class names and warnings for response
        sync_failed_classes = []
        sync_succeeded_classes = []
        all_warnings = []
        failure_reasons = []

        if isinstance(result, list):
            for item in result:
                if item.get("status") == "success":
                    sync_succeeded_classes.append(item.get("class"))
                    # Collect warnings (skipped properties) for this class
                    item_warnings = item.get("warnings", [])
                    all_warnings.extend(item_warnings)
                else:
                    class_name = item.get("class", "Unknown")
                    sync_failed_classes.append(class_name)
                    # Surface why, so the user isn't left with a bare failed
                    # count and a CloudWatch hunt for the actual API error.
                    reason = item.get("error")
                    if reason:
                        failure_reasons.append(f"{class_name}: {reason}")

        failure_detail = f" ({'; '.join(failure_reasons)})" if failure_reasons else ""

        logger.info(
            f"BDA/IDP sync completed. Direction: {sync_direction}, Succeeded: {len(sync_succeeded_classes)}, Failed: {len(sync_failed_classes)}"
        )

        # Update BDA project tracking in version table
        sync_status = "synced" if len(sync_failed_classes) == 0 else "partial"
        if manager and save_arn:
            try:
                manager.set_bda_project_arn(versionName, bda_project_arn, sync_status)
                logger.info(
                    f"Updated BDA tracking for version '{versionName}': {sync_status}"
                )
            except Exception as e:
                logger.warning(f"Failed to update BDA tracking: {e}")

        # Handle different scenarios
        if len(sync_succeeded_classes) == 0 and len(sync_failed_classes) > 0:
            # Complete failure
            if manager:
                manager.set_bda_sync_status(versionName, "error")
            return {
                "success": False,
                "message": f"Synchronization failed for all {len(sync_failed_classes)} document classes.{failure_detail}",
                "processedClasses": [],
                "direction": sync_direction,
                "bdaProjectArn": bda_project_arn,
                "bdaSyncStatus": "error",
                "error": {
                    # Reasons live on top-level `message`; keep error.message
                    # to a short shape so a UI rendering both fields doesn't
                    # duplicate the same failure-reasons text twice.
                    # Round-7 review fix.
                    "type": "SYNC_ERROR",
                    "message": f"Failed to sync classes: {', '.join(sync_failed_classes)}",
                },
            }
        elif len(sync_failed_classes) > 0:
            # Partial failure
            return {
                "success": True,  # Partial success
                "message": f"Successfully synchronized {len(sync_succeeded_classes)} document classes. Failed to sync {len(sync_failed_classes)} classes: {', '.join(sync_failed_classes)}{failure_detail}",
                "processedClasses": sync_succeeded_classes,
                "direction": sync_direction,
                "bdaProjectArn": bda_project_arn,
                "bdaSyncStatus": "partial",
                "error": {
                    "type": "PARTIAL_SYNC_ERROR",
                    "message": f"Failed to sync classes: {', '.join(sync_failed_classes)}",
                },
            }
        else:
            # Complete success
            direction_label = {
                "bda_to_idp": "from BDA to IDP",
                "idp_to_bda": "from IDP to BDA",
                "bidirectional": "bidirectionally",
            }.get(sync_direction, sync_direction)

            # Build message with warning info if any
            message = f"Successfully synchronized {len(sync_succeeded_classes)} document classes {direction_label}"
            if all_warnings:
                # Group warnings by class for cleaner reporting
                warnings_by_class = {}
                for w in all_warnings:
                    cls = w.get("class", "Unknown")
                    if cls not in warnings_by_class:
                        warnings_by_class[cls] = []
                    warnings_by_class[cls].append(w.get("property", "unknown"))

                warning_details = []
                for cls, props in warnings_by_class.items():
                    warning_details.append(f"{cls}: {', '.join(props)}")

                message += (
                    f". WARNING: Some properties were skipped due to a current BDA limitation - "
                    f"nested arrays and objects within schema definitions are not yet supported. "
                    f"To include these properties, flatten your schema by moving nested structures to top-level $defs. "
                    f"Skipped: {'; '.join(warning_details)}"
                )

            response = {
                "success": True,
                "message": message,
                "processedClasses": sync_succeeded_classes,
                "direction": sync_direction,
                "bdaProjectArn": bda_project_arn,
                "bdaSyncStatus": "synced",
            }

            # Add warnings array if any exist
            if all_warnings:
                response["warnings"] = all_warnings

            return response

    except Exception as e:
        logger.error(f"BDA/IDP sync failed: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": {
                "type": "SYNC_ERROR",
                "message": f"Sync operation failed: {str(e)}",
            },
            "processedClasses": [],
            "direction": arguments.get("direction", "bidirectional")
            if "arguments" in event
            else "bidirectional",
        }
