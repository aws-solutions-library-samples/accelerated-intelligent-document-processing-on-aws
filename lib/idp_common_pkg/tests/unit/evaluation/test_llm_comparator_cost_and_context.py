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
