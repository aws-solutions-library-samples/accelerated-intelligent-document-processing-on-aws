# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression tests for the LLM comparator's cost and field context.

Three defects are pinned here, all observed live on a v0.6.5 stack where a
54-line-item invoice wedged the evaluation Lambda:

1. An LLM comparator on a field INSIDE a structured list is quadratic. Hungarian
   row matching builds a full N_gt x N_pred similarity matrix, invoking each item
   field's comparator per cell, then scores the matched pairs — measured at
   N^2 + 2N Bedrock calls. At ~0.9 s per call a 54-row list needs ~45 minutes,
   so the 900 s Lambda could never finish it at any retry count.
2. The comparator received no field context — every judge call went out as
   `class: . For the attribute named "" described as "":` — because Stickler's
   comparator protocol is compare(value1, value2).
3. Identical values still cost a Bedrock round trip.

The call-count assertions are the load-bearing part: they fail loudly if the
quadratic path is ever reintroduced.
"""

import copy
import json
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from idp_common.evaluation.stickler_backend.comparators import (
    LLMComparator,
    compare_llm,
    register_idp_comparators,
)
from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper
from idp_common.evaluation.stickler_backend.model_factory import get_stickler_model


def _schema() -> Dict[str, Any]:
    """An invoice-shaped class: one scalar LLM field + an LLM field inside a list."""
    return {
        "$id": "Invoice",
        "type": "object",
        "x-aws-idp-document-type": "Invoice",
        "properties": {
            "Agency": {
                "type": "string",
                "description": "The advertising agency placing the order",
                "x-aws-idp-evaluation-method": "LLM",
            },
            "LineItems": {
                "type": "array",
                # An evaluation method on a structured array is ignored (the list
                # scores through its item fields) — asserted below to warn.
                "x-aws-idp-evaluation-method": "LLM",
                "items": {
                    "type": "object",
                    "properties": {
                        "Desc": {
                            "type": "string",
                            "description": "Description of the advertising spot",
                            "x-aws-idp-evaluation-method": "LLM",
                        },
                        "Rate": {
                            "type": "number",
                            "x-aws-idp-evaluation-method": "NUMERIC_EXACT",
                        },
                    },
                },
            },
        },
    }


def _build(schema: Dict[str, Any]) -> Dict[str, Any]:
    return SticklerConfigMapper.build_stickler_model_config(
        json.loads(json.dumps(schema)), llm_config={"model": "test-model"}
    )


def _bedrock_ok() -> Dict[str, Any]:
    payload = json.dumps({"match": True, "score": 1.0, "reason": "r"})
    return {"response": {"output": {"message": {"content": [{"text": payload}]}}}}


def _compare_counting_calls(cfg: Dict[str, Any], gt: Any, pred: Any) -> List[Any]:
    """Run a full Stickler comparison, returning the Bedrock calls it made."""
    calls: List[Any] = []

    def fake_invoke(**kwargs):
        calls.append(kwargs)
        return _bedrock_ok()

    register_idp_comparators()
    model = get_stickler_model(
        "Invoice", {"invoice": cfg}, {}, set(), lambda *a, **k: None
    )
    with patch("idp_common.bedrock.invoke_model", side_effect=fake_invoke):
        model.model_validate(gt).compare_with(model.model_validate(pred))
    return calls


def _doc(n_rows: int, agency: str) -> Dict[str, Any]:
    return {
        "Agency": agency,
        "LineItems": [{"Desc": f"spot {i}", "Rate": float(i)} for i in range(n_rows)],
    }


# ---------------------------------------------------------------------------
# 1. No LLM comparator inside a structured list (the quadratic blowup).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_llm_method_inside_list_is_downgraded_to_type_default():
    """The list-item field must NOT get IDPLLMComparator.

    Dropping the comparator override lets Stickler's JsonSchemaFieldConverter
    apply its own type default (string -> Levenshtein), which is what a matching
    cost function should be.
    """
    items = _build(_schema())["schema"]["properties"]["LineItems"]["items"]
    assert "x-aws-stickler-comparator" not in items["properties"]["Desc"]
    # The scalar field outside the list keeps its LLM comparator.
    assert (
        _build(_schema())["schema"]["properties"]["Agency"]["x-aws-stickler-comparator"]
        == "IDPLLMComparator"
    )


@pytest.mark.unit
def test_llm_method_inside_list_can_be_opted_back_in():
    schema = _schema()
    desc = schema["properties"]["LineItems"]["items"]["properties"]["Desc"]
    desc["x-aws-idp-evaluation-allow-llm-in-list"] = True
    items = _build(schema)["schema"]["properties"]["LineItems"]["items"]
    assert (
        items["properties"]["Desc"]["x-aws-stickler-comparator"] == "IDPLLMComparator"
    )


@pytest.mark.unit
@pytest.mark.parametrize("false_value", ["false", "FALSE", "no", "off", "0", False])
def test_yaml_quoted_false_bypass_guarded(false_value):
    """Regression pin (#625 close-4-blockers): a YAML config with the
    opt-in flag set to the STRING ``"false"`` (or ``"no"``/``"off"``)
    must not bypass the LLM-in-list guard. Raw ``bool()`` returns True
    for any non-empty string; the guard now uses ``_coerce_bool``."""
    schema = _schema()
    desc = schema["properties"]["LineItems"]["items"]["properties"]["Desc"]
    desc["x-aws-idp-evaluation-allow-llm-in-list"] = false_value
    items = _build(schema)["schema"]["properties"]["LineItems"]["items"]
    # Guard fired → LLM comparator NOT applied → downgrade happened.
    assert "x-aws-stickler-comparator" not in items["properties"]["Desc"], (
        f"YAML value {false_value!r} bypassed the LLM-in-list guard"
    )


@pytest.mark.unit
def test_top_level_scalar_llm_attribute_gets_context_name():
    """Regression pin: a top-level scalar attribute (empty field_path)
    scored via LLM must NOT send ``ATTRIBUTE_NAME = ""`` to the judge.
    Falls back to document_class then "root" so the judge always has
    a nameable subject."""
    from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper

    schema = {
        "type": "object",
        "x-aws-idp-evaluation-method": "LLM",  # method on the top-level object itself
        "description": "The whole document",
    }
    # Direct translate at field_path="", document_class="Invoice"
    translated = SticklerConfigMapper._translate_extensions_in_schema(
        copy.deepcopy(schema),
        field_path="",
        llm_config={"model": "test"},
        in_list_items=False,
        document_class="Invoice",
    )
    ctx = translated.get("x-aws-stickler-comparator-config", {})
    # Falls back to document_class when field_path is empty.
    assert ctx.get("attribute_name") == "Invoice"


@pytest.mark.unit
def test_bracketed_field_path_falls_back_to_document_class():
    """Regression pin: a bare-bracket ``field_path`` like ``"[3]"`` (the
    strip produces empty) must fall through to ``document_class``, not
    keep the original bracketed name."""
    from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper

    schema = {
        "type": "string",
        "x-aws-idp-evaluation-method": "LLM",
        "description": "top-level positional",
    }
    translated = SticklerConfigMapper._translate_extensions_in_schema(
        copy.deepcopy(schema),
        field_path="[3]",  # bracket-only path
        llm_config={"model": "test"},
        in_list_items=False,
        document_class="Invoice",
    )
    ctx = translated.get("x-aws-stickler-comparator-config", {})
    assert ctx.get("attribute_name") == "Invoice"


@pytest.mark.unit
def test_whitespace_document_class_falls_back_to_root():
    """Regression pin: a whitespace-only ``document_class`` must NOT
    pass through as truthy — the LLM judge would otherwise see
    ``ATTRIBUTE_NAME = "   "``. Falls back to ``"root"``."""
    from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper

    schema = {
        "type": "string",
        "x-aws-idp-evaluation-method": "LLM",
        "description": "top-level positional",
    }
    translated = SticklerConfigMapper._translate_extensions_in_schema(
        copy.deepcopy(schema),
        field_path="[3]",
        llm_config={"model": "test"},
        in_list_items=False,
        document_class="   ",  # whitespace-only
    )
    ctx = translated.get("x-aws-stickler-comparator-config", {})
    assert ctx.get("attribute_name") == "root"


@pytest.mark.unit
def test_hungarian_on_structured_array_does_not_warn(caplog):
    """Regression pin: HUNGARIAN is the default/correct list-matching
    algorithm — it must NOT trigger the array-method warning meant
    for LLM/etc."""
    import logging as _logging

    from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper

    schema = {
        "type": "array",
        "x-aws-idp-evaluation-method": "HUNGARIAN",
        "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        },
    }
    with caplog.at_level(
        _logging.WARNING,
        logger="idp_common.evaluation.stickler_backend.mapper",
    ):
        SticklerConfigMapper._translate_extensions_in_schema(
            copy.deepcopy(schema),
            field_path="Items",
            llm_config={"model": "test"},
            in_list_items=False,
            document_class="Invoice",
        )
    warns = [r for r in caplog.records if "on a structured array" in r.getMessage()]
    assert warns == [], (
        f"HUNGARIAN on a structured array should not trigger the "
        f"array-method warning; got: {[r.getMessage() for r in warns]}"
    )


@pytest.mark.unit
def test_array_method_key_stripped_after_processing():
    """Regression pin: after the array-method branch runs, the
    ``x-aws-idp-evaluation-method`` key must be stripped from the
    schema (it's inert for structured arrays and lingering metadata
    could be re-interpreted by downstream code)."""
    from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper

    schema = {
        "type": "array",
        "x-aws-idp-evaluation-method": "HUNGARIAN",
        "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
        },
    }
    translated = SticklerConfigMapper._translate_extensions_in_schema(
        copy.deepcopy(schema),
        field_path="Items",
        llm_config={"model": "test"},
        in_list_items=False,
        document_class="Invoice",
    )
    assert "x-aws-idp-evaluation-method" not in translated


@pytest.mark.unit
@pytest.mark.parametrize("n_rows", [3, 10, 54])
def test_list_comparison_llm_calls_do_not_grow_with_row_count(n_rows):
    """The regression that wedged a stack: calls were N^2 + 2N.

    With the downgrade in place the row comparisons make no Bedrock calls at all,
    so the count is flat in N rather than quadratic.
    """
    cfg = _build(_schema())
    calls = _compare_counting_calls(
        cfg, _doc(n_rows, "AnyCompany Media"), _doc(n_rows, "AnyCompany Media")
    )
    assert len(calls) == 0, (
        f"{len(calls)} Bedrock calls for a {n_rows}-row list; the LLM comparator "
        f"is being invoked inside Hungarian matching again (was N^2+2N = "
        f"{n_rows * n_rows + 2 * n_rows})"
    )


@pytest.mark.unit
def test_downgrade_preserves_the_fields_other_extensions():
    """Only the comparator choice is overridden — nothing else on the field.

    Regression guard for a bug in the first draft of this fix, which returned
    early from the translation pass on the downgrade path and therefore skipped
    threshold / weight / clip-under-threshold / aggregate translation for the
    field it had just rewritten.
    """
    schema = _schema()
    desc = schema["properties"]["LineItems"]["items"]["properties"]["Desc"]
    desc["x-aws-idp-evaluation-threshold"] = 0.9
    desc["x-aws-idp-evaluation-weight"] = 3.0
    desc["x-aws-idp-evaluation-clip-under-threshold"] = True
    desc["x-aws-idp-evaluation-aggregate"] = "mean"

    out = _build(schema)["schema"]["properties"]["LineItems"]["items"]["properties"][
        "Desc"
    ]
    assert "x-aws-stickler-comparator" not in out  # downgraded
    assert out["x-aws-stickler-threshold"] == 0.9
    assert out["x-aws-stickler-weight"] == 3.0
    assert out["x-aws-stickler-clip-under-threshold"] is True
    assert out["x-aws-stickler-aggregate"] == "mean"


@pytest.mark.unit
def test_downgrade_still_translates_nested_children():
    """A downgraded field's own children are still walked."""
    schema = _schema()
    items = schema["properties"]["LineItems"]["items"]
    items["properties"]["Nested"] = {
        "type": "object",
        "x-aws-idp-evaluation-method": "LLM",
        "properties": {
            "Inner": {"type": "number", "x-aws-idp-evaluation-method": "NUMERIC_EXACT"}
        },
    }
    out = _build(schema)["schema"]["properties"]["LineItems"]["items"]["properties"]
    assert (
        out["Nested"]["properties"]["Inner"]["x-aws-stickler-comparator"]
        == "NumericComparator"
    )


@pytest.mark.unit
def test_verdict_cache_is_bounded():
    from idp_common.evaluation.stickler_backend import comparators as mod

    comparator = LLMComparator(model="test-model")
    with patch.object(mod, "_VERDICT_CACHE_MAX", 2):
        with patch("idp_common.bedrock.invoke_model", return_value=_bedrock_ok()):
            for i in range(10):
                comparator.compare(f"expected {i}", f"actual {i}")
    assert len(comparator._verdict_cache) <= 2


@pytest.mark.unit
def test_evaluation_method_on_structured_array_warns(caplog):
    """The method is discarded — say so instead of dropping intent silently."""
    with caplog.at_level("WARNING"):
        _build(_schema())
    assert any("structured array is ignored" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 2. Field context reaches the judge.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mapper_supplies_field_context_to_llm_comparator():
    agency = _build(_schema())["schema"]["properties"]["Agency"]
    ctx = agency["x-aws-stickler-comparator-config"]
    assert ctx["document_class"] == "Invoice"
    assert ctx["attribute_name"] == "Agency"
    assert ctx["attribute_description"] == "The advertising agency placing the order"
    # The llm_method config still rides the same channel.
    assert ctx["model"] == "test-model"


@pytest.mark.unit
def test_comparator_puts_context_in_the_prompt():
    """Regression: the prompt used to render `class: . attribute named "" ...`."""
    calls = []

    def fake_invoke(**kwargs):
        calls.append(kwargs)
        return _bedrock_ok()

    comparator = LLMComparator(
        model="test-model",
        document_class="Invoice",
        attribute_name="Agency",
        attribute_description="The advertising agency placing the order",
    )
    with patch("idp_common.bedrock.invoke_model", side_effect=fake_invoke):
        comparator.compare("WNBW", "Buying Time, LLC")

    assert len(calls) == 1
    prompt = json.dumps(calls[0]["content"])
    assert "Invoice" in prompt
    assert "Agency" in prompt
    assert "The advertising agency placing the order" in prompt
    assert 'attribute named ""' not in prompt


@pytest.mark.unit
def test_context_reaches_the_prompt_end_to_end():
    """Mapper -> Stickler converter -> comparator instance -> prompt."""
    cfg = _build(_schema())
    calls = _compare_counting_calls(
        cfg, _doc(1, "AnyCompany Media"), _doc(1, "Some Other Agency")
    )
    assert len(calls) == 1, "expected exactly one judged field (Agency)"
    prompt = json.dumps(calls[0]["content"])
    assert "The advertising agency placing the order" in prompt


# ---------------------------------------------------------------------------
# 3. Identical values cost nothing.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "expected,actual",
    [
        ("Florida Democratic Party", "Florida Democratic Party"),
        ("AnyCompany Media", "anycompany media"),
        ("AnyCompany  Media", "AnyCompany Media"),
        ("  padded  ", "padded"),
        (None, None),
    ],
)
def test_identical_values_short_circuit_without_a_bedrock_call(expected, actual):
    with patch("idp_common.bedrock.invoke_model") as invoke:
        matched, score, reason = compare_llm(
            expected=expected, actual=actual, llm_config={"model": "test-model"}
        )
    invoke.assert_not_called()
    assert matched is True
    assert score == 1.0
    assert reason is not None and "no LLM call" in reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "expected,actual",
    [
        ("WNBW", "Buying Time, LLC"),
        ("Net 30", "30 days net"),  # needs real reasoning — must reach the judge
        ("Acme, Inc.", "Acme Inc"),  # punctuation is NOT folded, deliberately
    ],
)
def test_values_needing_judgement_still_call_bedrock(expected, actual):
    with patch("idp_common.bedrock.invoke_model", return_value=_bedrock_ok()) as invoke:
        compare_llm(
            expected=expected, actual=actual, llm_config={"model": "test-model"}
        )
    invoke.assert_called_once()


