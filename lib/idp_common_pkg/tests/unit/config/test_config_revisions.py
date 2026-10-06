# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Configuration Profile revision history.

Covers the invariants the feature rests on:
- a save is non-destructive (the previous configuration survives as a revision),
  which is what lets a scoped Author iterate without an admin;
- revision records never leak into the profile list that feeds the scope-filtered
  version dropdowns;
- retention never deletes a revision something still depends on.
"""

import copy
import datetime
import logging
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

from idp_common.config.configuration_manager import (
    EXPIRED_REVISION_REMEDY,
    ConfigurationManager,
)
from idp_common.config.constants import ACTIVE_POINTER_KEY, CONFIG_TYPE_CONFIG
from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.models import IDPConfig
from idp_common.config.revisions import ConfigRevisionStore

TABLE = "test-config-table"
BUCKET = "test-config-bucket"


def _make_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)


def _manager(monkeypatch, with_bucket=True):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    if with_bucket:
        monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)
    else:
        monkeypatch.delenv("CONFIGURATION_BUCKET", raising=False)
    return ConfigurationManager()


def _config(note):
    """A full config distinguishable by its notes field."""
    return IDPConfig(notes=note)


def _notes_of(config_dict):
    return config_dict.get("notes")


@pytest.mark.unit
@mock_aws
class TestCutOnSave:
    def test_first_save_cuts_r1_and_second_save_cuts_r2(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)

        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("first"), version="lending"
        )
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("second"), version="lending"
        )

        revisions = manager.list_revisions("lending")
        assert [r["revision"] for r in revisions] == [2, 1]  # newest first
        assert _notes_of(manager.get_revision("lending", 1)) == "first"
        assert _notes_of(manager.get_revision("lending", 2)) == "second"

    def test_previous_configuration_survives_an_overwrite(self, monkeypatch):
        """The whole point: an in-place save does not destroy what was there."""
        _make_table()
        manager = _manager(monkeypatch)

        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("good"), version="lending"
        )
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("broken"), version="lending"
        )

        head = manager.get_configuration(CONFIG_TYPE_CONFIG, "lending")
        assert head.notes == "broken"
        assert _notes_of(manager.get_revision("lending", 1)) == "good"

    def test_head_reflects_the_published_revision(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("b"), version="p")

        published = [r for r in manager.list_revisions("p") if r["published"]]
        assert [r["revision"] for r in published] == [2]

    def test_revision_counters_survive_a_later_save(self, monkeypatch):
        """put_item replaces the item, so the counters must be re-attached."""
        _make_table()
        manager = _manager(monkeypatch)
        for note in ("a", "b", "c"):
            manager.save_configuration(CONFIG_TYPE_CONFIG, _config(note), version="p")

        item = (
            boto3.resource("dynamodb", region_name="us-east-1")
            .Table(TABLE)
            .get_item(Key={"Configuration": "Config#p"})["Item"]
        )
        assert int(item["LatestRevision"]) == 3
        assert int(item["PublishedRevision"]) == 3

    def test_creator_is_recorded(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG,
            _config("a"),
            version="p",
            created_by="author@example.com",
        )
        assert manager.list_revisions("p")[0]["createdBy"] == "author@example.com"

    def test_notes_from_an_ordinary_update_reach_the_revision(self, monkeypatch):
        """
        `handle_update_custom_configuration` — the path the Web UI, the CLI and the
        SDK all take for an ordinary edit — accepted no notes, so every such
        revision recorded an author and a timestamp but nothing about the intent.
        A history of anonymous timestamps is unusable for an automated loop that
        cuts one revision per attempt.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.handle_update_custom_configuration(
            {"notes": "b"},
            version="p",
            created_by="author@example.com",
            revision_notes="raised topK to 20",
        )
        newest = manager.list_revisions("p")[0]
        assert newest["notes"] == "raised topK to 20"
        assert newest["createdBy"] == "author@example.com"

    def test_a_new_profile_prefers_the_callers_notes_over_the_generic_default(
        self, monkeypatch
    ):
        """
        Creating a profile records "Profile created" when the caller says nothing.
        A caller who did say something is more specific, so it wins — but the
        operation-specific notes ("Reset to default", "Saved as default") are left
        alone, because those describe what the operation WAS rather than why.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.handle_update_custom_configuration(
            {"notes": "a", "saveAsVersion": True},
            version="fresh",
            revision_notes="initial import from the tuning loop",
        )
        assert (
            manager.list_revisions("fresh")[0]["notes"]
            == "initial import from the tuning loop"
        )

    def test_a_new_profile_without_notes_still_says_profile_created(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.handle_update_custom_configuration(
            {"notes": "a", "saveAsVersion": True}, version="fresh"
        )
        assert manager.list_revisions("fresh")[0]["notes"] == "Profile created"

    def test_an_unchanged_save_records_nothing(self, monkeypatch):
        """
        Every stack deployment re-saves default and each managed profile. If a
        no-op save cut a revision, a handful of upgrades would push a user's real
        history out of the retention window.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")

        assert [r["revision"] for r in manager.list_revisions("p")] == [1]
        assert manager.list_revisions("p")[0]["published"] is True

    def test_a_changed_save_after_an_unchanged_one_is_recorded(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("b"), version="p")

        revisions = manager.list_revisions("p")
        assert [r["revision"] for r in revisions] == [2, 1]
        assert _notes_of(manager.get_revision("p", 2)) == "b"

    def test_cut_revision_false_records_nothing(self, monkeypatch):
        """The legacy-format auto-migration must not invent history."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("a"), version="p", cut_revision=False
        )
        assert manager.list_revisions("p") == []


@pytest.mark.unit
@mock_aws
class TestPreHistoryBackfill:
    def test_configuration_predating_history_is_captured(self, monkeypatch):
        """
        A profile that already existed gets its prior state cut as r1, so
        enabling history does not lose the state history was enabled to protect.
        """
        _make_table()
        # Simulate a profile written by a release without revision history.
        manager = _manager(monkeypatch, with_bucket=False)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("pre-existing"), version="p"
        )
        assert manager.list_revisions("p") == []

        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("new"), version="p")

        revisions = manager.list_revisions("p")
        assert [r["revision"] for r in revisions] == [2, 1]
        assert _notes_of(manager.get_revision("p", 1)) == "pre-existing"
        assert _notes_of(manager.get_revision("p", 2)) == "new"
        # Only the new content is published; the backfill is history, not current.
        assert {r["revision"]: r["published"] for r in revisions} == {2: True, 1: False}

    def test_an_upgrade_that_changes_nothing_leaves_one_revision(self, monkeypatch):
        """
        The common upgrade case: the shipped configuration is identical, so the
        pre-history snapshot IS the current configuration and there is no reason
        to store it twice.
        """
        _make_table()
        manager = _manager(monkeypatch, with_bucket=False)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("same"), version="p")

        manager = _manager(monkeypatch, with_bucket=True)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("same"), version="p")

        revisions = manager.list_revisions("p")
        assert [r["revision"] for r in revisions] == [1]
        assert revisions[0]["published"] is True
        assert _notes_of(manager.get_revision("p", 1)) == "same"

    def test_backfill_happens_only_once(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch, with_bucket=False)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("pre"), version="p")

        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("b"), version="p")
        assert [r["revision"] for r in manager.list_revisions("p")] == [3, 2, 1]


@pytest.mark.unit
@mock_aws
class TestRestore:
    def test_restore_is_forward_only(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("good"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("bad"), version="p")

        new_revision = manager.restore_revision("p", 1, created_by="author@example.com")

        assert new_revision == 3
        assert manager.get_configuration(CONFIG_TYPE_CONFIG, "p").notes == "good"
        # The replaced state is still inspectable.
        assert _notes_of(manager.get_revision("p", 2)) == "bad"
        assert manager.list_revisions("p")[0]["notes"] == "Restored from r1"

    def test_restoring_a_missing_revision_is_refused(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        with pytest.raises(ValueError, match="no longer available"):
            manager.restore_revision("p", 99)


@pytest.mark.unit
@mock_aws
class TestPinnedResolution:
    """Reading a pinned revision, which is what makes a run reproducible."""

    def test_a_pinned_revision_is_loaded_instead_of_the_head(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("old"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("new"), version="p")

        assert manager.get_merged_configuration("p").notes == "new"
        assert manager.get_merged_configuration("p", revision=1).notes == "old"

    def test_an_unavailable_pinned_revision_raises_rather_than_falling_back(
        self, monkeypatch
    ):
        """
        Silently processing under the wrong configuration is worse than failing:
        the run would look successful and its numbers would enter a comparison.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        with pytest.raises(ValueError, match="not available"):
            manager.get_merged_configuration("p", revision=99)

    def test_get_config_passes_the_revision_through(self, monkeypatch):
        """The pipeline's entry point is get_config(), not the manager."""
        from idp_common.config import get_config

        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("old"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("new"), version="p")

        assert get_config(as_model=True, version="p", revision=1).notes == "old"
        assert get_config(as_model=True, version="p").notes == "new"

    def test_published_revision_resolution(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("b"), version="p")
        assert manager.resolve_published_revision("p") == 2

    def test_published_revision_is_none_without_history(self, monkeypatch):
        """An older deployment: consumers fall back to the profile head."""
        _make_table()
        manager = _manager(monkeypatch, with_bucket=False)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        assert manager.resolve_published_revision("p") is None


