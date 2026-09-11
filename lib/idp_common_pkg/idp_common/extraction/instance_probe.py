# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Ask the extraction model how many documents a section actually holds (#753).

A section that spans several consecutive records of the SAME class has no type
change for classification to split on, so all of them land in one section. The
class schema describes one document and the model overwhelmingly answers with one
object — so records 2..N are simply absent from the response, and **nothing
reports it**: the section is SUCCESS, ``instance_count`` is 1, and no issue is
raised (GitHub #565, #753).

The cheapest reliable signal is to ask the question in the same inference: add
one auxiliary integer to the requested shape, read it, and strip it before
anything downstream sees the result. It costs one extra integer of output, needs
no second call, works whether or not confidence assessment is enabled, and asks
the only component that has both the pages and the schema in front of it.

This module is deliberately pure (no boto3, no Strands, no service imports) so it
is cheap to unit-test and cheap to import in every Lambda — same discipline as
``extraction/validation.py``.

Design notes:

* The property is injected into a **copy** of the class schema used for the
  prompt / tool wire format only. The real class schema is untouched, so
  ``_filter_extracted_to_schema``, the JSON-Schema validator, the generated
  Pydantic model and every downstream consumer keep seeing exactly the declared
  fields.
* The name is a valid Converse top-level property (``^[a-zA-Z0-9_.-]{1,64}$``),
  so the forced-tool path can carry it without the name sanitizer renaming it.
* If a class genuinely declares a property of the same name, the probe is
  skipped rather than shadowing the user's field.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# The auxiliary property name. Prefixed so it cannot plausibly collide with a
# real document attribute, and readable enough that a model treats it as a real
# question rather than noise.
INSTANCE_PROBE_FIELD = "IDPDocumentInstanceCount"

#: The question put to the model, and the fallback when
#: ``extraction.multi_instance_detection.question`` is empty — which it is for any
#: ``IDPConfig`` built without merging system defaults (unit tests, notebooks).
#: The shipped text lives in ``config/system_defaults/base-extraction.yaml`` so it
#: is visible and editable in ``Config#default`` like every other prompt;
#: ``test_instance_probe.py`` asserts the two are byte-identical so they cannot
#: drift.
#:
#: The wording is load-bearing, and two clauses in particular:
#:
#: * *"Do not count pages, sections or repeated headers"* — without it a document
#:   with an identical banner on each of four pages reads as four documents.
#: * *"DIAGNOSTIC METADATA, not extracted document data"* — the field must not be
#:   mistaken for something to extract from the page.
#:
#: ``{DOCUMENT_CLASS}`` is substituted with the section's class label.
DEFAULT_INSTANCE_QUESTION = (
    "DIAGNOSTIC METADATA, not extracted document data. How many separate, "
    "complete '{DOCUMENT_CLASS}' documents are present in the supplied pages? "
    "Answer 1 for the normal case of one document. Answer more than 1 only when "
    "the pages clearly contain several distinct documents of this same type — for "
    "example statements covering different periods, or records for different "
    "people — including when a document starts part-way down a page. Do not count "
    "pages, sections or repeated headers: count complete documents. Extract all "
    "other fields exactly as you otherwise would."
)


def _probe_property_schema(
    class_label: str, question: str | None = None
) -> dict[str, Any]:
    """The JSON-Schema fragment describing the auxiliary count property.

    Phrased as a question about the PAGE RANGE, not about the document's
    content, and explicitly labelled as diagnostic metadata — the point is to
    add a question, not to change how the model extracts the fields it is
    already being asked for.

    ``question`` overrides the shipped wording (from
    ``extraction.multi_instance_detection.question``). Empty or None falls back to
    :data:`DEFAULT_INSTANCE_QUESTION`.
    """
    label = class_label or "document"
    text = (question or "").strip() or DEFAULT_INSTANCE_QUESTION
    return {
        "type": "integer",
        "minimum": 1,
        "description": text.replace("{DOCUMENT_CLASS}", label),
    }


def augment_schema_with_probe(
    class_schema: dict[str, Any] | None,
    class_label: str,
    question: str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(wire_schema, probe_added)``.

    ``wire_schema`` is a shallow copy of ``class_schema`` with the probe property
    added to a copied ``properties`` dict. Nothing else is touched and the input
    is never mutated. The probe is NOT added to ``required``: a model that omits
    it should not fail validation of the wire schema, and an absent count is
    simply "not determined".

    Returns the schema unchanged with ``probe_added=False`` when:

    * there is no schema, or it declares no ``properties`` (nothing is being
      asked for, so there is nothing to add a diagnostic to — this mirrors
      ``_filter_extracted_to_schema``'s fail-open guard), or
    * the class already declares a property of the same name (never shadow a
      user's field).
    """
    if not isinstance(class_schema, dict):
        return class_schema, False

    properties = class_schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return class_schema, False

    if INSTANCE_PROBE_FIELD in properties:
        logger.info(
            "Class '%s' already declares a '%s' property; skipping the "
            "multi-instance detection probe rather than shadowing it",
            class_label,
            INSTANCE_PROBE_FIELD,
        )
        return class_schema, False

    wire = dict(class_schema)
    wire["properties"] = {
        **properties,
        INSTANCE_PROBE_FIELD: _probe_property_schema(class_label, question),
    }
    return wire, True


def _coerce_probe_value(raw: Any) -> int | None:
    """Coerce a model-returned probe value to a positive int, or None.

    Tolerant on purpose — this is a diagnostic, so a value that cannot be read
    costs a log line, never an extraction:

    * ``3``, ``"3"``, ``3.0`` -> ``3``
    * ``{"G1": 3, "P1": 0.9}`` -> ``3`` (the 1S-TopK integrated-confidence
      candidate shape, which wraps every field value)
    * ``None``, ``""``, ``"three"``, ``0``, ``-1``, ``[]`` -> ``None``
    """
    if isinstance(raw, dict):
        # 1S-TopK candidate object: the top-ranked guess is G1.
        if "G1" in raw:
            return _coerce_probe_value(raw.get("G1"))
        return None
    if isinstance(raw, bool):
        # bool is an int subclass; a True/False answer is not a count.
        return None
    try:
        # float() first so "3", 3, 3.0 and " 3 " all land; a non-integral answer
        # (2.5 documents) is not a count and is refused rather than rounded.
        as_float = float(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    if as_float != int(as_float):
        return None
    value = int(as_float)
    return value if value >= 1 else None


def pop_probe_value(fields: Any) -> int | None:
    """Remove the probe property from ``fields`` and return its integer value.

    Always pops, even when the value is unusable, so the auxiliary field can
    never reach ``inference_result`` (where it would be reported as an
    off-schema field, scored by assessment, written to a reporting column, and
    compared against a ground-truth baseline that has no such key).
    """
    if not isinstance(fields, dict) or INSTANCE_PROBE_FIELD not in fields:
        return None
    raw = fields.pop(INSTANCE_PROBE_FIELD)
    value = _coerce_probe_value(raw)
    if value is None and raw is not None:
        # Truncated: this is arbitrary model output, and an unusable "value" can be
        # a paragraph of prose.
        logger.info(
            "Multi-instance probe returned an unusable value %.120r; ignoring", raw
        )
    return value
