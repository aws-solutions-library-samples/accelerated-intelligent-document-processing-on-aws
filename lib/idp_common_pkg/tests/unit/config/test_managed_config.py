# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for managed configuration feature.

Verifies:
- IDPConfig model supports managed and test_set fields
- DynamoDB serialization uses PascalCase 'Managed' convention
- DynamoDB deserialization maps 'Managed' back to lowercase 'managed'
- list_config_versions returns managed flag
"""

from unittest.mock import Mock, patch

import pytest

from idp_common.config.models import ConfigMetadata, ConfigurationRecord, IDPConfig


@pytest.mark.unit
class TestManagedConfigModel:
    """Test IDPConfig model managed and test_set fields."""

    def test_idp_config_managed_defaults_to_false(self):
        """managed field defaults to False."""
        config = IDPConfig()
        assert config.managed is False

    def test_idp_config_managed_true(self):
        """managed field can be set to True."""
        config = IDPConfig(managed=True)
        assert config.managed is True

    def test_idp_config_test_set_defaults_to_none(self):
        """test_set field defaults to None."""
        config = IDPConfig()
        assert config.test_set is None

    def test_idp_config_test_set_value(self):
        """test_set field can be set."""
        config = IDPConfig(managed=True, test_set="Fake-W2-Tax-Forms")
        assert config.test_set == "Fake-W2-Tax-Forms"
        assert config.managed is True

    def test_idp_config_managed_in_model_dump(self):
        """managed and test_set fields appear in model_dump output."""
        config = IDPConfig(managed=True, test_set="docsplit")
        dumped = config.model_dump(mode="python")
        assert dumped["managed"] is True
        assert dumped["test_set"] == "docsplit"

    def test_idp_config_managed_false_in_model_dump(self):
        """managed=False still appears in model_dump output."""
        config = IDPConfig()
        dumped = config.model_dump(mode="python")
        assert dumped["managed"] is False


@pytest.mark.unit
class TestManagedDynamoDBSerialization:
    """Test that managed field uses PascalCase 'Managed' in DynamoDB."""

    def test_to_dynamodb_item_maps_managed_to_pascal_case(self):
        """to_dynamodb_item should write 'Managed' (PascalCase), not 'managed'."""
        config = IDPConfig(managed=True, test_set="fake-w2")
        record = ConfigurationRecord(
            configuration_type="Config",
            version="fake-w2",
            config=config,
            metadata=ConfigMetadata(
                created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z"
            ),
        )
        item = record.to_dynamodb_item()

        # PascalCase 'Managed' should be present
        assert "Managed" in item
        assert item["Managed"] is True

        # lowercase 'managed' should NOT be in the item (it was popped before spreading)
        assert "managed" not in item

    def test_to_dynamodb_item_managed_false_still_mapped(self):
        """Even managed=False should be stored as PascalCase 'Managed'."""
        config = IDPConfig(managed=False)
        record = ConfigurationRecord(
            configuration_type="Config",
            version="test-v1",
            config=config,
        )
        item = record.to_dynamodb_item()

        assert "Managed" in item
        assert item["Managed"] is False
        assert "managed" not in item

    def test_from_dynamodb_item_maps_managed_back_to_lowercase(self):
        """from_dynamodb_item should map PascalCase 'Managed' back to lowercase 'managed'."""
        dynamo_item = {
            "Configuration": "Config#fake-w2",
            "IsActive": False,
            "Managed": True,
            "Description": "Test managed config",
            "CreatedAt": "2026-01-01T00:00:00Z",
            "UpdatedAt": "2026-01-01T00:00:00Z",
            # Minimal config fields to pass IDPConfig validation
            "use_bda": False,
            "managed": True,  # This would come from compressed config data
            "ocr": {"backend": "textract", "features": [], "max_workers": "20"},
            "classification": {
                "model": "us.amazon.nova-pro-v1:0",
                "temperature": "0.0",
                "top_p": "0.1",
                "top_k": "5.0",
                "max_tokens": "4096",
                "system_prompt": "",
                "task_prompt": "",
                "maxPagesForClassification": "ALL",
                "granularity": "page",
                "sectionSplitting": "llm_determined",
                "contextPagesCount": "0",
            },
            "extraction": {
                "model": "us.amazon.nova-pro-v1:0",
                "temperature": "0.0",
                "top_p": "0.1",
                "top_k": "5.0",
                "max_tokens": "10000",
                "system_prompt": "",
                "task_prompt": "",
            },
            "assessment": {"enabled": True},
            "summarization": {"enabled": True, "model": "us.amazon.nova-premier-v1:0"},
            "classes": [],
        }

        record = ConfigurationRecord.from_dynamodb_item(dynamo_item)
        assert isinstance(record.config, IDPConfig)
        assert record.config.managed is True
        assert record.version == "fake-w2"

    def test_roundtrip_managed_config(self):
        """Managed field survives a to_dynamodb_item -> from_dynamodb_item roundtrip."""
        original_config = IDPConfig(managed=True, test_set="ocr-benchmark")
        original_record = ConfigurationRecord(
            configuration_type="Config",
            version="ocr-benchmark",
            is_active=False,
            description="OCR benchmark managed config",
            config=original_config,
            metadata=ConfigMetadata(
                created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z"
            ),
        )

        # Serialize
        item = original_record.to_dynamodb_item()
        assert item["Managed"] is True
        assert "managed" not in item

        # Deserialize
        restored_record = ConfigurationRecord.from_dynamodb_item(item)
        assert isinstance(restored_record.config, IDPConfig)
        assert restored_record.config.managed is True
        assert restored_record.config.test_set == "ocr-benchmark"


@pytest.mark.unit
class TestListConfigVersionsManaged:
    """Test that list_config_versions returns managed flag correctly."""

    @patch("idp_common.config.configuration_manager.boto3")
    def test_list_config_versions_returns_managed_field(self, mock_boto3):
        """list_config_versions should return 'managed' key from DynamoDB 'Managed' field."""
        from idp_common.config.configuration_manager import ConfigurationManager

        mock_table = Mock()
        mock_boto3.resource.return_value.Table.return_value = mock_table

        mock_table.scan.return_value = {
            "Items": [
                {
                    "Configuration": "Config#fake-w2",
                    "IsActive": False,
                    "CreatedAt": "2026-01-01T00:00:00Z",
                    "UpdatedAt": "2026-01-01T00:00:00Z",
                    "Description": "Managed W2 config",
                    "Managed": True,
                },
                {
                    "Configuration": "Config#custom-v1",
                    "IsActive": True,
                    "CreatedAt": "2026-01-02T00:00:00Z",
                    "UpdatedAt": "2026-01-02T00:00:00Z",
                    "Description": "User config",
                    # No Managed field - should default to False
                },
            ]
        }

        manager = ConfigurationManager(table_name="test-table")
        versions = manager.list_config_versions()

        assert len(versions) == 2

        # Find the managed version
        managed_version = next(v for v in versions if v["versionName"] == "fake-w2")
        assert managed_version["managed"] is True
        assert managed_version["description"] == "Managed W2 config"

        # Find the non-managed version
        custom_version = next(v for v in versions if v["versionName"] == "custom-v1")
        assert custom_version["managed"] is False
        assert custom_version["isActive"] is True

        # Verify DynamoDB scan used PascalCase 'Managed' in ProjectionExpression
        scan_call = mock_table.scan.call_args
        projection = scan_call[1]["ProjectionExpression"]
        assert "Managed" in projection
        assert "managed" not in projection.split(
            ", "
        )  # lowercase should not be in projection
