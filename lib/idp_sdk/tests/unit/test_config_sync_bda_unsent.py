# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A class the BDA sync did not send has to reach the SDK's caller (issue #1755).

`BdaBlueprintService` does not send a class whose blueprint would have no
properties, because BDA rejects one. Its status entry is a `success` carrying an
`empty_blueprint` warning: nothing failed, but no blueprint exists for it. Both SDK
entry points that run a sync count `success` entries as synced, so without this the
class was reported synced.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_sdk.operations.config import ConfigOperation

PROJECT_ARN = "arn:aws:bedrock:us-west-2:123456789012:data-automation-project/p1"

UNSENT = {
    "class": "Notice",
    "status": "success",
    "warnings": [
        {"class": "Notice", "property": "Hdr", "type": "nested_object"},
        {"class": "Notice", "property": "Notice", "type": "empty_blueprint"},
    ],
}
SENT = {
    "class": "Invoice",
    "status": "success",
    "warnings": [{"class": "Invoice", "property": "Hdr", "type": "nested_object"}],
}


@pytest.fixture
def ops():
    client = MagicMock()
    client._region = "us-west-2"
    client._require_stack.return_value = "test-stack"
    operation = ConfigOperation(client)
    with patch.object(ConfigOperation, "_configure_config_env", return_value="tbl"):
        yield operation


def _doubles(statuses):
    service = MagicMock()
    service.create_blueprints_from_custom_configuration.return_value = statuses
    service.orphaned_blueprint_arns = []
    manager = MagicMock()
    manager.get_bda_project_arn.return_value = PROJECT_ARN
    config = MagicMock()
    config.use_bda = True
    manager.get_configuration.return_value = config
    return (
        manager,
        patch.multiple(
            "idp_common.bda.bda_blueprint_service",
            BdaBlueprintService=MagicMock(return_value=service),
        ),
        patch(
            "idp_common.config.configuration_manager.ConfigurationManager",
            return_value=manager,
        ),
    )


@pytest.mark.unit
class TestSyncBda:
    def test_an_unsent_class_is_named_and_not_counted_as_synced(self, ops):
        _manager, p_service, p_manager = _doubles([SENT, UNSENT])
        with p_service, p_manager:
            result = ops.sync_bda(config_version="v1", mode="replace")

        assert result.success is True
        assert result.unsent_classes == ["Notice"]
        assert (result.classes_synced, result.classes_failed) == (1, 0)
        assert result.processed_classes == ["Invoice", "Notice"]

    def test_a_dropped_property_alone_does_not_make_a_class_unsent(self, ops):
        _manager, p_service, p_manager = _doubles([SENT])
        with p_service, p_manager:
            result = ops.sync_bda(config_version="v1", mode="replace")

        assert result.unsent_classes == []
        assert result.classes_synced == 1


@pytest.mark.unit
class TestActivate:
    def test_an_unsent_class_is_named_and_not_counted_as_synced(self, ops):
        manager, p_service, p_manager = _doubles([SENT, UNSENT])
        with p_service, p_manager:
            result = ops.activate(config_version="v1")

        assert result.success is True
        assert result.bda_unsent_classes == ["Notice"]
        assert (result.bda_classes_synced, result.bda_classes_failed) == (1, 0)
        manager.activate_version.assert_called_once_with("v1")

    def test_the_unsent_class_is_carried_when_every_other_class_failed(self, ops):
        manager, p_service, p_manager = _doubles(
            [UNSENT, {"class": "Receipt", "status": "failed", "error": "x"}]
        )
        with p_service, p_manager:
            result = ops.activate(config_version="v1")

        assert result.success is False
        assert result.bda_unsent_classes == ["Notice"]
        assert result.bda_classes_synced == 0
        manager.activate_version.assert_not_called()
