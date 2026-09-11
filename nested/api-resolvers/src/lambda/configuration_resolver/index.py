# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
import re
import time

import boto3
from boto3.dynamodb.conditions import Key as DDBKey
from pydantic import ValidationError

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import (
    CONFIG_TYPE_CONFIG,
    CONFIG_TYPE_DEFAULT_MODEL_CONFIG_LIMITS,
    CONFIG_TYPE_DEFAULT_PRICING,
    CONFIG_TYPE_SCHEMA,
    DEFAULT_VERSION,
    RESERVED_VERSION_NAMES,
)
from idp_common.config.models import IDPConfig, ModelConfigLimitsConfig, PricingConfig
from idp_common.config_scope import scope_allows
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("idp_common.bedrock.client").setLevel(
    os.environ.get("BEDROCK_LOG_LEVEL", "INFO")
)

# DynamoDB resource for user scope lookups
_dynamodb = boto3.resource("dynamodb")

# User scope cache (TTL-based, per Lambda container)
_user_scope_cache = {}
_USER_SCOPE_CACHE_TTL = 60  # seconds

# Lazy-initialized RuleTranslator (reused across requests within same container)
_rule_translator = None


def _get_caller_info(event):
    """Extract caller's email and groups from AppSync event identity."""
    identity = event.get("identity", {})
    claims = identity.get("claims", {})
    groups = claims.get("cognito:groups", [])
    username = claims.get("cognito:username", "") or claims.get("sub", "")
    email = claims.get("email", "") or identity.get("username", "") or username
    if isinstance(groups, str):
        groups = [groups]
    return {
        "email": email,
        "username": username,
        "groups": groups,
        "is_admin": "Admin" in groups,
        "is_author": "Author" in groups,
    }


# Defense-in-depth RBAC. The GraphQL schema restricts these fields via
# @aws_cognito_user_pools(cognito_groups), but we enforce the required group
# server-side so a non-privileged caller can never reach a write operation even
# if the schema directive is missing/misconfigured. Maps each operation to the
# Cognito groups permitted to invoke it.
_OPERATION_REQUIRED_GROUPS = {
    # Admin-only writes
    "deleteConfigVersion": {"Admin"},
    # Deleting a revision destroys history, so it stays Admin-only — the same
    # boundary deleteDocumentVersion draws.
    "deleteConfigProfileRevision": {"Admin"},
    "updatePricing": {"Admin"},
    "restoreDefaultPricing": {"Admin"},
    "updateModelConfigLimits": {"Admin"},
    "restoreDefaultModelConfigLimits": {"Admin"},
    # Admin + Author writes
    "updateConfiguration": {"Admin", "Author"},
    "setActiveVersion": {"Admin", "Author"},
    "generateRuleJson": {"Admin", "Author"},
    # Revisions are CONTENT, not access-control objects: an Author scoped to a
    # profile may move its content freely. Only minting a new profile (a new
    # RBAC object, via saveAsVersion) stays Admin-only.
    "restoreConfigProfileRevision": {"Admin", "Author"},
    "labelConfigProfileRevision": {"Admin", "Author"},
    # Admin + Author + Viewer reads (everything except Reviewer)
    "getConfigVersions": {"Admin", "Author", "Viewer"},
    # Annotator included so the annotate view can populate its class dropdown, and
    # ONLY for that: the response is reduced to the class vocabulary for a caller who
    # is not otherwise entitled to the configuration. See _class_vocabulary_only.
    "getConfigVersion": {"Admin", "Author", "Viewer", "Annotator"},
    # Not Annotator: revision history serves no annotator flow, and unlike
    # getConfigVersion there is no reduced payload for it to receive.
    "listConfigProfileRevisions": {"Admin", "Author", "Viewer"},
    "getConfigProfileRevision": {"Admin", "Author", "Viewer"},
    "getPricing": {"Admin", "Author", "Viewer"},
    "getModelConfigLimits": {"Admin", "Author", "Viewer"},
    "listConfigurationLibrary": {"Admin", "Author", "Viewer"},
    "getConfigurationLibraryFile": {"Admin", "Author", "Viewer"},
}


# Groups entitled to the configuration in full.
_FULL_CONFIG_GROUPS = {"Admin", "Author", "Viewer"}

# The only keys the class dropdown reads: a class names itself with `$id` or
# `x-aws-idp-document-type` (post-migration) or `name` (pre-migration), and may carry
# a description. Mirrors getConfigClassOptions in
# src/ui/src/components/common/config-class-options.ts.
_CLASS_VOCABULARY_KEYS = ("$id", "x-aws-idp-document-type", "name", "description")


def _class_vocabulary_only(config):
    """Reduce a configuration payload to just its class names and descriptions.

    An Annotator needs the class list to correct a misclassified section — the write
    side of that, ``reextractTestSetDocument``, has always accepted them. Without the
    list the dropdown had nothing to offer and the UI fell back to a free-text box,
    handing the role least able to know the vocabulary an unconstrained field; a class
    no config defines yields a section with no schema and so no extracted fields.

    Granting Annotator the whole configuration would have exposed prompts, model ids
    and every other setting to the lowest-privilege role, and moving the class list
    onto ``getAnnotationQueue`` would have needed a config-table grant for a Lambda
    that has none. Reducing the response here needs neither.

    Anything not recognised is dropped rather than passed through, so a future config
    key cannot leak by default.
    """
    classes = config.get("classes") if isinstance(config, dict) else None
    if not isinstance(classes, list):
        return {}
    reduced = []
    for cls in classes:
        if not isinstance(cls, dict):
            continue
        keep = {k: cls[k] for k in _CLASS_VOCABULARY_KEYS if k in cls}
        if keep:
            reduced.append(keep)
    return {"classes": reduced}


