# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Z3-based constraint validator for business rule validation system.

This module implements the Z3_Validator component that:
- Creates Z3 variables with appropriate type mappings (Int, Real, Bool, String)
- Parses SMT-LIB constraint strings into Z3 expressions
- Binds parameter values to Z3 variables
- Invokes the Z3 solver with timeout and error handling
- Extracts models for satisfiable results
- Handles edge cases: null values, type mismatches, solver errors, timeouts

Requirements: 5.2, 5.5, 5.6, 5.7, 5.8, 9.2
"""

import logging
import time
from typing import Any, Dict, List, Optional

import z3

from .exceptions import ValidationError
from .models import Parameter, RuleJSON, ValidationResult

# Configure logging
logger = logging.getLogger(__name__)


class Z3Validator:
    """
    Validates SMT-LIB constraints using the Z3 theorem prover.

    The validator:
    1. Creates Z3 variables matching parameter declarations
    2. Parses SMT-LIB constraint strings into Z3 expressions
    3. Binds extracted parameter values to variables
    4. Invokes Z3 solver to check satisfiability
    5. Extracts models for sat results

    Handles all edge cases including null values, type mismatches, and solver errors.
    """

    def __init__(self, timeout_ms: int = 5000):
        """
        Initialize Z3 validator.

        Args:
            timeout_ms: Solver timeout in milliseconds (default: 5000ms = 5 seconds)
        """
        self.timeout_ms = timeout_ms

    def validate(
        self,
        rule_or_rule_with_values,
        extracted_values: Optional[Dict[str, Any]] = None,
    ) -> ValidationResult:
        """
        Validate constraints using Z3 solver.

        Two usage modes:
        1. Standard: validate(rule_json, extracted_values)
        2. Direct: validate(rule_with_values)

        Process:
        1. Check for null values in required parameters
        2. Create Z3 variables for all parameters
        3. Parse and add SMT-LIB constraints
        4. Bind parameter values
        5. Invoke solver
        6. Extract model if sat

        Args:
            rule_or_rule_with_values: Either RuleJSON or RuleWithValues
            extracted_values: Required if first arg is RuleJSON, ignored if RuleWithValues

        Returns:
            ValidationResult with outcome, model, and metadata

        Raises:
            ValidationError: If validation fails due to errors (not unsat)
            ValueError: If arguments are invalid

        Examples:
            # Mode 1: Standard (with data extraction)
            result = validator.validate(rule_json, extracted_values)

            # Mode 2: Direct (values already extracted)
            result = validator.validate(rule_with_values)
        """
        from .models import RuleWithValues

        # Determine which mode we're in
        if isinstance(rule_or_rule_with_values, RuleWithValues):
            # Mode 2: Direct validation
            rule_id = rule_or_rule_with_values.rule_id
            parameters = rule_or_rule_with_values.parameters
            constraints = rule_or_rule_with_values.constraints
            values = rule_or_rule_with_values.extracted_values
        elif isinstance(rule_or_rule_with_values, RuleJSON):
            # Mode 1: Standard validation
            if extracted_values is None:
                raise ValueError(
                    "extracted_values required when validating with RuleJSON. "
                    "Use RuleWithValues for validation without separate values."
                )
            rule_id = rule_or_rule_with_values.rule_id
            parameters = rule_or_rule_with_values.parameters
            constraints = rule_or_rule_with_values.constraints
            values = extracted_values
        else:
            raise ValueError(
                f"First argument must be RuleJSON or RuleWithValues, got {type(rule_or_rule_with_values)}"
            )

        start_time = time.time()

        logger.info(f"Starting Z3 validation for rule: {rule_id}")
        logger.debug(f"Parameters: {len(parameters)}, Constraints: {len(constraints)}")
        logger.debug(f"Extracted values: {values}")

        try:
            # Check for null values in required parameters (Requirement 9.2)
            null_params = self._check_null_values(parameters, values)
            if null_params:
                # Null values in required parameters — cannot evaluate the rule
                execution_time_ms = (time.time() - start_time) * 1000
                logger.info(
                    f"Validation result: error (null required parameters) - "
                    f"Solver time: {execution_time_ms:.2f}ms"
                )
                return ValidationResult(
                    rule_id=rule_id,
                    outcome="error",
                    satisfied=False,
                    extracted_values=values,
                    model=None,
                    error_message=f"Required parameters have null values: {', '.join(null_params)}",
                    execution_time_ms=execution_time_ms,
                )

            # Create Z3 solver with timeout
            logger.debug(f"Creating Z3 solver with timeout: {self.timeout_ms}ms")
            solver = z3.Solver()
            solver.set("timeout", self.timeout_ms)

            # Create Z3 variables for all parameters (Requirement 5.2)
            logger.debug("Creating Z3 variables")
            z3_vars = self._create_z3_variables(parameters, rule_id)

            # Parse and add constraints (Requirement 5.3)
            logger.debug(f"Adding {len(constraints)} constraints to solver")
            self._add_constraints(solver, constraints, z3_vars, rule_id)

            # Bind parameter values (Requirement 5.4)
            logger.debug("Binding parameter values")
            self._bind_values(solver, z3_vars, values, parameters, rule_id)

            # Check satisfiability (Requirement 5.5)
            logger.debug("Invoking Z3 solver...")
            solver_start = time.time()
            check_result = solver.check()
            solver_time_ms = (time.time() - solver_start) * 1000

            execution_time_ms = (time.time() - start_time) * 1000

            logger.info(
                f"Z3 solver completed: {check_result} - "
                f"Solver time: {solver_time_ms:.2f}ms, Total time: {execution_time_ms:.2f}ms"
            )

            # Interpret result (Requirements 5.6, 5.7)
            if check_result == z3.sat:
                # Extract model (Requirement 5.8)
                model = self._extract_model(solver.model(), z3_vars)
                return ValidationResult(
                    rule_id=rule_id,
                    outcome="sat",
                    satisfied=True,
                    extracted_values=values,
                    model=model,
                    error_message=None,
                    execution_time_ms=execution_time_ms,
                )
            elif check_result == z3.unsat:
                return ValidationResult(
                    rule_id=rule_id,
                    outcome="unsat",
                    satisfied=False,
                    extracted_values=values,
                    model=None,
                    error_message=None,
                    execution_time_ms=execution_time_ms,
                )
            else:  # unknown (timeout or other issue)
                return ValidationResult(
                    rule_id=rule_id,
                    outcome="error",
                    satisfied=False,
                    extracted_values=values,
                    model=None,
                    error_message=f"Z3 solver returned unknown (possibly timeout after {self.timeout_ms}ms)",
                    execution_time_ms=execution_time_ms,
                )

        except ValidationError:
            # Re-raise ValidationError as-is
            raise
        except Exception as e:
            # Wrap unexpected errors in ValidationError
            execution_time_ms = (time.time() - start_time) * 1000
            raise ValidationError(
                message=f"Unexpected error during validation: {str(e)}",
                operation="validate",
                rule_id=rule_id,
                constraints=constraints,
                parameter_values=values,
                context={"execution_time_ms": execution_time_ms},
            )

    def _check_null_values(
        self, parameters: List[Parameter], extracted_values: Dict[str, Any]
    ) -> List[str]:
        """
        Check for null values in required parameters.

        Requirement 9.2: When a parameter value is null, treat constraint as unsatisfiable.

        Args:
            parameters: List of parameter declarations
            extracted_values: Dictionary of extracted values

        Returns:
            List of parameter names with null values (empty if all valid)
        """
        null_params = []

        for param in parameters:
            if param.required:
                value = extracted_values.get(param.name)
                if value is None:
                    null_params.append(param.name)

        return null_params

    def _create_z3_variables(
        self, parameters: List[Parameter], rule_id: str
    ) -> Dict[str, Any]:
        """
        Create Z3 variables with types matching parameter declarations.

        Requirement 5.2: Create Z3 variables with types matching declarations.

        Type mapping:
        - Int → z3.Int
        - Real → z3.Real
        - Bool → z3.Bool
        - String → z3.String

        Args:
            parameters: List of parameter declarations
            rule_id: Rule ID for error context

        Returns:
            Dictionary mapping parameter names to Z3 variables

        Raises:
            ValidationError: If parameter type is unsupported
        """
        z3_vars = {}

        for param in parameters:
            try:
                if param.type == "Int":
                    z3_vars[param.name] = z3.Int(param.name)
                elif param.type == "Real":
                    z3_vars[param.name] = z3.Real(param.name)
                elif param.type == "Bool":
                    z3_vars[param.name] = z3.Bool(param.name)
                elif param.type == "String":
                    z3_vars[param.name] = z3.String(param.name)
                else:
                    raise ValidationError(
                        message=f"Unsupported parameter type: {param.type}",
                        operation="create_z3_variables",
                        rule_id=rule_id,
                        context={
                            "parameter_name": param.name,
                            "parameter_type": param.type,
                        },
                    )
            except Exception as e:
                if isinstance(e, ValidationError):
                    raise
                raise ValidationError(
                    message=f"Failed to create Z3 variable for parameter '{param.name}': {str(e)}",
                    operation="create_z3_variables",
                    rule_id=rule_id,
                    context={
                        "parameter_name": param.name,
                        "parameter_type": param.type,
                    },
                )

        return z3_vars

    def _add_constraints(
        self,
        solver: z3.Solver,
        constraints: List[str],
        z3_vars: Dict[str, Any],
        rule_id: str,
    ):
        """
        Parse SMT-LIB constraint strings and add them to the solver.

        Requirement 5.3: Parse SMT-LIB strings and add to solver.

        Supports:
        - Arithmetic: +, -, *, /, % (mod)
        - Comparison: =, <, >, <=, >=, != (distinct)
        - Logical: and, or, not, implies, ite
        - Functions: abs, max, min (converted to ite expressions)

        Args:
            solver: Z3 solver instance
            constraints: List of SMT-LIB constraint strings
            z3_vars: Dictionary of Z3 variables
            rule_id: Rule ID for error context

        Raises:
            ValidationError: If constraint parsing fails
        """
        for i, constraint_str in enumerate(constraints):
            try:
                # Parse the SMT-LIB constraint string into a Z3 expression
                z3_expr = self._parse_smt_constraint(
                    constraint_str, z3_vars, rule_id, i
                )
                solver.add(z3_expr)
            except ValidationError:
                raise
            except Exception as e:
                raise ValidationError(
                    message=f"Failed to parse constraint: {str(e)}",
                    operation="add_constraints",
                    rule_id=rule_id,
                    constraints=constraints,
                    constraint_index=i,
                    z3_error=str(e),
                )

    def _parse_smt_constraint(
        self,
        constraint_str: str,
        z3_vars: Dict[str, Any],
        rule_id: str,
        constraint_index: int,
    ) -> Any:
        """
        Parse an SMT-LIB constraint string into a Z3 expression.

        Uses a hand-rolled recursive-descent parser instead of z3.parse_smt2_string
        because the latter requires explicit sort/const declarations (declare-const,
        set-logic) wrapping each expression. Our LLM-generated constraints are bare
        S-expressions like "(>= x 10)" without preamble. The supported operator
        subset (arithmetic, comparison, boolean, string equality) covers all
        constraints the RuleTranslator generates. Unsupported operators surface as
        "Information Not Found" at runtime.

        Args:
            constraint_str: SMT-LIB constraint string (e.g., "(>= x 10)")
            z3_vars: Dictionary of Z3 variables
            rule_id: Rule ID for error context
            constraint_index: Index of constraint for error reporting

        Returns:
            Z3 expression

        Raises:
            ValidationError: If parsing fails
        """
        try:
            # Tokenize the constraint string
            tokens = self._tokenize_smt(constraint_str)

            # Parse the token list into a Z3 expression
            expr, pos = self._parse_smt_expr(tokens, 0, z3_vars)

            # Ensure all tokens were consumed (no trailing expressions silently dropped)
            if pos != len(tokens):
                raise ValueError(
                    f"Unexpected tokens after position {pos}: "
                    f"'{' '.join(tokens[pos:])}'. Each constraint must be a single "
                    f"S-expression. Split multiple expressions into separate constraints."
                )

            return expr
        except Exception as e:
            raise ValidationError(
                message=f"Failed to parse SMT-LIB constraint: {str(e)}",
                operation="parse_smt_constraint",
                rule_id=rule_id,
                constraints=[constraint_str],
                constraint_index=constraint_index,
                context={"constraint": constraint_str},
            )

    def _tokenize_smt(self, s: str) -> List[str]:
        """
        Tokenize an SMT-LIB expression string.

        Converts "(>= x 10)" into ["(", ">=", "x", "10", ")"]
        Handles quoted strings: '(= name "John Doe")' -> ["(", "=", "name", '"John Doe"', ")"]

        Args:
            s: SMT-LIB expression string

        Returns:
            List of tokens
        """
        tokens = []
        i = 0

        while i < len(s):
            # Skip whitespace
            if s[i].isspace():
                i += 1
                continue

            # Handle opening parenthesis
            if s[i] == "(":
                tokens.append("(")
                i += 1
                continue

            # Handle closing parenthesis
            if s[i] == ")":
                tokens.append(")")
                i += 1
                continue

            # Handle quoted strings
            if s[i] == '"':
                # Find the closing quote
                j = i + 1
                while j < len(s) and s[j] != '"':
                    # Handle escaped quotes if needed
                    if s[j] == "\\" and j + 1 < len(s):
                        j += 2
                    else:
                        j += 1

                if j < len(s):
                    # Include the quotes in the token
                    tokens.append(s[i : j + 1])
                    i = j + 1
                else:
                    # Unclosed quote - treat as regular token
                    j = i + 1
                    while j < len(s) and not s[j].isspace() and s[j] not in "()":
                        j += 1
                    tokens.append(s[i:j])
                    i = j
                continue

            # Handle regular tokens (operators, variables, numbers)
            j = i
            while j < len(s) and not s[j].isspace() and s[j] not in '()"':
                j += 1

            if j > i:
                tokens.append(s[i:j])
                i = j
            else:
                i += 1

        return tokens

    def _parse_smt_expr(
        self, tokens: List[str], pos: int, z3_vars: Dict[str, Any]
    ) -> tuple:
        """
        Parse SMT-LIB tokens into a Z3 expression.

        Recursive descent parser for S-expressions.

        Args:
            tokens: List of tokens
            pos: Current position in token list
            z3_vars: Dictionary of Z3 variables

        Returns:
            Tuple of (z3_expression, next_position)
        """
        if pos >= len(tokens):
            raise ValueError("Unexpected end of tokens")

        token = tokens[pos]

        # Handle opening parenthesis (function application)
        if token == "(":  # nosec B105 - SMT-LIB grammar literal, not a password
            pos += 1
            if pos >= len(tokens):
                raise ValueError("Unexpected end after '('")

            op = tokens[pos]
            pos += 1

            # Parse arguments
            args = []
            while pos < len(tokens) and tokens[pos] != ")":
                arg_expr, pos = self._parse_smt_expr(tokens, pos, z3_vars)
                args.append(arg_expr)

            if pos >= len(tokens):
                raise ValueError("Missing closing ')'")

            pos += 1  # Skip closing ')'

            # Apply operator to arguments
            result = self._apply_smt_operator(op, args)
            return result, pos

        # Handle closing parenthesis (shouldn't happen in well-formed input)
        elif token == ")":  # nosec B105 - SMT-LIB grammar literal, not a password
            raise ValueError("Unexpected ')'")

        # Handle atoms (variables, numbers, booleans, strings)
        else:
            return self._parse_smt_atom(token, z3_vars), pos + 1

    def _parse_smt_atom(self, token: str, z3_vars: Dict[str, Any]) -> Any:
        """
        Parse an atomic SMT-LIB token (variable, number, boolean, string).

        Args:
            token: Token string
            z3_vars: Dictionary of Z3 variables

        Returns:
            Z3 value or variable
        """
        # Check if it's a variable
        if token in z3_vars:
            return z3_vars[token]

        # Check if it's a boolean
        if token == "true":  # nosec B105 - SMT-LIB boolean literal, not a password
            return z3.BoolVal(True)
        if token == "false":  # nosec B105 - SMT-LIB boolean literal, not a password
            return z3.BoolVal(False)

        # Check if it's a number
        try:
            # Try integer first
            if "." not in token:
                return z3.IntVal(int(token))
            else:
                # Parse as real (float)
                return z3.RealVal(float(token))
        except ValueError:
            pass

        # Check if it's a string literal (quoted)
        if token.startswith('"') and token.endswith('"'):
            return z3.StringVal(token[1:-1])

        # Unknown unquoted token — likely a typo in a parameter name from LLM output.
        # Raise instead of silently coercing to StringVal, which would mask errors.
        from .exceptions import ValidationError

        raise ValidationError(
            message=f"Unknown atom '{token}' in SMT-LIB constraint. "
            f"Expected a declared parameter name, numeric literal, or quoted string. "
            f"This may indicate an LLM translation error.",
            operation="parse_atom",
        )

    def _apply_smt_operator(self, op: str, args: List[Any]) -> Any:
        """
        Apply an SMT-LIB operator to arguments.

        Supports:
        - Arithmetic: +, -, *, /, mod
        - Comparison: =, <, >, <=, >=, distinct
        - Logical: and, or, not, implies, ite

        Args:
            op: Operator name
            args: List of Z3 expressions (arguments)

        Returns:
            Z3 expression

        Raises:
            ValueError: If operator is unsupported or argument count is wrong
        """
        # Arithmetic operators
        if op == "+":
            if len(args) < 2:
                raise ValueError(
                    f"Operator '+' requires at least 2 arguments, got {len(args)}"
                )
            result = args[0]
            for arg in args[1:]:
                result = result + arg
            return result

        elif op == "-":
            if len(args) == 1:
                return -args[0]
            elif len(args) >= 2:
                result = args[0]
                for arg in args[1:]:
                    result = result - arg
                return result
            else:
                raise ValueError(
                    f"Operator '-' requires at least 1 argument, got {len(args)}"
                )

        elif op == "*":
            if len(args) < 2:
                raise ValueError(
                    f"Operator '*' requires at least 2 arguments, got {len(args)}"
                )
            result = args[0]
            for arg in args[1:]:
                result = result * arg
            return result

        elif op == "/":
            if len(args) != 2:
                raise ValueError(
                    f"Operator '/' requires exactly 2 arguments, got {len(args)}"
                )
            return args[0] / args[1]

        elif op == "mod" or op == "%":
            if len(args) != 2:
                raise ValueError(
                    f"Operator 'mod' requires exactly 2 arguments, got {len(args)}"
                )
            return args[0] % args[1]

        # Comparison operators
        elif op == "=":
            if len(args) < 2:
                raise ValueError(
                    f"Operator '=' requires at least 2 arguments, got {len(args)}"
                )
            result = args[0] == args[1]
            for arg in args[2:]:
                result = z3.And(result, args[0] == arg)
            return result

        elif op == "<":
            if len(args) != 2:
                raise ValueError(
                    f"Operator '<' requires exactly 2 arguments, got {len(args)}"
                )
            return args[0] < args[1]

        elif op == ">":
            if len(args) != 2:
                raise ValueError(
                    f"Operator '>' requires exactly 2 arguments, got {len(args)}"
                )
            return args[0] > args[1]

        elif op == "<=":
            if len(args) != 2:
                raise ValueError(
                    f"Operator '<=' requires exactly 2 arguments, got {len(args)}"
                )
            return args[0] <= args[1]

        elif op == ">=":
            if len(args) != 2:
                raise ValueError(
                    f"Operator '>=' requires exactly 2 arguments, got {len(args)}"
                )
            return args[0] >= args[1]

        elif op == "!=" or op == "distinct":
            if len(args) < 2:
                raise ValueError(
                    f"Operator 'distinct' requires at least 2 arguments, got {len(args)}"
                )
            # distinct means all arguments are pairwise different
            constraints = []
            for i in range(len(args)):
                for j in range(i + 1, len(args)):
                    constraints.append(args[i] != args[j])
            return z3.And(*constraints) if len(constraints) > 1 else constraints[0]

        # Logical operators
        elif op == "and":
            if len(args) < 1:
                raise ValueError(
                    f"Operator 'and' requires at least 1 argument, got {len(args)}"
                )
            return z3.And(*args)

        elif op == "or":
            if len(args) < 1:
                raise ValueError(
                    f"Operator 'or' requires at least 1 argument, got {len(args)}"
                )
            return z3.Or(*args)

        elif op == "not":
            if len(args) != 1:
                raise ValueError(
                    f"Operator 'not' requires exactly 1 argument, got {len(args)}"
                )
            return z3.Not(args[0])

        elif op == "implies" or op == "=>":
            if len(args) != 2:
                raise ValueError(
                    f"Operator 'implies' requires exactly 2 arguments, got {len(args)}"
                )
            return z3.Implies(args[0], args[1])

        elif op == "ite":
            if len(args) != 3:
                raise ValueError(
                    f"Operator 'ite' requires exactly 3 arguments, got {len(args)}"
                )
            return z3.If(args[0], args[1], args[2])

        else:
            raise ValueError(f"Unsupported operator: {op}")

    def _bind_values(
        self,
        solver: z3.Solver,
        z3_vars: Dict[str, Any],
        extracted_values: Dict[str, Any],
        parameters: List[Parameter],
        rule_id: str,
    ):
        """
        Bind extracted parameter values to Z3 variables.

        Requirement 5.4: Assert equality constraints binding parameters to values.

        Args:
            solver: Z3 solver instance
            z3_vars: Dictionary of Z3 variables
            extracted_values: Dictionary of extracted values
            parameters: List of parameter declarations
            rule_id: Rule ID for error context

        Raises:
            ValidationError: If type mismatch or binding fails
        """
        for param in parameters:
            param_name = param.name

            # Skip if value is None (already handled in _check_null_values)
            if (
                param_name not in extracted_values
                or extracted_values[param_name] is None
            ):
                continue

            value = extracted_values[param_name]
            z3_var = z3_vars[param_name]

            try:
                # Convert value to appropriate Z3 value based on parameter type
                if param.type == "Int":
                    if isinstance(value, bool):
                        # Avoid treating bool as int
                        raise ValidationError(
                            message=f"Type mismatch: parameter '{param_name}' expects Int, got Bool",
                            operation="bind_values",
                            rule_id=rule_id,
                            parameter_values=extracted_values,
                            context={
                                "parameter_name": param_name,
                                "expected_type": "Int",
                                "actual_value": value,
                            },
                        )
                    z3_value = z3.IntVal(int(value))

                elif param.type == "Real":
                    if isinstance(value, bool):
                        raise ValidationError(
                            message=f"Type mismatch: parameter '{param_name}' expects Real, got Bool",
                            operation="bind_values",
                            rule_id=rule_id,
                            parameter_values=extracted_values,
                            context={
                                "parameter_name": param_name,
                                "expected_type": "Real",
                                "actual_value": value,
                            },
                        )
                    z3_value = z3.RealVal(float(value))

                elif param.type == "Bool":
                    if isinstance(value, bool):
                        z3_value = z3.BoolVal(value)
                    elif isinstance(value, str):
                        # Convert string to bool
                        if value.lower() in ("true", "yes", "1"):
                            z3_value = z3.BoolVal(True)
                        elif value.lower() in ("false", "no", "0"):
                            z3_value = z3.BoolVal(False)
                        else:
                            raise ValidationError(
                                message=f"Cannot convert string '{value}' to Bool for parameter '{param_name}'",
                                operation="bind_values",
                                rule_id=rule_id,
                                parameter_values=extracted_values,
                                context={
                                    "parameter_name": param_name,
                                    "expected_type": "Bool",
                                    "actual_value": value,
                                },
                            )
                    else:
                        raise ValidationError(
                            message=f"Type mismatch: parameter '{param_name}' expects Bool, got {type(value).__name__}",
                            operation="bind_values",
                            rule_id=rule_id,
                            parameter_values=extracted_values,
                            context={
                                "parameter_name": param_name,
                                "expected_type": "Bool",
                                "actual_value": value,
                            },
                        )

                elif param.type == "String":
                    z3_value = z3.StringVal(str(value))

                else:
                    raise ValidationError(
                        message=f"Unsupported parameter type: {param.type}",
                        operation="bind_values",
                        rule_id=rule_id,
                        parameter_values=extracted_values,
                        context={
                            "parameter_name": param_name,
                            "parameter_type": param.type,
                        },
                    )

                # Add equality constraint: variable == value
                solver.add(z3_var == z3_value)

            except ValidationError:
                raise
            except Exception as e:
                raise ValidationError(
                    message=f"Failed to bind value for parameter '{param_name}': {str(e)}",
                    operation="bind_values",
                    rule_id=rule_id,
                    parameter_values=extracted_values,
                    context={
                        "parameter_name": param_name,
                        "value": value,
                        "parameter_type": param.type,
                    },
                )

    def _extract_model(
        self, z3_model: z3.ModelRef, z3_vars: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Extract satisfying assignment from Z3 model.

        Requirement 5.8: Extract model for sat results.

        Args:
            z3_model: Z3 model from solver
            z3_vars: Dictionary of Z3 variables

        Returns:
            Dictionary mapping parameter names to values in the model
        """
        model = {}

        for var_name, z3_var in z3_vars.items():
            try:
                # Get the value from the model
                value = z3_model[z3_var]

                if value is not None:
                    # Convert Z3 value to Python value
                    if z3.is_int_value(value):
                        model[var_name] = value.as_long()
                    elif z3.is_rational_value(value):
                        # Convert rational to float
                        model[var_name] = float(value.numerator_as_long()) / float(
                            value.denominator_as_long()
                        )
                    elif z3.is_true(value):
                        model[var_name] = True
                    elif z3.is_false(value):
                        model[var_name] = False
                    elif z3.is_string_value(value):
                        model[var_name] = value.as_string()
                    else:
                        # For other types, convert to string representation
                        model[var_name] = str(value)
                else:
                    model[var_name] = None
            except Exception:
                # If we can't extract the value, skip it
                model[var_name] = None

        return model
