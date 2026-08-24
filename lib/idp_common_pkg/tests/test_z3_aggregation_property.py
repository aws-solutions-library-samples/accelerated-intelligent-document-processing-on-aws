# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for aggregated results completeness.

Verifies that _process_policy_type returns exactly N results for N rules,
with uniform structure regardless of engine.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from idp_common.config.schema_constants import (
    X_AWS_IDP_RULE_ID,
    X_AWS_IDP_VALIDATION_ENGINE,
)


def _build_config(rules):
    rule_properties = {}
    for i, rule in enumerate(rules):
        prop = {"type": "string", "description": rule["description"]}
        if rule.get("engine"):
            prop[X_AWS_IDP_VALIDATION_ENGINE] = rule["engine"]
        if rule.get("rule_id"):
            prop[X_AWS_IDP_RULE_ID] = rule["rule_id"]
        rule_properties[f"rule_{i}"] = prop
    return {
        "policy_classes": [
            {"x-aws-idp-policy-type": "TestPolicy", "rule_properties": rule_properties}
        ],
        "rule_validation": {
            "fact_extraction": {
                "model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
            },
            "semaphore": 5,
            "max_chunk_size": 4000,
            "token_size": 1000,  # nosec B105 - config value, not a password
            "overlap_percentage": 0.1,
        },
    }


@pytest.mark.unit
class TestAggregatedResults:
    """Results completeness: N rules → N results, uniform structure."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "rules",
        [
            [{"description": "rule A", "engine": "llm", "rule_id": None}],
            [
                {"description": "rule A", "engine": "z3", "rule_id": "r1"},
                {"description": "rule B", "engine": "llm", "rule_id": None},
                {"description": "rule C", "engine": None, "rule_id": None},
            ],
            [
                {"description": f"rule {i}", "engine": "z3", "rule_id": f"r{i}"}
                for i in range(5)
            ],
        ],
        ids=["single-llm", "mixed-3-rules", "all-z3-5-rules"],
    )
    async def test_result_count_equals_rule_count(self, rules):
        config = _build_config(rules)

        with patch(
            "idp_common.rule_validation.service.RuleValidationService.__init__",
            return_value=None,
        ):
            from idp_common.rule_validation.service import RuleValidationService

            service = RuleValidationService.__new__(RuleValidationService)
            service.config = MagicMock()
            service.config.rule_validation.semaphore = 5
            service._semaphore = asyncio.Semaphore(5)
            service.timing_metrics = {"criteria_processing_time": []}

            async def mock_z3(
                rule_description,
                rule_id,
                policy_type,
                extraction_results,
                document_text,
                config,
                rule_json=None,
            ):
                return {
                    "policy_type": policy_type,
                    "rule": rule_description,
                    "extracted_facts": [],
                    "extraction_summary": "z3",
                }

            async def mock_llm(
                rule, user_history, policy_type, config, extraction_results=None
            ):
                return {
                    "policy_type": policy_type,
                    "rule": rule,
                    "extracted_facts": [],
                    "extraction_summary": "llm",
                }

            service._process_z3_fact_extraction = mock_z3
            service._process_rule_question = mock_llm

            results = await service._process_policy_type(
                policy_type="TestPolicy",
                user_history="doc",
                config=config,
                extraction_results=None,
            )

            assert len(results) == len(rules)

    @pytest.mark.asyncio
    async def test_mixed_engine_results_uniform_structure(self):
        rules = [
            {"description": "z3 rule", "engine": "z3", "rule_id": "r1"},
            {"description": "llm rule", "engine": "llm", "rule_id": None},
        ]
        config = _build_config(rules)

        with patch(
            "idp_common.rule_validation.service.RuleValidationService.__init__",
            return_value=None,
        ):
            from idp_common.rule_validation.service import RuleValidationService

            service = RuleValidationService.__new__(RuleValidationService)
            service.config = MagicMock()
            service.config.rule_validation.semaphore = 5
            service._semaphore = asyncio.Semaphore(5)
            service.timing_metrics = {"criteria_processing_time": []}

            async def mock_z3(
                rule_description,
                rule_id,
                policy_type,
                extraction_results,
                document_text,
                config,
                rule_json=None,
            ):
                return {
                    "policy_type": policy_type,
                    "rule": rule_description,
                    "extracted_facts": [],
                    "extraction_summary": "z3",
                }

            async def mock_llm(
                rule, user_history, policy_type, config, extraction_results=None
            ):
                return {
                    "policy_type": policy_type,
                    "rule": rule,
                    "extracted_facts": [],
                    "extraction_summary": "llm",
                }

            service._process_z3_fact_extraction = mock_z3
            service._process_rule_question = mock_llm

            results = await service._process_policy_type(
                policy_type="TestPolicy",
                user_history="doc",
                config=config,
                extraction_results=None,
            )

            # All results have the same keys
            keys_0 = set(results[0].keys())
            for r in results[1:]:
                assert set(r.keys()) == keys_0
