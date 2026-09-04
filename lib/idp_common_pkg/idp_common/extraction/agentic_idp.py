"""
Agentic IDP implementation using Strands agents with tool-based structured output.

This module implements structured data extraction using Strands agents and tools,
recreating the structured_output_async functionality from ai-tools-registry using
tool-based approach with dynamic tool creation based on Pydantic models.
"""

import asyncio
import contextvars
import io
import json
import logging
import os
import threading
from pathlib import Path
from typing import (
    Any,
    Callable,
    TypedDict,
    TypeVar,
)

import jsonpatch
from aws_lambda_powertools import Logger
from botocore.config import Config
from PIL import Image
from pydantic import BaseModel, Field
from strands import Agent, tool
from strands.agent.conversation_manager import SummarizingConversationManager
from strands.models import BedrockModel
from strands.types.agent import AgentInput
from strands.types.content import CachePoint, ContentBlock, Message
from strands.types.media import (
    DocumentContent,
    ImageContent,
    ImageSource,
)

from idp_common.bedrock.client import (
    CACHEPOINT_SUPPORTED_MODELS,
    CLAUDE_EFFORT_LEVELS,
    GROK_EFFORT_LEVELS,
    is_claude_effort_model,
    is_grok_model,
    strips_sampling_params,
)
from idp_common.bedrock.model_utils import get_model_max_output_tokens
from idp_common.bedrock.openai_responses import is_openai_responses_model
from idp_common.config.models import IDPConfig
from idp_common.extraction.topk_resolver import resolve_candidates
from idp_common.utils.bedrock_utils import (
    async_exponential_backoff_retry,
)
from idp_common.utils.strands_agent_tools.todo_list import (
    create_todo_list,
    update_todo,
    view_todo_list,
)

# Supported image formats for Bedrock API
SUPPORTED_IMAGE_FORMATS = {"jpeg", "png", "gif", "webp"}


def detect_image_format(image_bytes: bytes) -> str:
    """
    Detect the image format from raw bytes.

    Args:
        image_bytes: Raw image bytes

    Returns:
        Image format string suitable for Bedrock API ('jpeg', 'png', 'gif', 'webp')

    Raises:
        ValueError: If the image format is unsupported or cannot be detected
    """
    img = Image.open(io.BytesIO(image_bytes))

    if not img.format or img.format.lower() not in SUPPORTED_IMAGE_FORMATS:
        raise ValueError(
            f"Unsupported image format: {img.format}. "
            f"Supported formats: {', '.join(SUPPORTED_IMAGE_FORMATS)}"
        )
    return img.format.lower()


# Use AWS Lambda Powertools Logger for structured logging
# Automatically logs as JSON with Lambda context, request_id, timestamp, etc.
# In Lambda: Full JSON structured logs
# Outside Lambda: Human-readable format for local development
logger = Logger(service="agentic_idp", level=os.getenv("LOG_LEVEL", "INFO"))
# Configure strands bedrock logger based on environment variable
logging.getLogger("strands.models.bedrock").setLevel(
    os.getenv("STRANDS_LOG_LEVEL", os.getenv("LOG_LEVEL", "INFO"))
)
TargetModel = TypeVar("TargetModel", bound=BaseModel)


def supports_tool_caching(model_id: str) -> bool:
    """
    Check if a model supports tool caching (cachePoint in toolConfig).

    Note: Only Claude models support tool caching. Nova models support
    prompt caching but NOT tool caching.

    Args:
        model_id: The Bedrock model identifier

    Returns:
        True if the model supports tool caching, False otherwise
    """
    return "anthropic.claude" in model_id or "us.anthropic.claude" in model_id


def _summarization_params(
    model_id: str | None, _context_buffer: float
) -> tuple[int, float]:
    """Derive (preserve_recent_messages, summary_ratio) from the model window.

    Larger context window → preserve more recent turns verbatim before the
    conversation manager summarizes, so the agent is far less likely to lose the
    parsed-table / finalize-instruction context mid-run (the re-parse/no-commit
    loop root cause). Buckets keep it simple and safe:

    - >= 1M input tokens  -> preserve 40 (summarize very rarely)
    - >= 400K             -> preserve 20
    - >= 200K             -> preserve 10
    - otherwise / unknown -> preserve 5  (the conservative legacy value)

    ``summary_ratio`` stays high (0.9) so that WHEN summarization does trigger it
    compresses aggressively — we want few, large-preserve windows, not frequent
    small summaries. ``context_buffer`` is accepted for future tuning but not
    currently needed (the buckets already bake in headroom).
    """
    try:
        from idp_common.bedrock.model_utils import get_model_max_input_tokens

        window = get_model_max_input_tokens(model_id) if model_id else 0
    except Exception:  # noqa: BLE001 - unknown model → conservative default
        window = 0

    if window >= 1_000_000:
        preserve = 40
    elif window >= 400_000:
        preserve = 20
    elif window >= 200_000:
        preserve = 10
    else:
        preserve = 5
    logger.info(
        "Conversation summarization sizing: model=%s input_window=%d -> "
        "preserve_recent_messages=%d summary_ratio=0.9",
        model_id,
        window,
        preserve,
    )
    return preserve, 0.9


def supports_prompt_caching(model_id: str) -> bool:
    """
    Check if a model supports prompt caching (cachePoint in system prompt).

    Args:
        model_id: The Bedrock model identifier

    Returns:
        True if the model supports prompt caching, False otherwise
    """
    return model_id in CACHEPOINT_SUPPORTED_MODELS


class BedrockUsage(TypedDict, total=False):
    """Token usage information from Bedrock response."""

    inputTokens: int
    outputTokens: int
    totalTokens: int
    cacheReadInputTokens: int
    cacheWriteInputTokens: int


class BedrockMessageContent(TypedDict):
    """Content item in a Bedrock message."""

    text: str | None


class BedrockMessage(TypedDict):
    """Message structure in Bedrock response."""

    role: str
    content: list[BedrockMessageContent]


class BedrockOutput(TypedDict):
    """Output structure in Bedrock response."""

    message: BedrockMessage


class BedrockResponse(TypedDict, total=False):
    """Raw response from Bedrock converse API."""

    output: BedrockOutput
    usage: BedrockUsage
    stopReason: str | None
    metrics: dict[str, Any] | None


class BedrockInvokeModelResponse(TypedDict):
    """
    Complete response structure from bedrock.invoke_model method.

    This represents the structure returned by:
    response_with_metering = bedrock.invoke_model(...)

    The response contains both the raw Bedrock API response and
    metering information with usage statistics.

    The metering dict may also contain a special "_table_parsing_stats" key
    with tool usage statistics when agentic extraction with table parsing is used.
    """

    response: BedrockResponse
    metering: dict[
        str, BedrockUsage | dict[str, Any]
    ]  # Key format: "{context}/bedrock/{model_id}" or "_table_parsing_stats"


class JsonPatchModel(BaseModel):
    """Model for JSON patch operations."""

    patches: list[dict[str, Any]] = Field(
        ...,
        description="JSON patch operations to apply. Each patch should follow RFC 6902 format with 'op', 'path', and optionally 'value' keys.",
    )
    reasoning: str = Field(
        ...,
        description="Explanation of what these patches are intended to fix or update",
    )