def _enforce_operation_group(operation, caller):
    """Raise if the caller is not in a group permitted to run this operation."""
    required = _OPERATION_REQUIRED_GROUPS.get(operation)
    if required is None:
        return
    if not required.intersection(caller["groups"]):
        logger.warning(
            f"Forbidden: caller {caller['email']} (groups={caller['groups']}) "
            f"attempted operation '{operation}' requiring one of {sorted(required)}"
        )
        raise Exception(
            f"Unauthorized: operation '{operation}' requires membership in one of "
            f"{sorted(required)}"
        )


def _get_user_allowed_config_versions(caller_email):
    """Look up user's allowedConfigVersions from UsersTable with caching."""
    users_table_name = os.environ.get("USERS_TABLE_NAME", "")
    if not users_table_name:
        return None

    now = time.time()
    cached = _user_scope_cache.get(caller_email)
    if cached and (now - cached["timestamp"]) < _USER_SCOPE_CACHE_TTL:
        return cached["scope"]

    try:
        users_table = _dynamodb.Table(users_table_name)
        response = users_table.query(
            IndexName="EmailIndex",
            KeyConditionExpression=DDBKey("email").eq(caller_email),
        )
        items = response.get("Items", [])
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


def validate_version_name(name):
    """Validate version name: alphanumeric, hyphens, underscores, periods, max 50 chars.

    Periods are allowed so semver-style names work — notably the config
    versions feature presets create (e.g. `sample-health-insurance-review-v0.1.6`,
    from apply_feature_config_preset). Periods are safe in the DynamoDB
    `Config#<name>` key and never participate in dot-path splitting (which only
    applies to config *field* paths, not version names).
    """
    if not name or not isinstance(name, str):
        return False
    # Reserved names collide with sentinel records in the configuration table
    # (e.g. the active-profile pointer), so a user must not be able to claim one.
    if name in RESERVED_VERSION_NAMES:
        return False
    return re.match(r"^[a-zA-Z0-9._-]+$", name) and len(name) <= 50


def validate_description(description):
    """Validate description: max 200 chars only"""
    if description is None or description == "":
        return True  # Optional field
    if not isinstance(description, str):
        return False
    return len(description) <= 200


