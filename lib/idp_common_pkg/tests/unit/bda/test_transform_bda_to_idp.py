# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the transform_bda_blueprint_to_idp_class_schema method in BdaBlueprintService.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from idp_common.bda.bda_blueprint_service import BdaBlueprintService


@pytest.mark.unit
class TestTransformBdaToIdp:
    """Tests for the transform_bda_blueprint_to_idp_class_schema method."""

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

            service = BdaBlueprintService()
            return service

    @pytest.fixture
    def bda_blueprint_schema(self):
        """Sample BDA blueprint schema for testing."""
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": "Form-1040",
            "description": "U.S. Individual Income Tax Return",
            "type": "object",
            "definitions": {
                "PersonalInformation": {
                    "type": "object",
                    "properties": {
                        "FirstName": {
                            "type": "string",
                            "inferenceType": "explicit",
                            "instruction": "First name of the taxpayer",
                        },
                        "LastName": {
                            "type": "string",
                            "inferenceType": "explicit",
                            "instruction": "Last name of the taxpayer",
                        },
                    },
                },
                "Income": {
                    "type": "object",
                    "properties": {
                        "Wages": {
                            "type": "number",
                            "inferenceType": "explicit",
                            "instruction": "Wages reported on Form(s) W-2",
                        }
                    },
                },
            },
            "properties": {
                "PersonalInformation": {"$ref": "#/definitions/PersonalInformation"},
                "Income": {"$ref": "#/definitions/Income"},
                "Dependents": {
                    "type": "array",
                    "items": {"$ref": "#/definitions/DependentsItem"},
                    "instruction": "Dependents of the taxpayer",
                },
            },
        }

    def test_transform_basic_structure(self, service, bda_blueprint_schema):
        """Test that the basic structure is correctly transformed."""
        result = service.transform_bda_blueprint_to_idp_class_schema(
            bda_blueprint_schema
        )

        # Verify basic IDP structure
        assert result["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert result["$id"] == "Form-1040"
        assert result["x-aws-idp-document-type"] == "Form-1040"
        assert result["type"] == "object"
        assert result["description"] == "U.S. Individual Income Tax Return"

    def test_transform_definitions_to_defs(self, service, bda_blueprint_schema):
        """Test that definitions are converted to $defs."""
        result = service.transform_bda_blueprint_to_idp_class_schema(
            bda_blueprint_schema
        )

        # Verify definitions -> $defs conversion
        assert "$defs" in result
        assert "definitions" not in result
        assert "PersonalInformation" in result["$defs"]
        assert "Income" in result["$defs"]

    def test_transform_removes_bda_fields(self, service, bda_blueprint_schema):
        """Test that BDA-specific fields are removed and converted."""
        result = service.transform_bda_blueprint_to_idp_class_schema(
            bda_blueprint_schema
        )

        # Check PersonalInformation definition
        personal_info = result["$defs"]["PersonalInformation"]
        first_name = personal_info["properties"]["FirstName"]

        # Verify BDA fields are removed and converted
        assert "inferenceType" not in first_name
        assert "instruction" not in first_name
        assert "description" in first_name
        assert first_name["description"] == "First name of the taxpayer"
        assert first_name["type"] == "string"

        # Check Income definition
        income = result["$defs"]["Income"]
        wages = income["properties"]["Wages"]

        assert "inferenceType" not in wages
        assert "instruction" not in wages
        assert "description" in wages
        assert wages["description"] == "Wages reported on Form(s) W-2"
        assert wages["type"] == "number"

    def test_transform_ref_paths(self, service, bda_blueprint_schema):
        """Test that $ref paths are converted from definitions to $defs."""
        result = service.transform_bda_blueprint_to_idp_class_schema(
            bda_blueprint_schema
        )

        # Verify $ref path conversions
        assert (
            result["properties"]["PersonalInformation"]["$ref"]
            == "#/$defs/PersonalInformation"
        )
        assert result["properties"]["Income"]["$ref"] == "#/$defs/Income"

    def test_transform_array_properties(self, service, bda_blueprint_schema):
        """Test that array properties are handled correctly."""
        result = service.transform_bda_blueprint_to_idp_class_schema(
            bda_blueprint_schema
        )

        # Check array property
        dependents = result["properties"]["Dependents"]
        assert dependents["type"] == "array"
        assert dependents["items"]["$ref"] == "#/$defs/DependentsItem"
        assert dependents["description"] == "Dependents of the taxpayer"
        assert "instruction" not in dependents
        assert "inferenceType" not in dependents

    def test_transform_with_missing_fields(self, service):
        """Test transformation with missing optional fields."""
        minimal_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": "SimpleDoc",
            "type": "object",
            "properties": {
                "simpleField": {
                    "type": "string",
                    "inferenceType": "explicit",
                    "instruction": "A simple field",
                }
            },
        }

        result = service.transform_bda_blueprint_to_idp_class_schema(minimal_schema)

        assert result["$id"] == "SimpleDoc"
        assert result["x-aws-idp-document-type"] == "SimpleDoc"
        assert "description" not in result  # No description in input
        assert result["properties"]["simpleField"]["description"] == "A simple field"

    def test_transform_invalid_input(self, service):
        """Test that invalid input raises appropriate error."""
        with pytest.raises(ValueError, match="Blueprint schema must be a dictionary"):
            service.transform_bda_blueprint_to_idp_class_schema("not a dict")

    def test_transform_empty_schema(self, service):
        """Test transformation with minimal schema."""
        empty_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": "EmptyDoc",
            "type": "object",
        }

        result = service.transform_bda_blueprint_to_idp_class_schema(empty_schema)

        assert result["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert result["$id"] == "EmptyDoc"
        assert result["x-aws-idp-document-type"] == "EmptyDoc"
        assert result["type"] == "object"
        assert "$defs" not in result or result.get("$defs") == {}
        assert "properties" not in result or result.get("properties") == {}

    def test_transform_nested_objects(self, service):
        """Test transformation with nested object properties."""
        nested_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": "NestedDoc",
            "type": "object",
            "definitions": {
                "Address": {
                    "type": "object",
                    "properties": {
                        "street": {
                            "type": "string",
                            "inferenceType": "explicit",
                            "instruction": "Street address",
                        },
                        "nestedObject": {
                            "type": "object",
                            "properties": {
                                "city": {
                                    "type": "string",
                                    "inferenceType": "explicit",
                                    "instruction": "City name",
                                }
                            },
                        },
                    },
                }
            },
            "properties": {"address": {"$ref": "#/definitions/Address"}},
        }

        result = service.transform_bda_blueprint_to_idp_class_schema(nested_schema)

        # Verify nested object transformation
        address_def = result["$defs"]["Address"]
        assert address_def["properties"]["street"]["description"] == "Street address"

        # Verify nested object properties are also transformed
        nested_obj = address_def["properties"]["nestedObject"]
        assert nested_obj["type"] == "object"
        assert nested_obj["properties"]["city"]["description"] == "City name"
        assert "inferenceType" not in nested_obj["properties"]["city"]

    def test_transform_preserves_non_bda_fields(self, service):
        """Test that non-BDA fields are preserved during transformation."""
        schema_with_extra_fields = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "class": "DocWithExtras",
            "type": "object",
            "definitions": {
                "ItemWithExtras": {
                    "type": "object",
                    "properties": {
                        "fieldWithExtras": {
                            "type": "string",
                            "inferenceType": "explicit",
                            "instruction": "Field with extra properties",
                            "minLength": 1,
                            "maxLength": 100,
                            "pattern": "^[A-Za-z]+$",
                        }
                    },
                }
            },
            "properties": {"item": {"$ref": "#/definitions/ItemWithExtras"}},
        }

        result = service.transform_bda_blueprint_to_idp_class_schema(
            schema_with_extra_fields
        )

        # Verify extra fields are preserved
        field = result["$defs"]["ItemWithExtras"]["properties"]["fieldWithExtras"]
        assert field["description"] == "Field with extra properties"
        assert field["minLength"] == 1
        assert field["maxLength"] == 100
        assert field["pattern"] == "^[A-Za-z]+$"
        assert "inferenceType" not in field
        assert "instruction" not in field

    def test_validate_array_instruction_requirements(self, service):
        """Test that array properties get default instruction field when missing."""
        # Test IDP schema with array missing instruction - this should now default to "-"
        idp_schema_missing_array_instruction = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "TestDoc",
            "type": "object",
            "$defs": {
                "Item": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Item name"}
                    },
                }
            },
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/Item"},
                    # Missing "instruction" or "description" field - this should get defaulted to "-"
                }
            },
        }

        # This should now work and default the instruction to "-"
        result = service._transform_json_schema_to_bedrock_blueprint(
            idp_schema_missing_array_instruction
        )

        # Verify array instruction is defaulted to "-"
        assert "instruction" in result["properties"]["items"]
        assert result["properties"]["items"]["instruction"] == "-"

    def test_validate_all_properties_have_instruction(self, service):
        """Test that all properties (including arrays) must have instruction field."""
        # Test IDP schema with valid array instruction
        idp_schema_with_valid_array = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "TestDoc",
            "type": "object",
            "$defs": {
                "Item": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Item name"}
                    },
                }
            },
            "properties": {
                "items": {
                    "type": "array",
                    "description": "List of items",  # Required instruction field (as description in IDP)
                    "items": {"$ref": "#/$defs/Item"},
                },
                "title": {"type": "string", "description": "Document title"},
            },
        }

        # This should work without errors
        result = service._transform_json_schema_to_bedrock_blueprint(
            idp_schema_with_valid_array
        )

        # Debug: print the result to see the structure
        import json

        print("Result:", json.dumps(result, indent=2))

        # Verify array instruction is preserved in BDA format
        assert "instruction" in result["properties"]["items"]
        assert result["properties"]["items"]["instruction"] == "List of items"

    def test_validate_nested_array_instruction_requirements(self, service):
        """Arrays inside a definition used as array items are skipped.

        BDA rejects an array nested in an array (measured, issue #1742).
        """
        idp_schema_with_nested_array = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "TestDoc",
            "type": "object",
            "$defs": {
                "Employee": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Employee name"},
                        "shifts": {
                            "type": "array",
                            "description": "Weekly shifts",  # Array in an array-items definition - skipped
                            "items": {"type": "string"},
                        },
                        "tasks": {
                            "type": "array",
                            "items": {"type": "string"},
                            # Array in an array-items definition - skipped
                        },
                    },
                }
            },
            "properties": {
                "employees": {
                    "type": "array",
                    "description": "List of employees",  # Top-level array - should be processed
                    "items": {"$ref": "#/$defs/Employee"},
                },
                "departments": {
                    "type": "array",
                    "items": {"type": "string"},
                    # Top-level array - should default to "-"
                },
            },
        }

        # This should work without errors and skip nested arrays
        result = service._transform_json_schema_to_bedrock_blueprint(
            idp_schema_with_nested_array
        )

        # Verify top-level array instructions are handled correctly
        assert result["properties"]["employees"]["instruction"] == "List of employees"
        assert result["properties"]["departments"]["instruction"] == "-"  # Defaulted

        # Verify nested arrays are skipped due to BDA limitations
        employee_def = result["definitions"]["Employee"]
        assert (
            "shifts" not in employee_def["properties"]
        )  # Skipped due to BDA limitations
        assert (
            "tasks" not in employee_def["properties"]
        )  # Skipped due to BDA limitations

        # Verify leaf properties are still processed
        assert employee_def["properties"]["name"]["instruction"] == "Employee name"


