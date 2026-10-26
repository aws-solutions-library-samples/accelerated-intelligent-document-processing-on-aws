# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pydantic models for IDP configuration.

These models provide type-safe access to configuration data and can be used
as type hints throughout the codebase.

Usage:
    from idp_common.config.models import IDPConfig

    config_dict = get_config()
    config = IDPConfig.model_validate(config_dict)

    # Type-safe access
    if config.extraction.agentic.enabled:
        model = config.extraction.model
"""

import difflib
import functools
import math
import re
from functools import lru_cache
from typing import (
    Annotated,
    Any,
    Dict,
    List,
    Literal,
    NamedTuple,
    Optional,
    Tuple,
    Union,
    get_args,
    get_origin,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    ValidationInfo,
    model_validator,
)
from pydantic import field_validator as _pydantic_field_validator
from typing_extensions import Self

# Current config schema/shape version. Bump when the stored config shape changes
# in a way that requires a migration (see config/migrations/). v0.6 folded the
# top-level `assessment` block into `extraction.confidence` / `extraction.geometry`
# and introduced the top-level `hitl` block. v0.7 moved
# `extraction.agentic.validation` up to `extraction.validation`, because simple
# extraction now runs the same validate-and-retry path and the knob is no longer
# agentic-only. v0.8 removed `classification.classificationMethod`, whose two
# values each bundled two independent choices, in favour of
# `classification.granularity` plus `classification.modality`; a holistic
# configuration also has its hand-written flat prompts copied into
# `classification.packet`, which is where packet granularity reads them. v0.9
# nested every classification setting inside the call that uses it
# (`one_stage`, `two_stage.segmentation`, `two_stage.section_classification`),
# moved the file-shape assertions under `file_shape`, and replaced
# `invalidClassFallback` with `fallback_class`. v0.10 made the error analyzer's
# `parameters.max_log_events` take effect with a default of 10, and raises a stored
# 5 (the old default, which the tools ignored in favour of 10) to 10.
CONFIG_FORMAT_VERSION = "0.10"


#: Exceptions a field validator here can raise on malformed input that pydantic
#: does not convert into a ``ValidationError``: ``int([])`` and ``float({})`` raise
#: ``TypeError``, and ``int(float("inf"))`` and ``float(10**400)`` raise
#: ``OverflowError``. Pydantic converts only ``ValueError`` and ``AssertionError``.
_UNCONVERTED_INPUT_ERRORS = (TypeError, OverflowError)


def field_validator(*fields: str, **kwargs: Any) -> Any:
    """``pydantic.field_validator`` whose validator cannot fail the whole load.

    A stored value of the wrong shape (``temperature: []``, ``top_k: 1e400``,
    ``ocr.max_workers: .inf``) made a validator raise ``TypeError`` or
    ``OverflowError``. Pydantic lets those propagate, so ``IDPConfig`` raised
    something that is not a ``ValidationError``, naming no setting, and callers
    that report invalid configuration did not recognise it. This wraps every
    validator in this module so those two errors are re-raised as ``ValueError``,
    which pydantic reports as a ``ValidationError`` naming the field.

    Nothing that parsed before parses differently: the wrapper acts only when the
    validator has already raised, and it does not substitute a default. Every
    validator in this module is declared through this name, which
    ``tests/unit/config/test_null_numeric_settings.py`` checks per field.
    """
    decorate = _pydantic_field_validator(*fields, **kwargs)

    def wrap(validator: Any) -> Any:
        is_classmethod = isinstance(validator, classmethod)
        func = validator.__func__ if is_classmethod else validator

        @functools.wraps(func)
        def guarded(*args: Any, **kw: Any) -> Any:
            try:
                return func(*args, **kw)
            except _UNCONVERTED_INPUT_ERRORS as exc:
                raise ValueError(f"{type(exc).__name__}: {exc}") from exc

        guarded.__idp_input_errors_converted__ = True  # type: ignore[attr-defined]
        return decorate(classmethod(guarded) if is_classmethod else guarded)

    return wrap


def _parse_optional_max_tokens(v: Any) -> Optional[int]:
    """Parse an optional max_tokens value from config.

    max_tokens is an optional cap on model output. An empty string, ``None``,
    or a value that coerces to 0 means "unset" — the Bedrock client then
    resolves the selected model's maximum output limit
    (model_config_limits.yaml). A positive int/string is used as an upper cap.
    """
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        v = int(v)
    v = int(v)
    return v if v > 0 else None


def _parse_required_int(v: Any, info: ValidationInfo, cls: type) -> int:
    """Parse a required int, falling back to the field's default on empty/None.

    A stored config may carry an explicit ``null`` or empty string for a field
    that is otherwise a required int with a default (e.g. ``list_batch_size``,
    ``max_empty_line_gap``). Coercing that directly via ``int(None)`` raises
    ``TypeError``, which would fail config load/validation on upgrade. Treat
    empty/None as "use the model's declared default" instead.
    """
    if v is None or (isinstance(v, str) and not v.strip()):
        default = cls.model_fields[info.field_name].default
        return int(default) if default is not None else 0
    return int(v)


def _default_if_none(v: Any, info: ValidationInfo, cls: type) -> Any:
    """Return the field's declared default when a stored value is ``None``.

    A YAML key with no value (``temperature:``) or a JSON ``null`` reaches a
    ``mode="before"`` validator as ``None``. A validator that passes it to
    ``int()`` or ``float()`` raises ``TypeError``, which pydantic does not turn
    into a ``ValidationError``, so the whole configuration fails to load. Any
    other value is returned unchanged, so a validator applying this first keeps
    its parsing of every non-null value exactly as it was.
    """
    if v is None:
        return cls.model_fields[info.field_name].get_default(call_default_factory=True)
    return v


def _parse_required_float(v: Any, info: ValidationInfo, cls: type) -> float:
    """Parse a required float, falling back to the field's default on ``None``.

    The float counterpart of ``_parse_required_int`` for the sampling parameters
    (``temperature``, ``top_p``, ``top_k``) and thresholds. Only ``None`` falls
    back to the default: an empty string still parses as ``0.0`` and a numeric
    string or number as ``float(v)``, as these validators always did.
    """
    v = _default_if_none(v, info, cls)
    if isinstance(v, str):
        return float(v) if v else 0.0
    return float(v)


def _parse_whole_number(v: Any, info: ValidationInfo, cls: type) -> int:
    """Parse a whole number from an int, a numeric string or an integral float.

    The Configuration editor saves ``type: number`` fields as numbers, and stored
    records are stringified (``"30"``, ``"30.0"``). Empty/None falls back to the
    field's declared default. Fractions (``30.5``), non-numeric strings and
    booleans are rejected with ``ValueError``.
    """
    if v is None or (isinstance(v, str) and not v.strip()):
        return int(cls.model_fields[info.field_name].default)
    if isinstance(v, bool):
        raise ValueError("must be a whole number, not a boolean")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            v = float(v.strip())
        except ValueError:
            raise ValueError(f"must be a whole number, got {v!r}") from None
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError(f"must be a whole number, got {v!r}")
        return int(v)
    raise ValueError(f"must be a whole number, got {type(v).__name__}")


class ImageConfig(BaseModel):
    """Image processing configuration"""

    target_width: Optional[int] = Field(
        default=None, description="Target width for images"
    )
    target_height: Optional[int] = Field(
        default=None, description="Target height for images"
    )
    dpi: Optional[int] = Field(default=None, description="DPI for image rendering")
    preprocessing: Optional[bool] = Field(
        default=None, description="Enable image preprocessing"
    )

    @field_validator("target_width", "target_height", mode="before")
    @classmethod
    def parse_dimensions(cls, v: Any) -> Optional[int]:
        """Parse a dimension from a string or number; empty means unset.

        ⚠️ **An unreadable value raises rather than becoming ``None``.** It used to
        return ``None``, which is the same value "not configured" produces — so
        ``target_width: "abc"`` was discarded and ``OcrService`` then applied the
        default ceiling and logged *"No image sizing configured, applying default
        ceiling"*, which is the opposite of what happened. The warning written for
        exactly this case sat behind an ``except`` that could never fire, because by
        then the value was already ``int | None`` (#1158).

        Raising also removes an asymmetry inside this one model: ``parse_dpi`` below
        calls ``int(v)`` with no ``try``, so ``dpi: "abc"`` has always failed config
        validation while ``target_width: "abc"`` was silently dropped. Same config
        block, opposite behaviour, and the silent half is the one that misreports.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            return int(v)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"expected a pixel count or an empty value, got {v!r}. An unreadable "
                "dimension is rejected rather than ignored, because ignoring it "
                "applies the default ceiling and reports it as 'no image sizing "
                "configured'."
            ) from exc

    @field_validator("dpi", mode="before")
    @classmethod
    def parse_dpi(cls, v: Any) -> Optional[int]:
        """Parse DPI from string or number"""
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        if isinstance(v, str):
            return int(v) if v else None
        return int(v)

    @field_validator("preprocessing", mode="before")
    @classmethod
    def parse_preprocessing(cls, v: Any) -> Optional[bool]:
        """Parse preprocessing bool from string or bool"""
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes")
        return bool(v)


class TableParsingConfig(BaseModel):
    """Configuration for deterministic table parsing tool in agentic extraction.

    When enabled, the extraction agent gains a parse_table tool that can
    deterministically parse well-formatted Markdown tables from OCR output
    without LLM inference. The agent decides when to use this tool based
    on table quality and confidence metrics.
    """

    enabled: bool = Field(
        default=False,
        description="Enable the parse_table tool for the extraction agent. "
        "When enabled, the agent can use deterministic table parsing "
        "for well-formatted Markdown tables in OCR output (works with any OCR backend "
        "that produces Markdown tables: Textract with TABLES/LAYOUT, or Bedrock OCR).",
    )
    min_confidence_threshold: float = Field(
        default=95.0,
        ge=0.0,
        le=100.0,
        description="Minimum average OCR text confidence (Textract 0-100 scale) "
        "for the agent to prefer table parsing over LLM extraction. "
        "Included in the agent's system prompt as guidance.",
    )
    min_parse_success_rate: float = Field(
        default=0.90,
        ge=0.0,
        le=1.0,
        description="Minimum parse_success_rate from the parse_table tool "
        "for the agent to trust the parsed results. Below this threshold, "
        "the agent should fall back to LLM extraction.",
    )
    use_confidence_data: bool = Field(
        default=True,
        description="Whether to load and provide OCR confidence data to the "
        "parse_table tool for quality assessment.",
    )
    max_empty_line_gap: int = Field(
        default=3,
        ge=0,
        le=10,
        description=(
            "Maximum consecutive empty lines to tolerate within a table "
            "before treating it as table boundary. Helps handle OCR page "
            "breaks and artifacts. Higher values are more tolerant but may "
            "merge unrelated tables."
        ),
    )
    auto_merge_adjacent_tables: bool = Field(
        default=True,
        description="Automatically merge consecutive tables with identical column "
        "structure. Helps recover from table splits caused by OCR artifacts like "
        "page breaks. Disable if documents contain multiple similar tables that "
        "should remain separate.",
    )
    lazy_images: bool = Field(
        default=True,
        description="When the deterministic table parser successfully parses the "
        "document's table(s) in pre-flight, do NOT pre-load page images into the "
        "agentic extraction prompt. The table parser is text/markdown-driven and "
        "never reads images, and the agent can still fetch a page on demand via "
        "the view_image tool. Pre-loaded images are re-sent every agent turn and "
        "dominate cost on multi-page documents. Set to false to always attach page "
        "images (image-dependent corpora where the LLM must see page layout even "
        "when a table is present).",
    )

    @field_validator(
        "min_confidence_threshold", "min_parse_success_rate", mode="before"
    )
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        """Parse float from string or number (None -> field default)."""
        return _parse_required_float(v, info, cls)

    @field_validator("max_empty_line_gap", mode="before")
    @classmethod
    def parse_int(cls, v: Any, info: ValidationInfo) -> int:
        """Parse int from string or number (empty/None -> field default)."""
        return _parse_required_int(v, info, cls)


class ValidationConfig(BaseModel):
    """Schema-constraint validation + model-escalation for agentic extraction.

    The dynamic Pydantic model already enforces ``enum``/``pattern``/numeric
    bounds/``minItems`` at the ``extraction_tool`` boundary. This adds full
    JSON-Schema validation (notably ``format`` keywords) on the final result
    and, when it still fails, an optional bounded re-extraction with a stronger
    model. See ``idp_common.extraction.validation``.
    """

    enabled: bool = Field(
        default=True,
        description="Enable full JSON-Schema constraint validation of the "
        "extraction result (in addition to the Pydantic type validation that "
        "always runs). ON by default as of v0.7 — previously off, which left the "
        "guard against silent schema violations disabled on exactly the "
        "configurations that needed it. Free when paired with fail_action='warn'.",
    )
    check_formats: bool = Field(
        default=True,
        description="Enforce JSON-Schema 'format' keywords (date, email, uuid, "
        "...). 'format: date' expects ISO-8601 (YYYY-MM-DD); disable if a config "
        "uses 'format: date' for non-ISO values such as MM/DD/YYYY.",
    )
    fail_action: str = Field(
        default="warn",
        description="What to do when validation fails after the model's own "
        "retries: 'warn' (record a ProcessingIssue only), 'escalate' (re-extract "
        "the failing fields with escalation_model, then warn if still invalid), "
        "or 'reject' (mark parsing_succeeded=false). Defaults to 'warn' as of "
        "v0.7 because 'warn' is FREE, and validation is now enabled by default — "
        "pairing an on-by-default guard with a default action that spends money "
        "on every failure would be a cost surprise. Opt into 'escalate' "
        "deliberately.",
    )
    escalation_model: str | None = Field(
        default=None,
        description="Stronger Bedrock model used to re-extract when validation "
        "fails and fail_action='escalate'. Falls back to the per-class "
        "'x-aws-idp-extraction-escalation-model' override, then to the extraction "
        "model itself (escalation becomes a plain retry).",
    )
    min_population_ratio: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Advisory completeness threshold. After extraction, the "
        "fraction of schema-defined leaf fields that came back populated is "
        "computed; if it falls below this ratio a warning is logged and the "
        "result metadata is flagged (catches silent loss such as nested fields "
        "returning null). Advisory only — never fails extraction. Set to 0 to "
        "disable the warning.",
    )

    @field_validator("min_population_ratio", mode="before")
    @classmethod
    def parse_min_population_ratio(cls, v: Any) -> float:
        """Parse ratio from string or number; empty/None -> default 0.5."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return 0.5
        return float(v)

    @field_validator("fail_action", mode="before")
    @classmethod
    def validate_fail_action(cls, v: Any) -> str:
        """Reject unknown actions early so misconfiguration fails fast.

        An absent/blank value resolves to ``warn`` — the same as the field
        default. It previously resolved to ``escalate``, which was left behind
        when the default was changed and quietly defeated the whole cost-safety
        argument for enabling validation by default: any stored config,
        hand-written YAML or CLI path carrying a null/blank ``fail_action`` would
        have paid for a stronger-model re-extraction on every validation failure.
        A null here is not hypothetical — the config editor has persisted nulls
        for scalar fields before (the ``int(None)`` upgrade-rollback bug).
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return "warn"
        v_str = str(v).lower()
        if v_str not in ("warn", "escalate", "reject"):
            raise ValueError(
                "validation.fail_action must be 'warn', 'escalate' or 'reject', "
                f"got {v!r}"
            )
        return v_str


class AgenticConfig(BaseModel):
    """Agentic extraction configuration"""

    enabled: bool = Field(default=False, description="Enable agentic extraction")
    integrated_confidence_strategy: str = Field(
        default="two_step",
        description=(
            "HIDDEN/EXPERIMENTAL (not surfaced in the config UI). How the agentic "
            "extractor produces confidence when confidence.mode == 'integrated'. "
            "'two_step' (default): the agent extracts via the extraction tool, then "
            "calls provide_field_assessment in a follow-up inference within the same "
            "turn (a dedicated reflection pass over the finalized values). "
            "'single_shot': the agent emits values AND per-field confidence together "
            "in ONE combined tool call, saving the follow-up inference. "
            "'topk': the agent emits, per field, its top-K guesses with probabilities "
            "(G1/P1 … GK/PK) in ONE combined tool call; the shared topk_resolver takes "
            "G1 as the value and P1 as the confidence — the agentic analogue of the "
            "simple-mode 1S-TopK path, for better-calibrated scores. All three produce "
            "identical explainability_info downstream; this only changes inference "
            "mechanics. Provided so cost/latency vs. confidence-calibration can be "
            "A/B tested before choosing a default. Ignored unless "
            "confidence.mode == 'integrated' AND agentic extraction is active."
        ),
    )
    restate_schema_in_system_prompt: bool = Field(
        default=True,
        description=(
            "Append the generated JSON Schema to the agent's SYSTEM prompt "
            "('Expected Schema: ...'). The class schema is already on the wire as "
            "the extraction tool's inputSchema, derived from the same "
            "model_json_schema(), so this is a byte-for-byte duplicate — measured "
            "at ~2,595 of ~5,692 schema tokens per request on the lending "
            "Payslip class (#710). Turning it OFF reclaims that. It defaults ON "
            "because restating a schema in prose often improves adherence, so the "
            "duplication may be load-bearing rather than accidental — this is an "
            "A/B knob, not a recommendation. The agent can still fetch the schema "
            "on demand via get_extraction_schema_reminder, which is unaffected. "
            "Both copies sit inside the prompt-cache prefix, so the dollar saving "
            "is roughly a tenth of the token count. Since #775 the shard budget "
            "subtracts the measured prompt overhead, so turning this OFF frees one "
            "schema copy's worth of per-shard budget; whether that changes a "
            "document's shard count depends on it sitting near a boundary, and the "
            "page ceiling (max_pages_per_shard) usually binds first. Measured on "
            "the benchmark suite before #775: no completeness or accuracy cost, "
            "and no measurable benefit either. Treat it as an instrument for "
            "measuring on your own documents, not a recommendation."
        ),
    )
    review_agent: bool = Field(default=False, description="Enable review agent")
    review_agent_model: str | None = Field(
        default=None,
        description="Model used for reviewing and correcting extraction work",
    )
    max_concurrent_batches: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Max concurrent page-batch agents for parallel extraction. "
        "1 = sequential (default). >1 shards the section's pages into "
        "token-budgeted ranges (each agent sees ONLY its pages' OCR text/images, "
        "not the whole document) and runs them concurrently. This both reduces "
        "wall-clock time AND prevents context-window overflow on long documents. "
        "Acts as an upper bound on parallelism and shard count. Increases Bedrock "
        "RPM — tune to your quota.",
    )
    shard_token_budget: int = Field(
        default=0,
        ge=0,
        description="OPTIONAL OVERRIDE (0 = auto-size from the model). Target "
        "maximum input tokens (~chars/4) of OCR text per shard when "
        "max_concurrent_batches > 1. When 0 (default), this is auto-derived from "
        "the extraction model's context window minus extraction.context_buffer "
        "(see idp_common.bedrock.sizing) — so a 1M-context model shards larger "
        "than a 200K one automatically. Set a non-zero value only to pin it.",
    )
    max_pages_per_shard: int = Field(
        default=5,
        ge=0,
        description="Page count at which a shard closes when max_concurrent_batches "
        "> 1, even if its OCR text is still under the token budget. This is the "
        "TIMEOUT-critical lever (fewer pages/shard = fewer sequential agent turns = "
        "each shard Lambda finishes well under 900s), so it stays a small fixed "
        "default (5) rather than model-derived — a roomy token budget must NOT "
        "collapse a large doc back into one giant shard. NOT an absolute ceiling: "
        "once honouring it would need more shards than max_concurrent_batches "
        "allows, plan_shards repacks the pages into exactly that many "
        "token-balanced shards and this value no longer applies. 0 = disabled "
        "(token budget alone bounds shards; not recommended for large docs).",
    )
    max_images_per_agent: int = Field(
        default=20,
        ge=0,
        description="Safety cap on how many page images are attached to a single "
        "agent invocation when the task prompt uses {DOCUMENT_IMAGE}. Sending many "
        "large images in one request can cause Bedrock read timeouts / oversized "
        "first turns (a long doc with 25+ page images is the classic case). When "
        "the section (or a shard) has more images than this, only the first N are "
        "attached and a warning is logged; the agent still has the full OCR text "
        "and can fetch specific pages with the view_image tool. 0 = unlimited "
        "(legacy behavior). Per-shard sharding already bounds this; the cap is the "
        "backstop for the single-agent path.",
    )
    table_parsing: TableParsingConfig = Field(
        default_factory=TableParsingConfig,
        description="Configuration for deterministic table parsing tool. "
        "When enabled, the extraction agent can parse well-formatted "
        "Markdown tables from OCR output without LLM inference.",
    )
    runtime: str | None = Field(
        default=None,
        description="Sharded-extraction orchestration backend. None/'in_process' "
        "(default) runs shards via asyncio in the single section Lambda — the "
        "standalone/notebook path. 'step_functions' selects the nested SFN "
        "Distributed Map (one Lambda per shard, native per-shard retry/resume). "
        "Selection only affects orchestration; shard/merge logic is shared.",
    )

    @field_validator("integrated_confidence_strategy", mode="before")
    @classmethod
    def _validate_integrated_confidence_strategy(cls, v: Any) -> str:
        """Normalize/validate the (hidden) integrated-confidence strategy.

        Empty/None falls back to the default 'two_step' so a blanked config value
        never breaks the runtime; unknown values are rejected loudly.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return "two_step"
        v = str(v).strip().lower()
        if v not in ("two_step", "single_shot", "topk"):
            raise ValueError(
                "integrated_confidence_strategy must be 'two_step', 'single_shot', "
                f"or 'topk', got {v!r}"
            )
        return v


class ForcedToolConfig(BaseModel):
    """Put the class schema on the wire as a forced Converse tool (WS-05 / #710).

    Simple extraction asks for JSON in prose and parses the reply. This declares
    the class schema as a tool and forces the model to call it, so the response
    SHAPE is enforced by the API rather than requested in text.

    **Off by default, and gated on evidence.** Forcing constrains shape, not
    values: a model that would have returned a stray key may instead return a
    worse value that happens to fit the schema. Whether that trade is a net win is
    an empirical question the benchmark ``forcing`` suite exists to answer, and it
    is entirely possible the answer is no — in which case this stays off.

    What it buys regardless of accuracy is that a malformed-JSON parse failure
    becomes structurally impossible for the declared fields. Note Anthropic
    ``strict`` tool use and structured outputs are rejected by Bedrock on Converse,
    InvokeModel and Mantle Messages alike, so a forced ``toolChoice`` is the
    strongest enforcement available on Claude and Nova.
    """

    enabled: bool = Field(
        default=False,
        description=(
            "Send the document schema as a forced Converse tool instead of "
            "describing it in the prompt. OFF by default: forcing constrains the "
            "response SHAPE, not the values in it, so it is not self-evidently an "
            "improvement and is gated on a measured win. Models that cannot carry "
            "a toolConfig (a custom Lambda hook, GPT-5.x) or that reject a forced "
            "toolChoice (Claude Opus 5.5, Claude Sonnet 5.5) fall back to the "
            "prompt automatically, and the reason is recorded in the section "
            "metadata. "
            "Property names Bedrock rejects are sanitized on the way out and "
            "restored on the way back, so no extracted field is renamed."
        ),
    )
    fallback_to_prompt: bool = Field(
        default=True,
        description=(
            "When the model accepts the tool but answers in prose anyway (a normal "
            "outcome, not an error), parse the text response as before. Turn this "
            "OFF only to make such a response a hard parse failure — useful for "
            "measuring how often forcing is actually honored, and a bad idea in "
            "production."
        ),
    )


class MultiInstanceDetectionConfig(BaseModel):
    """Detect a section that holds several documents of the same class (#753).

    When classification finds no type change to split on, several consecutive
    records of one class land in a single section. The class schema describes ONE
    document, and the model overwhelmingly prefers to answer with one object — so
    the second and third records are simply absent from the response and
    **nothing anywhere reports it**: the section is SUCCESS, the document is
    COMPLETED, ``instance_count`` is 1, and no issue is raised. That is the
    original complaint in GitHub #565 and it is the common branch, not the rare
    one (the rare one — a top-level JSON array — is already recovered and
    warned about).

    The mechanism is to ask the model, in the SAME inference, how many separate
    documents of the class the supplied pages contain: one extra integer in the
    response, no second call, and it asks the only component that has both the
    pages and the schema in front of it. The field is stripped from the result
    before anything downstream sees it.

    **Detection only.** A count greater than 1 raises the
    ``extraction_multi_instance_suspected`` warning and populates
    ``Section.instance_count``; it never changes the extracted data, never fails
    the section, and never flips a class's schema flags. Turning a class into a
    List-of-Class shape stays an explicit opt-in
    (``x-aws-idp-multi-instance`` / ``x-aws-idp-instance-array``).
    """

    enabled: bool = Field(
        default=False,
        description=(
            "Ask the extraction model, in the same inference, how many separate "
            "documents of the section's class the pages contain, and warn when "
            "the answer exceeds the number of records in the result. One extra "
            "integer of output; no second call. "
            "MEASURED on two real labeled corpora (80 paired Test Studio runs): "
            "it is excellent at the job — on 40 bank-check images with committed "
            "ground truth it found all 18 multi-check images, raised 0 false "
            "alarms on the 22 single-check images, and got the count EXACTLY "
            "right on all 18 (2 to 8 checks). Token cost is negligible (input "
            "+1.8%, output -0.5%). "
            "But on a corpus with NO multi-record documents to find it is pure "
            "cost: RealKIE-FCC-Verified lost ~1.3 accuracy points (0.7678 -> "
            "0.7552; worse on 14 of 40 documents, better on 1, sign test "
            "p=0.001), spread diffusely over four attributes rather than any one "
            "failure mode. "
            "So it is OFF by default — a default has to be safe for the corpus "
            "that gets no benefit from it — and you should turn it ON, per "
            "configuration profile, whenever one section can hold several "
            "documents of the same class. There the measurement says it will be "
            "right, and the alternative is shipping one record out of N with no "
            "signal at all. "
            "Applies to Simple extraction (prompt and forced-tool paths); "
            "Advanced (agentic) extraction is not covered."
        ),
    )
    question: str = Field(
        default="",
        description=(
            "The question put to the model, as the description of the auxiliary "
            "count property. Supports {DOCUMENT_CLASS}. Populated from system "
            "defaults; leave blank to use the shipped wording. Two clauses are "
            "load-bearing and should survive any edit: 'do not count pages, "
            "sections or repeated headers' (without it, a document with an "
            "identical banner on each of four pages reads as four documents), and "
            "'DIAGNOSTIC METADATA, not extracted document data' (so the field is "
            "not mistaken for something to extract)."
        ),
    )


class CoercionConfig(BaseModel):
    """Deterministic type/format repair of extraction output before validation.

    Fixes the mismatches that actually occur in otherwise well-formed output —
    ``"$1,234.00"`` in a ``number`` field, ``"03/15/2024"`` under
    ``format: date`` — without a model call. Every change is recorded in
    ``metadata.coercion`` and anything ambiguous is refused rather than guessed.
    See ``idp_common.extraction.coercion``.
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Repair type/format mismatches in the extraction result before "
            "validating it. Free — no model call. On by default because the "
            "alternative is a wrongly-typed value reaching storage, but it does "
            "REWRITE extracted values (always recorded under "
            "metadata.coercion), so set false to disable it entirely and leave "
            "the model's output exactly as returned."
        ),
    )
    date_order: str = Field(
        default="auto",
        description=(
            "How to read an all-numeric date whose day/month order is ambiguous "
            "(e.g. '01/02/2024'). 'auto' (default) REFUSES to guess and leaves "
            "such values untouched; 'MDY' or 'DMY' resolves them for a corpus "
            "you know the convention for. Never overrides a value that is "
            "already unambiguous (a 15 cannot be a month whatever this says)."
        ),
    )

    @field_validator("date_order", mode="before")
    @classmethod
    def validate_date_order(cls, v: Any) -> str:
        """Normalize/reject date_order early rather than at extraction time."""
        if v is None or v == "":
            return "auto"
        value = str(v).strip()
        upper = value.upper()
        if upper in ("MDY", "DMY"):
            return upper
        if value.lower() == "auto":
            return "auto"
        raise ValueError(
            f"extraction.coercion.date_order must be 'auto', 'MDY' or 'DMY', got '{v}'"
        )


