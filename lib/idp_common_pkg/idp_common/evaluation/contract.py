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


def _is_match_true(value: Any) -> bool:
    """Narrow allowlist for a ``field_comparisons`` row's ``match`` field.

    Accepts ``True``, numeric ``1`` / ``1.0``, and the case-insensitive
    string ``"true"``. Rejects everything else. Deliberately narrow to
    avoid the raw-``bool()`` false-positive on the string ``"false"``
    (any non-empty string is truthy under ``bool()``); a Stickler
    variant emitting the string ``"false"`` for a rejected row would
    otherwise flip to matched=True.

    Shared by ``classify_field_comparison`` here AND the per-attribute
    verdict in ``stickler_backend/results.py`` so section-level counts
    and the parent verdict agree on the SAME row.
    """
    # Python bool. Guarded BEFORE the int/float branch because ``bool``
    # is a subclass of int in Python (``True == 1``) and would otherwise
    # slip through the numeric check.
    if isinstance(value, bool):
        return bool(value)
    # numpy scalar types (``bool_``, ``int64``, ``int32``, ``float64``…)
    # are NOT subclasses of Python's builtin ``bool``/``int``/``float``
    # (verified live on current numpy). Detect via class name so a
    # numpy-emitted ``1`` / ``bool_(True)`` matches without needing to
    # import numpy at module load time. ``__bool__`` conversion handles
    # both cases: numpy bool → its boolean, numeric ``1`` → True,
    # ``0`` → False, other numerics fall through.
    value_type = type(value)
    class_name = value_type.__name__
    # Require the numpy MODULE so an arbitrary user class named
    # ``IntBox`` doesn't slip through the numeric-name check.
    is_numpy_scalar = value_type.__module__ == "numpy"
    # numpy renamed ``np.bool_.__name__`` from ``"bool_"`` to ``"bool"``
    # between 1.x and 2.x. Accept both so this check works across the
    # relaxed ``numpy>=1.26,<3`` pin in the evaluation extra.
    if is_numpy_scalar and class_name in ("bool_", "bool"):
        return bool(value)
    if is_numpy_scalar and class_name.startswith(("int", "uint", "float")):
        try:
            if int(value) == 1 and float(value) == 1.0:
                return True
        except (TypeError, ValueError):
            pass
    if isinstance(value, (int, float)) and value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() == "true":
        return True
    return False


def _is_empty_value(v: Any) -> bool:
    """Semantic "empty" for a field's expected/actual value in a
    ``field_comparisons`` row.

    Stickler emits `None` when the value is absent and `""`/`[]`/`{}` when the
    value is present-but-empty. All count as "no value" for the purposes
    of tn (correctly-empty) / fa (hallucinated) / fn (missed) classification —
    a correctly-empty list is a `tn`, not a `tp`.

    Whitespace-only strings (``"   "``, ``"\\n"``, ``"\\t\\t"``) also count
    as empty — Stickler's ``NullHelper`` strips them before deciding
    null-ness, so the classifier here must too or the row-derived
    counts drift from ``cm.aggregate``.

    Coverage mirrors ``_is_structured``: every shape that ``_is_structured``
    admits as a "container" must have an emptiness check here, or the
    classifier and row-weighting diverge on that shape (finding B4 from
    #625 adversarial self-review — a plain ``class Empty: pass`` instance
    was structured-but-not-empty). For arbitrary objects, "empty" means:
    a ``model_dump()`` that returns a falsy dict, or a ``__dict__`` with
    no non-underscore keys.
    """
    if v is None:
        return True
    # Whitespace-only strings match Stickler's ``NullHelper`` behavior.
    if isinstance(v, str) and not v.strip():
        return True
    if isinstance(v, (list, dict, tuple, set, frozenset)) and len(v) == 0:
        return True
    if not isinstance(v, (str, int, float, bool)):
        # Pydantic model → check its serialized shape.
        if hasattr(v, "model_dump"):
            try:
                dumped = v.model_dump()
                return isinstance(dumped, dict) and not dumped
            except Exception:  # noqa: BLE001
                return False
        # Arbitrary class with a public ``__dict__``.
        if hasattr(v, "__dict__"):
            try:
                public = {k: val for k, val in vars(v).items() if not k.startswith("_")}
                return not public
            except Exception:  # noqa: BLE001
                return False
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
    _collect_leaf_paths_tagged(value, prefix, result, None)


