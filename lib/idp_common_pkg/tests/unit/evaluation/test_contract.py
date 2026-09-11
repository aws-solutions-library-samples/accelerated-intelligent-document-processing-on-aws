# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``idp_common.evaluation.contract`` helpers that are
shared across the per-doc evaluation service and the run-level
aggregation Lambda.
"""

import logging

import pytest

from idp_common.evaluation import contract


@pytest.mark.unit
class TestAnonymousRootDedup:
    """The anonymous-root warning inside ``iter_countable_rows`` uses a
    process-wide LRU dedupe so:
      1. Repeated occurrences of the same (unattributable) context in one
         run log once, not O(rows).
      2. A warm Lambda that accumulates >``_SEEN_ANONYMOUS_ROOT_MAX``
         distinct contexts across many test runs still logs a fresh
         Stickler shape change — the LRU evicts the oldest context to
         admit the new one, rather than going silent for the container's
         remaining lifetime (finding from #625 xhigh review).
    """

    # The evaluation-suite conftest.py resets the LRU between every test
    # (autouse fixture), so no explicit setup_method is needed here — the
    # LRU is guaranteed empty at the start of each test in this class.

    def test_same_context_logs_once(self, caplog):
        # A row with no root attribute (bare-bracket path) — Stickler
        # never actually emits this shape, so the branch fires only on
        # future shape drift.
        row = {"field_path": "[3]"}
        with caplog.at_level(logging.WARNING, logger="idp_common.evaluation.contract"):
            contract.iter_countable_rows([row], context="ctx-1")
            contract.iter_countable_rows([row], context="ctx-1")
            contract.iter_countable_rows([row], context="ctx-1")
        assert sum(1 for r in caplog.records if "anonymous root" in r.getMessage()) == 1

    def test_different_contexts_same_shape_log_once(self, caplog):
        """Dedup key is the SHAPE SIGNATURE (leading punctuation), not the
        caller-supplied context — two docs with the same anomalous
        shape share one warning, not one per doc/section. Prevents the
        CloudWatch flood a run-wide shape drift would cause under
        per-(doc, section) context dedup (finding from #625 review)."""
        row = {"field_path": "[3]"}
        with caplog.at_level(logging.WARNING, logger="idp_common.evaluation.contract"):
            contract.iter_countable_rows([row], context="ctx-A")
            contract.iter_countable_rows([row], context="ctx-B")
            contract.iter_countable_rows([row], context="ctx-A")
        assert sum(1 for r in caplog.records if "anonymous root" in r.getMessage()) == 1

    def test_different_shapes_each_log_once(self, caplog):
        """Different anomalous-shape signatures (leading ``[`` vs leading
        ``.``) log independently — each distinct shape drift is a
        distinct signal worth surfacing once."""
        with caplog.at_level(logging.WARNING, logger="idp_common.evaluation.contract"):
            contract.iter_countable_rows(
                [{"field_path": "[3]"}], context="ctx"
            )  # shape "["
            contract.iter_countable_rows(
                [{"field_path": ".city"}], context="ctx"
            )  # shape "."
            contract.iter_countable_rows(
                [{"field_path": "[7]"}], context="ctx"
            )  # shape "[" again
        assert sum(1 for r in caplog.records if "anonymous root" in r.getMessage()) == 2

    def test_lru_eviction_admits_new_shape_past_cap(self, monkeypatch, caplog):
        # Cap the LRU tightly so we can test eviction without generating
        # 256 records in the test log. Three distinct anonymous-root
        # shape signatures exist: leading ``[`` (bare-bracket rows),
        # leading ``.`` (leading-dot rows), and ``empty`` (empty path).
        monkeypatch.setattr(contract, "_SEEN_ANONYMOUS_ROOT_MAX", 2)
        with caplog.at_level(logging.WARNING, logger="idp_common.evaluation.contract"):
            contract.iter_countable_rows(
                [{"field_path": "[3]"}], context="c"
            )  # shape "["
            contract.iter_countable_rows(
                [{"field_path": ".x"}], context="c"
            )  # shape "."
            contract.iter_countable_rows(
                [{"field_path": ""}], context="c"
            )  # shape "empty" (evicts "[")
            # Shape "[" was evicted, so this should log again.
            contract.iter_countable_rows(
                [{"field_path": "[9]"}], context="c"
            )  # shape "[" (evicts ".")
        assert sum(1 for r in caplog.records if "anonymous root" in r.getMessage()) == 4

    def test_two_shapes_in_same_call_each_log_once(self, caplog):
        """Regression pin: a SINGLE call to ``iter_countable_rows`` with
        rows of two distinct shapes (leading ``[`` AND leading ``.``)
        must log BOTH shapes, not just the first. The earlier
        ``decision_made_this_call`` bool silently dropped the second."""
        rows = [
            {"field_path": "[3]"},  # shape "["
            {"field_path": ".city"},  # shape "."
            {"field_path": "[7]"},  # shape "[" (already logged this call)
        ]
        with caplog.at_level(logging.WARNING, logger="idp_common.evaluation.contract"):
            contract.iter_countable_rows(rows, context="ctx")
        # Two distinct shapes → two warnings. Third row (also shape "[")
        # is silent because we already logged that shape this call.
        anonymous_warns = [
            r for r in caplog.records if "anonymous root" in r.getMessage()
        ]
        assert len(anonymous_warns) == 2

    def test_recent_shape_survives_eviction(self, monkeypatch, caplog):
        # A shape re-seen before the cap fills is moved to the "recent"
        # end and survives eviction of a colder one.
        monkeypatch.setattr(contract, "_SEEN_ANONYMOUS_ROOT_MAX", 2)
        with caplog.at_level(logging.WARNING, logger="idp_common.evaluation.contract"):
            contract.iter_countable_rows([{"field_path": "[3]"}], context="c")  # ["["]
            contract.iter_countable_rows(
                [{"field_path": ".x"}], context="c"
            )  # ["[", "."]
            contract.iter_countable_rows(
                [{"field_path": "[7]"}], context="c"
            )  # touches "[" → [".", "["]
            contract.iter_countable_rows(
                [{"field_path": ""}], context="c"
            )  # evicts "." → ["[", "empty"]
            contract.iter_countable_rows(
                [{"field_path": "[9]"}], context="c"
            )  # "[" still cached
        anonymous_warns = [
            r for r in caplog.records if "anonymous root" in r.getMessage()
        ]
        # "[" logged once (first call), "." once, "empty" once — the
        # subsequent "[" calls stayed silent because "[" was still cached.
        assert len(anonymous_warns) == 3


@pytest.mark.unit
class TestScalarPositionalCount:
    """``_scalar_positional_count`` counts positional slots in list-like
    values that contribute no dotted leaf paths — bare scalars AND
    empty containers (finding #7 from #625 review: empty-dict list
    items were previously invisible to the row weight even when
    ``_count_leaves`` counted them 1×).
    """

    def test_bare_scalars_counted(self):
        assert contract._scalar_positional_count(["a", "b", "c"]) == 3
        assert contract._scalar_positional_count([1, 2.0, None, True]) == 4

    def test_empty_containers_counted(self):
        # Empty dict / list / tuple / set / frozenset in a list all
        # count as positional slots (no dotted path but a real slot).
        assert contract._scalar_positional_count([{}, [], (), set(), frozenset()]) == 5

    def test_mixed_scalar_and_empty_containers(self):
        assert contract._scalar_positional_count(["a", {}, 1, []]) == 4

    def test_populated_containers_not_counted(self):
        # Populated containers are visible via ``leaf_paths``; don't
        # double-count them here.
        assert contract._scalar_positional_count([{"a": 1}, [1, 2]]) == 0

    def test_non_list_returns_zero(self):
        assert contract._scalar_positional_count("string") == 0
        assert contract._scalar_positional_count({"a": 1}) == 0
        assert contract._scalar_positional_count(42) == 0
        assert contract._scalar_positional_count(None) == 0


@pytest.mark.unit
class TestRowRootAttributeDefensive:
    """``row_root_attribute`` must not crash on a non-string
    ``expected_key`` / ``actual_key`` / ``field_path`` — the whole
    doc's evaluation would otherwise fail on ``.find()``.
    """

    def test_none_path_returns_empty(self):
        assert contract.row_root_attribute({}) == ""

    def test_int_field_path_coerced_to_str(self):
        # A Stickler variant emitting int for ``field_path`` shouldn't
        # crash — coerced via str() so ``.find()`` works.
        assert contract.row_root_attribute({"field_path": 42}) == "42"

    def test_none_expected_key_falls_through_to_field_path(self):
        assert (
            contract.row_root_attribute(
                {"expected_key": None, "field_path": "Items[0].name"}
            )
            == "Items"
        )

    def test_list_shaped_expected_key_stringifies_to_anonymous_root(self):
        """A list-shaped ``expected_key`` stringifies to ``"['Items', 3]"``
        which starts with ``[`` — anonymous-root, returns ``""`` so
        ``iter_countable_rows`` drops it (rather than crashing OR
        misinterpreting it as an attribute name). Guards against a
        future 'fix' that stringifies lists into attribute-like paths."""
        assert contract.row_root_attribute({"expected_key": ["Items", 3]}) == ""


@pytest.mark.unit
class TestClassifyMatchTruthiness:
    """Regression pin (#625 close-4-blockers): ``classify_field_comparison``
    and the per-attribute verdict in ``stickler_backend/results.py`` must
    use the SAME truthiness predicate for the ``match`` field. Asymmetric
    checks (``is True`` vs ``bool(...)``) let section-level confusion-
    matrix counts and per-attribute verdict disagree on the SAME row —
    the exact parent-vs-section drift #625 exists to eliminate.
    """

    def test_bool_true_is_tp(self):
        fc = {"match": True, "expected_value": "x", "actual_value": "x"}
        assert contract.classify_field_comparison(fc) == "tp"

    def test_int_1_is_tp(self):
        """A Stickler variant emitting ``1`` for a matched row lands in
        the same bucket as ``True``."""
        fc = {"match": 1, "expected_value": "x", "actual_value": "x"}
        assert contract.classify_field_comparison(fc) == "tp"

    def test_string_true_is_tp(self):
        """String ``"true"`` (from a JSON parser that keeps booleans as
        strings) reads as matched under the truthy check."""
        fc = {"match": "true", "expected_value": "x", "actual_value": "x"}
        assert contract.classify_field_comparison(fc) == "tp"

    def test_missing_match_is_not_matched(self):
        """Fail-closed on absent evidence — no ``match`` field means we
        don't know, so treat as not-matched. Row with expected and
        actual differing lands as ``fd``."""
        fc = {"expected_value": "x", "actual_value": "y"}
        assert contract.classify_field_comparison(fc) == "fd"

    def test_none_match_is_not_matched(self):
        fc = {"match": None, "expected_value": "x", "actual_value": "y"}
        assert contract.classify_field_comparison(fc) == "fd"

    def test_float_1_5_is_not_matched(self):
        """Narrow allowlist: only numeric ``1`` / ``1.0`` count as
        matched — arbitrary truthy floats like ``1.5`` must NOT slip
        through as a match signal."""
        fc = {"match": 1.5, "expected_value": "x", "actual_value": "y"}
        assert contract.classify_field_comparison(fc) == "fd"

    def test_string_1_digit_is_not_matched(self):
        """Narrow allowlist: only the string ``"true"`` (case-insensitive)
        counts as matched — a string ``"1"`` or ``"0"`` must NOT be
        accepted as a boolean-like."""
        fc_one = {"match": "1", "expected_value": "x", "actual_value": "y"}
        fc_zero = {"match": "0", "expected_value": "x", "actual_value": "y"}
        assert contract.classify_field_comparison(fc_one) == "fd"
        assert contract.classify_field_comparison(fc_zero) == "fd"

    def test_bool_false_is_not_matched(self):
        fc = {"match": False, "expected_value": "x", "actual_value": "y"}
        assert contract.classify_field_comparison(fc) == "fd"

    def test_string_false_is_not_matched(self):
        """Regression pin: raw ``bool("false")`` is True — the narrow
        allowlist must reject the literal string ``"false"`` so a
        Stickler variant emitting it for a rejected row is classified
        correctly."""
        fc = {"match": "false", "expected_value": "x", "actual_value": "y"}
        assert contract.classify_field_comparison(fc) == "fd"

    def test_string_False_capitalized_is_not_matched(self):
        fc = {"match": "False", "expected_value": "x", "actual_value": "y"}
        assert contract.classify_field_comparison(fc) == "fd"

    def test_empty_string_is_not_matched(self):
        fc = {"match": "", "expected_value": "x", "actual_value": "y"}
        assert contract.classify_field_comparison(fc) == "fd"

    def test_models_html_drilldown_agrees_with_classify(self):
        """Regression pin (blocker #1): the HTML drilldown in
        ``DocumentEvaluationResult._format_nested_comparisons`` must
        paint rows GREEN iff the row would classify as matched. Raw
        ``bool(fc.get("match"))`` used to paint ``"false"`` (string)
        GREEN while ``classify_field_comparison`` counted it as
        failure — the exact parent-vs-drilldown contradiction #625
        exists to eliminate."""
        from idp_common.evaluation.models import DocumentEvaluationResult

        # Build a minimal comparison_details row with match="false"
        # string. If the drilldown agrees with classify, the row is
        # painted RED. If the old raw-bool code runs, it's GREEN.
        nested = [
            {
                "expected_key": "name",
                "actual_key": "name",
                "expected_value": "Alice",
                "actual_value": "Alice",
                "match": "false",  # STRING, not bool
                "score": 0.0,
                "reason": "",
            }
        ]
        # Call the private formatter directly.
        doc = DocumentEvaluationResult(document_id="d", section_results=[])
        html = doc._format_nested_comparisons(nested)
        # RED background = ``#f8d7da``; GREEN = ``#d4edda``.
        assert "#f8d7da" in html, (
            "match='false' (string) must paint RED (agree with "
            "classify's 'fd' verdict). Raw bool() would incorrectly "
            "paint it GREEN."
        )
        assert "#d4edda" not in html, "match='false' (string) must NOT paint GREEN"

    def test_numpy_bool_true_is_matched(self):
        """Regression pin: ``numpy.bool_(True)`` must classify as matched.
        Previous version used ``is True`` identity check which failed for
        the numpy subclass, dropping numpy-emitted matches to False."""
        np = pytest.importorskip("numpy")
        fc = {
            "match": np.bool_(True),
            "expected_value": "x",
            "actual_value": "x",
        }
        assert contract.classify_field_comparison(fc) == "tp"

    def test_numpy_bool_false_is_not_matched(self):
        np = pytest.importorskip("numpy")
        fc = {
            "match": np.bool_(False),
            "expected_value": "x",
            "actual_value": "y",
        }
        assert contract.classify_field_comparison(fc) == "fd"

    def test_numpy_int64_one_is_matched(self):
        """Regression pin: ``numpy.int64(1)`` (any numpy integer where
        the value equals 1) must classify as matched. Docstring pointed
        at this case but the earlier ``isinstance(value, int)`` branch
        rejected numpy integers (not subclasses of Python int on numpy
        2.x)."""
        np = pytest.importorskip("numpy")
        fc = {
            "match": np.int64(1),
            "expected_value": "x",
            "actual_value": "x",
        }
        assert contract.classify_field_comparison(fc) == "tp"

    def test_numpy_int64_zero_is_not_matched(self):
        np = pytest.importorskip("numpy")
        fc = {
            "match": np.int64(0),
            "expected_value": "x",
            "actual_value": "y",
        }
        assert contract.classify_field_comparison(fc) == "fd"

    def test_numpy_int64_two_is_not_matched(self):
        """Only numeric ``1`` counts — arbitrary integers must not
        opt in to the matched bucket."""
        np = pytest.importorskip("numpy")
        fc = {
            "match": np.int64(2),
            "expected_value": "x",
            "actual_value": "y",
        }
        assert contract.classify_field_comparison(fc) == "fd"


@pytest.mark.unit
class TestRowWeight:
    """``_row_weight`` picks the max of expected/actual leaf counts, not just
    the expected side, so a hallucinated multi-leaf actual against an empty
    expected still contributes its real leaf count to the confusion matrix
    (finding from #625 high review — previous ``exp if exp is not None else
    act`` undercounted hallucinations whose expected side was ``{}``/``[]``).
    """

    def test_scalar_leaf_row_weight_is_one(self):
        fc = {"expected_value": "Alice", "actual_value": "Alice"}
        assert contract._row_weight(fc) == 1

    def test_multi_leaf_structured_expected_weights_by_leaves(self):
        fc = {
            "expected_value": {"name": "A", "amount": "1"},
            "actual_value": None,
        }
        assert contract._row_weight(fc) == 2

    def test_hallucinated_multi_leaf_actual_against_empty_expected(self):
        # Expected present-but-empty ({}) — earlier code picked exp and got
        # weight=1 despite a 2-leaf hallucination on the actual side.
        fc = {
            "expected_value": {},
            "actual_value": {"a": 1, "b": 2},
        }
        assert contract._row_weight(fc) == 2

    def test_hallucinated_multi_leaf_actual_against_none_expected(self):
        fc = {
            "expected_value": None,
            "actual_value": {"a": 1, "b": 2, "c": 3},
        }
        assert contract._row_weight(fc) == 3

    def test_missing_expected_multi_leaf_against_none_actual(self):
        fc = {
            "expected_value": {"a": 1, "b": 2},
            "actual_value": None,
        }
        assert contract._row_weight(fc) == 2

    def test_disjoint_sides_use_union(self):
        # Both sides structured but their key sets don't overlap → union
        # is 4 leaves. Earlier "max of the two" rule returned 2 (each
        # side has 2 leaves) and per-field would spread to only one
        # side's names, hiding the other side's attributes.
        fc = {
            "expected_value": {"a": 1, "b": 2},
            "actual_value": {"c": 3, "d": 4},
        }
        assert contract._row_weight(fc) == 4

    def test_partial_overlap_uses_union(self):
        # Overlapping key ``a`` counts once in the union.
        fc = {
            "expected_value": {"a": 1, "b": 2},
            "actual_value": {"a": 9, "c": 3},
        }
        assert contract._row_weight(fc) == 3

    def test_empty_container_placeholder_shadowed_by_populated_side(self):
        # Empty-container leaves (``leaf_paths`` emits the prefix so
        # ``_count_leaves`` can floor at 1) get shadowed by populated
        # descendants on the other side. Weight should reflect only the
        # real terminal leaves — otherwise
        # ``_synthesize_parent_buckets`` fires its cross-schema collision
        # warning within a single row's spread (finding B2 from #625
        # adversarial self-review).
        fc = {
            "expected_value": {"items": [], "name": "A"},
            "actual_value": {"items": [{"x": 1}], "name": "B"},
        }
        # Placeholder ``items`` shadowed by ``items.x`` → drop. Leaves:
        # ``name``, ``items.x`` = weight 2.
        assert contract._row_weight(fc) == 2
        leaves = contract._row_leaves(fc)
        assert "items" not in leaves
        assert "items.x" in leaves
        assert "name" in leaves

    def test_list_of_scalars_weighed_by_element_count(self):
        # A truncated list of scalars: exp has 3 elements, act has 0.
        # Bare-scalar leaves don't produce dotted paths, so the union
        # is empty — fall through to ``_count_leaves`` (prefix="_")
        # which counts positional elements. Weight 3 (3 missing leaves).
        fc = {
            "expected_value": ["x", "y", "z"],
            "actual_value": [],
        }
        assert contract._row_weight(fc) == 3

    def test_mixed_dotted_and_positional_sides(self):
        # exp has one dotted leaf ("name"); act has three positional
        # scalars with no attribute name. Weight = 1 (dotted) + 3
        # (positional) = 4 so neither side's slots are lost (finding
        # B1 from #625 adversarial self-review — earlier ``max``-only
        # union returned 1 and dropped the 3 positional hallucinations).
        fc = {
            "expected_value": {"name": "A"},
            "actual_value": ["x", "y", "z"],
        }
        assert contract._row_weight(fc) == 4
        leaves = contract._row_leaves(fc)
        assert "name" in leaves
        # Positional slots emit the ``POSITIONAL_LEAF_NAME`` sub-name.
        # A magic name (not the empty string) so the aggregation
        # Lambda's per-field spread lands them under
        # ``<parent>.__positional__`` — a plain "" would collide with
        # the parent bucket and trip the synthesis collision warning.
        assert leaves.count(contract.POSITIONAL_LEAF_NAME) == 3


@pytest.mark.unit
class TestIsEmptyValue:
    """``_is_empty_value`` and ``_is_structured`` must cover the same
    container shapes — divergence splits classifier semantics from
    row-weighting.
    """

    def test_none_is_empty(self):
        assert contract._is_empty_value(None) is True

    def test_empty_containers_are_empty(self):
        for v in ("", [], {}, (), set(), frozenset()):
            assert contract._is_empty_value(v) is True, f"empty {type(v)}"

    def test_whitespace_only_strings_are_empty(self):
        """Regression pin: Stickler's ``NullHelper`` strips whitespace
        before deciding null-ness. Row-derived counts must agree with
        ``cm.aggregate``, so ``"   "`` / ``"\\n"`` / ``"\\t"`` all
        classify as empty here."""
        for v in ("   ", "\n", "\t\t", "  \t \n  "):
            assert contract._is_empty_value(v) is True, f"whitespace {v!r}"

    def test_non_empty_containers_not_empty(self):
        for v in ("a", [1], {"a": 1}, (1,), {1}, frozenset({1})):
            assert contract._is_empty_value(v) is False, f"non-empty {type(v)}"

    def test_scalars_not_empty(self):
        assert contract._is_empty_value(0) is False
        assert contract._is_empty_value(0.0) is False
        assert contract._is_empty_value(False) is False

    def test_arbitrary_class_with_empty_dict_is_empty(self):
        class Empty:
            pass

        assert contract._is_empty_value(Empty()) is True
        assert contract._is_structured(Empty()) is True

    def test_arbitrary_class_with_public_attrs_not_empty(self):
        class WithAttrs:
            def __init__(self):
                self.name = "A"

        assert contract._is_empty_value(WithAttrs()) is False
        assert contract._is_structured(WithAttrs()) is True

    def test_arbitrary_class_with_only_underscore_attrs_is_empty(self):
        # Only private attributes → semantically empty (nothing to compare).
        class OnlyPrivate:
            def __init__(self):
                self._x = 1

        assert contract._is_empty_value(OnlyPrivate()) is True


@pytest.mark.unit
class TestSafeDiv:
    """``safe_div`` is imported by both the per-doc and run-level paths;
    its zero-denominator convention (return 0.0, not None) is what keeps
    per-doc and run-level dashboards from rendering the same field as
    ``0.000`` on one and ``N/A`` on the other."""

    def test_positive_denominator_returns_ratio(self):
        assert contract.safe_div(3, 4) == 0.75

    def test_zero_denominator_returns_zero(self):
        assert contract.safe_div(0, 0) == 0.0
        assert contract.safe_div(5, 0) == 0.0

    def test_negative_denominator_treated_as_zero(self):
        # Not a real scenario (all callers pass counts), but defensive:
        # only strictly-positive denominators divide.
        assert contract.safe_div(1, -1) == 0.0
