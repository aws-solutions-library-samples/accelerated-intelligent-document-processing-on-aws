# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Union

import boto3
import cfnresponse  # type: ignore[import-untyped]
import yaml
from botocore.exceptions import ClientError
from idp_common.config.configuration_manager import (
    ConfigurationManager,  # type: ignore[import-untyped]
)
from idp_common.config.merge_utils import merge_config_with_defaults
from pydantic import ValidationError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("idp_common.bedrock.client").setLevel(
    os.environ.get("BEDROCK_LOG_LEVEL", "INFO")
)

s3_client = boto3.client("s3")

# Remove slugify function - no longer needed


def fetch_content_from_s3(s3_uri: str) -> Union[Dict[str, Any], str]:
    """
    Fetches content from S3 URI and parses as JSON or YAML if possible
    """
    try:
        # Parse S3 URI
        if not s3_uri.startswith("s3://"):
            raise ValueError(f"Invalid S3 URI: {s3_uri}")

        # Remove s3:// prefix and split bucket and key
        s3_path = s3_uri[5:]
        bucket, key = s3_path.split("/", 1)

        logger.info(f"Fetching content from S3: bucket={bucket}, key={key}")

        # Fetch object from S3
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read().decode("utf-8")

        # Try to parse as JSON first, then YAML, return as string if both fail
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            try:
                return yaml.safe_load(content)
            except yaml.YAMLError:
                logger.warning(
                    f"Content from {s3_uri} is not valid JSON or YAML, returning as string"
                )
                return content

    except ClientError as e:
        logger.error(f"Error fetching content from S3 {s3_uri}: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error processing S3 URI {s3_uri}: {str(e)}")
        raise


def resolve_content(content: Union[str, Dict[str, Any]]) -> Union[Dict[str, Any], str]:
    """
    Resolves content - if it's a string starting with s3://, fetch from S3
    Otherwise return as-is
    """
    if isinstance(content, str) and content.startswith("s3://"):
        return fetch_content_from_s3(content)
    return content


# Model mapping between regions
MODEL_MAPPINGS = {
    "us.amazon.nova-lite-v1:0": "eu.amazon.nova-lite-v1:0",
    "us.amazon.nova-pro-v1:0": "eu.amazon.nova-pro-v1:0",
    "us.amazon.nova-premier-v1:0": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.amazon.nova-2-lite-v1:0": "eu.amazon.nova-2-lite-v1:0",
    "us.anthropic.claude-3-haiku-20240307-v1:0": "eu.anthropic.claude-3-haiku-20240307-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-3-5-sonnet-20241022-v2:0": "eu.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "us.anthropic.claude-3-7-sonnet-20250219-v1:0": "eu.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "us.anthropic.claude-sonnet-4-20250514-v1:0": "eu.anthropic.claude-sonnet-4-20250514-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-sonnet-4-6": "eu.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-sonnet-4-6:1m": "eu.anthropic.claude-sonnet-4-6:1m",
    "us.anthropic.claude-opus-4-20250514-v1:0": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-opus-4-1-20250805-v1:0": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-opus-4-5-20251101-v1:0": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-opus-4-6-v1": "eu.anthropic.claude-opus-4-6-v1",
    "us.anthropic.claude-opus-4-6-v1:1m": "eu.anthropic.claude-opus-4-6-v1:1m",
    "us.anthropic.claude-opus-4-7": "eu.anthropic.claude-opus-4-7",
    "us.anthropic.claude-opus-4-7:1m": "eu.anthropic.claude-opus-4-7:1m",
    "us.anthropic.claude-opus-4-8": "eu.anthropic.claude-opus-4-8",
    "us.anthropic.claude-opus-4-8:1m": "eu.anthropic.claude-opus-4-8:1m",
    "us.anthropic.claude-opus-5": "eu.anthropic.claude-opus-5",
    "us.anthropic.claude-opus-5:1m": "eu.anthropic.claude-opus-5:1m",
    # Third-party models (US-only, no EU equivalent - fall back to themselves)
    "us.meta.llama4-maverick-17b-instruct-v1:0": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.meta.llama4-scout-17b-instruct-v1:0": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
}


def get_current_region() -> str:
    """Get the current AWS region"""
    region = boto3.Session().region_name
    if region is None:
        # Fallback to environment variable or default
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    return region


