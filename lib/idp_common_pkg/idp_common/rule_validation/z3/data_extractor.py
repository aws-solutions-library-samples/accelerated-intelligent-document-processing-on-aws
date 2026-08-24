# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Data extraction component for Z3-based business rule validation system.

NOTE: This module is used by the demo notebook and Z3EngineAdapter for path-based
and LLM-assisted value extraction. The production pipeline uses orchestrator.py's
_extract_z3_values_from_facts (a dedicated LLM call) instead of this extractor.

This module implements the Data_Extractor component that extracts values from
nested JSON documents using path mappings from Rule_JSON.

Key features:
- Path traversal with dot notation support
- Type conversion (string to Int/Real/Bool)
- Caching mechanism for performance
- Comprehensive error handling
- Null vs zero distinction
"""

from typing import Any, Dict, Optional, Tuple

from .exceptions import ExtractionError
from .models import RuleJSON


class DataExtractor:
    """
    Extracts parameter values from nested JSON documents using path mappings.

    The DataExtractor traverses JSON structures using dot notation paths,
    converts values to appropriate types, and caches results for performance.

    Features:
    - Dot notation path traversal (e.g., "documents.tax_bill.inference_result.amount")
    - Type conversion: string → Int/Real/Bool based on parameter declarations
    - Caching: Avoids redundant extraction of the same path
    - Null handling: Distinguishes between null/missing and zero/empty string
    - Error context: Provides detailed error messages with available keys

    Example:
        extractor = DataExtractor()
        values = extractor.extract_values(rule_json, data)
        # Returns: {"municipal_tax": 5100.0, "sap_tax": 5200.0, ...}
    """

    def __init__(self):
        """Initialize DataExtractor with empty cache."""
        self._cache: Dict[Tuple[int, str], Any] = {}

    def extract_values(self, rule_json: RuleJSON, data: dict) -> Dict[str, Any]:
        """
        Extract parameter values from data using path mappings.

        For each path mapping in rule_json:
        1. Traverse the data using the dot notation path
        2. Convert the extracted value to the declared parameter type
        3. Handle missing/null values based on the required flag
        4. Cache results to avoid redundant extraction

        Args:
            rule_json: Rule_JSON containing path mappings and parameter declarations
            data: Nested JSON data document

        Returns:
            Dictionary mapping parameter names to extracted values

        Raises:
            ExtractionError: If required path is missing, type conversion fails,
                           or path syntax is invalid

        Example:
            rule_json = RuleJSON(...)
            data = {"documents": {"tax_bill": {"inference_result": {"amount": "5100"}}}}
            values = extractor.extract_values(rule_json, data)
            # Returns: {"tax_amount": 5100.0}
        """
        # Build parameter lookup for type information
        param_lookup = {param.name: param for param in rule_json.parameters}

        # Extract values for each path mapping
        extracted_values = {}

        for mapping in rule_json.path_mappings:
            param_name = mapping.parameter_name
            data_path = mapping.data_path

            # Get parameter declaration for type information
            if param_name not in param_lookup:
                raise ExtractionError(
                    message=f"Path mapping references undeclared parameter: {param_name}",
                    operation="extract_values",
                    rule_id=rule_json.rule_id,
                    parameter_name=param_name,
                    data_path=data_path,
                )

            param = param_lookup[param_name]

            # Extract value from data
            try:
                value = self._extract_path(data, data_path, rule_json.rule_id)
            except ExtractionError:
                # Re-raise extraction errors
                raise
            except Exception as e:
                raise ExtractionError(
                    message=f"Unexpected error extracting path: {str(e)}",
                    operation="extract_path",
                    rule_id=rule_json.rule_id,
                    parameter_name=param_name,
                    data_path=data_path,
                )

            # Handle missing/null values
            if value is None:
                if param.required:
                    raise ExtractionError(
                        message=f"Required parameter '{param_name}' has null/missing value at path '{data_path}'",
                        operation="handle_missing",
                        rule_id=rule_json.rule_id,
                        parameter_name=param_name,
                        data_path=data_path,
                        expected_type=param.type,
                    )
                else:
                    # Optional parameter with null value
                    extracted_values[param_name] = None
                    continue

            # Convert value to declared type
            try:
                converted_value = self._convert_type(
                    value, param.type, rule_json.rule_id, param_name, data_path
                )
            except ExtractionError:
                # Re-raise extraction errors
                raise
            except Exception as e:
                raise ExtractionError(
                    message=f"Unexpected error converting type: {str(e)}",
                    operation="convert_type",
                    rule_id=rule_json.rule_id,
                    parameter_name=param_name,
                    data_path=data_path,
                    expected_type=param.type,
                    actual_value=value,
                )

            extracted_values[param_name] = converted_value

        return extracted_values

    def _extract_path(
        self, data: dict, path: str, rule_id: Optional[str] = None
    ) -> Any:
        """
        Traverse nested dictionary using dot notation path.

        Supports paths like:
        - "documents.tax_bill.inference_result.amount"
        - "sap_data.transaction.financial_details.purchase_price"

        Uses caching to avoid redundant traversal of the same path.

        Args:
            data: Nested dictionary to traverse
            path: Dot-notation path string
            rule_id: Optional rule ID for error context

        Returns:
            Value at the specified path, or None if path doesn't exist

        Raises:
            ExtractionError: If path syntax is invalid
        """
        # Check cache first
        cache_key = self._cache_key(data, path)
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Validate path syntax
        if not path or not isinstance(path, str):
            raise ExtractionError(
                message=f"Path must be a non-empty string, got: {path}",
                operation="extract_path",
                rule_id=rule_id,
                data_path=path,
            )

        # Split path into components
        components = path.split(".")

        # Traverse the nested structure
        current = data
        traversed_path = []

        for component in components:
            traversed_path.append(component)

            # Check if current level is a dictionary
            if not isinstance(current, dict):
                # Path goes deeper but current value is not a dict
                # This means the path doesn't exist
                self._cache[cache_key] = None
                return None

            # Check if component exists at current level
            if component not in current:
                # Path component doesn't exist
                # Provide helpful error context with available keys
                list(current.keys()) if isinstance(current, dict) else []

                # For optional parameters, return None instead of raising error
                # The caller will handle required vs optional logic
                self._cache[cache_key] = None
                return None

            # Move to next level
            current = current[component]

        # Cache and return the result
        self._cache[cache_key] = current
        return current

    def _convert_type(
        self,
        value: Any,
        expected_type: str,
        rule_id: Optional[str] = None,
        parameter_name: Optional[str] = None,
        data_path: Optional[str] = None,
    ) -> Any:
        """
        Convert extracted value to declared parameter type.

        Type conversions:
        - Int: Convert string/number to integer
        - Real: Convert string/number to float
        - Bool: Convert string ("Yes"/"No", "true"/"false") or bool to boolean
        - String: Keep as string (no conversion needed)

        Preserves type information:
        - Integer values remain integers
        - Floating-point values remain floats
        - Strings are preserved exactly (including whitespace)

        Args:
            value: Value to convert
            expected_type: Target type ("Int", "Real", "Bool", "String")
            rule_id: Optional rule ID for error context
            parameter_name: Optional parameter name for error context
            data_path: Optional data path for error context

        Returns:
            Converted value

        Raises:
            ExtractionError: If conversion fails
        """
        # Handle None values (should be caught earlier, but defensive check)
        if value is None:
            return None

        try:
            if expected_type == "Int":
                # Convert to integer
                if isinstance(value, int):
                    return value
                elif isinstance(value, float):
                    # Check if float is actually an integer value
                    if value.is_integer():
                        return int(value)
                    else:
                        raise ValueError(
                            f"Float value {value} cannot be converted to Int without loss"
                        )
                elif isinstance(value, str):
                    # Remove whitespace and convert
                    value_str = value.strip()
                    if not value_str:
                        raise ValueError("Empty string cannot be converted to Int")
                    # Try to parse as integer
                    return int(value_str)
                else:
                    raise ValueError(f"Cannot convert {type(value).__name__} to Int")

            elif expected_type == "Real":
                # Convert to float
                if isinstance(value, (int, float)):
                    return float(value)
                elif isinstance(value, str):
                    # Remove whitespace and convert
                    value_str = value.strip()
                    if not value_str:
                        raise ValueError("Empty string cannot be converted to Real")
                    return float(value_str)
                else:
                    raise ValueError(f"Cannot convert {type(value).__name__} to Real")

            elif expected_type == "Bool":
                # Convert to boolean
                if isinstance(value, bool):
                    return value
                elif isinstance(value, str):
                    value_lower = value.strip().lower()
                    if value_lower in ("yes", "true", "1"):
                        return True
                    elif value_lower in ("no", "false", "0"):
                        return False
                    else:
                        raise ValueError(
                            f"String '{value}' cannot be converted to Bool (expected yes/no, true/false, 1/0)"
                        )
                elif isinstance(value, int):
                    return bool(value)
                else:
                    raise ValueError(f"Cannot convert {type(value).__name__} to Bool")

            elif expected_type == "String":
                # Keep as string (preserve exact content including whitespace)
                if isinstance(value, str):
                    return value
                else:
                    # Convert other types to string
                    return str(value)

            else:
                # Should not happen if Parameter validation is working
                raise ValueError(f"Unknown type: {expected_type}")

        except ValueError as e:
            raise ExtractionError(
                message=f"Type conversion failed: {str(e)}",
                operation="convert_type",
                rule_id=rule_id,
                parameter_name=parameter_name,
                data_path=data_path,
                expected_type=expected_type,
                actual_value=value,
            )

    def _cache_key(self, data: dict, path: str) -> Tuple[int, str]:
        """
        Generate cache key for memoization.

        Uses the id() of the data dictionary and the path string as the key.
        This ensures that the same path on the same data object returns cached results,
        but different data objects (even with same content) are treated separately.

        Args:
            data: Data dictionary
            path: Path string

        Returns:
            Tuple of (data_id, path) for use as cache key
        """
        return (id(data), path)

    def clear_cache(self):
        """
        Clear the extraction cache.

        Should be called when:
        - Processing a new data document
        - Memory needs to be freed
        - Testing (to ensure clean state)
        """
        self._cache.clear()
