# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Put the class schema on the wire as a forced tool, instead of describing it.

Simple (traditional) extraction asks for JSON in prose and then parses whatever
comes back. That works, and the surrounding machinery — coercion, full-schema
validation, escalation — exists precisely because it sometimes does not. This
module offers the alternative: declare the class schema as a Converse tool and
force the model to call it, so the *shape* is the API's problem rather than the
prompt's.

**This is an experiment with an honest null hypothesis.** Nothing about tool use
guarantees better extraction. Forcing constrains the shape a model must produce,
not the values it puts in it — and a model that would have emitted a stray key or
a string where a number belongs may instead emit a *worse value* that happens to
fit. It is enabled off by default and gated on a measured win; if the A/B does
not show one, the honest outcome is that it stays off.

What forcing does buy, independent of accuracy:

* A malformed-JSON parse failure becomes structurally impossible for the fields
  the schema declares.
* The response arrives as a dict, so `extract_json_from_text` heuristics
  (fenced blocks, truncation repair) are bypassed for that path.

What it costs, and why the caller must handle both:

* Not every route reaches Converse. `LambdaHook` and the GPT-5.x Responses path
  cannot carry a `toolConfig` at all, so this must be skipped for them.
* A model can accept a `toolConfig` and still answer in prose. That is a normal
  outcome, not an error, so the caller keeps the text path as a fallback.
