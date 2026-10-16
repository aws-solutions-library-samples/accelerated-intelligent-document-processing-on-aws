# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Admin-only `hitl.review_queues` at every configuration write mode (FR-4, D31).

Each test runs the real ``ConfigurationManager`` on a moto table with the
``ReviewQueuesAdminGuard`` the configuration resolver passes. A non-Admin save
that changes the block is refused before anything is written, in every mode:
normal update, reset to default, save as version, save as default and
restoring a revision.
"""

import boto3
import pytest
from moto import mock_aws

from idp_common.config.configuration_manager import (
    ConfigurationManager,
    ConfigurationValidationError,
)
from idp_common.config.constants import CONFIG_TYPE_CONFIG, DEFAULT_VERSION
from idp_common.config.models import IDPConfig
from idp_common.review_queues.config_guard import ReviewQueuesAdminGuard

TABLE = "rq-config-table"
BUCKET = "rq-config-bucket"

CUSTOM_QUEUES = {
    "hitl": {
        "review_queues": {
            "queues": [
                {"name": "default", "member_groups": ["QueueReviewer"]},
                {"name": "escalation", "member_groups": ["Admin"]},
                {"name": "claims", "member_emails": ["a@example.com"]},
            ]
        }
    }
}


def _author():
    return ReviewQueuesAdminGuard(is_admin=False, caller_email="author@example.com")


def _admin():
    return ReviewQueuesAdminGuard(is_admin=True, caller_email="admin@example.com")


@pytest.mark.unit
@mock_aws
class TestWriteGuard:
    def setup_method(self, method):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
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

    def _queues(self, manager, version):
        cfg = manager._get_full_config_for_version(version)
        return [q.name for q in cfg.hitl.review_queues.queues] if cfg else None

    def _seed_custom(self, manager, version="lending"):
        manager.handle_update_custom_configuration(
            CUSTOM_QUEUES, version=version, write_guard=_admin()
        )

    def test_author_unrelated_change_saved(self, monkeypatch):
        manager = self._manager(monkeypatch)
        manager.handle_update_custom_configuration(
            {"notes": "edited"}, version="lending", write_guard=_author()
        )
        assert manager._get_full_config_for_version("lending").notes == "edited"

    def test_author_queue_change_refused_and_nothing_saved(self, monkeypatch):
        manager = self._manager(monkeypatch)
        with pytest.raises(PermissionError):
            manager.handle_update_custom_configuration(
                CUSTOM_QUEUES, version="lending", write_guard=_author()
            )
        assert manager._get_full_config_for_version("lending") is None

    def test_author_reset_refused_when_queues_differ(self, monkeypatch):
        manager = self._manager(monkeypatch)
        self._seed_custom(manager)
        with pytest.raises(PermissionError):
            manager.handle_update_custom_configuration(
                {"resetToDefault": True}, version="lending", write_guard=_author()
            )
        assert self._queues(manager, "lending") == ["default", "escalation", "claims"]

    def test_author_save_as_version_copying_custom_queues_refused(self, monkeypatch):
        manager = self._manager(monkeypatch)
        self._seed_custom(manager)
        copied = manager._get_full_config_for_version("lending").model_dump(
            mode="python"
        )
        with pytest.raises(PermissionError):
            manager.handle_update_custom_configuration(
                {**copied, "saveAsVersion": True}, version="copy", write_guard=_author()
            )
        assert manager._get_full_config_for_version("copy") is None

    def test_author_save_as_default_with_custom_queues_refused(self, monkeypatch):
        manager = self._manager(monkeypatch)
        full = IDPConfig.model_validate(CUSTOM_QUEUES).model_dump(mode="python")
        with pytest.raises(PermissionError):
            manager.handle_update_custom_configuration(
                {**full, "saveAsDefault": True},
                version="lending",
                write_guard=_author(),
            )
        assert self._queues(manager, DEFAULT_VERSION) == ["default", "escalation"]

    def test_author_restore_revision_with_other_queues_refused(self, monkeypatch):
        manager = self._manager(monkeypatch)
        self._seed_custom(manager)
        manager.handle_update_custom_configuration(
            {
                "hitl": {
                    "review_queues": {
                        "queues": [
                            {"name": "default", "member_groups": ["QueueReviewer"]},
                            {"name": "escalation", "member_groups": ["Admin"]},
                        ]
                    }
                }
            },
            version="lending",
            write_guard=_admin(),
        )
        first = manager.list_revisions("lending")[-1]["revision"]
        with pytest.raises(PermissionError):
            manager.restore_revision("lending", int(first), write_guard=_author())

    def test_admin_change_saved_and_recorded(self, monkeypatch):
        manager = self._manager(monkeypatch)
        guard = _admin()
        manager.handle_update_custom_configuration(
            CUSTOM_QUEUES, version="lending", write_guard=guard
        )
        assert self._queues(manager, "lending") == ["default", "escalation", "claims"]
        assert [c[0] for c in guard.changes] == ["lending"]

    def test_cross_field_invalid_delta_refused(self, monkeypatch):
        manager = self._manager(monkeypatch)
        with pytest.raises(ConfigurationValidationError) as exc:
            manager.handle_update_custom_configuration(
                {"hitl": {"review_queues": {"escalation_queue": "default"}}},
                version="lending",
                write_guard=_admin(),
            )
        assert ValueError in type(exc.value).__mro__

    def test_unrelated_save_of_invalid_stored_config_saved(self, monkeypatch):
        manager = self._manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG,
            IDPConfig.model_validate(
                {"hitl": {"review_queues": {"default_queue": "nope"}}}
            ),
            version="lending",
        )
        manager.handle_update_custom_configuration(
            {"notes": "still saves"}, version="lending", write_guard=_author()
        )
        assert manager._get_full_config_for_version("lending").notes == "still saves"

    def test_no_guard_saves_but_cross_field_still_checked(self, monkeypatch):
        manager = self._manager(monkeypatch)
        manager.handle_update_custom_configuration(CUSTOM_QUEUES, version="lending")
        assert self._queues(manager, "lending") == ["default", "escalation", "claims"]
        with pytest.raises(ConfigurationValidationError):
            manager.handle_update_custom_configuration(
                {"hitl": {"review_queues": {"default_queue": "nope"}}},
                version="lending",
            )
