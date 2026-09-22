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
- Per-call memoization of path readings
- Comprehensive error handling
- Null vs zero distinction
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from idp_common.schema.multi_instance import INSTANCES_KEY

from .exceptions import ExtractionError
from .models import RuleJSON
from .type_coercion import NUMERIC_TYPES, coerce_numeric_reading

logger = logging.getLogger(__name__)

# `name`, `name[0]`, `name[0][2]`, `name[-1]`.
#
# The key must be NON-EMPTY. Allowing an empty one let `a.[0].b` match with
# key="", which resolved to a silent miss — indistinguishable from an absent
# optional parameter, which is the exact failure mode this module's error handling
# exists to avoid.
_SUBSCRIPT_RE = re.compile(r"^(?P<key>[^\[\]]+)(?P<subs>(?:\[-?\d+\])+)$")
# A subscript with no property name in front of it: `[0]`, `[-1]`. Not a legal
# dot-notation segment, so it is a malformed path rather than a key.
_EMPTY_KEY_SUBSCRIPT_RE = re.compile(r"^(?:\[-?\d+\])+$")


def _split_subscripts(
    component: str, path: str, rule_id: Optional[str]
) -> Tuple[str, List[int]]:
    """Split ``name[0][2]`` into ``("name", [0, 2])``.

    A component with no subscripts returns an empty index list, so the ordinary
    dot-notation path is unaffected.

    A component is treated as subscripted ONLY when it is a non-empty key followed
    by one or more bracketed integers. Anything else is an ordinary key, brackets
    and all — so a legitimate data key like ``Amount[USD]`` (plausible in an ERP
    payload) still resolves by plain lookup and still just *misses*, exactly as it
    did before subscripts existed. Turning that into a hard error would convert
    "this rule was never firing" into "this document fails", which is a worse
    outcome than the latent bug.

    The one shape that IS rejected is a subscript with no key at all (``[0]``,
    ``.[1].x``). That cannot be a real dot-notation segment, so it is unambiguously
    a malformed path — and letting it match with an empty key resolved it to a
    silent miss, indistinguishable from an absent optional parameter, which is the
    exact failure mode this module's error handling exists to avoid.
    """
    if _EMPTY_KEY_SUBSCRIPT_RE.match(component):
        raise ExtractionError(
            message=(
                f"Invalid list subscript in path component {component!r} (in "
                f"path {path!r}): a subscript needs a property name before it, "
                f"e.g. name[0]"
            ),
            operation="extract_path",
            rule_id=rule_id,
            data_path=path,
        )
    match = _SUBSCRIPT_RE.match(component)
    if not match or not match.group("subs"):
        return component, []
    subs = match.group("subs")
    indices = [int(value) for value in re.findall(r"\[(-?\d+)\]", subs)]
    return match.group("key"), indices