def is_eu_region(region: str) -> bool:
    """Check if the region is an EU region"""
    return region.startswith("eu-")


def is_us_region(region: str) -> bool:
    """Check if the region is a US region"""
    return region.startswith("us-")


def get_model_mapping(model_id: str, target_region_type: str) -> str:
    """Get the equivalent model for the target region type"""
    if target_region_type == "eu":
        return MODEL_MAPPINGS.get(model_id, model_id)
    elif target_region_type == "us":
        # Reverse mapping for US
        for us_model, eu_model in MODEL_MAPPINGS.items():
            if model_id == eu_model:
                return us_model
        return model_id
    return model_id


def filter_models_by_region(data: Any, region_type: str) -> Any:
    """Filter out models that don't match the region type.

    Special model values like 'LambdaHook' are always preserved regardless of region.
    """
    # Region-agnostic special model values that should always be included
    REGION_AGNOSTIC_MODELS = {"LambdaHook"}

    # Models that carry no region prefix but are only available in US (and
    # us-gov) regions via the bedrock-mantle endpoint. They must NOT be offered
    # in EU-region deployments where they are not callable. See openai_responses.py.
    US_ONLY_MODELS = {
        "openai.gpt-5.4",
        "openai.gpt-5.5",
        "openai.gpt-5.6-sol",
        "openai.gpt-5.6-terra",
        "openai.gpt-5.6-luna",
    }

    def _is_us_only(item: str) -> bool:
        return item in US_ONLY_MODELS or item.startswith("openai.gpt-5")

    if isinstance(data, dict):
        filtered_data = {}
        for key, value in data.items():
            if isinstance(value, list) and any(
                isinstance(item, str) and ("us." in item or "eu." in item)
                for item in value
            ):
                # This is a model list - filter it
                filtered_list = []
                for item in value:
                    if isinstance(item, str):
                        # Always include region-agnostic special values (e.g., LambdaHook)
                        if item in REGION_AGNOSTIC_MODELS:
                            filtered_list.append(item)
                        # Include models that match the region type or are region-agnostic
                        elif region_type == "us":
                            # Include US models and non-region-specific models, exclude EU models
                            if item.startswith("us.") or (
                                not item.startswith("eu.")
                                and not item.startswith("us.")
                            ):
                                filtered_list.append(item)
                        elif region_type == "eu":
                            # Exclude US-only models (e.g., openai.gpt-5.*) that have no EU availability
                            if _is_us_only(item):
                                continue
                            # Include EU models and non-region-specific models, exclude US models
                            if item.startswith("eu.") or (
                                not item.startswith("eu.")
                                and not item.startswith("us.")
                            ):
                                filtered_list.append(item)
                        else:
                            # For other regions, include all models
                            filtered_list.append(item)
                    else:
                        filtered_list.append(item)
                filtered_data[key] = filtered_list
            else:
                filtered_data[key] = filter_models_by_region(value, region_type)
        return filtered_data
    elif isinstance(data, list):
        return [filter_models_by_region(item, region_type) for item in data]
    return data


def swap_model_ids(data: Any, region_type: str) -> Any:
    """Swap model IDs to match the region type.

    Special model values like 'LambdaHook' are never swapped.
    """
    if isinstance(data, dict):
        swapped_data = {}
        for key, value in data.items():
            if isinstance(value, str) and value == "LambdaHook":
                # Never swap special model values
                swapped_data[key] = value
            elif isinstance(value, str) and ("us." in value or "eu." in value):
                # This is a model ID - check if it needs swapping
                if region_type == "us" and value.startswith("eu."):
                    new_model = get_model_mapping(value, "us")
                    if new_model != value:
                        logger.info(f"Swapped EU model {value} to US model {new_model}")
                    swapped_data[key] = new_model
                elif region_type == "eu" and value.startswith("us."):
                    new_model = get_model_mapping(value, "eu")
                    if new_model != value:
                        logger.info(f"Swapped US model {value} to EU model {new_model}")
                    swapped_data[key] = new_model
                else:
                    swapped_data[key] = value
            else:
                swapped_data[key] = swap_model_ids(value, region_type)
        return swapped_data
    elif isinstance(data, list):
        return [swap_model_ids(item, region_type) for item in data]
    return data