def handler(event, context):
    """
    AWS Lambda handler for GraphQL operations related to configuration.

    Returns structured responses with success/error information:

    Success response:
    {
        "success": true,
        "Schema": {...},
        "Default": {...},
        "Custom": {...}
    }

    Error response:
    {
        "success": false,
        "error": {
            "type": "ValidationError" | "JSONDecodeError",
            "message": "...",
            "validationErrors": [...]  // if ValidationError
        }
    }
    """
    logger.info(f"Event received: {json.dumps(sanitize_event_for_logging(event))}")

    # Extract the GraphQL operation type
    operation = event["info"]["fieldName"]

    # Initialize ConfigurationManager
    manager = ConfigurationManager()

    # Get caller info for scope enforcement
    caller = _get_caller_info(event)
    # Defense-in-depth: enforce group membership server-side before any work.
    _enforce_operation_group(operation, caller)
    allowed_versions = None
    if not caller["is_admin"]:
        allowed_versions = _get_user_allowed_config_versions(caller["email"])
        logger.info(
            f"Config scope for {caller['email']}: {allowed_versions or 'unrestricted'}"
        )

    try:
        if operation == "getConfigVersions":
            return handle_get_config_versions(manager, allowed_versions)
        elif operation == "getConfigVersion":
            version_name = event["arguments"].get("versionName")
            # Enforce scope on getConfigVersion
            if not scope_allows(allowed_versions, version_name):
                return {
                    "success": False,
                    "error": {
                        "type": "Unauthorized",
                        "message": f"Access denied: version '{version_name}' is not in your allowed scope",
                    },
                }
            config_result = handle_get_configuration(manager, version_name)
            # Reduced for a caller whose only entitlement is annotation. Applied here
            # rather than inside handle_get_configuration so the full payload has one
            # producer and the narrowing is visible at the authorization boundary.
            if not _FULL_CONFIG_GROUPS.intersection(caller["groups"]):
                logger.info(
                    "Reducing getConfigVersion to the class vocabulary for "
                    f"{caller['email']} (groups={caller['groups']})"
                )
                return {
                    "success": config_result.get("success", True),
                    "Schema": {},
                    "Default": _class_vocabulary_only(config_result.get("Default")),
                    "Custom": _class_vocabulary_only(config_result.get("Custom")),
                }
            return config_result
        elif operation == "updateConfiguration":
            args = event["arguments"]
            version = args.get("versionName")
            custom_config = args.get("customConfig")
            description = args.get("description")
            if not version:
                return {
                    "success": False,
                    "error": {
                        "type": "ValidationError",
                        "message": "versionId is required",
                    },
                }
            # Validate version name if provided
            if not validate_version_name(version):
                return {
                    "success": False,
                    "error": {
                        "type": "ValidationError",
                        "message": "Version name can only contain letters, numbers, hyphens, and underscores (max 50 characters)",
                    },
                }
            # Validate description if provided
            if description and not validate_description(description):
                return {
                    "success": False,
                    "error": {
                        "type": "ValidationError",
                        "message": "Description cannot exceed 200 characters",
                    },
                }
            # RBAC: scope-enforce non-admin writes. If the caller has
            # a restricted `allowedConfigVersions` scope, they MUST NOT
            # be able to update any version outside that scope, even
            # for plain updateConfiguration (without saveAsVersion).
            if not scope_allows(allowed_versions, version):
                return {
                    "success": False,
                    "error": {
                        "type": "Unauthorized",
                        "message": f"Access denied: version '{version}' is not in your allowed scope",
                    },
                }
            # RBAC: "Save as Version" and "Save as Default" are Admin-only operations.
            # The updateConfiguration mutation allows Admin+Author at the schema level,
            # but saveAsVersion and saveAsDefault flags require Admin role.
            if custom_config:
                config_data = (
                    json.loads(custom_config)
                    if isinstance(custom_config, str)
                    else custom_config
                )
                is_save_as_version = config_data.get("saveAsVersion", False)
                is_save_as_default = config_data.get("saveAsDefault", False)
                if (is_save_as_version or is_save_as_default) and not caller[
                    "is_admin"
                ]:
                    operation_name = (
                        "Save as Version" if is_save_as_version else "Save as Default"
                    )
                    return {
                        "success": False,
                        "error": {
                            "type": "Unauthorized",
                            "message": f"Access denied: '{operation_name}' is an Admin-only operation",
                        },
                    }
            success = manager.handle_update_custom_configuration(
                custom_config, version, description, created_by=caller["email"]
            )
            return {
                "success": success,
                "message": "Configuration updated successfully"
                if success
                else "Configuration update failed",
            }
        elif operation == "setActiveVersion":
            args = event["arguments"]
            version = args.get("versionName")
            # RBAC: scope-enforce. A scoped Author cannot flip the
            # active version pointer onto something outside their scope,
            # which would otherwise redirect new document processing to a
            # config they aren't trusted with.
            if not scope_allows(allowed_versions, version):
                return {
                    "success": False,
                    "error": {
                        "type": "Unauthorized",
                        "message": f"Access denied: version '{version}' is not in your allowed scope",
                    },
                }
            return handle_set_active_version(manager, version)
        elif operation == "deleteConfigVersion":
            args = event["arguments"]
            version = args.get("versionName")
            # RBAC: scope-enforce. A scoped Author cannot delete a
            # version outside their scope. (Deletion of `default` is
            # also blocked inside handle_delete_config_version.)
            if not scope_allows(allowed_versions, version):
                return {
                    "success": False,
                    "error": {
                        "type": "Unauthorized",
                        "message": f"Access denied: version '{version}' is not in your allowed scope",
                    },
                }
            return handle_delete_config_version(manager, version)

        elif operation in (
            "listConfigProfileRevisions",
            "getConfigProfileRevision",
            "restoreConfigProfileRevision",
            "labelConfigProfileRevision",
            "deleteConfigProfileRevision",
        ):
            args = event["arguments"]
            profile = args.get("profileName")
            # Scope is enforced at the PROFILE, never at the revision: a
            # revision is content inside a profile, so one check covers every
            # revision operation and there is only one place to get it right.
            if not scope_allows(allowed_versions, profile):
                return {
                    "success": False,
                    "error": {
                        "type": "Unauthorized",
                        "message": f"Access denied: configuration profile '{profile}' is not in your allowed scope",
                    },
                }
            if not validate_version_name(profile):
                return {
                    "success": False,
                    "error": {
                        "type": "ValidationError",
                        "message": "profileName is required and must be a valid profile name",
                    },
                }
            if operation == "listConfigProfileRevisions":
                return handle_list_profile_revisions(manager, profile)
            revision = args.get("revision")
            try:
                revision = int(revision)
            except (TypeError, ValueError):
                return {
                    "success": False,
                    "error": {
                        "type": "ValidationError",
                        "message": "revision is required and must be a number",
                    },
                }
            if operation == "getConfigProfileRevision":
                return handle_get_profile_revision(manager, profile, revision)
            if operation == "restoreConfigProfileRevision":
                return handle_restore_profile_revision(
                    manager, profile, revision, caller["email"]
                )
            if operation == "labelConfigProfileRevision":
                return handle_label_profile_revision(
                    manager, profile, revision, args.get("label"), args.get("notes")
                )
            return handle_delete_profile_revision(manager, profile, revision)

        elif operation == "getPricing":
            return handle_get_pricing(manager)
        elif operation == "updatePricing":
            args = event["arguments"]
            pricing_config = args.get("pricingConfig")
            return handle_update_pricing(manager, pricing_config)
        elif operation == "restoreDefaultPricing":
            return handle_restore_default_pricing(manager)
        elif operation == "getModelConfigLimits":
            return handle_get_model_config_limits(manager)
        elif operation == "updateModelConfigLimits":
            args = event["arguments"]
            model_config_limits = args.get("modelConfigLimits")
            return handle_update_model_config_limits(manager, model_config_limits)
        elif operation == "restoreDefaultModelConfigLimits":
            return handle_restore_default_model_config_limits(manager)
        elif operation == "listConfigurationLibrary":
            return handle_list_config_library(event["arguments"])
        elif operation == "getConfigurationLibraryFile":
            return handle_get_config_library_file(event["arguments"])
        elif operation == "generateRuleJson":
            args = event["arguments"]
            rule_description = args.get("ruleDescription")
            if not rule_description or not rule_description.strip():
                return {
                    "success": False,
                    "error": {
                        "type": "ValidationError",
                        "message": "ruleDescription is required and cannot be empty",
                    },
                }
            if len(rule_description) > 2000:
                return {
                    "success": False,
                    "error": {
                        "type": "ValidationError",
                        "message": "ruleDescription cannot exceed 2000 characters",
                    },
                }
            return handle_generate_rule_json(rule_description.strip())
        else:
            raise Exception(f"Unsupported operation: {operation}")
    except ValidationError as e:
        # Pydantic validation error - return structured error for UI
        logger.error(f"Configuration validation error: {e}")

        # Build structured error response that UI can parse
        validation_errors = []
        for error in e.errors():
            field_path = " -> ".join(str(loc) for loc in error["loc"])
            validation_errors.append(
                {"field": field_path, "message": error["msg"], "type": error["type"]}
            )

        # Return error as data (not exception) so UI can handle it
        return {
            "success": False,
            "error": {
                "type": "ValidationError",
                "message": "Configuration validation failed",
                "validationErrors": validation_errors,
            },
        }

    except json.JSONDecodeError as e:
        # JSON parsing error - return structured error
        logger.error(f"JSON decode error: {e}")
        return {
            "success": False,
            "error": {
                "type": "JSONDecodeError",
                "message": f"Invalid JSON format: {str(e)}",
                "position": {
                    "line": e.lineno if hasattr(e, "lineno") else None,
                    "column": e.colno if hasattr(e, "colno") else None,
                },
            },
        }
    except Exception as e:
        # Catch all other exceptions to prevent lambda failures
        logger.error(f"Unexpected error in {operation}: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": {
                "type": "UnexpectedError",
                "message": f"An unexpected error occurred: {str(e)}",
            },
        }


