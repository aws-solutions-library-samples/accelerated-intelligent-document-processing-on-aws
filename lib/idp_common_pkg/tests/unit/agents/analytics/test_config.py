# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the analytics configuration module.
"""

# ruff: noqa: E402, I001
# The above line disables E402 (module level import not at top of file) and I001 (import block sorting) for this file

import os
from unittest.mock import patch

import pytest

# `strands` and `bedrock_agentcore` are stubbed by tests/conftest.py, and ONLY
# when genuinely absent. This module used to force a MagicMock into
# `sys.modules` here instead, unconditionally and without ever restoring it —
# which replaced a real installed `strands` for every test module imported after
# this one. See the note in tests/conftest.py.

from idp_common.agents.analytics.config import (
    get_analytics_config,
)
from idp_common.agents.common.config import load_result_format_description


@pytest.mark.unit
class TestGetAnalyticsConfig:
    """Tests for the get_analytics_config function."""

    def test_get_analytics_config_success(self):
        """Test successful analytics configuration loading."""
        env_vars = {
            "AWS_REGION": "us-west-2",
            "ATHENA_DATABASE": "test_database",
            "ATHENA_OUTPUT_LOCATION": "s3://test-bucket/results/",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = get_analytics_config()

            assert config["aws_region"] == "us-west-2"
            assert config["athena_database"] == "test_database"
            assert config["athena_output_location"] == "s3://test-bucket/results/"
            assert config["max_polling_attempts"] == 30
            assert config["query_timeout_seconds"] == 300

    def test_missing_required_config_raises_error(self):
        """Test that missing required configuration raises ValueError."""
        env_vars = {
            "AWS_REGION": "us-west-2",
            "ATHENA_DATABASE": "test_database",
            # Missing ATHENA_OUTPUT_LOCATION
        }
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(ValueError) as exc_info:
                get_analytics_config()

            assert "Missing required environment variables" in str(exc_info.value)
            assert "ATHENA_OUTPUT_LOCATION" in str(exc_info.value)

    def test_all_missing_required_config(self):
        """Test that all missing required configuration raises ValueError."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError) as exc_info:
                get_analytics_config()

            error_message = str(exc_info.value)
            assert "Missing required environment variables" in error_message
            assert "ATHENA_DATABASE" in error_message
            assert "ATHENA_OUTPUT_LOCATION" in error_message


@pytest.mark.unit
class TestLoadResultFormatDescription:
    """Tests for the load_result_format_description function."""

    def test_load_result_format_description_success(self):
        """Test successful loading of result format description."""
        result = load_result_format_description()
        assert isinstance(result, str)
        assert len(result) > 0
        assert "Result Format Description" in result

    def test_load_result_format_description_contains_expected_content(self):
        """Test that result format description contains expected format information."""
        result = load_result_format_description()
        assert "responseType" in result
        assert "JSON object" in result
