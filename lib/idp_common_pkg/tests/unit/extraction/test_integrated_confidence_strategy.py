# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the integrated-confidence strategies (agentic extraction).

`extraction.agentic.integrated_confidence_strategy` (a hidden experimental knob)
selects how confidence is produced when `confidence.mode == integrated`:

  - two_step (default): extract via extraction_tool, then a follow-up
    provide_field_assessment call (a dedicated reflection inference).
  - single_shot: extract + confidence in ONE combined tool call
    (extraction_with_confidence_tool), saving the follow-up inference.
  - topk: extract + confidence in ONE combined tool call
    (extraction_with_topk_tool) where the agent emits top-K guesses with
    probabilities; the shared topk_resolver takes G1 as value, P1 as confidence.

All must record the SAME agent.state["field_assessment"] shape so the downstream
collation/grounding/explainability path is identical. These tests cover the
config field, each tool's behavior, and (lightly) tool selection.
"""

from __future__ import annotations

import pytest

# The factories' tools are decorated with the REAL strands ``@tool`` and called
# directly: ``DecoratedFunctionTool.__call__`` forwards to the wrapped function.
# Do not replace the decorator here (``strands.tool``, or the name a production
# module bound from it): a replacement made at import outlives this module and
# turns every Strands tool built later in the same process into a plain
# function, which an Agent then cannot register (#1756). The strands-binding
# check in tests/conftest.py fails a module that does it.
DecoratedFunctionTool = pytest.importorskip(
    "strands.tools.decorator",
    reason="strands-agents not installed (a conftest stub does not count)",
).DecoratedFunctionTool

from pydantic import BaseModel  # noqa: E402

from idp_common.extraction.agentic_idp import (  # noqa: E402
    create_dynamic_extraction_tool_and_patch_tool,
)


# --- minimal Strands Agent mock (only needs .state.get/.set) ----------------
class _SimpleState:
    def __init__(self):
        self._data: dict = {}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value):
        self._data[key] = value


class MockAgent:
    def __init__(self):
        self.state = _SimpleState()


# --- test data model --------------------------------------------------------
class _Row(BaseModel):
    description: str
    amount: str


class _Statement(BaseModel):
    account_number: str
    transactions: list[_Row]


def _tools(model_class):
    """Unpack the factory tuple by name for readability."""
    (
        extraction_tool,
        extraction_with_confidence_tool,
        extraction_with_topk_tool,
        apply_json_patches,
        make_buffer_data_final_extraction,
        finalize_table_extraction,
    ) = create_dynamic_extraction_tool_and_patch_tool(model_class)
    return extraction_tool, extraction_with_confidence_tool, extraction_with_topk_tool


# =============================================================================
# Config field
# =============================================================================
class TestStrategyConfigField:
    def test_default_is_two_step(self):
        from idp_common.config.models import AgenticConfig

        assert AgenticConfig().integrated_confidence_strategy == "two_step"

    def test_single_shot_accepted(self):
        from idp_common.config.models import AgenticConfig

        cfg = AgenticConfig(integrated_confidence_strategy="single_shot")
        assert cfg.integrated_confidence_strategy == "single_shot"

    def test_blank_falls_back_to_two_step(self):
        from idp_common.config.models import AgenticConfig

        assert (
            AgenticConfig(
                integrated_confidence_strategy=""
            ).integrated_confidence_strategy
            == "two_step"
        )
        assert (
            AgenticConfig(
                integrated_confidence_strategy=None
            ).integrated_confidence_strategy
            == "two_step"
        )

    def test_case_insensitive_normalization(self):
        from idp_common.config.models import AgenticConfig

        assert (
            AgenticConfig(
                integrated_confidence_strategy=" Single_Shot "
            ).integrated_confidence_strategy
            == "single_shot"
        )

    def test_unknown_value_rejected(self):
        from idp_common.config.models import AgenticConfig

        with pytest.raises(Exception):
            AgenticConfig(integrated_confidence_strategy="inline")


# =============================================================================
# single_shot: the combined tool records BOTH extraction and confidence
# =============================================================================
class TestSingleShotCombinedTool:
    def test_records_extraction_and_confidence_in_one_call(self):
        _, combined, _topk = _tools(_Statement)
        agent = MockAgent()

        extraction = {
            "account_number": "12345",
            "transactions": [
                {"description": "Coffee", "amount": "4.50"},
                {"description": "Books", "amount": "22.00"},
            ],
        }
        field_assessment = {
            "account_number": {"confidence": 0.99, "confidence_reason": "clear"},
            "transactions": [
                {"confidence": 0.95, "confidence_reason": "legible"},
                {"confidence": 0.80, "confidence_reason": "faint"},
            ],
        }

        result = combined(
            extraction=extraction, field_assessment=field_assessment, agent=agent
        )

        # Extraction stored exactly as extraction_tool would (validated round-trip).
        stored = agent.state.get("current_extraction")
        assert stored["account_number"] == "12345"
        assert len(stored["transactions"]) == 2
        # Confidence stored in the SAME slot two_step's provide_field_assessment uses.
        assert agent.state.get("field_assessment") == field_assessment
        assert "recorded" in result.lower()

    def test_schema_invalid_extraction_raises_and_records_nothing(self):
        _, combined, _topk = _tools(_Statement)
        agent = MockAgent()
        # Missing required field -> Pydantic raises (surfaced to the retry loop);
        # neither extraction nor confidence is recorded.
        with pytest.raises(Exception):
            combined(
                extraction={"transactions": []},  # no account_number
                field_assessment={"x": 1},
                agent=agent,
            )
        assert agent.state.get("current_extraction") is None
        assert agent.state.get("field_assessment") is None

    def test_empty_array_returns_error_string_and_records_nothing(self):
        _, combined, _topk = _tools(_Statement)
        agent = MockAgent()
        # Array-shape guard returns an error string (not a raise) before validation.
        result = combined(extraction=[], field_assessment={"x": 1}, agent=agent)
        assert isinstance(result, str) and "empty array" in result.lower()
        assert agent.state.get("current_extraction") is None
        assert agent.state.get("field_assessment") is None

    def test_single_element_array_unwrapped(self):
        _, combined, _topk = _tools(_Statement)
        agent = MockAgent()
        payload = {"account_number": "9", "transactions": []}
        combined(extraction=[payload], field_assessment={}, agent=agent)
        assert agent.state.get("current_extraction")["account_number"] == "9"


# =============================================================================
# two_step: the plain extraction tool records ONLY extraction (no confidence)
# =============================================================================
class TestTwoStepPlainTool:
    def test_extraction_tool_records_only_extraction(self):
        plain, _, _topk = _tools(_Statement)
        agent = MockAgent()
        plain(
            extraction={"account_number": "1", "transactions": []},
            agent=agent,
        )
        assert agent.state.get("current_extraction")["account_number"] == "1"
        # No confidence recorded by the plain tool — that's the follow-up call's job.
        assert agent.state.get("field_assessment") is None

    def test_provide_field_assessment_records_confidence(self):
        from idp_common.extraction.agentic_idp import provide_field_assessment

        agent = MockAgent()
        fa = {"account_number": {"confidence": 0.9, "confidence_reason": "x"}}
        provide_field_assessment(assessment=fa, agent=agent)
        assert agent.state.get("field_assessment") == fa


# =============================================================================
# Equivalence: both strategies yield the same recorded state shape
# =============================================================================
def test_both_strategies_produce_same_state_shape():
    plain, combined, _topk = _tools(_Statement)
    from idp_common.extraction.agentic_idp import provide_field_assessment

    extraction = {
        "account_number": "1",
        "transactions": [{"description": "a", "amount": "1"}],
    }
    fa = {
        "account_number": {"confidence": 0.9, "confidence_reason": "x"},
        "transactions": [{"confidence": 0.8, "confidence_reason": "y"}],
    }

    # two_step: two calls
    a1 = MockAgent()
    plain(extraction=extraction, agent=a1)
    provide_field_assessment(assessment=fa, agent=a1)

    # single_shot: one call
    a2 = MockAgent()
    combined(extraction=extraction, field_assessment=fa, agent=a2)

    assert a1.state.get("current_extraction") == a2.state.get("current_extraction")
    assert a1.state.get("field_assessment") == a2.state.get("field_assessment")


# =============================================================================
# topk: the combined tool resolves G1->value and P1->confidence in one call
# =============================================================================
class TestTopKConfigField:
    def test_topk_accepted(self):
        from idp_common.config.models import AgenticConfig

        cfg = AgenticConfig(integrated_confidence_strategy="topk")
        assert cfg.integrated_confidence_strategy == "topk"

    def test_topk_case_insensitive(self):
        from idp_common.config.models import AgenticConfig

        assert (
            AgenticConfig(
                integrated_confidence_strategy=" TopK "
            ).integrated_confidence_strategy
            == "topk"
        )


class TestTopKCombinedTool:
    def test_resolves_g1_value_and_p1_confidence(self):
        _plain, _combined, topk = _tools(_Statement)
        agent = MockAgent()

        candidates = {
            "account_number": {"G1": "12345", "P1": 0.97, "G2": "1234S", "P2": 0.03},
            "transactions": [
                {
                    "description": {"G1": "Coffee", "P1": 0.9},
                    "amount": {"G1": "4.50", "P1": 0.8},
                },
            ],
        }
        result = topk(candidates=candidates, agent=agent)

        # G1 became the extracted value (validated round-trip).
        stored = agent.state.get("current_extraction")
        assert stored["account_number"] == "12345"
        assert stored["transactions"][0]["description"] == "Coffee"
        # P1 became the confidence, in the SAME slot the other strategies use.
        fa = agent.state.get("field_assessment")
        assert fa["account_number"]["confidence"] == 0.97
        assert fa["transactions"][0]["description"]["confidence"] == 0.9
        assert "recorded" in result.lower()

    def test_topk_invalid_extraction_records_nothing(self):
        # Missing required account_number after resolution -> validation error;
        # neither extraction nor confidence recorded.
        _plain, _combined, topk = _tools(_Statement)
        agent = MockAgent()
        with pytest.raises(Exception):
            topk(candidates={"transactions": []}, agent=agent)
        assert agent.state.get("current_extraction") is None
        assert agent.state.get("field_assessment") is None


# =============================================================================
# The factory returns a real Strands tool
# =============================================================================


class TestStrategyToolsAreStrandsTools:
    """Each strategy's tool must be registrable with an Agent, not a bare function."""

    def test_each_strategy_tool_is_a_strands_tool(self):
        names = []
        for tool_obj in _tools(_Statement):
            assert isinstance(tool_obj, DecoratedFunctionTool), tool_obj
            assert (
                "agent" not in tool_obj.tool_spec["inputSchema"]["json"]["properties"]
            )
            names.append(tool_obj.tool_name)
        assert names == [
            "extraction_tool",
            "extraction_with_confidence_tool",
            "extraction_with_topk_tool",
        ]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