def _classes_config(note):
    """A full config whose classes carry numbers, which storage stringifies."""
    return IDPConfig(
        notes=note,
        classes=[
            {
                "$id": "Invoice",
                "x-aws-idp-document-type": "Invoice",
                "type": "object",
                "description": "An invoice",
                "properties": {
                    "total": {
                        "type": "number",
                        "description": "Amount due",
                        "x-aws-idp-confidence-threshold": 0.85,
                        "x-aws-idp-evaluation-weight": 2,
                    },
                    "code": {"type": "string", "enum": ["01", "02"]},
                },
            }
        ],
    )


def _expire_body(profile, revision):
    """What the Configuration bucket's lifecycle rule does after DataRetentionInDays."""
    boto3.client("s3", region_name="us-east-1").delete_object(
        Bucket=BUCKET, Key=ConfigRevisionStore.body_key(profile, revision)
    )


def _forget_stored_hash(manager, profile, revision):
    """Make an entry look like one cut before revisions recorded a stored hash."""
    assert manager.revisions.update_entry(profile, revision, storedHash=None)


@pytest.mark.unit
@mock_aws
class TestExpiredPublishedBody:
    """
    Revision bodies expire under the Configuration bucket's lifecycle rule, and every
    new document is pinned to its profile's published revision. A profile that is
    not saved within the retention window must keep processing, and a revision
    number must never be put on a configuration it did not record.
    """

    def test_an_expired_published_body_is_served_from_the_head(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _classes_config("live"), version="p"
        )
        _expire_body("p", 1)

        config = manager.get_merged_configuration("p", revision=1)

        assert config.notes == "live"
        assert config.classes[0]["properties"]["code"]["enum"] == ["01", "02"]
        assert _notes_of(manager.get_revision("p", 1)) == "live"

    def test_the_pipeline_entry_point_recovers_too(self, monkeypatch):
        from idp_common.config import get_config

        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _classes_config("live"), version="p"
        )
        _expire_body("p", 1)

        assert get_config(as_model=True, version="p", revision=1).notes == "live"

    def test_an_older_expired_revision_still_raises(self, monkeypatch):
        """Only the published revision can be proven identical to the head."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("old"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("new"), version="p")
        _expire_body("p", 1)

        with pytest.raises(ValueError, match="not available"):
            manager.get_merged_configuration("p", revision=1)
        assert manager.get_revision("p", 1) is None

    def test_a_head_rewritten_without_a_revision_is_refused(self, monkeypatch):
        """
        A writer with revision history disabled updates the head and cuts nothing,
        so the head is newer than the published revision it still points at.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("r1"), version="p")
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("unrecorded"), version="p", cut_revision=False
        )
        _expire_body("p", 1)

        with pytest.raises(ValueError, match="not available"):
            manager.get_merged_configuration("p", revision=1)

    def test_the_failure_names_expiry_and_how_each_kind_of_profile_recovers(
        self, monkeypatch
    ):
        """
        A failed document's error is what the operator reads, and the editor cannot
        save `default` or a stack-managed profile, so the remedy covers those too.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("r1"), version="p")
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("unrecorded"), version="p", cut_revision=False
        )
        _expire_body("p", 1)

        with pytest.raises(ValueError) as failure:
            manager.get_merged_configuration("p", revision=1)

        message = str(failure.value)
        assert "expired under the Configuration bucket's DataRetentionInDays" in message
        assert EXPIRED_REVISION_REMEDY in message
        assert "'default'" in EXPIRED_REVISION_REMEDY
        assert "stack-managed" in EXPIRED_REVISION_REMEDY

    def test_a_legacy_revision_is_recovered_when_the_head_is_untouched(
        self, monkeypatch
    ):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _classes_config("live"), version="p"
        )
        _forget_stored_hash(manager, "p", 1)
        _expire_body("p", 1)

        assert manager.get_merged_configuration("p", revision=1).notes == "live"

    def test_a_legacy_revision_is_refused_once_the_head_was_rewritten(
        self, monkeypatch
    ):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("r1"), version="p")
        _forget_stored_hash(manager, "p", 1)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("unrecorded"), version="p", cut_revision=False
        )
        _expire_body("p", 1)

        with pytest.raises(ValueError, match="not available"):
            manager.get_merged_configuration("p", revision=1)

    def test_an_unchanged_save_records_the_stored_hash_from_the_body(self, monkeypatch):
        """
        Stack deployments re-save `default` and managed profiles unchanged, which
        rewrites the head without cutting a revision. The published revision must
        stay recoverable afterwards.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")
        _forget_stored_hash(manager, "p", 1)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")

        assert [r["revision"] for r in manager.list_revisions("p")] == [1]
        assert manager.revisions.get_entry("p", 1)["storedHash"]
        _expire_body("p", 1)
        assert manager.get_merged_configuration("p", revision=1).notes == "live"

    def test_an_unchanged_save_never_vouches_for_an_unrecorded_head(self, monkeypatch):
        """The refresh is computed from the revision's body, not from the head."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("r1"), version="p")
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("unrecorded"), version="p", cut_revision=False
        )
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("unrecorded"), version="p"
        )
        _expire_body("p", 1)

        with pytest.raises(ValueError, match="not available"):
            manager.get_merged_configuration("p", revision=1)

    def test_an_unchanged_save_after_expiry_keeps_a_legacy_revision_servable(
        self, monkeypatch
    ):
        """
        The deployment that installs stored hashes re-saves `default` and managed
        profiles unchanged, possibly after a revision's body has expired. That save
        rewrites the head, which defeats the legacy rule, so the hash is taken from
        the head it replaced, which the rule still proved.
        """
        from idp_common.config.configuration_manager import _stored_content_hash

        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")
        _forget_stored_hash(manager, "p", 1)
        _expire_body("p", 1)

        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")
        index_seq = manager.revisions._read_index_item("p")["IndexSeq"]
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")

        assert [r["revision"] for r in manager.list_revisions("p")] == [1]
        assert manager.revisions._read_index_item("p")["IndexSeq"] == index_seq
        stored_hash = manager.revisions.get_entry("p", 1)["storedHash"]
        assert stored_hash == _stored_content_hash(_head_item(manager, "p"))
        assert manager.get_merged_configuration("p", revision=1).notes == "live"

    @pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "stored-hash"])
    def test_an_unchanged_save_after_expiry_never_vouches_for_an_unproven_head(
        self, monkeypatch, legacy
    ):
        """The head the save replaced must itself be proven, by either rule."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("r1"), version="p")
        if legacy:
            _forget_stored_hash(manager, "p", 1)
        recorded = manager.revisions.get_entry("p", 1).get("storedHash")
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("unrecorded"), version="p", cut_revision=False
        )
        _expire_body("p", 1)

        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _config("unrecorded"), version="p"
        )

        assert manager.revisions.get_entry("p", 1).get("storedHash") == recorded
        with pytest.raises(ValueError, match="not available"):
            manager.get_merged_configuration("p", revision=1)

    def test_an_unchanged_save_after_expiry_follows_a_change_in_how_heads_are_stored(
        self, monkeypatch
    ):
        """
        A release that stores the same configuration differently changes the hash of
        every head it re-saves. Once the body has expired, the new hash can only come
        from the head that save replaced, which the old hash still proved.
        """
        from idp_common.config.configuration_manager import _stored_content_hash
        from idp_common.config.models import ConfigurationRecord

        _make_table()
        manager = _manager(monkeypatch)
        with monkeypatch.context() as earlier_release:
            earlier_release.setattr(
                ConfigurationRecord,
                "_omit_rollback_hostile_defaults",
                staticmethod(lambda model, dumped: dumped),
            )
            manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")
        earlier_hash = manager.revisions.get_entry("p", 1)["storedHash"]
        _expire_body("p", 1)

        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")

        stored_hash = manager.revisions.get_entry("p", 1)["storedHash"]
        assert stored_hash != earlier_hash
        assert stored_hash == _stored_content_hash(_head_item(manager, "p"))
        assert manager.get_merged_configuration("p", revision=1).notes == "live"

    def test_an_unchanged_save_after_expiry_never_vouches_for_the_head_it_wrote(
        self, monkeypatch
    ):
        """
        `True == 1`, so a save that replaces one with the other inside a class counts
        as unchanged and cuts nothing, yet the head then stores `"1"` where the
        revision held `true`. The hash comes from the configuration of the head the
        save replaced, so the head it wrote is not passed off as the revision.
        """

        def flagged(value):
            return IDPConfig(
                notes="live",
                classes=[
                    {
                        "$id": "Invoice",
                        "x-aws-idp-document-type": "Invoice",
                        "type": "object",
                        "x-example-flag": value,
                    }
                ],
            )

        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, flagged(True), version="p")
        _expire_body("p", 1)

        manager.save_configuration(CONFIG_TYPE_CONFIG, flagged(1), version="p")

        assert [r["revision"] for r in manager.list_revisions("p")] == [1]
        with pytest.raises(ValueError, match="not available"):
            manager.get_merged_configuration("p", revision=1)

    def test_an_unchanged_save_after_expiry_needs_the_replaced_head_to_be_latest(
        self, monkeypatch
    ):
        """
        A pinned read requires the head to name the revision as both published and
        newest, so the head a save replaced must too. A number allocated by a cut
        that never published is what leaves the two apart.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")
        _forget_stored_hash(manager, "p", 1)
        manager.revisions.next_number("p")
        _expire_body("p", 1)

        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")

        assert not manager.revisions.get_entry("p", 1).get("storedHash")

    def test_a_published_cut_records_the_heads_stored_hash(self, monkeypatch):
        from idp_common.config.configuration_manager import _stored_content_hash

        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, _classes_config("live"), version="p"
        )
        head = manager._decompress_item(
            manager.table.get_item(Key={"Configuration": "Config#p"})["Item"]
        )

        entry = manager.revisions.get_entry("p", 1)
        assert entry["storedHash"] == _stored_content_hash(head)
        assert entry["storedHash"] == manager._stored_hash_of_body(
            "p", manager.get_revision("p", 1)
        )

    def test_the_stored_hash_of_a_given_head_never_changes(self):
        """
        Recorded hashes outlive the code that wrote them. A change to how an item is
        digested leaves every recorded hash unmatched at once, so each published
        revision whose body has already expired stops being served on upgrade.
        Change it only together with a way to read the hashes already recorded.

        Which attributes count as metadata is part of the digest, so the set is
        pinned too. Moving a configuration field into it changes the hash of every
        real head that carries the field, which the digest of the fixed head below
        shows only for the few fields it carries.
        """
        from idp_common.config.configuration_manager import (
            _DYNAMODB_METADATA_FIELDS,
            _stored_content_hash,
        )

        head = {
            "Configuration": "Config#p",
            "UpdatedAt": "2026-10-02T10:00:00Z",
            "Managed": True,
            "notes": "live",
            "extraction": {"temperature": "0.0", "enabled": True},
            "classes": [{"$id": "Invoice", "enum": ["01", "02"]}],
            "_config_format": "full",
        }
        rewritten = {**head, "UpdatedAt": "2027-01-01T00:00:00Z", "Description": "x"}

        assert sorted(_DYNAMODB_METADATA_FIELDS) == [
            "BdaLastSyncedAt",
            "BdaProjectArn",
            "BdaSyncStatus",
            "Configuration",
            "CreatedAt",
            "Description",
            "IsActive",
            "LatestRevision",
            "Managed",
            "PublishedRevision",
            "UpdatedAt",
        ]
        assert _stored_content_hash(head) == "b26a02095c8935891409b2bc7429c1fa"
        assert _stored_content_hash(rewritten) == _stored_content_hash(head)

    def test_the_stored_hash_stays_out_of_the_revision_list(self, monkeypatch):
        """The revision list is what the API returns; the hash is internal proof."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")

        assert manager.revisions.get_entry("p", 1)["storedHash"]
        assert "storedHash" not in manager.list_revisions("p")[0]

    def test_an_entry_is_none_when_not_retained_or_history_is_disabled(
        self, monkeypatch
    ):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")

        assert manager.revisions.get_entry("p", 2) is None
        disabled = ConfigRevisionStore(manager.table, bucket="")
        assert disabled.get_entry("p", 1) is None

    def test_a_profile_with_no_head_has_nothing_to_stand_in(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")
        _expire_body("p", 1)
        manager.table.delete_item(Key={"Configuration": "Config#p"})

        assert manager._published_body_from_head("p", 1) is None

    def test_a_published_revision_with_no_index_entry_is_not_rebuilt(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")
        _expire_body("p", 1)
        assert manager.revisions.remove_entry("p", 1)

        with pytest.raises(ValueError, match="not available"):
            manager.get_merged_configuration("p", revision=1)

    def test_the_refresh_does_nothing_without_a_published_revision(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")
        monkeypatch.setattr(manager, "resolve_published_revision", lambda p: None)
        updates = []
        monkeypatch.setattr(
            manager.revisions, "update_entry", lambda *a, **k: updates.append(k)
        )

        manager._refresh_published_stored_hash("p")

        assert updates == []

    def test_the_refresh_does_nothing_without_an_index_entry(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")
        assert manager.revisions.remove_entry("p", 1)
        _expire_body("p", 1)
        updates = []
        monkeypatch.setattr(
            manager.revisions, "update_entry", lambda *a, **k: updates.append(k)
        )

        manager._refresh_published_stored_hash("p", _head_item(manager, "p"))

        assert updates == []

    def test_the_refresh_does_nothing_after_expiry_without_the_replaced_head(
        self, monkeypatch
    ):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")
        _forget_stored_hash(manager, "p", 1)
        _expire_body("p", 1)

        manager._refresh_published_stored_hash("p")

        assert not manager.revisions.get_entry("p", 1).get("storedHash")

    def test_a_refresh_that_fails_is_logged_and_never_raised(self, monkeypatch, caplog):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("live"), version="p")

        def unreadable(profile, revision):
            raise RuntimeError("index unreadable")

        monkeypatch.setattr(manager.revisions, "get_entry", unreadable)

        with caplog.at_level(logging.WARNING):
            manager._refresh_published_stored_hash("p")

        assert "Could not refresh the stored-content hash of 'p'" in caplog.text

    def test_timestamps_parse_to_aware_datetimes_or_none(self):
        from idp_common.config.configuration_manager import _parse_timestamp

        assert _parse_timestamp(None) is None
        assert _parse_timestamp("") is None
        assert _parse_timestamp(20261002) is None
        assert _parse_timestamp("not a timestamp") is None
        naive = _parse_timestamp("2026-10-02T10:00:00")
        assert naive is not None and naive.tzinfo is datetime.timezone.utc
        assert _parse_timestamp("2026-10-02T10:00:00Z") == naive


_REPO_ROOT = Path(__file__).resolve().parents[5]
_MANAGED_PROFILES = sorted(
    (_REPO_ROOT / "config_library" / "managed_config").glob("*/config.yaml")
)
_LIBRARY_PROFILES = _MANAGED_PROFILES + sorted(
    (_REPO_ROOT / "config_library" / "unified").glob("*/config.yaml")
)


def _profile_id(path):
    return f"{path.parent.parent.name}/{path.parent.name}"


def _library_profile(path):
    """A config_library profile, merged with system defaults as a deployment does."""
    raw = yaml.safe_load(path.read_text())
    raw.pop("description", None)
    raw.pop("pricing", None)
    return merge_config_with_defaults(raw, pattern="pattern-2")


def _head_item(manager, profile):
    return manager._decompress_item(
        manager.table.get_item(Key={"Configuration": f"Config#{profile}"})["Item"]
    )


@pytest.mark.unit
@mock_aws
class TestLibraryProfileStoredHash:
    """
    The proof rests on two code paths agreeing: the hash `_write_record` takes of the
    head it stores, and the hash `_stored_hash_of_body` derives from a revision's
    body. A disagreement raises nothing at save time; the profile fails once its
    body expires. Both are therefore pinned on every profile the stack ships, which
    carry nested thresholds and numeric and boolean leaves in every section.
    """

    def test_the_shipped_profiles_are_found(self):
        """An empty parameter list would skip the tests below rather than fail."""
        assert _MANAGED_PROFILES
        assert len(_LIBRARY_PROFILES) > len(_MANAGED_PROFILES)

    @pytest.mark.parametrize("path", _LIBRARY_PROFILES, ids=_profile_id)
    def test_the_cut_the_head_and_the_body_agree_on_the_hash(self, monkeypatch, path):
        """The pricing table is added so its numeric leaves go through storage too."""
        from idp_common.config.configuration_manager import _stored_content_hash

        _make_table()
        manager = _manager(monkeypatch)
        config = _library_profile(path)
        config["pricing"] = yaml.safe_load(
            (_REPO_ROOT / "config_library" / "pricing.yaml").read_text()
        )["pricing"]
        manager.save_configuration(CONFIG_TYPE_CONFIG, config, version="p")

        recorded = manager.revisions.get_entry("p", 1)["storedHash"]
        assert recorded == _stored_content_hash(_head_item(manager, "p"))
        assert recorded == manager._stored_hash_of_body(
            "p", manager.revisions.get_body("p", 1)
        )

    @pytest.mark.parametrize(
        "expired_first", [False, True], ids=["body-live", "body-expired"]
    )
    @pytest.mark.parametrize("path", _LIBRARY_PROFILES, ids=_profile_id)
    def test_a_redeployed_profile_is_served_once_its_body_expires(
        self, monkeypatch, path, expired_first
    ):
        """
        A shipped profile's revision cut before stored hashes existed, then re-saved
        unchanged by the deployment that upgrades the stack, which does this to each
        managed profile and to `default`, built from a unified preset. That save
        records the hash from the body or, if the body has already expired, from the
        head it replaced; where it does not recognise the configuration as
        unchanged, it cuts a new revision instead. Either way the current revision
        must outlive its body, as exactly the configuration the head serves.
        """
        from idp_common.config.configuration_manager import _stored_content_hash

        _make_table()
        manager = _manager(monkeypatch)
        config = _library_profile(path)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, copy.deepcopy(config), version="p"
        )
        _forget_stored_hash(manager, "p", 1)
        if expired_first:
            _expire_body("p", 1)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, copy.deepcopy(config), version="p"
        )
        published = manager.resolve_published_revision("p")

        stored_hash = manager.revisions.get_entry("p", published)["storedHash"]
        assert stored_hash == _stored_content_hash(_head_item(manager, "p"))
        _expire_body("p", published)
        served = manager.get_merged_configuration("p", revision=published)
        head = manager.get_merged_configuration("p")
        assert served is not None and head is not None
        assert served.model_dump() == head.model_dump()


@pytest.mark.unit
@mock_aws
class TestRetention:
    def test_cap_prunes_oldest_first(self, monkeypatch):
        _make_table()
        monkeypatch.setenv("CONFIG_REVISION_CAP", "3")
        manager = _manager(monkeypatch)
        for i in range(6):
            manager.save_configuration(
                CONFIG_TYPE_CONFIG, _config(f"v{i}"), version="p"
            )

        assert [r["revision"] for r in manager.list_revisions("p")] == [6, 5, 4]
        # The pruned bodies are gone from S3 too, not just de-indexed.
        assert manager.get_revision("p", 1) is None

    def test_labeled_and_pinned_revisions_survive_the_cap(self, monkeypatch):
        _make_table()
        monkeypatch.setenv("CONFIG_REVISION_CAP", "3")
        manager = _manager(monkeypatch)
        for i in range(3):
            manager.save_configuration(
                CONFIG_TYPE_CONFIG, _config(f"v{i}"), version="p"
            )
        assert manager.label_revision("p", 1, label="known good") is True
        assert manager.mark_revision_pinned("p", 2) is True
        for i in range(3, 6):
            manager.save_configuration(
                CONFIG_TYPE_CONFIG, _config(f"v{i}"), version="p"
            )

        kept = {r["revision"] for r in manager.list_revisions("p")}
        # r3 falls outside the cap and is unprotected, so it goes; r1 (labeled)
        # and r2 (pinned by a test run) must not.
        assert kept == {6, 5, 4, 2, 1}
        assert manager.get_revision("p", 1) is not None
        assert manager.get_revision("p", 3) is None

    def test_published_revision_survives_a_cap_of_one(self, monkeypatch):
        _make_table()
        monkeypatch.setenv("CONFIG_REVISION_CAP", "1")
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("b"), version="p")
        revisions = manager.list_revisions("p")
        assert [r["revision"] for r in revisions] == [2]
        assert revisions[0]["published"] is True


