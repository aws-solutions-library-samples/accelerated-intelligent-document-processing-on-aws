# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the agents common configuration module.
"""

import os
from unittest.mock import patch

import pytest

from idp_common.agents.common.config import (
    get_environment_config,
    validate_aws_credentials,
)


@pytest.mark.unit
class TestGetEnvironmentConfig:
    """Tests for the get_environment_config function."""

    def test_get_basic_config(self):
        """Test getting basic configuration without required keys."""
        with patch.dict(os.environ, {"AWS_REGION": "us-west-2"}, clear=True):
            config = get_environment_config()
            assert config["aws_region"] == "us-west-2"

    def test_get_config_with_default_region(self):
        """Test getting configuration with default AWS region."""
        with patch.dict(os.environ, {}, clear=True):
            config = get_environment_config()
            assert config["aws_region"] == "us-east-1"

    def test_get_config_with_required_keys(self):
        """Test getting configuration with required keys."""
        env_vars = {
            "AWS_REGION": "us-west-2",
            "TEST_KEY": "test_value",
            "ANOTHER_KEY": "another_value",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = get_environment_config(["TEST_KEY", "ANOTHER_KEY"])
            assert config["aws_region"] == "us-west-2"
            assert config["test_key"] == "test_value"
            assert config["another_key"] == "another_value"

    def test_missing_required_keys_raises_error(self):
        """Test that missing required keys raise ValueError."""
        with patch.dict(os.environ, {"AWS_REGION": "us-west-2"}, clear=True):
            with pytest.raises(ValueError) as exc_info:
                get_environment_config(["MISSING_KEY", "ANOTHER_MISSING_KEY"])

            assert "Missing required environment variables" in str(exc_info.value)
            assert "MISSING_KEY" in str(exc_info.value)
            assert "ANOTHER_MISSING_KEY" in str(exc_info.value)

    def test_partial_missing_required_keys(self):
        """Test that partially missing required keys raise ValueError."""
        env_vars = {"AWS_REGION": "us-west-2", "PRESENT_KEY": "present_value"}
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(ValueError) as exc_info:
                get_environment_config(["PRESENT_KEY", "MISSING_KEY"])

            assert "Missing required environment variables: MISSING_KEY" in str(
                exc_info.value
            )


@pytest.mark.unit
class TestValidateAwsCredentials:
    """Tests for the validate_aws_credentials function."""

    def test_explicit_credentials_available(self):
        """Test validation when explicit AWS credentials are available."""
        env_vars = {
            "AWS_ACCESS_KEY_ID": "test_key",
            "AWS_SECRET_ACCESS_KEY": "test_secret",  # nosec B105 - dummy test credential  # pragma: allowlist secret
        }
        with patch.dict(os.environ, env_vars, clear=True):
            assert validate_aws_credentials() is True

    def test_lambda_environment(self):
        """Test validation in Lambda environment."""
        env_vars = {"AWS_LAMBDA_FUNCTION_NAME": "test_function"}
        with patch.dict(os.environ, env_vars, clear=True):
            assert validate_aws_credentials() is True

    def test_default_credential_chain(self):
        """Test validation using default credential chain."""
        with patch.dict(os.environ, {}, clear=True):
            # Should return True assuming default credential chain
            assert validate_aws_credentials() is True
