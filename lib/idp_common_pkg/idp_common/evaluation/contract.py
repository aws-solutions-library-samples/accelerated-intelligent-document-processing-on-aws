# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cross-Lambda contract for the evaluation pipeline.

The evaluation service (``EvaluationService`` in ``service.py``) writes
per-doc ``results.json`` files that the ``test_execution_aggregation_function``
Lambda reads. Both sides live in different packages / deploy artifacts; the
shape of what they exchange (``stickler_comparison_result`` blob, the
``compare_with`` flag set that produced it, and the S3 key template) is a
de-facto API. This module makes the contract explicit so a shape change fails
loudly at read time rather than as wrong dashboard numbers downstream.

Bump ``STICKLER_RESULT_VERSION`` on any change that alters the raw blob shape
Stickler emits (a stickler-eval upgrade, a new ``compare_with`` flag, a
different accumulator). The aggregation Lambda can key off it to reject
mismatched blobs (or migrate) instead of silently doubling counters.
"""

import logging
from typing import Any, Dict, Iterable, List

logger = logging.getLogger(__name__)


def _is_empty_value(v: Any) -> bool:
    """Semantic "empty" for a field's expected/actual value in a
    ``field_comparisons`` row.

    Stickler emits `None` when the value is absent and `""`/`[]`/`{}` when the
    value is present-but-empty. All four count as "no value" for the purposes
    of tn (correctly-empty) / fa (hallucinated) / fn (missed) classification —
    a correctly-empty list is a `tn`, not a `tp`.
    """
    if v is None:
        return True
    if isinstance(v, (str, list, dict)) and len(v) == 0:
        return True
    return False


def _count_leaves(value: Any) -> int:
    """Count scalar leaves in a possibly-nested value.

    An "item-level" Stickler row for a rejected/missing/extra list item carries
    the whole item as one side of the comparison. To keep counts truly leaf-
    normalized we weight that row by the number of scalar leaves inside the
    item — otherwise dropping N items counts as N fn while dropping N leaves
    inside a kept item counts as N fn, so a truncated 5-item / 2-leaf-per-item
    list under-counts fn by 2× and recall inflates from 0.40 to 0.57 (#625,
    finding 1 from adversarial review).

    Recurses through dicts, lists, and Stickler / pydantic models. Returns 0
    for None and empty containers (caller applies min-1 floor).
    """
    if value is None:
        return 0
    if isinstance(value, (str, int, float, bool)):
        return 1
    if isinstance(value, dict):
        return sum(_count_leaves(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return sum(_count_leaves(v) for v in value)
    # Stickler / pydantic model — prefer model_dump if present, else __dict__.
    if hasattr(value, "model_dump"):
        try:
            return _count_leaves(value.model_dump())
        except Exception:  # noqa: BLE001 — fall through to __dict__
            pass
    if hasattr(value, "__dict__"):
        try:
            return _count_leaves(
                {k: v for k, v in vars(value).items() if not k.startswith("_")}
            )
        except Exception:  # noqa: BLE001 — unknown object shape
            pass
    # Unknown scalar-like → 1
    return 1


def _row_weight(fc: Dict[str, Any]) -> int:
    """Number of leaf comparisons a row represents.

    Stickler emits one row per LEAF for Hungarian-paired items and one row per
    ITEM for rejected/missing/extra items. For a row whose non-None side is
    structured (dict / list / model), we weight by ``max(1, leaf_count)`` so
    both shapes contribute the same leaf-normalized units to the confusion
    matrix. For a scalar leaf row, the weight is 1.
    """
    exp = fc.get("expected_value")
    act = fc.get("actual_value")
    value = exp if exp is not None else act
    if isinstance(value, (dict, list, tuple, set)) or (
        value is not None
        and not isinstance(value, (str, int, float, bool))
        and (hasattr(value, "model_dump") or hasattr(value, "__dict__"))
    ):
        return max(1, _count_leaves(value))
    return 1


def classify_field_comparison(fc: Dict[str, Any]) -> str:
    """Classify a single ``field_comparisons`` row into one of the confusion-
    matrix cells.

    Returns one of ``"tp"``, ``"tn"``, ``"fa"``, ``"fn"``, ``"fd"`` — matching
    Stickler's confusion-matrix meaning:

    * ``tp`` — match=True with an expected value (correct hit)
    * ``tn`` — match=True with no expected value (correctly-empty field)
    * ``fa`` — match=False with no expected value, actual present (hallucination)
    * ``fn`` — match=False with expected present, no actual (missed)
    * ``fd`` — match=False with both sides present (false discovery / wrong value)

    Both-empty ``match=False`` (unreachable on current Stickler — an empty vs
    empty comparison scores 1.0 → matched=True — but the safer terminal
    branch semantically) is classified as ``fn`` (nothing predicted).

    Consumed by:
    * ``stickler_backend/results.py`` for section-level ``_stickler_counts``
    * ``test_execution_aggregation_function/index.py`` for run-level metrics

    Kept in this module because both call sites must agree on the classification
    for per-doc and run-level dashboards to report the same numbers on the same
    input — a divergence between them would silently reintroduce the class of
    inconsistency issue #625 was fixing at a different level.
    """
    matched = fc.get("match") is True
    gt_empty = _is_empty_value(fc.get("expected_value"))
    pr_empty = _is_empty_value(fc.get("actual_value"))
    if matched:
        return "tn" if gt_empty else "tp"
    # match=False branches — order matters. Both-empty is unreachable in
    # practice but if it happens, prefer "fn" (nothing came out) over "fd"
    # (wrong value) as the terminal branch.
    if pr_empty:
        return "fn"
    if gt_empty:
        return "fa"
    return "fd"


def row_root_attribute(fc: Dict[str, Any]) -> str:
    """Extract the root attribute name from a row's field path.

    Stickler emits ``expected_key`` (and ``field_path`` on some code paths) as
    either a scalar name (``customer_name``), a list index path
    (``Items[3].name``), or a nested-object path (``Address.city``). The root
    is everything before the first ``[`` or ``.``. Rows whose path begins with
    ``[`` or ``.`` have no attributable root — the caller decides what to do
    (see ``iter_countable_rows``).
    """
    path = fc.get("expected_key") or fc.get("actual_key") or fc.get("field_path") or ""
    idx_bracket = path.find("[")
    idx_dot = path.find(".")
    cuts = [i for i in (idx_bracket, idx_dot) if i >= 0]
    return path[: min(cuts)] if cuts else path


def iter_countable_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    context: str = "",
) -> List[Dict[str, Any]]:
    """Filter rows to those that attribute to a parent attribute.

    A row whose ``field_path`` begins with ``[`` or ``.`` (no leading attribute
    name) has no root attribute — such rows can't be reflected in the parent-
    attribute verdict, so counting them at the section level while excluding
    them from per-attribute buckets would break the "parent ✓ iff no red row"
    invariant. Both per-doc and run-level aggregators must apply the same
    filter for their counts to agree.

    Not observed on current Stickler builds; the log surfaces it if the shape
    ever appears rather than silently reintroducing parent-vs-section drift.
    """
    kept: List[Dict[str, Any]] = []
    for fc in rows:
        root = row_root_attribute(fc)
        if not root:
            logger.warning(
                "Skipping field_comparisons row with anonymous root "
                "(path=%r, context=%r) — cannot attribute to a parent attribute.",
                fc.get("expected_key") or fc.get("actual_key") or fc.get("field_path"),
                context or "unknown",
            )
            continue
        kept.append(fc)
    return kept


def aggregate_row_counts(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Aggregate ``field_comparisons`` rows into a confusion-matrix count dict.

    Returns ``{tp, fa, fd, fp, tn, fn}`` where ``fp = fa + fd``. Rows are
    weighted by leaf count (see ``_row_weight``) so item-level and leaf-level
    rows contribute the same units — this is what keeps recall on a truncated
    list from inflating (finding 1 from #625 adversarial review). Callers layer
    their own derived metrics (precision/recall/F1/accuracy/FAR/FDR) on top —
    the derivation matches Stickler's ``DerivedMetricsCalculator`` semantics
    and lives in the caller so this module stays a pure schema/contract.
    """
    counts = {"tp": 0, "fa": 0, "fd": 0, "tn": 0, "fn": 0}
    for fc in rows:
        counts[classify_field_comparison(fc)] += _row_weight(fc)
    counts["fp"] = counts["fa"] + counts["fd"]
    return counts


# S3 key template for per-document evaluation output. Both the evaluation
# service and the aggregation Lambda import this rather than each pinning
# their own copy of the string.
EVALUATION_RESULTS_KEY_TEMPLATE = "{document_input_key}/evaluation/results.json"


def evaluation_results_key(document_input_key: str) -> str:
    """Return the S3 key under which per-doc results.json is stored.

    Args:
        document_input_key: The document's ``input_key`` field (the S3 key of
            the source document within its input bucket).
    """
    return EVALUATION_RESULTS_KEY_TEMPLATE.format(document_input_key=document_input_key)


# Flag set passed to ``expected_instance.compare_with(...)`` for each section.
# Change this (add/remove a flag) → the raw blob's shape changes → bump
# STICKLER_RESULT_VERSION.
def compare_with_flags() -> Dict[str, Any]:
    """The exact keyword arguments the evaluation service passes to
    ``StructuredModel.compare_with`` for every section. Kept in one place so
    the aggregation Lambda can assert the same flags are in force when it
    validates old ``results.json`` payloads.
    """
    # Avoid importing stickler here — this dict is data, not a call site.
    return {
        "document_field_comparisons": True,
        "document_non_matches": True,
        "include_confusion_matrix": True,
        "add_derived_metrics": True,
        "add_confidence_metrics": True,
    }


# Version stamp for the ``stickler_comparison_result`` blob shape AND the
# derived ``_stickler_counts`` semantics. Bump on ANY change that alters what
# appears at ``result["fields"][name]``, ``result["confusion_matrix"]``,
# ``result["confidence_metrics"]``, the top-level keys the aggregation Lambda
# relies on, OR the meaning of the values in ``_stickler_counts`` on
# ``SectionEvaluationResult.metrics``. Numeric MAJOR.MINOR string so
# lexicographic and numeric ordering agree.
#
# Version history:
#   1.0 — initial (v0.6.3 stickler cleanup): counts sourced from
#         ``cm["aggregate"]`` (leaf-level of matched items) and later
#         ``cm["overall"]`` (item-level after Hungarian pairing). Both
#         missed at least one failure mode on list-heavy documents (see #625).
#   2.0 — leaf-level from row-level ``field_comparisons``: every threshold-
#         gated leaf verdict Stickler emits contributes to section and
#         document counts. Fixes both the parent-vs-children contradiction
#         and the section-metric inflation on list-heavy documents.
STICKLER_RESULT_VERSION = "2.0"
