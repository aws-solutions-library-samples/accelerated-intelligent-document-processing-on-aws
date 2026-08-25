# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the BdaBlueprintService class.
"""

# ruff: noqa: E402, I001
# The above line disables E402 (module level import not at top of file) and I001 (import block sorting) for this file

import pytest

# Import standard library modules first
import json
import re
from copy import deepcopy
from unittest.mock import MagicMock, patch

# Import third-party modules
from botocore.exceptions import ClientError

# Import application modules
from idp_common.bda.bda_blueprint_service import BdaBlueprintService


def build_json_schema(
    doc_id="W-4",
    description="Employee's Withholding Certificate form",
    properties=None,
    defs=None,
):
    """Helper to construct JSON Schema documents for tests."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": doc_id,
        "x-aws-idp-document-type": doc_id,
        "description": description,
        "type": "object",
    }

    if properties is not None:
        schema["properties"] = properties
    if defs is not None:
        schema["$defs"] = defs

    return schema


@pytest.mark.unit
class TestBdaBlueprintService:
    """Tests for the BdaBlueprintService class."""

    @pytest.fixture
    def mock_custom_configuration(self):
        """Fixture providing mock custom configuration data."""
        w4_properties = {
            "personalInformation": {
                "type": "object",
                "description": "Personal information of employee",
                "properties": {
                    "firstName": {
                        "type": "string",
                        "description": "First Name of Employee",
                    },
                    "lastName": {
                        "type": "string",
                        "description": "Last Name of Employee",
                    },
                },
            }
        }

        i9_properties = {
            "employeeInfo": {
                "type": "object",
                "description": "Employee information section",
                "properties": {
                    "fullName": {
                        "type": "string",
                        "description": "Employee full name",
                    }
                },
            }
        }

        return {
            "Configuration": "Custom",
            "classes": [
                build_json_schema(
                    doc_id="W-4",
                    description="Employee's Withholding Certificate form",
                    properties=w4_properties,
                ),
                {
                    **build_json_schema(
                        doc_id="I-9",
                        description="Employment Eligibility Verification",
                        properties=i9_properties,
                    ),
                    "blueprint_arn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/existing-i9-blueprint",
                },
            ],
        }

    @pytest.fixture
    def mock_blueprint_schema(self):
        """Fixture providing mock blueprint schema."""
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "description": "Employee's Withholding Certificate form",
            "class": "W-4",
            "type": "object",
            "definitions": {
                "PersonalInformation": {
                    "type": "object",
                    "properties": {
                        "firstname": {
                            "type": "string",
                            "instruction": "First Name of Employee",
                        },
                        "lastname": {
                            "type": "string",
                            "instruction": "Last Name of Employee",
                        },
                    },
                }
            },
            "properties": {
                "PersonalInformation": {"$ref": "#/definitions/PersonalInformation"}
            },
        }

    @pytest.fixture
    def mock_blueprint_response(self):
        """Fixture providing mock blueprint creation response."""
        return {
            "status": "success",
            "blueprint": {
                "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/w4-12345678",
                "blueprintName": "W-4-12345678",
                "blueprintStage": "LIVE",
                "blueprintVersion": "1",
            },
        }

    @pytest.fixture
    def service(self):
        """Fixture providing a BdaBlueprintService instance with mocked dependencies."""
        with (
            patch("boto3.resource") as mock_dynamodb,
            patch("boto3.client") as mock_boto_client,
            patch(
                "idp_common.bda.bda_blueprint_service.ConfigurationManager"
            ) as mock_config_manager,
            patch.dict("os.environ", {"CONFIGURATION_TABLE_NAME": "test-config-table"}),
        ):
            # Mock DynamoDB table
            mock_table = MagicMock()
            mock_dynamodb.return_value.Table.return_value = mock_table

            # Mock boto3 client for BDABlueprintCreator
            mock_bedrock_client = MagicMock()
            mock_boto_client.return_value = mock_bedrock_client

            # Mock configuration manager
            mock_manager_instance = mock_config_manager.return_value
            mock_manager_instance.get_configuration.return_value = None
            mock_manager_instance.handle_update_custom_configuration.return_value = True

            service = BdaBlueprintService(
                dataAutomationProjectArn="arn:aws:bedrock:us-west-2:123456789012:project/test-project"
            )

            # Store mocks for access in tests
            # Note: Using setattr to avoid type checker issues with dynamic attributes
            setattr(service, "_mock_table", mock_table)
            service.config_manager = mock_manager_instance

            # Replace the blueprint_creator with a mock
            mock_blueprint_creator = MagicMock()
            service.blueprint_creator = mock_blueprint_creator
            setattr(service, "_mock_blueprint_creator", mock_blueprint_creator)

            return service

    def test_init(self):
        """Test initialization of BdaBlueprintService."""
        with (
            patch("boto3.resource") as mock_dynamodb,
            patch("boto3.client") as mock_boto_client,
            patch.dict(
                "os.environ",
                {
                    "CONFIGURATION_TABLE_NAME": "test-config-table",
                    "AWS_REGION": "us-east-1",
                },
            ),
        ):
            service = BdaBlueprintService(
                dataAutomationProjectArn="arn:aws:bedrock:us-west-2:123456789012:project/test-project"
            )

            assert (
                service.dataAutomationProjectArn
                == "arn:aws:bedrock:us-west-2:123456789012:project/test-project"
            )

            # Verify boto3 client was called for BDABlueprintCreator
            mock_boto_client.assert_called_with(service_name="bedrock-data-automation")

            # Verify DynamoDB table was set up
            mock_dynamodb.assert_called_once_with("dynamodb")

    def test_create_blueprints_from_custom_configuration_no_config(self, service):
        """Test handling when no custom configuration exists."""
        # Mock empty configuration retrieval
        config_obj = MagicMock()
        config_obj.classes = []
        service.config_manager.get_configuration.return_value = config_obj

        # Mock empty blueprints list
        service._retrieve_all_blueprints = MagicMock(return_value=[])

        # Mock AWS standard conversion
        service._convert_aws_standard_blueprints_to_custom = MagicMock(
            return_value={
                "status": "success",
                "converted_count": 0,
                "conversion_details": [],
            }
        )

        # This should not raise an exception but should handle empty classes gracefully
        # Test with default bidirectional sync - returns empty list
        result = service.create_blueprints_from_custom_configuration("test-version")

        # Should complete without processing any classes - returns empty list
        assert isinstance(result, list)
        assert len(result) == 0
        service.blueprint_creator.create_blueprint.assert_not_called()
        service.blueprint_creator.update_blueprint.assert_not_called()

        # Test with explicit sync directions - all return empty list
        for direction in ["bda_to_idp", "idp_to_bda", "bidirectional"]:
            result = service.create_blueprints_from_custom_configuration(
                version="test-version", sync_direction=direction
            )
            assert isinstance(result, list)
            assert len(result) == 0  # No classes processed

    def test_create_blueprints_from_custom_configuration_no_classes_key(self, service):
        """Test handling when configuration has no 'classes' key."""
        # Mock configuration without classes key
        service.config_manager.get_configuration.return_value = {
            "Configuration": "Custom"
        }

        # Should handle missing classes key gracefully - returns empty list
        result = service.create_blueprints_from_custom_configuration("test-version")

        assert isinstance(result, list)
        assert len(result) == 0
        service.blueprint_creator.create_blueprint.assert_not_called()

    def test_create_blueprints_from_custom_configuration_dynamodb_error(self, service):
        """Test handling of DynamoDB error during configuration retrieval."""
        # Mock DynamoDB error
        error_response = {
            "Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}
        }
        service.config_manager.get_configuration.side_effect = ClientError(
            error_response, "GetItem"
        )

        # Should raise exception on DynamoDB error
        with pytest.raises(Exception, match="Failed to process blueprint creation"):
            service.create_blueprints_from_custom_configuration("test-version")

    def test_create_blueprints_invalid_sync_direction(self, service):
        """Test handling of invalid sync direction parameter."""
        # Mock configuration
        config_obj = MagicMock()
        config_obj.classes = [build_json_schema(doc_id="W-4")]
        service.config_manager.get_configuration.return_value = config_obj

        # Should raise Exception (not ValueError) for invalid direction
        with pytest.raises(Exception, match="Invalid sync_direction"):
            service.create_blueprints_from_custom_configuration(
                version="test-version", sync_direction="invalid_direction"
            )

    def test_create_blueprints_from_custom_configuration_partial_failure(
        self, service, mock_custom_configuration
    ):
        """Test handling when one blueprint succeeds and another fails."""
        # Mock configuration retrieval - wrap dict in object with classes attribute
        config_obj = MagicMock()
        config_obj.classes = mock_custom_configuration["classes"]
        service.config_manager.get_configuration.return_value = config_obj

        # Mock first blueprint creation success, second failure
        success_response = {
            "status": "success",
            "blueprint": {
                "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/w4-12345678",
                "blueprintName": "W-4-12345678",
            },
        }

        service.blueprint_creator.create_blueprint.return_value = success_response
        service.blueprint_creator.update_blueprint.side_effect = Exception(
            "Update failed"
        )
        service.blueprint_creator.create_blueprint_version.return_value = (
            success_response
        )

        # Mock blueprints and AWS standard conversion
        service._retrieve_all_blueprints = MagicMock(return_value=[])
        service._convert_aws_standard_blueprints_to_custom = MagicMock(
            return_value={
                "status": "success",
                "converted_count": 0,
                "conversion_details": [],
            }
        )

        # Mock _blueprint_lookup to return None (no existing blueprints)
        service._blueprint_lookup = MagicMock(return_value=None)

        # Should continue processing despite individual failures
        # The method should complete and return status for all classes
        result = service.create_blueprints_from_custom_configuration("test-version")

        # Verify result is a list
        assert isinstance(result, list)

        # Should have processed both classes (W-4 and I-9)
        assert len(result) == 2

    def test_check_for_updates_no_changes(self, service):
        """Test _check_for_updates when no changes are detected."""
        properties = {
            "personalInformation": {
                "type": "object",
                "description": "Personal info",
                "properties": {
                    "firstName": {
                        "type": "string",
                        "description": "First Name of Employee",
                    },
                    "lastName": {
                        "type": "string",
                        "description": "Last Name of Employee",
                    },
                },
            }
        }
        custom_class = build_json_schema(properties=properties)
        blueprint_schema = service._transform_json_schema_to_bedrock_blueprint(
            custom_class
        )
        existing_blueprint = {"schema": json.dumps(blueprint_schema)}

        assert service._check_for_updates(custom_class, existing_blueprint) is False

    def test_check_for_updates_class_id_changed(self, service):
        """Test _check_for_updates when the document class identifier changes."""
        base_schema = build_json_schema()
        blueprint_schema = service._transform_json_schema_to_bedrock_blueprint(
            base_schema
        )
        existing_blueprint = {"schema": json.dumps(blueprint_schema)}

        updated_schema = deepcopy(base_schema)
        updated_schema["$id"] = "W-4-Updated"
        updated_schema["x-aws-idp-document-type"] = "W-4-Updated"

        assert service._check_for_updates(updated_schema, existing_blueprint) is True

    def test_check_for_updates_description_changed(self, service):
        """Test _check_for_updates when the schema description change is detected."""
        base_schema = build_json_schema()
        blueprint_schema = service._transform_json_schema_to_bedrock_blueprint(
            base_schema
        )
        existing_blueprint = {"schema": json.dumps(blueprint_schema)}

        updated_schema = deepcopy(base_schema)
        updated_schema["description"] = (
            "Updated Employee's Withholding Certificate form"
        )

        assert service._check_for_updates(updated_schema, existing_blueprint) is True

    def test_check_for_updates_new_property_added(self, service):
        """Test _check_for_updates when a new top-level property is added."""
        base_schema = build_json_schema(properties={"foo": {"type": "string"}})
        blueprint_schema = service._transform_json_schema_to_bedrock_blueprint(
            base_schema
        )
        existing_blueprint = {"schema": json.dumps(blueprint_schema)}

        updated_schema = deepcopy(base_schema)
        # Type annotation to help type checker understand this is a dict
        updated_schema_dict = updated_schema  # type: dict
        updated_schema_dict["properties"]["bar"] = {
            "type": "string",
            "description": "New field",
        }

        assert (
            service._check_for_updates(updated_schema_dict, existing_blueprint) is True
        )

    def test_check_for_updates_nested_description_changed(self, service):
        """Test _check_for_updates when a nested description changes."""
        base_schema = build_json_schema(
            properties={
                "personalInformation": {
                    "type": "object",
                    "description": "Personal info",
                    "properties": {
                        "firstName": {
                            "type": "string",
                            "description": "First Name of Employee",
                        }
                    },
                }
            }
        )
        blueprint_schema = service._transform_json_schema_to_bedrock_blueprint(
            base_schema
        )
        existing_blueprint = {"schema": json.dumps(blueprint_schema)}

        updated_schema = deepcopy(base_schema)
        # Type annotation to help type checker understand this is a dict
        updated_schema_dict = updated_schema  # type: dict
        updated_schema_dict["properties"]["personalInformation"]["properties"][
            "firstName"
        ]["description"] = "Updated first name description"

        assert (
            service._check_for_updates(updated_schema_dict, existing_blueprint) is True
        )

    def test_check_for_updates_nested_property_added(self, service):
        """Test _check_for_updates when a nested property is added."""
        base_schema = build_json_schema(
            properties={
                "personalInformation": {
                    "type": "object",
                    "description": "Personal info",
                    "properties": {
                        "firstName": {
                            "type": "string",
                            "description": "First Name of Employee",
                        }
                    },
                }
            }
        )
        blueprint_schema = service._transform_json_schema_to_bedrock_blueprint(
            base_schema
        )
        existing_blueprint = {"schema": json.dumps(blueprint_schema)}

        updated_schema = deepcopy(base_schema)
        # Type annotation to help type checker understand this is a dict
        updated_schema_dict = updated_schema  # type: dict
        updated_schema_dict["properties"]["personalInformation"]["properties"][
            "middleName"
        ] = {
            "type": "string",
            "description": "Middle Name of Employee",
        }

        assert (
            service._check_for_updates(updated_schema_dict, existing_blueprint) is True
        )

    def test_check_for_updates_blueprint_retrieval_error(self, service):
        """Test _check_for_updates when blueprint has invalid schema."""
        custom_class = build_json_schema()

        # Invalid blueprint with malformed schema
        invalid_blueprint = {"schema": "invalid json"}

        # Should raise the exception
        with pytest.raises(json.JSONDecodeError):
            service._check_for_updates(custom_class, invalid_blueprint)

    def test_check_for_updates_empty_properties(self, service):
        """Test _check_for_updates with empty properties."""
        custom_class = build_json_schema(
            properties={}, description="Employee's Withholding Certificate form"
        )
        blueprint_schema = service._transform_json_schema_to_bedrock_blueprint(
            custom_class
        )
        existing_blueprint = {"schema": json.dumps(blueprint_schema)}

        assert service._check_for_updates(custom_class, existing_blueprint) is False

    def test_check_for_updates_without_properties_key(self, service):
        """Test _check_for_updates when properties key is absent."""
        custom_class = build_json_schema()
        custom_class.pop("properties", None)
        blueprint_schema = service._transform_json_schema_to_bedrock_blueprint(
            build_json_schema()
        )
        existing_blueprint = {"schema": json.dumps(blueprint_schema)}

        assert service._check_for_updates(custom_class, existing_blueprint) is False

    def test_transform_does_not_mutate_input_schema(self, service):
        """_transform_json_schema_to_bedrock_blueprint should not mutate the original schema."""
        schema = build_json_schema(
            doc_id="Invoice",
            description="Invoice schema",
            properties={
                "invoiceNumber": {
                    "type": "string",
                    "description": "Unique invoice identifier",
                }
            },
        )

        original = deepcopy(schema)

        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)

        # Original schema should remain untouched
        assert schema == original
        # Blueprint should contain Bedrock fields
        assert (
            blueprint["properties"]["invoiceNumber"]["instruction"]
            == "Unique invoice identifier"
        )
        assert blueprint["properties"]["invoiceNumber"]["inferenceType"] == "explicit"

    def test_transform_converts_defs_to_definitions(self, service):
        """Ensure that $defs is converted to definitions for BDA draft-07 compatibility.

        BDA uses JSON Schema draft-07 which uses "definitions", not "$defs".
        References should be preserved but updated to #/definitions/ path.
        """
        schema = build_json_schema(
            doc_id="Document",
            description="Document schema",
            properties={
                "address": {
                    "$ref": "#/$defs/Address",
                }
            },
            defs={
                "Address": {
                    "type": "object",
                    "description": "Address information",
                    "properties": {
                        "street": {
                            "type": "string",
                            "description": "Street line",
                        },
                        "city": {
                            "type": "string",
                            "description": "City name",
                        },
                    },
                }
            },
        )

        blueprint = service._transform_json_schema_to_bedrock_blueprint(schema)

        # Verify $defs was converted to definitions
        assert "definitions" in blueprint
        assert "$defs" not in blueprint
        assert "Address" in blueprint["definitions"]

        # Verify $ref path was updated
        address_prop = blueprint["properties"]["address"]
        assert address_prop["$ref"] == "#/definitions/Address"
        assert (
            address_prop["instruction"] == "-"
        )  # $ref properties should have instruction: "-"

        # Verify definition has proper structure (object should NOT have inferenceType/instruction)
        address_def = blueprint["definitions"]["Address"]
        assert address_def["type"] == "object"
        assert "inferenceType" not in address_def  # Objects should not have this
        assert "instruction" not in address_def  # Objects should not have this
        assert "properties" in address_def

        # Verify leaf properties DO have BDA fields
        street_prop = address_def["properties"]["street"]
        assert street_prop["type"] == "string"
        assert "inferenceType" in street_prop
        assert "instruction" in street_prop
        assert street_prop["instruction"] == "Street line"

        city_prop = address_def["properties"]["city"]
        assert city_prop["type"] == "string"
        assert "inferenceType" in city_prop
        assert "instruction" in city_prop

    def test_create_blueprints_creates_idp_classes_from_orphaned_bda_blueprints(
        self, service
    ):
        """Test that orphaned blueprint sync is disabled (commented out).

        NOTE: The orphaned blueprint sync feature (creating IDP classes from BDA blueprints
        without corresponding IDP classes) has been commented out for optimization.
        This test now verifies that only existing IDP classes are processed.
        """
        # Mock existing configuration with only one IDP class
        existing_idp_classes = [
            build_json_schema(
                doc_id="W-4",
                description="Employee's Withholding Certificate form",
                properties={
                    "personalInformation": {
                        "type": "object",
                        "description": "Personal information of employee",
                        "properties": {
                            "firstName": {
                                "type": "string",
                                "description": "First Name of Employee",
                            },
                            "lastName": {
                                "type": "string",
                                "description": "Last Name of Employee",
                            },
                        },
                    }
                },
            )
        ]

        # Mock configuration manager to return existing classes
        config_obj = MagicMock()
        config_obj.classes = existing_idp_classes
        service.config_manager.get_configuration.return_value = config_obj

        # Mock existing BDA blueprints - some have corresponding IDP classes, some don't
        existing_bda_blueprints = [
            # This blueprint has a corresponding IDP class (W-4)
            {
                "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/test-stack-W-4-12345678",
                "blueprintName": "test-stack-W-4-12345678",
                "blueprintVersion": "1",
                "schema": json.dumps(
                    {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "class": "W-4",
                        "description": "Employee's Withholding Certificate form",
                        "type": "object",
                        "definitions": {
                            "PersonalInformation": {
                                "type": "object",
                                "properties": {
                                    "firstName": {
                                        "type": "string",
                                        "inferenceType": "explicit",
                                        "instruction": "First Name of Employee",
                                    },
                                    "lastName": {
                                        "type": "string",
                                        "inferenceType": "explicit",
                                        "instruction": "Last Name of Employee",
                                    },
                                },
                            }
                        },
                        "properties": {
                            "personalInformation": {
                                "$ref": "#/definitions/PersonalInformation"
                            }
                        },
                    }
                ),
            },
            # This blueprint does NOT have a corresponding IDP class (I-9)
            {
                "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/test-stack-I-9-87654321",
                "blueprintName": "test-stack-I-9-87654321",
                "blueprintVersion": "1",
                "schema": json.dumps(
                    {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "class": "I-9",
                        "description": "Employment Eligibility Verification",
                        "type": "object",
                        "definitions": {
                            "EmployeeInfo": {
                                "type": "object",
                                "properties": {
                                    "fullName": {
                                        "type": "string",
                                        "inferenceType": "explicit",
                                        "instruction": "Employee full name",
                                    },
                                    "dateOfBirth": {
                                        "type": "string",
                                        "inferenceType": "explicit",
                                        "instruction": "Employee date of birth",
                                    },
                                },
                            }
                        },
                        "properties": {
                            "employeeInfo": {"$ref": "#/definitions/EmployeeInfo"}
                        },
                    }
                ),
            },
            # This blueprint also does NOT have a corresponding IDP class (1099)
            {
                "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/test-stack-1099-11111111",
                "blueprintName": "test-stack-1099-11111111",
                "blueprintVersion": "1",
                "schema": json.dumps(
                    {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "class": "1099",
                        "description": "Miscellaneous Income Tax Form",
                        "type": "object",
                        "properties": {
                            "payerInfo": {
                                "type": "string",
                                "inferenceType": "explicit",
                                "instruction": "Payer information",
                            },
                            "recipientInfo": {
                                "type": "string",
                                "inferenceType": "explicit",
                                "instruction": "Recipient information",
                            },
                        },
                    }
                ),
            },
        ]

        # Mock the _retrieve_all_blueprints method
        service._retrieve_all_blueprints = MagicMock(
            return_value=existing_bda_blueprints
        )

        # Mock convert_aws_standard_blueprints_to_custom to return no conversions
        service._convert_aws_standard_blueprints_to_custom = MagicMock(
            return_value={
                "status": "success",
                "converted_count": 0,
                "conversion_details": [],
            }
        )

        # Mock blueprint operations for the existing W-4 class
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "success",
            "blueprint": {
                "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/test-stack-W-4-12345678",
                "blueprintName": "test-stack-W-4-12345678",
            },
        }
        service.blueprint_creator.create_blueprint_version.return_value = {
            "status": "success"
        }
        service.blueprint_creator.update_blueprint.return_value = {"status": "success"}

        # Mock the _check_for_updates to return False (no updates needed)
        service._check_for_updates = MagicMock(return_value=False)

        # Mock the _blueprint_lookup method
        def mock_blueprint_lookup(existing_blueprints, doc_class):
            for blueprint in existing_blueprints:
                blueprint_name = blueprint.get("blueprintName", "")
                if doc_class in blueprint_name:
                    return blueprint
            return None

        service._blueprint_lookup = MagicMock(side_effect=mock_blueprint_lookup)

        # Execute the method with bidirectional sync (default)
        result = service.create_blueprints_from_custom_configuration("test-version")

        # Verify the result - only the existing W-4 class should be processed
        # Orphaned blueprints (I-9, 1099) are NOT synced because that code is commented out
        assert isinstance(result, list)
        assert len(result) == 1  # Only W-4 processed (orphaned sync disabled)

        # Verify only W-4 was processed - check only status and class fields
        assert result[0]["class"] == "W-4"
        assert result[0]["status"] == "success"

        # Verify that handle_update_custom_configuration was NOT called
        # (no new classes added since orphaned sync is disabled)
        service.config_manager.handle_update_custom_configuration.assert_not_called()

    def test_sync_direction_bda_to_idp_only(self, service):
        """Test that version="test-version", sync_direction='bda_to_idp' only syncs from BDA to IDP."""
        # Mock configuration
        config_obj = MagicMock()
        config_obj.classes = [build_json_schema(doc_id="W-4")]
        service.config_manager.get_configuration.return_value = config_obj

        # Mock blueprints
        service._retrieve_all_blueprints = MagicMock(return_value=[])
        service._convert_aws_standard_blueprints_to_custom = MagicMock(
            return_value={
                "status": "success",
                "converted_count": 0,
                "conversion_details": [],
            }
        )

        # Execute with bda_to_idp direction
        service.create_blueprints_from_custom_configuration(
            version="test-version", sync_direction="bda_to_idp"
        )

        # Should not create or update blueprints (Phase 2 skipped)
        service.blueprint_creator.create_blueprint.assert_not_called()
        service.blueprint_creator.update_blueprint.assert_not_called()

    def test_sync_direction_idp_to_bda_only(self, service):
        """Test that version="test-version", sync_direction='idp_to_bda' only syncs from IDP to BDA."""
        # Mock configuration
        config_obj = MagicMock()
        config_obj.classes = [build_json_schema(doc_id="W-4")]
        service.config_manager.get_configuration.return_value = config_obj

        # Mock blueprints
        service._retrieve_all_blueprints = MagicMock(return_value=[])
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "success",
            "blueprint": {
                "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/new-w4",
                "blueprintName": "new-w4",
            },
        }
        service.blueprint_creator.create_blueprint_version.return_value = {
            "status": "success"
        }

        # Execute with idp_to_bda direction
        service.create_blueprints_from_custom_configuration(
            version="test-version", sync_direction="idp_to_bda"
        )

        # Should not call convert_aws_standard_blueprints_to_custom (Phase 1 skipped)
        # But should create blueprints (Phase 2 executed)
        service.blueprint_creator.create_blueprint.assert_called_once()

    def test_create_blueprints_no_orphaned_blueprints(self, service):
        """Test that no IDP classes are created when all BDA blueprints have corresponding IDP classes."""
        # Mock existing configuration with IDP classes that match all blueprints
        existing_idp_classes = [
            build_json_schema(doc_id="W-4", description="W-4 form"),
            build_json_schema(doc_id="I-9", description="I-9 form"),
        ]

        config_obj = MagicMock()
        config_obj.classes = existing_idp_classes
        service.config_manager.get_configuration.return_value = config_obj

        # Mock existing BDA blueprints that all have corresponding IDP classes
        existing_bda_blueprints = [
            {
                "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/test-stack-W-4-12345678",
                "blueprintName": "test-stack-W-4-12345678",
                "blueprintVersion": "1",
                "schema": json.dumps({"class": "W-4", "type": "object"}),
            },
            {
                "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/test-stack-I-9-87654321",
                "blueprintName": "test-stack-I-9-87654321",
                "blueprintVersion": "1",
                "schema": json.dumps({"class": "I-9", "type": "object"}),
            },
        ]

        service._retrieve_all_blueprints = MagicMock(
            return_value=existing_bda_blueprints
        )
        service._check_for_updates = MagicMock(return_value=False)

        # Mock AWS standard conversion
        service._convert_aws_standard_blueprints_to_custom = MagicMock(
            return_value={
                "status": "success",
                "converted_count": 0,
                "conversion_details": [],
            }
        )

        # Mock blueprint operations
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "success",
            "blueprint": {
                "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/new-w4",
                "blueprintName": "new-w4",
            },
        }
        service.blueprint_creator.create_blueprint_version.return_value = {
            "status": "success"
        }

        def mock_blueprint_lookup(existing_blueprints, doc_class):
            for blueprint in existing_blueprints:
                blueprint_name = blueprint.get("blueprintName", "")
                if doc_class in blueprint_name:
                    return blueprint
            return None

        service._blueprint_lookup = MagicMock(side_effect=mock_blueprint_lookup)

        # Execute the method
        result = service.create_blueprints_from_custom_configuration("test-version")

        # Verify result is a list
        assert isinstance(result, list)

        # Should have processed 2 classes (W-4 and I-9)
        assert len(result) == 2

    def test_create_blueprints_empty_existing_blueprints(self, service):
        """Test behavior when no BDA blueprints exist in the project."""
        # Mock existing configuration with IDP classes
        existing_idp_classes = [
            build_json_schema(doc_id="W-4", description="W-4 form"),
        ]

        config_obj = MagicMock()
        config_obj.classes = existing_idp_classes
        service.config_manager.get_configuration.return_value = config_obj

        # Mock empty blueprints list
        service._retrieve_all_blueprints = MagicMock(return_value=[])

        # Mock AWS standard conversion
        service._convert_aws_standard_blueprints_to_custom = MagicMock(
            return_value={
                "status": "success",
                "converted_count": 0,
                "conversion_details": [],
            }
        )

        # Mock blueprint operations
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "success",
            "blueprint": {
                "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/new-w4",
                "blueprintName": "new-w4",
            },
        }
        service.blueprint_creator.create_blueprint_version.return_value = {
            "status": "success"
        }

        # Execute the method
        result = service.create_blueprints_from_custom_configuration("test-version")

        # Verify result is a list
        assert isinstance(result, list)

        # Should have processed 1 class (W-4)
        assert len(result) == 1
        assert result[0]["class"] == "W-4"
        assert result[0]["status"] == "success"

    def test_convert_aws_standard_blueprints_to_custom_success(self, service):
        """Test successful conversion of AWS standard blueprints to custom blueprints."""
        # Mock existing configuration
        config_obj = MagicMock()
        config_obj.classes = []
        service.config_manager.get_configuration.return_value = config_obj

        # Mock AWS standard blueprints
        aws_standard_blueprints = [
            {
                "blueprintArn": "arn:aws:bedrock:us-east-1:aws:blueprint/invoice-standard",
                "blueprintName": "Invoice-Standard",
                "blueprintVersion": "1",
                "schema": json.dumps(
                    {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "class": "Invoice",
                        "description": "Standard invoice blueprint",
                        "type": "object",
                        "properties": {
                            "invoiceNumber": {
                                "type": "string",
                                "inferenceType": "explicit",
                                "instruction": "Invoice number",
                            }
                        },
                    }
                ),
            },
            {
                "blueprintArn": "arn:aws:bedrock:us-east-1:aws:blueprint/receipt-standard",
                "blueprintName": "Receipt-Standard",
                "blueprintVersion": "1",
                "schema": json.dumps(
                    {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "class": "Receipt",
                        "description": "Standard receipt blueprint",
                        "type": "object",
                        "properties": {
                            "receiptNumber": {
                                "type": "string",
                                "inferenceType": "explicit",
                                "instruction": "Receipt number",
                            }
                        },
                    }
                ),
            },
        ]

        # Mock _retrieve_all_blueprints to return AWS standard blueprints
        service._retrieve_all_blueprints = MagicMock(
            return_value=aws_standard_blueprints
        )

        # Mock blueprint creation
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "success",
            "blueprint": {
                "blueprintArn": "arn:aws:bedrock:us-east-1:123456789012:blueprint/custom-invoice",
                "blueprintName": "custom-invoice",
            },
        }
        service.blueprint_creator.create_blueprint_version.return_value = {
            "status": "success"
        }

        # Mock list_blueprints and update_project_with_custom_configurations
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": aws_standard_blueprints
        }
        service.blueprint_creator.update_project_with_custom_configurations.return_value = {
            "status": "success"
        }

        # Execute the method
        result = service._convert_aws_standard_blueprints_to_custom("test-version")

        # Verify the result
        assert result["status"] == "success"
        assert result["converted_count"] == 2
        assert len(result["conversion_details"]) == 2

        # Verify blueprint creation was called for each AWS standard blueprint
        assert service.blueprint_creator.create_blueprint.call_count == 2

        # Verify project was updated to remove AWS standard blueprints
        service.blueprint_creator.update_project_with_custom_configurations.assert_called_once()

        # Verify configuration was updated with new IDP classes
        service.config_manager.handle_update_custom_configuration.assert_called_once()
        call_args = service.config_manager.handle_update_custom_configuration.call_args
        # Access keyword arguments
        updated_classes = call_args.kwargs["custom_config"]["classes"]

        # Should have 2 new IDP classes
        assert len(updated_classes) == 2
        assert any(cls.get("$id") == "Invoice" for cls in updated_classes)
        assert any(cls.get("$id") == "Receipt" for cls in updated_classes)

    def test_convert_aws_standard_blueprints_no_aws_blueprints(self, service):
        """Test conversion when no AWS standard blueprints exist."""
        # Mock existing configuration
        config_obj = MagicMock()
        config_obj.classes = []
        service.config_manager.get_configuration.return_value = config_obj

        # Mock custom blueprints only (no AWS standard)
        custom_blueprints = [
            {
                "blueprintArn": "arn:aws:bedrock:us-east-1:123456789012:blueprint/custom-w4",
                "blueprintName": "custom-w4",
                "blueprintVersion": "1",
                "schema": json.dumps({"class": "W-4", "type": "object"}),
            }
        ]

        service._retrieve_all_blueprints = MagicMock(return_value=custom_blueprints)

        # Mock blueprint creation to fail (since these aren't AWS standard blueprints)
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "failed",
            "error": "Not an AWS standard blueprint",
        }

        # Execute the method
        result = service._convert_aws_standard_blueprints_to_custom("test-version")

        # Verify the method completes but reports failures for non-AWS blueprints
        assert result["status"] == "success"
        assert result["converted_count"] == 0
        assert len(result["conversion_details"]) == 1
        assert result["conversion_details"][0]["status"] == "failed"

        # Verify no project updates were attempted
        service.blueprint_creator.update_project_with_custom_configurations.assert_not_called()

    def test_convert_aws_standard_blueprints_partial_failure(self, service):
        """Test conversion when some blueprints fail to convert."""
        # Mock existing configuration
        config_obj = MagicMock()
        config_obj.classes = []
        service.config_manager.get_configuration.return_value = config_obj

        # Mock AWS standard blueprints
        aws_standard_blueprints = [
            {
                "blueprintArn": "arn:aws:bedrock:us-east-1:aws:blueprint/invoice-standard",
                "blueprintName": "Invoice-Standard",
                "blueprintVersion": "1",
                "schema": json.dumps(
                    {
                        "class": "Invoice",
                        "type": "object",
                        "properties": {
                            "invoiceNumber": {
                                "type": "string",
                                "instruction": "Invoice number",
                            }
                        },
                    }
                ),
            },
            {
                "blueprintArn": "arn:aws:bedrock:us-east-1:aws:blueprint/receipt-standard",
                "blueprintName": "Receipt-Standard",
                "blueprintVersion": "1",
                "schema": json.dumps(
                    {
                        "class": "Receipt",
                        "type": "object",
                        "properties": {
                            "receiptNumber": {
                                "type": "string",
                                "instruction": "Receipt number",
                            }
                        },
                    }
                ),
            },
        ]

        service._retrieve_all_blueprints = MagicMock(
            return_value=aws_standard_blueprints
        )

        # Mock first blueprint creation success, second failure
        service.blueprint_creator.create_blueprint.side_effect = [
            {
                "status": "success",
                "blueprint": {
                    "blueprintArn": "arn:aws:bedrock:us-east-1:123456789012:blueprint/custom-invoice",
                    "blueprintName": "custom-invoice",
                },
            },
            Exception("Creation failed"),
        ]
        service.blueprint_creator.create_blueprint_version.return_value = {
            "status": "success"
        }
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": aws_standard_blueprints
        }
        service.blueprint_creator.update_project_with_custom_configurations.return_value = {
            "status": "success"
        }

        # Execute the method
        result = service._convert_aws_standard_blueprints_to_custom("test-version")

        # Verify partial success (method returns success even with failures)
        assert result["status"] == "success"
        assert result["converted_count"] == 1
        assert len(result["conversion_details"]) == 2

        # Verify one success and one failure
        success_details = [
            d for d in result["conversion_details"] if d["status"] == "success"
        ]
        failure_details = [
            d for d in result["conversion_details"] if d["status"] == "failed"
        ]
        assert len(success_details) == 1
        assert len(failure_details) == 1

    def test_convert_aws_standard_blueprints_project_update_failure(self, service):
        """Test conversion when project update fails but creation succeeds."""
        # Mock existing configuration
        config_obj = MagicMock()
        config_obj.classes = []
        service.config_manager.get_configuration.return_value = config_obj

        # Mock AWS standard blueprint
        aws_standard_blueprints = [
            {
                "blueprintArn": "arn:aws:bedrock:us-east-1:aws:blueprint/invoice-standard",
                "blueprintName": "Invoice-Standard",
                "blueprintVersion": "1",
                "schema": json.dumps(
                    {
                        "class": "Invoice",
                        "type": "object",
                        "properties": {
                            "invoiceNumber": {
                                "type": "string",
                                "instruction": "Invoice number",
                            }
                        },
                    }
                ),
            }
        ]

        service._retrieve_all_blueprints = MagicMock(
            return_value=aws_standard_blueprints
        )

        # Mock successful creation but failed project update
        service.blueprint_creator.create_blueprint.return_value = {
            "status": "success",
            "blueprint": {
                "blueprintArn": "arn:aws:bedrock:us-east-1:123456789012:blueprint/custom-invoice",
                "blueprintName": "custom-invoice",
            },
        }
        service.blueprint_creator.create_blueprint_version.return_value = {
            "status": "success"
        }
        service.blueprint_creator.list_blueprints.return_value = {
            "blueprints": aws_standard_blueprints
        }
        service.blueprint_creator.update_project_with_custom_configurations.side_effect = Exception(
            "Update failed"
        )

        # Execute the method
        result = service._convert_aws_standard_blueprints_to_custom("test-version")

        # Verify conversion still marked as success (project update failure is logged but not critical)
        assert result["status"] == "success"
        assert result["converted_count"] == 1

    def test_convert_aws_standard_blueprints_invalid_schema(self, service):
        """Test conversion when AWS standard blueprint has invalid schema."""
        # Mock existing configuration
        config_obj = MagicMock()
        config_obj.classes = []
        service.config_manager.get_configuration.return_value = config_obj

        # Mock AWS standard blueprint with invalid schema
        aws_standard_blueprints = [
            {
                "blueprintArn": "arn:aws:bedrock:us-east-1:aws:blueprint/invalid-standard",
                "blueprintName": "Invalid-Standard",
                "blueprintVersion": "1",
                "schema": "invalid json",
            }
        ]

        service._retrieve_all_blueprints = MagicMock(
            return_value=aws_standard_blueprints
        )

        # Execute the method
        result = service._convert_aws_standard_blueprints_to_custom("test-version")

        # Verify failure was handled (method returns success even with failures)
        assert result["status"] == "success"
        assert result["converted_count"] == 0
        assert len(result["conversion_details"]) == 1
        assert result["conversion_details"][0]["status"] == "failed"
        # Only check for status and class fields (no error_message)
        assert "class" in result["conversion_details"][0]

    def test_normalize_aws_blueprint_schema_adds_missing_types(self, service):
        """Test that normalization adds missing type fields to AWS blueprint schema."""
        # AWS API response format - missing type fields
        aws_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": "US-drivers-licenses",
            "description": "This document is for US driving license from any US state",
            # Missing "type": "object" at root
            "definitions": {
                "Address": {
                    # Missing "type": "object" for definition
                    "properties": {
                        "STREET_ADDRESS": {
                            "type": "string",
                            "inferenceType": "explicit",
                            "instruction": "The street address",
                        }
                    }
                },
                "NameDetails": {
                    # Missing "type": "object" for definition
                    "properties": {
                        "FIRST_NAME": {
                            "type": "string",
                            "inferenceType": "explicit",
                            "instruction": "The first name",
                        }
                    }
                },
            },
            "properties": {
                "NAME_DETAILS": {
                    "$ref": "#/definitions/NameDetails"
                    # Missing "instruction": "-"
                },
                "ADDRESS_DETAILS": {
                    "$ref": "#/definitions/Address"
                    # Missing "instruction": "-"
                },
                "ID_NUMBER": {
                    "type": "string",
                    "inferenceType": "explicit",
                    "instruction": "The unique identification number",
                },
            },
        }

        # Normalize the schema
        normalized = service._normalize_aws_blueprint_schema(aws_schema)

        # Verify root type was added
        assert normalized["type"] == "object"

        # Verify definition types were added
        assert normalized["definitions"]["Address"]["type"] == "object"
        assert normalized["definitions"]["NameDetails"]["type"] == "object"

        # Verify $ref properties got instruction field
        assert normalized["properties"]["NAME_DETAILS"]["instruction"] == "-"
        assert normalized["properties"]["ADDRESS_DETAILS"]["instruction"] == "-"

        # Verify existing fields were not modified
        assert normalized["class"] == "US-drivers-licenses"
        assert (
            normalized["definitions"]["Address"]["properties"]["STREET_ADDRESS"]["type"]
            == "string"
        )

    def test_normalize_aws_blueprint_schema_preserves_existing_types(self, service):
        """Test that normalization preserves existing type fields."""
        # Schema with existing types
        schema_with_types = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": "Document",
            "type": "object",  # Already has type
            "definitions": {
                "Address": {
                    "type": "object",  # Already has type
                    "properties": {
                        "STREET": {"type": "string", "instruction": "Street address"}
                    },
                }
            },
            "properties": {
                "ADDRESS": {
                    "$ref": "#/definitions/Address",
                    "instruction": "-",  # Already has instruction
                }
            },
        }

        # Normalize the schema
        normalized = service._normalize_aws_blueprint_schema(schema_with_types)

        # Verify types were preserved (not duplicated or changed)
        assert normalized["type"] == "object"
        assert normalized["definitions"]["Address"]["type"] == "object"
        assert normalized["properties"]["ADDRESS"]["instruction"] == "-"

    def test_normalize_aws_blueprint_schema_handles_empty_definitions(self, service):
        """Test normalization with no definitions."""
        schema_no_defs = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": "SimpleDocument",
            "properties": {"FIELD": {"type": "string", "instruction": "A field"}},
        }

        # Normalize the schema
        normalized = service._normalize_aws_blueprint_schema(schema_no_defs)

        # Verify root type was added
        assert normalized["type"] == "object"

        # Verify no errors with missing definitions
        assert "definitions" not in normalized or normalized.get("definitions") == {}

    def test_normalize_aws_blueprint_schema_handles_non_object_definitions(
        self, service
    ):
        """Test normalization doesn't add type to definitions without properties."""
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": "Document",
            "definitions": {
                "StringDef": {
                    "type": "string"  # Not an object, should not be modified
                },
                "ObjectDef": {
                    # Has properties, should get type
                    "properties": {"FIELD": {"type": "string"}}
                },
            },
            "properties": {},
        }

        # Normalize the schema
        normalized = service._normalize_aws_blueprint_schema(schema)

        # Verify string definition was not modified
        assert normalized["definitions"]["StringDef"]["type"] == "string"

        # Verify object definition got type added
        assert normalized["definitions"]["ObjectDef"]["type"] == "object"

    def test_transform_with_normalized_aws_schema(self, service):
        """Test full transformation flow with normalized AWS schema."""
        # AWS API response format
        aws_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": "US-drivers-licenses",
            "description": "US driving license",
            "definitions": {
                "Address": {
                    "properties": {
                        "STREET": {
                            "type": "string",
                            "inferenceType": "explicit",
                            "instruction": "Street",
                        }
                    }
                }
            },
            "properties": {
                "ADDRESS": {"$ref": "#/definitions/Address"},
                "ID": {
                    "type": "string",
                    "inferenceType": "explicit",
                    "instruction": "ID",
                },
            },
        }

        # Normalize then transform
        normalized = service._normalize_aws_blueprint_schema(aws_schema)
        idp_schema = service.transform_bda_blueprint_to_idp_class_schema(normalized)

        # Verify IDP schema structure
        assert idp_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert idp_schema["$id"] == "US-drivers-licenses"
        assert idp_schema["type"] == "object"

        # Verify definitions were transformed to $defs
        assert "$defs" in idp_schema
        assert "Address" in idp_schema["$defs"]
        assert idp_schema["$defs"]["Address"]["type"] == "object"

        # Verify properties were transformed
        assert "ADDRESS" in idp_schema["properties"]
        assert idp_schema["properties"]["ADDRESS"]["$ref"] == "#/$defs/Address"

    def test_normalize_handles_dl_blueprint_schema_with_issues(self, service):
        """Test normalization fixes array items and double-escaped quotes."""
        # Create a test schema with the issues that normalization should fix
        schema_with_issues = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": "US-drivers-licenses",
            "description": "Test schema for US drivers licenses",
            "type": "object",
            "definitions": {
                "Address": {
                    # Missing type field - should be added
                    "properties": {
                        "STREET_ADDRESS": {
                            "type": "string",
                            "inferenceType": "explicit",
                            "instruction": "The street address",
                        }
                    }
                },
                "PersonalDetails": {
                    "properties": {
                        "SEX": {
                            "type": "string",
                            "inferenceType": "explicit",
                            "instruction": 'One of [\\"M\\", \\"F\\"]',  # Double-escaped quotes
                        }
                    },
                    "type": "object",
                },
            },
            "properties": {
                "CLASS": {
                    "type": "string",
                    "inferenceType": "explicit",
                    "instruction": 'The single letter class code, one of \\"A\\", \\"B\\" or \\"C\\"',  # Double-escaped
                },
                "RESTRICTIONS": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "inferenceType": "explicit",  # Should be removed
                        "instruction": "Extract this field from the document",  # Should be removed
                    },
                    "instruction": "The restrictions listed",
                },
                "ENDORSEMENTS": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "inferenceType": "explicit",  # Should be removed
                        "instruction": "Extract this field from the document",  # Should be removed
                    },
                    "instruction": "The endorsement codes if any",
                },
                "NAME_DETAILS": {
                    "$ref": "#/definitions/Address"
                    # Missing instruction - should be added
                },
            },
        }

        # Normalize the schema
        normalized = service._normalize_aws_blueprint_schema(schema_with_issues)

        # Write the normalized schema to a file for manual verification
        import os

        output_path = os.path.join(
            os.path.dirname(__file__),
            "../../resources/dl_blueprint_schema_normalized_output.json",
        )
        with open(output_path, "w") as f:
            json.dump(normalized, f, indent=2)
        print(f"\n✅ Normalized schema written to: {output_path}")

        # Verify the key fixes were applied:

        # 1. Array items only have type field (no inferenceType or instruction)
        assert normalized["properties"]["RESTRICTIONS"]["items"] == {"type": "string"}
        assert normalized["properties"]["ENDORSEMENTS"]["items"] == {"type": "string"}

        # 2. Arrays have instruction and inferenceType at the array level
        assert (
            normalized["properties"]["RESTRICTIONS"]["instruction"]
            == "The restrictions listed"
        )
        assert (
            normalized["properties"]["ENDORSEMENTS"]["instruction"]
            == "The endorsement codes if any"
        )
        assert normalized["properties"]["RESTRICTIONS"]["inferenceType"] == "explicit"
        assert normalized["properties"]["ENDORSEMENTS"]["inferenceType"] == "explicit"

        # 3. Definitions have type field added
        assert normalized["definitions"]["Address"]["type"] == "object"

        # 4. Double-escaped quotes are fixed
        assert (
            normalized["properties"]["CLASS"]["instruction"]
            == 'The single letter class code, one of "A", "B" or "C"'
        )
        assert (
            normalized["definitions"]["PersonalDetails"]["properties"]["SEX"][
                "instruction"
            ]
            == 'One of ["M", "F"]'
        )

        # 5. $ref properties have instruction added
        assert normalized["properties"]["NAME_DETAILS"]["instruction"] == "-"

    def test_sanitize_property_names_removes_special_characters(self, service):
        """Test that special characters are removed from property names."""
        # Load test schema with special characters
        import os

        schema_path = os.path.join(
            os.path.dirname(__file__),
            "../../resources/schema_with_special_chars.json",
        )
        with open(schema_path, "r") as f:
            schema = json.load(f)

        # Sanitize the schema
        sanitized_schema, name_mapping = service._sanitize_property_names(
            deepcopy(schema)
        )

        # Verify special characters were removed
        assert "PropertyWithAmpersand" in sanitized_schema["properties"]
        assert "Property&WithAmpersand" not in sanitized_schema["properties"]

        assert "PropertyWithSlash" in sanitized_schema["properties"]
        assert "Property/WithSlash" not in sanitized_schema["properties"]

        assert "PropertyWithAt" in sanitized_schema["properties"]
        assert "Property@WithAt" not in sanitized_schema["properties"]

        assert "PropertyWithSpaces" in sanitized_schema["properties"]
        assert "Property With Spaces" not in sanitized_schema["properties"]

        # Verify nested properties are sanitized
        nested_obj = sanitized_schema["properties"]["NestedObject"]
        assert "NestedProperty" in nested_obj["properties"]
        assert "Nested&Property" not in nested_obj["properties"]
        assert "NestedProperty" in nested_obj["properties"]
        assert "Nested/Property" not in nested_obj["properties"]

        # Verify array item properties are sanitized
        array_items = sanitized_schema["properties"]["ArrayProperty"]["items"]
        assert "ItemField" in array_items["properties"]
        assert "Item&Field" not in array_items["properties"]

        # Verify name mapping was created
        assert "Property&WithAmpersand" in name_mapping
        assert name_mapping["Property&WithAmpersand"] == "PropertyWithAmpersand"
        assert "Property/WithSlash" in name_mapping
        assert name_mapping["Property/WithSlash"] == "PropertyWithSlash"

        # Verify normal properties are unchanged
        assert "Normal_Property" in sanitized_schema["properties"]

    def test_sanitize_property_names_in_idp_to_bda_sync(self, service):
        """Test that property names are sanitized during IDP to BDA synchronization."""
        # Load test schema with special characters
        import os

        schema_path = os.path.join(
            os.path.dirname(__file__),
            "../../resources/schema_with_special_chars.json",
        )
        with open(schema_path, "r") as f:
            schema = json.load(f)

        # Mock configuration with schema containing special characters
        mock_config = MagicMock()
        mock_config.classes = [schema]

        service.config_manager.get_configuration = MagicMock(return_value=mock_config)
        service._retrieve_all_blueprints = MagicMock(return_value=[])
        service.blueprint_creator.create_blueprint = MagicMock(
            return_value={
                "status": "success",
                "blueprint": {
                    "blueprintArn": "arn:aws:bedrock:us-east-1:123456789012:blueprint/test",
                    "blueprintName": "test-blueprint",
                },
            }
        )
        service.blueprint_creator.create_blueprint_version = MagicMock(
            return_value={"status": "success"}
        )

        # Execute synchronization
        service.create_blueprints_from_custom_configuration(
            version="test-version", sync_direction="idp_to_bda"
        )

        # Verify blueprint was created
        assert service.blueprint_creator.create_blueprint.called
        call_args = service.blueprint_creator.create_blueprint.call_args

        # Get the schema that was passed to create_blueprint
        blueprint_schema_str = call_args[1]["schema"]
        blueprint_schema = json.loads(blueprint_schema_str)

        # Verify special characters were removed from property names
        assert "PropertyWithAmpersand" in blueprint_schema["properties"]
        assert "Property&WithAmpersand" not in blueprint_schema["properties"]

        assert "PropertyWithSlash" in blueprint_schema["properties"]
        assert "Property/WithSlash" not in blueprint_schema["properties"]

        # Verify the configuration was updated with sanitized class
        assert service.config_manager.handle_update_custom_configuration.called
        call_args = service.config_manager.handle_update_custom_configuration.call_args
        # Access keyword arguments
        updated_classes = call_args.kwargs["custom_config"]["classes"]

        # Verify the class in configuration has sanitized property names
        assert "PropertyWithAmpersand" in updated_classes[0]["properties"]
        assert "Property&WithAmpersand" not in updated_classes[0]["properties"]

    def test_normalize_real_aws_dl_blueprint_schema(self, service):
        """Test normalization with real AWS driver's license blueprint schema."""
        import os

        # Load the real AWS blueprint schema
        schema_path = os.path.join(
            os.path.dirname(__file__),
            "../../resources/dl_blueprint_schema_with_issues.json",
        )
        with open(schema_path, "r") as f:
            aws_schema = json.load(f)

        # Normalize the schema
        normalized = service._normalize_aws_blueprint_schema(aws_schema)

        # Verify the key fixes were applied:

        # 1. Root type should be added
        assert normalized["type"] == "object"

        # 2. Definitions should have type field added
        assert normalized["definitions"]["Address"]["type"] == "object"
        assert normalized["definitions"]["NameDetails"]["type"] == "object"
        assert normalized["definitions"]["PersonalDetails"]["type"] == "object"

        # 3. $ref properties should have instruction added
        assert normalized["properties"]["NAME_DETAILS"]["instruction"] == "-"
        assert normalized["properties"]["ADDRESS_DETAILS"]["instruction"] == "-"
        assert normalized["properties"]["PERSONAL_DETAILS"]["instruction"] == "-"

        # 4. Array items should only have type field (already correct in AWS schema)
        assert normalized["properties"]["RESTRICTIONS"]["items"] == {"type": "string"}
        assert normalized["properties"]["ENDORSEMENTS"]["items"] == {"type": "string"}

        # 5. Arrays should have instruction and inferenceType at array level (already correct)
        assert "instruction" in normalized["properties"]["RESTRICTIONS"]
        assert "inferenceType" in normalized["properties"]["RESTRICTIONS"]
        assert "instruction" in normalized["properties"]["ENDORSEMENTS"]
        assert "inferenceType" in normalized["properties"]["ENDORSEMENTS"]

        # Write the normalized schema for verification
        output_path = os.path.join(
            os.path.dirname(__file__),
            "../../resources/dl_blueprint_schema_normalized_from_aws.json",
        )
        with open(output_path, "w") as f:
            json.dump(normalized, f, indent=2)
        print(f"\n✅ Normalized AWS schema written to: {output_path}")

        # Verify the schema can be used to create a blueprint
        # Transform to IDP format and back to verify round-trip
        idp_schema = service.transform_bda_blueprint_to_idp_class_schema(normalized)
        assert idp_schema["$id"] == "US-drivers-licenses"
        assert idp_schema["type"] == "object"
        assert "$defs" in idp_schema

        # Transform back to BDA format
        bda_schema = service._transform_json_schema_to_bedrock_blueprint(idp_schema)
        assert bda_schema["class"] == "US-drivers-licenses"
        assert bda_schema["type"] == "object"
        assert "definitions" in bda_schema

        print("✅ Round-trip transformation successful")

    def test_transform_implements_all_normalization_rules(self, service):
        """
        Verify that _transform_json_schema_to_bedrock_blueprint implements
        all the rules from _normalize_aws_blueprint_schema.
        """
        import os

        # Load test IDP schema
        schema_path = os.path.join(
            os.path.dirname(__file__),
            "../../resources/test_idp_schema_for_transform.json",
        )
        with open(schema_path, "r") as f:
            idp_schema = json.load(f)

        # Transform to BDA blueprint
        bda_schema = service._transform_json_schema_to_bedrock_blueprint(idp_schema)

        # Verify Rule 1: $schema field is present (draft-07)
        assert bda_schema["$schema"] == "http://json-schema.org/draft-07/schema#"

        # Verify Rule 2: Root type is present
        assert bda_schema["type"] == "object"

        # Verify Rule 2: Definitions have type field
        assert bda_schema["definitions"]["Address"]["type"] == "object"

        # Verify Rule 3: $ref properties have instruction field
        assert bda_schema["properties"]["address"]["instruction"] == "-"
        assert bda_schema["properties"]["address"]["$ref"] == "#/definitions/Address"

        # Verify Rule 4: Array items only have type field (no inferenceType or instruction)
        assert bda_schema["properties"]["tags"]["items"] == {"type": "string"}
        assert bda_schema["properties"]["scores"]["items"] == {"type": "number"}

        # Verify Rule 4: Arrays have instruction at array level
        assert "instruction" in bda_schema["properties"]["tags"]
        assert "instruction" in bda_schema["properties"]["scores"]

        # Verify Rule 4: Arrays do NOT have inferenceType at array level
        assert "inferenceType" not in bda_schema["properties"]["tags"]
        assert "inferenceType" not in bda_schema["properties"]["scores"]

        # Verify leaf properties have inferenceType and instruction
        assert bda_schema["properties"]["name"]["type"] == "string"
        assert bda_schema["properties"]["name"]["inferenceType"] == "explicit"
        assert "instruction" in bda_schema["properties"]["name"]

        # Verify definitions use "definitions" not "$defs"
        assert "definitions" in bda_schema
        assert "$defs" not in bda_schema

        # Verify $ref paths use "#/definitions/" not "#/$defs/"
        assert "#/definitions/" in bda_schema["properties"]["address"]["$ref"]
        assert "#/$defs/" not in bda_schema["properties"]["address"]["$ref"]

        print("✅ All normalization rules are implemented in transform method")


