# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Author an IDP document-class schema from a natural-language prompt.

This is the "chicken-and-egg" solver for cold-start bootstrap: the generator
needs a schema before it can produce documents, and Discovery needs a document
before it can infer a schema. ``author_class_schema`` produces a valid
draft-2020-12 IDP class from *zero documents* - just a description.

It deliberately mirrors ``ClassesDiscovery`` (text-only, no document/image
content block) and reuses that module's static helpers (``_extract_json``,
schema validation shape, the validate-and-retry loop) so the two stay
consistent. Two differences from Discovery:

  * Input is a text description (and optional field hints / seed schema), not a
    document image - so this runs in any Lambda with no document handling.
  * It emits a per-field ``x-aws-idp-evaluation-method`` (Discovery does not),
    so the bootstrapped class is immediately evaluable.

When a ``seed_schema`` is supplied (a catalog match, Tier 2/3), the model is
asked to *adapt* it to the prompt rather than invent from scratch.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from idp_common.config import schema_constants as sc

logger = logging.getLogger(__name__)

_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
_DEFAULT_EVAL_METHOD = sc.EVALUATION_METHOD_EXACT


def _extract_json(text: str) -> str:
    """Strip markdown code fences before JSON parsing (mirrors Discovery)."""
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        return match.group(1)
    return text


def _validate_authored_schema(schema: Dict[str, Any]) -> tuple[bool, str]:
    """Validate the authored schema is a usable IDP document class.

    Same required-field checks as ``ClassesDiscovery._validate_json_schema``
    plus a JSON Schema meta-validation. Kept local so the authoring path has no
    runtime dependency on the Discovery class itself.
    """
    try:
        from jsonschema import Draft202012Validator, SchemaError
    except ImportError:  # pragma: no cover - jsonschema is a core dep
        Draft202012Validator = None  # type: ignore
        SchemaError = Exception  # type: ignore

    required_fields = [sc.SCHEMA_FIELD, sc.ID_FIELD, "type", "properties"]
    for fld in required_fields:
        if fld not in schema:
            return False, f"Missing required field: {fld}"
    if sc.X_AWS_IDP_DOCUMENT_TYPE not in schema:
        return False, f"Missing {sc.X_AWS_IDP_DOCUMENT_TYPE} field"
    if schema.get("type") != "object":
        return False, "Root type must be 'object'"
    if not isinstance(schema.get("properties"), dict):
        return False, "Properties must be an object"
    if not schema["properties"]:
        return False, "Properties must not be empty"
    if Draft202012Validator is not None:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as e:
            return False, f"Invalid JSON Schema: {e}"
    return True, ""


def _normalize_eval_methods(node: Any) -> None:
    """In place: ensure every leaf property has a valid eval method.

    Defaults to EXACT when missing or invalid, so a bad/omitted value from the
    model never blocks bootstrap. Recurses through nested objects and array
    items. Leaves (string/number/etc.) get the annotation; containers do not.
    """
    if not isinstance(node, dict):
        return
    props = node.get("properties")
    if isinstance(props, dict):
        for spec in props.values():
            if not isinstance(spec, dict):
                continue
            ntype = spec.get("type")
            if ntype == "object" and isinstance(spec.get("properties"), dict):
                _normalize_eval_methods(spec)
            elif ntype == "array" and isinstance(spec.get("items"), dict):
                _normalize_eval_methods(spec["items"])
            else:
                method = spec.get(sc.X_AWS_IDP_EVALUATION_METHOD)
                if method not in sc.VALID_EVALUATION_METHODS:
                    spec[sc.X_AWS_IDP_EVALUATION_METHOD] = _DEFAULT_EVAL_METHOD


def _sample_output_format() -> str:
    """A draft-2020-12 sample including per-field eval methods."""
    return json.dumps(
        {
            "$schema": _DRAFT_2020_12,
            "$id": "Auto-Insurance-Claim",
            "x-aws-idp-document-type": "Auto-Insurance-Claim",
            "type": "object",
            "description": "Brief summary of the document (< 50 words)",
            "properties": {
                "ClaimNumber": {
                    "type": "string",
                    "description": "Unique claim identifier",
                    "x-aws-idp-evaluation-method": "EXACT",
                },
                "ClaimantName": {
                    "type": "string",
                    "description": "Full name of the claimant",
                    "x-aws-idp-evaluation-method": "FUZZY",
                },
                "DateOfLoss": {
                    "type": "string",
                    "format": "date",
                    "description": "Date the loss occurred",
                    "x-aws-idp-evaluation-method": "DATE",
                },
                "AmountClaimed": {
                    "type": "number",
                    "description": "Total amount claimed",
                    "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
                },
            },
        },
        indent=2,
    )


