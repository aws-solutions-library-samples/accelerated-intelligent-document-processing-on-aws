# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Validation System orchestrator for Z3-based business rule validation.

NOTE: This module is used by the demo notebook and Z3EngineAdapter for standalone
validation. The production pipeline uses orchestrator.py's _run_z3_validation
(which calls Z3Validator directly) instead of this system.

This module provides the ValidationSystem class that orchestrates the complete
validation workflow:
1. Rule Translation: Natural language → Rule_JSON (via LLM)
2. Data Extraction: Rule_JSON + Data → Parameter values
3. Z3 Validation: Constraints + Values → Validation result

Features:
- Full orchestration of all components
- Batch validation support
- Timing instrumentation
- Comprehensive error context propagation
- Progress tracking for long-running operations

Requirements: 10.1, 10.2, 10.3, 10.4
"""

import logging
import time
from typing import Any, Dict, List, Optional

from .data_extractor import DataExtractor
from .exceptions import (
    ExtractionError,
    TranslationError,
    ValidationError,
    ValidationSystemError,
)
from .models import RuleJSON, ValidationResult
from .rule_translator import RuleTranslator
from .z3_validator import Z3Validator

# Configure logging
logger = logging.getLogger(__name__)


class ValidationSystem:
    """
    Orchestrates the complete validation workflow.

    The ValidationSystem coordinates three components:
    1. RuleTranslator: Translates natural language rules to Rule_JSON
    2. DataExtractor: Extracts parameter values from JSON data
    3. Z3Validator: Validates constraints using Z3 solver

    Features:
    - Single rule validation
    - Batch validation of multiple rules
    - Timing instrumentation for performance monitoring
    - Comprehensive error handling with context propagation
    - Progress tracking for batch operations

    Example:
        system = ValidationSystem()

        # Translate and validate a rule
        result = system.validate_rule(
            natural_language_rule="Municipal tax must match SAP within 5%",
            data_example=example_data,
            actual_data=actual_data
        )

        # Batch validation
        results = system.validate_batch(
            rules=[rule1, rule2, rule3],
            data=actual_data
        )
    """

    def __init__(
        self,
        translator_config_path: Optional[str] = None,
        z3_timeout_ms: int = 5000,
        translator_config=None,
        extraction_config=None,
        region: str = None,
    ):
        """
        Initialize ValidationSystem with component configurations.

        Args:
            translator_config_path: Path to translator configuration YAML file.
                                   If None, uses default config.
            z3_timeout_ms: Z3 solver timeout in milliseconds (default: 5000ms)
            translator_config: Pre-built TranslatorConfig (overrides YAML file)
            extraction_config: Pre-built ValueExtractionConfig (overrides YAML file)
            region: AWS region for Bedrock client
        """
        logger.info("Initializing ValidationSystem")

        try:
            # Initialize components
            self.translator = RuleTranslator(
                config_path=translator_config_path,
                translator_config=translator_config,
                extraction_config=extraction_config,
                region=region,
            )
            self.extractor = DataExtractor()
            self.validator = Z3Validator(timeout_ms=z3_timeout_ms)

            logger.info("ValidationSystem initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize ValidationSystem: {e}")
            raise

    def extract(
        self, rule_json: RuleJSON, data: Any, use_llm: bool = False
    ) -> Dict[str, Any]:
        """
        Extract parameter values from data (smart extraction with automatic fallback).

        This method intelligently chooses the extraction strategy:
        1. If use_llm=True, uses LLM extraction directly
        2. If rule has path_mappings, tries path-based extraction first
        3. If path-based fails or no path_mappings, falls back to LLM extraction

        Args:
            rule_json: Rule with parameters (and optionally path_mappings)
            data: Data in any format
            use_llm: If True, skip path-based extraction and use LLM directly

        Returns:
            Dictionary of extracted parameter values

        Raises:
            ExtractionError: If both path-based and LLM extraction fail
            TranslationError: If LLM extraction fails

        Example:
            # Smart extraction (tries path-based first if available)
            extracted = system.extract(rule_json, data)

            # Force LLM extraction
            extracted = system.extract(rule_json, data, use_llm=True)

            # Then validate
            result = system.validate(rule_json, extracted)
        """
        logger.info(f"Extracting values for rule: {rule_json.rule_id}")

        # If user explicitly wants LLM, use it directly
        if use_llm:
            logger.info("use_llm=True: Using LLM-based extraction")
            return self._extract_with_llm(rule_json, data)

        # Check if rule has path_mappings
        if not rule_json.has_path_mappings():
            logger.info(
                f"Rule {rule_json.rule_id} has no path_mappings. "
                f"Using LLM-based extraction."
            )
            return self._extract_with_llm(rule_json, data)

        # Try path-based extraction first
        logger.info(
            f"Rule {rule_json.rule_id} has path_mappings. "
            f"Attempting path-based extraction."
        )

        try:
            return self._extract_with_paths(rule_json, data)

        except ExtractionError as e:
            # Path-based extraction failed, try LLM fallback
            logger.warning(
                f"Path-based extraction failed for rule {rule_json.rule_id}: {e}. "
                f"Falling back to LLM-based extraction."
            )

            try:
                return self._extract_with_llm(rule_json, data)
            except (TranslationError, Exception) as llm_error:
                # LLM also failed - raise the original extraction error
                logger.error(
                    f"LLM fallback also failed for rule {rule_json.rule_id}: {llm_error}. "
                    f"Raising original extraction error."
                )
                raise e

        except Exception as e:
            # Unexpected error in path-based extraction, try LLM fallback
            logger.warning(
                f"Unexpected error in path-based extraction for rule {rule_json.rule_id}: {e}. "
                f"Falling back to LLM-based extraction."
            )

            try:
                return self._extract_with_llm(rule_json, data)
            except (TranslationError, Exception) as llm_error:
                # LLM also failed - raise the original error
                logger.error(
                    f"LLM fallback also failed for rule {rule_json.rule_id}: {llm_error}. "
                    f"Raising original error."
                )
                raise e

    def _extract_with_paths(self, rule_json: RuleJSON, data: dict) -> Dict[str, Any]:
        """
        Internal: Extract parameter values using path mappings.

        Fast, deterministic extraction for structured JSON data.
        Use the public extract() method instead.

        Args:
            rule_json: Rule with path_mappings
            data: Structured JSON data

        Returns:
            Dictionary of extracted parameter values

        Raises:
            ExtractionError: If extraction fails
        """
        extraction_start = time.time()

        extracted_values = self.extractor.extract_values(rule_json=rule_json, data=data)

        extraction_time = (time.time() - extraction_start) * 1000
        logger.info(
            f"Path-based extraction completed in {extraction_time:.2f}ms, "
            f"extracted {len(extracted_values)} parameters"
        )

        return extracted_values

    def _extract_with_llm(self, rule_json: RuleJSON, data: Any) -> Dict[str, Any]:
        """
        Internal: Extract parameter values using LLM.

        Flexible extraction that works with any data format.
        Use the public extract() method instead.

        Args:
            rule_json: Rule with parameters (path_mappings ignored)
            data: Data in any format (dict, string, list, etc.)

        Returns:
            Dictionary of extracted parameter values

        Raises:
            TranslationError: If LLM extraction fails
        """
        extraction_start = time.time()

        extracted_values = self.translator.extract_values_with_llm(
            rule_json=rule_json, data=data
        )

        extraction_time = (time.time() - extraction_start) * 1000
        logger.info(
            f"LLM extraction completed in {extraction_time:.2f}ms, "
            f"extracted {len(extracted_values)} parameters"
        )

        return extracted_values

    # Legacy methods for backward compatibility
    def extract_with_paths(self, rule_json: RuleJSON, data: dict) -> Dict[str, Any]:
        """
        DEPRECATED: Use extract() instead.

        Extract parameter values using path mappings (Workflow A).
        This method is kept for backward compatibility.

        Args:
            rule_json: Rule with path_mappings
            data: Structured JSON data

        Returns:
            Dictionary of extracted parameter values
        """
        logger.warning("extract_with_paths() is deprecated. Use extract() instead.")
        return self.extract(rule_json, data, use_llm=False)

    def extract_with_llm(self, rule_json: RuleJSON, data: Any) -> Dict[str, Any]:
        """
        DEPRECATED: Use extract(rule_json, data, use_llm=True) instead.

        Extract parameter values using LLM (Workflow B).
        This method is kept for backward compatibility.

        Args:
            rule_json: Rule with parameters (path_mappings ignored)
            data: Data in any format (dict, string, list, etc.)

        Returns:
            Dictionary of extracted parameter values
        """
        logger.warning(
            "extract_with_llm() is deprecated. Use extract(rule_json, data, use_llm=True) instead."
        )
        return self.extract(rule_json, data, use_llm=True)

    def validate(
        self, rule_json: RuleJSON, extracted_values: Dict[str, Any]
    ) -> ValidationResult:
        """
        Validate extracted values against rule constraints.

        This is the single public solver method used by both workflows.
        It takes extracted values (from any source) and validates them.

        The validation result indicates pass/fail:
        - satisfied=True, outcome="sat" → PASS (data meets all rules)
        - satisfied=False, outcome="unsat" → FAIL (data violates rules)
        - satisfied=False, outcome="error" → ERROR (validation failed)

        Args:
            rule_json: Rule with parameters and constraints
            extracted_values: Parameter values (from extract_with_paths or extract_with_llm)

        Returns:
            ValidationResult with outcome and timing

        Raises:
            ValidationError: If validation fails

        Example:
            # Workflow A
            extracted = system.extract_with_paths(rule_json, data)
            result = system.validate(rule_json, extracted)

            # Workflow B
            extracted = system.extract_with_llm(rule_json, data)
            result = system.validate(rule_json, extracted)

            # Check pass/fail
            if result.passes():
                print("✓ All rules satisfied")
            else:
                print("✗ Rules violated")
        """
        start_time = time.time()

        logger.info(f"Validating rule: {rule_json.rule_id}")

        try:
            result = self._solve(rule_json, extracted_values)

            # Update total execution time
            total_time = (time.time() - start_time) * 1000
            result.execution_time_ms = total_time

            logger.info(
                f"Validation completed in {total_time:.2f}ms, "
                f"outcome: {result.outcome}, satisfied: {result.satisfied}"
            )

            return result

        except ValidationError as e:
            logger.error(f"Validation failed: {e}")
            raise

        except Exception as e:
            total_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error during validation: {e}")

            raise ValidationSystemError(
                message=f"Unexpected error during validation: {str(e)}",
                component="ValidationSystem",
                operation="validate",
                rule_id=rule_json.rule_id,
                context={
                    "execution_time_ms": total_time,
                    "error_type": type(e).__name__,
                },
            )

    def _solve(
        self, rule_json: RuleJSON, extracted_values: Dict[str, Any]
    ) -> ValidationResult:
        """
        Internal method: Solve constraints using Z3 validator.

        This is the single solver method used by all validation workflows.
        It takes extracted values (from any source) and validates them
        against the rule's constraints.

        Args:
            rule_json: Rule with parameters and constraints
            extracted_values: Parameter values extracted from data

        Returns:
            ValidationResult with outcome and timing

        Raises:
            ValidationError: If Z3 validation fails
        """
        logger.info("Solving constraints with Z3")
        validation_start = time.time()

        result = self.validator.validate(rule_json, extracted_values)

        validation_time = (time.time() - validation_start) * 1000
        logger.info(
            f"Solver completed in {validation_time:.2f}ms, "
            f"outcome: {result.outcome}, satisfied: {result.satisfied}"
        )

        return result

    def validate_rule(
        self,
        natural_language_rule: str,
        data_example: dict,
        actual_data: dict,
        rule_id: Optional[str] = None,
        version: str = "1.0",
        description: Optional[str] = None,
    ) -> ValidationResult:
        """
        Validate a natural language rule against actual data.

        Complete workflow:
        1. Translate natural language rule to Rule_JSON using LLM
        2. Extract parameter values from actual_data using path mappings
        3. Validate constraints using Z3 solver

        Args:
            natural_language_rule: Business rule in natural language
            data_example: Example data for LLM to understand structure
            actual_data: Actual data to validate against
            rule_id: Optional rule identifier (auto-generated if not provided)
            version: Rule version string (default: "1.0")
            description: Optional rule description

        Returns:
            ValidationResult with outcome, extracted values, and timing

        Raises:
            TranslationError: If rule translation fails
            ExtractionError: If data extraction fails
            ValidationError: If Z3 validation fails

        Example:
            result = system.validate_rule(
                natural_language_rule="Tax must match within 5%",
                data_example=example_data,
                actual_data=actual_data
            )
            print(f"Rule satisfied: {result.satisfied}")
        """
        start_time = time.time()

        logger.info(f"Starting validation for rule: {natural_language_rule[:100]}...")

        try:
            # Step 1: Translate rule (Requirement 10.2)
            logger.info("Step 1: Translating natural language rule to Rule_JSON")
            translation_start = time.time()

            rule_json = self.translator.translate_rule(
                natural_language_rule=natural_language_rule,
                data_example=data_example,
                rule_id=rule_id,
                version=version,
                description=description,
            )

            translation_time = (time.time() - translation_start) * 1000
            logger.info(
                f"Translation completed in {translation_time:.2f}ms, rule_id: {rule_json.rule_id}"
            )

            # Step 2: Extract data (Requirement 10.2)
            logger.info("Step 2: Extracting parameter values from data")
            extraction_start = time.time()

            extracted_values = self.extractor.extract_values(
                rule_json=rule_json, data=actual_data
            )

            extraction_time = (time.time() - extraction_start) * 1000
            logger.info(
                f"Extraction completed in {extraction_time:.2f}ms, "
                f"extracted {len(extracted_values)} parameters"
            )

            # Step 3: Validate with Z3 (Requirement 10.2)
            logger.info("Step 3: Validating constraints with Z3")

            result = self._solve(rule_json, extracted_values)

            validation_time = result.execution_time_ms  # Time from solver
            logger.info(
                f"Validation completed in {validation_time:.2f}ms, "
                f"outcome: {result.outcome}, satisfied: {result.satisfied}"
            )

            # Update total execution time
            total_time = (time.time() - start_time) * 1000
            result.execution_time_ms = total_time

            logger.info(
                f"Complete validation finished in {total_time:.2f}ms "
                f"(translation: {translation_time:.2f}ms, "
                f"extraction: {extraction_time:.2f}ms, "
                f"validation: {validation_time:.2f}ms)"
            )

            return result

        except (TranslationError, ExtractionError, ValidationError) as e:
            # Log and re-raise known errors
            logger.error(f"Validation failed: {e}")
            raise

        except Exception as e:
            # Wrap unexpected errors
            total_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error during validation: {e}")

            raise ValidationSystemError(
                message=f"Unexpected error during validation: {str(e)}",
                component="ValidationSystem",
                operation="validate_rule",
                rule_id=rule_id,
                context={
                    "natural_language_rule": natural_language_rule[:200],
                    "execution_time_ms": total_time,
                    "error_type": type(e).__name__,
                },
            )

    def validate_with_rule_json(
        self, rule_json: RuleJSON, actual_data: dict
    ) -> ValidationResult:
        """
        Validate using pre-translated Rule_JSON (skip translation step).

        Useful when:
        - Rule_JSON is already available (cached or pre-generated)
        - Avoiding LLM calls for performance or cost reasons
        - Testing with known Rule_JSON

        Workflow:
        1. Extract parameter values from actual_data
        2. Validate constraints using Z3 solver

        Args:
            rule_json: Pre-translated Rule_JSON specification
            actual_data: Actual data to validate against

        Returns:
            ValidationResult with outcome, extracted values, and timing

        Raises:
            ExtractionError: If data extraction fails
            ValidationError: If Z3 validation fails

        Example:
            # Load pre-translated rule
            rule_json = RuleJSON.from_dict(json.load(open("rule.json")))

            # Validate without LLM call
            result = system.validate_with_rule_json(rule_json, actual_data)
        """
        start_time = time.time()

        logger.info(f"Starting validation with Rule_JSON: {rule_json.rule_id}")

        try:
            # Step 1: Extract data
            logger.info("Step 1: Extracting parameter values from data")

            extracted_values = self.extract(rule_json, actual_data)

            # Step 2: Validate with Z3
            logger.info("Step 2: Validating constraints with Z3")

            result = self.validate(rule_json, extracted_values)

            # Update total execution time
            total_time = (time.time() - start_time) * 1000
            result.execution_time_ms = total_time

            logger.info(f"Complete validation finished in {total_time:.2f}ms")

            return result

        except (ExtractionError, ValidationError) as e:
            # Log and re-raise known errors
            logger.error(f"Validation failed: {e}")
            raise

        except Exception as e:
            # Wrap unexpected errors
            total_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error during validation: {e}")

            raise ValidationSystemError(
                message=f"Unexpected error during validation: {str(e)}",
                component="ValidationSystem",
                operation="validate_with_rule_json",
                rule_id=rule_json.rule_id,
                context={
                    "execution_time_ms": total_time,
                    "error_type": type(e).__name__,
                },
            )

    def validate_batch(
        self, rules: List[RuleJSON], data: dict, stop_on_error: bool = False
    ) -> List[ValidationResult]:
        """
        Validate multiple rules against the same data.

        Batch validation features:
        - Validates all rules against the same data document
        - Clears extraction cache between rules for correctness
        - Provides progress logging
        - Optionally stops on first error or continues through all rules
        - Returns results for all rules (including errors)

        Args:
            rules: List of Rule_JSON specifications to validate
            data: Data document to validate against
            stop_on_error: If True, stop on first error; if False, continue (default: False)

        Returns:
            List of ValidationResult objects, one per rule

        Raises:
            ValidationSystemError: If stop_on_error=True and a rule fails

        Example:
            rules = [rule1, rule2, rule3]
            results = system.validate_batch(rules, data)

            for result in results:
                print(f"Rule {result.rule_id}: {result.satisfied}")
        """
        start_time = time.time()

        logger.info(f"Starting batch validation of {len(rules)} rules")

        results = []

        for i, rule_json in enumerate(rules, 1):
            logger.info(f"Validating rule {i}/{len(rules)}: {rule_json.rule_id}")

            try:
                # Clear cache before each rule to ensure fresh extraction
                self.extractor.clear_cache()

                # Validate the rule
                result = self.validate_with_rule_json(
                    rule_json=rule_json, actual_data=data
                )

                results.append(result)

                logger.info(
                    f"Rule {i}/{len(rules)} completed: "
                    f"outcome={result.outcome}, satisfied={result.satisfied}"
                )

            except (ExtractionError, ValidationError) as e:
                # Create error result
                error_result = ValidationResult(
                    rule_id=rule_json.rule_id,
                    outcome="error",
                    satisfied=False,
                    extracted_values={},
                    model=None,
                    error_message=str(e),
                    execution_time_ms=0.0,
                )

                results.append(error_result)

                logger.error(f"Rule {i}/{len(rules)} failed: {e}")

                if stop_on_error:
                    logger.error(
                        "Stopping batch validation due to error (stop_on_error=True)"
                    )
                    raise ValidationSystemError(
                        message=f"Batch validation stopped at rule {i}/{len(rules)}: {str(e)}",
                        component="ValidationSystem",
                        operation="validate_batch",
                        rule_id=rule_json.rule_id,
                        context={
                            "rule_index": i,
                            "total_rules": len(rules),
                            "completed_rules": i - 1,
                        },
                    )

            except Exception as e:
                # Unexpected error
                error_result = ValidationResult(
                    rule_id=rule_json.rule_id,
                    outcome="error",
                    satisfied=False,
                    extracted_values={},
                    model=None,
                    error_message=f"Unexpected error: {str(e)}",
                    execution_time_ms=0.0,
                )

                results.append(error_result)

                logger.error(f"Rule {i}/{len(rules)} encountered unexpected error: {e}")

                if stop_on_error:
                    logger.error(
                        "Stopping batch validation due to error (stop_on_error=True)"
                    )
                    raise ValidationSystemError(
                        message=f"Batch validation stopped at rule {i}/{len(rules)}: {str(e)}",
                        component="ValidationSystem",
                        operation="validate_batch",
                        rule_id=rule_json.rule_id,
                        context={
                            "rule_index": i,
                            "total_rules": len(rules),
                            "completed_rules": i - 1,
                            "error_type": type(e).__name__,
                        },
                    )

        total_time = (time.time() - start_time) * 1000

        # Calculate summary statistics
        satisfied_count = sum(1 for r in results if r.satisfied)
        error_count = sum(1 for r in results if r.outcome == "error")

        logger.info(
            f"Batch validation completed in {total_time:.2f}ms: "
            f"{len(rules)} rules, {satisfied_count} satisfied, {error_count} errors"
        )

        return results

    def get_summary(self, results: List[ValidationResult]) -> Dict[str, Any]:
        """
        Generate summary statistics for batch validation results.

        Args:
            results: List of ValidationResult objects

        Returns:
            Dictionary with summary statistics:
            - total_rules: Total number of rules
            - satisfied_count: Number of satisfied rules
            - unsatisfied_count: Number of unsatisfied rules
            - error_count: Number of errors
            - total_time_ms: Total execution time
            - avg_time_ms: Average time per rule

        Example:
            results = system.validate_batch(rules, data)
            summary = system.get_summary(results)
            print(f"Satisfied: {summary['satisfied_count']}/{summary['total_rules']}")
        """
        total_rules = len(results)
        satisfied_count = sum(1 for r in results if r.satisfied)
        unsatisfied_count = sum(
            1 for r in results if not r.satisfied and r.outcome != "error"
        )
        error_count = sum(1 for r in results if r.outcome == "error")

        total_time_ms = sum(r.execution_time_ms for r in results)
        avg_time_ms = total_time_ms / total_rules if total_rules > 0 else 0.0

        return {
            "total_rules": total_rules,
            "satisfied_count": satisfied_count,
            "unsatisfied_count": unsatisfied_count,
            "error_count": error_count,
            "total_time_ms": total_time_ms,
            "avg_time_ms": avg_time_ms,
            "satisfaction_rate": satisfied_count / total_rules
            if total_rules > 0
            else 0.0,
        }

    def validate_with_llm_extraction(
        self, rule_json: RuleJSON, data: Any
    ) -> ValidationResult:
        """
        Validate using LLM to extract values from any data format.

        This mode is useful when:
        - Data schema has changed and you don't want to update path_mappings
        - Data is in unstructured format (text, mixed formats)
        - You want flexible extraction without rigid path specifications

        Workflow:
        1. LLM extracts parameter values from data (any format)
        2. Validate constraints using Z3 solver

        Args:
            rule_json: Rule with parameters and constraints (path_mappings ignored)
            data: Data in any format (dict, string, list, etc.)

        Returns:
            ValidationResult with outcome, extracted values, and timing

        Raises:
            TranslationError: If LLM extraction fails
            ValidationError: If Z3 validation fails

        Example:
            # Extract from unstructured text
            result = system.validate_with_llm_extraction(
                rule_json=rule,
                data="Municipal tax is $5,100 and SAP shows $5,200"
            )

            # Extract from different JSON schema
            result = system.validate_with_llm_extraction(
                rule_json=rule,
                data={"new_schema": {"tax_info": {...}}}
            )
        """
        start_time = time.time()

        logger.info(f"Starting LLM extraction validation for rule: {rule_json.rule_id}")

        try:
            # Step 1: Extract values using LLM
            logger.info("Step 1: Extracting parameter values with LLM")

            extracted_values = self.extract(rule_json, data, use_llm=True)

            # Step 2: Validate with Z3
            logger.info("Step 2: Validating constraints with Z3")

            result = self.validate(rule_json, extracted_values)

            # Update total execution time
            total_time = (time.time() - start_time) * 1000
            result.execution_time_ms = total_time

            logger.info(
                f"Complete LLM extraction validation finished in {total_time:.2f}ms"
            )

            return result

        except (TranslationError, ValidationError) as e:
            # Log and re-raise known errors
            logger.error(f"LLM extraction validation failed: {e}")
            raise

        except Exception as e:
            # Wrap unexpected errors
            total_time = (time.time() - start_time) * 1000
            logger.error(f"Unexpected error during LLM extraction validation: {e}")

            raise ValidationSystemError(
                message=f"Unexpected error during LLM extraction validation: {str(e)}",
                component="ValidationSystem",
                operation="validate_with_llm_extraction",
                rule_id=rule_json.rule_id,
                context={
                    "execution_time_ms": total_time,
                    "error_type": type(e).__name__,
                },
            )