def handle_get_configuration(manager, version: str):
    """
    Handle the getConfiguration GraphQL query
    Returns Schema and version configuration items.

    DESIGN PATTERN (CRITICAL):
    - Default: Full stack baseline (Pydantic validated)
    - Version: SPARSE DELTAS ONLY (raw from DynamoDB, NO Pydantic defaults!)
    - Frontend merges Default + Version for display
    - Runtime uses get_merged_configuration() for processing

    This design allows:
    - Stack upgrades to safely update Default without losing user customizations
    - Empty Version = all defaults (clean reset)
    - User customizations survive stack updates

    ANTI-PATTERNS TO AVOID:
    - DO NOT auto-copy default → version when version is empty
    - DO NOT use Pydantic validation on version (fills in defaults)
    """
    try:
        # Get Schema configuration (Pydantic validated - this is correct for Schema)
        schema_config = manager.get_configuration(CONFIG_TYPE_SCHEMA)
        if schema_config:
            # Remove config_type discriminator before sending to frontend
            schema_dict = schema_config.model_dump(
                mode="python", exclude={"config_type"}
            )
        else:
            schema_dict = {}

        # Get Default configuration (Pydantic validated - full stack baseline)
        default_config = manager.get_configuration(CONFIG_TYPE_CONFIG, DEFAULT_VERSION)
        if default_config and isinstance(default_config, IDPConfig):
            default_dict = default_config.model_dump(
                mode="python", exclude={"config_type"}
            )
        else:
            default_dict = {}

        if not version:
            raise ValueError("version is missing")

        # Get Version configuration as RAW dict (NO Pydantic defaults!)
        # This is critical for the sparse delta pattern to work correctly
        version_dict = manager.get_raw_configuration(CONFIG_TYPE_CONFIG, version)

        # If version dict doesn't exist or is empty, return empty dict
        # DO NOT auto-copy Default → Custom (this breaks the delta pattern)
        if not version_dict:
            logger.info(
                "Custom config is empty or not found - returning empty dict (expected behavior)"
            )
            version_dict = {}

        # Return all configurations as dicts (GraphQL requires JSON-serializable)
        result = {
            "success": True,
            "Schema": schema_dict,
            "Default": default_dict,
            "Custom": {} if version == "default" else version_dict,
        }

        logger.info("Returning configuration (default=full, Version=deltas only)")
        return result

    except Exception as e:
        logger.error(f"Error in getConfiguration: {str(e)}")
        raise e


def handle_list_config_library(args):
    """
    List available configurations from S3 config_library for a specific pattern
    Returns: { success: bool, items: [...], error: str }
    """
    import boto3
    from botocore.exceptions import ClientError

    pattern = args.get("pattern")
    if not pattern:
        return {"success": False, "items": [], "error": "Pattern parameter is required"}

    try:
        s3_client = boto3.client("s3")
        bucket_name = os.environ.get("CONFIGURATION_BUCKET")
        prefix = f"config_library/{pattern}/"

        logger.info(
            f"Listing config library for pattern: {pattern} in bucket: {bucket_name}"
        )

        # List "directories" under the pattern folder
        response = s3_client.list_objects_v2(
            Bucket=bucket_name, Prefix=prefix, Delimiter="/"
        )

        items = []

        # CommonPrefixes are the "directories" (config folders)
        for common_prefix in response.get("CommonPrefixes", []):
            config_dir = common_prefix["Prefix"]
            config_name = config_dir.rstrip("/").split("/")[-1]

            # Check if README.md exists in this config directory
            readme_key = f"{config_dir}README.md"
            has_readme = False

            try:
                s3_client.head_object(Bucket=bucket_name, Key=readme_key)
                has_readme = True
            except ClientError as e:
                if e.response["Error"]["Code"] != "404":
                    logger.warning(f"Error checking README for {config_name}: {e}")

            # Detect which config file type exists (prefer YAML, fallback to JSON)
            config_file_type = None
            yaml_key = f"{config_dir}config.yaml"
            json_key = f"{config_dir}config.json"

            try:
                s3_client.head_object(Bucket=bucket_name, Key=yaml_key)
                config_file_type = "yaml"
            except ClientError:
                # YAML doesn't exist, try JSON
                try:
                    s3_client.head_object(Bucket=bucket_name, Key=json_key)
                    config_file_type = "json"
                except ClientError:
                    logger.warning(
                        f"No config file found for {config_name} (checked yaml and json)"
                    )
                    # Skip this config if no config file exists
                    continue

            items.append(
                {
                    "name": config_name,
                    "hasReadme": has_readme,
                    "path": config_dir,
                    "configFileType": config_file_type,
                }
            )

        if not items:
            logger.info(f"No configurations found for pattern: {pattern}")

        logger.info(f"Found {len(items)} configurations for pattern: {pattern}")
        return {"success": True, "items": items, "error": None}

    except ClientError as e:
        logger.error(f"S3 error listing config library: {e}")
        return {
            "success": False,
            "items": [],
            "error": f"Failed to list configurations: {str(e)}",
        }
    except Exception as e:
        logger.error(f"Error listing config library: {e}")
        return {
            "success": False,
            "items": [],
            "error": f"Unexpected error: {str(e)}",
        }


