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

from typing import Any, Dict, Iterable


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
    if gt_empty and not pr_empty:
        return "fa"
    if not gt_empty and pr_empty:
        return "fn"
    return "fd"


def aggregate_row_counts(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Aggregate ``field_comparisons`` rows into a confusion-matrix count dict.

    Returns ``{tp, fa, fd, fp, tn, fn}`` where ``fp = fa + fd``. Callers layer
    their own derived metrics (precision/recall/F1/accuracy/FAR/FDR) on top —
    the derivation matches Stickler's ``DerivedMetricsCalculator`` semantics
    and lives in the caller so this module stays a pure schema/contract.
    """
    counts = {"tp": 0, "fa": 0, "fd": 0, "tn": 0, "fn": 0}
    for fc in rows:
        counts[classify_field_comparison(fc)] += 1
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
