# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Only an Admin can change a profile's routing rules.

Routing decides which documents people review and in which queue, so the
``routing`` section is Admin-only while the rest of a profile stays editable by an
Author. The server enforces it by comparing the section before and after every
write, whichever operation makes the write: an edit, a restored revision, a reset
to default. The handler runs against the real ``ConfigurationManager`` on moto, so
what is asserted is what is stored.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.models import IDPConfig
from idp_common.config.routing_models import RoutingConfig
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.store import ReviewStore

os.environ.setdefault("CONFIGURATION_TABLE_NAME", "test-config-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

pytestmark = pytest.mark.unit

TABLE = "routing-guard-config"
BUCKET = "routing-guard-config-bucket"
HITL_TABLE = "routing-guard-hitl"
PROFILE = "lending"


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "config_resolver_routing_guard_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["config_resolver_routing_guard_index"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()


def _routing(queue="approvals", rule_id="everything"):
    return {
        "tables": [
            {
                "rules": [
                    {
                        "id": rule_id,
                        "conditions": [],
                        "actions": [{"type": "review_queue", "queue": queue}],
                    }
                ]
            }
        ]
    }


def _event(field, args, group, email="someone@example.com"):
    return {
        "info": {"fieldName": field},
        "arguments": args,
        "identity": {"claims": {"cognito:groups": [group], "email": email}},
    }


def _save(delta, group, version=PROFILE):
    return index.handler(
        _event(
            "updateConfiguration",
            {"versionName": version, "customConfig": json.dumps(delta)},
            group,
        ),
        None,
    )


@pytest.fixture
def stack(monkeypatch):
    """A configuration table and bucket, with no review table."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)
    monkeypatch.delenv("HITL_TABLE_NAME", raising=False)
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        # An unscoped caller: scope is another file's subject.
        monkeypatch.setattr(
            index, "_get_user_allowed_config_versions", lambda email, sub="": None
        )
        monkeypatch.setattr(index, "_REVIEW_STORE", None)
        manager = ConfigurationManager()
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
        manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)
        yield manager


def _stored_routing(manager, version=PROFILE):
    return manager.get_configuration(CONFIG_TYPE_CONFIG, version).routing


class TestAnAuthorCannotChangeRouting:
    def test_a_save_that_changes_routing_is_refused(self, stack):
        with pytest.raises(PermissionError, match="only Admins can change routing"):
            _save({"routing": _routing()}, "Author")

        assert _stored_routing(stack) == RoutingConfig()

    def test_the_refusal_is_logged_as_a_denial_without_a_trace(self, stack, caplog):
        with caplog.at_level("WARNING"), pytest.raises(PermissionError):
            _save({"routing": _routing()}, "Author")

        (record,) = [r for r in caplog.records if "routing rules" in r.getMessage()]
        assert record.levelname == "WARNING"
        assert "Rejecting" in record.getMessage()
        assert record.exc_info is None

    def test_a_change_to_the_placement_limit_alone_is_refused(self, stack):
        with pytest.raises(PermissionError):
            _save({"routing": {"max_consecutive_placements": 7}}, "Author")

        assert _stored_routing(stack).max_consecutive_placements == 3

    def test_a_save_as_version_of_an_existing_profile_is_refused(self, stack):
        """Save as is Admin-only at the handler, and the guard holds without it."""
        result = _save({"routing": _routing(), "saveAsVersion": True}, "Author")

        assert result["success"] is False
        assert result["error"]["type"] == "Unauthorized"
        with pytest.raises(PermissionError, match="only Admins can change routing"):
            stack.handle_update_custom_configuration(
                {"routing": _routing(), "saveAsVersion": True},
                version=PROFILE,
                write_guard=index._routing_write_guard(False),
            )
        assert _stored_routing(stack) == RoutingConfig()

    def test_a_restore_of_a_revision_whose_routing_differs_is_refused(self, stack):
        stack.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(routing=_routing()), version=PROFILE
        )
        with_rules = stack.resolve_published_revision(PROFILE)
        stack.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)

        with pytest.raises(PermissionError, match="only Admins can change routing"):
            index.handler(
                _event(
                    "restoreConfigProfileRevision",
                    {"profileName": PROFILE, "revision": int(with_rules)},
                    "Author",
                ),
                None,
            )

        assert _stored_routing(stack) == RoutingConfig()

    def test_a_reset_to_default_is_refused_when_the_defaults_routing_differs(
        self, stack
    ):
        stack.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(routing=_routing()), version="default"
        )

        with pytest.raises(PermissionError, match="only Admins can change routing"):
            _save({"resetToDefault": True}, "Author")

        assert _stored_routing(stack) == RoutingConfig()


class TestAnAuthorCanChangeTheRest:
    def test_a_save_that_leaves_routing_alone_passes(self, stack):
        result = _save({"notes": "edited by an author"}, "Author")

        assert result["success"] is True
        assert stack.get_configuration(CONFIG_TYPE_CONFIG, PROFILE).notes == (
            "edited by an author"
        )

    def test_it_passes_when_the_profile_has_custom_routing_rules(self, stack):
        stack.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(routing=_routing()), version=PROFILE
        )
        classes = [
            {
                "$id": "Invoice",
                "x-aws-idp-document-type": "Invoice",
                "type": "object",
                "properties": {"Total": {"type": "number"}},
            }
        ]

        result = _save({"classes": classes}, "Author")

        assert result["success"] is True
        saved = stack.get_configuration(CONFIG_TYPE_CONFIG, PROFILE)
        assert [c["$id"] for c in saved.classes] == ["Invoice"]
        assert [rule.id for rule in saved.routing.tables[0].rules] == ["everything"]

    def test_a_save_that_sends_the_stored_routing_back_unchanged_passes(self, stack):
        """The editor sends whole sections; an unchanged one is not a change."""
        unchanged = RoutingConfig().model_dump()

        result = _save({"routing": unchanged, "notes": "n"}, "Author")

        assert result["success"] is True

    def test_a_restore_of_a_revision_with_the_same_routing_passes(self, stack):
        first = stack.resolve_published_revision(PROFILE)
        stack.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="second"), version=PROFILE
        )

        result = index.handler(
            _event(
                "restoreConfigProfileRevision",
                {"profileName": PROFILE, "revision": int(first)},
                "Author",
            ),
            None,
        )

        assert result["success"] is True

    def test_a_reset_to_default_passes_when_the_routing_is_equal(self, stack):
        stack.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="default notes"), version="default"
        )

        result = _save({"resetToDefault": True}, "Author")

        assert result["success"] is True
        assert stack.get_configuration(CONFIG_TYPE_CONFIG, PROFILE).notes == (
            "default notes"
        )


class TestAnAdminCanChangeRouting:
    def test_a_save_that_changes_routing_passes(self, stack):
        result = _save({"routing": _routing()}, "Admin")

        assert result["success"] is True
        assert [rule.id for rule in _stored_routing(stack).tables[0].rules] == [
            "everything"
        ]

    def test_a_restore_that_changes_routing_passes(self, stack):
        stack.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(routing=_routing()), version=PROFILE
        )
        with_rules = stack.resolve_published_revision(PROFILE)
        stack.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE)

        result = index.handler(
            _event(
                "restoreConfigProfileRevision",
                {"profileName": PROFILE, "revision": int(with_rules)},
                "Admin",
            ),
            None,
        )

        assert result["success"] is True
        assert [rule.id for rule in _stored_routing(stack).tables[0].rules] == [
            "everything"
        ]

    def test_an_invalid_table_is_reported_as_a_validation_error(self, stack):
        bad = _routing()
        bad["tables"][0]["hit_policy"] = "UNIQUE"

        result = _save({"routing": bad}, "Admin")

        assert result["success"] is False
        assert result["error"]["type"] == "ValidationError"
        assert "'UNIQUE'" in result["error"]["message"]
        assert _stored_routing(stack) == RoutingConfig()


class TestTheQueueLookup:
    def test_with_no_review_table_a_custom_queue_id_is_accepted(self, stack):
        assert index._routing_queue_lookup() is None

        result = _save({"routing": _routing(queue="never-created")}, "Admin")

        assert result["success"] is True

    @pytest.fixture
    def review_table(self, stack, monkeypatch):
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=HITL_TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        store = ReviewStore(HITL_TABLE, region="us-east-1")
        store.queue_defs.put(
            QueueDefinition(id="approvals", name="Approvals"), create=True
        )
        store.queue_defs.put(QueueDefinition(id="paused", enabled=False), create=True)
        monkeypatch.setattr(index, "_REVIEW_STORE", store)
        return store

    def _items(self):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(HITL_TABLE)
        return sorted(item["SK"] for item in table.scan()["Items"])

    def test_an_existing_queue_is_accepted(self, stack, review_table):
        result = _save({"routing": _routing(queue="approvals")}, "Admin")

        assert result["success"] is True

    def test_a_missing_queue_is_refused_by_name(self, stack, review_table):
        result = _save({"routing": _routing(queue="never-created")}, "Admin")

        assert result["success"] is False
        assert result["error"]["type"] == "ValidationError"
        assert "'never-created' does not exist" in result["error"]["message"]
        assert _stored_routing(stack) == RoutingConfig()

    def test_a_disabled_queue_is_accepted(self, stack, review_table):
        result = _save({"routing": _routing(queue="paused")}, "Admin")

        assert result["success"] is True

    def test_the_default_queue_is_accepted_before_it_is_seeded(
        self, stack, review_table
    ):
        before = self._items()
        assert not [sk for sk in before if sk.endswith("default")]

        result = _save({"routing": _routing(queue="default")}, "Admin")

        assert result["success"] is True
        # The lookup reads; it never seeds or writes a queue definition.
        assert self._items() == before

    def test_the_lookup_reports_name_and_state(self, stack, review_table):
        lookup = index._routing_queue_lookup()

        assert lookup("approvals") == ("approvals", "Approvals", True)
        assert lookup("paused") == ("paused", "Paused", False)
        assert lookup("default") == ("default", "Default", True)
        assert lookup("never-created") is None