class MissingFieldHandlingConfig(BaseModel):
    """Controls how extraction treats fields whose source pages are absent.

    See ``x-aws-idp-page-types`` and ``x-aws-idp-source-page-types`` schema
    extensions for the page-type → property mapping that drives this.
    """

    enabled: bool = Field(
        default=False,
        description=(
            "Enable BLANK vs MISSING field handling. When enabled, properties "
            "whose declared source page-types are absent from the section are "
            "marked as MISSING per the configured representation. Default off "
            "to preserve existing behavior."
        ),
    )
    representation: str = Field(
        default="omit",
        description=(
            "How to represent missing fields in extraction output. 'omit' drops "
            "the key entirely; 'null_with_metadata' keeps the key as null and "
            "lists it under a sibling 'missing_fields' array."
        ),
    )

    @field_validator("representation", mode="before")
    @classmethod
    def validate_representation(cls, v: Any) -> str:
        """Reject unknown representations early so misconfiguration fails fast."""
        if v is None:
            return "omit"
        v_str = str(v)
        if v_str not in ("omit", "null_with_metadata"):
            raise ValueError(
                "missing_field_handling.representation must be 'omit' or "
                f"'null_with_metadata', got {v_str!r}"
            )
        return v_str


class PipelineHook(BaseModel):
    """A single pipeline-hook registration stored inline in a config version
    under a processing step's `postHook` list.

    Feature Platform features (and admins) register post-step hooks by adding
    entries here; the host's pipeline-hooks dispatcher
    (patterns/unified/src/pipeline_hooks_function) reads the active version's
    `<step>.postHook` list and invokes each enabled hook Lambda after that step.

    This MUST be a declared field on every step config (extra="ignore" would
    otherwise silently drop `postHook` whenever a config round-trips through
    IDPConfig — e.g. Save-as-Version, updateConfiguration, or the
    sparse-config auto-migration in ConfigurationManager — leaving the
    dispatcher with no hook to call).
    """

    # extra="allow" so future hook fields don't get dropped on round-trip.
    model_config = ConfigDict(extra="allow")

    featureId: str = Field(  # noqa: N815 — matches stored config key
        description="Owner feature id, for traceability and replace-on-reregister"
    )
    arn: str = Field(description="Lambda ARN the dispatcher invokes")
    order: int = Field(default=100, description="Lower runs first within a hook point")
    # Literal, not bare str: the dispatcher degrades an unrecognised value to
    # "continue" (see _normalize_on_error in
    # patterns/unified/src/pipeline_hooks_function/index.py, which explains why it
    # degrades rather than failing closed), so a typo like "Fail" or "fail-fast"
    # STOPS THE GATE FROM GATING. template.yaml constrains this with `enum` for
    # the config editor and register_feature_hooks validates feature-registered
    # hooks, but a hand-edited YAML pushed through `idp-cli config-upload` reaches
    # here with no other check.
    #
    # This is the WRITE boundary, and it is the only place the typo can be
    # rejected rather than merely reported: the dispatcher is packaged boto3-only
    # and reads the raw DynamoDB record without re-running IDPConfig, so a record
    # written before this constraint existed still reaches it (it logs a warning
    # and records `onErrorInvalid` on the hook's result). Note this validation is
    # also on the stack-deployment path — update_configuration's custom resource
    # builds IDPConfig for every configuration it saves — so an out-of-enum value
    # in a CustomConfigPath YAML fails the stack create/update, not just an upload.
    onError: Literal["continue", "skip-remaining", "fail"] = Field(  # noqa: N815
        default="continue",
        description="continue | skip-remaining | fail",
    )
    enabled: bool = Field(default=True, description="Whether this hook is active")
    args: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Generic key/value args passed to the hook (list of "
        "{key, value}); the hook reads its own config from these, keeping the "
        "pipeline hook-agnostic.",
    )
    allowDocumentUpdate: bool = Field(  # noqa: N815 — matches stored config key
        default=True,
        description="Whether this hook may return an `updatedDocument` that "
        "replaces the document for downstream steps. Set false to pin the hook "
        "to observe-only (it can still read the document and write to S3).",
    )


class FlatHookConfig(BaseModel):
    """Shared shape of the two STANDALONE, single-hook pipeline-hook sections:
    `preprocessing` (runs first) and `postprocessing` (runs last).

    Unlike the post-step hooks — which live in a LIST under each step's
    `postHook` — these two carry ONE inline hook whose `arn`/`args`/`onError`
    live directly on the section. Keeping them flat makes the config UI read
    cleanly (ARN + args right under Preprocessing / Postprocessing).

    Subclasses MUST be declared fields on IDPConfig — otherwise IDPConfig's
    extra="ignore" would silently drop the whole block (and its `args`) whenever
    a config round-trips through IDPConfig (Save-as-Version, updateConfiguration,
    applyFeatureConfigPreset, sparse-config auto-migration), leaving the
    dispatcher with no hook to call.
    """

    # extra="allow" is harmless (the args list carries feature config, not extra
    # top-level fields), but kept for forward-compat with new declared knobs.
    model_config = ConfigDict(extra="allow")

    enabled: bool = Field(
        default=False,
        description="Run this hook for this config version.",
    )
    arn: Optional[str] = Field(
        default=None,
        description="Lambda ARN the dispatcher invokes. Must be tagged "
        "idp:feature-id or named GENAIIDP-*.",
    )
    # Literal for the same reason as PipelineHook.onError above: the dispatcher
    # normalizes an unrecognised value to "continue", so an unconstrained typo
    # turns a declared gate into a no-op.
    onError: Literal["continue", "skip-remaining", "fail"] = Field(  # noqa: N815
        default="continue",
        description="Behavior when the hook errors: continue | skip-remaining | fail",
    )
    args: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Generic key/value args (list of {key, value}) the hook reads "
        "its own config from — keeps the step reusable for any job.",
    )
    featureId: str = Field(  # noqa: N815 — matches stored config key
        default="",
        description="Feature/owner that provides this hook (label only).",
    )
    allowDocumentUpdate: bool = Field(  # noqa: N815 — matches stored config key
        default=True,
        description="Whether this hook may return an `updatedDocument` that "
        "replaces the document. Set false to pin the hook to observe-only (it "
        "can still read the document and write to S3).",
    )


class PreprocessingConfig(FlatHookConfig):
    """Top-level `preprocessing` section (v0.6).

    The standalone PRE-step extension point, which runs FIRST (before the
    BDA/pipeline routing) and may halt the execution. Single flat hook — see
    :class:`FlatHookConfig`.
    """


class PostprocessingConfig(FlatHookConfig):
    """Top-level `postprocessing` section (v0.6).

    The standalone FINAL extension point, which runs LAST (after evaluation,
    before the workflow's terminal state) on the shared tail, so it fires in
    both processing modes. Deliberately symmetrical with
    :class:`PreprocessingConfig`: same single flat-hook shape.

    Two behavioral differences from `preprocessing`, both because the document is
    already fully processed by the time it runs:

    - `onError: continue` (the default) is the recommended setting. `fail` marks
      an otherwise-successful document FAILED, which is rarely what you want for
      a delivery-integration error.
    - `halt` is not actionable — there is nothing downstream to skip. The
      dispatcher reports such a request as `haltIgnored` and continues.
    """


class MalwareScanningConfig(BaseModel):
    """Top-level `malware_scanning` section.

    The POLICY applied to Amazon GuardDuty Malware Protection verdicts on
    uploaded documents. Scanning itself is enabled by the ``MalwareScanning``
    stack parameter, not here — nothing in this section can turn it on, because a
    GuardDuty protection plan is infrastructure and a config row that claimed to
    create one would be lying.

    ⚠️ **Declared here so it survives a model round-trip.** ``IDPConfig`` takes
    Pydantic's default ``extra="ignore"``, so a section with no field would be
    silently dropped the first time a configuration passed through the model —
    an Admin's saved setting would simply vanish on the next save, with nothing
    reporting it.

    A document GuardDuty reports as *infected* is always refused; that is not
    configurable and so has no field.
    """

    on_unscannable: str = Field(
        default="block",
        description=(
            "What to do with a document GuardDuty returned no verdict for "
            "(password-protected, over the size/archive quotas, SSE-C encrypted, "
            "or an internal scan error). 'block' (default) stops it with status "
            "MALWARE_SCAN_FAILED. 'allow' processes it anyway — and takes effect "
            "ONLY if the AllowUnscannableDocuments stack parameter is also true, "
            "so this setting cannot by itself admit an unscanned file."
        ),
    )
    alert_on_block: bool = Field(
        default=True,
        description=(
            "Publish to the stack's SNS alerts topic when a document is "
            "quarantined or refused as unscannable. The CloudWatch metrics and "
            "the document's own status are recorded either way."
        ),
    )

    @field_validator("on_unscannable")
    @classmethod
    def _validate_on_unscannable(cls, value: str) -> str:
        """Refuse anything but the two known values, lower-cased.

        Fails loudly rather than falling back to 'block': a typo like 'Allow' or
        'permit' in a security policy must not be read as the stricter setting
        and then quietly ignored, because the author believed they had loosened
        it and would not find out until an unscannable document was refused.
        """
        normalised = str(value).strip().lower()
        if normalised not in ("block", "allow"):
            raise ValueError(
                f"malware_scanning.on_unscannable must be 'block' or 'allow', "
                f"got {value!r}"
            )
        return normalised


class ConfidenceConfig(BaseModel):
    """Per-field confidence configuration (v0.6).

    Confidence is an optional OUTPUT of extraction, not a separate stage. This
    block (nested as ``extraction.confidence``) is the single home for every knob
    that used to live under the top-level ``assessment`` block — the confidence
    model, its prompts/image/decoding params, the integration mode, and list
    batching (``list_batch_size``). HITL (human review) is its own top-level
    ``hitl`` block; geometry is ``extraction.geometry``.
    """

    mode: str = Field(
        default="separate",
        description=(
            "Confidence scoring mode — the single control for per-field confidence: "
            "'off' (no confidence scoring at all — no extra model pass, no "
            "explainability_info); 'separate' (default — scored in a distinct "
            "inference: a per-shard second pass for advanced/agentic extraction, or "
            "the standalone Assessment step for simple extraction); 'integrated' "
            "(the extraction inference emits each value's confidence in one pass, "
            "saving a model call — the standalone step is bypassed). In simple "
            "extraction a class that declares list fields is automatically scored "
            "in a separate pass even when 'integrated' is selected, because simple "
            "+ integrated loses list rows silently; set "
            "x-aws-idp-allow-integrated-lists: true on a class to opt it back in."
        ),
    )
    enabled: bool = Field(
        default=True,
        description=(
            "DERIVED from `mode` (enabled = mode != 'off'). Retained for backward "
            "compatibility of code that reads confidence.enabled; do not set directly "
            "— use `mode`."
        ),
    )
    model: Optional[str] = Field(
        default=None,
        description="Bedrock model ID for confidence assessment. Use 'LambdaHook' to invoke a custom Lambda function instead of Bedrock.",
    )
    model_lambda_hook_arn: Optional[str] = Field(
        default=None,
        description="Lambda function ARN for custom inference (used when model is 'LambdaHook'). Function name must start with GENAIIDP-.",
    )
    system_prompt: str = Field(
        default="",
        description="System prompt for confidence assessment (populated from system defaults)",
    )
    task_prompt: str = Field(
        default="",
        description=(
            "CONFIDENCE-ONLY task prompt — used by the separate confidence pass "
            "(agentic in-shard second inference and the standalone Assessment step). "
            "The bounding-box block (extraction.geometry.task_prompt_bbox) is "
            "composed in for LLM-box geometry modes. See "
            "prompt_assembly.select_confidence_task_prompt."
        ),
    )
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    top_p: float = Field(default=0.1, ge=0.0, le=1.0)
    top_k: float = Field(default=5.0, ge=0.0)
    reasoning_effort: str = Field(
        default="medium",
        description=(
            "Reasoning effort for reasoning-capable models. Claude Sonnet 5 / "
            "Sonnet 4.6 / Opus 4.5-4.8 / Fable 5 accept low, medium, high, xhigh, "
            "or max (via output_config.effort); OpenAI GPT-5.x accept minimal, "
            "low, medium, or high (via reasoning.effort); xAI Grok accepts none, "
            "low, medium, high, or xhigh (via reasoning.effort, NOT max); OpenAI "
            "GPT-6 Astra accepts none, low, medium, high, xhigh, or max (via "
            "reasoning.effort, NOT minimal). "
            "Ignored by models "
            "without an effort control (Nova, Sonnet 4.5, Haiku 4.5)."
        ),
    )
    # NOTE: max_tokens is intentionally NOT a field. Output is always requested at
    # the model's maximum (resolved from model_config_limits.yaml in the Bedrock
    # client) — Bedrock's default-when-omitted truncates, and capping confidence
    # output risks incomplete per-field scoring. A leftover max_tokens in a stored
    # config is ignored (extra="ignore" default).
    list_batch_size: int = Field(
        default=25,
        gt=0,
        description=(
            "UPPER BOUND on list rows assessed per inference. A single assessment call "
            "over a large list (e.g. 75 transaction rows) is unreliable — the model "
            "under-enumerates or omits the list, leaving rows unassessed — so the list "
            "is scored in batches and the results concatenated, giving every row a "
            "confidence. This is a CEILING, not a target: the batch actually used is "
            "derived per list from the confidence model's output cap, the row's column "
            "count and whether the geometry mode adds a bounding box per cell, and is "
            "only ever smaller than this. On Nova Lite (10,000-token output cap) with "
            "bounding boxes the derived size is 13 rows for a 3-column list and 5 for "
            "8 columns. Lower this to force smaller batches than the derivation; "
            "raising it above the derived size has no effect."
        ),
    )
    escalation_enabled: bool = Field(
        default=True,
        description=(
            "Enable the assessment self-healing ladder: when confidence rows still "
            "come back unscored/truncated after token-aware batch shrinking and "
            "same-model retries, re-assess ONLY the still-missing rows with a "
            "stronger 'escalation_model' (larger output cap). ON by default so "
            "advanced mode completes correctly the first time; the ladder is a no-op "
            "when nothing is missing."
        ),
    )
    escalation_model: Optional[str] = Field(
        default=None,
        description=(
            "Stronger Bedrock confidence model the ladder escalates to (e.g. a "
            "128K-output Claude model when the primary confidence model is Nova Lite "
            "at 10K). Falls back to the per-class "
            "'x-aws-idp-confidence-escalation-model' override. None -> the model "
            "escalation step is skipped (ladder stays at token-aware shrink + retry)."
        ),
    )
    max_escalation_rounds: int = Field(
        default=2,
        ge=0,
        description=(
            "Upper bound on self-healing ladder rounds (token-aware shrink/retry "
            "rounds plus the model-escalation round). Bounds added cost/latency so "
            "the ladder stays within the Lambda wall-clock budget. 0 disables the "
            "ladder entirely."
        ),
    )
    image: ImageConfig = Field(default_factory=ImageConfig)

    @field_validator("temperature", "top_p", "top_k", mode="before")
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        """Parse float from string or number (None -> field default)."""
        return _parse_required_float(v, info, cls)

    @field_validator("list_batch_size", mode="before")
    @classmethod
    def parse_int(cls, v: Any, info: ValidationInfo) -> int:
        """Parse int from string or number (empty/None -> field default)."""
        return _parse_required_int(v, info, cls)

    @field_validator("max_escalation_rounds", mode="before")
    @classmethod
    def parse_max_escalation_rounds(cls, v: Any) -> int:
        """Parse int from string or number; empty/None -> default 2."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return 2
        return int(v)

    @field_validator("mode", mode="before")
    @classmethod
    def validate_mode(cls, v: Any) -> str:
        """Normalize the confidence scoring mode; reject unknown values early."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return "separate"
        v_str = str(v).strip().lower()
        if v_str not in ("off", "separate", "integrated"):
            raise ValueError(
                "extraction.confidence.mode must be 'off', 'separate', or "
                f"'integrated', got {v!r}"
            )
        return v_str

    @model_validator(mode="after")
    def derive_enabled_from_mode(self) -> Self:
        """`enabled` is derived from `mode` (enabled = mode != 'off').

        Back-compat: a config that set `enabled: false` but left mode at its
        'separate' default is honored as OFF (so old disable-via-enabled configs
        still turn confidence off); otherwise mode is authoritative.
        """
        if self.enabled is False and self.mode != "off":
            # Legacy disable-via-enabled: respect it.
            self.mode = "off"
        self.enabled = self.mode != "off"
        return self


class GeometryConfig(BaseModel):
    """Field bounding-box (geometry) configuration (v0.6).

    Nested as ``extraction.geometry``. Geometry is advisory enrichment attached
    to per-field confidence leaves.
    """

    mode: str = Field(
        default="ocr_only",
        description=(
            "How field bounding boxes are produced. A box locates an extracted "
            "VALUE on the page, so a field the model returned nothing for is never "
            "located under any mode. "
            "'ocr_only' (default): the model is not asked for boxes — each "
            "extracted value is matched to real OCR lines (pageData.json), "
            "repeated values disambiguated by row order. No added prompt tokens, "
            "and more accurate than LLM-estimated boxes. "
            "'llm_grounded': the model emits boxes and OCR grounding corrects "
            "them; use this when values are not present verbatim in the OCR text "
            "(reformatted dates, figures read off a chart) and ocr_only therefore "
            "matches too little. "
            "'llm': the model's boxes as-is. DISCOURAGED, and retained only for "
            "compatibility — it sends the same prompt as llm_grounded and costs "
            "the same, then skips the correction step, so there is no case where "
            "it is the better of the two. "
            "'off': no geometry at all; the viewers stop offering to show fields "
            "on the page."
        ),
    )
    task_prompt_bbox: str = Field(
        default="",
        description=(
            "Bounding-box instruction block appended to whichever confidence-bearing "
            "prompt is active (integrated or confidence-only) ONLY when mode is 'llm' "
            "or 'llm_grounded'. Ignored for 'ocr_only'/'off'. See "
            "prompt_assembly._append_bbox_block."
        ),
    )

    @field_validator("mode", mode="before")
    @classmethod
    def validate_mode(cls, v: Any) -> str:
        """Normalize geometry mode; reject unknown values early."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return "ocr_only"
        v_str = str(v).strip().lower()
        if v_str not in ("ocr_only", "llm_grounded", "llm", "off"):
            raise ValueError(
                "extraction.geometry.mode must be 'ocr_only', 'llm_grounded', "
                f"'llm', or 'off', got {v!r}"
            )
        return v_str


class HITLNotificationsConfig(BaseModel):
    """Inbox message retention and per-user cap (idp_common.notifications)."""

    retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Days an Inbox message is kept before DynamoDB TTL removes it.",
    )
    max_per_user: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Messages kept per user; the oldest read message is removed first.",
    )

    @field_validator("retention_days", "max_per_user", mode="before")
    @classmethod
    def parse_whole_number(cls, v: Any, info: ValidationInfo) -> int:
        return _parse_whole_number(v, info, cls)


class HITLConfig(BaseModel):
    """Human-in-the-Loop review configuration (v0.6, top-level ``hitl``).

    HITL is a genuinely separate concern (routing low-confidence extractions to
    human review), so it lives outside extraction. The confidence-scoring path
    reads ``confidence_threshold`` to flag fields; the processresults path reads
    ``enabled`` to decide whether flagged fields trigger a review task.
    """

    enabled: bool = Field(
        default=False,
        description="Enable Human-in-the-Loop review for low-confidence extractions",
    )
    confidence_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Confidence threshold below which a field is flagged for review",
    )
    notifications: HITLNotificationsConfig = Field(
        default_factory=HITLNotificationsConfig,
        description="Inbox message retention and cap. Applies whether or not hitl.enabled is set.",
    )

    @field_validator("confidence_threshold", mode="before")
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        """Parse float from string or number (None -> field default)."""
        return _parse_required_float(v, info, cls)


class ExtractionConfig(BaseModel):
    """Document extraction configuration"""

    postHook: List[PipelineHook] = Field(  # noqa: N815 — matches stored config key
        default_factory=list,
        description="Pipeline hooks invoked after extraction (Feature Platform)",
    )
    context_buffer: float = Field(
        default=0.30,
        ge=0.0,
        le=0.95,
        description=(
            "Fraction of each model's context/output window kept free as safety "
            "headroom (default 0.30 = never use more than 70% of a window). This "
            "is the ONE knob for model-aware auto-sizing: shard token/page budgets "
            "and confidence list-batch sizes are derived from the model's input "
            "and output limits minus this buffer (see idp_common.bedrock.sizing), "
            "so you don't hand-set per-model sizes. The shard budget ALSO subtracts "
            "the estimated per-request prompt overhead (system prompt, the task "
            "prompt with the schema rendered in, few-shot text, the tool schema "
            "and the Advanced-path restatement), so this buffer is a safety margin "
            "on top of that rather than the thing that silently absorbs it; the "
            "other agent tool specs are not counted and stay inside the buffer. "
            "Raise it (e.g. 0.5) if you see "
            "context-overflow or truncation; lower it (e.g. 0.15) to pack more per "
            "shard/batch on a roomy model. The derived sizes, including the "
            "prompt overhead, are logged and (for Advanced extraction) shown in "
            "the processing report."
        ),
    )
    model: str = Field(
        default="us.amazon.nova-pro-v1:0",
        description="Bedrock model ID for extraction. Use 'LambdaHook' to invoke a custom Lambda function instead of Bedrock.",
    )
    prompt_cache: Literal["auto", "off"] = Field(
        default="auto",
        description=(
            "Prompt caching for extraction requests. 'auto' (default) turns every "
            "<<CACHEPOINT>> marker into a Bedrock cachePoint on models that support "
            "it. 'off' sends no cache points at all. A cache WRITE is billed at "
            "1.25x input price and only pays back when a second request with the "
            "same prefix arrives inside the 5-minute TTL, so a low-volume or "
            "interactive deployment that processes one document of a class per "
            "TTL pays about +25% on the prefix for nothing; 'off' is the way to "
            "decline that. Also note each model's MINIMUM cacheable prefix (512 to "
            "4,096 tokens depending on the model): below it a cachePoint silently "
            "does nothing, which config validation now warns about per class."
        ),
    )
    model_lambda_hook_arn: Optional[str] = Field(
        default=None,
        description="Lambda function ARN for custom inference (used when model is 'LambdaHook'). Function name must start with GENAIIDP-.",
    )
    row_shortfall_action: Literal["fail", "warn"] = Field(
        default="warn",
        description=(
            "What a section's outcome MEANS when extraction returned fewer than "
            "half the table rows the section's own OCR text evidences for a list "
            "field — the ``extraction_rows_below_ocr_estimate`` check. 'warn' "
            "(default): record the issue and report success. 'fail': the partial "
            "result and the issue are still written, then the section fails, so "
            "the document's status cannot report COMPLETED on a list that lost "
            "most of its rows. This does NOT change WHEN the shortfall is "
            "detected, only what it costs; the detection threshold is the same "
            "one the warning has always used.\n\n"
            "⚠️ 'fail' is OPT-IN, and the reason is a property of the evidence, "
            "not caution. The check's evidence is OCR tables whose column count "
            "equals the list item's property count, summed over the whole "
            "section, and a 2- or 3-property array that models an entity GROUP "
            "rather than table rows is structurally indistinguishable from one "
            "that models rows. So a monthly statement's 31-row two-column Daily "
            "Balance table is counted as evidence about a 5-row 2-property "
            "``account_summary``, and a completely correct extraction scores "
            "0.13. Nine such fields ship in the config library "
            "(``account_summary``, ``W2.codes``, ``Payslip.{Federal,State,City}"
            "Taxes``, ``Medical-Insurance-Invoice.Charges`` …), so 'fail' by "
            "default would fail correct extractions of the default preset. "
            "Turn it on for a corpus whose narrow arrays really are table rows — "
            "long transaction lists are what it is for — and see "
            "docs/extraction-and-confidence.md for the shapes to check first."
        ),
    )

    @field_validator("row_shortfall_action", mode="before")
    @classmethod
    def _coerce_row_shortfall_action(cls, v: Any) -> Any:
        """An absent/blank/None value resolves to the field default.

        Same hazard as ``validation.fail_action``: the config editor has
        persisted nulls for scalar fields before, and a null arriving here must
        not become a ``ValidationError`` that wedges the whole config load.
        Resolving to ``warn`` is also what keeps an upgrade behaviour-neutral —
        every stored config predating this field has the key absent.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return "warn"
        return str(v).lower()

    @field_validator("prompt_cache", mode="before")
    @classmethod
    def _coerce_prompt_cache(cls, v: Any) -> Any:
        """Accept the YAML booleans that ``off``/``on`` parse to.

        PyYAML (YAML 1.1) reads a bare ``prompt_cache: off`` as ``False`` and
        ``on`` as ``True``; the DynamoDB layer keeps booleans as booleans. Without
        this the documented value failed validation everywhere it is loaded —
        ``idp-cli config-validate``, the extraction Lambda and the deploy-time
        UpdateDefaultConfig custom resource. Unknown strings still fail on the
        Literal.
        """
        if isinstance(v, bool):
            return "off" if v is False else "auto"
        if isinstance(v, str):
            lowered = v.strip().lower()
            if lowered in ("off", "false", "no", "disabled", "0"):
                return "off"
            if lowered in ("auto", "on", "true", "yes", "enabled", "1"):
                return "auto"
        return v

    system_prompt: str = Field(
        default="",
        description="System prompt for extraction (populated from system defaults)",
    )
    task_prompt: str = Field(
        default="",
        description="Task prompt template for EXTRACTION ONLY (used when confidence is disabled or runs separately). Populated from system defaults.",
    )
    task_prompt_extraction_with_confidence: str = Field(
        default="",
        description=(
            "Task prompt template for INTEGRATED extraction+confidence in AGENTIC "
            "(advanced) mode — used when extraction.confidence.mode == 'integrated' "
            "and extraction.mode == 'advanced', where the agent calls the "
            "provide_field_assessment tool after extracting. Populated from system "
            "defaults."
        ),
    )
    task_prompt_extraction_with_confidence_topk: str = Field(
        default="",
        description=(
            "Task prompt template for 1-Stage TopK INTEGRATED extraction+confidence "
            "in SIMPLE (non-agentic) mode — used when "
            "extraction.confidence.mode == 'integrated' and extraction.mode == "
            "'simple'. A single LLM call emits its top-K guesses with probabilities "
            "(G1/P1 … GK/PK) per field; topk_resolver takes G1 as the value and P1 "
            "as the confidence. Requesting ranked alternatives yields better-"
            "calibrated confidence than single-value self-assessment. Populated "
            "from system defaults."
        ),
    )
    # NOTE (v0.6): the confidence-only prompt lives at extraction.confidence.task_prompt
    # and the bounding-box block at extraction.geometry.task_prompt_bbox — each with its
    # own section. Only the extraction-only and integrated templates are top-level here.
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    top_p: float = Field(default=0.1, ge=0.0, le=1.0)
    top_k: float = Field(default=5.0, ge=0.0)
    reasoning_effort: str = Field(
        default="medium",
        description=(
            "Reasoning effort for reasoning-capable models. Claude Sonnet 5 / "
            "Sonnet 4.6 / Opus 4.5-4.8 / Fable 5 accept low, medium, high, xhigh, "
            "or max (via output_config.effort); OpenAI GPT-5.x accept minimal, "
            "low, medium, or high (via reasoning.effort); xAI Grok accepts none, "
            "low, medium, high, or xhigh (via reasoning.effort, NOT max); OpenAI "
            "GPT-6 Astra accepts none, low, medium, high, xhigh, or max (via "
            "reasoning.effort, NOT minimal). "
            "Ignored by models "
            "without an effort control (Nova, Sonnet 4.5, Haiku 4.5)."
        ),
    )
    # NOTE: max_tokens is intentionally NOT a field. Extraction output is always
    # requested at the model's maximum (resolved from model_config_limits.yaml in
    # the Bedrock client / agentic path) — Bedrock's default-when-omitted
    # truncates, and completeness matters more than an output cap for extraction.
    # A leftover max_tokens in a stored config is ignored (extra="ignore" default).
    image: ImageConfig = Field(default_factory=ImageConfig)
    mode: Optional[str] = Field(
        default=None,
        description=(
            "Extraction mode: 'simple' (single-pass — fast/cheap, best for short "
            "documents) or 'advanced' (robust/sharded engine for large documents, "
            "big tables, and completeness). This is the user-facing control; the "
            "underlying 'agentic.enabled' flag is derived from it "
            "('advanced' -> agentic on). If omitted, it is inferred from "
            "agentic.enabled for backward compatibility."
        ),
    )
    agentic: AgenticConfig = Field(default_factory=AgenticConfig)
    confidence: ConfidenceConfig = Field(
        default_factory=ConfidenceConfig,
        description=(
            "Per-field confidence configuration. Confidence is an optional output "
            "of extraction; this block is the single home for the confidence "
            "model, prompts, integration mode, and list batching (v0.6 — replaces "
            "the former top-level 'assessment' block and "
            "'extraction.assessment_integration')."
        ),
    )
    geometry: GeometryConfig = Field(
        default_factory=GeometryConfig,
        description=(
            "Field bounding-box (geometry) configuration (v0.6 — replaces "
            "'assessment.geometry_mode')."
        ),
    )
    forced_tool: ForcedToolConfig = Field(
        default_factory=ForcedToolConfig,
        description=(
            "Send the class schema as a forced Converse tool rather than "
            "describing it in the prompt. Off by default and gated on a measured "
            "win — see ForcedToolConfig."
        ),
    )
    multi_instance_detection: MultiInstanceDetectionConfig = Field(
        default_factory=MultiInstanceDetectionConfig,
        description=(
            "Detect (and warn about) a section that holds several documents of "
            "the same class when the model returned only one — see "
            "MultiInstanceDetectionConfig."
        ),
    )
    coercion: CoercionConfig = Field(
        default_factory=CoercionConfig,
        description=(
            "Deterministic type/format repair of the extraction result, applied "
            "before validation. Free (no model call) and fully recorded; set "
            "enabled=false to leave the model's output exactly as returned."
        ),
    )
    validation: ValidationConfig = Field(
        default_factory=ValidationConfig,
        description=(
            "Full JSON-Schema validation of the extraction result, and the "
            "optional model escalation that follows a failure (v0.7 — moved up "
            "from 'extraction.agentic.validation', because simple extraction "
            "now runs the same validate-and-retry path and the setting is no "
            "longer agentic-only)."
        ),
    )
    missing_field_handling: MissingFieldHandlingConfig = Field(
        default_factory=MissingFieldHandlingConfig,
        description=(
            "Configuration for distinguishing BLANK fields (page present, "
            "field empty) from MISSING fields (page absent). Requires class "
            "schemas to declare 'x-aws-idp-page-types' and properties to "
            "declare 'x-aws-idp-source-page-types'."
        ),
    )
    custom_prompt_lambda_arn: Optional[str] = Field(
        default=None, description="ARN of custom prompt Lambda"
    )

    @field_validator("temperature", "top_p", "top_k", mode="before")
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        """Parse float from string or number (None -> field default)."""
        return _parse_required_float(v, info, cls)

    @field_validator("context_buffer", mode="before")
    @classmethod
    def parse_context_buffer(cls, v: Any) -> float:
        """Parse the context buffer; empty/None -> default 0.30."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return 0.30
        return float(v)

    @field_validator("mode", mode="before")
    @classmethod
    def validate_mode(cls, v: Any) -> Optional[str]:
        """Normalize the extraction mode; reject unknown values early."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        v_str = str(v).strip().lower()
        if v_str not in ("simple", "advanced"):
            raise ValueError(
                f"extraction.mode must be 'simple' or 'advanced', got {v!r}"
            )
        return v_str

    @model_validator(mode="after")
    def reconcile_mode_and_agentic(self) -> Self:
        """Reconcile the user-facing extraction.mode with agentic.enabled.

        - If ``mode`` is set, it is authoritative: 'advanced' -> agentic.enabled=True,
          'simple' -> False (so all existing ``agentic.enabled`` read-sites keep
          working while the UI exposes only Simple/Advanced).
        - If ``mode`` is omitted (legacy config), infer it from agentic.enabled so the
          field is always populated for the UI.
        """
        if self.mode is not None:
            self.agentic.enabled = self.mode == "advanced"
        else:
            self.mode = "advanced" if self.agentic.enabled else "simple"
        return self

    @model_validator(mode="after")
    def set_default_review_agent_model(self) -> Self:
        """Set review_agent_model to extraction model if not specified."""
        if not self.agentic.review_agent_model:
            self.agentic.review_agent_model = self.model

        return self