def handle_get_config_library_file(args):
    """
    Get a specific file (config.yaml or README.md) from config library
    Returns: { success: bool, content: str, contentType: str, error: str }
    """
    import boto3
    from botocore.exceptions import ClientError

    pattern = args.get("pattern")
    config_name = args.get("configName")
    file_name = args.get("fileName")

    if not all([pattern, config_name, file_name]):
        return {
            "success": False,
            "content": "",
            "contentType": "",
            "error": "Missing required parameters",
        }

    # Security: Only allow specific file names
    if file_name not in ["config.yaml", "config.json", "README.md"]:
        return {
            "success": False,
            "content": "",
            "contentType": "",
            "error": f"Invalid file name: {file_name}",
        }

    try:
        s3_client = boto3.client("s3")
        bucket_name = os.environ.get("CONFIGURATION_BUCKET")
        key = f"config_library/{pattern}/{config_name}/{file_name}"

        logger.info(f"Getting file from S3: {bucket_name}/{key}")

        response = s3_client.get_object(Bucket=bucket_name, Key=key)
        content = response["Body"].read().decode("utf-8")

        # Set appropriate content type based on file extension
        if file_name == "README.md":
            content_type = "text/markdown"
        elif file_name == "config.json":
            content_type = "application/json"
        else:
            content_type = "text/yaml"

        logger.info(
            f"Successfully retrieved {file_name} for {pattern}/{config_name} "
            f"({len(content)} bytes)"
        )
        return {
            "success": True,
            "content": content,
            "contentType": content_type,
            "error": None,
        }

    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            error_msg = f"File not found: {file_name}"
        else:
            error_msg = f"S3 error: {str(e)}"

        logger.error(f"Error getting config library file: {error_msg}")
        return {
            "success": False,
            "content": "",
            "contentType": "",
            "error": error_msg,
        }
    except Exception as e:
        logger.error(f"Error getting config library file: {e}")
        return {
            "success": False,
            "content": "",
            "contentType": "",
            "error": f"Unexpected error: {str(e)}",
        }


def handle_get_pricing(manager):
    """
    Handle the getPricing GraphQL query
    Returns both merged pricing and default pricing for UI diff/restore features

    This mirrors the Default/Custom pattern for IDP configuration:
    - DefaultPricing: Full baseline from deployment (stored at deployment time)
    - CustomPricing: User overrides only (deltas)
    - Returns:
      - pricing: Merged result (default + custom overrides)
      - defaultPricing: Original defaults for diff highlighting and restore

    Returns: { success: bool, pricing: {...}, defaultPricing: {...}, error: {...} }
    """
    try:
        # Get merged pricing (DefaultPricing + CustomPricing deltas)
        pricing_config = manager.get_merged_pricing()

        # Also get default pricing for UI diff/restore features
        default_pricing_config = manager.get_configuration(CONFIG_TYPE_DEFAULT_PRICING)

        empty_pricing = {
            "textract": {},
            "bedrock": {},
            "bda": {},
            "sagemaker": {},
        }

        if pricing_config and isinstance(pricing_config, PricingConfig):
            # Convert to dict, excluding config_type discriminator
            pricing_dict = pricing_config.model_dump(
                mode="python", exclude={"config_type"}
            )
            logger.info("Returning merged pricing configuration from DynamoDB")
        else:
            # No DefaultPricing in DynamoDB - this shouldn't happen after deployment
            logger.warning("No DefaultPricing found in DynamoDB")
            pricing_dict = empty_pricing

        if default_pricing_config and isinstance(default_pricing_config, PricingConfig):
            default_pricing_dict = default_pricing_config.model_dump(
                mode="python", exclude={"config_type"}
            )
            logger.info("Returning default pricing for UI diff/restore")
        else:
            logger.warning("No DefaultPricing found for diff/restore")
            default_pricing_dict = empty_pricing

        return {
            "success": True,
            "pricing": pricing_dict,
            "defaultPricing": default_pricing_dict,
        }

    except Exception as e:
        logger.error(f"Error in getPricing: {str(e)}")
        return {
            "success": False,
            "error": {
                "type": "Error",
                "message": f"Failed to get pricing: {str(e)}",
            },
        }


def handle_update_pricing(manager, pricing_config_json):
    """
    Handle the updatePricing GraphQL mutation
    Saves custom pricing overrides (deltas) to DynamoDB

    This saves to CustomPricing, which stores only user overrides.
    The overrides are merged with DefaultPricing when reading.

    Args:
        manager: ConfigurationManager instance
        pricing_config_json: JSON string or dict with pricing deltas

    Returns: { success: bool, message: str, error: {...} }
    """
    try:
        # Parse JSON if it's a string
        if isinstance(pricing_config_json, str):
            pricing_data = json.loads(pricing_config_json)
        else:
            pricing_data = pricing_config_json

        # Validate and create PricingConfig
        pricing_config = PricingConfig(**pricing_data)

        # Save to CustomPricing (deltas only)
        success = manager.save_custom_pricing(pricing_config)

        if success:
            logger.info("Custom pricing configuration updated successfully")
            return {
                "success": True,
                "message": "Pricing configuration updated successfully",
            }
        else:
            return {
                "success": False,
                "message": "Failed to save pricing configuration",
                "error": {
                    "type": "SaveError",
                    "message": "Failed to save pricing configuration to database",
                },
            }

    except ValidationError as e:
        logger.error(f"Pricing validation error: {e}")
        validation_errors = []
        for error in e.errors():
            field_path = " -> ".join(str(loc) for loc in error["loc"])
            validation_errors.append(
                {"field": field_path, "message": error["msg"], "type": error["type"]}
            )
        return {
            "success": False,
            "error": {
                "type": "ValidationError",
                "message": "Pricing validation failed",
                "validationErrors": validation_errors,
            },
        }

    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in pricing: {e}")
        return {
            "success": False,
            "error": {
                "type": "JSONDecodeError",
                "message": f"Invalid JSON format: {str(e)}",
            },
        }

    except Exception as e:
        logger.error(f"Error in updatePricing: {str(e)}")
        return {
            "success": False,
            "error": {
                "type": "Error",
                "message": f"Failed to update pricing: {str(e)}",
            },
        }


