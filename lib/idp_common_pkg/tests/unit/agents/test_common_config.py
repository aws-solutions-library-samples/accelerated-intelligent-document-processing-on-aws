# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the agents common configuration module.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from idp_common.agents.common import config as common_config
from idp_common.agents.common.config import (
    get_environment_config,
    validate_aws_credentials,
)
from idp_common.agents.error_analyzer import config as error_analyzer_config
from idp_common.agents.quick_start import agent as quick_start_agent
from idp_common.config.models import ChatCompanionConfig, ErrorAnalyzerConfig


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

#: Each agent fallback, called with the process environment as it stands.
_FALLBACKS = {
    "agent": common_config._default_agent_model_id,
    "error_analyzer": error_analyzer_config.default_error_analyzer_model_id,
    "quick_start": quick_start_agent.default_model_id,
}

#: The model each fallback serves: the shipped configuration's field default.
_SHIPPED = {
    "agent": ChatCompanionConfig().model_id,
    "error_analyzer": ErrorAnalyzerConfig().model_id,
    "quick_start": ErrorAnalyzerConfig().model_id,
}


def _pricing_names() -> set:
    return {
        entry.get("name") for entry in yaml.safe_load(_PRICING.read_text())["pricing"]
    }


def _region_env(region):
    env = {k: v for k, v in os.environ.items() if k not in _REGION_VARS}
    if region:
        env["AWS_REGION"] = region
    return env


_REGION_VARS = ("AWS_REGION", "AWS_DEFAULT_REGION")


_HAIKU = "anthropic.claude-haiku-4-5-20251001-v1:0"
_SONNET = "anthropic.claude-sonnet-4-6"

#: What ``update_configuration``'s ``get_model_mapping`` makes of each shipped
#: model, measured per Region type: EU moves to ``eu.``, and Canada keeps the US
#: profile for both (neither is in ``CA_UNAVAILABLE_MODELS``).
#: ``src/lambda/update_configuration/test_eu_region.py`` holds
#: ``regional_models`` to the swap tables directly; these rows pin the
#: agent-facing result so a fallback that stopped translating fails here.
_EXPECTED = {
    "us-east-1": {"agent": f"us.{_HAIKU}", "sonnet": f"us.{_SONNET}"},
    "us-west-2": {"agent": f"us.{_HAIKU}", "sonnet": f"us.{_SONNET}"},
    "eu-central-1": {"agent": f"eu.{_HAIKU}", "sonnet": f"eu.{_SONNET}"},
    "eu-west-1": {"agent": f"eu.{_HAIKU}", "sonnet": f"eu.{_SONNET}"},
    "ca-central-1": {"agent": f"us.{_HAIKU}", "sonnet": f"us.{_SONNET}"},
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_FALLBACKS))
@pytest.mark.parametrize("region", sorted(_EXPECTED))
def test_each_fallback_is_what_the_swap_gives_the_stacks_region(name, region):
    """A ``us.`` fallback cannot be invoked from an EU stack, and a ``global.`` one
    can route an EU stack's requests outside the EU. The fallback has to be what
    the deploy-time swap makes of the shipped model for the same Region, so a stack
    that fell back runs the model a stack that read its configuration runs."""
    with patch.dict(os.environ, _region_env(region), clear=True):
        fallback = _FALLBACKS[name]()
    expected = _EXPECTED[region]["agent" if name == "agent" else "sonnet"]
    assert fallback == expected
    assert fallback.partition(".")[2] == _SHIPPED[name].partition(".")[2]
    assert f"bedrock/{fallback}" in _pricing_names()


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_FALLBACKS))
def test_aws_default_region_is_read_when_aws_region_is_unset(name):
    """A local SDK or CLI session often carries only ``AWS_DEFAULT_REGION``."""
    env = _region_env(None)
    env["AWS_DEFAULT_REGION"] = "eu-west-1"
    with patch.dict(os.environ, env, clear=True):
        assert _FALLBACKS[name]().startswith("eu."), name


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_FALLBACKS))
def test_aws_region_wins_over_aws_default_region(name):
    env = _region_env("eu-west-1")
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    with patch.dict(os.environ, env, clear=True):
        assert _FALLBACKS[name]().startswith("eu."), name


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_FALLBACKS))
def test_no_region_gives_the_shipped_model(name):
    """With no Region to translate for, the fallback is the shipped value, which
    is what the swap leaves in a US stack."""
    with patch.dict(os.environ, _region_env(None), clear=True):
        assert _FALLBACKS[name]() == _SHIPPED[name]


@pytest.mark.unit
def test_the_translated_ids_are_the_shipped_defaults():
    """The fallbacks name their model through ``regional_models`` constants; those
    must stay the shipped field defaults, or retiring a model in the configuration
    would leave the fallback naming the old one."""
    from idp_common import regional_models

    assert regional_models.AGENT_MODEL_ID == ChatCompanionConfig().model_id
    assert regional_models.SONNET_4_6_MODEL_ID == ErrorAnalyzerConfig().model_id


@pytest.mark.unit
def test_default_agent_model_id_is_the_import_time_fallback():
    """``DEFAULT_AGENT_MODEL_ID`` is what the other agent modules import."""
    assert (
        common_config.DEFAULT_AGENT_MODEL_ID == common_config._default_agent_model_id()
    )


@pytest.mark.unit
def test_the_quick_start_agent_uses_the_fallback_when_given_no_model(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    with (
        patch.object(quick_start_agent, "create_strands_bedrock_model") as make_model,
        patch.object(quick_start_agent.strands, "Agent"),
        patch.object(quick_start_agent, "with_cost_hook") as cost_hook,
    ):
        quick_start_agent.create_quick_start_agent(session=object())
    assert make_model.call_args.kwargs["model_id"] == "eu.anthropic.claude-sonnet-4-6"
    assert cost_hook.call_args.args[2] == "eu.anthropic.claude-sonnet-4-6"


@pytest.mark.unit
def test_the_quick_start_agent_keeps_a_model_it_is_given(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    with (
        patch.object(quick_start_agent, "create_strands_bedrock_model") as make_model,
        patch.object(quick_start_agent.strands, "Agent"),
    ):
        quick_start_agent.create_quick_start_agent(session=object(), model_id="given")
    assert make_model.call_args.kwargs["model_id"] == "given"


@pytest.mark.unit
def test_error_analyzer_uses_the_fallback_when_configuration_cannot_be_read():
    with patch("idp_common.config.get_config", side_effect=RuntimeError("no table")):
        model_id = error_analyzer_config.get_error_analyzer_model_id()
    assert model_id == error_analyzer_config.default_error_analyzer_model_id()
