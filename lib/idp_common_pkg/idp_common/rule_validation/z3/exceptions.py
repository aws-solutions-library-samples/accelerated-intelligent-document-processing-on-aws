# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Custom exception classes for Z3-based business rule validation system.

This module defines a hierarchy of exceptions with rich context tracking:
- ValidationSystemError: Base exception with full context tracking
- TranslationError: Errors during LLM-based rule translation
- ExtractionError: Errors during data extraction from JSON
- ValidationError: Errors during Z3 constraint validation

All exceptions include context information for debugging and logging.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional


class ValidationSystemError(Exception):
    """
    Base exception for all validation system errors.

    Provides comprehensive context tracking including:
    - Component name (which part of the system failed)
    - Operation being performed
    - Rule ID (if applicable)
    - Timestamp
    - Additional context data

    Attributes:
        message: Human-readable error description
        component: Name of the component where error occurred
        operation: Operation being performed when error occurred
        rule_id: ID of the rule being processed (optional)
        timestamp: When the error occurred
        context: Additional context data (optional)
    """

    def __init__(
        self,
        message: str,
        component: str,
        operation: str,
        rule_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize ValidationSystemError with context.

        Args:
            message: Human-readable error description
            component: Component name (e.g., "Rule_Translator", "Data_Extractor", "Z3_Validator")
            operation: Operation being performed (e.g., "translate_rule", "extract_path", "solve_constraints")
            rule_id: Optional rule ID being processed
            context: Optional dictionary with additional context data
        """
        super().__init__(message)
        self.message = message
        self.component = component
        self.operation = operation
        self.rule_id = rule_id
        self.timestamp = datetime.now(timezone.utc)
        self.context = context or {}

    def __str__(self) -> str:
        """
        Format error message with full context.

        Returns:
            Formatted error string with component, operation, rule_id, and message
        """
        parts = [f"[{self.component}]", f"Operation: {self.operation}"]

        if self.rule_id:
            parts.append(f"Rule ID: {self.rule_id}")

        parts.append(f"Error: {self.message}")

        if self.context:
            context_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
            parts.append(f"Context: {{{context_str}}}")

        parts.append(f"Timestamp: {self.timestamp.isoformat()}")

        return " | ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert exception to dictionary for structured logging.

        Returns:
            Dictionary with all error context fields
        """
        return {
            "error_type": self.__class__.__name__,
            "message": self.message,
            "component": self.component,
            "operation": self.operation,
            "rule_id": self.rule_id,
            "timestamp": self.timestamp.isoformat(),
            "context": self.context,
        }


class TranslationError(ValidationSystemError):
    """
    Exception raised during rule translation (LLM invocation and parsing).

    Specific context fields:
    - natural_language_rule: The rule that failed to translate
    - llm_response: Raw LLM output (if available)
    - validation_errors: List of validation errors (if applicable)

    Common scenarios:
    - LLM API failures (timeout, rate limit, service unavailable)
    - Invalid LLM output (malformed JSON, missing fields)
    - Schema validation failures
    """

    def __init__(
        self,
        message: str,
        operation: str,
        rule_id: Optional[str] = None,
        natural_language_rule: Optional[str] = None,
        llm_response: Optional[str] = None,
        validation_errors: Optional[list] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize TranslationError with translation-specific context.

        Args:
            message: Human-readable error description
            operation: Operation being performed (e.g., "invoke_bedrock", "parse_llm_output")
            rule_id: Optional rule ID
            natural_language_rule: The rule text that failed to translate
            llm_response: Raw LLM response (for debugging)
            validation_errors: List of validation error messages
            context: Additional context data
        """
        # Build context dictionary with translation-specific fields
        full_context = context or {}

        if natural_language_rule:
            full_context["natural_language_rule"] = natural_language_rule

        if llm_response:
            # Truncate long responses for readability
            max_len = 500
            if len(llm_response) > max_len:
                full_context["llm_response"] = (
                    llm_response[:max_len] + "... (truncated)"
                )
            else:
                full_context["llm_response"] = llm_response

        if validation_errors:
            full_context["validation_errors"] = validation_errors

        super().__init__(
            message=message,
            component="Rule_Translator",
            operation=operation,
            rule_id=rule_id,
            context=full_context,
        )

        # Store as attributes for easy access
        self.natural_language_rule = natural_language_rule
        self.llm_response = llm_response
        self.validation_errors = validation_errors


class ExtractionError(ValidationSystemError):
    """
    Exception raised during data extraction from JSON documents.

    Specific context fields:
    - data_path: The path that failed to extract
    - parameter_name: Name of the parameter being extracted
    - expected_type: Expected data type
    - actual_value: Actual value found (if any)
    - available_keys: Available keys at the failed path level

    Common scenarios:
    - Missing required paths
    - Type conversion failures
    - Malformed path syntax
    - Null values for required parameters
    """

    def __init__(
        self,
        message: str,
        operation: str,
        rule_id: Optional[str] = None,
        data_path: Optional[str] = None,
        parameter_name: Optional[str] = None,
        expected_type: Optional[str] = None,
        actual_value: Any = None,
        available_keys: Optional[list] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize ExtractionError with extraction-specific context.

        Args:
            message: Human-readable error description
            operation: Operation being performed (e.g., "extract_path", "convert_type")
            rule_id: Optional rule ID
            data_path: The JSON path that failed
            parameter_name: Name of the parameter being extracted
            expected_type: Expected parameter type (Int, Real, Bool, String)
            actual_value: Actual value found at the path
            available_keys: List of available keys at the failed path level
            context: Additional context data
        """
        # Build context dictionary with extraction-specific fields
        full_context = context or {}

        if data_path:
            full_context["data_path"] = data_path

        if parameter_name:
            full_context["parameter_name"] = parameter_name

        if expected_type:
            full_context["expected_type"] = expected_type

        if actual_value is not None:
            full_context["actual_value"] = str(actual_value)
            full_context["actual_type"] = type(actual_value).__name__

        if available_keys:
            full_context["available_keys"] = available_keys

        super().__init__(
            message=message,
            component="Data_Extractor",
            operation=operation,
            rule_id=rule_id,
            context=full_context,
        )

        # Store as attributes for easy access
        self.data_path = data_path
        self.parameter_name = parameter_name
        self.expected_type = expected_type
        self.actual_value = actual_value
        self.available_keys = available_keys


class ValidationError(ValidationSystemError):
    """
    Exception raised during Z3 constraint validation.

    Specific context fields:
    - constraints: SMT-LIB constraint strings being validated
    - parameter_values: Dictionary of parameter values
    - z3_error: Raw Z3 error message (if applicable)
    - constraint_index: Index of the constraint that failed (if applicable)

    Common scenarios:
    - Malformed SMT-LIB constraints
    - Z3 solver errors
    - Type mismatches between parameters and Z3 variables
    - Solver timeouts
    - Unsupported operations
    """

    def __init__(
        self,
        message: str,
        operation: str,
        rule_id: Optional[str] = None,
        constraints: Optional[list] = None,
        parameter_values: Optional[Dict[str, Any]] = None,
        z3_error: Optional[str] = None,
        constraint_index: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize ValidationError with validation-specific context.

        Args:
            message: Human-readable error description
            operation: Operation being performed (e.g., "parse_constraint", "solve", "bind_values")
            rule_id: Optional rule ID
            constraints: List of SMT-LIB constraint strings
            parameter_values: Dictionary of parameter names to values
            z3_error: Raw Z3 error message
            constraint_index: Index of the constraint that caused the error
            context: Additional context data
        """
        # Build context dictionary with validation-specific fields
        full_context = context or {}

        if constraints:
            full_context["constraints"] = constraints
            full_context["constraint_count"] = len(constraints)

        if parameter_values:
            full_context["parameter_values"] = parameter_values
            full_context["parameter_count"] = len(parameter_values)

        if z3_error:
            full_context["z3_error"] = z3_error

        if constraint_index is not None:
            full_context["constraint_index"] = constraint_index
            if constraints and 0 <= constraint_index < len(constraints):
                full_context["failed_constraint"] = constraints[constraint_index]

        super().__init__(
            message=message,
            component="Z3_Validator",
            operation=operation,
            rule_id=rule_id,
            context=full_context,
        )

        # Store as attributes for easy access
        self.constraints = constraints
        self.parameter_values = parameter_values
        self.z3_error = z3_error
        self.constraint_index = constraint_index
