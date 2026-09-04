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

from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import ACTIVE_POINTER_KEY, CONFIG_TYPE_CONFIG
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
