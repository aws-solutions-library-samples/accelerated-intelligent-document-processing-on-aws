# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The moto fixture's table is the one the parent template deploys."""

import pytest

from ._cfn_yaml import REPO_ROOT, load_cfn_yaml
from ._table import TABLE_ATTRIBUTE_DEFINITIONS, TABLE_GSIS, TABLE_KEY_SCHEMA

_PARENT_TEMPLATE = REPO_ROOT / "template.yaml"


@pytest.fixture(scope="module")
def template():
    return load_cfn_yaml(_PARENT_TEMPLATE)


@pytest.fixture(scope="module")
def table_properties(template):
    resource = template["Resources"]["NotificationsTable"]
    assert resource["Type"] == "AWS::DynamoDB::Table"
    return resource["Properties"]


def _by_name(entries, key):
    return sorted(entries, key=lambda e: e[key])


@pytest.mark.unit
class TestNotificationsTableParity:
    def test_attribute_definitions_match_fixture(self, table_properties):
        assert _by_name(
            table_properties["AttributeDefinitions"], "AttributeName"
        ) == _by_name(TABLE_ATTRIBUTE_DEFINITIONS, "AttributeName")

    def test_key_schema_matches_fixture(self, table_properties):
        assert table_properties["KeySchema"] == TABLE_KEY_SCHEMA

    def test_gsi_matches_fixture(self, table_properties):
        assert table_properties["GlobalSecondaryIndexes"] == TABLE_GSIS

    def test_ttl_pitr_and_cmk_encryption(self, table_properties):
        assert table_properties["TimeToLiveSpecification"] == {
            "AttributeName": "ExpiresAfter",
            "Enabled": True,
        }
        assert table_properties["PointInTimeRecoverySpecification"] == {
            "PointInTimeRecoveryEnabled": True
        }
        assert table_properties["BillingMode"] == "PAY_PER_REQUEST"
        sse = table_properties["SSESpecification"]
        assert sse["SSEType"] == "KMS"
        assert sse["KMSMasterKeyId"] == {"!Ref": "CustomerManagedEncryptionKey"}

    def test_table_name_output_has_no_export(self, template):
        output = template["Outputs"]["NotificationsTableName"]
        assert output["Value"] == {"!Ref": "NotificationsTable"}
        assert "Export" not in output
