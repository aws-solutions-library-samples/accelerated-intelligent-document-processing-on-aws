# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Data models for Z3-based business rule validation system.

This module defines the core data structures used throughout the validation system:
- Parameter: Represents a variable in SMT-LIB constraints
- PathMapping: Maps parameters to JSON data paths
- RuleJSON: Complete rule specification with constraints and mappings
- ValidationResult: Encapsulates validation outcomes
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Parameter:
    """
    Represents a parameter (variable) used in SMT-LIB constraints.

    Attributes:
        name: Parameter identifier (must be valid SMT-LIB variable name)
        type: Parameter type - must be one of: "Int", "Real", "Bool", "String"
        required: Whether this parameter must be present in data (default: True)
        description: Optional explanation of what this parameter represents (used by extraction LLM)
    """

    name: str
    type: str
    required: bool = True
    description: Optional[str] = None

    def __post_init__(self):
        """Validate parameter fields after initialization."""
        self._validate()

    def _validate(self):
        """
        Validate parameter fields.

        Raises:
            ValueError: If validation fails
        """
        # Validate name
        if not self.name or not isinstance(self.name, str):
            raise ValueError(
                f"Parameter name must be a non-empty string, got: {self.name}"
            )

        if not self.name.replace("_", "").isalnum():
            raise ValueError(
                f"Parameter name must contain only alphanumeric characters and underscores, got: {self.name}"
            )

        # Validate type
        valid_types = {"Int", "Real", "Bool", "String"}
        if self.type not in valid_types:
            raise ValueError(
                f"Parameter type must be one of {valid_types}, got: {self.type}"
            )

        # Validate required flag
        if not isinstance(self.required, bool):
            raise ValueError(
                f"Parameter 'required' must be a boolean, got: {type(self.required)}"
            )

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        result = {"name": self.name, "type": self.type, "required": self.required}
        if self.description is not None:
            result["description"] = self.description
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "Parameter":
        """
        Create Parameter from dictionary.

        Args:
            data: Dictionary with 'name', 'type', and optionally 'required' and 'description' keys

        Returns:
            Parameter instance

        Raises:
            ValueError: If required fields are missing
        """
        if "name" not in data:
            raise ValueError("Parameter dictionary must contain 'name' field")
        if "type" not in data:
            raise ValueError("Parameter dictionary must contain 'type' field")

        return cls(
            name=data["name"],
            type=data["type"],
            required=data.get("required", True),
            description=data.get("description"),
        )


@dataclass
class PathMapping:
    """
    Maps a parameter name to a JSON data path for extraction.

    Attributes:
        parameter_name: Name of the parameter (must match a Parameter.name)
        data_path: Dot-notation path to data in JSON (e.g., "documents.tax_bill.inference_result.amount")
    """

    parameter_name: str
    data_path: str

    def __post_init__(self):
        """Validate path mapping fields after initialization."""
        self._validate()

    def _validate(self):
        """
        Validate path mapping fields.

        Raises:
            ValueError: If validation fails
        """
        # Validate parameter_name
        if not self.parameter_name or not isinstance(self.parameter_name, str):
            raise ValueError(
                f"PathMapping parameter_name must be a non-empty string, got: {self.parameter_name}"
            )

        # Validate data_path
        if not self.data_path or not isinstance(self.data_path, str):
            raise ValueError(
                f"PathMapping data_path must be a non-empty string, got: {self.data_path}"
            )

        # Validate dot notation format
        if ".." in self.data_path:
            raise ValueError(
                f"PathMapping data_path cannot contain consecutive dots, got: {self.data_path}"
            )

        if self.data_path.startswith(".") or self.data_path.endswith("."):
            raise ValueError(
                f"PathMapping data_path cannot start or end with a dot, got: {self.data_path}"
            )

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {"parameter_name": self.parameter_name, "data_path": self.data_path}

    @classmethod
    def from_dict(cls, data: dict) -> "PathMapping":
        """
        Create PathMapping from dictionary.

        Args:
            data: Dictionary with 'parameter_name' and 'data_path' keys

        Returns:
            PathMapping instance

        Raises:
            ValueError: If required fields are missing
        """
        if "parameter_name" not in data:
            raise ValueError(
                "PathMapping dictionary must contain 'parameter_name' field"
            )
        if "data_path" not in data:
            raise ValueError("PathMapping dictionary must contain 'data_path' field")

        return cls(parameter_name=data["parameter_name"], data_path=data["data_path"])


