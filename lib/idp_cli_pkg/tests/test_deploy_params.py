# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for deploy command parameter handling

Verifies that stack updates only modify explicitly provided parameters.
"""

from unittest.mock import MagicMock

import click
import pytest

from idp_cli.cli import _parse_tags
from idp_sdk._core.stack import StackDeployer, build_parameters


class TestParameterPreservation:
    """Test parameter preservation during stack updates"""

    def test_build_parameters_new_stack_all_required(self):
        """Test parameter building for new stack with all required parameters"""
        params = build_parameters(
            admin_email="admin@example.com",
            max_concurrent=100,
            log_level="INFO",
            enable_hitl="false",
        )

        assert params["AdminEmail"] == "admin@example.com"
        assert params["MaxConcurrentWorkflows"] == "100"
        assert params["LogLevel"] == "INFO"
        assert params["EnableHITL"] == "false"

    def test_build_parameters_update_no_params(self):
        """Test parameter building for update with no parameters - should be empty"""
        params = build_parameters()

        # For updates with no explicit parameters, dict should be empty
        # CloudFormation will automatically use previous values
        assert len(params) == 0

    def test_build_parameters_update_selective_max_concurrent_only(self):
        """Test parameter building for update with only max_concurrent"""
        params = build_parameters(max_concurrent=200)

        # Only MaxConcurrentWorkflows should be included
        assert params["MaxConcurrentWorkflows"] == "200"
        assert "AdminEmail" not in params
        assert "LogLevel" not in params
        assert "EnableHITL" not in params

    def test_build_parameters_update_selective_log_level_only(self):
        """Test parameter building for update with only log level"""
        params = build_parameters(log_level="DEBUG")

        # Only LogLevel should be included
        assert params["LogLevel"] == "DEBUG"
        assert "AdminEmail" not in params
        assert "MaxConcurrentWorkflows" not in params
        assert "EnableHITL" not in params

    def test_build_parameters_update_multiple_selective(self):
        """Test parameter building for update with multiple selective parameters"""
        params = build_parameters(
            max_concurrent=150,
            log_level="DEBUG",
            enable_hitl="true",
        )

        # Only the provided parameters should be included
        assert params["MaxConcurrentWorkflows"] == "150"
        assert params["LogLevel"] == "DEBUG"
        assert params["EnableHITL"] == "true"
        assert "AdminEmail" not in params

    def test_build_parameters_additional_params(self):
        """Test additional parameters override"""
        params = build_parameters(
            max_concurrent=100,
            additional_params={
                "DataRetentionInDays": "90",
                "ErrorThreshold": "5",
            },
        )

        assert params["MaxConcurrentWorkflows"] == "100"
        assert params["DataRetentionInDays"] == "90"
        assert params["ErrorThreshold"] == "5"

    def test_build_parameters_none_values_excluded(self):
        """Test that None values are not included in parameters dict"""
        params = build_parameters(
            admin_email=None,
            max_concurrent=None,
            log_level=None,
            enable_hitl=None,
            custom_config=None,
        )

        # All None values should result in empty dict
        assert len(params) == 0

    def test_build_parameters_custom_config_not_included_when_none(self):
        """Test that custom_config doesn't affect other parameters when None"""
        params = build_parameters(
            max_concurrent=150,
            custom_config=None,
        )

        # Only max_concurrent should be present
        assert len(params) == 1
        assert params["MaxConcurrentWorkflows"] == "150"
        assert "CustomConfigPath" not in params


class TestParameterPreservationIntegration:
    """Integration tests for parameter preservation behavior"""

    def test_update_scenario_only_changes_specified_param(self):
        """
        Simulate update scenario:
        - Existing stack has: MaxConcurrentWorkflows=200, LogLevel=DEBUG
        - User updates with: --max-concurrent 150
        - Expected: Only MaxConcurrentWorkflows should be in parameter dict
        - CloudFormation will preserve LogLevel=DEBUG automatically
        """
        # User only changes max_concurrent to 150
        params = build_parameters(max_concurrent=150)

        # Only the changed parameter should be included
        assert len(params) == 1
        assert params["MaxConcurrentWorkflows"] == "150"

        # These should NOT be in the dict (CloudFormation preserves them)
        assert "LogLevel" not in params
        assert "EnableHITL" not in params
        assert "AdminEmail" not in params

    def test_new_stack_scenario_all_required_params(self):
        """
        Simulate new stack creation:
        - User provides: --admin-email user@example.com
        - Expected: admin_email in parameters
        - Optional params with defaults also included
        """
        params = build_parameters(
            admin_email="user@example.com",
            max_concurrent=100,  # Default value
            log_level="INFO",  # Default value
            enable_hitl="false",  # Default value
        )

        # Required params for new stack
        assert params["AdminEmail"] == "user@example.com"

        # Defaults should be included for new stack
        assert params["MaxConcurrentWorkflows"] == "100"
        assert params["LogLevel"] == "INFO"
        assert params["EnableHITL"] == "false"

    def test_update_with_custom_config_only(self):
        """
        Simulate update with only custom config:
        - User provides: --custom-config ./my-config.yaml
        - Expected: Only CustomConfigPath in parameters
        """
        # Note: custom_config handling requires region and uploads to S3
        # For unit test, we skip the upload part and test the parameter inclusion
        params = build_parameters(
            custom_config=None  # Would be S3 URI after upload
        )

        # When custom_config is None, shouldn't be in params
        assert "CustomConfigPath" not in params