def handle_restore_default_pricing(manager):
    """
    Handle the restoreDefaultPricing GraphQL mutation
    Restores pricing to the default values by deleting CustomPricing

    This simply deletes the CustomPricing record from DynamoDB.
    After deletion, get_merged_pricing() returns DefaultPricing only.

    Returns: { success: bool, message: str, error: {...} }
    """
    try:
        # Delete CustomPricing - this effectively resets to defaults
        success = manager.delete_custom_pricing()

        if success:
            logger.info("Pricing restored to default by deleting CustomPricing")
            return {
                "success": True,
                "message": "Pricing restored to default values",
            }
        else:
            return {
                "success": False,
                "message": "Failed to restore default pricing",
                "error": {
                    "type": "DeleteError",
                    "message": "Failed to delete custom pricing from database",
                },
            }

    except Exception as e:
        logger.error(f"Error in restoreDefaultPricing: {str(e)}")
        return {
            "success": False,
            "error": {
                "type": "Error",
                "message": f"Failed to restore default pricing: {str(e)}",
            },
        }


def handle_get_model_config_limits(manager):
    """
    Handle the getModelConfigLimits GraphQL query
    Returns both effective and default model limits for UI diff/restore features

    Mirrors the DefaultPricing/CustomPricing pattern, except Custom stores the
    FULL replacement list (model_limits is ordered, first-match-wins):
    - DefaultModelConfigLimits: baseline seeded from config_library/model_config_limits.yaml
    - CustomModelConfigLimits: complete user-edited replacement list
    - Returns:
      - modelConfigLimits: Effective result (Custom if present, else Default)
      - defaultModelConfigLimits: Original defaults for diff highlighting and restore

    Returns: { success: bool, modelConfigLimits: {...}, defaultModelConfigLimits: {...}, error: {...} }
    """
    try:
        limits_config = manager.get_merged_model_config_limits()

        # Also get defaults for UI diff/restore features
        default_limits_config = manager.get_configuration(
            CONFIG_TYPE_DEFAULT_MODEL_CONFIG_LIMITS
        )

        empty_limits = {"model_limits": []}

        if limits_config and isinstance(limits_config, ModelConfigLimitsConfig):
            limits_dict = limits_config.model_dump(
                mode="python", exclude={"config_type"}, exclude_none=True
            )
            logger.info("Returning effective model config limits from DynamoDB")
        else:
            # No DefaultModelConfigLimits in DynamoDB - stack predates the seed
            logger.warning("No DefaultModelConfigLimits found in DynamoDB")
            limits_dict = empty_limits

        if default_limits_config and isinstance(
            default_limits_config, ModelConfigLimitsConfig
        ):
            default_limits_dict = default_limits_config.model_dump(
                mode="python", exclude={"config_type"}, exclude_none=True
            )
            logger.info("Returning default model config limits for UI diff/restore")
        else:
            logger.warning("No DefaultModelConfigLimits found for diff/restore")
            default_limits_dict = empty_limits

        return {
            "success": True,
            "modelConfigLimits": limits_dict,
            "defaultModelConfigLimits": default_limits_dict,
        }

    except Exception as e:
        logger.error(f"Error in getModelConfigLimits: {str(e)}")
        return {
            "success": False,
            "error": {
                "type": "Error",
                "message": f"Failed to get model config limits: {str(e)}",
            },
        }


def handle_update_model_config_limits(manager, model_config_limits_json):
    """
    Handle the updateModelConfigLimits GraphQL mutation
    Saves the complete user-edited limits list to CustomModelConfigLimits

    Unlike pricing (deltas), the payload must be the FULL ordered list —
    model_limits is matched first-pattern-wins, so order is semantic.

    Args:
        manager: ConfigurationManager instance
        model_config_limits_json: JSON string or dict with the full model_limits list

    Returns: { success: bool, message: str, error: {...} }
    """
    try:
        # Parse JSON if it's a string
        if isinstance(model_config_limits_json, str):
            limits_data = json.loads(model_config_limits_json)
        else:
            limits_data = model_config_limits_json

        # Validate and create ModelConfigLimitsConfig
        limits_config = ModelConfigLimitsConfig(**limits_data)

        # Save to CustomModelConfigLimits (full replacement list)
        success = manager.save_custom_model_config_limits(limits_config)

        if success:
            logger.info("Custom model config limits updated successfully")
            return {
                "success": True,
                "message": (
                    "Model config limits updated successfully. Running workers "
                    "pick up the change within about a minute."
                ),
            }
        else:
            return {
                "success": False,
                "message": "Failed to save model config limits",
                "error": {
                    "type": "SaveError",
                    "message": "Failed to save model config limits to database",
                },
            }

    except ValidationError as e:
        logger.error(f"Model config limits validation error: {e}")
        validation_errors = []
        for error in e.errors():
            field_path = " -> ".join(str(loc) for loc in error["loc"])
            validation_errors.append(
                {"field": field_path, "message": error["msg"], "type": error["type"]}
            )
        return {
            "success": False,
            "error": {
                "type": "ValidationError",
                "message": "Model config limits validation failed",
                "validationErrors": validation_errors,
            },
        }

    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in model config limits: {e}")
        return {
            "success": False,
            "error": {
                "type": "JSONDecodeError",
                "message": f"Invalid JSON format: {str(e)}",
            },
        }

    except Exception as e:
        logger.error(f"Error in updateModelConfigLimits: {str(e)}")
        return {
            "success": False,
            "error": {
                "type": "Error",
                "message": f"Failed to update model config limits: {str(e)}",
            },
        }


def handle_restore_default_model_config_limits(manager):
    """
    Handle the restoreDefaultModelConfigLimits GraphQL mutation
    Restores limits to defaults by deleting CustomModelConfigLimits

    After deletion, get_merged_model_config_limits() returns
    DefaultModelConfigLimits only.

    Returns: { success: bool, message: str, error: {...} }
    """
    try:
        success = manager.delete_custom_model_config_limits()

        if success:
            logger.info(
                "Model config limits restored to default by deleting CustomModelConfigLimits"
            )
            return {
                "success": True,
                "message": "Model config limits restored to default values",
            }
        else:
            return {
                "success": False,
                "message": "Failed to restore default model config limits",
                "error": {
                    "type": "DeleteError",
                    "message": "Failed to delete custom model config limits from database",
                },
            }

    except Exception as e:
        logger.error(f"Error in restoreDefaultModelConfigLimits: {str(e)}")
        return {
            "success": False,
            "error": {
                "type": "Error",
                "message": f"Failed to restore default model config limits: {str(e)}",
            },
        }