def detect_pattern_from_config(config: Dict[str, Any]) -> str:
    """
    Auto-detect the IDP pattern from config content.

    Args:
        config: Configuration dictionary

    Returns:
        Pattern name (pattern-1 or pattern-2)
    """
    # Check classification method
    classification_method = config.get("classification", {}).get(
        "classificationMethod", ""
    )

    if classification_method == "bda":
        return "pattern-1"
    else:
        # Default to pattern-2 (most common - Textract + Bedrock LLM)
        return "pattern-2"


def merge_custom_with_defaults(
    custom_config: Dict[str, Any],
    pattern: str = None,
) -> Dict[str, Any]:
    """
    Merge a minimal custom config with system defaults.

    This allows users to provide only the fields they want to customize,
    with all other fields populated from system defaults.

    Args:
        custom_config: User's custom configuration (may be partial)
        pattern: Pattern to use for defaults. If None, auto-detected.

    Returns:
        Complete configuration with defaults applied
    """
    # Auto-detect pattern if not provided
    if pattern is None:
        pattern = detect_pattern_from_config(custom_config)
        logger.info(f"Auto-detected pattern: {pattern}")

    try:
        # Merge with system defaults
        merged = merge_config_with_defaults(
            custom_config, pattern=pattern, validate=False
        )

        # Log merge summary
        user_keys = set(custom_config.keys())
        merged_keys = set(merged.keys())
        logger.info(
            f"Merged custom config: user provided {len(user_keys)} sections, merged has {len(merged_keys)} sections"
        )
        logger.info(f"User-provided sections: {user_keys}")

        return merged

    except FileNotFoundError as e:
        # System defaults not available in Lambda - return config as-is
        logger.warning(f"System defaults not available, using config as-is: {e}")
        return custom_config
    except Exception as e:
        # Any other error - log and return original config
        logger.warning(f"Error merging with defaults, using config as-is: {e}")
        return custom_config


def save_configuration_bypass_manager(
    config_type: str, config_data: Any, version: str = None, description: str = None
) -> None:
    """
    Save configuration directly to DynamoDB bypassing ConfigurationManager.
    Used when ConfigurationManager is unreliable (e.g., after migration from legacy format).
    """
    import boto3
    from idp_common.config.models import (
        IDPConfig,
        ModelConfigLimitsConfig,
        PricingConfig,
        SchemaConfig,
    )

    # Get table name from environment
    table_name = os.environ.get("CONFIGURATION_TABLE_NAME")
    if not table_name:
        logger.error("CONFIGURATION_TABLE_NAME environment variable not set")
        return

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)

    # Get existing record to preserve created date and active status
    existing_item = None
    try:
        key = {"Configuration": f"{config_type}#{version}" if version else config_type}
        response = table.get_item(Key=key)
        existing_item = response.get("Item")
        # Decompress if stored in compressed format
        if existing_item:
            existing_item = ConfigurationManager._decompress_item(existing_item)
    except Exception as e:
        logger.warning(f"Could not retrieve existing record: {e}")

    # Convert dict to appropriate config type if needed (same logic as ConfigurationManager)
    if isinstance(config_data, dict):
        if config_type == "Schema":
            config_data = SchemaConfig(**config_data)
        elif config_type in ("DefaultPricing", "CustomPricing"):
            config_data = PricingConfig(**config_data)
        elif config_type in ("DefaultModelConfigLimits", "CustomModelConfigLimits"):
            config_data = ModelConfigLimitsConfig(**config_data)
        else:
            config_data = IDPConfig(**config_data)

    # Get config as dict and stringify values (same as ConfigurationRecord.to_dynamodb_item)
    from idp_common.config.models import ConfigurationRecord

    config_dict = config_data.model_dump(mode="python")
    config_dict.pop("config_type", None)  # Remove discriminator
    # Rollback-safety: omit default-valued None/0 scalars that older-release
    # config models reject on read (mirrors ConfigurationRecord.to_dynamodb_item).
    config_dict = ConfigurationRecord._omit_rollback_hostile_defaults(
        config_data, config_dict
    )
    stringified = _stringify_values(config_dict)

    # Create DynamoDB item with flattened config
    item = {
        "Configuration": f"{config_type}#{version}" if version else config_type,
        **stringified,
    }

    # Preserve existing created date and active status
    if config_type == "Config":
        if existing_item:
            if "CreatedAt" in existing_item:
                item["CreatedAt"] = existing_item["CreatedAt"]
            if "IsActive" in existing_item:
                item["IsActive"] = existing_item["IsActive"]
            if "Description" in existing_item and not description:
                item["Description"] = existing_item["Description"]
        # Set description if provided
        if description:
            item["Description"] = description
        # Always update the timestamp
        from datetime import datetime

        item["UpdatedAt"] = datetime.utcnow().isoformat() + "Z"

    # Compress config data to match ConfigurationManager storage format
    compressed_item = ConfigurationManager._compress_item(item)
    table.put_item(Item=compressed_item)
    logger.info(
        f"Saved {config_type}{f'#{version}' if version else ''} configuration bypassing ConfigurationManager"
    )


