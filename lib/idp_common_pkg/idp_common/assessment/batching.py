# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared large-list assessment batching + reconciliation.

A single assessment inference over a large list field (e.g. a 120-row
transaction table) is unreliable: the model under-enumerates or omits the list
entirely, leaving most rows unassessed. This module holds the two primitives
that make large-list assessment robust, factored out so BOTH the standalone
Assessment step (``AssessmentService.process_document_section``) and the agentic
in-shard path (``ExtractionService``) share exactly one implementation:

- :func:`reconcile_assessment_to_data` — force the per-field assessment to
  index-align with the extracted data (truncate over-long lists, pad short/omitted
  ones with per-sub-field placeholders, fan a per-row confidence out to per-column
  leaves) so ``explainability_info[0][field][i]`` lines up with
  ``inference_result[field][i]`` for every list cell.
- :func:`assess_results_batched` — slice the single largest oversized list field
  into ``list_batch_size`` chunks, assess each chunk with the SAME scalars/context,
  concatenate the per-row assessments in order, and reconcile against the full data.

The module is deliberately import-light (no strands / PIL / boto3 at top level)
so it is cheap to import from the assessment service and unit-testable without the
agentic stack. Metering is accumulated with ``utils.merge_metering_data`` — the
canonical token-count merge — so this module carries no dependency on the
extraction package.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Iterator
from typing import Any, Callable

from idp_common import utils

logger = logging.getLogger(__name__)

# --- Token-aware batch sizing (see compute_token_aware_batch_size) ------------
# The per-row/per-cell token estimate, the output safety fraction and the absolute
# batch ceiling now live in ``idp_common.bedrock.sizing`` and are imported here, so
# there is ONE estimator. They used to be duplicated between the two modules, and
# the copies disagreed by a factor of the column count.
# Legacy value-length-based multiplier, kept ONLY as the fallback estimate for
# scalar/opaque rows where per-column counting doesn't apply (non-dict rows).
_CONFIDENCE_ENVELOPE_MULTIPLIER = 8.0
# Bounding-box multiplier for that same opaque-row fallback. Mirrors
# ``bedrock.sizing._BBOX_GEOMETRY_MULTIPLIER``; kept local because the fallback
# applies it to a value-length estimate rather than to a per-column one.
_BBOX_GEOMETRY_MULTIPLIER_FALLBACK = 3.0
# Rows are not guaranteed uniform — an extraction can omit a key on some rows — and
# sizing off row 0 alone under-counts the width of every other row, which inflates
# the batch. Counting keys over EVERY row is O(rows x columns) on data already in
# memory, so there is no sampling window: a window would just move the same bug
# from row 0 to the first N rows (an 800-row statement whose first 25 rows are
# narrow would still be sized as if the whole list were narrow).

# Wall-clock safety reserve (seconds). Before starting a NEW escalation round —
# the slow, big-model call — the ladder checks that an estimated round fits in
# the Lambda's remaining time minus this reserve. If not, it stops and flags
# ``deadline_reached`` rather than risk a hard task timeout (which the ASL now
# retries, but we still prefer to avoid). See plan "Lambda timeout & resume".
_DEADLINE_SAFETY_RESERVE_SECONDS = 90.0

# --- Confidence-coverage shortfall thresholds (#901 item 3) -------------------
# ``audit_explainability`` knows exactly how many extracted list rows carry a real
# confidence score. When materially fewer rows are scored than were extracted, the
# document is still returned (extraction is correct and paid for) but its
# confidence surface can no longer be used for HITL triage — so say so out loud
# instead of reporting plain success, which is what happened in #901 (coverage fell
# to ~24% of the leaves a smaller-shard run produced, with the document reporting
# success and no issue at all).
#
# Why 5% for "materially short": reconciliation pads the assessment to exactly one
# entry per extracted row, so a run whose model scored every row lands at 0%
# shortfall. The threshold is not 0 because the ladder ALREADY reports small
# residual shortfalls precisely (``assessment_incomplete`` names the exact row
# count), and a 1-2 row gap on an 800-row table is that issue's job, not a second
# document-level alarm — 5% is the point where a reader should stop trusting the
# confidence surface as a whole rather than a few rows in it.
#
# Why 25% for error severity: at a quarter of the rows unscored the surface is no
# longer a usable sample of the document — the #901 run was ~76% unscored. Below
# that it is a warning: coverage is degraded but the scored majority is still
# informative.
#
# Why an ABSOLUTE floor on the error rung as well as the fraction: a fraction alone
# makes short lists fire hardest. One unscored row in a four-row list is 25%, and an
# error renders the section red ("Incomplete") in the Sections panel — so a shipped
# two-entry list attribute (e.g. ENDORSEMENTS in lending-package-sample) with a
# single ``None`` confidence leaf would present as an error-severity document
# defect. Error severity is reserved for a shortfall that is large in ABSOLUTE rows
# of unreviewable data, not merely large as a proportion of a tiny list: below the
# floor the same shortfall is still reported, as a warning ("Degraded"), and the
# ladder's own ``assessment_incomplete`` still names the exact rows. 10 unscored
# rows is more data than a reviewer can be assumed to spot-check by hand, and it is
# ~1% of the #901 shape (912 of 1,200 unscored), so the case the guard exists for is
# unaffected.
_COVERAGE_SHORTFALL_WARNING_FRACTION = 0.05
_COVERAGE_SHORTFALL_ERROR_FRACTION = 0.25
_COVERAGE_SHORTFALL_ERROR_MIN_UNSCORED_ROWS = 10


def _deadline_allows(deadline_epoch: float | None, estimated_seconds: float) -> bool:
    """True if ``estimated_seconds`` of work fits before ``deadline_epoch`` minus
    the safety reserve. Always True when no deadline was threaded in (local /
    non-Lambda use), so behavior is unchanged outside Lambda."""
    if deadline_epoch is None:
        return True
    remaining = deadline_epoch - time.time()
    return remaining - _DEADLINE_SAFETY_RESERVE_SECONDS >= estimated_seconds


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


# A path that addresses a list row, e.g. ``cost_elements[3].unit_cost``. In the
# batched flow, row indexes in alert paths are local to the slice that produced
# them (the core enumerates the rows it was handed), so two slices can emit the
# same indexed path for DIFFERENT rows. Indexed alerts are therefore never
# deduped here — an alert we cannot key uniquely is an alert we must not drop.
_INDEXED_ALERT_PATH_RE = re.compile(r"\[\d+\]")


def dedupe_alerts(alerts: Any) -> Any:
    """Collapse repeated confidence alerts for the same non-indexed attribute path.

    Every slice is assessed with the SAME scalars/context (see the module
    docstring), so a non-list attribute is re-assessed once per slice and
    contributes its alert once per slice. The merge already treats those repeats
    as redundant for the *assessment* — it keeps the first slice's scalars and
    discards the rest — but the alert list is a plain ``extend``, so the same
    finding lands N times. On a 512-row workbook that produced 2,603 alerts over
    16 distinct group paths (~163 copies each), and the duplicated list was the
    dominant share of a tracking item that breached DynamoDB's 409,600-byte
    ceiling: the write is where a document is LOST, because a section that will
    not fit fails the whole run with no result at all.

    This makes the alerts obey the rule the assessment merge already applies,
    and it drops no finding:

    - Only paths WITHOUT a row index are deduped. Indexed paths pass through
      untouched (see ``_INDEXED_ALERT_PATH_RE`` above for why).
    - Where two copies of one path disagree, the LOWEST confidence wins — the
      alert asserts "this attribute scored below threshold", and the worst
      score is the strongest true form of that claim. A missing or non-numeric
      ``confidence`` coerces to 1.0, so a scoreless copy never displaces a
      scored one. Deduping on the whole record instead would let float jitter
      across slices defeat the collapse.
    - Entries that are not dicts, or that carry no string ``attribute_name``,
      pass through untouched.
    - First-appearance order is preserved, and the collapse is idempotent.

    PRECONDITION on the caller: within one list, a non-indexed path must
    identify one finding. Every producer that reaches the current call sites
    satisfies this by construction — they walk the assessment **dict**, and list
    rows always carry an ``[i]`` suffix, so a single pass cannot emit the same
    non-indexed path twice; repeats can only come from re-assessing the same
    scalars. The BDA path is the counter-example and must NOT be routed here:
    ``patterns/unified/src/bda_processresults_function/index.py`` builds alerts
    by iterating *pages* and using the raw key-value key as ``attribute_name``,
    so the same key found on two pages is two findings sharing one path.
    (It assigns ``section.confidence_threshold_alerts`` directly and never
    reaches this function; if that ever changes, put the page in the path
    first.)
    """
    if not isinstance(alerts, list):
        return alerts

    kept: list[Any] = []
    index_by_name: dict[str, int] = {}

    for alert in alerts:
        name = alert.get("attribute_name") if isinstance(alert, dict) else None
        if not isinstance(name, str) or _INDEXED_ALERT_PATH_RE.search(name):
            kept.append(alert)
            continue
        seen_at = index_by_name.get(name)
        if seen_at is None:
            index_by_name[name] = len(kept)
            kept.append(alert)
            continue
        if _to_float(alert.get("confidence"), 1.0) < _to_float(
            kept[seen_at].get("confidence"), 1.0
        ):
            kept[seen_at] = alert

    if len(kept) != len(alerts):
        logger.info(
            "Collapsed %d duplicate confidence alerts (%d -> %d); scalars are "
            "re-assessed once per slice and each repeat re-alerted.",
            len(alerts) - len(kept),
            len(alerts),
            len(kept),
        )
    return kept


