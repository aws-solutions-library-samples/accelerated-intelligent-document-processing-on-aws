# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Extraction service for documents using LLMs.

This module provides a service for extracting fields and values from documents
using LLMs, with support for text and image content.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

from idp_common import bedrock, image, metrics, s3, utils
from idp_common.bedrock import format_prompt, is_openai_responses_model
from idp_common.config.models import IDPConfig
from idp_common.config.schema_constants import (
    ID_FIELD,
    SCHEMA_ITEMS,
    SCHEMA_PROPERTIES,
    SCHEMA_TYPE,
    TYPE_ARRAY,
    TYPE_OBJECT,
    X_AWS_IDP_DOCUMENT_TYPE,
    X_AWS_IDP_EXTRACTION_ESCALATION_MODEL,
    X_AWS_IDP_EXTRACTION_MODEL,
    X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT,
    X_AWS_IDP_EXTRACTION_TASK_PROMPT,
    X_AWS_IDP_INSTANCE_ARRAY,
    X_AWS_IDP_SOURCE_PAGE_TYPES,
)
from idp_common.extraction.page_type_resolver import (
    PageTypePresence,
    resolve_page_types,
)
from idp_common.extraction.sharding import (
    DEFAULT_MAX_PAGES_PER_SHARD,
    DEFAULT_SHARD_TOKEN_BUDGET,
    estimate_tokens,
    plan_shards,
)
from idp_common.extraction.topk_resolver import (
    is_topk_response,
    resolve_candidates,
)
from idp_common.extraction.validation import (
    ValidationReport,
    build_empty_list_feedback,
    build_subset_schema,
    find_empty_declared_lists,
    validate_extraction,
)
from idp_common.models import Document, Section
from idp_common.utils.few_shot_example_builder import (
    build_few_shot_extraction_examples_content,
)

# Conditional import for agentic extraction (requires Python 3.12+ dependencies)
try:
    from idp_common.extraction.agentic_idp import (
        concurrent_structured_output_async,
        set_confidence_data,
        structured_output,
    )
    from idp_common.schema import create_pydantic_model_from_json_schema

    AGENTIC_AVAILABLE = True
except ImportError:
    AGENTIC_AVAILABLE = False
from pydantic import BaseModel

from idp_common.utils import extract_json_from_text, repair_truncated_json

logger = logging.getLogger(__name__)


# Pydantic models for internal data transfer
class SectionInfo(BaseModel):
    """Metadata about a document section being processed."""

    model_config = {"arbitrary_types_allowed": True}

    class_label: str
    sorted_page_ids: list[str]
    page_indices: list[int]
    output_bucket: str
    output_key: str
    output_uri: str
    start_page: int
    end_page: int
    page_type_presence: PageTypePresence | None = None


class ExtractionConfig(BaseModel):
    """Configuration for model invocation."""

    model_id: str
    temperature: float
    top_k: float
    top_p: float
    max_tokens: int | None
    system_prompt: str


class ExtractionResult(BaseModel):
    """Result from model extraction."""

    extracted_fields: dict[str, Any]
    metering: dict[str, Any]
    parsing_succeeded: bool
    total_duration: float
    output_truncated: bool = False
    output_repaired: bool = False
    repair_method: str | None = None
    schema_analysis: dict[str, Any] | None = None
    ocr_analysis: dict[str, Any] | None = None
    instance_count: int = 0
    """Number of documents (instances) of the section's class found. 0 =
    undetermined, 1 = normal, >1 = the section holds several distinct
    documents that classification did not split apart."""
    recovered_instances: list[dict[str, Any]] | None = None
    """Every instance parsed from a multi-instance response, in document order.
    Set only when ``instance_count > 1``. ``extracted_fields`` holds the FIRST
    instance (so the output shape is unchanged for every downstream consumer);
    this carries the rest so nothing the model produced is thrown away."""
    instance_probe: int | None = None
    """The model's own answer to "how many separate documents of this class are
    in these pages" (#753), asked in the same inference and stripped from
    ``extracted_fields``. None when detection is off, unsupported on this path,
    or the model did not answer. Greater than the number of records actually in
    the result means records were silently lost — see
    ``extraction_multi_instance_suspected``."""


