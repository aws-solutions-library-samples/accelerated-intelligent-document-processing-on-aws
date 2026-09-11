# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Stickler-based Evaluation Service for document extraction results.

This module provides a service for evaluating extraction results using
the Stickler library for structured object comparison.
"""

import concurrent.futures
import logging
import os
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Type, Union

if TYPE_CHECKING:
    from idp_common.evaluation.stickler_backend import StructuredModel

from idp_common import s3
from idp_common.config.models import IDPConfig
from idp_common.evaluation.contract import (
    STICKLER_RESULT_VERSION,
    evaluation_results_key,
)
from idp_common.evaluation.models import (
    AttributeEvaluationResult,
    DocSplitMetrics,
    DocumentEvaluationResult,
    SectionEvaluationResult,
)
from idp_common.evaluation.stickler_backend import (
    DocSplitClassificationMetrics,
    SticklerConfigMapper,
    attach_page_confidence,
    compute_graded_packet_metrics,
    get_stickler_model,
    load_sections_for_doc_split,
    register_idp_comparators,
    transform_stickler_result,
)
from idp_common.models import Document, Section, Status

logger = logging.getLogger(__name__)


def _compute_derived_metrics(
    tp: int,
    fp: int,
    fn: int,
    tn: int = 0,
    fa: int = 0,
    fd: int = 0,
) -> Dict[str, float]:
    """Compute the same precision/recall/F1/accuracy/FAR/FDR that Stickler's
    ``DerivedMetricsCalculator`` computes per field, but over aggregated counts.

    ``fa`` (false alarm — predicted when should be absent) and ``fd`` (false
    discovery — predicted wrong value) are Stickler's finer split of ``fp``;
    FAR = fa / (fa + tn), FDR = fd / (fd + tp). Both denominators guard
    against divide-by-zero.
    """
    denom_p = tp + fp
    denom_r = tp + fn
    denom_a = tp + fp + fn + tn
    precision = tp / denom_p if denom_p > 0 else 0.0
    recall = tp / denom_r if denom_r > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    accuracy = (tp + tn) / denom_a if denom_a > 0 else 0.0
    denom_far = fa + tn
    denom_fdr = fd + tp
    far = fa / denom_far if denom_far > 0 else 0.0
    fdr = fd / denom_fdr if denom_fdr > 0 else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1_score": f1,
        "accuracy": accuracy,
        "false_alarm_rate": far,
        "false_discovery_rate": fdr,
    }


# _compute_graded_packet_metrics moved to
# idp_common.evaluation.stickler_backend.doc_split.compute_graded_packet_metrics
# (§6 reorg, R14 landing site).


def _normalize_comparator_name(comparator: str) -> str:
    """
    Map Stickler comparator names to UI picklist values (PascalCase).

    Args:
        comparator: Internal Stickler comparator name

    Returns:
        Normalized UI-friendly method name
    """
    mapping = {
        "FuzzyComparator": "Fuzzy",
        "ExactComparator": "Exact",
        "NumericComparator": "NumericExact",
        "LevenshteinComparator": "Levenshtein",
        "SemanticComparator": "Semantic",
        "DateComparator": "Date",
        "LLMComparator": "LLM",
    }
    return mapping.get(comparator, comparator)


# Comparison methods whose display string includes a similarity threshold.
# NumericExact uses tolerance (not threshold) and LLM returns a binary match,
# so neither shows a threshold suffix.
#
# The actual applied threshold comes from Stickler's model at compare time
# (read in stickler_backend/results.py from
# ``model_fields[...].json_schema_extra._threshold`` and threaded here as
# ``field_specific_threshold``). When the caller can't produce that value
# (auto-generated section that failed to build a model, non-Stickler path
# reusing this helper), we render just the method name without a threshold
# suffix rather than guessing at a hardcoded default that could disagree
# with whatever Stickler actually scored against.
_METHODS_WITH_THRESHOLD_DISPLAY = frozenset({"Fuzzy", "Semantic", "Levenshtein"})


def _format_evaluation_method(
    comparator_method: Optional[str],
    expected_value: Any,
    actual_value: Any,
    field_specific_threshold: Optional[float],
    match_threshold: float,
    list_match_threshold: Optional[float] = None,
) -> str:
    """
    Build the human-readable evaluation-method string shown in reports.

    This is the single source of truth for the "Method" column in both the
    top-level attributes table and the Nested Field Comparison table, so the two
    stay consistent (e.g. "Fuzzy (threshold: 0.70)", "Hungarian (threshold: 0.80)",
    "NumericExact", "AggregateObject").

    Args:
        comparator_method: Explicit Stickler comparator name for the field, if any.
        expected_value: Expected value (used for type inference when no comparator).
        actual_value: Actual value (used for type inference when no comparator).
        field_specific_threshold: Field-level similarity threshold, if configured.
        match_threshold: Document-level Hungarian match threshold fallback.
        list_match_threshold: Field-level Hungarian match threshold, if configured.

    Returns:
        Formatted method string for display.
    """
    if comparator_method:
        # Normalize comparator name to UI-friendly format
        method = _normalize_comparator_name(comparator_method)

        # Show threshold ONLY for methods that use similarity thresholds AND
        # when the caller supplied the applied value. Missing threshold →
        # render just the method name (no fake-default suffix).
        if (
            method in _METHODS_WITH_THRESHOLD_DISPLAY
            and field_specific_threshold is not None
        ):
            method = f"{method} (threshold: {field_specific_threshold:.2f})"
        # Exact, NumericExact, LLM, Date don't show thresholds
        return method

    if isinstance(expected_value, list) or isinstance(actual_value, list):
        # Arrays use Hungarian matching - show field-specific or document-level threshold
        display_threshold = list_match_threshold or match_threshold
        return f"Hungarian (threshold: {display_threshold:.2f})"

    if isinstance(expected_value, dict) or isinstance(actual_value, dict):
        # Nested objects - no threshold
        return "AggregateObject"

    # Infer method based on data types when no explicit comparator
    if isinstance(expected_value, bool) or isinstance(actual_value, bool):
        # Booleans use exact matching - no threshold
        return "Exact"
    if isinstance(expected_value, (int, float)) or isinstance(
        actual_value, (int, float)
    ):
        # Numbers use tolerance-based comparison - no threshold display
        return "NumericExact"
    if isinstance(expected_value, str) or isinstance(actual_value, str):
        # Strings default to fuzzy matching. Only include the threshold suffix
        # when the caller could supply one (same reasoning as above).
        if field_specific_threshold is not None:
            return f"Fuzzy (threshold: {field_specific_threshold:.2f})"
        return "Fuzzy"

    # Safe default for any other types
    return "Exact"


def _convert_numpy_types(obj: Any) -> Any:
    """
    Recursively convert numpy types and Pydantic models to Python native types for JSON serialization.

    Args:
        obj: Object that may contain numpy types or Pydantic models

    Returns:
        Object with numpy types and Pydantic models converted to Python native types
    """
    import numpy as np

    if isinstance(obj, np.bool_):
        return bool(obj)
    elif isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, dict):
        return {key: _convert_numpy_types(value) for key, value in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_convert_numpy_types(item) for item in obj]
    elif hasattr(obj, "model_dump"):
        # Handle Pydantic v2 models (including DynamicModel from Stickler)
        return _convert_numpy_types(obj.model_dump())
    elif hasattr(obj, "dict"):
        # Handle Pydantic v1 models
        return _convert_numpy_types(obj.dict())
    else:
        return obj


class EvaluationService:
    """
    Stickler-based evaluation service for document extraction results.

    This service maintains the same API as the legacy implementation but uses
    Stickler internally for comparison logic, providing enhanced features like
    field weighting and optimized list matching.
    """

    def __init__(
        self,
        region: Optional[str] = None,
        config: Optional[Union[Dict[str, Any], IDPConfig]] = None,
        max_workers: int = 10,
    ):
        """
        Initialize the evaluation service.

        Args:
            region: AWS region
            config: Configuration dictionary or IDPConfig model containing evaluation settings
            max_workers: Maximum number of concurrent workers for section evaluation
        """
        # Convert dict to IDPConfig if needed
        if config is not None and isinstance(config, dict):
            config_model: IDPConfig = IDPConfig(**config)
        elif config is None:
            config_model = IDPConfig()
        else:
            config_model = config

        self.config = config_model
        self.region = region or os.environ.get("AWS_REGION")
        self.max_workers = max_workers

        # Import and check Stickler availability. StructuredModel is re-exported
        # via stickler_backend to preserve the single-boundary rule.
        try:
            from idp_common.evaluation.stickler_backend import StructuredModel

            self._StructuredModel = StructuredModel

            # Register IDP's LLM comparator with Stickler before any model is
            # built (the mapper's "IDPLLMComparator" reference resolves at
            # model-construction time). Registration is idempotent — safe to
            # call from every EvaluationService init.
            try:
                register_idp_comparators()

                # Build config dict for extraction
                if hasattr(config_model, "model_dump"):
                    config_dict = config_model.model_dump()
                elif hasattr(config_model, "dict"):
                    config_dict = config_model.dict()
                else:
                    config_dict = (
                        dict(config_model)
                        if not isinstance(config_model, dict)
                        else config_model
                    )
            except ImportError as e:
                logger.warning(f"LLMComparator setup failed: {e}")
                config_dict = None

        except ImportError:
            raise ImportError(
                "Stickler library is required for evaluation. "
                "Install with: pip install -e '.[evaluation]'"
            )

        # Build Stickler configurations using mapper
        # Reuse config_dict if already built, otherwise build it now
        if config_dict is None:
            if hasattr(config_model, "model_dump"):
                config_dict = config_model.model_dump()
            elif hasattr(config_model, "dict"):
                config_dict = config_model.dict()
            else:
                config_dict = (
                    dict(config_model)
                    if not isinstance(config_model, dict)
                    else config_model
                )

        self.stickler_models = SticklerConfigMapper.build_all_stickler_configs(
            config_dict
        )

        # Cache for Stickler model classes
        self._model_cache: Dict[str, Type["StructuredModel"]] = {}

        # Track which models were auto-generated (for annotation in results)
        self._auto_generated_models: set = set()

        logger.info(
            f"Initialized Stickler-based evaluation service with "
            f"{len(self.stickler_models)} document classes, max_workers={max_workers}"
        )

    def _infer_schema_from_data(
        self, data: Dict[str, Any], document_class: str
    ) -> Dict[str, Any]:
        """
        Infer JSON Schema from data structure using genson library.

        Uses the production-ready genson library for robust schema generation,
        then adds IDP-specific evaluation extensions.

        Args:
            data: Dictionary containing the expected extraction results
            document_class: Name of the document class

        Returns:
            Generated JSON Schema with IDP evaluation extensions
        """
        from genson import SchemaBuilder

        from idp_common.config.schema_constants import (
            X_AWS_IDP_DOCUMENT_TYPE,
            X_AWS_IDP_EVALUATION_MATCH_THRESHOLD,
        )

        # Use genson to generate base schema
        builder = SchemaBuilder()
        builder.add_object(data)
        schema = builder.to_schema()

        # Add IDP-specific metadata
        schema["$id"] = f"autogenerated_{document_class.lower().replace(' ', '_')}"
        schema[X_AWS_IDP_DOCUMENT_TYPE] = document_class
        schema[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD] = 0.8

        # Normalize integer types to number to handle decimal values in extraction
        # This prevents type mismatch errors when baseline has int but prediction has float
        self._normalize_integer_to_number(schema)

        # Normalize null types to string - genson produces "type": "null" for fields
        # with null values in baseline data, which Stickler doesn't support
        self._normalize_null_types(schema)

        # Strip 'required' arrays - genson marks every observed key as required, but
        # for evaluation a field that wasn't extracted (None, then removed by
        # _remove_none_values) is a scored miss, not a hard validation failure. Making
        # all fields optional matches the semantics of explicit IDP configs (which omit
        # 'required') and avoids "Field required [type=missing]" errors.
        self._strip_required(schema)

        # Add evaluation method extensions recursively
        self._add_evaluation_extensions_recursive(schema)

        # Count properties for logging
        num_properties = len(schema.get("properties", {}))

        logger.warning(
            f"Auto-generated schema for document class '{document_class}' using genson library. "
            f"For production use, please define an explicit configuration. "
            f"Generated {num_properties} properties."
        )

        return schema

    def _strip_required(self, schema: Dict[str, Any]) -> None:
        """
        Recursively remove all 'required' arrays from an auto-generated schema.

        genson marks every key it observes as required. For evaluation, a field
        present in the baseline but absent from a prediction (or vice versa) should
        score as a miss, not raise a hard "Field required" validation error. Explicit
        IDP configs omit 'required' entirely (all fields optional); this aligns the
        auto-generated schema with that convention.

        Args:
            schema: Schema object to modify in-place
        """
        if not isinstance(schema, dict):
            return

        schema.pop("required", None)

        if "properties" in schema:
            for prop_schema in schema["properties"].values():
                self._strip_required(prop_schema)

        if "items" in schema:
            items = schema["items"]
            if isinstance(items, dict):
                self._strip_required(items)

        if "$defs" in schema:
            for def_schema in schema["$defs"].values():
                self._strip_required(def_schema)

    def _normalize_null_types(self, schema: Dict[str, Any]) -> None:
        """
        Recursively convert 'null' types to 'string' in auto-generated schemas.

        The genson library produces "type": "null" for fields where the baseline
        data has a null/None value. Stickler's JsonSchemaFieldConverter only supports
        ['string', 'number', 'integer', 'boolean'] and crashes on 'null'.

        By converting to 'string', we allow the field to pass through and be
        compared as an empty/missing value during evaluation.

        For union types like ["string", "null"] or ["number", "null"], we keep
        only the non-null type.

        Args:
            schema: Schema object to modify in-place
        """
        schema_type = schema.get("type")

        if schema_type == "null":
            schema["type"] = "string"
        elif isinstance(schema_type, list):
            # Handle union types from genson (e.g., ["string", "null"], ["number", "null"])
            non_null = [t for t in schema_type if t != "null"]
            if non_null:
                # Use the first non-null type (e.g., "string" or "number")
                schema["type"] = non_null[0] if len(non_null) == 1 else non_null
            else:
                # All types are null (shouldn't happen, but be safe)
                schema["type"] = "string"

        # Recursively process nested structures
        if "properties" in schema:
            for prop_schema in schema["properties"].values():
                self._normalize_null_types(prop_schema)

        if "items" in schema:
            items = schema["items"]
            if isinstance(items, dict):
                self._normalize_null_types(items)

    def _normalize_integer_to_number(self, schema: Dict[str, Any]) -> None:
        """
        Recursively convert 'integer' types to 'number' in auto-generated schemas.

        This prevents Pydantic validation errors when the baseline data has an
        integer value but the prediction has a float (e.g., baseline edited to 9999
        but prediction is 2111.2). By using 'number' type instead of 'integer',
        we allow both int and float values to pass validation.

        Args:
            schema: Schema object to modify in-place
        """
        schema_type = schema.get("type")

        # Convert integer to number
        if schema_type == "integer":
            schema["type"] = "number"
        elif isinstance(schema_type, list):
            # Handle union types from genson (e.g., ["string", "integer"])
            schema["type"] = ["number" if t == "integer" else t for t in schema_type]

        # Recursively process nested structures
        if "properties" in schema:
            for prop_schema in schema["properties"].values():
                self._normalize_integer_to_number(prop_schema)

        if "items" in schema:
            items = schema["items"]
            if isinstance(items, dict):
                self._normalize_integer_to_number(items)

    def _add_evaluation_extensions_recursive(self, schema: Dict[str, Any]) -> None:
        """
        Recursively add IDP evaluation method extensions to schema.

        Adds x-aws-idp-evaluation-method and x-aws-idp-evaluation-threshold
        based on the inferred JSON Schema types.

        Args:
            schema: Schema object to modify in-place
        """
        from idp_common.config.schema_constants import (
            EVALUATION_METHOD_EXACT,
            EVALUATION_METHOD_FUZZY,
            EVALUATION_METHOD_HUNGARIAN,
            EVALUATION_METHOD_NUMERIC_EXACT,
            SCHEMA_ITEMS,
            SCHEMA_PROPERTIES,
            SCHEMA_TYPE,
            TYPE_ARRAY,
            TYPE_BOOLEAN,
            TYPE_INTEGER,
            TYPE_NUMBER,
            TYPE_OBJECT,
            TYPE_STRING,
            X_AWS_IDP_EVALUATION_METHOD,
            X_AWS_IDP_EVALUATION_THRESHOLD,
        )

        schema_type = schema.get(SCHEMA_TYPE)

        # Handle union types from genson (e.g., ["string", "integer"])
        if isinstance(schema_type, list):
            # Use first type for evaluation method
            schema_type = schema_type[0] if schema_type else TYPE_STRING

        # Add evaluation method based on type
        if schema_type == TYPE_STRING:
            schema[X_AWS_IDP_EVALUATION_METHOD] = EVALUATION_METHOD_FUZZY
            schema[X_AWS_IDP_EVALUATION_THRESHOLD] = 0.85
        elif schema_type in [TYPE_NUMBER, TYPE_INTEGER]:
            schema[X_AWS_IDP_EVALUATION_METHOD] = EVALUATION_METHOD_NUMERIC_EXACT
            schema[X_AWS_IDP_EVALUATION_THRESHOLD] = 0.01
        elif schema_type == TYPE_BOOLEAN:
            schema[X_AWS_IDP_EVALUATION_METHOD] = EVALUATION_METHOD_EXACT
        elif schema_type == TYPE_ARRAY:
            # Recursively process array items
            items = schema.get(SCHEMA_ITEMS, {})
            if isinstance(items, dict):
                items_type = items.get(SCHEMA_TYPE)
                # Array of objects gets Hungarian matching
                if items_type == TYPE_OBJECT:
                    schema[X_AWS_IDP_EVALUATION_METHOD] = EVALUATION_METHOD_HUNGARIAN
                # Recurse into items
                self._add_evaluation_extensions_recursive(items)
        elif schema_type == TYPE_OBJECT:
            # Recursively process object properties
            properties = schema.get(SCHEMA_PROPERTIES, {})
            for prop_schema in properties.values():
                self._add_evaluation_extensions_recursive(prop_schema)

    def _get_stickler_model(
        self, document_class: str, expected_data: Optional[Dict[str, Any]] = None
    ) -> Type["StructuredModel"]:
        """Thin delegate to
        ``stickler_backend.model_factory.get_stickler_model``.

        The real implementation lives in ``stickler_backend/`` — this method
        just threads the service's cache dicts through so multiple sections
        share model instances.
        """
        return get_stickler_model(
            document_class=document_class,
            stickler_models=self.stickler_models,
            model_cache=self._model_cache,
            auto_generated_models=self._auto_generated_models,
            infer_schema_fn=self._infer_schema_from_data,
            expected_data=expected_data,
        )

    def _has_no_extractable_schema(
        self,
        document_class: str,
        expected_results: Optional[Dict[str, Any]],
    ) -> bool:
        """Return True if the section's class has no fields to evaluate.

        A section is a scoring no-op when there is nothing to compare against:
        the class is either absent from the schema config (and there is no
        expected data to auto-infer from) or the class is configured with an
        empty attribute list. Scoring these as ``0.0`` — the previous
        behavior — drags document-level and run-level weighted averages down
        even though no extraction failure occurred. Treat them as excluded so
        the aggregation loop can omit them from the weighted mean.
        """
        cache_key = document_class.lower()
        config = self.stickler_models.get(cache_key)
        if config is None:
            # No config — evaluatable only if expected data lets us auto-infer.
            return not expected_results
        schema = config.get("schema") or {}
        properties = schema.get("properties") or {}
        return len(properties) == 0

    @staticmethod
    def _build_excluded_section_result(
        section: Section,
        document_class: str,
        exclusion_reason: str,
        message: str,
    ) -> SectionEvaluationResult:
        """Build a stub ``SectionEvaluationResult`` for an excluded section.

        The stub carries ``evaluation_skipped=True`` and ``weighted_overall_score=None``
        in ``metrics`` so downstream aggregation can distinguish "no fields to
        score" from a legitimate zero. Field names (``exclusion_reason`` /
        ``message``) mirror the vocabulary used by ``section_exclusion.py`` so
        the two exclusion paths share one namespace.
        """
        return SectionEvaluationResult(
            section_id=section.section_id,
            document_class=document_class,
            attributes=[],
            metrics={
                "weighted_overall_score": None,
                "evaluation_skipped": True,
                "exclusion_reason": exclusion_reason,
                "message": message,
            },
        )

    def _prepare_stickler_data(self, uri: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Load extraction results and confidence scores from S3.

        Args:
            uri: S3 URI to the extraction results

        Returns:
            Tuple of (extraction_data, confidence_scores)
        """
        try:
            content = s3.get_json_content(uri)

            # Extract inference result
            if isinstance(content, dict) and "inference_result" in content:
                extraction_data = content["inference_result"]
            else:
                extraction_data = content

            # Extract confidence scores from explainability_info
            confidence_scores = {}
            if isinstance(content, dict) and "explainability_info" in content:
                explainability_info = content["explainability_info"]
                if (
                    isinstance(explainability_info, list)
                    and len(explainability_info) > 0
                ):
                    confidence_scores = explainability_info[0]

            return extraction_data, confidence_scores

        except Exception as e:
            logger.error(
                f"Error loading extraction results from {uri}: {str(e)}", exc_info=True
            )
            return {}, {}

    @staticmethod
    def _unwrap_confidence_envelope(
        value: Dict[str, Any],
        conf_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Strip a synthetic wrapper key off a confidence map, if there is one.

        Some assessment output nests an object's confidence map inside a
        synthetic key that has no counterpart in the extracted value —
        ``{"Item #6": {"LineItemRate": {...}, "LineItemDays": {...}}}`` against
        an inference result of ``{"LineItemRate": 1000.0, "LineItemDays": "..."}``.
        Left in place, every confidence lookup misses and the whole object
        scores with no confidence at all, so the envelope has to come off.

        The original heuristic keyed off shape alone: a single non-metadata key
        whose value held >= 2 confidence children was assumed to be an
        envelope. That misfires on any class whose schema legitimately declares
        **one** top-level object property — ``{"InvoiceDetails": {...}}`` is
        indistinguishable from a wrapper by key count — and the unwrap then
        silently discarded every confidence score beneath it (issue #713).

        This gates the unwrap on evidence from the data instead:

        1. The candidate key names **no field present in** ``value``. A real
           field would appear on both sides; a synthetic envelope key appears
           only on the confidence side. This is the discriminating signal.
        2. Its contents **do** name at least one field present in ``value``,
           so unwrapping demonstrably recovers confidence rather than
           guessing.
        3. Its contents are not themselves a leaf confidence entry (no
           ``confidence`` key), which would make it a field's own scores
           rather than a map of sibling fields.

        Note (1) also makes the unwrap strictly non-destructive: a key absent
        from ``value`` can never be matched by the caller's per-field lookup,
        so the envelope contributes nothing before it is stripped. Because the
        evidence no longer depends on how many children the envelope has, a
        wrapper around a *single* field is now unwrapped too — previously its
        confidence was dropped for the same reason.

        Args:
            value: The extracted object (values only) being annotated.
            conf_data: The confidence map that may be wrapped in one synthetic key.

        Returns:
            The inner confidence map when the evidence above holds, otherwise
            ``conf_data`` unchanged.
        """
        candidate_keys = [k for k in conf_data if k != "confidence_threshold"]
        if len(candidate_keys) != 1:
            return conf_data

        candidate_key = candidate_keys[0]
        inner = conf_data[candidate_key]
        if not isinstance(inner, dict):
            return conf_data

        if candidate_key in value:
            # Declared field of this object, not an envelope - leave it alone.
            return conf_data
        if "confidence" in inner:
            # A field's own confidence entry, not a map of sibling fields.
            return conf_data
        if not any(k in value for k in inner):
            # Nothing under it matches this object - unwrapping would recover
            # no confidence, so there is no evidence it is an envelope.
            return conf_data

        logger.debug(
            f"Unwrapping synthetic confidence envelope {candidate_key!r} "
            f"(absent from extracted value; contents match "
            f"{sorted(k for k in inner if k in value)})"
        )
        return inner

    def _convert_to_rich_values(
        self,
        inference_result: Dict[str, Any],
        explainability_info: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Convert inference result to Stickler Rich Value format.

        Stickler natively supports rich values with embedded confidence:
        {"_value": actual_value, "_confidence": 0.99}

        When using ModelClass(**rich_values), Stickler automatically extracts
        confidence and makes it available in the comparison result as 'prediction_confidences'.

        Handles wrapper keys (Item_N, Record_N) by detecting and unwrapping them
        for backward compatibility with existing extraction data. See
        ``_unwrap_confidence_envelope`` for the evidence that gates the unwrap.

        Args:
            inference_result: Actual extraction output (values only)
            explainability_info: Confidence data from extraction service (already unwrapped dict)

        Returns:
            Dict with rich value format: {"field": {"_value": val, "_confidence": conf}}
        """
        if not explainability_info:
            return inference_result

        def add_confidence(value: Any, conf_data: Any) -> Any:
            """Recursively merge value with confidence data."""
            if conf_data is None:
                return value

            # Simple field with confidence
            if isinstance(conf_data, dict) and "confidence" in conf_data:
                return {"_value": value, "_confidence": conf_data["confidence"]}

            # Array field
            if isinstance(value, list):
                logger.debug(
                    f"Processing array with {len(value)} elements, conf_data type: {type(conf_data).__name__}"
                )
                if isinstance(conf_data, list):
                    # conf_data is a list - direct indexing
                    logger.debug(
                        f"Array: conf_data is list with {len(conf_data)} elements"
                    )
                    return [
                        add_confidence(
                            value[i], conf_data[i] if i < len(conf_data) else None
                        )
                        for i in range(len(value))
                    ]
                elif isinstance(conf_data, dict):
                    # conf_data is a dict with string indices like {"0": {...}, "1": {...}}
                    logger.debug(
                        f"Array: conf_data is dict with keys: {list(conf_data.keys())}"
                    )
                    return [
                        add_confidence(value[i], conf_data.get(str(i)))
                        for i in range(len(value))
                    ]
                else:
                    # No confidence data for array
                    logger.debug(
                        f"Array: no confidence data (conf_data is {type(conf_data).__name__})"
                    )
                    return value

            # Object field - strip a synthetic confidence envelope first
            if isinstance(value, dict) and isinstance(conf_data, dict):
                conf_data = self._unwrap_confidence_envelope(value, conf_data)

                # Standard object processing
                result = {}
                for key in value.keys():
                    # R15 (deferred): ``geometry`` is intentionally dropped
                    # here. Stickler 0.5.0 supports a ``_bbox`` rich-value key
                    # + ``BBoxIoUComparator`` / ``BBoxMAPAccumulator``; to score
                    # localization quality, emit
                    # ``{"_value": v, "_confidence": c, "_bbox": [...]}`` and
                    # enable bbox metrics in bulk aggregation. See §4 R15 in
                    # STICKLER_INTEGRATION_RECOMMENDATIONS.md.
                    if key not in ("geometry", "confidence_threshold"):
                        result[key] = add_confidence(value.get(key), conf_data.get(key))
                return result

            # No confidence data available
            return value

        return add_confidence(inference_result, explainability_info)

    def _get_nested_value(self, obj: Any, path: str) -> Any:
        """
        Get value from nested dict using dot notation path.

        Args:
            obj: Dict to extract value from (already serialized by Pydantic)
            path: Dot-notation path (e.g., "address.city" or "items[0].name")

        Returns:
            Value at the specified path, or None if not found.
        """
        import re

        try:
            # Handle list indices in path (e.g., "items[0].name")
            parts = []
            for part in path.split("."):
                # Check for list index notation
                match = re.match(r"^([^\[]+)\[(\d+)\]$", part)
                if match:
                    parts.append(("field", match.group(1)))
                    parts.append(("index", int(match.group(2))))
                else:
                    parts.append(("field", part))

            # Navigate through the path
            current = obj
            for part_type, part_value in parts:
                if part_type == "field":
                    if isinstance(current, dict):
                        current = current.get(part_value)
                    elif hasattr(current, part_value):
                        # Handle objects with attributes (e.g., Pydantic models, MagicMock)
                        current = getattr(current, part_value)
                    else:
                        return None
                elif part_type == "index":
                    if isinstance(current, (list, tuple)):
                        if part_value < len(current):
                            current = current[part_value]
                        else:
                            return None
                    else:
                        return None

            return current

        except Exception as e:
            logger.debug(f"Error getting nested value for path '{path}': {str(e)}")
            return None

    def _get_confidence_for_field(
        self, confidence_scores: Dict[str, Any], field_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get confidence information for a specific field.

        Args:
            confidence_scores: Nested confidence scores dictionary
            field_name: Field name (may use dot notation or list indices)

        Returns:
            Dictionary with confidence and confidence_threshold, or None
        """
        try:
            # Try to get confidence using the same path logic
            confidence_value = self._get_nested_value(confidence_scores, field_name)

            if isinstance(confidence_value, dict) and "confidence" in confidence_value:
                conf_threshold = confidence_value.get("confidence_threshold")
                return {
                    "confidence": float(confidence_value["confidence"]),
                    "confidence_threshold": (
                        float(conf_threshold) if conf_threshold is not None else None
                    ),
                }

            return None

        except Exception as e:
            logger.debug(
                f"Error extracting confidence for field '{field_name}': {str(e)}"
            )
            return None

    def _generate_reason(
        self,
        field_name: str,
        expected_value: Any,
        actual_value: Any,
        score: float,
        matched: bool,
        comparator: Optional[str],
        is_auto_generated: bool = False,
    ) -> str:
        """
        Generate a reason explanation for the comparison result.

        Args:
            field_name: Name of the field
            expected_value: Expected value
            actual_value: Actual value
            score: Comparison score
            matched: Whether the values matched
            comparator: Comparator type used
            is_auto_generated: Whether the schema was auto-generated

        Returns:
            Reason string explaining the result
        """
        # Check for empty values
        exp_empty = expected_value is None or (
            isinstance(expected_value, str) and not expected_value.strip()
        )
        act_empty = actual_value is None or (
            isinstance(actual_value, str) and not actual_value.strip()
        )

        # Build base reason
        if exp_empty and act_empty:
            base_reason = "Both values are empty"
        elif matched:
            if score >= 0.99:
                base_reason = "Exact match"
            elif score >= 0.9:
                base_reason = f"Very close match (score: {score:.2f})"
            else:
                base_reason = f"Match above threshold (score: {score:.2f})"
        else:
            if exp_empty:
                base_reason = "Expected value missing but actual value present"
            elif act_empty:
                base_reason = "Actual value missing but expected value present"
            else:
                base_reason = f"Values do not match (score: {score:.2f}, comparator: {comparator or 'default'})"

        # Append auto-generation notice if applicable
        if is_auto_generated:
            return f"{base_reason}. Note: Schema inferred (no config)"
        else:
            return base_reason

    def _transform_stickler_result(
        self,
        section: Section,
        expected_instance: "StructuredModel",
        actual_instance: "StructuredModel",
        stickler_result: Dict[str, Any],
        confidence_scores: Dict[str, Any],
        document_context: str = "",
    ) -> SectionEvaluationResult:
        """Delegate to ``stickler_backend.results.transform_stickler_result``.

        The real implementation lives in ``stickler_backend/results.py``; this
        method threads the service's per-instance state (stickler_models,
        auto_generated_models) and display callbacks through.
        """
        return transform_stickler_result(
            section=section,
            expected_instance=expected_instance,
            actual_instance=actual_instance,
            stickler_result=stickler_result,
            confidence_scores=confidence_scores,
            stickler_models=self.stickler_models,
            auto_generated_models=self._auto_generated_models,
            get_nested_value=self._get_nested_value,
            get_confidence_for_field=self._get_confidence_for_field,
            generate_reason=self._generate_reason,
            format_evaluation_method=_format_evaluation_method,
            document_context=document_context,
        )

    # _transform_stickler_result / _annotate_nested_comparison_methods /
    # _resolve_leaf_schema moved to
    # idp_common.evaluation.stickler_backend.results (§6 reorg — R3 lives there).

    # _clean_null_descriptions moved to
    # idp_common.evaluation.stickler_backend.model_factory.clean_null_descriptions
    # (§6 reorg — called inside get_stickler_model, no other callers).

    def _remove_none_values(self, data: Any) -> Any:
        """
        Recursively remove None values from data structure.

        None in extraction results means the field wasn't extracted,
        so we remove it to let Pydantic use field defaults/optional behavior.

        Args:
            data: Data structure to clean

        Returns:
            Cleaned data structure without None values
        """
        if isinstance(data, dict):
            return {
                k: self._remove_none_values(v) for k, v in data.items() if v is not None
            }
        elif isinstance(data, list):
            return [self._remove_none_values(item) for item in data if item is not None]
        else:
            return data

    def _coerce_data_to_schema(
        self, data: Dict[str, Any], model_class: Type["StructuredModel"]
    ) -> Dict[str, Any]:
        """
        Coerce data values to match the Pydantic model's expected types.

        This prevents validation errors when baseline data has different types
        than the schema expects (e.g., float values when schema expects strings).

        Also handles required fields that have None/null values by providing
        appropriate defaults to prevent Pydantic validation errors.

        Args:
            data: Dictionary of extraction data
            model_class: Pydantic model class with field type annotations

        Returns:
            Data dictionary with values coerced to match schema types
        """
        try:
            # Get the model's field information
            model_fields = (
                model_class.model_fields if hasattr(model_class, "model_fields") else {}
            )

            coerced_data = {}

            # First pass: process existing data
            for key, value in data.items():
                if key not in model_fields:
                    # Field not in schema, keep as-is
                    coerced_data[key] = value
                    continue

                field_info = model_fields[key]

                # Get the field's annotation (expected type)
                field_annotation = field_info.annotation

                # Handle Optional types by extracting the inner type
                # Check if it's a Union type (which Optional creates)
                origin = getattr(field_annotation, "__origin__", None)
                if origin is Union:
                    # Get the non-None type from Union
                    args = getattr(field_annotation, "__args__", ())
                    field_annotation = next(
                        (arg for arg in args if arg is not type(None)), field_annotation
                    )

                # Coerce the value based on expected type
                coerced_data[key] = self._coerce_value_to_type(
                    value, field_annotation, key
                )

            # R4: previously a second pass here injected type-appropriate
            # defaults ("", 0, 0.0, False, [], {}) for any "required" field
            # missing from ``coerced_data``. That path was dead in practice
            # (the mapper clears ``required`` at :737-738 and the genson path
            # strips it at :351) but dangerous: injecting ``0`` for a
            # genuinely-absent numeric turns a false negative into a value
            # mismatch (fd), inflating error counts for missing-data cases.
            # Rely instead on all-fields-optional + ``_remove_none_values``.

            return coerced_data

        except Exception as e:
            logger.warning(
                f"Error during type coercion: {str(e)}. Returning original data."
            )
            return data

    def _coerce_value_to_type(
        self, value: Any, expected_type: Any, field_name: str = ""
    ) -> Any:
        """
        Coerce a single value to match the expected type.

        Args:
            value: The value to coerce
            expected_type: The expected type annotation
            field_name: Name of the field (for logging)

        Returns:
            Coerced value matching expected type
        """
        if value is None:
            return None

        # Get the origin of the type (e.g., list, dict) for generic types
        origin = getattr(expected_type, "__origin__", None)

        try:
            # Handle string types
            if expected_type is str:
                if not isinstance(value, str):
                    return str(value)
                return value

            # Handle numeric types
            elif (
                expected_type in (int, float)
                or expected_type is int
                or expected_type is float
            ):
                if isinstance(value, str):
                    # Empty strings for numeric fields should be treated as missing (None)
                    # This prevents Pydantic validation errors like:
                    # "Input should be a valid number, unable to parse string as a number"
                    if not value.strip():
                        return None
                    # Try to convert non-empty string to number
                    try:
                        return (
                            float(value)
                            if expected_type is float
                            else int(float(value))
                        )
                    except ValueError:
                        logger.warning(
                            f"Could not convert '{value}' to {expected_type} for field {field_name}"
                        )
                        # Return None instead of original string to prevent Pydantic crash
                        # The field will be treated as missing/empty during comparison
                        return None
                elif isinstance(value, float) and expected_type is int:
                    # Coerce float to int when schema expects int
                    # This handles cases where baseline has int but prediction has float
                    return int(value)
                elif isinstance(value, int) and expected_type is float:
                    # Coerce int to float when schema expects float
                    return float(value)
                return value

            # Handle boolean
            elif expected_type is bool:
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "yes")
                return bool(value)

            # Handle list types
            elif origin is list:
                if not isinstance(value, list):
                    return value

                # Get the item type if specified
                args = getattr(expected_type, "__args__", ())
                if args:
                    item_type = args[0]
                    # Recursively coerce list items
                    return [
                        self._coerce_value_to_type(item, item_type, f"{field_name}[]")
                        for item in value
                    ]
                return value

            # Handle dict/object types - recursion needed for nested Pydantic models
            elif origin is dict or (hasattr(expected_type, "model_fields")):
                if not isinstance(value, dict):
                    return value

                # If it's a Pydantic model, recursively coerce
                if hasattr(expected_type, "model_fields"):
                    return self._coerce_data_to_schema(value, expected_type)
                return value

            # Default: return as-is
            else:
                return value

        except Exception as e:
            logger.warning(
                f"Error coercing value for field {field_name}: {str(e)}. Returning original value."
            )
            return value

    def _drop_field_at_path(
        self, data: Dict[str, Any], path: Tuple[Any, ...]
    ) -> Dict[str, Any]:
        """
        Return a deep copy of ``data`` with the value at ``path`` removed.

        Pydantic error locations are tuples of dict keys and list indices, e.g.
        ``("PersonalInformation", "ContactInformation", "WorkPhone")`` or
        ``("Liabilities", 0, "UnpaidBalance")``. Navigates that path and deletes
        the leaf. Missing/incompatible path segments are ignored (the leaf may
        not exist on one side).

        Args:
            data: Source data (not mutated)
            path: Pydantic error location tuple

        Returns:
            Deep copy of ``data`` with the offending leaf removed
        """
        import copy

        result = copy.deepcopy(data)
        cursor: Any = result
        for segment in path[:-1]:
            if isinstance(cursor, dict) and segment in cursor:
                cursor = cursor[segment]
            elif (
                isinstance(cursor, list)
                and isinstance(segment, int)
                and 0 <= segment < len(cursor)
            ):
                cursor = cursor[segment]
            else:
                return result  # Path doesn't exist on this side - nothing to drop

        leaf = path[-1]
        if isinstance(cursor, dict):
            cursor.pop(leaf, None)
        elif (
            isinstance(cursor, list)
            and isinstance(leaf, int)
            and 0 <= leaf < len(cursor)
        ):
            del cursor[leaf]
        return result

    def _build_instances_tolerant(
        self,
        model_class: Type["StructuredModel"],
        coerced_expected: Dict[str, Any],
        coerced_actual: Dict[str, Any],
        confidence_scores: Optional[Dict[str, Any]],
        max_drops: int = 50,
    ) -> Tuple[Any, Any, List[Tuple[Any, ...]]]:
        """
        Instantiate expected/actual models, dropping individual fields that fail.

        A single field that fails Pydantic validation would otherwise raise and
        abort the entire section (zeroing every attribute). Instead, on a
        ValidationError we extract the offending field path(s), drop them from
        BOTH expected and actual (so the comparison stays symmetric and fair),
        and retry. This bounds the blast radius to just the unparseable fields.

        UPSTREAM: candidate for `awslabs/stickler` — good fit for a "lenient
        ingest" mode on `StructuredModel.from_json` / `model_validate` that
        drops a single failing field rather than failing the whole record.
        Delete this method once upstream supports it. No open issue yet.

        Args:
            model_class: The Stickler StructuredModel subclass
            coerced_expected: Cleaned/coerced baseline data
            coerced_actual: Cleaned/coerced prediction data
            confidence_scores: Optional confidence data for the rich-value path
            max_drops: Safety cap on retry iterations

        Returns:
            Tuple of (expected_instance, actual_instance, skipped_field_paths)

        Raises:
            Exception: Re-raises if the error is not a per-field ValidationError
                or if it cannot be resolved by dropping fields.
        """
        from pydantic import ValidationError

        skipped: List[Tuple[Any, ...]] = []

        def build():
            expected_instance = model_class(**coerced_expected)
            if confidence_scores:
                actual_rich = self._convert_to_rich_values(
                    coerced_actual, confidence_scores
                )
                actual_instance = model_class.from_json(
                    actual_rich, process_rich_values=True
                )
            else:
                actual_instance = model_class(**coerced_actual)
            return expected_instance, actual_instance

        for _ in range(max_drops):
            try:
                expected_instance, actual_instance = build()
                return expected_instance, actual_instance, skipped
            except ValidationError as ve:
                # Collect the offending field paths from this validation pass
                error_paths = {err["loc"] for err in ve.errors() if err.get("loc")}
                if not error_paths:
                    raise  # Nothing actionable to drop
                progressed = False
                for path in error_paths:
                    if path in skipped:
                        continue
                    coerced_expected = self._drop_field_at_path(coerced_expected, path)
                    coerced_actual = self._drop_field_at_path(coerced_actual, path)
                    skipped.append(path)
                    progressed = True
                if not progressed:
                    # Same fields failing again - avoid infinite loop
                    raise

        # Exceeded max_drops - one final attempt; let any error propagate
        expected_instance, actual_instance = build()
        return expected_instance, actual_instance, skipped

    def _warn_on_multi_instance_shape_mismatch(
        self,
        class_name: Optional[str],
        expected_results: Any,
        actual_results: Any,
        section_id: Any,
    ) -> None:
        """Say so loudly when a baseline's shape does not match the prediction's.

        Turning on ``x-aws-idp-multi-instance`` (GitHub #715) changes a class's
        ``inference_result`` from a flat record to ``{"instances": [ … ]}``.
        Evaluation compares against a stored baseline **of the same shape**, so a
        wrapped prediction against a flat baseline scores every field as
        missing-on-one-side: the class reads ~0 accuracy and **nothing says why**.
        That is the single biggest risk this feature introduces, and relying on the
        operator to remember
        ``scripts/migrate_multi_instance_baselines.py`` is not a control.

        Both directions are checked, because the rollback (flag off, baselines
        still wrapped) fails exactly as silently.

        Advisory only — it never changes a score. The point is that a mismatch
        stops being invisible.
        """
        from idp_common.schema.multi_instance import is_multi_instance, unwrap_instances

        class_schema = None
        for candidate in self.config.classes or []:
            if not isinstance(candidate, dict):
                continue
            label = candidate.get("$id") or candidate.get("x-aws-idp-document-type")
            if (
                isinstance(label, str)
                and isinstance(class_name, str)
                and label.lower() == class_name.lower()
            ):
                class_schema = candidate
                break
        if class_schema is None:
            return

        flagged = is_multi_instance(class_schema)
        expected_wrapped = unwrap_instances(expected_results) is not None
        actual_wrapped = unwrap_instances(actual_results) is not None

        if flagged and actual_wrapped and not expected_wrapped:
            logger.warning(
                "Section %s (class=%s): the extraction result is multi-instance "
                "({'instances': [...]}) but the evaluation BASELINE is flat, so "
                "every field will score as missing on one side and this class's "
                "accuracy will read as ~0 for reasons that have nothing to do "
                "with extraction quality. Migrate the baselines: "
                "python3 scripts/migrate_multi_instance_baselines.py "
                "--stack-name <stack> --apply",
                section_id,
                class_name,
            )
        elif not flagged and expected_wrapped and not actual_wrapped:
            logger.warning(
                "Section %s (class=%s): the evaluation BASELINE is multi-instance "
                "({'instances': [...]}) but the extraction result is flat — "
                "x-aws-idp-multi-instance was presumably turned back off without "
                "migrating the baselines back. This class's accuracy will read as "
                "~0. Roll the baselines back: "
                "python3 scripts/migrate_multi_instance_baselines.py "
                "--stack-name <stack> --direction unwrap --apply",
                section_id,
                class_name,
            )

    def evaluate_section(
        self,
        section: Section,
        expected_results: Dict[str, Any],
        actual_results: Dict[str, Any],
        confidence_scores: Optional[Dict[str, Any]] = None,
        document_context: str = "",
    ) -> SectionEvaluationResult:
        """
        Evaluate extraction results for a document section using Stickler.

        Args:
            section: Document section
            expected_results: Expected extraction results
            actual_results: Actual extraction results
            confidence_scores: Confidence scores for actual values from assessment

        Returns:
            Evaluation results for the section
        """
        class_name = section.classification
        logger.debug(
            f"Evaluating Section {section.section_id} - class: {class_name} using Stickler"
        )

        # No-op detection: a section whose class has no extractable fields
        # (missing from config with no expected data OR configured with an
        # empty attribute list) has nothing to score. Emit a skipped-result
        # stub so the document-level aggregator can exclude it from the
        # weighted mean instead of counting a spurious 0.0.
        if self._has_no_extractable_schema(class_name, expected_results):
            logger.info(
                "Section %s (class=%s) has no extractable schema — excluding "
                "from scoring.",
                section.section_id,
                class_name,
            )
            return self._build_excluded_section_result(
                section=section,
                document_class=class_name,
                exclusion_reason="no_extractable_schema",
                message=(
                    f"Class '{class_name}' has no attributes defined in the "
                    f"evaluation schema; section excluded from scoring."
                ),
            )

        self._warn_on_multi_instance_shape_mismatch(
            class_name, expected_results, actual_results, section.section_id
        )

        try:
            # Get Stickler model for this document class
            # Pass expected_results to enable auto-generation if needed
            ModelClass = self._get_stickler_model(
                class_name, expected_data=expected_results
            )

            # Clean data by removing None values (None means field not extracted)
            cleaned_expected = self._remove_none_values(expected_results)
            cleaned_actual = self._remove_none_values(actual_results)

            # Check for unparsed LLM output (raw_output indicates extraction parsing failed)
            # This typically happens when LLM output is truncated due to max_tokens limit
            if (
                isinstance(cleaned_actual, dict)
                and "raw_output" in cleaned_actual
                and len(cleaned_actual) == 1
            ):
                raw_output_preview = str(cleaned_actual.get("raw_output", ""))[:500]
                logger.error(
                    f"Section {section.section_id}: Extraction produced unparsed raw_output. "
                    f"This indicates the LLM output could not be parsed as valid JSON. "
                    f"Raw output preview: {raw_output_preview}..."
                )

                failure_reason = (
                    f"Extraction parsing failed for section {section.section_id}. "
                    f"The LLM output could not be parsed as valid JSON. "
                    f"This typically indicates truncated output (model hit max_tokens limit). "
                    f"Consider increasing max_tokens in extraction config. "
                    f"Raw output preview: {raw_output_preview[:200]}..."
                )

                # Count expected fields as false negatives since none were extracted
                num_expected_fields = len(cleaned_expected) if cleaned_expected else 1

                return SectionEvaluationResult(
                    section_id=section.section_id,
                    document_class=class_name,
                    attributes=[
                        AttributeEvaluationResult(
                            name="__EXTRACTION_PARSING_FAILED__",
                            expected=f"Expected {num_expected_fields} fields",
                            actual="raw_output (unparsed LLM response)",
                            matched=False,
                            score=0.0,
                            reason=failure_reason,
                            evaluation_method="N/A",
                        )
                    ],
                    metrics={
                        "precision": 0.0,
                        "recall": 0.0,
                        "f1_score": 0.0,
                        "accuracy": 0.0,
                        "false_alarm_rate": 0.0,
                        "false_discovery_rate": 0.0,
                        "weighted_overall_score": 0.0,
                        "evaluation_failed": True,
                        "failure_type": "extraction_parsing_failed",
                    },
                )

            # Coerce data types to match schema expectations
            # This prevents Pydantic validation errors from type mismatches
            coerced_expected = self._coerce_data_to_schema(cleaned_expected, ModelClass)
            coerced_actual = self._coerce_data_to_schema(cleaned_actual, ModelClass)

            # Remove None values introduced by coercion (e.g., empty strings converted to None
            # for numeric fields). Without this second pass, Pydantic would reject None for
            # required numeric fields in nested objects like LineItems[].LineItemRate.
            coerced_expected = self._remove_none_values(coerced_expected)
            coerced_actual = self._remove_none_values(coerced_actual)

            # Create model instances using Rich Value Pattern for confidence integration.
            # Tolerant build: if a single field still fails Pydantic validation (e.g. an
            # unexpected type the coercion didn't catch), drop just that field from BOTH
            # sides and retry, rather than failing the entire section. This limits the
            # blast radius so the rest of the fields still score.
            (
                expected_instance,
                actual_instance,
                skipped_field_paths,
            ) = self._build_instances_tolerant(
                ModelClass, coerced_expected, coerced_actual, confidence_scores
            )

            if skipped_field_paths:
                logger.warning(
                    f"Section {section.section_id}: {len(skipped_field_paths)} field(s) "
                    f"could not be validated and were skipped (the rest were still "
                    f"evaluated): "
                    f"{', '.join('.'.join(p) for p in sorted(skipped_field_paths))}"
                )

            # Compare using Stickler. Flag set comes from the cross-Lambda
            # contract module (change one place if the raw blob shape has to
            # change, and bump STICKLER_RESULT_VERSION with it). Metric
            # classes come via stickler_backend so this module doesn't need
            # a direct ``import stickler`` (single-boundary rule).
            from idp_common.evaluation.contract import compare_with_flags
            from idp_common.evaluation.stickler_backend.confidence import (
                AUROCMetric,
                BrierScoreMetric,
                ECEMetric,
            )

            stickler_result = expected_instance.compare_with(
                actual_instance,
                **compare_with_flags(),
                confidence_metrics=[  # Compute AUROC, ECE, and Brier for confidence calibration
                    AUROCMetric(),
                    ECEMetric(),
                    BrierScoreMetric(),
                ],
            )

            logger.debug(
                f"Stickler comparison complete. Overall score: {stickler_result.get('overall_score', 'N/A'):.3f}"
            )

            # Log confidence extraction if available
            if confidence_scores and "prediction_confidences" in stickler_result:
                logger.debug(
                    f"Stickler extracted {len(stickler_result['prediction_confidences'])} confidence scores for calibration metrics"
                )

            # Patch field_comparisons to add ``field_path`` where Stickler drops it.
            # UPSTREAM: candidate for `awslabs/stickler` — the canonical
            # `expected_key` is always present but `field_path` is inconsistently
            # populated across Stickler versions, so the ConfidenceCalculator
            # (which keys off `field_path`) sometimes can't join a comparison
            # to its confidence value. Delete this shim once Stickler
            # guarantees `field_path` on every field_comparisons row.
            field_comparisons = stickler_result.get("field_comparisons", [])
            if field_comparisons:
                for fc in field_comparisons:
                    # Use expected_key as field_path (the canonical field path)
                    if "field_path" not in fc or fc["field_path"] is None:
                        fc["field_path"] = fc.get("expected_key")

                logger.debug(
                    f"Field comparisons enabled: {len(field_comparisons)} detailed comparisons available"
                )

            # Transform Stickler result to IDP format
            section_result = self._transform_stickler_result(
                section,
                expected_instance,
                actual_instance,
                stickler_result,
                confidence_scores or {},
                document_context=document_context,
            )

            # Surface any fields that were skipped due to per-field validation
            # errors so reduced coverage is visible in the report (not silently
            # dropped). These are informational and excluded from scoring.
            for path in skipped_field_paths:
                field_label = ".".join(str(p) for p in path)
                section_result.attributes.append(
                    AttributeEvaluationResult(
                        name=f"__SKIPPED__{field_label}",
                        expected=None,
                        actual=None,
                        matched=False,
                        score=0.0,
                        reason=(
                            f"Field '{field_label}' could not be validated against "
                            f"the schema and was excluded from scoring. The remaining "
                            f"fields in this section were evaluated normally."
                        ),
                        evaluation_method="N/A",
                    )
                )
            if skipped_field_paths:
                section_result.metrics["skipped_field_count"] = len(skipped_field_paths)

            return section_result

        except ValueError as ve:
            # Schema configuration error - determine specific cause for better messaging
            error_message = str(ve)
            logger.error(
                f"Configuration error for section {section.section_id}: {error_message}",
                exc_info=True,
            )

            # Check for specific known error patterns
            if "field_definitions must contain at least one field" in error_message:
                # This happens when a nested object has empty properties: {}
                # Extract the field name from the error message if possible
                import re

                field_match = re.search(r"Error in field '([^']+)'", error_message)
                field_name = field_match.group(1) if field_match else "unknown"

                failure_type = "empty_nested_object"
                failure_reason = (
                    f"Schema error for document class '{class_name}': "
                    f"Nested object '{field_name}' has no properties defined. "
                    f"Stickler requires at least one field in nested objects. "
                    f"Either add properties to '{field_name}' in your schema or remove it entirely."
                )
            elif "No schema configuration found" in error_message:
                failure_type = "missing_schema_configuration"
                failure_reason = (
                    f"No schema configuration found for document class: {class_name}. "
                    f"Cannot evaluate without configuration or baseline data. "
                    f"Please add configuration for this document class or provide baseline data."
                )
            else:
                # Generic schema/configuration error
                failure_type = "schema_configuration_error"
                failure_reason = f"Schema configuration error for document class '{class_name}': {error_message}"

            return SectionEvaluationResult(
                section_id=section.section_id,
                document_class=class_name,
                attributes=[
                    AttributeEvaluationResult(
                        name="__EVALUATION_FAILURE__",
                        expected=None,
                        actual=None,
                        matched=False,
                        score=0.0,
                        reason=failure_reason,
                        evaluation_method="N/A",
                    )
                ],
                metrics={
                    "precision": 0.0,
                    "recall": 0.0,
                    "f1_score": 0.0,
                    "accuracy": 0.0,
                    "false_alarm_rate": 0.0,
                    "false_discovery_rate": 0.0,
                    "weighted_overall_score": 0.0,
                    "evaluation_failed": True,
                    # Names the cause so the report can explain *this* failure
                    # rather than assuming a missing configuration.
                    "failure_type": failure_type,
                },
            )

        except Exception as e:
            # Data validation error or other issues
            logger.error(
                f"Error evaluating section {section.section_id}: {str(e)}",
                exc_info=True,
            )

            # Check if it's a Pydantic validation error
            error_type = type(e).__name__
            if "ValidationError" in error_type:
                failure_type = "baseline_data_validation_error"
                failure_reason = (
                    f"Data validation error: The baseline data format doesn't match the schema for document class '{class_name}'. "
                    f"This typically means the baseline data has different types than expected. "
                    f"Details: {str(e)}"
                )
            else:
                failure_type = "unexpected_error"
                failure_reason = f"Unexpected error during evaluation: {str(e)}"

            return SectionEvaluationResult(
                section_id=section.section_id,
                document_class=class_name,
                attributes=[
                    AttributeEvaluationResult(
                        name="__EVALUATION_FAILURE__",
                        expected=None,
                        actual=None,
                        matched=False,
                        score=0.0,
                        reason=failure_reason,
                        evaluation_method="N/A",
                    )
                ],
                metrics={
                    "precision": 0.0,
                    "recall": 0.0,
                    "f1_score": 0.0,
                    "accuracy": 0.0,
                    "false_alarm_rate": 0.0,
                    "false_discovery_rate": 0.0,
                    "weighted_overall_score": 0.0,
                    "evaluation_failed": True,  # Flag to identify failed evaluations
                    "failure_type": failure_type,
                },
            )

    # _load_sections_for_doc_split moved to
    # idp_common.evaluation.stickler_backend.doc_split.load_sections_for_doc_split
    # (§6 reorg).

    def _process_section(
        self,
        actual_section: Section,
        expected_section: Section,
        document_context: str = "",
    ) -> Tuple[Optional[SectionEvaluationResult], Dict[str, int]]:
        """
        Process a single section for evaluation.

        Args:
            actual_section: Section with actual extraction results
            expected_section: Section with expected extraction results

        Returns:
            Tuple of (section_result, metrics_count)
        """
        # Load extraction results from S3
        actual_uri = actual_section.extraction_result_uri
        expected_uri = expected_section.extraction_result_uri

        if not actual_uri or not expected_uri:
            logger.warning(
                f"Missing extraction URI for section: {actual_section.section_id}"
            )
            return None, {}

        # Load data and confidence scores
        actual_results, confidence_scores = self._prepare_stickler_data(actual_uri)
        expected_results, _ = self._prepare_stickler_data(expected_uri)

        # Evaluate section using Stickler
        section_result = self.evaluate_section(
            section=actual_section,
            expected_results=expected_results,
            actual_results=actual_results,
            confidence_scores=confidence_scores,
            document_context=document_context,
        )

        # Extract metrics from section result — R3: use Stickler counts stored
        # on section_result.metrics by _transform_stickler_result. No more
        # re-counting from attributes; the two paths used to disagree because
        # attribute-level empty detection ran again with slightly different
        # semantics from Stickler's NullHelper.
        metrics = {
            "tp": 0,
            "fp": 0,
            "fn": 0,
            "tn": 0,
            "fp1": 0,  # -> Stickler `fa` (false alarm: predicted when absent)
            "fp2": 0,  # -> Stickler `fd` (false discovery: predicted wrong value)
        }

        # Skipped no-op sections contribute no confusion-matrix counts —
        # Stickler was never invoked, so tp/fp/fn/tn all stay at zero and the
        # document-level rollup treats the section as absent from scoring.
        if section_result.metrics.get("evaluation_skipped", False):
            return section_result, metrics

        # Check if evaluation failed for this section
        if section_result.metrics.get("evaluation_failed", False):
            # For failed evaluations, count each expected top-level KEY as
            # a false negative. Kept at top-level (not leaf-normalized)
            # because Athena / parquet historical baselines were built
            # against the top-level count — switching to a leaf-normalized
            # count would silently shift historical trends 10-30× on
            # list-heavy schemas without operators knowing what changed.
            # A failed section under-counts fn relative to a healthy
            # section on the same schema; that's a KNOWN LIMITATION,
            # documented, and preferable to a silent baseline break.
            if expected_results:
                num_expected_fields = len(expected_results)
                metrics["fn"] = num_expected_fields if num_expected_fields > 0 else 1
            else:
                # If no expected data, still count as at least 1 failure
                metrics["fn"] = 1

            logger.warning(
                f"Section {section_result.section_id} evaluation failed. "
                f"Counted {metrics['fn']} false negatives for document-level metrics."
            )
            return section_result, metrics

        # Normal processing: pull Stickler's aggregate counts (already stored
        # on section_result.metrics["_stickler_counts"] by
        # _transform_stickler_result).
        counts = section_result.metrics.get("_stickler_counts") or {}
        metrics["tp"] = int(counts.get("tp", 0) or 0)
        metrics["fp"] = int(counts.get("fp", 0) or 0)
        metrics["fn"] = int(counts.get("fn", 0) or 0)
        metrics["tn"] = int(counts.get("tn", 0) or 0)
        metrics["fp1"] = int(counts.get("fa", 0) or 0)
        metrics["fp2"] = int(counts.get("fd", 0) or 0)

        return section_result, metrics

    def evaluate_document(
        self,
        actual_document: Document,
        expected_document: Document,
        store_results: bool = True,
    ) -> Document:
        """
        Evaluate extraction results for an entire document using Stickler.

        This method maintains the same API as the legacy implementation but uses
        Stickler for comparison logic.

        Args:
            actual_document: Document with actual extraction results
            expected_document: Document with expected extraction results
            store_results: Whether to store results in S3 (default: True)

        Returns:
            Updated actual document with evaluation results
        """
        try:
            # Start timing
            start_time = time.time()

            # Calculate document split classification metrics FIRST
            doc_split_metrics_obj = None
            try:
                logger.info("Calculating document split classification metrics...")
                doc_split_calculator = DocSplitClassificationMetrics()
                # Adapter: upstream's load_sections accepts plain dicts. IDP's
                # Document.sections carries Section objects whose data lives in
                # S3 at ``extraction_result_uri``. Load and hand the upstream
                # class dict shape it expects
                # ({section_id, document_class, split_document.page_indices}).
                gt_dicts = load_sections_for_doc_split(
                    expected_document.sections, doc_split_calculator
                )
                pred_dicts = load_sections_for_doc_split(
                    actual_document.sections, doc_split_calculator
                )
                doc_split_calculator.load_sections(
                    ground_truth_sections=gt_dicts,
                    predicted_sections=pred_dicts,
                )

                # Calculate all metrics
                doc_split_results = doc_split_calculator.calculate_all_metrics()

                # Create DocSplitMetrics object
                page_level = doc_split_results["page_level_accuracy"]
                split_no_order = doc_split_results["split_accuracy_without_order"]
                split_with_order = doc_split_results["split_accuracy_with_order"]

                # R14: graded packet metrics from stickler.doc_split. These
                # (V-measure / Rand / ordering) score partial correctness the
                # exact-match counters above ignore — a 9/10-page section
                # scores near 1.0 here but 0/1 under
                # ``split_accuracy_without_order`` (which requires set
                # equality). Average cleanly across documents.
                graded = compute_graded_packet_metrics(
                    doc_split_calculator.sections_gt,
                    doc_split_calculator.sections_pred,
                )

                doc_split_metrics_obj = DocSplitMetrics(
                    page_level_accuracy=page_level["accuracy"],
                    split_accuracy_without_order=split_no_order["accuracy"],
                    split_accuracy_with_order=split_with_order["accuracy"],
                    total_pages=page_level["total_pages"],
                    total_splits=split_no_order["total_sections"],
                    correctly_classified_pages=page_level["correct_pages"],
                    correctly_split_without_order=split_no_order["correct_sections"],
                    correctly_split_with_order=split_with_order["correct_sections"],
                    # Annotated with the classifier's own confidence in each
                    # page's class, so `correct` and `predicted_confidence`
                    # sit side by side — that pairing is the calibration
                    # measurement (GitHub #673). Absent/None for an unscored
                    # page, which is the default.
                    page_details=attach_page_confidence(
                        page_level["page_details"], actual_document
                    ),
                    section_details_without_order=split_no_order["section_details"],
                    section_details_with_order=split_with_order["section_details"],
                    predicted_sections=doc_split_calculator.sections_pred,  # Add predicted sections for unmatched display
                    errors=doc_split_results.get("errors", []),
                    final_score=graded.get("final_score") if graded else None,
                    clustering_score=graded.get("clustering_score") if graded else None,
                    v_measure=graded.get("v_measure") if graded else None,
                    rand_index=graded.get("rand_index") if graded else None,
                    avg_ordering_score=(
                        graded.get("avg_ordering_score") if graded else None
                    ),
                )

                logger.info(
                    f"Doc split metrics calculated - Page accuracy: {page_level['accuracy']:.3f}, "
                    f"Split accuracy (no order): {split_no_order['accuracy']:.3f}, "
                    f"Split accuracy (with order): {split_with_order['accuracy']:.3f}"
                )

            except Exception as e:
                logger.error(
                    f"Error calculating doc split metrics: {str(e)}", exc_info=True
                )
                actual_document.errors.append(f"Doc split metrics error: {str(e)}")

            # Track overall metrics for extraction evaluation
            total_tp = total_fp = total_fn = total_tn = total_fp1 = total_fp2 = 0

            # Create a list of section pairs to evaluate
            # Skip sections whose class is marked as excluded — those sections
            # have no extraction / assessment output by design, so including
            # them would make accuracy metrics meaningless. We track them
            # separately so the markdown report can annotate which sections
            # were intentionally skipped.
            from idp_common.section_exclusion import is_section_excluded

            section_pairs = []
            excluded_sections_info: List[Dict[str, Any]] = []
            for actual_section in actual_document.sections:
                section_id = actual_section.section_id

                if is_section_excluded(actual_section):
                    logger.info(
                        "Evaluation skipped for excluded section %s "
                        "(class=%s, reason=%s)",
                        section_id,
                        actual_section.classification,
                        actual_section.exclusion_reason or "excluded",
                    )
                    excluded_sections_info.append(
                        {
                            "section_id": section_id,
                            "classification": actual_section.classification,
                            "exclusion_reason": actual_section.exclusion_reason,
                            "page_ids": list(actual_section.page_ids or []),
                        }
                    )
                    continue

                # Find corresponding section in expected document
                expected_section = next(
                    (
                        s
                        for s in expected_document.sections
                        if s.section_id == section_id
                    ),
                    None,
                )

                if not expected_section:
                    logger.warning(
                        f"No matching section found for section_id: {section_id}"
                    )
                    continue

                section_pairs.append((actual_section, expected_section))

            section_results = []

            # Track weighted scores for document-level aggregation. R17: sum
            # section scores weighted by section field-count (proxy for
            # section size) rather than taking an unweighted mean over
            # sections — a 20-field section should contribute more than a
            # 2-field section, matching how the aggregation Lambda's Stickler
            # bulk aggregator weights the run-level rollup. Falls back to an
            # unweighted mean when no section reports a field count.
            total_weighted_score = 0.0
            total_field_weight = 0.0
            unweighted_score_sum = 0.0
            unweighted_score_count = 0
            skipped_section_count = 0

            # Map section_id → actual Section so the skipped-section branch
            # can pull ``page_ids`` / ``classification`` for the excluded-
            # sections table without re-scanning the document.
            actual_sections_by_id = {s.section_id: s for s, _ in section_pairs}

            # Pass the document identity through so any per-doc log warnings
            # can be located to the source document (a section_id like
            # ``"s1"`` recurs across documents; without doc-scoping the
            # first doc's warning would silence every subsequent doc's —
            # finding 1 from round-4 review). Prefer input_key over id when
            # present (more human-locatable in CloudWatch).
            # Fall back to a ``uuid4()`` rather than an empty string —
            # multi-doc runs where every document has missing/empty
            # ``input_key`` and ``id`` would otherwise share ``doc_ctx=""``
            # and silence every subsequent doc's anonymous-root warning.
            # A uuid (not ``id(actual_document)``) so the context is unique
            # for the LIFE of the run even if a warm Lambda GC's an earlier
            # doc and reuses its memory address (which ``id()`` would
            # collide on — finding from #625 review).
            doc_ctx = (
                actual_document.input_key
                or actual_document.id
                or f"anonymous:{uuid.uuid4()}"
            )
            # Process sections in parallel using ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                # Submit all section evaluations to the executor
                future_to_section = {
                    executor.submit(
                        self._process_section,
                        actual_section,
                        expected_section,
                        doc_ctx,
                    ): actual_section.section_id
                    for actual_section, expected_section in section_pairs
                }

                # Collect results as they complete
                for future in concurrent.futures.as_completed(future_to_section):
                    section_id = future_to_section[future]
                    try:
                        result, metrics = future.result()
                        if result is None:
                            logger.warning(
                                f"Section {section_id} evaluation returned no result"
                            )
                            continue

                        # Add to section results
                        section_results.append(result)

                        # No-op sections (no extractable schema for the class)
                        # contribute nothing to the document score — Stickler
                        # was never run and their weighted score is intentionally
                        # None. Skip the aggregation branch so a spurious 0.0
                        # doesn't leak in, and record the section in
                        # ``excluded_sections_info`` so the markdown report's
                        # "Excluded Sections (Not Evaluated)" table renders it
                        # alongside the x-aws-idp-exclude-from-processing path.
                        if result.metrics.get("evaluation_skipped"):
                            skipped_section_count += 1
                            skipped_section = actual_sections_by_id.get(section_id)
                            excluded_sections_info.append(
                                {
                                    "section_id": section_id,
                                    "classification": (
                                        skipped_section.classification
                                        if skipped_section
                                        else result.document_class
                                    ),
                                    "exclusion_reason": result.metrics.get(
                                        "exclusion_reason", "no_extractable_schema"
                                    ),
                                    "page_ids": (
                                        list(skipped_section.page_ids or [])
                                        if skipped_section
                                        else []
                                    ),
                                }
                            )
                            continue

                        # Update overall metrics
                        total_tp += metrics["tp"]
                        total_fp += metrics["fp"]
                        total_fn += metrics["fn"]
                        total_tn += metrics["tn"]
                        total_fp1 += metrics["fp1"]
                        total_fp2 += metrics["fp2"]

                        # Track weighted score from section — weight by
                        # section field count (R17). Fall back to unweighted
                        # mean if a section has no field counts recorded.
                        section_weighted_score = result.metrics.get(
                            "weighted_overall_score", 0.0
                        )
                        counts = result.metrics.get("_stickler_counts") or {}
                        # Stickler's invariant: fp == fa + fd (every FA row is
                        # {fa:1, fp:1}; every FD row is {fd:1, fp:1} — see
                        # ``ConfusionMatrixCalculator``). Summing tp+fa+fd+fp+tn+fn
                        # would double-count every FP and bias the rollup toward
                        # low-scoring sections. Use tp+fp+tn+fn instead.
                        section_field_count = float(
                            counts.get("tp", 0)
                            + counts.get("fp", 0)
                            + counts.get("tn", 0)
                            + counts.get("fn", 0)
                        )
                        if section_field_count > 0:
                            total_weighted_score += (
                                section_weighted_score * section_field_count
                            )
                            total_field_weight += section_field_count
                        elif section_weighted_score > 0:
                            unweighted_score_sum += section_weighted_score
                            unweighted_score_count += 1

                    except Exception as e:
                        logger.error(
                            f"Error evaluating section {section_id}: {traceback.format_exc()}"
                        )
                        actual_document.errors.append(
                            f"Error evaluating section {section_id}: {str(e)}"
                        )

            # Sort section results by section_id for consistent output
            # Use natural sorting to handle numeric section IDs correctly (1, 2, 10 vs 1, 10, 2)
            def natural_sort_key(x):
                """Extract numeric parts for natural sorting."""
                import re

                # Split section_id into text and numeric parts
                parts = re.split(r"(\d+)", x.section_id)
                # Convert numeric parts to integers for proper numerical sorting
                return [int(p) if p.isdigit() else p.lower() for p in parts]

            section_results.sort(key=natural_sort_key)

            # Document-level metrics: same formulas as Stickler's
            # ``DerivedMetricsCalculator``, evaluated over the sums of
            # per-section Stickler counts. Inlined here (R10) so the module
            # boundary owns the roll-up shape; individual sections already
            # source ``tp/fa/fd/fp/tn/fn`` directly from Stickler.
            overall_metrics = _compute_derived_metrics(
                tp=total_tp,
                fp=total_fp,
                fn=total_fn,
                tn=total_tn,
                fa=total_fp1,
                fd=total_fp2,
            )

            # Calculate document-level weighted overall score. R17: prefer
            # field-count-weighted mean; only fall back to unweighted mean when
            # no section reported counts (e.g. sections that failed evaluation).
            # Documents whose every section was excluded from scoring (no
            # extractable schema) get ``None`` so downstream UI / rollups can
            # render them as "N/A — Excluded" instead of a misleading 0.0.
            # ``skipped_section_count`` was maintained in the executor loop; no
            # second pass over section_results.
            # Compare against the ORIGINAL pair count, not
            # ``len(section_results)``. Sections that raised an exception
            # (rather than emitting the ``evaluation_skipped`` metric flag)
            # never make it into ``section_results``, so
            # ``len(section_results)`` is already reduced by errored
            # sections. Using it as the denominator would let a doc with
            # (skipped=N, errored=M) match ``skipped_section_count == N ==
            # len(section_results)=N`` and misclassify as "all excluded"
            # even though M sections genuinely failed and should surface as
            # such.
            total_sections_attempted = len(section_pairs)
            all_sections_skipped = (
                skipped_section_count > 0
                and skipped_section_count == total_sections_attempted
            )
            if total_field_weight > 0:
                document_weighted_score = total_weighted_score / total_field_weight
            elif unweighted_score_count > 0:
                document_weighted_score = unweighted_score_sum / unweighted_score_count
            elif all_sections_skipped:
                document_weighted_score = None
            else:
                document_weighted_score = 0.0
            overall_metrics["weighted_overall_score"] = document_weighted_score
            if all_sections_skipped:
                overall_metrics["evaluation_excluded"] = True
                overall_metrics["exclusion_reason"] = "no_extractable_schema"
            if skipped_section_count:
                overall_metrics["skipped_section_count"] = skipped_section_count

            execution_time = time.time() - start_time

            # Validate required document fields
            if not actual_document.id:
                raise ValueError("Document ID is required for evaluation")
            if not actual_document.output_bucket:
                raise ValueError("Output bucket is required for storing results")
            if not actual_document.input_key:
                raise ValueError("Input key is required for storing results")

            # Create evaluation result with doc split metrics and any
            # excluded sections that were intentionally skipped.
            evaluation_result = DocumentEvaluationResult(
                document_id=actual_document.id,
                section_results=section_results,
                overall_metrics=overall_metrics,
                execution_time=execution_time,
                doc_split_metrics=doc_split_metrics_obj,
                excluded_sections=excluded_sections_info,
            )

            # Store results if requested
            if store_results:
                # Generate output path via the cross-Lambda contract module.
                output_bucket = actual_document.output_bucket
                output_key = evaluation_results_key(actual_document.input_key)

                # Store evaluation results in S3.
                result_dict = evaluation_result.to_dict()
                # Stamp the version at write time — the ONLY place we stamp,
                # so round-tripping a historical payload through
                # ``DocumentEvaluationResult`` doesn't silently upgrade it
                # (which would defeat the drift-detection soft gate). Any new
                # writer of ``results.json`` must call this line explicitly.
                result_dict["stickler_result_version"] = STICKLER_RESULT_VERSION
                # Convert numpy types to native Python types for JSON serialization
                result_dict = _convert_numpy_types(result_dict)
                s3.write_content(
                    content=result_dict,
                    bucket=output_bucket,
                    key=output_key,
                    content_type="application/json",
                )

                # Generate Markdown report
                markdown_report = evaluation_result.to_markdown()
                report_key = f"{actual_document.input_key}/evaluation/report.md"
                s3.write_content(
                    content=markdown_report,
                    bucket=output_bucket,
                    key=report_key,
                    content_type="text/markdown",
                )

                # Update document with evaluation report and results URIs
                actual_document.evaluation_report_uri = (
                    f"s3://{output_bucket}/{report_key}"
                )
                actual_document.evaluation_results_uri = (
                    f"s3://{output_bucket}/{output_key}"
                )
                actual_document.status = Status.COMPLETED

                logger.info(
                    f"Evaluation complete for document {actual_document.id} in {execution_time:.2f} seconds"
                )

            # Attach evaluation result to document for immediate use
            actual_document.evaluation_result = evaluation_result

            return actual_document

        except Exception as e:
            logger.error(f"Error evaluating document: {traceback.format_exc()}")
            actual_document.errors.append(f"Evaluation error: {str(e)}")
            return actual_document