class ClassificationClassConfidenceConfig(BaseModel):
    """How a classification call reports confidence in the CLASS it chose.

    Carried by each call that assigns a class:
    ``classification.one_stage.llm.confidence`` and
    ``classification.two_stage.section_classification.llm.confidence``. Deliberately NOT the same thing as
    ``extraction.confidence``: that block configures a whole confidence-scoring
    *inference* over extracted fields, whereas this one only decides what the
    classification prompt asks the model to return alongside the class. There is
    no separate classification confidence pass and no separate model — a
    confidence costs output tokens on the inference that is already happening.

    Defaults to ``topk`` on measured evidence rather than on the assumption that
    more information is better. Over 298 pages of a 13-class corpus, asking for
    ranked candidates cost ~0.5 % of TOTAL document cost (+17 % of the
    classification step, which is only ~3 % of the bill on the default model),
    changed accuracy by nothing consistent, and gave a signal that caught 43 % of
    the default model's own misclassifications from 8 % of pages. See
    ``docs/benchmarking/classification-confidence.md``.

    ⚠️ Page-level classification runs ONE INFERENCE PER PAGE, so anything added
    here multiplies by page count — that is why ``mode: off`` exists and why the
    cost was measured before this was turned on. And how *useful* the number is
    depends strongly on the classification model: a small model emits a coarse
    two-level flag, a mid-tier one a graded distribution. Measure the calibration
    on your own documents before routing work on the score.

    Independent of BDA mode, which always has a real score (BDA's matched
    blueprint confidence) at no extra cost.
    """

    mode: str = Field(
        default="topk",
        description=(
            "What the classification prompt asks for beyond the class: 'topk' "
            "(default — ranked candidate classes with probabilities, e.g. 80% W-2 "
            "/ 15% 1099; better calibrated, because enumerating alternatives "
            "forces the model to distribute probability mass instead of answering "
            "~0.95 for everything, cf. Tian et al., 'Just Ask for Calibration', "
            "EMNLP 2023), 'verbalized' (a single self-reported 0-1 number — "
            "cheapest, and the most overconfident), or 'off' (nothing; a page is "
            "then scored only if a custom prompt happens to ask for "
            "`confidence`). Costs OUTPUT TOKENS PER PAGE in every mode but 'off' "
            "— measured at ~0.5% of total document cost for 'topk'."
        ),
    )
    top_k_candidates: int = Field(
        default=3,
        ge=2,
        le=10,
        description=(
            "How many ranked candidate classes to request in "
            "'topk' mode. Must be at least 2 — one candidate is a verbalized "
            "confidence with extra syntax, and the calibration benefit comes "
            "precisely from having to rank alternatives. Capped because the "
            "instruction is repeated per page and a document set rarely has more "
            "than a handful of plausible confusions per page. Automatically "
            "reduced to the number of configured classes when that is smaller."
        ),
    )

    task_prompt_topk: str = Field(
        default="",
        description=(
            "Instruction block spliced into this call's task prompt in 'topk' "
            "mode (populated from system defaults). Editable like every other "
            "prompt here. `{TOP_K_CANDIDATES}` is substituted with the resolved "
            "candidate count. Inserted BEFORE the document/cache-point marker so "
            "it stays inside the prompt-cache prefix — which matters because "
            "classification runs per page."
        ),
    )
    task_prompt_verbalized: str = Field(
        default="",
        description=(
            "Instruction block spliced into this call's task prompt in "
            "'verbalized' mode (populated from system defaults), on the same "
            "splice rules as task_prompt_topk."
        ),
    )
    task_prompt_topk_packet: str = Field(
        default="",
        description=(
            "The 'topk' block for PACKET granularity. A separate template "
            "because the unit differs: a page-level call scores one page's "
            "class, a packet-level call scores each segment it returns, so the "
            "instruction has to name the key it goes in and the per-segment "
            "scope. Empty falls back to task_prompt_topk."
        ),
    )
    task_prompt_verbalized_packet: str = Field(
        default="",
        description=(
            "The 'verbalized' block for PACKET granularity, on the same "
            "per-segment rules as task_prompt_topk_packet. Empty falls back to "
            "task_prompt_verbalized."
        ),
    )

    @field_validator("mode", mode="before")
    @classmethod
    def validate_mode(cls, v: Any) -> str:
        """Normalize the mode; reject unknown values loudly."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return "off"
        v_str = str(v).strip().lower()
        if v_str not in ("off", "topk", "verbalized"):
            raise ValueError(
                f"confidence.mode must be 'off', 'topk', or 'verbalized', got {v!r}"
            )
        return v_str

    @field_validator("top_k_candidates", mode="before")
    @classmethod
    def parse_top_k(cls, v: Any) -> int:
        """Parse from a string (stored configs are string-typed) or number."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return 3
        return int(v)


def _parse_tristate_bool(v: Any, field: str) -> Optional[bool]:
    """Parse a bool that has a third state: ``None`` meaning "not configured".

    Stored configurations are string-typed (the UI writes strings, and so does
    DynamoDB), so ``""`` has to mean unset rather than ``False``. Getting that
    wrong on a modality flag is the difference between "infer from the prompt,
    as every existing config does" and "send nothing", so an unreadable value
    raises instead of defaulting either way.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if not s:
            return None
        if s in ("true", "yes", "y", "on", "1"):
            return True
        if s in ("false", "no", "n", "off", "0"):
            return False
        raise ValueError(
            f"{field} must be true, false, or empty (meaning 'infer from the "
            f"prompt's placeholders'), got {v!r}"
        )
    if isinstance(v, (int, float)):
        return bool(v)
    raise ValueError(f"{field} must be a boolean or empty, got {v!r}")


class ClassificationModalityConfig(BaseModel):
    """What each classification call sees: the page's text, its image, or both.

    Both flags are **tri-state**, and ``None`` — the default — means *infer from
    the prompt's placeholders*, which is precisely what the service did before
    this setting existed: ``{DOCUMENT_TEXT}`` present meant text was sent and
    ``{DOCUMENT_IMAGE}`` present meant the image was. Defaulting to ``None``
    rather than ``True`` is what makes every configuration written before this
    option a literal no-op, including the documented workaround of deleting a
    placeholder to get a single modality.

    Setting a flag explicitly takes over from the inference and the prompt is
    normalised to match: ``images: false`` removes the image placeholder (and so
    the image), ``images: true`` adds one if the prompt has none. The two
    directions are logged, because a prompt the operator did not write is a
    thing they need to be able to see.
    """

    text: Optional[bool] = Field(
        default=None,
        description=(
            "Send each page's OCR text. Empty (default) infers it from whether "
            "the task prompt contains {DOCUMENT_TEXT}. Forced to false by an OCR "
            "backend of 'none', which produces no text to send."
        ),
    )
    images: Optional[bool] = Field(
        default=None,
        description=(
            "Send each page's image. Empty (default) infers it from whether the "
            "task prompt contains {DOCUMENT_IMAGE}. Usually the largest input "
            "cost per page (roughly 1.1K-4.8K tokens depending on the model and "
            "the image size), so turning it off is the main lever on "
            "image-dominated classification cost."
        ),
    )

    @field_validator("text", "images", mode="before")
    @classmethod
    def parse_tristate(cls, v: Any, info: ValidationInfo) -> Optional[bool]:
        return _parse_tristate_bool(v, f"modality.{info.field_name}")

    @model_validator(mode="after")
    def refuse_no_modality(self) -> "ClassificationModalityConfig":
        """Refuse the one combination that sends the model nothing to look at.

        Raised at configuration-save time (every save validates through
        ``IDPConfig``), so the message lands in the UI next to the setting rather
        than as a failed document an hour later.

        Only the *explicit* pair is refused **here**, and the reason is scope rather
        than permission: this block is validated on its own and cannot see the task
        prompt, so when a flag is unset there is nothing at this level to infer it
        from. The equivalent inferred case — a prompt carrying neither
        ``{DOCUMENT_TEXT}`` nor ``{DOCUMENT_IMAGE}``, with neither flag turned on to
        make up for it — is reported by ``merge_utils.validate_config``, which does
        see both. The two now agree that "nothing is sent" is the thing refused;
        neither of them refuses a prompt missing one placeholder, which is the
        documented way to get a single modality.
        """
        if self.text is False and self.images is False:
            raise ValueError(
                "modality has both text and images turned off for this call, "
                "which leaves nothing to classify. Turn at least one on."
            )
        return self


class ClassificationPacketConfig(BaseModel):
    """What a call does at ``granularity: packet``: one call per packet or window.

    The prompts here are separate from the page prompts because granularity is
    not orthogonal to the prompt, even though modality is: a page call is asked
    for one class plus a ``start``/``continue`` verdict, a packet call for a list
    of segments. Different output shapes, so different prompts and parsers.
    """

    window_pages: int = Field(
        default=20,
        ge=0,
        description=(
            "Maximum pages per packet call. A larger packet is split into "
            "overlapping windows whose results are merged. 0 means never window "
            "— one call for the whole packet, which is what legacy holistic "
            "classification did and which fails outright on a packet larger "
            "than the model's context window. The default of 20 is where "
            "Bedrock starts downscaling images: above 20 image blocks in one "
            "request every image is shrunk to 2,000px, so a wider window with "
            "images on silently halves its own page resolution."
        ),
    )
    overlap_pages: int = Field(
        default=2,
        ge=0,
        description=(
            "Pages repeated between adjacent windows, so a document boundary "
            "falling at a window edge is still seen with the pages before it. "
            "Costs `overlap` extra pages per additional window."
        ),
    )
    system_prompt: str = Field(
        default="", description="System prompt for packet calls."
    )
    task_prompt: str = Field(
        default="",
        description=(
            "Task prompt for packet calls. Asks for a `segments` array of page "
            "ranges rather than one class plus a boundary verdict."
        ),
    )

    @field_validator("window_pages", "overlap_pages", mode="before")
    @classmethod
    def parse_page_count(cls, v: Any, info: ValidationInfo) -> int:
        """Parse from a string (stored configs are string-typed) or number."""
        default = 20 if info.field_name == "window_pages" else 2
        if v is None or (isinstance(v, str) and not v.strip()):
            return default
        try:
            return int(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"packet.{info.field_name} must be a whole number of pages, got {v!r}"
            ) from exc

    @model_validator(mode="after")
    def refuse_non_advancing_window(self) -> "ClassificationPacketConfig":
        """Refuse an overlap that would make the window stop advancing.

        With ``overlap >= window`` each window starts no later than the last, so
        a packet larger than one window never terminates — or, with the step
        clamped to 1, costs one call per page while claiming to be the
        cost-saving granularity.
        """
        if self.window_pages and self.overlap_pages >= self.window_pages:
            raise ValueError(
                f"packet.overlap_pages ({self.overlap_pages}) must "
                f"be smaller than window_pages ({self.window_pages}); an overlap "
                f"at least as large as the window means each window covers no new "
                f"pages, so a long packet would cost one call per page."
            )
        return self


_REASONING_EFFORT_DESCRIPTION = (
    "Reasoning effort for reasoning-capable models. Claude Sonnet 5 / Sonnet 4.6 / "
    "Opus 4.5-4.8 / Fable 5 accept low, medium, high, xhigh, or max (via "
    "output_config.effort); OpenAI GPT-5.x accept minimal, low, medium, or high "
    "(via reasoning.effort); xAI Grok accepts none, low, medium, high, or xhigh "
    "(via reasoning.effort, NOT max); OpenAI GPT-6 Astra accepts none, low, medium, "
    "high, xhigh, or max (via reasoning.effort, NOT minimal). Ignored by models "
    "without an effort control (Nova, Sonnet 4.5, Haiku 4.5)."
)

#: Every effort level any supported family accepts — the union of the per-family
#: lists in :data:`_REASONING_EFFORT_DESCRIPTION`, and the same set the editor's
#: picklist offers (asserted against the template in
#: ``scripts/sdlc/tests/test_config_ui_schema_parity.py``). Checked as the union
#: rather than per family because which levels a model takes is a fact about that
#: model, and the client already drops a level the chosen model does not accept with
#: a warning. What is caught here is a value **no** model accepts, which previously
#: reached Bedrock verbatim.
REASONING_EFFORT_VALUES = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

DEFAULT_REASONING_EFFORT = "medium"


def normalise_reasoning_effort(v: Any, path: str) -> str:
    """One of :data:`REASONING_EFFORT_VALUES`; anything else is the default.

    ⚠️ **Normalised rather than refused, and the reason is the same one
    ``FileShapeConfig.parse_max_pages`` gives**: this field has never had a
    validator, and ``save_configuration`` coerces through ``IDPConfig`` without
    touching it, so a typo written by hand or through the API **is stored
    verbatim today**. Raising on it would make such a profile fail validation on
    read — and because it is the stored profile itself that no longer validates,
    there is no resolved-config fallback to return, so every document fails at
    config load and the editor cannot render the profile to correct it. The worst
    case is a stack on Nova, which ignores effort entirely, running happily on a
    misspelled value right now.

    Contrast ``FileShapeConfig.validate_section_splitting``, which *does* raise:
    that validator already normalised on the way in, so an unrecognised value was
    never stored and raising cannot wedge anything. The two settings differ in
    whether the bad state is reachable in stored data, which is the whole question.

    The point of checking at all is that the value is forwarded to the provider as
    written, so the typo must not reach it; substituting the default achieves that
    and is what a family with no effort control did with it anyway.
    """
    if v is None or (isinstance(v, str) and not v.strip()):
        return DEFAULT_REASONING_EFFORT
    v_str = str(v).strip().lower()
    if v_str not in REASONING_EFFORT_VALUES:
        import logging

        logging.getLogger(__name__).warning(
            "%s is %r, which no model family accepts; using %s. Valid values: %s.",
            path,
            v,
            DEFAULT_REASONING_EFFORT,
            ", ".join(REASONING_EFFORT_VALUES),
        )
        return DEFAULT_REASONING_EFFORT
    return v_str


#: The model a classification call falls back to when its own ``model`` is empty.
#:
#: ⚠️ **It must stay equal to the model `base-classification.yaml` ships.** The YAML
#: wins on a normal load, so a divergence here is invisible until a call's model is
#: *cleared* — and at that moment ``default_empty_model`` moves that one call onto a
#: different model, which is a cost and accuracy change nobody asked for, and its
#: warning names a model the profile being edited does not use.
#: ``test_classification_defaults_parity.py`` pins the two together.
DEFAULT_CLASSIFICATION_MODEL = "us.amazon.nova-2-lite-v1:0"


def _parse_float_param(v: Any) -> float:
    if isinstance(v, str):
        return float(v) if v.strip() else 0.0
    return float(v)


def _parse_int_param(v: Any, default: int) -> int:
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    result = int(v)
    return result if result >= 0 else 0


def _normalise_granularity(v: Any, path: str) -> str:
    """``page`` | ``packet``; empty is ``page``; anything else raises.

    Not a silent fallback: the two values produce different prompts, different
    parsers and a roughly N-fold difference in call count, so quietly picking one
    is a cost and accuracy decision the operator did not make.
    """
    if v is None:
        return "page"
    v_str = str(v).strip().lower()
    if v_str == "":
        return "page"
    if v_str in ("page", "packet"):
        return v_str
    raise ValueError(f"{path} must be 'page' or 'packet', got {v!r}")


def _single_value_method(v: Any, path: str, planned: str) -> str:
    """A ``method`` enum that ships with ``llm`` only.

    The other methods are declared in the configuration shape now — their
    sub-blocks exist with defaults — so adding them later needs no migration. They
    are refused until they are implemented, rather than offered and then failing on
    the first document.
    """
    if v is None or (isinstance(v, str) and not v.strip()):
        return "llm"
    v_str = str(v).strip().lower()
    if v_str != "llm":
        raise ValueError(
            f"{path} must be 'llm', got {v!r}. {planned} is declared but not yet "
            "implemented; see GitHub issue #1329."
        )
    return v_str


class ClassificationModelSettings(BaseModel):
    """The settings of one LLM call. Reused by every classification call.

    One block, so a call's model, its parameters, what it is sent and how its page
    images are sized all sit together inside the call that uses them. There is no
    classification-level model any more: a setting one strategy silently ignores
    is the problem this layout exists to remove.
    """

    model: str = Field(
        default=DEFAULT_CLASSIFICATION_MODEL,
        description=(
            "Bedrock model ID for this call. Use 'LambdaHook' to invoke a custom "
            "Lambda function instead of Bedrock."
        ),
    )
    model_lambda_hook_arn: Optional[str] = Field(
        default=None,
        description=(
            "Lambda function ARN for custom inference (used when model is "
            "'LambdaHook'). Function name must start with GENAIIDP-."
        ),
    )
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    # 0.0, not 0.1, because 0.0 is what `base-classification.yaml` ships on all
    # three calls and a positive top_p *overrides* temperature. A field default
    # that disagreed with the shipped value changed the sampling behaviour of any
    # call whose top_p was cleared.
    top_p: float = Field(default=0.0, ge=0.0, le=1.0)
    top_k: float = Field(default=5.0, ge=0.0)
    reasoning_effort: str = Field(
        default=DEFAULT_REASONING_EFFORT, description=_REASONING_EFFORT_DESCRIPTION
    )
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        description=(
            "Optional cap on output tokens. Leave empty to use the selected "
            "model's maximum output limit (recommended). If set, it must not "
            "exceed the model's limit."
        ),
    )
    modality: ClassificationModalityConfig = Field(
        default_factory=ClassificationModalityConfig,
        description=(
            "What this call is sent: page text, page images, or both. Both flags "
            "default to 'infer from this call's task prompt'."
        ),
    )
    image: ImageConfig = Field(
        default_factory=ImageConfig,
        description="How page images are sized for this call, when images are sent.",
    )

    @field_validator("temperature", "top_p", "top_k", mode="before")
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        return _parse_float_param(_default_if_none(v, info, cls))

    @field_validator("max_tokens", mode="before")
    @classmethod
    def parse_max_tokens(cls, v: Any) -> Optional[int]:
        return _parse_optional_max_tokens(v)

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def validate_reasoning_effort(cls, v: Any) -> str:
        """Keep a level no model family accepts from reaching the provider.

        The value is forwarded to ``bedrock.invoke_model`` as written, so an
        unrecognised one previously reached the provider and failed the document —
        or was dropped by a family that ignores effort entirely, leaving the typo in
        the stored profile for the next model change to surface. See
        :func:`normalise_reasoning_effort` for why this substitutes rather than
        raises, and why the sibling ``sectionSplitting`` validator does raise.
        """
        return normalise_reasoning_effort(v, "classification reasoning_effort")

    @field_validator("model", mode="before")
    @classmethod
    def default_empty_model(cls, v: Any) -> str:
        """An empty model is the default model, not "no model".

        Each call has its own model, so a block whose model was cleared would
        otherwise send no model ID at all. The substitution is logged, and
        :data:`DEFAULT_CLASSIFICATION_MODEL` is kept equal to the model the system
        defaults ship so that clearing one call's model does not quietly move it
        onto a different, differently-priced model than its siblings run.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            if v is not None:
                import logging

                logging.getLogger(__name__).warning(
                    "A classification call's model is empty; using the default "
                    "model %s. Set the model on that call to choose another.",
                    DEFAULT_CLASSIFICATION_MODEL,
                )
            return DEFAULT_CLASSIFICATION_MODEL
        return str(v)


