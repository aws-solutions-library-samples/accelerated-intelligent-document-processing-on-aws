# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Comprehensive tests for Pydantic model generation from JSON Schema.
"""

import pytest
from pydantic import BaseModel, ValidationError

from idp_common.schema.pydantic_generator import (
    clean_schema_for_generation,
    create_pydantic_model_from_json_schema,
    nullable_required_containers_for_shard,
    validate_json_schema_for_pydantic,
)


class TestCleanSchemaForGeneration:
    """Test schema cleaning functionality."""

    def test_removes_aws_idp_fields(self):
        """Test that AWS IDP extension fields are removed."""
        schema = {
            "type": "object",
            "title": "Test",
            "x-aws-idp-document-type": "invoice",
            "x-aws-idp-confidence-threshold": 0.9,
            "properties": {
                "field1": {
                    "type": "string",
                    "x-aws-idp-evaluation-method": "LLM",
                }
            },
        }

        cleaned = clean_schema_for_generation(schema)

        assert "x-aws-idp-document-type" not in cleaned
        assert "x-aws-idp-confidence-threshold" not in cleaned
        assert "type" in cleaned
        assert "title" in cleaned
        assert "properties" in cleaned
        assert "x-aws-idp-evaluation-method" not in cleaned["properties"]["field1"]
        assert "type" in cleaned["properties"]["field1"]

    def test_preserves_standard_fields(self):
        """Test that standard JSON Schema fields are preserved."""
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "test-schema",
            "type": "object",
            "title": "Test Schema",
            "description": "A test schema",
            "required": ["field1"],
            "properties": {
                "field1": {
                    "type": "string",
                    "description": "Field 1",
                    "minLength": 1,
                }
            },
        }

        cleaned = clean_schema_for_generation(schema)

        assert cleaned["$schema"] == schema["$schema"]
        assert cleaned["$id"] == schema["$id"]
        assert cleaned["type"] == "object"
        assert cleaned["title"] == "Test Schema"
        assert cleaned["description"] == "A test schema"
        assert cleaned["required"] == ["field1"]
        assert cleaned["properties"]["field1"]["minLength"] == 1

    def test_nested_object_cleaning(self):
        """Test cleaning of nested objects."""
        schema = {
            "type": "object",
            "properties": {
                "address": {
                    "type": "object",
                    "x-aws-idp-group": True,
                    "properties": {
                        "street": {
                            "type": "string",
                            "x-aws-idp-required": True,
                        }
                    },
                }
            },
        }

        cleaned = clean_schema_for_generation(schema)

        assert "x-aws-idp-group" not in cleaned["properties"]["address"]
        assert (
            "x-aws-idp-required"
            not in cleaned["properties"]["address"]["properties"]["street"]
        )

    def test_array_items_cleaning(self):
        """Test cleaning of array items."""
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "x-aws-idp-list-item-description": "An item",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "x-aws-idp-index": 0,
                            }
                        },
                    },
                }
            },
        }

        cleaned = clean_schema_for_generation(schema)

        assert "x-aws-idp-list-item-description" not in cleaned["properties"]["items"]
        assert (
            "x-aws-idp-index"
            not in cleaned["properties"]["items"]["items"]["properties"]["name"]
        )

    def test_custom_fields_to_remove(self):
        """Test with custom field prefixes to remove."""
        schema = {
            "type": "object",
            "x-custom-field": "value",
            "x-aws-idp-field": "value",
            "normalField": "value",
        }

        cleaned = clean_schema_for_generation(schema, fields_to_remove=["x-custom-"])

        assert "x-custom-field" not in cleaned
        assert "x-aws-idp-field" in cleaned  # Not removed
        assert "normalField" in cleaned


class TestCreatePydanticModelFromJsonSchema:
    """Test Pydantic model creation from JSON Schema."""

    def test_simple_string_property(self):
        """Test model with a simple string property."""
        schema = {
            "type": "object",
            "title": "SimpleModel",
            "properties": {"name": {"type": "string"}},
        }

        Model = create_pydantic_model_from_json_schema(schema, "SimpleModel")

        # Test model creation
        instance = Model(name="test")
        assert instance.name == "test"

        # Test validation
        with pytest.raises(ValidationError):
            Model(name=123)  # Should fail - expects string

    def test_multiple_types(self):
        """Test model with multiple property types."""
        schema = {
            "type": "object",
            "title": "ComplexModel",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
                "active": {"type": "boolean"},
                "score": {"type": "number"},
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "ComplexModel")

        instance = Model(name="Alice", age=30, active=True, score=95.5)
        assert instance.name == "Alice"
        assert instance.age == 30
        assert instance.active is True
        assert instance.score == 95.5

    def test_nested_object(self):
        """Test model with nested object properties."""
        schema = {
            "type": "object",
            "title": "PersonWithAddress",
            "properties": {
                "name": {"type": "string"},
                "address": {
                    "type": "object",
                    "properties": {
                        "street": {"type": "string"},
                        "city": {"type": "string"},
                        "zip": {"type": "string"},
                    },
                },
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "PersonWithAddress")

        instance = Model(
            name="Bob",
            address={"street": "123 Main St", "city": "Springfield", "zip": "12345"},
        )
        assert instance.name == "Bob"
        assert instance.address.street == "123 Main St"
        assert instance.address.city == "Springfield"

    def test_array_property(self):
        """Test model with array properties."""
        schema = {
            "type": "object",
            "title": "PersonWithTags",
            "properties": {
                "name": {"type": "string"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "PersonWithTags")

        instance = Model(name="Carol", tags=["engineer", "manager"])
        assert instance.name == "Carol"
        assert len(instance.tags) == 2
        assert "engineer" in instance.tags

    def test_array_of_objects(self):
        """Test model with array of nested objects."""
        schema = {
            "type": "object",
            "title": "Invoice",
            "properties": {
                "invoice_number": {"type": "string"},
                "line_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "amount": {"type": "number"},
                        },
                    },
                },
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "Invoice")

        instance = Model(
            invoice_number="INV-001",
            line_items=[
                {"description": "Item 1", "amount": 100.0},
                {"description": "Item 2", "amount": 200.0},
            ],
        )
        assert instance.invoice_number == "INV-001"
        assert len(instance.line_items) == 2
        assert instance.line_items[0].description == "Item 1"
        assert instance.line_items[1].amount == 200.0

    def test_required_fields(self):
        """Test model with required fields."""
        schema = {
            "type": "object",
            "title": "RequiredFieldsModel",
            "required": ["name", "email"],
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "RequiredFieldsModel")

        # Should work with required fields
        instance = Model(name="Dave", email="dave@example.com")
        assert instance.name == "Dave"

        # Should fail without required field
        with pytest.raises(ValidationError):
            Model(name="Dave")  # Missing email

    def test_enums(self):
        """Test model with enum constraints."""
        schema = {
            "type": "object",
            "title": "StatusModel",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "approved", "rejected"],
                }
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "StatusModel")

        instance = Model(status="approved")
        # datamodel-code-generator creates an Enum, so check the value
        assert instance.status.value == "approved" or str(instance.status) == "approved"

        with pytest.raises(ValidationError):
            Model(status="invalid")

    def test_default_values(self):
        """Test model with default values."""
        schema = {
            "type": "object",
            "title": "DefaultsModel",
            "properties": {
                "name": {"type": "string"},
                "active": {"type": "boolean", "default": True},
                "count": {"type": "integer", "default": 0},
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "DefaultsModel")

        instance = Model(name="Eve")
        assert instance.name == "Eve"
        assert instance.active is True
        assert instance.count == 0

    def test_cleaning_integration(self):
        """Test that AWS IDP fields are cleaned automatically."""
        schema = {
            "type": "object",
            "title": "IDPModel",
            "x-aws-idp-document-type": "invoice",
            "properties": {
                "invoice_number": {
                    "type": "string",
                    "x-aws-idp-confidence-threshold": 0.9,
                }
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "IDPModel")

        # Model should work despite IDP fields
        instance = Model(invoice_number="INV-123")
        assert instance.invoice_number == "INV-123"

    def test_no_cleaning_when_disabled(self):
        """Test that cleaning can be disabled."""
        schema = {
            "type": "object",
            "title": "NoCleanModel",
            "x-custom-field": "value",  # This would normally cause an error
            "properties": {"name": {"type": "string"}},
        }

        # Should fail with cleaning disabled if x-custom-field is not valid JSON Schema
        # But datamodel-code-generator might ignore unknown fields
        Model = create_pydantic_model_from_json_schema(
            schema, "NoCleanModel", clean_schema=False
        )
        instance = Model(name="test")
        assert instance.name == "test"

    def test_class_label_with_special_chars(self):
        """Test that special characters in class_label are handled."""
        schema = {
            "type": "object",
            "title": "Model",
            "properties": {"value": {"type": "string"}},
        }

        # Class label with special characters should be sanitized
        Model = create_pydantic_model_from_json_schema(schema, "Test-Model_123!@#")
        instance = Model(value="test")
        assert instance.value == "test"

    def test_pascal_case_title_matching(self):
        """Test that PascalCase titles are matched correctly."""
        schema = {
            "type": "object",
            "title": "my-invoice-model",
            "properties": {"amount": {"type": "number"}},
        }

        Model = create_pydantic_model_from_json_schema(schema, "my_invoice_model")

        # Model name should be normalized
        assert Model.__name__ in ["MyInvoiceModel", "Model"]

    def test_missing_type_field(self):
        """Test handling of schema without type field."""
        schema = {
            "title": "NoTypeModel",
            "properties": {"field": {"type": "string"}},
        }

        # datamodel-code-generator might infer type=object
        Model = create_pydantic_model_from_json_schema(schema, "NoTypeModel")
        instance = Model(field="value")
        assert instance.field == "value"

    def test_self_referential_defs_resolve_to_a_usable_model(self):
        """A `$defs` entry that refers to itself generates a working model.

        datamodel-code-generator emits a forward reference for the recursive
        property, so the recursion has to survive both generation and the
        `model_rebuild()` this module performs afterwards. Both the base case
        (no `next`) and one level of nesting are asserted, because a forward
        reference that was generated but never resolved builds the class fine
        and only fails when the nested value is actually validated.
        """
        schema = {
            "type": "object",
            "title": "CircularModel",
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "next": {"$ref": "#/$defs/Node"},  # Circular reference
                    },
                }
            },
            "properties": {"root": {"$ref": "#/$defs/Node"}},
        }

        Model = create_pydantic_model_from_json_schema(schema, "CircularModel")

        leaf = Model(root={"value": "test"})
        assert leaf.root.value == "test"
        assert leaf.root.next is None

        nested = Model(root={"value": "a", "next": {"value": "b"}})
        assert nested.root.next.value == "b"

    def test_an_unrecognised_type_keyword_yields_a_permissive_model(self):
        """An unusable `type` keyword does not raise; it degrades to `Any`.

        The previous version of this test accepted either outcome, which made
        it unable to fail (#1129). The measured behaviour is that neither
        datamodel-code-generator nor this module validates the schema against
        the JSON Schema metaschema, so generation succeeds and the caller gets
        a model that accepts anything. That is worth pinning in either
        direction: if a dependency upgrade starts rejecting such a schema, the
        callers that today get a permissive model would start seeing an
        exception, and this test is what says so.
        """
        schema = {
            "type": "invalid_type",  # not a JSON Schema type
            "properties": {},
        }

        Model = create_pydantic_model_from_json_schema(schema, "InvalidModel")

        assert issubclass(Model, BaseModel)
        assert "root" in Model.model_fields
        # Nothing is constrained, so an arbitrary payload validates.
        assert Model(root={"anything": 1}).root == {"anything": 1}

    def test_empty_properties(self):
        """Test model with empty properties."""
        schema = {
            "type": "object",
            "title": "EmptyModel",
            "properties": {},
        }

        Model = create_pydantic_model_from_json_schema(schema, "EmptyModel")

        # Should create an empty model
        instance = Model()
        assert isinstance(instance, BaseModel)

    def test_model_rebuild_called(self):
        """Test that model_rebuild is called for proper configuration."""
        schema = {
            "type": "object",
            "title": "RebuildTest",
            "properties": {"field": {"type": "string"}},
        }

        Model = create_pydantic_model_from_json_schema(schema, "RebuildTest")

        # Model should have model_fields populated after rebuild
        assert hasattr(Model, "model_fields")
        assert "field" in Model.model_fields


class TestValidateJsonSchemaForPydantic:
    """Test JSON Schema validation."""

    def test_valid_schema(self):
        """Test that a valid schema returns no warnings."""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        }

        warnings = validate_json_schema_for_pydantic(schema)
        assert len(warnings) == 0

    def test_missing_type(self):
        """Test warning for missing type field."""
        schema = {"properties": {"name": {"type": "string"}}}

        warnings = validate_json_schema_for_pydantic(schema)
        assert any("missing 'type' field" in w for w in warnings)

    def test_non_object_type(self):
        """Test warning for non-object type."""
        schema = {"type": "string"}

        warnings = validate_json_schema_for_pydantic(schema)
        assert any("type='object'" in w for w in warnings)

    def test_missing_properties(self):
        """Test warning for missing properties."""
        schema = {"type": "object"}

        warnings = validate_json_schema_for_pydantic(schema)
        assert any("no 'properties'" in w for w in warnings)

    def test_circular_reference_warning(self):
        """Test warning for circular references."""
        schema = {
            "type": "object",
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {"next": {"$ref": "#/$defs/Node"}},
                }
            },
            "properties": {"root": {"$ref": "#/$defs/Node"}},
        }

        warnings = validate_json_schema_for_pydantic(schema)
        assert any("circular reference" in w.lower() for w in warnings)


class TestJsonSchemaConstraints:
    """Test JSON Schema constraint translation to Pydantic."""

    def test_string_pattern_regex(self):
        """Test that JSON Schema pattern (regex) constraints are enforced."""
        schema = {
            "type": "object",
            "title": "EmailModel",
            "properties": {
                "email": {
                    "type": "string",
                    "pattern": r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
                }
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "EmailModel")

        # Valid email should work
        instance = Model(email="test@example.com")
        assert instance.email == "test@example.com"

        # Invalid email should fail validation
        with pytest.raises(ValidationError) as exc_info:
            Model(email="not-an-email")

        # Check that it's a pattern/regex validation error
        errors = exc_info.value.errors()
        assert len(errors) > 0
        assert any(
            "pattern" in str(err).lower() or "string_pattern" in str(err).lower()
            for err in errors
        )

    def test_string_min_max_length(self):
        """Test that minLength and maxLength constraints are enforced."""
        schema = {
            "type": "object",
            "title": "UsernameModel",
            "properties": {
                "username": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 20,
                }
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "UsernameModel")

        # Valid length
        instance = Model(username="alice")
        assert instance.username == "alice"

        # Too short
        with pytest.raises(ValidationError):
            Model(username="ab")

        # Too long
        with pytest.raises(ValidationError):
            Model(username="a" * 21)

    def test_number_minimum_maximum(self):
        """Test that minimum and maximum constraints are enforced."""
        schema = {
            "type": "object",
            "title": "AgeModel",
            "properties": {
                "age": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 150,
                }
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "AgeModel")

        # Valid range
        instance = Model(age=25)
        assert instance.age == 25

        # Below minimum
        with pytest.raises(ValidationError):
            Model(age=-1)

        # Above maximum
        with pytest.raises(ValidationError):
            Model(age=151)

    def test_exclusive_minimum_maximum(self):
        """Test that exclusiveMinimum and exclusiveMaximum work."""
        schema = {
            "type": "object",
            "title": "ScoreModel",
            "properties": {
                "score": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "exclusiveMaximum": 100,
                }
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "ScoreModel")

        # Valid range (exclusive)
        instance = Model(score=50.5)
        assert instance.score == 50.5

        # At boundary (should fail - exclusive)
        with pytest.raises(ValidationError):
            Model(score=0)

        with pytest.raises(ValidationError):
            Model(score=100)

    def test_array_min_max_items(self):
        """Test that minItems and maxItems constraints are enforced."""
        schema = {
            "type": "object",
            "title": "TagsModel",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                }
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "TagsModel")

        # Valid number of items
        instance = Model(tags=["tag1", "tag2"])
        assert len(instance.tags) == 2

        # Too few items
        with pytest.raises(ValidationError):
            Model(tags=[])

        # Too many items
        with pytest.raises(ValidationError):
            Model(tags=["tag1", "tag2", "tag3", "tag4", "tag5", "tag6"])

    def test_array_unique_items(self):
        """Test that uniqueItems constraint is translated (may need custom validator)."""
        schema = {
            "type": "object",
            "title": "UniqueTagsModel",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                }
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "UniqueTagsModel")

        # Unique items should work
        instance = Model(tags=["tag1", "tag2", "tag3"])
        assert instance.tags == ["tag1", "tag2", "tag3"]

        # Note: uniqueItems validation may require pydantic's set type or custom validator
        # datamodel-code-generator may not enforce this automatically

    def test_string_format_constraints(self):
        """Test that format constraints (email, uri, date-time) are enforced."""
        schema = {
            "type": "object",
            "title": "FormatModel",
            "properties": {
                "email": {
                    "type": "string",
                    "format": "email",
                },
                "website": {
                    "type": "string",
                    "format": "uri",
                },
                "created_at": {
                    "type": "string",
                    "format": "date-time",
                },
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "FormatModel")

        # Valid formats
        instance = Model(
            email="user@example.com",
            website="https://example.com",
            created_at="2024-01-01T12:00:00Z",
        )
        assert instance.email == "user@example.com"

        # Invalid email format
        with pytest.raises(ValidationError):
            Model(
                email="not-an-email",
                website="https://example.com",
                created_at="2024-01-01T12:00:00Z",
            )

        # Invalid URI format
        with pytest.raises(ValidationError):
            Model(
                email="user@example.com",
                website="not a uri",
                created_at="2024-01-01T12:00:00Z",
            )
        assert instance.email == "user@example.com"

        # Invalid email format
        with pytest.raises(ValidationError):
            Model(
                email="not-an-email",
                website="https://example.com",
                created_at="2024-01-01T12:00:00Z",
            )

        # Invalid URI format
        with pytest.raises(ValidationError):
            Model(
                email="user@example.com",
                website="not a uri",
                created_at="2024-01-01T12:00:00Z",
            )

    def test_multiple_constraints_combined(self):
        """Test that multiple constraints work together."""
        schema = {
            "type": "object",
            "title": "ProductCodeModel",
            "properties": {
                "product_code": {
                    "type": "string",
                    "pattern": "^[A-Z]{3}-[0-9]{4}$",
                    "minLength": 8,
                    "maxLength": 8,
                }
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "ProductCodeModel")

        # Valid code
        instance = Model(product_code="ABC-1234")
        assert instance.product_code == "ABC-1234"

        # Wrong pattern
        with pytest.raises(ValidationError):
            Model(product_code="abc-1234")  # lowercase

        # Wrong length (even if pattern matches)
        with pytest.raises(ValidationError):
            Model(product_code="ABCD-12345")

    def test_array_contains_constraints(self):
        """Test that array contains, minContains, and maxContains constraints are supported."""
        schema = {
            "type": "object",
            "title": "OrderWithApprovedItems",
            "properties": {
                "OrderID": {"type": "string"},
                "LineItems": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ItemName": {"type": "string"},
                            "Status": {"type": "string"},
                            "Amount": {"type": "number"},
                        },
                    },
                    "contains": {
                        "type": "object",
                        "properties": {"Status": {"const": "approved"}},
                        "required": ["Status"],
                    },
                    "minContains": 2,
                    "maxContains": 5,
                },
            },
        }

        # Test that model can be created (datamodel-code-generator supports these constraints)
        OrderModel = create_pydantic_model_from_json_schema(
            schema, "OrderWithApprovedItems", clean_schema=False
        )

        # Verify model was created
        assert OrderModel is not None
        assert "LineItems" in OrderModel.model_fields

        # Test valid data with 2 approved items (meets minContains=2)
        valid_order = OrderModel(
            OrderID="ORD-001",
            LineItems=[
                {"ItemName": "Item1", "Status": "approved", "Amount": 100.0},
                {"ItemName": "Item2", "Status": "approved", "Amount": 200.0},
                {"ItemName": "Item3", "Status": "pending", "Amount": 150.0},
            ],
        )
        assert valid_order.OrderID == "ORD-001"
        assert len(valid_order.LineItems) == 3

        # Test that Pydantic now DOES enforce contains/minContains at runtime
        # Data with only 1 approved item should fail validation (violates minContains=2)
        with pytest.raises(ValidationError):
            OrderModel(
                OrderID="ORD-002",
                LineItems=[
                    {"ItemName": "Item1", "Status": "approved", "Amount": 100.0},
                    {"ItemName": "Item2", "Status": "pending", "Amount": 200.0},
                ],
            )

        # Conclusion: datamodel-code-generator successfully translates the schema,
        # and Pydantic v2 now enforces contains/minContains/maxContains at runtime.


class TestJsonSchemaValidationEnforcement:
    """Test JSON Schema validation enforcement for advanced constraints."""

    def test_contains_constraint_enforcement_enabled(self):
        """Test that JSON Schema validation enforces contains/minContains when enabled."""
        schema = {
            "type": "object",
            "title": "OrderWithValidation",
            "properties": {
                "OrderID": {"type": "string"},
                "LineItems": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Status": {"type": "string"},
                            "Amount": {"type": "number"},
                        },
                    },
                    "contains": {
                        "type": "object",
                        "properties": {"Status": {"const": "approved"}},
                        "required": ["Status"],
                    },
                    "minContains": 2,
                },
            },
        }

        # Create model with JSON Schema validation enabled (default)
        OrderModel = create_pydantic_model_from_json_schema(
            schema, "OrderWithValidation", clean_schema=False
        )

        # Valid data: 2 approved items (meets minContains=2)
        valid_order = OrderModel(
            OrderID="ORD-001",
            LineItems=[
                {"Status": "approved", "Amount": 100.0},
                {"Status": "approved", "Amount": 200.0},
                {"Status": "pending", "Amount": 150.0},
            ],
        )
        assert valid_order.OrderID == "ORD-001"

        # Invalid data: only 1 approved item (violates minContains=2)
        # Should raise ValidationError with JSON Schema details
        with pytest.raises(ValidationError) as exc_info:
            OrderModel(
                OrderID="ORD-002",
                LineItems=[
                    {"Status": "approved", "Amount": 100.0},
                    {"Status": "pending", "Amount": 200.0},
                ],
            )

        # Verify error mentions JSON Schema validation
        error_str = str(exc_info.value)
        assert (
            "json schema" in error_str.lower()
            or "schema validation" in error_str.lower()
        )

    def test_contains_constraint_enforcement_disabled(self):
        """Test that validation can be disabled for performance."""
        schema = {
            "type": "object",
            "title": "OrderWithoutValidation",
            "properties": {
                "OrderID": {"type": "string"},
                "LineItems": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Status": {"type": "string"},
                            "Amount": {"type": "number"},
                        },
                    },
                    "contains": {
                        "type": "object",
                        "properties": {"Status": {"const": "approved"}},
                        "required": ["Status"],
                    },
                    "minContains": 2,
                },
            },
        }

        # Create model with JSON Schema validation disabled
        OrderModel = create_pydantic_model_from_json_schema(
            schema,
            "OrderWithoutValidation",
            clean_schema=False,
            enable_json_schema_validation=False,
        )

        # Invalid data should pass (no JSON Schema validation)
        order = OrderModel(
            OrderID="ORD-002",
            LineItems=[
                {"Status": "approved", "Amount": 100.0},
                {"Status": "pending", "Amount": 200.0},
            ],
        )
        assert order.OrderID == "ORD-002"

    def test_no_validation_for_simple_schemas(self):
        """Test that simple schemas without advanced constraints don't add validation overhead."""
        schema = {
            "type": "object",
            "title": "SimpleModel",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
        }

        # Create model - should not add JSON Schema validation
        SimpleModel = create_pydantic_model_from_json_schema(
            schema, "SimpleModel", clean_schema=False
        )

        # Should work normally (Pydantic validation only)
        instance = SimpleModel(name="John", age=30)
        assert instance.name == "John"


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_deeply_nested_structure(self):
        """Test model with deeply nested structure."""
        schema = {
            "type": "object",
            "title": "DeepNest",
            "properties": {
                "level1": {
                    "type": "object",
                    "properties": {
                        "level2": {
                            "type": "object",
                            "properties": {
                                "level3": {
                                    "type": "object",
                                    "properties": {"value": {"type": "string"}},
                                }
                            },
                        }
                    },
                }
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "DeepNest")

        instance = Model(level1={"level2": {"level3": {"value": "deep"}}})
        assert instance.level1.level2.level3.value == "deep"

    def test_unicode_in_schema(self):
        """Test schema with unicode characters."""
        schema = {
            "type": "object",
            "title": "UnicodeModel",
            "properties": {
                "name": {"type": "string", "description": "名前"},
                "city": {"type": "string", "description": "Città"},
            },
        }

        Model = create_pydantic_model_from_json_schema(schema, "UnicodeModel")

        instance = Model(name="テスト", city="Roma")
        assert instance.name == "テスト"
        assert instance.city == "Roma"

    def test_large_schema(self):
        """Test model generation with many properties."""
        properties = {f"field_{i}": {"type": "string"} for i in range(50)}

        schema = {
            "type": "object",
            "title": "LargeModel",
            "properties": properties,
        }

        Model = create_pydantic_model_from_json_schema(schema, "LargeModel")

        data = {f"field_{i}": f"value_{i}" for i in range(50)}
        instance = Model(**data)
        assert instance.field_0 == "value_0"
        assert instance.field_49 == "value_49"

    def test_schema_as_json_string(self):
        """Test that JSON string schemas work."""
        import json

        schema_dict = {
            "type": "object",
            "title": "StringInputModel",
            "properties": {"value": {"type": "string"}},
        }

        schema_str = json.dumps(schema_dict)

        # Should work with string input (when clean_schema=False)
        Model = create_pydantic_model_from_json_schema(
            schema_str,  # type: ignore
            "StringInputModel",
            clean_schema=False,
        )

        instance = Model(value="test")
        assert instance.value == "test"


class TestNestedObjectAliases:
    """Test handling of nested objects where datamodel-code-generator creates field aliases."""

    def test_nested_object_with_alias_validation(self):
        """Test that nested objects work with field aliases during validation."""
        schema = {
            "type": "object",
            "title": "Employee",
            "properties": {
                "EmployeeName": {
                    "type": "object",
                    "properties": {
                        "FirstName": {"type": "string"},
                        "LastName": {"type": "string"},
                    },
                },
                "EmployeeNumber": {"type": "string"},
            },
        }

        EmployeeModel = create_pydantic_model_from_json_schema(
            schema, "Employee", clean_schema=False
        )

        # Validate using the alias name (as returned by LLM)
        data = {
            "EmployeeName": {"FirstName": "John", "LastName": "Doe"},
            "EmployeeNumber": "12345",
        }

        instance = EmployeeModel.model_validate(data)

        # Access using the actual field name (may have _1 suffix)
        assert hasattr(instance, "EmployeeName_1") or hasattr(instance, "EmployeeName")
        assert instance.EmployeeNumber == "12345"

    def test_nested_object_serialization_with_aliases(self):
        """Test that nested objects serialize correctly using aliases by default."""
        schema = {
            "type": "object",
            "title": "Company",
            "properties": {
                "CompanyAddress": {
                    "type": "object",
                    "properties": {
                        "Line1": {"type": "string"},
                        "City": {"type": "string"},
                        "ZipCode": {"type": "string"},
                    },
                },
                "CompanyName": {"type": "string"},
            },
        }

        CompanyModel = create_pydantic_model_from_json_schema(
            schema, "Company", clean_schema=False
        )

        # Create instance using alias names
        data = {
            "CompanyAddress": {
                "Line1": "123 Main St",
                "City": "New York",
                "ZipCode": "10001",
            },
            "CompanyName": "Acme Corp",
        }

        instance = CompanyModel.model_validate(data)

        # Test 1: Serialize WITHOUT by_alias parameter (should use alias by default due to serialize_by_alias=True)
        output = instance.model_dump()

        assert output["CompanyName"] == "Acme Corp"
        assert output["CompanyAddress"]["Line1"] == "123 Main St"
        assert output["CompanyAddress"]["City"] == "New York"
        assert output["CompanyAddress"]["ZipCode"] == "10001"

        # Ensure no _1 suffixes in output when using default serialization
        assert "CompanyAddress_1" not in output

        # Test 2: Verify by_alias=True gives same result
        output_explicit = instance.model_dump(by_alias=True)
        assert output == output_explicit

        # Test 3: Verify by_alias=False shows internal field names
        output_internal = instance.model_dump(by_alias=False)
        # Internal field names should have _1 suffix
        assert (
            "CompanyAddress_1" in output_internal or "CompanyAddress" in output_internal
        )

    def test_multiple_nested_objects_with_aliases(self):
        """Test multiple nested objects serialize by alias by default."""
        schema = {
            "type": "object",
            "title": "Payslip",
            "properties": {
                "EmployeeName": {
                    "type": "object",
                    "properties": {
                        "FirstName": {"type": "string"},
                        "LastName": {"type": "string"},
                        "MiddleName": {"type": "string"},
                    },
                },
                "CompanyAddress": {
                    "type": "object",
                    "properties": {
                        "Line1": {"type": "string"},
                        "Line2": {"type": "string"},
                        "City": {"type": "string"},
                        "State": {"type": "string"},
                        "ZipCode": {"type": "string"},
                    },
                },
                "EmployeeAddress": {
                    "type": "object",
                    "properties": {
                        "Line1": {"type": "string"},
                        "City": {"type": "string"},
                    },
                },
                "PayDate": {"type": "string"},
                "CurrentGrossPay": {"type": "string"},
            },
        }

        PayslipModel = create_pydantic_model_from_json_schema(
            schema, "Payslip", clean_schema=False
        )

        # Create instance with all nested objects
        data = {
            "EmployeeName": {
                "FirstName": "Jane",
                "LastName": "Smith",
                "MiddleName": "Marie",
            },
            "CompanyAddress": {
                "Line1": "100 Corporate Dr",
                "Line2": "Suite 200",
                "City": "Boston",
                "State": "MA",
                "ZipCode": "02101",
            },
            "EmployeeAddress": {
                "Line1": "456 Elm St",
                "City": "Cambridge",
            },
            "PayDate": "2025-10-22",
            "CurrentGrossPay": "$5,000.00",
        }

        instance = PayslipModel.model_validate(data)

        # Serialize WITHOUT by_alias parameter - should use aliases by default
        output = instance.model_dump()

        # Verify all fields use proper names (no _1 suffixes) in default serialization
        assert "EmployeeName" in output
        assert "CompanyAddress" in output
        assert "EmployeeAddress" in output
        assert "EmployeeName_1" not in output
        assert "CompanyAddress_1" not in output
        assert "EmployeeAddress_1" not in output

        # Verify nested data
        assert output["EmployeeName"]["FirstName"] == "Jane"
        assert output["CompanyAddress"]["City"] == "Boston"
        assert output["EmployeeAddress"]["Line1"] == "456 Elm St"
        assert output["PayDate"] == "2025-10-22"

    def test_nested_objects_with_arrays(self):
        """Test nested objects combined with arrays serialize by alias by default."""
        schema = {
            "type": "object",
            "title": "Invoice",
            "properties": {
                "BillingAddress": {
                    "type": "object",
                    "properties": {
                        "Line1": {"type": "string"},
                        "City": {"type": "string"},
                    },
                },
                "LineItems": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Description": {"type": "string"},
                            "Amount": {"type": "number"},
                        },
                    },
                },
                "InvoiceNumber": {"type": "string"},
            },
        }

        InvoiceModel = create_pydantic_model_from_json_schema(
            schema, "Invoice", clean_schema=False
        )

        data = {
            "BillingAddress": {"Line1": "789 Oak Ave", "City": "Seattle"},
            "LineItems": [
                {"Description": "Widget", "Amount": 99.99},
                {"Description": "Gadget", "Amount": 149.99},
            ],
            "InvoiceNumber": "INV-2025-001",
        }

        instance = InvoiceModel.model_validate(data)

        # Serialize WITHOUT by_alias parameter - should use aliases by default
        output = instance.model_dump()

        # Verify structure uses aliases (no _1 suffixes)
        assert "BillingAddress" in output
        assert "BillingAddress_1" not in output
        assert output["BillingAddress"]["City"] == "Seattle"
        assert len(output["LineItems"]) == 2
        assert output["LineItems"][0]["Amount"] == 99.99

    def test_model_configuration(self):
        """Test that model configuration is set correctly for alias support."""
        schema = {
            "type": "object",
            "title": "TestModel",
            "properties": {
                "NestedObject": {
                    "type": "object",
                    "properties": {"Field": {"type": "string"}},
                }
            },
        }

        TestModel = create_pydantic_model_from_json_schema(
            schema, "TestModel", clean_schema=False
        )

        # Verify model_config has both populate_by_name and serialize_by_alias
        assert hasattr(TestModel, "model_config")
        assert TestModel.model_config.get("populate_by_name") is True
        assert TestModel.model_config.get("serialize_by_alias") is True

    def test_nested_spaced_field_names_round_trip(self):
        """Regression: nested object/array properties whose names contain spaces
        must validate from and serialize back to their spaced aliases — not the
        underscore-sanitized field names.

        Previously the alias config was applied only to the top-level model, so
        nested fields like "Date of Birth" round-tripped as "Date_of_Birth" and
        agentic extraction silently lost nested values (real-world Homeowners
        Insurance Application regression).
        """
        schema = {
            "type": "object",
            "title": "HomeApp",
            "properties": {
                "Policy Number": {"type": "string"},
                "Primary Applicant Information": {
                    "type": "object",
                    "properties": {
                        "Name": {"type": "string"},
                        "Date of Birth": {"type": "string"},
                        "Marital Status": {"type": "string"},
                        "Drivers License Number": {"type": "string"},
                    },
                },
                "Auto Claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"Number of Accidents": {"type": "string"}},
                    },
                },
            },
        }

        Model = create_pydantic_model_from_json_schema(
            schema, "HomeApp", clean_schema=False
        )

        # The LLM/document produces the spaced (alias) keys.
        data = {
            "Policy Number": "123456",
            "Primary Applicant Information": {
                "Name": "Ziggy Starpixel",
                "Date of Birth": "02/20/2000",
                "Marital Status": "S",
                "Drivers License Number": "1234567A",
            },
            "Auto Claims": [{"Number of Accidents": "2"}],
        }

        out = Model.model_validate(data).model_dump(mode="json")

        # Nested values survive (the core regression: these were dropped to null).
        pai = out["Primary Applicant Information"]
        assert pai["Date of Birth"] == "02/20/2000"
        assert pai["Marital Status"] == "S"
        assert pai["Drivers License Number"] == "1234567A"
        # Keys are the spaced aliases, NOT the underscore field names.
        assert "Date_of_Birth" not in pai
        assert "Marital_Status" not in pai
        # Array-of-object nested fields round-trip too.
        assert out["Auto Claims"][0]["Number of Accidents"] == "2"
        assert "Number_of_Accidents" not in out["Auto Claims"][0]

    def test_deeply_nested_spaced_names_round_trip(self):
        """Three-level nesting with spaced names must also round-trip by alias."""
        schema = {
            "type": "object",
            "title": "Deep",
            "properties": {
                "Outer Group": {
                    "type": "object",
                    "properties": {
                        "Inner Group": {
                            "type": "object",
                            "properties": {"Deep Field": {"type": "string"}},
                        }
                    },
                }
            },
        }
        Model = create_pydantic_model_from_json_schema(
            schema, "Deep", clean_schema=False
        )
        out = Model.model_validate(
            {"Outer Group": {"Inner Group": {"Deep Field": "hello"}}}
        ).model_dump(mode="json")
        assert out["Outer Group"]["Inner Group"]["Deep Field"] == "hello"

    def test_array_contains_constraints(self):
        """Test that array contains, minContains, and maxContains constraints are supported."""
        schema = {
            "type": "object",
            "title": "OrderWithApprovedItems",
            "properties": {
                "OrderID": {"type": "string"},
                "LineItems": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ItemName": {"type": "string"},
                            "Status": {"type": "string"},
                            "Amount": {"type": "number"},
                        },
                    },
                    "contains": {
                        "type": "object",
                        "properties": {"Status": {"const": "approved"}},
                        "required": ["Status"],
                    },
                    "minContains": 2,
                    "maxContains": 5,
                },
            },
        }

        # Test that model can be created (datamodel-code-generator supports these constraints)
        OrderModel = create_pydantic_model_from_json_schema(
            schema, "OrderWithApprovedItems", clean_schema=False
        )

        # Verify model was created
        assert OrderModel is not None
        assert OrderModel.__name__.lower() == "orderwithapproveditems"
        assert "LineItems" in OrderModel.model_fields

        # Test valid data with 2 approved items (meets minContains)
        valid_order = OrderModel(
            OrderID="ORD-001",
            LineItems=[
                {"ItemName": "Item1", "Status": "approved", "Amount": 100.0},
                {"ItemName": "Item2", "Status": "approved", "Amount": 200.0},
                {"ItemName": "Item3", "Status": "pending", "Amount": 150.0},
            ],
        )
        assert valid_order.OrderID == "ORD-001"
        assert len(valid_order.LineItems) == 3

        # Note: Pydantic/datamodel-code-generator may not enforce contains/minContains/maxContains
        # at runtime, as these are JSON Schema validation constraints. The test verifies that
        # the schema can be converted to a Pydantic model without errors, even if the constraints
        # aren't enforced during validation.


