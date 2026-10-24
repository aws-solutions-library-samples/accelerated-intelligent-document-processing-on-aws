# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for Error Analyzer configuration.
"""

import pytest
from pydantic import ValidationError

from idp_common.agents.error_analyzer.config import get_aws_service_capabilities
from idp_common.config.models import (
    ErrorAnalyzerConfig,
    ErrorAnalyzerParameters,
    IDPConfig,
)


@pytest.mark.unit
class TestErrorAnalyzerConfig:
    """Test error analyzer configuration functions."""

    def test_default_error_patterns(self):
        """Test default error patterns in ErrorAnalyzerConfig."""
        config = ErrorAnalyzerConfig()

        assert isinstance(config.error_patterns, list)
        assert len(config.error_patterns) > 0
        assert "ERROR" in config.error_patterns
        assert "Exception" in config.error_patterns
        assert "Timeout" in config.error_patterns
        assert "ThrottlingException" in config.error_patterns

    def test_get_aws_service_capabilities(self):
        """Test AWS service capabilities are returned."""
        capabilities = get_aws_service_capabilities()

        assert isinstance(capabilities, dict)
        assert "cloudwatch_logs" in capabilities
        assert "dynamodb" in capabilities
        assert "benefits" in capabilities

        # Check CloudWatch capabilities
        cw_caps = capabilities["cloudwatch_logs"]
        assert "description" in cw_caps
        assert "capabilities" in cw_caps
        assert "search_log_events" in cw_caps["capabilities"]
        assert "implementation" in cw_caps

        # Check DynamoDB capabilities
        db_caps = capabilities["dynamodb"]
        assert "description" in db_caps
        assert "capabilities" in db_caps
        assert "scan_table" in db_caps["capabilities"]
        assert "implementation" in db_caps

    def test_error_analyzer_parameters_defaults(self):
        """Test ErrorAnalyzerParameters default values."""
        params = ErrorAnalyzerParameters()

        assert params.max_log_events == 5
        assert params.time_range_hours_default == 24
        assert params.max_log_message_length == 400
        assert params.max_events_per_log_group == 5
        assert params.max_log_groups == 20
        # MR-02: updated default from 3 to 50
        assert params.max_stepfunction_timeline_events == 50
        assert params.max_stepfunction_error_length == 400
        assert params.xray_slow_segment_threshold_ms == 5000
        assert params.xray_error_rate_threshold == 0.05
        assert params.xray_response_time_threshold_ms == 10000
        # MR-02: new parameters
        assert params.xray_analysis_hours == 3
        assert params.settings_cache_ttl_seconds == 300

    def test_error_analyzer_config_defaults(self):
        """Test ErrorAnalyzerConfig default values."""
        config = ErrorAnalyzerConfig()

        # MR-02 refinement: updated default model to claude-sonnet-4-6
        assert config.model_id == "us.anthropic.claude-sonnet-4-6"
        # MR-02 refinement: lookback_hours replaces time_range_hours_default at top level
        assert config.lookback_hours == 24
        assert isinstance(config.system_prompt, str)
        assert len(config.system_prompt) > 0
        assert isinstance(config.parameters, ErrorAnalyzerParameters)
        assert config.parameters.max_log_events == 5

    @pytest.mark.parametrize("stored", [None, "", "   "])
    def test_an_empty_stored_lookback_hours_loads_as_the_default(self, stored):
        """A stored profile may carry ``lookback_hours: null`` (a YAML key with no
        value, or a JSON null) or an empty string. Loading it must fall back to the
        field default as the other required-int fields do, not raise: ``int(None)``
        is a TypeError, which pydantic does not convert, so the whole configuration
        failed to load."""
        config = IDPConfig.model_validate(
            {"agents": {"error_analyzer": {"lookback_hours": stored}}}
        )
        assert config.agents.error_analyzer is not None
        assert config.agents.error_analyzer.lookback_hours == 24

    def test_a_stored_lookback_hours_string_is_still_parsed(self):
        assert (
            ErrorAnalyzerConfig.model_validate({"lookback_hours": "12"}).lookback_hours
            == 12
        )

    def test_a_non_positive_lookback_hours_is_still_rejected(self):
        with pytest.raises(ValidationError):
            ErrorAnalyzerConfig.model_validate({"lookback_hours": 0})

    def test_get_error_analyzer_model_id_uses_error_analyzer_not_chat_companion(self):
        """MR-02 bug fix: model ID must come from error_analyzer, not chat_companion."""
        from unittest.mock import MagicMock, patch

        from idp_common.agents.error_analyzer.config import get_error_analyzer_model_id

        mock_config = MagicMock()
        mock_config.agents.error_analyzer.model_id = (
            "us.anthropic.claude-sonnet-4-20250514-v1:0"
        )
        mock_config.agents.chat_companion.model_id = (
            "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        )

        # get_config is a lazy import inside the function body: `from ...config import get_config`.
        # Patching idp_common.config.get_config ensures the lazy import picks up the mock.
        with patch("idp_common.config.get_config", return_value=mock_config):
            model_id = get_error_analyzer_model_id()

        # Must return error_analyzer model, NOT chat_companion model
        assert model_id == "us.anthropic.claude-sonnet-4-20250514-v1:0"
        assert model_id != "us.anthropic.claude-haiku-4-5-20251001-v1:0"
