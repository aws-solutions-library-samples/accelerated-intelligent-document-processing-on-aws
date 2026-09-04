# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Assessment service for evaluating document extraction confidence using LLMs.

This module provides a service for assessing the confidence and accuracy of
extraction results by analyzing them against source documents using LLMs,
with support for text and image content.

Large list fields (e.g. hundreds of transaction rows) are assessed in batches of
``extraction.confidence.list_batch_size`` rows via
``idp_common.assessment.batching`` so the model reliably enumerates every row.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from idp_common import bedrock, image, metrics, s3, utils
from idp_common.config.models import IDPConfig
from idp_common.config.schema_constants import (
    SCHEMA_DESCRIPTION,
    SCHEMA_ITEMS,
    SCHEMA_PROPERTIES,
    SCHEMA_TYPE,
    TYPE_ARRAY,
    TYPE_OBJECT,
    TYPE_STRING,
    X_AWS_IDP_CONFIDENCE_THRESHOLD,
    X_AWS_IDP_LIST_ITEM_DESCRIPTION,
)
from idp_common.config.schema_utils import deref_schema
from idp_common.models import Document
from idp_common.utils import extract_json_from_text, repair_truncated_json

logger = logging.getLogger(__name__)


def _safe_float_conversion(value: Any, default: float = 0.0) -> float:
    """
    Safely convert a value to float, handling strings and None values.

    Args:
        value: Value to convert to float
        default: Default value if conversion fails

    Returns:
        Float value or default if conversion fails
    """
    if value is None:
        return default

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        # Handle empty strings
        if not value.strip():
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            logger.warning(
                f"Could not convert string '{value}' to float, using default {default}"
            )
            return default

    # Handle other types by attempting conversion
    try:
        return float(value)
    except (ValueError, TypeError):
        logger.warning(
            f"Could not convert {type(value)} '{value}' to float, using default {default}"
        )
        return default


@dataclass
class AssessmentCoreResult:
    """In-memory result of the pure assessment-inference core.

    This is what ``AssessmentService.assess_results`` returns so callers other
    than the S3/Document-bound ``process_document_section`` (notably the agentic
    in-shard assessment path) can reuse the exact same inference + threshold +
    alert logic without round-tripping S3. Geometry grounding is deliberately
    NOT applied here — the caller grounds against whatever page-data scope it
    owns (the standalone path grounds per section; the sharded path grounds once
    post-merge over the whole section), so the core stays scope-agnostic.
    """

    # ``{field: {confidence, confidence_reason, confidence_threshold, ...}}`` —
    # becomes ``explainability_info[0]`` once the caller has it.
    enhanced_assessment: Dict[str, Any] = field(default_factory=dict)
    # ``[{attribute_name, confidence, confidence_threshold}, ...]``
    confidence_threshold_alerts: List[Dict[str, Any]] = field(default_factory=list)
    metering: Dict[str, Any] = field(default_factory=dict)
    parsing_succeeded: bool = True
    duration_seconds: float = 0.0
    # True when the model response hit its max-output-tokens ceiling
    # (Converse ``stopReason == "max_tokens"``). A truncated response is usually
    # unparseable JSON, so callers must NOT treat the resulting default 0.5 /
    # null-placeholder scores as real — the batcher retries such a call over a
    # smaller row slice instead of accepting the placeholder.
    truncated: bool = False


