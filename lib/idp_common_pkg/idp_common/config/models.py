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

import re
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from typing_extensions import Self

# Current config schema/shape version. Bump when the stored config shape changes
# in a way that requires a migration (see config/migrations/). v0.6 folded the
# top-level `assessment` block into `extraction.confidence` / `extraction.geometry`
# and introduced the top-level `hitl` block. v0.7 moved
# `extraction.agentic.validation` up to `extraction.validation`, because simple
# extraction now runs the same validate-and-retry path and the knob is no longer
# agentic-only.
CONFIG_FORMAT_VERSION = "0.7"


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
        """Parse dimensions from string or number, treating empty strings as None"""
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        if isinstance(v, str):
            try:
                return int(v) if v else None
            except ValueError:
                return None  # Invalid value, return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

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
    def parse_float(cls, v: Any) -> float:
        """Parse float from string or number"""
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)

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
            "is roughly a tenth of the token count. It does NOT reduce shard "
            "count: plan_shards budgets against OCR page text only, and "
            "compute_sizing_plan never subtracts prompt overhead — it is absorbed "
            "by the blanket context_buffer, so the reclaimed tokens come off a "
            "reserve that was already unused (#775). Measured on the benchmark "
            "suite: no completeness or accuracy cost, and no measurable benefit "
            "either. Treat it as a neutral instrument, not an optimisation."
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
        description="Page-count ceiling per shard when max_concurrent_batches "
        "> 1. A shard is closed once it holds this many pages even if its OCR "
        "text is under the token budget. This is the TIMEOUT-critical lever "
        "(fewer pages/shard = fewer sequential agent turns = each shard Lambda "
        "finishes well under 900s), so it stays a small fixed default (5) rather "
        "than model-derived — a roomy token budget must NOT collapse a large doc "
        "back into one giant shard. 0 = disabled (token budget alone bounds "
        "shards; not recommended for large docs).",
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
            "improvement and is gated on a measured win. Routes that cannot carry "
            "a toolConfig (a custom Lambda hook, GPT-5.x) fall back to the prompt "
            "automatically, and the reason is recorded in the section metadata. "
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
    onError: str = Field(  # noqa: N815 — matches stored config key
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
    onError: str = Field(  # noqa: N815 — matches stored config key
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
            "saving a model call — the standalone step is bypassed)."
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
            "low, medium, high, or xhigh (via reasoning.effort, NOT max). "
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
            "Max list rows assessed per inference in the in-shard assessment path "
            "(agentic extraction). A single assessment call over a large list (e.g. "
            "75 transaction rows) is unreliable — the model under-enumerates or omits "
            "the list, leaving rows unassessed. When a shard's extracted list exceeds "
            "this size, the assessment is run in batches of this many rows and "
            "concatenated, so every row gets a confidence. Lower = more reliable "
            "enumeration but more inferences; raise for capable models. NOTE: this is "
            "an UPPER bound — the self-healing ladder derives a smaller token-aware "
            "first-pass size when the confidence model's output cap would truncate it."
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
    def parse_float(cls, v: Any) -> float:
        """Parse float from string or number"""
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)

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
            "How field bounding boxes are produced. 'ocr_only' (default): DO NOT "
            "ask the model for boxes — derive geometry purely by matching each "
            "extracted value to real OCR lines (pageData.json), disambiguating "
            "repeated values by row order. Cheaper and more accurate than "
            "LLM-estimated boxes. 'llm_grounded': the model emits boxes and OCR "
            "grounding refines them. 'llm': use the model's boxes as-is with no "
            "grounding. 'off': no geometry is produced at all."
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

    @field_validator("confidence_threshold", mode="before")
    @classmethod
    def parse_float(cls, v: Any) -> float:
        """Parse float from string or number"""
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)


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
            "so you don't hand-set per-model sizes. Raise it (e.g. 0.5) if you see "
            "context-overflow or truncation; lower it (e.g. 0.15) to pack more per "
            "shard/batch on a roomy model. The derived sizes are logged and shown "
            "in the processing report."
        ),
    )
    model: str = Field(
        default="us.amazon.nova-pro-v1:0",
        description="Bedrock model ID for extraction. Use 'LambdaHook' to invoke a custom Lambda function instead of Bedrock.",
    )
    model_lambda_hook_arn: Optional[str] = Field(
        default=None,
        description="Lambda function ARN for custom inference (used when model is 'LambdaHook'). Function name must start with GENAIIDP-.",
    )
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
            "low, medium, high, or xhigh (via reasoning.effort, NOT max). "
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
    def parse_float(cls, v: Any) -> float:
        """Parse float from string or number"""
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)

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
    """How classification reports confidence in the CLASS it chose (v0.7).

    Nested as ``classification.confidence``. Deliberately NOT the same thing as
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
            "Instruction block spliced into classification.task_prompt in 'topk' "
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
            "Instruction block spliced into classification.task_prompt in "
            "'verbalized' mode (populated from system defaults), on the same "
            "splice rules as task_prompt_topk."
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
                "classification.confidence.mode must be 'off', 'topk', or "
                f"'verbalized', got {v!r}"
            )
        return v_str

    @field_validator("top_k_candidates", mode="before")
    @classmethod
    def parse_top_k(cls, v: Any) -> int:
        """Parse from a string (stored configs are string-typed) or number."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return 3
        return int(v)


