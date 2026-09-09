# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Runtime-agnostic primitives + pluggable orchestration for sharded extraction.

This module is the **single source of truth** for how a section's extraction is
sharded, persisted, and merged. Two thin schedulers drive the same primitives:

- :class:`InProcessRuntime` (default) — plans shards, runs them concurrently via
  ``asyncio.gather`` + a semaphore, then merges. Identical observable behaviour
  to the historical ``concurrent_structured_output_async``. This is what a
  notebook / CLI / single Lambda uses; sharding works fully here with **no Step
  Functions dependency**.
- :class:`StepFunctionsRuntime` (production) — a thin marker selected by the
  infra/Lambda layer. The Step Functions Distributed Map calls
  :func:`extract_one_shard` once per iteration (one fresh Lambda per shard) and a
  following merge state calls :func:`merge_shard_results`. Because each shard
  persists its own result idempotently to S3, SFN's native per-iteration retry
  re-runs only the failed/incomplete shards — completed shards load from S3.

The primitives are deliberately import-light at module top (no strands / PIL /
boto3) so the planning, persistence-idempotency and runtime-selection logic is
cheap to import and unit-testable without the agentic stack. The strands-backed
agent call is reached lazily inside :func:`_default_shard_runner`.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import typing
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from pydantic import BaseModel

from idp_common.config.models import IDPConfig

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Pure merge primitives (no strands / PIL / boto3) — single source of truth.
# agentic_idp re-exports these for backward compatibility.
# --------------------------------------------------------------------------- #
def _annotation_is_list(annotation: Any) -> bool:
    """True if a type annotation is ``list`` / ``list[...]`` / ``Optional[list]``."""
    if annotation is list:
        return True
    origin = typing.get_origin(annotation)
    if origin in (list, __import__("builtins").list):
        return True
    if origin is not None:
        return any(_annotation_is_list(arg) for arg in typing.get_args(annotation))
    return False


def _list_valued_fields(data_format: type[BaseModel]) -> set[str]:
    """Return the names of ``data_format`` fields that are list-typed.

    Used so the shard merge treats a field consistently as a list even when an
    individual shard returns it as ``None`` (an optional ``list | None`` left
    empty by, e.g., a cover-page shard).
    """
    fields = getattr(data_format, "model_fields", None)
    if not fields:
        return set()
    list_fields: set[str] = set()
    for name, info in fields.items():
        annotation = getattr(info, "annotation", None)
        if _annotation_is_list(annotation):
            list_fields.add(name)
    return list_fields


# Special metering keys that are NOT additive token counters and must not be
# summed across shards. ``_table_parsing_stats`` carries quality metrics (rates,
# averages) merged with their own semantics in ``_merge_table_parsing_stats``.
_NON_ADDITIVE_METERING_KEYS = frozenset({"_table_parsing_stats"})


