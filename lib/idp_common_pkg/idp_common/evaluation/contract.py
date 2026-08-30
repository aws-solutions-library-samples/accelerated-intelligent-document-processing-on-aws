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
import threading
from collections import Counter, OrderedDict
from typing import Any, Dict, Iterable, List

logger = logging.getLogger(__name__)


def _is_empty_value(v: Any) -> bool:
    """Semantic "empty" for a field's expected/actual value in a
    ``field_comparisons`` row.

    Stickler emits `None` when the value is absent and `""`/`[]`/`{}` when the
    value is present-but-empty. All count as "no value" for the purposes
    of tn (correctly-empty) / fa (hallucinated) / fn (missed) classification —
    a correctly-empty list is a `tn`, not a `tp`.

    Includes tuple / set / frozenset so the emptiness check agrees with
    ``_is_structured`` (which treats those as containers). A divergence
    would split classifier semantics from ``_row_weight``'s enumeration
    (finding from #625 high review — a Stickler-emitted empty tuple was
    "structured" for weighting but "non-empty" for classification).
    """
    if v is None:
        return True
    if isinstance(v, (str, list, dict, tuple, set, frozenset)) and len(v) == 0:
        return True
    return False


def leaf_paths(value: Any, prefix: str = "") -> List[str]:
    """Return the list of leaf paths in a possibly-nested value.

    Semantics: one path per SCHEMA SLOT (dict key or scalar leaf position),
    NOT per non-None value. A ``{'name': 'A', 'amount': None}`` item has TWO
    leaf paths (``name``, ``amount``) — an Optional-typed schema field is
    still a slot, whether or not it happens to be null in this specific item.
    A ``[{'x': 1}, {'x': 2}]`` list has TWO leaf paths (both ``x``) — list
    indices are collapsed since per-field metrics bucket on collapsed paths.

    Consumers:
    * ``_count_leaves`` (via ``len(leaf_paths(...))``) for row weighting in
      ``aggregate_row_counts`` — the top-level side of the leaf-normalized
      counting invariant.
    * The aggregation Lambda's per-field bucketing —  the per-field side.

    Kept as ONE function so the two sides can't diverge (finding 1 from
    #625 adversarial review: unequal handling of None-valued keys and
    nested lists broke ``sum(per-field counts) == top-level counts``).

    Returns ``[]`` when the top-level value is None or a bare scalar with
    no attributable prefix — the caller applies the min-1 floor for row
    weighting.
    """
    result: List[str] = []
    _collect_leaf_paths(value, prefix, result)
    return result


def _collect_leaf_paths(value: Any, prefix: str, result: List[str]) -> None:
    """Recursive worker for ``leaf_paths``. See there for semantics."""
    if isinstance(value, dict):
        if not value:
            if prefix:
                result.append(prefix)
            return
        for k, v in value.items():
            child_prefix = f"{prefix}.{k}" if prefix else k
            _collect_leaf_paths(v, child_prefix, result)
        return
    if isinstance(value, (list, tuple, set)):
        if not value:
            if prefix:
                result.append(prefix)
            return
        for elem in value:
            _collect_leaf_paths(elem, prefix, result)
        return
    if hasattr(value, "model_dump"):
        try:
            _collect_leaf_paths(value.model_dump(), prefix, result)
            return
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__") and not isinstance(value, (str, int, float, bool)):
        try:
            _collect_leaf_paths(
                {k: v for k, v in vars(value).items() if not k.startswith("_")},
                prefix,
                result,
            )
            return
        except Exception:  # noqa: BLE001
            pass
    # Scalar, None, or unknown scalar-like — this position IS the leaf slot.
    if prefix:
        result.append(prefix)


def _count_leaves(value: Any) -> int:
    """Count leaf slots in a value. Wraps ``leaf_paths`` so top-level row
    weighting (``_row_weight``) and per-field metrics use the SAME enumeration.

    Diverging these counted None-valued keys and nested-list elements
    inconsistently before, so top-level and per-field metrics disagreed on
    the same input (#625 adversarial finding 1).
    """
    # Use a synthetic prefix so a bare scalar at top-level counts as 1 slot —
    # matches the original semantics used by _row_weight (min-1 floor).
    return len(leaf_paths(value, prefix="_"))


