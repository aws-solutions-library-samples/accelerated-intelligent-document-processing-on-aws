# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stickler raw ``compare_with`` dict → IDP dataclasses.

Verdicts / counts / derived metrics come from Stickler's ROW-LEVEL
``field_comparisons`` (issue #625): per-attribute ``matched`` is the AND of
its rows' ``match`` fields via ``_is_match_true``, and section counts come
from ``aggregate_row_counts`` over the same rows. The pre-#625 design read
``cm.fields[name].overall`` and ``cm.aggregate``, both of which are
item-level after Hungarian pairing and hid leaf failures inside kept items
— parent verdict and drilldown could disagree on the same row. Using the
raw rows guarantees parent, section counts, and drilldown agree by
construction. No re-scoring, no private threshold table.

Kept module-boundary-clean: this module knows about Stickler's result shape
and IDP's dataclasses, but not about ``EvaluationService`` state or
orchestration. The service provides ``field_config``, ``match_threshold``,
``is_auto_generated``, and small callbacks; this module returns a fully-built
``SectionEvaluationResult``.
"""

import logging
import types
import typing
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from idp_common.evaluation.contract import (
    _is_match_true,
    aggregate_row_counts,
    iter_countable_rows,
    row_root_attribute,
    safe_div,
)
from idp_common.evaluation.models import (
    AttributeEvaluationResult,
    SectionEvaluationResult,
)

if TYPE_CHECKING:
    from stickler import StructuredModel

    from idp_common.models import Section

logger = logging.getLogger(__name__)


def resolve_leaf_schema(
    field_schema: Dict[str, Any], expected_key: str
) -> Optional[Dict[str, Any]]:
    """Walk ``field_schema`` down to the leaf a comparison's key points at.

    Given the attribute's schema and a canonical key like ``LineItems[0].Amount``
    or ``checks[0].bankInfo.bank``, walk the schema (descending through array
    ``items`` and object ``properties``, ignoring ``[index]`` segments) to the
    leaf field's schema so its ``x-aws-stickler-*`` config can be read.

    Returns None if the path can't be resolved (e.g. the schema doesn't
    describe that nested field — happens for auto-generated schemas).
    """
    segments = [
        seg.split("[", 1)[0] for seg in expected_key.split(".") if seg.split("[", 1)[0]
    ]
    # First segment is the root attribute itself; walk from segment 1 down.
    current: Any = field_schema
    for seg in segments[1:]:
        if not isinstance(current, dict):
            return None
        while isinstance(current, dict) and current.get("type") == "array":
            current = current.get("items", {})
        props = current.get("properties", {}) if isinstance(current, dict) else {}
        current = props.get(seg)
    return current if isinstance(current, dict) else None


def _unwrap_annotation(annotation: Any) -> Any:
    """Descend into ``List[X]`` / ``Optional[X]`` to the underlying model class.

    Stickler's ``ComparableField`` stores the resolved threshold on the LEAF
    field's ``json_schema_extra``, which lives on the nested model's
    ``model_fields``. To reach it we need to peel wrapper types off the
    parent field's annotation — ``LineItems: List[LineItem]`` → ``LineItem``.
    """
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if not args:
            return annotation
        annotation = args[0]
        origin = typing.get_origin(annotation)
    if origin in (list, tuple):
        args = typing.get_args(annotation)
        if args:
            annotation = args[0]
    return annotation


def resolve_leaf_model_field(root_model_cls: Any, expected_key: str) -> Optional[Any]:
    """Walk a Stickler ``StructuredModel`` class to the leaf field's ``FieldInfo``.

    Model-class analog of :func:`resolve_leaf_schema`. Given a root
    ``StructuredModel`` subclass and a canonical comparison key like
    ``LineItems[0].Amount``, descends through nested-model / list / optional
    annotations and returns the ``FieldInfo`` for the leaf so its
    ``json_schema_extra._threshold`` (populated by ``ComparableField`` at model
    build time — Stickler's actual applied threshold) can be read
    without going through the JSON-schema ``x-aws-stickler-threshold`` extension.

    Returns None if the path can't be resolved.
    """
    segments = [
        seg.split("[", 1)[0] for seg in expected_key.split(".") if seg.split("[", 1)[0]
    ]
    if not segments:
        return None

    # First segment is the root attribute; walk from segment 1 down. At each
    # step we peel List[]/Optional[] off the parent's annotation to reach the
    # nested StructuredModel that owns the next leaf.
    if not hasattr(root_model_cls, "model_fields"):
        return None
    current_field = root_model_cls.model_fields.get(segments[0])
    for seg in segments[1:]:
        if current_field is None:
            return None
        nested_cls = _unwrap_annotation(current_field.annotation)
        if not hasattr(nested_cls, "model_fields"):
            return None
        current_field = nested_cls.model_fields.get(seg)
    return current_field


def applied_threshold_from_field_info(field_info: Any) -> Optional[float]:
    """Read the applied threshold Stickler resolved for a field.

    Stickler's ``ComparableField`` stashes ``_threshold`` on the field's
    ``json_schema_extra`` at model build time; that value is what
    ``ConfigurationHelper.get_comparison_info`` returns at compare time and
    what Stickler's reason string ``"below threshold (X < Y)"`` uses for Y.
    Reading it here — rather than the schema's ``x-aws-stickler-threshold``
    extension — means the Method column in reports reflects the same number
    Stickler actually applied, even when the operator wrote a bare method
    annotation without an explicit threshold (Stickler's JSON-schema
    converter defaults to 0.5 for that case; the previous display fallback
    printed 0.7).

    Returns None if the field / extra / attribute isn't present.
    """
    if field_info is None:
        return None
    extra = getattr(field_info, "json_schema_extra", None)
    if extra is None:
        return None
    threshold = getattr(extra, "_threshold", None)
    return threshold if isinstance(threshold, (int, float)) else None


def annotate_nested_comparison_methods(
    field_comparisons: List[Dict[str, Any]],
    field_schema: Dict[str, Any],
    match_threshold: float,
    format_evaluation_method: Callable[..., str],
    root_model_cls: Optional[Any] = None,
) -> None:
    """Add per-field ``evaluation_method`` and ``weight`` to nested comparisons.

    Stickler's ``field_comparisons`` dicts carry only expected/actual keys,
    values, match, score and reason — the comparator and weight used for each
    nested field are computed internally and dropped. Re-derive them by
    walking the translated schema to the leaf so the Nested Field Comparison
    table (and the Visual Editor overlay) can show the same Method/Weight
    columns as the top-level attributes table.

    Mutates the dicts in place (adds ``evaluation_method`` and ``weight``).

    Threshold source of truth: when ``root_model_cls`` is supplied,
    the per-leaf threshold is read from
    ``model_fields[…].json_schema_extra._threshold`` on the Stickler model —
    the value ``ComparableField`` stashed at build time and the one Stickler's
    ``ConfigurationHelper`` uses at compare time. This makes the Method
    column agree with Stickler's ``"below threshold (X < Y)"`` reason string
    even for fields where the operator wrote a bare method annotation with no
    explicit threshold (Stickler defaults 0.5; ``_format_evaluation_method``
    previously guessed 0.7 from a hardcoded per-method table that has since
    been removed). Schema-based lookup is kept as a fallback for callers
    that don't thread the model class through.

    UPSTREAM: candidate for `awslabs/stickler` — emit comparator name +
    weight + threshold + list_match_threshold on every ``field_comparisons``
    row so downstream renderers don't have to re-walk the schema. Delete this
    function (and ``resolve_leaf_schema`` / ``resolve_leaf_model_field``)
    once upstream carries the metadata. No open issue yet — file one if this
    branch survives beyond one Stickler upgrade.

    Args:
        field_comparisons: Stickler nested comparison dicts for one attribute.
        field_schema: Translated schema for the (array or object) attribute.
        match_threshold: Document-level Hungarian match threshold fallback.
        format_evaluation_method: Callback that produces the display string
            (kept as a callback so this module doesn't depend on service.py's
            display helpers).
        root_model_cls: The Stickler ``StructuredModel`` subclass for the
            section (``type(expected_instance)``). When provided, the per-leaf
            threshold is read from the model rather than from the schema
            extension. Pass ``None`` and the function falls back to the
            schema-only behavior.
    """
    for fc in field_comparisons:
        key = str(fc.get("expected_key") or fc.get("actual_key") or "")
        leaf_schema = resolve_leaf_schema(field_schema, key)

        if isinstance(leaf_schema, dict):
            comparator = leaf_schema.get("x-aws-stickler-comparator")
            threshold = leaf_schema.get("x-aws-stickler-threshold")
            weight = leaf_schema.get("x-aws-stickler-weight")
            list_match_threshold = leaf_schema.get("x-aws-stickler-match-threshold")
        else:
            comparator = threshold = weight = list_match_threshold = None

        # Prefer Stickler's applied threshold from the model — the schema's
        # x-aws-stickler-threshold extension is absent for bare method
        # annotations (operator wrote FUZZY without evaluation-threshold), but
        # ComparableField still stashed the resolved value on _threshold.
        if root_model_cls is not None:
            leaf_field_info = resolve_leaf_model_field(root_model_cls, key)
            applied = applied_threshold_from_field_info(leaf_field_info)
            if applied is not None:
                threshold = applied

        fc["evaluation_method"] = format_evaluation_method(
            comparator_method=comparator,
            expected_value=fc.get("expected_value"),
            actual_value=fc.get("actual_value"),
            field_specific_threshold=threshold,
            match_threshold=match_threshold,
            list_match_threshold=list_match_threshold,
        )
        fc["weight"] = weight if weight is not None else 1.0


def _instance_to_dict(instance: Any) -> Dict[str, Any]:
    """Serialize a Stickler ``StructuredModel`` instance to a plain dict."""
    if hasattr(instance, "model_dump"):
        return instance.model_dump(mode="python")
    if hasattr(instance, "dict"):
        return instance.dict()
    return dict(instance)


def _has_nonzero_counts(node: Optional[Dict[str, Any]]) -> bool:
    """True iff a Stickler confusion-matrix cell dict has ANY positive count.

    Used to distinguish "genuinely-empty section" (all-zero populated
    ``cm.aggregate``/``overall`` dict, which is what Stickler emits for
    that shape and is NOT a Stickler shape change to warn about) from
    "row list is empty but counts say otherwise" (a shape drift we DO
    want to log).

    Defensive against non-numeric values under the count keys — a
    Stickler shape drift that put a string / None / dict at one of the
    ``tp/fa/fd/tn/fn`` slots should be a WARN (from the caller), not a
    ValueError-crash in the helper that was supposed to detect it.
    """
    if not node:
        return False
    for k in ("tp", "fa", "fd", "tn", "fn"):
        raw = node.get(k, 0)
        if raw is None:
            continue
        try:
            if int(raw) > 0:
                return True
        except (TypeError, ValueError):
            # Non-numeric under a count key — treat as "some content"
            # so the caller's shape-drift warning still fires.
            return True
    return False


def transform_stickler_result(
    section: "Section",
    expected_instance: "StructuredModel",
    actual_instance: "StructuredModel",
    stickler_result: Dict[str, Any],
    confidence_scores: Dict[str, Any],
    stickler_models: Dict[str, Dict[str, Any]],
    auto_generated_models: set,
    get_nested_value: Callable[[Any, str], Any],
    get_confidence_for_field: Callable[[Dict[str, Any], str], Optional[Dict[str, Any]]],
    generate_reason: Callable[..., str],
    format_evaluation_method: Callable[..., str],
    document_context: str = "",
) -> SectionEvaluationResult:
    """Convert Stickler's ``compare_with`` dict into a ``SectionEvaluationResult``.

    Verdicts / counts / derived metrics come from Stickler's row-level
    ``field_comparisons`` (issue #625): per-attribute ``matched`` is the
    AND of its rows' ``match`` fields via the shared ``_is_match_true``
    predicate, and section-level counts / precision / recall / F1 /
    accuracy are derived from ``aggregate_row_counts`` over the same
    rows. The pre-#625 design read ``cm.fields[name].overall`` and
    ``cm.aggregate``, both item-level rollups that hid leaf failures
    inside Hungarian-matched items — parent verdict could show ✓ while
    the drilldown showed red children. Row-level derivation guarantees
    parent, section counts, and drilldown agree by construction.

    Args:
        section: Section metadata (id, classification).
        expected_instance / actual_instance: Stickler model instances used for
            the comparison, dumped to dicts here so per-field lookups can
            resolve nested paths.
        stickler_result: Raw dict returned by ``expected.compare_with(actual, ...)``.
        confidence_scores: Assessment-side confidence dict (keyed by field path).
        stickler_models: The service's pre-built Stickler config map (used to
            surface per-field comparator / weight / threshold on the IDP
            dataclass output).
        auto_generated_models: Set of lowercase class names whose schema was
            auto-inferred (annotated in the report).
        get_nested_value / get_confidence_for_field / generate_reason /
            format_evaluation_method: Callbacks kept in ``service.py`` (this
            module is purely a Stickler→IDP converter, no display / helper
            logic of its own).

    Returns:
        Fully-populated ``SectionEvaluationResult`` with the raw
        ``stickler_comparison_result`` blob attached (the cross-Lambda
        contract; see ``contract.py``).
    """
    expected_dict = _instance_to_dict(expected_instance)
    actual_dict = _instance_to_dict(actual_instance)

    # Root model class — used to read Stickler's applied per-field threshold
    # from ``model_fields[...].json_schema_extra._threshold``.
    # Available whenever the section produced a Stickler comparison; falls
    # back to schema-only reads otherwise (auto-generated schemas that failed
    # to build a model, etc.).
    root_model_cls = type(expected_instance) if expected_instance is not None else None

    field_scores = stickler_result.get("field_scores", {})
    # ``or []`` (not ``.get(key, [])``) so a ``{"field_comparisons": null}``
    # payload from a Stickler variant emitting an explicit null still
    # iterates safely — the default in ``.get`` only fires when the key
    # is missing, not when the value is None.
    field_comparisons: List[Dict[str, Any]] = (
        stickler_result.get("field_comparisons") or []
    )

    # Group field comparisons by top-level field name for attachment to
    # attributes: field_comparisons is a flat list, group by root field.
    # Uses the SHARED ``row_root_attribute`` helper (also consumed by
    # ``iter_countable_rows`` below and by the aggregation Lambda) so
    # the two groupings can't drift on rows whose root comes from
    # ``actual_key`` or ``field_path`` rather than ``expected_key``
    # (finding from #625 high review — the previous local
    # ``expected_key.split(...)`` was a second, less-forgiving copy of
    # the same logic).
    field_comparison_map: Dict[str, List[Dict[str, Any]]] = {}
    for fc in field_comparisons:
        root_field = row_root_attribute(fc)
        if root_field:
            field_comparison_map.setdefault(root_field, []).append(fc)

    stickler_config = stickler_models.get(section.classification.lower(), {})
    match_threshold = stickler_config.get("match_threshold", 0.8)
    is_auto_generated = section.classification.lower() in auto_generated_models

    schema = stickler_config.get("schema", {})
    properties = schema.get("properties", {})

    # Per-field config surfaces on the IDP dataclass. NUMERIC_EXACT routes
    # ``evaluation-threshold`` into ``comparator-config.tolerance`` (R1) — pick
    # that up as the display threshold so the report keeps showing the
    # user-configured value.
    #
    # ``configured_threshold`` (schema extension or NUMERIC_EXACT tolerance)
    # becomes ``AttributeEvaluationResult.evaluation_threshold`` — the user's
    # explicit configuration, preserved as ``None`` when nothing was set.
    # ``applied_threshold`` (from the Stickler model) is what
    # Stickler actually scored against; it's only used to build the Method
    # display string and is not persisted on the dataclass.
    field_configs: Dict[str, Dict[str, Any]] = {}
    for field_name, field_schema in properties.items():
        comparator_cfg = field_schema.get("x-aws-stickler-comparator-config") or {}
        tolerance = (
            comparator_cfg.get("tolerance")
            if isinstance(comparator_cfg, dict)
            else None
        )
        # ``is not None`` (not ``or``) so a valid threshold of exactly 0.0
        # survives. ``0.0 or tolerance`` would silently substitute the
        # tolerance and change the meaning of the comparison.
        explicit_threshold = field_schema.get("x-aws-stickler-threshold")
        configured_threshold = (
            explicit_threshold if explicit_threshold is not None else tolerance
        )
        applied_threshold: Optional[float] = None
        if root_model_cls is not None and hasattr(root_model_cls, "model_fields"):
            applied_threshold = applied_threshold_from_field_info(
                root_model_cls.model_fields.get(field_name)
            )
        field_configs[field_name] = {
            "threshold": configured_threshold,
            "applied_threshold": applied_threshold,
            "match_threshold": field_schema.get("x-aws-stickler-match-threshold"),
            "comparator": field_schema.get("x-aws-stickler-comparator"),
            "weight": field_schema.get("x-aws-stickler-weight"),
        }

    # Per-field verdicts + section counts come from Stickler's row-level
    # ``field_comparisons`` — the same rows the UI drilldown displays.
    # Reading these directly is the only way to guarantee the parent verdict
    # never contradicts its children: for a list field, ``cm.overall`` and
    # ``all_fields_matched`` collapse to item-level after Hungarian pairing
    # and hide leaf failures inside kept items (issue #625). ``cm.aggregate``
    # goes the other way — it drops rejected items entirely, so their
    # false discoveries never reach the section counts. Only the raw
    # ``field_comparisons`` rows honor every failure mode.
    cm = stickler_result.get("confusion_matrix") or {}
    # ``field_comparisons`` was already bound above for the attachment map;
    # reuse that binding instead of re-reading from ``stickler_result``.

    # Filter to rows attributable to a parent attribute — both per-doc and
    # run-level aggregators use the same shared filter so their counts agree
    # (issue #625 finding 2). Then bucket by root attribute for O(N) per-
    # attribute verdict evaluation.
    # Include doc context so warnings from different documents with the
    # same section_id don't dedupe against each other (a section_id like
    # ``"s1"`` recurs across documents; without doc-scoping the first doc
    # would silence every subsequent doc's warning — finding 1 from
    # round-4 review). Falls back to section-only when no doc context is
    # available.
    ctx_prefix = f"doc={document_context} " if document_context else ""
    countable_rows = iter_countable_rows(
        field_comparisons, context=f"{ctx_prefix}section:{section.section_id}"
    )
    rows_by_attr: Dict[str, List[Dict[str, Any]]] = {}
    for fc in countable_rows:
        rows_by_attr.setdefault(row_root_attribute(fc), []).append(fc)

    attribute_results: List[AttributeEvaluationResult] = []
    for field_name, score in field_scores.items():
        field_config = field_configs.get(field_name, {})
        expected_value = get_nested_value(expected_dict, field_name)
        actual_value = get_nested_value(actual_dict, field_name)
        confidence_info = get_confidence_for_field(confidence_scores, field_name)

        # Verdict: parent is ✓ iff every drilldown row under it is ✓. Falls
        # through to a counts-only rule ONLY if the field has no rows (rare —
        # Stickler always emits at least one for scalars, and one per item or
        # leaf for structured fields). Fallback re-derives its counts from the
        # SAME per-attribute rows source ``aggregate_row_counts`` reads for
        # section-level metrics — reading ``cm_fields[name].overall`` here
        # would let the row-based section aggregate and the attribute verdict
        # drift on the same document (finding 2 from #625 round-4 review).
        # Fallback deliberately does NOT consult ``all_fields_matched`` — that
        # flag is item-level for list fields and is exactly the item-level
        # rollup this module was rewritten to stop trusting (issue #625).
        my_rows = rows_by_attr.get(field_name) or []
        if my_rows:
            # Narrow allowlist via the SHARED ``_is_match_true`` helper
            # so this predicate agrees with ``classify_field_comparison``
            # on the SAME row (section counts and per-attribute verdict
            # must not disagree — that's the parent-vs-section drift
            # #625 exists to eliminate). Accepts True / 1 / "true";
            # rejects everything else including the truthy-but-semantic-
            # False string ``"false"`` that raw ``bool()`` would flip.
            matched = all(_is_match_true(fc.get("match")) for fc in my_rows)
        else:
            # No rows — nothing in ``field_comparisons`` for this attribute.
            # Section counts are unaffected (no rows contributed), so any
            # verdict here is compatible with the section-count source.
            # Defer to Stickler's ``score`` compared to the field's match
            # threshold: keeping the verdict and the score on the SAME row
            # in agreement avoids "✗ with score 1.0" contradictions in the
            # UI (finding from #625 review-effort code review — earlier
            # unconditional False produced score/matched drift). For a
            # LIST-typed field, use ``list_match_threshold`` (the Hungarian
            # match gate) rather than ``applied_threshold`` (the scalar
            # comparator's per-field threshold) — those two are not the
            # same number and a partial list score compared against a
            # scalar threshold can flip the verdict the wrong way. Falls
            # back to the section-level ``match_threshold`` (Stickler's
            # 0.8 default) when neither is configured.
            field_schema_here = properties.get(field_name, {}) or {}
            is_list_field = field_schema_here.get("type") == "array"
            if is_list_field:
                list_thresh = field_config.get("match_threshold")
                attr_threshold = (
                    list_thresh if list_thresh is not None else match_threshold
                )
            else:
                applied = field_config.get("applied_threshold")
                attr_threshold = applied if applied is not None else match_threshold
            try:
                matched = float(score) >= float(attr_threshold)
            except (TypeError, ValueError):
                matched = False

        reason = generate_reason(
            field_name,
            expected_value,
            actual_value,
            score,
            matched,
            field_config.get("comparator"),
            is_auto_generated=is_auto_generated,
        )

        field_specific_threshold = field_config.get("threshold")
        comparator_method = field_config.get("comparator")
        # The Method display string uses Stickler's applied threshold when the
        # operator omitted an explicit one — that's the value
        # Stickler's reason string ``"below threshold (X < Y)"`` uses for Y.
        # Falls back to the configured value when the model lookup wasn't
        # possible (e.g. auto-generated section with no built model).
        # ``is not None`` (not ``or``) so a display threshold of exactly
        # 0.0 survives — same reasoning as the ``configured_threshold``
        # binding above.
        applied = field_config.get("applied_threshold")
        display_threshold = applied if applied is not None else field_specific_threshold
        evaluation_method_value = format_evaluation_method(
            comparator_method=comparator_method,
            expected_value=expected_value,
            actual_value=actual_value,
            field_specific_threshold=display_threshold,
            match_threshold=match_threshold,
            list_match_threshold=field_config.get("match_threshold"),
        )

        detailed_comparisons = field_comparison_map.get(field_name)
        if detailed_comparisons:
            annotate_nested_comparison_methods(
                detailed_comparisons,
                field_schema=properties.get(field_name, {}),
                match_threshold=match_threshold,
                format_evaluation_method=format_evaluation_method,
                root_model_cls=root_model_cls,
            )

        attribute_results.append(
            AttributeEvaluationResult(
                name=field_name,
                expected=expected_value,
                actual=actual_value,
                matched=matched,
                score=score,
                reason=reason,
                evaluation_method=evaluation_method_value,
                evaluation_threshold=field_specific_threshold,
                comparator_type=field_config.get("comparator"),
                confidence=(
                    confidence_info.get("confidence") if confidence_info else None
                ),
                confidence_threshold=(
                    confidence_info.get("confidence_threshold")
                    if confidence_info
                    else None
                ),
                weight=field_config.get("weight"),
                field_comparison_details=detailed_comparisons,
            )
        )

    attribute_results.sort(key=lambda ar: ar.name)

    # Section-level counts: derive by classifying every ``field_comparisons``
    # row via the shared ``aggregate_row_counts`` helper (kept in
    # ``idp_common.evaluation.contract`` so the aggregation Lambda uses the same
    # semantics — a divergence between per-doc and run-level classification
    # would silently reintroduce the class of inconsistency issue #625 fixed).
    # This view captures both list-item FDs (which ``cm.aggregate`` misses) and
    # leaf-level FDs inside kept items (which ``cm.overall`` misses).
    #
    # If Stickler emits no rows we do NOT silently fall back to
    # ``cm.aggregate`` — that would stamp v2.0-semantics on v1.0-semantics
    # counts, defeating the drift-warning gate downstream (finding 2 from #625
    # adversarial review). An empty row list is either a genuinely empty
    # document (0 counts is correct) or a Stickler shape change we want to
    # surface loudly rather than paper over.
    if not countable_rows and (
        _has_nonzero_counts(cm.get("aggregate"))
        or _has_nonzero_counts(cm.get("overall"))
    ):
        logger.warning(
            "Stickler emitted non-zero confusion_matrix counts but empty "
            "field_comparisons on section %s — reporting 0 counts. This may "
            "indicate a Stickler shape change; investigate before trusting "
            "the run's metrics.",
            section.section_id,
        )
    counts = aggregate_row_counts(countable_rows)
    agg_tp = counts["tp"]
    agg_fa = counts["fa"]
    agg_fd = counts["fd"]
    agg_fp = counts["fp"]
    agg_tn = counts["tn"]
    agg_fn = counts["fn"]
    total = agg_tp + agg_fp + agg_fn + agg_tn

    # ``Dict[str, Any]`` because later code adds ``_stickler_counts`` (nested
    # dict), ``evaluation_failed`` (bool), and ``failure_type`` (str). An
    # earlier ``Dict[str, float]`` annotation lied about the actual shape.
    metrics: Dict[str, Any] = {
        "precision": safe_div(agg_tp, agg_tp + agg_fp),
        "recall": safe_div(agg_tp, agg_tp + agg_fn),
        "f1_score": safe_div(2 * agg_tp, 2 * agg_tp + agg_fp + agg_fn),
        "accuracy": safe_div(agg_tp + agg_tn, total),
        "false_alarm_rate": safe_div(agg_fa, agg_fa + agg_tn),
        "false_discovery_rate": safe_div(agg_fd, agg_fd + agg_tp),
    }
    # Raw counts for _process_section's document-level rollup (surfaced under
    # a stable key so the metrics dict stays visually clean).
    metrics["_stickler_counts"] = {
        "tp": agg_tp,
        "fa": agg_fa,
        "fd": agg_fd,
        "fp": agg_fp,
        "tn": agg_tn,
        "fn": agg_fn,
    }
    metrics["weighted_overall_score"] = stickler_result.get("overall_score", 0.0)

    return SectionEvaluationResult(
        section_id=section.section_id,
        document_class=section.classification,
        attributes=attribute_results,
        metrics=metrics,
        stickler_comparison_result=stickler_result,
    )
