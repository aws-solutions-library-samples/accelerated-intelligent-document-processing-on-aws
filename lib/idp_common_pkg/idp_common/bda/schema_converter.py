"""
Schema Converter Utility

This module provides functionality to convert extraction response JSON
to Bedrock Document Analysis blueprint schema format.
"""

# `reportArgumentType` is off repo-wide but on for this file, which has no findings:
# the two `str = None` defaults that produced them are `Optional[str]`. See the note
# at the top of bda_blueprint_service.py.
# pyright: reportArgumentType=error

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SchemaConverter:
    """
    Converts extraction response JSON to Bedrock Document Analysis blueprint schema format.

    The extraction response contains field information extracted from documents,
    and this class transforms it into a structured blueprint schema that can be
    used with Amazon Bedrock Document Analysis.
    """

    def __init__(
        self,
        document_class: str = "Generic-Document",
        description: str = "Document schema for data extraction",
    ):
        """
        Initialize the SchemaConverter.

        Args:
            document_class (str): The document class identifier for the schema
            description (str): Description of the document type
        """
        self.document_class = document_class
        self.description = description

    def convert(self, extraction_response: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert extraction response to blueprint schema.

        Args:
            extraction_response (dict): The extraction response JSON containing field information

        Returns:
            dict: Blueprint schema in Bedrock Document Analysis format
        """
        # Initialize the blueprint schema structure
        blueprint_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "description": self.description,
            "class": self.document_class,
            "type": "object",
            "definitions": {},
            "properties": {},
        }

        # Process each section in the extraction response
        groups = extraction_response.get("attributes", [])
        for group in groups:
            # Create a definition for this section
            section_def_name = self._format_section_name(group.get("name"))

            # Add the section definition
            blueprint_schema["definitions"][section_def_name] = {
                "type": "object",
                "properties": {},
            }

            # Process fields in this section
            group_type = group.get("attributeType")
            fields = group.get("groupAttributes", [])
            if group_type and group_type.lower() == "list":
                listItemTemplate = group.get("listItemTemplate", {})
                fields = listItemTemplate.get("itemAttributes", [])

            for field in fields:
                field_name = self._format_field_name(field.get("name", ""))
                if not field_name:
                    continue

                # Create field schema based on datatype
                field_schema = self._create_field_schema(field)

                # Add field to section definition
                blueprint_schema["definitions"][section_def_name]["properties"][
                    field_name
                ] = field_schema

            if group_type and group_type.lower() == "list":
                # Create array property for tables
                blueprint_schema["properties"][section_def_name] = {
                    "type": "array",
                    "instruction": group.get("description"),
                    "items": {"$ref": f"#/definitions/{section_def_name}"},
                }
            else:
                # Add section reference to properties
                blueprint_schema["properties"][section_def_name] = {
                    "$ref": f"#/definitions/{section_def_name}"
                }

        return blueprint_schema

    def _format_section_name(self, section_name: str) -> str:
        """Format section name to PascalCase for definitions."""
        words = section_name.replace(" ", "-").split()
        return "".join(word.capitalize() for word in words)

    def _format_field_name(self, field_name: str) -> str:
        """Format field name to snake_case for properties."""
        # Remove any non-alphanumeric characters except spaces
        field_name = "".join(
            c if c.isalnum() or c.isspace() else " " for c in field_name
        )
        # Convert to snake_case
        field_name = "".join(field_name.lower().split())
        return field_name.replace("_", "-")

    def _create_field_schema(self, field: Dict[str, Any]) -> Dict[str, Any]:
        """Create field schema based on field information."""
        datatype = field.get("dataType", "string").lower()

        # Map extraction datatypes to JSON schema types
        type_mapping = {
            "string": "string",
            "number": "number",
            "currency": "number",
            "checkbox": "boolean",
            "boolean": "boolean",
            "date": "string",
            "table": "object",
        }
        evaluation_method = "explicit"
        # field.get("evaluationMethod", "explicit")
        field_schema = {
            "type": type_mapping.get(datatype, "string"),
            "inferenceType": evaluation_method,
        }

        # Add instructions if available
        # instructions = []
        # if "description" in field:
        #    instructions.append(field["description"])

        field_schema["instruction"] = field["description"]

        # Handle special formats
        # if datatype == "date":
        #    field_schema["instruction"] = (
        #        " ".join(instructions) + " .Format as YYYY-MM-DD"
        #    )

        return field_schema

    def save_schema(self, schema: Dict[str, Any], output_path: str) -> None:
        """
        Save the schema to a JSON file.

        Args:
            schema (dict): The blueprint schema
            output_path (str): Path to save the schema JSON file
        """
        try:
            with open(output_path, "w") as f:
                json.dump(schema, f, indent=2)
            logger.info(f"Schema saved to {output_path}")
        except Exception as e:
            logger.error(f"Error saving schema: {e}")
            raise

    @classmethod
    def from_file(
        cls,
        extraction_file_path: str,
        document_class: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a blueprint schema from an extraction response file.

        Args:
            extraction_file_path (str): Path to the extraction response JSON file
            document_class (str, optional): Document class identifier
            description (str, optional): Document description

        Returns:
            dict: Blueprint schema in Bedrock Document Analysis format
        """
        try:
            with open(extraction_file_path, "r") as f:
                extraction_response = json.load(f)

            # Use filename as document class if not provided
            if not document_class:
                import os

                document_class = os.path.splitext(
                    os.path.basename(extraction_file_path)
                )[0]
                document_class = "-".join(
                    word.capitalize() for word in document_class.split("_")
                )

            # Use generic description if not provided
            if not description:
                description = f"Document schema for {document_class}"

            converter = cls(document_class=document_class, description=description)
            return converter.convert(extraction_response)
        except Exception as e:
            logger.error(f"Error creating schema from file: {e}")
            raise
