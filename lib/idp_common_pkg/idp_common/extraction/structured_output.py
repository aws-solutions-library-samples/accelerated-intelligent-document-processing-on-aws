# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Constrain Simple extraction's reply to the class schema with structured outputs.

Simple extraction asks for JSON in the prompt and then parses, repairs and
validates whatever comes back. Bedrock structured outputs removes the first two
steps for a supported Claude model: the class schema goes on the request as a
Converse ``outputConfig.textFormat`` and constrained decoding guarantees the reply
is JSON that matches it. Unlike a forced tool (``forced_tool.py``) it needs no
``toolChoice``, so it also works on Claude Opus 5.5 and Sonnet 5.5, which reject a
forced one.

**Off by default** (``extraction.schema_enforcement.mode``: ``structured_output``
or ``auto``). Constraining the
shape says nothing about the values, so whether it changes accuracy is a question
for the benchmark, not this module.

It falls back to the prompt-described path, recording why, whenever it cannot
apply:

* the route cannot carry it (a Lambda hook, the GPT-5.x Responses API);
* the model's card does not list structured outputs (Nova, Grok, Astra);
* the class schema does not fit structured outputs' subset or limits — see
  :func:`idp_common.bedrock.structured_output.prepare_schema`;
* Bedrock rejects the schema anyway. That is a 400 which can take up to ~90 s
  (the limit on optional properties is enforced while the grammar compiles), so
  the rejection is remembered for the life of the container and the next section
  of the same class does not pay for it again.

The prompt is left exactly as it is. Whether the prose schema can then be dropped
is a separate question (#710), and bundling it would confound the comparison.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional, Tuple

from idp_common.bedrock.structured_output import (
    PreparedSchema,
    converse_output_config,
    prepare_schema,
)
from idp_common.bedrock.tool_schema import NameMap, restore_names, sanitize_tool_schema

logger = logging.getLogger(__name__)

#: ``jsonSchema.name`` on the wire. Stable, because the request — and therefore
#: Bedrock's 24-hour grammar cache key and the prompt-cache prefix — includes it.
OUTPUT_FORMAT_NAME = "extracted_fields"

_OUTPUT_FORMAT_DESCRIPTION = (
    "The values extracted from the document. Every value must come from the "
    "document itself; use null for a field the document does not contain."
)

#: Schema fingerprint -> the reason Bedrock gave for rejecting it. Per container,
#: deliberately: a rejection is a property of the schema and the service, and a
#: deploy or a container recycle is the right point to ask again.
_REJECTED_SCHEMAS: Dict[str, str] = {}

#: Bound on the rejection cache, so a stream of distinct schemas cannot grow it
#: without limit in a long-lived container.
_MAX_REJECTED = 256


def _fingerprint(output_config: Dict[str, Any]) -> str:
    raw = json.dumps(output_config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def rejected_reason(output_config: Dict[str, Any]) -> Optional[str]:
    """The reason Bedrock rejected this exact output format earlier, if it did."""
    return _REJECTED_SCHEMAS.get(_fingerprint(output_config))


def record_rejection(output_config: Dict[str, Any], reason: str) -> None:
    """Remember that Bedrock rejected this output format."""
    if len(_REJECTED_SCHEMAS) >= _MAX_REJECTED:
        _REJECTED_SCHEMAS.pop(next(iter(_REJECTED_SCHEMAS)))
    _REJECTED_SCHEMAS[_fingerprint(output_config)] = reason


def should_use_structured_output(
    model_id: str, enabled: bool, class_schema: Optional[Dict[str, Any]]
) -> Tuple[bool, Optional[str]]:
    """Decide whether this request can use structured outputs.

    Returns ``(use, skip_reason)``; ``skip_reason`` is None when using it, and also
    when the feature is simply off (that is not a skip).
    """
    if not enabled:
        return False, None
    if not class_schema or not class_schema.get("properties"):
        return False, "the class declares no properties, so there is no schema"

    from idp_common.bedrock.client import structured_output_unsupported_reason

    reason = structured_output_unsupported_reason(model_id)
    if reason:
        return False, f"structured outputs unavailable for this model: {reason}"
    return True, None


def build_extraction_output_config(
    class_schema: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[NameMap], PreparedSchema]:
    """Build the Converse ``outputConfig`` for a document class.

    Returns ``(output_config, name_map, prepared)``. ``output_config`` is None when
    the schema does not fit, and ``prepared.reason`` then says why. Pass
    ``name_map`` to :func:`restore_extracted_fields` on the parsed reply.

    Deterministic for a given schema, which is what lets Bedrock reuse a compiled
    grammar instead of paying the first-request compilation again.
    """
    clean_schema, name_map = sanitize_tool_schema(class_schema)
    prepared = prepare_schema(clean_schema)
    if not prepared.fits or prepared.schema is None:
        return None, name_map, prepared
    return (
        converse_output_config(
            prepared.schema, OUTPUT_FORMAT_NAME, _OUTPUT_FORMAT_DESCRIPTION
        ),
        name_map,
        prepared,
    )


def parse_structured_reply(
    text: Optional[str], stop_reason: Optional[str], name_map: Optional[NameMap]
) -> Optional[Any]:
    """Parse a structured-outputs reply, or return None to use the text path.

    Only a reply that ended normally is trusted as complete. A ``max_tokens`` stop
    returns incomplete JSON even under constrained decoding, and a refusal or a
    content filter is not an answer at all, so both return None and the caller's
    ordinary parse-and-repair path handles them exactly as it handles any other
    reply. No heuristics are applied here: the reply is the JSON, or it is not
    trusted.
    """
    if stop_reason != "end_turn" or not isinstance(text, str):
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        logger.warning(
            "Structured-output reply ended normally but is not valid JSON; "
            "falling back to the text parse path"
        )
        return None
    restored = restore_names(parsed, name_map)
    return restored if isinstance(restored, (dict, list)) else None