def _collect_leaf_paths_tagged(
    value: Any,
    prefix: str,
    result: List[str],
    placeholders: "set[str] | None",
) -> None:
    """Worker for ``leaf_paths`` / ``leaf_paths_tagged``.

    When ``placeholders`` is provided, paths emitted because the value at
    that position is an EMPTY container (rather than a real scalar leaf)
    are added to the set — the caller can then distinguish "real leaf at
    this path" from "empty-container placeholder" for shadow filtering.
    Without this tagging, a shadow filter based only on path structure
    can't tell a legitimate scalar leaf ``"items": "value"`` apart from
    an empty placeholder ``"items": []`` and drops both when a strict
    descendant exists on the other side of the row.
    """
    if isinstance(value, dict):
        if not value:
            if prefix:
                result.append(prefix)
                if placeholders is not None:
                    placeholders.add(prefix)
            return
        for k, v in value.items():
            child_prefix = f"{prefix}.{k}" if prefix else k
            _collect_leaf_paths_tagged(v, child_prefix, result, placeholders)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        # Include frozenset so leaf enumeration matches ``_is_structured`` /
        # ``_is_empty_value`` (all three now agree on the same container
        # set; a divergence would let a frozenset-valued row weigh
        # differently than it classifies).
        if not value:
            if prefix:
                result.append(prefix)
                if placeholders is not None:
                    placeholders.add(prefix)
            return
        for elem in value:
            _collect_leaf_paths_tagged(elem, prefix, result, placeholders)
        return
    if hasattr(value, "model_dump"):
        try:
            _collect_leaf_paths_tagged(value.model_dump(), prefix, result, placeholders)
            return
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__") and not isinstance(value, (str, int, float, bool)):
        try:
            _collect_leaf_paths_tagged(
                {k: v for k, v in vars(value).items() if not k.startswith("_")},
                prefix,
                result,
                placeholders,
            )
            return
        except Exception:  # noqa: BLE001
            pass
    # Scalar, None, or unknown scalar-like — this position IS the leaf slot.
    # NOT a placeholder — a real value lives here.
    if prefix:
        result.append(prefix)


