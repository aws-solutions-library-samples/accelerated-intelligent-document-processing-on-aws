# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for engine routing correctness in RuleValidationService._process_policy_type.

Verifies that Z3 rules (with rule_id) go to _process_z3_fact_extraction,
Z3 rules without rule_id return a config error, and LLM rules go to
_process_rule_question.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from idp_common.config.schema_constants import (
    X_AWS_IDP_RULE_ID,
    X_AWS_IDP_VALIDATION_ENGINE,
)


def _build_config(rules):
    """Build config dict from a list of rule dicts."""
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
class TestEngineRouting:
    """Verify rules are dispatched to the correct engine handler."""

    @pytest.mark.asyncio
    async def test_z3_with_rule_id_routes_to_z3_fact_extraction(self):
        rules = [
            {
                "description": "coverage / income <= 20",
                "engine": "z3",
                "rule_id": "cov_ratio",
            },
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

            z3_calls = []

            async def mock_z3(
                rule_description,
                rule_id,
                policy_type,
                extraction_results,
                document_text,
                config,
                rule_json=None,
            ):
                z3_calls.append(rule_description)
                return {
                    "policy_type": policy_type,
                    "rule": rule_description,
                    "extracted_facts": [],
                    "extraction_summary": "z3",
                }

            service._process_z3_fact_extraction = mock_z3
            service._process_rule_question = MagicMock()

            results = await service._process_policy_type(
                policy_type="TestPolicy",
                user_history="doc text",
                config=config,
                extraction_results=None,
            )

            assert z3_calls == ["coverage / income <= 20"]
            assert len(results) == 1

    @pytest.mark.asyncio
    async def test_z3_without_rule_id_returns_config_error(self):
        rules = [
            {"description": "some z3 rule", "engine": "z3", "rule_id": None},
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

            results = await service._process_policy_type(
                policy_type="TestPolicy",
                user_history="doc text",
                config=config,
                extraction_results=None,
            )

            assert len(results) == 1
            assert "Z3 configuration error" in results[0].get("extraction_summary", "")

    @pytest.mark.asyncio
    async def test_llm_rules_route_to_process_rule_question(self):
        rules = [
            {"description": "must be signed", "engine": "llm", "rule_id": None},
            {"description": "date must be valid", "engine": None, "rule_id": None},
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

            llm_calls = []

            async def mock_llm(
                rule, user_history, policy_type, config, extraction_results=None
            ):
                llm_calls.append(rule)
                return {
                    "policy_type": policy_type,
                    "rule": rule,
                    "extracted_facts": [],
                    "extraction_summary": "llm",
                }

            service._process_rule_question = mock_llm
            service._process_z3_fact_extraction = MagicMock()

            results = await service._process_policy_type(
                policy_type="TestPolicy",
                user_history="doc text",
                config=config,
                extraction_results=None,
            )

            assert sorted(llm_calls) == sorted(["must be signed", "date must be valid"])
            assert len(results) == 2

    @pytest.mark.asyncio
    async def test_mixed_engines_no_cross_routing(self):
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

            z3_calls = []
            llm_calls = []

            async def mock_z3(
                rule_description,
                rule_id,
                policy_type,
                extraction_results,
                document_text,
                config,
                rule_json=None,
            ):
                z3_calls.append(rule_description)
                return {
                    "policy_type": policy_type,
                    "rule": rule_description,
                    "extracted_facts": [],
                    "extraction_summary": "z3",
                }

            async def mock_llm(
                rule, user_history, policy_type, config, extraction_results=None
            ):
                llm_calls.append(rule)
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
                user_history="doc text",
                config=config,
                extraction_results=None,
            )

            assert z3_calls == ["z3 rule"]
            assert llm_calls == ["llm rule"]
            assert len(results) == 2
            # No cross-routing
            assert set(z3_calls) & set(llm_calls) == set()
