# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the orchestrator's Z3 cross-section rule validation path.

Tests _process_z3_cross_section_rules, _collect_facts_across_sections,
_run_z3_validation, and _get_rule_json_from_config.
"""

import pytest

from idp_common.config.schema_constants import (
    VALIDATION_ENGINE_Z3,
    X_AWS_IDP_RULE_ID,
    X_AWS_IDP_RULE_JSON,
    X_AWS_IDP_VALIDATION_ENGINE,
)


def _make_config(policy_type, rule_id, rule_description, rule_json=None):
    """Build a minimal config dict with one Z3 rule."""
    prop = {
        "type": "string",
        "description": rule_description,
        X_AWS_IDP_VALIDATION_ENGINE: VALIDATION_ENGINE_Z3,
        X_AWS_IDP_RULE_ID: rule_id,
    }
    if rule_json:
        prop[X_AWS_IDP_RULE_JSON] = rule_json
    return {
        "policy_classes": [
            {
                "x-aws-idp-policy-type": policy_type,
                "rule_properties": {"r1": prop},
            }
        ]
    }


SAMPLE_RULE_JSON = {
    "rule_id": "coverage_ratio",
    "version": "1.0",
    "description": "Coverage ratio check",
    "natural_language_rule": "coverage / income <= 20",
    "parameters": [
        {
            "name": "coverage",
            "type": "Real",
            "required": True,
            "description": "Coverage amount",
        },
        {
            "name": "income",
            "type": "Real",
            "required": True,
            "description": "Annual income",
        },
    ],
    "constraints": ["(> income 0)", "(<= (/ coverage income) 20)"],
    "path_mappings": [],
    "metadata": {},
}


@pytest.mark.unit
class TestCollectFactsAcrossSections:
    def test_collects_from_multiple_responses(self):
        from idp_common.rule_validation.orchestrator import (
            RuleValidationOrchestratorService,
        )

        service = RuleValidationOrchestratorService.__new__(
            RuleValidationOrchestratorService
        )
        responses = [
            {
                "extracted_facts": [
                    {"fact": "coverage = 500000", "citation": "1", "relevance": "r"}
                ]
            },
            {
                "extracted_facts": [
                    {"fact": "income = 85000", "citation": "3", "relevance": "r"}
                ]
            },
        ]
        facts = service._collect_facts_across_sections(responses)
        assert len(facts) == 2
        assert facts[0]["fact"] == "coverage = 500000"
        assert facts[1]["fact"] == "income = 85000"

    def test_handles_empty_facts(self):
        from idp_common.rule_validation.orchestrator import (
            RuleValidationOrchestratorService,
        )

        service = RuleValidationOrchestratorService.__new__(
            RuleValidationOrchestratorService
        )
        responses = [{"extracted_facts": []}, {"other_field": "x"}]
        facts = service._collect_facts_across_sections(responses)
        assert facts == []


@pytest.mark.unit
class TestGetRuleJsonFromConfig:
    def test_finds_rule_json_by_id_and_policy_type(self):
        from idp_common.rule_validation.orchestrator import (
            RuleValidationOrchestratorService,
        )

        service = RuleValidationOrchestratorService.__new__(
            RuleValidationOrchestratorService
        )
        config = _make_config("PolicyA", "rule_1", "test rule", SAMPLE_RULE_JSON)
        result = service._get_rule_json_from_config("rule_1", config, "PolicyA")
        assert result == SAMPLE_RULE_JSON

    def test_returns_none_for_wrong_policy_type(self):
        from idp_common.rule_validation.orchestrator import (
            RuleValidationOrchestratorService,
        )

        service = RuleValidationOrchestratorService.__new__(
            RuleValidationOrchestratorService
        )
        config = _make_config("PolicyA", "rule_1", "test rule", SAMPLE_RULE_JSON)
        result = service._get_rule_json_from_config("rule_1", config, "PolicyB")
        assert result is None

    def test_returns_none_when_rule_json_missing(self):
        from idp_common.rule_validation.orchestrator import (
            RuleValidationOrchestratorService,
        )

        service = RuleValidationOrchestratorService.__new__(
            RuleValidationOrchestratorService
        )
        config = _make_config("PolicyA", "rule_1", "test rule", None)
        result = service._get_rule_json_from_config("rule_1", config, "PolicyA")
        assert result is None


@pytest.mark.unit
class TestRunZ3Validation:
    def test_pass_when_constraints_satisfied(self):
        from idp_common.config.models import IDPConfig
        from idp_common.rule_validation.orchestrator import (
            RuleValidationOrchestratorService,
        )

        service = RuleValidationOrchestratorService.__new__(
            RuleValidationOrchestratorService
        )
        service.config = IDPConfig()

        result = service._run_z3_validation(
            SAMPLE_RULE_JSON, {"coverage": 100000, "income": 85000}, ["1", "3"]
        )
        assert result["recommendation"] == "Pass"
        assert result["_z3_validated"] is True
        assert "1" in result["supporting_pages"]

    def test_fail_when_constraints_violated(self):
        from idp_common.config.models import IDPConfig
        from idp_common.rule_validation.orchestrator import (
            RuleValidationOrchestratorService,
        )

        service = RuleValidationOrchestratorService.__new__(
            RuleValidationOrchestratorService
        )
        service.config = IDPConfig()

        # coverage/income = 2000000/85000 ≈ 23.5 > 20
        result = service._run_z3_validation(
            SAMPLE_RULE_JSON, {"coverage": 2000000, "income": 85000}, ["1"]
        )
        assert result["recommendation"] == "Fail"
        assert result["_z3_validated"] is True


@pytest.mark.unit
class TestProcessZ3CrossSectionRules:
    @pytest.mark.asyncio
    async def test_z3_rule_produces_verdict(self):
        from idp_common.config.models import IDPConfig
        from idp_common.rule_validation.orchestrator import (
            RuleValidationOrchestratorService,
        )

        service = RuleValidationOrchestratorService.__new__(
            RuleValidationOrchestratorService
        )
        service.config = IDPConfig()
        service._semaphore = None
        service.semaphore_limit = 5
        service.token_metrics = {}

        config = _make_config(
            "InsuranceUW", "coverage_ratio", "coverage / income <= 20", SAMPLE_RULE_JSON
        )
        all_responses = {
            "InsuranceUW": [
                {
                    "policy_type": "InsuranceUW",
                    "rule": "coverage / income <= 20",
                    "extracted_facts": [
                        {
                            "fact": "coverage = 500000",
                            "citation": "1",
                            "relevance": "r",
                        },
                        {"fact": "income = 85000", "citation": "3", "relevance": "r"},
                    ],
                    "extraction_summary": "Found values",
                }
            ]
        }

        # Mock the LLM value extraction to return typed values
        async def mock_extract(rule_json_data, all_facts, rule_description):
            return {"coverage": 500000.0, "income": 85000.0}

        service._extract_z3_values_from_facts = mock_extract

        result = await service._process_z3_cross_section_rules(all_responses, config)

        # Should have replaced the fact extraction response with a Z3 verdict
        assert len(result["InsuranceUW"]) == 1
        verdict = result["InsuranceUW"][0]
        assert verdict["_z3_validated"] is True
        assert verdict["recommendation"] == "Pass"

    @pytest.mark.asyncio
    async def test_missing_rule_json_returns_information_not_found(self):
        from idp_common.config.models import IDPConfig
        from idp_common.rule_validation.orchestrator import (
            RuleValidationOrchestratorService,
        )

        service = RuleValidationOrchestratorService.__new__(
            RuleValidationOrchestratorService
        )
        service.config = IDPConfig()
        service._semaphore = None
        service.semaphore_limit = 5
        service.token_metrics = {}

        # Config has Z3 rule but NO rule_json
        config = _make_config(
            "InsuranceUW", "coverage_ratio", "coverage / income <= 20", None
        )
        all_responses = {
            "InsuranceUW": [
                {
                    "policy_type": "InsuranceUW",
                    "rule": "coverage / income <= 20",
                    "extracted_facts": [
                        {"fact": "x", "citation": "1", "relevance": "r"}
                    ],
                    "extraction_summary": "Found",
                }
            ]
        }

        result = await service._process_z3_cross_section_rules(all_responses, config)
        verdict = result["InsuranceUW"][0]
        assert verdict["recommendation"] == "Information Not Found"
        assert "RuleJSON" in verdict["reasoning"]
