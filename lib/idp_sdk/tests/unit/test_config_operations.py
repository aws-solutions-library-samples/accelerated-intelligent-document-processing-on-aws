# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for Config operations (mocked).
"""

from unittest.mock import patch

import pytest

from idp_sdk import IDPClient
from idp_sdk.models import ConfigCreateResult, ConfigValidationResult


@pytest.mark.unit
@pytest.mark.config
class TestConfigOperationsMocked:
    """Test config operations with mocked file I/O."""

    @patch("idp_common.config.merge_utils.generate_config_template")
    def test_create_config(self, mock_generate, tmp_path):
        """Test creating config file."""
        # Setup mock
        mock_generate.return_value = "key: value"

        # Write to a temp path — not the repo root — so the test leaves no
        # stray config.yaml in the working tree.
        output = tmp_path / "config.yaml"

        # Test
        client = IDPClient()
        result = client.config.create(features="min", output=str(output))

        assert isinstance(result, ConfigCreateResult)
        assert result.yaml_content
        assert result.output_path == str(output)

    @patch("idp_common.config.merge_utils.validate_config")
    @patch("idp_common.config.merge_utils.load_yaml_file")
    def test_validate_config(self, mock_load, mock_validate):
        """Test validating config file."""
        # Setup mocks
        mock_load.return_value = {"key": "value"}
        mock_validate.return_value = {"valid": True, "errors": [], "warnings": []}

        # Test
        client = IDPClient()
        result = client.config.validate(config_file="config.yaml")

        assert isinstance(result, ConfigValidationResult)
        assert result.valid is True
        assert len(result.errors) == 0

    def test_config_no_stack_required(self):
        """Test config operations don't require stack."""
        client = IDPClient()  # No stack name

        # Should not raise - config operations are stack-independent
        assert client.config is not None

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_list_config(self, mock_manager_class, mock_boto3):
        """Test listing configuration versions."""
        # Setup mocks
        mock_cfn = mock_boto3.return_value
        mock_paginator = mock_cfn.get_paginator.return_value
        mock_paginator.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "ConfigurationTable",
                        "PhysicalResourceId": "test-table",
                    }
                ]
            }
        ]

        mock_manager = mock_manager_class.return_value
        mock_manager.list_config_versions.return_value = [
            {"versionName": "default", "isActive": False},
            {"versionName": "v1", "isActive": True},
        ]

        # Test
        client = IDPClient(stack_name="test-stack")
        result = client.config.list()

        assert result.count == 2
        assert len(result.versions) == 2
        assert result.versions[0].version_name == "default"

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_activate_config(self, mock_manager_class, mock_boto3):
        """Test activating configuration version."""
        # Setup mocks
        mock_cfn = mock_boto3.return_value
        mock_paginator = mock_cfn.get_paginator.return_value
        mock_paginator.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "ConfigurationTable",
                        "PhysicalResourceId": "test-table",
                    }
                ]
            }
        ]

        mock_manager = mock_manager_class.return_value
        mock_manager.get_configuration.return_value = {
            "version": "v1"
        }  # Version exists
        mock_manager.activate_version.return_value = None

        # Test
        client = IDPClient(stack_name="test-stack")
        result = client.config.activate("v1")

        assert result.success is True
        assert result.activated_version == "v1"
        mock_manager.activate_version.assert_called_once_with("v1")

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_activate_config_not_found(self, mock_manager_class, mock_boto3):
        """Test activating non-existent configuration version."""
        # Setup mocks
        mock_cfn = mock_boto3.return_value
        mock_paginator = mock_cfn.get_paginator.return_value
        mock_paginator.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "ConfigurationTable",
                        "PhysicalResourceId": "test-table",
                    }
                ]
            }
        ]

        mock_manager = mock_manager_class.return_value
        mock_manager.get_configuration.return_value = None  # Version doesn't exist

        # Test
        client = IDPClient(stack_name="test-stack")
        result = client.config.activate("nonexistent")

        assert result.success is False
        assert result.error is not None
        assert "does not exist" in result.error

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_delete_config(self, mock_manager_class, mock_boto3):
        """Test deleting configuration version."""
        # Setup mocks
        mock_cfn = mock_boto3.return_value
        mock_paginator = mock_cfn.get_paginator.return_value
        mock_paginator.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "ConfigurationTable",
                        "PhysicalResourceId": "test-table",
                    }
                ]
            }
        ]

        mock_manager = mock_manager_class.return_value
        mock_manager.delete_configuration.return_value = None

        # Test
        client = IDPClient(stack_name="test-stack")
        result = client.config.delete("old-version")

        assert result.success is True
        assert result.deleted_version == "old-version"
        mock_manager.delete_configuration.assert_called_once_with(
            "Config", version="old-version"
        )

    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_delete_config_error(self, mock_manager_class, mock_boto3):
        """Test deleting configuration version with error."""
        # Setup mocks
        mock_cfn = mock_boto3.return_value
        mock_paginator = mock_cfn.get_paginator.return_value
        mock_paginator.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "ConfigurationTable",
                        "PhysicalResourceId": "test-table",
                    }
                ]
            }
        ]

        mock_manager = mock_manager_class.return_value
        mock_manager.delete_configuration.side_effect = ValueError(
            "Cannot delete active version"
        )

        # Test
        client = IDPClient(stack_name="test-stack")
        result = client.config.delete("active-version")

        assert result.success is False
        assert result.error is not None
        assert "Cannot delete active version" in result.error

    @patch("builtins.open", create=True)
    @patch("boto3.client")
    def test_upload_managed_config_rejected(self, mock_boto3, mock_open):
        """Test that uploading a managed config is rejected."""
        import io

        # Setup mocks
        mock_cfn = mock_boto3.return_value
        mock_paginator = mock_cfn.get_paginator.return_value
        mock_paginator.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "ConfigurationTable",
                        "PhysicalResourceId": "test-table",
                    }
                ]
            }
        ]

        # Mock file content with managed: true
        yaml_content = """managed: true
use_bda: false
classes: []
"""
        mock_open.return_value.__enter__.return_value = io.StringIO(yaml_content)

        # Test
        client = IDPClient(stack_name="test-stack")
        result = client.config.upload(
            config_file="managed_config.yaml", config_version="test-version"
        )

        assert result.success is False
        assert result.error is not None
        assert "Cannot upload managed configuration" in result.error
        assert "managed: true" in result.error or "managed: false" in result.error

    @patch("builtins.open", create=True)
    @patch("boto3.client")
    @patch("idp_common.config.configuration_manager.ConfigurationManager")
    def test_upload_non_managed_config_sets_managed_false(
        self, mock_manager_class, mock_boto3, mock_open
    ):
        """Test that uploading a config without managed field sets it to false."""
        import io

        # Setup mocks
        mock_cfn = mock_boto3.return_value
        mock_paginator = mock_cfn.get_paginator.return_value
        mock_paginator.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "ConfigurationTable",
                        "PhysicalResourceId": "test-table",
                    }
                ]
            }
        ]

        mock_manager = mock_manager_class.return_value
        mock_manager.get_configuration.return_value = None  # New version
        mock_manager.handle_update_custom_configuration.return_value = True

        # Mock file content without managed field
        yaml_content = """use_bda: false
classes: []
"""
        mock_open.return_value.__enter__.return_value = io.StringIO(yaml_content)

        # Test
        client = IDPClient(stack_name="test-stack")
        result = client.config.upload(
            config_file="config.yaml", config_version="test-version", validate=False
        )

        assert result.success is True

        # Verify that the config passed to manager has managed=False
        call_args = mock_manager.handle_update_custom_configuration.call_args
        import json

        config_passed = json.loads(call_args[0][0])
        assert config_passed["managed"] is False

    @patch("builtins.open", create=True)
    @patch("boto3.client")
    def test_upload_json_config_rejected_if_managed(self, mock_boto3, mock_open):
        """Test that uploading a JSON config with managed=true is rejected."""
        import io

        # Setup mocks
        mock_cfn = mock_boto3.return_value
        mock_paginator = mock_cfn.get_paginator.return_value
        mock_paginator.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "ConfigurationTable",
                        "PhysicalResourceId": "test-table",
                    }
                ]
            }
        ]

        # Mock JSON file content with managed: true
        json_content = '{"managed": true, "use_bda": false, "classes": []}'
        mock_open.return_value.__enter__.return_value = io.StringIO(json_content)

        # Test
        client = IDPClient(stack_name="test-stack")
        result = client.config.upload(
            config_file="managed_config.json", config_version="test-version"
        )

        assert result.success is False
        assert result.error is not None
        assert "Cannot upload managed configuration" in result.error

    @staticmethod
    def _upload_with_a_save_that_collects(
        mock_manager_class, mock_boto3, mock_open, collected
    ):
        """Upload through a manager whose save adds ``collected`` to its list."""
        import io

        mock_paginator = mock_boto3.return_value.get_paginator.return_value
        mock_paginator.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "ConfigurationTable",
                        "PhysicalResourceId": "test-table",
                    }
                ]
            }
        ]
        mock_open.return_value.__enter__.return_value = io.StringIO(
            "use_bda: false\nclasses: []\n"
        )

        def save(config, *, warnings, **kwargs):
            warnings.extend(collected)
            return True

        mock_manager = mock_manager_class.return_value
        mock_manager.get_configuration.return_value = {"version": "test-version"}
        mock_manager.handle_update_custom_configuration.side_effect = save
        mock_manager.resolve_published_revision.return_value = 4

        client = IDPClient(stack_name="test-stack")
        return client.config.upload(
            config_file="config.yaml", config_version="test-version", validate=False
        )

    # The manager is autospecced in the two cases below, so the keyword the upload
    # hands the save is one the real `handle_update_custom_configuration` takes: a
    # keyword it does not have raises, and the upload reports a failure.
    @patch("builtins.open", create=True)
    @patch("boto3.client")
    @patch(
        "idp_common.config.configuration_manager.ConfigurationManager", autospec=True
    )
    def test_upload_returns_the_warnings_the_save_collected(
        self, mock_manager_class, mock_boto3, mock_open
    ):
        """What the saved configuration will not do reaches the caller, in order."""
        collected = [
            "Routing table 'review-routing', rule 'by-count': this routing rule "
            "tests a policy rule result (failed_policy_rule_count), and policy "
            "rule validation is off for this profile or it has no policies, so "
            "the routing rule never matches.",
            "Routing table 'review-routing', default outcome: the reason shows a "
            "policy rule result ({failed_policy_rule_count}), which is empty "
            "while policy rule validation is off.",
        ]

        result = self._upload_with_a_save_that_collects(
            mock_manager_class, mock_boto3, mock_open, collected
        )

        assert result.error is None
        assert result.success is True
        assert result.warnings == collected
        assert result.revision == 4

    @patch("builtins.open", create=True)
    @patch("boto3.client")
    @patch(
        "idp_common.config.configuration_manager.ConfigurationManager", autospec=True
    )
    def test_upload_with_nothing_to_warn_about_has_no_warnings(
        self, mock_manager_class, mock_boto3, mock_open
    ):
        result = self._upload_with_a_save_that_collects(
            mock_manager_class, mock_boto3, mock_open, []
        )

        assert result.error is None
        assert result.success is True
        assert result.warnings == []