def build_author_prompt(
    prompt: str,
    *,
    field_hints: Optional[List[str]] = None,
    class_name: Optional[str] = None,
    seed_schema: Optional[Dict[str, Any]] = None,
) -> str:
    """Construct the user prompt for schema authoring.

    Separated from the Bedrock call so it can be unit-tested without AWS.
    """
    methods = ", ".join(sorted(sc.VALID_EVALUATION_METHODS))
    parts: List[str] = []

    if seed_schema is not None:
        parts.append(
            "You are ADAPTING an existing JSON Schema for a document type to "
            "match a user's description. Start from the SEED SCHEMA below and "
            "add, remove, or rename fields so it matches the description. Keep "
            "field names that still apply; do not gratuitously rename them.\n\n"
            f"SEED SCHEMA:\n{json.dumps(seed_schema, indent=2)}\n"
        )
    else:
        parts.append(
            "You are designing a JSON Schema that describes the structure of a "
            "document type, based ONLY on a natural-language description. Do not "
            "invent values; describe the FIELDS a real instance of this document "
            "would contain."
        )

    parts.append(f"\nUSER DESCRIPTION OF THE DOCUMENT TYPE:\n{prompt}\n")

    if field_hints:
        parts.append(
            "\nThe schema MUST include fields for each of these (choose good "
            "names/types): " + "; ".join(field_hints)
        )

    parts.append(
        "\nRules:\n"
        f'- Use "$schema": "{_DRAFT_2020_12}"\n'
        '- Set "$id" and "x-aws-idp-document-type" to a short document class '
        "name"
        + (f' ("{class_name}")' if class_name else ' (e.g., "Paystub", "W2")')
        + "\n"
        '- Set "type": "object" and add a brief "description" (< 50 words)\n'
        '- For the "properties" object:\n'
        "  - Group related fields as nested objects (type: object) with their "
        "own properties\n"
        "  - For repeating/table data use type: array with items as an object "
        "schema\n"
        '  - Each leaf field needs "type", "description", and '
        '"x-aws-idp-evaluation-method"\n'
        f"  - Evaluation method must be one of: {methods}. Use EXACT for "
        "codes/IDs/exact values, NUMERIC_EXACT for amounts, DATE for dates and "
        "date ranges, FUZZY for names/addresses, SEMANTIC for free-text "
        "descriptions\n"
        "  - Field names < 30 chars; no leading digit; no special characters\n"
        "Return ONLY the JSON Schema in exactly this shape:\n"
        f"{_sample_output_format()}"
    )

    if class_name:
        parts.append(
            f'\nIMPORTANT: Use "{class_name}" as the document class name. Set '
            f'"$id" and "x-aws-idp-document-type" to "{class_name}".'
        )

    return "\n".join(parts)


def author_class_schema(
    prompt: str,
    *,
    field_hints: Optional[List[str]] = None,
    class_name: Optional[str] = None,
    seed_schema: Optional[Dict[str, Any]] = None,
    model_id: Optional[str] = None,
    region: Optional[str] = None,
    max_retries: int = 3,
    bedrock_client: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Author an IDP document-class schema from a prompt.

    Returns a validated draft-2020-12 class dict (with per-field eval methods),
    or ``None`` if a valid schema could not be produced after ``max_retries``.

    ``bedrock_client`` may be injected for testing; otherwise a
    ``BedrockClient`` is constructed. The model defaults to a capable text model
    suitable for schema design when ``model_id`` is not given.
    """
    from idp_common import bedrock

    client = bedrock_client or bedrock.BedrockClient(region=region)
    model = model_id or "us.anthropic.claude-sonnet-4-6"
    system_prompt = (
        "You are an expert in document understanding and JSON Schema design. "
        "You produce precise, well-named extraction schemas."
    )

    base_prompt = build_author_prompt(
        prompt,
        field_hints=field_hints,
        class_name=class_name,
        seed_schema=seed_schema,
    )

    validation_feedback = ""
    for attempt in range(max_retries):
        retry_prefix = ""
        if attempt > 0 and validation_feedback:
            retry_prefix = (
                f"PREVIOUS ATTEMPT FAILED: {validation_feedback}\n"
                "Fix the issue and return a valid JSON Schema.\n\n"
            )
        content = [{"text": f"{retry_prefix}{base_prompt}"}]
        try:
            response = client.invoke_model(
                model_id=model,
                system_prompt=system_prompt,
                content=content,
                temperature=0.0,
                context="SchemaAuthor",
            )
            text = client.extract_text_from_response(response)
            schema = json.loads(_extract_json(text))
        except json.JSONDecodeError as e:
            validation_feedback = f"Invalid JSON: {e}"
            logger.warning("schema_author attempt %d JSON error: %s", attempt + 1, e)
            continue
        except Exception as e:
            logger.error("schema_author attempt %d Bedrock error: %s", attempt + 1, e)
            if attempt == max_retries - 1:
                return None
            continue

        if class_name:
            schema[sc.ID_FIELD] = class_name
            schema[sc.X_AWS_IDP_DOCUMENT_TYPE] = class_name

        is_valid, err = _validate_authored_schema(schema)
        if is_valid:
            _normalize_eval_methods(schema)
            logger.info(
                "schema_author produced a valid schema on attempt %d", attempt + 1
            )
            return schema
        validation_feedback = err
        logger.warning("schema_author attempt %d invalid: %s", attempt + 1, err)

    logger.error("schema_author failed after %d attempts", max_retries)
    return None