class DataExtractor:
    """
    Extracts parameter values from nested JSON documents using path mappings.

    The DataExtractor traverses JSON structures using dot notation paths and
    converts values to the types the rule declares.

    Features:
    - Dot notation path traversal (e.g., "documents.tax_bill.inference_result.amount")
    - Type conversion: string → Int/Real/Bool based on parameter declarations
    - Path memoization within one extract_values call
    - Null handling: Distinguishes between null/missing and zero/empty string
    - Error context: Provides detailed error messages with available keys

    The extractor holds **no per-document state**, so one instance is safe to
    reuse across documents: each ``extract_values`` call memoizes its own path
    lookups and discards them on return. The readings this class produces are the
    values bound into the solver, so a reading served for the wrong document is a
    compliance verdict computed against another document's data — reported with
    normal confidence and no signal that it happened. Keeping the memo inside the
    call is what makes that unrepresentable; see ``_extract_path``.

    Example:
        extractor = DataExtractor()
        values = extractor.extract_values(rule_json, data)
        # Returns: {"municipal_tax": 5100.0, "sap_tax": 5200.0, ...}
    """

    def __init__(self):
        """Initialize DataExtractor. The extractor carries no document state."""

    def extract_values(self, rule_json: RuleJSON, data: dict) -> Dict[str, Any]:
        """
        Extract parameter values from data using path mappings.

        For each path mapping in rule_json:
        1. Traverse the data using the dot notation path
        2. Convert the extracted value to the declared parameter type
        3. Handle missing/null values based on the required flag
        4. Memoize path lookups for the duration of this call

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

        # One memo per call, holding readings of THIS `data` only. Two parameters
        # may share a data_path, which is the only redundancy there is to avoid
        # here, and it is entirely within one call.
        path_cache: Dict[str, Any] = {}

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
                value = self._extract_path(
                    data, data_path, rule_json.rule_id, path_cache=path_cache
                )
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
        self,
        data: dict,
        path: str,
        rule_id: Optional[str] = None,
        path_cache: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Traverse nested dictionary using dot notation path.

        Supports paths like:
        - "documents.tax_bill.inference_result.amount"
        - "sap_data.transaction.financial_details.purchase_price"
        - "documents.pay_statement.inference_result.instances[0].NetPay"
        - "documents.invoice.inference_result.instances[1].line_items[2].amount"

        List subscripts (``name[i]``, and chains like ``name[0][1]``) exist so a
        rule can address a specific element of a list — necessary for a
        multi-instance class (GitHub #715), whose extraction result is
        ``{"instances": [ … ]}``, and useful for any list attribute. A negative
        index counts from the end, as in Python. An out-of-range index resolves to
        ``None``, the same as a missing key.

        ``path_cache`` memoizes readings of ``data`` and is supplied by
        ``extract_values``, which creates one per call and drops it on return. It
        is keyed by path alone, which is correct **because** its lifetime is one
        call against one ``data`` object: there is no second document for a key to
        confuse it with. Anything longer-lived would have to identify the document
        as well, and identity is the part that has no cheap answer — ``id(data)``
        is an address CPython recycles, so a freed document's entries can be served
        to whatever is allocated next (#1115), and a content hash costs about four
        milliseconds per megabyte to avoid a sub-microsecond dict walk.

        Args:
            data: Nested dictionary to traverse
            path: Dot-notation path string
            rule_id: Optional rule ID for error context
            path_cache: Per-call memo of readings of ``data``. A caller that omits
                it gets a memo scoped to this single call, i.e. no memoization.

        Returns:
            Value at the specified path, or None if path doesn't exist

        Raises:
            ExtractionError: If path syntax is invalid
        """
        if path_cache is None:
            path_cache = {}

        # Check the memo first. `None` is a legitimate cached reading (a miss), so
        # membership is the test, not truthiness.
        if path in path_cache:
            return path_cache[path]

        # Validate path syntax
        if not path or not isinstance(path, str):
            raise ExtractionError(
                message=f"Path must be a non-empty string, got: {path}",
                operation="extract_path",
                rule_id=rule_id,
                data_path=path,
            )

        # Split path into components
        # Parse EVERY component before traversing, so a malformed path is reported
        # as malformed whatever the data happens to contain. Parsing lazily inside
        # the loop meant a bad segment later in the path was masked by an ordinary
        # miss earlier in it — the error surfaced or not depending on the document,
        # which is the worst of both behaviours.
        components = [
            (component, *_split_subscripts(component, path, rule_id))
            for component in path.split(".")
        ]

        # Traverse the nested structure
        current = data
        traversed_path = []

        for component, key, indices in components:
            traversed_path.append(component)

            # Check if current level is a dictionary
            if not isinstance(current, dict):
                # Path goes deeper but current value is not a dict
                # This means the path doesn't exist
                path_cache[path] = None
                return None

            # Check if component exists at current level
            if key not in current:
                # Path component doesn't exist. Returning None (rather than
                # raising) is deliberate — the caller decides required vs
                # optional — but that means a path that has become WRONG looks
                # identical to an optional parameter that is simply absent, and
                # the rule quietly stops firing. The one case where we can
                # confidently name the cause is a multi-instance class (#715),
                # whose result is {"instances": [...]}: say so instead of
                # leaving the author to wonder why the rule went silent.
                if INSTANCES_KEY in current and key != INSTANCES_KEY:
                    logger.warning(
                        "Rule path '%s' looks for '%s' at '%s', which is not "
                        "there — but that level DOES have a '%s' list, so this "
                        "looks like a multi-instance class whose records sit one "
                        "level down. The rule will NOT fire as written. Address "
                        "an instance explicitly, e.g. '%s[0].%s'.",
                        path,
                        key,
                        ".".join(traversed_path[:-1]) or "<root>",
                        INSTANCES_KEY,
                        INSTANCES_KEY,
                        key,
                    )
                path_cache[path] = None
                return None

            # Move to next level
            current = current[key]

            for index in indices:
                if not isinstance(current, (list, tuple)):
                    path_cache[path] = None
                    return None
                try:
                    current = current[index]
                except IndexError:
                    # Out of range is a miss, not an error — same contract as a
                    # missing key, so an optional parameter still behaves.
                    path_cache[path] = None
                    return None

        # Cache and return the result
        path_cache[path] = current
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
        - Int: Convert string/number to integer, if it denotes a whole number
        - Real: Convert string/number to float
        - Bool: Convert string ("Yes"/"No", "true"/"false") or bool to boolean
        - String: Keep as string (no conversion needed)

        `Int` and `Real` are handled by `coerce_numeric_reading`, shared with
        `Z3Validator._bind_values`, so the numeric contract is the same whichever
        route a reading arrives by. A reading that cannot be represented in its
        declared type without loss is refused rather than truncated.

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
            if expected_type in NUMERIC_TYPES:
                return coerce_numeric_reading(value, expected_type)

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

    def clear_cache(self):
        """
        No-op, kept on this class's public surface for existing callers.

        There is no cross-call state to clear: ``extract_values`` memoizes path
        readings in a dict it owns for the duration of one call, so a later call
        already reads fresh data and no document's values are retained after one
        returns. Calling this is harmless and changes no reading — correctness
        across documents is a property of the extractor rather than something a
        caller has to remember to do.
        """