class ExtractionService:
    """Service for extracting fields from documents using LLMs."""

    def __init__(
        self,
        region: str | None = None,
        config: dict[str, Any] | IDPConfig | None = None,
    ):
        """
        Initialize the extraction service.

        Args:
            region: AWS region for Bedrock
            config: Configuration dictionary or IDPConfig model
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

        # Instance variables for prompt context
        # These are initialized here and populated during each process_document_section call
        # This allows methods to access context without passing multiple parameters
        self._document_text: str = ""
        self._class_label: str = ""
        self._attribute_descriptions: str = ""
        self._class_schema: dict[str, Any] = {}
        # The class schema as sent to the model (prompt text / forced toolSpec).
        # Identical to _class_schema except for the multi-instance detection
        # probe property (#753), which must never reach the off-schema filter,
        # the validator, or any downstream stage.
        self._wire_class_schema: dict[str, Any] = {}
        self._instance_probe_requested: bool = False
        self._page_images: list[bytes] = []
        self._image_uris: list[str] = []
        # Per-page OCR text in section order, populated per section. Used to
        # shard the input by page range for concurrent agentic extraction.
        self._page_texts: list[str] = []
        # Optional checkpoint callback for incremental saves during agentic extraction.
        # When set, called after each successful extraction_tool or apply_json_patches
        # invocation with the current extraction dict, enabling resume on Lambda timeout.
        self._checkpoint_callback: Any | None = None
        # Optional per-shard persistence backend (idp_common.extraction.runtime.
        # ShardPersistence). When set (e.g. an S3ShardPersistence wired by the
        # extraction Lambda), the concurrent/sharded path persists each shard's
        # result to a deterministic key and skips shards whose complete result is
        # already present — so an SFN retry of a timed-out section re-runs ONLY
        # the incomplete shards. None => in-memory only (standalone/notebook).
        self._shard_persistence: Any | None = None
        # Validation outcome from the most recent _invoke_extraction_model call,
        # consumed by _save_results when building the metadata block. Reset per
        # section so a prior section's result can never leak into the next.
        self._pending_validation_metadata: dict[str, Any] | None = None
        self._pending_forced_tool_metadata: dict[str, Any] | None = None
        # Deterministic type/format repairs applied to the most recent section's
        # simple-mode result, so nothing is silently rewritten. Reset per section.
        self._pending_coercion_metadata: dict[str, Any] | None = None
        # Model actually used for the most recent section's extraction (after
        # per-class override resolution), recorded in metadata for audit. Reset
        # per section.
        self._pending_extraction_model: str | None = None
        # Grounded per-field assessment staged by _attach_explainability for the
        # current section, emitted as explainability_info by _save_results.
        # Reset per section. None when in-shard assessment did not run.
        self._grounded_assessment: dict[str, Any] | None = None
        # Wall-clock deadline (epoch seconds) for the in-shard assessment ladder
        # (1.5). Set per-invocation from the Lambda context; None = no guard.
        self._assessment_deadline_epoch: float | None = None
        # Memoized model-aware SizingPlan for the current section (item 2).
        self._sizing_plan: Any = None
        # The Document for the current section, stashed by _prepare_section_context
        # so per-shard grounding can load each shard's OCR pageData without
        # threading `document` through _invoke_extraction_model. Reset per section.
        self._document: Document | None = None
        # Agent's optional self-reported table-tool rationale (a "TABLE_TOOL_NOTE:"
        # line) captured from its final message; consumed by _save_results. Reset
        # per section so a prior section's note never leaks.
        self._agent_table_tool_note: str | None = None

        # Get model_id from config for logging (type-safe access with fallback)
        model_id = (
            self.config.extraction.model if self.config.extraction else "not configured"
        )
        logger.info(f"Initialized extraction service with model {model_id}")

    @property
    def _substitutions(self) -> dict[str, str]:
        """Get prompt placeholder substitutions from stored context."""
        return {
            "DOCUMENT_TEXT": self._document_text,
            "DOCUMENT_CLASS": self._class_label,
            "ATTRIBUTE_NAMES_AND_DESCRIPTIONS": self._attribute_descriptions,
        }

    def _get_default_prompt_content(self) -> list[dict[str, Any]]:
        """
        Build default fallback prompt content when no template is provided.

        Returns:
            List of content items with default prompt text and images
        """
        task_prompt = f"""
        Extract the following fields from this {self._class_label} document:
        
        {self._attribute_descriptions}
        
        Document text:
        {self._document_text}
        
        Respond with a JSON object containing each field name and its extracted value.
        """
        content = [{"text": task_prompt}]

        # Add image attachments to the content - no limit with latest Bedrock API
        if self._page_images:
            logger.info(
                f"Attaching {len(self._page_images)} images to default extraction prompt"
            )
            for img in self._page_images:
                content.append(image.prepare_bedrock_image_attachment(img))

        return content

    def _get_class_schema(self, class_label: str) -> dict[str, Any]:
        """
        Get the EFFECTIVE JSON Schema for a document class from configuration.

        "Effective" because a class flagged ``x-aws-idp-multi-instance: true``
        (Synthesize mode, GitHub #715) has its schema replaced by an
        ``instances[]`` List-of-Class wrapper here, at the single point every
        other part of this service reads the schema from. Wrapping here rather
        than at each call site is what makes the prompt, the generated Pydantic
        model, the JSON-Schema validator, the off-schema filter, the completeness
        check and the instance-count resolver all inherit the transform without
        knowing about it. The transform is a no-op (returning the same object)
        for every unflagged class, which is all of them by default.

        Args:
            class_label: The document class name

        Returns:
            JSON Schema for the class, or empty dict if not found
        """
        from idp_common.schema.multi_instance import wrap_class_schema

        # Access classes through IDPConfig - returns List of dicts
        classes_config = self.config.classes

        # Find class by $id or x-aws-idp-document-type using constants
        for class_obj in classes_config:
            class_id = class_obj.get(ID_FIELD, "") or class_obj.get(
                X_AWS_IDP_DOCUMENT_TYPE, ""
            )
            if class_id.lower() == class_label.lower():
                return wrap_class_schema(class_obj)

        return {}

    def _clean_schema_for_prompt(self, schema: dict[str, Any]) -> dict[str, Any]:
        """
        Clean JSON Schema by removing IDP custom fields (x-aws-idp-*) for the prompt.
        Keeps all standard JSON Schema fields including descriptions.

        Args:
            schema: JSON Schema definition

        Returns:
            Cleaned JSON Schema
        """
        cleaned = {}

        for key, value in schema.items():
            # Skip IDP custom fields
            if key.startswith("x-aws-idp-"):
                continue

            # Recursively clean nested objects and arrays
            if isinstance(value, dict):
                cleaned[key] = self._clean_schema_for_prompt(value)
            elif isinstance(value, list):
                cleaned[key] = [
                    (
                        self._clean_schema_for_prompt(item)
                        if isinstance(item, dict)
                        else item
                    )
                    for item in value
                ]
            else:
                cleaned[key] = value

        return cleaned

    def _format_schema_for_prompt(self, schema: dict[str, Any]) -> str:
        """
        Format JSON Schema for inclusion in the extraction prompt.

        Args:
            schema: JSON Schema definition

        Returns:
            Formatted JSON Schema as a string with IDP custom fields removed
        """
        # Clean the schema to remove IDP custom fields
        cleaned_schema = self._clean_schema_for_prompt(schema)

        # Return the cleaned JSON Schema with nice formatting
        return json.dumps(cleaned_schema, indent=2)

    def _prepare_prompt_from_template(
        self,
        prompt_template: str,
        substitutions: dict[str, str],
        required_placeholders: list[str] | None = None,
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

        return format_prompt(prompt_template, substitutions, required_placeholders)

    def _build_prompt_content(
        self,
        prompt_template: str,
        image_content: Any = None,
    ) -> list[dict[str, Any]]:
        """
        Build prompt content array handling FEW_SHOT_EXAMPLES and DOCUMENT_IMAGE placeholders.

        This consolidated method handles all placeholder types and combinations:
        - {FEW_SHOT_EXAMPLES}: Inserts few-shot examples from config
        - {DOCUMENT_IMAGE}: Inserts images at specific location
        - Regular text placeholders: DOCUMENT_TEXT, DOCUMENT_CLASS, etc.

        Args:
            prompt_template: The prompt template with optional placeholders
            image_content: Optional image content to insert (only used with {DOCUMENT_IMAGE})

        Returns:
            List of content items with text and image content properly ordered
        """
        content: list[dict[str, Any]] = []

        # Handle FEW_SHOT_EXAMPLES placeholder first
        if "{FEW_SHOT_EXAMPLES}" in prompt_template:
            parts = prompt_template.split("{FEW_SHOT_EXAMPLES}")
            if len(parts) == 2:
                # Process before examples
                content.extend(
                    self._build_text_and_image_content(parts[0], image_content)
                )

                # Add few-shot examples
                content.extend(self._build_few_shot_examples_content())

                # Process after examples (only pass images if not already used)
                image_for_after = (
                    None if "{DOCUMENT_IMAGE}" in parts[0] else image_content
                )
                content.extend(
                    self._build_text_and_image_content(parts[1], image_for_after)
                )

                return content

        # No FEW_SHOT_EXAMPLES, just handle text and images
        return self._build_text_and_image_content(prompt_template, image_content)

    def _build_text_and_image_content(
        self,
        prompt_template: str,
        image_content: Any = None,
    ) -> list[dict[str, Any]]:
        """
        Build content array with text and optionally images based on DOCUMENT_IMAGE placeholder.

        Args:
            prompt_template: Template that may contain {DOCUMENT_IMAGE}
            image_content: Optional image content

        Returns:
            List of content items
        """
        content: list[dict[str, Any]] = []

        # Handle DOCUMENT_IMAGE placeholder
        if "{DOCUMENT_IMAGE}" in prompt_template:
            parts = prompt_template.split("{DOCUMENT_IMAGE}")
            if len(parts) == 2:
                # Add text before image
                before_text = self._prepare_prompt_from_template(
                    parts[0], self._substitutions, required_placeholders=[]
                )
                if before_text.strip():
                    content.append({"text": before_text})

                # Add images
                if image_content:
                    content.extend(self._prepare_image_attachments(image_content))

                # Add text after image
                after_text = self._prepare_prompt_from_template(
                    parts[1], self._substitutions, required_placeholders=[]
                )
                if after_text.strip():
                    content.append({"text": after_text})

                return content
            else:
                logger.warning("Invalid DOCUMENT_IMAGE placeholder usage")

        # No image placeholder, just text
        task_prompt = self._prepare_prompt_from_template(
            prompt_template, self._substitutions, required_placeholders=[]
        )
        content.append({"text": task_prompt})

        return content

    def _prepare_image_attachments(self, image_content: Any) -> list[dict[str, Any]]:
        """
        Prepare image attachments for Bedrock - no image limit.

        Args:
            image_content: Single image or list of images

        Returns:
            List of image attachment dicts
        """
        attachments: list[dict[str, Any]] = []

        if isinstance(image_content, list):
            # Multiple images - no limit with latest Bedrock API
            logger.info(f"Attaching {len(image_content)} images to extraction prompt")
            for img in image_content:
                attachments.append(image.prepare_bedrock_image_attachment(img))
        else:
            # Single image
            attachments.append(image.prepare_bedrock_image_attachment(image_content))

        return attachments

    def _build_shard_payloads(
        self,
        prompt_template: str,
        send_images: bool,
        max_shards: int,
        table_boundary_pages: frozenset[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Split the section into token-budgeted page shards and render each
        shard's prompt content independently.

        Each shard's content is built with the SAME prompt path as the single
        pass (``_build_prompt_content``) by temporarily scoping
        ``self._document_text`` and ``self._page_images`` to the shard's page
        slice — so few-shot / {DOCUMENT_IMAGE} / placeholder handling is
        identical. The section's first-page text is prepended as shared header
        context to every shard but the first, so column headers and page-1
        scalar context survive the split.

        Returns a list of payload dicts: ``{"content", "page_start", "page_end",
        "total_pages"}``. Falls back to a single whole-section payload when
        per-page text isn't available.
        """
        page_texts = self._page_texts
        num_pages = len(page_texts)
        if num_pages <= 1:
            # Nothing to shard; let the caller use the single-pass path.
            return []

        # Header context = the first page's *header region* (title / account
        # context / table COLUMN HEADERS), prepended to later shards so they
        # retain column meaning. We deliberately DROP page-1 data rows here: the
        # full page 1 is already in shard 0, and re-including its data rows in
        # later shards makes the deterministic table parser re-emit them ->
        # duplicate rows after merge. Truncate at the Markdown table separator
        # (the `|---|` line) so only the header survives; fall back to a small
        # line cap for non-table documents.
        header_context = self._table_header_context(page_texts[0] if page_texts else "")

        # Reserve the header-context tokens from the per-shard budget so the
        # *rendered* shard (own pages + prepended header) still respects the
        # configured budget — otherwise a shard packed to the budget would
        # exceed it once the header is added. Page 0's own shard carries no
        # added header, but using the reduced budget for all shards is the safe
        # (conservative) choice. Never reduce below a small floor.
        full_budget = self._shard_token_budget()
        header_tokens = estimate_tokens(header_context)
        effective_budget = max(1000, full_budget - header_tokens)

        shards = plan_shards(
            page_texts,
            token_budget=effective_budget,
            max_shards=max_shards,
            max_pages_per_shard=self._max_pages_per_shard(),
            table_boundary_pages=table_boundary_pages,
        )
        if len(shards) <= 1:
            return []

        # Save the full-section context to restore afterwards.
        saved_text = self._document_text
        saved_images = self._page_images
        payloads: list[dict[str, Any]] = []
        try:
            for shard in shards:
                shard_page_texts = page_texts[shard.start : shard.end]
                # Build this shard's document text with PAGE markers preserved.
                parts: list[str] = []
                if shard.start > 0 and header_context.strip():
                    parts.append(
                        "--- DOCUMENT HEADER (page 1, for context only) ---\n"
                        f"{header_context}"
                    )
                for offset, text in enumerate(shard_page_texts):
                    page_num = shard.start + offset + 1
                    parts.append(f"--- PAGE {page_num} ---\n{text}")
                self._document_text = "\n".join(parts)
                self._page_images = (
                    self._cap_agent_images(
                        self._slice_images(saved_images, shard.start, shard.end)
                    )
                    if send_images
                    else []
                )
                shard_images = self._page_images if send_images else None
                content = self._build_prompt_content(prompt_template, shard_images)
                payloads.append(
                    {
                        "content": content,
                        "page_start": shard.start,
                        "page_end": shard.end,
                        "total_pages": num_pages,
                        # Raw (un-rendered) shard text + images, kept so an
                        # in-shard assessment pass (integrated-assessment feature)
                        # can reuse AssessmentService.assess_results over the SAME
                        # pages without re-reading S3. Always page images here (not
                        # gated on send_images): assessment benefits from the image
                        # even when the extraction prompt has no {DOCUMENT_IMAGE}.
                        # These never cross the SFN JSON boundary — each shard Lambda
                        # rebuilds payloads locally — so bytes in the dict are safe.
                        "assess_document_text": self._document_text,
                        "assess_page_images": self._cap_agent_images(
                            self._slice_images(saved_images, shard.start, shard.end)
                        ),
                        # Integrated mode: tell the shard agent to emit per-field
                        # confidence/bbox inline (one inference, no second pass).
                        "emit_field_assessment": self._integrated_assessment_enabled(),
                    }
                )
        finally:
            self._document_text = saved_text
            self._page_images = saved_images

        logger.info(
            "Built %d input shards for concurrent extraction "
            "(budget=%d tokens, header-reserved=%d)",
            len(payloads),
            effective_budget,
            header_tokens,
        )
        return payloads

    @staticmethod
    def _slice_images(images: list[bytes], start: int, end: int) -> list[bytes]:
        """Slice page images to a page range, tolerating an empty/short list."""
        if not images:
            return []
        return images[start:end]

    def _cap_agent_images(self, images: list[bytes]) -> list[bytes]:
        """Cap how many page images are attached to one agent invocation.

        Sending many large page images in a single Bedrock request can cause an
        oversized first turn and a read timeout (a 25-page doc with
        ``{DOCUMENT_IMAGE}`` is the classic failure). Beyond
        ``agentic.max_images_per_agent`` (0 = unlimited), attach only the first N
        and log a warning — the agent still has the full OCR text and can pull
        specific pages on demand via the view_image tool.
        """
        cap = getattr(self.config.extraction.agentic, "max_images_per_agent", 0) or 0
        if cap <= 0 or not images or len(images) <= cap:
            return images
        logger.warning(
            "Capping agent page-images from %d to %d (agentic.max_images_per_agent) "
            "to avoid oversized-request read timeouts; OCR text is still complete "
            "and the agent can fetch other pages via the view_image tool.",
            len(images),
            cap,
        )
        return images[:cap]

    def _get_sizing_plan(self) -> Any:
        """Model-aware sizing plan for this section (memoized per-invocation).

        Derives shard token budget + confidence list-batch size from the
        extraction model's context/output windows minus
        ``extraction.context_buffer`` (see idp_common.bedrock.sizing). Explicit
        non-zero config values for shard_token_budget / list_batch_size are
        passed as overrides so they still win. Logged once per section.
        """
        cached = getattr(self, "_sizing_plan", None)
        if cached is not None:
            return cached
        from idp_common.bedrock.sizing import compute_sizing_plan

        ex = self.config.extraction
        agentic = ex.agentic
        # Explicit (non-zero) config values act as overrides; 0 => auto-size.
        stb = getattr(agentic, "shard_token_budget", 0) or None
        lbs = getattr(ex.confidence, "list_batch_size", 0) or None
        # list_batch_size has a legacy non-zero default (25); treat the legacy
        # default as "not an explicit override" so auto-sizing applies unless the
        # user genuinely changed it. We can't perfectly detect that, so honor any
        # value <= 0 as auto and otherwise pass it as an override only when the
        # confidence model is unknown (auto-size can't help). Simplest robust
        # rule: always auto-size list batch, but never EXCEED the configured
        # ceiling — handled in assess_results_batched's token-aware sizing.
        plan = compute_sizing_plan(
            model_id=(self._pending_extraction_model or ex.model),
            context_buffer=getattr(ex, "context_buffer", 0.30),
            geometry_mode=ex.geometry.mode,
            max_images_per_agent=getattr(agentic, "max_images_per_agent", 20),
            default_max_pages_per_shard=self._max_pages_per_shard(),
            shard_token_budget_override=stb,
            list_batch_size_override=lbs if (lbs and lbs != 25) else None,
            log_label="extraction",
        )
        self._sizing_plan = plan
        return plan

    def _shard_token_budget(self) -> int:
        """Per-shard input-token budget.

        Uses the explicit config override when set (non-zero); otherwise the
        model-aware auto-derived budget (falls back to the module default if
        sizing is somehow unavailable).
        """
        agentic = self.config.extraction.agentic
        budget = getattr(agentic, "shard_token_budget", None)
        if budget:
            return int(budget)
        try:
            return int(self._get_sizing_plan().shard_token_budget)
        except Exception as e:  # noqa: BLE001 - never break sharding on sizing
            logger.warning("Auto-sizing shard budget failed (%s); using default", e)
            return DEFAULT_SHARD_TOKEN_BUDGET

    def _max_pages_per_shard(self) -> int:
        """Per-shard page ceiling (config override or default).

        Returns the configured ``extraction.agentic.max_pages_per_shard``. A
        configured ``0`` disables the page ceiling (token budget only); a
        missing field (older configs) falls back to the default. Distinguishes
        missing (``None``) from an explicit ``0``.
        """
        agentic = self.config.extraction.agentic
        cap = getattr(agentic, "max_pages_per_shard", None)
        if cap is None:
            return DEFAULT_MAX_PAGES_PER_SHARD
        return int(cap)

    @staticmethod
    def _table_header_context(page_one_text: str, max_lines: int = 30) -> str:
        """Return the *header region* of page 1 for prepending to later shards.

        Includes title/account context and the table's COLUMN HEADERS but DROPS
        the page-1 data rows so the deterministic table parser doesn't re-emit
        them in every later shard (which would duplicate rows after merge).

        Strategy:
        - If the text contains a Markdown table separator line (``|---|---|``),
          keep everything up to and including that line and drop the rest (the
          data rows that follow). This preserves the column header exactly once.
        - Otherwise (no detectable table header) keep at most ``max_lines`` lines
          so account-level context still propagates without dragging a full page
          of data into every shard.
        """
        if not page_one_text:
            return ""
        lines = page_one_text.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Markdown separator row: pipes/dashes/colons/spaces and >=1 dash.
            if (
                "-" in stripped
                and set(stripped) <= set("|-: ")
                and stripped.count("-") >= 3
            ):
                return "\n".join(lines[: i + 1])
        return "\n".join(lines[:max_lines])

    def _build_few_shot_examples_content(self) -> list[dict[str, Any]]:
        """
        Build content items for few-shot examples from the configuration for a specific class.

        Returns:
            List of content items containing text and image content for examples
        """
        content: list[dict[str, Any]] = []

        # Use the stored class schema
        if not self._class_schema:
            logger.warning(
                f"No class schema found for '{self._class_label}' for few-shot examples"
            )
            return content

        # Get examples from the JSON Schema for this specific class
        content = build_few_shot_extraction_examples_content(self._class_schema)

        return content

    def _make_json_serializable(self, obj: Any) -> Any:
        """
        Recursively convert any object to a JSON-serializable format.

        Args:
            obj: Object to make JSON serializable

        Returns:
            JSON-serializable version of the object
        """
        from enum import Enum

        if isinstance(obj, dict):
            return {
                key: self._make_json_serializable(value) for key, value in obj.items()
            }
        elif isinstance(obj, (list, tuple)):
            return [self._make_json_serializable(item) for item in obj]
        elif isinstance(obj, Enum):
            return obj.value
        elif hasattr(obj, "__dict__"):
            # Handle custom objects by converting to dict
            return self._make_json_serializable(obj.__dict__)
        elif hasattr(obj, "to_dict"):
            # Handle objects with to_dict method
            return self._make_json_serializable(obj.to_dict())
        elif isinstance(obj, bytes):
            # Convert bytes to base64 string or placeholder
            return f"<bytes_object_{len(obj)}_bytes>"
        else:
            try:
                # Test if it's already JSON serializable
                json.dumps(obj)
                return obj
            except (TypeError, ValueError):
                # Convert non-serializable objects to string representation
                return str(obj)

    def _invoke_custom_prompt_lambda(
        self, lambda_arn: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Invoke custom prompt generator Lambda function with JSON-serializable payload.

        Args:
            lambda_arn: ARN of the Lambda function to invoke
            payload: Payload to send to Lambda function (must be JSON serializable)

        Returns:
            Dict containing system_prompt and task_prompt_content

        Raises:
            Exception: If Lambda invocation fails or returns invalid response
        """
        import boto3

        lambda_client = boto3.client("lambda", region_name=self.region)

        try:
            logger.info(f"Invoking custom prompt Lambda: {lambda_arn}")
            response = lambda_client.invoke(
                FunctionName=lambda_arn,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload),
            )

            if response.get("FunctionError"):
                error_payload = response.get("Payload", b"").read().decode()
                error_msg = f"Custom prompt Lambda failed: {error_payload}"
                logger.error(error_msg)
                raise Exception(error_msg)

            result = json.loads(response["Payload"].read())
            logger.info("Custom prompt Lambda invoked successfully")

            # Validate response structure
            if not isinstance(result, dict):
                error_msg = f"Custom prompt Lambda returned invalid response format: expected dict, got {type(result)}"
                logger.error(error_msg)
                raise Exception(error_msg)

            if "system_prompt" not in result:
                error_msg = "Custom prompt Lambda response missing required field: system_prompt"
                logger.error(error_msg)
                raise Exception(error_msg)

            if "task_prompt_content" not in result:
                error_msg = "Custom prompt Lambda response missing required field: task_prompt_content"
                logger.error(error_msg)
                raise Exception(error_msg)

            return result

        except Exception as e:
            error_msg = f"Failed to invoke custom prompt Lambda {lambda_arn}: {str(e)}"
            logger.error(error_msg)
            raise Exception(error_msg)

    def _reset_context(self) -> None:
        """Reset instance variables for clean state before processing."""
        self._document_text = ""
        self._class_label = ""
        self._attribute_descriptions = ""
        self._class_schema = {}
        self._wire_class_schema = {}
        self._instance_probe_requested = False
        self._page_images = []
        self._image_uris = []
        self._grounded_assessment = None
        # Top-level fields the simple-extraction schema-compliance filter dropped
        # because the class schema does not define them (off-schema/hallucinated).
        # Set by _filter_extracted_to_schema, surfaced by _build_extraction_issues.
        self._off_schema_fields: list[str] = []
        # Wall-clock deadline (epoch seconds) for the in-shard assessment ladder
        # (1.5); set per-invocation from the Lambda context. None = no guard.
        self._assessment_deadline_epoch = None
        # Memoized model-aware SizingPlan for the current section (item 2).
        self._sizing_plan = None
        self._document = None
        self._agent_table_tool_note = None

    def _validate_and_find_section(
        self, document: Document, section_id: str
    ) -> Any | None:
        """
        Validate document and find section by ID.

        Args:
            document: Document to validate
            section_id: ID of section to find

        Returns:
            Section if found, None otherwise (errors added to document)
        """
        if not document:
            logger.error("No document provided")
            return None

        if not document.sections:
            logger.error("Document has no sections to process")
            document.errors.append("Document has no sections to process")
            return None

        # Find the section with the given ID
        for section in document.sections:
            if section.section_id == section_id:
                return section

        error_msg = f"Section {section_id} not found in document"
        logger.error(error_msg)
        document.errors.append(error_msg)
        return None

    def _prepare_section_info(self, document: Document, section: Any) -> SectionInfo:
        """
        Prepare section metadata and output paths.

        Args:
            document: Document being processed
            section: Section being processed

        Returns:
            SectionInfo with all metadata
        """
        class_label = section.classification
        output_bucket = document.output_bucket
        output_prefix = document.input_key
        output_key = f"{output_prefix}/sections/{section.section_id}/result.json"
        output_uri = f"s3://{output_bucket}/{output_key}"

        # Check if the section has required pages
        if not section.page_ids:
            error_msg = f"Section {section.section_id} has no page IDs"
            logger.error(error_msg)
            document.errors.append(error_msg)
            raise ValueError(error_msg)

        # Sort pages by page number
        sorted_page_ids = sorted(section.page_ids, key=int)
        start_page = int(sorted_page_ids[0])
        end_page = int(sorted_page_ids[-1])

        # Use pre-calculated page_indices from classification service if available
        # This ensures consistent page_indices calculation across all sections in a document packet
        if section.attributes and "page_indices" in section.attributes:
            page_indices = section.attributes["page_indices"]
            logger.info(
                f"Using pre-calculated page_indices from section attributes: {page_indices}"
            )
        else:
            # Fallback: calculate page_indices for backward compatibility
            # This handles sections processed before the fix was implemented
            try:
                # Find minimum page ID across all available sections
                all_page_ids = []
                for sec in document.sections:
                    all_page_ids.extend(sec.page_ids)

                if all_page_ids:
                    global_min_page_id = min(int(page_id) for page_id in all_page_ids)
                else:
                    global_min_page_id = 1

                page_indices = [
                    int(page_id) - global_min_page_id for page_id in sorted_page_ids
                ]
                logger.warning(
                    f"page_indices not found in section attributes, calculated: {page_indices} (global_min_page_id={global_min_page_id})"
                )
            except (ValueError, TypeError) as e:
                # Final fallback: assume 1-indexed page IDs
                page_indices = [int(page_id) - 1 for page_id in sorted_page_ids]
                logger.warning(
                    f"Error calculating page_indices, using 1-indexed fallback: {page_indices} - {e}"
                )

        logger.info(
            f"Processing {len(sorted_page_ids)} pages, class {class_label}: {start_page}-{end_page}"
        )

        # Track metrics
        metrics.put_metric("InputDocuments", 1)
        metrics.put_metric("InputDocumentPages", len(section.page_ids))

        return SectionInfo(
            class_label=class_label,
            sorted_page_ids=sorted_page_ids,
            page_indices=page_indices,
            output_bucket=output_bucket,
            output_key=output_key,
            output_uri=output_uri,
            start_page=start_page,
            end_page=end_page,
        )

    def _load_page_texts(
        self, document: Document, sorted_page_ids: list[str]
    ) -> dict[str, str]:
        """Load OCR text for each page in the section, preserving page IDs.

        Pages missing from ``document.pages`` are recorded as errors and
        omitted from the result.
        """
        t0 = time.time()
        page_id_to_text: dict[str, str] = {}
        for page_id in sorted_page_ids:
            if page_id not in document.pages:
                error_msg = f"Page {page_id} not found in document"
                logger.error(error_msg)
                document.errors.append(error_msg)
                continue
            page = document.pages[page_id]
            text_path = page.parsed_text_uri
            page_id_to_text[page_id] = s3.get_text_content(text_path)
        logger.info(f"Time taken to read text content: {time.time() - t0:.2f} seconds")
        return page_id_to_text

    def _format_document_text(
        self,
        sorted_page_ids: list[str],
        page_id_to_text: dict[str, str],
        page_type_presence: PageTypePresence | None = None,
    ) -> str:
        """Concatenate per-page text with sequential page markers.

        When ``page_type_presence`` resolves a page type for a page, the
        marker is annotated as ``--- PAGE N [PageType] ---`` so the LLM
        gets a soft hint about the page's role.
        """
        page_id_to_type: dict[str, str] = (
            page_type_presence.page_id_to_page_type if page_type_presence else {}
        )
        parts: list[str] = []
        for idx, page_id in enumerate(
            pid for pid in sorted_page_ids if pid in page_id_to_text
        ):
            page_num = idx + 1
            page_type = page_id_to_type.get(page_id)
            marker = (
                f"--- PAGE {page_num} [{page_type}] ---"
                if page_type
                else f"--- PAGE {page_num} ---"
            )
            parts.append(f"{marker}\n{page_id_to_text[page_id]}")
        return "\n".join(parts)

    def _load_document_text(
        self, document: Document, sorted_page_ids: list[str]
    ) -> str:
        """Backwards-compatible loader that returns the concatenated text only.

        Prefer :meth:`_load_page_texts` + :meth:`_format_document_text` when
        per-page text is also needed (e.g., for page-type resolution).
        """
        page_id_to_text = self._load_page_texts(document, sorted_page_ids)
        return self._format_document_text(sorted_page_ids, page_id_to_text)

    def _load_confidence_data(
        self, document: Document, sorted_page_ids: list[str]
    ) -> dict[str, str]:
        """
        Load OCR confidence data for pages in a section.

        Reads text_confidence_uri from each page to provide confidence scores
        to the table parsing tool.

        Args:
            document: Document containing pages
            sorted_page_ids: Sorted list of page IDs

        Returns:
            Dict mapping page IDs to confidence data strings
        """
        confidence_data: dict[str, str] = {}
        for page_id in sorted_page_ids:
            if page_id not in document.pages:
                continue
            page = document.pages[page_id]
            confidence_uri = getattr(page, "text_confidence_uri", None)
            if confidence_uri:
                try:
                    conf_text = s3.get_text_content(confidence_uri)
                    if conf_text:
                        confidence_data[page_id] = conf_text
                except Exception as e:
                    logger.warning(
                        f"Failed to load confidence data for page {page_id}: {e}"
                    )
        return confidence_data

    def _load_document_images(
        self, document: Document, sorted_page_ids: list[str]
    ) -> list[Any]:
        """
        Load images from all pages.

        Args:
            document: Document containing pages
            sorted_page_ids: Sorted list of page IDs

        Returns:
            List of prepared images
        """
        t0 = time.time()
        target_width = self.config.extraction.image.target_width
        target_height = self.config.extraction.image.target_height

        page_images = []
        for page_id in sorted_page_ids:
            if page_id not in document.pages:
                continue

            page = document.pages[page_id]
            image_uri = page.image_uri
            image_content = image.prepare_image(image_uri, target_width, target_height)
            page_images.append(image_content)

        t1 = time.time()
        logger.info(f"Time taken to read images: {t1 - t0:.2f} seconds")

        return page_images

    def _initialize_extraction_context(
        self,
        class_label: str,
        document_text: str,
        page_images: list[Any],
        sorted_page_ids: list[str],
        document: Document,
    ) -> tuple[dict[str, Any], str]:
        """
        Initialize extraction context and set instance variables.

        Args:
            class_label: Document class
            document_text: Text content
            page_images: Prepared images
            sorted_page_ids: Sorted page IDs
            document: Document being processed

        Returns:
            Tuple of (class_schema, attribute_descriptions)
        """
        # Get JSON Schema for this document class
        class_schema = self._get_class_schema(class_label)

        # The schema put ON THE WIRE (prompt text and, when forced tool use is
        # on, the toolSpec) can differ from the effective class schema by exactly
        # one auxiliary property: the multi-instance detection probe (#753). It
        # is added to a COPY, so the off-schema filter, the JSON-Schema
        # validator, the generated Pydantic model and every downstream stage
        # still see only the declared fields.
        wire_schema, probe_added = self._build_wire_schema(class_schema, class_label)
        attribute_descriptions = self._format_schema_for_prompt(wire_schema)

        # Store context in instance variables
        self._document_text = document_text
        self._class_label = class_label
        self._attribute_descriptions = attribute_descriptions
        self._class_schema = class_schema
        self._wire_class_schema = wire_schema
        self._instance_probe_requested = probe_added
        self._page_images = page_images

        # Prepare image URIs for Lambda
        image_uris = []
        for page_id in sorted_page_ids:
            if page_id in document.pages:
                page = document.pages[page_id]
                if page.image_uri:
                    image_uris.append(page.image_uri)
        self._image_uris = image_uris

        return class_schema, attribute_descriptions

    def _handle_empty_schema(
        self,
        document: Document,
        section: Any,
        section_info: SectionInfo,
        section_id: str,
        t0: float,
    ) -> Document:
        """
        Handle case when schema has no attributes - skip LLM and return empty result.

        Args:
            document: Document being processed
            section: Section being processed
            section_info: Section metadata
            section_id: Section ID
            t0: Start time

        Returns:
            Updated document
        """
        logger.info(
            f"No attributes defined for class {section_info.class_label}, skipping LLM extraction"
        )

        # Create empty result structure
        extracted_fields = {}
        metering = {
            "input_tokens": 0,
            "output_tokens": 0,
            "invocation_count": 0,
            "total_cost": 0.0,
        }
        total_duration = 0.0
        parsing_succeeded = True

        # Write to S3
        output = {
            "document_class": {"type": section_info.class_label},
            "split_document": {"page_indices": section_info.page_indices},
            "inference_result": extracted_fields,
            "metadata": {
                "parsing_succeeded": parsing_succeeded,
                "extraction_time_seconds": total_duration,
                "skipped_due_to_empty_attributes": True,
            },
        }
        s3.write_content(
            output,
            section_info.output_bucket,
            section_info.output_key,
            content_type="application/json",
        )

        # Update section and document
        section.extraction_result_uri = section_info.output_uri
        document.metering = utils.merge_metering_data(document.metering, metering)

        t3 = time.time()
        logger.info(
            f"Skipped extraction for section {section_id} due to empty attributes: {t3 - t0:.2f} seconds"
        )
        return document

    def _build_extraction_content(
        self,
        document: Document,
        page_images: list[Any],
    ) -> tuple[list[dict[str, Any]], str]:
        """
        Build prompt content (with or without custom Lambda).

        Args:
            document: Document being processed
            page_images: Prepared page images

        Returns:
            Tuple of (content, system_prompt)
        """
        # Resolve prompts — use per-class system/task prompt overrides if
        # specified on the class schema, otherwise fall back to the global
        # extraction prompts. Backward compatible: classes without overrides
        # use the global prompts unchanged.
        class_system_prompt_override = self._class_schema.get(
            X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT
        )
        system_prompt = (
            class_system_prompt_override or self.config.extraction.system_prompt
        )
        if class_system_prompt_override:
            logger.info(
                f"Using per-class extraction system prompt override for "
                f"'{self._class_label}'"
            )

        from idp_common.extraction.prompt_assembly import (
            select_extraction_task_prompt,
        )

        class_task_prompt_override = self._class_schema.get(
            X_AWS_IDP_EXTRACTION_TASK_PROMPT
        )
        # Select the extraction task prompt per settings: integrated mode uses the
        # extraction+confidence template (+ bbox for LLM-box geometry); otherwise
        # the plain extraction template. A per-class override still wins.
        task_prompt = class_task_prompt_override or select_extraction_task_prompt(
            self.config.extraction
        )
        if class_task_prompt_override:
            logger.info(
                f"Using per-class extraction task prompt override for "
                f"'{self._class_label}'"
            )

        custom_lambda_arn = self.config.extraction.custom_prompt_lambda_arn

        if custom_lambda_arn and custom_lambda_arn.strip():
            logger.info(f"Using custom prompt Lambda: {custom_lambda_arn}")

            prompt_placeholders = {
                "DOCUMENT_TEXT": self._document_text,
                "DOCUMENT_CLASS": self._class_label,
                "ATTRIBUTE_NAMES_AND_DESCRIPTIONS": self._attribute_descriptions,
                "DOCUMENT_IMAGE": self._image_uris,
            }

            logger.info(
                f"Lambda will receive {len(self._image_uris)} image URIs in DOCUMENT_IMAGE placeholder"
            )

            # Build default content for Lambda input
            prompt_template = task_prompt
            if prompt_template:
                default_content = self._build_prompt_content(
                    prompt_template, page_images
                )
            else:
                default_content = self._get_default_prompt_content()

            # Prepare Lambda payload
            try:
                document_dict = document.to_dict()
            except Exception as e:
                logger.warning(f"Error serializing document for Lambda payload: {e}")
                document_dict = {"id": getattr(document, "id", "unknown")}

            payload = {
                "config": self._make_json_serializable(self.config),
                "prompt_placeholders": prompt_placeholders,
                "default_task_prompt_content": self._make_json_serializable(
                    default_content
                ),
                "serialized_document": document_dict,
            }

            # Invoke custom Lambda
            lambda_result = self._invoke_custom_prompt_lambda(
                custom_lambda_arn, payload
            )

            # Use Lambda results
            system_prompt = lambda_result.get("system_prompt", system_prompt)
            content = lambda_result.get("task_prompt_content", default_content)

            logger.info("Successfully applied custom prompt from Lambda function")
        else:
            # Use default prompt logic
            logger.info(
                "No custom prompt Lambda configured - using default prompt generation"
            )
            prompt_template = task_prompt

            if not prompt_template:
                content = self._get_default_prompt_content()
            else:
                try:
                    content = self._build_prompt_content(prompt_template, page_images)
                except ValueError as e:
                    logger.warning(
                        f"Error formatting prompt template: {str(e)}. Using default prompt."
                    )
                    content = self._get_default_prompt_content()

        return content, system_prompt

    def _analyze_schema_for_table_requirements(
        self, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Analyze schema to detect large array fields that require table parsing.

        NOTE: This is OPTIONAL - minItems is not required for tool usage.
        OCR analysis is the primary adaptive trigger. Schema analysis only
        provides additional signal when minItems is explicitly set.

        Returns:
            Dict with analysis results including whether tool usage is recommended
        """
        large_arrays = []
        max_min_items = 0

        properties = schema.get(SCHEMA_PROPERTIES, {})
        for field_name, field_def in properties.items():
            if field_def.get("type") == "array":
                # minItems can arrive as a string after a config round-trip
                # (the Configuration table stores numeric schema fields as
                # strings); coerce defensively so the comparison never raises.
                try:
                    min_items = int(field_def.get("minItems", 0) or 0)
                except (TypeError, ValueError):
                    min_items = 0
                if min_items > 50:  # Match OCR threshold for consistency
                    large_arrays.append(
                        {
                            "field": field_name,
                            "min_items": min_items,
                            "description": field_def.get("description", ""),
                        }
                    )
                    max_min_items = max(max_min_items, min_items)

        recommendation = len(large_arrays) > 0

        return {
            "large_array_fields": [arr["field"] for arr in large_arrays],
            "max_min_items": max_min_items,
            "field_details": large_arrays,
            "tool_usage_recommended": recommendation,
            "recommendation_reason": (
                f"Schema has {len(large_arrays)} array field(s) with minItems > 50"
                if recommendation
                else "No large array constraints detected"
            ),
            "recommendation_strength": (
                "MANDATORY"
                if max_min_items >= 500  # Match OCR threshold
                else "STRONGLY_RECOMMENDED"
                if max_min_items >= 100  # Medium-large
                else "RECOMMENDED"
                if max_min_items >= 50  # Medium
                else "OPTIONAL"
            ),
        }

    def _analyze_ocr_for_tables(self, ocr_text: str) -> dict[str, Any]:
        """
        Analyze OCR text to detect large Markdown tables.

        This is the PRIMARY trigger for table parsing tool guidance, adapting
        automatically to documents of any size without requiring specific minItems.

        Returns:
            Dict with table detection results
        """
        import re

        # Detect Markdown table rows (lines with | delimiters)
        table_rows = []
        lines = ocr_text.split("\n")

        for i, line in enumerate(lines):
            stripped = line.strip()
            # Skip separator rows (e.g., |---|---|)
            if "|" in stripped and not re.match(r"^[\s|:-]+$", stripped):
                table_rows.append(i)

        # Estimate table count by gaps
        tables_detected = 0
        estimated_total_rows = 0
        if table_rows:
            tables_detected = 1
            gap_threshold = 5
            for i in range(1, len(table_rows)):
                if table_rows[i] - table_rows[i - 1] > gap_threshold:
                    tables_detected += 1
            estimated_total_rows = len(table_rows)

        # Adaptive thresholds - lower for better real-world coverage
        # These are optimized for automatic detection without minItems requirement
        recommendation = estimated_total_rows > 30

        return {
            "tables_detected": tables_detected,
            "estimated_row_count": estimated_total_rows,
            "tool_usage_recommended": recommendation,
            "recommendation_reason": (
                f"Detected {tables_detected} table(s) with ~{estimated_total_rows} total rows"
                if recommendation
                else "No large tables detected in OCR text"
            ),
            "recommendation_strength": (
                "MANDATORY"
                if estimated_total_rows >= 500  # Large documents
                else "STRONGLY_RECOMMENDED"
                if estimated_total_rows >= 100  # Medium-large tables
                else "RECOMMENDED"
                if estimated_total_rows >= 50  # Medium tables
                else "OPTIONAL"
            ),
        }

    def _build_table_parsing_guidance(
        self, schema_analysis: dict[str, Any], ocr_analysis: dict[str, Any]
    ) -> str:
        """Build custom table parsing guidance based on pre-flight analysis."""

        # Determine overall recommendation strength
        strengths = [
            schema_analysis.get("recommendation_strength", "OPTIONAL"),
            ocr_analysis.get("recommendation_strength", "OPTIONAL"),
        ]

        # Get the strongest recommendation
        strength_order = [
            "OPTIONAL",
            "RECOMMENDED",
            "STRONGLY_RECOMMENDED",
            "MANDATORY",
        ]
        max_strength = max(
            strengths,
            key=lambda x: strength_order.index(x) if x in strength_order else 0,
        )

        if max_strength == "MANDATORY":
            # For very large tables (500+ rows detected by OCR)
            guidance = """
**CRITICAL - MANDATORY TABLE PARSING TOOL USAGE**:
This document contains a large table with {row_count}+ rows detected by OCR analysis.
You MUST use the parse_table tool for complete and accurate extraction:

1. IMMEDIATELY call parse_table with the full document text or table section
2. DO NOT attempt manual row-by-row LLM extraction for large tables
3. Verify parse_table returned ALL expected rows (check row_count in response)
4. If parse_table returns fewer rows than expected, investigate warnings and ensure all
   table fragments are parsed (tables may be split across pages)

FAILURE TO USE parse_table will result in:
- Incomplete extraction (missing rows) - you will miss hundreds of data points
- Schema validation failures
- Excessive token usage (may hit context limits)
- Poor extraction performance

This is not optional - use the tool immediately for any tabular data.
"""
            return guidance.format(
                row_count=ocr_analysis.get("estimated_row_count", "500"),
            )

        elif max_strength == "STRONGLY_RECOMMENDED":
            # For medium-large tables (100-499 rows) - still use explicit instructions
            guidance = """
**IMPORTANT - USE TABLE PARSING TOOL**:
This document contains tabular data with {row_count}+ rows detected.
You MUST use the parse_table tool for accurate and complete extraction:

1. Call parse_table with the document text containing the table
2. The tool handles OCR artifacts (empty lines, page breaks) automatically
3. Verify the row_count matches your expectation
4. Review any warnings about table fragmentation
5. Map the parsed columns to the required schema fields

Using the tool ensures:
- Complete data extraction (no missing rows)
- Faster processing (10x more efficient than manual)
- Better accuracy (deterministic parsing of well-structured tables)

Do NOT attempt manual row-by-row extraction for tables with 100+ rows.
"""
            return guidance.format(
                row_count=ocr_analysis.get("estimated_row_count", "100"),
            )

        elif max_strength == "RECOMMENDED":
            # For medium tables (50-99 rows) - gentler guidance
            guidance = """
**RECOMMENDED - TABLE PARSING TOOL**:
Detected a table with {row_count}+ rows. Consider using the parse_table tool:

1. Call parse_table to extract the table data efficiently
2. Review the quality metrics and warnings
3. Fall back to LLM extraction if parse quality is poor

Benefits: Faster, more accurate, handles OCR artifacts automatically.
"""
            return guidance.format(
                row_count=ocr_analysis.get("estimated_row_count", "50"),
            )

        # Return empty for optional cases (standard TABLE_PARSING_PROMPT_ADDENDUM will be used)
        return ""

    @staticmethod
    def _extract_table_tool_note(response_with_metering: Any) -> str | None:
        """Pull the agent's optional ``TABLE_TOOL_NOTE:`` line from its final text.

        The extraction agent is instructed to end its final message with a single
        ``TABLE_TOOL_NOTE: <one sentence>`` line when it extracted a large table
        directly instead of calling the deterministic parser. We surface that
        first-person rationale in the processing report (clearly labelled as the
        agent's *stated* reason — a post-hoc explanation, not ground truth).
        Returns the note text (without the prefix), or None. Best-effort.
        """
        try:
            from idp_common import bedrock

            text = bedrock.extract_text_from_response(dict(response_with_metering))
        except Exception:  # noqa: BLE001 - report-only; never fail extraction
            return None
        if not text or "TABLE_TOOL_NOTE:" not in text:
            return None
        # Take the content after the last occurrence of the marker, first line only.
        note = text.split("TABLE_TOOL_NOTE:")[-1].strip().splitlines()[0].strip()
        return note or None

    def _explain_tool_usage_decision(
        self,
        expected: bool,
        actual: bool,
        schema_analysis: dict[str, Any] | None,
        ocr_analysis: dict[str, Any] | None,
        tool_enabled: bool = True,
        ocr_had_markdown_tables: bool = True,
        agent_note: str | None = None,
    ) -> str:
        """Generate a human-readable explanation of the table-parsing-tool decision.

        Distinguishes the cases that matter for the report:
          - tool DISABLED in config,
          - tool ENABLED but OCR produced no Markdown tables (nothing to parse),
          - tool AVAILABLE and recommended, but the AGENT chose not to call it
            (a model decision — it judged direct LLM extraction sufficient),
          - normal recommended-and-used / not-required cases.
        """
        if expected and actual:
            return "Tool was recommended and used as expected."

        if expected and not actual:
            # Recommended but not used — explain the actual blocker.
            if not tool_enabled:
                return (
                    "Table parsing was recommended but is DISABLED in this "
                    "configuration (extraction.agentic.table_parsing.enabled=false), "
                    "so the agent used direct LLM extraction instead."
                )
            if not ocr_had_markdown_tables:
                return (
                    "Table parsing is enabled but the OCR output contained NO "
                    "Markdown tables for it to parse (the deterministic parser only "
                    "runs on Markdown pipe-tables — e.g. Textract with the TABLES "
                    "feature, or another OCR backend that emits Markdown tables). "
                    "The agent used direct LLM extraction instead."
                )
            reasons = []
            if ocr_analysis and ocr_analysis.get("tool_usage_recommended"):
                reasons.append(ocr_analysis.get("recommendation_reason", ""))
            if schema_analysis and schema_analysis.get("tool_usage_recommended"):
                reasons.append(schema_analysis.get("recommendation_reason", ""))
            reason_str = "; ".join(r for r in reasons if r) or "large tables detected"
            base = (
                "Table parsing was ENABLED and RECOMMENDED "
                f"({reason_str}), and Markdown tables were present in the OCR, but "
                "the extraction agent CHOSE NOT to call the deterministic parser and "
                "extracted the table directly instead."
            )
            if agent_note:
                # First-person reason the agent gave — a stated (post-hoc)
                # explanation, not verified ground truth.
                base += f' Agent\'s stated reason: "{agent_note}"'
            else:
                base += (
                    " (No reason was reported by the agent.) The row counts below "
                    "and the completeness check indicate whether that was safe; if a "
                    "large table looks truncated, this is the first thing to inspect."
                )
            return base

        if not expected and actual:
            return "Tool was used even though not strictly required (agent chose to use it)."

        return "Tool usage was not required (no large Markdown tables detected) and was not used."

    def _check_completeness_detailed(
        self,
        extracted_fields: dict[str, Any],
        schema: dict[str, Any],
        tool_used: bool,
        ocr_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Detailed completeness check with violations.

        ``minItems`` is the only *schema* signal for a short list, and most configs
        do not set it — so this used to print "All schema constraints satisfied"
        directly above a warning that a declared 100-row list had come back
        ``null``, which reads as a clean bill of health. When ``ocr_analysis``
        supplies the evidence, that case is now reported here too, without
        overstating it as a schema violation (it isn't one).
        """

        violations = []
        properties = schema.get(SCHEMA_PROPERTIES, {})

        for field_name, field_def in properties.items():
            if field_def.get("type") == "array":
                min_items = field_def.get("minItems", 0)
                actual_items = len(extracted_fields.get(field_name) or [])

                if min_items > 0 and actual_items < min_items:
                    violations.append(
                        {
                            "field": field_name,
                            "constraint": f"minItems: {min_items}",
                            "actual": actual_items,
                            "shortfall": min_items - actual_items,
                            "completeness_pct": round(
                                100 * actual_items / min_items, 1
                            ),
                            "message": (
                                f"Extracted {actual_items} items but schema requires "
                                f"minimum {min_items} ({100 * actual_items / min_items:.1f}% complete)"
                            ),
                            "possible_cause": (
                                "Agent did not use table parsing tool"
                                if not tool_used
                                else "Table parsing may have stopped early"
                            ),
                        }
                    )

        # Evidence-based emptiness: no schema constraint is broken, but the OCR
        # pre-flight found a substantial table and every declared list came back
        # with no rows. Reported separately so `schema_constraints_met` keeps its
        # exact meaning while the summary stops claiming everything is fine.
        ocr = ocr_analysis or {}
        empty_lists: list[str] = []
        if ocr.get("tool_usage_recommended") and ocr.get("tables_detected"):
            empty, populated = find_empty_declared_lists(extracted_fields, schema)
            if empty and not populated:
                empty_lists = empty

        schema_ok = len(violations) == 0
        if violations:
            summary = (
                f"{len(violations)} constraint violation(s) detected - "
                "extraction may be incomplete"
            )
        elif empty_lists:
            summary = (
                f"Schema constraints satisfied, but list field(s) "
                f"{', '.join(empty_lists)} returned NO rows while the OCR found "
                f"{ocr.get('tables_detected')} table region(s) with ~"
                f"{ocr.get('estimated_row_count')} rows. "
                + (
                    "The agent did not use the table parsing tool."
                    if not tool_used
                    else "The table parsing tool ran but produced no rows."
                )
                + " Set minItems on the list field to make this a hard constraint."
            )
        else:
            summary = "All schema constraints satisfied"

        return {
            "schema_constraints_met": schema_ok,
            "violations": violations,
            "unexplained_empty_lists": empty_lists,
            "complete": schema_ok and not empty_lists,
            "summary": summary,
        }

    @staticmethod
    def _count_schema_leaf_fields(
        schema_node: dict[str, Any], value: Any
    ) -> tuple[int, int, list[str]]:
        """Recursively count schema-defined leaf fields vs. populated ones.

        Walks the schema's ``properties`` (descending into nested ``object``
        properties and the ``items`` of ``array`` properties) and, in parallel,
        the extracted ``value``. Returns ``(defined, populated, empty_paths)``:

        - ``defined``  — number of leaf (scalar) fields the schema declares.
        - ``populated`` — how many of those have a non-empty value
          (not None, not "" / [] / {}).
        - ``empty_paths`` — dotted paths of the leaf fields that came back empty.

        For arrays of objects, the item schema's leaves are counted once per
        present row (so a 0-row array contributes its leaves as "defined but
        empty", surfacing tables that extracted nothing). This is the signal
        that catches silent nested-field loss (all-null nested objects).
        """
        defined = 0
        populated = 0
        empty: list[str] = []

        node_type = schema_node.get(SCHEMA_TYPE)
        properties = schema_node.get(SCHEMA_PROPERTIES) or {}

        if node_type == TYPE_OBJECT or properties:
            value_dict = value if isinstance(value, dict) else {}
            for prop_name, prop_schema in properties.items():
                if not isinstance(prop_schema, dict):
                    continue
                prop_type = prop_schema.get(SCHEMA_TYPE)
                child_value = value_dict.get(prop_name)
                path = prop_name

                if prop_type == TYPE_OBJECT or (prop_schema.get(SCHEMA_PROPERTIES)):
                    d, p, e = ExtractionService._count_schema_leaf_fields(
                        prop_schema, child_value
                    )
                    defined += d
                    populated += p
                    empty.extend(f"{path}.{sub}" for sub in e)
                elif prop_type == TYPE_ARRAY:
                    item_schema = prop_schema.get(SCHEMA_ITEMS) or {}
                    rows = child_value if isinstance(child_value, list) else []
                    if isinstance(item_schema, dict) and item_schema.get(
                        SCHEMA_PROPERTIES
                    ):
                        if rows:
                            for idx, row in enumerate(rows):
                                d, p, e = ExtractionService._count_schema_leaf_fields(
                                    item_schema, row
                                )
                                defined += d
                                populated += p
                                empty.extend(f"{path}[{idx}].{sub}" for sub in e)
                        else:
                            # Empty array: count the item leaves once as "missing".
                            d, _p, _e = ExtractionService._count_schema_leaf_fields(
                                item_schema, {}
                            )
                            defined += d
                            empty.append(path)
                    else:
                        # Array of scalars.
                        defined += 1
                        if rows:
                            populated += 1
                        else:
                            empty.append(path)
                else:
                    # Scalar leaf.
                    defined += 1
                    if child_value not in (None, "", [], {}):
                        populated += 1
                    else:
                        empty.append(path)

        return defined, populated, empty

    def _check_population_completeness(
        self, extracted_fields: dict[str, Any], schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Heuristic: how much of the schema actually got populated.

        Unlike ``_check_completeness_detailed`` (which only flags hard
        ``minItems`` constraint violations), this flags *suspiciously sparse*
        extractions — e.g. an agentic run that returned a correct top-level
        object but null for nearly every nested field. It cannot know a field
        *should* have had a value, so it is advisory (a warning signal), not a
        hard failure: a genuinely sparse document will also score low.
        """
        defined, populated, empty_paths = self._count_schema_leaf_fields(
            schema, extracted_fields
        )
        ratio = (populated / defined) if defined else 1.0
        threshold = self.config.extraction.validation.min_population_ratio
        below = defined > 0 and ratio < threshold

        if below:
            logger.warning(
                "Extraction populated only %d/%d schema fields (%.0f%%), below the "
                "%.0f%% completeness threshold — possible silent extraction loss "
                "(e.g. nested fields not captured).",
                populated,
                defined,
                ratio * 100,
                threshold * 100,
                extra={"empty_fields": empty_paths[:50]},
            )

        return {
            "fields_defined": defined,
            "fields_populated": populated,
            "population_ratio": round(ratio, 3),
            "threshold": threshold,
            "below_threshold": below,
            # Cap the echoed list so a huge sparse schema can't bloat metadata.
            "empty_fields": empty_paths[:50],
        }

    def _filter_extracted_to_schema(
        self, extracted_fields: dict[str, Any]
    ) -> dict[str, Any]:
        """Drop top-level fields not defined in the class schema (simple mode).

        Advanced (agentic) extraction validates output through a generated Pydantic
        model, which structurally ignores keys the schema does not declare. Simple
        extraction has no such step — it keeps whatever the model returns — so
        hallucinated or cross-class attributes reach ``inference_result`` and later
        break the confidence assessment (the enhancer collapses a list emitted for a
        non-array/undefined attribute, leaving those rows permanently unscored — the
        IDP1 ``assessment_schema_mismatch`` failure). This mirrors the agentic
        behavior for simple mode at zero extra model cost.

        Conservative and fail-open:
        - No-op when the class schema has no ``properties`` (can't tell what's
          off-schema → keep everything, unchanged behavior).
        - Preserves the raw dict when a ``$defs``/``properties`` schema is absent.
        - Only drops UNKNOWN top-level keys; known fields (and their values, however
          malformed) pass through untouched — value/type correction is assessment's
          job, not this filter's.

        Records dropped names on ``self._off_schema_fields`` for the processing
        report. Returns the filtered dict (same object identity not guaranteed).
        """
        from idp_common.config.schema_constants import SCHEMA_PROPERTIES

        self._off_schema_fields = []
        properties = (self._class_schema or {}).get(SCHEMA_PROPERTIES) or {}
        if not isinstance(properties, dict) or not properties:
            return extracted_fields

        allowed = set(properties.keys())
        dropped = [k for k in extracted_fields if k not in allowed]
        if not dropped:
            return extracted_fields

        self._off_schema_fields = dropped
        logger.warning(
            "Simple extraction returned %d off-schema field(s) not defined in the "
            "'%s' class schema; dropping them: %s",
            len(dropped),
            self._class_label,
            ", ".join(dropped),
        )
        return {k: v for k, v in extracted_fields.items() if k in allowed}

    def _build_wire_schema(
        self, class_schema: dict[str, Any], class_label: str
    ) -> tuple[dict[str, Any], bool]:
        """Return ``(schema_to_send_to_the_model, probe_added)``.

        The only difference from the effective class schema is the multi-instance
        detection probe (#753) — one auxiliary integer property asking how many
        separate documents of this class the pages contain. Added to a COPY, so
        the off-schema filter, the validator and every downstream stage still see
        exactly the declared fields.

        Skipped when detection is disabled, and when Advanced (agentic)
        extraction is in use — a documented gap, not a design decision, tracked in
        GitHub #772. Two things make agentic more than a one-line change:

        * The agentic path is driven by a generated **Pydantic model**
          (``create_pydantic_model_from_json_schema(self._class_schema, …)`` →
          ``structured_output_async(data_format=…)``), not by a JSON-Schema dict,
          so a property added to the schema copy here never reaches the wire. The
          probe would have to be generated onto the model and stripped off the
          validated object.
        * Agentic **shards by page range** when a section is long enough, so each
          shard would answer the count for its own pages. Summing double-counts a
          document that spans a shard boundary and taking the max under-counts, so
          the reconciliation rule is a real decision rather than a detail.

        Simple extraction — the default, and the mode in which the silent loss was
        reported — is covered on both its prompt and forced-tool paths. Note the
        #715 wrapper is unaffected either way: it lives in ``_get_class_schema``,
        which both modes share, so a flagged class extracts every record in
        agentic mode too. It is only the detector that is missing there.
        """
        if not self.config.extraction.multi_instance_detection.enabled:
            return class_schema, False
        if self.config.extraction.agentic.enabled:
            return class_schema, False

        from idp_common.extraction.instance_probe import augment_schema_with_probe

        wire, added = augment_schema_with_probe(
            class_schema,
            class_label,
            self.config.extraction.multi_instance_detection.question,
        )
        return (wire if isinstance(wire, dict) else class_schema), added

    def _read_instance_probe(self, extracted_fields: Any) -> int | None:
        """Pop the multi-instance probe from a parsed result and return its value.

        Always pops when present — even when detection was not requested for this
        section — so the auxiliary field can never reach ``inference_result``,
        where it would be reported as an off-schema field, scored by assessment,
        written to a reporting column and compared against a ground-truth
        baseline that has no such key.
        """
        from idp_common.extraction.instance_probe import pop_probe_value

        value = pop_probe_value(extracted_fields)
        if value is not None:
            logger.info(
                "Multi-instance probe: model reports %d '%s' document(s) in this "
                "section",
                value,
                self._class_label,
            )
        return value

    def _strip_probe_from_instances(
        self,
        extracted_fields: Any,
        recovered_instances: list[dict[str, Any]] | None,
    ) -> int | None:
        """Remove the detection probe from every per-instance record, in place.

        A multi-instance response carries the auxiliary count inside EVERY
        element, and the records can end up in either of two containers — the
        synthesized ``instances`` list, or ``recovered_instances`` for an
        unflagged class — so both are swept. Called after
        ``_adapt_to_instances_wrapper``, because that is what decides which
        container holds them.

        Necessary because ``_filter_extracted_to_schema`` only filters TOP-LEVEL
        keys: a probe left inside a record would reach ``inference_result``, be
        scored by assessment, become a reporting column, and be diffed against a
        baseline that has no such key.

        Returns the LARGEST count found inside a record, or None. Normally the
        caller already has the value from the top-level pop — but a model that
        answers with the count only inside each record and never at the top level
        would otherwise leave ``instance_probe`` unset and fire no warning at all,
        so the value is not thrown away just because it arrived in an unexpected
        place.
        """
        from idp_common.schema.multi_instance import INSTANCES_KEY

        containers: list[Any] = []
        if isinstance(extracted_fields, dict):
            containers.append(extracted_fields.get(INSTANCES_KEY))
        containers.append(recovered_instances)

        best: int | None = None
        for container in containers:
            if not isinstance(container, list):
                continue
            for record in container:
                value = self._read_instance_probe(record)
                if value is not None and (best is None or value > best):
                    best = value
        return best

    def _adapt_to_instances_wrapper(
        self,
        extracted_fields: Any,
        recovered_instances: list[dict[str, Any]] | None,
    ) -> tuple[Any, list[dict[str, Any]] | None]:
        """Rescue a multi-instance response that ignored the wrapper (#715).

        For a class in Synthesize mode the requested shape is
        ``{"instances": [ … ]}``, but a model can answer in either of two other
        shapes, and BOTH are total data loss without this:

        - **A bare top-level array** of records. ``_normalize_list_result``
          reduces it to element 0, and the off-schema filter — whose only allowed
          top-level key is now ``instances`` — then deletes even that, emitting
          ``{}``. Every record lost, from a response that contained all of them.
        - **A single flat record** (the shape the class had before the flag was
          set). Same ending: the filter deletes every key and emits ``{}``.

        Both are re-expressed as the wrapper. Deliberately forgiving rather than
        strict: the alternative to accepting a recoverable shape here is throwing
        the section's data away, and this feature exists precisely to stop records
        being discarded silently.

        No-op for an unflagged class, and for a response that already used the
        wrapper.
        """
        from idp_common.schema.multi_instance import INSTANCES_KEY, is_wrapped

        if not is_wrapped(self._class_schema):
            return extracted_fields, recovered_instances

        # Already correct.
        if isinstance(extracted_fields, dict) and isinstance(
            extracted_fields.get(INSTANCES_KEY), list
        ):
            return extracted_fields, recovered_instances

        # Shape 1: a bare array, already split out by _normalize_list_result.
        if recovered_instances:
            logger.warning(
                "Class '%s' is multi-instance but the model returned a bare "
                "array of %d record(s) instead of the '%s' wrapper; adopting the "
                "array as the instance list",
                self._class_label,
                len(recovered_instances),
                INSTANCES_KEY,
            )
            return {INSTANCES_KEY: list(recovered_instances)}, None

        # Shape 2: a single flat record. Only when it actually looks like one —
        # an error/raw_output sentinel must stay a parse failure.
        if isinstance(extracted_fields, dict) and extracted_fields:
            record_properties = (self._class_schema.get(SCHEMA_PROPERTIES) or {}).get(
                INSTANCES_KEY, {}
            ).get("items", {}).get(SCHEMA_PROPERTIES) or {}
            if record_properties and set(extracted_fields) & set(record_properties):
                logger.warning(
                    "Class '%s' is multi-instance but the model returned a single "
                    "flat record instead of the '%s' wrapper; adopting it as one "
                    "instance",
                    self._class_label,
                    INSTANCES_KEY,
                )
                return {INSTANCES_KEY: [extracted_fields]}, None

        return extracted_fields, recovered_instances

    def _resolve_instance_reporting(
        self, result: ExtractionResult
    ) -> tuple[int, dict[str, Any]]:
        """Decide the section's instance count and how it was arrived at.

        Returns ``(instance_count, metadata_fragment)``. Split out of
        ``_save_results`` because it is the whole of the multi-instance
        reporting contract and needs to be testable without S3.

        ``instance_source`` labels, in resolution order:

        - ``single`` — the ordinary case: the model returned one object, so the
          count is 1 and nothing was recovered. (Labelling this "recovered", as
          an earlier version did once the count started reporting 1 for a plain
          object, is simply untrue in the audit trail and would send someone
          reading metadata hunting for records that never existed.)
        - ``recovered`` — the model returned a LIST where the schema describes one
          document, so the extra documents were unexpected and only the first is
          scored. Worth a warning (``extraction_multi_instance_detected``).
        - ``declared`` — the class named its own instance axis
          (``x-aws-idp-instance-array``, Designate mode) or was transformed into
          a List-of-Class shape (``x-aws-idp-multi-instance``, Synthesize mode).
          Several documents are exactly what the config asked for and every one
          is extracted and scored, so there is nothing to warn about.
        - ``suspected`` — #753. The model's own answer to "how many separate
          documents of this class are in these pages" EXCEEDS the number of
          records in the result, so records were silently lost. The model's count
          becomes the section's ``instance_count`` (it is the honest answer to
          "how many documents are in this section") and the number actually
          extracted is recorded beside it as ``instance_extracted_count``, so the
          metadata cannot be misread as "3 records were extracted".

          Deliberately NOT limited to unflagged classes: a class in Designate or
          Synthesize mode whose array came back with fewer records than the model
          says are present is under-extracting, which is the same data loss.
        """
        instance_count = result.instance_count
        if not instance_count:
            instance_source: str | None = None
        elif result.recovered_instances:
            instance_source = "recovered"
        else:
            instance_source = "single"

        meta: dict[str, Any] = {}

        designated = self._designated_instance_count(result.extracted_fields)
        if designated is not None:
            instance_count = designated
            instance_source = "declared"

        probe = result.instance_probe
        if probe is not None:
            meta["instance_probe"] = probe
        if probe is not None and probe > max(instance_count, 1):
            meta["instance_extracted_count"] = instance_count
            instance_count = probe
            instance_source = "suspected"
            try:
                metrics.put_metric("MultiInstanceSectionsSuspected", 1)
            except Exception as e:  # noqa: BLE001 - telemetry must never fail a doc
                logger.debug("Could not publish suspected-instance metric: %s", e)

        if instance_count:
            meta["instance_count"] = instance_count
            meta["instance_source"] = instance_source
        return instance_count, meta

    def _designated_instance_count(self, extracted_fields: Any) -> int | None:
        """Count instances via the class's declared instance axis, or None.

        A class whose schema is already modelled as a *packet* — one top-level
        array of objects, one element per record — can name that array with
        ``x-aws-idp-instance-array``. This is the cheap half of multi-instance
        support (plan D3, "Designate mode"): it performs **no** schema transform
        and changes **no** output shape, so it costs nothing downstream. It only
        tells the pipeline which existing array means "one document per element",
        which is enough to populate ``Section.instance_count`` and light the UI
        badge.

        It exists so configs that already solved multi-record packets by hand —
        the workaround validated in GitHub #565 — get the count without
        restructuring their schema or migrating their evaluation baselines.

        Returns None when the class declares no instance axis (the overwhelmingly
        common case), or when the declared property is missing or not a list in
        this particular result. Deliberately forgiving at runtime: a
        misconfiguration should cost a log line, not an extraction. The shape of
        the declaration itself is checked at config-validate time.
        """
        prop = (self._class_schema or {}).get(X_AWS_IDP_INSTANCE_ARRAY)
        if not prop:
            return None
        if not isinstance(prop, str):
            logger.warning(
                "%s on class '%s' must be a property name (got %s); ignoring",
                X_AWS_IDP_INSTANCE_ARRAY,
                self._class_label,
                type(prop).__name__,
            )
            return None
        if not isinstance(extracted_fields, dict):
            return None
        if prop not in extracted_fields:
            logger.warning(
                "Class '%s' declares instance array '%s' but the extraction "
                "result has no such field; instance count not determined",
                self._class_label,
                prop,
            )
            return None
        value = extracted_fields[prop]
        if value is None:
            # Extracted as null — genuinely zero records found, not a config
            # error. 0 reads back as "undetermined", which is the honest answer.
            return 0
        if not isinstance(value, list):
            logger.warning(
                "Class '%s' declares instance array '%s' but the extracted value "
                "is %s, not a list; instance count not determined",
                self._class_label,
                prop,
                type(value).__name__,
            )
            return None
        return len(value)

    @staticmethod
    def _normalize_list_result(
        parsed: Any, *, context: str
    ) -> tuple[dict[str, Any], bool, int, list[dict[str, Any]] | None]:
        """Normalize a top-level JSON array response into a single-object result.

        The class schema describes ONE document, so a well-behaved model returns
        an object. When it returns an array instead, that is nearly always
        because the section actually contains several documents of the same
        class — a section that classification failed to split (GitHub #565).

        Returns ``(fields, parsing_succeeded, instance_count, recovered)``.

        Behaviour by array length:

        - **1** — unwrap to the single object. Indistinguishable from a correct
          response; ``instance_count = 1``.
        - **>1** — **keep every instance.** ``fields`` becomes the FIRST one so
          the output shape is identical to a normal extraction and no downstream
          consumer changes, ``recovered`` carries all of them, and
          ``instance_count = N``. Parsing is reported as SUCCEEDED.

          This replaces the previous behaviour, which stored the array under a
          ``raw_array`` key that **nothing in the codebase ever read**, marked
          the section FAILED, and then reported the misleading
          ``extraction_sparse`` issue ("0/N schema fields populated"). Every
          record had been extracted correctly and all of them were discarded —
          the worst outcome available. The caller pairs this with a
          ``extraction_multi_instance_detected`` warning and a section
          ``instance_count``, so the situation is visible and reviewable instead
          of either silent or fatal.

          Note the asymmetry this does NOT fix: when the model returns a single
          object for a two-document section, only the first record exists in the
          response and there is nothing here to recover. That case needs
          multi-instance extraction (an opt-in per-class list schema), not
          response normalization.
        - **0**, or elements that are not objects — genuinely unusable; reported
          as a parse failure, unchanged.
        """
        if not isinstance(parsed, list):
            # A plain object is one document, and we know that — so report 1
            # rather than 0. 0 is reserved for "not determined" (an extraction
            # that failed before producing a result, or a section written by
            # older code), which the UI renders as "-" instead of a count.
            return parsed, True, 1, None

        if len(parsed) == 1 and isinstance(parsed[0], dict):
            logger.warning(
                "LLM returned single-element array instead of object, unwrapping",
                extra={"original_type": "list", "element_count": 1, "context": context},
            )
            return parsed[0], True, 1, None

        if len(parsed) == 0:
            logger.error(
                "LLM returned empty array when single object expected",
                extra={"element_count": 0, "context": context},
            )
            return (
                {"error": "Received empty array instead of single object"},
                False,
                0,
                None,
            )

        instances = [item for item in parsed if isinstance(item, dict)]
        if len(instances) != len(parsed):
            logger.error(
                "LLM returned an array containing non-object elements; cannot "
                "interpret it as multiple document instances",
                extra={
                    "element_count": len(parsed),
                    "object_count": len(instances),
                    "context": context,
                },
            )
            return (
                {
                    "error": (
                        f"Received array with {len(parsed)} elements, "
                        f"{len(parsed) - len(instances)} of which were not objects"
                    )
                },
                False,
                0,
                None,
            )

        logger.warning(
            "LLM returned %d objects for a single-document schema — treating the "
            "section as multi-instance and preserving every instance. The first "
            "is reported as the section's inference_result; all %d are recorded "
            "in metadata.recovered_instances.",
            len(instances),
            len(instances),
            extra={"element_count": len(instances), "context": context},
        )
        return instances[0], True, len(instances), instances

    def _build_extraction_issues(
        self,
        *,
        extracted_fields: dict[str, Any],
        metadata: dict[str, Any],
        section_id: str | None,
    ) -> list[Any]:
        """Detect extraction shortfalls and emit structured ProcessingIssues.

        Runs for BOTH traditional (simple) and agentic extraction so an
        under-produced extraction is never silently reported as SUCCESS. Two
        signals:

        - **Empty schema-defined list fields** (``extraction_incomplete``,
          warning): a field the schema declares as an array came back empty. This
          is the classic Simple-mode large-table failure (``PortfolioDetail: []``
          on a 25-page statement). When extraction is NOT already agentic, the
          message recommends toggling to Advanced (agentic) extraction, whose
          deterministic table parser is built for exactly this.
        - **Below-threshold population** (``extraction_sparse``, info): reuses the
          existing ``population_check`` heuristic when present.

        Best-effort and advisory: a genuinely empty section (no such data in the
        document) will also flag, so these are warning/info, never hard errors.
        """
        from idp_common.config.schema_constants import (
            SCHEMA_PROPERTIES,
            SCHEMA_TYPE,
            TYPE_ARRAY,
        )
        from idp_common.models import ProcessingIssue

        issues: list[Any] = []
        is_agentic = self.config.extraction.agentic.enabled
        properties = (self._class_schema or {}).get(SCHEMA_PROPERTIES, {}) or {}

        # 1) Empty OR ABSENT schema-defined array fields.
        #
        # "Absent" matters as much as "empty" and used to be missed entirely: the
        # `isinstance(..., list)` guard meant a response that simply OMITTED a
        # declared list produced no issue at all. Measured on an 800-row
        # transaction document in integrated-confidence mode, the response came
        # back with the `Transactions` key **not present** — status COMPLETED,
        # scalar accuracy 1.000, and not one warning anywhere. A declared list
        # that the model declined to emit is the single largest silent-data-loss
        # shape this pipeline has.
        array_fields = [
            name
            for name, spec in properties.items()
            if isinstance(spec, dict) and spec.get(SCHEMA_TYPE) == TYPE_ARRAY
        ]
        empty_lists = [
            name
            for name in array_fields
            if name not in extracted_fields
            or extracted_fields.get(name) is None
            or (
                isinstance(extracted_fields.get(name), list)
                and len(extracted_fields[name]) == 0
            )
        ]
        if empty_lists:
            fields_str = ", ".join(empty_lists)
            rec = (
                " This document has large/tabular fields; Advanced (agentic) "
                "extraction with table parsing is recommended for it."
                if not is_agentic
                else ""
            )
            issues.append(
                ProcessingIssue(
                    stage="extraction",
                    severity="warning",
                    code="extraction_incomplete",
                    message=(
                        f"Extraction returned no rows for list field(s): "
                        f"{fields_str}. If the document contains this data, it was "
                        f"not captured. Note that scalar fields can still score "
                        f"perfectly while a whole list is missing, so no other "
                        f"signal will flag this.{rec}"
                    ),
                    root_cause=(
                        f"{'agentic' if is_agentic else 'traditional'} extraction "
                        f"with model "
                        f"{self._pending_extraction_model or self.config.extraction.model}; "
                        f"empty array field(s): {fields_str}"
                    ),
                    section_id=section_id,
                    details={"empty_list_fields": empty_lists, "agentic": is_agentic},
                )
            )

        # 2) Off-schema fields dropped by the simple-mode schema-compliance filter.
        # The model returned attributes the class schema does not define; they were
        # dropped (mirroring agentic Pydantic validation) so they can't corrupt
        # downstream assessment. Info severity — the data was never valid for this
        # class — but surfaced so a systematic prompt/schema mismatch is visible.
        off_schema = list(getattr(self, "_off_schema_fields", []) or [])
        if off_schema:
            fields_str = ", ".join(off_schema)
            rec = (
                " Advanced (agentic) extraction enforces this automatically."
                if not is_agentic
                else ""
            )
            issues.append(
                ProcessingIssue(
                    stage="extraction",
                    severity="info",
                    code="extraction_off_schema_fields",
                    message=(
                        f"Extraction returned {len(off_schema)} field(s) not defined "
                        f"in the class schema; they were dropped before assessment: "
                        f"{fields_str}. This usually indicates a prompt/schema "
                        f"mismatch or cross-class hallucination.{rec}"
                    ),
                    root_cause=(
                        f"{'agentic' if is_agentic else 'traditional'} extraction "
                        f"with model "
                        f"{self._pending_extraction_model or self.config.extraction.model}; "
                        f"off-schema field(s): {fields_str}"
                    ),
                    section_id=section_id,
                    details={"off_schema_fields": off_schema, "agentic": is_agentic},
                )
            )

        # 2b) The section holds MORE THAN ONE document of this class.
        #
        # Classification produced one section, but extraction found several
        # distinct records in it (GitHub #565). The records are all preserved in
        # metadata.recovered_instances and inference_result holds the first, so
        # nothing is lost — but only the first is scored, reviewed and reported,
        # so this must be visible rather than quietly accepted.
        #
        # A warning, not an error: the extracted data is real and usable, and
        # failing the section would discard it. The point is to stop the loss
        # being silent, which is exactly what made this hard to find — the old
        # code marked the section FAILED and reported "0/N fields populated",
        # which described neither what happened nor what to do about it.
        # Only when the extra documents were UNEXPECTED. A class that declared its
        # own instance axis (x-aws-idp-instance-array) is meant to hold several
        # records, and every one of them is extracted and scored, so warning about
        # it would be pure noise on a correctly-configured class.
        # Only "recovered" warrants the warning. "declared" means the class asked
        # for several records and every one is extracted and scored; "single"
        # means one object, which the > 1 check already excludes. Naming both
        # exclusions is clearer than excluding "declared" alone and leaving
        # "single" to fall through by accident. An ABSENT source is treated as
        # recovered, so metadata written before the label existed still warns.
        instance_count = int(metadata.get("instance_count") or 0)
        instance_source = metadata.get("instance_source")
        if instance_count > 1 and instance_source not in (
            "declared",
            "single",
            "suspected",
        ):
            issues.append(
                ProcessingIssue(
                    stage="extraction",
                    severity="warning",
                    code="extraction_multi_instance_detected",
                    message=(
                        f"This section contains {instance_count} separate "
                        f"'{self._class_label}' documents. All {instance_count} were "
                        f"extracted, but only the first is shown as the section "
                        f"result — review the section and consider splitting it."
                    ),
                    root_cause=(
                        "classification produced one section for several "
                        "consecutive documents of the same class, so the model "
                        "returned a list where the schema describes one document; "
                        "every instance is preserved under "
                        "metadata.recovered_instances"
                    ),
                    section_id=section_id,
                    details={
                        "instance_count": instance_count,
                        "agentic": is_agentic,
                    },
                )
            )

        # 2c) The section holds MORE documents than the result contains (#753).
        #
        # THE silent case, and the common one. Classification produced one section
        # over several consecutive records of this class; the schema describes one
        # document, so the model answered with one object and records 2..N are
        # simply absent. Nothing reported it: SUCCESS, COMPLETED,
        # ProcessingIssueCount 0, instance_count 1. #687's array-recovery branch
        # cannot help — it fires only when the model returned a list, which is the
        # branch where nothing had been lost yet.
        #
        # The evidence is the model's own answer to the auxiliary count asked in
        # the same inference. A warning, not an error: the extracted record is
        # real and usable, and failing the section would discard it. The point is
        # that the loss stops being invisible, so the section can be reviewed or
        # routed instead of quietly shipping one record out of three.
        if instance_source == "suspected" and instance_count > 1:
            extracted_n = int(metadata.get("instance_extracted_count") or 1)
            missing = max(instance_count - extracted_n, 0)
            issues.append(
                ProcessingIssue(
                    stage="extraction",
                    severity="warning",
                    code="extraction_multi_instance_suspected",
                    message=(
                        f"These pages appear to contain {instance_count} separate "
                        f"'{self._class_label}' documents, but extraction returned "
                        f"{extracted_n}. {missing} document(s) are NOT in the "
                        f"result. Review the section, then either split it "
                        f"(classification sectionSplitting) or turn on "
                        f"multi-instance extraction for this class so every "
                        f"document is extracted."
                    ),
                    root_cause=(
                        f"the extraction model reported {instance_count} document "
                        f"instance(s) in this section while returning "
                        f"{extracted_n}; the class schema describes a single "
                        f"document, so the additional records had nowhere to go "
                        f"(model "
                        f"{self._pending_extraction_model or self.config.extraction.model})"
                    ),
                    section_id=section_id,
                    details={
                        "instance_count": instance_count,
                        "extracted_instance_count": extracted_n,
                        "detection": "model_self_report",
                        "agentic": is_agentic,
                    },
                )
            )

        # 3) Below-threshold population heuristic (agentic populates this today).
        pop = metadata.get("population_check")
        if isinstance(pop, dict) and pop.get("below_threshold"):
            issues.append(
                ProcessingIssue(
                    stage="extraction",
                    severity="info",
                    code="extraction_sparse",
                    message=(
                        f"Only {pop.get('fields_populated')}/{pop.get('fields_defined')} "
                        f"schema fields were populated — possible silent extraction "
                        f"loss."
                    ),
                    root_cause=(
                        f"population ratio {pop.get('population_ratio')} below "
                        f"threshold {pop.get('threshold')}"
                    ),
                    section_id=section_id,
                    details={"empty_fields": pop.get("empty_fields", [])[:20]},
                )
            )

        # 4) A list came back SHORTER than its declared `minItems`.
        #
        # This is the one unambiguous, mode-agnostic truncation signal available
        # without ground truth: the config author asserted a floor and the
        # extraction is under it. Previously the `minItems` check ran only on the
        # agentic path, so simple mode — which is where the silent completeness
        # cliff actually lives (recall 1.000 through ~800 rows, then 0.199 at
        # 1,200 and 0.009 at 3,200) — had no completeness check of any kind.
        #
        # Deliberately a *warning*, not a failure: the partial rows are real data
        # and are more useful than a discarded document. What matters is that it
        # stops being silent, because neither status nor cost reveals it — a
        # truncated run is actually CHEAPER than a complete one.
        short_lists: list[dict[str, Any]] = []
        for name in array_fields:
            value = extracted_fields.get(name)
            if not isinstance(value, list) or not value:
                continue  # empty/absent already reported by (1)
            raw_min = (properties.get(name) or {}).get("minItems")
            if raw_min in (None, ""):
                continue
            try:
                # minItems can arrive as a string after a config round-trip.
                min_items = int(raw_min)
            except (TypeError, ValueError):
                continue
            if min_items > 0 and len(value) < min_items:
                short_lists.append(
                    {"field": name, "rows": len(value), "min_items": min_items}
                )

        if short_lists:
            detail = ", ".join(
                f"{s['field']} ({s['rows']} of at least {s['min_items']})"
                for s in short_lists
            )
            rec = (
                " Advanced (agentic) extraction shards large lists and is "
                "recommended for documents this size."
                if not is_agentic
                else ""
            )
            issues.append(
                ProcessingIssue(
                    stage="extraction",
                    severity="warning",
                    code="extraction_list_truncated",
                    message=(
                        f"List field(s) returned fewer rows than the schema's "
                        f"minItems requires: {detail}. The extraction is very "
                        f"likely truncated — the returned rows are usable but "
                        f"incomplete.{rec}"
                    ),
                    root_cause=(
                        f"{'agentic' if is_agentic else 'traditional'} extraction "
                        f"with model "
                        f"{self._pending_extraction_model or self.config.extraction.model}; "
                        f"minItems violated on: {detail}"
                    ),
                    section_id=section_id,
                    details={"short_lists": short_lists, "agentic": is_agentic},
                )
            )

        # 5) The extraction result still violates the class JSON Schema.
        #
        # This is what makes `fail_action: warn` — the default, and the whole
        # justification for turning validation ON by default — actually deliver
        # something. Without it the outcome landed only in
        # `metadata.validation` inside the section result JSON: no section status
        # icon, nothing in the document list's Processing Issues column, nothing
        # in the Processing Report. "Free and makes an otherwise-silent violation
        # visible" was only half true; the violation was still silent.
        #
        # Applies to BOTH modes (agentic writes the same metadata block). Severity
        # follows fail_action, because that is what the deployment decided the
        # outcome MEANS: `reject` already failed the section, so `error`; `warn`
        # and `escalate` leave the data in place, so `warning`.
        validation = metadata.get("validation")
        if isinstance(validation, dict) and validation.get("valid") is False:
            failed = [str(f) for f in (validation.get("failed_fields") or [])]
            error_count = int(validation.get("error_count") or 0)
            fail_action = str(validation.get("fail_action") or "warn")
            escalated = bool(validation.get("escalated"))
            named = ", ".join(failed[:8]) or "unnamed fields"
            if len(failed) > 8:
                named += f", +{len(failed) - 8} more"
            if fail_action == "reject":
                consequence = (
                    " The section is marked FAILED because Fail Action is 'reject'."
                )
            elif escalated:
                consequence = (
                    " Re-extraction with the escalation model did not resolve it;"
                    " the values are stored as extracted."
                )
            else:
                consequence = " The values are stored as extracted."
            issues.append(
                ProcessingIssue(
                    stage="extraction",
                    severity="error" if fail_action == "reject" else "warning",
                    code="extraction_validation_failed",
                    message=(
                        f"{error_count} schema violation(s) remain after "
                        f"extraction, affecting: {named}.{consequence}"
                    ),
                    root_cause=(
                        f"{'agentic' if is_agentic else 'traditional'} extraction "
                        f"with model "
                        f"{self._pending_extraction_model or self.config.extraction.model}; "
                        f"full-schema validation failed (fail_action={fail_action})"
                    ),
                    section_id=section_id,
                    details={
                        "failed_fields": failed[:20],
                        "error_count": error_count,
                        "fail_action": fail_action,
                        "escalated": escalated,
                        # The first few concrete messages, so the issue is
                        # actionable without opening the section result JSON.
                        "errors": (validation.get("errors") or [])[:5],
                        "agentic": is_agentic,
                    },
                )
            )
        return issues

    def _build_processing_flow(
        self,
        metadata: dict[str, Any],
        extraction_method: str,
        tool_used: bool,
    ) -> dict[str, Any]:
        """Build a normalized, render-agnostic description of the processing path.

        Returns ``{"stages": [ {key,label,detail,status,fanout?}, ... ],
        "recovery": {...}|None}``. ``status`` is one of ``ok`` | ``info`` |
        ``warning`` | ``skipped``. The UI renders ``stages`` as a left-to-right
        flow graph and ``recovery`` as an explicit "what failed and was recovered"
        callout; the text report renders the same data as lines. Populated for BOTH
        simple and advanced so there is always a flow to show.
        """
        is_agentic = extraction_method == "agentic"
        sizing = metadata.get("sizing_plan") or {}
        geometry_mode = (
            sizing.get("geometry_mode") or self.config.extraction.geometry.mode or ""
        ).lower()
        conf_cfg = self.config.extraction.confidence
        conf_enabled = bool(conf_cfg.enabled)
        conf_mode = (conf_cfg.mode or "").lower()  # separate | integrated | off
        split = metadata.get("assessment_batch_split_stats") or {}
        decision = metadata.get("tool_usage_decision") or {}
        validation = metadata.get("validation") or {}

        stages: list[dict[str, Any]] = []

        # 1) OCR
        ocr_info = metadata.get("ocr_analysis") or {}
        tables_seen = ocr_info.get("tables_detected", 0)
        stages.append(
            {
                "key": "ocr",
                "label": "OCR",
                "detail": (
                    f"{tables_seen} markdown table(s) in text"
                    if tables_seen
                    else "no markdown tables"
                ),
                "status": "ok",
            }
        )
        # 2) Classify
        stages.append(
            {"key": "classify", "label": "Classify", "detail": "", "status": "ok"}
        )

        # 3) Extract — mode + sharding fan-out.
        num_shards = 0
        shard_trace = metadata.get("shard_trace")
        if isinstance(shard_trace, dict):
            num_shards = shard_trace.get("num_shards", 0) or 0
        if is_agentic:
            if num_shards and num_shards > 1:
                extract_detail = f"agentic · {num_shards} shards (concurrent)"
                fanout = num_shards
            else:
                extract_detail = "agentic · single agent"
                fanout = 0
        else:
            extract_detail = "single LLM pass"
            fanout = 0
        stages.append(
            {
                "key": "extract",
                "label": "Extract",
                "detail": extract_detail,
                "status": "ok",
                "fanout": fanout,
                "model": metadata.get("extraction_model"),
            }
        )

        # 4) Table tool (only meaningful for agentic) — used / declined / unavailable.
        if is_agentic and (decision or "table_parsing_tool_used" in metadata):
            if tool_used:
                t_status, t_detail = "ok", "deterministic parser used"
            elif not decision.get("tool_enabled", True):
                t_status, t_detail = "skipped", "disabled in config"
            elif not decision.get("ocr_had_markdown_tables", True):
                t_status, t_detail = "skipped", "no markdown tables in OCR"
            elif decision.get("expected") and not decision.get("actual"):
                t_status, t_detail = "warning", "available but agent declined"
            else:
                t_status, t_detail = "skipped", "not needed"
            stages.append(
                {
                    "key": "table_tool",
                    "label": "Table tool",
                    "detail": t_detail,
                    "status": t_status,
                }
            )

        # 5) Model escalation (extraction schema-validation escalation), if any.
        if validation.get("escalated"):
            stages.append(
                {
                    "key": "escalation",
                    "label": "Escalation",
                    "detail": (
                        f"{validation.get('escalation_scope') or 'fields'} → "
                        f"{validation.get('escalation_model') or 'stronger model'}"
                        + (
                            " (resolved)"
                            if validation.get("resolved_by_escalation")
                            else ""
                        )
                    ),
                    "status": "warning",
                }
            )

        # 6) Confidence — source (deterministic OCR-inline vs LLM), batching/concurrency.
        if not conf_enabled or conf_mode == "off":
            stages.append(
                {
                    "key": "confidence",
                    "label": "Confidence",
                    "detail": "disabled",
                    "status": "skipped",
                }
            )
        else:
            batch_count = split.get("batch_count")
            concurrent = split.get("concurrent_batches")
            if conf_mode == "integrated":
                c_detail = "integrated (inline with extraction)"
                fan = 0
            elif batch_count and batch_count > 1:
                c_detail = f"{conf_cfg.model or 'model'} · {batch_count} batches"
                if concurrent and concurrent > 1:
                    c_detail += f" · {concurrent}× concurrent"
                fan = concurrent if (concurrent and concurrent > 1) else 0
            else:
                c_detail = f"{conf_cfg.model or 'model'} · 1 pass"
                fan = 0
            stages.append(
                {
                    "key": "confidence",
                    "label": "Confidence",
                    "detail": c_detail,
                    "status": "ok",
                    "fanout": fan,
                    "model": conf_cfg.model,
                }
            )

            # 7) Geometry — OCR-grounded vs LLM-estimated box.
            if geometry_mode in ("ocr_only", "llm_grounded"):
                g_detail = (
                    "OCR-grounded (deterministic)"
                    if geometry_mode == "ocr_only"
                    else "LLM box refined by OCR"
                )
            elif geometry_mode == "llm":
                g_detail = "LLM-estimated box"
            else:
                g_detail = "none"
            stages.append(
                {
                    "key": "geometry",
                    "label": "Geometry",
                    "detail": g_detail,
                    "status": "skipped" if geometry_mode in ("", "off") else "ok",
                }
            )

        # Recovery summary: what the confidence self-healing had to fix. Only
        # present when there was truncation/splitting/escalation activity, so an
        # "auto recovered" status can point at EXACTLY what failed and was rescued.
        recovery = None
        if split:
            recovered = (split.get("rows_recovered_by_retry", 0) or 0) + (
                split.get("rows_recovered_by_escalation", 0) or 0
            )
            truncated = split.get("truncated_calls", 0) or 0
            unrecoverable = split.get("unrecoverable_rows", 0) or 0
            if truncated or recovered or unrecoverable:
                recovery = {
                    "truncated_calls": truncated,
                    "splits": split.get("splits", 0) or 0,
                    "rows_recovered_by_retry": split.get("rows_recovered_by_retry", 0)
                    or 0,
                    "rows_recovered_by_escalation": split.get(
                        "rows_recovered_by_escalation", 0
                    )
                    or 0,
                    "escalation_model": split.get("escalation_model"),
                    "unrecoverable_rows": unrecoverable,
                    "deadline_reached": bool(split.get("deadline_reached")),
                }

        return {"stages": stages, "recovery": recovery}

    def _generate_processing_report(self, metadata: dict[str, Any]) -> str:
        """Generate user-friendly processing report."""

        # Status reflects BOTH parse success AND any structured processing issues,
        # so an extraction that parsed but under-produced (e.g. an empty large
        # table) is not misreported as a clean SUCCESS.
        issues_for_status = metadata.get("processing_issues") or []
        severities = {str(i.get("severity", "")).lower() for i in issues_for_status}
        if not metadata.get("parsing_succeeded"):
            status_line = "FAILED"
        elif "error" in severities:
            status_line = "COMPLETED WITH ERRORS"
        elif "warning" in severities:
            status_line = "COMPLETED WITH WARNINGS"
        else:
            status_line = "SUCCESS"

        report_lines = [
            "=== EXTRACTION PROCESSING REPORT ===",
            "",
            f"Extraction Method: {metadata.get('extraction_method', 'N/A').upper()}",
            f"Processing Time: {metadata.get('extraction_time_seconds', 0):.1f} seconds",
            f"Status: {status_line}",
            "",
        ]

        # Processing flow — systematic, single-line-per-stage depiction of the whole
        # path (mirrors the UI graph): OCR → Classify → Extract(→shards) →
        # [Table tool] → [Escalation] → Confidence(→batches) → Geometry.
        flow = metadata.get("processing_flow") or {}
        stages = flow.get("stages") or []
        if stages:
            icon = {"ok": "✓", "info": "•", "warning": "⚠", "skipped": "–"}
            report_lines.append("Processing Flow:")
            for st in stages:
                mark = icon.get(str(st.get("status", "ok")), "•")
                detail = st.get("detail")
                report_lines.append(
                    f"  {mark} {st.get('label', '?')}"
                    + (f": {detail}" if detail else "")
                )
            # Explicit recovery callout — WHAT failed and was recovered by retry.
            rec = flow.get("recovery")
            if rec:
                recovered = (rec.get("rows_recovered_by_retry", 0)) + (
                    rec.get("rows_recovered_by_escalation", 0)
                )
                report_lines.append("")
                report_lines.append("  Confidence self-healing (auto-recovery):")
                report_lines.append(
                    f"    - Truncated confidence calls: {rec.get('truncated_calls', 0)} "
                    f"(batches split {rec.get('splits', 0)}×)"
                )
                report_lines.append(
                    f"    - Rows recovered by same-model retry: "
                    f"{rec.get('rows_recovered_by_retry', 0)}"
                )
                if rec.get("rows_recovered_by_escalation", 0):
                    report_lines.append(
                        f"    - Rows recovered by ESCALATION to "
                        f"{rec.get('escalation_model') or 'stronger model'}: "
                        f"{rec.get('rows_recovered_by_escalation', 0)}"
                    )
                report_lines.append(f"    - Total rows auto-recovered: {recovered}")
                if rec.get("unrecoverable_rows", 0):
                    report_lines.append(
                        f"    - ⚠ Rows still unscored after recovery: "
                        f"{rec.get('unrecoverable_rows', 0)}"
                    )
                if rec.get("deadline_reached"):
                    report_lines.append(
                        "    - ⚠ Stopped early on the Lambda wall-clock guard"
                    )
            report_lines.append("")

        # Schema analysis
        if "schema_analysis" in metadata:
            schema_info = metadata["schema_analysis"]
            report_lines.extend(
                [
                    "Schema Analysis:",
                    f"  - Large array fields detected: {len(schema_info.get('large_array_fields', []))}",
                    f"  - Maximum minItems constraint: {schema_info.get('max_min_items', 0)}",
                    f"  - Tool usage recommendation: {schema_info.get('recommendation_strength', 'N/A')}",
                    "",
                ]
            )

        # OCR analysis
        if "ocr_analysis" in metadata:
            ocr_info = metadata["ocr_analysis"]
            tables_detected = ocr_info.get("tables_detected", 0)
            # Make the Markdown-table prerequisite explicit: the deterministic table
            # parser can ONLY act on Markdown pipe-tables in the OCR text, and this
            # pre-flight scan is what detects them.
            if tables_detected > 0:
                md_line = (
                    f"  - Markdown tables in OCR text: YES "
                    f"({tables_detected} region(s), ~{ocr_info.get('estimated_row_count', 0)} rows)"
                )
            else:
                md_line = (
                    "  - Markdown tables in OCR text: NONE — the OCR output has no "
                    "Markdown pipe-tables, so the deterministic table parser cannot "
                    "run on this section (enable Textract TABLES, or use an OCR "
                    "backend that emits Markdown tables, to make it available)"
                )
            report_lines.extend(
                [
                    "OCR Table Detection (pre-flight scan of the OCR text):",
                    md_line,
                    f"  - Tool usage recommendation: {ocr_info.get('recommendation_strength', 'N/A')}",
                    "",
                ]
            )

        # Tool usage decision — make availability + the WHY prominent.
        if "tool_usage_decision" in metadata:
            decision = metadata["tool_usage_decision"]
            status_icon = "✓" if not decision.get("mismatch") else "⚠"
            # Availability status: enabled in config AND OCR had Markdown tables.
            if not decision.get("tool_enabled", True):
                avail = "DISABLED in configuration"
            elif not decision.get("ocr_had_markdown_tables", True):
                avail = "enabled, but UNAVAILABLE (no Markdown tables in OCR text)"
            else:
                avail = "ENABLED and AVAILABLE (Markdown tables present in OCR)"
            report_lines.extend(
                [
                    f"{status_icon} Table Parsing Tool Decision:",
                    f"  - Deterministic table parser: {avail}",
                    f"  - Recommended: {'YES' if decision.get('expected') else 'NO'}",
                    f"  - Actually used by agent: {'YES' if decision.get('actual') else 'NO'}",
                    f"  - Why: {decision.get('explanation', 'N/A')}",
                    "",
                ]
            )

        # Completeness check
        if "completeness_check" in metadata:
            check = metadata["completeness_check"]
            if not check.get("schema_constraints_met"):
                status_icon = "✗"
            elif check.get("unexplained_empty_lists"):
                # No schema constraint broken, but a table's worth of rows is
                # missing — a ✓ here is what made this read as healthy.
                status_icon = "⚠"
            else:
                status_icon = "✓"
            report_lines.extend(
                [
                    f"{status_icon} Completeness Validation:",
                    f"  - {check.get('summary', 'N/A')}",
                    "",
                ]
            )

            if check.get("violations"):
                report_lines.append("  Detected Issues:")
                for v in check["violations"]:
                    report_lines.append(f"    • Field '{v['field']}': {v['message']}")
                    report_lines.append(f"      Possible cause: {v['possible_cause']}")
                report_lines.append("")

        # Table parsing stats (if used)
        if (
            metadata.get("table_parsing_tool_used")
            and "table_parsing_stats" in metadata
        ):
            stats = metadata["table_parsing_stats"]
            report_lines.extend(
                [
                    "✓ Table Parsing Tool Results:",
                    f"  - Tables parsed: {stats.get('tables_parsed', 0)}",
                    f"  - Total rows extracted: {stats.get('rows_parsed', 0)}",
                    f"  - Parse success rate: {stats.get('parse_success_rate') or 0:.1%}",
                    f"  - Avg OCR confidence: {stats.get('avg_confidence') or 0:.1f}%",
                    "",
                ]
            )

            if stats.get("warnings"):
                report_lines.append("  Warnings:")
                for w in stats["warnings"]:
                    report_lines.append(f"    {w}")
                report_lines.append("")

        # Item 3: how the document was SIZED and SPLIT (model-aware auto-sizing).
        # Always shown for agentic so the user can see the processing path.
        sizing = metadata.get("sizing_plan")
        if sizing:
            report_lines.append("How this document was processed:")
            report_lines.append(
                f"  - Model: {sizing.get('model_id')} "
                f"(input window {sizing.get('max_input_tokens'):,}, "
                f"output cap {sizing.get('max_output_tokens'):,})"
            )
            report_lines.append(
                f"  - Context buffer: {int(sizing.get('context_buffer', 0) * 100)}% "
                f"kept free"
            )
            report_lines.append(
                f"  - Auto shard budget: ~{sizing.get('shard_token_budget'):,} "
                f"input tokens/shard, max {sizing.get('max_pages_per_shard')} "
                f"pages/shard"
            )
            report_lines.append(
                f"  - Auto confidence list-batch size: "
                f"{sizing.get('list_batch_size')} rows"
            )
            if sizing.get("overrides"):
                report_lines.append(
                    f"  - Manual overrides in effect: {sizing['overrides']}"
                )
            report_lines.append("")

        # Assessment batch-splitting (only present when the confidence model
        # truncated its output and batches had to shrink to recover coverage).
        if "assessment_batch_split_stats" in metadata:
            from idp_common.assessment.batching import format_split_stats_report

            block = format_split_stats_report(metadata["assessment_batch_split_stats"])
            if block:
                report_lines.append("⚠ " + block)
                report_lines.append("")

        # Structured processing issues (1.4): the definitive, user-facing list of
        # what went wrong / was auto-healed, with root cause and the model chain.
        if metadata.get("processing_issues"):
            _icons = {"error": "✗", "warning": "⚠", "info": "ℹ"}
            report_lines.append("Processing Issues:")
            for issue in metadata["processing_issues"]:
                icon = _icons.get(issue.get("severity", "info"), "•")
                report_lines.append(
                    f"  {icon} [{issue.get('code', 'issue')}] "
                    f"{issue.get('message', '')}"
                )
                if issue.get("root_cause"):
                    report_lines.append(f"      Root cause: {issue['root_cause']}")
            report_lines.append("")

        report_lines.append("=" * 40)

        return "\n".join(report_lines)

    def _resolve_escalation_model(self) -> str | None:
        """Pick the model used to re-extract on validation failure.

        Precedence: per-class ``x-aws-idp-extraction-escalation-model`` >
        global ``validation.escalation_model``. Returns None when neither is
        set, in which case escalation falls back to the extraction model and is
        effectively a plain retry (still useful as a second attempt).
        """
        return (
            self._class_schema.get(X_AWS_IDP_EXTRACTION_ESCALATION_MODEL)
            or self.config.extraction.validation.escalation_model
        )

    def _build_schema_validator(self, ocr_analysis: dict[str, Any] | None = None):
        """Return an in-loop validation callback for the agent's self-correction
        round, or None when there is nothing to check.

        Two independent checks, with **independent enablement**:

        1. **Full JSON-Schema validation** — gated on
           ``extraction.validation.enabled``, **on by default as of v0.7**.
           Catches what the generated Pydantic model does not, notably ``format``
           keywords. Free at the default ``fail_action: warn``: no extra
           inference, just a visible ProcessingIssue instead of a silent
           violation.

        2. **Empty declared list with OCR table evidence** — NOT gated. When the
           pre-flight found a substantial table and *every* declared list field
           came back with no rows, the result is rejected regardless of whether
           schema validation is on.

        Check 2 is deliberately ungated, and that is a considered decision rather
        than an oversight. It was originally written behind ``validation.enabled``
        and **live verification caught the mistake**: the very config that
        produced the bug had ``validation.enabled: false`` — the documented
        default at the time — so the safety net was dead on exactly the
        configurations that needed it. A guard against *silent data loss* that is
        itself off by default reproduces the problem it was written to fix.

        v0.7 acted on that argument for check 1 as well by flipping its default
        on. Check 2 nevertheless stays ungated: it guards against silent data
        loss, and a config that explicitly turns validation off should not
        thereby turn off a check that costs nothing and only ever asks the model
        to try again.

        The blast radius is small enough to justify that:

        - It fires only on positive evidence (the OCR pre-flight's own >30
          pipe-table-row rule, the same signal that produced the agent's tool
          guidance) AND only when *no* declared list has any rows.
        - Its effect is one more agent turn with specific feedback. It cannot
          fail the extraction — after the last attempt the loop keeps the
          best-effort result — so the worst case is one wasted turn on a document
          that genuinely has no rows in a detected table.
        - A populated sibling list suppresses it: the detected tables plausibly
          belong to that one, so an empty sibling may be genuinely absent.

        Observed live at Sonnet 5: an agent declined the deterministic parser
        because one column was OCR-corrupted, returned ``Transactions: null`` for
        a 100-row table, and the section was reported COMPLETED with scalar
        accuracy 1.000.
        """
        vcfg = self.config.extraction.validation
        class_schema = self._class_schema
        check_formats = vcfg.check_formats
        schema_checks = bool(vcfg.enabled)
        ocr = ocr_analysis or {}
        table_evidence = bool(ocr.get("tool_usage_recommended")) and bool(
            ocr.get("tables_detected")
        )

        if not schema_checks and not table_evidence:
            return None

        def _validate(data: dict[str, Any]) -> tuple[bool, str]:
            report = (
                validate_extraction(data, class_schema, check_formats=check_formats)
                if schema_checks
                else ValidationReport(valid=True)
            )
            if table_evidence:
                empty, populated = find_empty_declared_lists(data, class_schema)
                if empty and not populated:
                    logger.warning(
                        "Declared list field(s) returned no rows despite OCR table "
                        "evidence; asking the agent to extract them",
                        extra={
                            "empty_list_fields": empty,
                            "ocr_tables_detected": ocr.get("tables_detected"),
                            "ocr_estimated_rows": ocr.get("estimated_row_count"),
                            "schema_validation_enabled": schema_checks,
                        },
                    )
                    feedback = build_empty_list_feedback(
                        empty,
                        int(ocr.get("tables_detected") or 0),
                        int(ocr.get("estimated_row_count") or 0),
                    )
                    if not report.valid:
                        feedback = f"{report.agent_feedback()}\n\n{feedback}"
                    return False, feedback
            return report.valid, report.agent_feedback()

        return _validate

    def _coerce_simple_result(
        self, extracted_fields: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Deterministically fix type/format mismatches before validating.

        Returns ``(fields, coercion_metadata_or_None)``.

        Constrained decoding makes a model emit *a* number; it does not make it
        emit the *right* one. The mismatches that actually occur are
        ``"$1,234.00"`` in a ``number`` field and ``"03/15/2024"`` under
        ``format: date`` — cheap to fix exactly, and wasteful to fix by paying for
        another inference. So this runs BEFORE validation, and validation then
        only reports what could not be repaired deterministically.

        Free: no model call. Every change is recorded in metadata so nothing is
        silently rewritten, and anything ambiguous is refused rather than guessed
        (see ``extraction.coercion``).

        Runs on the simple path only. Agentic extraction validates through a
        generated Pydantic model, which already coerces on the way in.
        """
        from idp_common.extraction.coercion import coerce_extraction

        ccfg = self.config.extraction.coercion
        if not ccfg.enabled:
            # Explicitly disabled: leave the model's output exactly as returned.
            # Validation still runs (if enabled) and will report the mismatches
            # coercion would have repaired.
            return extracted_fields, None

        try:
            report = coerce_extraction(
                extracted_fields, self._class_schema, date_order=ccfg.date_order
            )
        except Exception as e:  # noqa: BLE001 - never fail extraction on a repair
            logger.warning("Coercion failed; leaving values as extracted: %s", e)
            return extracted_fields, None

        if not report.changed and not report.refusals:
            return extracted_fields, None

        if report.changed:
            logger.info("Coercion: %s", report.summary_line())
        return report.data, report.to_metadata()

    def _validate_simple_result(
        self,
        *,
        extracted_fields: dict[str, Any],
        content: list[dict[str, Any]],
        system_prompt: str,
        model_id: str,
        metering: dict[str, Any],
        section_info: SectionInfo,
        parsing_succeeded: bool,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
        """Validate a simple-mode result and act on ``fail_action``.

        Returns ``(extracted_fields, validation_metadata_or_None,
        parsing_succeeded)``.

        Simple extraction had no validation at all: it did a raw ``json.loads``
        and passed whatever came back downstream, so a wrong *type* or a
        non-ISO date reached DynamoDB unchallenged. Agentic extraction has had
        full-schema validation plus escalation for some time; this brings the
        same guarantee to the path most deployments actually run.

        Cost, which is the design constraint:

        - ``warn`` (the default) — validate and record. **No extra inference.**
          Validation is on by default precisely because this combination is free;
          a default that quietly spent money on every schema violation would be a
          cost surprise rather than a safety net.
        - ``reject`` — same, plus ``parsing_succeeded=False`` so downstream/HITL
          treats the section as failed. Also free.
        - ``escalate`` — one scoped re-extraction of ONLY the failing top-level
          fields with the stronger ``escalation_model``, merged back over the
          fields that already validated. This is the opt-in that costs money.

        Deliberately NOT a same-model retry loop before escalating: re-asking the
        model that just produced invalid output, with the same prompt, mostly buys
        another invalid answer at full document cost. The escalation model is the
        thing likely to do better.
        """
        vcfg = self.config.extraction.validation
        if not vcfg.enabled or not parsing_succeeded:
            return extracted_fields, None, parsing_succeeded

        report = validate_extraction(
            extracted_fields, self._class_schema, check_formats=vcfg.check_formats
        )
        initial_error_count = len(report.errors)
        initial_failed_fields = sorted(report.failed_top_level_fields)

        escalated = False
        escalation_model: str | None = None
        if not report.valid:
            logger.warning(
                "Simple extraction failed full-schema validation for '%s'",
                section_info.class_label,
                extra={
                    "error_count": initial_error_count,
                    "failed_fields": initial_failed_fields,
                    "fail_action": vcfg.fail_action,
                },
            )

        if not report.valid and vcfg.fail_action == "escalate":
            escalation_model = self._resolve_escalation_model() or model_id
            escalated = True
            extracted_fields, report, escalation_metering = (
                self._escalate_simple_fields(
                    extracted_fields=extracted_fields,
                    report=report,
                    escalation_model=escalation_model,
                    content=content,
                    system_prompt=system_prompt,
                    section_info=section_info,
                )
            )
            if escalation_metering:
                metering.update(
                    utils.merge_metering_data(metering, escalation_metering)
                )

        if not report.valid and vcfg.fail_action == "reject":
            parsing_succeeded = False

        validation_metadata: dict[str, Any] = {
            **report.to_metadata(),
            "check_formats": vcfg.check_formats,
            "fail_action": vcfg.fail_action,
            "escalated": escalated,
            "initial_error_count": initial_error_count,
            "initial_failed_fields": initial_failed_fields,
            "mode": "simple",
        }
        if escalated:
            validation_metadata["escalation_model"] = escalation_model
            validation_metadata["escalation_scope"] = "field-subset"
            validation_metadata["escalation_fields"] = initial_failed_fields
            validation_metadata["resolved_by_escalation"] = report.valid

        return extracted_fields, validation_metadata, parsing_succeeded

    def _escalate_simple_fields(
        self,
        *,
        extracted_fields: dict[str, Any],
        report: ValidationReport,
        escalation_model: str,
        content: list[dict[str, Any]],
        system_prompt: str,
        section_info: SectionInfo,
    ) -> tuple[dict[str, Any], ValidationReport, dict[str, Any]]:
        """Re-extract only the failing fields with a stronger model (simple mode).

        Mirrors the agentic ``_escalate_failing_fields`` intent — scope the retry
        to what actually failed, so the fields that already validated are neither
        re-paid for nor put at risk of regressing — but over a single plain
        Converse call rather than an agent loop.

        Best-effort: on any failure the inputs are returned unchanged, because a
        broken escalation must not be worse than no escalation.
        """
        failed_fields = sorted(report.failed_top_level_fields)
        if not failed_fields:
            return extracted_fields, report, {}

        subset_schema = build_subset_schema(self._class_schema, failed_fields)
        vcfg = self.config.extraction.validation

        logger.info(
            "Escalating simple extraction for '%s' to %s (fields=%s)",
            section_info.class_label,
            escalation_model,
            failed_fields,
        )

        corrective = (
            "\n\nA previous extraction attempt produced data that violated the "
            "schema. Re-extract ONLY the following fields and correct these "
            "issues. Return a JSON object containing exactly these keys: "
            f"{failed_fields}\n\n{report.agent_feedback()}\n\n"
            "Schema for these fields:\n"
            f"{self._format_schema_for_prompt(subset_schema)}"
        )
        retry_content = [*content, {"text": corrective}]

        try:
            response = bedrock.invoke_model(
                model_id=escalation_model,
                system_prompt=system_prompt,
                content=retry_content,
                temperature=self.config.extraction.temperature,
                top_k=self.config.extraction.top_k,
                top_p=self.config.extraction.top_p,
                max_tokens=None,
                context="Extraction-Escalation",
                model_lambda_hook_arn=self.config.extraction.model_lambda_hook_arn,
                reasoning_effort=self.config.extraction.reasoning_effort,
            )
            text = bedrock.extract_text_from_response(dict(response))
            corrected = json.loads(extract_json_from_text(text))
            corrected, ok, _count, _rec = self._normalize_list_result(
                corrected, context="escalation"
            )
            if not ok or not isinstance(corrected, dict):
                logger.warning(
                    "Escalation response was not a usable object; keeping the "
                    "original extraction"
                )
                return extracted_fields, report, response.get("metering", {})

            # In integrated (1S-TopK) confidence mode the original content carries
            # the top-K task prompt, so the escalation model may answer in the
            # {"G1": ..., "P1": ...} candidate shape even though the corrective
            # instruction asked for plain values. Merging that in would write a
            # candidate object over an already-resolved value, and the confidence
            # split has already run — so resolve_candidates would never unwrap it
            # and the field would reach storage as an object. Keep the original.
            if is_topk_response(corrected):
                logger.warning(
                    "Escalation answered in the top-K candidate shape rather than "
                    "plain values; keeping the original extraction"
                )
                return extracted_fields, report, response.get("metering", {})

            # Merge back ONLY the fields we asked for, so an over-eager
            # escalation response cannot overwrite fields that already validated.
            merged = dict(extracted_fields)
            for field_name in failed_fields:
                if field_name in corrected:
                    merged[field_name] = corrected[field_name]

            merged, _ = self._coerce_simple_result(merged)
            new_report = validate_extraction(
                merged, self._class_schema, check_formats=vcfg.check_formats
            )
            if new_report.valid:
                logger.info("Escalation resolved all schema violations")
            else:
                logger.warning(
                    "Escalation reduced violations from %d to %d but the result "
                    "is still invalid",
                    len(report.errors),
                    len(new_report.errors),
                )
            return merged, new_report, response.get("metering", {})
        except Exception as e:  # noqa: BLE001 - escalation is best-effort
            logger.warning("Simple-mode escalation failed: %s", e)
            return extracted_fields, report, {}

    def _validate_and_maybe_escalate(
        self,
        extracted_fields: dict[str, Any],
        structured_data: Any,
        data_model: Any,
        model_id: str,
        message_prompt: Any,
        agentic_images: list[bytes],
        custom_instruction: str | None,
        section_info: SectionInfo,
        parsing_succeeded: bool,
    ) -> tuple[dict[str, Any], Any, dict[str, Any] | None, dict[str, Any], bool]:
        """Validate the final extraction and optionally escalate to a stronger model.

        Returns ``(extracted_fields, structured_data, validation_metadata,
        escalation_metering, parsing_succeeded)``. A no-op (returns inputs
        unchanged with ``validation_metadata=None``) unless
        ``extraction.validation.enabled``.
        """
        vcfg = self.config.extraction.validation
        escalation_metering: dict[str, Any] = {}
        if not vcfg.enabled:
            return (
                extracted_fields,
                structured_data,
                None,
                escalation_metering,
                parsing_succeeded,
            )

        report: ValidationReport = validate_extraction(
            extracted_fields, self._class_schema, check_formats=vcfg.check_formats
        )
        initial_error_count = len(report.errors)
        initial_failed_fields = sorted(report.failed_top_level_fields)

        escalated = False
        escalation_model: str | None = None
        escalation_scope: str | None = None
        escalation_fields: list[str] = []
        if not report.valid:
            logger.warning(
                "Extraction failed full-schema validation for "
                f"'{section_info.class_label}'",
                extra={
                    "error_count": initial_error_count,
                    "failed_fields": initial_failed_fields,
                    "fail_action": vcfg.fail_action,
                },
            )

        # Escalate: re-extract ONLY the failing top-level fields with a stronger
        # model, then merge the corrected fields back into the full result. This
        # is cheaper/faster than re-running the whole section and preserves the
        # fields that already validated.
        if not report.valid and vcfg.fail_action == "escalate":
            escalation_model = self._resolve_escalation_model() or model_id
            escalated = True
            escalation_fields = initial_failed_fields
            (
                extracted_fields,
                structured_data,
                report,
                escalation_metering,
                escalation_scope,
            ) = self._escalate_failing_fields(
                extracted_fields=extracted_fields,
                structured_data=structured_data,
                data_model=data_model,
                full_report=report,
                escalation_model=escalation_model,
                message_prompt=message_prompt,
                agentic_images=agentic_images,
                custom_instruction=custom_instruction,
                section_info=section_info,
            )

        # reject: surface the failure so downstream/HITL can act on it.
        if not report.valid and vcfg.fail_action == "reject":
            parsing_succeeded = False

        # Rich audit trail: what was enforced, what failed, what we did about it.
        validation_metadata: dict[str, Any] = {
            **report.to_metadata(),
            "check_formats": vcfg.check_formats,
            "fail_action": vcfg.fail_action,
            "escalated": escalated,
            "initial_error_count": initial_error_count,
            "initial_failed_fields": initial_failed_fields,
        }
        if escalated:
            validation_metadata["escalation_model"] = escalation_model
            validation_metadata["escalation_scope"] = escalation_scope
            validation_metadata["escalation_fields"] = escalation_fields
            validation_metadata["resolved_by_escalation"] = report.valid

        return (
            extracted_fields,
            structured_data,
            validation_metadata,
            escalation_metering,
            parsing_succeeded,
        )

    def _escalate_failing_fields(
        self,
        extracted_fields: dict[str, Any],
        structured_data: Any,
        data_model: Any,
        full_report: ValidationReport,
        escalation_model: str,
        message_prompt: Any,
        agentic_images: list[bytes],
        custom_instruction: str | None,
        section_info: SectionInfo,
    ) -> tuple[dict[str, Any], Any, ValidationReport, dict[str, Any], str]:
        """Re-extract only the failing top-level fields with a stronger model.

        Builds a reduced schema containing just ``full_report.failed_top_level_fields``,
        runs a scoped extraction, merges the corrected fields back into the full
        result and re-validates. Falls back to a whole-section re-extraction when
        a usable subset schema can't be built (e.g. failures are root-level only).

        Returns ``(extracted_fields, structured_data, report, metering, scope)``
        where ``scope`` is ``"field-subset"`` or ``"full-section"``. On any error
        the original inputs are returned unchanged with an empty metering dict.
        """
        failed_fields = sorted(full_report.failed_top_level_fields)
        subset_schema = build_subset_schema(self._class_schema, failed_fields)
        scope = (
            "field-subset"
            if subset_schema is not self._class_schema
            else "full-section"
        )

        logger.info(
            f"Escalating extraction for '{section_info.class_label}' to model "
            f"{escalation_model} (scope={scope}, fields={failed_fields})",
        )

        check_formats = self.config.extraction.validation.check_formats
        instruction = (
            (custom_instruction + "\n\n" if custom_instruction else "")
            + "A previous extraction attempt produced data that violated the "
            "schema. Carefully re-extract and correct these issues:\n"
            + full_report.agent_feedback()
        )

        try:
            if scope == "field-subset":
                subset_model = create_pydantic_model_from_json_schema(
                    schema=subset_schema,
                    class_label=f"{section_info.class_label}__escalation",
                    clean_schema=False,
                )
                # Seed with current values for the failing fields only.
                seed = {k: extracted_fields.get(k) for k in failed_fields}
                try:
                    existing_model = subset_model(
                        **{k: v for k, v in seed.items() if v is not None}
                    )
                except Exception:
                    existing_model = None

                esc_data, esc_response = structured_output(
                    model_id=escalation_model,
                    data_format=subset_model,
                    prompt=message_prompt,
                    existing_data=existing_model,
                    page_images=agentic_images,
                    config=self.config,
                    context="ExtractionEscalation",
                    custom_instruction=instruction,
                    schema_validator=None,  # validated against the full schema below
                )
                metering = esc_response.get("metering", {}) or {}
                metering.pop("_table_parsing_stats", None)

                # Merge corrected subset fields back into the full result.
                merged = dict(extracted_fields)
                merged.update(esc_data.model_dump(mode="json"))
            else:
                # Fallback: whole-section re-extraction with the full model.
                try:
                    existing_model = data_model(**extracted_fields)
                except Exception:
                    existing_model = None
                esc_data, esc_response = structured_output(
                    model_id=escalation_model,
                    data_format=data_model,
                    prompt=message_prompt,
                    existing_data=existing_model,
                    page_images=agentic_images,
                    config=self.config,
                    context="ExtractionEscalation",
                    custom_instruction=instruction,
                    schema_validator=self._build_schema_validator(),
                )
                metering = esc_response.get("metering", {}) or {}
                metering.pop("_table_parsing_stats", None)
                merged = esc_data.model_dump(mode="json")

            esc_report = validate_extraction(
                merged, self._class_schema, check_formats=check_formats
            )

            # Keep the escalated result only if it's valid or strictly improves.
            if esc_report.valid or len(esc_report.errors) < len(full_report.errors):
                # Re-validate the merged dict through the full Pydantic model so
                # the returned structured_data stays consistent with the fields.
                try:
                    structured_data = data_model(**merged)
                except Exception:
                    pass  # keep prior structured_data; fields dict is source of truth
                return merged, structured_data, esc_report, metering, scope

            logger.info(
                "Escalation did not improve validation; keeping original result",
                extra={
                    "original_errors": len(full_report.errors),
                    "escalated_errors": len(esc_report.errors),
                },
            )
            return extracted_fields, structured_data, full_report, metering, scope
        except Exception as e:
            logger.error(
                "Escalation re-extraction failed; keeping original result",
                extra={"error": str(e)},
                exc_info=True,
            )
            return extracted_fields, structured_data, full_report, {}, scope

    def _check_extraction_completeness(
        self,
        extracted_data: Any,
        data_model: Any,
        section_label: str,
    ) -> None:
        """
        Check if extraction meets schema constraints (e.g., min_length for arrays).

        Logs warnings if extracted data appears incomplete based on schema constraints.
        This helps catch cases where table parsing or extraction stopped early.

        Args:
            extracted_data: The extracted data instance (Pydantic model)
            data_model: The Pydantic model class with constraints
            section_label: Section label for logging context
        """
        if not hasattr(data_model, "model_fields"):
            return

        for field_name, field_info in data_model.model_fields.items():
            field_value = getattr(extracted_data, field_name, None)

            # Check array min_length constraints
            if isinstance(field_value, list) and hasattr(field_info, "metadata"):
                for constraint in field_info.metadata:
                    if hasattr(constraint, "min_length"):
                        expected_min = constraint.min_length
                        actual_count = len(field_value)

                        if actual_count < expected_min:
                            logger.warning(
                                f"Extraction may be INCOMPLETE for {section_label}: "
                                f"field '{field_name}' has {actual_count} items, "
                                f"but schema expects at least {expected_min}. "
                                f"Verify all table rows were extracted.",
                                extra={
                                    "field": field_name,
                                    "actual_count": actual_count,
                                    "expected_min": expected_min,
                                    "completeness_ratio": f"{actual_count}/{expected_min}",
                                },
                            )
                        elif actual_count == expected_min:
                            logger.info(
                                f"Extraction meets minimum constraint for {section_label}: "
                                f"field '{field_name}' has exactly {actual_count} items "
                                f"(minimum: {expected_min})"
                            )
                        else:
                            logger.debug(
                                f"Extraction exceeds minimum constraint for {section_label}: "
                                f"field '{field_name}' has {actual_count} items "
                                f"(minimum: {expected_min})"
                            )

    def _invoke_extraction_model(
        self,
        content: list[dict[str, Any]],
        system_prompt: str,
        section_info: SectionInfo,
        checkpoint_data: dict[str, Any] | None = None,
    ) -> ExtractionResult:
        """
        Invoke Bedrock model (agentic or standard) and parse response.

        Args:
            content: Prompt content
            system_prompt: System prompt
            section_info: Section metadata

        Returns:
            ExtractionResult with extracted fields and metering
        """
        logger.info(
            f"Extracting fields for {section_info.class_label} document, section"
        )

        # Clear any per-section audit state from a previously processed section.
        self._pending_validation_metadata = None
        self._pending_coercion_metadata = None
        self._pending_extraction_model = None
        self._pending_forced_tool_metadata = None

        # Get extraction config — use per-class model override if specified,
        # otherwise fall back to the global extraction model.
        class_model_override = self._class_schema.get(X_AWS_IDP_EXTRACTION_MODEL)
        model_id = class_model_override or self.config.extraction.model
        # Record the resolved model for audit metadata (set here so it is
        # captured even on the non-agentic/standard path).
        self._pending_extraction_model = model_id
        if class_model_override:
            logger.info(
                f"Using per-class extraction model override for "
                f"'{section_info.class_label}': {model_id}"
            )
        temperature = self.config.extraction.temperature
        top_k = self.config.extraction.top_k
        top_p = self.config.extraction.top_p
        reasoning_effort = self.config.extraction.reasoning_effort
        # max_tokens is no longer a config knob — pass None so the Bedrock client
        # resolves the model's maximum output (model_config_limits.yaml). Bedrock's
        # default-when-omitted truncates, so the client always sets it explicitly.
        max_tokens = None

        # Time the model invocation
        request_start_time = time.time()

        # Initialize repair tracking variables
        output_truncated = False
        output_repaired = False
        repair_method = None

        # Multi-instance tracking (see _normalize_list_result). Stays 0/None on
        # the agentic path, which validates through a Pydantic model and so can
        # never receive a bare array.
        instance_count = 0
        recovered_instances: list[dict[str, Any]] | None = None
        # The model's own answer to "how many documents of this class are in
        # these pages" (#753). None on the agentic path, which does not carry the
        # probe — see _build_wire_schema.
        instance_probe: int | None = None

        # Initialize analysis tracking
        schema_analysis: dict[str, Any] | None = None
        ocr_analysis: dict[str, Any] | None = None

        # OpenAI GPT-5.x models are served via the bedrock-mantle Responses API
        # and are incompatible with agentic (Strands) extraction, which relies on
        # the Converse API. This combination is rejected at config-validate time;
        # fail loudly here too rather than silently changing the extraction mode,
        # so a config that bypassed validation surfaces the error immediately.
        use_agentic = self.config.extraction.agentic.enabled
        if use_agentic and is_openai_responses_model(model_id):
            raise ValueError(
                f"OpenAI Responses model '{model_id}' is not compatible with agentic "
                "extraction (extraction.agentic.enabled=true). Set agentic.enabled=false "
                "or choose a non-OpenAI model. (This is also enforced by "
                "'idp-cli config-validate'.)"
            )

        if use_agentic:
            if not AGENTIC_AVAILABLE:
                raise ImportError(
                    "Agentic extraction requires Python 3.12+ and strands-agents dependencies. "
                    "Install with: pip install -e 'lib/idp_common_pkg[agents]' or use agentic=False"
                )

            # Pre-flight analysis: Detect large tables and assess tool requirements
            schema_analysis = self._analyze_schema_for_table_requirements(
                self._class_schema
            )
            ocr_analysis = self._analyze_ocr_for_tables(self._document_text)

            logger.info(
                "Pre-flight analysis complete",
                extra={
                    "schema_recommendation": schema_analysis.get(
                        "recommendation_strength"
                    ),
                    "ocr_recommendation": ocr_analysis.get("recommendation_strength"),
                    "schema_max_min_items": schema_analysis.get("max_min_items"),
                    "ocr_estimated_rows": ocr_analysis.get("estimated_row_count"),
                },
            )

            # Create dynamic Pydantic model from JSON Schema
            dynamic_model = create_pydantic_model_from_json_schema(
                schema=self._class_schema,
                class_label=section_info.class_label,
                clean_schema=False,  # Already cleaned
            )

            # Log schema for debugging
            model_schema = dynamic_model.model_json_schema()
            logger.debug(f"Pydantic model schema for {section_info.class_label}:")
            logger.debug(json.dumps(model_schema, indent=2))

            # Use agentic extraction
            if isinstance(content, list):
                message_prompt = {"role": "user", "content": content}
            else:
                message_prompt = content

            logger.info("Using Agentic extraction")
            logger.debug(f"Using input: {str(message_prompt)}")

            # Convert checkpoint data for resume-on-timeout
            existing_data_model = None
            checkpoint_buffer = None
            if checkpoint_data:
                # Check if this is a buffer checkpoint (from intermediate_extraction)
                checkpoint_source = checkpoint_data.pop(
                    "_checkpoint_source", "current_extraction"
                )
                if checkpoint_source == "intermediate_extraction":
                    # Buffer checkpoint — load into agent's intermediate_extraction state
                    checkpoint_buffer = checkpoint_data
                    logger.info(
                        "Resuming agentic extraction from buffer checkpoint "
                        "(intermediate_extraction)"
                    )
                else:
                    # Validated extraction checkpoint — load as existing_data
                    try:
                        existing_data_model = dynamic_model(**checkpoint_data)
                        logger.info("Resuming agentic extraction from checkpoint data")
                    except Exception as e:
                        logger.warning(
                            f"Failed to validate checkpoint data, starting fresh: {e}"
                        )
                        existing_data_model = None

            # Build dynamic custom instruction based on pre-flight analysis
            custom_instruction = None
            if schema_analysis and ocr_analysis:
                dynamic_guidance = self._build_table_parsing_guidance(
                    schema_analysis=schema_analysis, ocr_analysis=ocr_analysis
                )
                if dynamic_guidance:
                    custom_instruction = dynamic_guidance
                    logger.info(
                        "Injecting dynamic table parsing guidance into agent instructions",
                        extra={
                            "schema_strength": schema_analysis.get(
                                "recommendation_strength"
                            ),
                            "ocr_strength": ocr_analysis.get("recommendation_strength"),
                        },
                    )

            # Pre-flight table parsing: parse all tables BEFORE LLM to avoid
            # the LLM having to call parse_table and then generate JSON row-by-row.
            # The LLM only needs to provide a column-to-field mapping, and the
            # map_table_to_schema tool does the bulk transformation instantly.
            preflight_parse_result = None
            if (
                self.config.extraction.agentic.table_parsing.enabled
                and ocr_analysis.get("estimated_row_count", 0) >= 50
            ):
                from idp_common.extraction.tools.table_parser import (
                    parse_markdown_tables,
                )

                tp_config = self.config.extraction.agentic.table_parsing
                preflight_parse_result = parse_markdown_tables(
                    text=self._document_text,
                    max_empty_line_gap=tp_config.max_empty_line_gap,
                    auto_merge_adjacent_tables=tp_config.auto_merge_adjacent_tables,
                )

                if preflight_parse_result.get("status") == "success":
                    total_rows = sum(
                        t.get("row_count", 0)
                        for t in preflight_parse_result.get("tables", [])
                    )
                    columns = preflight_parse_result.get("columns", [])
                    table_count = preflight_parse_result.get("table_count", 0)

                    logger.info(
                        "Pre-flight table parsing complete",
                        extra={
                            "total_rows": total_rows,
                            "table_count": table_count,
                            "columns": columns,
                        },
                    )

                    # Build efficient extraction guidance with pre-parsed summary
                    preflight_guidance = (
                        f"\n\n**PRE-PARSED TABLE DATA AVAILABLE**:\n"
                        f"Found {table_count} table(s) with {total_rows} total rows.\n"
                        f"Table columns: {columns}\n\n"
                        f"PAGE MARKERS: The document text contains '--- PAGE N ---' "
                        f"markers between pages. When you are assigned a page range, "
                        f"extract ONLY text between markers for your pages before "
                        f"calling parse_table.\n\n"
                        f"EFFICIENT EXTRACTION WORKFLOW:\n"
                        f"1. Extract scalar fields from your pages' text\n"
                        f"2. Call parse_table with your pages' text\n"
                        f"3. Call map_table_to_schema with column_mapping + static_fields\n"
                        f"   (merged rows are auto-split — no manual handling needed)\n"
                        f"4. Call finalize_table_extraction with table_array_field + "
                        f"scalar_fields\n\n"
                        f"finalize reads mapped rows from state — no JSON generation needed."
                    )

                    if custom_instruction:
                        custom_instruction += preflight_guidance
                    else:
                        custom_instruction = preflight_guidance

            # Determine if images should be sent to the agentic model.
            # If the task prompt does not reference {DOCUMENT_IMAGE}, sending
            # page images is wasteful and can cause context-window overflow
            # on large documents.
            from idp_common.extraction.prompt_assembly import (
                select_extraction_task_prompt,
            )

            prompt_template = (
                select_extraction_task_prompt(self.config.extraction) or ""
            )
            send_images = "{DOCUMENT_IMAGE}" in prompt_template

            # Cost optimization: when the deterministic table parser already
            # parsed the document's table(s) in pre-flight, the agentic loop is
            # text/markdown-driven (parse_table / map_table_to_schema never read
            # images) and the agent can still fetch a page on demand via the
            # view_image tool. Pre-loading every page image re-sends them on
            # every agent turn and dominates cost on multi-page docs (and can
            # push large docs toward context-window limits). So suppress the
            # up-front image attachment when a successful pre-flight table parse
            # covers the doc. Gated by config (lazy_images, default on) so
            # image-dependent corpora can opt back in. Local+live A/B: identical
            # completeness/accuracy (recall 1.0) with images off on this path.
            suppress_images_for_table = (
                send_images
                and self.config.extraction.agentic.table_parsing.lazy_images
                and preflight_parse_result is not None
                and preflight_parse_result.get("status") == "success"
            )
            if suppress_images_for_table:
                send_images = False

            agentic_images = (
                self._cap_agent_images(self._page_images) if send_images else []
            )
            num_pages = len(self._page_images) or len(section_info.sorted_page_ids)

            if suppress_images_for_table and self._page_images:
                logger.info(
                    "Skipping up-front image attachment for agentic extraction "
                    "(pre-flight table parse succeeded; table tool is text-driven, "
                    "view_image remains available on demand)",
                    extra={"page_count": num_pages},
                )
            elif not send_images and self._page_images:
                logger.info(
                    "Skipping image attachment for agentic extraction "
                    "(task prompt does not reference {DOCUMENT_IMAGE})",
                    extra={"page_count": num_pages},
                )

            # Build the in-loop schema validator (None unless validation enabled).
            # Used only on the single-agent path: per-batch validation would
            # falsely fail minItems before the batches are merged, so batch
            # output is validated once after merge below.
            #
            # ocr_analysis is the whole-document pre-flight, which is exactly the
            # right scope here for the same reason: the single agent sees the
            # whole section, so "the OCR has a 100-row table but every declared
            # list came back empty" is a sound inference. It would NOT be sound
            # per-shard, where a shard legitimately contains none of the rows.
            schema_validator = self._build_schema_validator(ocr_analysis=ocr_analysis)

            # Use concurrent SHARDED extraction if configured and enough pages.
            # Each shard's prompt contains only its pages' text/images (built by
            # _build_shard_payloads), so no agent loads the whole document —
            # this bounds the context window in addition to parallelizing.
            num_batches = self.config.extraction.agentic.max_concurrent_batches
            shard_payloads: list[dict[str, Any]] = []
            if (
                num_batches > 1
                and num_pages > 1
                and len(self._page_texts) > 1
                and not existing_data_model  # Don't use concurrent mode for resume
                and not checkpoint_buffer
            ):
                shard_payloads = self._build_shard_payloads(
                    prompt_template=prompt_template,
                    send_images=send_images,
                    max_shards=num_batches,
                )

            if shard_payloads:
                import asyncio as _asyncio

                logger.info(
                    f"Using sharded concurrent extraction: {len(shard_payloads)} "
                    f"shard(s), parallelism {num_batches}, for {num_pages} pages"
                )
                from idp_common.extraction.runtime import select_runtime

                runtime = select_runtime(self.config, num_batches)
                structured_data, response_with_metering = _asyncio.run(
                    concurrent_structured_output_async(
                        model_id=model_id,
                        data_format=dynamic_model,
                        shard_payloads=shard_payloads,
                        max_parallelism=num_batches,
                        config=self.config,
                        context="Extraction",
                        checkpoint_callback=self._checkpoint_callback,
                        custom_instruction=custom_instruction,
                        section_id=(
                            f"{section_info.class_label}_"
                            f"{section_info.start_page}_{section_info.end_page}"
                        ),
                        persistence=self._shard_persistence,
                        runtime=runtime,
                        assess_runner=self._build_assess_runner(
                            section_info, self._document
                        ),
                    )
                )
            else:
                structured_data, response_with_metering = structured_output(
                    model_id=model_id,
                    data_format=dynamic_model,
                    prompt=message_prompt,
                    existing_data=existing_data_model,
                    page_images=agentic_images,
                    config=self.config,
                    context="Extraction",
                    checkpoint_callback=self._checkpoint_callback,
                    checkpoint_buffer_data=checkpoint_buffer,
                    custom_instruction=custom_instruction,
                    schema_validator=schema_validator,
                    emit_field_assessment=self._integrated_assessment_enabled(),
                )

            extracted_fields = structured_data.model_dump(mode="json")
            metering = response_with_metering["metering"]
            parsing_succeeded = True

            # Capture the agent's optional self-reported table-tool rationale (a
            # "TABLE_TOOL_NOTE:" line it emits when it extracted a large table
            # directly instead of calling parse_table). Best-effort; empty when the
            # agent used the tool or added no note.
            self._agent_table_tool_note = self._extract_table_tool_note(
                response_with_metering
            )

            # Full JSON-Schema validation of the final result, with optional
            # bounded escalation to a stronger model. No-op unless
            # extraction.validation.enabled. Updates extracted_fields,
            # may flip parsing_succeeded (fail_action="reject"), and records the
            # outcome under metadata["validation"].
            (
                extracted_fields,
                structured_data,
                validation_metadata,
                escalation_metering,
                parsing_succeeded,
            ) = self._validate_and_maybe_escalate(
                extracted_fields=extracted_fields,
                structured_data=structured_data,
                data_model=dynamic_model,
                model_id=model_id,
                message_prompt=message_prompt,
                agentic_images=agentic_images,
                custom_instruction=custom_instruction,
                section_info=section_info,
                parsing_succeeded=parsing_succeeded,
            )
            if validation_metadata is not None:
                self._pending_validation_metadata = validation_metadata
            if escalation_metering:
                from idp_common.extraction.agentic_idp import _accumulate_metering

                _accumulate_metering(metering, escalation_metering)

            # Check extraction completeness (warns if schema constraints not met)
            self._check_extraction_completeness(
                extracted_data=structured_data,
                data_model=dynamic_model,
                section_label=section_info.class_label,
            )

            # In-shard assessment for the NON-sharded single-agent agentic path
            # (sharding did not engage). Equivalent to a single full-section
            # shard: assess the final (post-escalation) values over all pages,
            # then let _save_results ground + emit explainability_info. Mirrors
            # the sharded path's _merged_assessment metering marker.
            if not shard_payloads and self._inshard_assessment_enabled():
                self._assess_single_agent(
                    extracted_fields=extracted_fields,
                    section_info=section_info,
                    metering=metering,
                )
            elif not shard_payloads and self._integrated_assessment_enabled():
                # Integrated mode, single-agent: the agent already emitted
                # confidence/bbox inline. Lift it from metering into the
                # _merged_assessment slot so _save_results grounds + emits it.
                inline = metering.pop("_integrated_field_assessment", None)
                if inline:
                    metering["_merged_assessment"] = inline
                    metering["_merged_assessment_alerts"] = []
        else:
            # Optionally put the class schema on the wire as a FORCED tool instead
            # of relying on the prompt's description of it (WS-05). Off by default
            # and gated on a measured win — forcing constrains the response shape,
            # not the values in it. The prompt is deliberately left unchanged so
            # the A/B measures forcing alone; dropping the prose schema is #710's
            # separate question, and bundling the two would confound both.
            from idp_common.extraction.forced_tool import (
                EXTRACTION_TOOL_NAME,
                build_extraction_tool_config,
                forced_tool_choice,
                restore_extracted_fields,
                should_force_tool,
                unwrap_tool_payload,
            )

            # The WIRE schema, so a forced toolSpec carries the same shape the
            # prompt describes — including the multi-instance detection probe
            # (#753). A forced tool whose input schema lacks the probe would make
            # the count structurally impossible to return, which is exactly the
            # regression #753 warns about for forced tool use generally.
            ft_cfg = self.config.extraction.forced_tool
            wire_schema = self._wire_class_schema or self._class_schema
            force, skip_reason = should_force_tool(
                model_id, ft_cfg.enabled, wire_schema
            )
            tool_config = tool_choice = None
            tool_name_map = None
            if force:
                tool_config, tool_name_map = build_extraction_tool_config(wire_schema)
                tool_choice = forced_tool_choice()
            elif skip_reason:
                # Recorded, not just logged: an A/B in which "no effect" and
                # "never ran" look identical is unreadable.
                logger.info("Forced tool use skipped — %s", skip_reason)

            response_with_metering = bedrock.invoke_model(
                model_id=model_id,
                system_prompt=system_prompt,
                content=content,
                temperature=temperature,
                top_k=top_k,
                top_p=top_p,
                max_tokens=max_tokens,
                context="Extraction",
                model_lambda_hook_arn=self.config.extraction.model_lambda_hook_arn,
                reasoning_effort=reasoning_effort,
                tool_config=tool_config,
                tool_choice=tool_choice,
            )

            # A model can accept a toolConfig and still answer in prose. That is a
            # normal outcome (stopReason end_turn rather than tool_use), so an
            # absent toolUse block falls back to the text path unless the operator
            # has explicitly asked for it to be a failure.
            forced_tool_input = None
            if force:
                # Unwrap BEFORE restoring names: the wrapper key is the model's
                # invention and is not in the name map, while the payload inside it
                # is keyed by the sanitized names the map knows about.
                forced_tool_input = restore_extracted_fields(
                    unwrap_tool_payload(
                        bedrock.extract_tool_use_from_response(
                            dict(response_with_metering),
                            tool_name=EXTRACTION_TOOL_NAME,
                        ),
                        wire_schema,
                        tool_name_map,
                    ),
                    tool_name_map,
                )
                self._pending_forced_tool_metadata = {
                    "requested": True,
                    "honored": forced_tool_input is not None,
                    "tool_name": EXTRACTION_TOOL_NAME,
                    "renamed_properties": len(tool_name_map.renamed)
                    if tool_name_map
                    else 0,
                }
                if forced_tool_input is None:
                    logger.warning(
                        "Forced tool use was requested but the model answered in "
                        "prose; %s",
                        "falling back to text parsing"
                        if ft_cfg.fallback_to_prompt
                        else "treating it as a parse failure (fallback disabled)",
                    )
            elif skip_reason:
                self._pending_forced_tool_metadata = {
                    "requested": True,
                    "honored": False,
                    "skipped": skip_reason,
                }

            extracted_text = bedrock.extract_text_from_response(
                dict(response_with_metering)
            )
            metering = response_with_metering["metering"]

            # Parse response into JSON
            extracted_fields = {}
            parsing_succeeded = True
            output_truncated = False
            output_repaired = False
            repair_method = None
            instance_count = 0
            recovered_instances = None

            if forced_tool_input is not None:
                # The model called the tool, so the shape is the API's guarantee
                # and there is no text to parse. Still run it through
                # _normalize_list_result: a forced tool constrains the SHAPE, and a
                # class whose schema is a packet of records can legitimately come
                # back as a list, which is the multi-instance case (#565/#687).
                (
                    extracted_fields,
                    parsing_succeeded,
                    instance_count,
                    recovered_instances,
                ) = self._normalize_list_result(
                    forced_tool_input, context="forced-tool"
                )
            elif force and not ft_cfg.fallback_to_prompt:
                # Explicitly configured to treat an unhonored force as a failure.
                # Only useful for measuring how often forcing is actually honored.
                logger.warning(
                    "Forced tool use was not honored and fallback_to_prompt is "
                    "off; marking the section as a parse failure"
                )
                extracted_fields = {
                    "error": (
                        "forced tool use was requested but the model answered in "
                        "prose, and extraction.forced_tool.fallback_to_prompt is "
                        "disabled"
                    )
                }
                parsing_succeeded = False
            else:
                try:
                    extracted_fields = json.loads(
                        extract_json_from_text(extracted_text)
                    )

                    # A top-level array means either a needlessly wrapped single
                    # object or — far more often — several documents of the same
                    # class in one section. Both are handled (and preserved) here.
                    (
                        extracted_fields,
                        parsing_succeeded,
                        instance_count,
                        recovered_instances,
                    ) = self._normalize_list_result(extracted_fields, context="parsed")

                except Exception as e:
                    logger.warning(
                        f"Error parsing LLM output - attempting JSON repair: {e}"
                    )

                    # Attempt to repair truncated JSON
                    repaired_data, repair_info = repair_truncated_json(extracted_text)
                    output_truncated = repair_info.get("was_truncated", False)

                    if repaired_data:
                        # Repair succeeded
                        extracted_fields = repaired_data

                        # Repaired output can be an array for the same reasons the
                        # cleanly-parsed output can; normalize identically. A
                        # truncated multi-instance response is exactly the case worth
                        # salvaging — the earlier instances are complete.
                        (
                            extracted_fields,
                            parsing_succeeded,
                            instance_count,
                            recovered_instances,
                        ) = self._normalize_list_result(
                            extracted_fields, context="repaired"
                        )

                        output_repaired = True
                        repair_method = repair_info.get("repair_method")
                        if parsing_succeeded:
                            logger.info(
                                f"JSON repair successful using '{repair_method}': "
                                f"recovered {repair_info.get('fields_recovered', 0)} fields"
                            )
                    else:
                        # Repair failed - store raw output
                        logger.error(
                            f"JSON repair failed: {repair_info.get('error', 'unknown error')}. "
                            f"Raw output preview: {extracted_text[:500]}..."
                        )
                        extracted_fields = {"raw_output": extracted_text}
                        parsing_succeeded = False

            # Multi-instance detection probe (#753): read and REMOVE the auxiliary
            # count before anything else looks at the result. It must come out
            # ahead of the off-schema filter (which would otherwise report it as a
            # hallucinated field), the integrated-confidence split, coercion,
            # validation and every downstream stage. Popped unconditionally — even
            # if detection is off for this section, an echoed field must not reach
            # inference_result.
            probe_value = self._read_instance_probe(extracted_fields)
            if probe_value is not None:
                instance_probe = probe_value

            # Multi-instance (#715): re-express a response that ignored the
            # wrapper, BEFORE the off-schema filter deletes every key and emits
            # {}.
            if parsing_succeeded:
                extracted_fields, recovered_instances = (
                    self._adapt_to_instances_wrapper(
                        extracted_fields, recovered_instances
                    )
                )

            # Strip the probe from EVERY instance, from whichever container ends up
            # holding them, and do it AFTER the adapt above.
            #
            # This ran before the adapt and only over `recovered_instances`, which
            # leaked: for a flagged class answering with a bare array, the adapt
            # moves the records into `extracted_fields["instances"]` and returns
            # `recovered_instances = None`, so the loop never ran — and
            # `_filter_extracted_to_schema` only filters TOP-LEVEL keys, so the
            # probe survived inside `inference_result["instances"][1..N]` to be
            # scored by assessment, written to a reporting column and diffed
            # against a baseline that has no such key. Element 0 looked clean
            # because it is aliased to `extracted_fields`, which is why the
            # isolated unit tests passed.
            nested_probe = self._strip_probe_from_instances(
                extracted_fields, recovered_instances
            )
            if instance_probe is None and nested_probe is not None:
                instance_probe = nested_probe

            # Schema-compliance filter (simple/traditional extraction only): drop
            # any top-level fields the model returned that the class schema does
            # NOT define. Unlike Advanced (agentic) extraction — which validates
            # through a Pydantic model that structurally ignores off-schema keys —
            # the simple path does a raw ``json.loads`` and keeps whatever the model
            # emits, so hallucinated/cross-class attributes (e.g. a ``resume`` with
            # ``publications`` or a ``scientific_publication`` with ``invoice_number``)
            # flow downstream and later break assessment (the enhancer collapses a
            # list-for-non-array field, leaving its rows permanently unscored). This
            # closes that gap for simple mode at zero extra model cost; the dropped
            # names are recorded in metadata and surfaced as an
            # ``extraction_off_schema_fields`` issue. Runs only when parsing produced
            # a real object (not an error/raw_output sentinel) and a schema exists.
            if parsing_succeeded and isinstance(extracted_fields, dict):
                extracted_fields = self._filter_extracted_to_schema(extracted_fields)

            # Non-agentic INTEGRATED confidence: the single extraction inference
            # was asked (via the 1S-TopK prompt) to return, per field, its top-K
            # guesses with probabilities (G1/P1 … GK/PK). Split it so
            # inference_result holds only the values (G1) and the confidence (P1)
            # rides in metering under the same marker the agentic path uses
            # (lifted in _save_results). This lets the standalone Assessment step
            # be skipped (no 2nd pass); a flat response with no candidates passes
            # through untouched so the standalone step runs as the fallback.
            if self._integrated_assessment_enabled() and isinstance(
                extracted_fields, dict
            ):
                extracted_fields = self._split_inline_confidence(
                    extracted_fields, metering
                )

            # Deterministic type/format repair, then full-schema validation.
            #
            # Order matters twice over. Coercion before validation, so validation
            # only reports what could NOT be fixed for free. And BOTH after the
            # integrated-confidence split above: before it, a 1S-TopK response is
            # {field: {"G1": ..., "P1": ...}} — candidate objects, not values — so
            # coercion would refuse every field as a type-family mismatch (a
            # silent no-op) and validation would report every field as failing.
            # At the shipped default (validation on, fail_action=warn) that is
            # 100% false failures on every section, which buries the real
            # violations this exists to surface; under `reject` every section
            # fails; under `escalate` every field of every section is re-extracted
            # with the stronger model, and the plain value merged back would
            # corrupt the candidate shape resolve_candidates reads. The split
            # leaves resolved values that match the class schema, which is exactly
            # what both steps expect.
            if parsing_succeeded and isinstance(extracted_fields, dict):
                extracted_fields, coercion_metadata = self._coerce_simple_result(
                    extracted_fields
                )
                if coercion_metadata is not None:
                    self._pending_coercion_metadata = coercion_metadata

                (
                    extracted_fields,
                    validation_metadata,
                    parsing_succeeded,
                ) = self._validate_simple_result(
                    extracted_fields=extracted_fields,
                    content=content,
                    system_prompt=system_prompt,
                    model_id=model_id,
                    metering=metering,
                    section_info=section_info,
                    parsing_succeeded=parsing_succeeded,
                )
                if validation_metadata is not None:
                    self._pending_validation_metadata = validation_metadata

        total_duration = time.time() - request_start_time
        logger.info(f"Time taken for extraction: {total_duration:.2f} seconds")

        return ExtractionResult(
            extracted_fields=extracted_fields,
            metering=metering,
            parsing_succeeded=parsing_succeeded,
            total_duration=total_duration,
            output_truncated=output_truncated,
            output_repaired=output_repaired,
            repair_method=repair_method,
            schema_analysis=schema_analysis,
            ocr_analysis=ocr_analysis,
            instance_count=instance_count,
            recovered_instances=recovered_instances,
            instance_probe=instance_probe,
        )

    def _split_inline_confidence(
        self, parsed: dict[str, Any], metering: dict[str, Any]
    ) -> dict[str, Any]:
        """Split a simple-path integrated response into values + confidence.

        The simple (non-agentic) integrated prompt is the 1S-TopK prompt: it asks
        the model to return, per field, its top-K guesses with probabilities
        (``G1/P1`` … ``GK/PK``). Two shapes result:

        1. 1S-TopK: each field value is a ``{G1, P1, G2, P2, ...}`` candidate
           object (or, for arrays, per-row/per-column candidates).
           ``resolve_candidates()`` takes ``G1`` as the value and ``P1`` as the
           confidence, splitting the response into ``inference_result`` + raw
           per-field confidence leaves. The confidence is stashed in
           ``metering["_integrated_field_assessment"]`` (enriched with thresholds
           and emitted as ``explainability_info`` by ``_save_results``, so the
           standalone Assessment step is skipped — integrated mode's whole point
           is one inference, not two). The full candidate set is preserved in
           ``metering["_topk_candidates"]`` for auditability.
        2. Flat: no ``G1``/``P1`` candidates recognized (the model ignored the
           TopK contract). Return as-is and stash nothing; the standalone
           Assessment step then runs as the fallback (no regression).
        """
        # 1S-TopK candidate format — each field is {G1, P1, G2, P2, ...} (or,
        # for arrays, candidates nested per row/column). resolve_candidates()
        # splits into values (G1) + raw confidence (P1); thresholds/alerts are
        # attached later by the shared enricher in _save_results.
        if is_topk_response(parsed):
            inference_result, assessment_data, candidates_meta = resolve_candidates(
                parsed, self._class_schema
            )
            if assessment_data:
                metering["_integrated_field_assessment"] = assessment_data
                logger.info(
                    "Non-agentic integrated confidence (1S-TopK): resolved "
                    "%d fields from candidate format",
                    len(assessment_data),
                )
            # Stash raw candidates for auditability in metadata.
            metering["_topk_candidates"] = candidates_meta
            return inference_result

        # Flat response (no recognizable TopK candidates) — nothing to lift; the
        # standalone Assessment step will run as the fallback.
        logger.info(
            "Non-agentic integrated mode: response was flat (no TopK "
            "candidates); standalone Assessment step will run."
        )
        return parsed

    def _apply_missing_field_handling(
        self,
        extracted_fields: dict[str, Any],
        class_schema: dict[str, Any],
        section_info: SectionInfo,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Mark or remove fields whose source pages were not present.

        Returns the (possibly mutated) extracted_fields and a report listing
        the fields treated as MISSING. When the feature is disabled, the
        config doesn't declare page-types, or no top-level property has
        ``x-aws-idp-source-page-types``, the input is returned unchanged
        with an empty report.
        """
        cfg = self.config.extraction.missing_field_handling
        presence = section_info.page_type_presence
        if not cfg.enabled or presence is None or not presence.declared:
            return extracted_fields, []

        properties = class_schema.get(SCHEMA_PROPERTIES) or {}
        if not properties:
            return extracted_fields, []

        present = presence.present_page_types
        report: list[dict[str, Any]] = []
        # Operate on a copy so we don't mutate the agent's result in place.
        fields_out = dict(extracted_fields)

        for prop_name, prop_schema in properties.items():
            if not isinstance(prop_schema, dict):
                continue
            declared_pages = prop_schema.get(X_AWS_IDP_SOURCE_PAGE_TYPES)
            if not declared_pages:
                continue
            if not isinstance(declared_pages, list):
                logger.warning(
                    "Property %s declares %s as %s; expected list — ignoring",
                    prop_name,
                    X_AWS_IDP_SOURCE_PAGE_TYPES,
                    type(declared_pages).__name__,
                )
                continue
            if any(p in present for p in declared_pages):
                # At least one source page-type is present → field is BLANK
                # if empty, not MISSING. Leave it alone.
                continue
            report.append(
                {
                    "field": prop_name,
                    "reason": "page types not present",
                    "expected_page_types": list(declared_pages),
                }
            )
            if cfg.representation == "omit":
                fields_out.pop(prop_name, None)
            else:  # null_with_metadata
                fields_out[prop_name] = None

        return fields_out, report

    @staticmethod
    def _normalize_table_parsing_stats(stats: dict[str, Any]) -> dict[str, Any]:
        """Sanitize merged table-parsing stats for display in metadata.

        Drops the internal ``_rate_weight`` accumulator and defensively clamps
        the quality metrics to their valid ranges (rate 0-1, confidence 0-100) so
        a stats-merge regression can never again surface impossible values like
        a 500% parse-success rate or 496% confidence in the Processing Report.
        """
        clean = {k: v for k, v in stats.items() if k != "_rate_weight"}
        rate = clean.get("parse_success_rate")
        if isinstance(rate, (int, float)):
            clean["parse_success_rate"] = max(0.0, min(1.0, float(rate)))
        conf = clean.get("avg_confidence")
        if isinstance(conf, (int, float)):
            clean["avg_confidence"] = max(0.0, min(100.0, float(conf)))
        return clean

    def _attach_explainability(
        self,
        *,
        output_metadata: dict[str, Any],
        merged_assessment: dict[str, Any],
        merged_assessment_alerts: list[dict[str, Any]],
        extracted_fields: dict[str, Any],
        document: Document,
        section: Any,
        section_info: SectionInfo,
    ) -> None:
        """Ground the merged in-shard assessment and stage it for output.

        Grounds field geometry in real OCR data over the whole section (one pass,
        same as the standalone Assessment step), stashes the grounded assessment
        in ``self._grounded_assessment`` for ``_save_results`` to emit as
        ``explainability_info``, sets ``section.confidence_threshold_alerts``, and
        records lightweight metadata mirroring the standalone path. Best-effort:
        grounding never fails extraction.
        """
        # AUTHORITATIVE alignment point: reconcile the fully-merged assessment
        # against the fully-merged, final extracted_fields here — AFTER shard
        # merge (which drops phantom rows from data) and any escalation. Doing it
        # only per-shard is not enough: the data list is phantom-filtered on
        # merge, so per-shard counts can drift from the final data. This single
        # post-merge reconcile guarantees explainability_info[field][i] lines up
        # with inference_result[field][i] for every list field.
        merged_assessment = self._reconcile_assessment_to_data(
            merged_assessment, extracted_fields
        )

        # Robustness: INTEGRATED confidence comes inline from the extraction
        # inference, which can silently drop some list rows (esp. single-shot,
        # cramming values+confidence in one call). Those rows are currently
        # null-confidence placeholders. Re-assess ONLY the missing rows with a
        # focused standalone confidence call and splice real scores back, so
        # integrated large-list coverage reaches 100% like the separate path
        # (which retries inside assess_results_batched). No-op when nothing is
        # missing. Best-effort: never fails extraction.
        if self._integrated_assessment_enabled():
            try:
                merged_assessment, extra_alerts, split_stats = (
                    self._retry_missing_integrated_rows(
                        merged_assessment=merged_assessment,
                        extracted_fields=extracted_fields,
                        section_info=section_info,
                    )
                )
                if extra_alerts:
                    merged_assessment_alerts = list(merged_assessment_alerts) + (
                        extra_alerts
                    )
                # Surface adaptive batch-splitting activity (only when the
                # confidence model truncated and batches had to shrink).
                from idp_common.assessment.batching import split_stats_are_notable

                if split_stats_are_notable(split_stats):
                    output_metadata["assessment_batch_split_stats"] = split_stats
            except Exception as e:  # noqa: BLE001 - retry is advisory
                logger.warning("Integrated missing-row retry failed: %s", e)

        grounded = merged_assessment
        geometry_mode = self.config.extraction.geometry.mode
        if geometry_mode not in ("llm", "off"):
            try:
                from idp_common.assessment.ocr_grounding import (
                    ground_assessment_geometry,
                    load_page_ocr_data,
                )

                page_data_by_page = load_page_ocr_data(
                    document.pages, section_info.sorted_page_ids
                )
                if page_data_by_page:
                    # skip_grounded: in the sharded path each shard already
                    # grounded its own rows against its own pages (in
                    # _build_assess_runner), so this post-merge pass only needs to
                    # ground residual leaves (e.g. reconcile-padded placeholder
                    # rows) — the O(rows × all-pages) full-section fuzzy sweep that
                    # blew the merge Lambda's 900s ceiling is skipped. On the
                    # non-sharded single-agent path no leaf is pre-grounded, so
                    # everything is grounded here exactly as before.
                    grounded = ground_assessment_geometry(
                        merged_assessment,
                        extracted_fields,
                        page_data_by_page,
                        geometry_mode,
                        self._class_schema,
                        skip_grounded=True,
                    )
            except Exception as e:  # noqa: BLE001 - grounding is advisory
                logger.warning(
                    "OCR geometry grounding failed for in-shard assessment "
                    "(keeping existing boxes): %s",
                    e,
                )

        self._grounded_assessment = grounded
        section.confidence_threshold_alerts = merged_assessment_alerts
        output_metadata["assessment_integrated_in_extraction"] = True
        output_metadata["assessment_alert_count"] = len(merged_assessment_alerts)

    def _retry_missing_integrated_rows(
        self,
        *,
        merged_assessment: dict[str, Any],
        extracted_fields: dict[str, Any],
        section_info: SectionInfo,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
        """Re-score list rows the integrated extraction inference left unscored.

        Integrated confidence rides inline on the extraction call, which can drop
        rows on large tables (leaving null-confidence placeholders after reconcile).
        For each list field with missing rows, run a focused standalone confidence
        pass over ONLY those rows + the shared section context, then splice the
        recovered per-row scores back by index. Bounded to 2 rounds; sequential.

        Each retry chunk goes through the shared ``_assess_slice_adaptive`` helper,
        so a chunk the confidence model TRUNCATES (e.g. Nova Lite over an
        ``llm``-geometry batch too large for its output cap) is recursively halved
        and re-assessed until it fits — the same recovery the ``separate`` path
        gets inside ``assess_results_batched``. When rows STILL truncate after
        shrinking, the shared self-healing ladder escalates the residual rows to
        a stronger confidence model (bigger output cap) — the same ladder the
        ``separate`` path uses — so integrated mode is equally robust.

        Returns ``(merged_assessment, new_alerts, split_stats)`` where
        ``split_stats`` records any adaptive-splitting/escalation activity (None
        when nothing was retried). Best-effort — a failed retry keeps placeholders.
        """
        from idp_common.assessment.batching import (
            _missing_row_indices,
            _new_split_stats,
            _retry_missing_rows,
            compute_token_aware_batch_size,
            enrich_assessment_with_thresholds,
        )
        from idp_common.assessment.service import AssessmentService

        # Which list fields still have unscored rows?
        targets = {
            f: _missing_row_indices(merged_assessment.get(f), v)
            for f, v in extracted_fields.items()
            if isinstance(v, list) and _missing_row_indices(merged_assessment.get(f), v)
        }
        if not targets:
            return merged_assessment, [], None

        assessment_service = AssessmentService(region=self.region, config=self.config)
        confidence_cfg = self.config.extraction.confidence
        default_threshold = self.config.hitl.confidence_threshold
        new_alerts: list[dict[str, Any]] = []
        configured_batch_size = confidence_cfg.list_batch_size
        geometry_mode = self.config.extraction.geometry.mode
        split_stats = _new_split_stats()
        split_stats["configured_batch_size"] = configured_batch_size

        # 1.2 Resolve the escalation model (per-class override > global) so the
        # ladder can rescue rows a small-cap confidence model keeps truncating.
        escalation_model = assessment_service._resolve_confidence_escalation_model(
            section_info.class_label
        )
        escalation_enabled = confidence_cfg.escalation_enabled

        def _one_call(results: dict[str, Any]) -> Any:
            return assessment_service.assess_results(
                class_label=section_info.class_label,
                extraction_results=results,
                document_text=self._document_text,
                page_images=self._page_images,
                ocr_text_confidence="",
            )

        escalation_one_call: Callable[[dict[str, Any]], Any] | None = None
        if (
            escalation_enabled
            and escalation_model
            and confidence_cfg.max_escalation_rounds
        ):

            def _escalation_call(results: dict[str, Any]) -> Any:
                return assessment_service.assess_results(
                    class_label=section_info.class_label,
                    extraction_results=results,
                    document_text=self._document_text,
                    page_images=self._page_images,
                    ocr_text_confidence="",
                    model_id_override=escalation_model,
                )

            escalation_one_call = _escalation_call

        for field, _missing in targets.items():
            rows = extracted_fields[field]
            # 1.1 Token-aware first-pass size for this field's confidence model.
            sample_row = rows[0] if rows else None
            batch_size = compute_token_aware_batch_size(
                confidence_cfg.model, sample_row, geometry_mode, configured_batch_size
            )
            if split_stats["derived_batch_size"] is None:
                split_stats["derived_batch_size"] = batch_size
            esc_batch = (
                compute_token_aware_batch_size(
                    escalation_model, sample_row, geometry_mode, configured_batch_size
                )
                if escalation_one_call is not None
                else batch_size
            )
            # Reuse the shared self-healing ladder: same-model retry rounds, then
            # model escalation for whatever still truncates.
            merged_assessment, _field_alerts, _metering, _dur = _retry_missing_rows(
                _one_call,
                extraction_results=extracted_fields,
                big_field=field,
                merged_assessment=merged_assessment,
                merged_alerts=new_alerts,
                merged_metering={},
                batch_size=batch_size,
                max_retries=2,
                split_stats=split_stats,
                escalation_one_call=escalation_one_call,
                escalation_model=escalation_model if escalation_enabled else None,
                escalation_batch_size=esc_batch,
                max_escalation_rounds=confidence_cfg.max_escalation_rounds,
                deadline_epoch=self._assessment_deadline_epoch,
            )
            split_stats["unrecoverable_rows"] += len(
                _missing_row_indices(merged_assessment.get(field), rows)
            )

        # Re-enrich so any spliced-in rows carry confidence_threshold like the rest.
        merged_assessment, _ = enrich_assessment_with_thresholds(
            merged_assessment, self._class_schema, default_threshold
        )
        return merged_assessment, new_alerts, split_stats

    def _save_results(
        self,
        document: Document,
        section: Any,
        result: ExtractionResult,
        section_info: SectionInfo,
        section_id: str,
        t0: float,
    ) -> None:
        """
        Save extraction results to S3 and update document.

        Args:
            document: Document being processed
            section: Section being processed
            result: Extraction result
            section_info: Section metadata
            section_id: Section ID
            t0: Start time
        """
        # Determine extraction method
        extraction_method = (
            "agentic" if self.config.extraction.agentic.enabled else "traditional"
        )

        # Check if table parsing tool was used (extract from metering before building metadata)
        tool_used = False
        table_stats = None
        shard_conflicts = None
        merged_assessment = None
        merged_assessment_alerts = None
        assessment_split_stats = None
        if extraction_method == "agentic" and result.metering:
            table_stats = result.metering.pop("_table_parsing_stats", None)
            tool_used = table_stats is not None
            if table_stats:
                table_stats = self._normalize_table_parsing_stats(table_stats)
            # Scalar conflicts surfaced by sharded concurrent extraction.
            shard_conflicts = result.metering.pop("_shard_scalar_conflicts", None)
            # Per-field confidence/bbox assessment collated from in-shard
            # assessment (integrated-assessment feature). Popped here so it does
            # not leak into metering; emitted as explainability_info below.
            merged_assessment = result.metering.pop("_merged_assessment", None)
            merged_assessment_alerts = result.metering.pop(
                "_merged_assessment_alerts", None
            )
            # Adaptive batch-splitting activity from the in-shard assessment
            # (present only when the confidence model truncated its output and
            # batches had to shrink). Popped so it doesn't leak into metering.
            assessment_split_stats = result.metering.pop(
                "_merged_assessment_split_stats", None
            )
        elif result.metering and self._integrated_assessment_enabled():
            # Non-agentic INTEGRATED confidence: the extraction inference emitted
            # confidence inline (split into this marker by _split_inline_confidence).
            # Enrich the raw confidence leaves with per-field thresholds + alerts
            # (same contract the standalone/separate path produces) and route it
            # through the shared ground+emit path so explainability_info is written
            # and the standalone Assessment step is skipped.
            inline = result.metering.pop("_integrated_field_assessment", None)
            if isinstance(inline, dict) and inline:
                from idp_common.assessment.batching import (
                    enrich_assessment_with_thresholds,
                )

                default_threshold = self.config.hitl.confidence_threshold
                merged_assessment, merged_assessment_alerts = (
                    enrich_assessment_with_thresholds(
                        inline, self._class_schema, default_threshold
                    )
                )

        # Pop 1S-TopK raw candidates (all K guesses per field) for metadata
        # auditability. Present only on the simple integrated (TopK) path; popped
        # here so it does not leak into metering.
        topk_candidates = (
            result.metering.pop("_topk_candidates", None) if result.metering else None
        )

        # Build base metadata
        metadata: dict[str, Any] = {
            "parsing_succeeded": result.parsing_succeeded,
            "extraction_time_seconds": result.total_duration,
            "extraction_method": extraction_method,
        }

        # How many documents of this class the section turned out to hold, and —
        # when more than one — every instance the model produced. inference_result
        # keeps the first so the output shape is unchanged; these carry the rest
        # so nothing is discarded, and _build_extraction_issues turns the count
        # into a user-visible warning.
        instance_count, instance_meta = self._resolve_instance_reporting(result)
        metadata.update(instance_meta)

        # Assigned unconditionally, including 0. A re-extraction (reprocess, or a
        # reclassify that reuses the section) starts from the RESTORED section,
        # which still carries the previous run's count — so a guarded write would
        # leave a stale "3 documents" badge on a section this run found one
        # document in, or failed on entirely. 0 renders as "-" (undetermined),
        # which is the honest reading of a run that determined nothing.
        section.instance_count = instance_count
        if result.recovered_instances:
            metadata["recovered_instances"] = result.recovered_instances
            # Surface on the dashboard so multi-document sections are visible
            # without opening a document.
            try:
                metrics.put_metric("MultiInstanceSections", 1)
                metrics.put_metric(
                    "MultiInstanceRecordsRecovered", len(result.recovered_instances)
                )
            except Exception as e:  # noqa: BLE001 - telemetry must never fail a doc
                logger.debug("Could not publish multi-instance metrics: %s", e)

        # Record 1S-TopK raw candidates for auditability (all K guesses per
        # field). assessment_method is an audit breadcrumb only — the downstream
        # Assessment Lambda skips on explainability_info presence, not this field.
        if topk_candidates:
            metadata["topk_candidates"] = topk_candidates
            metadata["assessment_method"] = "1s_topk"

        # Audit: which model actually ran this section, and whether it came from
        # a per-class override vs the global extraction model.
        if self._pending_extraction_model is not None:
            metadata["extraction_model"] = self._pending_extraction_model
            metadata["extraction_model_overridden"] = (
                self._class_schema.get(X_AWS_IDP_EXTRACTION_MODEL) is not None
            )

        # Add pre-flight analysis results (if agentic)
        if extraction_method == "agentic":
            if result.schema_analysis:
                metadata["schema_analysis"] = result.schema_analysis
            if result.ocr_analysis:
                metadata["ocr_analysis"] = result.ocr_analysis

            # Track tool usage decision
            tool_expected = False
            if result.schema_analysis or result.ocr_analysis:
                tool_expected = (
                    result.schema_analysis.get("tool_usage_recommended", False)
                    if result.schema_analysis
                    else False
                ) or (
                    result.ocr_analysis.get("tool_usage_recommended", False)
                    if result.ocr_analysis
                    else False
                )

                # Facts that determine WHY the tool was or wasn't used, so the
                # report can distinguish "tool unavailable" from "available but the
                # agent declined it":
                #  - tool_enabled: is the deterministic parser turned on in config?
                #  - ocr_had_markdown_tables: did the OCR text actually contain
                #    Markdown pipe-tables? The parser can ONLY run on those, and
                #    ocr_analysis (a pre-flight scan of the OCR text) is what detects
                #    them — so this is known before extraction, regardless of which
                #    Textract features produced the text.
                tool_enabled = self.config.extraction.agentic.table_parsing.enabled
                ocr_had_markdown_tables = bool(
                    result.ocr_analysis
                    and result.ocr_analysis.get("tables_detected", 0) > 0
                )
                # Agent's optional self-reported rationale for skipping the tool
                # (captured from its final message). Only meaningful when it was
                # recommended but not used.
                agent_note = getattr(self, "_agent_table_tool_note", None)
                decision: dict[str, Any] = {
                    "expected": tool_expected,
                    "actual": tool_used,
                    "mismatch": tool_expected != tool_used,
                    "tool_enabled": tool_enabled,
                    "ocr_had_markdown_tables": ocr_had_markdown_tables,
                    "explanation": self._explain_tool_usage_decision(
                        expected=tool_expected,
                        actual=tool_used,
                        schema_analysis=result.schema_analysis,
                        ocr_analysis=result.ocr_analysis,
                        tool_enabled=tool_enabled,
                        ocr_had_markdown_tables=ocr_had_markdown_tables,
                        agent_note=agent_note,
                    ),
                }
                if agent_note and tool_expected and not tool_used:
                    decision["agent_stated_reason"] = agent_note
                metadata["tool_usage_decision"] = decision

            # Validate completeness
            if result.schema_analysis or result.ocr_analysis:
                metadata["completeness_check"] = self._check_completeness_detailed(
                    extracted_fields=result.extracted_fields,
                    schema=self._class_schema,
                    tool_used=tool_used,
                    ocr_analysis=result.ocr_analysis,
                )

        # Population heuristic: flag suspiciously sparse extractions (e.g.
        # nested fields silently returning null). Advisory only.
        #
        # Deliberately OUTSIDE the `extraction_method == "agentic"` block above.
        # It used to run only on the agentic path, which is backwards: simple mode
        # is the one with no validation step and the one where the silent
        # completeness cliff lives (recall 1.000 through ~800 list rows, then 0.199
        # at 1,200 and 0.009 at 3,200 — reported as success either way). The
        # consumer that turns this into a surfaced ProcessingIssue was already
        # mode-agnostic; only the producer was gated.
        if self._class_schema:
            metadata["population_check"] = self._check_population_completeness(
                extracted_fields=result.extracted_fields,
                schema=self._class_schema,
            )

        # Add table parsing stats if tool was used
        if tool_used and table_stats:
            metadata["table_parsing_tool_used"] = True
            metadata["table_parsing_stats"] = table_stats
        elif (
            extraction_method == "agentic"
            and self.config.extraction.agentic.table_parsing.enabled
        ):
            metadata["table_parsing_tool_used"] = False

        # Surface adaptive assessment batch-splitting activity (only when the
        # confidence model truncated output and batches had to shrink).
        from idp_common.assessment.batching import split_stats_are_notable

        if split_stats_are_notable(assessment_split_stats):
            metadata["assessment_batch_split_stats"] = assessment_split_stats

        # Item 3: record HOW the document was split/sized so the processing report
        # (and the UI process graph) can show it. Emit the model-aware sizing plan
        # + the shard count for this section. Best-effort.
        if extraction_method == "agentic":
            try:
                plan = self._get_sizing_plan()
                metadata["sizing_plan"] = plan.to_dict()
            except Exception as e:  # noqa: BLE001 - reporting only
                logger.debug("Could not record sizing_plan: %s", e)
            shard_meta = (
                result.metering.pop("_shard_process_trace", None)
                if result.metering
                else None
            )
            if shard_meta:
                metadata["shard_trace"] = shard_meta

        # Add truncation/repair metadata when relevant
        if result.output_truncated:
            metadata["output_truncated"] = True
        if result.output_repaired:
            metadata["output_repaired"] = True
            metadata["repair_method"] = result.repair_method

        # Add full-schema validation outcome (set by _validate_and_maybe_escalate
        # on the agentic path, or _validate_simple_result on the simple path, when
        # extraction.validation.enabled).
        if self._pending_validation_metadata is not None:
            metadata["validation"] = self._pending_validation_metadata
        if self._pending_forced_tool_metadata is not None:
            # Recorded so an A/B can tell "forcing had no effect" from "forcing
            # never ran" — a skipped route or an unhonored force are both normal
            # and both invisible without this.
            metadata["forced_tool"] = self._pending_forced_tool_metadata

        # Deterministic type/format repairs. Recorded so a human can audit exactly
        # what was rewritten and what was refused — coercion must never silently
        # alter extracted document data.
        if self._pending_coercion_metadata is not None:
            metadata["coercion"] = self._pending_coercion_metadata

        # Record scalar-field conflicts detected when merging sharded concurrent
        # extraction (two shards disagreed on a scalar; first value kept).
        if shard_conflicts:
            metadata["shard_scalar_conflicts"] = shard_conflicts

        # Apply BLANK vs MISSING field handling (no-op unless configured + declared).
        fields_for_output, missing_fields_report = self._apply_missing_field_handling(
            result.extracted_fields,
            self._class_schema,
            section_info,
        )

        # In-shard assessment (integrated-assessment feature): ground the merged
        # per-field assessment in real OCR geometry over the WHOLE section (one
        # pass, identical to the standalone Assessment step) and emit it as
        # explainability_info — the same output contract the standalone path
        # produces, so all downstream consumers (HITL, UI, evaluation) are
        # unchanged. No-op when in-shard assessment did not run. Runs BEFORE the
        # processing report so any adaptive batch-splitting it records
        # (assessment_batch_split_stats) is reflected in the report too.
        if merged_assessment:
            # Align against fields_for_output (what becomes inference_result), so
            # explainability_info[field][i] matches inference_result[field][i]
            # exactly — including after missing-field handling.
            self._attach_explainability(
                output_metadata=metadata,
                merged_assessment=merged_assessment,
                merged_assessment_alerts=merged_assessment_alerts or [],
                extracted_fields=fields_for_output,
                document=document,
                section=section,
                section_info=section_info,
            )

        # Structured issues (1.4) + completeness gate (1.3): translate the
        # in-shard assessment's split_stats + a coverage audit of the grounded
        # assessment into user-surfacing ProcessingIssues attached to the section.
        section_issues: list[Any] = []
        if merged_assessment is not None:
            from idp_common.assessment.batching import (
                audit_explainability,
                build_assessment_issues,
            )

            geometry_mode = self.config.extraction.geometry.mode
            _gaps, audit_issues = audit_explainability(
                self._grounded_assessment,
                fields_for_output,
                geometry_mode=geometry_mode,
                section_id=section_id,
            )
            section_issues = (
                build_assessment_issues(
                    metadata.get("assessment_batch_split_stats"),
                    section_id=section_id,
                    confidence_model=self.config.extraction.confidence.model,
                    geometry_mode=geometry_mode,
                )
                + audit_issues
            )

        # Extraction-completeness issue (BOTH modes): flag empty / suspiciously
        # sparse extractions — e.g. a large list schema field that came back
        # empty (the exact simple-mode failure on large tables). Recommends
        # Advanced (agentic) extraction when Simple under-produced.
        section_issues += self._build_extraction_issues(
            extracted_fields=fields_for_output,
            metadata=metadata,
            section_id=section_id,
        )

        # Replace the issues THIS step owns, unconditionally — including with an
        # empty list. A re-extraction starts from the restored section, which still
        # carries the previous run's warnings, so a guarded write left a stale
        # "list came back empty" on a section this run extracted correctly. Issues
        # from other stages (OCR, classification) are preserved: the DynamoDB
        # writer replaces the whole section map, so dropping them here would
        # delete them.
        owned_stages = {"extraction", "assessment"}
        preserved = [
            issue
            for issue in (section.processing_issues or [])
            if getattr(issue, "stage", None) not in owned_stages
        ]
        section.processing_issues = preserved + section_issues
        if section.processing_issues:
            metadata["processing_issues"] = [
                pi.to_dict() for pi in section.processing_issues
            ]
        else:
            metadata.pop("processing_issues", None)

        # Normalized processing-flow summary: ONE object capturing every stage
        # decision so the report (text + UI graph) depicts the whole path
        # systematically — extraction mode, sharding, table-tool use+rationale,
        # confidence source (LLM vs deterministic) + batching/concurrency, geometry
        # source (OCR-grounded vs LLM box), model escalations, and what (if
        # anything) was recovered by retry. Built for BOTH simple and advanced so
        # the UI always has a flow to render. Best-effort; consumers tolerate
        # missing keys.
        try:
            metadata["processing_flow"] = self._build_processing_flow(
                metadata, extraction_method, tool_used
            )
        except Exception as e:  # noqa: BLE001 - reporting only
            logger.debug("Could not build processing_flow: %s", e)

        # Generate user-friendly processing report
        processing_report = self._generate_processing_report(metadata)
        logger.info(f"Processing Report:\n{processing_report}")

        # Write to S3 with processing report
        output: dict[str, Any] = {
            "document_class": {"type": section_info.class_label},
            "split_document": {"page_indices": section_info.page_indices},
            "inference_result": fields_for_output,
            "metadata": metadata,
            "processing_report": processing_report,
        }
        if merged_assessment is not None:
            output["explainability_info"] = [self._grounded_assessment]
        # Surface page-type resolution and the missing-field report when the
        # feature is in use, so downstream consumers can act on the signal.
        presence = section_info.page_type_presence
        if presence is not None and presence.declared:
            output["page_type_resolution"] = presence.to_output_dict()
        if missing_fields_report:
            output["missing_fields_report"] = missing_fields_report
            if self.config.extraction.missing_field_handling.representation == (
                "null_with_metadata"
            ):
                output["missing_fields"] = [
                    entry["field"] for entry in missing_fields_report
                ]
        s3.write_content(
            output,
            section_info.output_bucket,
            section_info.output_key,
            content_type="application/json",
        )

        # Update section and document
        section.extraction_result_uri = section_info.output_uri
        document.metering = utils.merge_metering_data(
            document.metering, result.metering or {}
        )

        t3 = time.time()
        logger.info(
            f"Total extraction time for section {section_id}: {t3 - t0:.2f} seconds"
        )

    def process_document_section(
        self,
        document: Document,
        section_id: str,
        checkpoint_data: dict[str, Any] | None = None,
        deadline_epoch: float | None = None,
    ) -> Document:
        """
        Process a single section from a Document object.

        Args:
            document: Document object containing section to process
            section_id: ID of the section to process
            checkpoint_data: Optional partial extraction data from a previous
                timed-out invocation.  When provided the agentic agent will
                resume from this state instead of starting from scratch.
            deadline_epoch: Optional absolute epoch (seconds) by which this Lambda
                must finish (from ``context.get_remaining_time_in_millis()``).
                Threaded into the in-shard assessment self-healing ladder's
                wall-clock guard so escalation stops before a hard timeout (1.5).

        Returns:
            Document: Updated Document object with extraction results for the section
        """
        # Reset state
        self._reset_context()
        self._assessment_deadline_epoch = deadline_epoch

        # Validate and get section
        section = self._validate_and_find_section(document, section_id)
        if not section:
            return document

        # Short-circuit: skip sections whose class is marked
        # x-aws-idp-exclude-from-processing=true (e.g., static instruction
        # pages). A stub result.json is written so reporting / UI can show
        # a meaningful message.
        from idp_common.section_exclusion import (
            is_section_excluded,
            write_skipped_stub,
        )

        if is_section_excluded(section):
            output_bucket = document.output_bucket
            output_key = (
                f"{document.input_key}/sections/{section.section_id}/result.json"
                if document.input_key
                else None
            )
            stub_uri = write_skipped_stub(
                document,
                section,
                stage="extraction",
                output_bucket=output_bucket,
                output_key=output_key,
            )
            if stub_uri:
                section.extraction_result_uri = stub_uri
            logger.info(
                "Extraction skipped for excluded section %s (class=%s, reason=%s)",
                section.section_id,
                section.classification,
                section.exclusion_reason or "excluded",
            )
            return document

        # Prepare section metadata
        try:
            section_info = self._prepare_section_info(document, section)
        except ValueError:
            return document

        try:
            t0 = time.time()

            prepared = self._prepare_section_context(document, section, section_info)
            if prepared is None:
                # Empty schema — already handled (stub written).
                return self._handle_empty_schema(
                    document, section, section_info, section_id, t0
                )
            content, system_prompt = prepared

            # Invoke model (pass checkpoint_data for agentic resume-on-timeout)
            result = self._invoke_extraction_model(
                content, system_prompt, section_info, checkpoint_data=checkpoint_data
            )

            # Save results
            self._save_results(document, section, result, section_info, section_id, t0)

        except Exception as e:
            error_msg = f"Error processing section {section_id}: {str(e)}"
            logger.error(error_msg)
            document.errors.append(error_msg)
            raise

        return document

    def _prepare_section_context(
        self,
        document: Document,
        section: Section,
        section_info: SectionInfo,
    ) -> tuple[list[dict[str, Any]], str] | None:
        """Load page text/images, resolve page types, init context, build prompt.

        Populates the per-section instance state (``self._page_texts``,
        ``self._document_text``, ``self._page_images``, ``self._class_schema`` …)
        and returns ``(content, system_prompt)``. Returns ``None`` when the class
        schema is empty (caller writes the skip stub). Extracted from
        ``process_document_section`` so the SFN shard/merge entry points can reuse
        the identical preparation without duplicating it (single source of truth).
        """
        # Stash the document so per-shard grounding (in _build_assess_runner) can
        # load each shard's OCR pageData without threading `document` through
        # _invoke_extraction_model.
        self._document = document

        # Load per-page text first so the page-type resolver and the
        # prompt-formatter can both consume it without re-reading S3.
        page_id_to_text = self._load_page_texts(document, section_info.sorted_page_ids)
        class_schema_for_resolver = self._get_class_schema(section_info.class_label)
        section_info.page_type_presence = resolve_page_types(
            class_schema_for_resolver, page_id_to_text
        )
        if section_info.page_type_presence.declared:
            logger.info(
                "Page-type resolution for section %s: present=%s missing=%s",
                section.section_id,
                sorted(section_info.page_type_presence.present_page_types),
                sorted(section_info.page_type_presence.missing_page_types),
            )
        document_text = self._format_document_text(
            section_info.sorted_page_ids,
            page_id_to_text,
            section_info.page_type_presence,
        )
        page_images = self._load_document_images(document, section_info.sorted_page_ids)
        # Stash the per-page OCR text (in section page order) so the agentic
        # path can shard the input by page range when concurrent batches are
        # configured. Pages missing from page_id_to_text contribute "".
        self._page_texts = [
            page_id_to_text.get(pid, "") for pid in section_info.sorted_page_ids
        ]

        # Initialize extraction context
        class_schema, attribute_descriptions = self._initialize_extraction_context(
            section_info.class_label,
            document_text,
            page_images,
            section_info.sorted_page_ids,
            document,
        )

        # Handle empty schema case (signal to caller).
        if (
            not class_schema.get(SCHEMA_PROPERTIES)
            or not attribute_descriptions.strip()
        ):
            return None

        # Build prompt content
        content, system_prompt = self._build_extraction_content(document, page_images)

        # Load OCR confidence data for table parsing tool (if enabled)
        # Confidence data is only available from Textract OCR backend.
        # For Bedrock OCR or other backends, skip loading — the tool
        # handles missing confidence gracefully (confidence_available=false).
        if (
            AGENTIC_AVAILABLE
            and self.config.extraction.agentic.enabled
            and self.config.extraction.agentic.table_parsing.enabled
            and self.config.extraction.agentic.table_parsing.use_confidence_data
            and self.config.ocr.backend == "textract"
        ):
            confidence_data_by_page = self._load_confidence_data(
                document, section_info.sorted_page_ids
            )
            set_confidence_data(confidence_data_by_page)
            logger.info(
                f"Loaded OCR confidence data for table parsing tool: "
                f"{len(confidence_data_by_page)} pages"
            )
        elif AGENTIC_AVAILABLE and self.config.extraction.agentic.enabled:
            set_confidence_data(None)
            if (
                self.config.extraction.agentic.table_parsing.enabled
                and self.config.ocr.backend != "textract"
            ):
                logger.info(
                    "Table parsing tool enabled without confidence data "
                    f"(OCR backend: {self.config.ocr.backend})"
                )

        return content, system_prompt

    def _inshard_assessment_enabled(self) -> bool:
        """Whether the SEPARATE-mode in-shard assessment pass should run.

        True only when assessment is enabled AND the integration mode keeps a
        *separate* assessment inference (the second-turn per-shard path). The
        ``integrated`` (single-prompt) mode does NOT use this — the extraction
        inference itself emits confidence/bbox via ``_integrated_assessment_enabled``.
        Either way the standalone AssessmentStep is bypassed for the agentic path.
        """
        if not self.config.extraction.confidence.enabled:
            return False
        return self.config.extraction.confidence.mode == "separate"

    def _integrated_assessment_enabled(self) -> bool:
        """Whether the agent should emit confidence/bbox INLINE (single inference).

        True when confidence is enabled AND ``confidence.mode == "integrated"``.
        In this mode the extraction agent calls ``provide_field_assessment`` in its
        own session (document already in cached context — no second Bedrock pass),
        and the result rides the same collation/grounding/emit path as separate mode.
        """
        if not self.config.extraction.confidence.enabled:
            return False
        return self.config.extraction.confidence.mode == "integrated"

    @staticmethod
    def _reconcile_assessment_to_data(
        assessment: dict[str, Any], extraction_results: dict[str, Any]
    ) -> dict[str, Any]:
        """Force per-field assessment to index-align with the extracted data.

        Thin delegate to the shared
        :func:`idp_common.assessment.batching.reconcile_assessment_to_data` so the
        agentic in-shard path and the standalone Assessment step share one
        implementation. See that function for the full contract.
        """
        from idp_common.assessment.batching import reconcile_assessment_to_data

        return reconcile_assessment_to_data(assessment, extraction_results)

    def _assess_results_batched(
        self,
        assessment_service: Any,
        *,
        class_label: str,
        extraction_results: dict[str, Any],
        document_text: str,
        page_images: list[bytes],
    ) -> dict[str, Any]:
        """Assess one scope, batching large list fields across multiple inferences.

        Thin delegate to the shared
        :func:`idp_common.assessment.batching.assess_results_batched` (the same
        implementation the standalone Assessment step now uses) with the batch size
        read from ``extraction.confidence.list_batch_size``. Threads the confidence
        model + geometry mode (for token-aware first-pass sizing) and the resolved
        escalation model (for the self-healing ladder) so the agentic in-shard
        ``separate`` path is exactly as robust as the standalone step. Returns
        ``{"assessment", "alerts", "metering", "parsing_succeeded",
        "duration_seconds", "split_stats"}``.
        """
        from idp_common.assessment.batching import assess_results_batched

        confidence_cfg = self.config.extraction.confidence
        return assess_results_batched(
            assessment_service,
            class_label=class_label,
            extraction_results=extraction_results,
            document_text=document_text,
            page_images=page_images,
            batch_size=confidence_cfg.list_batch_size,
            confidence_model_id=confidence_cfg.model,
            geometry_mode=self.config.extraction.geometry.mode,
            escalation_enabled=confidence_cfg.escalation_enabled,
            escalation_model=assessment_service._resolve_confidence_escalation_model(
                class_label
            ),
            max_escalation_rounds=confidence_cfg.max_escalation_rounds,
            deadline_epoch=self._assessment_deadline_epoch,
            max_concurrent_batches=self.config.extraction.agentic.max_concurrent_batches,
            class_schema=assessment_service._get_class_schema(class_label),
        )

    def _build_assess_runner(
        self, section_info: SectionInfo, document: Document | None = None
    ) -> "Any | None":
        """Build the per-shard assess_runner closure (or None when disabled).

        The closure reuses ``AssessmentService.assess_results`` over a single
        shard's extracted values + that shard's already-loaded text/images, so
        the in-shard assessment is byte-for-byte the same logic the standalone
        Assessment step runs — just scoped to the shard and with no S3 round
        trip. Returns ``None`` when in-shard assessment is not enabled, leaving
        the default (no-assessment) extraction path completely unchanged.

        When ``document`` is provided and OCR geometry grounding is on
        (``geometry.mode`` not ``llm``/``off``), the shard also GROUNDS its own
        rows against ONLY its own pages' OCR data right here — so grounding
        scales per-shard exactly like the confidence assessment does, instead of
        being deferred to a single full-section sweep in the merge step (which is
        O(rows × all-pages) fuzzy matching and blew the merge Lambda's 900s ceiling
        on large tables). The merge step then re-grounds with ``skip_grounded=True``,
        a near-instant no-op over already-grounded leaves.
        """
        if not self._inshard_assessment_enabled():
            return None

        from idp_common.assessment.service import AssessmentService

        assessment_service = AssessmentService(region=self.region, config=self.config)
        class_label = section_info.class_label
        geometry_mode = self.config.extraction.geometry.mode
        sorted_page_ids = section_info.sorted_page_ids
        ground_in_shard = (
            document is not None
            and geometry_mode not in ("llm", "off")
            and bool(sorted_page_ids)
        )

        async def _assess_runner(
            *, extracted_fields: dict[str, Any], payload: dict[str, Any]
        ) -> dict[str, Any] | None:
            extraction_results = extracted_fields or {}
            if not extraction_results:
                return None
            document_text = payload.get("assess_document_text", "") or ""
            page_images = payload.get("assess_page_images", []) or []

            # assess_results does Bedrock I/O; run it off the event loop so the
            # concurrent shard scheduler is not blocked.
            import asyncio as _asyncio

            def _run() -> dict[str, Any]:
                # Batch large list fields across multiple inferences so the model
                # reliably enumerates every row (a single call over a big list
                # under-enumerates/omits it). Reconciliation inside keeps the
                # per-row assessment index-aligned with the shard's data.
                result = self._assess_results_batched(
                    assessment_service,
                    class_label=class_label,
                    extraction_results=extraction_results,
                    document_text=document_text,
                    page_images=page_images,
                )
                shard_assessment = result.get("assessment") or {}
                if ground_in_shard:
                    self._ground_shard_assessment(
                        assessment=shard_assessment,
                        extracted_fields=extraction_results,
                        payload=payload,
                        document=document,
                        section_info=section_info,
                        geometry_mode=geometry_mode,
                    )
                # Prune assessment rows for phantom DATA rows in lockstep with
                # what the merge drops from the data, so the persisted (already
                # grounded) per-shard assessment stays index-aligned with the
                # phantom-filtered merged data. MUST run AFTER grounding (grounding
                # needs full 1:1 alignment); it only shrinks, so grounded rows that
                # survive keep their boxes.
                from idp_common.extraction.runtime import (
                    _prune_phantom_rows_from_assessment,
                )

                _prune_phantom_rows_from_assessment(
                    extraction_results, shard_assessment
                )
                return result

            return await _asyncio.to_thread(_run)

        return _assess_runner

    def _ground_shard_assessment(
        self,
        *,
        assessment: dict[str, Any],
        extracted_fields: dict[str, Any],
        payload: dict[str, Any],
        document: Document,
        section_info: SectionInfo,
        geometry_mode: str,
    ) -> None:
        """Ground ONE shard's per-field assessment against ONLY its own pages.

        Loads OCR ``pageData.json`` for just the shard's page slice (keyed by the
        real global 1-indexed page number, exactly like the full-section path) and
        grounds the shard's assessment leaves in place. Scoping to the shard's
        pages both bounds the work (O(shard-rows × shard-pages) instead of
        O(all-rows × all-pages)) and improves correctness — a row's value can only
        appear on its own pages, so cross-page false matches are avoided. Row-order
        disambiguation of repeated values is naturally scoped to the shard's rows.
        Best-effort: grounding never fails the shard's assessment.
        """
        if not assessment:
            return
        try:
            from idp_common.assessment.ocr_grounding import (
                ground_assessment_geometry,
                load_page_ocr_data,
            )

            sorted_page_ids = section_info.sorted_page_ids
            page_start = payload.get("page_start", 0)
            page_end = payload.get("page_end", len(sorted_page_ids))
            shard_page_ids = sorted_page_ids[page_start:page_end]
            # page_offset=page_start so this shard's pages number relative to the
            # WHOLE section (section page 1 = section's first page), giving the same
            # section-relative geometry.page the merge/standalone paths produce.
            page_data_by_page = load_page_ocr_data(
                document.pages, shard_page_ids, page_offset=page_start
            )
            if not page_data_by_page:
                return
            # Observability: grounding is pure-CPU value→OCR-line matching; on a
            # large table it used to run for minutes SILENTLY (the exact-index fast
            # path now keeps it ~seconds, but log the size + duration so a
            # regression is never an invisible hang again — the user asked for every
            # heavy step to be traceable in CloudWatch).
            row_count = sum(
                len(v) for v in extracted_fields.values() if isinstance(v, list)
            )
            _t0 = time.time()
            ground_assessment_geometry(
                assessment,
                extracted_fields,
                page_data_by_page,
                geometry_mode,
                self._class_schema,
            )
            logger.info(
                "In-shard OCR grounding: pages %s-%s, %d page(s), ~%d list row(s) "
                "grounded in %.2fs (geometry=%s)",
                page_start,
                page_end,
                len(page_data_by_page),
                row_count,
                time.time() - _t0,
                geometry_mode,
            )
        except Exception as e:  # noqa: BLE001 - grounding is advisory
            logger.warning(
                "In-shard OCR grounding failed for pages %s-%s (keeping ungrounded): %s",
                payload.get("page_start"),
                payload.get("page_end"),
                e,
            )

    def _assess_single_agent(
        self,
        *,
        extracted_fields: dict[str, Any],
        section_info: SectionInfo,
        metering: dict[str, Any],
    ) -> None:
        """Run assessment over the whole section for the non-sharded agentic path.

        Equivalent to a single full-section shard: builds the assessment over the
        already-loaded section text/images (no S3 re-read) and writes the result
        into ``metering`` under the same ``_merged_assessment`` markers the
        sharded path uses, so ``_save_results`` grounds + emits it identically.
        Best-effort: assessment never fails extraction.
        """
        if not extracted_fields:
            return
        try:
            from idp_common.assessment.service import AssessmentService
            from idp_common.extraction.agentic_idp import _accumulate_metering

            assessment_service = AssessmentService(
                region=self.region, config=self.config
            )
            # Batch large list fields (same as the sharded path) so the model
            # reliably enumerates every row instead of omitting/under-counting it.
            batched = self._assess_results_batched(
                assessment_service,
                class_label=section_info.class_label,
                extraction_results=extracted_fields,
                document_text=self._document_text,
                page_images=self._page_images,
            )
            metering["_merged_assessment"] = batched["assessment"]
            metering["_merged_assessment_alerts"] = batched["alerts"]
            if batched.get("split_stats"):
                metering["_merged_assessment_split_stats"] = batched["split_stats"]
            _accumulate_metering(metering, batched["metering"])
        except Exception as e:  # noqa: BLE001 - assessment is advisory
            logger.warning(
                "Single-agent in-shard assessment failed (keeping extraction): %s", e
            )

    def _build_agentic_shard_plan(
        self, section_info: SectionInfo
    ) -> tuple[str, type, list[dict[str, Any]], str | None]:
        """Build the agentic shard plan for the SFN runtime.

        Returns ``(model_id, dynamic_model, shard_payloads, custom_instruction)``
        mirroring the construction in ``_invoke_extraction_model``'s agentic
        branch, but standalone so the per-shard SFN Lambda (and the merge step)
        can each rebuild the identical plan deterministically. Requires
        ``_prepare_section_context`` to have populated the per-section state.
        """
        class_model_override = self._class_schema.get(X_AWS_IDP_EXTRACTION_MODEL)
        model_id = class_model_override or self.config.extraction.model

        dynamic_model = create_pydantic_model_from_json_schema(
            schema=self._class_schema,
            class_label=section_info.class_label,
            clean_schema=False,
        )

        schema_analysis = self._analyze_schema_for_table_requirements(
            self._class_schema
        )
        ocr_analysis = self._analyze_ocr_for_tables(self._document_text)
        custom_instruction = self._build_table_parsing_guidance(
            schema_analysis=schema_analysis, ocr_analysis=ocr_analysis
        )

        prompt_template = self.config.extraction.task_prompt or ""
        send_images = "{DOCUMENT_IMAGE}" in prompt_template
        shard_payloads = self._build_shard_payloads(
            prompt_template=prompt_template,
            send_images=send_images,
            max_shards=self.config.extraction.agentic.max_concurrent_batches,
        )
        return model_id, dynamic_model, shard_payloads, custom_instruction

    def _persist_section_id(self, section_info: SectionInfo) -> str:
        """Deterministic per-section id used for shard persistence keys."""
        return (
            f"{section_info.class_label}_"
            f"{section_info.start_page}_{section_info.end_page}"
        )

    def run_one_section_shard(
        self,
        document: Document,
        section_id: str,
        shard_index: int,
        persistence: Any,
        deadline_epoch: float | None = None,
    ) -> dict[str, Any]:
        """Run ONE shard of a section — the nested-SFN Distributed Map body.

        Reuses ``extract_one_shard`` (idempotent skip-if-complete) over the SAME
        shard plan the in-process runtime builds. The shard's full result is
        persisted to S3 via ``persistence``; this returns a small descriptor so
        the Map collects only pointers. This is the SFN counterpart to one
        asyncio task in ``InProcessRuntime`` — same primitive, different scheduler.

        ``deadline_epoch`` (absolute epoch seconds, from the shard Lambda's
        ``context.get_remaining_time_in_millis()``) bounds the in-shard confidence
        self-healing ladder so a truncation-retry storm on a small-cap confidence
        model stops (with an ``assessment_deadline_reached`` warning, keeping
        recovered rows) instead of running the shard Lambda into its 900s wall.
        """
        import asyncio as _asyncio

        from idp_common.extraction.agentic_idp import default_shard_runner
        from idp_common.extraction.runtime import extract_one_shard

        self._reset_context()
        self._assessment_deadline_epoch = deadline_epoch
        section = self._validate_and_find_section(document, section_id)
        if not section:
            raise ValueError(f"Section {section_id} not found")
        section_info = self._prepare_section_info(document, section)
        if self._prepare_section_context(document, section, section_info) is None:
            return {"status": "empty_schema", "shard_index": shard_index}

        model_id, dynamic_model, shard_payloads, custom_instruction = (
            self._build_agentic_shard_plan(section_info)
        )
        if not shard_payloads or shard_index >= len(shard_payloads):
            raise ValueError(
                f"Shard index {shard_index} out of range "
                f"(planned {len(shard_payloads)} shards)"
            )
        payload = shard_payloads[shard_index]

        fields, response = _asyncio.run(
            extract_one_shard(
                shard_index=shard_index,
                total_shards=len(shard_payloads),
                payload=payload,
                model_id=model_id,
                data_format=dynamic_model,
                config=self.config,
                section_id=self._persist_section_id(section_info),
                custom_instruction=custom_instruction,
                persistence=persistence,
                shard_runner=default_shard_runner,
                assess_runner=self._build_assess_runner(section_info, self._document),
            )
        )
        return {
            "status": "complete",
            "shard_index": shard_index,
            "page_start": payload["page_start"],
            "page_end": payload["page_end"],
        }

    def plan_section_shards(
        self, document: Document, section_id: str
    ) -> dict[str, Any]:
        """Plan the shards for a section (the SFN runtime's planning step).

        Returns ``{shard_mode, num_shards, shards:[{shard_index,page_start,
        page_end}]}``. ``shard_mode`` is False when sharding does not apply
        (single-pass, in-process runtime, or <=1 shard) — the caller then runs
        the normal whole-section path instead of the Distributed Map.
        """
        self._reset_context()
        section = self._validate_and_find_section(document, section_id)
        if not section:
            raise ValueError(f"Section {section_id} not found")
        section_info = self._prepare_section_info(document, section)
        # Diagnostic: what the plan step actually sees for this section — page
        # count on the section, on the resolved section_info, and how many pages
        # are present in the document. A large section that reports few pages here
        # is why it would fall to single-pass.
        logger.info(
            "plan_section_shards ENTRY: section=%s class=%s section.page_ids=%d "
            "section_info.sorted_page_ids=%d document.pages=%d",
            section_id,
            getattr(section, "classification", "?"),
            len(getattr(section, "page_ids", []) or []),
            len(getattr(section_info, "sorted_page_ids", []) or []),
            len(getattr(document, "pages", {}) or {}),
        )

        agentic = self.config.extraction.agentic
        runtime_choice = (getattr(agentic, "runtime", None) or "in_process").lower()
        num_batches = agentic.max_concurrent_batches
        if (
            not agentic.enabled
            or runtime_choice not in ("step_functions", "stepfunctions", "sfn")
            or num_batches <= 1
        ):
            logger.info(
                "plan_section_shards: single-pass (agentic=%s runtime=%s "
                "max_concurrent_batches=%s) for section %s",
                agentic.enabled,
                runtime_choice,
                num_batches,
                section_id,
            )
            return {"shard_mode": False, "num_shards": 0, "shards": []}

        if self._prepare_section_context(document, section, section_info) is None:
            logger.info(
                "plan_section_shards: empty schema for section %s (class=%s) — "
                "single-pass",
                section_id,
                section_info.class_label,
            )
            return {"shard_mode": False, "num_shards": 0, "shards": []}

        _model_id, _dyn, shard_payloads, _ci = self._build_agentic_shard_plan(
            section_info
        )
        # Explicit trace of the shard decision (item 3 observability): page count,
        # budgets, and resulting shard count — so a "why didn't it shard?" is
        # answerable from CloudWatch alone.
        logger.info(
            "plan_section_shards: section=%s class=%s pages=%d "
            "shard_token_budget=%d max_pages_per_shard=%d max_concurrent_batches=%d "
            "-> %d shard payload(s)",
            section_id,
            section_info.class_label,
            len(self._page_texts),
            self._shard_token_budget(),
            self._max_pages_per_shard(),
            num_batches,
            len(shard_payloads),
        )
        if len(shard_payloads) <= 1:
            return {
                "shard_mode": False,
                "num_shards": len(shard_payloads),
                "shards": [],
            }

        shards = [
            {
                "shard_index": i,
                "page_start": p["page_start"],
                "page_end": p["page_end"],
            }
            for i, p in enumerate(shard_payloads)
        ]
        return {
            "shard_mode": True,
            "num_shards": len(shards),
            "shards": shards,
            "persist_section_id": self._persist_section_id(section_info),
        }

    def merge_section_shards(
        self,
        document: Document,
        section_id: str,
        persistence: Any,
        deadline_epoch: float | None = None,
    ) -> Document:
        """Merge per-shard results from S3 into the final section result.

        The SFN merge state calls this after the Distributed Map completes. It
        reloads every shard's persisted result, merges them with
        ``merge_shard_dicts`` (page-ordered), runs the same validation/escalation
        + completeness checks as the in-process path, and saves results — so the
        SFN path and in-process path produce identical output.

        ``deadline_epoch`` (from the merge Lambda's remaining time) bounds any
        residual confidence self-healing done here (e.g. re-scoring reconcile-padded
        rows) so it can't run the merge Lambda into its 900s wall.
        """
        from idp_common.extraction.runtime import merge_shard_dicts

        t0 = time.time()
        self._reset_context()
        self._assessment_deadline_epoch = deadline_epoch
        section = self._validate_and_find_section(document, section_id)
        if not section:
            raise ValueError(f"Section {section_id} not found")
        section_info = self._prepare_section_info(document, section)
        if self._prepare_section_context(document, section, section_info) is None:
            return self._handle_empty_schema(
                document, section, section_info, section_id, t0
            )

        model_id, dynamic_model, shard_payloads, _ci = self._build_agentic_shard_plan(
            section_info
        )
        persist_section_id = self._persist_section_id(section_info)

        # Load each shard's persisted result.
        shard_dicts: list[dict[str, Any]] = []
        missing: list[int] = []
        for i, p in enumerate(shard_payloads):
            cached = persistence.load(
                persist_section_id, p["page_start"], p["page_end"]
            )
            if not cached or cached.get("extracted_fields") is None:
                missing.append(i)
            else:
                shard_dicts.append(cached)
        if missing:
            raise RuntimeError(
                f"Cannot merge section {section_id}: shard(s) {missing} have no "
                "persisted result (Distributed Map should have completed them)."
            )

        merged_dict, merged_metering, conflicts = merge_shard_dicts(
            shard_dicts, dynamic_model
        )
        structured_data = dynamic_model(**merged_dict)
        extracted_fields = structured_data.model_dump(mode="json")
        if conflicts:
            merged_metering["_shard_scalar_conflicts"] = conflicts
        parsing_succeeded = True

        # Same validation/escalation + completeness as the in-process path.
        self._pending_validation_metadata = None
        self._pending_extraction_model = model_id
        message_prompt: Any = ""
        (
            extracted_fields,
            structured_data,
            validation_metadata,
            escalation_metering,
            parsing_succeeded,
        ) = self._validate_and_maybe_escalate(
            extracted_fields=extracted_fields,
            structured_data=structured_data,
            data_model=dynamic_model,
            model_id=model_id,
            message_prompt=message_prompt,
            agentic_images=[],
            custom_instruction=None,
            section_info=section_info,
            parsing_succeeded=parsing_succeeded,
        )
        if validation_metadata is not None:
            self._pending_validation_metadata = validation_metadata
        if escalation_metering:
            from idp_common.extraction.agentic_idp import _accumulate_metering

            _accumulate_metering(merged_metering, escalation_metering)
        self._check_extraction_completeness(
            extracted_data=structured_data,
            data_model=dynamic_model,
            section_label=section_info.class_label,
        )

        result = ExtractionResult(
            extracted_fields=extracted_fields,
            metering=merged_metering,
            parsing_succeeded=parsing_succeeded,
            total_duration=time.time() - t0,
        )
        self._save_results(document, section, result, section_info, section_id, t0)
        return document
