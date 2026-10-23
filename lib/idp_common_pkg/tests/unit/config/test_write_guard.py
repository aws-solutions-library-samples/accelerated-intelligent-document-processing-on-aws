# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``WriteGuard``: the callable a caller passes to refuse a configuration write.

``ConfigurationManager`` calls it before every save, in every write mode — normal
update, reset to default, save as version, save as default and restoring a
revision — with ``(config before, config about to be saved, profile)``. A guard
that raises stops the write before anything is stored.
"""

from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG, DEFAULT_VERSION
from idp_common.config.models import IDPConfig

TABLE = "write-guard-config-table"
BUCKET = "write-guard-config-bucket"


class _Recorder:
    """Records each call; refuses when ``refuse`` is set."""

    def __init__(self, refuse=False):
        self.refuse = refuse
        self.calls = []

    def __call__(self, before, after, version):
        self.calls.append((before.notes if before else None, after.notes, version))
        if self.refuse:
            raise PermissionError("refused")


@pytest.mark.unit
@mock_aws
class TestWriteGuard:
    def setup_method(self, method):
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)

    def _manager(self, monkeypatch):
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
        monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)
        manager = ConfigurationManager()
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="default"), version=DEFAULT_VERSION
        )
        return manager

    def _notes(self, manager, version):
        cfg = manager._get_full_config_for_version(version)
        return cfg.notes if cfg else None

    def test_an_update_is_guarded_and_a_refusal_saves_nothing(self, monkeypatch):
        manager = self._manager(monkeypatch)
        guard = _Recorder(refuse=True)
        with pytest.raises(PermissionError):
            manager.handle_update_custom_configuration(
                {"notes": "edited"}, version="lending", write_guard=guard
            )
        # A new profile starts from the default configuration.
        assert guard.calls == [("default", "edited", "lending")]
        assert self._notes(manager, "lending") is None

    def test_an_allowed_update_is_saved(self, monkeypatch):
        manager = self._manager(monkeypatch)
        guard = _Recorder()
        manager.handle_update_custom_configuration(
            {"notes": "edited"}, version="lending", write_guard=guard
        )
        assert self._notes(manager, "lending") == "edited"
        assert [c[2] for c in guard.calls] == ["lending"]

    def test_reset_to_default_is_guarded(self, monkeypatch):
        manager = self._manager(monkeypatch)
        manager.handle_update_custom_configuration(
            {"notes": "custom"}, version="lending"
        )
        guard = _Recorder(refuse=True)
        with pytest.raises(PermissionError):
            manager.handle_update_custom_configuration(
                {"resetToDefault": True}, version="lending", write_guard=guard
            )
        assert guard.calls == [("custom", "default", "lending")]
        assert self._notes(manager, "lending") == "custom"

    def test_save_as_version_is_guarded(self, monkeypatch):
        manager = self._manager(monkeypatch)
        guard = _Recorder(refuse=True)
        full = IDPConfig(notes="copy").model_dump(mode="python")
        with pytest.raises(PermissionError):
            manager.handle_update_custom_configuration(
                {**full, "saveAsVersion": True}, version="copy", write_guard=guard
            )
        assert [c[1:] for c in guard.calls] == [("copy", "copy")]
        assert self._notes(manager, "copy") is None

    def test_save_as_default_guards_both_profiles(self, monkeypatch):
        manager = self._manager(monkeypatch)
        guard = _Recorder()
        full = IDPConfig(notes="new default").model_dump(mode="python")
        manager.handle_update_custom_configuration(
            {**full, "saveAsDefault": True}, version="lending", write_guard=guard
        )
        assert [c[2] for c in guard.calls] == [DEFAULT_VERSION, "lending"]
        assert self._notes(manager, DEFAULT_VERSION) == "new default"

    def test_restoring_a_revision_is_guarded(self, monkeypatch):
        manager = self._manager(monkeypatch)
        manager.handle_update_custom_configuration(
            {"notes": "first"}, version="lending"
        )
        manager.handle_update_custom_configuration(
            {"notes": "second"}, version="lending"
        )
        first = manager.list_revisions("lending")[-1]["revision"]
        guard = _Recorder(refuse=True)
        with pytest.raises(PermissionError):
            manager.restore_revision("lending", int(first), write_guard=guard)
        assert guard.calls == [("second", "first", "lending")]
        assert self._notes(manager, "lending") == "second"


@pytest.mark.unit
class TestListVersionsErrors:
    def _manager(self):
        manager = ConfigurationManager.__new__(ConfigurationManager)
        table = MagicMock()
        table.scan.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}}, "Scan"
        )
        manager.table = table
        return manager

    def test_a_read_failure_is_an_empty_list_by_default(self):
        assert self._manager().list_config_versions() == []

    def test_a_caller_can_ask_for_the_error(self):
        with pytest.raises(ClientError):
            self._manager().list_config_versions(raise_errors=True)
