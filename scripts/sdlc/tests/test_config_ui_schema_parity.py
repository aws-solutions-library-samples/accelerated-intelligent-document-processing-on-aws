# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every ``IDPConfig`` field must have a UI control — or a written-down reason.

The configuration editor is fully schema-driven (``ConfigBuilder.tsx`` renders
from ``schema.properties``), but that schema is hardcoded inline in
CloudFormation — ``patterns/unified/template.yaml``, the ``UpdateSchemaConfig``
custom resource's ``Schema:`` block — completely separate from the Pydantic
models in ``idp_common/config/models.py``. Nothing tied the two together, so a
new knob could ship fully functional via YAML/CLI with every test green and be
invisible and unsettable in the UI. That happened to ``extraction.coercion``,
and was caught only by someone manually asking "where does this appear in the
UI?" (GitHub #707).

Exact bidirectional parity is not achievable: some config fields are genuinely
not user-settable (derived, machine-written, metadata), and the schema carries
real UI-only structure (the free-form ``classes`` document schema). So this is
an allowlist gate, not an equality assertion: a field must have a control, or
appear in :data:`NOT_USER_SETTABLE` / :data:`EXEMPT_SUBTREES` /
:data:`KNOWN_UI_GAPS` with a one-line reason. A new field lands in none of
those and the build fails — the gap becomes a deliberate, reviewed decision
instead of an invisible one.

The two exception vocabularies are deliberately distinct:

* :data:`NOT_USER_SETTABLE` / :data:`EXEMPT_SUBTREES` — "there should never be a
  control here" (derived from another field, written by a machine, record
  metadata, notebook-only, or an expert override of an auto-sized value).
* :data:`KNOWN_UI_GAPS` — "there arguably should be a control here and there
  isn't." Accepted for now, listed so it is visible. Shrinking this dict is
  progress; a reviewer should push back on anything new landing in it.

:func:`test_no_stale_exceptions` keeps all three honest: once a control is
added, the entry must be deleted or the build fails.