def handle_get_config_versions(manager, allowed_versions=None):
    """
    Handle the getConfigVersions GraphQL query
    Returns list of all available configuration versions, filtered by user scope.
    """
    try:
        versions = manager.list_config_versions()

        # Filter by user's allowed config versions if scope is set
        if allowed_versions:
            versions = [
                v for v in versions if scope_allows(allowed_versions, v.get("versionName"))
            ]
            logger.info(
                f"Filtered config versions by scope: {len(versions)} versions returned"
            )

        return {"success": True, "versions": versions}

    except Exception as e:
        logger.error(f"Error in getConfigVersions: {str(e)}")
        return {
            "success": False,
            "error": {
                "type": "Error",
                "message": f"Failed to get configuration versions: {str(e)}",
            },
        }


# ===== Configuration Profile revisions =====
#
# A revision is an immutable numbered snapshot of one profile's configuration,
# cut on every save. All five operations below are already scope-checked at the
# profile in handler(); they must not re-derive access from anything else.


def handle_list_profile_revisions(manager, profile):
    """Revision history for one Configuration Profile, newest first."""
    try:
        revisions = manager.list_revisions(profile)
        return {"success": True, "revisions": revisions}
    except Exception as e:
        logger.error(f"Error listing revisions for '{profile}': {e}")
        return {
            "success": False,
            "error": {"type": "Error", "message": f"Failed to list revisions: {str(e)}"},
        }


def handle_get_profile_revision(manager, profile, revision):
    """
    Full configuration recorded in one revision.

    Returned as a JSON string so the UI can feed it straight into the same
    comparison view it uses for profiles.
    """
    try:
        config = manager.get_revision(profile, revision)
        if config is None:
            return {
                "success": False,
                "error": {
                    "type": "NotFound",
                    "message": f"Revision r{revision} of '{profile}' is no longer retained",
                },
            }
        return {"success": True, "config": json.dumps(config, default=str)}
    except Exception as e:
        logger.error(f"Error reading r{revision} of '{profile}': {e}")
        return {
            "success": False,
            "error": {"type": "Error", "message": f"Failed to read revision: {str(e)}"},
        }


def handle_restore_profile_revision(manager, profile, revision, caller_email):
    """
    Restore an earlier revision as the profile's current configuration.

    Restoring is a forward-only operation: the restored configuration is saved as
    a NEW revision, so the state being replaced stays inspectable.
    """
    try:
        new_revision = manager.restore_revision(profile, revision, created_by=caller_email)
        return {
            "success": True,
            "revision": new_revision,
            "message": f"Restored r{revision} as r{new_revision}",
        }
    except ValueError as e:
        return {"success": False, "error": {"type": "ValidationError", "message": str(e)}}
    except Exception as e:
        logger.error(f"Error restoring r{revision} of '{profile}': {e}")
        return {
            "success": False,
            "error": {"type": "Error", "message": f"Failed to restore revision: {str(e)}"},
        }


def handle_label_profile_revision(manager, profile, revision, label, notes):
    """Label a revision, which also exempts it from retention pruning."""
    if label is not None and not validate_description(label):
        return {
            "success": False,
            "error": {"type": "ValidationError", "message": "Label cannot exceed 200 characters"},
        }
    if notes is not None and not validate_description(notes):
        return {
            "success": False,
            "error": {"type": "ValidationError", "message": "Notes cannot exceed 200 characters"},
        }
    try:
        updated = manager.label_revision(profile, revision, label=label, notes=notes)
        if not updated:
            return {
                "success": False,
                "error": {
                    "type": "NotFound",
                    "message": f"Revision r{revision} of '{profile}' was not found",
                },
            }
        return {"success": True, "message": f"Updated r{revision}"}
    except Exception as e:
        logger.error(f"Error labeling r{revision} of '{profile}': {e}")
        return {
            "success": False,
            "error": {"type": "Error", "message": f"Failed to label revision: {str(e)}"},
        }


def handle_delete_profile_revision(manager, profile, revision):
    """Delete one revision. Admin-only (enforced in _OPERATION_REQUIRED_GROUPS)."""
    try:
        deleted = manager.delete_revision(profile, revision)
        if not deleted:
            return {
                "success": False,
                "error": {
                    "type": "NotFound",
                    "message": f"Revision r{revision} of '{profile}' was not found",
                },
            }
        return {"success": True, "message": f"Deleted r{revision}"}
    except ValueError as e:
        return {"success": False, "error": {"type": "ValidationError", "message": str(e)}}
    except Exception as e:
        logger.error(f"Error deleting r{revision} of '{profile}': {e}")
        return {
            "success": False,
            "error": {"type": "Error", "message": f"Failed to delete revision: {str(e)}"},
        }