class ClassificationConfig(BaseModel):
    """Document classification configuration"""

    postHook: List[PipelineHook] = Field(  # noqa: N815 — matches stored config key
        default_factory=list,
        description="Pipeline hooks invoked after classification (Feature Platform)",
    )
    model: str = Field(
        default="us.amazon.nova-pro-v1:0",
        description="Bedrock model ID for classification. Use 'LambdaHook' to invoke a custom Lambda function instead of Bedrock.",
    )
    model_lambda_hook_arn: Optional[str] = Field(
        default=None,
        description="Lambda function ARN for custom inference (used when model is 'LambdaHook'). Function name must start with GENAIIDP-.",
    )
    system_prompt: str = Field(
        default="", description="System prompt for classification"
    )
    task_prompt: str = Field(
        default="", description="Task prompt template for classification"
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
            "low, medium, high, or xhigh (via reasoning.effort, NOT max). "
            "Ignored by models "
            "without an effort control (Nova, Sonnet 4.5, Haiku 4.5)."
        ),
    )
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        description="Optional cap on output tokens. Leave empty to use the selected model's maximum output limit (recommended). If set, it must not exceed the model's limit.",
    )
    maxPagesForClassification: str = Field(
        default="ALL",
        description="Max pages to use for classification. 'ALL' = all pages, or a number to limit to N pages",
    )
    classificationMethod: str = Field(default="multimodalPageLevelClassification")
    sectionSplitting: str = Field(
        default="llm_determined",
        description="Section splitting strategy: 'disabled' (entire doc as one section), 'page' (one section per page), 'llm_determined' (use LLM boundary detection)",
    )
    contextPagesCount: int = Field(
        default=0,
        description="Number of pages before/after target page to include as context for multimodalPageLevelClassification. "
        "0=no context (default), 1=include 1 page on each side, 2=include 2 pages on each side.",
    )
    enforceValidClasses: bool = Field(
        default=True,
        description="When True, validate the predicted class against the configured "
        "class vocabulary and retry (re-prompting the model) on out-of-vocabulary "
        "predictions. When False, an out-of-vocabulary prediction is logged and used "
        "as-is (legacy behavior). Applies to multimodalPageLevelClassification.",
    )
    maxValidationRetries: int = Field(
        default=2,
        ge=0,
        description="Maximum number of re-prompt retries when the predicted class is "
        "not in the configured class vocabulary. Only used when enforceValidClasses "
        "is True.",
    )
    invalidClassFallback: str = Field(
        default="unclassified",
        description="Class label assigned when all validation retries are exhausted. "
        "Should be one of the configured classes or the built-in 'unclassified'. "
        "Only used when enforceValidClasses is True.",
    )
    confidence: ClassificationClassConfidenceConfig = Field(
        default_factory=ClassificationClassConfidenceConfig,
        description=(
            "Confidence in the CLASS (see ClassificationClassConfidenceConfig). "
            "Defaults to 'topk'; unrelated to extraction.confidence, which scores "
            "extracted fields."
        ),
    )
    image: ImageConfig = Field(default_factory=ImageConfig)

    @field_validator("temperature", "top_p", "top_k", mode="before")
    @classmethod
    def parse_float(cls, v: Any) -> float:
        """Parse float from string or number"""
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)

    @field_validator("max_tokens", mode="before")
    @classmethod
    def parse_int(cls, v: Any) -> Optional[int]:
        """Parse optional max_tokens (empty/0 -> None = use model max)."""
        return _parse_optional_max_tokens(v)

    @field_validator("maxPagesForClassification", mode="before")
    @classmethod
    def parse_max_pages(cls, v: Any) -> str:
        """Parse maxPagesForClassification - accepts 'ALL' or numeric string/int.

        Converts legacy value of 0 to 'ALL' for backward compatibility.
        Returns string to match UI schema enum: ['ALL', '1', '2', '3', '5', '10']
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return "ALL"
        if isinstance(v, (int, float)):
            # Convert legacy 0 to "ALL" for backward compatibility
            if v <= 0:
                return "ALL"
            return str(int(v))
        if isinstance(v, str):
            v_upper = v.strip().upper()
            # "ALL" or legacy "0" both mean all pages
            if v_upper == "ALL" or v_upper == "0":
                return "ALL"
            return v.strip()
        return str(v)

    @field_validator("sectionSplitting", mode="before")
    @classmethod
    def validate_section_splitting(cls, v: Any) -> str:
        """Validate and normalize section splitting value"""
        import logging

        logger = logging.getLogger(__name__)

        if isinstance(v, str):
            v = v.lower().strip()

        valid_values = ["disabled", "page", "llm_determined"]
        if v not in valid_values:
            logger.warning(
                f"Invalid sectionSplitting value '{v}', using default 'llm_determined'. "
                f"Valid values: {', '.join(valid_values)}"
            )
            return "llm_determined"
        return v

    @field_validator("contextPagesCount", mode="before")
    @classmethod
    def parse_context_pages_count(cls, v: Any) -> int:
        """Parse contextPagesCount from string or number, ensuring non-negative value"""
        if isinstance(v, str):
            v = int(v) if v else 0
        result = int(v)
        if result < 0:
            return 0
        return result

    @field_validator("maxValidationRetries", mode="before")
    @classmethod
    def parse_max_validation_retries(cls, v: Any) -> int:
        """Parse maxValidationRetries from string or number, ensuring non-negative value"""
        if isinstance(v, str):
            v = int(v) if v.strip() else 2
        result = int(v)
        if result < 0:
            return 0
        return result

    @field_validator("enforceValidClasses", mode="before")
    @classmethod
    def parse_enforce_valid_classes(cls, v: Any) -> bool:
        """Parse enforceValidClasses from string or bool (config may store as string)"""
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on")
        return bool(v)


class SummarizationConfig(BaseModel):
    """Document summarization configuration"""

    postHook: List[PipelineHook] = Field(  # noqa: N815 — matches stored config key
        default_factory=list,
        description="Pipeline hooks invoked after summarization (Feature Platform)",
    )
    enabled: bool = Field(default=True, description="Enable summarization")
    model: str = Field(
        default="us.amazon.nova-premier-v1:0",
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
            "low, medium, high, or xhigh (via reasoning.effort, NOT max). "
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
    def parse_float(cls, v: Any) -> float:
        """Parse float from string or number"""
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)

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
            "low, medium, high, or xhigh (via reasoning.effort, NOT max). "
            "Ignored by models "
            "without an effort control (Nova, Sonnet 4.5, Haiku 4.5)."
        ),
    )

    @field_validator("temperature", "top_p", "top_k", mode="before")
    @classmethod
    def parse_float(cls, v: Any) -> float:
        """Parse float from string or number"""
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)

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
            "low, medium, high, or xhigh (via reasoning.effort, NOT max). "
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
    def parse_max_workers(cls, v: Any) -> int:
        """Parse max_workers from string or number"""
        if isinstance(v, str):
            return int(v) if v else 20
        return int(v)


class ErrorAnalyzerParameters(BaseModel):
    """Error analyzer parameters configuration"""

    max_log_events: int = Field(
        default=5, gt=0, description="Maximum number of log events to retrieve"
    )
    time_range_hours_default: int = Field(
        default=24, gt=0, description="Default time range in hours for log searches"
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
        description="How far back the error analyzer searches logs, traces, and execution history (in hours). Default: 24.",
    )

    @field_validator("lookback_hours", mode="before")
    @classmethod
    def parse_lookback_hours(cls, v: Any) -> int:
        """Parse lookback_hours from string or number"""
        if isinstance(v, str):
            return int(v) if v else 24
        return int(v)

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
- Document-specific analysis (user provides a filename or document ID):
  → Use cloudwatch_document_logs and dynamodb_status as primary tools
- System-wide or batch analysis (no specific document):
  → Use cloudwatch_logs and dynamodb_query to identify patterns
- Workflow failures and execution timeline:
  → Use stepfunction_details for the execution event history
- Lambda configuration and environment context:
  → Use lambda_lookup to check timeout settings, memory, and environment variables
- Which Bedrock model a pipeline stage uses (e.g. after a model error):
  → Use fetch_pipeline_configuration with the document's config version to read the per-stage model IDs
- Distributed service interaction issues:
  → Use xray_trace or xray_performance_analysis

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
- Document-specific analysis (user provides a filename or document ID):
  → Use cloudwatch_document_logs and dynamodb_status as primary tools
- System-wide or batch analysis (no specific document):
  → Use cloudwatch_logs and dynamodb_query to identify patterns
- Workflow failures and execution timeline:
  → Use stepfunction_details for the execution event history
- Lambda configuration and environment context:
  → Use lambda_lookup to check timeout settings, memory, and environment variables
- Which Bedrock model a pipeline stage uses (e.g. after a model error):
  → Use fetch_pipeline_configuration with the document's config version to read the per-stage model IDs
- Distributed service interaction issues:
  → Use xray_trace or xray_performance_analysis

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

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a mutable dictionary."""
        return self.model_dump(mode="python")