def _stringify_values(obj: Any) -> Any:
    """
    Recursively convert values to strings for DynamoDB storage.
    Same logic as ConfigurationRecord._stringify_values
    """
    if obj is None:
        return None
    elif isinstance(obj, bool):
        return obj
    elif isinstance(obj, dict):
        return {k: _stringify_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_stringify_values(item) for item in obj]
    else:
        return str(obj)


def detect_and_migrate_legacy_format() -> bool:
    """
    Detect if legacy format exists by checking for 'Default' key and migrate to versioned format.

    Returns:
        bool: True if migration was performed, False if no migration needed
    """
    import boto3
    from botocore.exceptions import ClientError

    # Get table name from environment
    table_name = os.environ.get("CONFIGURATION_TABLE_NAME")
    if not table_name:
        logger.error("CONFIGURATION_TABLE_NAME environment variable not set")
        return False

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)

    try:
        # Check for 'Default' key as indicator of legacy format
        response = table.get_item(Key={"Configuration": "Default"})

        if "Item" not in response:
            logger.info("No 'Default' key found, no legacy migration needed")
            return False

        logger.info("Legacy 'Default' configuration detected, starting migration")

        # Check if Custom also exists
        custom_response = table.get_item(Key={"Configuration": "Custom"})
        has_custom = "Item" in custom_response

        current_time = datetime.utcnow().isoformat() + "Z"

        # Migrate Default
        default_item = response["Item"]
        new_default_item = dict(default_item)
        new_default_item["Configuration"] = "Config#default"
        new_default_item["IsActive"] = not has_custom  # Active only if no Custom exists
        new_default_item["Description"] = "Migrated from Default"
        new_default_item["CreatedAt"] = current_time
        new_default_item["UpdatedAt"] = current_time

        table.put_item(Item=new_default_item)
        logger.info(f"Migrated Default -> Config#default (IsActive: {not has_custom})")

        # Delete old Default record
        table.delete_item(Key={"Configuration": "Default"})
        logger.info("Deleted legacy Default record")

        # Migrate Custom if it exists
        if has_custom:
            custom_item = custom_response["Item"]
            new_custom_item = dict(custom_item)
            new_custom_item["Configuration"] = "Config#custom"
            new_custom_item["IsActive"] = True  # Custom is active if it exists
            new_custom_item["Description"] = "Migrated from Custom"
            new_custom_item["CreatedAt"] = current_time
            new_custom_item["UpdatedAt"] = current_time

            table.put_item(Item=new_custom_item)
            logger.info("Migrated Custom -> Config#custom (IsActive: True)")

            # Delete old Custom record
            table.delete_item(Key={"Configuration": "Custom"})
            logger.info("Deleted legacy Custom record")

        logger.info("Legacy format migration completed successfully")
        return True

    except ClientError as e:
        logger.error(f"Error during migration: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error during migration: {e}")
        raise


def generate_physical_id(stack_id: str, logical_id: str) -> str:
    """
    Generates a consistent physical ID for the custom resource
    """
    return f"{stack_id}/{logical_id}/configuration"


def _parse_format_version(value: Any) -> tuple:
    """Parse a 'MAJOR.MINOR' config_format_version stamp into a comparable tuple.

    Thin adapter over the migration chain's ``parse_version`` so there is ONE
    implementation of this comparison: rollback detection here and the
    never-downgrade guard in the migrations must agree about which stamp is
    newer, and two copies of an ordering rule is how they stop agreeing.

    Differs only in the fallback: missing/unparseable reads as ``(0,)`` — the
    oldest format — because this caller compares rather than branching on
    "unknown".
    """
    from idp_common.config.migrations._version import parse_version

    return parse_version(value) or (0,)