def leaf_paths_tagged(value: Any, prefix: str = "") -> tuple[List[str], "set[str]"]:
    """Return ``(paths, placeholders)`` where ``placeholders`` is the subset
    of ``paths`` that were emitted because the value at that position was
    an EMPTY container (rather than a real scalar leaf). Used by
    ``_row_leaves`` to shadow-filter empty-container placeholders without
    also dropping legitimate scalar leaves at the same path.
    """
    result: List[str] = []
    placeholders: set[str] = set()
    _collect_leaf_paths_tagged(value, prefix, result, placeholders)
    return result, placeholders


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
    dicts weighs 5, not 1. Cross-side overlap uses the elementwise max of
    a ``Counter`` per path so a partially-matched list-of-items contributes
    the ``max(count_exp, count_act)`` per key rather than double-counting.

    Empty-container placeholder filtering:
    ``leaf_paths`` emits an empty container's prefix as a placeholder slot
    (so ``_count_leaves`` can floor an all-empty value at one slot). When
    a row's OTHER side populates a strict descendant of that prefix, the
    placeholder is shadowed and would otherwise cause
    ``_synthesize_parent_buckets`` to fire its cross-schema collision
    warning spuriously — a row like ``exp={"items": [], "name": "A"}``
    against ``act={"items": [{"x": 1}], "name": "B"}`` produces
    ``{items, name, items.x}`` and treats ``items`` as a scalar-vs-
    structured collision. Drop the shadowed placeholder so the row
    spreads only to real terminal leaves.

    Mixed dotted-vs-scalar sides:
    A row like ``exp={"name": "A"}`` (one dotted leaf) vs
    ``act=["x", "y", "z"]`` (three positional scalars) has one dotted
    leaf ("name") and three positional leaves with no attribute
    attribution. If we returned only ``["name"]``, per-field spread
    would attribute one bucket and miss the three hallucinated actual
    leaves; if we counted only positional slots, the "name"
    attribution would be lost. Emit positional slots as synthetic
    entries at the magic path ``POSITIONAL_LEAF_NAME`` (currently
    ``"__positional__"``). The aggregation Lambda spreads those to
    ``<collapsed>.__positional__`` — a dedicated sub-bucket that
    can't collide with any real schema attribute name and doesn't trip
    the parent-bucket collision check in
    ``_synthesize_parent_buckets``. ``_row_weight`` includes them in
    the total so top-level and per-field still agree.

    Returns [] when neither side has any leaves at all — the caller
    (``_row_weight``) applies its scalar fallback, and the aggregation
    spread falls back to a single ``_add(collapsed, bucket, weight)``.

    Consolidated helper so top-level counts (via ``_row_weight``) and
    per-field spread enumerate the SAME slots — divergence reintroduces
    the class of inconsistency #625 was originally fixing.
    """
    exp = fc.get("expected_value")
    act = fc.get("actual_value")
    if _is_structured(exp):
        exp_paths, exp_placeholders = leaf_paths_tagged(exp)
    else:
        exp_paths, exp_placeholders = [], set()
    if _is_structured(act):
        act_paths, act_placeholders = leaf_paths_tagged(act)
    else:
        act_paths, act_placeholders = [], set()
    # NOTE: don't early-return here when both are empty — pure-scalar-list
    # rows (``exp=["a", "b"]`` vs ``act=[]``) have no dotted paths but DO
    # have positional slots. If we returned [], per-field spread falls
    # through to ``_add(collapsed, bucket, weight)`` which lands the row's
    # weight in the collapsed root bucket. Mixed dotted+positional rows
    # land POSITIONAL slots under ``<collapsed>.__positional__`` via the
    # sentinel code below. That splits the SAME conceptual positional
    # attribution across two per-field buckets across a run — pure-list
    # rows in ``<collapsed>``, mixed-shape rows in
    # ``<collapsed>.__positional__``. Consistent handling: always route
    # positional slots through the sentinel path (finding from #625
    # review — bucket split across shape combinations).
    exp_bag: Counter = Counter(exp_paths)
    act_bag: Counter = Counter(act_paths)
    union: Counter = exp_bag | act_bag
    # Filter placeholder paths shadowed by a strict descendant in the same
    # row. Only shadow paths that came from an empty-container placeholder
    # on the side where the path was emitted (or on BOTH sides). A path
    # that was a REAL scalar leaf on either side is preserved — otherwise
    # a row like ``exp={"items": "value", "name": "A"}`` vs
    # ``act={"items": {"x": 1}, "name": "B"}`` would drop the exp scalar
    # at ``items`` when act has ``items.x``.
    all_paths = list(union.keys())
    shadowed: set = set()
    for candidate in all_paths:
        prefix_check = candidate + "."
        has_descendant = any(
            other != candidate and other.startswith(prefix_check) for other in all_paths
        )
        if not has_descendant:
            continue
        # Placeholder on the side that emitted the path — i.e., not a real
        # scalar leaf. A path emitted by both sides is a placeholder only
        # if both sides emitted it as one; when either side had a real
        # value there, keep it.
        exp_has = candidate in exp_bag
        act_has = candidate in act_bag
        exp_only_placeholder = not exp_has or candidate in exp_placeholders
        act_only_placeholder = not act_has or candidate in act_placeholders
        if exp_only_placeholder and act_only_placeholder:
            shadowed.add(candidate)
    if shadowed:
        for p in shadowed:
            del union[p]
    result = list(union.elements())
    # Mixed dotted-vs-positional: if one side has positional slots
    # (list-like of BARE SCALARS or EMPTY CONTAINERS — both contribute
    # no dotted leaf paths but are real comparison slots) and the
    # other side has dotted leaves, add sentinel entries so both
    # sides' slots are counted. Non-empty containers/models are NOT
    # counted here — they reach the confusion matrix via their own
    # dotted ``leaf_paths`` above (avoiding double-count).
    positional = max(
        _scalar_positional_count(exp),
        _scalar_positional_count(act),
    )
    if positional > 0:
        # Use a sub-name (not the empty string) so per-field spread adds
        # to ``collapsed.__positional__``, not to ``collapsed`` directly.
        # Adding to ``collapsed`` would look like a leaf bucket at the
        # parent path and trip ``_synthesize_parent_buckets``' cross-
        # schema collision check when other leaves at ``collapsed.<attr>``
        # also exist — spurious warning + skipped synthesis.
        result.extend([POSITIONAL_LEAF_NAME] * positional)
    return result


# Magic leaf name for positional scalar slots on a mixed dotted+positional
# row. Kept namespaced (double-underscore both ends) so it can never
# collide with a real schema attribute name; the aggregation Lambda's
# per-field bucket for this appears under ``<parent>.__positional__``.
POSITIONAL_LEAF_NAME = "__positional__"


def _scalar_positional_count(v: Any) -> int:
    """Count of positional slots in a list-like value that contribute
    NO dotted leaf paths.

    Returns 0 for anything that isn't a list/tuple/set/frozenset. For
    list-likes, counts elements that either:

    * Are bare scalars (``None`` / str / int / float / bool) — they
      have no dotted paths, so are only visible to the confusion
      matrix as positional slots.
    * Are EMPTY containers (``{}`` / ``[]`` / ``()`` / ``set()`` /
      ``frozenset()``) — same story: they're a "slot" for the
      confusion matrix but have no attribute name to attribute to.
      Without this branch, ``[{}]*3`` had weight 0 via
      ``_row_leaves`` (empty dicts don't emit at prefix="") but
      ``_row_weight`` would try the ``_count_leaves`` fallback that
      counted them 3× — a documented asymmetry the reviewer flagged
      as softening the recall hit on empty-dict list items.

    Elements that are NON-EMPTY containers or models are NOT counted
    here — they reach the confusion matrix via their own dotted
    ``leaf_paths`` in ``_row_leaves``.
    """
    if not isinstance(v, (list, tuple, set, frozenset)):
        return 0
    count = 0
    for elem in v:
        if elem is None or isinstance(elem, (str, int, float, bool)):
            count += 1
        elif isinstance(elem, (list, dict, tuple, set, frozenset)) and len(elem) == 0:
            count += 1
    return count


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
    # Narrow allowlist — accept bool True, numeric 1, or the case-
    # insensitive string ``"true"``. Rejects everything else, INCLUDING
    # the string ``"false"`` which is truthy under plain ``bool()`` and
    # would flip a rejected row to matched=True. The per-attribute
    # verdict in ``stickler_backend/results.py`` uses the same
    # predicate via ``_is_match_true`` so section counts and per-
    # attribute verdict agree on the SAME row — asymmetric truthiness
    # would reintroduce the exact parent-vs-section drift #625 exists
    # to eliminate.
    matched = _is_match_true(fc.get("match"))
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
    # Coerce to str — a Stickler variant emitting a non-string ``expected_key``
    # (e.g. a list index or ``None``) would otherwise crash the whole doc's
    # evaluation on the ``.find()`` call. Every sibling helper is defensive
    # against unexpected shapes; this one should be too.
    raw = fc.get("expected_key") or fc.get("actual_key") or fc.get("field_path") or ""
    path = raw if isinstance(raw, str) else str(raw)
    # NOTE on multi-instance (GitHub #715): for a wrapped class every row's path is
    # `instances[i].Field`, so every leaf groups under the single root `instances`
    # and the per-attribute report is one giant attribute rather than one per
    # field. That is a real granularity loss — but it must NOT be "fixed" here by
    # stepping past the synthesized root.
    #
    # Measured live: doing that made `row_root_attribute` return `CheckNumber`,
    # which matches no attribute at all, because the attribute list is built from
    # the class SCHEMA — and a wrapped class has exactly one property, `instances`.
    # All 24 of Stickler's field_comparisons rows were therefore dropped from
    # `field_comparison_details`, emptying the report's per-field drilldown and the
    # UI's mismatch highlighting (which joins on `expected_key`). The section-level
    # metrics stayed correct, so the loss was invisible in the numbers — accuracy
    # still read 1.000 with an empty drilldown.
    #
    # Recovering per-field granularity for a wrapped class means changing how the
    # ATTRIBUTE LIST is constructed (unwrapping one level when the class is
    # flagged), not how rows are keyed. Until then, one drillable attribute
    # carrying every row beats N attributes carrying none.
    return _first_path_segment(path)


def _first_path_segment(path: str) -> str:
    """Everything before the first ``[`` or ``.`` in a field path."""
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
    # Track shape signatures we've already logged in THIS call, not just
    # a single "did we decide" boolean. A batch may contain rows of
    # multiple distinct anomalous shapes (e.g. some ``[3]`` bare-bracket
    # AND some ``.city`` leading-dot rows); each distinct shape should
    # log once per call (with the process-wide LRU still preventing
    # cross-call flood). The prior ``decision_made_this_call`` bool
    # silently dropped every shape after the first (finding from #625
    # review — defeated the per-shape dedup design).
    logged_shapes_this_call: set = set()
    for fc in rows:
        root = row_root_attribute(fc)
        if not root:
            # Dedup key is the SHAPE SIGNATURE of the anomaly, not
            # the caller-supplied context. Previous key ``context``
            # (per-doc + per-section on both call sites) meant a
            # run-wide Stickler shape drift emitted one warning per
            # (doc, section) pair — CloudWatch flood scaling with
            # the run size, defeating the stated dedup design
            # (finding from #625 review). The shape signature is
            # the leading punctuation of the anomalous path
            # (``[`` for bare-bracket rows, ``.`` for leading-dot
            # rows), which captures the DISTINCT Stickler emission
            # shape without depending on WHICH document surfaced
            # it. The caller's ``context`` still appears in the log
            # message so operators can locate a specific occurrence.
            anomalous_path = (
                fc.get("expected_key")
                or fc.get("actual_key")
                or fc.get("field_path")
                or ""
            )
            shape_sig = anomalous_path[:1] if anomalous_path else "empty"
            # Per-call dedup by shape_sig — a batch containing rows of
            # multiple distinct shapes each logs at most once.
            if shape_sig not in logged_shapes_this_call:
                logged_shapes_this_call.add(shape_sig)
                ctx = context or "unknown"
                should_log = False
                with _seen_anonymous_root_lock:
                    if shape_sig in _seen_anonymous_root_contexts:
                        _seen_anonymous_root_contexts.move_to_end(shape_sig)
                    else:
                        if (
                            len(_seen_anonymous_root_contexts)
                            >= _SEEN_ANONYMOUS_ROOT_MAX
                        ):
                            _seen_anonymous_root_contexts.popitem(last=False)
                        _seen_anonymous_root_contexts[shape_sig] = None
                        should_log = True
                if should_log:
                    logger.warning(
                        "Skipping field_comparisons row(s) with anonymous "
                        "root (first example path=%r, context=%r, "
                        "shape=%r) — cannot attribute to a parent "
                        "attribute. Further occurrences of the same shape "
                        "signature are not logged.",
                        anomalous_path,
                        ctx,
                        shape_sig,
                    )
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
