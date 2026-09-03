# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Persistence for confidence→accuracy curves.

Curves are read and written from three places that live in different deploy
artifacts — the test-set resolver (to serve an estimate), the review Lambda (to
fold in a human's verdict), and the test-run aggregation Lambda (to fold in a
scoring run) — so the storage layout and merge semantics are centralized here.

Curves are keyed by (test set, config version), because confidence means
different things across models and prompts and a curve measured under one
configuration must not be reused after a config change shifts those semantics. An
unknown config version falls back to the test set's aggregate curve, then to the
global prior, so a cold start still gets an answer.

Merging is additive: the curve is a bin-count table, so folding in observations is
``+=`` per bin, expressible as DynamoDB ``ADD`` on individual bin counters and
therefore safe under concurrent updates from several reviewers. The tradeoff is
that an observation cannot be un-folded; a curve is rebuilt from scratch
(``reset``) rather than corrected in place.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .confidence_curve import BIN_COUNT, ConfidenceCurve

logger = logging.getLogger(__name__)

# Sort-key prefix for curve items under a test set's partition, so curves are
# listable per set and are deleted along with it.
CURVE_SK_PREFIX = "curve#"

# Partition holding the cross-set prior. Keyed the same way so the same code
# path reads it.
GLOBAL_PRIOR_PK = "confidencecurve#global"

# Sentinel for "no config version recorded". Avoids a sort key of "curve#None"
# and keeps the aggregate curve for a set addressable.
AGGREGATE_KEY = "_aggregate"


def curve_sk(config_version: Optional[str]) -> str:
    return f"{CURVE_SK_PREFIX}{config_version or AGGREGATE_KEY}"


def test_set_pk(test_set_id: str) -> str:
    return f"testset#{test_set_id}"