@pytest.mark.unit
def test_repeated_pairs_are_memoized_within_a_comparator():
    with patch("idp_common.bedrock.invoke_model", return_value=_bedrock_ok()) as invoke:
        comparator = LLMComparator(model="test-model")
        first = comparator.compare("WNBW", "Buying Time, LLC")
        second = comparator.compare("WNBW", "Buying Time, LLC")
    assert first == second
    invoke.assert_called_once()


@pytest.mark.unit
def test_non_json_serializable_matching_pair_does_not_crash():
    """Regression pin (#625 close-4-blockers): compare_llm must NOT
    call ``json.dumps`` on the raw values BEFORE the None / trivial-
    equal short-circuits. A non-JSON-serializable value (Decimal,
    datetime, set, Pydantic BaseModel) would otherwise raise
    TypeError, the outer except would swallow it, and a MATCHING
    pair (both same Decimal) would score as False, 0.0."""
    from decimal import Decimal

    # No mock — this should short-circuit as trivially equal before
    # any Bedrock call or json.dumps happens.
    matched, score, reason = compare_llm(
        expected=Decimal("1.5"),
        actual=Decimal("1.5"),
        llm_config={"model": "test-model"},
    )
    assert matched is True
    assert score == 1.0


@pytest.mark.unit
def test_cache_dict_key_order_insensitive():
    """Regression pin: cache key must not depend on dict insertion
    order — Hungarian matching cross-compares dicts from different
    JSON parses whose key order may differ, and the cache must hit."""
    # Use non-matching dicts on both sides so trivial-equal doesn't
    # short-circuit; the actual Bedrock call is what we're pinning.
    with patch("idp_common.bedrock.invoke_model", return_value=_bedrock_ok()) as invoke:
        comparator = LLMComparator(model="test-model")
        comparator.compare({"a": 1, "b": 2}, {"a": 9, "b": 9})
        # Same values, different insertion order on BOTH sides → should
        # hit cache and NOT re-invoke Bedrock.
        comparator.compare({"b": 2, "a": 1}, {"b": 9, "a": 9})
    invoke.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize(
    "error_reason",
    [
        # The four ACTUAL error_msg formats compare_llm produces (see
        # lines ~472/558/596/602 of comparators.py). An earlier version
        # of this test used a synthetic prefix that matched the code's
        # list but NOT the actual production error strings, so the
        # test passed while production still poisoned the cache.
        "Task prompt formatting error: missing placeholder",
        "Error parsing LLM response as JSON: unexpected token",
        "Unexpected error processing LLM response: KeyError",
        "Error in LLM evaluation for MyField: throttled",
    ],
)
def test_transient_error_is_not_cached_permanently(error_reason):
    """Regression pin: EACH of compare_llm's four error-path reason
    strings must be recognized as transient and skipped from caching.
    Caching a transient failure would poison the (value1, value2)
    pair for the warm container's lifetime."""
    from idp_common.evaluation.stickler_backend import comparators

    comparator = LLMComparator(model="test-model")
    with patch.object(
        comparators, "compare_llm", return_value=(False, 0.0, error_reason)
    ):
        first = comparator.compare("A", "B")
    assert first == 0.0
    # Same key, real success. Cache MUST NOT hold the error tuple.
    with patch.object(comparators, "compare_llm", return_value=(True, 1.0, "match")):
        second = comparator.compare("A", "B")
    assert second == 1.0, (
        f"transient error with reason={error_reason!r} was cached "
        f"permanently — subsequent successful call returned the stale "
        f"0.0 instead of the fresh 1.0 score"
    )


@pytest.mark.unit
def test_cache_key_direct_equality_across_dict_order():
    """Regression pin: ``_cache_key`` produces the same string for
    two dicts with same values in different insertion order — the
    end-to-end cache-hit test alone doesn't rule out an accidental
    change to the key function itself."""
    from idp_common.evaluation.stickler_backend.comparators import _cache_key

    k1 = _cache_key({"a": 1, "b": 2}, {"c": 3, "d": 4})
    k2 = _cache_key({"b": 2, "a": 1}, {"d": 4, "c": 3})
    assert k1 == k2