# ---------------------------------------------------------------------------
# Regression: blueprint names must satisfy BDA's [a-zA-Z0-9-_]+ constraint.
#
# Discovery can emit class ids containing spaces ("Task cards"), which were
# composed into blueprint names verbatim and failed CreateBlueprint with a raw
# ValidationException for every affected class — leaving the config version
# with a BDA project and zero blueprints.
#
# The sanitization has to be applied identically at every site that composes
# or matches a blueprint name (create, lookup, orphan cleanup, optimizer),
# and must be a no-op for ids BDA already accepts — underscores included.
# ---------------------------------------------------------------------------

BDA_BLUEPRINT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


@pytest.mark.unit
class TestBlueprintNameSanitization:
    """Tests for class-id sanitization in composed blueprint names."""

    @pytest.fixture
    def service(self):
        with (
            patch("boto3.resource"),
            patch("boto3.client"),
            patch("idp_common.bda.bda_blueprint_service.ConfigurationManager"),
            patch.dict(
                "os.environ",
                {"CONFIGURATION_TABLE_NAME": "test-config-table", "STACK_NAME": "idp"},
            ),
        ):
            svc = BdaBlueprintService(
                dataAutomationProjectArn="arn:aws:bedrock:us-west-2:123456789012:project/test-project"
            )
            svc.blueprint_creator = MagicMock()
            svc.blueprint_creator.create_blueprint.return_value = {
                "status": "success",
                "blueprint": {
                    "blueprintArn": "arn:aws:bedrock:us-west-2:123456789012:blueprint/bp-1",
                    "blueprintName": "created-name",
                },
            }
            svc.blueprint_creator.create_blueprint_version_without_project_update.return_value = {
                "blueprint": {"blueprintVersion": "1"}
            }
            svc.config_manager = MagicMock()
            return svc

    def _created_name(self, service, class_id):
        """Run the create path for ``class_id`` and return the name BDA got."""
        result = service._process_single_class(
            build_json_schema(doc_id=class_id, properties={"a": {"type": "string"}}),
            existing_blueprints=[],
        )
        assert result["status"] == "success", result
        return service.blueprint_creator.create_blueprint.call_args.kwargs[
            "blueprint_name"
        ]

    def test_class_id_with_space_produces_a_valid_blueprint_name(self, service):
        """The reported failure: 'Task cards' must not reach CreateBlueprint raw."""
        name = self._created_name(service, "Task cards")
        assert BDA_BLUEPRINT_NAME_RE.match(name), name
        assert name.startswith("idp-Task-cards-")

    def test_underscores_are_preserved_so_existing_blueprints_stay_matched(
        self, service
    ):
        """An id BDA already accepts must render byte-identically.

        ``_sanitize_project_name`` would map ``_`` to ``-`` here, renaming a
        working class's blueprint — after which ``_blueprint_lookup`` would
        miss it (duplicate create) and orphan cleanup would delete the
        original. Underscores must survive.
        """
        name = self._created_name(service, "Bank_Statement")
        assert name.startswith("idp-Bank_Statement-")

    def test_lookup_matches_the_name_the_create_path_produced(self, service):
        """Create and lookup must sanitize identically, or every sync
        re-creates the blueprint it already made."""
        created = self._created_name(service, "Task cards")
        existing = [
            {"blueprintName": created, "blueprintArn": "arn:aws:bedrock:::bp/1"}
        ]

        assert service._blueprint_lookup(existing, "Task cards") is not None
        # And an unsanitized lookup key must not be how it matches.
        assert service._blueprint_lookup(existing, "Unrelated class") is None

    def test_orphan_cleanup_expects_the_sanitized_prefix(self, service):
        """Cleanup builds expected prefixes from config; if it used the raw id
        it would treat a live blueprint as orphaned and delete it."""
        created = self._created_name(service, "Task cards")

        config_obj = MagicMock()
        config_obj.classes = [
            {"$id": "Task cards", "x-aws-idp-document-type": "Task cards"}
        ]
        service.config_manager.get_configuration.return_value = config_obj
        service.blueprint_creator.list_all_blueprints_with_prefix.return_value = [
            {"blueprintName": created, "blueprintArn": "arn:aws:bedrock:::bp/1"}
        ]

        result = service.cleanup_orphaned_blueprints(version="v1")

        assert result["success"] is True, result
        assert result["deleted_count"] == 0, result
        service.blueprint_creator.delete_blueprint.assert_not_called()

    def test_class_id_with_no_usable_characters_fails_with_an_actionable_error(
        self, service
    ):
        """Sanitizing '???' yields nothing; report why instead of calling the
        API with a name that cannot be valid."""
        result = service._process_single_class(
            build_json_schema(doc_id="???", properties={"a": {"type": "string"}}),
            existing_blueprints=[],
        )

        assert result["status"] == "failed"
        assert "a-zA-Z0-9-_" in result["error"]
        assert "Rename the class" in result["error"]
        service.blueprint_creator.create_blueprint.assert_not_called()
