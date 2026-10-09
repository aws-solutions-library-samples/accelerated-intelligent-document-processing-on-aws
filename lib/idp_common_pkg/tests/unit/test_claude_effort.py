# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Claude reasoning-effort capability detection.

`output_config.effort` is accepted on Sonnet 5 / Sonnet 4.6 / Opus 4.5-4.8 /
Fable 5 (verified live on Bedrock Converse to change output tokens) but REJECTED
(400) on Sonnet 4.5 and Haiku 4.5. GPT-5.x uses the OpenAI Responses
`reasoning.effort` control instead, not this Converse path.
"""

import pytest

from idp_common.bedrock.client import CLAUDE_EFFORT_LEVELS, is_claude_effort_model


@pytest.mark.unit
class TestIsClaudeEffortModel:
    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-sonnet-5",
            "us.anthropic.claude-sonnet-5:1m",
            "global.anthropic.claude-sonnet-5",
            "eu.anthropic.claude-sonnet-5:1m",
            "us.anthropic.claude-sonnet-4-6",
            "us.anthropic.claude-opus-4-5-20251101-v1:0",
            "us.anthropic.claude-opus-4-6-v1",
            "us.anthropic.claude-opus-4-6-v1:1m",
            "us.anthropic.claude-opus-4-7",
            "us.anthropic.claude-opus-4-8:1m",
            "us.anthropic.claude-opus-5",
            "eu.anthropic.claude-opus-5:1m",
            "global.anthropic.claude-opus-5",
            "us.anthropic.claude-fable-5",
            # The first Haiku that takes effort. Its sibling is in the rejecting
            # list below, so the two together are what pins that "Haiku" is not the
            # rule — widening the set to a shared stem breaks the other assertion.
            "us.anthropic.claude-haiku-5-5",
            "eu.anthropic.claude-haiku-5-5",
            "au.anthropic.claude-haiku-5-5",
            "jp.anthropic.claude-haiku-5-5",
        ],
    )
    def test_effort_capable_models(self, model_id):
        assert is_claude_effort_model(model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",  # 4.5 rejects effort
            # Haiku 4.5 rejects effort; Haiku 5.5 accepts it (above). The family
            # name decides nothing.
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.anthropic.claude-3-7-sonnet-20250219-v1:0",  # 3.x
            "us.amazon.nova-lite-v1:0",  # nova
            "us.amazon.nova-2-lite-v1:0",
            "openai.gpt-5.5",  # OpenAI Responses path, not this one
            "",
        ],
    )
    def test_non_effort_models(self, model_id):
        assert is_claude_effort_model(model_id) is False

    def test_effort_levels(self):
        # Claude levels are a superset of OpenAI's (which also allows "minimal").
        assert CLAUDE_EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")
