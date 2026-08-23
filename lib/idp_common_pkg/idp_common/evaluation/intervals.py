# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Confidence intervals for measured accuracy.

A per-field accuracy is a proportion measured on a sample, and a proportion measured
on 3 observations means something very different from one measured on 300. Reporting
the point estimate alone lets a badly-broken field hide inside a healthy-looking
overall score: a run's *overall* accuracy firms up within roughly the first hundred
documents because every document contributes to it, while a field appearing once per
document gains one observation per document.

See ``docs/proposals/per-field-confidence-intervals.md`` for what these intervals do
and do not account for.
"""

from typing import NamedTuple, Optional

# Two-sided 95% normal quantile. The only interval level the UI reports, so it is a
# constant rather than a parameter on every call site.
Z_95 = 1.959963984540054


class AccuracyInterval(NamedTuple):
    """A measured proportion with its sampling uncertainty.

    ``margin`` is the half-width in proportion units (0.059 == 5.9 percentage points).
    It is derived from the bounds rather than being the interval's own half-width,
    because a Wilson interval is asymmetric near 0 and 1 — so ``point ± margin`` is an
    approximation and ``(low, high)`` is authoritative.
    """

    point: float
    low: float
    high: float
    margin: float
    observations: int


def wilson_interval(successes: int, total: int, z: float = Z_95) -> tuple:
    """Wilson score interval for a binomial proportion.

    Preferred over the textbook normal approximation, which is wrong in exactly the
    cases this reporting exists to surface. Per-field results routinely sit at p=0 or
    p=1 on few observations, where the normal interval leaves [0, 1] entirely: at
    n=20 and p=0.90 it puts the upper bound at 103%. An interval that can print an
    impossible accuracy is worse than no interval, because the reader discounts the
    number instead of the sample size.

    Returns ``(low, high)`` clamped to [0, 1]. A non-positive ``total`` yields the
    uninformative (0.0, 1.0) rather than raising, since "no observations" is a normal
    state for a field no document happened to contain.
    """
    if total <= 0:
        return (0.0, 1.0)

    p = successes / total
    denominator = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denominator
    half_width = (
        z * ((p * (1 - p) / total + z**2 / (4 * total**2)) ** 0.5) / denominator
    )
    return (max(0.0, centre - half_width), min(1.0, centre + half_width))


def accuracy_interval(
    correct: int, total: int, z: float = Z_95
) -> Optional[AccuracyInterval]:
    """Bundle a measured accuracy with its interval, or None if nothing was measured.

    None rather than a zero-filled record: a field with no observations has no
    accuracy, and rendering it as 0% would read as "always wrong".
    """
    if total <= 0:
        return None

    low, high = wilson_interval(correct, total, z)
    point = correct / total
    return AccuracyInterval(
        point=point,
        low=low,
        high=high,
        # Half the interval width, which is what "±" means to a reader. Not
        # centre-based: see AccuracyInterval.margin.
        margin=(high - low) / 2.0,
        observations=total,
    )


def accuracy_interval_from_confusion_matrix(
    metrics: dict, z: float = Z_95
) -> Optional[AccuracyInterval]:
    """Derive the interval from a field's confusion-matrix counts.

    Matches how accuracy itself is derived — ``(tp + tn) / (tp + fp + tn + fn)`` — so
    the interval always describes the number displayed beside it, and no new
    measurement or storage is needed.
    """
    if not isinstance(metrics, dict):
        return None

    def count(key: str) -> int:
        value = metrics.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return int(value)

    tp, fp, tn, fn = (count(k) for k in ("tp", "fp", "tn", "fn"))
    return accuracy_interval(tp + tn, tp + fp + tn + fn, z)