class FactExtractionConfig(BaseModel):
    """Fact extraction configuration for rule validation"""

    model: str = Field(
        default="us.anthropic.claude-3-5-sonnet-20240620-v1:0",
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
    def parse_float(cls, v: Any) -> float:
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)

    @field_validator("max_tokens", mode="before")
    @classmethod
    def parse_int(cls, v: Any) -> Optional[int]:
        """Parse optional max_tokens (empty/0 -> None = use model max)."""
        return _parse_optional_max_tokens(v)


class RuleValidationOrchestratorConfig(BaseModel):
    """Rule validation summarization configuration"""

    model: str = Field(
        default="us.anthropic.claude-3-5-sonnet-20240620-v1:0",
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
    def parse_float(cls, v: Any) -> float:
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)

    @field_validator("max_tokens", mode="before")
    @classmethod
    def parse_int(cls, v: Any) -> Optional[int]:
        """Parse optional max_tokens (empty/0 -> None = use model max)."""
        return _parse_optional_max_tokens(v)


class Z3RuleTranslatorConfig(BaseModel):
    """Z3 engine: translates natural language rules to SMT-LIB RuleJSON"""

    model: str = Field(
        default="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
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
            "low, medium, high, or xhigh (via reasoning.effort, NOT max). "
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
    model: str = Field(
        default="us.anthropic.claude-3-haiku-20240307-v1:0",
        description="Bedrock model ID for evaluation",
    )
    system_prompt: str = Field(
        default="ou are an evaluator that helps determine if the predicted and expected values match for document attribute extraction. You will consider the context and meaning rather than just exact string matching.",
        description="System prompt for evaluation",
    )

    @field_validator("temperature", "top_p", "top_k", mode="before")
    @classmethod
    def parse_float(cls, v: Any) -> float:
        """Parse float from string or number"""
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)

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
    def parse_float(cls, v: Any) -> float:
        """Parse float from string or number"""
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)

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
    def parse_float(cls, v: Any) -> float:
        """Parse float from string or number"""
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)

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
    def parse_float(cls, v: Any) -> float:
        """Parse float from string or number"""
        if isinstance(v, str):
            return float(v) if v else 0.0
        return float(v)

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
    preprocessing: PreprocessingConfig = Field(
        default_factory=PreprocessingConfig,
        description="Preprocessing configuration — home of the standalone "
        "`preprocessing` pipeline-hook point (runs first, before BDA/pipeline "
        "routing). Used by the PII Anonymization extension.",
    )
    ocr: OCRConfig = Field(default_factory=OCRConfig, description="OCR configuration")
    classification: ClassificationConfig = Field(
        default_factory=lambda: ClassificationConfig(model="us.amazon.nova-pro-v1:0"),
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
        default_factory=lambda: SummarizationConfig(
            model="us.amazon.nova-premier-v1:0"
        ),
        description="Summarization configuration",
    )
    chat: ChatConfig = Field(
        default_factory=ChatConfig,
        description="Chat-with-Document configuration (used by the interactive "
        "document Q&A feature in the Web UI)",
    )
    rule_validation: RuleValidationConfig = Field(
        default_factory=lambda: RuleValidationConfig(
            model="us.anthropic.claude-3-5-sonnet-20240620-v1:0"
        ),
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

            # Migrate rule_classes → policy_classes (renamed in v0.5.9)
            if "rule_classes" in data and "policy_classes" not in data:
                data["policy_classes"] = data.pop("rule_classes")
                logger.info("Migrated config key 'rule_classes' → 'policy_classes'")
            elif "rule_classes" in data:
                # Both keys present: policy_classes wins and rule_classes is
                # dropped. Say so loudly — this discards user-supplied rules, and
                # because 'rule_classes' is a known-deprecated key it does not
                # trip the unknown-field warning either. Silently losing it is
                # how hand-written and notebook-produced configs ended up with
                # rule validation that never fired.
                discarded = data.get("rule_classes")
                count = len(discarded) if isinstance(discarded, (list, dict)) else 1
                logger.warning(
                    "Both 'rule_classes' (deprecated) and 'policy_classes' are "
                    "present in this configuration; DISCARDING 'rule_classes' "
                    "(%d %s). 'rule_classes' was renamed to 'policy_classes' in "
                    "v0.5.9 — merge these entries into 'policy_classes' or they "
                    "will not be used.",
                    count,
                    "entry" if count == 1 else "entries",
                )
                del data["rule_classes"]

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

        return data

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