def _row_weight(fc: Dict[str, Any]) -> int:
    """Number of leaf comparisons a row represents.

    Weight equals ``len(_row_leaves(fc))`` when the row has dotted leaf
    paths (dict/list-of-dicts value shapes); both ``_row_weight`` and the
    per-field spread consume ``_row_leaves`` so
    ``sum(per-field counts) == top-level counts`` is a structural invariant.

    For a list of BARE SCALARS (``["a", "b", "c"]``) ``_row_leaves`` returns
    empty because the elements have no dotted paths — the per-field spread
    falls through to a single ``_add(collapsed, bucket, weight)`` and the
    weight must equal the positional element count so a truncated 5-item
    scalar list still weighs 5 leaf-normalized units. ``_count_leaves``
    (prefix="_") counts positional slots for that fallback.
    """
    leaves = _row_leaves(fc)
    if leaves:
        return len(leaves)
    # Fallback: neither side has dotted leaf paths. Preserve positional
    # element counting for bare-scalar lists via ``_count_leaves``. When
    # neither side is structured (both scalar or None), the max is 0 and
    # we return 1 for the single confusion-matrix event.
    exp = fc.get("expected_value")
    act = fc.get("actual_value")
    exp_count = _count_leaves(exp) if _is_structured(exp) else 0
    act_count = _count_leaves(act) if _is_structured(act) else 0
    scalar_max = max(exp_count, act_count)
    return scalar_max if scalar_max > 0 else 1


def _row_leaves(fc: Dict[str, Any]) -> List[str]:
    """Ordered list of leaf paths a row spreads over.

    Bag-semantic union of expected and actual leaf paths — repeated paths
    from list-of-items (where every item shares the same key shape)
    contribute one entry per item, so a 5-item list of ``{"name": ..}``
    dicts weighs 5, not 1. Cross-side overlap counts once (element-wise
    max of a Counter per path).

    Returns [] when neither side has enumerable leaf paths — the caller
    (``_row_weight``) applies the min-1 fallback, and the aggregation
    spread falls back to a single ``_add(collapsed, bucket, weight)``.

    Consolidated helper so top-level counts (via ``_row_weight``) and
    per-field spread in the aggregation Lambda enumerate the SAME slots
    — divergence between the two enumerations reintroduces the class of
    inconsistency issue #625 was originally fixing (finding from #625
    high review — a set-based union collapsed list-of-items duplicate
    paths and undercounted the row).
    """
    exp = fc.get("expected_value")
    act = fc.get("actual_value")
    exp_paths = leaf_paths(exp) if _is_structured(exp) else []
    act_paths = leaf_paths(act) if _is_structured(act) else []
    if not exp_paths and not act_paths:
        return []
    exp_bag: Counter = Counter(exp_paths)
    act_bag: Counter = Counter(act_paths)
    # Elementwise max: a path present on both sides with counts (3, 2)
    # contributes 3 (Hungarian pairing means 2 leaves match and 1 is
    # extra — max captures the total slots the row covers).
    union: Counter = exp_bag | act_bag
    return list(union.elements())