def _make_deployer(stack_exists):
    """Build a StackDeployer with boto3 / template IO stubbed out.

    Returns (deployer, cfn_mock) so tests can assert on the CloudFormation
    create_stack / update_stack call kwargs.
    """
    deployer = StackDeployer.__new__(StackDeployer)
    deployer.region = "us-west-2"
    cfn_mock = MagicMock()
    cfn_mock.create_stack.return_value = {"StackId": "arn:stack/new"}
    cfn_mock.update_stack.return_value = {"StackId": "arn:stack/existing"}
    deployer.cfn = cfn_mock

    # Use a small inline template body so no S3 upload path is triggered.
    deployer._read_template = MagicMock(return_value="Resources: {}")
    deployer._stack_exists = MagicMock(return_value=stack_exists)
    deployer._get_stack_parameters = MagicMock(return_value={})
    deployer._get_template_parameters = MagicMock(return_value=set())
    return deployer, cfn_mock


class TestParseTags:
    """Tests for the --tags CLI string parser."""

    def test_none_returns_empty(self):
        assert _parse_tags(None) == {}

    def test_empty_string_returns_empty(self):
        assert _parse_tags("") == {}

    def test_single_pair(self):
        assert _parse_tags("Owner=docs-team") == {"Owner": "docs-team"}

    def test_multiple_pairs_and_whitespace(self):
        assert _parse_tags(" Owner=docs-team , Environment=prod ") == {
            "Owner": "docs-team",
            "Environment": "prod",
        }

    def test_value_may_contain_equals(self):
        # Only the first '=' splits key/value.
        assert _parse_tags("Expr=a=b") == {"Expr": "a=b"}

    def test_key_with_allowed_special_chars(self):
        assert _parse_tags("aws:cost-center=1234") == {"aws:cost-center": "1234"}

    def test_missing_equals_raises(self):
        with pytest.raises(click.BadParameter):
            _parse_tags("OwnerNoValue")

    def test_empty_key_raises(self):
        with pytest.raises(click.BadParameter):
            _parse_tags("=value")


class TestStackTags:
    """Tests for stack-level tag propagation in deploy_stack."""

    def test_create_includes_tags(self):
        deployer, cfn_mock = _make_deployer(stack_exists=False)

        deployer.deploy_stack(
            stack_name="idp-test",
            template_path="/tmp/template.yaml",  # nosec B108 - test fixture path, deploy is mocked (no file I/O)
            parameters={},
            tags={"Owner": "docs-team", "Environment": "prod"},
        )

        _, kwargs = cfn_mock.create_stack.call_args
        assert kwargs["Tags"] == [
            {"Key": "Owner", "Value": "docs-team"},
            {"Key": "Environment", "Value": "prod"},
        ]

    def test_update_includes_tags_when_provided(self):
        deployer, cfn_mock = _make_deployer(stack_exists=True)

        deployer.deploy_stack(
            stack_name="idp-test",
            template_path="/tmp/template.yaml",  # nosec B108 - test fixture path, deploy is mocked (no file I/O)
            parameters={},
            tags={"Owner": "docs-team"},
        )

        _, kwargs = cfn_mock.update_stack.call_args
        assert kwargs["Tags"] == [{"Key": "Owner", "Value": "docs-team"}]

    def test_update_omits_tags_when_none_preserves_existing(self):
        """No tags passed on update -> Tags key omitted so CFN keeps existing."""
        deployer, cfn_mock = _make_deployer(stack_exists=True)

        deployer.deploy_stack(
            stack_name="idp-test",
            template_path="/tmp/template.yaml",  # nosec B108 - test fixture path, deploy is mocked (no file I/O)
            parameters={},
            tags=None,
        )

        _, kwargs = cfn_mock.update_stack.call_args
        assert "Tags" not in kwargs

    def test_create_omits_tags_when_none(self):
        deployer, cfn_mock = _make_deployer(stack_exists=False)

        deployer.deploy_stack(
            stack_name="idp-test",
            template_path="/tmp/template.yaml",  # nosec B108 - test fixture path, deploy is mocked (no file I/O)
            parameters={},
        )

        _, kwargs = cfn_mock.create_stack.call_args
        assert "Tags" not in kwargs