def handle_set_active_version(manager, version):
    """
    Handle the setActiveVersion GraphQL mutation
    Sets a specific version as active and deactivates others.

    BDA Auto-Sync: If the version has use_bda=True and a linked BDA project,
    auto-syncs the config to BDA on activation to ensure it's current.
    If use_bda=True but no BDA project exists, auto-creates one.
    """
    try:
        if not version:
            return {
                "success": False,
                "error": {
                    "type": "ValidationError",
                    "message": "versionId is required",
                },
            }

        # Check if the version exists
        config = manager.get_configuration("Config", version)
        if not config:
            return {
                "success": False,
                "error": {
                    "type": "NotFoundError",
                    "message": f"Configuration version '{version}' not found",
                },
            }

        # Set the version as active
        manager.activate_version(version)

        # BDA Auto-Sync on activation: if use_bda is enabled, ensure BDA project is synced
        bda_message = ""
        try:
            config_dict = (
                config.model_dump(mode="python")
                if hasattr(config, "model_dump")
                else {}
            )
            use_bda = config_dict.get("use_bda", False)

            if use_bda:
                bda_arn = manager.get_bda_project_arn(version)
                if bda_arn:
                    # Has linked project — log for visibility
                    logger.info(
                        f"Version {version} activated with BDA project {bda_arn}"
                    )
                    bda_message = f" BDA project linked: {bda_arn}"
                else:
                    # No BDA project — note this for the user
                    logger.info(
                        f"Version {version} activated with use_bda=True but no BDA project linked"
                    )
                    bda_message = " Note: BDA is enabled but no project is linked. Use 'Sync to BDA' to create one."
        except Exception as bda_e:
            logger.warning(
                f"BDA check during activation failed (non-blocking): {bda_e}"
            )

        return {
            "success": True,
            "message": f"Configuration version {version} set as active.{bda_message}",
        }

    except Exception as e:
        logger.error(f"Error in setActiveVersion: {str(e)}")
        return {
            "success": False,
            "error": {
                "type": "Error",
                "message": f"Failed to set active version: {str(e)}",
            },
        }


def handle_delete_config_version(manager, version, delete_bda_project=True):
    """
    Handle the deleteConfigVersion GraphQL mutation.
    Deletes a specific configuration version and optionally its linked BDA project.

    Args:
        manager: ConfigurationManager instance
        version: Version name to delete
        delete_bda_project: If True, also delete the linked BDA project (default: True)
    """
    try:
        if not version:
            return {
                "success": False,
                "error": {
                    "type": "ValidationError",
                    "message": "versionId is required",
                },
            }

        # Prevent deletion of system default version
        if version == "default":
            return {
                "success": False,
                "error": {
                    "type": "ValidationError",
                    "message": "Cannot delete system default version",
                },
            }

        # Prevent deletion of stack-managed versions
        try:
            existing_config = manager.get_configuration("Config", version)
            if existing_config and getattr(existing_config, "managed", False):
                return {
                    "success": False,
                    "error": {
                        "type": "ValidationError",
                        "message": f"Cannot delete stack-managed version '{version}'",
                    },
                }
        except Exception as e:
            logger.warning(f"Error checking managed status for version {version}: {e}")

        # Check for linked BDA project and optionally delete it
        bda_cleanup_message = ""
        if delete_bda_project:
            try:
                bda_arn = manager.get_bda_project_arn(version)
                if bda_arn:
                    logger.info(f"Attempting to delete linked BDA project: {bda_arn}")
                    try:
                        from idp_common.bda.bda_blueprint_service import (
                            BdaBlueprintService,
                        )

                        bda_service = BdaBlueprintService(
                            dataAutomationProjectArn=bda_arn
                        )
                        bda_service.delete_project(bda_arn)
                        bda_cleanup_message = f" Linked BDA project deleted: {bda_arn}"
                        logger.info(f"Successfully deleted BDA project: {bda_arn}")
                    except Exception as bda_e:
                        logger.warning(
                            f"Failed to delete BDA project {bda_arn}: {bda_e}"
                        )
                        bda_cleanup_message = (
                            f" Warning: Failed to delete linked BDA project: {bda_arn}"
                        )
            except Exception as e:
                logger.warning(f"Error checking BDA project for version {version}: {e}")

        # Delete the version
        manager.delete_configuration("Config", version)

        return {
            "success": True,
            "message": f"Configuration version {version} deleted successfully.{bda_cleanup_message}",
        }

    except Exception as e:
        logger.error(f"Error in deleteConfigVersion: {str(e)}")
        return {
            "success": False,
            "error": {
                "type": "Error",
                "message": f"Failed to delete configuration version: {str(e)}",
            },
        }


def handle_generate_rule_json(rule_description: str):
    """
    Handle the generateRuleJson GraphQL mutation.

    Calls the Z3 RuleTranslator to convert a natural language rule description
    into a structured RuleJSON object (SMT-LIB constraints, typed parameters).

    The generated RuleJSON is returned to the UI for review and is then stored
    inline in the config under x-aws-idp-rule-json by the frontend when the
    user saves.

    Note on metering: This is a config-authoring operation (not per-document
    processing), so Bedrock token usage is logged but not merged into
    document.metering. Per-document Z3 calls (fact extraction + value
    extraction) go through idp_common.bedrock and ARE metered.

    Args:
        rule_description: Natural language rule text (e.g., "coverage_amount
            divided by annual_income must be less than or equal to 20")

    Returns:
        {success: True, ruleJson: "{...}"} on success
        {success: False, error: {...}} on failure
    """
    try:
        logger.info(f"Generating RuleJSON for rule: '{rule_description[:80]}...'")

        from idp_common.rule_validation.z3.rule_translator import RuleTranslator

        # Reuse a module-level translator instance across requests
        global _rule_translator  # noqa: PLW0603
        if _rule_translator is None:
            _rule_translator = RuleTranslator()
        translator = _rule_translator

        # Translate the natural language rule to RuleJSON.
        # Pass empty data_example since we don't have sample data at config time.
        # This produces a Workflow B rule (no path_mappings, LLM extraction at runtime).
        rule_json = translator.translate_rule(
            natural_language_rule=rule_description,
            data_example={},
            extract_paths=False,
        )

        # Convert to dict for JSON serialization
        rule_json_dict = rule_json.to_dict()

        logger.info(
            f"Successfully generated RuleJSON with "
            f"{len(rule_json_dict.get('parameters', []))} parameters and "
            f"{len(rule_json_dict.get('constraints', []))} constraints"
        )

        return {
            "success": True,
            "ruleJson": json.dumps(rule_json_dict),
        }

    except Exception as e:
        logger.error(f"Error generating RuleJSON: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": {
                "type": "TranslationError",
                "message": f"Failed to generate RuleJSON: {str(e)}",
            },
        }
