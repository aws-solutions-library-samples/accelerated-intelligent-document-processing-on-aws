# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cold-start bootstrap orchestration: prompt/docs -> schema -> config + test set."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from idp_common.config import schema_constants as sc
from idp_common.config.class_settings import carry_forward_authored_settings
from idp_common.synthesis import catalog as catalog_mod
from idp_common.synthesis import packet_io, schema_author, schema_bridge

logger = logging.getLogger(__name__)

StatusCallback = Callable[[float, str], None]

CONFIG_TYPE_CONFIG = "Config"


@dataclass
class BootstrapRequest:
    prompt: str
    class_name: Optional[str] = None
    field_hints: List[str] = field(default_factory=list)
    config_version: Optional[str] = None
    target_version: Optional[str] = None
    doc_count: int = 3
    quality_threshold: int = 7
    augment: bool = False
    model_id: Optional[str] = None
    example_doc_keys: List[str] = field(default_factory=list)
    scenario: str = ""


@dataclass
class BootstrapResult:
    schema: Optional[Dict[str, Any]] = None
    resolution_tier: str = "none"
    catalog_match: Optional[str] = None
    config_version: Optional[str] = None
    test_set_id: Optional[str] = None
    docs_generated: int = 0
    generator_available: bool = False
    field_validation_ok: Optional[bool] = None
    field_validation_extra: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    success: bool = False
    error: Optional[str] = None


def resolve_schema(
    request: BootstrapRequest,
    *,
    config_classes: Optional[List[Dict[str, Any]]] = None,
    bedrock_client: Optional[Any] = None,
    status_cb: Optional[StatusCallback] = None,
) -> tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    def _report(pct: float, msg: str) -> None:
        if status_cb:
            status_cb(pct, msg)

    if request.example_doc_keys:
        _report(10.0, "Example documents provided; use Discovery to infer schema")
        return None, "user_docs_pending", None

    entries = catalog_mod.build_catalog(config_classes=config_classes)
    seed_schema: Optional[Dict[str, Any]] = None
    matched_name: Optional[str] = None
    if entries:
        _report(15.0, "Searching catalog for a matching template")
        match = catalog_mod.match_catalog(
            request.prompt,
            entries,
            model_id=request.model_id,
            bedrock_client=bedrock_client,
        )
        if match is not None and match.schema is not None:
            seed_schema = match.schema
            matched_name = match.name

    _report(25.0, "Authoring document schema")
    schema = schema_author.author_class_schema(
        request.prompt,
        field_hints=request.field_hints or None,
        class_name=request.class_name,
        seed_schema=seed_schema,
        model_id=request.model_id,
        bedrock_client=bedrock_client,
    )
    if schema is None:
        return None, "failed", matched_name

    tier = "catalog_adapt" if seed_schema is not None else "pure_llm"
    return schema, tier, matched_name


def merge_class_into_version(
    schema: Dict[str, Any],
    version: str,
    *,
    config_manager: Any,
) -> None:
    existing = config_manager.get_raw_configuration(CONFIG_TYPE_CONFIG, version) or {}
    classes = list(existing.get("classes", []))
    new_id = schema.get(sc.X_AWS_IDP_DOCUMENT_TYPE) or schema.get(sc.ID_FIELD)
    by_id: Dict[str, Dict[str, Any]] = {}
    for cls in classes:
        cid = cls.get(sc.X_AWS_IDP_DOCUMENT_TYPE) or cls.get(sc.ID_FIELD)
        if cid:
            by_id[cid] = cls
    if new_id:
        # Same rule as the discovery and BDA-optimizer write paths: the LLM
        # authored `properties`, not the class-level settings someone configured
        # on an existing class of this id, and replacing the dict wholesale
        # erased them silently (#764).
        if new_id in by_id:
            carry_forward_authored_settings(by_id[new_id], schema)
        by_id[new_id] = schema
    existing["classes"] = list(by_id.values())
    config_manager.save_raw_configuration(CONFIG_TYPE_CONFIG, existing, version=version)