class ClassificationValidationConfig(BaseModel):
    """Re-prompt when a call returns a class outside the configured vocabulary.

    Only on calls that decide a class: one stage, and two-stage section
    classification. Never segmentation, which returns no class.
    """

    enforceValidClasses: bool = Field(
        default=True,
        description=(
            "Validate the returned class against the configured classes and "
            "re-prompt when it is not one of them. Off: an out-of-vocabulary "
            "class is logged and used as-is."
        ),
    )
    maxValidationRetries: int = Field(
        default=2,
        ge=0,
        description=(
            "Re-prompts allowed before the fallback class is assigned. Only used "
            "when enforceValidClasses is on."
        ),
    )

    @field_validator("maxValidationRetries", mode="before")
    @classmethod
    def parse_retries(cls, v: Any) -> int:
        return _parse_int_param(v, 2)

    @field_validator("enforceValidClasses", mode="before")
    @classmethod
    def parse_enforce(cls, v: Any) -> bool:
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on")
        return bool(v)


class ClassificationClassAssigningSettings(ClassificationModelSettings):
    """Model settings for a call that decides a CLASS: adds validation and confidence."""

    validation: ClassificationValidationConfig = Field(
        default_factory=ClassificationValidationConfig
    )
    confidence: ClassificationClassConfidenceConfig = Field(
        default_factory=ClassificationClassConfidenceConfig,
        description=(
            "Confidence in the class (see ClassificationClassConfidenceConfig). "
            "Unrelated to extraction.confidence, which scores extracted fields."
        ),
    )


class ClassificationPageInputConfig(BaseModel):
    """What a call does at ``granularity: page``: one call per page."""

    contextPagesCount: int = Field(
        default=0,
        description=(
            "Pages before and after the current page to send as context. 0 sends "
            "none. Page granularity only: a packet call already sees every page in "
            "its window."
        ),
    )
    system_prompt: str = Field(default="", description="System prompt for page calls.")
    task_prompt: str = Field(default="", description="Task prompt for page calls.")

    @field_validator("contextPagesCount", mode="before")
    @classmethod
    def parse_context(cls, v: Any) -> int:
        return _parse_int_param(v, 0)


class OneStageConfig(BaseModel):
    """``strategy: single_pass`` — boundaries and classes from the same call."""

    granularity: str = Field(
        default="page",
        description=(
            "'page': one call per page. 'packet': one call per packet, or per "
            "window, paying the instructions, class list and examples once rather "
            "than per page. Image tokens are the same either way."
        ),
    )
    page: ClassificationPageInputConfig = Field(
        default_factory=ClassificationPageInputConfig
    )
    packet: ClassificationPacketConfig = Field(
        default_factory=ClassificationPacketConfig
    )
    llm: ClassificationClassAssigningSettings = Field(
        default_factory=ClassificationClassAssigningSettings
    )

    @field_validator("granularity", mode="before")
    @classmethod
    def validate_granularity(cls, v: Any) -> str:
        return _normalise_granularity(v, "classification.one_stage.granularity")


class SegmentationLLMConfig(ClassificationModelSettings):
    """Two-stage segmentation by an LLM: boundaries only, no class vocabulary.

    Model settings only — no validation and no class confidence — because this
    call returns page ranges and never a class. The saving over one stage is the
    prefix: no class list, descriptions or few-shot examples on any call.
    """

    granularity: str = Field(
        default="page",
        description="'page': one boundary call per page. 'packet': per packet or window.",
    )
    page: ClassificationPageInputConfig = Field(
        default_factory=ClassificationPageInputConfig
    )
    packet: ClassificationPacketConfig = Field(
        default_factory=ClassificationPacketConfig
    )
    boundary_hints: str = Field(
        default="none",
        description=(
            "'none' sends no class vocabulary. 'class_names' sends the class names "
            "only — not descriptions, not examples — which can help when a "
            "boundary is marked by a form number or title the names mention."
        ),
    )

    @field_validator("granularity", mode="before")
    @classmethod
    def validate_granularity(cls, v: Any) -> str:
        return _normalise_granularity(
            v, "classification.two_stage.segmentation.llm.granularity"
        )

    @field_validator("boundary_hints", mode="before")
    @classmethod
    def validate_boundary_hints(cls, v: Any) -> str:
        if v is None or (isinstance(v, str) and not v.strip()):
            return "none"
        v_str = str(v).strip().lower()
        if v_str not in ("none", "class_names"):
            raise ValueError(
                "classification.two_stage.segmentation.llm.boundary_hints must be "
                f"'none' or 'class_names', got {v!r}"
            )
        return v_str


class ExternalModelConfig(BaseModel):
    """An external model behind a stable contract. **Declared, not yet implemented.**

    Shared by the boundary model (segmentation) and the decision model (section
    classification), so both methods land without a configuration migration.
    """

    backend: str = Field(default="", description="sagemaker | bedrock_custom | lambda")
    endpoint: str = Field(
        default="",
        description="Endpoint name, model ARN, or function ARN (GENAIIDP- prefix).",
    )
    input: str = Field(default="images", description="images | text | both")
    timeout_ms: int = Field(default=30000, ge=0)
    max_concurrency: int = Field(default=4, ge=1)
    price_per_request: float = Field(
        default=0.0, ge=0.0, description="Per-request price, for metering."
    )


class SegmentationConfig(BaseModel):
    """Two-stage segmentation: where each document starts and ends."""

    method: str = Field(
        default="llm",
        description="'llm' (the only value today). 'boundary_model' is declared.",
    )
    llm: SegmentationLLMConfig = Field(default_factory=SegmentationLLMConfig)
    boundary_model: ExternalModelConfig = Field(default_factory=ExternalModelConfig)

    @field_validator("method", mode="before")
    @classmethod
    def validate_method(cls, v: Any) -> str:
        return _single_value_method(
            v, "classification.two_stage.segmentation.method", "'boundary_model'"
        )


class SectionClassificationLLMConfig(ClassificationClassAssigningSettings):
    """Two-stage section classification by an LLM: one call per section."""

    system_prompt: str = Field(default="", description="System prompt.")
    task_prompt: str = Field(
        default="",
        description=(
            "Given a section's pages, return its class. Carries the class "
            "vocabulary and {FEW_SHOT_EXAMPLES}."
        ),
    )


class EmbeddingsConfig(BaseModel):
    """Section classification by embedding similarity. **Declared, not implemented.**"""

    model: str = Field(default="", description="A multimodal embeddings model.")
    min_similarity: float = Field(default=0.0, ge=0.0, le=1.0)


class DecisionModelConfig(ExternalModelConfig):
    """Section classification by an external model. **Declared, not implemented.**"""

    label_map: Dict[str, str] = Field(
        default_factory=dict,
        description="Model label → configured class (or 'unclassified').",
    )
    score_calibration: str = Field(
        default="none", description="none | softmax | per_class_thresholds"
    )


class LowConfidenceConfig(BaseModel):
    """What happens when a non-LLM section classifier is not confident.

    Applies only when ``section_classification.method`` is not ``llm``. Below
    ``min_confidence`` the section is either re-run through the LLM block or given
    ``classification.fallback_class`` — the same fallback used when validation
    retries run out, so there is one fallback setting, not two.
    """

    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    on_low_confidence: str = Field(
        default="fallback_class", description="'llm' | 'fallback_class'"
    )


class SectionClassificationConfig(BaseModel):
    """Two-stage section classification: one class per section, not per page."""

    method: str = Field(
        default="llm",
        description="'llm' (the only value today). 'embeddings' and 'decision_model' are declared.",
    )
    pages: str = Field(
        default="first_k",
        description=(
            "Which pages of a section are sent. 'first' the first page, 'first_k' "
            "the first `k`, 'all' every page. A document's identity is almost "
            "always on its opening pages, while 'all' makes a long section cost as "
            "much as page-level classification."
        ),
    )
    k: int = Field(default=2, ge=1, description="How many pages 'first_k' sends.")
    llm: SectionClassificationLLMConfig = Field(
        default_factory=SectionClassificationLLMConfig
    )
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    decision_model: DecisionModelConfig = Field(default_factory=DecisionModelConfig)
    low_confidence: LowConfidenceConfig = Field(default_factory=LowConfidenceConfig)

    @field_validator("method", mode="before")
    @classmethod
    def validate_method(cls, v: Any) -> str:
        return _single_value_method(
            v,
            "classification.two_stage.section_classification.method",
            "'embeddings' / 'decision_model'",
        )

    @field_validator("pages", mode="before")
    @classmethod
    def validate_pages(cls, v: Any) -> str:
        if v is None or (isinstance(v, str) and not v.strip()):
            return "first_k"
        v_str = str(v).strip().lower()
        if v_str not in ("first", "first_k", "all"):
            raise ValueError(
                "classification.two_stage.section_classification.pages must be "
                f"'first', 'first_k', or 'all', got {v!r}"
            )
        return v_str

    @field_validator("k", mode="before")
    @classmethod
    def parse_k(cls, v: Any) -> int:
        return _parse_int_param(v, 2) or 1


class TwoStageConfig(BaseModel):
    """``strategy: two_stage`` — segment first, then classify each section."""

    segmentation: SegmentationConfig = Field(default_factory=SegmentationConfig)
    section_classification: SectionClassificationConfig = Field(
        default_factory=SectionClassificationConfig
    )


#: The three shapes a file can be declared to have. Shared with the migration, which
#: has to recognise ``disabled`` in a raw stored value before this validator sees it,
#: and mirrored in ``src/ui/src/utils/classification-config.ts`` (kept in step by
#: ``scripts/tests/test_classification_flat_keys_ui_parity.py``).
SECTION_SPLITTING_VALUES = ("llm_determined", "disabled", "page")

#: What an unset ``sectionSplitting`` means: detect the boundaries.
DEFAULT_SECTION_SPLITTING = "llm_determined"


def _unusable_page_limit(value: Any) -> str:
    """``'ALL'``, plus the warning that the operator's page limit is not in force."""
    import logging

    logging.getLogger(__name__).warning(
        "classification.file_shape.maxPagesForClassification is %r, which is not "
        "'ALL' and not a positive number of pages; classifying every page instead. "
        "Set it to ALL, or to the number of opening pages whose majority class the "
        "whole file should take.",
        value,
    )
    return "ALL"


class FileShapeConfig(BaseModel):
    """Advanced: what you already know about a file's shape.

    By default every file is treated as a packet and its boundaries are detected.
    These settings are assertions that override that, which is why they sit apart
    from the strategy rather than beside it.
    """

    sectionSplitting: str = Field(
        default="llm_determined",
        description=(
            "'llm_determined' (default): detect the boundaries. 'disabled': the "
            "whole file is one document. 'page': every page is its own document."
        ),
    )
    maxPagesForClassification: str = Field(
        default="ALL",
        description=(
            "With sectionSplitting 'disabled', classify only the first N pages and "
            "give the whole file their majority class. 'ALL' classifies every page."
        ),
    )

    @field_validator("sectionSplitting", mode="before")
    @classmethod
    def validate_section_splitting(cls, v: Any) -> str:
        """One of the three values; empty is the default; anything else raises.

        ⚠️ **Not a silent fallback**, for the reason ``_normalise_granularity``'s
        docstring gives about its own enum, and more so here: this setting decides
        whether boundary detection happens at all, and the value it used to fall back
        to — ``llm_determined`` — is the expensive one and changes where sections
        start. A warning is the wrong instrument for that, because the reader is a
        Lambda log nobody is watching at save time.

        An **empty** value is the default and is silent, matching every sibling
        validator here: clearing the field in the editor is "use the default", not a
        mistake, and it used to produce a spurious "Invalid sectionSplitting value
        ''" line.

        ⚠️ **This is the only classification setting that refuses an unrecognised
        value, and the neighbouring validators argue the opposite direction for
        themselves — so what distinguishes it is worth stating.** Two things, and
        they are independent. First, **there is no safe reading of an unrecognised
        splitting mode.** Its neighbours each have one that the pipeline already
        applied: an unusable page limit means "classify every page", and an
        unrecognised ``reasoning_effort`` means the default, because a family with
        no effort control did exactly that with it. A splitting mode decides whether
        boundaries are detected at all, and every candidate answer is a different
        document structure. Second, **a bad value here is not reachable in stored
        data**, because the validator on this key has normalised on the way in since
        v0.4.6 — in every release including v0.6.12, where the key was flat on
        ``ClassificationConfig`` and ``save_configuration`` coerced the dict through
        ``IDPConfig`` before writing it — so no save path in any shipped release could
        persist an off-list value. That is precisely the condition
        :func:`normalise_reasoning_effort` and :meth:`parse_max_pages` do *not* meet,
        and the reason they normalise: ``reasoning_effort`` had no validator at all,
        and ``maxPagesForClassification``'s stored whatever it was given.

        What that costs is worth knowing: ``update_configuration`` validates through
        these same models at deploy time, so an unrecognised value in a hand-written
        ``CustomConfigPath`` file fails the stack operation rather than a save.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return DEFAULT_SECTION_SPLITTING
        v_str = str(v).strip().lower()
        if v_str not in SECTION_SPLITTING_VALUES:
            raise ValueError(
                "classification.file_shape.sectionSplitting must be one of "
                f"{', '.join(SECTION_SPLITTING_VALUES)} (or empty for the default, "
                f"{DEFAULT_SECTION_SPLITTING}), got {v!r}"
            )
        return v_str

    @field_validator("maxPagesForClassification", mode="before")
    @classmethod
    def parse_max_pages(cls, v: Any) -> str:
        """``'ALL'`` or a positive page count; anything unusable becomes ``'ALL'``.

        ⚠️ **A ``bool`` is not a page count**, although ``bool`` is an ``int``
        subclass — an unquoted YAML ``maxPagesForClassification: true`` used to be
        read as a one-page limit. ``migrations/_version.py`` guards the same trap on
        the version stamp.

        Everything the runtime cannot use normalises to ``'ALL'``, with a warning,
        rather than being stored verbatim. ``'abc'`` and ``'-3'`` used to validate and
        be stored, then be discarded twice over at run time — by the
        ``sectionSplitting`` check and again by ``int()`` inside a bare ``except`` —
        so the editor showed a page limit that was never in force. ``'ALL'`` is what
        the runtime already does with such a value, so the stored value now says what
        will happen.

        The int/string asymmetry is **narrowed, not removed**: a negative limit now
        agrees whichever way it is spelled (``-5`` and ``'-5'`` are both no limit),
        and a fraction below one page is no limit either way. What still differs is
        a usable fraction — a real number is truncated, so ``2.5`` is two pages,
        while ``'2.5'`` is refused, because a string must already spell a whole
        number. ``_is_page_limit`` mirrors that split, and so does the editor's
        predicate; the cases all three must agree on are the shared table in
        ``src/ui/src/utils/__tests__/page-limit.fixtures.json``.

        Normalising rather than raising is deliberate: unlike ``sectionSplitting``,
        today's validator *stores* an unusable value, so one may already sit in a
        stored profile, and raising would make that profile unreadable — which fails
        every document — to correct a setting that was never in effect.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return "ALL"
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            return _unusable_page_limit(v)
        if isinstance(v, float) and not math.isfinite(v):
            # `int(inf)` raises OverflowError, which pydantic does not convert into
            # a ValidationError — so a `.inf` in a YAML file failed the profile's
            # load with a raw traceback, which is exactly what this validator
            # normalises in order to avoid.
            return _unusable_page_limit(v)
        if isinstance(v, str):
            if v.strip().upper() == "ALL":
                return "ALL"
            try:
                v = int(v.strip())
            except ValueError:
                return _unusable_page_limit(v)
        if v == 0:
            # The long-documented legacy spelling of "ALL". Silent, like every other
            # "this field is unset, take the default" case here.
            return "ALL"
        pages = int(v)
        if pages <= 0:
            # Negative, or a fraction below one page — which is not a page count.
            # `0.4` used to be stored as `"0"` while the migration read it as a
            # limit and took `sectionSplitting` to `disabled` with it, which is the
            # bool trap above in a second spelling.
            return _unusable_page_limit(v)
        return str(pages)


class ClassificationConfig(BaseModel):
    """Document classification configuration (config format 0.9).

    ``strategy`` decides how many calls are made and what each does. Every
    model setting lives inside the call that uses it — ``one_stage``, or
    ``two_stage.segmentation`` and ``two_stage.section_classification`` — so no
    setting at this level is silently ignored by one strategy. The flat keys of
    earlier formats are converted on read by ``migrations.v08_to_v09``.
    """

    postHook: List[PipelineHook] = Field(  # noqa: N815 — matches stored config key
        default_factory=list,
        description="Pipeline hooks invoked after classification (Feature Platform)",
    )
    strategy: str = Field(
        default="single_pass",
        description=(
            "'single_pass' (One stage): one call returns both the boundaries and "
            "the classes. 'two_stage': boundaries first with no class vocabulary, "
            "then one call per section to classify it."
        ),
    )
    fallback_class: str = Field(
        default="unclassified",
        description=(
            "Class assigned when a class cannot be decided: validation retries ran "
            "out, or a non-LLM section classifier was not confident. One of the "
            "configured classes, or the built-in 'unclassified'."
        ),
    )
    one_stage: OneStageConfig = Field(default_factory=OneStageConfig)
    two_stage: TwoStageConfig = Field(default_factory=TwoStageConfig)
    file_shape: FileShapeConfig = Field(default_factory=FileShapeConfig)

    @model_validator(mode="before")
    @classmethod
    def migrate_flat_keys(cls, data: Any) -> Any:
        """Convert pre-0.9 flat keys even when this model is built directly.

        ``IDPConfig`` already migrates on load. This covers a caller that builds
        ``ClassificationConfig(**flat)`` itself, which would otherwise have every
        flat key silently discarded by ``extra="ignore"`` — the same conversion,
        at a second entry point, so neither route loses a setting.

        ⚠️ **The whole chain, not the last hop.** Running only ``v08_to_v09`` made
        the two routes disagree for a v0.7 *holistic* configuration:
        ``classificationMethod`` is not a v0.8 flat key, so it survived that hop and
        was then dropped by ``extra="ignore"`` — leaving ``granularity: page``, and
        with it the shipped page prompt in place of the operator's hand-written
        packet one. ``migrate_config`` is idempotent and each hop is a no-op when its
        marker is absent, so running all of them costs nothing a 0.8 dict notices.
        """
        if not isinstance(data, dict):
            return data
        from .migrations import migrate_config
        from .migrations.v07_to_v08 import LEGACY_METHOD_KEY
        from .migrations.v08_to_v09 import FLAT_KEYS

        if not (FLAT_KEYS | {LEGACY_METHOD_KEY}) & data.keys():
            return data
        return migrate_config({"classification": data})["classification"]

    @field_validator("strategy", mode="before")
    @classmethod
    def validate_strategy(cls, v: Any) -> str:
        if v is None or (isinstance(v, str) and not v.strip()):
            return "single_pass"
        v_str = str(v).strip().lower()
        if v_str not in ("single_pass", "two_stage"):
            raise ValueError(
                "classification.strategy must be 'single_pass' or 'two_stage', "
                f"got {v!r}"
            )
        return v_str

    @field_validator("fallback_class", mode="before")
    @classmethod
    def default_fallback(cls, v: Any) -> str:
        if v is None or (isinstance(v, str) and not v.strip()):
            return "unclassified"
        return str(v)

    # ── Resolved views. Every reader goes through these rather than walking the
    # tree, so the strategy-dependent choice of block is made in one place.

    def page_input_call(self) -> Union[OneStageConfig, SegmentationLLMConfig]:
        """The block whose granularity / page / packet settings are in force."""
        if self.strategy == "two_stage":
            return self.two_stage.segmentation.llm
        return self.one_stage

    def class_assigning_llm(
        self,
    ) -> Union[ClassificationClassAssigningSettings, SectionClassificationLLMConfig]:
        """The block that decides the class under the active strategy."""
        if self.strategy == "two_stage":
            return self.two_stage.section_classification.llm
        return self.one_stage.llm


#: Where the class-assigning call's settings live, per strategy. A tool writing
#: "the classification model" — a custom or fine-tuned model ARN, say — writes
#: here, because that is the call whose model decides the class.
CLASS_ASSIGNING_LLM_PATHS: Dict[str, Tuple[str, ...]] = {
    "single_pass": ("one_stage", "llm"),
    "two_stage": ("two_stage", "section_classification", "llm"),
}


def class_assigning_llm_path(classification: Any) -> Tuple[str, ...]:
    """The path, under ``classification``, of the active class-assigning call.

    Reads the raw dict so a writer can use it before validation. An absent or
    unrecognised strategy is ``single_pass``, the default.
    """
    strategy = (
        str(classification.get("strategy") or "").strip().lower()
        if isinstance(classification, dict)
        else ""
    )
    return CLASS_ASSIGNING_LLM_PATHS.get(
        strategy, CLASS_ASSIGNING_LLM_PATHS["single_pass"]
    )


