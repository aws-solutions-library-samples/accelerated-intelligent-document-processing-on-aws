# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the agents common configuration module.
"""

import builtins
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from idp_common.agents.common import config as common_config
from idp_common.agents.common.config import (
    AGENT_MODEL_LAST_RESORT,
    DEFAULT_AGENT_MODEL_ID,
    get_environment_config,
    portable_profile,
    validate_aws_credentials,
)
from idp_common.agents.error_analyzer import config as error_analyzer_config
from idp_common.config.models import ChatCompanionConfig


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


_PRICING = Path(__file__).resolve().parents[5] / "config_library" / "pricing.yaml"


@pytest.mark.unit
class TestDefaultAgentModelId:
    """The fallback an agent uses when its configured model cannot be read."""

    def test_the_fallback_can_be_invoked_from_any_region(self):
        """A ``us.`` profile cannot be invoked from an EU or Canadian stack, and
        nothing remaps a code default the way a stored configuration is remapped."""
        assert DEFAULT_AGENT_MODEL_ID.startswith("global."), DEFAULT_AGENT_MODEL_ID

    def test_the_fallback_is_the_shipped_chat_companion_model(self):
        shipped = ChatCompanionConfig().model_id
        assert DEFAULT_AGENT_MODEL_ID.partition(".")[2] == shipped.partition(".")[2]

    def test_the_fallback_is_an_offered_model(self):
        """The ``global.`` profile has to exist; the pricing table lists the IDs
        the solution offers, so a profile absent from it is not one it can run."""
        names = {
            entry.get("name")
            for entry in yaml.safe_load(_PRICING.read_text())["pricing"]
        }
        assert f"bedrock/{DEFAULT_AGENT_MODEL_ID}" in names


def _without_config_models(monkeypatch):
    """Make the configuration models unimportable, as on the error path."""
    real_import = builtins.__import__
    models = {"ChatCompanionConfig", "ErrorAnalyzerConfig"}

    def refuse(name, globals=None, locals=None, fromlist=(), level=0):
        if models & set(fromlist or ()):
            raise ImportError("simulated: configuration cannot be loaded")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", refuse)


@pytest.mark.unit
class TestLastResortModels:
    """The literal each fallback returns when even the field default is unreadable.

    The literal equals the derived value by design, so "the call returned the
    literal" cannot show the import failure was reached; each test swaps the literal
    for a sentinel first, then checks the real literal separately.
    """

    def test_agent_last_resort_is_reached(self, monkeypatch):
        monkeypatch.setattr(common_config, "AGENT_MODEL_LAST_RESORT", "SENTINEL")
        _without_config_models(monkeypatch)
        assert common_config._default_agent_model_id() == "SENTINEL"

    def test_agent_last_resort_is_the_derived_model(self):
        assert AGENT_MODEL_LAST_RESORT == DEFAULT_AGENT_MODEL_ID

    def test_error_analyzer_last_resort_is_reached(self, monkeypatch):
        monkeypatch.setattr(
            error_analyzer_config, "ERROR_ANALYZER_MODEL_LAST_RESORT", "SENTINEL"
        )
        _without_config_models(monkeypatch)
        assert error_analyzer_config.default_error_analyzer_model_id() == "SENTINEL"

    def test_error_analyzer_last_resort_is_the_derived_model(self):
        assert (
            error_analyzer_config.ERROR_ANALYZER_MODEL_LAST_RESORT
            == error_analyzer_config.default_error_analyzer_model_id()
        )

    def test_error_analyzer_fallback_is_the_shipped_model_through_global(self):
        from idp_common.config.models import ErrorAnalyzerConfig

        shipped = ErrorAnalyzerConfig().model_id
        fallback = error_analyzer_config.default_error_analyzer_model_id()
        assert fallback.startswith("global."), fallback
        assert fallback.partition(".")[2] == shipped.partition(".")[2]
        names = {
            entry.get("name")
            for entry in yaml.safe_load(_PRICING.read_text())["pricing"]
        }
        assert f"bedrock/{fallback}" in names

    def test_error_analyzer_uses_it_when_configuration_cannot_be_read(self):
        with patch(
            "idp_common.config.get_config", side_effect=RuntimeError("no table")
        ):
            model_id = error_analyzer_config.get_error_analyzer_model_id()
        assert model_id == error_analyzer_config.default_error_analyzer_model_id()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("us.anthropic.claude-sonnet-4-6", "global.anthropic.claude-sonnet-4-6"),
        ("eu.amazon.nova-pro-v1:0", "global.amazon.nova-pro-v1:0"),
        ("global.anthropic.claude-sonnet-4-6", "global.anthropic.claude-sonnet-4-6"),
        ("amazon.nova-pro-v1:0", "amazon.nova-pro-v1:0"),
    ],
)
def test_portable_profile(model_id, expected):
    assert portable_profile(model_id) == expected