* Bedrock rejects property names outside `^[a-zA-Z0-9_.-]{1,64}$`, which four
  shipped preset classes contain. Names are sanitized on the way out and restored
  on the way back (#709), so nothing downstream sees a renamed field.

Deliberately NOT done here: dropping the prose schema from the task prompt. It is
the obvious follow-on saving (#710) and it is a *separate* question — removing the
prose changes adherence, so bundling it would confound the very A/B this exists to
run. Measure forcing first, with the prompt unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from idp_common.bedrock.tool_schema import NameMap, restore_names, sanitize_tool_schema

logger = logging.getLogger(__name__)

#: The tool name used for the extraction tool. Stable, because it is part of the
#: prompt-cache prefix — tools render before the system prompt, so changing it
#: invalidates the cached prefix for every class.
EXTRACTION_TOOL_NAME = "emit_extracted_fields"

_TOOL_DESCRIPTION = (
    "Return the extracted values as the TOP-LEVEL properties of this tool's input, "
    "exactly as the schema declares them. Do NOT nest them under a wrapper key "
    "such as 'fields', 'data' or 'result'. Every value must come from the document "
    "itself; use null for a field the document does not contain. Do not invent "
    "values and do not add properties the schema does not define."
)

#: Wrapper keys a model plausibly invents when it decides to nest its answer.
#: `fields` is not hypothetical — see :func:`unwrap_tool_payload`.
_WRAPPER_KEY_HINTS = frozenset(
    {"fields", "data", "result", "results", "output", "extraction", "values"}
)


def unwrap_tool_payload(
    tool_input: Optional[Dict[str, Any]],
    class_schema: Optional[Dict[str, Any]],
    name_map: Optional[NameMap] = None,
) -> Optional[Dict[str, Any]]:
    """Undo a model nesting the whole answer under one off-schema wrapper key.

    Sonnet 5 returned ``{"fields": {...the entire extraction...}}`` on a live run.
    Because ``fields`` is not a property the class declares, the off-schema-key
    handling downstream dropped it — and with it every extracted value, including a
    100-row transaction list. The section still reported ``parsing_succeeded`` and
    completed, so the loss was silent.

    The tool is named ``emit_extracted_fields`` and asks for "the fields", which
    plausibly cued the nesting; the description now says not to. This is the belt to
    that braces, because a prompt instruction is a request, not a guarantee.

    Deliberately narrow, so it can only ever recover a payload and never reshape a
    legitimate one. Unwraps only when ALL of:

    * the input has exactly one key, and that key is not a declared property;
    * the key looks like a wrapper (:data:`_WRAPPER_KEY_HINTS`) OR the class
      declares no property by that name at all;
    * its value is a dict that shares at least one key with the declared
      properties — i.e. the payload really is the extraction.

    A single-key input that IS a declared property (a class with one field) is
    untouched, as is any input whose inner dict looks nothing like the schema.
    """
    if not isinstance(tool_input, dict) or len(tool_input) != 1:
        return tool_input
    # The payload inside the wrapper is keyed by the SANITIZED names the model was
    # given, so recognising it needs both spellings. A class whose every property
    # was renamed (all names contain spaces, say) would otherwise never match.
    declared = set((class_schema or {}).get("properties") or {})
    if name_map is not None:
        declared |= set(name_map.renamed)
    (key,), (value,) = tool_input.keys(), tool_input.values()
    if key in declared or not isinstance(value, dict) or not value:
        return tool_input
    if key.lower() not in _WRAPPER_KEY_HINTS and declared:
        # An unrecognised single key: only unwrap if the payload is unmistakable.
        if not (set(value) & declared):
            return tool_input
    if declared and not (set(value) & declared):
        return tool_input
    logger.warning(
        "Forced tool returned its payload nested under the off-schema key %r; "
        "unwrapping it. Without this the whole extraction would be dropped as an "
        "off-schema field and the section would report success with no data.",
        key,
    )
    return value


def build_extraction_tool_config(
    class_schema: Dict[str, Any],
) -> Tuple[Dict[str, Any], NameMap]:
    """Build the Converse ``toolConfig`` for a document class.

    Returns ``(tool_config, name_map)``. Pass ``name_map`` to
    :func:`restore_extracted_fields` so the authored property names come back.

    The returned dict is deterministic for a given schema, which matters for
    prompt caching: ``tools`` render *before* the system prompt in the cache
    prefix, so a toolConfig that differs run-to-run would invalidate the whole
    prefix on every request.
    """
    clean_schema, name_map = sanitize_tool_schema(class_schema)

    # Converse wants a JSON-Schema object at the root. A class schema always is
    # one, but be explicit rather than trusting the config.
    if clean_schema.get("type") != "object":
        clean_schema = dict(clean_schema, type="object")

    tool_config: Dict[str, Any] = {
        "tools": [
            {
                "toolSpec": {
                    "name": EXTRACTION_TOOL_NAME,
                    "description": _TOOL_DESCRIPTION,
                    "inputSchema": {"json": clean_schema},
                }
            }
        ]
    }
    return tool_config, name_map


def forced_tool_choice() -> Dict[str, Any]:
    """``toolChoice`` that requires the extraction tool to be called.

    ``{"any": {}}`` would also force *a* tool, but naming the tool is stricter and
    survives a future request that declares more than one.
    """
    return {"tool": {"name": EXTRACTION_TOOL_NAME}}


def restore_extracted_fields(
    tool_input: Optional[Dict[str, Any]], name_map: Optional[NameMap]
) -> Optional[Dict[str, Any]]:
    """Map sanitized property names in a tool response back to authored names.

    Returns None unchanged, so a caller can pass the result of
    ``extract_tool_use_from_response`` straight through and branch once.
    """
    if tool_input is None:
        return None
    restored = restore_names(tool_input, name_map)
    return restored if isinstance(restored, dict) else None


def should_force_tool(
    model_id: str, enabled: bool, class_schema: Optional[Dict[str, Any]]
) -> Tuple[bool, Optional[str]]:
    """Decide whether this request can and should use a forced tool.

    Returns ``(force, skip_reason)``. ``skip_reason`` is None when forcing, and
    otherwise a human-readable reason suitable for a log line and for the
    section's audit metadata — a silent skip would make an A/B unreadable, since
    "no effect" and "never ran" would look identical.
    """
    if not enabled:
        return False, None  # not a skip; the feature is simply off
    if not class_schema or not class_schema.get("properties"):
        return False, "the class declares no properties, so there is no schema to force"

    from idp_common.bedrock.client import tool_config_unsupported_reason

    reason = tool_config_unsupported_reason(model_id)
    if reason:
        return False, f"model does not reach the Converse API: {reason}"
    return True, None