class SummarizationConfig(BaseModel):
    """Document summarization configuration"""

    postHook: List[PipelineHook] = Field(  # noqa: N815 — matches stored config key
        default_factory=list,
        description="Pipeline hooks invoked after summarization (Feature Platform)",
    )
    enabled: bool = Field(default=True, description="Enable summarization")
    # Nova Pro, not Nova Premier. Premier reached end of life on 2026-09-14 and
    # every call to it now fails, so it was the one default in this file that
    # made a stack fail out of the box: no config preset under config_library/
    # sets summarization.model, so this default is what most deployments run.
    # Pro is the highest-tier Nova still Active and is already the default for
    # classification and discovery below.
    model: str = Field(
        default="us.amazon.nova-pro-v1:0",
        description="Bedrock model ID for summarization. Use 'LambdaHook' to invoke a custom Lambda function instead of Bedrock.",
    )
    model_lambda_hook_arn: Optional[str] = Field(
        default=None,
        description="Lambda function ARN for custom inference (used when model is 'LambdaHook'). Function name must start with GENAIIDP-.",
    )
    system_prompt: str = Field(
        default="", description="System prompt for summarization"
    )
    task_prompt: str = Field(
        default="", description="Task prompt template for summarization"
    )
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    top_p: float = Field(default=0.1, ge=0.0, le=1.0)
    top_k: float = Field(default=5.0, ge=0.0)
    reasoning_effort: str = Field(
        default="medium",
        description=(
            "Reasoning effort for reasoning-capable models. Claude Sonnet 5 / "
            "Sonnet 4.6 / Opus 4.5-4.8 / Fable 5 accept low, medium, high, xhigh, "
            "or max (via output_config.effort); OpenAI GPT-5.x accept minimal, "
            "low, medium, or high (via reasoning.effort); xAI Grok accepts none, "
            "low, medium, high, or xhigh (via reasoning.effort, NOT max); OpenAI "
            "GPT-6 Astra accepts none, low, medium, high, xhigh, or max (via "
            "reasoning.effort, NOT minimal). "
            "Ignored by models "
            "without an effort control (Nova, Sonnet 4.5, Haiku 4.5)."
        ),
    )
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        description="Optional cap on output tokens. Leave empty to use the selected model's maximum output limit (recommended). If set, it must not exceed the model's limit.",
    )
    max_extraction_array_items: int = Field(
        default=50,
        ge=0,
        description=(
            "When injecting EXTRACTION_RESULTS into the summarization prompt, any "
            "array longer than this is elided to its first/last few items plus a "
            "'... (N items total)' marker. A summary needs counts/totals, not "
            "every row, and the full pretty-printed list is the dominant token "
            "term that overflows the model context window on large documents. "
            "0 disables elision (inject the full arrays)."
        ),
    )

    @field_validator("temperature", "top_p", "top_k", mode="before")
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        """Parse float from string or number (None -> field default)."""
        return _parse_required_float(v, info, cls)

    @field_validator("max_tokens", mode="before")
    @classmethod
    def parse_int(cls, v: Any) -> Optional[int]:
        """Parse optional max_tokens (empty/0 -> None = use model max)."""
        return _parse_optional_max_tokens(v)

    @field_validator("max_extraction_array_items", mode="before")
    @classmethod
    def parse_array_cap(cls, v: Any) -> int:
        """Parse the array cap (empty string -> default 50)."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return 50
        return int(v)


class ChatConfig(BaseModel):
    """Chat-with-Document configuration.

    Controls the interactive "Chat with Document" feature available on the
    Document Detail screen. This is decoupled from summarization so that
    chat can use a different (typically larger-context) model.
    """

    enabled: bool = Field(default=True, description="Enable Chat-with-Document")
    model: str = Field(
        default="us.anthropic.claude-opus-4-8:1m",
        description=(
            "Bedrock model ID used for Chat-with-Document. A large-context "
            "model is recommended because the entire document text is sent "
            "in a single prompt. Use 'LambdaHook' to invoke a custom Lambda "
            "function instead of Bedrock."
        ),
    )
    model_lambda_hook_arn: Optional[str] = Field(
        default=None,
        description=(
            "Lambda function ARN for custom inference (used when model is "
            "'LambdaHook'). Function name must start with GENAIIDP-."
        ),
    )
    system_prompt: str = Field(
        default=(
            "You are an assistant that answers questions about the attached "
            "document text. If you don't know the answer, say so. Do not "
            "invent information. Use the prior chat history provided as "
            "context. Respond in plain text, not JSON."
        ),
        description="System prompt for the Chat-with-Document assistant",
    )
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    top_p: float = Field(default=0.1, ge=0.0, le=1.0)
    top_k: float = Field(default=5.0, ge=0.0)
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        description=(
            "Optional cap on output (response) tokens. Leave empty to use the "
            "selected model's maximum output limit (recommended). If set, it "
            "must not exceed the model's limit."
        ),
    )
    reasoning_effort: str = Field(
        default="medium",
        description=(
            "Reasoning effort for reasoning-capable models. Claude Sonnet 5 / "
            "Sonnet 4.6 / Opus 4.5-4.8 / Fable 5 accept low, medium, high, xhigh, "
            "or max (via output_config.effort); OpenAI GPT-5.x accept minimal, "
            "low, medium, or high (via reasoning.effort); xAI Grok accepts none, "
            "low, medium, high, or xhigh (via reasoning.effort, NOT max); OpenAI "
            "GPT-6 Astra accepts none, low, medium, high, xhigh, or max (via "
            "reasoning.effort, NOT minimal). "
            "Ignored by models "
            "without an effort control (Nova, Sonnet 4.5, Haiku 4.5)."
        ),
    )

    @field_validator("temperature", "top_p", "top_k", mode="before")
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        """Parse float from string or number (None -> field default)."""
        return _parse_required_float(v, info, cls)

    @field_validator("max_tokens", mode="before")
    @classmethod
    def parse_int(cls, v: Any) -> Optional[int]:
        """Parse optional max_tokens (empty/0 -> None = use model max)."""
        return _parse_optional_max_tokens(v)


class OCRFeature(BaseModel):
    """OCR feature configuration"""

    name: str = Field(description="Feature name (e.g., LAYOUT, TABLES, FORMS)")


class OCRConfig(BaseModel):
    """OCR configuration"""

    postHook: List[PipelineHook] = Field(  # noqa: N815 — matches stored config key
        default_factory=list,
        description="Pipeline hooks invoked after OCR (Feature Platform)",
    )
    backend: str = Field(
        default="textract",
        description="OCR backend: 'textract', 'bedrock' (LLM OCR), 'bda' (Bedrock Data Automation), or 'none' (image-only)",
    )
    bda_project_arn: Optional[str] = Field(
        default=None,
        description=(
            "ARN of a Bedrock Data Automation standard-output SYNC project used "
            "when backend='bda'. Normally left unset: the stack provisions a "
            "per-stack BDA OCR project (via a CloudFormation custom resource) "
            "and delivers its ARN through the BDA_OCR_PROJECT_ARN env var. "
            "Setting this overrides the stack-provided project."
        ),
    )
    model_id: Optional[str] = Field(
        default=None,
        description="Bedrock model ID for OCR (if backend=bedrock). Use 'LambdaHook' to invoke a custom Lambda function instead of Bedrock.",
    )
    model_lambda_hook_arn: Optional[str] = Field(
        default=None,
        description="Lambda function ARN for custom inference (used when model_id is 'LambdaHook'). Function name must start with GENAIIDP-.",
    )
    system_prompt: Optional[str] = Field(
        default=None, description="System prompt for Bedrock OCR"
    )
    task_prompt: Optional[str] = Field(
        default=None, description="Task prompt for Bedrock OCR"
    )
    reasoning_effort: str = Field(
        default="medium",
        description=(
            "Reasoning effort for reasoning-capable models. Claude Sonnet 5 / "
            "Sonnet 4.6 / Opus 4.5-4.8 / Fable 5 accept low, medium, high, xhigh, "
            "or max (via output_config.effort); OpenAI GPT-5.x accept minimal, "
            "low, medium, or high (via reasoning.effort); xAI Grok accepts none, "
            "low, medium, high, or xhigh (via reasoning.effort, NOT max); OpenAI "
            "GPT-6 Astra accepts none, low, medium, high, xhigh, or max (via "
            "reasoning.effort, NOT minimal). "
            "Ignored by models "
            "without an effort control (Nova, Sonnet 4.5, Haiku 4.5)."
        ),
    )
    features: List[OCRFeature] = Field(
        default_factory=list, description="Textract features to enable"
    )
    max_workers: int = Field(default=20, gt=0, description="Max concurrent workers")
    image: ImageConfig = Field(default_factory=ImageConfig)

    @field_validator("max_workers", mode="before")
    @classmethod
    def parse_max_workers(cls, v: Any, info: ValidationInfo) -> int:
        """Parse max_workers from string or number (None -> field default)."""
        v = _default_if_none(v, info, cls)
        if isinstance(v, str):
            return int(v) if v else 20
        return int(v)


class ErrorAnalyzerParameters(BaseModel):
    """Error analyzer parameters configuration"""

    max_log_events: int = Field(
        default=10,
        gt=0,
        description=(
            "Log events returned per log group by the CloudWatch log searches when "
            "the question names no count. A value above 50, the maximum the tools "
            "advertise, is treated as 50."
        ),
    )
    time_range_hours_default: int = Field(
        default=24,
        gt=0,
        description=(
            "Unused alias, kept so saved configurations that carry it still "
            "validate. Not read by the agent or its tools. The default log-search "
            "window is agents.error_analyzer.lookback_hours."
        ),
    )

    max_log_message_length: int = Field(
        default=400,
        gt=0,
        description="Maximum length for log messages before truncation",
    )
    max_events_per_log_group: int = Field(
        default=5, gt=0, description="Maximum events to collect per log group"
    )
    max_log_groups: int = Field(
        default=20, gt=0, description="Maximum number of log groups to search"
    )
    max_stepfunction_timeline_events: int = Field(
        default=50, gt=0, description="Maximum Step Function timeline events to include"
    )
    max_stepfunction_error_length: int = Field(
        default=400, gt=0, description="Maximum length for Step Function error messages"
    )
    max_stepfunction_history_pages: int = Field(
        default=10,
        gt=0,
        le=50,
        description=(
            "Maximum pages of Step Function execution history to walk back through "
            "when looking for the transition into the failing state. Each page is 100 "
            "events. Bounded above because the walk is one API call per page and an "
            "unbounded value would make a misconfiguration expensive rather than slow."
        ),
    )

    # X-Ray analysis thresholds
    xray_slow_segment_threshold_ms: int = Field(
        default=5000,
        gt=0,
        description="Threshold for slow segment detection in milliseconds",
    )
    xray_error_rate_threshold: float = Field(
        default=0.05, ge=0.0, le=1.0, description="Error rate threshold (0.05 = 5%)"
    )
    xray_response_time_threshold_ms: int = Field(
        default=10000, gt=0, description="Response time threshold in milliseconds"
    )
    xray_analysis_hours: int = Field(
        default=3,
        gt=0,
        le=6,
        description="Hours to look back for X-Ray service graph analysis (max 6)",
    )
    settings_cache_ttl_seconds: int = Field(
        default=300,
        gt=0,
        description="TTL in seconds for the SSM settings cache",
    )

    @field_validator(
        "max_log_events",
        "time_range_hours_default",
        "max_log_message_length",
        "max_events_per_log_group",
        "max_log_groups",
        "max_stepfunction_timeline_events",
        "max_stepfunction_error_length",
        "max_stepfunction_history_pages",
        "xray_slow_segment_threshold_ms",
        "xray_response_time_threshold_ms",
        "xray_analysis_hours",
        "settings_cache_ttl_seconds",
        mode="before",
    )
    @classmethod
    def parse_int(cls, v: Any, info: ValidationInfo) -> int:
        """Parse int from string or number (empty/None -> field default)."""
        return _parse_required_int(v, info, cls)


class ErrorAnalyzerConfig(BaseModel):
    """Error analyzer agent configuration"""

    model_id: str = Field(
        default="us.anthropic.claude-sonnet-4-6",
        description="Bedrock model ID for error analyzer",
    )
    lookback_hours: int = Field(
        default=24,
        gt=0,
        description=(
            "Default window, in hours, for the error analyzer's CloudWatch log "
            "searches when the question names none. Default: 24."
        ),
    )

    @field_validator("lookback_hours", mode="before")
    @classmethod
    def parse_lookback_hours(cls, v: Any, info: ValidationInfo) -> int:
        """Parse lookback_hours from string or number (empty/None -> field default)."""
        return _parse_required_int(v, info, cls)

    error_patterns: list[str] = Field(
        default=[
            "ERROR",
            "CRITICAL",
            "FATAL",
            "Exception",
            "Traceback",
            "Failed",
            "Timeout",
            "AccessDenied",
            "ThrottlingException",
        ],
        description="Error patterns to search for in logs",
    )
    system_prompt: str = Field(
        default="""You are an intelligent error analysis agent for the GenAI IDP (Intelligent Document Processing) system with access to specialized diagnostic tools.

SYSTEM ARCHITECTURE:
The GenAI IDP system processes documents through an AWS Step Functions state machine with the following pipeline stages:
- OCR Stage: Extracts text/layout from documents using Amazon Textract or Amazon Bedrock Data Automation (BDA)
- Classification Stage: Identifies the document class using a Bedrock LLM
- Extraction Stage: Extracts structured fields using a Bedrock LLM based on class-specific configuration
- Assessment Stage: Evaluates extraction quality using a Bedrock LLM
- Summarization Stage (optional): Generates a document summary
- Evaluation Stage (optional): Scores extraction accuracy against ground truth

BDA Alternative Branch:
- InvokeBDA → BDA Completion (EventBridge-triggered) → BDA ProcessResults
- BDA jobs are asynchronous; failures may appear in EventBridge delivery or the BDA service itself

Key AWS services involved:
- AWS Step Functions: Orchestrates the pipeline workflow
- AWS Lambda: Executes each stage as an independent function
- Amazon DynamoDB: Tracks document status and metadata per stage
- Amazon CloudWatch: Captures logs from each Lambda function
- AWS X-Ray: Provides distributed tracing across Lambda and Bedrock calls
- Amazon Bedrock: Provides LLM inference for classification, extraction, assessment, and summarization
- Amazon Textract: Performs OCR for non-BDA documents
- Amazon S3: Stores input documents, OCR results, and extracted output

INVESTIGATION WORKFLOW:
1. Identify the document status in DynamoDB to understand which pipeline stage failed
2. Retrieve Step Functions execution details to get the execution timeline and error event
3. Collect CloudWatch logs from the failing Lambda stage for detailed error messages
4. Use X-Ray traces to identify performance bottlenecks or cascading failures across services
5. Synthesize all evidence to determine root cause — never stop at the first error message

TOOL USAGE:
The tools below are the only ones you have. Call them by these exact names.
- Document-specific analysis (user provides a filename or document ID):
  → Use fetch_document_record (object_key) for the document's status, timestamps, ConfigVersion and Step Functions execution ARN
  → Use retrieve_document_context (document_id) for the processing timeline, the failed Lambda function names and their request IDs
  → Use search_cloudwatch_logs with document_id set to search only the logs of that document's processing window and request IDs
- System-wide or batch analysis (no specific document):
  → Use fetch_recent_records (date, hours_back, limit) for recent documents and their completed/failed/in-progress counts
  → Use search_cloudwatch_logs without document_id (filter_pattern, hours_back) for recent errors across the stack
  → Use search_performance_issues (issue_type "performance", "throttling", "timeout" or "capacity") for throttling, timeout and capacity problems
- Workflow failures and execution timeline:
  → Use analyze_workflow_execution (document_id) for the Step Functions state transitions and failure point
- Which Bedrock model a pipeline stage uses (e.g. after a model error):
  → Use fetch_pipeline_configuration with the document's config version to read the per-stage model IDs
- Distributed service interaction issues:
  → Use analyze_document_trace (document_id) for one document's X-Ray trace
  → Use analyze_system_performance (stack_name, hours_back) for stack-wide X-Ray performance

Always use at least 2 different tool sources before concluding a root cause. If a tool call returns no useful data, try an alternative — never guess without evidence.

CRITICAL — DRILL INTO THE FAILING STAGE'S OWN LOGS:
The Step Functions error and the DynamoDB status_reason usually show a
GENERIC WRAPPER message (e.g. "Summarization failed for document X.pdf",
"Extraction failed"), because each stage Lambda catches the underlying
exception and re-raises a stage-level summary. That wrapper is a SYMPTOM,
not the root cause. You MUST fetch the CloudWatch logs of the specific
Lambda function for the failing stage and read the FIRST underlying
exception/traceback it logged (e.g. a Bedrock ResourceNotFoundException,
ThrottlingException, ValidationException, or a Python traceback). Search
that stage's log group by the failing Lambda's request ID (from the
Step Functions error cause or the X-Ray trace) with an "ERROR" filter, and
quote the earliest real error — not the last wrapper line. If the wrapper
message names a stage, the log group you need is that stage's function
(e.g. SummarizationFunction, ExtractionFunction, AssessmentFunction).

DO NOT CONFLATE UNRELATED NON-FATAL ISSUES WITH THE FAILURE:
A document's DynamoDB record may carry `ProcessingIssue` entries with
severity "warning" or non-terminal notes (e.g. an `assessment_incomplete`
row that self-healed or was recorded but did not stop the pipeline). These
are NOT necessarily the cause of a FAILED workflow. Before attributing the
failure to a ProcessingIssue, confirm it occurred in the SAME stage that
Step Functions reports as the failure point AND that its severity is
"error". If the failing stage differs from the stage that logged the
ProcessingIssue, treat the ProcessingIssue as context, not root cause, and
keep drilling into the failing stage's logs.

INVESTIGATION STRATEGY:
Use this approach for all investigations, whether a single document or a large batch:

1. TRIAGE: Check DynamoDB for document status and which stage failed. For batches, get a count of failed documents and their error status distribution.

2. SAMPLE: For multiple failures, select 2-3 representative failed documents. Avoid over-sampling — additional documents yield diminishing returns.

3. TRACE THE CAUSAL CHAIN for each sampled document:
   DynamoDB status → Step Functions execution timeline → CloudWatch error logs → X-Ray traces

4. APPLY THE "5 WHYS" — Never stop at the first error. Keep asking "what caused THIS?":
   Finding: "Extraction Lambda timed out" → Why?
   "Lambda waited 14 minutes on Bedrock InvokeModel" → Why was it slow?
   "Bedrock returned ThrottlingException, triggering exponential backoff" → Why throttled?
   "Batch of 200 docs with extraction concurrency=10 exceeded Bedrock RPM quota"
   ROOT CAUSE: "Extraction concurrency too high for the configured Bedrock account quota"

5. DISTINGUISH SYSTEMIC vs ISOLATED FAILURES:
   - Same error type across many documents → systemic issue (quota, permissions, configuration, service limit)
   - Different errors across documents → per-document issues (bad input, edge cases, unsupported format)

6. VALIDATE: Does the identified root cause explain ALL observed failures?

ROOT CAUSE vs SYMPTOM GUIDE:
- SYMPTOM: "Document processing failed"
- SYMPTOM: "Extraction Lambda returned error"
- CLOSER:  "ThrottlingException from Bedrock InvokeModel"
- ROOT CAUSE: "Bedrock RPM quota exceeded — batch concurrency generated too many concurrent API calls"

- SYMPTOM: "Classification failed"
- CLOSER:  "Textract API timeout"
- ROOT CAUSE: "150-page PDF exceeded Textract async processing limit for the configured region"

COMMON ERROR PATTERNS:
Use these patterns to guide your investigation and accelerate diagnosis:

1. THROTTLING — ThrottlingException, TooManyRequestsException, "Rate exceeded", "Too many requests"
   Likely cause: Batch size × concurrency > Bedrock RPM/TPM quota, or Textract TPS limit exceeded
   Check: Concurrent Lambda executions, batch size, Bedrock model quotas

2. TIMEOUT — "Task timed out", "Lambda timeout", "socket timeout", "Connection reset"
   Likely cause: Large document (many pages), undersized Lambda timeout or memory, slow Bedrock inference
   Check: Document page count, Lambda timeout configuration, model response latency in X-Ray

3. CONFIGURATION ERROR — KeyError, missing field, "not found in config", validation error, AttributeError
   Likely cause: Class definition or attribute names in config don't match expected schema; config changes deployed incorrectly
   Check: DynamoDB config table, class definitions, attribute names for the affected document class

4. PERMISSIONS — AccessDeniedException, "not authorized", "is not authorized to perform", ExpiredToken
   Likely cause: Missing IAM policy, cross-account access issue, Bedrock model access not granted, KMS policy gap
   Check: Lambda execution role policies, Bedrock model access in the console, S3 bucket policies

5. INPUT QUALITY — empty extraction results, very low confidence, "unable to parse", Textract errors on specific pages
   Likely cause: Poor scan quality, handwritten content, unsupported file format, corrupted PDF
   Check: OCR output in S3, original document quality, Textract response for page-level errors

6. BDA-SPECIFIC — "BDA Job Failed", blueprint mismatch, async job timeout, missing EventBridge event
   Likely cause: Blueprint schema mismatch with document type, BDA service limit, EventBridge delivery failure
   Check: BDA project configuration, blueprint compatibility, EventBridge rule and DLQ

7. BEDROCK MODEL ERRORS — ModelErrorException, "model returned an error", context length exceeded
   Likely cause: Document content too large for model context window, model unavailable in region, prompt issue
   Check: Document page count, OCR text length, model availability, extraction prompt configuration

8. RETIRED / UNAVAILABLE MODEL — ResourceNotFoundException, "This model version has reached the end of its life", "model identifier is invalid", "could not be found"
   Likely cause: The model ID configured for a stage (OCR/classification/extraction/assessment/summarization) has been retired (end-of-life) by Bedrock, or is not enabled/available in this account/region
   Check: The Bedrock error does NOT name the model. To identify it, call fetch_pipeline_configuration with the document's config version (the ConfigVersion field from fetch_document_record) and read the model configured for the FAILING stage (match the stage to the failing Lambda — e.g. the "summarization" stage for SummarizationFunction). Name that exact model ID as the root cause and recommend switching that stage to a currently-supported model in the UI Configuration panel — do NOT just tell the user to "confirm the configured model".

OUTPUT FORMAT:
Always format your response with exactly these three sections in this order:

## Root Cause
**Confidence:** [HIGH | MEDIUM | LOW]
Identify the specific underlying technical reason why the error occurred. Focus on the primary cause, not symptoms.

## Recommendations
Provide specific, actionable steps to resolve the issue. Limit to top three recommendations only.

<details>
<summary><strong>Evidence</strong></summary>

Format evidence with source information. Include relevant data from tool responses:

**For CloudWatch logs:**
**Log Group:** [full log_group name]
**Log Stream:** [full log_stream name]
```
[ERROR] timestamp message
```

**For other sources (DynamoDB, Step Functions, X-Ray):**
**Source:** [service name and resource]
```
Relevant data from tool response
```

</details>

FORMATTING RULES:
- Use the exact three-section structure above
- Add Confidence (HIGH/MEDIUM/LOW) as the first line of the Root Cause section
- Make the Evidence section collapsible using HTML details tags
- Include relevant data from all tool responses used
- For CloudWatch: Show complete log group and log stream names without truncation
- Present evidence data in code blocks with appropriate source labels

RECOMMENDATION GUIDELINES:
For code-related issues or system bugs:
- Do not suggest code modifications — users cannot change Lambda code
- Describe the error in detail with timestamps and context so it can be reported

For configuration-related issues:
- Direct users to the UI configuration panel
- Specify the exact configuration section and parameter name

For operational issues (throttling, timeouts, quotas):
- Provide immediate remediation steps (e.g., reduce concurrency, reprocess failed documents)
- Include preventive measures to avoid recurrence

COMMON MISTAKES TO AVOID:
- Do NOT report "Lambda function returned error" as a root cause — that is a symptom
- Do NOT recommend "check CloudWatch logs" as a recommendation — you are already doing that
- Do NOT suggest code changes — users cannot modify Lambda functions
- Do NOT speculate about root cause without corroborating tool evidence
- Do NOT investigate more than 3 sample documents in a batch — focus on pattern recognition
- Do NOT include search quality reflections, meta-analysis, or sections not listed in the output format above""",
        description="System prompt for error analyzer",
    )
    parameters: ErrorAnalyzerParameters = Field(
        default_factory=ErrorAnalyzerParameters, description="Error analyzer parameters"
    )


class ChatCompanionConfig(BaseModel):
    """Chat companion agent configuration"""

    model_id: str = Field(
        default="global.anthropic.claude-sonnet-4-6",
        description="Bedrock model ID for chat companion",
    )

    error_patterns: list[str] = [
        "ERROR",
        "CRITICAL",
        "FATAL",
        "Exception",
        "Traceback",
        "Failed",
        "Timeout",
        "AccessDenied",
        "ThrottlingException",
    ]
    system_prompt: str = Field(
        default="""You are an intelligent error analysis agent for the GenAI IDP (Intelligent Document Processing) system with access to specialized diagnostic tools.

SYSTEM ARCHITECTURE:
The GenAI IDP system processes documents through an AWS Step Functions state machine with the following pipeline stages:
- OCR Stage: Extracts text/layout from documents using Amazon Textract or Amazon Bedrock Data Automation (BDA)
- Classification Stage: Identifies the document class using a Bedrock LLM
- Extraction Stage: Extracts structured fields using a Bedrock LLM based on class-specific configuration
- Assessment Stage: Evaluates extraction quality using a Bedrock LLM
- Summarization Stage (optional): Generates a document summary
- Evaluation Stage (optional): Scores extraction accuracy against ground truth

BDA Alternative Branch:
- InvokeBDA → BDA Completion (EventBridge-triggered) → BDA ProcessResults
- BDA jobs are asynchronous; failures may appear in EventBridge delivery or the BDA service itself

Key AWS services involved:
- AWS Step Functions: Orchestrates the pipeline workflow
- AWS Lambda: Executes each stage as an independent function
- Amazon DynamoDB: Tracks document status and metadata per stage
- Amazon CloudWatch: Captures logs from each Lambda function
- AWS X-Ray: Provides distributed tracing across Lambda and Bedrock calls
- Amazon Bedrock: Provides LLM inference for classification, extraction, assessment, and summarization
- Amazon Textract: Performs OCR for non-BDA documents
- Amazon S3: Stores input documents, OCR results, and extracted output

INVESTIGATION WORKFLOW:
1. Identify the document status in DynamoDB to understand which pipeline stage failed
2. Retrieve Step Functions execution details to get the execution timeline and error event
3. Collect CloudWatch logs from the failing Lambda stage for detailed error messages
4. Use X-Ray traces to identify performance bottlenecks or cascading failures across services
5. Synthesize all evidence to determine root cause — never stop at the first error message

TOOL USAGE:
The tools below are the only ones you have. Call them by these exact names.
- Document-specific analysis (user provides a filename or document ID):
  → Use fetch_document_record (object_key) for the document's status, timestamps, ConfigVersion and Step Functions execution ARN
  → Use retrieve_document_context (document_id) for the processing timeline, the failed Lambda function names and their request IDs
  → Use search_cloudwatch_logs with document_id set to search only the logs of that document's processing window and request IDs
- System-wide or batch analysis (no specific document):
  → Use fetch_recent_records (date, hours_back, limit) for recent documents and their completed/failed/in-progress counts
  → Use search_cloudwatch_logs without document_id (filter_pattern, hours_back) for recent errors across the stack
  → Use search_performance_issues (issue_type "performance", "throttling", "timeout" or "capacity") for throttling, timeout and capacity problems
- Workflow failures and execution timeline:
  → Use analyze_workflow_execution (document_id) for the Step Functions state transitions and failure point
- Which Bedrock model a pipeline stage uses (e.g. after a model error):
  → Use fetch_pipeline_configuration with the document's config version to read the per-stage model IDs
- Distributed service interaction issues:
  → Use analyze_document_trace (document_id) for one document's X-Ray trace
  → Use analyze_system_performance (stack_name, hours_back) for stack-wide X-Ray performance

Always use at least 2 different tool sources before concluding a root cause. If a tool call returns no useful data, try an alternative — never guess without evidence.

CRITICAL — DRILL INTO THE FAILING STAGE'S OWN LOGS:
The Step Functions error and the DynamoDB status_reason usually show a
GENERIC WRAPPER message (e.g. "Summarization failed for document X.pdf",
"Extraction failed"), because each stage Lambda catches the underlying
exception and re-raises a stage-level summary. That wrapper is a SYMPTOM,
not the root cause. You MUST fetch the CloudWatch logs of the specific
Lambda function for the failing stage and read the FIRST underlying
exception/traceback it logged (e.g. a Bedrock ResourceNotFoundException,
ThrottlingException, ValidationException, or a Python traceback). Search
that stage's log group by the failing Lambda's request ID (from the
Step Functions error cause or the X-Ray trace) with an "ERROR" filter, and
quote the earliest real error — not the last wrapper line. If the wrapper
message names a stage, the log group you need is that stage's function
(e.g. SummarizationFunction, ExtractionFunction, AssessmentFunction).

DO NOT CONFLATE UNRELATED NON-FATAL ISSUES WITH THE FAILURE:
A document's DynamoDB record may carry `ProcessingIssue` entries with
severity "warning" or non-terminal notes (e.g. an `assessment_incomplete`
row that self-healed or was recorded but did not stop the pipeline). These
are NOT necessarily the cause of a FAILED workflow. Before attributing the
failure to a ProcessingIssue, confirm it occurred in the SAME stage that
Step Functions reports as the failure point AND that its severity is
"error". If the failing stage differs from the stage that logged the
ProcessingIssue, treat the ProcessingIssue as context, not root cause, and
keep drilling into the failing stage's logs.

INVESTIGATION STRATEGY:
Use this approach for all investigations, whether a single document or a large batch:

1. TRIAGE: Check DynamoDB for document status and which stage failed. For batches, get a count of failed documents and their error status distribution.

2. SAMPLE: For multiple failures, select 2-3 representative failed documents. Avoid over-sampling — additional documents yield diminishing returns.

3. TRACE THE CAUSAL CHAIN for each sampled document:
   DynamoDB status → Step Functions execution timeline → CloudWatch error logs → X-Ray traces

4. APPLY THE "5 WHYS" — Never stop at the first error. Keep asking "what caused THIS?":
   Finding: "Extraction Lambda timed out" → Why?
   "Lambda waited 14 minutes on Bedrock InvokeModel" → Why was it slow?
   "Bedrock returned ThrottlingException, triggering exponential backoff" → Why throttled?
   "Batch of 200 docs with extraction concurrency=10 exceeded Bedrock RPM quota"
   ROOT CAUSE: "Extraction concurrency too high for the configured Bedrock account quota"

5. DISTINGUISH SYSTEMIC vs ISOLATED FAILURES:
   - Same error type across many documents → systemic issue (quota, permissions, configuration, service limit)
   - Different errors across documents → per-document issues (bad input, edge cases, unsupported format)

6. VALIDATE: Does the identified root cause explain ALL observed failures?

ROOT CAUSE vs SYMPTOM GUIDE:
- SYMPTOM: "Document processing failed"
- SYMPTOM: "Extraction Lambda returned error"
- CLOSER:  "ThrottlingException from Bedrock InvokeModel"
- ROOT CAUSE: "Bedrock RPM quota exceeded — batch concurrency generated too many concurrent API calls"

- SYMPTOM: "Classification failed"
- CLOSER:  "Textract API timeout"
- ROOT CAUSE: "150-page PDF exceeded Textract async processing limit for the configured region"

COMMON ERROR PATTERNS:
Use these patterns to guide your investigation and accelerate diagnosis:

1. THROTTLING — ThrottlingException, TooManyRequestsException, "Rate exceeded", "Too many requests"
   Likely cause: Batch size × concurrency > Bedrock RPM/TPM quota, or Textract TPS limit exceeded
   Check: Concurrent Lambda executions, batch size, Bedrock model quotas

2. TIMEOUT — "Task timed out", "Lambda timeout", "socket timeout", "Connection reset"
   Likely cause: Large document (many pages), undersized Lambda timeout or memory, slow Bedrock inference
   Check: Document page count, Lambda timeout configuration, model response latency in X-Ray

3. CONFIGURATION ERROR — KeyError, missing field, "not found in config", validation error, AttributeError
   Likely cause: Class definition or attribute names in config don't match expected schema; config changes deployed incorrectly
   Check: DynamoDB config table, class definitions, attribute names for the affected document class

4. PERMISSIONS — AccessDeniedException, "not authorized", "is not authorized to perform", ExpiredToken
   Likely cause: Missing IAM policy, cross-account access issue, Bedrock model access not granted, KMS policy gap
   Check: Lambda execution role policies, Bedrock model access in the console, S3 bucket policies