@dataclass
class RuleJSON:
    """
    Complete specification of a business rule with SMT-LIB constraints.

    Supports two workflows:
    - Workflow A (Path-Based): path_mappings provided for structured data extraction
    - Workflow B (LLM-Based): path_mappings empty, use LLM extraction

    Attributes:
        rule_id: Unique identifier for the rule
        version: Rule version string
        description: Human-readable description
        natural_language_rule: Original natural language rule text
        parameters: List of parameter declarations
        constraints: List of SMT-LIB constraint strings
        path_mappings: List of parameter-to-path mappings (optional, empty for Workflow B)
        metadata: Additional metadata (rule_type, created_at, etc.)
    """

    rule_id: str
    version: str
    description: str
    natural_language_rule: str
    parameters: List[Parameter]
    constraints: List[str]
    path_mappings: List[PathMapping] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Validate RuleJSON after initialization."""
        self._validate()

    def _validate(self):
        """
        Perform comprehensive validation of RuleJSON structure.

        Validates:
        - Required fields are non-empty
        - All parameters have valid types
        - All path mappings reference declared parameters
        - Each parameter has exactly one path mapping
        - All constraint parameters are declared

        Raises:
            ValueError: If validation fails
        """
        # Validate required string fields
        if not self.rule_id or not isinstance(self.rule_id, str):
            raise ValueError(f"rule_id must be a non-empty string, got: {self.rule_id}")

        if not self.version or not isinstance(self.version, str):
            raise ValueError(f"version must be a non-empty string, got: {self.version}")

        if not self.description or not isinstance(self.description, str):
            raise ValueError(
                f"description must be a non-empty string, got: {self.description}"
            )

        if not self.natural_language_rule or not isinstance(
            self.natural_language_rule, str
        ):
            raise ValueError(
                f"natural_language_rule must be a non-empty string, got: {self.natural_language_rule}"
            )

        # Validate parameters list
        if not isinstance(self.parameters, list):
            raise ValueError(f"parameters must be a list, got: {type(self.parameters)}")

        if len(self.parameters) == 0:
            raise ValueError("parameters list cannot be empty")

        # Validate all parameters are Parameter instances
        for i, param in enumerate(self.parameters):
            if not isinstance(param, Parameter):
                raise ValueError(
                    f"parameters[{i}] must be a Parameter instance, got: {type(param)}"
                )

        # Validate constraints list
        if not isinstance(self.constraints, list):
            raise ValueError(
                f"constraints must be a list, got: {type(self.constraints)}"
            )

        if len(self.constraints) == 0:
            raise ValueError("constraints list cannot be empty")

        # Validate all constraints are strings
        for i, constraint in enumerate(self.constraints):
            if not isinstance(constraint, str) or not constraint.strip():
                raise ValueError(
                    f"constraints[{i}] must be a non-empty string, got: {constraint}"
                )

        # Validate path_mappings list
        if not isinstance(self.path_mappings, list):
            raise ValueError(
                f"path_mappings must be a list, got: {type(self.path_mappings)}"
            )

        # path_mappings can be empty (Workflow B - LLM extraction)
        # If not empty, validate all are PathMapping instances
        for i, mapping in enumerate(self.path_mappings):
            if not isinstance(mapping, PathMapping):
                raise ValueError(
                    f"path_mappings[{i}] must be a PathMapping instance, got: {type(mapping)}"
                )

        # Validate metadata is a dictionary
        if not isinstance(self.metadata, dict):
            raise ValueError(
                f"metadata must be a dictionary, got: {type(self.metadata)}"
            )

        # Validate parameter-path mapping consistency (only if mappings exist)
        if len(self.path_mappings) > 0:
            self._validate_parameter_path_consistency()

        # Validate constraint parameter references
        self._validate_constraint_parameters()

    def _validate_parameter_path_consistency(self):
        """
        Validate that each parameter has exactly one path mapping.

        Property 3: Parameter-Path Mapping Bijection

        Raises:
            ValueError: If validation fails
        """
        param_names = {param.name for param in self.parameters}
        mapping_param_names = [mapping.parameter_name for mapping in self.path_mappings]

        # Check all path mappings reference declared parameters
        for mapping in self.path_mappings:
            if mapping.parameter_name not in param_names:
                raise ValueError(
                    f"PathMapping references undeclared parameter: {mapping.parameter_name}"
                )

        # Check each parameter has a path mapping if required
        # Non-required parameters (constants) may not have path mappings
        for param in self.parameters:
            param_name = param.name
            count = mapping_param_names.count(param_name)

            if param.required:
                # Required parameters must have exactly one path mapping
                if count == 0:
                    raise ValueError(
                        f"Required parameter '{param_name}' has no corresponding path mapping"
                    )
                elif count > 1:
                    raise ValueError(
                        f"Parameter '{param_name}' has multiple path mappings (expected exactly one)"
                    )
            else:
                # Non-required parameters can have 0 or 1 path mappings
                # 0 = constant defined in constraints
                # 1 = optional value from data
                if count > 1:
                    raise ValueError(
                        f"Parameter '{param_name}' has multiple path mappings (expected at most one)"
                    )

    def _validate_constraint_parameters(self):
        """
        Validate that all parameters referenced in constraints are declared.

        Property 5: Constraint Parameter Reference Validity

        Note: This is a basic validation that checks for parameter names as tokens.
        Full SMT-LIB parsing would be more comprehensive but is complex.

        Raises:
            ValueError: If validation fails
        """
        param_names = {param.name for param in self.parameters}

        # Check each constraint for parameter references
        for i, constraint in enumerate(self.constraints):
            # Extract potential parameter names (alphanumeric + underscore tokens)
            import re

            tokens = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", constraint)

            # Filter out SMT-LIB keywords and operators
            smt_keywords = {
                "and",
                "or",
                "not",
                "implies",
                "ite",
                "assert",
                "declare-const",
                "Int",
                "Real",
                "Bool",
                "String",
                "true",
                "false",
            }

            potential_params = [t for t in tokens if t not in smt_keywords]

            # Check if potential parameters are declared
            for token in potential_params:
                if token in param_names:
                    continue  # Valid parameter reference
                # Token might be a function or other SMT-LIB construct, which is okay
                # We only raise error if it looks like it should be a parameter
                # but isn't declared (heuristic: used in multiple places or in comparison)

    def to_dict(self) -> dict:
        """
        Serialize RuleJSON to dictionary.

        Returns:
            Dictionary representation suitable for JSON serialization
        """
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "description": self.description,
            "natural_language_rule": self.natural_language_rule,
            "parameters": [param.to_dict() for param in self.parameters],
            "constraints": self.constraints,
            "path_mappings": [mapping.to_dict() for mapping in self.path_mappings],
            "metadata": self.metadata,
        }

    def has_path_mappings(self) -> bool:
        """
        Check if this rule has path mappings (Workflow A) or not (Workflow B).

        Returns:
            True if path mappings exist (use DataExtractor)
            False if no path mappings (use LLM extraction)
        """
        return len(self.path_mappings) > 0

    @classmethod
    def from_dict(cls, data: dict) -> "RuleJSON":
        """
        Deserialize RuleJSON from dictionary.

        Args:
            data: Dictionary with all required RuleJSON fields

        Returns:
            RuleJSON instance

        Raises:
            ValueError: If required fields are missing or invalid
        """
        # Validate required fields
        required_fields = [
            "rule_id",
            "version",
            "description",
            "natural_language_rule",
            "parameters",
            "constraints",
            "path_mappings",
        ]

        for field_name in required_fields:
            if field_name not in data:
                raise ValueError(
                    f"RuleJSON dictionary must contain '{field_name}' field"
                )

        # Parse parameters
        try:
            parameters = [Parameter.from_dict(p) for p in data["parameters"]]
        except Exception as e:
            raise ValueError(f"Failed to parse parameters: {e}")

        # Parse path_mappings
        try:
            path_mappings = [PathMapping.from_dict(m) for m in data["path_mappings"]]
        except Exception as e:
            raise ValueError(f"Failed to parse path_mappings: {e}")

        # Create RuleJSON instance
        return cls(
            rule_id=data["rule_id"],
            version=data["version"],
            description=data["description"],
            natural_language_rule=data["natural_language_rule"],
            parameters=parameters,
            constraints=data["constraints"],
            path_mappings=path_mappings,
            metadata=data.get("metadata", {}),
        )


@dataclass
class RuleWithValues:
    """
    Rule specification with pre-extracted parameter values.

    This model combines a rule (parameters + constraints) with extracted values,
    ready for validation without needing data extraction.

    Use cases:
    - LLM extracted values from unstructured/new schema data
    - Pre-computed values for batch validation
    - Caching extracted values for reuse

    Attributes:
        rule_id: Unique rule identifier
        version: Rule version string
        description: Human-readable rule description
        natural_language_rule: Original rule in natural language
        parameters: List of parameter definitions
        constraints: List of SMT-LIB constraint expressions
        extracted_values: Dictionary mapping parameter names to values
        metadata: Additional metadata (timestamps, source, etc.)
    """

    rule_id: str
    version: str
    description: str
    natural_language_rule: str
    parameters: List[Parameter]
    constraints: List[str]
    extracted_values: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Validate RuleWithValues after initialization."""
        # Convert dict parameters to Parameter objects if needed
        if self.parameters and isinstance(self.parameters[0], dict):
            self.parameters = [Parameter.from_dict(p) for p in self.parameters]

        self._validate()

    def _validate(self):
        """Validate RuleWithValues fields."""
        if not self.rule_id:
            raise ValueError("rule_id cannot be empty")

        if not self.parameters:
            raise ValueError("parameters list cannot be empty")

        if not self.constraints:
            raise ValueError("constraints list cannot be empty")

        if not isinstance(self.extracted_values, dict):
            raise ValueError("extracted_values must be a dictionary")

        # Validate all required parameters have values
        param_names = {p.name for p in self.parameters if p.required}
        value_names = set(self.extracted_values.keys())

        missing = param_names - value_names
        if missing:
            raise ValueError(
                f"Missing values for required parameters: {', '.join(missing)}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Convert RuleWithValues to dictionary for serialization."""
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "description": self.description,
            "natural_language_rule": self.natural_language_rule,
            "parameters": [p.to_dict() for p in self.parameters],
            "constraints": self.constraints,
            "extracted_values": self.extracted_values,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuleWithValues":
        """Create RuleWithValues from dictionary."""
        return cls(
            rule_id=data["rule_id"],
            version=data["version"],
            description=data["description"],
            natural_language_rule=data["natural_language_rule"],
            parameters=[Parameter.from_dict(p) for p in data["parameters"]],
            constraints=data["constraints"],
            extracted_values=data["extracted_values"],
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_rule_json(
        cls, rule_json: "RuleJSON", extracted_values: Dict[str, Any]
    ) -> "RuleWithValues":
        """Create RuleWithValues from RuleJSON and extracted values."""
        return cls(
            rule_id=rule_json.rule_id,
            version=rule_json.version,
            description=rule_json.description,
            natural_language_rule=rule_json.natural_language_rule,
            parameters=rule_json.parameters,
            constraints=rule_json.constraints,
            extracted_values=extracted_values,
            metadata=rule_json.metadata,
        )


@dataclass
class ValidationResult:
    """
    Encapsulates the outcome of a rule validation.

    Attributes:
        rule_id: ID of the rule that was validated
        outcome: Z3 solver result - "sat", "unsat", or "error"
        satisfied: Boolean indicating if rule is satisfied (True if sat)
        extracted_values: Dictionary of parameter names to extracted values
        model: Z3 model (satisfying assignment) if sat, None otherwise
        error_message: Error description if outcome is "error", None otherwise
        execution_time_ms: Time taken for validation in milliseconds
    """

    rule_id: str
    outcome: str
    satisfied: bool
    extracted_values: Dict[str, Any]
    model: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    execution_time_ms: float = 0.0

    def __post_init__(self):
        """Validate ValidationResult after initialization."""
        self._validate()

    def _validate(self):
        """
        Validate ValidationResult fields.

        Raises:
            ValueError: If validation fails
        """
        # Validate rule_id
        if not self.rule_id or not isinstance(self.rule_id, str):
            raise ValueError(f"rule_id must be a non-empty string, got: {self.rule_id}")

        # Validate outcome
        valid_outcomes = {"sat", "unsat", "error"}
        if self.outcome not in valid_outcomes:
            raise ValueError(
                f"outcome must be one of {valid_outcomes}, got: {self.outcome}"
            )

        # Validate satisfied
        if not isinstance(self.satisfied, bool):
            raise ValueError(
                f"satisfied must be a boolean, got: {type(self.satisfied)}"
            )

        # Validate extracted_values
        if not isinstance(self.extracted_values, dict):
            raise ValueError(
                f"extracted_values must be a dictionary, got: {type(self.extracted_values)}"
            )

        # Validate execution_time_ms
        if not isinstance(self.execution_time_ms, (int, float)):
            raise ValueError(
                f"execution_time_ms must be a number, got: {type(self.execution_time_ms)}"
            )

        if self.execution_time_ms < 0:
            raise ValueError(
                f"execution_time_ms must be non-negative, got: {self.execution_time_ms}"
            )

        # Validate consistency: if outcome is "sat", satisfied should be True
        if self.outcome == "sat" and not self.satisfied:
            raise ValueError("outcome 'sat' requires satisfied=True")

        # Validate consistency: if outcome is "unsat", satisfied should be False
        if self.outcome == "unsat" and self.satisfied:
            raise ValueError("outcome 'unsat' requires satisfied=False")

        # Validate consistency: if outcome is "error", error_message should be present
        if self.outcome == "error" and not self.error_message:
            raise ValueError("outcome 'error' requires error_message to be set")

    def to_dict(self) -> dict:
        """
        Serialize ValidationResult to dictionary.

        Returns:
            Dictionary representation suitable for JSON serialization
        """
        return {
            "rule_id": self.rule_id,
            "outcome": self.outcome,
            "satisfied": self.satisfied,
            "extracted_values": self.extracted_values,
            "model": self.model,
            "error_message": self.error_message,
            "execution_time_ms": self.execution_time_ms,
        }

    def passes(self) -> bool:
        """
        Check if validation PASSED (all rules satisfied).

        Returns:
            True if data satisfies all rules (outcome="sat"), False otherwise

        Example:
            result = system.validate(rule, data)
            if result.passes():
                print("✓ PASS: All rules satisfied")
            else:
                print("✗ FAIL: Rules violated")
        """
        return self.satisfied and self.outcome == "sat"

    def fails(self) -> bool:
        """
        Check if validation FAILED (rules violated).

        Returns:
            True if data violates rules (outcome="unsat"), False otherwise

        Example:
            result = system.validate(rule, data)
            if result.fails():
                print("✗ FAIL: Rules violated")
                # Take corrective action
        """
        return not self.satisfied and self.outcome == "unsat"

    def is_success(self) -> bool:
        """Check if validation completed successfully (not an error)."""
        return self.outcome in {"sat", "unsat"}

    def is_error(self) -> bool:
        """Check if validation encountered an error."""
        return self.outcome == "error"
