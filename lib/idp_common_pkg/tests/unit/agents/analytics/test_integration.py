# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Integration tests for the analytics agent functionality.
"""

# ruff: noqa: E402, I001
# The above line disables E402 (module level import not at top of file) and I001 (import block sorting) for this file

from unittest.mock import MagicMock, patch

import pytest

# `strands` and `bedrock_agentcore` are stubbed by tests/conftest.py, and ONLY
# when genuinely absent. This module used to force MagicMocks into `sys.modules`
# here instead, unconditionally and without ever restoring them — which replaced
# a real installed `strands` for every test module imported after this one, so
# whether the agentic tests ran against the real library depended on collection
# order. See the note in tests/conftest.py.

from idp_common.agents.analytics import (
    get_analytics_config,
)


@pytest.mark.unit
class TestAnalyticsIntegration:
    """Integration tests for analytics agent functionality."""

    @patch.dict(
        "os.environ",
        {
            "ATHENA_DATABASE": "test_db",
            "ATHENA_OUTPUT_LOCATION": "s3://test-bucket/results/",
            "AWS_REGION": "us-east-1",
        },
    )
    @patch("idp_common.agents.analytics.agent.strands.Agent")
    @patch("boto3.Session")
    def test_end_to_end_agent_creation(self, mock_session, mock_agent_class):
        """Test end-to-end agent creation with configuration."""
        mock_agent_instance = MagicMock()
        mock_agent_class.return_value = mock_agent_instance
        mock_session_instance = MagicMock()
        mock_session.return_value = mock_session_instance

        # Get configuration
        config = get_analytics_config()

        # Verify configuration was loaded correctly
        assert config["athena_database"] == "test_db"
        assert config["athena_output_location"] == "s3://test-bucket/results/"
        assert config["aws_region"] == "us-east-1"

    @patch.dict(
        "os.environ",
        {
            "ATHENA_DATABASE": "test_db",
            "ATHENA_OUTPUT_LOCATION": "s3://test-bucket/results/",
            "AWS_REGION": "us-east-1",
        },
    )
    def test_configuration_validation(self):
        """Test that configuration validation works correctly."""
        config = get_analytics_config()

        # Verify all required configuration is present
        assert "athena_database" in config
        assert "athena_output_location" in config
        assert "aws_region" in config

        # Verify default values are set
        assert config["max_polling_attempts"] == 30
        assert config["query_timeout_seconds"] == 300

    def test_missing_configuration_raises_error(self):
        """Test that missing configuration raises appropriate error."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError) as exc_info:
                get_analytics_config()

            assert "Missing required environment variables" in str(exc_info.value)