def apply_patches_to_data(
    existing_data: dict[str, Any],
    patches: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Apply JSON patches to existing data and validate the result.

    Args:
        existing_data: The current structured data to patch
        patches: List of JSON patch operations

    Returns:
        Patched and validated data
    """
    if not patches:
        return existing_data

    patch = jsonpatch.JsonPatch(patches)
    patched_dict = patch.apply(existing_data)

    return patched_dict


def create_view_image_tool(page_images: list[bytes]) -> Any:
    """
    Create a view_image tool that has access to page images.

    Args:
        page_images: List of page image bytes (with grid overlay already applied)
        sorted_page_ids: List of page IDs in sorted order

    Returns:
        A Strands tool function for viewing images
    """

    @tool
    def view_image(image_index: int, agent: Agent) -> dict:
        """
        View a specific page image. Use this tool when the doc has more images than what you already see.
        """

        # Validate image index exists
        if not page_images:
            raise ValueError("No images available to view.")
        if image_index >= len(page_images):
            raise ValueError(
                f"Invalid image_index {image_index}. "
                f"Valid range: 0-{len(page_images) - 1}"
            )

        # Get the base image (already has grid overlay)
        img_bytes = page_images[image_index]

        # Detect actual image format from bytes
        img_format = detect_image_format(img_bytes)

        logger.info(
            "Returning image to agent",
            extra={
                "image_index": image_index,
                "image_size_bytes": len(img_bytes),
                "image_format": img_format,
            },
        )

        return {
            "status": "success",
            "content": [
                {
                    "image": {
                        "format": img_format,
                        "source": {
                            "bytes": img_bytes,
                        },
                    }
                }
            ],
        }

    return view_image


# Thread-safe checkpoint callback and confidence data storage using contextvars.
# These are set before agent invocation and read by tools after each state update.
# Using ContextVar instead of module-level globals ensures thread safety when:
# - concurrent_structured_output_async runs multiple agents via asyncio.gather
# - structured_output() spawns a new thread for nested event loops
# - Multiple Lambda invocations share a warm container
_active_checkpoint_callback_var: contextvars.ContextVar[Any | None] = (
    contextvars.ContextVar("_active_checkpoint_callback", default=None)
)
_active_confidence_data_var: contextvars.ContextVar[dict[str, str] | None] = (
    contextvars.ContextVar("_active_confidence_data", default=None)
)


def set_confidence_data(confidence_data_by_page: dict[str, str] | None) -> None:
    """
    Set the context-local confidence data for the parse_table tool.

    Called by ExtractionService before invoking agentic extraction to make
    OCR confidence data available to the table parser tool.

    Args:
        confidence_data_by_page: Dict mapping page IDs to confidence data strings,
            or None to clear.
    """
    _active_confidence_data_var.set(confidence_data_by_page)


def _invoke_checkpoint_callback(agent: Agent) -> None:
    """
    Invoke the active checkpoint callback if one is set.

    Called after each successful update to current_extraction or
    intermediate_extraction to persist intermediate results for
    resume-on-timeout scenarios.

    Saves whichever state has data: prefers current_extraction (validated),
    falls back to intermediate_extraction (buffer/unvalidated).

    The callback is stored in a ContextVar (not in agent state) because
    Strands AgentState requires all values to be JSON-serializable, and
    Python callables are not.

    Args:
        agent: The Strands agent whose extraction state to checkpoint
    """
    callback = _active_checkpoint_callback_var.get()
    if not callback:
        return

    # Prefer validated extraction; fall back to buffer data
    data = agent.state.get("current_extraction")
    source = "current_extraction"
    if not data:
        data = agent.state.get("intermediate_extraction")
        source = "intermediate_extraction"
    if not data:
        return

    try:
        callback({"source": source, "data": data})
    except Exception as e:
        logger.warning(
            "Checkpoint save failed, continuing extraction",
            extra={"error": str(e)},
        )


def create_dynamic_extraction_tool_and_patch_tool(model_class: type[TargetModel]):
    """
    Create a dynamic tool function that extracts data according to a Pydantic model.

    This follows the pattern from ai-tools-registry where the tool's input schema
    is dynamically generated from the Pydantic model, ensuring the LLM knows exactly
    what structure to provide.

    Args:
        model_class: The Pydantic model class to use for extraction

    Returns:
        A tool-decorated function that validates against the model
    """

    def _record_extraction(extraction: Any, agent: Agent) -> str | None:
        """Validate + store an extraction payload in agent state.

        Shared by ``extraction_tool`` and ``extraction_with_confidence_tool`` so both
        the plain and the single-shot integrated paths validate/normalize/persist the
        extraction identically. Returns an error string to hand back to the model on a
        malformed payload, or ``None`` on success (extraction stored in state).
        """
        # Note: The @tool decorator passes data as a dict, not as a model instance
        # We need to validate it manually using the Pydantic model

        # Handle case where LLM returns a single-element array instead of dict
        # This happens when models mistakenly wrap the extraction in an array
        extraction_data = extraction
        if isinstance(extraction_data, list):
            if len(extraction_data) == 1:
                logger.warning(
                    "LLM returned single-element array instead of object, unwrapping",
                    extra={"original_type": "list", "element_count": 1},
                )
                extraction_data = extraction_data[0]
            elif len(extraction_data) == 0:
                logger.error(
                    "LLM returned empty array when single object expected",
                    extra={"element_count": 0},
                )
                return (
                    "Error: Expected single object but received empty array. "
                    "Please provide a single object, not an array."
                )
            else:  # len > 1
                logger.error(
                    "LLM returned multi-element array when single object expected",
                    extra={"element_count": len(extraction_data)},
                )
                return (
                    f"Error: Expected single object but received array with {len(extraction_data)} elements. "
                    "Please provide a single object, not an array."
                )

        extraction_model = model_class.model_validate(extraction_data)  # pyright: ignore[reportAssignmentType]
        extraction_dict = extraction_model.model_dump(mode="json")
        logger.info("extraction recorded", extra={"models_extraction": extraction_dict})
        agent.state.set(key="current_extraction", value=extraction_dict)
        logger.debug(
            "Successfully stored extraction in state",
            extra={"extraction": extraction_dict},
        )
        # Save checkpoint after successful extraction
        _invoke_checkpoint_callback(agent)
        return None

    @tool
    def extraction_tool(
        extraction: model_class,  # pyright: ignore[reportInvalidTypeForm]
        agent: Agent,  # pyright: ignore[reportInvalidTypeForm]
    ) -> str:  # pyright: ignore[reportInvalidTypeForm]
        """Use this tool to return the requested data extraction.
        When you call this tool it overwrites the previous extraction, if you want to expand the extraction use jsonpatch.
        This tool needs to be Successfully invoked before the patch tool can be used."""
        error = _record_extraction(extraction, agent)
        if error is not None:
            return error
        return "Extraction succeeded, the data format is correct"

    @tool
    def extraction_with_confidence_tool(
        extraction: model_class,  # pyright: ignore[reportInvalidTypeForm]
        field_assessment: dict[str, Any],
        agent: Agent,  # pyright: ignore[reportInvalidTypeForm]
    ) -> str:  # pyright: ignore[reportInvalidTypeForm]
        """Return the extracted data AND your per-field confidence in ONE call.

        Use this INSTEAD of calling an extraction tool and an assessment tool
        separately (single-shot integrated confidence). Provide:
        - extraction: the extracted values, matching the schema.
        - field_assessment: your confidence, MIRRORING the extraction structure —
          for each scalar/group field: {"confidence": 0.0-1.0, "confidence_reason": "..."};
          for each list field: a LIST with ONE object per extracted row, in the SAME
          ORDER as the rows (one entry for EVERY row — do not summarize or skip).
        confidence is your calibrated certainty the value matches the source document.
        This overwrites any previous extraction and assessment.
        """
        error = _record_extraction(extraction, agent)
        if error is not None:
            return error
        agent.state.set("field_assessment", field_assessment)
        logger.info(
            "extraction_with_confidence_tool called (single-shot integrated)",
            extra={
                "field_count": len(field_assessment)
                if isinstance(field_assessment, dict)
                else 0
            },
        )
        return "Extraction and confidence recorded; the data format is correct"

    @tool
    def extraction_with_topk_tool(
        candidates: dict[str, Any],
        agent: Agent,  # pyright: ignore[reportInvalidTypeForm]
    ) -> str:
        """Return your top-K guesses with probabilities for EVERY field in ONE call.

        Use this INSTEAD of a plain extraction tool + assessment tool (1S-TopK
        integrated confidence). For each field, provide a nested object with your
        ranked guesses and their probabilities — the top guess (G1) becomes the
        extracted value and its probability (P1) becomes the confidence:
          - Scalar/group field ->
            {"G1": "<best guess>", "P1": 0.0-1.0, "G2": "...", "P2": ..., ...}
          - List field -> a LIST with ONE object per extracted row, IN THE SAME
            ORDER, where each sub-attribute is itself a {G1, P1, ...} object.
        Provide as many guesses (K) as are meaningful; G1/P1 alone is valid.
        Ranking alternatives yields better-calibrated confidence than a single
        value + single score. This overwrites any previous extraction/assessment.
        """
        # Resolve G1 -> value and P1 -> confidence using the shared resolver.
        schema = model_class.model_json_schema()
        inference_result, assessment_data, _candidates_meta = resolve_candidates(
            candidates, schema
        )
        error = _record_extraction(inference_result, agent)
        if error is not None:
            return error
        agent.state.set("field_assessment", assessment_data)
        logger.info(
            "extraction_with_topk_tool called (1S-TopK integrated)",
            extra={
                "field_count": len(assessment_data)
                if isinstance(assessment_data, dict)
                else 0
            },
        )
        return "TopK extraction and confidence recorded; the data format is correct"

    @tool
    def apply_json_patches(
        patches: list[dict[str, Any]],
        agent: Agent,
    ) -> dict[str, Any]:
        """
        Apply JSON patches to fix or update the extracted data.

        Args:
            patches: List of JSON patch operations (RFC 6902 format)
            reasoning: Explanation of what the patches fix
        """
        current_data: dict[str, Any] | None = agent.state.get("current_extraction")

        logger.info("Patch tool called", extra={"patch_request": patches})
        if not current_data:
            return {"error": "No current extraction to patch"}

        patched_data = apply_patches_to_data(current_data, patches)
        validated_patched_data = model_class(**patched_data)
        agent.state.set(
            key="current_extraction",
            value=validated_patched_data.model_dump(mode="json"),
        )
        # Save checkpoint after successful patch
        _invoke_checkpoint_callback(agent)

        return {
            "status": "success",
            "patches_applied": len(patches),
        }

    @tool
    def make_buffer_data_final_extraction(agent: Agent) -> str:
        valid_extraction = model_class(**agent.state.get("intermediate_extraction"))

        agent.state.set("current_extraction", valid_extraction.model_dump(mode="json"))
        # Save checkpoint after buffer→extraction promotion
        _invoke_checkpoint_callback(agent)

        return f"Successfully made the existing extraction the same as the buffer data {str(valid_extraction.model_dump(mode='json'))[:100]}..."

    @tool
    def finalize_table_extraction(
        table_array_field: str,
        scalar_fields: dict[str, Any],
        agent: Agent,
    ) -> dict[str, Any]:
        """
        Finalize extraction by combining mapped table rows (from agent state)
        with scalar fields you provide.  This avoids generating large JSON —
        the mapped rows are read directly from state.

        PREREQUISITE: You must call map_table_to_schema BEFORE this tool.
        The mapped rows are stored automatically in agent state.

        Args:
            table_array_field: The schema field name for the table array
                (e.g., "holdings_positions", "transactions", "line_items")
            scalar_fields: Non-table fields extracted from the document.
                Example: {"Statement Period": "Jan 2025", "Account number": "1234"}
                Only include fields that are NOT part of the table rows.
        """
        # Read mapped rows from agent state
        mapped_result = agent.state.get("mapped_table_rows")
        if not mapped_result or not mapped_result.get("mapped_rows"):
            return {
                "status": "error",
                "message": (
                    "No mapped table rows found in agent state. "
                    "Call map_table_to_schema first."
                ),
            }

        mapped_rows = mapped_result["mapped_rows"]
        row_count = len(mapped_rows)

        # Assemble the complete extraction dict
        extraction_dict: dict[str, Any] = dict(scalar_fields)
        extraction_dict[table_array_field] = mapped_rows

        # Validate against the Pydantic model
        try:
            validated = model_class.model_validate(extraction_dict)
            validated_dict = validated.model_dump(mode="json")
            agent.state.set(key="current_extraction", value=validated_dict)
            # Save checkpoint
            _invoke_checkpoint_callback(agent)

            logger.info(
                "finalize_table_extraction succeeded",
                extra={
                    "table_field": table_array_field,
                    "row_count": row_count,
                    "scalar_fields": list(scalar_fields.keys()),
                },
            )
            return {
                "status": "success",
                "message": (
                    f"Extraction finalized: {row_count} rows in "
                    f"'{table_array_field}' + {len(scalar_fields)} scalar fields. "
                    f"Data validated successfully."
                ),
                "row_count": row_count,
                "scalar_fields": list(scalar_fields.keys()),
            }
        except Exception as e:
            logger.warning(
                "finalize_table_extraction validation failed",
                extra={"error": str(e)},
            )
            return {
                "status": "validation_error",
                "message": (
                    f"Validation failed: {str(e)[:500]}. "
                    f"Check that table_array_field and scalar_fields match the schema."
                ),
                "row_count": row_count,
            }

    return (
        extraction_tool,
        extraction_with_confidence_tool,
        extraction_with_topk_tool,
        apply_json_patches,
        make_buffer_data_final_extraction,
        finalize_table_extraction,
    )


@tool
def provide_field_assessment(assessment: dict[str, Any], agent: Agent) -> str:
    """Record a per-field confidence + bounding-box assessment for the extraction.

    Use this AFTER the extraction is complete and correct (integrated-assessment
    mode only). Provide one entry per extracted field mirroring the extraction
    structure:
      - Scalar/group field -> {"confidence": 0.0-1.0, "confidence_reason": "...",
        "bbox": [x1,y1,x2,y2], "page": N}   (bbox/page optional)
      - List field        -> a LIST with one assessment object per extracted row,
        IN THE SAME ORDER as the extracted rows.
    confidence is your calibrated certainty the value is correct given the source.
    This overwrites any previous assessment.
    """
    agent.state.set("field_assessment", assessment)
    logger.info(
        "provide_field_assessment called",
        extra={"field_count": len(assessment) if isinstance(assessment, dict) else 0},
    )
    return "Assessment recorded."


@tool
def view_existing_extraction(agent: Agent) -> str:
    """Use this tool to view data is currently stored as extracted."""
    logger.info(
        "Current extraction state",
        extra={"current_extraction": agent.state.get("current_extraction")},
    )
    return agent.state.get("current_extraction")


@tool
def write_buffer_date(data: dict[str, Any], agent: Agent) -> str:
    """
    Use this tool when the extraction is too large to do in a single step, this is a buffer where you can save intermediate data that wouldn't pass validation yet.

    IMPORTANT: The data you save here must eventually match the extraction schema structure. Plan your buffer data structure to align with the required schema fields and types.
    Review the extraction schema before using this tool to ensure compatibility.
    """
    agent.state.set("intermediate_extraction", data)
    logger.info("Saving intermediate data", extra={"intermediate_extraction": data})
    return f"Saved data: {str(data)[:100]}.... "


@tool
def view_buffer_data(agent: Agent) -> str:
    """View the intermediate buffer data with this tool, this data is not a validated extraction, but intermediate state for you to work with.

    WARNING: This returns the ENTIRE buffer which can be very large. For large extractions, prefer using view_buffer_data_section or view_buffer_data_stats.
    """

    return agent.state.get("intermediate_extraction")


@tool
def view_buffer_data_section(path: str, agent: Agent) -> Any:
    """View a specific section of the intermediate buffer data by JSON Pointer path (RFC 6901). Token-efficient way to inspect parts of large data.

    Args:
        path: JSON Pointer path (same format as JSON Patch). Must start with "/" for nested paths, or use "" for root.

    Examples:
        - path="/table_rows" -> returns the entire table_rows array
        - path="/table_rows/0" -> returns first item in table_rows array
        - path="/table_rows/0/fund_name" -> returns fund_name field of first row
        - path="/document_name" -> returns just the document_name field
        - path="" -> returns entire buffer (same as view_buffer_data)

    Note: Uses same path format as JSON Patch operations for consistency.
    """
    data = agent.state.get("intermediate_extraction")

    if not data:
        return {"error": "No intermediate data in buffer"}

    # Handle empty path (root)
    if path == "":
        return data

    # Remove leading slash and parse path
    if not path.startswith("/"):
        return {
            "error": "Path must start with '/' (e.g., '/table_rows/0') or be empty string for root"
        }

    parts = path[1:].split("/") if path != "/" else []
    current = data

    try:
        for part in parts:
            if not part:  # Skip empty parts from double slashes
                continue

            if isinstance(current, list):
                # Try to convert to int for list indexing
                current = current[int(part)]
            elif isinstance(current, dict):
                current = current[part]
            else:
                return {
                    "error": f"Cannot navigate path '{path}' - reached non-dict/list at '{part}'"
                }

        return current
    except (KeyError, IndexError, ValueError) as e:
        return {"error": f"Path '{path}' not found: {str(e)}"}


@tool
def get_extraction_schema_reminder(agent: Agent) -> str:
    """Use this tool during long extractions to review the expected data schema and field requirements. Helps ensure you stay aligned with the required structure.

    RECOMMENDED: Call this tool every 100-200 rows in large extractions to verify you're maintaining the correct structure.
    """

    schema = agent.state.get("extraction_schema_json")
    if not schema:
        return "Schema not available"

    return f"Remember: Your extraction must match this schema:\n\n{schema}\n\nEnsure all field names, types, and required fields are correct."


@tool
def view_buffer_data_stats(agent: Agent) -> dict[str, Any]:
    """View overview statistics of intermediate buffer data. Token-efficient alternative to viewing full data. Use this for progress checks during large extractions.

    TIP: For large extractions (500+ items), consider calling get_extraction_schema_reminder every 100-200 items to stay aligned with requirements.
    """

    data = agent.state.get("intermediate_extraction")

    if not data:
        return {"status": "empty", "message": "No intermediate data in buffer"}

    # Build statistics
    stats = {}

    if isinstance(data, dict):
        stats["keys"] = list(data.keys())
        stats["field_count"] = len(data)
        # Sample nested structure for arrays
        for key, value in data.items():
            if isinstance(value, list):
                stats[f"{key}_length"] = len(value)
                if value:
                    stats[f"{key}_sample_type"] = type(value[0]).__name__
    # Add estimated token count (rough approximation)
    data_str = str(data)

    return {
        "status": "contains_data",
        "structure": stats,
        "estimated_size_chars": len(data_str),
        "tip": "Use patch_buffer_data to update specific fields, Use make_buffer_data_final_extraction to complete or get detailed guidance on missing requirements.",
    }


@tool
def patch_buffer_data(patches: list[dict[str, Any]], agent: Agent) -> str:
    """Update the intermediate_extraction data inside the buffer, this is not validated yet

    Apply JSON patches to fix or update the extracted data.

    Args:
        patches: List of JSON patch operations (RFC 6902 format)
        reasoning: Explanation of what the patches fix

    """

    logger.info("Buffer Patch tool called", extra={"patch_request": patches})
    patched_data = apply_patches_to_data(
        existing_data=agent.state.get("intermediate_extraction"), patches=patches
    )

    agent.state.set("intermediate_extraction", patched_data)

    logger.info(f"Current length of buffer data {len(patched_data)} ")

    # Save checkpoint after buffer patch (critical for resume-on-timeout)
    _invoke_checkpoint_callback(agent)

    return f"Successfully patched {str(patched_data)[:100]}...."


SYSTEM_PROMPT = """
You are a useful assistant that helps turn unstructured data into structured data using the provided tools.

EXTRACTION APPROACH:
1. Use the extraction_tool for fresh data extraction - this validates data against the schema immediately
2. When updating existing data or fixing validation errors, use JSON patch operations via the apply_json_patches tool
3. JSON patches allow precise, targeted updates without losing correct data
4. If the document is large and the extraction request can't be done in one go, create a valid extraction object and iterate with jsonpatches until you completed the entire extraction!
5. Use intermediate data buffer if you can't extract a valid data object in a single step

IMPORTANT:
YOU MUST perform a batched extraction if there are more than 50 fields to extract.
batched extraction is when you create a viable format with extraction tool and then you expand it with jsonpatches. 
You can pass up to 100 records in a single patch operation.
When using batched extraction plan it out and make a todo list with target size based on the document and other key tasks.

NEVER STOP early on large documents, always extract all the data.

NEVER return null or an empty list for an array/list field whose rows are visible
in the document — that silently discards every row, and no other signal will show
it (the scalar fields still score perfectly). If part of the data is unreadable,
garbled, or ambiguous, emit EVERY row anyway with the problem cells set to null or
to the literal text you can see. A partially-correct list is far more useful than
no list. Difficulty is never a reason to return nothing.

JSON PATCH FORMAT (RFC 6902):
- {"op": "replace", "path": "/field_name", "value": "new_value"} - Update a field
- {"op": "add", "path": "/new_field", "value": "value"} - Add a field
- {"op": "remove", "path": "/field_name"} - Remove a field

CRITICAL EXTRACTION RULES:
1. Extract text EXACTLY as it appears in the source document - character for character
2. NEVER interpret, expand, or modify any text formatting, special characters, or punctuation
3. Preserve ALL original formatting including brackets, parentheses, hyphens, underscores, etc.
4. Maintain exact capitalization, spacing, and line structure as shown in the source
5. Do not treat any text as markdown, code, or special formatting - everything is literal text

VALIDATION AND CORRECTION:
1. Review each field in your extracted data
2. Double-check each value against the source
3. Pay special attention to dates, amounts, and similar-looking data
4. Verify that all characters and formatting are preserved exactly as they appear
5. When fixing errors, use JSON patches to target specific problems


FINAL REVIEW (CRITICAL):
After successfully using the extraction tool, you MUST:
1. Review the complete extracted data one more time
2. Compare each field against the source document character by character
3. Verify all punctuation, special characters, and formatting match exactly
4. Look for any missing fields, incorrect values, or formatting issues
5. If any discrepancies are found, use the apply_json_patches tool to fix them
6. Only finish when you are confident all data is accurate and complete

TABLE TOOL NOTE (for the processing report):
If the document contained a large table AND you did NOT use the parse_table /
map_table_to_schema tools to extract it, end your final response with ONE short
sentence, on its own line, beginning exactly with "TABLE_TOOL_NOTE:" that states
briefly why you extracted the table directly instead (e.g. the table was small,
the OCR pipe-table was malformed, columns didn't map cleanly). If you DID use the
table tools, or there was no large table, do not add this note.
"""


# Prompt templates are editable config fields (extraction.task_prompt*), selected
# per settings by idp_common.extraction.prompt_assembly (select_extraction_task_prompt
# / select_confidence_task_prompt). The integrated confidence instructions live in
# extraction.task_prompt_extraction_with_confidence — not hardcoded here.


TABLE_PARSING_PROMPT_ADDENDUM = """
TABLE EXTRACTION OPTIMIZATION:
When you encounter tables in the document text (Markdown tables with | delimiters),
use the EFFICIENT two-tool workflow instead of generating JSON row-by-row:

PREFERRED WORKFLOW (fast — use for tables with 50+ rows):
1. Extract non-table scalar fields first (e.g. account_number, statement_period)
2. Call parse_table with the full document text — parses ALL tables instantly
3. Review the columns and row_count returned
4. Call map_table_to_schema with:
   - column_mapping: maps each table column to the corresponding schema field
     (match by semantic meaning, e.g. "Amt" → "amount", "Desc" → "description")
   - static_fields: constant values to add to every row (e.g. account_number,
     cost_basis) — use values you extracted in step 1
   - value_transforms: optional cleanup rules (e.g. "strip_currency" for price fields)
5. Review the sample_first_row and sample_last_row to verify the mapping is correct
6. Call finalize_table_extraction with:
   - table_array_field: the schema field name for the array (e.g. "holdings_positions")
   - scalar_fields: the non-table fields from step 1

finalize_table_extraction reads the mapped rows directly from state — you do NOT
need to pass the rows as arguments.  This is critical for performance: it eliminates
generating thousands of lines of JSON.  Do NOT use extraction_tool for large tables.

FALLBACK WORKFLOW (use when map_table_to_schema is not suitable):
- Complex tables with merged cells, nested headers, or irregular structure
- Tables where column mapping is ambiguous
- Small tables (< 50 rows) where manual extraction is acceptable
- Any column that is OCR-corrupted, garbled, or cannot be mapped cleanly
In these cases, use parse_table + extraction_tool/apply_json_patches as before.

DECLINING THE TOOL IS NOT DECLINING THE TABLE (ABSOLUTE RULE):
Choosing not to use the table tools obliges you to extract the table DIRECTLY.
- NEVER return null or an empty list for an array field whose rows are visible
  in the document. That silently discards every row.
- If ONE column is unreadable, garbled, or cannot be mapped, still emit EVERY
  row. Set that single cell to null, or to the literal text you can see. Do not
  drop the row, and do not drop the whole list, because of one bad column.
- A partially-correct table is far more useful than no table. "I could not map
  this cleanly" is never a reason to return nothing.

QUALITY CHECKS:
- parse_success_rate target: >= {min_parse_success_rate}
- avg_confidence target: >= {min_confidence_threshold} (Textract only)
- If quality is poor, fall back to LLM extraction for those sections
- Always extract non-tabular fields (headers, metadata) using normal extraction

ROW COUNT VALIDATION:
- Verify row_count from parse_table matches the document
- If parse_table returns multiple tables with IDENTICAL columns, they are fragments
  of a single table — the tool auto-merges them, but verify completeness
- NEVER stop until ALL table data is captured
"""


@async_exponential_backoff_retry(
    max_retries=50,
    initial_delay=5,
    max_delay=1800,
    jitter=0.5,
)
async def invoke_agent_with_retry(input: AgentInput, agent: Agent):
    return await agent.invoke_async(input)


def _is_context_overflow_error(exc: Exception) -> bool:
    """Heuristically detect a model context-window overflow.

    Strands raises ``ContextWindowOverflowException`` and, when its
    SummarizingConversationManager can't recover (e.g. the very first turn
    already overflows), surfaces "Cannot summarize: insufficient messages for
    summarization". Bedrock may also report validation errors mentioning the
    input/context length. Match on type name and message text rather than
    importing Strands' exception type (keeps this module import-light).
    """
    name = type(exc).__name__
    msg = str(exc).lower()
    if "contextwindowoverflow" in name.lower():
        return True
    # Keep these specific to context-window/token-budget overflow so an
    # unrelated validation error is not mis-tagged (the message is advisory,
    # but a wrong remediation is still noise).
    needles = (
        "insufficient messages for summarization",
        "context window",
        "context length",
        "input is too long",
        "too many input tokens",
        "too many tokens",
        "maximum context length",
        "exceeds the context",
    )
    return any(n in msg for n in needles)


def _initialize_token_usage() -> dict[str, int]:
    """Initialize token usage tracking dictionary."""
    return {
        "inputTokens": 0,
        "outputTokens": 0,
        "totalTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheWriteInputTokens": 0,
    }


def _accumulate_token_usage(response: Any, token_usage: dict[str, int]) -> None:
    """
    Accumulate token usage from response into usage dict.

    Args:
        response: Agent response object with metrics
        token_usage: Dictionary to accumulate usage into (modified in place)
    """
    if response and response.metrics and response.metrics.accumulated_usage:
        for key in token_usage.keys():
            token_usage[key] += response.metrics.accumulated_usage.get(key, 0)


def _build_system_prompt(
    base_prompt: str,
    custom_instruction: str | None,
    data_format: type[BaseModel],
    restate_schema: bool = True,
) -> tuple[str, str]:
    """
    Build complete system prompt with custom instructions and schema.

    ``schema_json`` is returned regardless of ``restate_schema`` — it is stored in
    agent state for ``get_extraction_schema_reminder``, which lets the agent fetch
    the schema ON DEMAND. So turning the restatement off removes a per-request
    duplicate without removing the agent's access to the schema.

    Why the restatement is optional (#710): the class schema is already on the
    wire as ``extraction_tool``'s ``inputSchema``, which Strands derives from the
    same ``model_json_schema()`` — so this is a byte-for-byte duplicate, measured
    at ~2,595 of ~5,692 schema tokens per request on the lending ``Payslip``
    class. It is not obviously safe to drop: restating a schema in prose often
    improves adherence, which is why this is a knob defaulting to the existing
    behaviour rather than a removal.

    Args:
        base_prompt: The base system prompt (typically SYSTEM_PROMPT constant)
        custom_instruction: Optional custom instructions to append
        data_format: Pydantic model class to extract schema from
        restate_schema: Append "Expected Schema: ..." to the system prompt.

    Returns:
        Tuple of (complete system prompt, schema_json for state storage)
    """
    # Generate and clean schema
    schema_json = json.dumps(data_format.model_json_schema(), indent=2)

    # Build final prompt
    final_prompt = base_prompt
    if custom_instruction:
        final_prompt = f"{final_prompt}\n\nCustom Instructions for this specific task: {custom_instruction}"

    complete_prompt = (
        f"{final_prompt}\n\nExpected Schema:\n{schema_json}"
        if restate_schema
        else final_prompt
    )

    return complete_prompt, schema_json


def _build_model_config(
    model_id: str,
    max_tokens: int | None,
    max_retries: int,
    connect_timeout: float,
    read_timeout: float,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """
    Build model configuration with token limits and caching settings.

    This function:
    1. Creates boto3 Config with retry and timeout settings
    2. Determines model-specific max token limits
    3. Validates and caps max_tokens if needed
    4. Auto-detects and enables caching support (prompt and tool caching)

    Args:
        model_id: Bedrock model identifier (supports us.*, eu.*, and global.anthropic.*)
        max_tokens: Optional max tokens override (will be capped at model max)
        max_retries: Maximum retry attempts for API calls
        connect_timeout: Connection timeout in seconds
        read_timeout: Read timeout in seconds

    Returns:
        Dictionary of model configuration parameters for create_strands_bedrock_model.
        Automatically uses BedrockModel for regional models (us.*, eu.*) and
        AnthropicModel with AnthropicBedrock for cross-region models (global.anthropic.*).
    """
    # OpenAI GPT-5.x models are served via the bedrock-mantle Responses API and
    # are not callable through the Converse-based Strands path. Callers should
    # have routed these to standard extraction (see ExtractionService); fail
    # loudly if one reaches here so the misconfiguration is obvious.
    if is_openai_responses_model(model_id):
        raise ValueError(
            f"OpenAI Responses-API models ({model_id}) do not support agentic/"
            "Strands extraction. Use standard extraction (agentic.enabled=false)."
        )

    # Configure retry behavior and timeouts using boto3 Config
    boto_config = Config(
        retries={
            "max_attempts": max_retries,
            "mode": "adaptive",  # Uses exponential backoff with adaptive retry mode
        },
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )

    # Handle ':1m' suffix for 1M context window models (Claude 4.x beta).
    # Bedrock rejects ':1m' as a model identifier; strip it and pass the
    # anthropic_beta header via additional_request_fields, mirroring
    # bedrock/client.py::invoke_model. See GitHub issue #312.
    additional_request_fields: dict[str, Any] | None = None
    if model_id.endswith(":1m"):
        model_id = model_id[:-3]
        additional_request_fields = {"anthropic_beta": ["context-1m-2025-08-07"]}

    # Reasoning effort (output_config.effort) for effort-capable Claude models.
    # Maps to ConverseStream additionalModelRequestFields, controlling thinking
    # depth / output-token spend. Verified live: effort measurably changes output
    # tokens on Sonnet 5. Skipped for models that reject it (Sonnet 4.5 / Haiku).
    if reasoning_effort and is_claude_effort_model(model_id):
        effort = str(reasoning_effort).lower().strip()
        if effort in CLAUDE_EFFORT_LEVELS:
            if additional_request_fields is None:
                additional_request_fields = {}
            additional_request_fields["output_config"] = {"effort": effort}
            logger.info("Agentic extraction using reasoning effort '%s'", effort)

    # xAI Grok uses a DIFFERENT carrier and a different vocabulary: reasoning is
    # always on and effort rides in `reasoning.effort`, accepting
    # none/low/medium/high/xhigh but NOT Claude's `max`. Claude's
    # `output_config.effort` is silently ignored by Grok, as is any unrecognized
    # key — so an out-of-vocabulary value must be dropped, not forwarded.
    elif reasoning_effort and is_grok_model(model_id):
        effort = str(reasoning_effort).lower().strip()
        if effort in GROK_EFFORT_LEVELS:
            if additional_request_fields is None:
                additional_request_fields = {}
            additional_request_fields["reasoning"] = {"effort": effort}
            logger.info("Agentic extraction using reasoning effort '%s'", effort)
        else:
            logger.warning(
                "Ignoring unsupported Grok reasoning effort '%s' (valid: %s)",
                reasoning_effort,
                ", ".join(GROK_EFFORT_LEVELS),
            )

    # Resolve the model's true max output tokens from the single source of truth
    # (config_library/model_config_limits.yaml via get_model_max_output_tokens).
    # Agentic extraction always requests the model maximum — its multi-step tool
    # loop needs full output headroom, and Bedrock's default-when-omitted is a
    # small truncating value (measured 4096 for Claude, 2000 for Nova). A stale
    # per-model regex ladder here previously left new models (e.g. Sonnet 5) at a
    # 4096 fallback, causing MaxTokensReachedException on ordinary documents.
    try:
        model_max = get_model_max_output_tokens(model_id)
    except (ValueError, FileNotFoundError):
        # Model not in model_config_limits.yaml, or the file is unavailable in
        # this runtime. Don't truncate or crash extraction; use a conservative
        # Claude-class headroom and log loudly. Bedrock will reject an over-limit
        # request with a ValidationException naming the real cap if too high.
        model_max = 64_000
        logger.error(
            "Could not resolve max output tokens for %s (missing entry or "
            "model_config_limits.yaml unavailable); falling back to %d.",
            model_id,
            model_max,
        )

    # A config max_tokens (if still provided by a caller) is only ever an upper
    # bound below the model max; never exceed the model's true limit.
    if max_tokens is not None and max_tokens < model_max:
        max_output_tokens = max_tokens
    else:
        max_output_tokens = model_max

    # Build base model config
    # Honor BEDROCK_ASSUME_ROLE_ARN by sourcing the boto session from the
    # shared factory. Falls back to default credentials when unset.
    try:
        from idp_common.bedrock.session import get_bedrock_session

        bedrock_session = get_bedrock_session()
    except Exception as e:
        logger.debug(
            "Falling back to default Bedrock session for agentic extraction (%s)", e
        )
        bedrock_session = None

    model_config = dict(
        model_id=model_id,
        boto_client_config=boto_config,
        max_tokens=max_output_tokens,
    )
    if bedrock_session is not None:
        model_config["boto_session"] = bedrock_session

    # Forward anthropic_beta header for 1M context models via Strands'
    # additional_request_fields, which maps to ConverseStream's
    # additionalModelRequestFields.
    if additional_request_fields is not None:
        model_config["additional_request_fields"] = additional_request_fields

    logger.info(
        "Setting max_tokens for model",
        extra={
            "max_tokens": max_output_tokens,
            "model_id": model_id,
            "model_max_tokens": model_max,
        },
    )

    # Auto-detect caching support based on model capabilities
    if supports_prompt_caching(model_id):
        model_config["cache_prompt"] = "default"
        logger.info(
            "Prompt caching enabled for model",
            extra={"model_id": model_id, "auto_detected": True},
        )

        # Only enable tool caching if the model supports it (Claude only, not Nova)
        if supports_tool_caching(model_id):
            model_config["cache_tools"] = "default"
            logger.info(
                "Tool caching enabled for model",
                extra={"model_id": model_id, "auto_detected": True},
            )
        else:
            logger.info(
                "Tool caching not supported for model",
                extra={"model_id": model_id, "reason": "prompt_caching_only"},
            )
    else:
        logger.debug("Caching not supported for model", extra={"model_id": model_id})

    return model_config


def _get_inference_params(
    model_id: str,
    temperature: float,
    top_p: float | None,
) -> dict[str, float]:
    """
    Get inference parameters ensuring temperature and top_p are mutually exclusive.

    Some Bedrock models don't allow both temperature and top_p to be specified.
    This follows the same logic as bedrock/client.py.

    Claude 4.7+ models (e.g. ``us.anthropic.claude-opus-4-7``) deprecate the
    ``temperature``, ``top_p`` and ``top_k`` parameters and reject requests
    that pass them. xAI Grok rejects them outright with a 400 naming the field.
    For these models this helper returns an empty dict so that no inference
    parameters are forwarded to the Strands ``BedrockModel`` / ConverseStream
    call. See GitHub issue #304.

    Args:
        model_id: Bedrock model identifier.
        temperature: Temperature value from config.
        top_p: Top_p value from config (may be None).

    Returns:
        Dict with only one of temperature or top_p, or an empty dict for models
        that reject the sampling group.
    """
    # Claude 4.7+ and xAI Grok don't accept temperature/top_p/top_k. Omit them
    # entirely so ConverseStream doesn't fail with
    # "`top_p` is deprecated for this model" (Claude) or
    # "This model doesn't support the topP field" (Grok).
    if strips_sampling_params(model_id):
        logger.info(
            "Skipping temperature/top_p (rejected or deprecated for this model)",
            extra={"model_id": model_id},
        )
        return {}

    params: dict[str, float] = {}

    # Only use top_p if it's positive (greater than 0)
    # This allows temperature=0.0 for deterministic output (recommended by Anthropic)
    if top_p is not None and top_p > 0:
        params["top_p"] = top_p
        logger.debug(
            "Using top_p for inference (temperature ignored)", extra={"top_p": top_p}
        )
    else:
        params["temperature"] = temperature
        logger.debug(
            "Using temperature for inference (top_p is 0 or None)",
            extra={"temperature": temperature},
        )

    return params


def _prepare_prompt_content(
    prompt: str | Message | Image.Image,
    page_images: list[bytes] | None,
    existing_data: BaseModel | None,
    model_id: str | None = None,
) -> list[ContentBlock]:
    """
    Prepare prompt content from various input types.

    Converts different prompt types (text, PIL Image, Message dict) into
    a list of ContentBlocks, adds page images, and appends existing data context.

    Args:
        prompt: Input content (text string, PIL Image, or Message dict)
        page_images: Optional list of page image bytes to include
        existing_data: Optional existing extraction data to update
        model_id: Target model. Used ONLY to decide whether a trailing
            ``cachePoint`` block may be appended — a model that does not support
            prompt caching rejects the whole request (see below). When None the
            cachePoint is appended, preserving the historical behavior.

    Returns:
        List of ContentBlock objects ready for agent invocation
    """
    prompt_content: list[ContentBlock] = []

    # Process prompt based on type
    if isinstance(prompt, Image.Image):
        # Convert PIL Image to binary string
        img_buffer = io.BytesIO()
        prompt.save(img_buffer, format="PNG")
        img_bytes = img_buffer.getvalue()

        logger.debug(
            "Processing PIL Image",
            extra={"size": prompt.size, "mode": prompt.mode},
        )

        prompt_content = [
            ContentBlock(text="Extract structured data from this image:"),
            ContentBlock(
                image=ImageContent(format="png", source=ImageSource(bytes=img_bytes))
            ),
        ]
    elif isinstance(prompt, dict) and "content" in prompt:
        prompt_content = prompt["content"]  # type: ignore
    else:
        prompt_content = [ContentBlock(text=str(prompt))]

    # Add page images if provided - no limit with latest Bedrock API
    if page_images:
        logger.info(
            "Attaching images to agentic extraction prompt",
            extra={"image_count": len(page_images)},
        )

        for img_bytes in page_images:
            # Detect actual image format from bytes
            img_format = detect_image_format(img_bytes)
            prompt_content.append(
                ContentBlock(
                    image=ImageContent(
                        format=img_format,  # pyright: ignore[reportArgumentType]
                        source=ImageSource(bytes=img_bytes),
                    )
                )
            )

    # Add existing data context if provided (checkpoint resume scenario)
    if existing_data:
        existing_dict = existing_data.model_dump(mode="json")
        # Count items for a helpful summary without sending the full data in the prompt
        # (the full data is already loaded into agent.state["current_extraction"])
        item_counts = []
        for key, value in existing_dict.items():
            if isinstance(value, list):
                item_counts.append(f"{len(value)} {key}")
            elif value is not None:
                item_counts.append(key)
        items_summary = ", ".join(item_counts) if item_counts else "partial data"

        prompt_content.append(
            ContentBlock(
                text=(
                    f"IMPORTANT - RESUME FROM CHECKPOINT: Your extraction state already contains "
                    f"previously extracted data ({items_summary}). This data is already loaded "
                    f"in your current_extraction state. Do NOT call extraction_tool again — that "
                    f"would overwrite the existing data. Instead, use view_existing_extraction to "
                    f"review what's already extracted, then use apply_json_patches to ADD only the "
                    f"remaining items that are not yet extracted. Focus on the pages/rows that come "
                    f"AFTER the already-extracted data."
                )
            )
        )

    prompt_content = [
        x for x in prompt_content if x.get("cachePoint", {}).get("type") != "default"
    ]
    prompt_content.append(ContentBlock(text="end of your main task description"))

    # Append the trailing cachePoint ONLY for a model that supports prompt
    # caching. This is not an optimization — Bedrock rejects the ENTIRE request
    # for a model that doesn't: "AccessDeniedException: You invoked an
    # unsupported model or your request did not allow prompt caching". xAI Grok
    # is such a model, so leaving this unconditional failed every document in
    # agentic extraction (verified live on stack IDP1, 2026-09-03).
    #
    # `model_id is None` keeps the historical behavior for callers that don't
    # pass it (only the tests, today).
    if model_id is None or supports_prompt_caching(model_id):
        prompt_content.append(ContentBlock(cachePoint=CachePoint(type="default")))
    else:
        logger.info(
            "Omitting prompt cachePoint (model does not support prompt caching)",
            extra={"model_id": model_id},
        )
    return prompt_content


async def _invoke_agent_for_extraction(
    agent: Agent,
    prompt_content: list[ContentBlock],
    data_format: type[TargetModel],
    max_extraction_retries: int = 3,
    schema_validator: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
) -> tuple[Any, TargetModel | None]:
    """
    Invoke agent and retry if extraction fails.

    Unlike network retries (handled by invoke_agent_with_retry), this retries when
    the agent completes successfully but fails to produce a valid extraction.

    Args:
        agent: The Strands agent to invoke
        prompt_content: List of ContentBlocks to send to the agent
        data_format: Pydantic model class for validation
        max_extraction_retries: Maximum retry attempts for failed extractions (default: 3)
        schema_validator: Optional callback that takes the extracted dict and
            returns ``(is_valid, feedback)``. Used to enforce full JSON-Schema
            constraints (e.g. ``format`` keywords) that the Pydantic model does
            not, and to give the agent one more self-correction round with the
            list of violations. When None, only Pydantic type validation runs.

    Returns:
        Tuple of (response, validated_result or None)
    """
    response = None

    for attempt in range(max_extraction_retries):
        # invoke_agent_with_retry already handles network errors and throttling
        try:
            response = await invoke_agent_with_retry(agent=agent, input=prompt_content)
        except Exception as e:  # noqa: BLE001 - translate one specific failure mode
            if _is_context_overflow_error(e):
                raise ValueError(
                    "Extraction input exceeds the model's context window. The "
                    "document/section is too large to process in one agent pass. "
                    "Remedies: enable concurrent sharding "
                    "(extraction.agentic.max_concurrent_batches > 1) and/or lower "
                    "extraction.agentic.shard_token_budget; enable table parsing "
                    "(requires Textract TABLES so OCR emits Markdown tables); "
                    "reduce attached page images; or use a larger-context model "
                    "(e.g. a ':1m' Claude variant). "
                    f"(underlying error: {e})"
                ) from e
            raise
        logger.debug("Agent response received")

        # Try to get extraction from state
        current_extraction = agent.state.get("current_extraction")

        if current_extraction:
            try:
                result = data_format(**current_extraction)
                # Pydantic type validation passed. Optionally enforce the full
                # JSON Schema (format keywords, etc.) and feed any violations
                # back for one more self-correction round.
                if schema_validator is not None:
                    is_valid, feedback = schema_validator(current_extraction)
                    if not is_valid and attempt < max_extraction_retries - 1:
                        logger.info(
                            "Schema-constraint validation failed, asking agent to fix",
                            extra={
                                "attempt": attempt + 1,
                                "data_format": data_format.__name__,
                            },
                        )
                        prompt_content = [ContentBlock(text=feedback)]
                        continue
                    if not is_valid:
                        logger.warning(
                            "Schema-constraint validation still failing after retries; "
                            "returning best-effort result for escalation/alerting",
                            extra={"data_format": data_format.__name__},
                        )
                logger.debug(
                    "Successfully validated extraction",
                    extra={"data_format": data_format.__name__, "attempt": attempt + 1},
                )
                return response, result
            except Exception as e:
                logger.warning(
                    "Extraction validation failed, retrying",
                    extra={
                        "attempt": attempt + 1,
                        "max_retries": max_extraction_retries,
                        "error": str(e),
                        "data_format": data_format.__name__,
                    },
                )
                if attempt < max_extraction_retries - 1:
                    # Ask agent to fix the extraction
                    prompt_content = [
                        ContentBlock(
                            text=f"The extraction failed validation with error: {str(e)}. Please fix the extraction using the tools."
                        )
                    ]
                    continue
                else:
                    # Last attempt failed
                    logger.error(
                        "Failed to validate extraction after all retries",
                        extra={
                            "data_format": data_format.__name__,
                            "error": str(e),
                            "extraction_data": current_extraction,
                        },
                    )
                    return response, None
        else:
            logger.warning(
                "No extraction found in agent state",
                extra={"attempt": attempt + 1, "max_retries": max_extraction_retries},
            )
            if attempt < max_extraction_retries - 1:
                # Ask agent to provide extraction
                prompt_content = [
                    ContentBlock(
                        text="No extraction was found. Please use the extraction_tool to provide the extracted data."
                    )
                ]
                continue

    # Should never reach here, but handle it gracefully
    if response is None:
        raise ValueError("No response from agent after retries")

    return response, None


async def _run_shard_agent(
    shard_index: int,
    total_shards: int,
    page_start: int,
    page_end: int,
    total_pages: int,
    model_id: str,
    data_format: type[TargetModel],
    shard_prompt: str | Message | Image.Image,
    config: IDPConfig,
    context: str,
    max_retries: int,
    connect_timeout: float,
    read_timeout: float,
    max_tokens: int | None,
    checkpoint_callback: Any | None,
    base_custom_instruction: str | None = None,
    emit_field_assessment: bool = False,
) -> tuple[TargetModel, BedrockInvokeModelResponse]:
    """Run one extraction agent over a single shard.

    ``shard_prompt`` already contains ONLY this shard's page text/images (the
    service slices the input before calling). The instruction reinforces that
    the agent should extract everything visible in its pages; fields that live
    on other pages are simply absent (resolved at merge).
    """
    shard_instruction = (
        f"You are processing shard {shard_index + 1} of {total_shards}, covering "
        f"pages {page_start + 1}-{page_end} of {total_pages}. The document text "
        f"and images you have been given contain ONLY these pages. Extract every "
        f"requested field that appears in your pages. If a field does not appear "
        f"in your pages, leave it null — another shard will provide it."
    )
    combined_instruction = shard_instruction
    if base_custom_instruction:
        combined_instruction = f"{base_custom_instruction}\n\n{shard_instruction}"

    return await structured_output_async(
        model_id=model_id,
        data_format=data_format,
        prompt=shard_prompt,
        page_images=None,  # images already embedded in shard_prompt content
        config=config,
        context=context,
        max_retries=max_retries,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        max_tokens=max_tokens,
        custom_instruction=combined_instruction,
        checkpoint_callback=checkpoint_callback,
        emit_field_assessment=emit_field_assessment,
    )


async def default_shard_runner(
    *,
    shard_index: int,
    total_shards: int,
    payload: dict[str, Any],
    model_id: str,
    data_format: type[TargetModel],
    config: IDPConfig,
    context: str,
    max_retries: int,
    connect_timeout: float,
    read_timeout: float,
    max_tokens: int | None,
    checkpoint_callback: Any | None,
    custom_instruction: str | None,
) -> tuple[TargetModel, "BedrockInvokeModelResponse"]:
    """Strands-backed shard runner used by the runtime backends.

    Adapts the runtime's ``(shard_index, payload, ...)`` calling convention to
    ``_run_shard_agent``. Defined here (not in ``runtime.py``) so ``runtime`` has
    no dependency on the strands stack — the runtime stays import-light and the
    import graph has no cycle.
    """
    return await _run_shard_agent(
        shard_index=shard_index,
        total_shards=total_shards,
        page_start=payload["page_start"],
        page_end=payload["page_end"],
        total_pages=payload["total_pages"],
        model_id=model_id,
        data_format=data_format,
        shard_prompt={"role": "user", "content": payload["content"]},
        config=config,
        context=context,
        max_retries=max_retries,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        max_tokens=max_tokens,
        checkpoint_callback=checkpoint_callback,
        base_custom_instruction=custom_instruction,
        # Integrated-assessment mode flows through the payload (set by the
        # service when extraction.confidence.mode == "integrated").
        emit_field_assessment=bool(payload.get("emit_field_assessment")),
    )


# Merge primitives live in idp_common.extraction.runtime (strands-free, the
# single source of truth). Re-exported here for backward compatibility with
# existing imports (`from idp_common.extraction.agentic_idp import
# _merge_shard_results`, `_accumulate_metering`, ...).
from idp_common.extraction.runtime import (  # noqa: E402,F401
    _accumulate_metering,  # noqa: F401  (re-export for backward compat)
    _annotation_is_list,  # noqa: F401
    _list_valued_fields,  # noqa: F401
    _merge_shard_results,  # noqa: F401
)


async def concurrent_structured_output_async(
    model_id: str,
    data_format: type[TargetModel],
    shard_payloads: list[dict[str, Any]],
    max_parallelism: int,
    config: IDPConfig = IDPConfig(),
    context: str = "Extraction",
    max_retries: int = 7,
    connect_timeout: float = 10.0,
    read_timeout: float = 600.0,
    max_tokens: int | None = None,
    checkpoint_callback: Any | None = None,
    custom_instruction: str | None = None,
    section_id: str = "section",
    persistence: Any | None = None,
    runtime: Any | None = None,
    assess_runner: Any | None = None,
) -> tuple[TargetModel, BedrockInvokeModelResponse]:
    """
    Run one extraction agent per input shard, concurrently, and merge results.

    Each shard's prompt contains ONLY that shard's page text/images (the service
    slices the input before calling), so no agent loads the whole document — this
    bounds the context window and prevents overflow on long documents. At most
    ``max_parallelism`` shards run at once (Bedrock RPM control).

    This is now a thin shim over the runtime-agnostic primitives in
    :mod:`idp_common.extraction.runtime`: it delegates to an
    :class:`~idp_common.extraction.runtime.ExtractionRuntime` (default
    :class:`~idp_common.extraction.runtime.InProcessRuntime`), which schedules
    :func:`~idp_common.extraction.runtime.extract_one_shard` per shard and merges
    via :func:`~idp_common.extraction.runtime.merge_shard_results`. Observable
    behaviour is unchanged; existing callers keep working.

    Args:
        shard_payloads: List of dicts with keys ``content`` (pre-rendered prompt
            content blocks for the shard), ``page_start``, ``page_end``,
            ``total_pages``.
        max_parallelism: Max number of shards to run concurrently.
        section_id: Section identifier used to derive deterministic per-shard
            persistence keys (when ``persistence`` is supplied).
        persistence: Optional per-shard persistence backend (idempotent
            skip-if-complete). ``None`` => in-memory only (today's behaviour).
        runtime: Optional explicit ExtractionRuntime; defaults to InProcessRuntime.
        Other args: Same as structured_output_async.

    Returns:
        Merged (data_format instance, metering response). The response metering
        includes a ``_shard_scalar_conflicts`` marker when shards disagreed on a
        scalar field, so the service can record it in metadata.
    """
    from idp_common.extraction.runtime import InProcessRuntime

    rt = runtime or InProcessRuntime(max_parallelism)
    merged_result, response = await rt.run(
        shard_payloads=shard_payloads,
        model_id=model_id,
        data_format=data_format,
        config=config,
        section_id=section_id,
        context=context,
        max_retries=max_retries,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        max_tokens=max_tokens,
        checkpoint_callback=checkpoint_callback,
        custom_instruction=custom_instruction,
        persistence=persistence,
        shard_runner=default_shard_runner,
        assess_runner=assess_runner,
    )
    # Normalise the runtime's plain-dict response into the typed envelope that
    # existing callers expect. The runtime returns a BaseModel; it is an instance
    # of ``data_format`` (TargetModel) by construction.
    import typing as _typing

    typed_result = _typing.cast(TargetModel, merged_result)
    return typed_result, BedrockInvokeModelResponse(
        response=BedrockResponse(
            output=BedrockOutput(
                message=BedrockMessage(
                    role="assistant", content=[BedrockMessageContent(text="merged")]
                )
            )
        ),
        metering=response.get("metering", {}),
    )


async def structured_output_async(
    model_id: str,
    data_format: type[TargetModel],
    prompt: str | Message | Image.Image,
    existing_data: BaseModel | None = None,
    system_prompt: str | None = None,
    custom_instruction: str | None = None,
    config: IDPConfig = IDPConfig(),
    page_images: list[bytes] | None = None,
    context: str = "Extraction",
    max_retries: int = 7,
    connect_timeout: float = 10.0,
    read_timeout: float = 600.0,
    max_tokens: int | None = None,
    checkpoint_callback: Any | None = None,
    checkpoint_buffer_data: dict[str, Any] | None = None,
    schema_validator: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
    emit_field_assessment: bool = False,
) -> tuple[TargetModel, BedrockInvokeModelResponse]:
    """
    Extract structured data using Strands agents with tool-based validation.

    This recreates the structured_output_async functionality from ai-tools-registry
    using dynamically created tools that validate against the Pydantic model.

    USAGE GUIDELINES:
    - **STRONGLY DISCOURAGED**: Do not modify the system_prompt parameter unless absolutely necessary.
      The default SYSTEM_PROMPT is carefully crafted for optimal extraction accuracy and consistency.
    - **RECOMMENDED**: Use custom_instruction parameter to add task-specific guidance without
      disrupting the core extraction logic and validation rules.
    - Custom instructions are appended to the system prompt, preserving the original behavior
      while allowing for domain-specific customizations.

    DATA FORMAT GUIDELINES:
    - **IMPORTANT**: Neither custom_instruction nor system_prompt should contain data format
      specifications, field definitions, or schema requirements. All data structure requirements
      should be expressed through the Pydantic data model.
    - Use Pydantic field descriptions (Field(..., description="...")) and model docstrings
      to clarify field meanings, formats, and extraction requirements.
    - The extraction system automatically incorporates the complete model schema and field
      descriptions into the agent's understanding.

    Args:
        model_id: Model identifier (e.g., "us.anthropic.claude-sonnet-4-20250514-v1:0")
        data_format: Pydantic model class defining the expected structure
        prompt: Input content (text, image, or content blocks)
        enable_image_tools: Whether to enable image enhancement tools (default: True)
        existing_data: Optional existing data to update via patches
        system_prompt: **DISCOURAGED** - Custom system prompt. Only use if the default
                      SYSTEM_PROMPT is completely unsuitable for your use case.
        custom_instruction: **RECOMMENDED** - Additional task-specific instructions
                           appended to the system prompt. Use this for domain-specific
                           guidance, field clarifications, or extraction rules.
        max_retries: Maximum number of retry attempts for Bedrock API calls (default: 5).
                    Increase this value if your AWS account has low throttling limits.
        connect_timeout: Connection timeout in seconds (default: 60.0).
                        Increase if experiencing connection timeout errors.
        read_timeout: Read timeout in seconds (default: 300.0 = 5 minutes).
                     Increase for large documents or slow model responses.

    Returns:
        Tuple of (extracted data, bedrock response with token usage)

    Examples:
        # Define data structure with field descriptions (RECOMMENDED):
        # - Use Field(..., description="...") for field-specific requirements
        # - Use model docstrings for overall structure documentation
        # - All data format details belong in the Pydantic model, not instructions

        # Recommended usage with custom instructions for extraction guidance
        result, response = await structured_output_async(
            model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
            data_format=InvoiceModel,
            prompt=image_content,
            custom_instruction="Focus on line items in the main table. Ignore header/footer text."
        )

        # WRONG - Don't define data format in custom instructions:
        # custom_instruction="Extract: invoice_number, total_amount, line_items array..."

        # Discouraged - only use if default system prompt is completely unsuitable
        result, response = await structured_output_async(
            model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
            data_format=CustomModel,
            prompt=content,
            system_prompt="Your completely custom system prompt here..."
        )
    """
    if not system_prompt:
        system_prompt = SYSTEM_PROMPT

    # Append table parsing guidance to system prompt if enabled
    table_parsing_config = config.extraction.agentic.table_parsing
    if table_parsing_config.enabled:
        system_prompt += TABLE_PARSING_PROMPT_ADDENDUM.format(
            min_parse_success_rate=table_parsing_config.min_parse_success_rate,
            min_confidence_threshold=table_parsing_config.min_confidence_threshold,
        )
        logger.info(
            "Table parsing tool enabled for agentic extraction",
            extra={
                "min_confidence_threshold": table_parsing_config.min_confidence_threshold,
                "min_parse_success_rate": table_parsing_config.min_parse_success_rate,
                "use_confidence_data": table_parsing_config.use_confidence_data,
            },
        )

    logger.debug(
        "Starting agentic extraction",
        extra={"data_format": data_format.__name__, "model_id": model_id},
    )

    # Create the dynamic extraction tools for this specific model.
    (
        extraction_tool,
        extraction_with_confidence_tool,
        extraction_with_topk_tool,
        apply_json_patches,
        make_buffer_data_final_extraction,
        finalize_table_extraction,
    ) = create_dynamic_extraction_tool_and_patch_tool(data_format)

    # Integrated-assessment: select HOW the agent produces confidence.
    #   two_step (default): extract via extraction_tool, then call
    #     provide_field_assessment in a follow-up inference (a dedicated reflection
    #     pass over the finalized values).
    #   single_shot: extract + confidence in ONE combined tool call
    #     (extraction_with_confidence_tool), saving the follow-up inference.
    #   topk: extract + confidence in ONE combined tool call
    #     (extraction_with_topk_tool) where the agent emits its top-K guesses with
    #     probabilities per field; the shared topk_resolver takes G1 as the value
    #     and P1 as the confidence. Better-calibrated than single_shot.
    # All three write agent.state["field_assessment"], so the downstream collation
    # / reconcile / grounding / explainability_info path is identical. The choice
    # is a hidden experimental knob (extraction.agentic.integrated_confidence_strategy)
    # so cost/latency vs. confidence-calibration can be A/B tested; it is NOT in the
    # config UI schema. The instructions telling the agent which call to make live
    # in the selected extraction TASK prompt.
    strategy = config.extraction.agentic.integrated_confidence_strategy
    if emit_field_assessment and strategy == "single_shot":
        primary_extraction_tool = extraction_with_confidence_tool
    elif emit_field_assessment and strategy == "topk":
        primary_extraction_tool = extraction_with_topk_tool
    else:
        primary_extraction_tool = extraction_tool

    image_tools = []
    if page_images:
        image_tools.append(create_view_image_tool(page_images))

    # Prepare tools list
    tools = [
        primary_extraction_tool,
        apply_json_patches,
        make_buffer_data_final_extraction,
        finalize_table_extraction,
        *image_tools,
        view_existing_extraction,
        patch_buffer_data,
        view_buffer_data,
        view_buffer_data_section,
        view_buffer_data_stats,
        write_buffer_date,
        get_extraction_schema_reminder,
        create_todo_list,
        update_todo,
        view_todo_list,
    ]

    if emit_field_assessment:
        # two_step: this is the assessment tool the agent calls AFTER extraction.
        # single_shot: this stays available only as an OPTIONAL end-of-batch
        #   fallback so a multi-step / patched extraction (rows added AFTER the
        #   combined call) can still (re)assess all rows; the common single-call
        #   case never needs it.
        tools.append(provide_field_assessment)

    # Add table parsing tools if enabled
    if table_parsing_config.enabled:
        from idp_common.extraction.tools.table_parser import (
            create_map_table_to_schema_tool,
            create_parse_table_tool,
        )

        # The actual confidence data is injected via a ContextVar
        # (_active_confidence_data_var) by the ExtractionService before calling
        # structured_output via set_confidence_data().
        parse_table_tool = create_parse_table_tool(
            confidence_data_by_page=_active_confidence_data_var.get(),
            max_empty_line_gap=table_parsing_config.max_empty_line_gap,
            auto_merge_adjacent_tables=table_parsing_config.auto_merge_adjacent_tables,
        )
        tools.append(parse_table_tool)

        # map_table_to_schema: bulk-transforms parsed rows using a column mapping
        # Eliminates the need for the LLM to generate JSON row-by-row
        map_tool = create_map_table_to_schema_tool()
        tools.append(map_tool)

    # Build system prompt. The schema restatement is optional (#710): it duplicates
    # the extraction tool's inputSchema, which Strands derives from the same
    # model_json_schema(). schema_json is returned either way and stored in agent
    # state below, so get_extraction_schema_reminder still works when it is off.
    restate_schema = config.extraction.agentic.restate_schema_in_system_prompt
    final_system_prompt, schema_json = _build_system_prompt(
        base_prompt=system_prompt or SYSTEM_PROMPT,
        custom_instruction=custom_instruction,
        data_format=data_format,
        restate_schema=restate_schema,
    )
    if not restate_schema:
        logger.info(
            "Schema restatement disabled; the class schema goes on the wire once, "
            "as the extraction tool's inputSchema (saved ~%d chars of system prompt)",
            len(schema_json) + len("\n\nExpected Schema:\n"),
        )

    tool_names = [getattr(tool, "__name__", str(tool)) for tool in tools]
    logger.debug(
        "Created agent with tools",
        extra={
            "tool_count": len(tools),
            "data_format": data_format.__name__,
            "tool_names": tool_names,
        },
    )

    # Build model configuration with token limits and caching
    model_config = _build_model_config(
        model_id=model_id,
        max_tokens=max_tokens,
        max_retries=max_retries,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        reasoning_effort=config.extraction.reasoning_effort,
    )

    # Prepare prompt content
    prompt_content = _prepare_prompt_content(
        prompt=prompt,
        page_images=page_images,
        existing_data=existing_data,
        model_id=model_id,
    )

    # Track token usage
    token_usage = _initialize_token_usage()

    # Get inference params ensuring temperature and top_p are mutually exclusive.
    # For Claude 4.7+ models, no inference params are returned because
    # temperature/top_p/top_k are deprecated (see GitHub issue #304).
    inference_params = _get_inference_params(
        model_id=model_id,
        temperature=config.extraction.temperature,
        top_p=config.extraction.top_p,
    )

    # Set the context-local checkpoint callback so tools can invoke it.
    # Stored in a ContextVar (not in agent state) because AgentState
    # requires all values to be JSON-serializable.
    _active_checkpoint_callback_var.set(checkpoint_callback)

    # Model-aware conversation summarization (item 1). The Strands
    # SummarizingConversationManager only exposes count/ratio knobs (no token
    # budget), so we DERIVE preserve_recent_messages from the model's context
    # window: a roomy model keeps more recent turns verbatim before summarizing,
    # so it is far less likely to summarize away the "you already parsed the
    # table — now finalize with extraction_tool" context that caused the
    # re-parse/no-commit loop. A small-window model keeps the conservative
    # minimum. This complements sharding (which keeps each shard's conversation
    # small in the first place).
    preserve_recent, summary_ratio = _summarization_params(
        model_id, config.extraction.context_buffer
    )
    agent = Agent(
        model=BedrockModel(
            **model_config,
            **inference_params,
        ),  # pyright: ignore[reportArgumentType]
        tools=tools,
        system_prompt=final_system_prompt,
        state={
            "current_extraction": None,
            "images": {},
            "existing_data": (
                existing_data.model_dump(mode="json") if existing_data else None
            ),
            "extraction_schema_json": schema_json,  # Store for schema reminder tool
        },
        conversation_manager=SummarizingConversationManager(
            summary_ratio=summary_ratio, preserve_recent_messages=preserve_recent
        ),
    )
    if existing_data:
        agent.state.set("current_extraction", existing_data.model_dump(mode="json"))

    # Load buffer checkpoint into agent's intermediate_extraction state
    if checkpoint_buffer_data:
        agent.state.set("intermediate_extraction", checkpoint_buffer_data)
        # Count items for resume prompt
        buffer_counts = []
        for key, value in checkpoint_buffer_data.items():
            if isinstance(value, list):
                buffer_counts.append(f"{len(value)} {key}")
        buffer_summary = (
            ", ".join(buffer_counts) if buffer_counts else "partial buffer data"
        )
        # Add resume instruction to prompt
        prompt_content.insert(
            -2,
            ContentBlock(
                text=(
                    f"IMPORTANT - RESUME FROM BUFFER CHECKPOINT: Your intermediate_extraction "
                    f"buffer already contains previously extracted data ({buffer_summary}). "
                    f"This data is already loaded in your buffer state. Use view_buffer_data_stats "
                    f"to check progress, then continue adding remaining items with patch_buffer_data. "
                    f"When all items are extracted, use make_buffer_data_final_extraction to finalize. "
                    f"Do NOT start over — continue from where the buffer left off."
                )
            ),
        )
        logger.info(
            "Loaded buffer checkpoint into agent state",
            extra={"buffer_summary": buffer_summary},
        )

    response, result = await _invoke_agent_for_extraction(
        agent=agent,
        prompt_content=prompt_content,
        data_format=data_format,
        max_extraction_retries=3,
        schema_validator=schema_validator,
    )

    # Accumulate token usage
    _accumulate_token_usage(response, token_usage)

    # Review agent is deprecated and disabled. The main extraction agent already
    # performs a final review step as part of its SYSTEM_PROMPT instructions.
    # The review_agent config fields are preserved for backward compatibility
    # but are no-ops. See CHANGELOG for details.
    if config.extraction.agentic.review_agent:
        logger.warning(
            "review_agent config is deprecated and has no effect. "
            "The main extraction agent already performs self-review as part of its "
            "system prompt. Remove 'review_agent' and 'review_agent_model' from your "
            "config to suppress this warning.",
            extra={
                "review_agent": config.extraction.agentic.review_agent,
                "review_agent_model": config.extraction.agentic.review_agent_model,
            },
        )

    # Return best effort result
    if result and response:
        # Build metering dict with token usage
        metering_dict = {f"{context}/bedrock/{model_id}": BedrockUsage(**token_usage)}

        # Include table parsing stats if tool was used
        tool_stats = agent.state.get("table_parsing_stats")
        if tool_stats:
            # Use special key that service.py expects
            metering_dict["_table_parsing_stats"] = tool_stats
            logger.info(
                "Table parsing tool was used during extraction",
                extra={"tool_stats": tool_stats},
            )

        # Integrated-assessment mode: surface the agent's inline per-field
        # assessment (recorded via provide_field_assessment) so the caller can
        # route it through the same collation/grounding/emit path separate-mode
        # uses. Rides in metering to survive the typed response envelope.
        if emit_field_assessment:
            field_assessment = agent.state.get("field_assessment")
            if field_assessment:
                metering_dict["_integrated_field_assessment"] = field_assessment
                logger.info(
                    "Integrated field assessment recorded",
                    extra={
                        "field_count": len(field_assessment)
                        if isinstance(field_assessment, dict)
                        else 0
                    },
                )
            else:
                logger.warning(
                    "Integrated assessment enabled but agent did not call "
                    "provide_field_assessment; no confidence emitted for this shard."
                )

        return result, BedrockInvokeModelResponse(
            response=BedrockResponse(
                output=BedrockOutput(
                    message=BedrockMessage(
                        role="assistant",
                        content=[BedrockMessageContent(text=str(response))],
                    )
                )
            ),
            metering=metering_dict,
        )

    logger.error(
        "Failed to extract structured data",
        extra={"data_format": data_format.__name__},
    )
    raise ValueError("Failed to generate valid structured output.")


def structured_output(
    model_id: str,
    data_format: type[BaseModel],
    prompt: str | Message | Image.Image,
    existing_data: BaseModel | None = None,
    system_prompt: str | None = None,
    custom_instruction: str | None = None,
    page_images: list[bytes] | None = None,
    context: str = "Extraction",
    config: IDPConfig = IDPConfig(),
    max_retries: int = 7,
    connect_timeout: float = 10.0,
    read_timeout: float = 600.0,
    checkpoint_callback: Any | None = None,
    checkpoint_buffer_data: dict[str, Any] | None = None,
    schema_validator: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
    emit_field_assessment: bool = False,
) -> tuple[BaseModel, BedrockInvokeModelResponse]:
    """
    Synchronous version of structured_output_async.

    Extract structured data using Strands agents with tool-based validation.
    This is a wrapper that runs the async version in a sync event loop.

    USAGE GUIDELINES:
    - **STRONGLY DISCOURAGED**: Do not modify the system_prompt parameter unless absolutely necessary.
      The default SYSTEM_PROMPT is carefully crafted for optimal extraction accuracy and consistency.
    - **RECOMMENDED**: Use custom_instruction parameter to add task-specific guidance without
      disrupting the core extraction logic and validation rules.
    - Custom instructions are appended to the system prompt, preserving the original behavior
      while allowing for domain-specific customizations.

    Args:
        model_id: Model identifier (e.g., "us.anthropic.claude-sonnet-4-20250514-v1:0")
        data_format: Pydantic model class defining the expected structure
        prompt: Input content (text, image, or content blocks)
        existing_data: Optional existing data to update via patches
        system_prompt: **DISCOURAGED** - Custom system prompt. Only use if the default
                      SYSTEM_PROMPT is completely unsuitable for your use case.
        custom_instruction: **RECOMMENDED** - Additional task-specific instructions
                           appended to the system prompt. Use this for domain-specific
                           guidance, field clarifications, or extraction rules.

    Returns:
        Tuple of (extracted data, bedrock response with token usage)

    Examples:
        # Define data structure with field descriptions (RECOMMENDED):
        # - Use Field(..., description="...") for field-specific requirements
        # - Use model docstrings for overall structure documentation
        # - All data format details belong in the Pydantic model, not instructions

        # Recommended usage with custom instructions for extraction guidance
        result, response = structured_output(
            model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
            data_format=InvoiceModel,
            prompt=image_content,
            custom_instruction="Focus on line items in the main table. Ignore header/footer text."
        )

        # WRONG - Don't define data format in custom instructions:
        # custom_instruction="Extract: invoice_number, total_amount, line_items array..."

        # Discouraged - only use if default system prompt is completely unsuitable
        result, response = structured_output(
            model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
            data_format=CustomModel,
            prompt=content,
            system_prompt="Your completely custom system prompt here..."
        )
    """
    logger.debug(
        "Starting sync agentic extraction",
        extra={"data_format": data_format.__name__},
    )

    # Check if we're already in an event loop (e.g., Jupyter notebook)
    try:
        asyncio.get_running_loop()
        # We're in an existing event loop, use run_until_complete with a new task

        result = None
        exception = None

        def run_in_new_loop():
            nonlocal result, exception
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                result = new_loop.run_until_complete(
                    structured_output_async(
                        model_id=model_id,
                        data_format=data_format,
                        prompt=prompt,
                        existing_data=existing_data,
                        system_prompt=system_prompt,
                        custom_instruction=custom_instruction,
                        config=config,
                        context=context,
                        max_retries=max_retries,
                        connect_timeout=connect_timeout,
                        read_timeout=read_timeout,
                        page_images=page_images,
                        checkpoint_callback=checkpoint_callback,
                        checkpoint_buffer_data=checkpoint_buffer_data,
                        schema_validator=schema_validator,
                        emit_field_assessment=emit_field_assessment,
                    )
                )
            except Exception as e:
                exception = e
            finally:
                new_loop.close()

        thread = threading.Thread(target=run_in_new_loop)
        thread.start()
        thread.join()

        if exception:
            raise exception
        assert result is not None  # For type checker
        return result

    except RuntimeError:
        # No event loop running, safe to use asyncio.run()
        return asyncio.run(
            structured_output_async(
                model_id=model_id,
                data_format=data_format,
                prompt=prompt,
                existing_data=existing_data,
                system_prompt=system_prompt,
                custom_instruction=custom_instruction,
                config=config,
                context=context,
                max_retries=max_retries,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                page_images=page_images,
                checkpoint_callback=checkpoint_callback,
                checkpoint_buffer_data=checkpoint_buffer_data,
                schema_validator=schema_validator,
                emit_field_assessment=emit_field_assessment,
            )
        )


if __name__ == "__main__":

    class Persona(BaseModel):
        age: int
        name: str

    base_dir = Path(__file__).parent.parent.parent.parent.parent
    file_path = base_dir / "samples" / "Nuveen.pdf"

    # Multipage document testcase
    class DocumentRow(BaseModel):
        fund_name: str
        ticker: str
        record_date: str
        ex_dividend_date: str
        payment_date: str
        estimated_short_term_capital_gains: str
        estimated_long_term_capital_gains: str
        nav_as_of_10_31_2024: float
        total_cap_gain_distribution_prc_of_nav: float

    class DocumentFormat(BaseModel):
        document_name: str
        document_text: str
        table_rows: list[DocumentRow] = Field(min_length=500)

    with open(file_path, "rb") as f:
        data = f.read()

    async def async_main():
        result, _ = await structured_output_async(
            model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
            data_format=DocumentFormat,
            prompt=Message(
                role="user",
                content=[
                    ContentBlock(text="please extract the following document"),
                    ContentBlock(
                        document=DocumentContent(
                            format="pdf",
                            source={"bytes": data},
                            name="document to extract",
                        )
                    ),
                ],
            ),
        )
        print(result)
        print("\n\n")
        print(len(result.table_rows))

    asyncio.run(async_main())