@pytest.mark.unit
@mock_aws
class TestDelete:
    def test_current_configuration_cannot_be_deleted(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        with pytest.raises(ValueError, match="current configuration"):
            manager.delete_revision("p", 1)

    def test_deleting_an_older_revision_removes_body_and_entry(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("b"), version="p")

        assert manager.delete_revision("p", 1) is True
        assert [r["revision"] for r in manager.list_revisions("p")] == [2]
        assert manager.get_revision("p", 1) is None

    def test_deleting_a_profile_drops_its_history(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("b"), version="p")

        manager.delete_configuration(CONFIG_TYPE_CONFIG, "p")

        assert manager.list_revisions("p") == []
        keys = boto3.client("s3", region_name="us-east-1").list_objects_v2(
            Bucket=BUCKET
        )
        assert keys.get("KeyCount", 0) == 0


@pytest.mark.unit
@mock_aws
class TestProfileListIsolation:
    def test_revision_records_never_appear_as_profiles(self, monkeypatch):
        """
        list_config_versions() feeds the scope-filtered dropdowns. A revision
        index item leaking in would look to users like a profile with no config.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="lending")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("b"), version="lending")

        names = {v["versionName"] for v in manager.list_config_versions()}
        assert names == {"lending"}

    def test_active_pointer_is_not_a_profile(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.activate_version("p")

        names = {v["versionName"] for v in manager.list_config_versions()}
        assert names == {"p"}
        assert "__active" not in names

    def test_reserved_profile_name_is_refused(self, monkeypatch):
        """Otherwise a user could overwrite the active-profile pointer."""
        _make_table()
        manager = _manager(monkeypatch)
        with pytest.raises(ValueError, match="reserved"):
            manager.save_configuration(
                CONFIG_TYPE_CONFIG, _config("a"), version="__active"
            )

    def test_profile_list_exposes_revision_counters(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        entry = next(
            v for v in manager.list_config_versions() if v["versionName"] == "p"
        )
        assert entry["latestRevision"] == 1
        assert entry["publishedRevision"] == 1


@pytest.mark.unit
@mock_aws
class TestActivePointer:
    def test_activation_writes_the_pointer_and_resolution_reads_it(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.activate_version("p")

        pointer = (
            boto3.resource("dynamodb", region_name="us-east-1")
            .Table(TABLE)
            .get_item(Key={"Configuration": ACTIVE_POINTER_KEY})["Item"]
        )
        assert pointer["ActiveVersion"] == "p"
        assert manager.resolve_active_version() == "p"

    def test_resolution_falls_back_to_the_scan_without_a_pointer(self, monkeypatch):
        """A stack that has not activated anything since the upgrade still works."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE).update_item(
            Key={"Configuration": "Config#p"},
            UpdateExpression="SET IsActive = :t",
            ExpressionAttributeValues={":t": True},
        )
        assert manager.resolve_active_version() == "p"