This extends the sibling sweep in ``test_config_schema_order.py`` (same
templates, same short-form-intrinsic loader, same guard-the-guard discipline)
and deliberately reuses its loader rather than adding a second one — see that
module's docstring and ``test_cfn_loader_safety.py``.
"""

from __future__ import annotations

import importlib
import types
import typing
from typing import Any

import pytest

pytest.importorskip("yaml")
pydantic = pytest.importorskip("pydantic")

# conftest.py puts lib/idp_common_pkg on sys.path.
_models = pytest.importorskip("idp_common.config.models")
BaseModel = pydantic.BaseModel
IDPConfig = _models.IDPConfig


def _schemas():
    """The sibling sweep's template loader + ``Schema:`` discovery, reused.

    Imported lazily (as ``test_cfn_loader_safety.py`` does) because the tests
    directory only lands on ``sys.path`` once pytest starts importing modules
    from it — a module-level import here fails when this file is run on its own.
    """
    return importlib.import_module("test_config_schema_order")._schemas()


#: The template whose ``Schema:`` block drives the configuration editor. The
#: root ``template.yaml`` has no ConfigSchema, but ``_schemas()`` scans both, so
#: filter rather than assume.
_UI_SCHEMA_TEMPLATE = "patterns/unified/template.yaml"

#: Processing steps that carry a ``postHook`` list. Kept as data so the
#: per-stage hook exceptions below are generated rather than copy-pasted.
_HOOK_STAGES = (
    "ocr",
    "classification",
    "extraction",
    "summarization",
    "rule_validation",
)


# ---------------------------------------------------------------------------
# Exceptions — every entry carries its reason.
# ---------------------------------------------------------------------------

#: Whole subtrees that should never have controls. Matches the path itself and
#: anything beneath it, so a field added inside inherits the same reason —
#: correct here because the reason is a property of the subtree, not the field.
EXEMPT_SUBTREES: dict[str, str] = {
    "pricing": (
        "Pricing has its own admin page (DefaultPricing/CustomPricing records); "
        "a second writer in the config form would fight it."
    ),
    "agents.error_analyzer.parameters": (
        "Error Analyzer fetch/X-Ray/cache limits — SRE tuning, YAML-only; the UI "
        "exposes model_id, lookback_hours and the system prompt."
    ),
    "agents.chat_companion.parameters": (
        "Same ErrorAnalyzerParameters block reused by the chat companion; the UI "
        "exposes only its model_id."
    ),
    "rule_validation.z3_rule_translator": (
        "Notebook/standalone only — the deployed pipeline uses the packaged "
        "translator_config.yaml via handle_generate_rule_json (see the field's "
        "own description)."
    ),
    "rule_validation.z3_value_extraction": (
        "Notebook/standalone only — the deployed pipeline uses "
        "orchestrator._extract_z3_values_from_facts with fact_extraction settings."
    ),
}

#: Individual fields that should never have a control.
NOT_USER_SETTABLE: dict[str, str] = {
    # --- record plumbing / metadata -------------------------------------
    "config_type": "Record discriminator written by the config layer, not a setting.",
    "config_format_version": (
        "Migration stamp maintained by config/migrations; hand-editing it would "
        "skip or re-run a migration."
    ),
    "managed": "Set by the stack to mark configs that stack updates overwrite.",
    "test_set": "Documentation/reference label only (per the field description).",
    "summary": "Free-form rule-validation result payload, not a form field.",
    "policy_classes": (
        "Machine-written by Policy/Rules Discovery (discovery/rules_discovery.py "
        "saves into it); edited through Policy Discovery, not the generic form."
    ),
    "rule_validation.extraction_results": (
        "Runtime data carrier (extraction results injected into rule prompts), "
        "not a setting."
    ),
    # --- derived from a user-facing field --------------------------------
    "extraction.agentic.enabled": (
        "DERIVED from the user-facing extraction.mode ('advanced' -> on) by "
        "ExtractionConfig.reconcile_mode_and_agentic; the UI exposes mode."
    ),
    "extraction.confidence.enabled": (
        "DERIVED from extraction.confidence.mode (enabled = mode != 'off') by "
        "ConfidenceConfig.derive_enabled_from_mode; the UI exposes mode."
    ),
    # --- auto-sized values whose field is only an escape hatch -----------
    "extraction.context_buffer": (
        "Context headroom fraction consumed by idp_common.bedrock.sizing to "
        "auto-derive shard/batch sizes; expert override, not a user setting."
    ),
    # nosec B105 — "shard_token_budget" refers to LLM context tokens (Bedrock
    # sizing), not an authentication token; the value is a descriptive string,
    # not a credential.
    "extraction.agentic.shard_token_budget": (
        "0 = auto-size from the model's context window (bedrock/sizing.py); a "
        "non-zero value only pins what is already derived."
    ),
    "extraction.agentic.max_images_per_agent": (
        "Backstop cap against oversized first turns; per-shard sharding already "
        "bounds image count."
    ),
    # --- deliberately hidden / deployment-selected -----------------------
    "extraction.agentic.integrated_confidence_strategy": (
        "Marked HIDDEN/EXPERIMENTAL in its own description — an A/B knob for "
        "cost vs. confidence calibration, not a shipped choice."
    ),
    "extraction.agentic.runtime": (
        "Orchestration backend (in-process asyncio vs the nested Step Functions "
        "Distributed Map), resolved config > EXTRACTION_RUNTIME env var > "
        "default; the deployed stack picks it, not the user."
    ),
    "ocr.bda_project_arn": (
        "The stack provisions a per-stack BDA OCR project and injects the ARN via "
        "BDA_OCR_PROJECT_ARN; hand-setting it is an override."
    ),
    "agents.error_analyzer.error_patterns": (
        "Internal log-scan regex list; a free-form regex array is not a safe "
        "form control."
    ),
    "agents.chat_companion.error_patterns": (
        "Internal log-scan regex list, as for the error analyzer."
    ),
    # --- shared ImageConfig, only meaningful at OCR time -----------------
    # ImageConfig is reused for every stage's image panel, but ocr/service.py is
    # the only reader of dpi/preprocessing (they govern PDF->raster rendering,
    # which happens once, in OCR). The per-stage panels correctly expose just
    # target_width/target_height.
    "classification.image.dpi": "PDF-rendering knob; only read from ocr.image.",
    "classification.image.preprocessing": (
        "Binarization is applied during OCR rasterization; only read from ocr.image."
    ),
    "extraction.image.dpi": "PDF-rendering knob; only read from ocr.image.",
    "extraction.image.preprocessing": (
        "Binarization is applied during OCR rasterization; only read from ocr.image."
    ),
    "extraction.confidence.image.dpi": "PDF-rendering knob; only read from ocr.image.",
    "extraction.confidence.image.preprocessing": (
        "Binarization is applied during OCR rasterization; only read from ocr.image."
    ),
}

# Pipeline-hook entries: the UI hook editor exposes the admin-managed fields
# (featureId/arn/order/onError/enabled). `args` is the registering feature's own
# payload, written by the Feature Platform.
for _stage in _HOOK_STAGES:
    NOT_USER_SETTABLE[f"{_stage}.postHook.args"] = (
        "Hook-owned key/value payload written by the registering feature; the "
        "pipeline stays hook-agnostic."
    )
del _stage

#: Fields that arguably SHOULD have a control and do not. Accepted, not
#: endorsed — shrinking this dict is progress.
KNOWN_UI_GAPS: dict[str, str] = {
    "extraction.confidence.escalation_enabled": (
        "Confidence self-healing ladder is ON by default but tunable only via "
        "YAML — no control in the Confidence Assessment section."
    ),
    "extraction.confidence.escalation_model": (
        "Ladder escalation model has no picklist, unlike every other model field."
    ),
    "extraction.confidence.max_escalation_rounds": (
        "Cost/latency bound on the ladder; YAML-only alongside the two above."
    ),
    "extraction.agentic.review_agent": (
        "Agentic self-review pass is YAML-only; not surfaced in Advanced "
        "extraction settings."
    ),
    "extraction.agentic.review_agent_model": (
        "Defaults to extraction.model; YAML-only with review_agent above."
    ),
    "discovery.rules.agentic": (
        "Agentic rule discovery has no UI control; the Policy Discovery section "
        "exposes only the non-agentic model/prompt fields."
    ),
    "discovery.rules.agentic.enabled": "Part of the unexposed agentic rule discovery block.",
    "discovery.rules.agentic.review_agent": "Part of the unexposed agentic rule discovery block.",
    "discovery.rules.agentic.review_agent_model": (
        "Part of the unexposed agentic rule discovery block."
    ),
    "discovery.multi_document.system_prompt": (
        "Cluster-analysis system prompt; the Multi-Document Discovery section "
        "exposes every other knob in the block but not this prompt."
    ),
    "agents.chat_companion.system_prompt": (
        "Chat Companion exposes only model_id, while the sibling Error Analyzer "
        "does expose its system prompt — an inconsistency, not a design."
    ),
    "ocr.max_workers": "OCR concurrency; YAML-only.",
    "summarization.max_extraction_array_items": (
        "Prompt array-elision cap; YAML-only."
    ),
    "rule_validation.overlap_percentage": "Chunking internal; YAML-only.",
    "rule_validation.token_size": "Chunking internal (chars per token); YAML-only.",
    "rule_validation.response_prefix": "Response marker; YAML-only.",
    "rule_validation.z3_timeout_ms": "Z3 solver timeout; YAML-only.",
}

for _stage in _HOOK_STAGES:
    KNOWN_UI_GAPS[f"{_stage}.postHook.allowDocumentUpdate"] = (
        "Admin-relevant observe-only pin for a hook, but the hook editor does "
        "not offer it."
    )
del _stage

#: Schema paths with no ``IDPConfig`` field. ``IDPConfig`` is ``extra="ignore"``,
#: so anything here that is NOT backed by free-form config is a control whose
#: value is silently dropped — hence each entry states why it is safe.
UI_ONLY_SUBTREES: dict[str, str] = {
    "classes": (
        "Document-class definitions are free-form (List[Dict[str, Any]]) JSON "
        "Schema authored in the Schema Designer; the CFN schema describes that "
        "shape for the editor, the Pydantic model deliberately does not."
    ),
    "preprocessing.args": (
        "FlatHookConfig.args is List[Dict[str, Any]]; the schema describes the "
        "{key, value} row shape for the editor."
    ),
    "postprocessing.args": (
        "FlatHookConfig.args is List[Dict[str, Any]]; the schema describes the "
        "{key, value} row shape for the editor."
    ),
    "discovery.output_format": (
        "DEAD KNOB (GitHub #707 follow-up): rendered by the UI and present in "
        "base-discovery.yaml, but DiscoveryConfig has no output_format field so "
        "extra='ignore' drops it, and classes_discovery._sample_output_format() "
        "hardcodes the sample instead of reading config. The default is also "
        "stale (pre-JSON-Schema groups/attributeType shape). Left in place "
        "pending a decision to wire it up or remove it."
    ),
}


# ---------------------------------------------------------------------------
# Walkers
# ---------------------------------------------------------------------------


def _nested_models(annotation: Any) -> list[type]:  # noqa: ANN401
    """Pydantic models reachable from an annotation, through Optional/list/etc.

    ``Dict[...]`` is NOT descended into: a mapping annotation declares no field
    names, so there is nothing the UI could be expected to render.
    """
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType) or origin in (
        list,
        set,
        tuple,
        frozenset,
    ):
        found: list[type] = []
        for arg in typing.get_args(annotation):
            found.extend(_nested_models(arg))
        return found
    if origin is dict:
        return []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return []


def _config_field_paths(
    model: type = IDPConfig, prefix: str = "", seen: tuple[type, ...] = ()
) -> dict[str, str]:
    """Dotted path -> owning model name for every field reachable from ``model``."""
    paths: dict[str, str] = {}
    for name, field in model.model_fields.items():
        path = name if not prefix else f"{prefix}.{name}"
        paths[path] = model.__name__
        for nested in _nested_models(field.annotation):
            if nested not in seen:
                paths.update(_config_field_paths(nested, path, (*seen, nested)))
    return paths


def _ui_schema_paths(node: Any, prefix: str = "") -> set[str]:  # noqa: ANN401
    """Dotted config paths the editor renders a control for.

    A ``ghostGroup: true`` object is a purely visual grouping — ``ConfigBuilder``
    renders its children at the PARENT path — so it contributes no path of its
    own and its children are flattened up. ``items`` is followed so an array-of-
    objects control maps onto the list's item model.
    """
    found: set[str] = set()
    if not isinstance(node, dict):
        return found
    properties = node.get("properties")
    if isinstance(properties, dict):
        for name, sub in properties.items():
            if not isinstance(sub, dict):
                found.add(f"{prefix}.{name}" if prefix else name)
                continue
            is_ghost = sub.get("ghostGroup") in (True, "true")
            path = prefix if is_ghost else (f"{prefix}.{name}" if prefix else name)
            if not is_ghost:
                found.add(path)
            found |= _ui_schema_paths(sub, path)
            items = sub.get("items")
            if isinstance(items, dict):
                found |= _ui_schema_paths(items, path)
    return found


def _ui_paths() -> set[str]:
    paths: set[str] = set()
    for label, schema in _schemas():
        if _UI_SCHEMA_TEMPLATE in label:
            paths |= _ui_schema_paths(schema)
    return paths


def _exempt_reason(path: str) -> str | None:
    if path in NOT_USER_SETTABLE:
        return NOT_USER_SETTABLE[path]
    if path in KNOWN_UI_GAPS:
        return KNOWN_UI_GAPS[path]
    for subtree, reason in EXEMPT_SUBTREES.items():
        if path == subtree or path.startswith(f"{subtree}."):
            return reason
    return None


def _ui_only_reason(path: str) -> str | None:
    for subtree, reason in UI_ONLY_SUBTREES.items():
        if path == subtree or path.startswith(f"{subtree}."):
            return reason
    return None


# ---------------------------------------------------------------------------
# Guard the guard
# ---------------------------------------------------------------------------


def test_the_ui_schema_is_discovered():
    """A loader/template change must not silently make this suite vacuous."""
    paths = _ui_paths()
    assert paths, (
        f"no ConfigSchema properties found in {_UI_SCHEMA_TEMPLATE} — every "
        "parity assertion below would pass or fail for the wrong reason"
    )
    # Sanity floor: the editor renders hundreds of controls.
    assert len(paths) > 200, (
        f"only {len(paths)} UI paths discovered: {sorted(paths)[:20]}"
    )
    for expected in ("extraction.model", "ocr.backend", "summarization.enabled"):
        assert expected in paths, (expected, len(paths))


def test_the_config_model_is_walked():
    """Ditto for the Pydantic side."""
    paths = _config_field_paths()
    assert len(paths) > 200, f"only {len(paths)} config fields walked"
    for expected in (
        "extraction.model",
        "extraction.agentic.table_parsing.enabled",
        # Nested-model recursion (three levels deep) and list-item models must
        # both be walked, or whole subtrees would silently escape the gate.
        "extraction.validation.min_population_ratio",
        "extraction.coercion.enabled",
        "ocr.features.name",
    ):
        assert expected in paths, (expected, len(paths))


def test_ghost_groups_are_flattened_not_dropped():
    """`model_params` is a ghostGroup: its children save at the PARENT path.

    If that flattening breaks, ~30 real controls (every temperature/top_p/
    max_tokens/reasoning_effort) look missing at once and the exceptions list
    would get padded to compensate. Assert the flattening directly.
    """
    paths = _ui_paths()
    for flattened in (
        "extraction.temperature",
        "classification.max_tokens",
        "summarization.reasoning_effort",
        "chat.top_p",
        "evaluation.llm_method.top_k",
    ):
        assert flattened in paths, (
            f"{flattened} is not a discovered UI path — ghostGroup flattening "
            "(ConfigBuilder's childPath) is no longer being honored here"
        )
    assert "extraction.model_params" not in paths, (
        "a ghostGroup key must not contribute a path of its own — it does not "
        "exist in the saved config"
    )


# ---------------------------------------------------------------------------
# The parity gate
# ---------------------------------------------------------------------------


def test_every_config_field_has_a_ui_control_or_a_written_reason():
    config_paths = _config_field_paths()
    ui_paths = _ui_paths()

    unexplained = [
        f"{path}  (declared on {owner})"
        for path, owner in sorted(config_paths.items())
        if path not in ui_paths and _exempt_reason(path) is None
    ]
    assert not unexplained, (
        "These IDPConfig fields have no control in the CFN-embedded UI schema "
        f"({_UI_SCHEMA_TEMPLATE} -> UpdateSchemaConfig -> Schema), so they are "
        "unsettable in the configuration editor:\n  "
        + "\n  ".join(unexplained)
        + "\n\nAdd the field to that Schema block (with a unique sibling `order`), "
        "or record it in NOT_USER_SETTABLE / EXEMPT_SUBTREES / KNOWN_UI_GAPS in "
        "this file with a one-line reason."
    )


def test_every_ui_control_maps_to_a_config_field():
    """IDPConfig is ``extra="ignore"``, so an unbacked control silently no-ops."""
    config_paths = _config_field_paths()
    unexplained = sorted(
        path
        for path in _ui_paths()
        if path not in config_paths and _ui_only_reason(path) is None
    )
    assert not unexplained, (
        "These UI schema paths have no IDPConfig field. IDPConfig is "
        "extra='ignore', so whatever a user types into them is dropped the next "
        "time the config round-trips (Save-as-Version, updateConfiguration, "
        "migration):\n  "
        + "\n  ".join(unexplained)
        + "\n\nAdd the field to idp_common/config/models.py, or record it in "
        "UI_ONLY_SUBTREES in this file with a reason."
    )


def test_no_stale_exceptions():
    """An exception must name a field that (still) has no control.

    Without this, the lists rot: a control gets added, the entry stays, and the
    next field added under the same subtree is exempted by accident.
    """
    config_paths = _config_field_paths()
    ui_paths = _ui_paths()
    problems: list[str] = []

    for label, table in (
        ("NOT_USER_SETTABLE", NOT_USER_SETTABLE),
        ("KNOWN_UI_GAPS", KNOWN_UI_GAPS),
    ):
        for path in sorted(table):
            if path not in config_paths:
                problems.append(
                    f"{label}[{path!r}] is not an IDPConfig field any more — "
                    "delete the entry (or fix the path)."
                )
            elif path in ui_paths:
                problems.append(
                    f"{label}[{path!r}] now HAS a UI control — delete the entry."
                )

    for path in sorted(EXEMPT_SUBTREES):
        if not any(p == path or p.startswith(f"{path}.") for p in config_paths):
            problems.append(
                f"EXEMPT_SUBTREES[{path!r}] matches no IDPConfig field — delete it."
            )

    for path in sorted(UI_ONLY_SUBTREES):
        if not any(p == path or p.startswith(f"{path}.") for p in ui_paths):
            problems.append(
                f"UI_ONLY_SUBTREES[{path!r}] matches no UI schema path — delete it."
            )

    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "table",
    [NOT_USER_SETTABLE, EXEMPT_SUBTREES, KNOWN_UI_GAPS, UI_ONLY_SUBTREES],
    ids=["not_user_settable", "exempt_subtrees", "known_ui_gaps", "ui_only_subtrees"],
)
def test_every_exception_carries_a_reason(table: dict[str, str]):
    """An exception without a reason is just a silenced failure."""
    thin = {path: reason for path, reason in table.items() if len(reason.strip()) < 25}
    assert not thin, f"exceptions need a real one-line justification: {thin}"


def test_the_two_verdicts_do_not_overlap():
    """A path cannot be both "never settable" and "an acknowledged gap"."""
    both = sorted(set(NOT_USER_SETTABLE) & set(KNOWN_UI_GAPS))
    assert not both, (
        "these paths appear in both NOT_USER_SETTABLE and KNOWN_UI_GAPS, so the "
        f"recorded verdict is ambiguous: {both}"
    )
    shadowed = sorted(
        path
        for path in (*NOT_USER_SETTABLE, *KNOWN_UI_GAPS)
        for subtree in EXEMPT_SUBTREES
        if path == subtree or path.startswith(f"{subtree}.")
    )
    assert not shadowed, (
        "these paths are already covered by an EXEMPT_SUBTREES prefix, so the "
        f"per-field entry is dead text: {shadowed}"
    )