def _merge_table_parsing_stats(
    acc: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Merge per-shard table-parsing stats with CORRECT semantics.

    Summing these like token counts produced nonsense (5 shards each at
    ``parse_success_rate``≈1.0 → 5.0 / "500%"; ``avg_confidence``≈99 → "496%").
    Counts (``tables_parsed``, ``rows_parsed``, ``rows_mapped``,
    ``invocation_count``) are summed; ``parse_success_rate`` and
    ``avg_confidence`` are combined as **row-weighted averages** (weighted by
    each shard's ``rows_parsed``) so the merged value stays a real 0-1 rate /
    0-100 confidence. ``mapping_used`` / ``confidence_available`` OR together.
    """
    if not acc:
        out = dict(incoming)
        out["_rate_weight"] = incoming.get("rows_parsed", 0) or 0
        return out

    a_rows = acc.get("_rate_weight", acc.get("rows_parsed", 0)) or 0
    b_rows = incoming.get("rows_parsed", 0) or 0
    total_w = a_rows + b_rows

    def wavg(key: str) -> Any:
        av, bv = acc.get(key), incoming.get(key)
        if av is None and bv is None:
            return None
        if total_w == 0:  # no row weights — simple mean of present values
            present = [v for v in (av, bv) if v is not None]
            return sum(present) / len(present) if present else None
        return ((av or 0) * a_rows + (bv or 0) * b_rows) / total_w

    return {
        "tables_parsed": (acc.get("tables_parsed", 0) or 0)
        + (incoming.get("tables_parsed", 0) or 0),
        "rows_parsed": total_w,
        "rows_mapped": (acc.get("rows_mapped", 0) or 0)
        + (incoming.get("rows_mapped", 0) or 0),
        "invocation_count": (acc.get("invocation_count", 0) or 0)
        + (incoming.get("invocation_count", 0) or 0),
        "parse_success_rate": wavg("parse_success_rate"),
        "avg_confidence": wavg("avg_confidence"),
        "confidence_available": bool(acc.get("confidence_available"))
        or bool(incoming.get("confidence_available")),
        "mapping_used": bool(acc.get("mapping_used"))
        or bool(incoming.get("mapping_used")),
        "_rate_weight": total_w,
    }


def _accumulate_metering(
    merged_metering: dict[str, Any], metering: dict[str, Any]
) -> None:
    """Accumulate per-model token-metering counts into ``merged_metering``.

    Token values from Bedrock responses may be ``None``; both operands are
    coerced to ``0`` so addition never raises on a ``None`` operand (issue #337).

    The ``_table_parsing_stats`` key is NOT a token counter — summing its rates /
    averages produces impossible values (500% success rate, 496% confidence), so
    it is merged with quality-aware semantics instead.
    """
    for mk, mv in metering.items():
        if mk in _NON_ADDITIVE_METERING_KEYS:
            if mk == "_table_parsing_stats" and isinstance(mv, dict):
                merged_metering[mk] = _merge_table_parsing_stats(
                    merged_metering.get(mk) or {}, mv
                )
            continue
        if mk not in merged_metering:
            merged_metering[mk] = dict(mv)
        else:
            for tk, tv in mv.items():
                merged_metering[mk][tk] = (merged_metering[mk].get(tk) or 0) + (tv or 0)


def _is_phantom_row(item: Any) -> bool:
    """True if a list item is a phantom row carrying no real data.

    A model (or OCR gap-recovery) sometimes appends rows that have at most one
    populated field — e.g. a lone sequential ``RowID`` with every other column
    null — when "continuing" a table past its real end. A genuine tabular row
    populates several columns, so a multi-field row object with fewer than two
    non-empty values is an artifact and is dropped on merge. Only applies to
    dict items with >= 3 declared fields, so sparse 1-2 field row schemas (and
    scalar list elements) are never affected.
    """
    if not isinstance(item, dict) or len(item) < 3:
        return False
    non_empty = sum(1 for v in item.values() if v not in (None, "") and str(v).strip())
    return non_empty < 2


def _prune_phantom_rows_from_assessment(
    extracted_fields: dict[str, Any], assessment: dict[str, Any]
) -> None:
    """Drop assessment rows whose DATA row is a phantom, keeping index alignment.

    The merge drops phantom data rows (:func:`_is_phantom_row`) from each list
    field, so ``merged_data[field][i]`` skips them. The per-shard assessment is
    concatenated in the same order but is NOT phantom-filtered, so without this a
    phantom row sitting *mid-list* would shift every following assessment row by
    one relative to the merged data — misattributing confidence AND (now that
    grounding is done per-shard and reused via ``skip_grounded``) the bounding
    box. Pruning the shard's assessment in lockstep here — using the SAME phantom
    predicate on the SAME data rows — means the persisted assessment lines up with
    the phantom-filtered merged data exactly. Mutates ``assessment`` in place;
    a no-op when the assessment list length doesn't match the data (defensive).
    """
    if not assessment:
        return
    for field, data_val in extracted_fields.items():
        if not isinstance(data_val, list):
            continue
        assessed = assessment.get(field)
        if not isinstance(assessed, list) or len(assessed) != len(data_val):
            # Not index-aligned (e.g. reconcile hasn't run for this field) — the
            # merge/reconcile will realign by length, so don't risk a mis-prune.
            continue
        keep = [i for i, row in enumerate(data_val) if not _is_phantom_row(row)]
        if len(keep) != len(data_val):
            assessment[field] = [assessed[i] for i in keep]


def _merge_shard_results(
    results: list[tuple[Any, dict[str, Any]]],
    data_format: type[BaseModel],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Merge per-shard extraction dicts into one.

    - **List fields** (by the schema's field types) are concatenated in shard
      order; a shard that returns the field as ``None`` contributes nothing.
      Phantom rows (multi-column row objects with <2 populated fields, e.g. a
      hallucinated trailing ``RowID``-only row) are dropped — see
      :func:`_is_phantom_row`.
    - **Scalar fields** take the FIRST non-null value across shards; a later,
      *different* non-null value is recorded as a conflict (first value wins).

    List membership is decided by ``data_format`` field types (not the runtime
    value) so an optional ``list | None`` returned as ``None`` by one shard and
    a list by another merges correctly rather than crashing.

    Returns ``(merged_dict, merged_metering, conflicts)``.
    """
    list_fields = _list_valued_fields(data_format)
    merged_dict: dict[str, Any] = {}
    merged_metering: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []

    for result_data, result_response in results:
        result_dict = result_data.model_dump(mode="json")
        for key, value in result_dict.items():
            is_list_field = key in list_fields or isinstance(value, list)
            if is_list_field:
                existing = merged_dict.get(key)
                if not isinstance(existing, list):
                    merged_dict[key] = []
                if isinstance(value, list):
                    merged_dict[key].extend(v for v in value if not _is_phantom_row(v))
            elif value is not None:
                if key not in merged_dict or merged_dict[key] is None:
                    merged_dict[key] = value
                elif merged_dict[key] != value:
                    conflicts.append(
                        {"field": key, "kept": merged_dict[key], "discarded": value}
                    )
            else:
                merged_dict.setdefault(key, None)
        _accumulate_metering(merged_metering, result_response.get("metering", {}))

    return merged_dict, merged_metering, conflicts


def merge_assessment_dicts(
    shard_assessments: list[dict[str, Any]],
    list_fields: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Collate per-shard assessment dicts into one section-level assessment.

    Mirrors :func:`_merge_shard_results` so the per-field assessment aligns with
    the merged extraction data:

    - **List-valued fields** (e.g. ``Transactions``) have their per-item
      assessment arrays concatenated in page order — the same order the data
      list items are concatenated — so ``explainability_info[0]["Transactions"][i]``
      lines up with ``inference_result["Transactions"][i]``.
    - **Scalar/group fields** take the FIRST shard that assessed them (a field
      lives on one page, so only one shard meaningfully assesses it; later shards
      that left it null contribute nothing).

    ``shard_assessments`` must already be ordered by ``page_start`` (the caller
    sorts). Each element is the ``{"assessment": {...}, "alerts": [...],
    "page_start": int, "page_end": int}`` dict produced by ``extract_one_shard``.
    Returns ``(merged_assessment, merged_alerts)``.
    """
    merged_assessment: dict[str, Any] = {}
    merged_alerts: list[dict[str, Any]] = []

    for shard in shard_assessments:
        assessment = shard.get("assessment") or {}
        merged_alerts.extend(shard.get("alerts") or [])
        for key, value in assessment.items():
            is_list_field = key in list_fields or isinstance(value, list)
            if is_list_field:
                existing = merged_assessment.get(key)
                if not isinstance(existing, list):
                    merged_assessment[key] = []
                if isinstance(value, list):
                    merged_assessment[key].extend(value)
            else:
                # Scalar/group: first shard to assess it wins (a populated
                # assessment for a field only one shard saw).
                if key not in merged_assessment:
                    merged_assessment[key] = value

    return merged_assessment, merged_alerts


# A shard payload is the dict produced by ExtractionService._build_shard_payloads:
#   {"content": <prompt content blocks>, "page_start": int, "page_end": int,
#    "total_pages": int}
ShardPayload = dict[str, Any]

# A shard runner takes (shard_index, total_shards, payload, **kwargs) and returns
# an awaitable of (data_format instance, response-with-metering dict). Injecting
# it keeps extract_one_shard decoupled from strands for testing.
ShardRunner = Callable[..., Awaitable[tuple[BaseModel, dict[str, Any]]]]

# An assess runner takes (extracted_fields, payload) and returns an awaitable of
# ``{"assessment": {...}, "alerts": [...], "metering": {...}}`` (or None to skip).
# Injected by ExtractionService so runtime.py stays free of the assessment stack.
AssessRunner = Callable[..., Awaitable["dict[str, Any] | None"]]


# --------------------------------------------------------------------------- #
# Per-shard persistence
# --------------------------------------------------------------------------- #
@runtime_checkable
class ShardPersistence(Protocol):
    """Persistence backend for per-shard extraction results.

    Implementations must be idempotent: ``load`` returns a previously persisted
    result (or ``None``) and ``save`` overwrites the deterministic key. Keys are
    derived from ``(section_id, page_start, page_end)`` so a re-run of the same
    shard maps to the same object — this is what lets both asyncio re-entry and
    SFN iteration retry "skip completed shards".
    """

    def load(self, section_id: str, page_start: int, page_end: int) -> dict | None: ...

    def save(
        self, section_id: str, page_start: int, page_end: int, result: dict
    ) -> None: ...


def shard_result_key(
    execution_arn: str, section_id: str, page_start: int, page_end: int
) -> str:
    """Deterministic S3 key for one shard's result.

    Extends the existing whole-section checkpoint convention
    (``checkpoints/{safe_arn}/{section_id}/extraction_state.json``) with a
    ``shards/`` subkey so per-shard results live alongside it.
    """
    safe_arn = (execution_arn or "local").replace(":", "_").replace("/", "_")
    return (
        f"checkpoints/{safe_arn}/{section_id}/shards/shard_{page_start}_{page_end}.json"
    )


class NoopShardPersistence:
    """No-op persistence (used by standalone/notebook runs with no S3)."""

    def load(self, section_id: str, page_start: int, page_end: int) -> dict | None:
        return None

    def save(
        self, section_id: str, page_start: int, page_end: int, result: dict
    ) -> None:
        return None


class S3ShardPersistence:
    """Idempotent per-shard result persistence in S3.

    Stores ``{"extracted_fields": {...}, "metering": {...}, "page_start": int,
    "page_end": int, "timestamp": str}`` at :func:`shard_result_key`. ``load``
    returns that dict (or ``None`` if absent / unreadable).
    """

    def __init__(self, bucket: str, execution_arn: str, s3_client: Any | None = None):
        self.bucket = bucket
        self.execution_arn = execution_arn
        self._s3 = s3_client

    @property
    def s3(self):  # lazy boto3 client
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3")
        return self._s3

    def load(self, section_id: str, page_start: int, page_end: int) -> dict | None:
        key = shard_result_key(self.execution_arn, section_id, page_start, page_end)
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=key)
            data = json.loads(resp["Body"].read().decode("utf-8"))
            logger.info(
                "Loaded completed shard result from s3://%s/%s", self.bucket, key
            )
            return data
        except Exception as e:  # noqa: BLE001 - NoSuchKey or transient: treat as miss
            name = type(e).__name__
            if name not in ("NoSuchKey", "ClientError", "404"):
                logger.warning("Shard result load failed (treating as miss): %s", e)
            return None

    def save(
        self, section_id: str, page_start: int, page_end: int, result: dict
    ) -> None:
        import time

        key = shard_result_key(self.execution_arn, section_id, page_start, page_end)
        body = dict(result)
        body.setdefault("page_start", page_start)
        body.setdefault("page_end", page_end)
        body.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=json.dumps(body, default=str).encode("utf-8"),
                ContentType="application/json",
            )
            logger.info("Saved shard result to s3://%s/%s", self.bucket, key)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to save shard result: %s", e)


# --------------------------------------------------------------------------- #
# Shard primitive — the single body used by BOTH asyncio and SFN
# --------------------------------------------------------------------------- #
async def extract_one_shard(
    *,
    shard_index: int,
    total_shards: int,
    payload: ShardPayload,
    model_id: str,
    data_format: type[BaseModel],
    config: IDPConfig,
    section_id: str,
    context: str = "Extraction",
    max_retries: int = 7,
    connect_timeout: float = 10.0,
    read_timeout: float = 600.0,
    max_tokens: int | None = None,
    checkpoint_callback: Any | None = None,
    custom_instruction: str | None = None,
    persistence: ShardPersistence | None = None,
    shard_runner: ShardRunner | None = None,
    assess_runner: "AssessRunner | None" = None,
    schema_validator: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run ONE shard's agent (or load a previously-completed result).

    Idempotent: if ``persistence`` already holds a complete result for this
    shard's page range, it is loaded and returned **without re-inferring**. This
    is the same function that the asyncio scheduler (:class:`InProcessRuntime`)
    runs as a task body AND that the SFN Distributed Map runs per iteration.

    When ``assess_runner`` is provided (integrated-assessment feature), a
    confidence/bbox assessment is run over THIS shard's pages right after its
    extraction (or carried from the persisted result on a cache hit), so
    assessment inherits the same per-shard scaling + idempotent resume as
    extraction. The per-shard assessment + alerts are persisted alongside the
    extracted fields and also returned inside ``response["_shard_assessment"]``
    for the merge step to collate (tuple arity is unchanged so existing callers
    and tests are unaffected). ``assess_runner`` stays injected (not imported)
    so this module remains free of the assessment/strands stack.

    Returns ``(extracted_fields_dict, response_with_metering)`` so callers that
    persist/serialise (SFN) don't need the live Pydantic instance.
    """
    page_start = payload["page_start"]
    page_end = payload["page_end"]

    # B5 — two-phase resume. A shard persists in two steps: (1) extraction alone
    # (``assessment_pending: True``) the moment the expensive agent loop finishes,
    # then (2) the complete record after assessment. So a shard Lambda that dies
    # DURING assessment (e.g. a slow confidence model) resumes on retry by REUSING
    # the persisted extraction and re-running ONLY assessment — the costly agent
    # loop is never repeated. ``cached_extraction`` carries the reused extraction
    # into the assessment phase below.
    cached_extraction: dict[str, Any] | None = None
    if persistence is not None:
        cached = persistence.load(section_id, page_start, page_end)
        if cached and cached.get("extracted_fields") is not None:
            assessment_done = cached.get("assessment") is not None or not cached.get(
                "assessment_pending", False
            )
            # Fully complete (extraction + assessment, or no assessment expected)
            # → skip entirely. This is the original fast path.
            if assessment_done or assess_runner is None:
                logger.info(
                    "Skipping shard %d/%d (pages %d-%d): complete result already "
                    "persisted",
                    shard_index + 1,
                    total_shards,
                    page_start,
                    page_end,
                )
                hit_response: dict[str, Any] = {"metering": cached.get("metering", {})}
                if cached.get("assessment") is not None:
                    hit_response["_shard_assessment"] = {
                        "assessment": cached.get("assessment"),
                        "alerts": cached.get("alerts", []),
                        "page_start": page_start,
                        "page_end": page_end,
                    }
                return cached["extracted_fields"], hit_response
            # Extraction done but assessment did NOT finish (a prior attempt timed
            # out mid-assessment) → reuse the extraction, re-run assessment only.
            logger.info(
                "Resuming shard %d/%d (pages %d-%d): extraction already persisted; "
                "re-running assessment only (prior attempt did not complete it)",
                shard_index + 1,
                total_shards,
                page_start,
                page_end,
            )
            cached_extraction = cached

    # Deterministic fault-injection hook (Phase 3 resume proof). When
    # EXTRACTION_FORCE_FAIL_SHARDS lists a 0-based page_start (comma-separated)
    # the chosen shard fails ONCE: on its first attempt it writes a "fail marker"
    # to persistence and raises; on a later SFN retry the marker is present so it
    # proceeds normally. This makes the *whole section's* first attempt fail
    # (some other shards complete + persist), and the SFN retry then loads those
    # completed shards from S3 and re-runs only the previously-failed one —
    # proving per-shard resume. Safe no-op unless the env var is set.
    _force = os.environ.get("EXTRACTION_FORCE_FAIL_SHARDS", "").strip()
    if _force and persistence is not None:
        fail_starts = {int(x) for x in _force.split(",") if x.strip().isdigit()}
        if page_start in fail_starts:
            marker = persistence.load(section_id, page_start, -page_end - 1)
            if marker is None:
                logger.warning(
                    "FORCED shard failure (test hook) for pages %d-%d — writing "
                    "fail marker so the SFN retry can resume it",
                    page_start,
                    page_end,
                )
                persistence.save(
                    section_id,
                    page_start,
                    -page_end - 1,
                    {"forced_fail": True, "extracted_fields": None},
                )
                raise RuntimeError(
                    f"Injected shard failure for pages {page_start}-{page_end} "
                    "(EXTRACTION_FORCE_FAIL_SHARDS)"
                )
            logger.warning(
                "Forced-fail shard pages %d-%d retrying after marker — proceeding",
                page_start,
                page_end,
            )

    response: dict[str, Any]
    if cached_extraction is not None:
        # B5 resume: reuse the persisted extraction; do NOT re-run the agent.
        extracted_fields = cached_extraction["extracted_fields"]
        response = {"metering": cached_extraction.get("metering", {})}
    else:
        if shard_runner is None:
            raise ValueError(
                "extract_one_shard requires a shard_runner (the strands-backed agent "
                "callable). Callers in idp_common pass agentic_idp.default_shard_runner; "
                "tests inject a fake. This keeps runtime.py strands-free."
            )
        data, response = await shard_runner(
            shard_index=shard_index,
            total_shards=total_shards,
            payload=payload,
            model_id=model_id,
            data_format=data_format,
            config=config,
            context=context,
            max_retries=max_retries,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_tokens=max_tokens,
            checkpoint_callback=checkpoint_callback,
            custom_instruction=custom_instruction,
            # Opaque to this module (strands-free): the in-loop full-schema check
            # the in-process path has always had. Without it a shard had ONLY the
            # Pydantic guard, and once scalar leaves became nullable (#782) a shard
            # returning nulls had no self-correction round at all.
            schema_validator=schema_validator,
        )
        extracted_fields = data.model_dump(mode="json")

        # B5 — Persist the EXTRACTION checkpoint before assessment runs, flagged
        # ``assessment_pending`` so a crash during the (potentially slow) assessment
        # phase lets the retry skip the agent loop and re-run assessment only. Only
        # when a separate assessment pass is expected (assess_runner set); integrated
        # mode already carries confidence inline so there is nothing to defer.
        if persistence is not None and assess_runner is not None:
            persistence.save(
                section_id,
                page_start,
                page_end,
                {
                    "extracted_fields": extracted_fields,
                    "metering": response.get("metering", {}),
                    "assessment_pending": True,
                },
            )

    # In-shard assessment (integrated-assessment feature). Runs over THIS shard's
    # pages/values only, so it scales the same way extraction does. Best-effort:
    # an assessment failure must never fail the shard's extraction.
    shard_assessment: dict[str, Any] | None = None
    if assess_runner is not None:
        try:
            assess_out = await assess_runner(
                extracted_fields=extracted_fields,
                payload=payload,
            )
            if assess_out is not None:
                shard_assessment = {
                    "assessment": assess_out.get("assessment", {}),
                    "alerts": assess_out.get("alerts", []),
                    "split_stats": assess_out.get("split_stats"),
                    "page_start": page_start,
                    "page_end": page_end,
                }
                _accumulate_metering(
                    response.setdefault("metering", {}),
                    assess_out.get("metering", {}),
                )
        except Exception as e:  # noqa: BLE001 - assessment is advisory
            logger.warning(
                "In-shard assessment failed for pages %d-%d (keeping extraction): %s",
                page_start,
                page_end,
                e,
            )
    else:
        # Integrated-assessment mode: the extraction agent emitted per-field
        # confidence/bbox INLINE (one inference, no second pass). The shard
        # runner surfaced it in the response metering; lift it into the same
        # _shard_assessment slot so collation/reconcile/grounding are identical
        # to separate mode. Pop it out of metering so it doesn't leak downstream.
        inline = (response.get("metering") or {}).pop(
            "_integrated_field_assessment", None
        )
        if inline:
            shard_assessment = {
                "assessment": inline,
                "alerts": [],
                "page_start": page_start,
                "page_end": page_end,
            }

    if persistence is not None:
        persisted: dict[str, Any] = {
            "extracted_fields": extracted_fields,
            "metering": response.get("metering", {}),
        }
        if shard_assessment is not None:
            persisted["assessment"] = shard_assessment["assessment"]
            persisted["alerts"] = shard_assessment["alerts"]
            if shard_assessment.get("split_stats"):
                persisted["split_stats"] = shard_assessment["split_stats"]
        persistence.save(section_id, page_start, page_end, persisted)

    if shard_assessment is not None:
        response["_shard_assessment"] = shard_assessment

    return extracted_fields, response


# --------------------------------------------------------------------------- #
# Merge primitive — public wrapper over the single merge implementation
# --------------------------------------------------------------------------- #
def merge_shard_results(
    shard_results: list[tuple[Any, dict[str, Any]]],
    data_format: type[BaseModel],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Merge per-shard results into one section result.

    Thin, stable public wrapper over the pure ``_merge_shard_results`` so the
    merge logic has exactly one implementation (``agentic_idp`` re-exports it).
    ``shard_results`` is a list of ``(data_format_instance,
    response_with_metering)`` tuples (the same shape the in-process scheduler
    collects). Returns ``(merged_dict, merged_metering, conflicts)``.
    """
    return _merge_shard_results(shard_results, data_format)


def merge_shard_dicts(
    shard_dicts: list[dict[str, Any]],
    data_format: type[BaseModel],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Merge per-shard results given plain dicts (the SFN path).

    The Distributed Map persists/returns each shard as
    ``{"extracted_fields": {...}, "metering": {...}, "page_start": int, ...}``.
    This validates each into ``data_format`` and reuses :func:`merge_shard_results`
    so the SFN path and the in-process path share one merge implementation.
    Shards are sorted by ``page_start`` so list fields concatenate in page order
    regardless of completion order.
    """
    ordered = sorted(shard_dicts, key=lambda d: d.get("page_start", 0))
    tuples = []
    for d in ordered:
        response: dict[str, Any] = {"metering": d.get("metering", {})}
        if d.get("assessment") is not None:
            response["_shard_assessment"] = {
                "assessment": d.get("assessment"),
                "alerts": d.get("alerts", []),
                "split_stats": d.get("split_stats"),
                "page_start": d.get("page_start", 0),
                "page_end": d.get("page_end", 0),
            }
        tuples.append((data_format(**d["extracted_fields"]), response))
    merged_dict, merged_metering, conflicts = merge_shard_results(tuples, data_format)
    # Collate in-shard assessments into metering (parity with _finalize_merge) so
    # the SFN merge step surfaces explainability_info exactly like the in-process
    # path. No-op when no shard carried an assessment.
    collated = collate_shard_assessments(tuples, data_format)
    if collated is not None:
        merged_assessment, merged_alerts = collated
        merged_metering["_merged_assessment"] = merged_assessment
        merged_metering["_merged_assessment_alerts"] = merged_alerts
        agg_split = _aggregate_shard_split_stats(tuples)
        if agg_split is not None:
            merged_metering["_merged_assessment_split_stats"] = agg_split
    return merged_dict, merged_metering, conflicts


# --------------------------------------------------------------------------- #
# Runtime interface + implementations
# --------------------------------------------------------------------------- #
class ExtractionRuntime(abc.ABC):
    """Pluggable scheduler over the shard primitives. Subclasses only decide
    *how* shards are scheduled; they all call :func:`extract_one_shard` +
    :func:`merge_shard_results` so behaviour cannot diverge."""

    name: str = "abstract"

    @abc.abstractmethod
    async def run(
        self,
        *,
        shard_payloads: list[ShardPayload],
        model_id: str,
        data_format: type[BaseModel],
        config: IDPConfig,
        section_id: str,
        context: str = "Extraction",
        max_retries: int = 7,
        connect_timeout: float = 10.0,
        read_timeout: float = 600.0,
        max_tokens: int | None = None,
        checkpoint_callback: Any | None = None,
        custom_instruction: str | None = None,
        persistence: ShardPersistence | None = None,
        shard_runner: ShardRunner | None = None,
        assess_runner: AssessRunner | None = None,
        schema_validator: Any | None = None,
    ) -> tuple[BaseModel, dict[str, Any]]:
        """Run all shards and return ``(merged data_format instance, response)``."""
        raise NotImplementedError


class InProcessRuntime(ExtractionRuntime):
    """Default runtime: asyncio.gather + semaphore over :func:`extract_one_shard`.

    Identical observable behaviour to the historical
    ``concurrent_structured_output_async`` — used by notebook / CLI / single
    Lambda. Sharding fully works here with no SFN dependency.
    """

    name = "in_process"

    def __init__(self, max_parallelism: int):
        self.max_parallelism = max(1, int(max_parallelism))

    async def run(
        self,
        *,
        shard_payloads: list[ShardPayload],
        model_id: str,
        data_format: type[BaseModel],
        config: IDPConfig,
        section_id: str,
        context: str = "Extraction",
        max_retries: int = 7,
        connect_timeout: float = 10.0,
        read_timeout: float = 600.0,
        max_tokens: int | None = None,
        checkpoint_callback: Any | None = None,
        custom_instruction: str | None = None,
        persistence: ShardPersistence | None = None,
        shard_runner: ShardRunner | None = None,
        assess_runner: AssessRunner | None = None,
        schema_validator: Any | None = None,
    ) -> tuple[BaseModel, dict[str, Any]]:
        total_shards = len(shard_payloads)
        logger.info(
            "InProcessRuntime: %d shard(s), parallelism %d, ranges %s",
            total_shards,
            self.max_parallelism,
            [(p["page_start"], p["page_end"]) for p in shard_payloads],
        )
        semaphore = asyncio.Semaphore(self.max_parallelism)

        async def _run_one(i: int, payload: ShardPayload):
            async with semaphore:
                fields, response = await extract_one_shard(
                    shard_index=i,
                    total_shards=total_shards,
                    payload=payload,
                    model_id=model_id,
                    data_format=data_format,
                    config=config,
                    section_id=section_id,
                    context=context,
                    max_retries=max_retries,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    max_tokens=max_tokens,
                    checkpoint_callback=checkpoint_callback,
                    custom_instruction=custom_instruction,
                    persistence=persistence,
                    shard_runner=shard_runner,
                    assess_runner=assess_runner,
                    schema_validator=schema_validator,
                )
                # Re-hydrate into a model instance for the shared merge helper.
                return data_format(**fields), response

        results = await asyncio.gather(
            *[_run_one(i, p) for i, p in enumerate(shard_payloads)]
        )
        return _finalize_merge(results, data_format)


def collate_shard_assessments(
    results: list[tuple[Any, dict[str, Any]]],
    data_format: type[BaseModel],
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Pull per-shard ``_shard_assessment`` off the responses and collate them.

    Returns ``(merged_assessment, merged_alerts)`` page-ordered, or ``None`` when
    no shard produced an assessment (integrated-assessment disabled). Shared by
    the in-process and SFN merge paths so collation has one implementation.
    """
    shard_assessments = [
        resp["_shard_assessment"]
        for _data, resp in results
        if isinstance(resp, dict) and resp.get("_shard_assessment") is not None
    ]
    if not shard_assessments:
        return None
    shard_assessments.sort(key=lambda s: s.get("page_start", 0))
    list_fields = _list_valued_fields(data_format)
    return merge_assessment_dicts(shard_assessments, list_fields)


def _aggregate_shard_split_stats(
    results: list[tuple[Any, dict[str, Any]]],
) -> dict[str, Any] | None:
    """Sum per-shard assessment ``split_stats`` (adaptive batch-splitting activity)
    across all shards, for visibility. None when no shard recorded any."""
    from idp_common.assessment.batching import merge_split_stats

    merged: dict[str, Any] | None = None
    for _data, resp in results:
        if not isinstance(resp, dict):
            continue
        sa = resp.get("_shard_assessment")
        if isinstance(sa, dict) and sa.get("split_stats"):
            merged = merge_split_stats(merged, sa["split_stats"])
    return merged


def _finalize_merge(
    results: list[tuple[BaseModel, dict[str, Any]]],
    data_format: type[BaseModel],
) -> tuple[BaseModel, dict[str, Any]]:
    """Merge collected shard results and build the merged response envelope.

    Shared by InProcessRuntime and the SFN merge state so the merged-response
    shape (including the ``_shard_scalar_conflicts`` marker) is identical.
    """
    merged_dict, merged_metering, conflicts = merge_shard_results(results, data_format)
    merged_result = data_format(**merged_dict)

    total_items = sum(len(v) for v in merged_dict.values() if isinstance(v, list))
    logger.info(
        "Sharded extraction merge complete: %d list-item(s), %d shard(s), %d conflict(s)",
        total_items,
        len(results),
        len(conflicts),
    )
    if conflicts:
        logger.warning("Scalar field conflicts across shards (kept first value)")
        merged_metering["_shard_scalar_conflicts"] = conflicts

    response = {
        "response": {
            "output": {
                "message": {"role": "assistant", "content": [{"text": "merged"}]}
            }
        },
        "metering": merged_metering,
    }
    # Surface the collated per-field assessment (integrated-assessment feature)
    # so the service can ground it and emit explainability_info. Absent (None)
    # when in-shard assessment did not run — zero change to the default path.
    # Rides inside ``metering`` (like ``_shard_scalar_conflicts``) so it survives
    # the typed BedrockInvokeModelResponse envelope that callers re-wrap into;
    # the service pops it out in _save_results.
    collated = collate_shard_assessments(results, data_format)
    if collated is not None:
        merged_assessment, merged_alerts = collated
        merged_metering["_merged_assessment"] = merged_assessment
        merged_metering["_merged_assessment_alerts"] = merged_alerts
        agg_split = _aggregate_shard_split_stats(results)
        if agg_split is not None:
            merged_metering["_merged_assessment_split_stats"] = agg_split
    return merged_result, response


class StepFunctionsRuntime(ExtractionRuntime):
    """Marker runtime selected by the infra layer for the nested SFN Distributed
    Map. The library does NOT orchestrate Step Functions; the Lambda/ASL layer
    drives :func:`extract_one_shard` per iteration and :func:`merge_shard_dicts`
    in the merge state. Calling :meth:`run` in-process simply delegates to the
    in-process scheduler so standalone callers never break — the SFN selection is
    purely an infra concern."""

    name = "step_functions"

    def __init__(self, max_parallelism: int):
        self._fallback = InProcessRuntime(max_parallelism)

    async def run(self, **kwargs) -> tuple[BaseModel, dict[str, Any]]:
        logger.info(
            "StepFunctionsRuntime.run() called in-process; delegating to "
            "InProcessRuntime (SFN orchestration is driven by the Lambda/ASL layer)."
        )
        return await self._fallback.run(**kwargs)


# Runtime selection -------------------------------------------------------- #
RUNTIME_ENV_VAR = "EXTRACTION_RUNTIME"


def select_runtime(
    config: IDPConfig,
    max_parallelism: int,
    *,
    override: str | None = None,
) -> ExtractionRuntime:
    """Choose the runtime backend.

    Resolution order: explicit ``override`` arg > ``config.extraction.agentic.
    runtime`` (if present) > ``EXTRACTION_RUNTIME`` env var > default
    ``in_process``. Standalone/notebook usage gets ``InProcessRuntime`` with no
    configuration, preserving today's behaviour.
    """
    choice = override
    if not choice:
        agentic = getattr(config.extraction, "agentic", None)
        choice = getattr(agentic, "runtime", None) if agentic else None
    if not choice:
        choice = os.environ.get(RUNTIME_ENV_VAR)
    choice = (choice or "in_process").strip().lower()

    if choice in ("step_functions", "stepfunctions", "sfn"):
        return StepFunctionsRuntime(max_parallelism)
    return InProcessRuntime(max_parallelism)
