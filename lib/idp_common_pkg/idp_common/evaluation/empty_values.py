# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Confidence on empty predictions, kept visible next to the calibration metrics.

An *empty prediction* is a field the extractor returned no value for: ``None``, an
empty or whitespace-only string, or an empty container. The definition is
``contract._is_empty_value``, the same predicate that decides a field comparison's
``tn`` / ``fn`` verdict, so a field cannot be empty for the counts and non-empty for
the calibration figures.

Why this module exists (#2052). The evaluation service drops ``None`` values before
it builds the Stickler models, so Stickler's ``prediction_confidences`` never holds a
score for a field the extractor returned as null, and the run-level AUROC, ECE and
Brier score are computed without those fields. A confidence scorer that rates a
**missed** field higher than correctly found values is therefore invisible in those
three numbers: the measured case in #2033 read AUROC 0.68 with empty values left out
and 0.56 with them counted.

The existing figures keep their meaning, because stored reports, the API and the UI
already present them. Empty predictions are reported beside them instead:

* The writer (``EvaluationService``) records the confidence of each empty prediction
  that Stickler dropped under ``EMPTY_PREDICTION_CONFIDENCES_KEY`` in the section's
  ``stickler_comparison_result``.
* The reader (the test-execution aggregation Lambda) counts every empty prediction,
  how many of them were wrong, and how many carried a confidence, and computes the
  calibration metrics over the empty predictions alone and over all fields with the
  empty predictions included. That block lands at ``confidence_metrics.empty_values``.

The module uses only the standard library, so the benchmark harness can import the
same predicate without the ``[evaluation]`` extra.
"""

from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from idp_common.evaluation.contract import _is_empty_value, _is_match_true

# Key in a section's ``stickler_comparison_result`` holding ``{field path: confidence}``
# for empty predictions whose confidence Stickler did not record. Absent on results
# written before #2052, which the reader reports as "not measured" rather than zero.
EMPTY_PREDICTION_CONFIDENCES_KEY = "empty_prediction_confidences"


def is_empty_prediction(value: Any) -> bool:
    """True when a predicted value counts as empty. Public name for the shared rule."""
    return _is_empty_value(value)


def _empty_prediction_rows(
    field_comparisons: Optional[Iterable[Mapping[str, Any]]],
) -> List[Mapping[str, Any]]:
    """Rows whose prediction is an empty value at a joinable field path.

    A row without a string ``actual_key`` (a ground-truth list item the prediction has
    no counterpart for) is not an empty prediction of a field: there is no predicted
    field to carry a confidence, which is the same rule Stickler's confidence
    extraction applies.
    """
    rows = []
    for fc in field_comparisons or []:
        if not isinstance(fc, Mapping):
            continue
        if not isinstance(fc.get("actual_key"), str):
            continue
        if is_empty_prediction(fc.get("actual_value")):
            rows.append(fc)
    return rows


def empty_prediction_confidences(
    field_comparisons: Optional[Iterable[Mapping[str, Any]]],
    prediction_confidences: Optional[Mapping[str, Any]],
    confidence_for: Callable[[str], Optional[float]],
) -> Dict[str, float]:
    """Writer side: confidences of empty predictions Stickler did not record.

    ``confidence_for`` maps a field path to the assessment's confidence for it, or
    ``None`` when there is none. Paths already present in ``prediction_confidences``
    are skipped so that no field is scored twice.
    """
    recorded = prediction_confidences or {}
    found: Dict[str, float] = {}
    for fc in _empty_prediction_rows(field_comparisons):
        path = fc["actual_key"]
        if path in recorded or path in found:
            continue
        confidence = confidence_for(path)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        found[path] = float(confidence)
    return found


def empty_value_observations(
    comparison_result: Mapping[str, Any],
) -> Tuple[int, int, List[Tuple[str, float, bool, float]]]:
    """Reader side: ``(empty_count, empty_wrong, scored)`` for one section.

    ``scored`` holds ``(field path, confidence, is_match, similarity)``, the last
    being the row's comparator ``score`` as Stickler's own pairs carry it, for every empty
    prediction that carried a confidence, whether Stickler recorded it or the writer
    did. ``empty_count`` and ``empty_wrong`` count every empty prediction, with or
    without a confidence.
    """
    recorded = comparison_result.get("prediction_confidences") or {}
    added = comparison_result.get(EMPTY_PREDICTION_CONFIDENCES_KEY) or {}
    empty_count = 0
    empty_wrong = 0
    scored: List[Tuple[str, float, bool, float]] = []
    for fc in _empty_prediction_rows(comparison_result.get("field_comparisons")):
        is_match = _is_match_true(fc.get("match"))
        empty_count += 1
        if not is_match:
            empty_wrong += 1
        path = fc["actual_key"]
        confidence = recorded.get(path)
        if confidence is None:
            confidence = added.get(path)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        similarity = fc.get("score")
        if isinstance(similarity, bool) or not isinstance(similarity, (int, float)):
            similarity = 0.0
        scored.append((path, float(confidence), is_match, float(similarity)))
    return empty_count, empty_wrong, scored


def has_empty_prediction_record(comparison_result: Mapping[str, Any]) -> bool:
    """Whether the writer that produced this result recorded empty-value confidences.

    False for a result written before #2052, where a ``None`` prediction's
    confidence was never kept, so "no scored empty predictions" would be a claim the
    stored data cannot support.
    """
    return EMPTY_PREDICTION_CONFIDENCES_KEY in comparison_result