def _is_structured(value: Any) -> bool:
    """True iff ``value`` is a container / model — the shapes ``_count_leaves``
    can meaningfully enumerate slots inside. Bare scalars (str, int, bool,
    None) all count as one slot at their prefix and are handled by the
    ``return 1`` branch of ``_row_weight``.

    Includes frozenset so the "structured" check agrees with
    ``_is_empty_value``'s frozenset-aware emptiness check — a divergence
    would split classifier semantics from row-weighting on that shape
    (finding from #625 high review — the docstring of ``_is_empty_value``
    already claimed frozenset was in the container set).
    """
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return True
    if value is None or isinstance(value, (str, int, float, bool)):
        return False
    return hasattr(value, "model_dump") or hasattr(value, "__dict__")


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
    is everything before the first ``[`` or ``.``.

    Rows whose path begins with ``[`` or ``.`` (no leading attribute name,
    e.g. ``[3].name`` or ``.city``) return the empty string — the substring
    up to the first delimiter *is* empty. These "anonymous-root" rows are
    dropped by ``iter_countable_rows`` because they cannot be attributed to
    a parent attribute in the section, and counting them at the section
    level while excluding them from per-attribute buckets would break the
    "parent ✓ iff no red row" invariant.

    In practice current Stickler builds never emit anonymous-root rows;
    each ``field_comparisons`` row is anchored to a named schema field.
    The empty-return branch exists so a future Stickler change that
    introduces such a shape surfaces via the warning in
    ``iter_countable_rows`` instead of silently reintroducing parent-vs-
    section drift (finding 11 from #625 round-4 review — the previous
    docstring didn't explain what "cannot attribute" meant to a reader
    who wasn't in the review discussion).
    """
    path = fc.get("expected_key") or fc.get("actual_key") or fc.get("field_path") or ""
    idx_bracket = path.find("[")
    idx_dot = path.find(".")
    cuts = [i for i in (idx_bracket, idx_dot) if i >= 0]
    return path[: min(cuts)] if cuts else path


# Process-wide LRU cache for the anonymous-root warning. A test-run
# aggregation calls ``iter_countable_rows`` per document (per section on the
# per-doc path, plus twice more on the run-level path), so a Stickler shape
# change that emits anonymous-root rows would fire the same warning
# O(rows × sections) times without this — CloudWatch flood matching the
# version-drift warning we explicitly rate-limited.
#
# LRU rather than a plain set with a hard cap so a warm Lambda that
# processes many test runs keeps working: the container evicts the
# oldest contexts to make room for new ones, meaning a fresh Stickler
# shape change is still logged even after the container has already
# seen 256 distinct contexts (finding from #625 xhigh review — the
# previous set-with-cap silenced every subsequent context for the
# container's remaining lifetime once the cap was reached). Guarded by
# a lock — the aggregation Lambda's ``ThreadPoolExecutor`` calls
# ``iter_countable_rows`` from up to 20 workers concurrently, and while
# CPython's GIL makes individual dict ops atomic, the check-then-move-
# then-add sequence used here is three ops with a race window.
_SEEN_ANONYMOUS_ROOT_MAX = 256
_seen_anonymous_root_contexts: "OrderedDict[str, None]" = OrderedDict()
_seen_anonymous_root_lock = threading.Lock()


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
    Warning is rate-limited (once per ``context`` string per process) so an
    unexpected shape change doesn't flood CloudWatch on a run with many
    affected rows.
    """
    kept: List[Dict[str, Any]] = []
    # Rate-limit decision for this call: True once we've decided about the
    # first anonymous-root row in this batch (whether we logged or the LRU
    # said we already warned). Every subsequent anonymous-root row shares
    # the same ``context`` — one call, one context — so re-checking the LRU
    # for each row would spin the lock without changing the decision. The
    # log message reports "first example path=..." so an operator inspecting
    # the warning knows it represents the whole batch's anomaly.
    decision_made_this_call = False
    for fc in rows:
        root = row_root_attribute(fc)
        if not root:
            if not decision_made_this_call:
                ctx = context or "unknown"
                # Thread-safe check-then-record with LRU eviction. The
                # check and mutation are one critical section — separately
                # they'd race under the aggregation Lambda's 20-worker
                # executor. When the cache is full we evict the oldest
                # context to admit the new one, so a warm Lambda that has
                # seen 256 contexts still logs a fresh Stickler shape
                # change (rather than going silent for the rest of its
                # lifetime).
                should_log = False
                with _seen_anonymous_root_lock:
                    if ctx in _seen_anonymous_root_contexts:
                        _seen_anonymous_root_contexts.move_to_end(ctx)
                    else:
                        if (
                            len(_seen_anonymous_root_contexts)
                            >= _SEEN_ANONYMOUS_ROOT_MAX
                        ):
                            _seen_anonymous_root_contexts.popitem(last=False)
                        _seen_anonymous_root_contexts[ctx] = None
                        should_log = True
                if should_log:
                    logger.warning(
                        "Skipping field_comparisons row(s) with anonymous "
                        "root (first example path=%r, context=%r) — cannot "
                        "attribute to a parent attribute. Further "
                        "occurrences with this context are not logged.",
                        fc.get("expected_key")
                        or fc.get("actual_key")
                        or fc.get("field_path"),
                        ctx,
                    )
                decision_made_this_call = True
            continue
        kept.append(fc)
    return kept


def safe_div(num: float, den: float) -> float:
    """Zero-denominator convention: return 0.0.

    Used by both the per-doc path (``stickler_backend/results.py``) and the
    run-level aggregation Lambda so the same input produces the same shape
    on both dashboards — otherwise a run-level FAR of 0/0 rendering as
    ``None`` on one side and ``0.0`` on the other would show up in the UI
    as ``N/A`` vs ``0.000`` on the same document. Kept as one function so
    the two sides can't drift on this convention (finding 8 from #625
    round-4 review — previously duplicated in two files with comments in
    each citing the other as the source of truth).
    """
    return float(num) / float(den) if den > 0 else 0.0


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