def _is_rollback_to_older_format() -> bool:
    """Best-effort detection that THIS invocation is a stack rollback to an
    older release, i.e. this (reverted, older) Lambda code is being re-run
    against config records written by a NEWER release.

    CloudFormation gives no explicit "rollback" flag on a custom-resource
    Update event, so we infer it from the data: if any stored Config#* record
    carries a ``config_format_version`` NEWER than the format version this code
    understands (``CONFIG_FORMAT_VERSION``), then newer code must have written
    it and we are now the older code running during a rollback.

    Used only to decide whether to fail the resource (blocking the rollback) or
    return SUCCESS and let the rollback complete. A genuine forward update never
    sees a stored format newer than its own code, so it still fails loudly.
    """
    try:
        from idp_common.config.models import CONFIG_FORMAT_VERSION

        my_version = _parse_format_version(CONFIG_FORMAT_VERSION)

        table_name = os.environ.get("CONFIGURATION_TABLE_NAME")
        if not table_name:
            return False
        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)

        # config_format_version lives INSIDE the gzipped _compressed_config blob,
        # not as a top-level attribute, so read+decompress each Config#* record
        # via the manager's decompress helper and inspect the stamped version.
        resp = table.scan(ProjectionExpression="Configuration")
        for row in resp.get("Items", []):
            key = str(row.get("Configuration", ""))
            if not key.startswith("Config"):
                continue
            full = table.get_item(Key={"Configuration": key}).get("Item")
            if not full:
                continue
            item = ConfigurationManager._decompress_item(full)
            stored = _parse_format_version(item.get("config_format_version"))
            if stored > my_version:
                logger.warning(
                    f"Detected rollback: stored config '{key}' format "
                    f"{item.get('config_format_version')} is newer than this "
                    f"code's format {CONFIG_FORMAT_VERSION}"
                )
                return True
        return False
    except Exception as e:
        # Detection is best-effort; never let it mask the original error path.
        logger.warning(f"Rollback detection failed (treating as forward op): {e}")
        return False


