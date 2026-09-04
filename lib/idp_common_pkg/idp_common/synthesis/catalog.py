# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unified catalog of reusable schema/sample assets for bootstrap."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from idp_common.config import schema_constants as sc
from idp_common.schema.multi_instance import wrap_class_schema

logger = logging.getLogger(__name__)


@dataclass
class CatalogEntry:
    name: str
    source: str
    description: str
    schema: Optional[Dict[str, Any]] = None


def index_config_classes(classes: List[Dict[str, Any]]) -> List[CatalogEntry]:
    entries: List[CatalogEntry] = []
    for cls in classes or []:
        if not isinstance(cls, dict):
            continue
        name = (
            cls.get(sc.X_AWS_IDP_DOCUMENT_TYPE)
            or cls.get(sc.ID_FIELD)
            or cls.get("title")
        )
        if not name:
            continue
        entries.append(
            CatalogEntry(
                name=str(name),
                source="config",
                description=cls.get("description", ""),
                # The EFFECTIVE schema. A class flagged x-aws-idp-multi-instance
                # (GitHub #715) extracts `{"instances": [ … ]}`, so a synthetic
                # test set generated from the FLAT schema produces ground truth of
                # the wrong shape — and evaluation then scores every field as
                # missing on one side, which is the silent ~0 this feature's
                # baseline migration exists to prevent. Every consumer of the
                # catalog (synthesis bootstrap, the Quick Start tools, the
                # data-generator extension) reads `schema` from here, so wrapping
                # once at the source covers all of them. A no-op for an unflagged
                # class.
                schema=wrap_class_schema(cls),
            )
        )
    return entries


def build_catalog(
    config_classes: Optional[List[Dict[str, Any]]] = None,
) -> List[CatalogEntry]:
    entries: List[CatalogEntry] = []
    if config_classes:
        entries.extend(index_config_classes(config_classes))
    return entries


def _build_match_prompt(prompt: str, entries: List[CatalogEntry]) -> str:
    listing = "\n".join(
        f"{i}. [{e.source}] {e.name}: {e.description}" for i, e in enumerate(entries)
    )
    return (
        "A user wants to create a document type. Given their description and a "
        "list of available document templates, pick the SINGLE best match if "
        "one is a strong fit, otherwise return NONE.\n\n"
        f"USER DESCRIPTION:\n{prompt}\n\n"
        f"AVAILABLE TEMPLATES:\n{listing}\n\n"
        'Respond with ONLY a JSON object: {"index": <number or null>, '
        '"confidence": <0.0-1.0>, "reason": "<short>"}. Use null for index if '
        "no template is a good match."
    )


def match_catalog(
    prompt: str,
    entries: List[CatalogEntry],
    *,
    model_id: Optional[str] = None,
    region: Optional[str] = None,
    min_confidence: float = 0.6,
    bedrock_client: Optional[Any] = None,
) -> Optional[CatalogEntry]:
    if not entries:
        return None

    from idp_common import bedrock
    from idp_common.synthesis.schema_author import _extract_json

    client = bedrock_client or bedrock.BedrockClient(region=region)
    model = model_id or "us.anthropic.claude-sonnet-4-20250514-v1:0"

    try:
        response = client.invoke_model(
            model_id=model,
            system_prompt="You match user requests to document templates.",
            content=[{"text": _build_match_prompt(prompt, entries)}],
            temperature=0.0,
            context="CatalogMatch",
        )
        text = client.extract_text_from_response(response)
        result = json.loads(_extract_json(text))
    except Exception as e:
        logger.warning("Catalog match failed, falling back to no match: %s", e)
        return None

    index = result.get("index")
    confidence = result.get("confidence", 0.0)
    if index is None or not isinstance(index, int):
        return None
    if index < 0 or index >= len(entries):
        return None
    if confidence < min_confidence:
        logger.info(
            "Catalog match confidence %.2f below threshold %.2f; no match",
            confidence,
            min_confidence,
        )
        return None
    return entries[index]