@pytest.mark.unit
@mock_aws
class TestHistoryDisabled:
    def test_saves_work_without_a_configuration_bucket(self, monkeypatch):
        """
        History is optional infrastructure. An older deployment with no bucket
        configured must keep saving configurations normally.
        """
        _make_table()
        manager = _manager(monkeypatch, with_bucket=False)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")

        assert manager.get_configuration(CONFIG_TYPE_CONFIG, "p").notes == "a"
        assert manager.list_revisions("p") == []
        assert manager.revisions.enabled is False

    def test_a_failing_revision_store_does_not_fail_the_save(self, monkeypatch):
        """Losing a history entry is recoverable; refusing a save is an outage."""
        _make_table()
        manager = _manager(monkeypatch)

        def explode(*args, **kwargs):
            raise RuntimeError("s3 is having a day")

        monkeypatch.setattr(manager.revisions, "cut", explode)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        assert manager.get_configuration(CONFIG_TYPE_CONFIG, "p").notes == "a"


@pytest.mark.unit
@mock_aws
class TestStoreInternals:
    def test_revision_numbers_are_allocated_atomically(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        store = manager.revisions
        # Two allocations in a row must never collide, which is what protects two
        # people saving at the same moment.
        assert store.next_number("p") != store.next_number("p")

    def test_allocation_refuses_to_create_a_phantom_profile(self, monkeypatch):
        """
        An ADD on a missing item would create Config#<name> holding only a
        counter — a profile with no configuration, visible in the profile list.
        """
        _make_table()
        manager = _manager(monkeypatch)
        with pytest.raises(Exception):
            manager.revisions.next_number("never-saved")
        assert manager.list_config_versions() == []

    def test_profile_names_cannot_escape_the_revision_prefix(self, monkeypatch):
        _make_table()
        manager = _manager(monkeypatch)
        with pytest.raises(ValueError, match="Invalid configuration profile name"):
            ConfigRevisionStore.body_key("../../etc/passwd", 1)
        with pytest.raises(ValueError, match="Invalid configuration profile name"):
            manager.revisions.index_key("has space")

    def test_a_403_on_a_body_read_names_both_possible_causes(self, monkeypatch):
        """
        Without s3:ListBucket on the bucket, S3 answers a GetObject for a MISSING
        key with 403 AccessDenied rather than 404. A pruned or never-cut pinned
        revision then read as an IAM failure in OCR, and the clear "not
        available" path never fired (#878). The store must not map 403 to None
        either — a real permission defect must not pass as "revision missing".
        """
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError

        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        store = manager.revisions
        store._s3 = MagicMock()
        store._s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no ListBucket"}},
            "GetObject",
        )

        with pytest.raises(PermissionError, match="s3:ListBucket") as info:
            store.get_body("p", 1)
        assert "config_revisions/p/000001.json.gz" in str(info.value)
        assert isinstance(info.value.__cause__, ClientError)

    def test_a_404_on_a_body_read_is_simply_missing(self, monkeypatch):
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError

        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        store = manager.revisions
        store._s3 = MagicMock()
        store._s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "gone"}}, "GetObject"
        )

        assert store.get_body("p", 1) is None

    def test_body_key_is_zero_padded_for_stable_ordering(self):
        assert (
            ConfigRevisionStore.body_key("p", 7) == "config_revisions/p/000007.json.gz"
        )

    def test_confidence_fingerprint_ignores_irrelevant_edits(self, monkeypatch):
        """
        Editing something that does not change what a confidence number means
        keeps the fingerprint stable, so measurements stay comparable across the
        revision.
        """
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="a"), version="p"
        )
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="b"), version="p"
        )
        revisions = manager.list_revisions("p")
        assert (
            revisions[0]["confidenceFingerprint"]
            == revisions[1]["confidenceFingerprint"]
        )

    def test_confidence_fingerprint_changes_with_the_extraction_model(self):
        """A model swap must NOT inherit a curve measured under the old model."""
        from idp_common.config.revisions import confidence_fingerprint

        base = {"extraction": {"model": "model-a"}, "assessment": {"enabled": True}}
        swapped = {"extraction": {"model": "model-b"}, "assessment": {"enabled": True}}
        prompt_edit = {
            "extraction": {"model": "model-a", "task_prompt": "different"},
            "assessment": {"enabled": True},
        }
        assert confidence_fingerprint(base) != confidence_fingerprint(swapped)
        assert confidence_fingerprint(base) == confidence_fingerprint(prompt_edit)

    def test_confidence_fingerprint_survives_a_dynamodb_round_trip(self):
        """
        The same configuration must fingerprint identically whichever route it
        arrived by.

        A config reaches this function either straight from a save (JSON, so
        ``float``) or read back from DynamoDB, whose only numeric type is
        ``Decimal``. ``json.dumps`` cannot serialize ``Decimal`` and the
        ``default=str`` fallback stringified it, so ``temperature: 0.0`` hashed
        as the number on one route and as ``"0.0"`` on the other — one
        configuration with two fingerprints, which is exactly what a fingerprint
        exists to rule out. ``Decimal("0")`` vs ``Decimal("0.0")`` gave a third.
        """
        from idp_common.config.revisions import confidence_fingerprint

        from_save = {
            "extraction": {"model": "m", "temperature": 0.0, "top_k": 5, "top_p": 0.1},
            "assessment": {"enabled": True, "max_tokens": 4096},
        }
        from_dynamodb = {
            "extraction": {
                "model": "m",
                "temperature": Decimal("0.0"),
                "top_k": Decimal("5"),
                "top_p": Decimal("0.1"),
            },
            "assessment": {"enabled": True, "max_tokens": Decimal("4096")},
        }
        # DynamoDB preserves the scale it was given, so the same zero comes back
        # as either of these depending on how it was written.
        unscaled_zero = {
            "extraction": {
                "model": "m",
                "temperature": Decimal("0"),
                "top_k": Decimal("5"),
                "top_p": Decimal("0.1"),
            },
            "assessment": {"enabled": True, "max_tokens": Decimal("4096")},
        }

        assert (
            confidence_fingerprint(from_save)
            == confidence_fingerprint(from_dynamodb)
            == confidence_fingerprint(unscaled_zero)
        )

    def test_confidence_fingerprint_still_separates_real_numeric_changes(self):
        """
        Normalizing types must not flatten a genuine change in a sampling value.

        The guard against fixing the round-trip by making the hash insensitive to
        the numbers it exists to track.
        """
        from idp_common.config.revisions import confidence_fingerprint

        base = {"extraction": {"model": "m", "temperature": 0.0, "top_p": 0.1}}
        hotter = {"extraction": {"model": "m", "temperature": 0.7, "top_p": 0.1}}
        narrower = {"extraction": {"model": "m", "temperature": 0.0, "top_p": 0.9}}

        assert confidence_fingerprint(base) != confidence_fingerprint(hotter)
        assert confidence_fingerprint(base) != confidence_fingerprint(narrower)

    def test_fingerprints_do_not_conflate_booleans_with_numbers(self):
        """
        ``bool`` is an ``int`` subclass, so numeric normalization must special-case
        it or ``enabled: true`` becomes indistinguishable from ``enabled: 1``.
        """
        from idp_common.config.revisions import confidence_fingerprint

        assert confidence_fingerprint(
            {"assessment": {"enabled": True}}
        ) != confidence_fingerprint({"assessment": {"enabled": 1}})

    def test_class_fingerprint_survives_a_dynamodb_round_trip(self):
        """
        Same hazard as the confidence fingerprint, same fix.

        Classes carry no numerics in the shipped sample configs, so this is
        latent rather than active today — but ``classFingerprint`` is the BDA
        resync signal, and a fingerprint that changes on a round-trip would
        eventually report a resync as required when nothing had changed.
        """
        from idp_common.config.revisions import class_fingerprint

        assert class_fingerprint(
            {"classes": [{"name": "invoice", "threshold": 0.8}]}
        ) == class_fingerprint(
            {"classes": [{"name": "invoice", "threshold": Decimal("0.8")}]}
        )

    def test_class_fingerprint_tracks_document_classes(self, monkeypatch):
        """The BDA resync signal: same classes → same fingerprint."""
        _make_table()
        manager = _manager(monkeypatch)
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("a"), version="p")
        manager.save_configuration(CONFIG_TYPE_CONFIG, _config("b"), version="p")
        revisions = manager.list_revisions("p")
        assert revisions[0]["classFingerprint"] == revisions[1]["classFingerprint"]