def handler(event: Dict[str, Any], context: Any) -> None:
    """
    Handles CloudFormation Custom Resource events for configuration management
    """
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        request_type = event["RequestType"]
        properties = event["ResourceProperties"]
        stack_id = event["StackId"]
        logical_id = event["LogicalResourceId"]

        # Generate physical ID
        physical_id = generate_physical_id(stack_id, logical_id)

        # Remove ServiceToken from properties as it's not needed in DynamoDB
        properties.pop("ServiceToken", None)

        # Detect region type
        current_region = get_current_region()
        region_type = (
            "eu"
            if is_eu_region(current_region)
            else "us"
            if is_us_region(current_region)
            else "other"
        )
        logger.info(f"Detected region: {current_region}, region type: {region_type}")

        # Initialize ConfigurationManager for all database operations
        manager = ConfigurationManager()

        if request_type in ["Create", "Update"]:
            # Check for legacy migration on Update requests
            version_migration_performed = False
            if request_type == "Update":
                version_migration_performed = detect_and_migrate_legacy_format()
                if version_migration_performed:
                    logger.info(
                        "Legacy migration completed, using direct DynamoDB operations for remaining processing"
                    )

            # Collect all configurations to process
            configurations = {}

            # Process Schema configuration
            if "Schema" in properties:
                resolved_schema = resolve_content(properties["Schema"])
                # Filter models based on region
                if region_type in ["us", "eu"]:
                    resolved_schema = filter_models_by_region(
                        resolved_schema, region_type
                    )
                configurations["Schema"] = resolved_schema

            # Process Default configuration -> save with slugified name
            if "Default" in properties:
                resolved_default = resolve_content(properties["Default"])

                # Merge minimal config with system defaults
                # This allows config.yaml files to only specify what they want to customize
                if isinstance(resolved_default, dict):
                    logger.info("Merging default config with system defaults...")
                    resolved_default = merge_custom_with_defaults(resolved_default)

                # Apply custom model ARNs if provided
                if isinstance(resolved_default, dict):
                    # Replace classification model if CustomClassificationModelARN is provided and not empty
                    if (
                        "CustomClassificationModelARN" in properties
                        and properties["CustomClassificationModelARN"].strip()
                    ):
                        if "classification" in resolved_default:
                            resolved_default["classification"]["model"] = properties[
                                "CustomClassificationModelARN"
                            ]
                            logger.info(
                                f"Updated classification model to: {properties['CustomClassificationModelARN']}"
                            )

                    # Replace extraction model if CustomExtractionModelARN is provided and not empty
                    if (
                        "CustomExtractionModelARN" in properties
                        and properties["CustomExtractionModelARN"].strip()
                    ):
                        if "extraction" in resolved_default:
                            resolved_default["extraction"]["model"] = properties[
                                "CustomExtractionModelARN"
                            ]
                            logger.info(
                                f"Updated extraction model to: {properties['CustomExtractionModelARN']}"
                            )

                configurations["Config#default#System Default"] = resolved_default

            # Process Custom configuration -> save with slugified name
            if (
                "Custom" in properties
                and properties["Custom"].get("Info") != "Custom inference settings"
            ):
                resolved_custom = resolve_content(properties["Custom"])
                # Remove legacy pricing field if present (now stored separately as DefaultPricing)
                if isinstance(resolved_custom, dict):
                    resolved_custom.pop("pricing", None)

                    # Merge minimal custom config with system defaults
                    # This allows users to provide only customized fields
                    logger.info("Merging custom config with system defaults...")
                    resolved_custom = merge_custom_with_defaults(resolved_custom)

                configurations["Config#custom#"] = resolved_custom

            # Process DefaultPricing configuration if provided
            if "DefaultPricing" in properties:
                resolved_pricing = resolve_content(properties["DefaultPricing"])
                # Pricing doesn't need region-specific filtering or swapping
                # as it includes all regions (US, EU, Global) in one file
                configurations["DefaultPricing"] = resolved_pricing
                logger.info("Loaded DefaultPricing configuration")

            # Process DefaultModelConfigLimits configuration if provided
            if "DefaultModelConfigLimits" in properties:
                resolved_limits = resolve_content(
                    properties["DefaultModelConfigLimits"]
                )
                configurations["DefaultModelConfigLimits"] = resolved_limits
                logger.info("Loaded DefaultModelConfigLimits configuration")

            # Process ALL other properties as configuration versions dynamically
            excluded_properties = {
                "ServiceToken",
                "Schema",
                "Default",
                "Custom",
                "DefaultPricing",
                "DefaultModelConfigLimits",
                "ConfigLibraryHash",
                "CustomClassificationModelARN",
                "CustomExtractionModelARN",
                "PreviousIDPPattern",
            }

            for prop_name, prop_value in properties.items():
                if prop_name not in excluded_properties:
                    try:
                        # Skip if value is empty or not a string (likely not a config)
                        if not prop_value or not isinstance(prop_value, str):
                            logger.info(
                                f"Skipping property {prop_name}: not a valid config reference"
                            )
                            continue

                        # Use property name as-is for version name (preserve case)
                        version_name = prop_name
                        logger.info(
                            f"Processing configuration version: {prop_name} -> version '{version_name}'"
                        )

                        resolved_config = resolve_content(prop_value)

                        # Check if this looks like a schema (has "type": "object" at root)
                        if (
                            isinstance(resolved_config, dict)
                            and resolved_config.get("type") == "object"
                        ):
                            logger.warning(
                                f"Skipping {prop_name}: appears to be a schema, not a config"
                            )
                            continue

                        if isinstance(resolved_config, dict):
                            # Extract description before processing config
                            description = resolved_config.pop("description", "")
                            resolved_config.pop("pricing", None)
                            logger.info(
                                f"Merging {version_name} config with system defaults..."
                            )
                            resolved_config = merge_custom_with_defaults(
                                resolved_config
                            )
                            configurations[f"Config#{version_name}#{description}"] = (
                                resolved_config
                            )
                        else:
                            logger.warning(
                                f"Skipping {prop_name}: resolved content is not a dictionary"
                            )

                    except Exception as e:
                        logger.error(
                            f"Failed to process property {prop_name} as config: {e}"
                        )
                        continue

            # Apply region-specific model swapping to all configurations at once
            if region_type in ["us", "eu"] and configurations:
                configurations = swap_model_ids(configurations, region_type)
                logger.info(
                    f"Applied model swapping for {region_type} region to all configurations"
                )

            # Save all configurations
            for config_key, config in configurations.items():
                if config_key.split("#")[0] == "Config":  # config item
                    # Versioned format - use config_name as version
                    _, version, description = config_key.split("#")
                    if version_migration_performed:
                        # Use direct DynamoDB operations when migration was performed
                        save_configuration_bypass_manager(
                            "Config", config, version=version
                        )
                        logger.info(f"Updated Config {version} (bypass mode)")
                    else:
                        # Use ConfigurationManager for normal operations
                        # Check if this version already exists
                        existing_config = None
                        try:
                            existing_config = manager.get_configuration(
                                "Config", version
                            )
                        except Exception:
                            pass
                        # A stack deployment cuts a revision instead of
                        # overwriting silently, so an upgrade's configuration
                        # changes are diffable and can be rolled back.
                        if existing_config:
                            manager.save_configuration(
                                "Config",
                                config,
                                version=version,
                                created_by="stack-deployment",
                                revision_notes="Updated by stack deployment",
                            )
                        else:  # new config
                            manager.save_configuration(
                                "Config",
                                config,
                                version=version,
                                description=description,
                                created_by="stack-deployment",
                                revision_notes="Created by stack deployment",
                            )
                        logger.info(f"Updated config version: {version} configuration")
                else:
                    # Non-versioned configurations (Schema, DefaultPricing)
                    if version_migration_performed:
                        # Use direct DynamoDB operations when migration was performed
                        save_configuration_bypass_manager(config_key, config)
                        logger.info(
                            f"Updated {config_key} configuration during migration"
                        )
                    else:
                        # Use ConfigurationManager for normal operations
                        manager.save_configuration(config_key, config)
                        logger.info(f"Updated {config_key} configuration")

            # For Create: Activate custom if Custom was provided, otherwise default if Default provided
            if request_type == "Create":
                try:
                    # Check for custom configuration (Config#custom#...)
                    custom_configs = [
                        key
                        for key in configurations.keys()
                        if key.startswith("Config#custom#")
                    ]
                    default_configs = [
                        key
                        for key in configurations.keys()
                        if key.startswith("Config#default#")
                    ]

                    if custom_configs:
                        manager.activate_version("custom")
                        logger.info("Activated custom version")
                    elif default_configs:
                        manager.activate_version("default")
                        logger.info("Activated default version")
                except Exception as e:
                    logger.error(f"Error activating version during create, error: {e}")

            # Auto-enable BDA on ALL config versions when upgrading from a Pattern-1 stack
            previous_idp_pattern = properties.get("PreviousIDPPattern", "").strip()
            if (
                previous_idp_pattern
                and "pattern" in previous_idp_pattern.lower()
                and "1" in previous_idp_pattern
            ):
                logger.info(
                    f"Detected former Pattern-1 stack (PreviousIDPPattern={previous_idp_pattern}) — auto-enabling BDA on all config versions"
                )
                try:
                    all_versions = manager.list_config_versions()
                    for ver in all_versions:
                        ver_name = ver.get("versionName", "")
                        try:
                            ver_config = manager.get_configuration("Config", ver_name)
                            if ver_config:
                                # Convert Pydantic model to dict if needed
                                if hasattr(ver_config, "model_dump"):
                                    ver_config_dict = ver_config.model_dump()
                                elif isinstance(ver_config, dict):
                                    ver_config_dict = ver_config
                                else:
                                    ver_config_dict = dict(ver_config)
                                if not ver_config_dict.get("use_bda"):
                                    ver_config_dict["use_bda"] = True
                                    manager.save_configuration(
                                        "Config", ver_config_dict, version=ver_name
                                    )
                                    logger.info(
                                        f"Set use_bda=true on config version '{ver_name}'"
                                    )
                                # Also set bdaSyncStatus to needs-sync
                                try:
                                    manager.set_bda_project_arn(
                                        ver_name, sync_status="needs-sync"
                                    )
                                    logger.info(
                                        f"Set bdaSyncStatus=needs-sync on config version '{ver_name}'"
                                    )
                                except Exception as sync_err:
                                    logger.warning(
                                        f"Failed to set bdaSyncStatus on version '{ver_name}': {sync_err}"
                                    )
                        except Exception as ve:
                            logger.warning(
                                f"Failed to auto-enable BDA on version '{ver_name}': {ve}"
                            )
                except Exception as e:
                    logger.warning(
                        f"Failed to enumerate config versions for BDA auto-enable: {e}"
                    )

            cfnresponse.send(
                event,
                context,
                cfnresponse.SUCCESS,
                {"Message": f"Successfully {request_type.lower()}d configurations"},
                physical_id,
            )

        elif request_type == "Delete":
            # Do nothing on delete - preserve any existing configuration otherwise
            # data is lost during custom resource replacement (cleanup step), e.g.
            # if nested stack name or resource name is changed
            logger.info("Delete request received - preserving configuration (no-op)")
            cfnresponse.send(
                event,
                context,
                cfnresponse.SUCCESS,
                {"Message": "Success (delete = no-op)"},
                physical_id,
            )

    except ValidationError as e:
        # Pydantic validation error - format detailed error message
        logger.error(f"Configuration validation error: {e}")

        # Rollback-safety: if this reverted (older) code can't parse a config
        # written by a newer release, we are running during a stack rollback.
        # Return SUCCESS so the rollback can COMPLETE rather than wedging the
        # stack in UPDATE_ROLLBACK_FAILED. A genuine forward update never sees a
        # stored format newer than its own code, so it still fails loudly below.
        if _is_rollback_to_older_format():
            logger.warning(
                "Parse failure during a detected rollback — returning SUCCESS to "
                "allow the rollback to complete (config left as-is)."
            )
            physical_id = generate_physical_id(
                event["StackId"], event["LogicalResourceId"]
            )
            cfnresponse.send(
                event,
                context,
                cfnresponse.SUCCESS,
                {"Message": "Rollback detected; config preserved as-is."},
                physical_id,
            )
            return

        # Build detailed error message
        error_messages = []
        for error in e.errors():
            field_path = " -> ".join(str(loc) for loc in error["loc"])
            error_messages.append(
                f"{field_path}: {error['msg']} (type: {error['type']})"
            )

        detailed_error = "Configuration validation failed:\n" + "\n".join(
            error_messages
        )

        # Still need to send physical ID even on failure
        physical_id = generate_physical_id(event["StackId"], event["LogicalResourceId"])
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {
                "Error": "ValidationError",
                "Message": detailed_error,
                "ValidationErrors": [
                    {
                        "field": " -> ".join(str(loc) for loc in err["loc"]),
                        "message": err["msg"],
                        "type": err["type"],
                    }
                    for err in e.errors()
                ],
            },
            physical_id,
            reason=detailed_error[:256],  # CloudFormation has 256 char limit for reason
        )

    except json.JSONDecodeError as e:
        # JSON parsing error
        logger.error(f"JSON decode error: {e}")
        error_message = (
            f"Invalid JSON format at line {e.lineno}, column {e.colno}: {str(e)}"
            if hasattr(e, "lineno")
            else f"Invalid JSON format: {str(e)}"
        )

        physical_id = generate_physical_id(event["StackId"], event["LogicalResourceId"])
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {"Error": "JSONDecodeError", "Message": error_message},
            physical_id,
            reason=error_message[:256],
        )

    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")

        # Rollback-safety (see ValidationError handler): a TypeError such as
        # int(None) from an older model reading a newer config lands here.
        # Complete the rollback rather than wedging the stack.
        if _is_rollback_to_older_format():
            logger.warning(
                "Error during a detected rollback — returning SUCCESS to allow "
                "the rollback to complete (config left as-is)."
            )
            physical_id = generate_physical_id(
                event["StackId"], event["LogicalResourceId"]
            )
            cfnresponse.send(
                event,
                context,
                cfnresponse.SUCCESS,
                {"Message": "Rollback detected; config preserved as-is."},
                physical_id,
            )
            return

        # Still need to send physical ID even on failure
        physical_id = generate_physical_id(event["StackId"], event["LogicalResourceId"])
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {"Error": str(e)},
            physical_id,
            reason=str(e)[:256],
        )
