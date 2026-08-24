# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for engine output format invariant.

Verifies that per-section results from both engines contain the required
fact extraction fields: policy_type, rule, extracted_facts, extraction_summary.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from idp_common.config.schema_constants import (
    X_AWS_IDP_RULE_ID,
    X_AWS_IDP_VALIDATION_ENGINE,
)

REQUIRED_FIELDS = {"policy_type", "rule", "extracted_facts", "extraction_summary"}


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
class TestOutputFormat:
    """All results must have the required fact extraction fields."""

    @pytest.mark.asyncio
    async def test_all_results_have_required_fields(self):
        rules = [
            {"description": "z3 rule", "engine": "z3", "rule_id": "r1"},
            {"description": "llm rule", "engine": "llm", "rule_id": None},
            {"description": "default engine", "engine": None, "rule_id": None},
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
                    "extracted_facts": [
                        {"fact": "x", "citation": "1", "relevance": "r"}
                    ],
                    "extraction_summary": "z3",
                }

            async def mock_llm(
                rule, user_history, policy_type, config, extraction_results=None
            ):
                return {
                    "policy_type": policy_type,
                    "rule": rule,
                    "extracted_facts": [
                        {"fact": "y", "citation": "2", "relevance": "r"}
                    ],
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

            for result in results:
                for field in REQUIRED_FIELDS:
                    assert field in result, (
                        f"Missing field '{field}' in result: {list(result.keys())}"
                    )

    @pytest.mark.asyncio
    async def test_llm_engine_returns_valid_fact_extraction(self):
        """Test _process_rule_question returns proper format."""
        with patch(
            "idp_common.rule_validation.service.RuleValidationService.__init__",
            return_value=None,
        ):
            from idp_common.rule_validation.service import RuleValidationService

            service = RuleValidationService.__new__(RuleValidationService)
            service.config = MagicMock()
            service.config.rule_validation.semaphore = 5
            service._semaphore = asyncio.Semaphore(5)
            service.token_metrics = {}
            service.metrics_lock = asyncio.Lock()

            mock_response_dict = {
                "extracted_facts": [
                    {"fact": "test", "citation": "1", "relevance": "r"}
                ],
                "extraction_summary": "Found facts.",
            }

            async def mock_invoke(**kwargs):
                return {
                    "output": {
                        "message": {
                            "content": [
                                {
                                    "text": f"<response>{json.dumps(mock_response_dict)}</response>"
                                }
                            ]
                        }
                    },
                    "metering": {},
                }

            service._invoke_model_async = mock_invoke
            service._prepare_prompt = MagicMock(return_value="prompt")

            config = {
                "rule_validation": {
                    "fact_extraction": {
                        "model": "test-model",
                        "system_prompt": "sys",
                        "task_prompt": "{DOCUMENT_TEXT} {rule} {EXTRACTION_RESULTS} {recommendation_options} {policy_type}",
                        "temperature": 0.0,
                        "top_k": 50,
                        "top_p": 0.9,
                        "max_tokens": 1024,
                    },
                    "recommendation_options": "Pass, Fail",
                },
            }

            with patch(
                "idp_common.rule_validation.service.bedrock.extract_text_from_response",
                return_value=f"<response>{json.dumps(mock_response_dict)}</response>",
            ):
                result = await service._process_rule_question(
                    rule="test rule",
                    user_history="doc",
                    policy_type="TestPolicy",
                    config=config,
                )

            for field in REQUIRED_FIELDS:
                assert field in result