class AssessmentService:
    """Service for assessing extraction result confidence using LLMs."""

    def __init__(
        self,
        region: str | None = None,
        config: Union[Dict[str, Any], IDPConfig, None] = None,
    ):
        """
        Initialize the assessment service.

        Args:
            region: AWS region for Bedrock
            config: Configuration dictionary or IDPConfig model
        """
        # Convert config to IDPConfig if needed
        if config is None:
            config_model = IDPConfig()
        elif isinstance(config, IDPConfig):
            config_model = config
        elif isinstance(config, dict):
            config_model = IDPConfig(**config)
        else:
            # Fallback: attempt conversion for other types
            try:
                config_model = IDPConfig(**config)
            except Exception as e:
                logger.error(f"Failed to convert config to IDPConfig: {e}")
                raise ValueError(
                    f"Invalid config type: {type(config)}. Expected None, dict, or IDPConfig instance."
                )

        self.config = config_model
        self.region = region or os.environ.get("AWS_REGION")

        # Get model_id from typed config for logging (v0.6: extraction.confidence)
        model_id = self.config.extraction.confidence.model
        logger.info(f"Initialized assessment service with model {model_id}")

    def _get_class_schema(self, class_label: str) -> Dict[str, Any]:
        """
        Get the EFFECTIVE JSON Schema for a document class.

        Assessment reads the class schema **from config**, not from the
        extraction output, so it must derive the multi-instance wrapper
        independently (GitHub #715, plan D2). Doing it here — the single point
        assessment loads a schema from — is what makes the prompt's property
        descriptions, the per-attribute threshold lookup, the ``attr_type ==
        "list"`` branch that produces ``instances[i]`` row keys,
        ``resolve_array_item_thresholds`` and the batching module's
        ``_schema_field_mismatch_reason`` guard all see the same shape extraction
        produced. Without it, ``instances`` is an unknown key: the whole section
        collapses to one ``{"confidence": 0.5}`` leaf and the escalation ladder
        blacklists it permanently.

        A no-op (same object) for every unflagged class.

        Args:
            class_label: The document class name

        Returns:
            JSON Schema dict for the class, or empty dict if not found
        """
        from idp_common.assessment.threshold_resolver import find_class_schema
        from idp_common.schema.multi_instance import wrap_class_schema

        return (
            wrap_class_schema(find_class_schema(class_label, self.config.classes)) or {}
        )

    def _resolve_confidence_escalation_model(self, class_label: str) -> Optional[str]:
        """Pick the stronger confidence model the self-healing ladder escalates to.

        Precedence mirrors extraction's ``_resolve_escalation_model``: per-class
        ``x-aws-idp-confidence-escalation-model`` override > global
        ``extraction.confidence.escalation_model``. Returns None when neither is
        set, in which case the ladder's model-escalation rung is skipped (it stays
        at token-aware shrink + same-model retry).
        """
        from idp_common.config.schema_constants import (
            X_AWS_IDP_CONFIDENCE_ESCALATION_MODEL,
        )

        class_schema = self._get_class_schema(class_label)
        return (
            class_schema.get(X_AWS_IDP_CONFIDENCE_ESCALATION_MODEL)
            or self.config.extraction.confidence.escalation_model
        )

    def _format_property_descriptions(self, schema: Dict[str, Any]) -> str:
        """
        Format property descriptions from JSON Schema for the prompt.

        Group and list properties are commonly declared as a ``$ref`` into the
        class's ``$defs`` (this is what the UI's schema editor emits), so every
        subschema is dereferenced before its type/description is read — otherwise
        the group renders as a bare name with an empty description and the
        confidence model never sees what the field is supposed to contain.

        Args:
            schema: JSON Schema dict for the document class

        Returns:
            Formatted property descriptions as a string
        """
        properties = schema.get(SCHEMA_PROPERTIES, {})
        formatted_lines = []

        for prop_name, prop_schema in properties.items():
            prop_schema = deref_schema(prop_schema, schema)
            prop_type = prop_schema.get(SCHEMA_TYPE)
            description = prop_schema.get(SCHEMA_DESCRIPTION, "")

            if prop_type == TYPE_OBJECT:
                formatted_lines.append(f"{prop_name}  \t[ {description} ]")
                nested_props = prop_schema.get(SCHEMA_PROPERTIES, {})
                for nested_name, nested_schema in nested_props.items():
                    nested_desc = deref_schema(nested_schema, schema).get(
                        SCHEMA_DESCRIPTION, ""
                    )
                    formatted_lines.append(f"  - {nested_name}  \t[ {nested_desc} ]")

            elif prop_type == TYPE_ARRAY:
                formatted_lines.append(f"{prop_name}  \t[ {description} ]")
                items_schema = deref_schema(prop_schema.get(SCHEMA_ITEMS, {}), schema)

                item_desc = prop_schema.get(X_AWS_IDP_LIST_ITEM_DESCRIPTION, "")
                if item_desc:
                    formatted_lines.append(f"  Each item: {item_desc}")

                if items_schema.get(SCHEMA_TYPE) == TYPE_OBJECT:
                    item_props = items_schema.get(SCHEMA_PROPERTIES, {})
                    for item_name, item_schema in item_props.items():
                        item_prop_desc = deref_schema(item_schema, schema).get(
                            SCHEMA_DESCRIPTION, ""
                        )
                        formatted_lines.append(
                            f"  - {item_name}  \t[ {item_prop_desc} ]"
                        )
            else:
                formatted_lines.append(f"{prop_name}  \t[ {description} ]")

        return "\n".join(formatted_lines)

    def _enhance_dict_assessment(
        self, assessment_dict: Dict[str, Any], threshold: float
    ) -> Dict[str, Any]:
        """
        Enhance an assessment dictionary by adding confidence thresholds to confidence assessments.

        Args:
            assessment_dict: Dictionary containing assessment data
            threshold: Confidence threshold to add

        Returns:
            Enhanced assessment dictionary
        """
        # Safety check: ensure assessment_dict is actually a dictionary
        if not isinstance(assessment_dict, dict):
            logger.warning(
                f"Expected dictionary for assessment enhancement, got {type(assessment_dict)}. "
                f"Creating default assessment structure."
            )
            return {
                "confidence": 0.5,
                "confidence_reason": f"LLM returned unexpected type {type(assessment_dict)} instead of dictionary. Using default confidence.",
                "confidence_threshold": threshold,
            }

        # Check if this dictionary itself is a confidence assessment
        if "confidence" in assessment_dict:
            # This is a direct confidence assessment - add threshold
            return {
                **assessment_dict,
                "confidence_threshold": threshold,
            }

        # Otherwise, check nested values for confidence assessments
        enhanced = {}
        for key, value in assessment_dict.items():
            if isinstance(value, dict) and "confidence" in value:
                # This is a nested confidence assessment - add threshold
                enhanced[key] = {
                    **value,
                    "confidence_threshold": threshold,
                }
            elif isinstance(value, dict):
                # Recursively process nested dictionaries
                enhanced[key] = self._enhance_dict_assessment(value, threshold)
            else:
                # Not a confidence assessment - pass through unchanged
                enhanced[key] = value
        return enhanced

    def _enhance_dict_assessment_with_field_thresholds(
        self,
        assessment_dict: Dict[str, Any],
        default_threshold: float,
        field_thresholds: Dict[str, float],
    ) -> Dict[str, Any]:
        """Enhance a list-item assessment dict using per-sub-field thresholds.

        Like ``_enhance_dict_assessment`` but looks up each sub-field's threshold
        from ``field_thresholds`` (resolved from ``$defs`` via
        ``resolve_array_item_thresholds``). Falls back to ``default_threshold``
        for sub-fields not in the map.

        Args:
            assessment_dict: A single list-item assessment (e.g. one W2 copy's
                per-field confidence data).
            default_threshold: Fallback threshold when a sub-field has no entry
                in ``field_thresholds``.
            field_thresholds: ``{sub_field_name: threshold}`` from the resolved
                item schema.

        Returns:
            Enhanced assessment dict with per-sub-field ``confidence_threshold``.
        """
        if not isinstance(assessment_dict, dict):
            logger.warning(
                f"Expected dictionary for assessment enhancement, got {type(assessment_dict)}. "
                f"Creating default assessment structure."
            )
            return {
                "confidence": 0.5,
                "confidence_reason": f"LLM returned unexpected type {type(assessment_dict)} instead of dictionary. Using default confidence.",
                "confidence_threshold": default_threshold,
            }

        # If this dict itself is a confidence leaf (shouldn't happen for a list
        # item, but safety)
        if "confidence" in assessment_dict:
            return {
                **assessment_dict,
                "confidence_threshold": default_threshold,
            }

        # If no per-field thresholds resolved, fall back to uniform threshold
        if not field_thresholds:
            return self._enhance_dict_assessment(assessment_dict, default_threshold)

        # Apply per-sub-field thresholds
        enhanced = {}
        for key, value in assessment_dict.items():
            sub_threshold = field_thresholds.get(key, default_threshold)
            if isinstance(value, dict) and "confidence" in value:
                enhanced[key] = {
                    **value,
                    "confidence_threshold": sub_threshold,
                }
            elif isinstance(value, dict):
                # Nested dict without "confidence" — recurse with same sub_threshold
                enhanced[key] = self._enhance_dict_assessment(value, sub_threshold)
            else:
                enhanced[key] = value
        return enhanced

    def _check_confidence_alerts_with_field_thresholds(
        self,
        assessment_data: Dict[str, Any],
        attr_name: str,
        default_threshold: float,
        field_thresholds: Dict[str, float],
        alerts_list: List[Dict[str, Any]],
    ) -> None:
        """Check list-item assessment for threshold violations using per-sub-field thresholds.

        Like ``_check_confidence_alerts`` but uses ``field_thresholds`` to
        apply different thresholds per sub-field within a list item.

        Args:
            assessment_data: A single list-item assessment dict.
            attr_name: Prefix path (e.g. ``"w2_copies[0]"``).
            default_threshold: Fallback threshold for unresolved sub-fields.
            field_thresholds: ``{sub_field_name: threshold}`` from resolved schema.
            alerts_list: List to append alerts to (modified in place).
        """
        if not isinstance(assessment_data, dict):
            return

        # If this dict is itself a confidence leaf, alert on it — then still fall
        # through to the sub-field scan below. A dict can carry BOTH its own
        # ``confidence`` and nested per-field children; ``_check_confidence_alerts``
        # reports both levels, so returning early here would silently drop the
        # children's alerts relative to the uniform-threshold path.
        if "confidence" in assessment_data:
            confidence = _safe_float_conversion(
                assessment_data.get("confidence", 0.0), 0.0
            )
            if confidence < _safe_float_conversion(default_threshold, 0.9):
                alerts_list.append(
                    {
                        "attribute_name": attr_name,
                        "confidence": confidence,
                        "confidence_threshold": _safe_float_conversion(
                            default_threshold, 0.9
                        ),
                    }
                )

        # If no per-field thresholds, fall back to uniform check for the
        # sub-fields. Skip the leaf re-check (already handled above) by only
        # delegating when this dict is not itself a leaf.
        if not field_thresholds:
            if "confidence" not in assessment_data:
                self._check_confidence_alerts(
                    assessment_data, attr_name, default_threshold, alerts_list
                )
            else:
                for sub_attr_name, sub_assessment in assessment_data.items():
                    if (
                        isinstance(sub_assessment, dict)
                        and "confidence" in sub_assessment
                    ):
                        self._check_confidence_alerts(
                            sub_assessment,
                            f"{attr_name}.{sub_attr_name}",
                            default_threshold,
                            alerts_list,
                        )
            return

        # Check each sub-field against its specific threshold
        for sub_attr_name, sub_assessment in assessment_data.items():
            if isinstance(sub_assessment, dict) and "confidence" in sub_assessment:
                confidence = _safe_float_conversion(
                    sub_assessment.get("confidence", 0.0), 0.0
                )
                sub_threshold = _safe_float_conversion(
                    field_thresholds.get(sub_attr_name, default_threshold), 0.9
                )
                if confidence < sub_threshold:
                    alerts_list.append(
                        {
                            "attribute_name": f"{attr_name}.{sub_attr_name}",
                            "confidence": confidence,
                            "confidence_threshold": sub_threshold,
                        }
                    )

    def _check_confidence_alerts(
        self,
        assessment_data: Dict[str, Any],
        attr_name: str,
        threshold: float,
        alerts_list: List[Dict[str, Any]],
    ) -> None:
        """
        Check assessment data for confidence threshold violations and add alerts.

        Args:
            assessment_data: Dictionary containing assessment data
            attr_name: Name of the attribute being assessed
            threshold: Confidence threshold to check against
            alerts_list: List to append alerts to (modified in place)
        """
        # Safety check: ensure assessment_data is actually a dictionary
        if not isinstance(assessment_data, dict):
            logger.warning(
                f"Expected dictionary for confidence alert checking, got {type(assessment_data)} for attribute '{attr_name}'. "
                f"Skipping confidence alert check."
            )
            return

        # Safety check: ensure threshold is a valid float
        safe_threshold = _safe_float_conversion(threshold, 0.9)

        # First check if this assessment_data itself is a direct confidence assessment
        if "confidence" in assessment_data:
            confidence = _safe_float_conversion(
                assessment_data.get("confidence", 0.0), 0.0
            )
            if confidence < safe_threshold:
                alerts_list.append(
                    {
                        "attribute_name": attr_name,
                        "confidence": confidence,
                        "confidence_threshold": safe_threshold,
                    }
                )

        # Then check for nested sub-attributes (for group/complex attributes)
        for sub_attr_name, sub_assessment in assessment_data.items():
            if isinstance(sub_assessment, dict) and "confidence" in sub_assessment:
                confidence = _safe_float_conversion(
                    sub_assessment.get("confidence", 0.0), 0.0
                )
                if confidence < safe_threshold:
                    full_attr_name = (
                        f"{attr_name}.{sub_attr_name}"
                        if "." not in attr_name
                        else f"{attr_name}.{sub_attr_name}"
                    )
                    alerts_list.append(
                        {
                            "attribute_name": full_attr_name,
                            "confidence": confidence,
                            "confidence_threshold": safe_threshold,
                        }
                    )

    def _prepare_prompt_from_template(
        self,
        prompt_template: str,
        substitutions: Dict[str, str],
        required_placeholders: List[str] = None,
    ) -> str:
        """
        Prepare prompt from template by replacing placeholders with values.

        Args:
            prompt_template: The prompt template with placeholders
            substitutions: Dictionary of placeholder values
            required_placeholders: List of placeholder names that must be present in the template

        Returns:
            String with placeholders replaced by values

        Raises:
            ValueError: If a required placeholder is missing from the template
        """
        from idp_common.bedrock import format_prompt

        return format_prompt(prompt_template, substitutions, required_placeholders)

    def _build_content_with_or_without_image_placeholder(
        self,
        prompt_template: str,
        document_text: str,
        class_label: str,
        attribute_descriptions: str,
        extraction_results: str,
        ocr_text_confidence: str = "",
        image_content: Any = None,
    ) -> List[Dict[str, Any]]:
        """
        Build content array, automatically deciding whether to use image placeholder processing.

        Args:
            prompt_template: The prompt template that may contain {DOCUMENT_IMAGE}
            document_text: The document text content
            class_label: The document class label
            attribute_descriptions: Formatted attribute names and descriptions
            extraction_results: JSON string of extraction results to assess
            ocr_text_confidence: Raw OCR results with confidence scores
            image_content: Optional image content to insert

        Returns:
            List of content items with text and image content properly ordered
        """
        if "{DOCUMENT_IMAGE}" in prompt_template:
            return self._build_content_with_image_placeholder(
                prompt_template,
                document_text,
                class_label,
                attribute_descriptions,
                extraction_results,
                ocr_text_confidence,
                image_content,
            )
        else:
            return self._build_content_without_image_placeholder(
                prompt_template,
                document_text,
                class_label,
                attribute_descriptions,
                extraction_results,
                ocr_text_confidence,
                image_content,
            )

    def _build_content_with_image_placeholder(
        self,
        prompt_template: str,
        document_text: str,
        class_label: str,
        attribute_descriptions: str,
        extraction_results: str,
        ocr_text_confidence: str,
        image_content: Any = None,
    ) -> List[Dict[str, Any]]:
        """
        Build content array with image inserted at DOCUMENT_IMAGE placeholder if present.

        Args:
            prompt_template: The prompt template that may contain {DOCUMENT_IMAGE}
            document_text: The document text content
            class_label: The document class label
            attribute_descriptions: Formatted attribute names and descriptions
            extraction_results: JSON string of extraction results to assess
            ocr_text_confidence: Raw OCR results with confidence scores
            image_content: Optional image content to insert

        Returns:
            List of content items with text and image content properly ordered
        """
        # Split the prompt at the DOCUMENT_IMAGE placeholder
        parts = prompt_template.split("{DOCUMENT_IMAGE}")

        if len(parts) != 2:
            raise ValueError(
                f"Invalid DOCUMENT_IMAGE placeholder usage: found {len(parts) - 1} occurrences, "
                f"but exactly 1 is required. The DOCUMENT_IMAGE placeholder must appear exactly once in the template."
            )

        # Process the parts before and after the image placeholder
        before_image = self._prepare_prompt_from_template(
            parts[0],
            {
                "DOCUMENT_TEXT": document_text,
                "DOCUMENT_CLASS": class_label,
                "ATTRIBUTE_NAMES_AND_DESCRIPTIONS": attribute_descriptions,
                "EXTRACTION_RESULTS": extraction_results,
                "OCR_TEXT_CONFIDENCE": ocr_text_confidence,
            },
            required_placeholders=[],  # Don't enforce required placeholders for partial templates
        )

        after_image = self._prepare_prompt_from_template(
            parts[1],
            {
                "DOCUMENT_TEXT": document_text,
                "DOCUMENT_CLASS": class_label,
                "ATTRIBUTE_NAMES_AND_DESCRIPTIONS": attribute_descriptions,
                "EXTRACTION_RESULTS": extraction_results,
                "OCR_TEXT_CONFIDENCE": ocr_text_confidence,
            },
            required_placeholders=[],  # Don't enforce required placeholders for partial templates
        )

        # Build content array with image in the middle
        content = []

        # Add the part before the image
        if before_image.strip():
            content.append({"text": before_image})

        # Add the image if available
        if image_content:
            if isinstance(image_content, list):
                # Multiple images - no limit with latest Bedrock API
                logger.info(
                    f"Attaching {len(image_content)} images to assessment prompt"
                )
                for img in image_content:
                    content.append(image.prepare_bedrock_image_attachment(img))
            else:
                # Single image
                content.append(image.prepare_bedrock_image_attachment(image_content))

        # Add the part after the image
        if after_image.strip():
            content.append({"text": after_image})

        return content

    def _build_content_without_image_placeholder(
        self,
        prompt_template: str,
        document_text: str,
        class_label: str,
        attribute_descriptions: str,
        extraction_results: str,
        ocr_text_confidence: str,
        image_content: Any = None,
    ) -> List[Dict[str, Any]]:
        """
        Build content array without DOCUMENT_IMAGE placeholder (text-only processing).

        Args:
            prompt_template: The prompt template
            document_text: The document text content
            class_label: The document class label
            attribute_descriptions: Formatted attribute names and descriptions
            extraction_results: JSON string of extraction results to assess
            ocr_text_confidence: Raw OCR results with confidence scores
            image_content: Ignored - images are only attached when DOCUMENT_IMAGE placeholder is present

        Returns:
            List of content items with text content only
        """
        # Prepare the full prompt
        task_prompt = self._prepare_prompt_from_template(
            prompt_template,
            {
                "DOCUMENT_TEXT": document_text,
                "DOCUMENT_CLASS": class_label,
                "ATTRIBUTE_NAMES_AND_DESCRIPTIONS": attribute_descriptions,
                "EXTRACTION_RESULTS": extraction_results,
                "OCR_TEXT_CONFIDENCE": ocr_text_confidence,
            },
            required_placeholders=[],
        )

        # Return text content only - no images unless DOCUMENT_IMAGE placeholder is used
        return [{"text": task_prompt}]

    def _get_text_confidence_data(self, page) -> str:
        """
        Get text confidence data for a page from pre-generated text confidence files.

        Args:
            page: Page object containing OCR URIs

        Returns:
            JSON string of text confidence data, or empty string if unavailable
        """
        # First try to use the pre-generated text confidence file
        if hasattr(page, "text_confidence_uri") and page.text_confidence_uri:
            try:
                text_confidence_data = s3.get_json_content(page.text_confidence_uri)
                return json.dumps(text_confidence_data, indent=2)
            except Exception as e:
                logger.warning(
                    f"Failed to read text confidence data for page {page.page_id}: {str(e)}"
                )

        # Fallback: use raw OCR data if text confidence is not available (for backward compatibility)
        if page.raw_text_uri:
            try:
                from idp_common.ocr.service import OcrService

                ocr_service = OcrService()
                raw_ocr_data = s3.get_json_content(page.raw_text_uri)
                text_confidence_data = ocr_service._generate_text_confidence_data(
                    raw_ocr_data
                )
                return json.dumps(text_confidence_data, indent=2)
            except Exception as e:
                logger.warning(
                    f"Failed to generate text confidence data for page {page.page_id}: {str(e)}"
                )

        return ""

    def _convert_bbox_to_geometry(
        self, bbox_coords: List[float], page_num: int
    ) -> Dict[str, Any]:
        """
        Convert [x1,y1,x2,y2] coordinates to geometry format.

        Args:
            bbox_coords: List of 4 coordinates [x1, y1, x2, y2] in 0-1000 scale
            page_num: Page number where the bounding box appears

        Returns:
            Dictionary in geometry format compatible with pattern-1 UI
        """
        if len(bbox_coords) != 4:
            raise ValueError(f"Expected 4 coordinates, got {len(bbox_coords)}")

        x1, y1, x2, y2 = bbox_coords

        # Ensure coordinates are in correct order
        x1, x2 = min(x1, x2), max(x1, x2)
        y1, y2 = min(y1, y2), max(y1, y2)

        # Convert from normalized 0-1000 scale to 0-1
        left = x1 / 1000.0
        top = y1 / 1000.0
        width = (x2 - x1) / 1000.0
        height = (y2 - y1) / 1000.0

        return {
            "boundingBox": {"top": top, "left": left, "width": width, "height": height},
            "page": page_num,
        }

    def _process_single_assessment_geometry(
        self, attr_assessment: Dict[str, Any], attr_name: str = ""
    ) -> Dict[str, Any]:
        """
        Process geometry data for a single assessment (with confidence key).

        Args:
            attr_assessment: Single assessment dictionary with confidence data
            attr_name: Name of attribute for logging

        Returns:
            Enhanced assessment with geometry converted to proper format
        """
        enhanced_attr = attr_assessment.copy()

        # Check if this assessment includes bbox data
        if "bbox" in attr_assessment or "page" in attr_assessment:
            # Both bbox and page are required for valid geometry
            if "bbox" in attr_assessment and "page" in attr_assessment:
                try:
                    bbox_coords = attr_assessment["bbox"]
                    page_num = attr_assessment["page"]

                    # Validate bbox coordinates
                    if isinstance(bbox_coords, list) and len(bbox_coords) == 4:
                        # Convert to geometry format
                        geometry = self._convert_bbox_to_geometry(bbox_coords, page_num)
                        enhanced_attr["geometry"] = [geometry]

                        logger.debug(
                            f"Converted bounding box for {attr_name}: {bbox_coords} -> geometry format"
                        )
                    else:
                        logger.warning(
                            f"Invalid bounding box format for {attr_name}: {bbox_coords}"
                        )

                except Exception as e:
                    logger.warning(
                        f"Failed to process bounding box for {attr_name}: {str(e)}"
                    )
            else:
                # If only one of bbox/page exists, log a warning about incomplete data
                if "bbox" in attr_assessment and "page" not in attr_assessment:
                    logger.warning(
                        f"Found bbox without page for {attr_name} - removing incomplete bbox data"
                    )
                elif "page" in attr_assessment and "bbox" not in attr_assessment:
                    logger.warning(
                        f"Found page without bbox for {attr_name} - removing incomplete page data"
                    )

            # Always remove raw bbox/page data from output (whether processed or incomplete)
            enhanced_attr.pop("bbox", None)
            enhanced_attr.pop("page", None)

        return enhanced_attr

    def _extract_geometry_from_assessment(
        self, assessment_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Extract geometry data from assessment response and convert to proper format.
        Now supports recursive processing of nested group attributes.

        Args:
            assessment_data: Dictionary containing assessment results from LLM

        Returns:
            Enhanced assessment data with geometry information converted to proper format
        """
        enhanced_assessment = {}

        for attr_name, attr_assessment in assessment_data.items():
            if isinstance(attr_assessment, dict):
                # Check if this is a direct confidence assessment
                if "confidence" in attr_assessment:
                    # This is a direct assessment - process its geometry
                    enhanced_assessment[attr_name] = (
                        self._process_single_assessment_geometry(
                            attr_assessment, attr_name
                        )
                    )
                else:
                    # This is a group attribute (no direct confidence) - recursively process nested attributes
                    logger.debug(f"Processing group attribute: {attr_name}")
                    enhanced_assessment[attr_name] = (
                        self._extract_geometry_from_assessment(attr_assessment)
                    )

            elif isinstance(attr_assessment, list):
                # Handle list attributes - process each item recursively
                enhanced_list = []
                for i, item_assessment in enumerate(attr_assessment):
                    if isinstance(item_assessment, dict):
                        # Recursively process each list item
                        enhanced_item = self._extract_geometry_from_assessment(
                            item_assessment
                        )
                        enhanced_list.append(enhanced_item)
                    else:
                        # Non-dict items pass through unchanged
                        enhanced_list.append(item_assessment)
                enhanced_assessment[attr_name] = enhanced_list
            else:
                # Other types pass through unchanged
                enhanced_assessment[attr_name] = attr_assessment

        return enhanced_assessment

    def assess_results(
        self,
        *,
        class_label: str,
        extraction_results: Dict[str, Any],
        document_text: str,
        page_images: List[Any],
        ocr_text_confidence: str = "",
        model_id_override: Optional[str] = None,
    ) -> AssessmentCoreResult:
        """Run the pure assessment inference over in-memory inputs.

        This is the S3-free, Document-free core shared by both
        ``process_document_section`` (the standalone Assessment step) and the
        agentic in-shard assessment path. Given a class label, the extracted
        values, and the page text/images/OCR-confidence for the scope being
        assessed, it builds the assessment prompt, invokes Bedrock, parses the
        response, and enhances every field with its confidence threshold and
        alerts — returning the result in memory.

        Geometry grounding is NOT applied here; the caller grounds against the
        page-data scope it owns (see ``AssessmentCoreResult``).

        Args:
            class_label: Document class for schema/threshold lookup.
            extraction_results: The ``inference_result`` dict to assess.
            document_text: Concatenated OCR text for the assessed pages.
            page_images: Prepared page images (may be empty).
            ocr_text_confidence: Optional condensed OCR text-confidence block.
            model_id_override: When set, use this Bedrock model instead of the
                configured confidence model. Used by the self-healing escalation
                ladder to re-assess still-missing rows with a stronger model
                (bigger output cap) WITHOUT mutating config.

        Returns:
            An ``AssessmentCoreResult`` with the enhanced per-field assessment,
            confidence-threshold alerts, metering, parse-success flag, and the
            model invocation duration.
        """
        if not extraction_results:
            logger.warning("assess_results called with empty extraction_results")
            return AssessmentCoreResult()

        # Get confidence configuration (v0.6: extraction.confidence.*)
        confidence_cfg = self.config.extraction.confidence
        model_id = model_id_override or confidence_cfg.model
        temperature = confidence_cfg.temperature
        top_k = confidence_cfg.top_k
        top_p = confidence_cfg.top_p
        reasoning_effort = confidence_cfg.reasoning_effort
        # max_tokens is no longer a config knob — None lets the Bedrock client
        # resolve the confidence model's maximum output (model_config_limits.yaml).
        max_tokens = None
        system_prompt = confidence_cfg.system_prompt

        # Get schema for this document class
        class_schema = self._get_class_schema(class_label)
        if not class_schema:
            raise ValueError(f"No schema found for document class: {class_label}")

        property_descriptions = self._format_property_descriptions(class_schema)

        # Select the CONFIDENCE-ONLY task prompt (extraction.task_prompt_confidence)
        # and compose the editable bbox block (extraction.task_prompt_bbox) ONLY for
        # the LLM-box geometry modes (llm, llm_grounded). In ocr_only (default) / off
        # the model is never asked for boxes — geometry comes from OCR
        # value-matching — so those directions are omitted, saving output tokens and
        # freeing long-list enumeration.
        from idp_common.extraction.prompt_assembly import (
            select_confidence_task_prompt,
        )

        prompt_template = select_confidence_task_prompt(self.config.extraction)
        extraction_results_str = json.dumps(extraction_results, indent=2)

        if not prompt_template:
            raise ValueError(
                "Assessment task_prompt is required in configuration but not found"
            )

        # The prompt decides whether images are sent: they are attached whenever
        # the active confidence template contains {DOCUMENT_IMAGE} (see
        # ``_build_content_with_or_without_image_placeholder``), and omitted when it
        # does not. Images were previously dropped whenever geometry.mode was
        # ``ocr_only``/``off`` on the theory that a confidence pass not asked for
        # bounding boxes has no use for the page image. That is wrong for any field
        # whose evidence is visual rather than textual — a signature/checkbox/stamp
        # boolean, a struck-through or handwritten value — where the image IS the
        # only way to check the extraction. It also silently contradicted the
        # configured prompt: the shipped template asks for {DOCUMENT_IMAGE}, so the
        # request went out with an empty <document-image> block. Callers that want
        # a cheaper text-only confidence pass remove the placeholder from the
        # template instead.
        try:
            content = self._build_content_with_or_without_image_placeholder(
                prompt_template,
                document_text,
                class_label,
                property_descriptions,
                extraction_results_str,
                ocr_text_confidence,
                page_images,
            )
        except ValueError as e:
            logger.error(f"Error formatting prompt template: {str(e)}")
            raise ValueError(f"Assessment prompt template formatting failed: {str(e)}")

        # Time the model invocation
        request_start_time = time.time()

        response_with_metering = bedrock.invoke_model(
            model_id=model_id,
            system_prompt=system_prompt,
            content=content,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            max_tokens=max_tokens,
            context="Assessment",
            model_lambda_hook_arn=confidence_cfg.model_lambda_hook_arn,
            reasoning_effort=reasoning_effort,
        )

        total_duration = time.time() - request_start_time
        logger.info(f"Time taken for assessment: {total_duration:.2f} seconds")

        assessment_text = bedrock.extract_text_from_response(response_with_metering)
        metering = response_with_metering.get("metering", {})

        # Did the model hit its max-output-tokens ceiling? A truncated response
        # is almost always unparseable JSON; detecting it lets the batcher retry
        # over a smaller row slice instead of silently falling back to a
        # meaningless default 0.5 for every field. Recognize both the Bedrock
        # Converse signal (``stopReason == "max_tokens"``) and the OpenAI
        # Responses adapter's mapped ``status`` (``incomplete`` /
        # ``max_output_tokens``) so the guard holds across model families.
        raw_response = response_with_metering.get("response", response_with_metering)
        stop_reason = (
            raw_response.get("stopReason") if isinstance(raw_response, dict) else None
        )
        truncated = stop_reason in ("max_tokens", "max_output_tokens", "incomplete")
        if truncated:
            logger.warning(
                "Assessment response truncated at max output tokens "
                "(stopReason=max_tokens) — %d field(s) in this call will be "
                "retried over smaller batches by the caller.",
                len(extraction_results),
            )

        # Parse response into JSON
        assessment_data: Dict[str, Any] = {}
        parsing_succeeded = True

        try:
            assessment_data = json.loads(extract_json_from_text(assessment_text))

            # Handle case where LLM returns a single-element array instead of dict
            if isinstance(assessment_data, list):
                if len(assessment_data) == 1:
                    logger.warning(
                        "LLM returned single-element array instead of object, unwrapping",
                        extra={"original_type": "list", "element_count": 1},
                    )
                    assessment_data = assessment_data[0]
                elif len(assessment_data) == 0:
                    raise ValueError("Received empty array instead of single object")
                else:
                    raise ValueError(
                        f"Received array with {len(assessment_data)} elements "
                        "instead of single object"
                    )
        except Exception as e:
            # B4 — Salvage the VALIDATED PREFIX of a truncated response instead of
            # discarding the whole call. A truncated confidence body is well-formed
            # JSON up to the last complete row object; repair_truncated_json recovers
            # those rows and drops the partial tail. We keep only what parses into a
            # dict — the recovered rows carry real scores, and the caller re-scores
            # ONLY the still-missing remainder (see _splice_missing_rows), so the
            # ~10K output tokens the model already spent are not wasted. This is
            # safe even if the tail was run-away garbage: repair yields only the
            # clean prefix, and anything unrecovered stays a null placeholder that
            # the missing-row retry targets.
            salvaged: Dict[str, Any] = {}
            if truncated:
                try:
                    repaired, repair_info = repair_truncated_json(assessment_text)
                    if isinstance(repaired, dict) and repaired:
                        salvaged = repaired
                        logger.warning(
                            "Assessment output TRUNCATED at max output tokens; "
                            "salvaged %d top-level field(s) from the valid prefix "
                            "(method=%s). Unrecovered rows will be retried over a "
                            "smaller batch.",
                            repair_info.get("fields_recovered", len(repaired)),
                            repair_info.get("repair_method"),
                        )
                except Exception as re:  # noqa: BLE001 - salvage is best-effort
                    logger.warning("Truncated-response salvage failed: %s", re)
                if not salvaged:
                    logger.error(
                        "Assessment output was TRUNCATED at the model's max output "
                        "tokens and nothing could be salvaged. Assigning temporary "
                        "default scores; the caller retries over a smaller batch. "
                        "Error: %s",
                        e,
                    )
            else:
                logger.error(
                    f"Error parsing assessment LLM output - invalid JSON?: "
                    f"{assessment_text} - {e}"
                )

            if salvaged:
                # Keep the recovered rows; reconciliation downstream pads the
                # still-missing rows with null placeholders that the missing-row
                # retry (or escalation) then re-scores.
                assessment_data = salvaged
                # parsing_succeeded stays True: we DID parse a usable partial
                # result. ``truncated`` (already set) is what signals the caller to
                # retry the remainder — decoupled from parse success.
            else:
                logger.info("Using default confidence scores.")
                assessment_data = {}
                for attr_name in extraction_results.keys():
                    assessment_data[attr_name] = {
                        "confidence": 0.5,
                        "confidence_reason": "Unable to parse assessment response - default score assigned",
                    }
                parsing_succeeded = False

        # Convert any model-provided bbox into geometry — only in the LLM-box
        # geometry modes ('llm', 'llm_grounded'). In 'ocr_only' (default) and 'off'
        # we intentionally ignore model boxes (geometry comes solely from OCR
        # value-matching during grounding, or not at all), keeping hallucinated LLM
        # coordinates out of the result.
        if self.config.extraction.geometry.mode in ("llm", "llm_grounded"):
            try:
                assessment_data = self._extract_geometry_from_assessment(
                    assessment_data
                )
            except Exception as e:
                logger.warning(f"Failed to extract geometry data: {str(e)}")

        default_confidence_threshold = self.config.hitl.confidence_threshold

        enhanced_assessment_data: Dict[str, Any] = {}
        confidence_threshold_alerts: List[Dict[str, Any]] = []
        properties = class_schema.get(SCHEMA_PROPERTIES, {})

        for attr_name, attr_assessment in assessment_data.items():
            prop_schema = properties.get(attr_name, {})
            # NOTE: deliberately read off the RAW property, not the dereferenced
            # one. Honoring a threshold declared on the ``$defs`` definition
            # rather than the property is a change to threshold *inheritance*
            # semantics, which belongs with threshold_resolver's rules — not
            # here. See config/schema_utils.py.
            attr_threshold = _safe_float_conversion(
                prop_schema.get(
                    X_AWS_IDP_CONFIDENCE_THRESHOLD, default_confidence_threshold
                ),
                default_confidence_threshold,
            )

            # The type MUST be read off the dereferenced subschema: a property
            # declared as ``{"$ref": "#/$defs/TxnList"}`` carries no ``type``, so
            # the raw read defaulted an array to TYPE_STRING -> attr_type
            # "simple", and the list assessment below was collapsed to a single
            # default 0.5 leaf that reconciliation padded to N null placeholders
            # no model could ever fill.
            deref_prop_schema = deref_schema(prop_schema, class_schema)
            prop_type_json = deref_prop_schema.get(SCHEMA_TYPE, TYPE_STRING)
            if prop_type_json == TYPE_OBJECT:
                attr_type = "group"
            elif prop_type_json == TYPE_ARRAY:
                attr_type = "list"
            else:
                attr_type = "simple"

            if isinstance(attr_assessment, dict):
                enhanced_assessment_data[attr_name] = self._enhance_dict_assessment(
                    attr_assessment, attr_threshold
                )
                self._check_confidence_alerts(
                    attr_assessment,
                    attr_name,
                    attr_threshold,
                    confidence_threshold_alerts,
                )
            elif isinstance(attr_assessment, list):
                if attr_type == "list":
                    # Resolve per-sub-field thresholds from $ref/$defs for array items
                    from idp_common.assessment.threshold_resolver import (
                        resolve_array_item_thresholds,
                    )

                    # Dereferenced: a ``$ref``-wrapped array property has no
                    # ``items`` of its own, so the raw schema yielded {} and every
                    # sub-field silently fell back to the uniform container
                    # threshold. Reachable only now that such a property is
                    # correctly typed "list" above.
                    item_thresholds = resolve_array_item_thresholds(
                        deref_prop_schema, class_schema, attr_threshold
                    )

                    enhanced_list = []
                    for i, item_assessment in enumerate(attr_assessment):
                        if isinstance(item_assessment, dict):
                            enhanced_item = (
                                self._enhance_dict_assessment_with_field_thresholds(
                                    item_assessment, attr_threshold, item_thresholds
                                )
                            )
                            enhanced_list.append(enhanced_item)
                            self._check_confidence_alerts_with_field_thresholds(
                                item_assessment,
                                f"{attr_name}[{i}]",
                                attr_threshold,
                                item_thresholds,
                                confidence_threshold_alerts,
                            )
                        else:
                            logger.warning(
                                f"List item {i} in attribute '{attr_name}' is not a "
                                f"dictionary. Expected dict, got {type(item_assessment)}. "
                                "Using default confidence."
                            )
                            default_item = {
                                "confidence": 0.5,
                                "confidence_reason": f"List item {i} in '{attr_name}' has unexpected format. Using default confidence.",
                                "confidence_threshold": attr_threshold,
                            }
                            enhanced_list.append(default_item)
                            if 0.5 < attr_threshold:
                                confidence_threshold_alerts.append(
                                    {
                                        "attribute_name": f"{attr_name}[{i}]",
                                        "confidence": 0.5,
                                        "confidence_threshold": attr_threshold,
                                    }
                                )
                    enhanced_assessment_data[attr_name] = enhanced_list
                else:
                    logger.warning(
                        f"Attribute '{attr_name}' (type: {attr_type}) assessment is a "
                        "list but attribute is not configured as list type. Using "
                        "default confidence."
                    )
                    enhanced_assessment_data[attr_name] = {
                        "confidence": 0.5,
                        "confidence_reason": f"LLM returned list format for non-list attribute '{attr_name}'. Using default confidence (0.5) and threshold ({attr_threshold}).",
                        "confidence_threshold": attr_threshold,
                    }
            else:
                logger.warning(
                    f"Attribute '{attr_name}' assessment is of unexpected type "
                    f"{type(attr_assessment)}. Expected dictionary or list (for list "
                    "attributes). Using default confidence."
                )
                enhanced_assessment_data[attr_name] = {
                    "confidence": 0.5,
                    "confidence_reason": f"LLM returned unexpected type {type(attr_assessment)} for attribute '{attr_name}'. Using default confidence (0.5) and threshold ({attr_threshold}).",
                    "confidence_threshold": attr_threshold,
                }

        return AssessmentCoreResult(
            enhanced_assessment=enhanced_assessment_data,
            confidence_threshold_alerts=confidence_threshold_alerts,
            metering=metering or {},
            parsing_succeeded=parsing_succeeded,
            duration_seconds=total_duration,
            truncated=truncated,
        )

    def process_document_section(
        self,
        document: Document,
        section_id: str,
        deadline_epoch: Optional[float] = None,
    ) -> Document:
        """
        Process a single section from a Document object to assess extraction confidence.

        Args:
            document: Document object containing section to process
            section_id: ID of the section to process

        Returns:
            Document: Updated Document object with assessment results appended to extraction results
        """
        # Check if confidence assessment is enabled (v0.6: extraction.confidence)
        enabled = self.config.extraction.confidence.enabled
        if not enabled:
            logger.info("Assessment is disabled via configuration")
            return document

        # Validate input document
        if not document:
            logger.error("No document provided")
            return document

        if not document.sections:
            logger.error("Document has no sections to process")
            document.errors.append("Document has no sections to process")
            return document

        # Find the section with the given ID
        section = None
        for s in document.sections:
            if s.section_id == section_id:
                section = s
                break

        if not section:
            error_msg = f"Section {section_id} not found in document"
            logger.error(error_msg)
            document.errors.append(error_msg)
            return document

        # Short-circuit: skip sections whose class is marked as excluded
        # (e.g., static instruction pages). No extraction ran, so no
        # assessment is needed or meaningful.
        from idp_common.section_exclusion import is_section_excluded

        if is_section_excluded(section):
            logger.info(
                "Assessment skipped for excluded section %s (class=%s, reason=%s)",
                section.section_id,
                section.classification,
                section.exclusion_reason or "excluded",
            )
            return document

        # Check if section has extraction results to assess
        if not section.extraction_result_uri:
            error_msg = f"Section {section_id} has no extraction results to assess"
            logger.error(error_msg)
            document.errors.append(error_msg)
            return document

        # Extract information about the section
        class_label = section.classification

        # Check if the section has required pages
        if not section.page_ids:
            error_msg = f"Section {section_id} has no page IDs"
            logger.error(error_msg)
            document.errors.append(error_msg)
            return document

        # Sort pages by page number
        sorted_page_ids = sorted(section.page_ids, key=int)
        start_page = int(sorted_page_ids[0])
        end_page = int(sorted_page_ids[-1])
        logger.info(
            f"Assessing {len(sorted_page_ids)} pages, class {class_label}: {start_page}-{end_page}"
        )

        # Track metrics
        metrics.put_metric("InputDocumentsForAssessment", 1)
        metrics.put_metric("InputDocumentPagesForAssessment", len(section.page_ids))

        try:
            # Read existing extraction results
            t0 = time.time()
            extraction_data = s3.get_json_content(section.extraction_result_uri)
            extraction_results = extraction_data.get("inference_result", {})

            # Skip assessment if no extraction results found
            if not extraction_results:
                logger.warning(f"No extraction results found for section {section_id}")
                return document

            t1 = time.time()
            logger.info(f"Time taken to read extraction results: {t1 - t0:.2f} seconds")

            # Read document text from all pages in order
            document_texts = []
            for page_id in sorted_page_ids:
                if page_id not in document.pages:
                    error_msg = f"Page {page_id} not found in document"
                    logger.error(error_msg)
                    document.errors.append(error_msg)
                    continue

                page = document.pages[page_id]
                text_path = page.parsed_text_uri
                page_text = s3.get_text_content(text_path)
                document_texts.append(page_text)

            document_text = "\n".join(document_texts)
            t2 = time.time()
            logger.info(f"Time taken to read text content: {t2 - t1:.2f} seconds")

            # Read page images with configurable dimensions (v0.6: confidence.image)
            target_width = self.config.extraction.confidence.image.target_width
            target_height = self.config.extraction.confidence.image.target_height

            page_images = []
            for page_id in sorted_page_ids:
                if page_id not in document.pages:
                    continue

                page = document.pages[page_id]
                image_uri = page.image_uri
                # Just pass the values directly - prepare_image handles empty strings/None
                image_content = image.prepare_image(
                    image_uri, target_width, target_height
                )
                page_images.append(image_content)

            t3 = time.time()
            logger.info(f"Time taken to read images: {t3 - t2:.2f} seconds")

            # Read text confidence data for confidence information
            ocr_text_confidence = ""
            for page_id in sorted_page_ids:
                if page_id not in document.pages:
                    continue

                page = document.pages[page_id]
                text_confidence_data_str = self._get_text_confidence_data(page)
                if text_confidence_data_str:
                    ocr_text_confidence += (
                        f"\n--- Page {page_id} Text Confidence Data ---\n"
                    )
                    ocr_text_confidence += text_confidence_data_str

            t4 = time.time()
            logger.info(f"Time taken to read raw OCR results: {t4 - t3:.2f} seconds")

            # Run the pure inference + enhancement core (shared with the agentic
            # in-shard assessment path), batching large list fields so the model
            # reliably enumerates every row. A single call over a big list (e.g. a
            # 120-row statement) under-enumerates or omits it, leaving most rows
            # unassessed — this was previously the job of the (now retired) granular
            # service. ``assess_results_batched`` slices the largest oversized list
            # by ``extraction.confidence.list_batch_size``, assesses each chunk
            # sequentially with the shared scalars/context, and reconciles the
            # per-row assessments to full per-cell coverage. Falls back to a single
            # (still reconciled) call when no list exceeds the batch size.
            logger.info(
                f"Assessing extraction confidence for {class_label} document, "
                f"section {section_id}"
            )
            from idp_common.assessment.batching import assess_results_batched

            confidence_cfg = self.config.extraction.confidence
            batched = assess_results_batched(
                self,
                class_label=class_label,
                extraction_results=extraction_results,
                document_text=document_text,
                page_images=page_images,
                batch_size=confidence_cfg.list_batch_size,
                ocr_text_confidence=ocr_text_confidence,
                confidence_model_id=confidence_cfg.model,
                geometry_mode=self.config.extraction.geometry.mode,
                escalation_enabled=confidence_cfg.escalation_enabled,
                escalation_model=self._resolve_confidence_escalation_model(class_label),
                max_escalation_rounds=confidence_cfg.max_escalation_rounds,
                deadline_epoch=deadline_epoch,
                max_concurrent_batches=self.config.extraction.agentic.max_concurrent_batches,
                class_schema=self._get_class_schema(class_label),
            )
            enhanced_assessment_data = batched["assessment"]
            confidence_threshold_alerts = batched["alerts"]
            metering = batched["metering"]
            parsing_succeeded = batched["parsing_succeeded"]
            total_duration = batched["duration_seconds"]
            batched_split_stats = batched.get("split_stats")

            # Ground field geometry from OCR. In 'ocr_only' (default) geometry is
            # derived purely from OCR value-matching (model boxes were never
            # produced); in 'llm_grounded' real OCR boxes refine the model's
            # estimates. Skipped for 'llm' (boxes as-is) and 'off' (no geometry).
            geometry_mode = self.config.extraction.geometry.mode
            if geometry_mode not in ("llm", "off"):
                try:
                    from idp_common.assessment.ocr_grounding import (
                        ground_assessment_geometry,
                        load_page_ocr_data,
                    )

                    page_data_by_page = load_page_ocr_data(
                        document.pages, sorted_page_ids
                    )
                    if page_data_by_page:
                        enhanced_assessment_data = ground_assessment_geometry(
                            enhanced_assessment_data,
                            extraction_results,
                            page_data_by_page,
                            geometry_mode,
                            self._get_class_schema(class_label),
                        )
                except Exception as e:
                    logger.warning(
                        f"OCR geometry grounding failed for section {section_id}; "
                        f"keeping LLM-estimated boxes: {e}"
                    )

            # Update the existing extraction result with enhanced assessment data
            extraction_data["explainability_info"] = [enhanced_assessment_data]
            extraction_data["metadata"] = extraction_data.get("metadata", {})
            extraction_data["metadata"]["assessment_time_seconds"] = total_duration
            extraction_data["metadata"]["assessment_parsing_succeeded"] = (
                parsing_succeeded
            )
            # Surface adaptive batch-splitting activity (only when the model
            # truncated output and batches had to shrink) for visibility.
            from idp_common.assessment.batching import (
                audit_explainability,
                build_assessment_issues,
                split_stats_are_notable,
            )

            if split_stats_are_notable(batched_split_stats):
                extraction_data["metadata"]["assessment_batch_split_stats"] = (
                    batched_split_stats
                )

            # Completeness gate (1.3) + structured issues (1.4): audit the final
            # assessment for coverage/structure, then translate the ladder's
            # split_stats + audit findings into user-surfacing ProcessingIssues.
            _gaps, audit_issues = audit_explainability(
                enhanced_assessment_data,
                extraction_results,
                geometry_mode=geometry_mode,
                section_id=section_id,
            )
            processing_issues = (
                build_assessment_issues(
                    batched_split_stats,
                    section_id=section_id,
                    confidence_model=confidence_cfg.model,
                    geometry_mode=geometry_mode,
                )
                + audit_issues
            )
            # MERGE, do not replace. Extraction already wrote its own issues here
            # (extraction_incomplete, extraction_validation_failed, ...); this step
            # owns only the assessment-stage ones. Replacing the list dropped
            # everything extraction had reported — which mattered most for the
            # on-by-default validation issue, because `separate` (the recommended
            # confidence mode) is exactly the mode where this step runs.
            _inherited = [
                pi
                for pi in (
                    extraction_data.get("metadata", {}).get("processing_issues") or []
                )
                if isinstance(pi, dict) and pi.get("stage") != "assessment"
            ]
            _merged = _inherited + [pi.to_dict() for pi in processing_issues]
            if _merged:
                extraction_data["metadata"]["processing_issues"] = _merged
                # Append a Processing Issues block to the (extraction-generated)
                # processing report so the human-readable report on the simple/
                # separate path also surfaces the root cause — the extraction
                # report was written before this assessment step ran, so it
                # otherwise wouldn't mention assessment issues.
                existing_report = extraction_data.get("processing_report", "")
                icons = {"error": "✗", "warning": "⚠", "info": "ℹ"}
                issue_lines = ["", "Processing Issues (assessment):"]
                for pi in processing_issues:
                    issue_lines.append(
                        f"  {icons.get(pi.severity, '•')} [{pi.code}] {pi.message}"
                    )
                    if pi.root_cause:
                        issue_lines.append(f"      Root cause: {pi.root_cause}")
                extraction_data["processing_report"] = (
                    existing_report + "\n" + "\n".join(issue_lines)
                ).strip()

            # Write the updated result back to S3
            bucket, key = utils.parse_s3_uri(section.extraction_result_uri)
            s3.write_content(
                extraction_data, bucket, key, content_type="application/json"
            )

            # Update the section in the document with confidence threshold alerts
            # and any structured processing issues.
            for doc_section in document.sections:
                if doc_section.section_id == section_id:
                    doc_section.confidence_threshold_alerts = (
                        confidence_threshold_alerts
                    )
                    # Replace only the assessment-stage issues, keep the rest.
                    # The section write that follows in the assessment Lambda
                    # REPLACES the whole section map, so an unconditional
                    # `= processing_issues` did not merely skip extraction's
                    # issues — it deleted them from DynamoDB. Verified live: with
                    # `confidence.mode: separate` a section whose extraction had
                    # raised extraction_validation_failed came back with
                    # ProcessingIssues absent, while the same run under
                    # `integrated` (no standalone assessment step) kept it.
                    doc_section.processing_issues = [
                        pi
                        for pi in (doc_section.processing_issues or [])
                        if getattr(pi, "stage", None) != "assessment"
                    ] + processing_issues
                    break

            # Update document with metering data
            document.metering = utils.merge_metering_data(
                document.metering, metering or {}
            )

            t5 = time.time()
            logger.info(
                f"Total assessment time for section {section_id}: {t5 - t0:.2f} seconds"
            )

        except Exception as e:
            error_msg = (
                f"Error processing assessment for section {section_id}: {str(e)}"
            )
            logger.error(error_msg)
            document.errors.append(error_msg)
            raise

        return document

    def assess_document(self, document: Document) -> Document:
        """
        Assess extraction confidence for all sections in a document.

        Args:
            document: Document object with extraction results

        Returns:
            Document: Updated Document object with assessment results
        """
        logger.info(f"Starting assessment for document {document.id}")

        for section in document.sections:
            if section.extraction_result_uri:
                logger.info(f"Assessing section {section.section_id}")
                document = self.process_document_section(document, section.section_id)
            else:
                logger.warning(
                    f"Section {section.section_id} has no extraction results to assess"
                )

        logger.info(f"Completed assessment for document {document.id}")
        return document