@pytest.mark.unit
class TestPayslipTransformation:
    """Tests for payslip blueprint transformation using real payslip data."""

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

            service = BdaBlueprintService()
            return service

    @pytest.fixture
    def payslip_blueprint_schema(self):
        """Load the actual payslip blueprint schema from resources."""
        test_dir = Path(__file__).parent.parent.parent
        resources_dir = test_dir / "resources"
        payslip_file = resources_dir / "payslip_blueprint_schema.json"

        with open(payslip_file, "r") as f:
            return json.load(f)

    @pytest.fixture
    def payslip_idp_schema(self):
        """Load the payslip IDP schema from resources."""
        test_dir = Path(__file__).parent.parent.parent
        resources_dir = test_dir / "resources"
        idp_file = resources_dir / "payslip_idp_schema.json"

        with open(idp_file, "r") as f:
            return json.load(f)

    def test_payslip_blueprint_to_idp_transformation(
        self, service, payslip_blueprint_schema
    ):
        """Test converting payslip blueprint to IDP schema format."""
        # Transform BDA blueprint to IDP schema
        idp_schema = service.transform_bda_blueprint_to_idp_class_schema(
            payslip_blueprint_schema
        )

        # Verify basic IDP structure
        assert idp_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert idp_schema["$id"] == "Payslip"
        assert idp_schema["x-aws-idp-document-type"] == "Payslip"
        assert idp_schema["type"] == "object"
        assert idp_schema["description"] == payslip_blueprint_schema["description"]

        # Verify definitions -> $defs conversion
        assert "$defs" in idp_schema
        assert "definitions" not in idp_schema

        # Verify all definitions were converted
        original_definitions = payslip_blueprint_schema["definitions"]
        converted_defs = idp_schema["$defs"]
        assert set(original_definitions.keys()) == set(converted_defs.keys())

        # Verify BDA-specific fields are removed and converted to descriptions
        for def_name, def_value in converted_defs.items():
            if "properties" in def_value:
                for prop_name, prop_value in def_value["properties"].items():
                    # Should not have BDA-specific fields
                    assert "inferenceType" not in prop_value
                    assert "instruction" not in prop_value

                    # Should have description if instruction was present in original
                    original_prop = original_definitions[def_name]["properties"][
                        prop_name
                    ]
                    if "instruction" in original_prop:
                        assert "description" in prop_value
                        assert prop_value["description"] == original_prop["instruction"]

        # Verify $ref paths are converted
        for prop_name, prop_value in idp_schema["properties"].items():
            if "$ref" in prop_value:
                assert prop_value["$ref"].startswith("#/$defs/")
                assert not prop_value["$ref"].startswith("#/definitions/")

    def test_idp_to_blueprint_round_trip_transformation(
        self, service, payslip_blueprint_schema, payslip_idp_schema
    ):
        """Test converting IDP schema to blueprint, verifying structure matches original blueprint."""
        # Step 1: Use the pre-generated IDP schema from file
        idp_schema = payslip_idp_schema

        # Step 2: Convert IDP schema to BDA blueprint
        reconstructed_blueprint = service._transform_json_schema_to_bedrock_blueprint(
            idp_schema
        )

        # Step 3: Verify the reconstructed blueprint matches the original structure

        # Basic structure verification
        assert reconstructed_blueprint["$schema"] == payslip_blueprint_schema["$schema"]
        assert reconstructed_blueprint["class"] == payslip_blueprint_schema["class"]
        assert reconstructed_blueprint["type"] == payslip_blueprint_schema["type"]
        assert (
            reconstructed_blueprint["description"]
            == payslip_blueprint_schema["description"]
        )

        # Verify definitions structure
        assert "definitions" in reconstructed_blueprint
        assert "$defs" not in reconstructed_blueprint

        original_definitions = payslip_blueprint_schema["definitions"]
        reconstructed_definitions = reconstructed_blueprint["definitions"]

        # Verify all definition names are preserved
        assert set(original_definitions.keys()) == set(reconstructed_definitions.keys())

        # Verify properties structure for each definition
        for def_name in original_definitions.keys():
            original_def = original_definitions[def_name]
            reconstructed_def = reconstructed_definitions[def_name]

            assert original_def["type"] == reconstructed_def["type"]

            if "properties" in original_def:
                assert "properties" in reconstructed_def
                original_props = original_def["properties"]
                reconstructed_props = reconstructed_def["properties"]

                # Verify all property names are preserved
                assert set(original_props.keys()) == set(reconstructed_props.keys())

                # Verify property types are preserved
                for prop_name in original_props.keys():
                    original_prop = original_props[prop_name]
                    reconstructed_prop = reconstructed_props[prop_name]

                    assert original_prop["type"] == reconstructed_prop["type"]

                    # Verify BDA-specific fields are restored for leaf properties
                    if original_prop["type"] in ["string", "number", "boolean"]:
                        assert "inferenceType" in reconstructed_prop
                        assert "instruction" in reconstructed_prop

        # Verify main properties structure
        original_properties = payslip_blueprint_schema["properties"]
        reconstructed_properties = reconstructed_blueprint["properties"]

        # Verify all property names are preserved
        assert set(original_properties.keys()) == set(reconstructed_properties.keys())

        # Verify $ref paths are converted back to definitions format
        for prop_name, prop_value in reconstructed_properties.items():
            if "$ref" in prop_value:
                assert prop_value["$ref"].startswith("#/definitions/")
                assert not prop_value["$ref"].startswith("#/$defs/")

        # Verify array properties maintain their structure
        for prop_name, prop_value in original_properties.items():
            if prop_value.get("type") == "array":
                reconstructed_prop = reconstructed_properties[prop_name]
                assert reconstructed_prop["type"] == "array"

                # Check if original has items with $ref
                if "items" in prop_value and "$ref" in prop_value["items"]:
                    assert "items" in reconstructed_prop
                    # The transformation might convert array items differently
                    # Just verify that items exist and have some structure
                    assert reconstructed_prop["items"] is not None

                    # If the items still have $ref, verify the path format
                    if "$ref" in reconstructed_prop["items"]:
                        assert reconstructed_prop["items"]["$ref"].startswith(
                            "#/definitions/"
                        )

    def test_payslip_specific_field_transformations(self, service, payslip_idp_schema):
        """Test specific payslip field transformations to ensure data integrity using pre-generated IDP schema."""
        # Use the pre-generated IDP schema from file
        idp_schema = payslip_idp_schema

        # Test specific field transformations in the IDP schema

        # Test Name definition transformation
        name_def = idp_schema["$defs"]["Name"]
        assert name_def["type"] == "object"
        assert "FirstName" in name_def["properties"]
        assert name_def["properties"]["FirstName"]["type"] == "string"
        assert "inferenceType" not in name_def["properties"]["FirstName"]

        # Test MiddleName with instruction
        middle_name = name_def["properties"]["MiddleName"]
        assert middle_name["description"] == "if available"
        assert "instruction" not in middle_name

        # Test Address definition transformation
        address_def = idp_schema["$defs"]["Address"]
        assert address_def["type"] == "object"
        line1 = address_def["properties"]["Line1"]
        assert line1["description"] == "What is the address line 1?"
        assert line1["type"] == "string"

        # Test main properties with different inference types
        properties = idp_schema["properties"]

        # Test explicit inference type conversion
        current_gross_pay = properties["CurrentGrossPay"]
        assert current_gross_pay["type"] == "number"
        assert current_gross_pay["description"] == "What is the Current Gross Pay?"
        assert "inferenceType" not in current_gross_pay

        # Test inferred inference type conversion
        pay_period_start = properties["PayPeriodStartDate"]
        assert pay_period_start["type"] == "string"
        assert (
            pay_period_start["description"]
            == "What is the Pay Period Start Date? YYYY-MM-DD Format"
        )

        # Test array properties with $ref
        federal_taxes = properties["FederalTaxes"]
        assert federal_taxes["type"] == "array"
        assert federal_taxes["items"]["$ref"] == "#/$defs/FederalTaxes"

        # Test object references
        employee_name = properties["EmployeeName"]
        assert employee_name["$ref"] == "#/$defs/Name"

    def test_payslip_round_trip_data_preservation(
        self, service, payslip_blueprint_schema, payslip_idp_schema
    ):
        """Test that all data is preserved through the IDP-to-blueprint transformation."""
        # Use the pre-generated IDP schema from file
        idp_schema = payslip_idp_schema

        # Convert IDP schema to BDA blueprint
        reconstructed_blueprint = service._transform_json_schema_to_bedrock_blueprint(
            idp_schema
        )

        # Count properties in original vs reconstructed
        original_prop_count = len(payslip_blueprint_schema["properties"])
        reconstructed_prop_count = len(reconstructed_blueprint["properties"])
        assert original_prop_count == reconstructed_prop_count

        # Count definitions in original vs reconstructed
        original_def_count = len(payslip_blueprint_schema["definitions"])
        reconstructed_def_count = len(reconstructed_blueprint["definitions"])
        assert original_def_count == reconstructed_def_count

        # Verify all property types are preserved
        for prop_name, original_prop in payslip_blueprint_schema["properties"].items():
            reconstructed_prop = reconstructed_blueprint["properties"][prop_name]

            # Type should be preserved
            if "type" in original_prop:
                assert original_prop["type"] == reconstructed_prop["type"]

            # $ref should be preserved (with path conversion)
            if "$ref" in original_prop:
                assert "$ref" in reconstructed_prop
                # Extract the definition name from both refs
                original_def_name = original_prop["$ref"].split("/")[-1]
                reconstructed_def_name = reconstructed_prop["$ref"].split("/")[-1]
                assert original_def_name == reconstructed_def_name

        # Specifically verify array items structure
        array_properties = ["FederalTaxes", "StateTaxes", "CityTaxes"]
        for array_prop_name in array_properties:
            # Verify original has array structure
            original_array = payslip_blueprint_schema["properties"][array_prop_name]
            assert original_array["type"] == "array"
            assert "items" in original_array
            assert "$ref" in original_array["items"]

            # Verify IDP schema has correct array structure
            idp_array = idp_schema["properties"][array_prop_name]
            assert idp_array["type"] == "array"
            assert "items" in idp_array
            assert "$ref" in idp_array["items"]
            assert idp_array["items"]["$ref"].startswith("#/$defs/")

            # Verify reconstructed blueprint has correct array structure
            reconstructed_array = reconstructed_blueprint["properties"][array_prop_name]
            assert reconstructed_array["type"] == "array"
            assert "items" in reconstructed_array

            # Extract definition names for comparison
            original_def_name = original_array["items"]["$ref"].split("/")[-1]
            idp_def_name = idp_array["items"]["$ref"].split("/")[-1]

            # The reconstructed array should either:
            # 1. Have $ref items pointing to definitions (preferred)
            # 2. Have inline items structure that matches the definition
            if "$ref" in reconstructed_array["items"]:
                # Case 1: $ref preserved
                reconstructed_def_name = reconstructed_array["items"]["$ref"].split(
                    "/"
                )[-1]
                assert original_def_name == reconstructed_def_name
                assert reconstructed_array["items"]["$ref"].startswith("#/definitions/")
            else:
                # Case 2: Inline structure - verify it matches the definition structure
                assert "type" in reconstructed_array["items"]
                # The items should have the same structure as the referenced definition
                referenced_def = payslip_blueprint_schema["definitions"][
                    original_def_name
                ]

                # Verify the inline structure matches the definition
                if "properties" in referenced_def:
                    # For object types, we expect the transformation to inline the properties
                    # or maintain some structural consistency
                    assert (
                        reconstructed_array["items"]["type"] == referenced_def["type"]
                    )

            # Verify definition names are consistent across transformations
            assert original_def_name == idp_def_name

        # Verify all definition property counts are preserved
        for def_name, original_def in payslip_blueprint_schema["definitions"].items():
            reconstructed_def = reconstructed_blueprint["definitions"][def_name]

            if "properties" in original_def:
                assert "properties" in reconstructed_def
                original_prop_count = len(original_def["properties"])
                reconstructed_prop_count = len(reconstructed_def["properties"])
                assert original_prop_count == reconstructed_prop_count

                # Verify each property in the definition is preserved
                for prop_name, original_def_prop in original_def["properties"].items():
                    assert prop_name in reconstructed_def["properties"]
                    reconstructed_def_prop = reconstructed_def["properties"][prop_name]

                    # Verify property type is preserved
                    assert original_def_prop["type"] == reconstructed_def_prop["type"]

                    # Verify BDA-specific fields are restored for leaf properties
                    if original_def_prop["type"] in ["string", "number", "boolean"]:
                        assert "inferenceType" in reconstructed_def_prop
                        assert "instruction" in reconstructed_def_prop

                        # Verify instruction content is preserved (converted from description in IDP)
                        if "instruction" in original_def_prop:
                            # The instruction should match between original and reconstructed
                            assert (
                                reconstructed_def_prop["instruction"]
                                == original_def_prop["instruction"]
                            )