def enrich_assessment_with_thresholds(
    assessment: dict[str, Any],
    class_schema: dict[str, Any],
    default_confidence_threshold: float = 0.9,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Attach ``confidence_threshold`` to every confidence leaf and build alerts.

    The *integrated* confidence paths (the extraction inference emits confidence
    inline, whether via the simple free-text prompt or the agentic tool call)
    produce raw ``{confidence, confidence_reason}`` leaves with NO threshold and
    NO ``confidence_threshold_alerts`` — unlike the standalone/separate path,
    which enriches them in ``AssessmentService.assess_results``. This helper adds
    the same enrichment so all confidence modes share one output contract:
    each leaf gains ``confidence_threshold`` (from the field's
    ``x-aws-idp-confidence-threshold`` or the default), and a flat list of
    threshold-violation alerts is returned.

    Pure/importable (no S3/Bedrock). Mutates a copy; returns
    ``(enriched_assessment, alerts)``. Scalars, groups, and list rows (per-column
    leaves) are all handled recursively.
    """
    from idp_common.config.schema_constants import (
        SCHEMA_PROPERTIES,
        SCHEMA_TYPE,
        TYPE_ARRAY,
        X_AWS_IDP_CONFIDENCE_THRESHOLD,
    )
    from idp_common.config.schema_utils import deref_schema

    if not isinstance(assessment, dict):
        return assessment, []
    properties = (class_schema or {}).get(SCHEMA_PROPERTIES, {}) or {}
    alerts: list[dict[str, Any]] = []

    def _enrich_leaf_container(node: Any, threshold: float, path: str) -> Any:
        """Recursively add threshold to every {confidence,...} leaf under node."""
        if isinstance(node, dict):
            if "confidence" in node:
                conf = (
                    _to_float(node.get("confidence"), None)
                    if node.get("confidence") is not None
                    else None
                )
                out = {**node, "confidence_threshold": threshold}
                if conf is not None and conf < threshold:
                    alerts.append(
                        {
                            "attribute_name": path,
                            "confidence": conf,
                            "confidence_threshold": threshold,
                        }
                    )
                return out
            return {
                k: _enrich_leaf_container(v, threshold, f"{path}.{k}" if path else k)
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [
                _enrich_leaf_container(v, threshold, f"{path}[{i}]")
                for i, v in enumerate(node)
            ]
        return node

    def _enrich_leaf_with_field_thresholds(
        node: Any, field_thresholds: dict[str, float], fallback: float, path: str
    ) -> Any:
        """Enrich a list-item dict using per-sub-field thresholds."""
        if not isinstance(node, dict):
            return _enrich_leaf_container(node, fallback, path)
        if "confidence" in node:
            # Shouldn't happen at item level, but safety
            return _enrich_leaf_container(node, fallback, path)
        result = {}
        for k, v in node.items():
            sub_threshold = field_thresholds.get(k, fallback)
            sub_path = f"{path}.{k}" if path else k
            result[k] = _enrich_leaf_container(v, sub_threshold, sub_path)
        return result

    enriched: dict[str, Any] = {}
    for attr_name, attr_assessment in assessment.items():
        prop_schema = properties.get(attr_name, {}) or {}
        threshold = _to_float(
            prop_schema.get(
                X_AWS_IDP_CONFIDENCE_THRESHOLD, default_confidence_threshold
            ),
            default_confidence_threshold,
        )

        # For array-type fields, resolve per-sub-field thresholds from $ref/$defs.
        # ``type`` may be a union list (e.g. ["array", "null"]), so normalize.
        #
        # Read the type and the ``items`` shape off the DEREFERENCED subschema:
        # a property declared as ``{"$ref": "#/$defs/TxnList"}`` carries neither,
        # so the raw read left this path resolving zero per-sub-field thresholds
        # and falling back to the uniform container threshold — while the
        # standalone path (``AssessmentService._assess_core``) resolved them, so
        # the same schema and the same confidences produced HITL alerts in
        # ``confidence: separate`` and none in ``integrated``. The threshold on
        # the property itself stays a RAW read, matching _assess_core: honoring
        # one declared on the $defs definition is a threshold-INHERITANCE change.
        deref_prop_schema = deref_schema(prop_schema, class_schema)
        raw_type = deref_prop_schema.get(SCHEMA_TYPE)
        declared_types = raw_type if isinstance(raw_type, list) else [raw_type]
        is_array = TYPE_ARRAY in declared_types or isinstance(attr_assessment, list)
        if is_array and isinstance(attr_assessment, list):
            from idp_common.assessment.threshold_resolver import (
                resolve_array_item_thresholds,
            )

            item_thresholds = resolve_array_item_thresholds(
                deref_prop_schema, class_schema, threshold
            )
            if item_thresholds:
                enriched[attr_name] = [
                    _enrich_leaf_with_field_thresholds(
                        item, item_thresholds, threshold, f"{attr_name}[{i}]"
                    )
                    for i, item in enumerate(attr_assessment)
                ]
            else:
                # No per-sub-field thresholds resolved — use uniform threshold
                enriched[attr_name] = _enrich_leaf_container(
                    attr_assessment, threshold, attr_name
                )
        else:
            enriched[attr_name] = _enrich_leaf_container(
                attr_assessment, threshold, attr_name
            )
    return enriched, alerts


def reconcile_assessment_to_data(
    assessment: dict[str, Any], extraction_results: dict[str, Any]
) -> dict[str, Any]:
    """Force per-field assessment to index-align with the extracted data.

    The assessment LLM frequently emits a *different* number of list-item
    assessments than the data has rows (a 120-row table may come back with
    only 44 row assessments). Downstream consumers (HITL, UI) index
    ``explainability_info[0][field][i]`` against ``inference_result[field][i]``,
    so a length mismatch silently misattributes confidence to the wrong row —
    and in the sharded path the drift compounds across shards on merge.

    For every list-valued data field this truncates an over-long assessment
    list and pads a too-short one so ``len(assessment[field]) ==
    len(data[field])`` exactly — including the case where the model OMITTED
    the list field entirely (common for large tables: the shard extracted N
    rows but the assessment response left the field out, so without this every
    such row would be unassessed AND ungroundable).

    Crucially, each padded row is a **per-sub-field placeholder mirroring the
    data row's structure** — a ``{"confidence": null, ...}`` leaf for each
    sub-field the data row populated (e.g. ``date``, ``description``,
    ``amount``). This gives OCR geometry grounding a real value to match per
    sub-field, so an un-assessed row still gets a correct bounding box from its
    extracted values; only the LLM ``confidence`` is null. A scalar/non-dict
    row element falls back to a single neutral leaf.

    Scalar/group fields are left untouched. Mutates and returns ``assessment``.
    """
    if not isinstance(assessment, dict):
        return assessment

    def _row_placeholder(data_row: Any) -> dict[str, Any]:
        reason = (
            "Not individually assessed (assessment returned fewer items "
            "than were extracted)."
        )
        # Mirror the data row's sub-fields so grounding can attach a box per
        # populated sub-field from its actual value.
        if isinstance(data_row, dict):
            leaves = {
                sub: {"confidence": None, "confidence_reason": reason}
                for sub, sv in data_row.items()
                if sv is not None and not isinstance(sv, (dict, list))
            }
            if leaves:
                return leaves
        # Scalar row element (or all-null/nested row): single neutral leaf.
        return {"confidence": None, "confidence_reason": reason}

    def _expand_row_to_per_column(row_assess: Any, data_row: Any) -> Any:
        """Normalize a per-ROW confidence into per-COLUMN leaves.

        Some models (esp. integrated mode) emit ONE ``{"confidence", ...}`` object
        for an entire list row. Downstream (HITL, UI, grounding) index confidence
        per sub-field, so when the data row is a dict but the assessment row is a
        single confidence leaf, fan that one score out across the row's populated
        scalar columns (preserving the model's confidence/reason on each). Rows
        that already carry per-column leaves, or scalar row elements, pass through.
        """
        if (
            isinstance(row_assess, dict)
            and "confidence" in row_assess
            and isinstance(data_row, dict)
        ):
            leaf = {
                "confidence": row_assess.get("confidence"),
                "confidence_reason": row_assess.get("confidence_reason"),
            }
            cols = {
                sub: dict(leaf)
                for sub, sv in data_row.items()
                if sv is not None and not isinstance(sv, (dict, list))
            }
            if cols:
                return cols
        return row_assess

    for field, data_val in extraction_results.items():
        if not isinstance(data_val, list):
            continue
        target = len(data_val)
        assessed = assessment.get(field)
        assessed = assessed if isinstance(assessed, list) else []
        if len(assessed) > target:
            assessed = assessed[:target]
        elif len(assessed) < target:
            assessed = assessed + [
                _row_placeholder(data_val[i]) for i in range(len(assessed), target)
            ]
        # Normalize any per-row scalar confidence to per-column leaves so every
        # list-item field gets its own confidence + geometry downstream.
        assessment[field] = [
            _expand_row_to_per_column(assessed[i], data_val[i]) for i in range(target)
        ]
    return assessment


def _iter_confidence_leaves(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every confidence leaf in an assessment subtree.

    A leaf is a dict carrying a ``confidence`` key. Groups (nested objects) and
    inner lists are traversed rather than mistaken for leaves.
    """
    if isinstance(node, dict):
        if "confidence" in node:
            yield node
            return
        for value in node.values():
            yield from _iter_confidence_leaves(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_confidence_leaves(item)


def _row_confidence_missing(row_assess: Any) -> bool:
    """True if a reconciled list-row assessment still lacks a real confidence.

    A row is 'missing' when it carries no confidence leaf at all (the null
    placeholder case) or when any leaf it does carry has ``confidence is None``
    — i.e. the model didn't actually score it.

    **Recurses.** It used to look one level down only::

        leaves = [v for v in row_assess.values() if isinstance(v, dict)]
        return any(leaf.get("confidence") is None for leaf in leaves)

    which treated a NESTED GROUP inside a row as a leaf. A group has no
    ``confidence`` key of its own, so ``leaf.get("confidence")`` was None and the
    row was reported unscored **however well the model had scored it** — and an
    inner list was skipped entirely by the ``isinstance(v, dict)`` filter, so a
    row consisting only of scalars-plus-a-list could not be judged at all.

    Measured live on a 3-record pay statement whose rows carry an ``Employee``
    group and an ``Earnings`` list: every leaf came back at 0.99–1.0 confidence
    with OCR geometry, ``truncated_calls: 0`` — and the section still reported
    ``assessment_incomplete`` (**error**, rendering it Incomplete in the UI) for
    all 3 rows after burning a `claude-sonnet-5:1m` escalation call that
    recovered 0, because a stronger model reproduces the identical shape. The
    ladder had no way out.

    Pre-existing for any list-of-object attribute whose rows contain a group or
    an inner list; multi-instance sections (#715) make every record a row, so it
    became universal there.
    """
    if not isinstance(row_assess, dict):
        return True
    leaves = list(_iter_confidence_leaves(row_assess))
    if not leaves:
        return True
    return any(leaf.get("confidence") is None for leaf in leaves)


def _missing_row_indices(assessment_list: Any, data_list: Any) -> list[int]:
    if not isinstance(assessment_list, list) or not isinstance(data_list, list):
        return []
    n = min(len(assessment_list), len(data_list))
    return [i for i in range(n) if _row_confidence_missing(assessment_list[i])]


def _schema_field_mismatch_reason(
    field: str, class_schema: dict[str, Any] | None
) -> str | None:
    """Return a human-readable reason a list ``field`` can NEVER be row-scored,
    or None when the field IS validly list-typed in the class schema.

    The confidence enhancer (``AssessmentService.assess_results``) keys per-row
    scoring off the schema: a field the schema declares ``type: array`` gets a
    list of per-row leaves, but a field that is MISSING from the schema (an
    off-schema/hallucinated attribute) or declared as a scalar (``type: string``
    etc.) is collapsed to a single default ``{"confidence": 0.5}`` leaf. When the
    EXTRACTED data for such a field is a list, reconciliation then pads it to N
    null placeholders that ``_missing_row_indices`` reports as unscored — forever.
    A stronger model repeats the identical collapse, so escalating those rows only
    burns a large-model call. This detects that dead-end so the ladder can skip it
    and the report can name the true root cause (extraction produced list-valued
    data for an attribute the class schema does not define as an array).

    Returns None (do not skip) when no schema was threaded in, so behavior is
    unchanged for callers that don't supply one.

    The property is dereferenced before its ``type`` is read, because a property
    declared as ``{"$ref": "#/$defs/Foo"}`` carries no ``type`` of its own. That
    matters twice:

    * A ``$defs`` **group** resolves to ``type: object``, so the reason now names
      ``'object'`` instead of claiming ``'scalar'`` and sending whoever reads it
      hunting for a ``type: string`` that does not exist. Same skip decision.
    * A ``$defs`` **array** (hand-authored configs can put one there; the UI's
      schema editor only emits objects) resolves to ``type: array``, so the field
      is recognized as validly list-typed and is no longer skipped. This is only
      correct because ``_assess_core`` now dereferences before reading ``type``
      too — previously that attribute really was collapsed to a single default
      leaf, so skipping was the right call and only the label was wrong. Do not
      relax this guard without checking the enhancer still sees the same type.
    """
    from idp_common.config.schema_constants import (
        SCHEMA_PROPERTIES,
        SCHEMA_TYPE,
        TYPE_ARRAY,
    )
    from idp_common.config.schema_utils import deref_schema

    if not isinstance(class_schema, dict) or not class_schema:
        return None
    properties = class_schema.get(SCHEMA_PROPERTIES)
    if not isinstance(properties, dict):
        return None
    if field not in properties:
        return (
            f"attribute '{field}' is not defined in the class schema "
            "(extraction produced an off-schema/hallucinated field); its "
            "list rows cannot be confidence-scored"
        )
    prop_type = deref_schema(properties.get(field) or {}, class_schema).get(SCHEMA_TYPE)
    if prop_type != TYPE_ARRAY:
        return (
            f"attribute '{field}' is declared as '{prop_type or 'scalar'}' in "
            "the class schema but extraction returned a list; its rows cannot be "
            "confidence-scored as a list"
        )
    return None


def count_row_columns(rows: Any) -> int | None:
    """Widest scalar column count across a list's rows, or None if not countable.

    Takes the whole row list (not one row) and returns the MAXIMUM count over
    EVERY dict row. Sizing off ``rows[0]`` alone under-counts whenever extraction
    omitted a key on that row, and under-counting the width inflates the batch —
    the direction that truncates. A bare dict is accepted and treated as a single
    row. Returns None when no dict row is found, which tells the caller to use the
    value-length fallback.
    """
    if isinstance(rows, dict):
        candidates: list[Any] = [rows]
    elif isinstance(rows, (list, tuple)):
        candidates = list(rows)
    else:
        return None
    widest = 0
    for row in candidates:
        if not isinstance(row, dict):
            continue
        widest = max(
            widest,
            sum(1 for v in row.values() if not isinstance(v, (dict, list))),
        )
    return widest or None


def compute_token_aware_batch_size(
    model_id: str | None,
    sample_row: Any,
    geometry_mode: str | None,
    configured_batch_size: int,
) -> int:
    """Derive a list-batch size that fits the confidence model's output cap.

    ``sample_row`` should be the list's ROW LIST; a single dict is accepted and
    treated as one row. The column count is measured across a sample of rows
    (:func:`count_row_columns`) rather than off row 0.

    Why this exists: when the confidence model has a small output ceiling (Nova
    Lite: 10,000) and each row emits a confidence leaf per column — tripled again
    by the per-cell bounding-box block under ``geometry.mode: llm``/
    ``llm_grounded`` — a 25-row batch overruns the cap and the response truncates.
    That is the failure that left 34/68 rows unscored, and the failure whose
    recovery ladder ran an Assessment Lambda into its 900-second wall five times
    and lost a document. Sizing the FIRST pass to the model's real budget means it
    fits without the adaptive splitter having to bisect down from 25.

    ``configured_batch_size`` is a user CEILING, not a target: the result is never
    larger. Pass 0 (or any non-positive value) for "no user ceiling", in which case
    only the absolute reliability ceiling applies. The result is never 0.

    An unknown model, or a row shape whose width cannot be measured, now yields a
    CONSERVATIVE derived size rather than the configured value — previously either
    case silently returned the configured 25, which is how a permissive default
    reached a small-cap model.
    """
    from idp_common.bedrock.sizing import (
        confidence_per_row_tokens,
        confidence_rows_for_per_row_tokens,
        confidence_rows_per_call,
        model_list_batch_ceiling,
    )

    ceiling = configured_batch_size if configured_batch_size > 0 else None
    if ceiling is not None and ceiling <= 1:
        return 1

    output_cap: int | None = None
    if model_id:
        from idp_common.bedrock.model_utils import get_model_max_output_tokens

        try:
            output_cap = get_model_max_output_tokens(model_id)
        except Exception as e:  # noqa: BLE001 - unknown model → conservative cap
            logger.warning(
                "compute_token_aware_batch_size: no output cap known for "
                "confidence model %s (%s); sizing conservatively instead of "
                "trusting the configured batch size %s",
                model_id,
                e,
                configured_batch_size,
            )
    else:
        logger.warning(
            "compute_token_aware_batch_size: no confidence model id supplied; "
            "sizing conservatively instead of trusting the configured batch "
            "size %s",
            configured_batch_size,
        )

    num_columns = count_row_columns(sample_row)
    if num_columns is not None:
        result = confidence_rows_per_call(
            output_cap, num_columns, geometry_mode, ceiling, model_id=model_id
        )
        per_row_tokens = confidence_per_row_tokens(num_columns, geometry_mode)
    else:
        # Scalar/opaque rows: per-column counting does not apply, so fall back to
        # the value-length heuristic on the first element.
        from idp_common.extraction.sharding import estimate_tokens

        first = (
            sample_row[0]
            if isinstance(sample_row, (list, tuple)) and sample_row
            else sample_row
        )
        try:
            per_row_tokens = (
                estimate_tokens(json.dumps(first, default=str))
                * _CONFIDENCE_ENVELOPE_MULTIPLIER
            )
        except Exception:  # noqa: BLE001 - unserializable row → assumed width
            per_row_tokens = 0.0
        if per_row_tokens <= 0:
            # Unmeasurable row: fall back to the assumed column width rather than
            # to the configured ceiling.
            per_row_tokens = confidence_per_row_tokens(None, geometry_mode)
        else:
            if (geometry_mode or "").lower() in ("llm", "llm_grounded"):
                per_row_tokens *= _BBOX_GEOMETRY_MULTIPLIER_FALLBACK
            # FLOOR the value-length estimate at the cost of one confidence leaf.
            # ``json.dumps("Robert Smith")`` is ~3 tokens, so the x8 envelope
            # predicts ~24 output tokens for a row that really emits 40-120 — about
            # 5x optimistic, and optimism here means a batch that truncates. Even a
            # scalar row emits at least one leaf, so the per-cell figure is a valid
            # floor. Without this the estimate is also non-monotonic: a 1-character
            # scalar estimates 0 and lands on the conservative assumed width, while
            # a 12-character scalar estimates low enough to reach the ceiling.
            per_row_tokens = max(
                per_row_tokens, confidence_per_row_tokens(1, geometry_mode)
            )
        result = confidence_rows_for_per_row_tokens(output_cap, per_row_tokens, ceiling)
        family = model_list_batch_ceiling(model_id)
        if family is not None:
            result = max(1, min(result, family))

    # Logged unconditionally. Previously suppressed when the result equalled the
    # configured value, which hid the sizing decision from exactly the operator most
    # likely to be debugging it — someone who pinned a ceiling.
    logger.info(
        "Token-aware batch sizing: model=%s cap=%s geometry=%s cols=%s "
        "per_row~%d -> batch %d (configured ceiling %s)",
        model_id or "(none)",
        output_cap if output_cap else "unknown",
        geometry_mode,
        num_columns if num_columns is not None else "n/a",
        int(per_row_tokens),
        result,
        ceiling if ceiling is not None else "none",
    )
    return result


def _new_split_stats() -> dict[str, Any]:
    """Accumulator recording adaptive batch-split activity for visibility.

    Surfaced in extraction/assessment metadata + the processing report so a run
    that had to shrink its assessment batches (because the model truncated at its
    max-output-token ceiling) is observable rather than silent.
    """
    return {
        "truncated_calls": 0,  # assessment calls that hit max_tokens
        "splits": 0,  # times a slice was halved and re-assessed
        "min_batch_size_used": None,  # smallest row-slice actually assessed
        "rows_recovered_by_retry": 0,  # unscored rows rescued in the retry phase
        "unrecoverable_rows": 0,  # rows still unscored after all recovery
        "derived_batch_size": None,  # token-aware first-pass batch size (1.1)
        "configured_batch_size": None,  # the static list_batch_size for contrast
        "escalation_model": None,  # model the ladder escalated to (1.2), if any
        "rows_recovered_by_escalation": 0,  # rows rescued by the bigger model
        "escalation_rounds": 0,  # bounded ladder rounds actually run
        "deadline_reached": False,  # ladder stopped early on the wall-clock guard (1.5)
        "batch_count": None,  # number of list batches (item 4)
        "concurrent_batches": None,  # batches run concurrently after cache warm (item 4)
        # List fields whose recovery was SKIPPED because the data is a list but the
        # class schema does not define them as arrays (off-schema or scalar-typed).
        # A stronger model can't fix a schema mismatch, so escalation is futile —
        # see _schema_field_mismatch_reason / _retry_missing_rows.
        "schema_mismatch_fields": [],
        # #894 terminal condition: list field(s) where the model STILL truncated
        # its response with exactly ONE row in the call. There is no smaller batch
        # than one row, so halving cannot converge and re-running the same call is
        # pure waste — the ladder gives up for that field instead of spending the
        # remaining rungs on it, and reports the real cause.
        # See _record_oversized_row / _assess_slice_adaptive.
        "oversized_row_fields": [],
        "oversized_row_model": None,  # model that truncated on a single row
        "oversized_row_output_cap": None,  # that model's max output tokens
        "oversized_row_chars": None,  # approx serialized size of the offending row
        "oversized_row_class": None,  # document class the row belongs to
    }


def _record_min_batch(stats: dict[str, Any], size: int) -> None:
    cur = stats.get("min_batch_size_used")
    stats["min_batch_size_used"] = size if cur is None else min(cur, size)


def _record_oversized_row(
    stats: dict[str, Any],
    *,
    big_field: str,
    row: Any,
    model_id: str | None,
) -> None:
    """Record the #894 terminal condition: a SINGLE row still truncated the model.

    The adaptive splitter halves a truncating batch, which converges only while a
    smaller batch can fit. When a call carrying exactly ONE row still comes back
    with ``stopReason=max_tokens``, that row's own confidence output exceeds the
    model's max-output-token cap and **no batch size can work** — the row itself is
    too big (the observed case: a multi-instance wrapper makes one "row" a whole
    bank statement carrying a 100-row inner ``Transactions`` list, so the sizer's
    ``cols=2 per_row~80`` estimate is off by two orders of magnitude; see #894).

    Recording it here lets the ladder skip the futile same-model retry rung and lets
    :func:`build_assessment_issues` report the ACTUAL cause with the numbers an
    operator needs: the model, its output cap, the field, and the row's approximate
    serialized size.

    The ``logger.error`` fires **once per field per section** (the first time the
    condition is recorded for that field). Every row of a long list can hit it — one
    probe produced 81 identical error lines — and the message is a diagnosis of the
    field, not of the row, so repeating it only buries everything else in the log.
    """
    fields = stats.setdefault("oversized_row_fields", [])
    first_for_field = big_field not in fields
    if first_for_field:
        fields.append(big_field)

    try:
        row_chars = len(json.dumps(row, default=str))
    except Exception:  # noqa: BLE001 - unserializable row → size unknown
        row_chars = None
    if row_chars is not None:
        prev_chars = stats.get("oversized_row_chars")
        stats["oversized_row_chars"] = (
            row_chars if prev_chars is None else max(int(prev_chars), row_chars)
        )

    output_cap: int | None = None
    if model_id:
        try:
            from idp_common.bedrock.model_utils import get_model_max_output_tokens

            output_cap = get_model_max_output_tokens(model_id)
        except Exception:  # noqa: BLE001 - unknown model → cap unknown
            output_cap = None
        # Keep the model with the LARGEST known cap that still truncated: if the
        # escalation model (bigger cap) also failed on one row, naming it is far
        # more actionable than naming the small primary.
        prev_cap = stats.get("oversized_row_output_cap") or 0
        if not stats.get("oversized_row_model") or (output_cap or 0) > prev_cap:
            stats["oversized_row_model"] = model_id
            stats["oversized_row_output_cap"] = output_cap

    if not first_for_field:
        # Already diagnosed for this field — see the docstring. Keep a cheap trace
        # for anyone counting how many rows hit it.
        logger.debug(
            "Assessment: another single-row truncation on '%s' (~%s chars).",
            big_field,
            row_chars if row_chars is not None else "unknown",
        )
        return

    logger.error(
        "Assessment giving up on '%s': the model (%s, max output tokens %s) "
        "truncated its response with a SINGLE row in the call (~%s chars "
        "serialized). No smaller batch exists, so shrinking/retrying cannot help. "
        "Fix: use a confidence model with a larger output budget, or reduce the "
        "size of each list item (e.g. avoid wrapping a class that already contains "
        "a long inner list in a multi-instance list) — not a smaller batch size. "
        "(Logged once per field; further single-row truncations on '%s' are at "
        "DEBUG.)",
        big_field,
        model_id or "(unknown)",
        output_cap if output_cap else "unknown",
        row_chars if row_chars is not None else "unknown",
        big_field,
    )


def _oversized_row_summary(stats: dict[str, Any]) -> str:
    """One-line, actionable summary of the #894 terminal condition for reports."""
    fields = ", ".join(f"'{f}'" for f in (stats.get("oversized_row_fields") or []))
    cap = stats.get("oversized_row_output_cap")
    chars = stats.get("oversized_row_chars")
    cls = stats.get("oversized_row_class")
    return (
        f"⚠ Row too large to score (retries stopped): {fields}"
        + (f" of class '{cls}'" if cls else "")
        + f" — confidence model {stats.get('oversized_row_model') or '(unknown)'} "
        f"(max output tokens {cap if cap else 'unknown'}) truncated with a single "
        f"row in the call"
        + (f" (~{chars} chars serialized)" if chars else "")
        + ". No batch size can fit one row, so shrinking was abandoned; use a "
        "confidence model with a larger output budget or make each list item "
        "smaller."
    )


def split_stats_are_notable(stats: dict[str, Any] | None) -> bool:
    """True when the run actually had to shrink batches (worth surfacing).

    A clean run (no truncation, no splits, no leftover unscored rows) is not
    notable — callers omit the metadata block entirely in that case to avoid
    noise.
    """
    if not stats:
        return False
    return bool(
        stats.get("truncated_calls")
        or stats.get("splits")
        or stats.get("unrecoverable_rows")
        or stats.get("escalation_rounds")
        or stats.get("rows_recovered_by_escalation")
        or stats.get("schema_mismatch_fields")
        or stats.get("oversized_row_fields")
    )


def merge_split_stats(
    a: dict[str, Any] | None, b: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Additively merge two split-stats accumulators (for the sharded path, where
    each shard produces its own). Counters sum; ``min_batch_size_used`` takes the
    smaller non-null. Returns None only when both inputs are None."""
    if not a and not b:
        return None
    a = a or _new_split_stats()
    b = b or _new_split_stats()
    merged = _new_split_stats()
    for key in (
        "truncated_calls",
        "splits",
        "rows_recovered_by_retry",
        "unrecoverable_rows",
        "rows_recovered_by_escalation",
        "escalation_rounds",
    ):
        merged[key] = a.get(key, 0) + b.get(key, 0)
    mins = [
        v
        for v in (a.get("min_batch_size_used"), b.get("min_batch_size_used"))
        if v is not None
    ]
    merged["min_batch_size_used"] = min(mins) if mins else None
    # Sizing fields: keep the smaller derived size (most-constrained shard) and
    # the (shared) configured size; escalation_model is whichever shard set one.
    derived = [
        v
        for v in (a.get("derived_batch_size"), b.get("derived_batch_size"))
        if v is not None
    ]
    merged["derived_batch_size"] = min(derived) if derived else None
    # ``is not None``, not ``or``: a legitimate configured value can be falsy, and
    # ``or`` would silently discard it in favour of the other shard's.
    _cfg_a = a.get("configured_batch_size")
    merged["configured_batch_size"] = (
        _cfg_a if _cfg_a is not None else b.get("configured_batch_size")
    )
    merged["escalation_model"] = a.get("escalation_model") or b.get("escalation_model")
    merged["deadline_reached"] = bool(
        a.get("deadline_reached") or b.get("deadline_reached")
    )
    # Union the schema-mismatch fields across shards (order-preserving, deduped) so
    # a mismatch surfaced by any shard is reported once for the merged section.
    merged_mismatch: list[str] = []
    for src in (a.get("schema_mismatch_fields"), b.get("schema_mismatch_fields")):
        for fld in src or []:
            if fld not in merged_mismatch:
                merged_mismatch.append(fld)
    merged["schema_mismatch_fields"] = merged_mismatch
    # #894: union the oversized-row fields; keep the LARGEST known output cap that
    # still truncated (the strongest evidence: "even a model with cap N could not
    # score one row") and the biggest offending row seen.
    merged_oversized: list[str] = []
    for src in (a.get("oversized_row_fields"), b.get("oversized_row_fields")):
        for fld in src or []:
            if fld not in merged_oversized:
                merged_oversized.append(fld)
    merged["oversized_row_fields"] = merged_oversized
    caps = [
        (src.get("oversized_row_output_cap") or 0, src)
        for src in (a, b)
        if src.get("oversized_row_model")
    ]
    if caps:
        _, worst = max(caps, key=lambda pair: pair[0])
        merged["oversized_row_model"] = worst.get("oversized_row_model")
        merged["oversized_row_output_cap"] = worst.get("oversized_row_output_cap")
    chars = [
        v
        for v in (a.get("oversized_row_chars"), b.get("oversized_row_chars"))
        if v is not None
    ]
    merged["oversized_row_chars"] = max(chars) if chars else None
    merged["oversized_row_class"] = a.get("oversized_row_class") or b.get(
        "oversized_row_class"
    )
    # Batch counts sum across shards; concurrency takes the max any shard used.
    bc = [v for v in (a.get("batch_count"), b.get("batch_count")) if v is not None]
    merged["batch_count"] = sum(bc) if bc else None
    cc = [v for v in (a.get("concurrent_batches"), b.get("concurrent_batches")) if v]
    merged["concurrent_batches"] = max(cc) if cc else None
    return merged


def format_split_stats_report(stats: dict[str, Any] | None) -> str:
    """Human-readable processing-report block for adaptive batch splitting.

    Returns an empty string when nothing notable happened.
    """
    if not split_stats_are_notable(stats):
        return ""
    assert stats is not None
    lines = [
        "Assessment Batch Splitting (model truncated output):",
        f"  - Truncated assessment calls: {stats.get('truncated_calls', 0)}",
        f"  - Batch splits performed: {stats.get('splits', 0)}",
        f"  - Smallest batch assessed: {stats.get('min_batch_size_used')}",
        f"  - Rows recovered on retry: {stats.get('rows_recovered_by_retry', 0)}",
        f"  - Rows still unscored: {stats.get('unrecoverable_rows', 0)}",
    ]
    if stats.get("derived_batch_size") is not None:
        lines.append(
            f"  - Token-aware first-pass batch size: "
            f"{stats.get('derived_batch_size')} "
            f"(configured {stats.get('configured_batch_size')})"
        )
    if stats.get("escalation_model"):
        lines.append(
            f"  - Escalated confidence model: {stats.get('escalation_model')} "
            f"(rounds: {stats.get('escalation_rounds', 0)}, rows recovered: "
            f"{stats.get('rows_recovered_by_escalation', 0)})"
        )
    if stats.get("schema_mismatch_fields"):
        fields_str = ", ".join(stats["schema_mismatch_fields"])
        lines.append(
            f"  - ⚠ Schema mismatch (retry/escalation skipped): {fields_str} — "
            "extraction returned a list for attribute(s) the class schema does not "
            "define as arrays; fix the schema/extraction prompt."
        )
    if stats.get("oversized_row_fields"):
        lines.append("  - " + _oversized_row_summary(stats))
    if stats.get("deadline_reached"):
        lines.append(
            "  - ⏱ Self-healing stopped early: Lambda wall-clock budget reached "
            "(remaining rows left unscored to avoid a timeout)."
        )
    if stats.get("batch_count"):
        conc = stats.get("concurrent_batches") or 1
        mode = f"{conc}-way concurrent (cache-warmed)" if conc > 1 else "sequential"
        lines.append(
            f"  - Confidence list batches: {stats.get('batch_count')} ({mode})"
        )
    return "\n".join(lines)


def build_assessment_issues(
    stats: dict[str, Any] | None,
    *,
    section_id: str | None = None,
    confidence_model: str | None = None,
    geometry_mode: str | None = None,
) -> list[Any]:
    """Translate assessment ``split_stats`` into structured ``ProcessingIssue``s.

    ONE place that maps the batch-splitting/escalation accumulator into the
    user-surfacing issue contract, so both the standalone Assessment step and the
    agentic in-shard path emit identical issues. Returns ``[]`` when nothing
    notable happened (a clean run produces no issues). Severity ladder (only the
    FIRST matching rung is emitted):

    - ``schema_mismatch_fields`` set → ``assessment_schema_mismatch`` (**error**):
      extraction returned list-valued data for an attribute the class schema does
      not define as an array; the enhancer collapsed it so no model can score its
      rows. Takes precedence — the true fix is upstream (schema/extraction), not a
      stronger confidence model.
    - else ``oversized_row_fields`` set AND rows unscored → ``assessment_row_too_large``
      (**error**, #894): the model truncated with a single row in the call, so the
      row's own confidence output exceeds its output cap and NO batch size can fit
      it. Reported ahead of the generic incomplete rung because the generic message
      ("rows could not be scored") sends the operator to shrink the batch size,
      which is the one remedy that provably cannot work here.
    - else ``unrecoverable_rows > 0`` → ``assessment_incomplete`` (**error**): rows
      are still unscored after the full self-healing ladder.
    - else ``deadline_reached`` → ``assessment_deadline_reached`` (**warning**):
      the wall-clock guard stopped escalation before coverage completed.
    - else rows were ACTUALLY recovered (retry or escalation counters > 0) →
      ``assessment_recovered_with_retries`` (**info**): self-healed, but
      token-inefficiently (worth flagging).
    - else a call truncated but NOTHING was recovered and nothing is missing
      (e.g. the model truncated on an empty/near-empty list because extraction
      produced few/no rows) → ``assessment_truncated`` (**warning**): report the
      truncation honestly WITHOUT claiming a recovery that never happened.

    IMPORTANT: this deliberately does NOT claim "recovered" merely because a call
    truncated. A truncation with 0 rows recovered and 0 unrecoverable is not a
    success — it usually means extraction under-produced rows (see the
    extraction-side ``extraction_incomplete`` issue) and the confidence pass hit
    its ceiling on whatever little was there.

    **Schema-mismatch takes precedence over all rungs.** When
    ``schema_mismatch_fields`` is set, the unscored rows are NOT a model/truncation
    problem — extraction produced list-valued data for an attribute the class
    schema does not define as an array, so the confidence enhancer collapsed it and
    no model can score it per-row. That is emitted as ``assessment_schema_mismatch``
    (**error**) naming the offending field(s) and the true fix (correct the
    extraction schema / prompt), instead of a misleading "escalation failed" story.
    """
    from idp_common.models import ProcessingIssue

    if not split_stats_are_notable(stats):
        return []
    assert stats is not None

    unrecoverable = int(stats.get("unrecoverable_rows", 0) or 0)
    escalation_model = stats.get("escalation_model")
    derived = stats.get("derived_batch_size")
    configured = stats.get("configured_batch_size")
    recovered_by_retry = int(stats.get("rows_recovered_by_retry", 0) or 0)
    recovered_by_escalation = int(stats.get("rows_recovered_by_escalation", 0) or 0)
    total_recovered = recovered_by_retry + recovered_by_escalation
    schema_mismatch_fields = list(stats.get("schema_mismatch_fields") or [])

    # Compose a technical root-cause string shared by all severities.
    parts: list[str] = []
    if confidence_model:
        parts.append(f"confidence model {confidence_model}")
    if geometry_mode:
        parts.append(f"geometry.mode {geometry_mode}")
    if derived is not None and configured is not None:
        parts.append(f"token-aware batch {derived} (configured {configured})")
    if stats.get("truncated_calls"):
        parts.append(f"{stats['truncated_calls']} truncated call(s)")
    if escalation_model:
        parts.append(
            f"escalated to {escalation_model} "
            f"(recovered {recovered_by_escalation} row(s))"
        )
    root_cause = "; ".join(parts)

    # Highest precedence: a schema mismatch is the TRUE root cause when it fired.
    # The unscored rows are not a model/truncation problem — extraction produced a
    # list for an attribute the class schema does not define as an array, so the
    # confidence enhancer collapsed it to one default leaf and reconciliation padded
    # the data rows with null placeholders no model can fill. Name the field(s) and
    # point at the real fix (correct the extraction schema/prompt) so the report
    # doesn't tell a misleading "escalation failed" story.
    if schema_mismatch_fields:
        fields_str = ", ".join(f"'{f}'" for f in schema_mismatch_fields)
        return [
            ProcessingIssue(
                stage="assessment",
                severity="error",
                code="assessment_schema_mismatch",
                message=(
                    f"Extraction returned list-valued data for {fields_str}, which "
                    "the class schema does not define as a list attribute; those "
                    "rows cannot be confidence-scored. This is an extraction/schema "
                    "mismatch — fix the class schema or extraction prompt (a "
                    "stronger confidence model cannot resolve it, so escalation was "
                    "skipped). Consider enabling Advanced (agentic) extraction, "
                    "which validates output against the schema and drops off-schema "
                    "fields before assessment."
                ),
                root_cause=(
                    (root_cause + "; " if root_cause else "")
                    + f"off-schema/non-array attribute(s) {fields_str} extracted as "
                    "list(s); confidence enhancer collapsed to a scalar leaf"
                ),
                section_id=section_id,
                details=dict(stats),
            )
        ]

    # #894: next precedence — a row whose OWN confidence output exceeds the model's
    # cap. The ladder stopped instead of retrying, and the generic
    # "rows could not be scored" message would send the operator to shrink
    # ``list_batch_size``, which cannot help (the batch was already one row).
    oversized_fields = list(stats.get("oversized_row_fields") or [])
    if oversized_fields and unrecoverable > 0:
        fields_str = ", ".join(f"'{f}'" for f in oversized_fields)
        cap = stats.get("oversized_row_output_cap")
        chars = stats.get("oversized_row_chars")
        cls = stats.get("oversized_row_class")
        oversized_model = stats.get("oversized_row_model") or confidence_model
        return [
            ProcessingIssue(
                stage="assessment",
                severity="error",
                code="assessment_row_too_large",
                message=(
                    f"{unrecoverable} row(s) of {fields_str}"
                    + (f" in class '{cls}'" if cls else "")
                    + " have no confidence score: confidence model "
                    f"{oversized_model or '(unknown)'} (max output tokens "
                    f"{cap if cap else 'unknown'}) truncated its response with a "
                    "SINGLE row in the call"
                    + (f" (~{chars} chars serialized)" if chars else "")
                    + ", so no batch size can fit one row and batch shrinking was "
                    "abandoned rather than retried. Fix: choose a confidence model "
                    "with a larger output budget, or make each list item smaller "
                    "(e.g. do not wrap a class that already contains a long inner "
                    "list in a multi-instance list) — reducing "
                    "extraction.confidence.list_batch_size cannot help."
                ),
                root_cause=(
                    (root_cause + "; " if root_cause else "")
                    + f"single row of {fields_str} exceeds the confidence model's "
                    f"output budget (cap {cap if cap else 'unknown'} tokens"
                    + (f", row ~{chars} chars" if chars else "")
                    + ")"
                ),
                section_id=section_id,
                details=dict(stats),
            )
        ]

    if unrecoverable > 0:
        chain = f" after escalation to {escalation_model}" if escalation_model else ""
        return [
            ProcessingIssue(
                stage="assessment",
                severity="error",
                code="assessment_incomplete",
                message=(
                    f"{unrecoverable} list row(s) could not be confidence-scored"
                    f"{chain}; those rows have no confidence."
                ),
                root_cause=root_cause
                or f"{unrecoverable} rows unrecoverable after self-healing",
                section_id=section_id,
                details=dict(stats),
            )
        ]

    if stats.get("deadline_reached"):
        return [
            ProcessingIssue(
                stage="assessment",
                severity="warning",
                code="assessment_deadline_reached",
                message=(
                    "Confidence self-healing stopped early to stay within the "
                    "Lambda time budget; coverage completed but escalation was "
                    "cut short."
                ),
                root_cause=root_cause or "wall-clock budget reached",
                section_id=section_id,
                details=dict(stats),
            )
        ]

    if total_recovered > 0:
        # Genuinely self-healed — rows that were unscored are now scored.
        return [
            ProcessingIssue(
                stage="assessment",
                severity="info",
                code="assessment_recovered_with_retries",
                message=(
                    f"{total_recovered} row(s) were recovered after batch shrinking"
                    + (" and model escalation" if recovered_by_escalation else "")
                    + " (token-inefficient — consider geometry.mode ocr_only or a "
                    "larger-output confidence model)."
                ),
                root_cause=root_cause,
                section_id=section_id,
                details=dict(stats),
            )
        ]

    # A call truncated but NOTHING was recovered and nothing is left unscored.
    # Do NOT claim recovery. This is a degraded (warning) outcome — the confidence
    # model hit its output ceiling; on an empty/near-empty list this is usually a
    # downstream symptom of extraction under-producing rows.
    return [
        ProcessingIssue(
            stage="assessment",
            severity="warning",
            code="assessment_truncated",
            message=(
                "The confidence model truncated its output; no rows were scored "
                "by the truncated call. If extraction returned few/no rows this is "
                "usually an extraction shortfall — consider Advanced (agentic) "
                "extraction or a larger-output confidence model."
            ),
            root_cause=root_cause or "confidence model output truncated",
            section_id=section_id,
            details=dict(stats),
        )
    ]


def _ladder_reported_error(ladder_issues: list[Any] | None) -> bool:
    """True when ``build_assessment_issues`` already emitted an **error** issue.

    Used to suppress the coverage rung: the ladder's error rungs
    (``assessment_schema_mismatch``, ``assessment_row_too_large``,
    ``assessment_incomplete``) describe the SAME unscored rows *with a cause
    attached*, so emitting the coverage issue as well double-counts
    ``ProcessingIssueCount`` and can state a second, different row count. It also
    breaks the schema-mismatch rung's deliberate "emitted alone" contract, and would
    append "the extracted values themselves are unaffected" directly beneath a
    diagnosis that says extraction produced off-schema data.

    Accepts ``ProcessingIssue`` objects, plain dicts, or bare code/severity-less
    values, so either composition site can pass whatever it has.
    """
    for issue in ladder_issues or []:
        severity = (
            issue.get("severity")
            if isinstance(issue, dict)
            else getattr(issue, "severity", None)
        )
        if str(severity or "").lower() == "error":
            return True
    return False


def audit_explainability(
    assessment: dict[str, Any] | None,
    extraction_results: dict[str, Any] | None,
    *,
    geometry_mode: str | None = None,
    section_id: str | None = None,
    ladder_issues: list[Any] | None = None,
) -> tuple[dict[str, list[int]], list[Any]]:
    """Verify every extracted value has correctly-structured explainability.

    The completeness gate (plan 1.3): after the ladder runs, confirm the emitted
    assessment actually covers the data. Returns a list of the row indices per
    field that are still un-scored (as a dict) PLUS any structural ``ProcessingIssue``s.
    Checks, per config:

    - Every list row / scalar leaf has a real (non-null) ``confidence``.
    - When ``geometry_mode not in (None, "off")``, a confidence leaf carries a
      geometry block (``bbox``/``geometry``) — flagged as a *warning*, not an
      error, since geometry is advisory enrichment.
    - Confidence values are within [0, 1].
    - **Coverage (#901 item 3):** the share of extracted list rows that actually
      carry a confidence. A materially short section emits
      ``assessment_coverage_incomplete`` — ``warning`` past
      ``_COVERAGE_SHORTFALL_WARNING_FRACTION``, ``error`` only past
      ``_COVERAGE_SHORTFALL_ERROR_FRACTION`` **and**
      ``_COVERAGE_SHORTFALL_ERROR_MIN_UNSCORED_ROWS`` absolute unscored rows (a
      fraction alone makes a one-row gap in a four-row list an error) — so partial
      coverage is visible instead of the section reporting unqualified success. This
      is a *symptom* report computed from the final data; it deliberately makes no
      claim about the cause.

      Pass ``ladder_issues`` (the return of :func:`build_assessment_issues` for the
      same section) to suppress this rung when the ladder already reported an
      error-severity issue: that issue covers the same rows *with* a cause, so both
      would double-count ``ProcessingIssueCount``. Callers that compose
      ``build_assessment_issues(...) + audit_issues`` should always pass it.

    Returns ``(gaps, issues)`` where ``gaps`` maps ``field -> [missing row idx]``
    (fed back into the ladder once by the caller) and ``issues`` is a list of
    ``ProcessingIssue`` for anything structurally wrong that is NOT just a missing
    confidence (those are represented by the ladder's own split_stats issue) plus
    the coverage-shortfall issue described above.
    """
    from idp_common.models import ProcessingIssue

    gaps: dict[str, list[int]] = {}
    issues: list[ProcessingIssue] = []
    if not isinstance(assessment, dict) or not isinstance(extraction_results, dict):
        return gaps, issues

    want_geometry = bool(geometry_mode) and geometry_mode != "off"
    out_of_range = 0
    missing_geometry_rows = 0

    def _leaf_confidences(node: Any) -> list[dict[str, Any]]:
        """All ``{confidence: ...}`` leaves under a row/scalar assessment node."""
        if isinstance(node, dict):
            if "confidence" in node:
                return [node]
            leaves: list[dict[str, Any]] = []
            for v in node.values():
                leaves.extend(_leaf_confidences(v))
            return leaves
        return []

    def _has_geometry(leaf: dict[str, Any]) -> bool:
        geo = leaf.get("geometry") or leaf.get("bbox") or leaf.get("bounding_box")
        return bool(geo)

    for field, data_val in extraction_results.items():
        assessed = assessment.get(field)
        if isinstance(data_val, list):
            if not isinstance(assessed, list):
                gaps[field] = list(range(len(data_val)))
                continue
            # Audit EVERY data row. A trailing row the model omitted (assessed
            # shorter than data) is a genuine gap the completeness gate must catch —
            # iterating only min(len) would silently under-report exactly those
            # missing rows.
            for i in range(len(data_val)):
                if i >= len(assessed) or _row_confidence_missing(assessed[i]):
                    gaps.setdefault(field, []).append(i)
                    continue
                for leaf in _leaf_confidences(assessed[i]):
                    conf = leaf.get("confidence")
                    if isinstance(conf, (int, float)) and not (0.0 <= conf <= 1.0):
                        out_of_range += 1
                    if want_geometry and conf is not None and not _has_geometry(leaf):
                        missing_geometry_rows += 1
        else:
            # Scalar/group: verify a confidence leaf exists and is in range.
            for leaf in _leaf_confidences(assessed):
                conf = leaf.get("confidence")
                if isinstance(conf, (int, float)) and not (0.0 <= conf <= 1.0):
                    out_of_range += 1

    if out_of_range:
        issues.append(
            ProcessingIssue(
                stage="assessment",
                severity="warning",
                code="assessment_confidence_out_of_range",
                message=f"{out_of_range} confidence value(s) fell outside [0,1].",
                root_cause="model emitted out-of-range confidence",
                section_id=section_id,
                details={"out_of_range_leaves": out_of_range},
            )
        )
    if missing_geometry_rows:
        issues.append(
            ProcessingIssue(
                stage="assessment",
                severity="info",
                code="assessment_geometry_incomplete",
                message=(
                    f"{missing_geometry_rows} scored leaf/leaves have no bounding "
                    f"box under geometry.mode '{geometry_mode}'."
                ),
                root_cause=(
                    "value could not be matched to OCR lines"
                    if geometry_mode == "ocr_only"
                    else "model omitted a box for some cells"
                ),
                section_id=section_id,
                details={"missing_geometry_leaves": missing_geometry_rows},
            )
        )

    # Coverage shortfall (#901 item 3): make PARTIAL confidence coverage visible.
    # The per-field gaps above are computed anyway but were previously discarded by
    # every caller, so a section could come back with a fraction of its rows scored
    # and still report unqualified success. Thresholds and their rationale live on
    # _COVERAGE_SHORTFALL_* above.
    #
    # Suppressed entirely when the ladder already emitted an error for this section (see
    # ``ladder_issues``): that issue is the same shortfall with a cause attached, and
    # emitting both double-counts ProcessingIssueCount with two counts that can
    # legitimately disagree (``unrecoverable_rows`` tracks only the largest list
    # field; this audit counts every list field).
    total_list_rows = sum(
        len(v) for v in extraction_results.values() if isinstance(v, list)
    )
    unscored_list_rows = sum(len(idxs) for idxs in gaps.values())
    if total_list_rows and not _ladder_reported_error(ladder_issues):
        shortfall = unscored_list_rows / total_list_rows
        if shortfall >= _COVERAGE_SHORTFALL_WARNING_FRACTION:
            scored = total_list_rows - unscored_list_rows
            # Error severity needs BOTH a large proportion and a large absolute
            # number of unscored rows — see _COVERAGE_SHORTFALL_ERROR_MIN_UNSCORED_ROWS.
            severity = (
                "error"
                if (
                    shortfall >= _COVERAGE_SHORTFALL_ERROR_FRACTION
                    and unscored_list_rows
                    >= _COVERAGE_SHORTFALL_ERROR_MIN_UNSCORED_ROWS
                )
                else "warning"
            )
            worst = sorted(gaps.items(), key=lambda kv: -len(kv[1]))[:3]
            worst_str = ", ".join(f"'{f}' ({len(idxs)} row(s))" for f, idxs in worst)
            # No row scored at all is a different statement from partial coverage:
            # "covers only part of this section" is false at zero, and claiming the
            # extracted data is fine is a claim this audit cannot make when nothing
            # was scored (the cause may be upstream of confidence entirely).
            if scored == 0:
                message = (
                    f"None of the {total_list_rows} extracted list row(s) carry a "
                    f"confidence score ({worst_str}), so this section has no "
                    "confidence surface at all and confidence-based review (HITL "
                    "thresholds) does not apply to any of it. The extracted values "
                    "themselves were kept; treat every row as unverified."
                )
            else:
                message = (
                    f"Only {scored} of {total_list_rows} extracted list row(s) "
                    f"({(1 - shortfall):.0%}) carry a confidence score; "
                    f"{unscored_list_rows} row(s) are unscored ({worst_str}). "
                    "The extracted values themselves are unaffected, but "
                    "confidence-based review (HITL thresholds) covers only part "
                    "of this section — treat unscored rows as unverified."
                )
            issues.append(
                ProcessingIssue(
                    stage="assessment",
                    severity=severity,
                    code="assessment_coverage_incomplete",
                    message=message,
                    root_cause=(
                        f"{unscored_list_rows}/{total_list_rows} list rows have no "
                        "confidence after the self-healing ladder finished"
                    ),
                    section_id=section_id,
                    details={
                        "expected_rows": total_list_rows,
                        "scored_rows": scored,
                        "unscored_rows": unscored_list_rows,
                        "unscored_fraction": round(shortfall, 4),
                        "unscored_rows_by_field": {
                            f: len(idxs) for f, idxs in gaps.items()
                        },
                    },
                )
            )
    return gaps, issues


def _assess_slice_adaptive(
    one_call,
    *,
    base_results: dict[str, Any],
    big_field: str,
    rows: list[Any],
    reconcile,
    stats: dict[str, Any],
    min_slice: int = 1,
    deadline_epoch: float | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Assess ``rows`` for ``big_field`` and, if the model TRUNCATES the response
    (``AssessmentCoreResult.truncated``), recursively halve the slice and retry
    until it parses or the slice reaches ``min_slice`` rows.

    A truncated call yields unparseable JSON → default/placeholder scores for the
    whole slice, so retrying the *same* size is futile; the output is too big for
    the model's cap (common with ``geometry.mode: llm``, which adds a bounding box
    per cell). Halving shrinks the output until it fits. Records activity in
    ``stats`` for downstream visibility. Sequential (no fan-out), consistent with
    the batcher's cost model.

    **Wall-clock guard (1.5):** ``deadline_epoch`` (absolute epoch seconds) bounds
    the recursion — each halving step doubles the number of sequential model calls,
    so a small-cap model that keeps truncating (e.g. Nova Lite over an
    ``llm_grounded`` batch too large for its output cap) can otherwise fan a single
    batch into dozens of ~60s calls and run a shard/merge Lambda into its 900s
    wall. When the next call wouldn't fit before the deadline (minus safety
    reserve), the recursion stops, flags ``deadline_reached``, and keeps the
    truncated slice's best-effort scores rather than risk a hard task timeout.
    No-op when ``deadline_epoch`` is None (local / no context).

    **Terminal condition (#894):** when the call carries exactly ONE row and the
    response STILL truncates, halving has nowhere left to go — that single row's
    confidence output does not fit the model's cap, so no batch size can work. The
    condition is recorded via :func:`_record_oversized_row` (which also names
    ``model_id`` and its output cap in the log) and the caller's retry rung is
    skipped, instead of spending further rungs re-running the same impossible call
    and then reporting a generic "rows could not be scored".

    Returns ``{"rows": [per-row assessment...], "scalars": {enhanced non-big
    fields}, "alerts": [...], "metering": {...}, "duration": float}`` where
    ``rows`` is index-aligned to the input ``rows``.
    """
    slice_results = dict(base_results)
    slice_results[big_field] = rows
    core = one_call(slice_results)
    _record_min_batch(stats, len(rows))

    truncated = bool(getattr(core, "truncated", False))
    should_split = truncated and len(rows) > min_slice and len(rows) > 1
    if truncated:
        stats["truncated_calls"] += 1

    # #894 terminal condition: one row in the call and it STILL truncated. Halving
    # is exhausted and re-running is futile — record the real cause (row too large
    # for this model's output cap) so the caller skips the retry rung and the
    # operator gets an actionable message instead of "shrink the batch size".
    if truncated and len(rows) == 1:
        _record_oversized_row(
            stats, big_field=big_field, row=rows[0], model_id=model_id
        )

    # Wall-clock guard: a split doubles the number of sequential calls, so stop
    # recursing when the two halves (each ~ one model call) wouldn't fit before the
    # deadline. Estimate from the call we just made (its real duration), falling
    # back to a conservative 60s when unknown.
    if should_split and deadline_epoch is not None:
        est_call_seconds = max(
            float(getattr(core, "duration_seconds", 0.0) or 0.0), 60.0
        )
        if not _deadline_allows(deadline_epoch, est_call_seconds):
            stats["deadline_reached"] = True
            logger.warning(
                "Assessment adaptive-split for '%s' stopping early (%d rows): "
                "wall-clock deadline reached; keeping best-effort scores.",
                big_field,
                len(rows),
            )
            should_split = False

    if not should_split:
        enhanced = reconcile(core.enhanced_assessment, slice_results)
        enhanced_rows = (
            enhanced.get(big_field) if isinstance(enhanced.get(big_field), list) else []
        )
        # Pad/truncate to exactly len(rows) so indices stay aligned.
        enhanced_rows = list(enhanced_rows)[: len(rows)]
        scalars = {k: v for k, v in enhanced.items() if k != big_field}
        return {
            "rows": enhanced_rows,
            "scalars": scalars,
            "alerts": list(core.confidence_threshold_alerts or []),
            "metering": core.metering or {},
            "duration": core.duration_seconds or 0.0,
        }

    # Truncated with room to shrink: split in half and recurse.
    stats["splits"] += 1
    mid = len(rows) // 2
    logger.warning(
        "Assessment truncated for '%s' over %d rows; splitting into %d + %d "
        "and retrying with smaller batches.",
        big_field,
        len(rows),
        mid,
        len(rows) - mid,
    )
    left = _assess_slice_adaptive(
        one_call,
        base_results=base_results,
        big_field=big_field,
        rows=rows[:mid],
        reconcile=reconcile,
        stats=stats,
        min_slice=min_slice,
        deadline_epoch=deadline_epoch,
        model_id=model_id,
    )
    right = _assess_slice_adaptive(
        one_call,
        base_results=base_results,
        big_field=big_field,
        rows=rows[mid:],
        reconcile=reconcile,
        stats=stats,
        min_slice=min_slice,
        deadline_epoch=deadline_epoch,
        model_id=model_id,
    )
    # The truncated parent call still consumed real output tokens (and wall
    # time) before we decided to split — fold its metering/duration in so cost
    # dashboards reflect the TRUE spend, including the wasted truncated attempt.
    # (Discarding it would under-report cost by exactly the wasted work this
    # feature exists to make visible.)
    merged_metering = utils.merge_metering_data(left["metering"], right["metering"])
    merged_metering = utils.merge_metering_data(merged_metering, core.metering or {})
    # Scalars come from the first (left) sub-slice; both carry the same context.
    return {
        "rows": left["rows"] + right["rows"],
        "scalars": left["scalars"] or right["scalars"],
        "alerts": dedupe_alerts(left["alerts"] + right["alerts"]),
        "metering": merged_metering,
        "duration": left["duration"]
        + right["duration"]
        + (core.duration_seconds or 0.0),
    }


def assess_results_batched(
    assessment_service: Any,
    *,
    class_label: str,
    extraction_results: dict[str, Any],
    document_text: str,
    page_images: list[Any],
    batch_size: int,
    ocr_text_confidence: str = "",
    max_retries: int = 2,
    confidence_model_id: str | None = None,
    geometry_mode: str | None = None,
    escalation_enabled: bool = False,
    escalation_model: str | None = None,
    max_escalation_rounds: int = 2,
    deadline_epoch: float | None = None,
    max_concurrent_batches: int = 1,
    class_schema: dict[str, Any] | None = None,
    default_confidence_threshold: float | None = None,
) -> dict[str, Any]:
    """Assess one scope, batching large list fields across multiple inferences.

    A single assessment call over a large list (e.g. 120 transaction rows) is
    unreliable — the model under-enumerates or omits the list, leaving rows
    unassessed. When the largest list field exceeds ``batch_size``, the list is
    sliced into batches; each batch is assessed with the SAME scalars/context (so
    scalar assessments and the document context are preserved) but only that
    batch's rows, and the per-row assessments are concatenated in order.
    Scalar/group assessments come from the first batch.

    Concurrency (item 4): batches are INDEPENDENT and may run concurrently when
    ``max_concurrent_batches > 1``. To avoid the cacheWrite storm that made the
    original design sequential, the FIRST batch runs alone to warm the prompt
    cache (static instructions + per-doc image/OCR block), THEN the remaining
    batches fan out across a bounded thread pool and hit the warm cache. Row
    order is preserved (ordered chunks + ``pool.map``). ``max_concurrent_batches
    <= 1`` keeps the original fully-sequential behavior.

    ``assessment_service`` must expose the pure inference core
    ``assess_results(class_label, extraction_results, document_text, page_images,
    ocr_text_confidence) -> AssessmentCoreResult``.

    If a batch's response is TRUNCATED at the model's max-output-token ceiling
    (e.g. ``geometry.mode: llm`` makes per-row output too large for a small-cap
    model like Nova Lite), that slice is recursively halved and re-assessed until
    it fits — instead of accepting default 0.5 / null-placeholder scores. This
    activity is recorded and returned under ``split_stats`` for visibility.

    **Self-healing (this module's robustness spine):**
    - **Token-aware first-pass sizing (1.1):** when ``confidence_model_id`` is
      given, the effective batch size is shrunk (never grown) to fit the model's
      output cap via :func:`compute_token_aware_batch_size`, so the FIRST pass
      already fits instead of relying on the adaptive splitter to bisect from 25.
    - **Model-escalation ladder (1.2):** when ``escalation_enabled`` and an
      ``escalation_model`` is available, rows still unscored after token-aware
      shrink + same-model retries are re-assessed on the stronger model (bigger
      output cap) — the step that actually fixes small-cap truncation. Bounded by
      ``max_escalation_rounds``. **Guarded against schema mismatch:** when
      ``class_schema`` is supplied and a "missing"-row list field is not declared
      ``type: array`` in it (an off-schema/hallucinated attribute or a
      scalar-typed one that extraction returned as a list), the enhancer collapses
      it to a single default leaf that no model can turn into per-row scores — so
      both retry and escalation are SKIPPED for that field (recorded in
      ``schema_mismatch_fields``) instead of wasting a large-model call.
    - **Oversized-row terminal condition (#894):** if the model still truncates
      with a SINGLE row in the call, no batch size can fit that row — halving is
      abandoned, the same-model retry rung is skipped, escalation is capped at one
      round, and the cause is recorded in ``oversized_row_fields`` (surfaced as
      ``assessment_row_too_large``). Previously the ladder spent every remaining rung
      re-running the impossible call and then reported a generic
      ``assessment_incomplete``, whose remedy (a smaller batch) cannot work.

    Returns ``{"assessment", "alerts", "metering", "parsing_succeeded",
    "duration_seconds", "split_stats"}``. Falls back to a single call (still
    reconciled) when no list field exceeds the batch size.
    """
    split_stats = _new_split_stats()
    split_stats["configured_batch_size"] = batch_size

    # 1.1 Token-aware first-pass sizing: fit the batch to the confidence model's
    # output cap BEFORE the first call, so a small-cap model (Nova Lite, 10K) does
    # not truncate (bbox geometry roughly triples the per-row output). ``batch_size``
    # is a user CEILING and may be 0 meaning "no ceiling, derive it".
    all_list_fields_probe = [
        v for v in extraction_results.values() if isinstance(v, list) and v
    ]
    # Pass the whole row list: the widest row governs the batch, and rows are not
    # guaranteed uniform. Sized even when confidence_model_id is empty, so a missing
    # model falls back to a conservative size rather than to the configured ceiling.
    #
    # Only sized when there IS a list. With no list fields nothing below slices rows,
    # so the derived value would be unused — and computing it would import
    # ``extraction.sharding`` for the opaque-row fallback, pulling the whole
    # extraction package (and strands) into the Assessment Lambda for a number it
    # never reads.
    effective_batch_size = batch_size
    if all_list_fields_probe:
        widest_list = max(all_list_fields_probe, key=len)
        effective_batch_size = compute_token_aware_batch_size(
            confidence_model_id, widest_list, geometry_mode, batch_size
        )
        split_stats["derived_batch_size"] = effective_batch_size

    # Resolve the escalation model once (per-call override handled by caller via
    # config precedence). None -> the ladder's model step is skipped.
    ladder_escalation_model = escalation_model if escalation_enabled else None

    # Identify list fields large enough to warrant batching (uses the effective,
    # token-aware size so small-cap models batch sooner).
    list_fields = {
        k: v
        for k, v in extraction_results.items()
        if isinstance(v, list) and len(v) > effective_batch_size
    }

    def _one_call(results: dict[str, Any]) -> Any:
        return assessment_service.assess_results(
            class_label=class_label,
            extraction_results=results,
            document_text=document_text,
            page_images=page_images,
            ocr_text_confidence=ocr_text_confidence,
        )

    # 1.2 Escalation call closure + token-aware batch size for the STRONGER model
    # (a bigger output cap fits more rows per call). Built once so the ladder can
    # re-run only the still-missing rows on it. None when escalation is off.
    escalation_one_call: Callable[[dict[str, Any]], Any] | None = None
    escalation_batch_size = effective_batch_size
    if ladder_escalation_model and max_escalation_rounds > 0:

        def _escalation_call(results: dict[str, Any]) -> Any:
            return assessment_service.assess_results(
                class_label=class_label,
                extraction_results=results,
                document_text=document_text,
                page_images=page_images,
                ocr_text_confidence=ocr_text_confidence,
                model_id_override=ladder_escalation_model,
            )

        escalation_one_call = _escalation_call
        if all_list_fields_probe:
            escalation_batch_size = compute_token_aware_batch_size(
                ladder_escalation_model,
                max(all_list_fields_probe, key=len),
                geometry_mode,
                batch_size,
            )

    # All list fields (any size) — used to target missing-row retries even when
    # no list is large enough to require batching.
    all_list_fields = {
        k: v for k, v in extraction_results.items() if isinstance(v, list) and v
    }

    if not list_fields:
        core = _one_call(extraction_results)
        merged_assessment = reconcile_assessment_to_data(
            core.enhanced_assessment, extraction_results
        )
        merged_alerts = list(core.confidence_threshold_alerts or [])
        merged_metering = core.metering or {}
        duration_seconds = core.duration_seconds or 0.0
        # Retry missing rows for the largest list (small lists can still drop
        # rows — esp. agentic single-shot cramming values+confidence in one call).
        if core.truncated:
            split_stats["truncated_calls"] += 1
        if all_list_fields:
            big = max(all_list_fields, key=lambda k: len(all_list_fields[k]))
            merged_assessment, merged_alerts, merged_metering, dur = (
                _retry_missing_rows(
                    _one_call,
                    extraction_results=extraction_results,
                    big_field=big,
                    merged_assessment=merged_assessment,
                    merged_alerts=merged_alerts,
                    merged_metering=merged_metering,
                    batch_size=effective_batch_size,
                    max_retries=max_retries,
                    split_stats=split_stats,
                    escalation_one_call=escalation_one_call,
                    escalation_model=ladder_escalation_model,
                    escalation_batch_size=escalation_batch_size,
                    max_escalation_rounds=max_escalation_rounds,
                    deadline_epoch=deadline_epoch,
                    class_schema=class_schema,
                    model_id=confidence_model_id,
                )
            )
            duration_seconds += dur
            split_stats["unrecoverable_rows"] = len(
                _missing_row_indices(
                    merged_assessment.get(big), extraction_results.get(big)
                )
            )
        # #894: name the class in the oversized-row report/issue (the ladder itself
        # only knows the field), so the operator can find the offending class in
        # config without cross-referencing the section id.
        if split_stats.get("oversized_row_fields"):
            split_stats["oversized_row_class"] = class_label

        # Alert surface (upstream #813): row indexes in per-core alerts are LOCAL
        # to the slice each core was handed, so accumulating them yields paths
        # like transactions[6] for the merged list's row 16 — mislabeled, and
        # colliding across slices. When the schema and default threshold are
        # available, REGENERATE the surface from the merged assessment instead:
        # enrich_assessment_with_thresholds enumerates the full merged list, so
        # paths are globally indexed by construction, and the retry path's
        # slice-relative alerts stop mattering because they are no longer part
        # of the surface. The stored list is a derived copy of
        # explainability_info (see dedupe_alerts' docstring); this makes it a
        # pure projection of it. Without a schema the per-core accumulation
        # remains (there is nothing to resolve thresholds against).
        if class_schema is not None and default_confidence_threshold is not None:
            _, merged_alerts = enrich_assessment_with_thresholds(
                merged_assessment, class_schema, default_confidence_threshold
            )
        return {
            "assessment": merged_assessment,
            "alerts": dedupe_alerts(merged_alerts),
            "metering": merged_metering,
            "parsing_succeeded": core.parsing_succeeded,
            "duration_seconds": duration_seconds,
            "split_stats": split_stats,
        }

    # Batch by the largest list field; other (smaller) list fields ride the
    # first batch and are reconciled afterward.
    big_field = max(list_fields, key=lambda k: len(list_fields[k]))
    rows = extraction_results[big_field]
    merged_assessment: dict[str, Any] = {}
    merged_alerts: list[dict[str, Any]] = []
    merged_metering: dict[str, Any] = {}
    big_field_acc: list[Any] = []
    parsing_succeeded = True
    duration_seconds = 0.0

    chunks = [
        rows[start : start + effective_batch_size]
        for start in range(0, len(rows), effective_batch_size)
    ]
    split_stats["batch_count"] = len(chunks)
    split_stats["concurrent_batches"] = 1

    def _assess_chunk(chunk: list[Any], stats: dict[str, Any]) -> dict[str, Any]:
        # Same scalars/context every batch; only the big list is sliced. If the
        # model truncates the chunk's response, _assess_slice_adaptive halves it
        # and retries until it fits (recording the activity in ``stats``).
        # ``stats`` is a PER-CHUNK accumulator when running concurrently (merged
        # back afterward) so the shared split_stats dict is never mutated from
        # multiple threads.
        return _assess_slice_adaptive(
            _one_call,
            base_results=extraction_results,
            big_field=big_field,
            rows=chunk,
            reconcile=reconcile_assessment_to_data,
            stats=stats,
            deadline_epoch=deadline_epoch,
            model_id=confidence_model_id,
        )

    # Concurrency (item 4): a single confidence call over a large list is slow,
    # and batches are INDEPENDENT (each scores different rows with identical
    # scalars/context). But naively fanning out all batches cold makes every call
    # miss the prompt cache and pay the expensive cacheWrite (the storm the
    # sequential design avoided). So: run the FIRST batch alone to WARM the cache
    # (static instructions + the per-doc image/OCR block), THEN fan the rest out
    # concurrently — they hit the warm cache. When max_concurrent_batches <= 1
    # this degrades to the original sequential loop.
    #
    # Thread-safety: each concurrent chunk gets its OWN split-stats accumulator;
    # they are merged back into the shared split_stats deterministically after the
    # pool joins (no concurrent mutation of the shared dict).
    sliced_results: list[dict[str, Any]] = []
    if not chunks:
        pass
    elif max_concurrent_batches <= 1 or len(chunks) == 1:
        for chunk in chunks:
            sliced_results.append(_assess_chunk(chunk, split_stats))
    else:
        # Warm the cache with batch 0 (writes directly to shared stats).
        sliced_results.append(_assess_chunk(chunks[0], split_stats))

        # B3 — Fan-out inherits the batch size batch 0 actually SUCCEEDED on. If
        # batch 0 truncated and the adaptive splitter had to shrink it (recorded as
        # ``min_batch_size_used``), the full ``effective_batch_size`` is too big for
        # this model+payload — so re-chunk the REMAINING rows at the proven smaller
        # size instead of letting every fanned-out batch independently re-discover
        # the truncation (N parallel truncation storms). Only ever shrinks.
        proven = split_stats.get("min_batch_size_used")
        remaining_rows = rows[len(chunks[0]) :]
        if (
            proven
            and proven < effective_batch_size
            and split_stats.get("truncated_calls", 0) > 0
        ):
            logger.info(
                "assess_results_batched: batch 1 succeeded at %d rows (configured "
                "%d truncated); re-chunking remaining %d row(s) at %d for fan-out.",
                proven,
                effective_batch_size,
                len(remaining_rows),
                proven,
            )
            rest = [
                remaining_rows[s : s + proven]
                for s in range(0, len(remaining_rows), proven)
            ]
        else:
            rest = [
                remaining_rows[s : s + effective_batch_size]
                for s in range(0, len(remaining_rows), effective_batch_size)
            ]
        split_stats["batch_count"] = 1 + len(rest)
        if rest:
            import concurrent.futures as _cf

            workers = min(int(max_concurrent_batches), len(rest))
            split_stats["concurrent_batches"] = workers
            logger.info(
                "assess_results_batched: cache warmed on batch 1/%d; fanning out "
                "remaining %d batch(es) across %d worker(s)",
                1 + len(rest),
                len(rest),
                workers,
            )
            # Each concurrent chunk accumulates into its OWN stats dict; merge
            # them into the shared split_stats after the pool joins.
            per_chunk_stats = [_new_split_stats() for _ in rest]
            with _cf.ThreadPoolExecutor(max_workers=workers) as pool:
                # map preserves input order → results stay index-aligned.
                sliced_results.extend(pool.map(_assess_chunk, rest, per_chunk_stats))
            for cs in per_chunk_stats:
                # Sum every counter a worker could have incremented (including the
                # escalation counters — a worker can escalate inside its adaptive
                # split), and OR-merge the wall-clock `deadline_reached` flag so a
                # cutoff hit inside a concurrent worker still surfaces
                # `assessment_deadline_reached`. Batch_count/concurrent_batches are
                # set by the caller (below), not per-chunk, so they are not summed.
                for key in (
                    "truncated_calls",
                    "splits",
                    "rows_recovered_by_retry",
                    "rows_recovered_by_escalation",
                    "escalation_rounds",
                    "unrecoverable_rows",
                ):
                    split_stats[key] = split_stats.get(key, 0) + cs.get(key, 0)
                if cs.get("deadline_reached"):
                    split_stats["deadline_reached"] = True
                if cs.get("escalation_model") and not split_stats.get(
                    "escalation_model"
                ):
                    split_stats["escalation_model"] = cs["escalation_model"]
                # #894: an oversized-row terminal condition discovered INSIDE a
                # worker must survive the join, or the post-join retry rung would
                # still fire on the impossible rows and the reported issue would be
                # the generic "incomplete" one instead of the real cause.
                _oversized = split_stats.setdefault("oversized_row_fields", [])
                for fld in cs.get("oversized_row_fields") or []:
                    if fld not in _oversized:
                        _oversized.append(fld)
                _cs_cap = cs.get("oversized_row_output_cap") or 0
                if cs.get("oversized_row_model") and (
                    not split_stats.get("oversized_row_model")
                    or _cs_cap > (split_stats.get("oversized_row_output_cap") or 0)
                ):
                    split_stats["oversized_row_model"] = cs["oversized_row_model"]
                    split_stats["oversized_row_output_cap"] = cs.get(
                        "oversized_row_output_cap"
                    )
                if cs.get("oversized_row_chars") is not None:
                    split_stats["oversized_row_chars"] = max(
                        int(split_stats.get("oversized_row_chars") or 0),
                        int(cs["oversized_row_chars"]),
                    )
                cs_min = cs.get("min_batch_size_used")
                if cs_min is not None:
                    _record_min_batch(split_stats, cs_min)
            split_stats["concurrent_batches"] = workers

    # Accumulate per-row assessments IN ORDER (chunks were built in order and
    # concurrency preserved it), so big_field_acc lines up with rows.
    for sliced in sliced_results:
        big_field_acc.extend(sliced["rows"])
        merged_metering = utils.merge_metering_data(merged_metering, sliced["metering"])
        merged_alerts.extend(sliced["alerts"])
        duration_seconds += sliced["duration"]
        if not merged_assessment and sliced["scalars"]:
            merged_assessment = dict(sliced["scalars"])
    merged_assessment[big_field] = big_field_acc
    # Final alignment against the full extraction (pads any residual gap).
    merged_assessment = reconcile_assessment_to_data(
        merged_assessment, extraction_results
    )
    merged_assessment, merged_alerts, merged_metering, dur = _retry_missing_rows(
        _one_call,
        extraction_results=extraction_results,
        big_field=big_field,
        merged_assessment=merged_assessment,
        merged_alerts=merged_alerts,
        merged_metering=merged_metering,
        batch_size=effective_batch_size,
        max_retries=max_retries,
        split_stats=split_stats,
        escalation_one_call=escalation_one_call,
        escalation_model=ladder_escalation_model,
        escalation_batch_size=escalation_batch_size,
        max_escalation_rounds=max_escalation_rounds,
        deadline_epoch=deadline_epoch,
        class_schema=class_schema,
        model_id=confidence_model_id,
    )
    duration_seconds += dur

    # Count rows still unscored after all recovery, for visibility.
    split_stats["unrecoverable_rows"] = len(
        _missing_row_indices(merged_assessment.get(big_field), rows)
    )

    # #894: name the class in the oversized-row report/issue (see the single-call
    # branch above).
    if split_stats.get("oversized_row_fields"):
        split_stats["oversized_row_class"] = class_label

    # Alert surface regeneration — see the identical block on the single-call
    # branch above for the full rationale (upstream #813): per-core alert row
    # indexes are slice-local; rebuilding from the merged assessment makes them
    # global by construction.
    if class_schema is not None and default_confidence_threshold is not None:
        _, merged_alerts = enrich_assessment_with_thresholds(
            merged_assessment, class_schema, default_confidence_threshold
        )
    return {
        "assessment": merged_assessment,
        "alerts": dedupe_alerts(merged_alerts),
        "metering": merged_metering,
        "parsing_succeeded": parsing_succeeded,
        "duration_seconds": duration_seconds,
        "split_stats": split_stats,
    }


def _splice_missing_rows(
    call,
    *,
    rows: list[Any],
    base_results: dict[str, Any],
    big_field: str,
    merged_assessment: dict[str, Any],
    merged_alerts: list[dict[str, Any]],
    merged_metering: dict[str, Any],
    missing: list[int],
    batch_size: int,
    stats: dict[str, Any],
    recovery_counter: str,
    deadline_epoch: float | None = None,
    model_id: str | None = None,
) -> tuple[dict[str, Any], float, bool]:
    """One recovery pass over the ``missing`` indices with ``call``.

    Chunks the missing indices by ``batch_size``, runs each chunk through the
    adaptive splitter (so a chunk the model truncates is halved), and splices any
    recovered real scores back by original index. Increments ``stats`` under
    ``recovery_counter`` (``rows_recovered_by_retry`` for the same-model retry,
    ``rows_recovered_by_escalation`` for the stronger-model round). Best-effort:
    a failed call keeps the placeholder. Returns
    ``(merged_metering, added_duration, recovered_any)``."""
    added_duration = 0.0
    recovered_any = False
    for start in range(0, len(missing), batch_size):
        idx_chunk = missing[start : start + batch_size]
        try:
            sliced = _assess_slice_adaptive(
                call,
                base_results=base_results,
                big_field=big_field,
                rows=[rows[i] for i in idx_chunk],
                reconcile=reconcile_assessment_to_data,
                stats=stats,
                deadline_epoch=deadline_epoch,
                model_id=model_id,
            )
        except Exception as e:  # noqa: BLE001 - retry is best-effort
            logger.warning("Missing-row recovery call failed: %s", e)
            continue
        retry_rows = sliced["rows"]
        merged_metering = utils.merge_metering_data(merged_metering, sliced["metering"])
        merged_alerts.extend(sliced["alerts"])
        added_duration += sliced["duration"]
        for local_i, orig_i in enumerate(idx_chunk):
            if local_i < len(retry_rows) and not _row_confidence_missing(
                retry_rows[local_i]
            ):
                merged_assessment[big_field][orig_i] = retry_rows[local_i]
                recovered_any = True
                stats[recovery_counter] += 1
    return merged_metering, added_duration, recovered_any


def _retry_missing_rows(
    one_call,
    *,
    extraction_results: dict[str, Any],
    big_field: str,
    merged_assessment: dict[str, Any],
    merged_alerts: list[dict[str, Any]],
    merged_metering: dict[str, Any],
    batch_size: int,
    max_retries: int,
    split_stats: dict[str, Any] | None = None,
    escalation_one_call: Callable[[dict[str, Any]], Any] | None = None,
    escalation_model: str | None = None,
    escalation_batch_size: int | None = None,
    max_escalation_rounds: int = 0,
    deadline_epoch: float | None = None,
    class_schema: dict[str, Any] | None = None,
    model_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], float]:
    """Re-assess ONLY the list rows the model left unscored, splicing real scores
    back by index so large-list confidence coverage reaches 100% (not just null
    placeholders).

    **Schema-mismatch guard:** ``big_field`` may look permanently unscored not
    because the model dropped rows but because the extracted data is a list while
    the class schema does not declare the field as ``type: array`` (an off-schema
    attribute, or one typed as a scalar). The confidence enhancer collapses such a
    field to a single default leaf, so its rows are padded to null placeholders
    that never recover — no model can fix a schema mismatch. When ``class_schema``
    is supplied and flags this, BOTH recovery rungs are skipped for the field, the
    reason is recorded in ``split_stats['schema_mismatch_fields']``, and the rows
    are left as-is (surfaced by the caller as a schema-mismatch issue, not a futile
    escalation).

    **Oversized-row guard (#894):** likewise, when the adaptive splitter already
    truncated with a SINGLE row of ``big_field`` in the call
    (``split_stats['oversized_row_fields']``), the same-model retry rung is skipped
    outright and escalation is capped at ONE round. Retrying cannot converge — the
    batch was already one row — and each futile round costs a full model call (on a
    batching shape, 8 rows at batch 4, this halves the primary calls 28 → 14).

    The self-healing ladder, cheapest-first:
    1. **Same-model retry** — up to ``max_retries`` rounds on ``one_call`` (each
       chunk through the adaptive splitter, so a truncating chunk is halved). A
       round that recovers nothing stops this rung early.
    2. **Model escalation** — if rows are STILL missing and ``escalation_one_call``
       + ``escalation_model`` are set, re-assess only the residual rows on the
       stronger model (bigger output cap — the step that actually fixes small-cap
       truncation). Bounded by ``max_escalation_rounds``; a round that recovers
       nothing stops.

    **Wall-clock guard (1.5):** ``deadline_epoch`` (absolute epoch seconds, from
    the Lambda ``context.get_remaining_time_in_millis()``) bounds the slow
    escalation rung — before each escalation round the ladder checks the round's
    estimated cost fits in remaining time minus a safety reserve; if not it stops,
    sets ``deadline_reached`` in stats, and keeps what was recovered rather than
    risk a hard Lambda timeout. No-op when ``deadline_epoch`` is None.

    Sequential (no fan-out); best-effort (a failed call keeps the placeholder).
    Returns updated ``(assessment, alerts, metering, added_duration)``."""
    rows = extraction_results.get(big_field)
    if not isinstance(rows, list):
        return merged_assessment, merged_alerts, merged_metering, 0.0
    stats = split_stats if split_stats is not None else _new_split_stats()
    base_results = {k: v for k, v in extraction_results.items() if k != big_field}
    added_duration = 0.0

    # Schema-mismatch guard: if the "missing" rows are an artifact of the field
    # not being an array in the class schema (off-schema/scalar-typed, so the
    # enhancer collapsed it to one default leaf), NO model can score them per-row.
    # Record the reason and skip BOTH rungs — escalating here only burns a slow
    # large-model call to re-collapse the same list. (Only skip when the field is
    # ACTUALLY affected — i.e. it currently has missing rows — so a validly-typed
    # field is never blocked.)
    mismatch_reason = _schema_field_mismatch_reason(big_field, class_schema)
    if mismatch_reason and _missing_row_indices(merged_assessment.get(big_field), rows):
        stats.setdefault("schema_mismatch_fields", [])
        if big_field not in stats["schema_mismatch_fields"]:
            stats["schema_mismatch_fields"].append(big_field)
        logger.warning(
            "assess_results_batched: skipping retry/escalation for '%s' — %s. "
            "A stronger model cannot fix a schema mismatch; leaving rows unscored "
            "and flagging the root cause.",
            big_field,
            mismatch_reason,
        )
        return merged_assessment, merged_alerts, merged_metering, added_duration

    # #894 oversized-row guard: the first pass already proved the model truncates
    # with a SINGLE row of this field in the call, so every same-model retry round
    # would re-run the identical impossible call (each ~60s) and then bisect it again,
    # ending in the same unscored rows plus a misleading "shrink the batch" remedy.
    # Skip rung 1 entirely and allow at most ONE escalation round: a model with a
    # bigger output cap is the only remedy that can legitimately succeed, and one
    # round is enough to find out (a round that recovers nothing stops the ladder).
    oversized = big_field in (stats.get("oversized_row_fields") or [])
    retry_rounds = max_retries
    escalation_rounds_allowed = max_escalation_rounds
    if oversized:
        retry_rounds = 0
        escalation_rounds_allowed = min(max_escalation_rounds, 1)
        logger.error(
            "assess_results_batched: NOT retrying '%s' on %s — a single row already "
            "truncated the model's output, so no smaller batch exists and retrying "
            "cannot converge. %s",
            big_field,
            model_id or "the configured confidence model",
            (
                f"Trying at most one escalation round on a stronger model "
                f"({escalation_model})."
                if escalation_one_call is not None
                and escalation_model
                and escalation_rounds_allowed
                else "Leaving the rows unscored and reporting the root cause."
            ),
        )

    # Rung 1: same-model retry rounds.
    for _round in range(retry_rounds):
        missing = _missing_row_indices(merged_assessment.get(big_field), rows)
        if not missing:
            break
        logger.info(
            "assess_results_batched: retrying %d unscored '%s' rows (round %d)",
            len(missing),
            big_field,
            _round + 1,
        )
        merged_metering, dur, recovered_any = _splice_missing_rows(
            one_call,
            rows=rows,
            base_results=base_results,
            big_field=big_field,
            merged_assessment=merged_assessment,
            merged_alerts=merged_alerts,
            merged_metering=merged_metering,
            missing=missing,
            batch_size=batch_size,
            stats=stats,
            recovery_counter="rows_recovered_by_retry",
            deadline_epoch=deadline_epoch,
            model_id=model_id,
        )
        added_duration += dur
        # #894: the retry itself can be the first place a slice bisects down to one
        # row and still truncates. Stop this rung as soon as that is known instead
        # of spending the remaining rounds on the same impossible call.
        if big_field in (stats.get("oversized_row_fields") or []):
            logger.error(
                "assess_results_batched: stopping retries for '%s' — a single row "
                "still truncated the model's output; no smaller batch exists.",
                big_field,
            )
            escalation_rounds_allowed = min(escalation_rounds_allowed, 1)
            break
        if not recovered_any:
            logger.info("Missing-row retry made no progress; stopping retries.")
            break

    # Rung 2: model escalation for whatever is still missing. A stronger model
    # with a bigger output cap does not truncate on the residual rows that a
    # small-cap model (Nova Lite, 10K) kept dropping — this is the rung that
    # fixes the investigated failure.
    if (
        escalation_one_call is not None
        and escalation_model
        and escalation_rounds_allowed
    ):
        esc_batch = escalation_batch_size or batch_size
        for _eround in range(escalation_rounds_allowed):
            missing = _missing_row_indices(merged_assessment.get(big_field), rows)
            if not missing:
                break
            # Wall-clock guard: estimate this escalation round's cost (number of
            # chunks × observed avg call duration, floor 60s/chunk when nothing
            # measured yet — big models are slow) and stop if it won't fit in the
            # Lambda's remaining time minus the safety reserve.
            n_chunks = math.ceil(len(missing) / max(1, esc_batch))
            est_round_seconds = n_chunks * 60.0
            if not _deadline_allows(deadline_epoch, est_round_seconds):
                stats["deadline_reached"] = True
                logger.warning(
                    "assess_results_batched: skipping escalation of %d '%s' rows "
                    "— estimated %.0fs would exceed the Lambda time budget; "
                    "stopping self-healing and flagging deadline_reached.",
                    len(missing),
                    big_field,
                    est_round_seconds,
                )
                break
            logger.warning(
                "assess_results_batched: escalating %d still-unscored '%s' rows "
                "to stronger confidence model %s (round %d/%d)",
                len(missing),
                big_field,
                escalation_model,
                _eround + 1,
                escalation_rounds_allowed,
            )
            stats["escalation_model"] = escalation_model
            stats["escalation_rounds"] += 1
            merged_metering, dur, recovered_any = _splice_missing_rows(
                escalation_one_call,
                rows=rows,
                base_results=base_results,
                big_field=big_field,
                merged_assessment=merged_assessment,
                merged_alerts=merged_alerts,
                merged_metering=merged_metering,
                missing=missing,
                batch_size=esc_batch,
                stats=stats,
                recovery_counter="rows_recovered_by_escalation",
                deadline_epoch=deadline_epoch,
                model_id=escalation_model,
            )
            added_duration += dur
            if not recovered_any:
                logger.info("Escalation round made no progress; stopping ladder.")
                break

    return merged_assessment, merged_alerts, merged_metering, added_duration