class TestNullableRequiredContainersForShard:
    """The shard-scoped half of the transport transform (#1078).

    ``nullable_leaves_for_transport`` widens scalar leaves; this widens a **required**
    array or nested object as well, for the one caller that needs it — a shard agent
    that is told to leave an out-of-shard field null. Its contract as a pure schema
    function is pinned here; what the service does with it is in
    ``tests/unit/extraction/test_truncation_warnings.py``.
    """

    _SCHEMA = {
        "type": "object",
        "properties": {
            "Scalar": {"type": "string"},
            "Rows": {"type": "array", "minItems": 5, "items": {"type": "string"}},
            "Group": {"type": "object", "properties": {"A": {"type": "string"}}},
            "OptionalRows": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["Scalar", "Rows", "Group"],
    }

    def test_only_required_containers_gain_a_null_branch(self):
        out = nullable_required_containers_for_shard(self._SCHEMA)
        props = out["properties"]
        assert props["Rows"]["type"] == ["array", "null"]
        assert props["Group"]["type"] == ["object", "null"]
        # A scalar is this transform's business only through the other one.
        assert props["Scalar"]["type"] == "string"
        # Already optional: nothing to relax.
        assert props["OptionalRows"]["type"] == "array"
        # required itself is untouched — the key must still be present.
        assert out["required"] == ["Scalar", "Rows", "Group"]

    def test_row_count_bounds_are_left_alone(self):
        """Presence and row counts are separate decisions; only presence relaxes."""
        out = nullable_required_containers_for_shard(self._SCHEMA)
        assert out["properties"]["Rows"]["minItems"] == 5

    def test_the_input_is_not_mutated(self):
        """The caller keeps validating the merged section against the real schema."""
        import copy

        before = copy.deepcopy(self._SCHEMA)
        nullable_required_containers_for_shard(self._SCHEMA)
        assert self._SCHEMA == before

    def test_it_is_idempotent(self):
        once = nullable_required_containers_for_shard(self._SCHEMA)
        assert nullable_required_containers_for_shard(once) == once

    def test_it_reaches_required_lists_at_every_level(self):
        """One rule for the whole schema, including ``$defs`` and ``items``."""
        schema = {
            "type": "object",
            "$defs": {
                "Item": {
                    "type": "object",
                    "properties": {"Tags": {"type": "array", "items": {}}},
                    "required": ["Tags"],
                }
            },
            "properties": {
                "Items": {"type": "array", "items": {"$ref": "#/$defs/Item"}},
                "Nested": {
                    "type": "object",
                    "properties": {"Inner": {"type": "array", "items": {}}},
                    "required": ["Inner"],
                },
            },
            "required": ["Items", "Nested"],
        }
        out = nullable_required_containers_for_shard(schema)
        assert out["properties"]["Items"]["type"] == ["array", "null"]
        assert out["properties"]["Nested"]["type"] == ["object", "null"]
        assert out["properties"]["Nested"]["properties"]["Inner"]["type"] == [
            "array",
            "null",
        ]
        assert out["$defs"]["Item"]["properties"]["Tags"]["type"] == ["array", "null"]

    def test_a_container_with_no_declared_type_is_recognised_by_shape(self):
        schema = {
            "type": "object",
            "properties": {
                "Group": {"properties": {"A": {"type": "string"}}},
                "Rows": {"items": {"type": "string"}},
            },
            "required": ["Group", "Rows"],
        }
        out = nullable_required_containers_for_shard(schema)
        assert out["properties"]["Group"]["type"] == ["object", "null"]
        assert out["properties"]["Rows"]["type"] == ["array", "null"]

    def test_a_bare_ref_property_is_the_documented_residual(self):
        """A required property that is only a ``$ref`` is NOT widened.

        There is no keyword to widen without resolving the reference, and the
        ``anyOf`` form the generator would need turns an array field into a wrapper
        model — which breaks the shard merge's list detection, a worse outcome than
        the one correction round this leaves in place. Pinned so the limitation is a
        recorded decision rather than an accident, and so a later fix has a test to
        flip.
        """
        schema = {
            "type": "object",
            "$defs": {
                "Sum": {"type": "object", "properties": {"T": {"type": "number"}}}
            },
            "properties": {"G": {"$ref": "#/$defs/Sum"}},
            "required": ["G"],
        }
        out = nullable_required_containers_for_shard(schema)
        assert out["properties"]["G"] == {"$ref": "#/$defs/Sum"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
