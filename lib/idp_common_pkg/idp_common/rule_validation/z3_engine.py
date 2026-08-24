# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Z3 engine adapter for the IDP rule validation pipeline.

NOTE: This module is used by the demo notebook (notebooks/examples/dual-engine-
rule-validation.ipynb) for standalone Z3 validation. The production pipeline uses
a different path: service.py (fact extraction) + orchestrator.py (LLM value
extraction + Z3Validator directly). This adapter is NOT called by the deployed
Lambda functions.

Bridges the Z3-based ValidationSystem with the IDP Document model,
producing results in the same format as the LLM-based engine so the
orchestrator can consume them interchangeably.

Flow per rule:
  1. Check if a cached RuleJSON exists (memory → S3) → load it
  2. If not, translate the natural-language rule via RuleTranslator → cache it
  3. Extract parameter values:
     a. Structured extraction_results available + rule has path_mappings → path-based
     b. Otherwise → LLM extraction (works with structured or unstructured data)
  4. Validate with Z3 solver
  5. Return response dict matching LLMResponse shape
"""

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

from idp_common import s3
from idp_common.config.models import (
    IDPConfig,
    Z3RuleTranslatorConfig,
    Z3ValueExtractionConfig,
)

if TYPE_CHECKING:
    from .z3.models import RuleJSON, ValidationResult

logger = logging.getLogger(__name__)

_OUTCOME_TO_RECOMMENDATION = {
    "sat": "Pass",
    "unsat": "Fail",
    "error": "Information Not Found",
}


def _build_translator_config(idp_z3_cfg: "Z3RuleTranslatorConfig"):
    """Convert IDP Z3RuleTranslatorConfig → internal TranslatorConfig dataclass."""
    from .z3.config_loader import TranslatorConfig

    return TranslatorConfig(
        model_id=idp_z3_cfg.model,
        temperature=idp_z3_cfg.temperature,
        max_tokens=idp_z3_cfg.max_tokens,
        system_prompt=idp_z3_cfg.system_prompt,
        task_prompt_template=idp_z3_cfg.task_prompt,
        few_shot_examples=idp_z3_cfg.few_shot_examples or [],
    )


def _build_extraction_config(idp_z3_cfg: "Z3ValueExtractionConfig"):
    """Convert IDP Z3ValueExtractionConfig → internal ValueExtractionConfig dataclass."""
    from .z3.config_loader import ValueExtractionConfig

    return ValueExtractionConfig(
        model_id=idp_z3_cfg.model,
        temperature=idp_z3_cfg.temperature,
        max_tokens=idp_z3_cfg.max_tokens,
        system_prompt=idp_z3_cfg.system_prompt,
        task_prompt_template=idp_z3_cfg.task_prompt,
    )


class Z3EngineAdapter:
    """Adapts the Z3 ValidationSystem for use inside RuleValidationService.

    This adapter bridges the Z3-based validation engine with the IDP pipeline,
    using policy_classes and x-aws-idp-policy-type naming conventions consistent
    with the current repository.
    """

    def __init__(
        self,
        config: Union[Dict[str, Any], "IDPConfig", None] = None,
        region: str = None,
    ):
        """
        Initialize Z3 engine adapter.

        Args:
            config: IDPConfig or dict. If provided, reads z3_rule_translator,
                    z3_value_extraction, and z3_timeout_ms from rule_validation section.
                    If None or if both z3_rule_translator and z3_value_extraction are
                    not provided (or only one is provided), falls back to the default
                    YAML-based configuration files from the z3/config/ subfolder.
            region: AWS region for Bedrock client.
        """
        translator_cfg = None
        extraction_cfg = None
        z3_timeout_ms = 5000

        if config is not None:
            # Normalize to IDPConfig if dict is provided
            if isinstance(config, dict):
                config = IDPConfig(**config)

            rv = config.rule_validation

            # Read z3_rule_translator from Pydantic config
            if rv.z3_rule_translator and rv.z3_rule_translator.system_prompt:
                translator_cfg = _build_translator_config(rv.z3_rule_translator)

            # Read z3_value_extraction from Pydantic config
            if rv.z3_value_extraction and rv.z3_value_extraction.system_prompt:
                extraction_cfg = _build_extraction_config(rv.z3_value_extraction)

            # Read z3_timeout_ms from Pydantic config
            z3_timeout_ms = rv.z3_timeout_ms

        from .z3.validation_system import ValidationSystem

        # Fallback logic: Both must be set together, or both fall back to YAML defaults.
        # If only one is provided and the other is None, fall back to defaults for both.
        if translator_cfg and extraction_cfg:
            self.system = ValidationSystem(
                translator_config=translator_cfg,
                extraction_config=extraction_cfg,
                z3_timeout_ms=z3_timeout_ms,
                region=region,
            )
        else:
            # Fall back to default YAML-based configuration files
            self.system = ValidationSystem(
                z3_timeout_ms=z3_timeout_ms,
                region=region,
            )

        self._rule_cache: Dict[str, "RuleJSON"] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate_rule(
        self,
        rule_description: str,
        rule_type: str,
        extraction_results: Dict[str, Any],
        document_text: str,
        output_bucket: Optional[str] = None,
        cache_prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validate a single rule using Z3.

        Handles both structured and unstructured data:
        - If extraction_results has data AND rule has path_mappings → path-based extraction
        - If path-based fails or no path_mappings → LLM extraction using the richer
          of the two data sources (structured results preferred, document text as fallback)

        Args:
            rule_description: Natural language rule text.
            rule_type: The policy type name (policy_classes type).
            extraction_results: Structured extraction results from prior pipeline stages.
            document_text: Raw document text for fallback extraction.
            output_bucket: Optional S3 bucket for caching translated RuleJSON.
            cache_prefix: Optional S3 key prefix for caching.

        Returns:
            Dict matching LLMResponse shape:
            {rule_type, rule, recommendation, reasoning, supporting_pages}
        """
        try:
            data_example = extraction_results if extraction_results else document_text

            rule_json = self._get_or_translate_rule(
                rule_description=rule_description,
                data_example=data_example,
                output_bucket=output_bucket,
                cache_prefix=cache_prefix,
            )

            extracted_values = self._extract_values(
                rule_json=rule_json,
                extraction_results=extraction_results,
                document_text=document_text,
            )

            result = self.system.validate(rule_json, extracted_values)

            return self._to_llm_response(result, rule_type, rule_description)

        except Exception as e:
            # Catch all Z3-related exceptions (TranslationError, ExtractionError,
            # ValidationError, ValidationSystemError) and any unexpected errors.
            logger.error(
                f"Z3 validation failed for rule '{rule_description[:80]}': {e}"
            )
            return {
                "rule_type": rule_type,
                "rule": rule_description,
                "recommendation": "Information Not Found",
                "reasoning": f"Z3 validation error: {e}",
                "supporting_pages": [],
                "_z3_error": True,
            }

    # ------------------------------------------------------------------
    # Extraction — handles structured, unstructured, or both
    # ------------------------------------------------------------------

    def _extract_values(
        self,
        rule_json: "RuleJSON",
        extraction_results: Dict[str, Any],
        document_text: str,
    ) -> Dict[str, Any]:
        """
        Extract parameter values, trying the best strategy for the available data.

        Strategy:
        1. If structured data exists AND rule has path_mappings → try path-based
        2. If path-based fails → LLM extraction with structured data
        3. If structured data is empty → LLM extraction with document text
        4. If LLM extraction with structured data fails → LLM with document text
        """
        has_structured = bool(extraction_results)
        has_paths = rule_json.has_path_mappings()

        if has_structured and has_paths:
            try:
                return self.system.extractor.extract_values(
                    rule_json, extraction_results
                )
            except Exception as e:
                logger.warning(
                    f"Path-based extraction failed for {rule_json.rule_id}: {e}. "
                    f"Falling back to LLM extraction."
                )

        if has_structured:
            try:
                return self.system.translator.extract_values_with_llm(
                    rule_json, extraction_results
                )
            except Exception as e:
                logger.warning(
                    f"LLM extraction from structured data failed for {rule_json.rule_id}: {e}. "
                    f"Falling back to document text."
                )

        return self.system.translator.extract_values_with_llm(rule_json, document_text)

    # ------------------------------------------------------------------
    # Rule translation & caching
    # ------------------------------------------------------------------

    def _get_or_translate_rule(
        self,
        rule_description: str,
        data_example: Any,
        output_bucket: Optional[str] = None,
        cache_prefix: Optional[str] = None,
    ):
        """Load cached RuleJSON or translate from natural language."""

        # Check memory cache first
        if rule_description in self._rule_cache:
            logger.info("Z3 rule loaded from memory cache")
            return self._rule_cache[rule_description]

        # Check S3 cache
        if output_bucket and cache_prefix:
            rule_json = self._load_from_s3(
                output_bucket, cache_prefix, rule_description
            )
            if rule_json:
                self._rule_cache[rule_description] = rule_json
                return rule_json

        # Translate from natural language
        logger.info(f"Translating rule to RuleJSON: '{rule_description[:80]}...'")

        rule_json = self.system.translator.translate_rule(
            natural_language_rule=rule_description,
            data_example=data_example,
        )

        # Cache in memory
        self._rule_cache[rule_description] = rule_json

        # Persist to S3 (non-blocking on failure)
        if output_bucket and cache_prefix:
            self._save_to_s3(output_bucket, cache_prefix, rule_description, rule_json)

        return rule_json

    # ------------------------------------------------------------------
    # S3 cache helpers
    # ------------------------------------------------------------------

    def _load_from_s3(self, bucket, prefix, rule_description):
        """Attempt to load a cached RuleJSON from S3."""
        from .z3.models import RuleJSON

        key = self._s3_key(prefix, rule_description)
        try:
            data = s3.get_json_content(f"s3://{bucket}/{key}")
            if data:
                logger.info(f"Z3 rule loaded from S3 cache: {key}")
                return RuleJSON.from_dict(data)
        except Exception as e:
            logger.debug(f"Failed to load Z3 rule from S3 cache ({key}): {e}")
        return None

    def _save_to_s3(self, bucket, prefix, rule_description, rule_json):
        """Persist a translated RuleJSON to S3 for future reuse."""
        key = self._s3_key(prefix, rule_description)
        try:
            s3.write_content(
                rule_json.to_dict(), bucket, key, content_type="application/json"
            )
            logger.info(f"Z3 rule cached to S3: {key}")
        except Exception as e:
            logger.warning(f"Failed to cache Z3 rule to S3: {e}")

    @staticmethod
    def _s3_key(prefix, rule_description):
        """Generate a deterministic S3 key for a rule description."""
        import hashlib

        rule_hash = hashlib.sha256(rule_description.encode()).hexdigest()[:12]
        return f"{prefix}/z3_rules/{rule_hash}.json"

    @staticmethod
    def _to_llm_response(
        result: "ValidationResult", rule_type: str, rule_description: str
    ) -> Dict[str, Any]:
        """Convert Z3 ValidationResult to the dict shape expected by the orchestrator."""
        recommendation = _OUTCOME_TO_RECOMMENDATION.get(
            result.outcome, "Information Not Found"
        )

        if result.passes():
            reasoning = (
                f"Z3 solver confirmed rule is satisfied. "
                f"Extracted values: {json.dumps(result.extracted_values)}. "
                f"Execution time: {result.execution_time_ms:.1f}ms."
            )
        elif result.fails():
            reasoning = (
                f"Z3 solver determined rule is NOT satisfied. "
                f"Extracted values: {json.dumps(result.extracted_values)}. "
                f"Execution time: {result.execution_time_ms:.1f}ms."
            )
        else:
            reasoning = (
                f"Z3 validation error: {result.error_message}. "
                f"Execution time: {result.execution_time_ms:.1f}ms."
            )

        return {
            "rule_type": rule_type,
            "rule": rule_description,
            "recommendation": recommendation,
            "reasoning": reasoning,
            "supporting_pages": [],
        }
