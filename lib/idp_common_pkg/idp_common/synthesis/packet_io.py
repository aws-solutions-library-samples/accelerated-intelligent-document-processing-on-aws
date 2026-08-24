# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read SEED generator packet output and shape it into the IDP test-set layout."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class PacketDocument:
    pdf_path: str
    sections: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class FieldNameValidation:
    ok: bool
    extra_keys: Set[str] = field(default_factory=set)
    checked_documents: int = 0


def _collect_inference_keys(node: Any, prefix: str, out: Set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                _collect_inference_keys(value, f"{path}.", out)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _collect_inference_keys(item, f"{path}[].", out)
                    else:
                        out.add(path)
            else:
                out.add(path)


def read_packet(packet_dir: str) -> List[PacketDocument]:
    input_dir = os.path.join(packet_dir, "input")
    baseline_dir = os.path.join(packet_dir, "baseline")
    if not os.path.isdir(input_dir):
        raise FileNotFoundError(f"No input/ directory in packet: {packet_dir}")

    documents: List[PacketDocument] = []
    for pdf_name in sorted(os.listdir(input_dir)):
        if not pdf_name.lower().endswith(".pdf"):
            continue
        pdf_path = os.path.join(input_dir, pdf_name)
        sections_dir = os.path.join(baseline_dir, pdf_name, "sections")
        sections: List[Dict[str, Any]] = []
        if os.path.isdir(sections_dir):
            for sect in sorted(os.listdir(sections_dir), key=_safe_int):
                result_path = os.path.join(sections_dir, sect, "result.json")
                if os.path.isfile(result_path):
                    with open(result_path, "r", encoding="utf-8") as fh:
                        sections.append(json.load(fh))
        documents.append(PacketDocument(pdf_path=pdf_path, sections=sections))
    return documents


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def validate_field_names(
    documents: List[PacketDocument],
    allowed_leaf_names: Set[str],
) -> FieldNameValidation:
    extra: Set[str] = set()
    checked = 0
    for doc in documents:
        for section in doc.sections:
            inference = section.get("inference_result", {})
            keys: Set[str] = set()
            _collect_inference_keys(inference, "", keys)
            leaf_names = {k.rstrip("[]").split(".")[-1].rstrip("[]") for k in keys}
            extra |= leaf_names - allowed_leaf_names
            checked += 1
    return FieldNameValidation(
        ok=not extra, extra_keys=extra, checked_documents=checked
    )


def _prune_to_allowed(node: Any, allowed_leaf_names: Set[str]) -> Any:
    if isinstance(node, dict):
        pruned = {}
        for key, value in node.items():
            child = _prune_to_allowed(value, allowed_leaf_names)
            if isinstance(value, (dict, list)):
                if child:
                    pruned[key] = child
            elif key in allowed_leaf_names:
                pruned[key] = value
        return pruned
    if isinstance(node, list):
        out = []
        for item in node:
            child = _prune_to_allowed(item, allowed_leaf_names)
            if child or not isinstance(item, (dict, list)):
                out.append(child)
        return out
    return node


def prune_documents_to_allowed_fields(
    documents: List[PacketDocument], allowed_leaf_names: Set[str]
) -> int:
    """Drop any baseline keys whose leaf name is not in the schema.

    The generator may add realistic fields beyond the authored schema. Those
    extra keys can't affect evaluation (only schema fields are scored), so prune
    them rather than failing the whole batch on drift. Returns the number of
    extra leaf names removed.
    """
    removed: Set[str] = set()
    for doc in documents:
        for section in doc.sections:
            inference = section.get("inference_result")
            if not isinstance(inference, dict):
                continue
            before: Set[str] = set()
            _collect_inference_keys(inference, "", before)
            section["inference_result"] = _prune_to_allowed(
                inference, allowed_leaf_names
            )
            after: Set[str] = set()
            _collect_inference_keys(section["inference_result"], "", after)
            before_leaves = {k.rstrip("[]").split(".")[-1].rstrip("[]") for k in before}
            removed |= before_leaves - allowed_leaf_names
    return len(removed)


def upload_packet_to_test_set(
    documents: List[PacketDocument],
    test_set_id: str,
    bucket: str,
    *,
    s3_client: Optional[Any] = None,
    name_prefix: str = "",
) -> int:
    """Upload generated docs into <test_set_id>/input|baseline.

    ``name_prefix`` is prepended to each document's filename so a run's docs
    have unique keys — without it, every SEED run names files doc_0001.pdf,
    doc_0002.pdf, ... and appending to an existing test set overwrites the
    prior run's same-named docs instead of adding.
    """
    import boto3

    client = s3_client or boto3.client("s3")

    # Mark this test set as synthetic. The API's get_test_sets auto-discovers
    # test-set folders in S3 and defaults them to 'uploaded'; this marker lets
    # discovery tag generated sets as 'synthetic' without the runtime-agnostic
    # synthesis module needing DynamoDB access.
    client.put_object(
        Bucket=bucket,
        Key=f"{test_set_id}/.source",
        Body=b"synthetic",
        ContentType="text/plain",
    )

    uploaded = 0
    for doc in documents:
        pdf_name = f"{name_prefix}{os.path.basename(doc.pdf_path)}"
        input_key = f"{test_set_id}/input/{pdf_name}"
        client.upload_file(
            doc.pdf_path,
            bucket,
            input_key,
            ExtraArgs={"ContentType": "application/pdf"},
        )
        uploaded += 1
        for i, section in enumerate(doc.sections, start=1):
            baseline_key = f"{test_set_id}/baseline/{pdf_name}/sections/{i}/result.json"
            client.put_object(
                Bucket=bucket,
                Key=baseline_key,
                Body=json.dumps(section, indent=2).encode("utf-8"),
                ContentType="application/json",
            )
    return uploaded