5. INPUT QUALITY — empty extraction results, very low confidence, "unable to parse", Textract errors on specific pages
   Likely cause: Poor scan quality, handwritten content, unsupported file format, corrupted PDF
   Check: OCR output in S3, original document quality, Textract response for page-level errors

6. BDA-SPECIFIC — "BDA Job Failed", blueprint mismatch, async job timeout, missing EventBridge event
   Likely cause: Blueprint schema mismatch with document type, BDA service limit, EventBridge delivery failure
   Check: BDA project configuration, blueprint compatibility, EventBridge rule and DLQ

7. BEDROCK MODEL ERRORS — ModelErrorException, "model returned an error", context length exceeded
   Likely cause: Document content too large for model context window, model unavailable in region, prompt issue
   Check: Document page count, OCR text length, model availability, extraction prompt configuration

8. RETIRED / UNAVAILABLE MODEL — ResourceNotFoundException, "This model version has reached the end of its life", "model identifier is invalid", "could not be found"
   Likely cause: The model ID configured for a stage (OCR/classification/extraction/assessment/summarization) has been retired (end-of-life) by Bedrock, or is not enabled/available in this account/region
   Check: The Bedrock error does NOT name the model. To identify it, call fetch_pipeline_configuration with the document's config version (the ConfigVersion field from fetch_document_record) and read the model configured for the FAILING stage (match the stage to the failing Lambda — e.g. the "summarization" stage for SummarizationFunction). Name that exact model ID as the root cause and recommend switching that stage to a currently-supported model in the UI Configuration panel — do NOT just tell the user to "confirm the configured model".

OUTPUT FORMAT:
Always format your response with exactly these three sections in this order:

## Root Cause
**Confidence:** [HIGH | MEDIUM | LOW]
Identify the specific underlying technical reason why the error occurred. Focus on the primary cause, not symptoms.

## Recommendations
Provide specific, actionable steps to resolve the issue. Limit to top three recommendations only.

<details>
<summary><strong>Evidence</strong></summary>

Format evidence with source information. Include relevant data from tool responses:

**For CloudWatch logs:**
**Log Group:** [full log_group name]
**Log Stream:** [full log_stream name]
```
[ERROR] timestamp message
```

**For other sources (DynamoDB, Step Functions, X-Ray):**
**Source:** [service name and resource]
```
Relevant data from tool response
```

</details>

FORMATTING RULES:
- Use the exact three-section structure above
- Add Confidence (HIGH/MEDIUM/LOW) as the first line of the Root Cause section
- Make the Evidence section collapsible using HTML details tags
- Include relevant data from all tool responses used
- For CloudWatch: Show complete log group and log stream names without truncation
- Present evidence data in code blocks with appropriate source labels

RECOMMENDATION GUIDELINES:
For code-related issues or system bugs:
- Do not suggest code modifications — users cannot change Lambda code
- Describe the error in detail with timestamps and context so it can be reported

For configuration-related issues:
- Direct users to the UI configuration panel
- Specify the exact configuration section and parameter name

For operational issues (throttling, timeouts, quotas):
- Provide immediate remediation steps (e.g., reduce concurrency, reprocess failed documents)
- Include preventive measures to avoid recurrence

COMMON MISTAKES TO AVOID:
- Do NOT report "Lambda function returned error" as a root cause — that is a symptom
- Do NOT recommend "check CloudWatch logs" as a recommendation — you are already doing that
- Do NOT suggest code changes — users cannot modify Lambda functions
- Do NOT speculate about root cause without corroborating tool evidence
- Do NOT investigate more than 3 sample documents in a batch — focus on pattern recognition
- Do NOT include search quality reflections, meta-analysis, or sections not listed in the output format above""",
        description="System prompt for error analyzer",
    )
    parameters: ErrorAnalyzerParameters = Field(
        default_factory=ErrorAnalyzerParameters, description="Error analyzer parameters"
    )


class AgentsConfig(BaseModel):
    """Agents configuration"""

    error_analyzer: Optional[ErrorAnalyzerConfig] = Field(
        default_factory=ErrorAnalyzerConfig, description="Error analyzer configuration"
    )
    chat_companion: Optional[ChatCompanionConfig] = Field(
        default_factory=ChatCompanionConfig, description="Chat companion configuration"
    )


class PricingUnit(BaseModel):
    """Individual pricing unit within a service/API"""

    name: str = Field(
        description="Unit name (e.g., 'pages', 'inputTokens', 'outputTokens')"
    )
    price: str = Field(
        description="Price as string (supports scientific notation like '6.0E-8')"
    )

    @field_validator("price", mode="before")
    @classmethod
    def parse_price(cls, v: Any) -> str:
        """Ensure price is stored as string"""
        if v is None:
            return "0.0"
        return str(v)


class PricingEntry(BaseModel):
    """Single pricing entry with service/API name and associated units"""

    name: str = Field(
        description="Service/API identifier (e.g., 'textract/detect_document_text', 'bedrock/us.amazon.nova-lite-v1:0')"
    )
    units: List[PricingUnit] = Field(
        description="List of pricing units for this service/API"
    )


class PricingConfig(BaseModel):
    """
    Pricing configuration model.

    This represents the Pricing configuration type stored in DynamoDB.
    It contains a list of pricing entries, each with:
    - name: Service/API identifier (format: service/api-name)
    - units: List of pricing units with name and price

    Structure matches the config.yaml pricing format from the original IDP config:
    pricing:
      - name: textract/detect_document_text
        units:
          - name: pages
            price: "0.0015"
      - name: bedrock/us.amazon.nova-lite-v1:0
        units:
          - name: inputTokens
            price: "6.0E-8"
          - name: outputTokens
            price: "2.4E-7"

    Uses DefaultPricing/CustomPricing pattern that mirrors Default/Custom for IDPConfig.
    """

    config_type: Literal["DefaultPricing", "CustomPricing"] = Field(
        default="DefaultPricing", description="Discriminator for config type"
    )

    pricing: List[PricingEntry] = Field(
        default_factory=list,
        description="List of pricing entries with service/API name and units",
    )

    model_config = ConfigDict(
        extra="forbid",  # Strict validation - only 'pricing' field allowed
        validate_assignment=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _report_ignored_keys(cls, data: Any) -> Any:
        """Name every key below the root that this record will drop.

        ``extra="forbid"`` above covers the root and nothing under it:
        ``PricingEntry`` and ``PricingUnit`` take Pydantic's default
        ``extra="ignore"``, so ``pricing[0].units[0].priec`` was discarded with no
        diagnostic and the price the operator wrote was never recorded. Reports
        only — nothing that loads today stops loading.
        """
        log_ignored_config_keys(data, cls)
        return data

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a mutable dictionary."""
        return self.model_dump(mode="python")


class ModelLimitEntry(BaseModel):
    """Single model-limit entry.

    Entries form an ORDERED list matched by case-insensitive regex against the
    model ID — first match wins, so list order is semantically meaningful.
    """

    pattern: str = Field(
        description="Case-insensitive regex matched against the model ID (order matters; first match wins)"
    )
    max_output_tokens: int = Field(
        gt=0, description="Maximum output tokens for matching models"
    )
    max_input_tokens: Optional[int] = Field(
        default=None, gt=0, description="Model input/context window in tokens"
    )
    description: Optional[str] = Field(
        default=None, description="Human-readable description of the model family"
    )
    reference: Optional[str] = Field(
        default=None, description="Documentation URL for the limit values"
    )

    @field_validator("pattern")
    @classmethod
    def _pattern_must_compile(cls, v: str) -> str:
        """Reject a pattern that isn't a valid regex.

        Patterns are user-editable (via the Model Limits UI) and are later
        passed to ``re.search`` on the Bedrock hot path. Validating at save
        time surfaces a clear error instead of letting a bad pattern raise
        ``re.error`` deep inside model-limit resolution.
        """
        if not v or not v.strip():
            raise ValueError("pattern must be a non-empty string")
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"pattern is not a valid regular expression: {e}") from e
        return v