def run_bootstrap(
    request: BootstrapRequest,
    *,
    config_manager: Any,
    test_set_bucket: Optional[str] = None,
    bedrock_client: Optional[Any] = None,
    s3_client: Optional[Any] = None,
    status_cb: Optional[StatusCallback] = None,
) -> BootstrapResult:
    from idp_common.synthesis import engine

    result = BootstrapResult()

    def _report(pct: float, msg: str) -> None:
        result.messages.append(msg)
        logger.info("bootstrap %.0f%%: %s", pct, msg)
        if status_cb:
            status_cb(pct, msg)

    config_classes: List[Dict[str, Any]] = []
    if request.config_version:
        try:
            raw = config_manager.get_raw_configuration(
                CONFIG_TYPE_CONFIG, request.config_version
            )
            if raw:
                config_classes = list(raw.get("classes", []))
        except Exception as e:
            logger.warning("Could not read config classes for catalog: %s", e)

    schema, tier, matched = resolve_schema(
        request,
        config_classes=config_classes,
        bedrock_client=bedrock_client,
        status_cb=_report,
    )
    result.resolution_tier = tier
    result.catalog_match = matched

    if schema is None and tier == "user_docs_pending":
        result.error = (
            "Example-document path (Discovery) is wired in the deployed "
            "processor; this orchestrator handles the prompt-first path."
        )
        return result
    if schema is None:
        result.error = "Failed to author a valid schema from the prompt"
        return result

    result.schema = schema

    target_version = request.target_version or _default_version_name(schema)
    _report(45.0, f"Creating config version '{target_version}'")
    try:
        merge_class_into_version(schema, target_version, config_manager=config_manager)
        result.config_version = target_version
    except Exception as e:
        result.error = f"Failed to create config version: {e}"
        return result

    available, reason = engine.generator_available()
    result.generator_available = available
    if not available:
        _report(
            100.0,
            "Config version created. Document generator unavailable; "
            "skipping synthetic test set. " + engine.INSTALL_HINT,
        )
        result.success = True
        return result

    if not test_set_bucket:
        _report(100.0, "Config version created; no test-set bucket configured")
        result.success = True
        return result

    _report(55.0, f"Generating {request.doc_count} synthetic document(s)")
    work_dir = tempfile.mkdtemp(prefix="idp-bootstrap-")
    schema_dir = os.path.join(work_dir, "schema")
    out_dir = os.path.join(work_dir, "out")
    os.makedirs(schema_dir, exist_ok=True)
    _write_schema_dir(schema, schema_dir)

    job = engine.SynthesisJob(
        schema_dir=schema_dir,
        out_dir=out_dir,
        count=request.doc_count,
        threshold=request.quality_threshold,
        augment=request.augment,
        extra=request.scenario or request.prompt,
        model_id=request.model_id,
    )
    try:
        syn = engine.synthesize(job, status_cb=_report)
    except Exception as e:
        result.error = f"Generation failed: {e}"
        result.success = True
        _report(100.0, f"Config version created; generation failed: {e}")
        return result

    if not syn.success or not syn.packet_dir:
        result.error = syn.error or "Generation produced no packet"
        result.success = True
        return result

    _report(85.0, "Validating generated field names against schema")
    documents = packet_io.read_packet(syn.packet_dir)
    allowed = set(schema_bridge.field_names(schema))
    validation = packet_io.validate_field_names(documents, allowed)
    result.field_validation_extra = sorted(validation.extra_keys)
    if not validation.ok:
        removed = packet_io.prune_documents_to_allowed_fields(documents, allowed)
        _report(
            90.0,
            f"Pruned {removed} extra field name(s) not in schema: "
            f"{sorted(validation.extra_keys)}",
        )
    result.field_validation_ok = True

    test_set_id = target_version
    _report(95.0, f"Registering test set '{test_set_id}'")
    import uuid as _uuid

    uploaded = packet_io.upload_packet_to_test_set(
        documents,
        test_set_id,
        test_set_bucket,
        s3_client=s3_client,
        name_prefix=f"{_uuid.uuid4().hex[:8]}_",
    )
    result.test_set_id = test_set_id
    result.docs_generated = uploaded
    result.success = True
    _report(100.0, f"Bootstrap complete: {uploaded} document(s) in test set")
    return result


def _write_schema_dir(class_schema: Dict[str, Any], dest_dir: str) -> None:
    import json

    generator_schema = schema_bridge.config_class_to_generator_schema(class_schema)
    with open(os.path.join(dest_dir, "schema.json"), "w", encoding="utf-8") as fh:
        json.dump(generator_schema, fh, indent=2)
    guidance = _build_guidance(class_schema)
    with open(
        os.path.join(dest_dir, "generation_guidance.md"), "w", encoding="utf-8"
    ) as fh:
        fh.write(guidance)


def _build_guidance(class_schema: Dict[str, Any]) -> str:
    name = (
        class_schema.get(sc.X_AWS_IDP_DOCUMENT_TYPE)
        or class_schema.get(sc.ID_FIELD)
        or class_schema.get("title")
        or "Document"
    )
    description = class_schema.get("description", "")
    lines = [f"# {name} — Generation Guidance", "", "## Document Type", description, ""]
    return "\n".join(lines)


def _default_version_name(schema: Dict[str, Any]) -> str:
    base = (
        schema.get(sc.X_AWS_IDP_DOCUMENT_TYPE) or schema.get(sc.ID_FIELD) or "bootstrap"
    )
    slug = "".join(c if c.isalnum() else "-" for c in str(base)).strip("-").lower()
    return f"bootstrap-{slug}"