class CurveStore:
    """Reads and writes confidence curves on the tracking table.

    Takes a boto3 DynamoDB ``Table`` rather than creating one, so callers that
    already hold a table resource don't end up with a second client.
    """

    def __init__(self, table: Any):
        self._table = table

    # -- reads -----------------------------------------------------------

    def get_curve(
        self, test_set_id: str, config_version: Optional[str] = None
    ) -> ConfidenceCurve:
        """Return the curve for a set+config, or an empty curve if none exists.

        Never raises for a missing curve: a set that has never been reviewed or
        scored legitimately has no curve, and the estimator handles that by
        reporting a prior-driven estimate.
        """
        item = self._get_item(test_set_pk(test_set_id), curve_sk(config_version))
        if not item and config_version:
            # The set's aggregate curve beats the global prior: it is at least
            # measured on this set's documents.
            item = self._get_item(test_set_pk(test_set_id), curve_sk(None))
        curve = ConfidenceCurve.from_dict(_item_to_curve_dict(item))
        curve.test_set_id = test_set_id
        curve.config_version = config_version
        return curve

    def get_global_prior(self) -> ConfidenceCurve:
        """The cross-set prior, used before a set has observations of its own."""
        item = self._get_item(GLOBAL_PRIOR_PK, curve_sk(None))
        return ConfidenceCurve.from_dict(_item_to_curve_dict(item))

    def list_curves(self, test_set_id: str) -> List[Dict[str, Any]]:
        """All curves recorded for a test set, one per config version."""
        from boto3.dynamodb.conditions import Key

        items: List[Dict[str, Any]] = []
        kwargs = {
            "KeyConditionExpression": Key("PK").eq(test_set_pk(test_set_id))
            & Key("SK").begins_with(CURVE_SK_PREFIX)
        }
        while True:
            response = self._table.query(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        curves = []
        for item in items:
            sk = item.get("SK", "")
            version = sk[len(CURVE_SK_PREFIX) :] or AGGREGATE_KEY
            curves.append(
                {
                    "configVersion": None if version == AGGREGATE_KEY else version,
                    "curve": ConfidenceCurve.from_dict(_item_to_curve_dict(item)),
                }
            )
        return curves

    # -- writes ----------------------------------------------------------

    def add_observations(
        self,
        test_set_id: str,
        observations: Sequence[Tuple[float, bool]],
        config_version: Optional[str] = None,
        source: str = "review",
    ) -> int:
        """Fold ``(confidence, correct)`` pairs into the stored curve(s).

        Writes the config-specific curve *and* the set's aggregate curve, so an
        estimate is available whether or not the caller knows which config
        produced a document's labels.

        Returns the number of observations accepted (out-of-range values are
        dropped by ``ConfidenceCurve``).
        """
        staged = ConfidenceCurve()
        accepted = staged.add_observations(observations, source=source)
        if not accepted:
            return 0

        self._merge(test_set_pk(test_set_id), config_version, staged, source)
        if config_version:
            self._merge(test_set_pk(test_set_id), None, staged, source)
        # Feed the global prior too, so a brand-new test set inherits what past
        # sets measured instead of starting from nothing.
        self._merge(GLOBAL_PRIOR_PK, None, staged, source)
        return accepted

    def add_ece_bins(
        self,
        test_set_id: str,
        bins: Sequence[Dict[str, Any]],
        config_version: Optional[str] = None,
    ) -> int:
        """Fold a scoring run's Stickler ECE bins into the stored curve(s).

        This is the highest-fidelity source: a scoring run measures the whole
        confidence range, including the high-confidence zone worst-first review
        never reaches.
        """
        staged = ConfidenceCurve()
        accepted = staged.add_ece_bins(bins)
        if not accepted:
            return 0

        self._merge(test_set_pk(test_set_id), config_version, staged, "scoring")
        if config_version:
            self._merge(test_set_pk(test_set_id), None, staged, "scoring")
        self._merge(GLOBAL_PRIOR_PK, None, staged, "scoring")
        return accepted

    def reset(self, test_set_id: str, config_version: Optional[str] = None) -> None:
        """Discard a stored curve.

        Observations are additive and cannot be individually un-folded, so
        discarding and rebuilding is the only correction path when ground truth
        turns out to have been wrong.

        Known limitation: this clears one set's curve, not the contribution that
        set already made to the global prior. Bad observations therefore keep a
        small residual influence on other sets — bounded, because the prior is only
        a fallback for bins a set has not measured itself, and it shrinks as other
        sets contribute. Removing it exactly would mean storing per-set
        contributions to the prior, which is not worth the write amplification;
        rebuild the prior from scratch if it is ever badly skewed.
        """
        self._table.delete_item(
            Key={
                "PK": test_set_pk(test_set_id),
                "SK": curve_sk(config_version),
            }
        )

    # -- internals -------------------------------------------------------

    def _get_item(self, pk: str, sk: str) -> Optional[Dict[str, Any]]:
        try:
            return self._table.get_item(Key={"PK": pk, "SK": sk}).get("Item")
        except Exception as e:  # noqa: BLE001 — a missing curve must not break a read
            logger.warning("Could not read confidence curve %s/%s: %s", pk, sk, e)
            return None

    def _merge(
        self,
        pk: str,
        config_version: Optional[str],
        staged: ConfidenceCurve,
        source: str,
    ) -> None:
        """Add a staged curve's counts into the stored item atomically.

        Uses per-bin ``ADD`` rather than read-modify-write, which would silently
        drop all but the last writer when several reviewers finish at once.
        """
        set_parts = ["ItemType = :type", "updatedAt = :now"]
        add_parts = []
        values: Dict[str, Any] = {
            ":type": "confidence_curve",
            ":now": _utc_now(),
        }

        from decimal import Decimal

        for index in range(BIN_COUNT):
            if staged.total[index] <= 0:
                continue
            add_parts.append(f"correct{index} :c{index}")
            add_parts.append(f"total{index} :t{index}")
            # DynamoDB rejects float. Counts are small and only fractional via
            # ECE-bin ingestion, so a rounded Decimal is exact enough.
            values[f":c{index}"] = Decimal(str(round(staged.correct[index], 4)))
            values[f":t{index}"] = Decimal(str(round(staged.total[index], 4)))

        if not add_parts:
            return

        counter = "scoringObservations" if source == "scoring" else "reviewObservations"
        observed = int(
            staged.scoring_observations
            if source == "scoring"
            else staged.review_observations
        )
        add_parts.append(f"{counter} :obs")
        values[":obs"] = observed

        expression = f"SET {', '.join(set_parts)} ADD {', '.join(add_parts)}"
        self._table.update_item(
            Key={"PK": pk, "SK": curve_sk(config_version)},
            UpdateExpression=expression,
            ExpressionAttributeValues=values,
        )


def _item_to_curve_dict(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Convert a stored DynamoDB curve item into ``ConfidenceCurve`` input.

    Bins are stored as flat ``correct<N>`` / ``total<N>`` attributes rather than
    a nested list so each can be the target of an atomic ``ADD``.
    """
    if not item:
        return None
    return {
        "correct": [float(item.get(f"correct{i}", 0) or 0) for i in range(BIN_COUNT)],
        "total": [float(item.get(f"total{i}", 0) or 0) for i in range(BIN_COUNT)],
        "reviewObservations": int(item.get("reviewObservations", 0) or 0),
        "scoringObservations": int(item.get("scoringObservations", 0) or 0),
    }


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def observations_from_comparison_results(
    comparison_results: Sequence[Dict[str, Any]],
) -> List[Tuple[float, bool]]:
    """Extract ``(confidence, correct)`` pairs from Stickler comparison results.

    Each field in a scored document contributes one observation: the confidence
    the extractor claimed, and whether the field actually matched ground truth.
    Fields with no confidence are skipped — they carry no information about
    calibration.
    """
    observations: List[Tuple[float, bool]] = []
    for result in comparison_results or []:
        fields = (result or {}).get("fields") or {}
        # ``fields`` may be a dict keyed by field path or a list of field dicts,
        # depending on which Stickler flags produced the blob.
        entries = fields.values() if isinstance(fields, dict) else fields
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            confidence = entry.get("confidence")
            if confidence is None:
                continue
            matched = entry.get("is_match")
            if matched is None:
                matched = entry.get("matched")
            if matched is None:
                continue
            observations.append((confidence, bool(matched)))
    return observations


def observations_from_baseline_review(
    before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]
) -> List[Tuple[float, bool]]:
    """Derive curve observations from a human's review of a drafted label.

    A reviewer implicitly labels each field correct or incorrect: a field they left
    alone was right, a field they changed was wrong. Paired with the confidence the
    model claimed, that is the ``(confidence, correct)`` observation the curve
    needs. Because review is worst-first, these land in the low-confidence bins.

    Only fields present in the drafted result with a recorded confidence
    contribute; a field the reviewer added had no prediction to be right or wrong
    about.

    Known limitation: list members are keyed by position, so inserting or deleting
    a row shifts every path after it and those fields compare against the wrong
    neighbour — a reviewer who deletes one spurious transaction can look like they
    corrected the whole remaining table. It costs accuracy in the pessimistic
    direction only (over-counting errors, never under-counting), and a
    position-independent key would need a stable per-row identity the extraction
    result does not carry.
    """
    if not before or not after:
        return []

    explainability = before.get("explainability_info")
    confidences = _flatten_confidences(explainability)
    if not confidences:
        return []

    before_values = _flatten_values(before.get("inference_result") or {})
    after_values = _flatten_values(after.get("inference_result") or {})

    observations: List[Tuple[float, bool]] = []
    for path, confidence in confidences.items():
        if path not in before_values:
            continue
        # Unchanged ⇒ the model was right; changed ⇒ it was wrong.
        correct = after_values.get(path, _MISSING) == before_values[path]
        observations.append((confidence, correct))
    return observations


_MISSING = object()


def _flatten_confidences(node: Any, prefix: str = "") -> Dict[str, float]:
    """Map field path → confidence from an ``explainability_info`` payload."""
    found: Dict[str, float] = {}
    if isinstance(node, dict):
        value = node.get("confidence")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and prefix:
            found[prefix] = float(value)
        for key, child in node.items():
            if key in ("confidence", "confidence_threshold", "geometry"):
                continue
            path = f"{prefix}.{key}" if prefix else key
            found.update(_flatten_confidences(child, path))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            found.update(_flatten_confidences(child, _list_child_path(prefix, index)))
    return found


def _list_child_path(prefix: str, index: int) -> str:
    """Path for element ``index`` of a list found at ``prefix``.

    The un-indexed case is ONLY the outermost list: ``explainability_info``
    arrives wrapped in a single-element list, which must not add a path level.

    This used to be ``prefix if len(node) == 1 else f"{prefix}[{index}]"`` — keyed
    on list LENGTH rather than on depth, so any single-element list lost its
    index. That made the key depend on the data: a one-row table produced
    ``Transactions.date`` while a two-row table produced ``Transactions[0].date``,
    and confidence-curve keys therefore did not join across documents. Multi-
    instance sections (#715) make it acute — every field of a one-instance section
    keys as ``instances.Field`` and of a two-instance section as
    ``instances[0].Field``.

    ⚠ Consequence: keys for single-element lists change shape (``F.x`` ->
    ``F[0].x``). Curve points already stored under the old key for a
    single-element list will not join with new ones; they were already failing to
    join with the multi-element form, so this makes one consistent shape out of
    two inconsistent ones rather than breaking a working join.
    """
    if not prefix:
        return prefix
    return f"{prefix}[{index}]"


def _flatten_values(node: Any, prefix: str = "") -> Dict[str, Any]:
    """Map field path → scalar value, matching ``_flatten_confidences`` paths."""
    found: Dict[str, Any] = {}
    if isinstance(node, dict):
        for key, child in node.items():
            path = f"{prefix}.{key}" if prefix else key
            found.update(_flatten_values(child, path))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            found.update(_flatten_values(child, _list_child_path(prefix, index)))
    elif prefix:
        found[prefix] = node
    return found