class ModelConfigLimitsConfig(BaseModel):
    """
    Model config limits configuration model.

    Represents the DefaultModelConfigLimits / CustomModelConfigLimits config
    types stored in DynamoDB (mirroring the DefaultPricing/CustomPricing
    pattern). Seeded from config_library/model_config_limits.yaml at deploy.

    Unlike pricing, CustomModelConfigLimits stores the FULL replacement list,
    not deltas: model_limits is an ordered first-match-wins list, so a partial
    merge cannot preserve ordering intent.
    """

    config_type: Literal["DefaultModelConfigLimits", "CustomModelConfigLimits"] = Field(
        default="DefaultModelConfigLimits", description="Discriminator for config type"
    )

    model_limits: List[ModelLimitEntry] = Field(
        default_factory=list,
        description="Ordered list of model limit entries (first pattern match wins)",
    )

    model_config = ConfigDict(
        extra="forbid",  # Strict validation - only 'model_limits' field allowed
        validate_assignment=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _report_ignored_keys(cls, data: Any) -> Any:
        """Name every key inside a limit entry that this record will drop.

        ``extra="forbid"`` above covers the root and nothing under it:
        ``ModelLimitEntry`` takes Pydantic's default ``extra="ignore"``, so
        ``model_limits[0].max_input_tokenz`` was accepted and discarded, leaving
        the shipped context window for that model family in force while the UI
        showed the save as successful. ``max_input_tokens`` is the window the
        Bedrock client resolves a model against, so the effect is a limit that
        appears to have been set and was not. Reports only — nothing that loads
        today stops loading.
        """
        log_ignored_config_keys(data, cls)
        return data

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a mutable dictionary."""
        return self.model_dump(mode="python")


class FactExtractionConfig(BaseModel):
    """Fact extraction configuration for rule validation"""

    # No config preset under config_library/ sets this field, so this default is
    # what rule validation actually runs on — which makes it worth keeping on a
    # model AWS has not deprecated. Claude Sonnet 4.5 moved to LEGACY on
    # 2026-10-08 (EOL 2027-04-08, recorded in LEGACY_EXAMPLES in
    # scripts/tests/test_model_surface_consistency.py), and a LEGACY model takes
    # no further Service Quota increases, so it is a poor thing to inherit by
    # default. Sonnet 4.6 is Active and is the Sonnet-class default elsewhere in
    # system_defaults/.
    model: str = Field(
        default="us.anthropic.claude-sonnet-4-6",
        description="Bedrock model ID for fact extraction",
    )
    system_prompt: str = Field(
        default="", description="System prompt for fact extraction"
    )
    task_prompt: str = Field(default="", description="Task prompt for fact extraction")
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    top_p: float = Field(default=0.01, ge=0.0, le=1.0)
    top_k: float = Field(default=20.0, ge=0.0)
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        description="Optional cap on output tokens. Leave empty to use the selected model's maximum output limit (recommended). If set, it must not exceed the model's limit.",
    )

    @field_validator("temperature", "top_p", "top_k", mode="before")
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        """Parse float from string or number (None -> field default)."""
        return _parse_required_float(v, info, cls)

    @field_validator("max_tokens", mode="before")
    @classmethod
    def parse_int(cls, v: Any) -> Optional[int]:
        """Parse optional max_tokens (empty/0 -> None = use model max)."""
        return _parse_optional_max_tokens(v)


class RuleValidationOrchestratorConfig(BaseModel):
    """Rule validation summarization configuration"""

    # See FactExtractionConfig.model above for why this default tracks the
    # Active Sonnet rather than a LEGACY one.
    model: str = Field(
        default="us.anthropic.claude-sonnet-4-6",
        description="Bedrock model ID for rule validation summarization",
    )
    system_prompt: str = Field(
        default="", description="System prompt for summarization"
    )
    task_prompt: str = Field(default="", description="Task prompt for summarization")
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    top_p: float = Field(default=0.01, ge=0.0, le=1.0)
    top_k: float = Field(default=20.0, ge=0.0)
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        description="Optional cap on output tokens. Leave empty to use the selected model's maximum output limit (recommended). If set, it must not exceed the model's limit.",
    )

    @field_validator("temperature", "top_p", "top_k", mode="before")
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        """Parse float from string or number (None -> field default)."""
        return _parse_required_float(v, info, cls)

    @field_validator("max_tokens", mode="before")
    @classmethod
    def parse_int(cls, v: Any) -> Optional[int]:
        """Parse optional max_tokens (empty/0 -> None = use model max)."""
        return _parse_optional_max_tokens(v)


class Z3RuleTranslatorConfig(BaseModel):
    """Z3 engine: translates natural language rules to SMT-LIB RuleJSON"""

    model: str = Field(
        default="us.anthropic.claude-sonnet-4-6",
        max_length=256,
        description="Bedrock model ID for rule translation",
    )
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=4096, gt=0)
    system_prompt: str = Field(
        default="", description="System prompt for rule translation"
    )
    task_prompt: str = Field(
        default="", description="Task prompt template for rule translation"
    )
    few_shot_examples: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Few-shot examples for rule translation [{rule, data_example, output}]",
    )


class Z3ValueExtractionConfig(BaseModel):
    """Z3 engine: extracts parameter values from data"""

    model: str = Field(
        default="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        max_length=256,
        description="Bedrock model ID for value extraction",
    )
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=2048, gt=0)
    system_prompt: str = Field(
        default="", description="System prompt for value extraction"
    )
    task_prompt: str = Field(
        default="", description="Task prompt template for value extraction"
    )


class RuleValidationConfig(BaseModel):
    """Rule validation configuration"""

    enabled: bool = Field(default=True, description="Enable rule validation")
    semaphore: int = Field(
        default=5, gt=0, description="Number of concurrent API calls"
    )
    max_chunk_size: int = Field(
        default=8000, gt=0, description="Maximum tokens per chunk"
    )
    token_size: int = Field(default=4, gt=0, description="Average characters per token")
    overlap_percentage: int = Field(
        default=10, ge=0, le=100, description="Chunk overlap percentage"
    )
    response_prefix: str = Field(
        default="<response>", description="Response prefix marker"
    )
    recommendation_options: Optional[str] = Field(
        default=None, description="Available recommendation options"
    )
    extraction_results: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Extraction results to include in rule validation prompts",
    )
    fact_extraction: Optional[FactExtractionConfig] = Field(
        default=None, description="Configuration for fact extraction step"
    )
    rule_validation_orchestrator: Optional[RuleValidationOrchestratorConfig] = Field(
        default=None, description="Configuration for rule validation summarization"
    )
    postHook: List[PipelineHook] = Field(  # noqa: N815 — matches stored config key
        default_factory=list,
        description="Pipeline hooks invoked after rule validation (Feature Platform)",
    )
    z3_rule_translator: Optional[Z3RuleTranslatorConfig] = Field(
        default=None,
        description="Z3 rule translator config (notebook/standalone use only — the deployed pipeline "
        "uses the packaged translator_config.yaml via handle_generate_rule_json instead).",
    )
    z3_value_extraction: Optional[Z3ValueExtractionConfig] = Field(
        default=None,
        description="Z3 value extraction config (notebook/standalone use only — the deployed pipeline "
        "uses orchestrator._extract_z3_values_from_facts with fact_extraction settings instead).",
    )
    z3_timeout_ms: int = Field(
        default=5000,
        gt=0,
        le=300000,
        description="Z3 solver timeout in milliseconds. Must be between 1 and 300000.",
    )

    @field_validator(
        "semaphore",
        "max_chunk_size",
        "token_size",
        "overlap_percentage",
        "z3_timeout_ms",
        mode="before",
    )
    @classmethod
    def parse_int(cls, v: Any, info: ValidationInfo) -> int:
        """Parse int from string or number (empty/None -> field default)."""
        return _parse_required_int(v, info, cls)


class EvaluationLLMMethodConfig(BaseModel):
    """Evaluation LLM method configuration"""

    top_p: float = Field(default=0.1, ge=0.0, le=1.0)
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        description="Optional cap on output tokens. Leave empty to use the selected model's maximum output limit (recommended). If set, it must not exceed the model's limit.",
    )
    top_k: float = Field(default=5.0, ge=0.0)
    reasoning_effort: str = Field(
        default="medium",
        description=(
            "Reasoning effort for reasoning-capable models. Claude Sonnet 5 / "
            "Sonnet 4.6 / Opus 4.5-4.8 / Fable 5 accept low, medium, high, xhigh, "
            "or max (via output_config.effort); OpenAI GPT-5.x accept minimal, "
            "low, medium, or high (via reasoning.effort); xAI Grok accepts none, "
            "low, medium, high, or xhigh (via reasoning.effort, NOT max); OpenAI "
            "GPT-6 Astra accepts none, low, medium, high, xhigh, or max (via "
            "reasoning.effort, NOT minimal). "
            "Ignored by models "
            "without an effort control (Nova, Sonnet 4.5, Haiku 4.5)."
        ),
    )
    task_prompt: str = Field(
        default="""
        I need to evaluate attribute extraction for a document of class: {DOCUMENT_CLASS}.
        For the attribute named "{ATTRIBUTE_NAME}" described as "{ATTRIBUTE_DESCRIPTION}":
        - Expected value: {EXPECTED_VALUE}
        - Actual value: {ACTUAL_VALUE}

        Do these values match in meaning, taking into account formatting differences, word order, abbreviations, and semantic equivalence?
        Provide your assessment as a JSON with three fields:

            - "match": boolean (true if they match, false if not)

            - "score": number between 0 and 1 representing the confidence/similarity score

            - "reason": brief explanation of your decision


        Respond ONLY with the JSON and nothing else. Here's the exact format:

        {
            "match": true or false,
            "score": 0.0 to 1.0,
            "reason": "Your explanation here"
        }""",
        description="Task prompt for evaluation",
    )

    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    # Claude Haiku 4.5, not Claude 3 Haiku. The latter reached end of life, so
    # LLM-based evaluation ran a dead model out of the box; this is the current
    # Haiku-class model and keeps the same cost/latency intent.
    model: str = Field(
        default="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        description="Bedrock model ID for evaluation",
    )
    system_prompt: str = Field(
        default="ou are an evaluator that helps determine if the predicted and expected values match for document attribute extraction. You will consider the context and meaning rather than just exact string matching.",
        description="System prompt for evaluation",
    )

    @field_validator("temperature", "top_p", "top_k", mode="before")
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        """Parse float from string or number (None -> field default)."""
        return _parse_required_float(v, info, cls)

    @field_validator("max_tokens", mode="before")
    @classmethod
    def parse_int(cls, v: Any) -> Optional[int]:
        """Parse optional max_tokens (empty/0 -> None = use model max)."""
        return _parse_optional_max_tokens(v)


class EvaluationConfig(BaseModel):
    """Evaluation configuration for assessment"""

    enabled: bool = Field(default=True)
    llm_method: EvaluationLLMMethodConfig = Field(
        default_factory=EvaluationLLMMethodConfig,
        description="LLM method configuration for evaluation",
    )


class DiscoveryModelConfig(BaseModel):
    """Discovery model configuration for class extraction"""

    model_id: str = Field(
        default="us.amazon.nova-pro-v1:0", description="Bedrock model ID for discovery"
    )
    system_prompt: str = Field(default="", description="System prompt for discovery")
    temperature: float = Field(default=1.0, ge=0.0, le=1.0)
    top_p: float = Field(default=0.1, ge=0.0, le=1.0)
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        description="Optional cap on output tokens. Leave empty to use the selected model's maximum output limit (recommended). If set, it must not exceed the model's limit.",
    )
    user_prompt: str = Field(
        default="", description="User prompt template for discovery"
    )

    @field_validator("temperature", "top_p", mode="before")
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        """Parse float from string or number (None -> field default)."""
        return _parse_required_float(v, info, cls)

    @field_validator("max_tokens", mode="before")
    @classmethod
    def parse_int(cls, v: Any) -> Optional[int]:
        """Parse optional max_tokens (empty/0 -> None = use model max)."""
        return _parse_optional_max_tokens(v)


class MultiDocumentDiscoveryConfig(BaseModel):
    """Multi-document discovery configuration for batch clustering.

    Settings for discovering document classes from a collection of documents
    using embedding-based clustering and AI analysis.
    """

    embedding_model_id: str = Field(
        default="us.cohere.embed-v4:0",
        description="Bedrock model ID for generating document embeddings",
    )
    analysis_model_id: str = Field(
        default="us.anthropic.claude-sonnet-4-6",
        description="Bedrock model ID for analyzing document clusters",
    )
    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Temperature for cluster analysis model",
    )
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        description="Optional cap on output tokens for cluster analysis. Leave "
        "empty to use the selected model's maximum output limit (recommended). "
        "If set, it must not exceed the model's limit.",
    )
    max_documents: int = Field(
        default=500,
        gt=0,
        description="Maximum documents to process in a single discovery run",
    )
    min_cluster_size: int = Field(
        default=2,
        gt=0,
        description="Minimum documents required to form a cluster",
    )
    num_sample_documents: int = Field(
        default=3,
        gt=0,
        description="Number of sample documents selected per cluster for analysis",
    )
    max_sample_size: int = Field(
        default=5,
        gt=0,
        description="Maximum sample size for cluster analysis",
    )
    max_concurrent_embeddings: int = Field(
        default=5,
        gt=0,
        description="Maximum concurrent embedding API requests",
    )
    max_concurrent_clusters: int = Field(
        default=3,
        gt=0,
        description="Maximum concurrent cluster analysis requests",
    )
    system_prompt: str = Field(
        default="",
        description="System prompt for the cluster analysis agent (leave empty to use built-in Jinja2 template)",
    )

    @field_validator("temperature", mode="before")
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        """Parse float from string or number (None -> field default)."""
        return _parse_required_float(v, info, cls)

    @field_validator(
        "max_documents",
        "min_cluster_size",
        "num_sample_documents",
        "max_sample_size",
        "max_concurrent_embeddings",
        "max_concurrent_clusters",
        mode="before",
    )
    @classmethod
    def parse_int(cls, v: Any, info: ValidationInfo) -> int:
        """Parse int from string or number (empty/None -> field default)."""
        return _parse_required_int(v, info, cls)

    @field_validator("max_tokens", mode="before")
    @classmethod
    def parse_max_tokens(cls, v: Any) -> Optional[int]:
        """Parse optional max_tokens (empty/0 -> None = use model max)."""
        return _parse_optional_max_tokens(v)


class RuleDiscoveryAgenticConfig(BaseModel):
    """Agentic rule discovery configuration"""

    enabled: bool = Field(default=False, description="Enable agentic rule discovery")
    review_agent: bool = Field(
        default=False, description="Enable review agent for rule discovery"
    )
    review_agent_model: str | None = Field(
        default=None,
        description="Model used for reviewing and correcting rule discovery work",
    )


class RuleDiscoveryConfig(BaseModel):
    """Rule discovery configuration for extracting rules from policy documents"""

    model: str = Field(
        default="global.anthropic.claude-sonnet-4-6",
        description="Bedrock model ID for rule discovery",
    )
    system_prompt: str = Field(
        default="", description="System prompt for rule discovery"
    )
    task_prompt: str = Field(
        default="", description="Task prompt template for rule discovery"
    )
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    top_p: float = Field(default=0.0, ge=0.0, le=1.0)
    top_k: float = Field(default=5.0, ge=0.0)
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        description="Optional cap on output tokens. Leave empty to use the selected model's maximum output limit (recommended). If set, it must not exceed the model's limit.",
    )
    agentic: RuleDiscoveryAgenticConfig = Field(
        default_factory=RuleDiscoveryAgenticConfig,
        description="Agentic rule discovery configuration",
    )

    @field_validator("temperature", "top_p", "top_k", mode="before")
    @classmethod
    def parse_float(cls, v: Any, info: ValidationInfo) -> float:
        """Parse float from string or number (None -> field default)."""
        return _parse_required_float(v, info, cls)

    @field_validator("max_tokens", mode="before")
    @classmethod
    def parse_int(cls, v: Any) -> Optional[int]:
        """Parse optional max_tokens (empty/0 -> None = use model max)."""
        return _parse_optional_max_tokens(v)

    @model_validator(mode="after")
    def set_default_review_agent_model(self) -> Self:
        """Set review_agent_model to rule discovery model if not specified."""
        if not self.agentic.review_agent_model:
            self.agentic.review_agent_model = self.model
        return self


class DiscoveryConfig(BaseModel):
    """Discovery configuration"""

    without_ground_truth: DiscoveryModelConfig = Field(
        default_factory=DiscoveryModelConfig,
        description="Configuration for discovery without ground truth",
    )
    with_ground_truth: DiscoveryModelConfig = Field(
        default_factory=DiscoveryModelConfig,
        description="Configuration for discovery with ground truth",
    )
    auto_split: DiscoveryModelConfig = Field(
        default_factory=DiscoveryModelConfig,
        description="Configuration for auto-detecting document section boundaries in multi-page packages",
    )
    multi_document: MultiDocumentDiscoveryConfig = Field(
        default_factory=MultiDocumentDiscoveryConfig,
        description="Configuration for multi-document batch discovery using embedding clustering",
    )
    rules: RuleDiscoveryConfig = Field(
        default_factory=RuleDiscoveryConfig,
        description="Configuration for rules discovery from policy documents",
    )


# Known deprecated fields that should be logged when encountered
# Defined at module level to avoid Pydantic converting to ModelPrivateAttr
IDP_CONFIG_DEPRECATED_FIELDS = {
    "criteria_bucket",
    "criteria_types",
    "request_bucket",
    "request_history_prefix",
    "cost_report_bucket",
    "output_bucket",
    "textract_page_tracker",
    "summary",
    "processing_mode",  # Renamed to use_bda (bool) in Phase 1
    # DynamoDB storage metadata fields (not part of IDPConfig model)
    "BdaProjectArn",
    "BdaSyncStatus",
    "BdaLastSyncedAt",
    "_config_format",
    "_config_storage",
    "rule_classes",  # Renamed to policy_classes in v0.5.9
}


# --------------------------------------------------------------------------- #
# Reporting a key the configuration models will drop, at any depth
# --------------------------------------------------------------------------- #
#
# Of the models **reachable from ``IDPConfig``**, three take ``extra="allow"``,
# none takes ``extra="forbid"`` and the rest take Pydantic's default
# ``extra="ignore"`` — so a key no field matches is discarded during validation. At
# the top level ``IDPConfig`` logs a warning about that; below the top level nothing
# did, so a misspelled or mis-nested key left the shipped default in force with no
# diagnostic anywhere — and a default is indistinguishable from a working setting.
#
# The scope of that sentence is exactly ``IDPConfig``'s tree, and this module holds
# three other root models that are **not** in it. ``PricingConfig`` and
# ``ModelConfigLimitsConfig`` take ``extra="forbid"``, which is a better answer than
# reporting; ``SchemaConfig`` takes ``extra="allow"`` and drops nothing. But
# ``ModelLimitEntry``, nested inside ``ModelConfigLimitsConfig``, takes the default,
# so a mistyped key in a per-model limit row is still dropped in silence — the same
# defect in a different record, tracked as
# https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1211.
# The walk below takes any root model, so covering it is a call site rather than new
# machinery.
#
# The walk reports those keys with their dotted path. It reports; it never rejects.
# ``extra`` is unchanged on every model, so a stored configuration that loads today
# still loads.
#
# Three things bound what it calls unknown. The first two are properties of the
# model tree rather than lists kept in step with it; the third is a limitation:
#
#   * It descends only where a field's annotation names a model. A field typed
#     ``Dict[str, Any]`` or ``List[Dict[str, Any]]`` is a free-form document whose
#     keys are the user's to choose — ``classes``, ``policy_classes``, a hook's
#     ``args``, ``summary`` — so there is nothing there to be unknown, and it is
#     never entered.
#   * It reports nothing on a model whose ``model_config`` says ``extra="allow"``.
#     Such a model *keeps* an undeclared key, so the key is not dropped and there
#     is nothing to warn about. It is still descended into, because its declared
#     model-typed fields are ordinary.
#   * ⚠️ It does not enter a field whose annotation names **more than one** model —
#     a discriminated union — because nothing in the annotation says which member a
#     given value is. Keys in there *are* dropped, so unlike the two cases above
#     this is a gap rather than a decision. No field in the tree is shaped that way
#     today and a test fails if one appears, because the guarantee this walk offers
#     would otherwise narrow silently.


#: Top-level keys ``IDPConfig`` **relocates** rather than drops, and their
#: destination. Read by the rename in ``log_deprecated_fields`` and skipped by the
#: walk, from one definition, because the two answers must agree: a relocated key
#: works, so reporting it as "no longer used" sends the author to delete a setting
#: that is in force. ``rule_classes`` carries policy rules, and that is exactly how
#: hand-written configs ended up with rule validation that never fired.
#:
#: This is the one rename that lives here rather than in ``migrations/``, which is
#: why it needs saying twice-over: a caller that migrates first — as this walk's
#: callers must — still has not seen it.
class LegacyRename(NamedTuple):
    """Where a renamed top-level key goes, and the release that moved it."""

    to: str
    since: str


LEGACY_TOP_LEVEL_RENAMES: Dict[str, LegacyRename] = {
    "rule_classes": LegacyRename(to="policy_classes", since="v0.5.9"),
}

#: Keys a model deliberately stopped declaring, reported as *deprecated* rather
#: than *unknown*. This changes the wording of a finding and never suppresses one,
#: which is why it is not a gate exemption: both kinds are logged.
#:
#: Keyed by the model class itself, so renaming a class is an import-time failure
#: rather than a silently inert entry. ``ExtractionConfig`` and
#: ``ConfidenceConfig`` both declare no ``max_tokens`` on purpose (see the NOTE in
#: each: output is always requested at the model's maximum), and a configuration
#: stored before that decision still carries one — so "unknown" would be the wrong
#: word for the commonest live case.
DEPRECATED_CONFIG_FIELDS_BY_MODEL: Dict[type, frozenset[str]] = {
    ExtractionConfig: frozenset({"max_tokens"}),
    ConfidenceConfig: frozenset({"max_tokens"}),
}

#: Dotted paths the report stays quiet about, because this repository ships them
#: in its own configuration. A warning the operator did not cause and cannot act
#: on, on every load, is what teaches them to ignore the line on the day it is
#: about their own typo. One entry per path — never per subtree — and each entry
#: names where the same fact is already **recorded**, so silence here is not
#: silence everywhere.
#:
#: *Recorded*, not reported: nothing in the tree emits a finding for
#: ``discovery.output_format``. The fact lives in the prose of another exemption,
#: and what the gate holding it does with the key is excuse a parity finding.
#:
#: Not in ``scripts/tests/gate_exemptions.json``, and the reason is a measured
#: trade-off rather than a claim that this is not an exemption — it is one, for two
#: gates. ``exemption_discovery.PYTHON_PATHSPECS`` does not read the library, so an
#: entry would fail ``test_no_registered_exemption_has_vanished``; adding the
#: narrowest glob that would find this (``lib/idp_common_pkg/idp_common/config/*.py``)
#: discovers six surfaces — four unrelated to this change, three of them
#: function-locals — each then owing an authored judgement. A registry that asks for
#: judgements on noise is how a reviewer learns to rubber-stamp it, which is the
#: failure that registry exists to prevent. So the ratchets it would ask for live in
#: ``tests/unit/config/test_unknown_nested_keys.py`` instead, one per entry:
#: non-vacuity (the path must still be one the models drop, or it is pre-suppressing
#: whatever next occupies it), closure (the path must still be shipped in this
#: repository's own system defaults, which is the entire justification), and a pin on
#: the size of this map so a second entry arrives as a failure.
SUPPRESSED_IGNORED_KEY_PATHS: Dict[str, str] = {
    "discovery.output_format": (
        "Shipped in system_defaults/base-discovery.yaml, so it is on the default "
        "path of every deployment and no operator wrote it. Already registered as "
        "a dead knob by UI_ONLY_SUBTREES in "
        "scripts/sdlc/tests/test_config_ui_schema_parity.py, which is where the "
        "decision to wire it up or remove it belongs (GitHub #707 follow-up). "
        "Delete this entry when that decision is taken either way."
    ),
}

#: Paths ``IDPConfig`` does not model but that **another consumer reads**, so
#: "ignored, leaving the default in force" is false for them. Same shape as
#: ``TOP_LEVEL_KEY_EXEMPT`` in ``scripts/tests/test_preset_keys_are_read.py``, and
#: for the same reason: the entry names the production reader and the source text
#: that constitutes the read, so the premise is computed per entry rather than
#: asserted in prose. A test that merely looked for the key name would stay green
#: after the line doing the reading was deleted.
#:
#: This knowledge used to live only in that test module, which no production code
#: can consult — and that is how three separate reporters came to tell an operator
#: that ``description`` would be ignored.
PATHS_READ_ELSEWHERE: Dict[str, Tuple[str, str]] = {
    "description": (
        "src/lambda/update_configuration/index.py",
        'pop("description"',
    ),
}

#: Upper bound on findings named in one log line. A configuration written against
#: a different product entirely would otherwise produce an unreadable line. A value
#: large enough not to bound anything is the way this stops working, so the test
#: asserts the magnitude rather than only the truncation branch.
MAX_REPORTED_IGNORED_KEYS = 20

#: How close a sibling field name must be to earn a "did you mean" on a
#: misspelling. Only consulted when the key is not a real field somewhere else in
#: the tree, since that case is answered exactly.
_SUGGESTION_CUTOFF = 0.8

#: The block that holds a classification call's model parameters. A call setting
#: written directly in the call's own block (``two_stage.section_classification.
#: model``) belongs one level down in this block, and a same-depth tie with another
#: sub-block declaring the same name (``section_classification.embeddings.model``,
#: a different model for a method that is declared but not selectable) is settled
#: in its favour. Only for a key written one level too shallow inside its own block:
#: every other tie still declines.
_CALL_SETTINGS_BLOCK = "llm"


class IgnoredConfigKey(NamedTuple):
    """One key in a configuration that the models will not read.

    ``path`` is the dotted path as written (list elements appear as ``[i]``),
    ``kind`` is ``"unknown"`` or ``"deprecated"``, and ``suggestion`` is the dotted
    path of a declared field the author plausibly meant, or ``None``.
    """

    path: str
    kind: str
    suggestion: Optional[str]

    def describe(self) -> str:
        """Render the finding for a log line or a CLI warning."""
        if self.suggestion:
            return f"{self.path} (did you mean {self.suggestion}?)"
        return self.path


def _strip_annotated(annotation: Any) -> Any:
    """Return the underlying type of ``Annotated[T, ...]``, else ``annotation``."""
    while hasattr(annotation, "__metadata__"):
        args = get_args(annotation)
        if not args:
            break
        annotation = args[0]
    return annotation


def _nested_model_target(annotation: Any) -> Tuple[Optional[str], Optional[type]]:
    """Resolve where a field's value carries a nested config model.

    Returns ``(shape, model)`` where shape is ``"model"`` (the value is that
    model), ``"list"`` (each element is), ``"map"`` (each value is), or
    ``(None, None)`` when the annotation names no single model — which is the case
    for every free-form ``Dict[str, Any]`` block and is why those are never walked.
    """
    annotation = _strip_annotated(annotation)

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "model", annotation

    origin = get_origin(annotation)
    if origin is None:
        return None, None

    args = [a for a in get_args(annotation) if a is not type(None)]

    # Optional[X] / Union[X, Y]: usable only when exactly one member is a model,
    # otherwise there is no way to tell which one this value is.
    if origin is Union or str(origin) in (
        "types.UnionType",
        "<class 'types.UnionType'>",
    ):
        resolved = [_nested_model_target(a) for a in args]
        models = [r for r in resolved if r[0] is not None]
        return models[0] if len(models) == 1 else (None, None)

    if origin in (list, set, frozenset, tuple):
        if len(args) != 1:
            return None, None
        shape, model = _nested_model_target(args[0])
        return ("list", model) if shape == "model" else (None, None)

    if origin is dict:
        if len(args) != 2:
            return None, None
        shape, model = _nested_model_target(args[1])
        return ("map", model) if shape == "model" else (None, None)

    return None, None


@lru_cache(maxsize=None)
def _field_path_index(root: type) -> Dict[str, Tuple[Tuple[str, ...], ...]]:
    """Map every field name in a model tree to the paths that declare it.

    Derived from the annotations, so a model added to the tree later is indexed
    without being listed, and each path is a tuple of segments. All paths to a
    repeated model are enumerated — ``ImageConfig`` is reachable four ways — because
    what makes a suggestion trustworthy is knowing whether the answer is unique.

    A segment for a list-typed field carries ``[]``: ``ocr.postHook[].arn`` says
    unambiguously where that field lives, where ``ocr.postHook.arn`` reads as a path
    and is not one. It is notation rather than something to paste — the index is a
    lookup table, not config syntax.

    ⚠️ A **map-typed** field (``Dict[str, Model]``) is deliberately not indexed, and
    nor is anything under it. The walk itself descends there and names findings with
    the key the author used, but a *suggestion* would have to name a key that does
    not exist yet, and every spelling of that (``field.*``, ``field.<name>``) either
    breaks the segment round-trip or invents a name. No field in the tree is shaped
    that way today; when one is, a suggestion inside it is absent rather than wrong.
    """
    index: Dict[str, List[Tuple[str, ...]]] = {}

    def visit(model: type, prefix: Tuple[str, ...], chain: Tuple[type, ...]) -> None:
        for name, field in model.model_fields.items():
            index.setdefault(name, []).append(prefix + (name,))
            shape, nested = _nested_model_target(field.annotation)
            if nested is None or nested in chain or shape == "map":
                continue
            step = f"{name}[]" if shape == "list" else name
            visit(nested, prefix + (step,), chain + (nested,))

    visit(root, (), (root,))
    return {name: tuple(paths) for name, paths in index.items()}


def _generalise(segments: Tuple[str, ...]) -> Tuple[str, ...]:
    """Rewrite the indices of a written path to the index's own spelling.

    ``ocr.features[0]`` and ``ocr.features[]`` are the same place; comparing them
    as written would make every suggestion inside a list unreachable.
    """
    return tuple(re.sub(r"\[\d+\]$", "[]", part) for part in segments)


def _suggest_path(
    key: str, prefix: Tuple[str, ...], model: type, root: type
) -> Optional[str]:
    """Name a declared field the author plausibly meant, or ``None``.

    Two different questions, in order, and **neither is answered by guessing**. A
    wrong path is worse than no path: it sends the author to edit something that was
    already correct, and it is the reading that makes them distrust the next warning.

    1. *Wrong depth, either direction.* The key is a real field near where it was
       written, and the nearest place is unambiguous. "Near" is read outwards: the
       written prefix first, then its parent, and so on to the root, stopping at the
       **first** prefix that has any candidate at all. Within that prefix the
       shallowest candidate wins if it is alone at that depth, and a tie declines.
       One tie is settled rather than declined: a key one level too shallow inside
       the block it was written in, where exactly one tied candidate sits in that
       block's ``llm`` sub-block (``_CALL_SETTINGS_BLOCK``) —
       ``two_stage.section_classification.model`` →
       ``...section_classification.llm.model`` rather than nothing, because
       ``...embeddings.model`` ties with it at the same depth.

       Both mistakes happen, and both are answered. Too shallow: ``ocr.dpi`` →
       ``ocr.image.dpi``. Too deep: ``ocr.image.backend`` → ``ocr.backend``, which is
       the mistake the documentation's own "``ocr.backend`` really is one level up"
       invites an author to over-generalise. Depth settles the ordinary ambiguity —
       ``extraction.dpi`` has ``extraction.image.dpi`` one level down and
       ``extraction.confidence.image.dpi`` two — and a tie is what keeps a guess out:
       ``enabled`` is declared at eight distinct places one level under
       ``extraction``, so ``extraction.enabled`` gets no hint rather than one of the
       eight, and ``hitl.model`` reaches the root only to find eleven equally near
       candidates and decline, rather than being answered with
       ``summarization.model``.
    2. *Misspelling.* Failing that, a close name among the **siblings** — the fields
       of the model the key was actually written in.

    **Neither step guesses.** A wrong path is worse than no path: it sends the author
    to edit something that was already correct, and it is the reading that makes them
    distrust the next warning.
    """
    written = _generalise(prefix + (key,))
    here = _generalise(prefix)
    declared = _field_path_index(root).get(key, ())

    # Outwards from where it was written, stopping at the first level that has any
    # candidate: a nearer prefix with a tie must decline rather than widen, or every
    # ambiguous key would walk out to the root and be answered from another section.
    for depth in range(len(here), -1, -1):
        scope = here[:depth]
        candidates = [
            path for path in declared if path != written and path[: len(scope)] == scope
        ]
        if not candidates:
            continue
        nearest = min(len(path) for path in candidates)
        closest = [path for path in candidates if len(path) == nearest]
        if len(closest) == 1:
            return ".".join(closest[0])
        if depth == len(here) and nearest == len(written) + 1:
            # One level too shallow inside the block it was written in: prefer
            # the call's own settings block when exactly one candidate is in it.
            in_call_block = [
                path for path in closest if path[len(here)] == _CALL_SETTINGS_BLOCK
            ]
            if len(in_call_block) == 1:
                return ".".join(in_call_block[0])
        break

    close = difflib.get_close_matches(
        key, list(model.model_fields.keys()), n=1, cutoff=_SUGGESTION_CUTOFF
    )
    if close:
        return ".".join(prefix + (close[0],))
    return None


def collect_ignored_config_keys(
    data: Any,
    model: type,
    *,
    include_top_level: bool = False,
) -> List[IgnoredConfigKey]:
    """Find every key in ``data`` that ``model`` and its nested models will drop.

    ⚠️ ``data`` must already have been through ``migrations.migrate_config`` if it
    could be legacy-shaped. A legacy key is *relocated* on load rather than dropped
    — ``extraction.agentic.validation`` becomes ``extraction.validation`` — so
    against the pre-migration shape this would report a key that works. The
    ``IDPConfig`` validator that calls it runs after the migration chain for that
    reason, and ``merge_utils`` migrates a copy before asking.

    Args:
        data: The configuration mapping as written, before validation.
        model: The root model to interpret it against, normally ``IDPConfig``.
        include_top_level: Report depth-0 keys too. Off by default and **no
            production caller turns it on**, deliberately: depth 0 already has
            three reporters (``IDPConfig``'s own two messages, and a block each in
            ``idp_cli`` and ``idp_sdk``), and two of the things they know a generic
            walk does not — that ``update_configuration`` pops and stores
            ``description``, and that ``TOP_LEVEL_KEY_EXEMPT`` in
            ``scripts/tests/test_preset_keys_are_read.py`` is where such a key is
            recorded — are what make "it will be ignored" false for those keys.
            Consult that list before enabling this for anything a person reads.

    Returns:
        Findings sorted by dotted path. Empty when every key is read.
    """
    findings: List[IgnoredConfigKey] = []

    def visit(value: Any, current: type, prefix: Tuple[str, ...]) -> None:
        if not isinstance(value, dict):
            return
        fields = current.model_fields
        keeps_extra = current.model_config.get("extra") == "allow"
        deprecated = DEPRECATED_CONFIG_FIELDS_BY_MODEL.get(current, frozenset())

        for key, item in value.items():
            if not isinstance(key, str):
                continue
            field = fields.get(key)
            if field is None:
                if keeps_extra:
                    # The model retains this key, so it is not being dropped.
                    continue
                if not prefix and key in LEGACY_TOP_LEVEL_RENAMES:
                    # Relocated on load, not dropped: the setting works.
                    continue
                if not prefix and not include_top_level:
                    continue
                dotted = ".".join(prefix + (key,))
                if dotted in SUPPRESSED_IGNORED_KEY_PATHS:
                    continue
                if dotted in PATHS_READ_ELSEWHERE:
                    # Something other than IDPConfig reads this one.
                    continue
                findings.append(
                    IgnoredConfigKey(
                        path=dotted,
                        kind="deprecated" if key in deprecated else "unknown",
                        suggestion=(
                            None
                            if key in deprecated
                            else _suggest_path(key, prefix, current, model)
                        ),
                    )
                )
                continue

            shape, nested = _nested_model_target(field.annotation)
            if nested is None:
                continue
            if shape == "model":
                visit(item, nested, prefix + (key,))
            elif shape == "list" and isinstance(item, (list, tuple)):
                for position, element in enumerate(item):
                    visit(element, nested, prefix + (f"{key}[{position}]",))
            elif shape == "map" and isinstance(item, dict):
                for name, element in item.items():
                    if isinstance(name, str):
                        visit(element, nested, prefix + (f"{key}.{name}",))

    visit(data, model, ())
    findings.sort(key=lambda finding: finding.path)
    return findings


def format_ignored_config_keys(findings: List[IgnoredConfigKey], kind: str) -> str:
    """Render the findings of one kind as a bounded, sorted list for a message."""
    described = [finding.describe() for finding in findings if finding.kind == kind]
    if len(described) > MAX_REPORTED_IGNORED_KEYS:
        remaining = len(described) - MAX_REPORTED_IGNORED_KEYS
        described = described[:MAX_REPORTED_IGNORED_KEYS]
        described.append(f"... and {remaining} more")
    return ", ".join(described)


def log_ignored_config_keys(
    data: Any,
    model: type,
    *,
    include_top_level: bool = False,
) -> List[IgnoredConfigKey]:
    """Report every key ``model`` will drop to the module logger, and return them.

    The one reporter every record root shares, so a root added to this module does
    not have to re-derive the wording, the ``deprecated``/``unknown`` split or the
    bound on the line. The message names ``model`` itself, which is what lets the
    same code serve ``IDPConfig`` and the smaller record roots without either of
    them reading as the other.

    **It reports; it does not reject.** ``extra`` is unchanged on every model, so a
    stored record that loads today still loads.

    Call it from a ``mode="before"`` validator rather than from a save path, and note
    the reason is not that the save path is bypassed. ``save_custom_pricing`` and
    ``save_custom_model_config_limits`` do call ``save_configuration``, and the UI
    resolvers call them — but they hand it an already-validated model, because the
    resolver constructs the model itself. ``save_configuration`` reads a dict in one
    branch only, and that branch is not on the operator's path: by the time the record
    reaches it, the mistyped key is already gone. The validator is the one place that
    covers every construction site — ``ConfigurationManager``, the configuration
    resolver, and ``update_configuration`` at deploy time.

    Args:
        data: The record as written, before validation. A non-mapping is ignored.
        model: The root model to interpret it against.
        include_top_level: Report depth-0 keys too. Leave off for a root that
            takes ``extra="forbid"``: Pydantic already raises for those, and a
            warning beside the exception says the key was ignored when it was not.

    Returns:
        The findings, so a caller that needs to act can.
    """
    import logging

    if not isinstance(data, dict):
        return []
    findings = collect_ignored_config_keys(
        data, model, include_top_level=include_top_level
    )
    if not findings:
        return findings
    logger = logging.getLogger(__name__)
    deprecated_line = format_ignored_config_keys(findings, "deprecated")
    unknown_line = format_ignored_config_keys(findings, "unknown")
    if deprecated_line:
        logger.warning(
            "%s: Ignoring deprecated nested fields (these are no longer used): %s",
            model.__name__,
            deprecated_line,
        )
    if unknown_line:
        logger.warning(
            "%s: Ignoring unknown nested fields (not defined in model, so the "
            "shipped default stays in force): %s",
            model.__name__,
            unknown_line,
        )
    return findings


class SchemaConfig(BaseModel):
    """
    Schema configuration model.

    This represents the JSON Schema configuration type stored in DynamoDB.
    It contains the structure/definition of document schemas.
    """

    config_type: Literal["Schema"] = Field(
        default="Schema", description="Discriminator for config type"
    )

    # Schema config contains the JSON Schema format
    type: str = Field(default="object", description="JSON Schema type")
    required: List[str] = Field(default_factory=list, description="Required properties")
    properties: Dict[str, Any] = Field(
        default_factory=dict, description="Schema properties definitions"
    )
    order: Optional[str] = Field(default=None, description="Display order")

    model_config = ConfigDict(
        extra="allow",  # Allow additional JSON Schema fields
        validate_assignment=True,
    )


class IDPConfig(BaseModel):
    """
    Complete IDP configuration model.

    This model provides type-safe access to IDP configuration and handles
    automatic conversion of string representations (e.g., "0.5" -> 0.5).

    Example:
        config_dict = get_config()
        config = IDPConfig.model_validate(config_dict)

        if config.extraction.agentic.enabled:
            temperature = config.extraction.temperature
    """

    config_type: Literal["Config"] = Field(
        default="Config", description="Configuration type"
    )

    config_format_version: str = Field(
        default=CONFIG_FORMAT_VERSION,
        description=(
            "Config schema/shape version. Configs without this stamp (or stamped "
            "below the current version) are migrated on read (see "
            "config/migrations)."
        ),
    )

    use_bda: bool = Field(
        default=False,
        description="Use Bedrock Data Automation (BDA) for document processing. "
        "When true, BDA handles OCR, classification, and extraction as a single managed service. "
        "When false (default), uses the step-by-step pipeline with configurable OCR, classification, "
        "extraction, and assessment stages.",
    )

    enable_blueprint_optimization: bool = Field(
        default=False,
        description="Enable BDA blueprint optimization during discovery. "
        "When true and a ground truth file is provided, discovery will automatically "
        "optimize the BDA blueprint using the InvokeBlueprintOptimizationAsync API "
        "to improve extraction accuracy. Defaults to false.",
    )

    managed: bool = Field(
        default=False,
        description="Stack-managed configuration that is overwritten on stack updates.",
    )

    test_set: Optional[str] = Field(
        default=None,
        description="Associated test set name (documentation/reference only).",
    )

    notes: Optional[str] = Field(default=None, description="Configuration notes")
    malware_scanning: MalwareScanningConfig = Field(
        default_factory=MalwareScanningConfig,
        description="Policy applied to GuardDuty Malware Protection verdicts on "
        "uploaded documents. Scanning is enabled by the MalwareScanning stack "
        "parameter; this section only decides how a verdict is acted on.",
    )
    preprocessing: PreprocessingConfig = Field(
        default_factory=PreprocessingConfig,
        description="Preprocessing configuration — home of the standalone "
        "`preprocessing` pipeline-hook point (runs first, before BDA/pipeline "
        "routing). Used by the PII Anonymization extension.",
    )
    ocr: OCRConfig = Field(default_factory=OCRConfig, description="OCR configuration")
    classification: ClassificationConfig = Field(
        default_factory=ClassificationConfig,
        description="Classification configuration",
    )
    extraction: ExtractionConfig = Field(
        default_factory=ExtractionConfig, description="Extraction configuration"
    )
    hitl: HITLConfig = Field(
        default_factory=HITLConfig,
        description="Human-in-the-Loop review configuration (v0.6, top-level)",
    )
    summarization: SummarizationConfig = Field(
        default_factory=lambda: SummarizationConfig(model="us.amazon.nova-pro-v1:0"),
        description="Summarization configuration",
    )
    chat: ChatConfig = Field(
        default_factory=ChatConfig,
        description="Chat-with-Document configuration (used by the interactive "
        "document Q&A feature in the Web UI)",
    )
    # No `model=` kwarg: RuleValidationConfig declares no `model` field (its models
    # live on the nested fact_extraction / rule_validation_orchestrator configs), so
    # Pydantic silently dropped it. It was therefore an inert end-of-life model
    # literal, which basedpyright already flagged as `No parameter named "model"`.
    rule_validation: RuleValidationConfig = Field(
        default_factory=RuleValidationConfig,
        description="Rule validation configuration",
    )
    agents: AgentsConfig = Field(
        default_factory=AgentsConfig, description="Agents configuration"
    )
    classes: List[Dict[str, Any]] = Field(
        default_factory=list, description="Document class definitions (JSON Schema)"
    )

    @field_validator("classes", mode="after")
    @classmethod
    def validate_instance_array(cls, v: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Validate ``x-aws-idp-instance-array`` names a real array-of-object.

        The key tells the pipeline which top-level array means "one document per
        element", so ``Section.instance_count`` can be populated for a class whose
        schema is already modelled as a packet of records. A typo would otherwise
        fail silently at runtime (the count simply never appears), which is
        exactly the class of silent-no-op bug this whole workstream exists to
        remove — so it is caught here instead.

        Note ``_validate_schema_fields`` only walks ``properties``/``$defs`` and
        never inspects class-level keys, hence this validator.
        """
        import logging

        from idp_common.config.schema_constants import (
            SCHEMA_ITEMS,
            SCHEMA_PROPERTIES,
            SCHEMA_TYPE,
            TYPE_ARRAY,
            TYPE_OBJECT,
            X_AWS_IDP_DOCUMENT_TYPE,
            X_AWS_IDP_INSTANCE_ARRAY,
            X_AWS_IDP_MULTI_INSTANCE,
        )
        from idp_common.config.schema_utils import deref_schema
        from idp_common.schema.multi_instance import (
            INSTANCES_KEY,
            is_multi_instance,
            is_wrapped,
        )

        logger = logging.getLogger(__name__)

        for doc_class in v:
            if not isinstance(doc_class, dict):
                continue
            label = (
                doc_class.get("$id") or doc_class.get(X_AWS_IDP_DOCUMENT_TYPE) or "?"
            )
            properties_map = doc_class.get(SCHEMA_PROPERTIES)
            properties_map = properties_map if isinstance(properties_map, dict) else {}

            # An ALREADY-TRANSFORMED schema legitimately carries both keys (the
            # wrapper sets instance-array: instances so #694's count machinery
            # applies to it). The transform is applied at runtime and never
            # persisted to config, so this should not happen — but rejecting a
            # schema this code produced itself would be a nasty trap for anyone
            # who round-trips one, so recognise and skip it.
            if is_wrapped(doc_class):
                continue

            if is_multi_instance(doc_class):
                # The two modes answer opposite questions — Designate names an
                # array the class ALREADY has, Synthesize creates one — so
                # setting both is not a stronger request, it is a contradiction.
                if doc_class.get(X_AWS_IDP_INSTANCE_ARRAY) is not None:
                    raise ValueError(
                        f"Class '{label}' sets both {X_AWS_IDP_MULTI_INSTANCE} and "
                        f"{X_AWS_IDP_INSTANCE_ARRAY}, which are mutually exclusive. "
                        f"Use {X_AWS_IDP_MULTI_INSTANCE} when the class describes "
                        f"ONE record and you want a list of them synthesized; use "
                        f"{X_AWS_IDP_INSTANCE_ARRAY} when the class is already a "
                        f"packet of records and you only want to name its existing "
                        f"instance axis."
                    )

                # Wrapper-key collision: the transform adds a top-level
                # 'instances' property, which would shadow the user's own field
                # of that name and make the original unreachable. Rejected, not
                # renamed — a silent rename changes the extraction contract under
                # the user.
                if INSTANCES_KEY in properties_map:
                    raise ValueError(
                        f"Class '{label}' sets {X_AWS_IDP_MULTI_INSTANCE} but "
                        f"already declares a top-level property named "
                        f"'{INSTANCES_KEY}', which the synthesized wrapper would "
                        f"shadow. Rename that property, or use "
                        f"{X_AWS_IDP_INSTANCE_ARRAY}: {INSTANCES_KEY} instead if it "
                        f"is already the class's record array."
                    )

                # WARN ONLY when the class looks like it is already a list
                # wrapper. Deliberately narrow: having an internal array is NOT
                # evidence of this. An invoice with line_items[] is a
                # single-instance document with an internal list, and
                # multi-instance on it is perfectly correct — three invoices in
                # one section becomes instances[i].line_items[j]. Erroring here
                # would block a legitimate and common case, so this is a log
                # line, never a failure.
                array_props = [
                    name
                    for name, spec in properties_map.items()
                    if isinstance(spec, dict)
                    and deref_schema(spec, doc_class).get(SCHEMA_TYPE) == TYPE_ARRAY
                ]
                # Few-shot examples are hand-authored FLAT JSON in
                # `attributesPrompt`. For a flagged class they therefore teach the
                # model the opposite of the requested shape — and
                # `_adapt_to_instances_wrapper` then rescues the flat answer as
                # exactly ONE instance, so the loss looks like success. Warn, not
                # error: the examples may already have been re-authored wrapped,
                # and this validator cannot read prose.
                examples = doc_class.get("x-aws-idp-examples")
                if isinstance(examples, list) and examples:
                    logger.warning(
                        "Class '%s' sets %s and also carries %d few-shot "
                        "example(s). Example prompts are hand-authored text: if "
                        "they show a FLAT record they now contradict the "
                        "requested {'instances': [...]} shape, and a flat answer "
                        "is salvaged as exactly one instance — so the loss looks "
                        "like success. Re-author them wrapped.",
                        label,
                        X_AWS_IDP_MULTI_INSTANCE,
                        len(examples),
                    )

                if len(array_props) == 1 and len(properties_map) == 1:
                    only = array_props[0]
                    logger.warning(
                        "Class '%s' sets %s but its top level is nothing but the "
                        "array property '%s', so it already looks like a packet of "
                        "records — the transform would produce instances[i].%s[j], "
                        "one level too many. If '%s' is the record array, use "
                        "%s: %s instead.",
                        label,
                        X_AWS_IDP_MULTI_INSTANCE,
                        only,
                        only,
                        only,
                        X_AWS_IDP_INSTANCE_ARRAY,
                        only,
                    )

            prop_name = doc_class.get(X_AWS_IDP_INSTANCE_ARRAY)
            if prop_name is None:
                continue
            if not isinstance(prop_name, str) or not prop_name:
                raise ValueError(
                    f"{X_AWS_IDP_INSTANCE_ARRAY} on class '{label}' must be the "
                    f"name of a top-level array property, got {prop_name!r}"
                )
            properties = doc_class.get(SCHEMA_PROPERTIES)
            if not isinstance(properties, dict) or prop_name not in properties:
                available = sorted(properties) if isinstance(properties, dict) else []
                raise ValueError(
                    f"{X_AWS_IDP_INSTANCE_ARRAY} on class '{label}' names "
                    f"'{prop_name}', which is not a top-level property of that "
                    f"class. Available properties: {available}"
                )
            prop_schema = properties[prop_name]
            if not isinstance(prop_schema, dict):
                raise ValueError(
                    f"{X_AWS_IDP_INSTANCE_ARRAY} on class '{label}' names "
                    f"'{prop_name}', whose schema is not an object"
                )
            # Resolve a local $ref first. Declaring the record list as
            # {"$ref": "#/$defs/RecordList"} is the idiom the UI schema editor and
            # several shipped presets use for a reusable record type, and the
            # runtime resolver does not care (it just reads the extracted list's
            # length). Type-checking the un-dereferenced node would reject a
            # perfectly valid schema — and reject it as a HARD config-load
            # failure, which is worse than the silent no-op this validator exists
            # to prevent. deref_schema returns the node as-is when the $ref cannot
            # be resolved, so an unresolvable ref is still type-checked.
            prop_schema = deref_schema(prop_schema, doc_class)
            if prop_schema.get(SCHEMA_TYPE) != TYPE_ARRAY:
                raise ValueError(
                    f"{X_AWS_IDP_INSTANCE_ARRAY} on class '{label}' names "
                    f"'{prop_name}', which is type "
                    f"'{prop_schema.get(SCHEMA_TYPE)}' — it must be an array, "
                    f"since each element is one document instance"
                )
            items = prop_schema.get(SCHEMA_ITEMS)
            # Allow $ref'd items (resolved at runtime); only reject an inline
            # items schema that is explicitly a non-object.
            if (
                isinstance(items, dict)
                and "$ref" not in items
                and items.get(SCHEMA_TYPE) not in (None, TYPE_OBJECT)
            ):
                raise ValueError(
                    f"{X_AWS_IDP_INSTANCE_ARRAY} on class '{label}' names "
                    f"'{prop_name}', whose items are type "
                    f"'{items.get(SCHEMA_TYPE)}' — each element must be an "
                    f"object representing one document instance"
                )
        return v

    policy_classes: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Policy class definitions for rule validation (JSON Schema). Also receives rule classes extracted by Policy Discovery.",
    )

    @field_validator("policy_classes", mode="after")
    @classmethod
    def validate_policy_classes_engine_field(
        cls, v: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Validate x-aws-idp-validation-engine values in policy classes.

        Ensures that when the engine field is present on a rule property,
        its value is one of the accepted values ("llm" or "z3", case-sensitive).
        If the field is absent, that's fine (defaults to "llm" at runtime).
        """
        from idp_common.config.schema_constants import (
            VALID_VALIDATION_ENGINES,
            X_AWS_IDP_VALIDATION_ENGINE,
        )

        for policy_class in v:
            rule_properties = policy_class.get("rule_properties", {})
            if not isinstance(rule_properties, dict):
                continue
            for prop_name, prop_value in rule_properties.items():
                if not isinstance(prop_value, dict):
                    continue
                if X_AWS_IDP_VALIDATION_ENGINE in prop_value:
                    engine_value = prop_value[X_AWS_IDP_VALIDATION_ENGINE]
                    if engine_value not in VALID_VALIDATION_ENGINES:
                        raise ValueError(
                            f"Invalid validation engine '{engine_value}' for rule "
                            f"property '{prop_name}'. "
                            f"Accepted values are: {sorted(VALID_VALIDATION_ENGINES)}"
                        )
        return v

    discovery: DiscoveryConfig = Field(
        default_factory=DiscoveryConfig, description="Discovery configuration"
    )
    evaluation: EvaluationConfig = Field(
        default_factory=EvaluationConfig, description="Evaluation configuration"
    )
    postprocessing: PostprocessingConfig = Field(
        default_factory=PostprocessingConfig,
        description="Postprocessing configuration — home of the standalone "
        "`postprocessing` pipeline-hook point (runs last, after evaluation and "
        "before the workflow completes). The mirror image of `preprocessing`.",
    )

    # Pricing configuration (optional - loaded separately but can be merged for convenience)
    pricing: Optional[List[PricingEntry]] = Field(
        default=None,
        description="Pricing entries (optional - usually loaded from PricingConfig)",
    )

    # Rule validation specific fields (used in pattern-2/rule-validation)
    summary: Optional[Dict[str, Any]] = Field(
        default=None, description="Summary configuration for rule validation"
    )

    model_config = ConfigDict(
        # Allow extra fields to be ignored - supports backward compatibility
        # with older configs that may have deprecated fields
        extra="ignore",
        # Validate on assignment
        validate_assignment=True,
    )

    @model_validator(mode="before")
    @classmethod
    def log_deprecated_fields(cls, data: Any) -> Any:
        """Log warnings for deprecated/unknown fields before they're silently ignored."""
        import logging

        logger = logging.getLogger(__name__)

        if isinstance(data, dict):
            # Apply the whole migration chain (v0.5 → v0.6 → v0.7): assessment.*
            # → extraction.confidence / extraction.geometry / top-level hitl, then
            # extraction.agentic.validation → extraction.validation. Idempotent:
            # a no-op once the config is already stamped with
            # CONFIG_FORMAT_VERSION and carries no legacy-shaped keys.
            #
            # This is why every path that builds an IDPConfig is covered without
            # its own migrate call — including config paths added later, e.g.
            # ConfigurationManager._load_revision_config loading a stored revision
            # written before the upgrade.
            from .migrations import migrate_config

            data = migrate_config(data)

            # Apply the renames this model performs itself rather than in
            # migrations/ — one definition, LEGACY_TOP_LEVEL_RENAMES, so that the
            # unknown-key walk below knows these keys are relocated and not lost.
            for old_name, rename in LEGACY_TOP_LEVEL_RENAMES.items():
                new_name = rename.to
                if old_name not in data:
                    continue
                if new_name not in data:
                    data[new_name] = data.pop(old_name)
                    logger.info("Migrated config key '%s' → '%s'", old_name, new_name)
                    continue
                # Both keys present: the new name wins and the old one is dropped.
                # Say so loudly — this discards user-supplied content, and because
                # the old name is a known-deprecated key it does not trip the
                # unknown-field warning either. Silently losing it is how
                # hand-written and notebook-produced configs ended up with rule
                # validation that never fired.
                discarded = data.get(old_name)
                count = len(discarded) if isinstance(discarded, (list, dict)) else 1
                logger.warning(
                    "Both '%s' (deprecated) and '%s' are present in this "
                    "configuration; DISCARDING '%s' (%d %s). '%s' was renamed to "
                    "'%s' in %s — merge these entries into '%s' or they will not be "
                    "used.",
                    old_name,
                    new_name,
                    old_name,
                    count,
                    "entry" if count == 1 else "entries",
                    old_name,
                    new_name,
                    rename.since,
                    new_name,
                )
                del data[old_name]

            # Get all field names defined in the model
            defined_fields = set(cls.model_fields.keys())

            # Find extra fields in the input data
            extra_fields = set(data.keys()) - defined_fields

            if extra_fields:
                # Categorize as deprecated vs unknown
                deprecated = extra_fields & IDP_CONFIG_DEPRECATED_FIELDS
                unknown = extra_fields - IDP_CONFIG_DEPRECATED_FIELDS

                if deprecated:
                    logger.warning(
                        f"IDPConfig: Ignoring deprecated fields (these are no longer used): "
                        f"{sorted(deprecated)}"
                    )

                if unknown:
                    logger.warning(
                        f"IDPConfig: Ignoring unknown fields (not defined in model): "
                        f"{sorted(unknown)}"
                    )

            # The same report for every level below this one. Without it a
            # misspelled or mis-nested key was dropped in silence, leaving the
            # shipped default in force — which looks exactly like a working
            # setting, and in the mis-nested case routes the value around the
            # validator that would have rejected it. Reports only; `extra` is
            # unchanged, so nothing that loads today stops loading.
            #
            # Shared with the other record roots in this module, which have the
            # same defect one list-entry down and no top-level message of their
            # own: the wording, the deprecated/unknown split and the bound on the
            # line are written once.
            log_ignored_config_keys(data, cls)

        return data

    @model_validator(mode="after")
    def refuse_classification_with_no_content(self) -> "IDPConfig":
        """Refuse a classification modality that can only ever send empty content.

        One cross-section combination is unsatisfiable and silent: an OCR
        backend of ``none`` produces no page text at all, so a classification
        configuration that also turns images **off** has nothing left. Before
        this check the request still went out, with every page substituted as
        the literal string ``[No text content for page N]``, and the model
        returned a confident class derived from nothing — a document that
        completes green with a fabricated answer.

        Only the explicit ``images: false`` is refused here. When ``images`` is
        unset it is inferred from the prompt, and the prompt the inference reads
        depends on the granularity, so the residual case is caught at the point
        the request is built instead (see
        ``ClassificationService._packet_text_unavailable_error``). That split is
        deliberate: a config-load failure must be a thing the operator can act
        on from the message alone.
        """
        if not (self.ocr.backend and self.ocr.backend.strip().lower() == "none"):
            return self
        cls = self.classification
        # Only the calls the active strategy makes: an inactive block with images
        # off can send nothing to anyone, and refusing it would stop an operator
        # saving a configuration whose live calls are fine.
        if cls.strategy == "two_stage":
            calls = {
                "two_stage.segmentation.llm": cls.two_stage.segmentation.llm,
                "two_stage.section_classification.llm": (
                    cls.two_stage.section_classification.llm
                ),
            }
        else:
            calls = {"one_stage.llm": cls.one_stage.llm}
        for path, call in calls.items():
            if call.modality.images is False:
                raise ValueError(
                    "ocr.backend is 'none' (no page text is produced) and "
                    f"classification.{path}.modality.images is false, so that call "
                    "would have no text and no images to work from. Either choose "
                    "a real OCR backend, or turn its images on."
                )
        return self

    def to_dict(self, **extra_fields: Any) -> Dict[str, Any]:
        """
        Convert to a mutable dictionary with optional extra fields.

        This is useful when you need to add runtime-specific fields (like endpoint names)
        to the configuration that aren't part of the model schema.

        Args:
            **extra_fields: Additional fields to add to the dictionary

        Returns:
            Mutable dictionary with model data plus any extra fields

        Example:
            config = get_config(as_model=True)
            config_dict = config.to_dict(sagemaker_endpoint_name=endpoint)
        """
        result = self.model_dump(mode="python")
        result.update(extra_fields)
        return result


# Registered here rather than in the literal above because `IDPConfig` does not
# exist yet at that point. One authority for "deprecated, not unknown" at every
# depth: a caller asking for top-level keys too (`include_top_level=True`) gets the
# same wording `IDPConfig`'s own validator uses for them.
DEPRECATED_CONFIG_FIELDS_BY_MODEL[IDPConfig] = frozenset(IDP_CONFIG_DEPRECATED_FIELDS)


class ConfigMetadata(BaseModel):
    """Metadata for configuration records"""

    created_at: Optional[str] = Field(default=None, description="Creation timestamp")
    updated_at: Optional[str] = Field(default=None, description="Update timestamp")


class ConfigurationRecord(BaseModel):
    """
    DynamoDB storage model for IDP configurations.

    This model wraps IDPConfig and handles serialization/deserialization
    to/from DynamoDB, including the critical string conversion for storage.

    Example:
        # Create from IDPConfig
        config = IDPConfig(...)
        record = ConfigurationRecord(
            configuration_type="config",
            config=config
        )

        # Serialize to DynamoDB
        item = record.to_dynamodb_item()

        # Deserialize from DynamoDB
        record = ConfigurationRecord.from_dynamodb_item(item)
        idp_config = record.config
    """

    configuration_type: str = Field(
        description="Configuration type (Config, Schema, Pricing)"
    )
    version: Optional[str] = Field(default=None, description="Version Name")
    is_active: Optional[bool] = Field(
        default=None, description="Whether this version is active"
    )

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, v: Any) -> Optional[str]:
        """Ensure version field accepts None or string values"""
        if v is None:
            return None
        return str(v) if v else None

    description: Optional[str] = Field(default=None, description="Version description")
    config: Annotated[
        Union[SchemaConfig, IDPConfig, PricingConfig, ModelConfigLimitsConfig],
        Discriminator("config_type"),
    ] = Field(
        description="The configuration - SchemaConfig for Schema type, PricingConfig for Pricing type, ModelConfigLimitsConfig for ModelConfigLimits type, IDPConfig for Default/Custom"
    )
    metadata: Optional[ConfigMetadata] = Field(
        default=None, description="Optional metadata about the configuration"
    )

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """
        Convert to DynamoDB item format.

        This method:
        1. Exports config as a Python dict
        2. Removes the config_type discriminator (not needed in DynamoDB)
        3. Stringifies values (preserving booleans, converting numbers to strings)
        4. Adds the Configuration partition key

        Returns:
            Dict suitable for DynamoDB put_item() with:
            - Configuration: str (partition key)
            - All config fields stringified (except booleans)
        """

        # Get config as dict using Pydantic's model_dump
        config_dict = self.config.model_dump(mode="python")

        # Remove the discriminator field - it's only for Pydantic, not DynamoDB
        config_dict.pop("config_type", None)

        # Rollback-safety: omit scalar fields whose value is a "default sentinel"
        # that an OLDER release's Pydantic model would reject. A stack rollback
        # reverts the config custom-resource Lambda to the PRIOR release's code
        # but leaves this (current-shape) record in DynamoDB; the old code then
        # re-reads it. Two current-shape values are known to break older models:
        #   - None on a field the old model coerces with a bare int()  -> int(None) TypeError
        #   - 0   on a field the old model constrains with gt=0         -> ValidationError
        # Omitting a field that equals its own default is a no-op on read for the
        # CURRENT model (absent == default), so this is behavior-neutral here while
        # sparing the reverted old model from values it can't parse. Only None and
        # integer 0 (the two proven-breaking classes) are stripped; positive
        # defaults and float 0.0 (e.g. temperature) are preserved untouched.
        config_dict = self._omit_rollback_hostile_defaults(self.config, config_dict)

        # Stringify values (preserve booleans, convert numbers to strings)
        stringified = self._stringify_values(config_dict)

        # Map managed field to PascalCase DynamoDB convention (before spreading into item)
        managed_value = stringified.pop("managed", None)

        configuration_type = (
            f"{self.configuration_type}#{self.version}"
            if self.version
            else self.configuration_type
        )

        # Build DynamoDB item
        item = {"Configuration": configuration_type, **stringified}

        if managed_value is not None:
            item["Managed"] = managed_value

        # Add ConfigurationRecord level fields
        if self.is_active is not None:
            item["IsActive"] = self.is_active
        if self.description is not None:
            item["Description"] = self.description

        # Add metadata fields as separate DynamoDB columns
        if self.metadata:
            metadata_dict = self.metadata.model_dump(mode="python", exclude_none=True)
            if "created_at" in metadata_dict:
                item["CreatedAt"] = metadata_dict["created_at"]
            if "updated_at" in metadata_dict:
                item["UpdatedAt"] = metadata_dict["updated_at"]

        return item

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "ConfigurationRecord":
        """
        Create ConfigurationRecord from DynamoDB item.

        This method:
        1. Extracts the Configuration key
        2. Auto-migrates legacy format if needed
        3. Validates into IDPConfig (Pydantic handles type conversions)

        Args:
            item: Raw DynamoDB item dict

        Returns:
            ConfigurationRecord with validated IDPConfig

        Raises:
            ValueError: If Configuration key is missing
        """
        import logging

        logger = logging.getLogger(__name__)

        # Extract configuration key
        config_key = item.get("Configuration")
        if not config_key:
            raise ValueError("DynamoDB item missing 'Configuration' key")

        # Parse configuration type and version from single key
        if "#" in config_key:
            # Versioned format: Config#v0, Config#v1, etc.
            config_type, version = config_key.split("#", 1)
        else:
            # Non-versioned format: Schema, Pricing, Default, Custom
            config_type = config_key
            version = ""

        # Remove DynamoDB keys and metadata
        # Remove DynamoDB partition key, record metadata, and storage metadata fields
        # These are not part of the config data model
        _DYNAMODB_NON_CONFIG_FIELDS = {
            "Configuration",
            "IsActive",
            "CreatedAt",
            "UpdatedAt",
            "Description",
            "BdaProjectArn",
            "BdaSyncStatus",
            "BdaLastSyncedAt",
            "Managed",
            # Revision counters live on the profile head item and are maintained
            # by ConfigRevisionStore, not by this model.
            "LatestRevision",
            "PublishedRevision",
            "_config_format",
            "_config_storage",
        }
        config_data = {
            k: v for k, v in item.items() if k not in _DYNAMODB_NON_CONFIG_FIELDS
        }

        # Map PascalCase DynamoDB field back to lowercase Pydantic field
        if "Managed" in item:
            config_data["managed"] = item["Managed"]

        # Set config_type discriminator directly from DynamoDB Configuration key
        # DynamoDB keys match Pydantic discriminators exactly:
        # - "Schema" -> SchemaConfig
        # - "Config#version" -> IDPConfig
        # - "DefaultPricing", "CustomPricing" -> PricingConfig
        # - "DefaultModelConfigLimits", "CustomModelConfigLimits" -> ModelConfigLimitsConfig
        # Legacy non-versioned "Default" / "Custom" keys map to IDPConfig
        if config_type in ("Default", "Custom"):
            config_data["config_type"] = "Config"
        else:
            config_data["config_type"] = config_type

        # Auto-migrate legacy format if needed
        if config_data.get("classes"):
            from .migration import is_legacy_format, migrate_legacy_to_schema

            if is_legacy_format(config_data["classes"]):
                logger.info(
                    f"Migrating {config_type} configuration to JSON Schema format"
                )
                config_data["classes"] = migrate_legacy_to_schema(
                    config_data["classes"]
                )

        # Auto-migrate legacy format for policy_classes if needed
        if config_data.get("policy_classes"):
            from .migration import is_legacy_format, migrate_legacy_to_schema

            if is_legacy_format(config_data["policy_classes"]):
                logger.info(
                    f"Migrating {config_type} policy_classes to JSON Schema format"
                )
                config_data["policy_classes"] = migrate_legacy_to_schema(
                    config_data["policy_classes"]
                )

        # Remove legacy pricing field (now stored separately as DefaultPricing/CustomPricing)
        # This handles migration for existing stacks with old embedded pricing
        if config_data.get("pricing") is not None and config_type in (
            "Config",
            "Default",
            "Custom",
        ):
            logger.info(
                f"Removing legacy pricing field from {config_type} configuration"
            )
            config_data.pop("pricing", None)

        # Parse into appropriate config type - Pydantic discriminator handles this automatically
        config = cls.model_validate(
            {"configuration_type": config_type, "config": config_data}
        ).config

        return cls(
            configuration_type=config_type,
            version=version,
            is_active=item.get("IsActive"),
            description=item.get("Description"),
            config=config,
            metadata=ConfigMetadata(
                created_at=item.get("CreatedAt"), updated_at=item.get("UpdatedAt")
            ),
        )

    @staticmethod
    def _omit_rollback_hostile_defaults(model: Any, dumped: Any) -> Any:
        """
        Strip scalar fields whose stored value equals their model default AND is a
        value that older-release Pydantic models are known to reject on read.

        Walks the live Pydantic ``model`` alongside its ``dumped`` dict (from
        ``model_dump``) so each field's declared default is known. A scalar is
        omitted only when ALL of:
          * its value equals the field's declared default (so removal is a no-op
            for the current model — absent == default on the next read), and
          * that value is ``None`` or integer ``0`` (the two classes proven to
            break prior models: bare ``int(None)`` and ``gt=0`` constraints).

        Booleans (``0``-like but semantically real), float ``0.0`` (e.g.
        ``temperature``), and any positive default are preserved. Non-model
        containers (plain dicts/lists with no schema) pass through unchanged,
        since we can't know a default for them.
        """
        from pydantic import BaseModel as _BaseModel

        def _hostile(value: Any) -> bool:
            if value is None:
                return True
            # bool is a subclass of int; never treat True/False as a hostile 0
            if isinstance(value, bool):
                return False
            return isinstance(value, int) and value == 0

        if isinstance(model, _BaseModel) and isinstance(dumped, dict):
            fields = type(model).model_fields
            result: Dict[str, Any] = {}
            for key, value in dumped.items():
                if key not in fields:
                    # Unknown/extra key (e.g. hook passthrough) — keep as-is.
                    result[key] = value
                    continue
                child = getattr(model, key, None)
                if isinstance(child, _BaseModel) or isinstance(value, (dict, list)):
                    result[key] = ConfigurationRecord._omit_rollback_hostile_defaults(
                        child, value
                    )
                elif value == fields[key].default and _hostile(value):
                    # Omit: equals default AND is a prior-model-breaking sentinel.
                    continue
                else:
                    result[key] = value
            return result

        if isinstance(dumped, list):
            # Lists of sub-models: recurse element-wise when we have the models.
            if isinstance(model, list) and len(model) == len(dumped):
                return [
                    ConfigurationRecord._omit_rollback_hostile_defaults(m, d)
                    for m, d in zip(model, dumped)
                ]
            return dumped

        return dumped

    @staticmethod
    def _stringify_values(obj: Any) -> Any:
        """
        Recursively convert values to strings for DynamoDB storage.

        Strategy:
        - Preserve booleans as native bool (CRITICAL - string "False" is truthy in Python)
        - Preserve None as NULL
        - Convert numbers to strings (avoids Decimal conversion issues)
        - Recursively process dicts and lists

        Args:
            obj: Value to stringify

        Returns:
            Stringified value suitable for DynamoDB storage
        """
        # Preserve None (NULL type in DynamoDB)
        if obj is None:
            return None

        # Preserve booleans (BOOL type in DynamoDB)
        # CRITICAL: MUST check bool before int, since bool is subclass of int
        # Booleans must stay native because string "False" evaluates as truthy
        elif isinstance(obj, bool):
            return obj

        # Recursively process dicts (M type in DynamoDB)
        elif isinstance(obj, dict):
            return {k: ConfigurationRecord._stringify_values(v) for k, v in obj.items()}

        # Recursively process lists (L type in DynamoDB)
        elif isinstance(obj, list):
            return [ConfigurationRecord._stringify_values(item) for item in obj]

        # Convert everything else to string (numbers, Decimals, custom objects, etc.)
        else:
            return str(obj)
