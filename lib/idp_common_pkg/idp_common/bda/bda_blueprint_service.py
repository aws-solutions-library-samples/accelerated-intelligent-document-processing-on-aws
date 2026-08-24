# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Optional

from botocore.exceptions import ClientError
from deepdiff import DeepDiff

from idp_common.bda.bda_blueprint_creator import BDABlueprintCreator
from idp_common.config.class_names import sanitize_class_name
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.schema_constants import (
    DEFS_FIELD,
    ID_FIELD,
    REF_FIELD,
    SCHEMA_DESCRIPTION,
    SCHEMA_ITEMS,
    SCHEMA_PROPERTIES,
    SCHEMA_TYPE,
    TYPE_ARRAY,
    TYPE_OBJECT,
    X_AWS_IDP_DOCUMENT_TYPE,
)

logger = logging.getLogger(__name__)


class BdaBlueprintService:
    def __init__(self, dataAutomationProjectArn: Optional[str] = None):
        self.dataAutomationProjectArn = dataAutomationProjectArn
        self.blueprint_creator = BDABlueprintCreator()
        self.blueprint_name_prefix = os.environ.get("STACK_NAME", "")
        self.config_manager = ConfigurationManager()
        self.max_workers = int(os.environ.get("BDA_SYNC_MAX_WORKERS", "5"))
        # Track skipped properties during schema transformation for reporting
        self._skipped_properties = []
        self._current_class = None  # Track which class is being processed

        return

    def _sanitize_project_name(self, name: str) -> str:
        """Sanitize a string for use in BDA project names.

        BDA project names must be alphanumeric with hyphens only.
        """
        import re

        # Replace non-alphanumeric chars (except hyphens) with hyphens
        sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", name)
        # Collapse multiple hyphens
        sanitized = re.sub(r"-+", "-", sanitized)
        # Trim to reasonable length (BDA limit)
        return sanitized[:128].strip("-")

    @staticmethod
    def _sanitize_class_name(name: str) -> str:
        """Sanitize a document class id for use inside a BDA blueprint name.

        ``CreateBlueprint`` requires ``blueprintName`` to match
        ``[a-zA-Z0-9-_]+``, but a class id is user- or LLM-authored and may
        contain spaces or punctuation (Discovery has been observed emitting
        ``Task cards``), which fails the API call for every affected class.

        Deliberately not ``_sanitize_project_name``: that one maps ``_`` to
        ``-``, while BDA permits underscores in a blueprint name. A class id
        that BDA already accepts must render byte-identically here, otherwise
        ``_blueprint_lookup`` would stop recognizing the blueprint it created
        for e.g. ``Bank_Statement`` and orphan cleanup would delete it as
        unexpected. Only ids that could never have produced a blueprint change.
        """
        return sanitize_class_name(name)

    def get_or_create_project_for_version(self, version_name: str) -> str:
        """Get or create a BDA project for a specific config version.

        Each config version gets its own BDA project to enable isolated
        blueprint configurations. Project ARNs are stored in DynamoDB
        (ConfigurationTable) with key 'BdaProject#{version_name}'.

        Args:
            version_name: Config version name (e.g., 'default', 'v1', 'production')

        Returns:
            BDA project ARN for this version
        """
        import boto3

        table_name = os.environ.get("CONFIGURATION_TABLE_NAME")
        if not table_name:
            logger.warning(
                "CONFIGURATION_TABLE_NAME not set, falling back to constructor ARN"
            )
            return self.dataAutomationProjectArn

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)

        # Look up stored project ARN for this version
        db_key = f"BdaProject#{version_name}"
        try:
            response = table.get_item(Key={"Configuration": db_key})
            item = response.get("Item")
            if item and item.get("ProjectArn"):
                project_arn = item["ProjectArn"]
                logger.info(
                    f"Found existing BDA project for version '{version_name}': {project_arn}"
                )
                # Verify the project still exists
                try:
                    self.blueprint_creator.bedrock_client.get_data_automation_project(
                        projectArn=project_arn, projectStage="LIVE"
                    )
                    return project_arn
                except ClientError as e:
                    if e.response["Error"]["Code"] == "ResourceNotFoundException":
                        logger.warning(
                            f"Stored BDA project {project_arn} no longer exists, creating new one"
                        )
                    else:
                        raise
        except ClientError as e:
            logger.warning(
                f"Error looking up BDA project for version '{version_name}': {e}"
            )

        # Create a new project for this version
        sanitized_version = self._sanitize_project_name(version_name)
        project_name = f"{self.blueprint_name_prefix}-{sanitized_version}"
        project_description = f"BDA project for config version '{version_name}' in stack {self.blueprint_name_prefix}"

        logger.info(
            f"Creating new BDA project for version '{version_name}': {project_name}"
        )

        # Create a minimal blueprint to bootstrap the project
        # (BDA requires at least one blueprint for project creation)
        bootstrap_schema = json.dumps(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "class": "Bootstrap",
                "description": "Temporary bootstrap blueprint for project creation",
                "type": "object",
                "properties": {
                    "placeholder": {
                        "type": "string",
                        "inferenceType": "explicit",
                        "instruction": "Temporary placeholder field",
                    }
                },
            }
        )
        bootstrap_blueprint = self.blueprint_creator.create_blueprint(
            document_type="DOCUMENT",
            blueprint_name=f"{project_name}-bootstrap-{uuid.uuid4().hex[:8]}",
            schema=bootstrap_schema,
        )

        if not bootstrap_blueprint:
            raise RuntimeError(
                f"Failed to create bootstrap blueprint for project {project_name}"
            )

        bootstrap_arn = bootstrap_blueprint.get("blueprint", {}).get("blueprintArn")

        result = self.blueprint_creator.create_data_automation_project(
            project_name=project_name,
            description=project_description,
            blueprint_arn=bootstrap_arn,
        )

        if not result:
            raise RuntimeError(
                f"Failed to create BDA project for version '{version_name}'"
            )

        project_arn = result.get("projectArn")
        if not project_arn:
            raise RuntimeError(
                f"No projectArn returned when creating BDA project for version '{version_name}'"
            )

        # Store the project ARN in DynamoDB
        try:
            table.put_item(
                Item={
                    "Configuration": db_key,
                    "ProjectArn": project_arn,
                    "ProjectName": project_name,
                    "VersionName": version_name,
                }
            )
            logger.info(
                f"Stored BDA project ARN for version '{version_name}': {project_arn}"
            )
        except ClientError as e:
            logger.error(f"Failed to store BDA project ARN: {e}")
            # Don't fail - project was created successfully

        # Clean up bootstrap blueprint (it will be replaced by real ones during sync)
        if bootstrap_arn:
            try:
                self.blueprint_creator.bedrock_client.delete_blueprint(
                    blueprintArn=bootstrap_arn
                )
                logger.info(f"Cleaned up bootstrap blueprint: {bootstrap_arn}")
            except Exception as e:
                logger.warning(f"Failed to clean up bootstrap blueprint: {e}")

        return project_arn

    def _normalize_aws_blueprint_schema(self, blueprint_schema: dict) -> dict:
        """
        Normalize AWS blueprint schema by fixing common issues.

        Handles:
        1. Missing $schema field (should be draft-07)
        2. Missing type fields (root and definitions)
        3. Missing instruction fields on $ref properties
        4. Array items with BDA fields (inferenceType, instruction)
        5. Double-escaped quotes in instruction strings

        Args:
            blueprint_schema: Raw schema from AWS API

        Returns:
            Normalized schema with fixes applied
        """
        schema = deepcopy(blueprint_schema)

        # Add $schema if missing (BDA uses draft-07)
        if "$schema" not in schema:
            schema["$schema"] = "http://json-schema.org/draft-07/schema#"
            logger.debug("Added missing '$schema' field to root schema")

        # Add root type if missing
        if "properties" in schema and "type" not in schema:
            schema["type"] = "object"
            logger.debug("Added missing 'type': 'object' to root schema")

        # Add type to definitions and fix their properties
        if "definitions" in schema:
            for def_name, def_value in schema["definitions"].items():
                if isinstance(def_value, dict):
                    # Add type if missing
                    if "properties" in def_value and "type" not in def_value:
                        def_value["type"] = "object"
                        logger.debug(
                            f"Added missing 'type': 'object' to definition '{def_name}'"
                        )

                    # Fix properties within definitions
                    if "properties" in def_value:
                        self._normalize_properties(
                            def_value["properties"], f"definitions.{def_name}"
                        )

        # Fix root-level properties
        if "properties" in schema:
            self._normalize_properties(schema["properties"], "root")

        return schema

    def _normalize_properties(self, properties: dict, path: str = "") -> None:
        """
        Normalize properties by fixing common issues.

        Args:
            properties: Properties dict to normalize (modified in-place)
            path: Current path for logging purposes
        """
        for prop_name, prop_value in properties.items():
            if not isinstance(prop_value, dict):
                continue

            current_path = f"{path}.{prop_name}" if path else prop_name

            # Fix $ref properties missing instruction
            if "$ref" in prop_value and "instruction" not in prop_value:
                prop_value["instruction"] = "-"
                logger.debug(
                    f"Added missing 'instruction': '-' to $ref property '{current_path}'"
                )

            # Fix double-escaped quotes in instruction strings
            if "instruction" in prop_value and isinstance(
                prop_value["instruction"], str
            ):
                original = prop_value["instruction"]
                # Replace double-escaped quotes (\") with single quotes (")
                fixed = original.replace('\\"', '"')
                if fixed != original:
                    prop_value["instruction"] = fixed
                    logger.debug(
                        f"Fixed double-escaped quotes in instruction for '{current_path}'"
                    )

            # Fix array items with BDA fields
            if prop_value.get("type") == "array" and "items" in prop_value:
                items = prop_value["items"]
                if isinstance(items, dict):
                    # Remove BDA fields from array items - keep only type
                    type_str = items.get("type", "string")
                    prop_value["items"] = {"type": type_str}
                    # Ensure the array itself has instruction and inferenceType fields
                    if "instruction" not in prop_value:
                        prop_value["instruction"] = "-"
                        logger.debug(
                            f"Added missing 'instruction' to array property '{current_path}'"
                        )
                    if "inferenceType" not in prop_value:
                        prop_value["inferenceType"] = "explicit"
                        logger.debug(
                            f"Added missing 'inferenceType' to array property '{current_path}'"
                        )

            # Recursively fix nested object properties
            if prop_value.get("type") == "object" and "properties" in prop_value:
                self._normalize_properties(prop_value["properties"], current_path)

    def transform_bda_blueprint_to_idp_class_schema(
        self, blueprint_schema: dict
    ) -> dict:
        """
        Transform BDA blueprint schema to IDP class schema format.

        This is the reverse transformation of _transform_json_schema_to_bedrock_blueprint.

        BDA Blueprint Format (input):
        - Uses JSON Schema draft-07
        - Has "definitions" (not "$defs")
        - References use "#/definitions/"
        - Leaf properties have "inferenceType" and "instruction" fields
        - Object/array types do NOT have BDA-specific fields
        - Array properties MUST have "instruction" field

        IDP Class Schema Format (output):
        - Uses JSON Schema draft 2020-12
        - Has "$defs" (not "definitions")
        - References use "#/$defs/"
        - Uses "description" instead of "instruction"
        - No "inferenceType" field

        Args:
            blueprint_schema: BDA blueprint schema in draft-07 format

        Returns:
            IDP class schema in draft 2020-12 format

        Raises:
            ValueError: If blueprint schema is invalid or array properties missing instruction
        """
        if not isinstance(blueprint_schema, dict):
            raise ValueError("Blueprint schema must be a dictionary")

        # Work on a copy to avoid mutating the input
        schema_copy = deepcopy(blueprint_schema)

        # Validate that array properties have instruction field
        self._validate_bda_array_instruction_requirements(schema_copy)

        # Create base IDP schema structure
        idp_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": schema_copy.get("class", "Document"),
            "x-aws-idp-document-type": schema_copy.get("class", "Document"),
            "type": "object",
        }

        # Add description if present
        if "description" in schema_copy:
            idp_schema["description"] = schema_copy["description"]

        # Transform definitions to $defs
        if "definitions" in schema_copy:
            idp_schema["$defs"] = {}
            for def_name, def_value in schema_copy["definitions"].items():
                idp_schema["$defs"][def_name] = self._transform_bda_definition_to_idp(
                    def_value
                )

        # Transform properties
        if "properties" in schema_copy:
            idp_schema["properties"] = {}
            for prop_name, prop_value in schema_copy["properties"].items():
                idp_schema["properties"][prop_name] = (
                    self._transform_bda_property_to_idp(prop_value)
                )

        return idp_schema

    def _validate_bda_array_instruction_requirements(
        self, blueprint_schema: dict
    ) -> None:
        """
        Validate that all array properties in BDA blueprint have the required 'instruction' field.

        Args:
            blueprint_schema: BDA blueprint schema to validate (works in-place on the copy)

        Raises:
            ValueError: If any array property is missing the 'instruction' field
        """

        def validate_properties(properties: dict, path: str = ""):
            """Recursively validate properties for array instruction requirements."""
            for prop_name, prop_value in properties.items():
                current_path = f"{path}.{prop_name}" if path else prop_name

                if not isinstance(prop_value, dict):
                    continue

                # Check if this is an array property
                if prop_value.get("type") == "array":
                    # Array properties must have instruction field
                    if "instruction" not in prop_value:
                        prop_value["instruction"] = "-"  # default it

                # Recursively check nested object properties
                if prop_value.get("type") == "object" and "properties" in prop_value:
                    validate_properties(prop_value["properties"], current_path)

        # Validate main properties
        if "properties" in blueprint_schema:
            validate_properties(blueprint_schema["properties"])

        # Validate definitions properties
        definitions = blueprint_schema.get("definitions", {})
        for def_name, def_value in definitions.items():
            if isinstance(def_value, dict) and "properties" in def_value:
                validate_properties(def_value["properties"], f"definitions.{def_name}")

    def _transform_bda_definition_to_idp(self, definition: dict) -> dict:
        """
        Transform a BDA definition to IDP format.

        Args:
            definition: BDA definition schema

        Returns:
            IDP definition schema
        """
        if not isinstance(definition, dict):
            return definition

        result = deepcopy(definition)

        # Infer type if missing but properties exist
        if "properties" in result and "type" not in result:
            result["type"] = "object"
            logger.debug("Inferred 'type': 'object' for definition with properties")

        # Transform properties if present
        if "properties" in result:
            transformed_properties = {}
            for prop_name, prop_value in result["properties"].items():
                transformed_properties[prop_name] = self._transform_bda_property_to_idp(
                    prop_value
                )
            result["properties"] = transformed_properties

        return result

    def _transform_bda_property_to_idp(self, property_schema: dict) -> dict:
        """
        Transform a BDA property to IDP format.

        Args:
            property_schema: BDA property schema

        Returns:
            IDP property schema
        """
        if not isinstance(property_schema, dict):
            return property_schema

        result = deepcopy(property_schema)

        # Handle $ref properties - convert definitions to $defs
        if "$ref" in result:
            result["$ref"] = result["$ref"].replace("/definitions/", "/$defs/")
            return result

        # Convert BDA-specific fields to IDP format
        if "instruction" in result:
            # Convert instruction to description
            result["description"] = result.pop("instruction")

        # Remove BDA-specific fields
        if "inferenceType" in result:
            result.pop("inferenceType")

        # Handle array items
        if result.get("type") == "array" and "items" in result:
            result["items"] = self._transform_bda_property_to_idp(result["items"])

        # Handle object properties
        if result.get("type") == "object" and "properties" in result:
            transformed_properties = {}
            for prop_name, prop_value in result["properties"].items():
                transformed_properties[prop_name] = self._transform_bda_property_to_idp(
                    prop_value
                )
            result["properties"] = transformed_properties

        return result

    def _retrieve_all_blueprints(
        self, project_arn: str, include_aws_standard: bool = False
    ):
        """
        Retrieve all blueprints from the Bedrock Data Automation service.
        If project_arn is provided, retrieves blueprints associated with that project.

        Args:
            project_arn (Optional[str]): ARN of the data automation project to filter blueprints
            include_aws_standard (bool): If True, includes AWS standard blueprints. Default False.

        Returns:
            list: List of blueprint names and ARNs
        """
        try:
            all_blueprints = []

            # If project ARN is provided, get blueprints from the project
            if project_arn:
                try:
                    blueprint_response = self.blueprint_creator.list_blueprints(
                        projectArn=project_arn, projectStage="LIVE"
                    )

                    blueprints = blueprint_response.get("blueprints", [])
                    for blueprint in blueprints:
                        blueprint_arn = blueprint.get("blueprintArn", None)
                        # Skip AWS standard blueprints unless explicitly requested
                        if (
                            not include_aws_standard
                            and "aws:blueprint" in blueprint_arn
                        ):
                            continue
                        response = self.blueprint_creator.get_blueprint(
                            blueprint_arn=blueprint_arn, stage="LIVE"
                        )
                        _blueprint = response.get("blueprint")
                        # Add blueprintVersion with default if missing
                        _blueprint["blueprintVersion"] = blueprint.get(
                            "blueprintVersion", "1"
                        )
                        all_blueprints.append(_blueprint)
                    logger.info(
                        f"{len(all_blueprints)} blueprints retrieved for {project_arn}"
                    )
                    return all_blueprints

                except ClientError as e:
                    logger.warning(f"Could not retrieve project {project_arn}: {e}")
                    # Fall through to list all blueprints
                    return []

        except Exception as e:
            logger.error(f"Error retrieving blueprints: {e}")
            return []

    def _sanitize_property_names(self, schema: dict) -> tuple[dict, dict]:
        """
        Sanitize property names by removing special characters that BDA doesn't support.

        Special characters like &, /, and others can cause blueprint creation failures.
        This method removes these characters and creates a mapping for reference.

        Args:
            schema: JSON Schema to sanitize (will be modified in-place)

        Returns:
            Tuple of (sanitized_schema, name_mapping) where name_mapping is
            {original_name: sanitized_name}
        """
        import re

        name_mapping = {}

        def sanitize_name(name: str) -> str:
            """Remove special characters from property name."""
            # Replace special characters with empty string or underscore
            # Keep alphanumeric, hyphens, and underscores
            sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", name)

            # If name changed, log it
            if sanitized != name:
                logger.info(f"Sanitized property name: '{name}' -> '{sanitized}'")
                name_mapping[name] = sanitized

            return sanitized

        def sanitize_properties(properties: dict) -> dict:
            """Recursively sanitize property names in a properties dict."""
            sanitized = {}
            for prop_name, prop_value in properties.items():
                new_name = sanitize_name(prop_name)

                if isinstance(prop_value, dict):
                    # Recursively sanitize nested object properties
                    if (
                        prop_value.get("type") == "object"
                        and "properties" in prop_value
                    ):
                        prop_value["properties"] = sanitize_properties(
                            prop_value["properties"]
                        )

                    # Recursively sanitize array item properties
                    if prop_value.get("type") == "array" and "items" in prop_value:
                        items = prop_value["items"]
                        if isinstance(items, dict) and "properties" in items:
                            items["properties"] = sanitize_properties(
                                items["properties"]
                            )

                sanitized[new_name] = prop_value

            return sanitized

        # Sanitize root properties
        if "properties" in schema:
            schema["properties"] = sanitize_properties(schema["properties"])

        # Sanitize $defs properties
        if "$defs" in schema:
            for def_name, def_value in schema["$defs"].items():
                if isinstance(def_value, dict) and "properties" in def_value:
                    def_value["properties"] = sanitize_properties(
                        def_value["properties"]
                    )

        # Sanitize definitions properties (for draft-07 schemas)
        if "definitions" in schema:
            for def_name, def_value in schema["definitions"].items():
                if isinstance(def_value, dict) and "properties" in def_value:
                    def_value["properties"] = sanitize_properties(
                        def_value["properties"]
                    )

        return schema, name_mapping

    def _strip_idp_extension_fields(self, schema: dict) -> dict:
        """
        Recursively strip IDP-specific extension fields that BDA doesn't understand.

        BDA's CreateBlueprint API rejects schemas containing unknown fields.
        IDP schemas use custom extension fields (x-aws-idp-*) for evaluation,
        classification, and other IDP-specific metadata. These must be removed
        before sending to BDA.

        Fields stripped:
        - x-aws-idp-* (evaluation-method, list-item-description, document-type, etc.)
        - format (JSON Schema format field not supported in BDA blueprint schemas)
        - required (not used in BDA blueprint schemas at property level)

        Args:
            schema: JSON Schema dict to strip (modified in-place)

        Returns:
            The same schema dict with extension fields removed
        """
        if not isinstance(schema, dict):
            return schema

        # Collect keys to remove (can't modify dict during iteration)
        keys_to_remove = []
        for key in schema:
            if key.startswith("x-aws-idp"):
                keys_to_remove.append(key)

        # Remove format field (BDA doesn't support JSON Schema format on properties)
        if "format" in schema:
            keys_to_remove.append("format")

        # Remove required field (not used in BDA blueprint property definitions)
        if "required" in schema:
            keys_to_remove.append("required")

        for key in keys_to_remove:
            removed_val = schema.pop(key)
            logger.debug(f"Stripped field '{key}' (value: {removed_val})")

        # Recurse into nested structures
        for key, value in schema.items():
            if isinstance(value, dict):
                self._strip_idp_extension_fields(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        self._strip_idp_extension_fields(item)

        return schema

    def _sanitize_def_names(self, schema: dict) -> dict:
        """
        Sanitize $defs / definitions key names that contain special characters.

        BDA blueprint definition names must not contain spaces, commas, or other
        special characters. This method sanitizes the keys and updates all $ref
        paths that reference them.

        Args:
            schema: JSON Schema dict (modified in-place)

        Returns:
            The same schema dict with sanitized definition key names
        """
        import re

        defs_field = (
            "$defs"
            if "$defs" in schema
            else "definitions"
            if "definitions" in schema
            else None
        )
        if not defs_field:
            return schema

        defs = schema[defs_field]
        ref_prefix = f"#/{defs_field}/"

        # Build mapping of old name -> new name
        name_mapping = {}
        for def_name in list(defs.keys()):
            sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", def_name)
            if sanitized != def_name:
                name_mapping[def_name] = sanitized
                logger.info(f"Sanitized definition name: '{def_name}' -> '{sanitized}'")

        # Rename definition keys
        for old_name, new_name in name_mapping.items():
            defs[new_name] = defs.pop(old_name)

        # Update all $ref paths in the schema
        if name_mapping:
            self._update_ref_paths(schema, ref_prefix, name_mapping)

        return schema

    def _update_ref_paths(self, obj: any, ref_prefix: str, name_mapping: dict) -> None:
        """Recursively update $ref paths based on definition name mapping."""
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref_val = obj["$ref"]
                if ref_val.startswith(ref_prefix):
                    old_def_name = ref_val[len(ref_prefix) :]
                    if old_def_name in name_mapping:
                        obj["$ref"] = f"{ref_prefix}{name_mapping[old_def_name]}"
                        logger.debug(f"Updated $ref: '{ref_val}' -> '{obj['$ref']}'")
            for value in obj.values():
                self._update_ref_paths(value, ref_prefix, name_mapping)
        elif isinstance(obj, list):
            for item in obj:
                self._update_ref_paths(item, ref_prefix, name_mapping)

    def _transform_json_schema_to_bedrock_blueprint(self, json_schema: dict) -> dict:
        """
        Transform JSON Schema (draft 2020-12) to BDA blueprint format (draft-07).

        Handles two input patterns:
        1. Schemas with $defs (from migration.py)
        2. Flat nested schemas (from classes_discovery.py)

        BDA requirements based on working schemas:
        - Uses "definitions" (not "$defs") - JSON Schema draft-07
        - References use "#/definitions/" (not "#/$defs/")
        - Only LEAF properties get "inferenceType" and "instruction"
        - Object/array types do NOT get these fields
        - Array properties MUST have "instruction" field
        - Property names must not contain special characters like &, /
        - No IDP extension fields (x-aws-idp-*) allowed
        - No JSON Schema 'format' or 'required' fields allowed

        Args:
            json_schema: JSON Schema from configuration (should already be sanitized)

        Returns:
            Blueprint schema in BDA-compatible draft-07 format

        Raises:
            ValueError: If array properties are missing required instruction field
        """
        # Work on a deep copy to avoid mutating the input
        schema_copy = deepcopy(json_schema)

        # Extract class name BEFORE stripping extension fields, since x-aws-idp-document-type
        # is used as a fallback for the "class" field in the BDA blueprint
        preserved_class = schema_copy.get(
            ID_FIELD, schema_copy.get(X_AWS_IDP_DOCUMENT_TYPE, "Document")
        )

        # Strip IDP-specific extension fields that BDA doesn't understand
        # This MUST happen before transformation to prevent x-aws-idp-*, format,
        # and required fields from leaking into the BDA blueprint schema
        self._strip_idp_extension_fields(schema_copy)

        # Restore $id so _create_base_blueprint_structure can use it for the "class" field
        if ID_FIELD not in schema_copy:
            schema_copy[ID_FIELD] = preserved_class

        # Sanitize $defs key names (remove spaces, commas, etc.)
        self._sanitize_def_names(schema_copy)

        # Validate array properties have instruction field
        self._validate_array_instruction_requirements(schema_copy)

        blueprint = self._create_base_blueprint_structure(schema_copy)

        defs = schema_copy.get(DEFS_FIELD, {})
        properties = schema_copy.get(SCHEMA_PROPERTIES, {})

        if defs:
            blueprint.update(self._process_schema_with_defs(defs, properties))
        else:
            blueprint.update(self._process_flat_schema(properties))

        return blueprint

    def _validate_array_instruction_requirements(self, json_schema: dict) -> None:
        """
        Ensure that all array properties have the required 'instruction' field.
        If not present, this will default it to "-" during transformation.

        BDA requires all array properties to have an 'instruction' field for proper
        blueprint creation. This method ensures compliance by defaulting missing instructions.

        Args:
            json_schema: JSON Schema to validate and update (works in-place on the copy)

        Note: This method works in-place on the provided schema (which should be a copy).
        """

        def add_missing_instructions(properties: dict, path: str = ""):
            """Recursively add missing instruction fields to array properties."""
            for prop_name, prop_value in properties.items():
                current_path = f"{path}.{prop_name}" if path else prop_name

                if not isinstance(prop_value, dict):
                    continue

                # Check if this is an array property
                if prop_value.get("type") == "array":
                    # Array properties must have instruction field - default to "-" if missing
                    if (
                        "instruction" not in prop_value
                        and "description" not in prop_value
                    ):
                        prop_value["instruction"] = "-"
                    elif (
                        "description" in prop_value and "instruction" not in prop_value
                    ):
                        # Use description as instruction if available
                        prop_value["instruction"] = prop_value["description"]

                    # Ensure items only contains "type" field for simple types
                    # But preserve complex objects with properties for extraction
                    if "items" in prop_value and isinstance(prop_value["items"], dict):
                        items = prop_value["items"]
                        # Only normalize if items is not a $ref and not a complex object
                        if "$ref" not in items and "properties" not in items:
                            type_str = items.get("type", "string")
                            prop_value["items"] = {"type": type_str}

                # Recursively check nested object properties
                if prop_value.get("type") == "object" and "properties" in prop_value:
                    add_missing_instructions(prop_value["properties"], current_path)

        # Add missing instructions to main properties
        if "properties" in json_schema:
            add_missing_instructions(json_schema["properties"])

        # Add missing instructions to $defs properties
        defs = json_schema.get("$defs", {})
        for def_name, def_value in defs.items():
            if isinstance(def_value, dict) and "properties" in def_value:
                add_missing_instructions(def_value["properties"], f"$defs.{def_name}")

    def _create_base_blueprint_structure(self, json_schema: dict) -> dict:
        """Create the base blueprint structure."""
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": json_schema.get(
                ID_FIELD, json_schema.get(X_AWS_IDP_DOCUMENT_TYPE, "Document")
            ),
            "description": json_schema.get(
                SCHEMA_DESCRIPTION, "Document schema for data extraction"
            ),
            "type": TYPE_OBJECT,
        }

    def _process_schema_with_defs(self, defs: dict, properties: dict) -> dict:
        """Process schema that has $defs (Pattern 1: from migration.py)."""
        result = {"definitions": {}, "properties": {}}

        # Process definitions
        for def_name, def_value in defs.items():
            result["definitions"][def_name] = self._add_bda_fields_to_schema(def_value)

        # Process properties and update $ref paths
        for prop_name, prop_value in properties.items():
            if REF_FIELD in prop_value:
                # $ref properties should have instruction: "-" based on correct_format.json
                result["properties"][prop_name] = {
                    REF_FIELD: self._normalize_ref_path(prop_value[REF_FIELD]),
                    "instruction": "-",
                }
            else:
                result["properties"][prop_name] = self._add_bda_fields_to_schema(
                    prop_value
                )

        return result

    def _process_flat_schema(self, properties: dict) -> dict:
        """Process flat nested schema (Pattern 2: from classes_discovery.py)."""
        simple_properties, extracted_definitions = self._extract_complex_objects(
            properties
        )

        result = {"properties": {}}

        # Add definitions if any were extracted
        if extracted_definitions:
            result["definitions"] = {}
            for def_name, def_value in extracted_definitions.items():
                result["definitions"][def_name] = self._add_bda_fields_to_schema(
                    def_value
                )

        # Transform simple properties
        for prop_name, prop_value in simple_properties.items():
            if REF_FIELD in prop_value:
                # This is a $ref to an extracted definition
                result["properties"][prop_name] = {REF_FIELD: prop_value[REF_FIELD]}
            else:
                # Handle arrays and simple properties
                prop_type = prop_value.get(SCHEMA_TYPE, "string")
                if prop_type == TYPE_ARRAY and SCHEMA_ITEMS in prop_value:
                    result["properties"][prop_name] = (
                        self._process_array_property_simple(prop_value)
                    )
                else:
                    result["properties"][prop_name] = self._add_bda_fields_to_schema(
                        prop_value
                    )

        return result

    def _normalize_ref_path(self, ref_path: str) -> str:
        """Convert $defs references to definitions references."""
        return ref_path.replace("/$defs/", "/definitions/")

    def _add_bda_fields_to_leaf_property(
        self, prop_schema: dict, original_description: str = None
    ) -> dict:
        """Add BDA fields (inferenceType, instruction) to a leaf property."""
        result = {
            "type": prop_schema.get(SCHEMA_TYPE, "string"),
            "inferenceType": prop_schema.get("inferenceType", "explicit"),
        }

        if "instruction" in prop_schema:
            result["instruction"] = prop_schema["instruction"]
        elif original_description:
            result["instruction"] = original_description
        else:
            result["instruction"] = "Extract this field from the document"

        return result

    def _process_object_properties(self, properties: dict) -> dict:
        """
        Process object properties, adding BDA fields to leaf properties only.

        BDA Limitations (as of current version):
        - Nested objects are not supported
        - Nested arrays (arrays within object definitions) are not supported

        TODO: When BDA supports nested structures, re-enable processing of:
        - TYPE_OBJECT properties with nested properties
        - TYPE_ARRAY properties within object definitions
        """
        processed_properties = {}

        for name, value in properties.items():
            # Handle $ref properties — normalize path and include with instruction
            if REF_FIELD in value:
                processed_properties[name] = {
                    REF_FIELD: self._normalize_ref_path(value[REF_FIELD]),
                    "instruction": "-",
                }
                continue

            # Skip nested objects - BDA doesn't support them
            # TODO: Re-enable when BDA supports nested objects
            if value.get(SCHEMA_TYPE) == TYPE_OBJECT:
                self._skipped_properties.append(
                    {
                        "class": self._current_class,
                        "property": name,
                        "type": "nested_object",
                        "message": f"Property '{name}' skipped - BDA does not support nested objects",
                    }
                )
                logger.warning(
                    f"Skipping nested object property '{name}' in class '{self._current_class}' - not supported by BDA"
                )
                continue

            # Skip nested arrays - BDA doesn't support arrays within object definitions
            # TODO: Re-enable when BDA supports nested arrays within objects
            if value.get(SCHEMA_TYPE) == TYPE_ARRAY:
                self._skipped_properties.append(
                    {
                        "class": self._current_class,
                        "property": name,
                        "type": "nested_array",
                        "message": f"Property '{name}' skipped - BDA does not support nested arrays within definitions",
                    }
                )
                logger.warning(
                    f"Skipping nested array property '{name}' in class '{self._current_class}' - not supported by BDA"
                )
                continue

            # Process leaf property (non-array, non-object)
            processed_properties[name] = self._add_bda_fields_to_leaf_property(
                value, value.get(SCHEMA_DESCRIPTION)
            )

        return processed_properties

    def _process_array_property_simple(self, prop_value: dict) -> dict:
        """Process array properties for simple cases."""
        items = prop_value[SCHEMA_ITEMS]
        if isinstance(items, dict) and REF_FIELD in items:
            # Array with $ref items - preserve array description as instruction
            array_property = {
                SCHEMA_TYPE: TYPE_ARRAY,
                SCHEMA_ITEMS: {REF_FIELD: items[REF_FIELD]},
            }
            # Ensure instruction field is present
            if "instruction" in prop_value:
                array_property["instruction"] = prop_value["instruction"]
            elif SCHEMA_DESCRIPTION in prop_value:
                array_property["instruction"] = prop_value[SCHEMA_DESCRIPTION]
            else:
                # Default instruction to "-" if not present
                array_property["instruction"] = "-"
            return array_property
        else:
            # Array with simple items - ensure instruction field is added but NO inferenceType
            result = self._add_bda_fields_to_schema(prop_value)
            # Ensure array has instruction field but remove inferenceType if present
            if result.get(SCHEMA_TYPE) == TYPE_ARRAY:
                if "instruction" not in result:
                    if SCHEMA_DESCRIPTION in prop_value:
                        result["instruction"] = prop_value[SCHEMA_DESCRIPTION]
                    else:
                        result["instruction"] = "-"
                # Arrays should not have inferenceType
                result.pop("inferenceType", None)
            return result

    def _process_array_property(
        self, prop_name: str, prop_value: dict, extracted_definitions: dict
    ) -> dict:
        """
        Process array properties, extracting complex item types to definitions.

        BDA Limitations (as of current version):
        - Arrays with complex nested object items are not supported if those objects contain nested structures

        TODO: When BDA supports nested structures, re-enable processing of complex nested array items.
        """
        items = prop_value.get(SCHEMA_ITEMS, {})

        if (
            isinstance(items, dict)
            and items.get(SCHEMA_TYPE) == TYPE_OBJECT
            and SCHEMA_PROPERTIES in items
        ):
            # Check if the array item object has nested complex structures
            if self._has_nested_complex_structures(items[SCHEMA_PROPERTIES]):
                # Skip arrays with complex nested item structures
                logger.info(
                    f"Skipping array property '{prop_name}' with complex nested item structures - not supported by BDA"
                )
                # Return a simple array property with instruction but no items processing
                result = {
                    SCHEMA_TYPE: TYPE_ARRAY,
                    "instruction": prop_value.get(SCHEMA_DESCRIPTION, "-"),
                }
                return result

            # Array of simple objects - extract to definition
            item_def_name = f"{prop_name}Item"

            item_definition = {
                SCHEMA_TYPE: TYPE_OBJECT,
                SCHEMA_PROPERTIES: self._process_object_properties(
                    items[SCHEMA_PROPERTIES]
                ),
            }

            if SCHEMA_DESCRIPTION in items:
                item_definition[SCHEMA_DESCRIPTION] = items[SCHEMA_DESCRIPTION]

            extracted_definitions[item_def_name] = item_definition

            # Return array with $ref to definition
            array_property = {
                SCHEMA_TYPE: TYPE_ARRAY,
                SCHEMA_ITEMS: {REF_FIELD: f"#/definitions/{item_def_name}"},
            }

            # Ensure instruction field is present
            if "instruction" in prop_value:
                array_property["instruction"] = prop_value["instruction"]
            elif SCHEMA_DESCRIPTION in prop_value:
                array_property["instruction"] = prop_value[SCHEMA_DESCRIPTION]
            else:
                # Default instruction to "-" if not present
                array_property["instruction"] = "-"

            return array_property
        else:
            # Array of simple items - ensure instruction field is added
            result = deepcopy(prop_value)
            if result.get(SCHEMA_TYPE) == TYPE_ARRAY and "instruction" not in result:
                if SCHEMA_DESCRIPTION in result:
                    result["instruction"] = result[SCHEMA_DESCRIPTION]
                else:
                    result["instruction"] = "-"
            return result

    def _extract_complex_objects(self, properties: dict) -> tuple[dict, dict]:
        """
        Extract complex objects from flat nested schema properties.

        BDA Limitations (as of current version):
        - Nested objects within objects are not supported
        - Nested arrays within objects are not supported

        This method will skip complex nested structures that BDA cannot handle.
        TODO: When BDA supports nested structures, re-enable extraction of complex nested objects.

        Args:
            properties: Properties dict from flat nested schema

        Returns:
            tuple: (simple_properties, extracted_definitions)
                - simple_properties: Properties with complex objects replaced by $ref
                - extracted_definitions: Complex objects extracted to definitions
        """
        simple_properties = {}
        extracted_definitions = {}

        for prop_name, prop_value in properties.items():
            if not isinstance(prop_value, dict):
                simple_properties[prop_name] = prop_value
                continue

            prop_type = prop_value.get(SCHEMA_TYPE, "string")

            if prop_type == TYPE_OBJECT and SCHEMA_PROPERTIES in prop_value:
                # Check if this object has nested complex structures
                has_nested_complex = self._has_nested_complex_structures(
                    prop_value[SCHEMA_PROPERTIES]
                )

                if has_nested_complex:
                    # Skip objects with nested complex structures - BDA doesn't support them
                    logger.info(
                        f"Skipping complex object property '{prop_name}' with nested structures - not supported by BDA"
                    )
                    continue

                # This is a simple object - extract to definitions
                definition_name = prop_name

                # Create the definition for this object
                object_definition = {
                    SCHEMA_TYPE: TYPE_OBJECT,
                    SCHEMA_PROPERTIES: self._process_object_properties(
                        prop_value[SCHEMA_PROPERTIES]
                    ),
                }

                # Preserve description if present
                if SCHEMA_DESCRIPTION in prop_value:
                    object_definition[SCHEMA_DESCRIPTION] = prop_value[
                        SCHEMA_DESCRIPTION
                    ]

                extracted_definitions[definition_name] = object_definition

                # Replace with $ref in simple properties
                simple_properties[prop_name] = {
                    REF_FIELD: f"#/definitions/{definition_name}"
                }

            elif prop_type == TYPE_ARRAY and SCHEMA_ITEMS in prop_value:
                # Check if array items are complex objects with nested structures
                items = prop_value.get(SCHEMA_ITEMS, {})
                if (
                    isinstance(items, dict)
                    and items.get(SCHEMA_TYPE) == TYPE_OBJECT
                    and SCHEMA_PROPERTIES in items
                    and self._has_nested_complex_structures(items[SCHEMA_PROPERTIES])
                ):
                    # Skip arrays with complex nested item structures
                    logger.info(
                        f"Skipping array property '{prop_name}' with complex nested item structures - not supported by BDA"
                    )
                    continue

                # Handle arrays using the dedicated array processor
                simple_properties[prop_name] = self._process_array_property(
                    prop_name, prop_value, extracted_definitions
                )
            else:
                # Simple property - keep as is
                simple_properties[prop_name] = prop_value

        return simple_properties, extracted_definitions

    def _has_nested_complex_structures(self, properties: dict) -> bool:
        """
        Check if properties contain nested complex structures (objects or arrays).

        BDA doesn't support:
        - Objects within objects
        - Arrays within objects

        Args:
            properties: Properties dict to check

        Returns:
            bool: True if nested complex structures are found
        """
        for prop_name, prop_value in properties.items():
            if not isinstance(prop_value, dict):
                continue

            prop_type = prop_value.get(SCHEMA_TYPE, "string")

            # Check for nested objects
            if prop_type == TYPE_OBJECT:
                logger.debug(f"Found nested object property '{prop_name}'")
                return True

            # Check for nested arrays
            if prop_type == TYPE_ARRAY:
                logger.debug(f"Found nested array property '{prop_name}'")
                return True

        return False

    def _add_bda_fields_to_schema(self, schema: dict) -> dict:
        """
        Add BDA fields (inferenceType, instruction) ONLY to leaf properties.
        Ensure array properties have instruction field but NOT inferenceType.

        Critical BDA requirements (based on working schemas):
        - Pure $ref properties: ONLY the $ref field (when inside definitions)
        - Object/array types: ONLY type and properties (NO description, inferenceType)
        - Array types: MUST have instruction field but NO inferenceType
        - Leaf types: type, inferenceType, instruction (NO description)

        Args:
            schema: Property or definition schema

        Returns:
            Schema with BDA fields, description removed
        """
        if not isinstance(schema, dict):
            return schema

        # Make deep copy to avoid mutation
        result = deepcopy(schema)

        # Remove description field - BDA doesn't use it (only instruction)
        original_description = result.pop(SCHEMA_DESCRIPTION, None)

        # Infer type from structure if missing
        if SCHEMA_TYPE not in result:
            if SCHEMA_PROPERTIES in result:
                result[SCHEMA_TYPE] = TYPE_OBJECT
                logger.debug("Inferred 'type': 'object' from properties")
            elif SCHEMA_ITEMS in result:
                result[SCHEMA_TYPE] = TYPE_ARRAY
                logger.debug("Inferred 'type': 'array' from items")
            else:
                result[SCHEMA_TYPE] = "string"  # Default for leaf properties

        prop_type = result.get(SCHEMA_TYPE)

        # Add BDA fields ONLY for leaf/primitive types
        if prop_type not in [TYPE_OBJECT, TYPE_ARRAY]:
            # This is a leaf property - use the dedicated helper
            return self._add_bda_fields_to_leaf_property(result, original_description)

        # Handle object types - process properties using helper method
        if prop_type == TYPE_OBJECT and SCHEMA_PROPERTIES in result:
            result[SCHEMA_PROPERTIES] = self._process_object_properties(
                result[SCHEMA_PROPERTIES]
            )

        # Handle array items (but don't add BDA fields to the array itself)
        if prop_type == TYPE_ARRAY and SCHEMA_ITEMS in result:
            # Special handling for $ref items - preserve the $ref structure
            items = result[SCHEMA_ITEMS]
            if isinstance(items, dict) and REF_FIELD in items:
                # For $ref items, just normalize the path and preserve the structure
                result[SCHEMA_ITEMS] = {
                    REF_FIELD: self._normalize_ref_path(items[REF_FIELD])
                }
            elif isinstance(items, dict):
                # For primitive items (string, number, etc.), keep only type
                item_type = items.get("type", "string")
                # Only process recursively if items has nested structure (object with properties)
                if items.get("type") == "object" and "properties" in items:
                    result[SCHEMA_ITEMS] = self._add_bda_fields_to_schema(items)
                else:
                    # For primitive types, keep only the type field
                    result[SCHEMA_ITEMS] = {"type": item_type}

            # Ensure array has instruction field but NO inferenceType
            if "instruction" not in result:
                if original_description:
                    result["instruction"] = original_description
                else:
                    result["instruction"] = "-"

            # Remove inferenceType if it was added (arrays should not have it)
            result.pop("inferenceType", None)

        return result

    def _check_for_updates(self, custom_class: dict, blueprint: dict):
        """
        Check if the custom_class JSON Schema differs from the existing blueprint.
        Transform both to Bedrock format and then compare.
        """
        # Parse the blueprint schema
        blueprint_schema = blueprint["schema"]
        if isinstance(blueprint_schema, str):
            blueprint_schema = json.loads(blueprint_schema)

        # Transform the custom_class to Bedrock format for comparison
        transformed_custom = self._transform_json_schema_to_bedrock_blueprint(
            custom_class
        )

        # Use DeepDiff to compare the schemas
        diff = DeepDiff(blueprint_schema, transformed_custom, ignore_order=True)

        updates_found = bool(diff)

        if updates_found:
            logger.info("Schema changes detected between custom class and blueprint")
            # Log specific differences for debugging
            logger.info(f"Differences found: {diff}")

        return updates_found

    def _process_single_class(
        self, custom_class: dict, existing_blueprints: list
    ) -> dict:
        """
        Process a single document class (create or update blueprint).
        Thread-safe method designed for parallel execution.

        Note: This method does NOT associate blueprints with the BDA project to avoid
        race conditions. The association is done after all parallel processing completes.

        Args:
            custom_class: Document class schema to process
            existing_blueprints: List of existing blueprints for lookup

        Returns:
            dict: Status information with class, blueprint_arn, blueprint_version, status, direction, classes_modified, warnings
        """
        try:
            blueprint_arn = custom_class.get("blueprint_arn", None)
            blueprint_name = custom_class.get("blueprint_name", None)
            docu_class = custom_class.get(
                ID_FIELD, custom_class.get(X_AWS_IDP_DOCUMENT_TYPE, "")
            )

            # A blueprint name must match [a-zA-Z0-9-_]+, so a class id
            # carrying a space or punctuation has to be reduced before it goes
            # into one — otherwise CreateBlueprint fails with a raw
            # ValidationException for every affected class and the version is
            # left with a project and zero blueprints.
            bda_class_name = self._sanitize_class_name(docu_class)
            if not bda_class_name:
                raise ValueError(
                    f"Document class '{docu_class}' has no characters that are "
                    f"valid in a BDA blueprint name ([a-zA-Z0-9-_]). Rename the "
                    f"class in your configuration before syncing to BDA."
                )
            if bda_class_name != docu_class:
                logger.warning(
                    "Class '%s' is not a valid BDA blueprint name component; "
                    "using '%s' for its blueprint name.",
                    docu_class,
                    bda_class_name,
                )

            # Set current class context for tracking skipped properties
            self._current_class = docu_class

            blueprint_exists = self._blueprint_lookup(existing_blueprints, docu_class)
            if blueprint_exists:
                blueprint_arn = blueprint_exists.get("blueprintArn")
                blueprint_name = blueprint_exists.get("blueprintName")

            classes_modified = False
            blueprint_version = None

            if blueprint_arn:
                # Check for updates on existing blueprint
                if self._check_for_updates(
                    custom_class=custom_class, blueprint=blueprint_exists
                ):
                    # Sanitize the class before transformation
                    sanitized_class, name_mapping = self._sanitize_property_names(
                        deepcopy(custom_class)
                    )
                    if name_mapping:
                        custom_class.clear()
                        custom_class.update(sanitized_class)
                        classes_modified = True

                    blueprint_schema = self._transform_json_schema_to_bedrock_blueprint(
                        custom_class
                    )

                    self.blueprint_creator.update_blueprint(
                        blueprint_arn=blueprint_arn,
                        stage="LIVE",
                        schema=json.dumps(blueprint_schema),
                    )
                    # Create version but don't associate with project yet (to avoid race condition)
                    version_result = self.blueprint_creator.create_blueprint_version_without_project_update(
                        blueprint_arn=blueprint_arn
                    )
                    blueprint_version = version_result["blueprint"].get(
                        "blueprintVersion"
                    )
                    logger.info(f"Updated blueprint for class {docu_class}")

            else:
                # Create new blueprint
                sanitized_class, name_mapping = self._sanitize_property_names(
                    deepcopy(custom_class)
                )
                if name_mapping:
                    custom_class.clear()
                    custom_class.update(sanitized_class)
                    classes_modified = True

                blueprint_name = (
                    f"{self.blueprint_name_prefix}-{bda_class_name}-"
                    f"{uuid.uuid4().hex[:8]}"
                )
                blueprint_schema = self._transform_json_schema_to_bedrock_blueprint(
                    custom_class
                )

                result = self.blueprint_creator.create_blueprint(
                    document_type="DOCUMENT",
                    blueprint_name=blueprint_name,
                    schema=json.dumps(blueprint_schema),
                )
                status = result["status"]
                if status != "success":
                    raise Exception(f"Failed to create blueprint: {result}")

                blueprint_arn = result["blueprint"]["blueprintArn"]
                blueprint_name = result["blueprint"]["blueprintName"]

                # Create version but don't associate with project yet (to avoid race condition)
                version_result = self.blueprint_creator.create_blueprint_version_without_project_update(
                    blueprint_arn=blueprint_arn
                )
                blueprint_version = version_result["blueprint"].get("blueprintVersion")
                logger.info(f"Created blueprint for class {docu_class}")

            # Collect any warnings for this class
            class_warnings = [
                w for w in self._skipped_properties if w.get("class") == docu_class
            ]

            return {
                "class": docu_class,
                "status": "success",
                "warnings": class_warnings,
                "_internal": {
                    "blueprint_arn": blueprint_arn,
                    "blueprint_version": blueprint_version,
                    "classes_modified": classes_modified,
                },
            }

        except Exception as e:
            class_name = (
                custom_class.get(
                    ID_FIELD, custom_class.get(X_AWS_IDP_DOCUMENT_TYPE, "unknown")
                )
                if custom_class
                else "unknown"
            )
            logger.error(f"Error processing class {class_name}: {e}")
            logger.error(f"Dump class {json.dumps(custom_class)}")
            return {
                "class": class_name,
                "status": "failed",
                # Carry the reason with the result. Callers previously reported
                # only a failed count, so a per-class cause (an API
                # ValidationException, say) was visible only in CloudWatch.
                "error": str(e),
            }

    def _process_classes_parallel(
        self, classess: list, existing_blueprints: list
    ) -> tuple:
        """
        Process multiple document classes in parallel using ThreadPoolExecutor.

        Args:
            classess: List of document class schemas to process
            existing_blueprints: List of existing blueprints for lookup

        Returns:
            tuple: (classess_status, blueprints_updated, classes_modified)
        """
        classess_status = []
        blueprints_updated = []
        blueprints_to_associate = []  # Collect blueprints to associate with project
        classes_modified = False
        status_lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(
                    self._process_single_class, custom_class, existing_blueprints
                ): custom_class
                for custom_class in classess
            }

            for future in as_completed(futures):
                try:
                    result = future.result()
                    with status_lock:
                        # Include warnings in the status for reporting
                        status_entry = {
                            "status": result["status"],
                            "class": result["class"],
                        }
                        # Include warnings if present
                        if result.get("warnings"):
                            status_entry["warnings"] = result["warnings"]
                        if result.get("error"):
                            status_entry["error"] = result["error"]
                        classess_status.append(status_entry)

                        if result["status"] == "success" and "_internal" in result:
                            internal = result["_internal"]
                            blueprints_updated.append(internal["blueprint_arn"])
                            # Collect blueprint info for project association
                            blueprints_to_associate.append(
                                {
                                    "blueprintArn": internal["blueprint_arn"],
                                    "blueprintVersion": internal.get(
                                        "blueprint_version"
                                    ),
                                }
                            )
                            if internal.get("classes_modified"):
                                classes_modified = True
                except Exception as e:
                    logger.error(f"Thread execution error: {e}")

        # After all parallel processing is complete, update the BDA project once
        # This avoids race conditions from multiple threads updating the project simultaneously
        if blueprints_to_associate:
            logger.info(
                f"Associating {len(blueprints_to_associate)} blueprints with BDA project"
            )
            try:
                self.blueprint_creator.bulk_update_data_automation_project(
                    self.dataAutomationProjectArn, blueprints_to_associate
                )
            except Exception as e:
                logger.error(f"Error associating blueprints with project: {e}")

        return classess_status, blueprints_updated, classes_modified

    def _convert_single_aws_blueprint(
        self, aws_blueprint: dict, existing_classes: list
    ) -> dict:
        """
        Convert a single AWS standard blueprint to custom blueprint.
        Thread-safe method designed for parallel execution.

        Note: This method does NOT associate blueprints with the BDA project to avoid
        race conditions. The association is done after all parallel processing completes.

        Args:
            aws_blueprint: AWS standard blueprint to convert
            existing_classes: List of existing custom classes

        Returns:
            dict: Conversion result with status, class, and internal data
        """
        try:
            blueprint_arn = aws_blueprint.get("blueprintArn", "")
            blueprint_name = aws_blueprint.get("blueprintName", "")
            blueprint_schema = aws_blueprint.get("schema")

            if isinstance(blueprint_schema, str):
                blueprint_schema = json.loads(blueprint_schema)

            docu_class = blueprint_schema.get("class", None)
            class_exists = False

            if docu_class:
                class_exists = any(
                    cls.get(ID_FIELD, cls.get(X_AWS_IDP_DOCUMENT_TYPE, ""))
                    == docu_class
                    for cls in existing_classes
                )

            if class_exists:
                return {
                    "status": "success",  # Treat skipped as success
                    "class": docu_class,
                    "_internal": {
                        "skipped": True,
                    },
                }

            # Normalize and transform
            blueprint_schema = self._normalize_aws_blueprint_schema(blueprint_schema)
            idp_class_schema = self.transform_bda_blueprint_to_idp_class_schema(
                blueprint_schema
            )
            docu_class = idp_class_schema.get(
                ID_FIELD, idp_class_schema.get(X_AWS_IDP_DOCUMENT_TYPE, "Document")
            )

            # Create new custom blueprint
            new_blueprint_name = (
                f"{self.blueprint_name_prefix}-"
                f"{self._sanitize_class_name(docu_class)}-{uuid.uuid4().hex[:8]}"
            )

            result = self.blueprint_creator.create_blueprint(
                document_type="DOCUMENT",
                blueprint_name=new_blueprint_name,
                schema=json.dumps(blueprint_schema),
            )

            status = result.get("status")
            if status != "success":
                raise Exception(f"Failed to create custom blueprint: {result}")

            new_blueprint_arn = result["blueprint"]["blueprintArn"]

            # Create version but don't associate with project yet (to avoid race condition)
            version_result = (
                self.blueprint_creator.create_blueprint_version_without_project_update(
                    blueprint_arn=new_blueprint_arn
                )
            )
            blueprint_version = version_result["blueprint"].get("blueprintVersion")

            return {
                "status": "success",
                "class": docu_class,
                "_internal": {
                    "idp_class_schema": idp_class_schema,
                    "new_blueprint_arn": new_blueprint_arn,
                    "blueprint_version": blueprint_version,
                    "aws_blueprint_arn": blueprint_arn,
                },
            }

        except Exception as e:
            blueprint_name = aws_blueprint.get("blueprintName", "unknown")
            logger.error(f"Error converting AWS blueprint {blueprint_name}: {e}")
            return {
                "status": "failed",
                "class": blueprint_name,
            }

    def _convert_aws_standard_blueprints_parallel(
        self, project_bda_blueprints: list, existing_classes: list
    ) -> dict:
        """
        Convert AWS standard blueprints to custom blueprints in parallel.

        Args:
            project_bda_blueprints: List of AWS standard blueprints
            existing_classes: List of existing custom classes

        Returns:
            dict: Conversion results with converted classes and status
        """
        converted_classes = []
        conversion_status = []
        new_custom_blueprint_arns = []
        blueprints_to_associate = []  # Collect blueprints to associate with project
        aws_blueprint_arns_to_remove = []
        status_lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=min(3, self.max_workers)) as executor:
            futures = {
                executor.submit(
                    self._convert_single_aws_blueprint, aws_blueprint, existing_classes
                ): aws_blueprint
                for aws_blueprint in project_bda_blueprints
            }

            for future in as_completed(futures):
                try:
                    result = future.result()
                    with status_lock:
                        conversion_status.append(
                            {"status": result["status"], "class": result["class"]}
                        )
                        if result["status"] == "success" and "_internal" in result:
                            internal = result["_internal"]
                            if not internal.get("skipped", False):
                                converted_classes.append(internal["idp_class_schema"])
                                new_custom_blueprint_arns.append(
                                    internal["new_blueprint_arn"]
                                )
                                # Collect blueprint info for project association
                                blueprints_to_associate.append(
                                    {
                                        "blueprintArn": internal["new_blueprint_arn"],
                                        "blueprintVersion": internal.get(
                                            "blueprint_version"
                                        ),
                                    }
                                )
                                aws_blueprint_arns_to_remove.append(
                                    internal["aws_blueprint_arn"]
                                )
                except Exception as e:
                    logger.error(f"Thread execution error during conversion: {e}")

        # After all parallel processing is complete, update the BDA project once
        # This avoids race conditions from multiple threads updating the project simultaneously
        if blueprints_to_associate:
            logger.info(
                f"Associating {len(blueprints_to_associate)} converted blueprints with BDA project"
            )
            try:
                self.blueprint_creator.bulk_update_data_automation_project(
                    self.dataAutomationProjectArn, blueprints_to_associate
                )
            except Exception as e:
                logger.error(
                    f"Error associating converted blueprints with project: {e}"
                )

        return {
            "converted_classes": converted_classes,
            "conversion_status": conversion_status,
            "new_custom_blueprint_arns": new_custom_blueprint_arns,
            "aws_blueprint_arns_to_remove": aws_blueprint_arns_to_remove,
        }

    def _blueprint_lookup(self, existing_blueprints, doc_class):
        # Create a lookup dictionary for existing blueprints by name prefix.
        # The class id is sanitized the same way it is when a blueprint is
        # created, so lookup keeps matching the name that was actually used.
        _blueprint_prefix = (
            f"{self.blueprint_name_prefix}-{self._sanitize_class_name(doc_class)}"
        )
        logger.info(f"blueprint lookup using name {_blueprint_prefix}")
        for blueprint in existing_blueprints:
            blueprint_name = blueprint.get("blueprintName", "")
            if blueprint_name.startswith(_blueprint_prefix):
                return blueprint
        return None

    def _convert_aws_standard_blueprints_to_custom(self, version: str):
        """
        Convert all AWS standard blueprints in the project to custom blueprints.

        This method:
        1. Retrieves all AWS standard blueprints from the project
        2. Converts each to an IDP class schema (in parallel)
        3. Creates new custom blueprints from the schemas
        4. Removes AWS standard blueprints from the project
        5. Saves the new IDP classes to configuration
        Args:
            version: config version name
        Returns:
            dict: Status information with converted blueprints

        Raises:
            Exception: If conversion fails
        """
        if not version:
            raise ValueError("Converting AWS standard blueprints error missing version")

        logger.info(
            f"Converting AWS standard blueprints to custom blueprints for version {version}"
        )

        try:
            # Retrieve ALL blueprints including AWS standard ones
            project_bda_blueprints = self._retrieve_all_blueprints(
                self.dataAutomationProjectArn, include_aws_standard=True
            )

            if not project_bda_blueprints:
                return []  # Return empty list for consistency

            # Get existing custom configuration
            config_item = self.config_manager.get_configuration(
                config_type="Config", version=version
            )
            existing_classes = (
                getattr(config_item, "classes", []) if config_item else []
            )

            # Convert blueprints in parallel
            conversion_results = self._convert_aws_standard_blueprints_parallel(
                project_bda_blueprints, existing_classes
            )

            converted_classes = conversion_results["converted_classes"]
            conversion_status = conversion_results["conversion_status"]
            aws_blueprint_arns_to_remove = conversion_results[
                "aws_blueprint_arns_to_remove"
            ]

            # Remove AWS standard blueprints from project if any were converted
            if aws_blueprint_arns_to_remove:
                logger.info(
                    f"Removing {len(aws_blueprint_arns_to_remove)} AWS standard blueprints from project version: {version}"
                )

                try:
                    response = self.blueprint_creator.list_blueprints(
                        self.dataAutomationProjectArn, "LIVE"
                    )
                    current_blueprints = response.get("blueprints", [])

                    updated_blueprints = [
                        bp
                        for bp in current_blueprints
                        if bp.get("blueprintArn") not in aws_blueprint_arns_to_remove
                    ]

                    updated_config = {"blueprints": updated_blueprints}
                    self.blueprint_creator.update_project_with_custom_configurations(
                        self.dataAutomationProjectArn,
                        customConfiguration=updated_config,
                    )

                except Exception as e:
                    logger.error(
                        f"Error removing AWS standard blueprints from project: {e}"
                    )

            # Save converted classes to custom configuration
            if converted_classes:
                all_classes = existing_classes + converted_classes
                self.config_manager.handle_update_custom_configuration(
                    custom_config={"classes": all_classes}, version=version
                )

            return {
                "status": "success",
                "converted_count": len(converted_classes),
                "conversion_details": conversion_status,
            }

        except Exception as e:
            logger.error(
                f"Error converting AWS standard blueprints: {e}", exc_info=True
            )
            raise Exception(f"Failed to convert AWS standard blueprints: {str(e)}")

    def create_blueprints_from_custom_configuration(
        self,
        version: str,
        sync_direction: str = "bidirectional",
        sync_mode: str = "replace",
    ):
        """
        Synchronize blueprints between BDA and IDP based on the specified direction.
        Uses parallel processing for improved performance.

        Args:
            version: config version
            sync_direction: Direction of synchronization
                - "bda_to_idp": Sync from BDA blueprints to IDP classes (read BDA, update IDP)
                - "idp_to_bda": Sync from IDP classes to BDA blueprints (read IDP, update BDA)
                - "bidirectional": Sync both directions (default, backward compatible)
            sync_mode: Mode of synchronization
                - "replace": Full replacement — target is aligned to match source exactly.
                  For bda_to_idp: IDP classes not in BDA are removed.
                  For idp_to_bda: BDA blueprints not in IDP are removed (current default behavior).
                - "merge": Additive merge — source items are added to target without removing existing items.
                  For bda_to_idp: BDA blueprints are added to IDP classes (existing classes kept).
                  For idp_to_bda: IDP classes are pushed to BDA (existing BDA-only blueprints kept).

        Raises:
            Exception: If blueprint creation fails
        """
        logger.info(
            f"Starting blueprint synchronization with direction: {sync_direction}, mode: {sync_mode}"
        )

        try:
            # Validate sync direction and mode
            valid_directions = ["bda_to_idp", "idp_to_bda", "bidirectional"]
            if sync_direction not in valid_directions:
                raise ValueError(
                    f"Invalid sync_direction: {sync_direction}. Must be one of {valid_directions}"
                )
            valid_modes = ["replace", "merge"]
            if sync_mode not in valid_modes:
                raise ValueError(
                    f"Invalid sync_mode: {sync_mode}. Must be one of {valid_modes}"
                )

            config_item = self.config_manager.get_configuration(
                config_type="Config", version=version
            )
            classess = getattr(config_item, "classes", []) if config_item else []

            classess_status = []
            classess_added = []

            # ========================================================================
            # PHASE 1: BDA → IDP Synchronization
            # Convert BDA blueprints (including AWS standard) to IDP classes
            # ========================================================================
            if sync_direction in ["bda_to_idp", "bidirectional"]:
                # For explicit bda_to_idp direction, use the requested sync_mode.
                # For bidirectional, always use merge (legacy additive behavior) to preserve backward compatibility.
                effective_phase1_mode = (
                    sync_mode if sync_direction == "bda_to_idp" else "merge"
                )
                logger.info(
                    f"Phase 1: Synchronizing BDA blueprints to IDP classes (mode: {effective_phase1_mode})"
                )

                if effective_phase1_mode == "replace":
                    # REPLACE mode: BDA project is the source of truth for IDP classes.
                    # All BDA blueprints become IDP classes, IDP classes not in BDA are removed.
                    try:
                        # Retrieve ALL blueprints from the BDA project (including AWS standard)
                        all_project_blueprints = self._retrieve_all_blueprints(
                            self.dataAutomationProjectArn, include_aws_standard=True
                        )

                        if all_project_blueprints:
                            # Convert all BDA blueprints to IDP class schemas
                            new_classes = []
                            for blueprint in all_project_blueprints:
                                try:
                                    blueprint_schema = blueprint.get("schema")
                                    if isinstance(blueprint_schema, str):
                                        blueprint_schema = json.loads(blueprint_schema)

                                    # Normalize and transform to IDP format
                                    blueprint_schema = (
                                        self._normalize_aws_blueprint_schema(
                                            blueprint_schema
                                        )
                                    )
                                    idp_class_schema = self.transform_bda_blueprint_to_idp_class_schema(
                                        blueprint_schema
                                    )
                                    docu_class = idp_class_schema.get(
                                        ID_FIELD,
                                        idp_class_schema.get(
                                            X_AWS_IDP_DOCUMENT_TYPE, "Document"
                                        ),
                                    )
                                    new_classes.append(idp_class_schema)
                                    classess_status.append(
                                        {"status": "success", "class": docu_class}
                                    )
                                    logger.info(
                                        f"Converted BDA blueprint to IDP class: {docu_class}"
                                    )
                                except Exception as e:
                                    blueprint_name = blueprint.get(
                                        "blueprintName", "unknown"
                                    )
                                    logger.error(
                                        f"Error converting blueprint {blueprint_name}: {e}"
                                    )
                                    classess_status.append(
                                        {"status": "failed", "class": blueprint_name}
                                    )

                            # Replace IDP classes entirely with the BDA-derived classes
                            if new_classes or not all_project_blueprints:
                                removed_classes = []
                                new_class_ids = {
                                    cls.get(
                                        ID_FIELD, cls.get(X_AWS_IDP_DOCUMENT_TYPE, "")
                                    )
                                    for cls in new_classes
                                }
                                for old_cls in classess:
                                    old_id = old_cls.get(
                                        ID_FIELD,
                                        old_cls.get(X_AWS_IDP_DOCUMENT_TYPE, ""),
                                    )
                                    if old_id not in new_class_ids:
                                        removed_classes.append(old_id)

                                if removed_classes:
                                    logger.info(
                                        f"Replace mode: Removing {len(removed_classes)} IDP classes not in BDA: {removed_classes}"
                                    )

                                classess = new_classes
                                self.config_manager.handle_update_custom_configuration(
                                    custom_config={"classes": classess}, version=version
                                )
                                logger.info(
                                    f"Replaced IDP classes with {len(new_classes)} classes from BDA"
                                )

                                # Refresh config_item for Phase 2
                                config_item = self.config_manager.get_configuration(
                                    config_type="Config", version=version
                                )
                        else:
                            # No blueprints in BDA project — clear all IDP classes
                            logger.info(
                                "Replace mode: No blueprints in BDA project, clearing IDP classes"
                            )
                            classess = []
                            self.config_manager.handle_update_custom_configuration(
                                custom_config={"classes": classess}, version=version
                            )
                            config_item = self.config_manager.get_configuration(
                                config_type="Config", version=version
                            )

                    except Exception as e:
                        logger.error(f"Error during replace-mode BDA→IDP sync: {e}")

                else:
                    # MERGE mode: Add BDA blueprints to IDP classes without removing existing ones.
                    # This is the original/legacy behavior.
                    try:
                        conversion_result = (
                            self._convert_aws_standard_blueprints_to_custom(
                                version=version
                            )
                        )

                        if (
                            conversion_result
                            and conversion_result.get("converted_count", 0) > 0
                        ):
                            logger.info(
                                f"Converted {conversion_result.get('converted_count', 0)} AWS standard blueprints"
                            )
                            # Refresh classes list as new classes were added
                            config_item = self.config_manager.get_configuration(
                                config_type="Config", version=version
                            )
                            classess_status.extend(
                                conversion_result.get("conversion_details", [])
                            )

                    except Exception as e:
                        logger.error(f"Error converting AWS standard blueprints: {e}")

            # ========================================================================
            # PHASE 2: IDP → BDA Synchronization
            # Create/update BDA blueprints from IDP classes (in parallel)
            # ========================================================================
            blueprints_updated = []
            classes_modified = False

            if sync_direction in ["idp_to_bda", "bidirectional"]:
                logger.info(
                    f"Phase 2: Synchronizing IDP classes to BDA blueprints (mode: {sync_mode})"
                )

                # Retrieve all blueprints for this project
                existing_blueprints = self._retrieve_all_blueprints(
                    self.dataAutomationProjectArn
                )
                if not existing_blueprints:
                    existing_blueprints = []

                if not config_item:
                    return []  # Return empty list for consistency

                # version is stored as sparse so it wont have class for default clone versions
                # Let empty versions proceed to _synchronize_deletes to clear BDA

                # Process classes in parallel
                status, updated, modified = self._process_classes_parallel(
                    classess, existing_blueprints
                )
                classess_status.extend(status)
                blueprints_updated.extend(updated)
                classes_modified = classes_modified or modified

                # Synchronize deletes only in replace mode (remove BDA blueprints not in IDP)
                # In merge mode, keep existing BDA-only blueprints alive
                if sync_mode == "replace":
                    self._synchronize_deletes(
                        existing_blueprints=existing_blueprints,
                        blueprints_updated=blueprints_updated,
                    )
                else:
                    logger.info(
                        "Merge mode: Skipping blueprint deletion — existing BDA-only blueprints will be kept"
                    )

                # Remove any remaining AWS standard blueprints from the project
                # so the project only contains custom blueprints matching the IDP config
                self._remove_aws_standard_blueprints_from_project()

            # Save updated classes if any were added from BDA or if any were sanitized
            if len(classess_added) > 0 or classes_modified:
                if len(classess_added) > 0:
                    classess.extend(classess_added)
                logger.info(
                    f"Saving updated classes (added: {len(classess_added)}, modified: {classes_modified})"
                )
                self.config_manager.handle_update_custom_configuration(
                    custom_config={"classes": classess}, version=version
                )

            return classess_status

        except Exception as e:
            logger.error(f"Error processing blueprint creation: {e}", exc_info=True)
            raise Exception(f"Failed to process blueprint creation: {str(e)}")

    def delete_project(self, project_arn: str) -> bool:
        """
        Delete a BDA project and clean up its tracking entry in DynamoDB.

        This is used when deleting a config version that has a linked BDA project.
        It deletes the BDA project from AWS and removes the DynamoDB tracking entry.

        Args:
            project_arn: The ARN of the BDA project to delete

        Returns:
            True if deletion was successful, False otherwise
        """
        import boto3

        try:
            logger.info(f"Deleting BDA project: {project_arn}")

            # Delete the BDA project from AWS
            self.blueprint_creator.bedrock_client.delete_data_automation_project(
                projectArn=project_arn
            )
            logger.info(f"Successfully deleted BDA project from AWS: {project_arn}")

            # Clean up DynamoDB tracking entry
            configuration_table_name = os.environ.get("CONFIGURATION_TABLE_NAME")
            if configuration_table_name:
                try:
                    dynamodb = boto3.resource("dynamodb")
                    table = dynamodb.Table(configuration_table_name)
                    # Find and delete BdaProject# entries that reference this ARN.
                    # Must paginate: DynamoDB bounds the 1MB page by items
                    # EXAMINED, not items matching FilterExpression, so a single
                    # call can miss the very entry being cleaned up and leave an
                    # orphaned tracking row pointing at a deleted BDA project.
                    scan_kwargs = {
                        "FilterExpression": "begins_with(Configuration, :prefix)",
                        "ExpressionAttributeValues": {":prefix": "BdaProject#"},
                    }
                    while True:
                        response = table.scan(**scan_kwargs)
                        for item in response.get("Items", []):
                            if item.get("ProjectArn") == project_arn:
                                table.delete_item(
                                    Key={"Configuration": item["Configuration"]}
                                )
                                logger.info(
                                    f"Deleted DynamoDB tracking entry: {item['Configuration']}"
                                )
                        last_key = response.get("LastEvaluatedKey")
                        if not last_key:
                            break
                        scan_kwargs["ExclusiveStartKey"] = last_key
                except Exception as db_e:
                    logger.warning(
                        f"Failed to clean up DynamoDB tracking for {project_arn}: {db_e}"
                    )

            return True

        except Exception as e:
            logger.error(f"Failed to delete BDA project {project_arn}: {e}")
            return False

    def cleanup_orphaned_blueprints(self, version: str) -> dict:
        """
        Delete all BDA blueprints with the stack prefix that are NOT in current IDP config.

        This is useful for cleaning up orphaned blueprints that remain in BDA after
        document classes have been removed from the IDP configuration.
        Args:
            version: config version name
        Returns:
            dict: Status information with deleted_count, failed_count, and details
        """
        logger.info(
            f"Starting orphaned blueprint cleanup with prefix: {self.blueprint_name_prefix}"
        )

        try:
            # Get all blueprints with our prefix from BDA (account-wide, not project-specific)
            all_bda_blueprints = self.blueprint_creator.list_all_blueprints_with_prefix(
                self.blueprint_name_prefix
            )

            if not all_bda_blueprints:
                logger.info("No blueprints found with prefix, nothing to clean up")
                return {
                    "success": True,
                    "message": "No orphaned blueprints found",
                    "deleted_count": 0,
                    "failed_count": 0,
                    "details": [],
                }

            # Get current IDP configuration classes
            config_item = self.config_manager.get_configuration(
                config_type="Config", version=version
            )
            current_classes = getattr(config_item, "classes", []) if config_item else []

            # Build set of expected blueprint name prefixes from current config
            expected_prefixes = set()
            for cls in current_classes:
                doc_class = cls.get(ID_FIELD, cls.get(X_AWS_IDP_DOCUMENT_TYPE, ""))
                if doc_class:
                    # Must sanitize identically to the creation path, or a
                    # class whose id needed sanitizing would never match its
                    # own blueprint and cleanup would delete a live one.
                    expected_prefixes.add(
                        f"{self.blueprint_name_prefix}-"
                        f"{self._sanitize_class_name(doc_class)}"
                    )

            logger.info(
                f"Found {len(expected_prefixes)} expected class prefixes from IDP config"
            )

            # Find orphaned blueprints (not matching any expected prefix)
            orphaned_blueprints = []
            for blueprint in all_bda_blueprints:
                blueprint_name = blueprint.get("blueprintName", "")
                is_orphaned = True
                for prefix in expected_prefixes:
                    if blueprint_name.startswith(prefix):
                        is_orphaned = False
                        break
                if is_orphaned:
                    orphaned_blueprints.append(blueprint)

            logger.info(
                f"Found {len(orphaned_blueprints)} orphaned blueprints to delete"
            )

            if not orphaned_blueprints:
                return {
                    "success": True,
                    "message": "No orphaned blueprints found",
                    "deleted_count": 0,
                    "failed_count": 0,
                    "details": [],
                }

            # First, remove orphaned blueprints from the BDA project (if associated)
            try:
                response = self.blueprint_creator.list_blueprints(
                    self.dataAutomationProjectArn, "LIVE"
                )
                project_blueprints = response.get("blueprints", [])
                orphaned_arns = {bp.get("blueprintArn") for bp in orphaned_blueprints}

                # Filter out orphaned blueprints from project
                updated_blueprints = [
                    bp
                    for bp in project_blueprints
                    if bp.get("blueprintArn") not in orphaned_arns
                ]

                if len(updated_blueprints) < len(project_blueprints):
                    logger.info(
                        f"Removing {len(project_blueprints) - len(updated_blueprints)} orphaned blueprints from project"
                    )
                    self.blueprint_creator.update_project_with_custom_configurations(
                        self.dataAutomationProjectArn,
                        customConfiguration={"blueprints": updated_blueprints},
                    )
            except Exception as e:
                logger.warning(
                    f"Could not update project to remove orphaned blueprints: {e}"
                )

            # Delete the orphaned blueprints
            deleted_count = 0
            failed_count = 0
            details = []

            for blueprint in orphaned_blueprints:
                blueprint_arn = blueprint.get("blueprintArn")
                blueprint_name = blueprint.get("blueprintName")
                blueprint_version = blueprint.get("blueprintVersion", "1")

                try:
                    success = self.blueprint_creator.delete_blueprint(
                        blueprint_arn, blueprint_version
                    )
                    if success:
                        deleted_count += 1
                        details.append(
                            {
                                "name": blueprint_name,
                                "arn": blueprint_arn,
                                "status": "deleted",
                            }
                        )
                        logger.info(f"Deleted orphaned blueprint: {blueprint_name}")
                    else:
                        failed_count += 1
                        details.append(
                            {
                                "name": blueprint_name,
                                "arn": blueprint_arn,
                                "status": "failed",
                            }
                        )
                except Exception as e:
                    failed_count += 1
                    details.append(
                        {
                            "name": blueprint_name,
                            "arn": blueprint_arn,
                            "status": "failed",
                            "error": str(e),
                        }
                    )
                    logger.error(
                        f"Failed to delete orphaned blueprint {blueprint_name}: {e}"
                    )

            message = f"Deleted {deleted_count} orphaned blueprints"
            if failed_count > 0:
                message += f", {failed_count} failed"

            return {
                "success": failed_count == 0,
                "message": message,
                "deleted_count": deleted_count,
                "failed_count": failed_count,
                "details": details,
            }

        except Exception as e:
            logger.error(f"Error during orphaned blueprint cleanup: {e}", exc_info=True)
            return {
                "success": False,
                "message": f"Cleanup failed: {str(e)}",
                "deleted_count": 0,
                "failed_count": 0,
                "details": [],
            }

    def _remove_aws_standard_blueprints_from_project(self):
        """
        Remove AWS standard blueprints from the BDA project's custom output configuration.

        When syncing IDP → BDA, the project should only contain custom blueprints
        that match the IDP config. AWS standard blueprints (with 'aws:blueprint' in ARN)
        that were added during initial project creation should be disassociated.
        """
        try:
            response = self.blueprint_creator.list_blueprints(
                self.dataAutomationProjectArn, "LIVE"
            )
            all_project_blueprints = response.get("blueprints", [])

            # Filter out AWS standard blueprints
            custom_only = [
                bp
                for bp in all_project_blueprints
                if "aws:blueprint" not in bp.get("blueprintArn", "")
            ]

            removed_count = len(all_project_blueprints) - len(custom_only)
            if removed_count > 0:
                logger.info(
                    f"Removing {removed_count} AWS standard blueprints from project"
                )
                self.blueprint_creator.update_project_with_custom_configurations(
                    self.dataAutomationProjectArn,
                    customConfiguration={"blueprints": custom_only},
                )
            else:
                logger.debug("No AWS standard blueprints to remove from project")

        except Exception as e:
            logger.warning(
                f"Failed to remove AWS standard blueprints from project: {e}"
            )

    def _synchronize_deletes(self, existing_blueprints, blueprints_updated):
        # remove all blueprints which are not in custom class
        blueprints_to_delete = []
        blueprints_arn_to_delete = []
        for blueprint in existing_blueprints:
            blueprint_name = blueprint.get("blueprintName", "")
            blueprint_arn = blueprint.get("blueprintArn", "")
            if blueprint_arn in blueprints_updated:
                continue
            if blueprint_name.startswith(self.blueprint_name_prefix):
                # delete detected - remove the blueprint
                blueprints_to_delete.append(blueprint)
                blueprints_arn_to_delete.append(blueprint_arn)

        if len(blueprints_to_delete) > 0:
            # remove the blueprints marked for deletion for the project first before deleting them.
            response = self.blueprint_creator.list_blueprints(
                self.dataAutomationProjectArn, "LIVE"
            )
            custom_configurations = response.get("blueprints", [])
            new_custom_configurations = []
            for custom_blueprint in custom_configurations:
                if custom_blueprint.get("blueprintArn") not in blueprints_arn_to_delete:
                    new_custom_configurations.append(custom_blueprint)
            new_custom_configurations = {"blueprints": new_custom_configurations}
            self.blueprint_creator.update_project_with_custom_configurations(
                self.dataAutomationProjectArn,
                customConfiguration=new_custom_configurations,
            )

            try:
                for _blueprint_delete in blueprints_to_delete:
                    self.blueprint_creator.delete_blueprint(
                        _blueprint_delete.get("blueprintArn"),
                        _blueprint_delete.get("blueprintVersion"),
                    )
            except Exception as e:
                logger.error(f"Error deleting blueprint with version: {e}")
