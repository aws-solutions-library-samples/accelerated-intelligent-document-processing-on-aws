# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Discovery's save is the configuration documents run, not only the one shown (#1778).

Reported shape: a profile created as a copy of `default` (r1, the default preset's
classes), then single-document discovery in **Replace** mode saving one `Form-I-9`
class into it. The profile head held only `Form-I-9`, but no revision was cut, so
`PublishedRevision` stayed 1 and the next document, pinned to r1, was classified
against the default classes and split into a `W2` and a `Payslip` section.

These tests drive the real save path — the upload resolver's Replace-mode clear,
then `ClassesDiscovery._merge_and_save_class` — and read the configuration back the
way the pipeline does: resolve the published revision, then `get_config` pinned to
it. The cause was a missing `CONFIGURATION_BUCKET` on the deployed Lambdas, so the
template half is asserted in `scripts/tests/test_config_profile_writer_wiring.py`;
what is asserted here is that, with the bucket, both save modes produce a published
revision equal to the head, and that without it the divergence is no longer silent.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Dict, List

import boto3
import pytest
from moto import mock_aws

from idp_common.config import get_config
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.models import IDPConfig
from idp_common.config.revisions import ConfigRevisionStore, RevisionNotRecordedError

_REPO_ROOT = Path(__file__).resolve().parents[5]
_DISCOVERY_UPLOAD_DIR = (
    _REPO_ROOT
    / "nested"
    / "api-resolvers"
    / "src"
    / "lambda"
    / "discovery_upload_resolver"
)

TABLE = "discovery-revision-table"
BUCKET = "discovery-revision-bucket"
PROFILE = "MyI9"


def _class(class_id: str) -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": class_id,
        "x-aws-idp-document-type": class_id,
        "type": "object",
        "description": f"{class_id} document",
        "properties": {"Field": {"type": "string", "description": "a field"}},
    }


def _env(monkeypatch, with_bucket: bool) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)
    if not with_bucket:
        monkeypatch.delenv("CONFIGURATION_BUCKET")


def _create_resources() -> None:
    boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)


def _create_profile_as_copy_of_default() -> None:
    """r1: the profile as the configuration resolver creates it, with history on."""
    manager = ConfigurationManager()
    assert manager.revisions.enabled
    manager.save_configuration(
        CONFIG_TYPE_CONFIG,
        IDPConfig(classes=[_class("W2"), _class("Payslip")]),
        version=PROFILE,
        description="Copied from default",
    )


def _replace_mode_clear() -> None:
    """The upload resolver's `_clear_version_schema`, imported from its own file."""
    sys.path.insert(0, str(_DISCOVERY_UPLOAD_DIR))
    sys.modules.pop("index", None)
    try:
        module = importlib.import_module("index")
        assert Path(module.__file__).parent == _DISCOVERY_UPLOAD_DIR
        module._clear_version_schema(PROFILE, "classes")
    finally:
        sys.path.remove(str(_DISCOVERY_UPLOAD_DIR))
        sys.modules.pop("index", None)


def _discovery_merge(class_id: str) -> None:
    from idp_common.discovery.classes_discovery import ClassesDiscovery

    discovery = object.__new__(ClassesDiscovery)
    discovery.config_manager = ConfigurationManager()
    discovery.version = PROFILE
    discovery._merge_and_save_class(_class(class_id))


def _head_class_ids() -> List[str]:
    raw = ConfigurationManager().get_raw_configuration(CONFIG_TYPE_CONFIG, PROFILE)
    return [c["$id"] for c in raw["classes"]]


def _class_ids_a_new_document_runs() -> List[str]:
    """What the pipeline loads for a document submitted now."""
    published = ConfigurationManager().resolve_published_revision(PROFILE)
    config = get_config(as_model=True, version=PROFILE, revision=published)
    return [c["$id"] for c in config.classes]


@pytest.mark.unit
class TestDiscoverySaveWithHistory:
    @pytest.fixture(autouse=True)
    def _aws(self, monkeypatch):
        _env(monkeypatch, with_bucket=True)
        with mock_aws():
            _create_resources()
            _create_profile_as_copy_of_default()
            yield

    def test_replace_mode_publishes_only_the_discovered_class(self):
        _replace_mode_clear()
        _discovery_merge("Form-I-9")

        assert _head_class_ids() == ["Form-I-9"]
        assert _class_ids_a_new_document_runs() == ["Form-I-9"]
        published = ConfigurationManager().resolve_published_revision(PROFILE)
        assert published is not None and published > 1

    def test_add_mode_publishes_the_existing_classes_and_the_discovered_one(self):
        _discovery_merge("Form-I-9")

        expected = ["W2", "Payslip", "Form-I-9"]
        assert _head_class_ids() == expected
        assert _class_ids_a_new_document_runs() == expected

    def test_every_discovery_save_is_kept_in_history(self):
        _replace_mode_clear()
        _discovery_merge("Form-I-9")

        revisions = ConfigurationManager().list_revisions(PROFILE)
        published = [r["revision"] for r in revisions if r["published"]]
        assert published == [max(r["revision"] for r in revisions)]
        # r1 (the copy), the Replace-mode clear, and the merge.
        assert len(revisions) == 3


def _published() -> int:
    published = ConfigurationManager().resolve_published_revision(PROFILE)
    assert published is not None
    return published


@pytest.mark.unit
class TestASaveThatCannotRecordARevisionIsRefused:
    """A process with no `CONFIGURATION_BUCKET`: what the deployed Lambdas were.

    Saving there used to rewrite the head and leave `PublishedRevision` behind,
    which is the reported divergence. The profile already has history, so the
    save is now refused and the profile is left exactly as it was.
    """

    @pytest.fixture(autouse=True)
    def _aws(self, monkeypatch):
        _env(monkeypatch, with_bucket=True)
        with mock_aws():
            _create_resources()
            _create_profile_as_copy_of_default()
            monkeypatch.delenv("CONFIGURATION_BUCKET")
            yield

    def test_the_discovery_merge_is_refused_and_changes_nothing(self):
        with pytest.raises(RevisionNotRecordedError) as refused:
            _discovery_merge("Form-I-9")

        assert "CONFIGURATION_BUCKET" in str(refused.value)
        assert "r1" in str(refused.value)
        assert _head_class_ids() == ["W2", "Payslip"]
        assert _published() == 1

    def test_an_unchanged_save_is_not_refused(self):
        """It cuts no revision and rewrites an identical head: nothing diverges.

        Stack deployments re-save every managed profile this way.
        """
        manager = ConfigurationManager()
        raw = manager.get_raw_configuration(CONFIG_TYPE_CONFIG, PROFILE)
        manager.save_configuration(CONFIG_TYPE_CONFIG, raw, version=PROFILE)

        assert _head_class_ids() == ["W2", "Payslip"]
        assert _published() == 1

    def test_the_replace_mode_clear_is_refused_and_changes_nothing(self):
        with pytest.raises(RevisionNotRecordedError):
            _replace_mode_clear()

        assert _head_class_ids() == ["W2", "Payslip"]


@pytest.mark.unit
class TestASaveWhoseRevisionFailsIsUndone:
    """History is enabled, but recording this save's revision fails."""

    @pytest.fixture(autouse=True)
    def _aws(self, monkeypatch):
        _env(monkeypatch, with_bucket=True)
        with mock_aws():
            _create_resources()
            _create_profile_as_copy_of_default()
            yield

    def test_a_failed_body_write_puts_the_profile_back(self, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise RuntimeError("S3 refused the revision body")

        monkeypatch.setattr(ConfigRevisionStore, "put_body", refuse)
        with pytest.raises(RevisionNotRecordedError) as refused:
            _discovery_merge("Form-I-9")

        assert "undone" in str(refused.value)
        assert "S3 refused the revision body" in str(refused.value)
        assert _head_class_ids() == ["W2", "Payslip"]
        assert _published() == 1
        assert ConfigurationManager().head_matches_published(PROFILE) is True

    def test_a_revision_recorded_but_not_published_puts_the_profile_back(
        self, monkeypatch
    ):
        # `set_published` logs a failed update rather than raising, so this is
        # the shape a throttled or refused counter update takes.
        monkeypatch.setattr(ConfigRevisionStore, "set_published", lambda *_a: None)
        with pytest.raises(RevisionNotRecordedError) as refused:
            _discovery_merge("Form-I-9")

        assert "not published" in str(refused.value)
        assert _head_class_ids() == ["W2", "Payslip"]
        assert _published() == 1

    def test_the_next_save_after_a_failure_records_a_fresh_revision(self, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise RuntimeError("S3 refused the revision body")

        with monkeypatch.context() as patched:
            patched.setattr(ConfigRevisionStore, "put_body", refuse)
            with pytest.raises(RevisionNotRecordedError):
                _discovery_merge("Form-I-9")

        _discovery_merge("Form-I-9")

        # The failed attempt allocated r2 and the restore kept that counter, so
        # the retry is r3 rather than a second r2 over a number already handed out.
        assert _published() == 3
        assert _class_ids_a_new_document_runs() == ["W2", "Payslip", "Form-I-9"]
        assert ConfigurationManager().head_matches_published(PROFILE) is True


@pytest.mark.unit
def test_a_profile_without_history_still_saves_without_the_bucket(monkeypatch):
    """With no history, documents run the head, so there is nothing to diverge from."""
    _env(monkeypatch, with_bucket=False)
    with mock_aws():
        _create_resources()
        manager = ConfigurationManager()
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="first"), version=PROFILE
        )
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="second"), version=PROFILE
        )
        raw = manager.get_raw_configuration(CONFIG_TYPE_CONFIG, PROFILE)
        assert raw["notes"] == "second"
        assert manager.resolve_published_revision(PROFILE) is None


@pytest.mark.unit
class TestHeadMatchesPublished:
    """The signal the configuration editor shows a warning on."""

    @pytest.fixture(autouse=True)
    def _aws(self, monkeypatch):
        _env(monkeypatch, with_bucket=True)
        with mock_aws():
            _create_resources()
            yield

    def test_a_profile_saved_normally_matches(self):
        _create_profile_as_copy_of_default()
        _discovery_merge("Form-I-9")
        assert ConfigurationManager().head_matches_published(PROFILE) is True

    def test_a_profile_changed_without_a_revision_does_not_match(self):
        # The state the reported profile was left in: a head rewritten after its
        # published revision without a revision of its own. `cut_revision=False`
        # is the one sanctioned way left to write that.
        _create_profile_as_copy_of_default()
        manager = ConfigurationManager()
        manager.save_configuration(
            CONFIG_TYPE_CONFIG,
            IDPConfig(classes=[_class("Form-I-9")]),
            version=PROFILE,
            cut_revision=False,
        )
        assert manager.resolve_published_revision(PROFILE) == 1
        assert manager.head_matches_published(PROFILE) is False

    def test_the_mismatch_clears_once_a_save_records_a_revision(self):
        _create_profile_as_copy_of_default()
        manager = ConfigurationManager()
        manager.save_configuration(
            CONFIG_TYPE_CONFIG,
            IDPConfig(classes=[_class("Form-I-9")]),
            version=PROFILE,
            cut_revision=False,
        )
        _discovery_merge("Form-I-9-Supplement")
        assert manager.head_matches_published(PROFILE) is True
        assert _class_ids_a_new_document_runs() == _head_class_ids()

    def test_nothing_to_compare_is_none_not_false(self, monkeypatch):
        assert ConfigurationManager().head_matches_published(PROFILE) is None
        monkeypatch.delenv("CONFIGURATION_BUCKET")
        manager = ConfigurationManager()
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="no history"), version=PROFILE
        )
        assert manager.head_matches_published(PROFILE) is None


@pytest.mark.unit
class TestAFailureAfterThePublishIsNotUndone:
    """Once the save's revision is published, restoring the old head would invert
    the divergence: the editor showing the old configuration while documents run
    the new one, under a message saying the save was undone."""

    @pytest.fixture(autouse=True)
    def _aws(self, monkeypatch):
        _env(monkeypatch, with_bucket=True)
        with mock_aws():
            _create_resources()
            _create_profile_as_copy_of_default()
            yield

    def test_a_failed_prune_does_not_fail_the_save(self, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise RuntimeError("index rewrite throttled")

        monkeypatch.setattr(ConfigRevisionStore, "prune", refuse)
        _discovery_merge("Form-I-9")

        assert _head_class_ids() == ["W2", "Payslip", "Form-I-9"]
        assert _class_ids_a_new_document_runs() == _head_class_ids()
        assert ConfigurationManager().head_matches_published(PROFILE) is True

    def test_the_store_reports_a_published_revision_despite_a_failed_prune(
        self, monkeypatch
    ):
        """The store-level half: `cut` returns the revision it published."""

        def refuse(*_args, **_kwargs):
            raise RuntimeError("index rewrite throttled")

        monkeypatch.setattr(ConfigRevisionStore, "prune", refuse)
        store = ConfigurationManager().revisions
        revision = store.cut(PROFILE, {"notes": "direct"})
        assert revision == 2
        assert _published() == 2

    def test_a_failure_after_the_publish_keeps_the_saved_head(self, monkeypatch):
        def fail_after_publish(*_args, **_kwargs):
            raise RuntimeError("read-back failed")

        monkeypatch.setattr(
            ConfigurationManager, "_require_published", fail_after_publish
        )
        _discovery_merge("Form-I-9")

        assert _published() == 2
        assert _head_class_ids() == ["W2", "Payslip", "Form-I-9"]
        assert ConfigurationManager().head_matches_published(PROFILE) is True


@pytest.mark.unit
def test_discovery_keeps_the_refusal_type(monkeypatch):
    """`discovery_classes_with_document` re-wraps failures as a bare Exception;
    a refused save must reach the job status as itself."""
    from idp_common.discovery.classes_discovery import ClassesDiscovery

    discovery = object.__new__(ClassesDiscovery)

    def refuse(*_args, **_kwargs):
        raise RevisionNotRecordedError("Refusing to save configuration profile 'p'")

    monkeypatch.setattr(discovery, "_extract_data_from_document", refuse, raising=False)
    with pytest.raises(RevisionNotRecordedError, match="Refusing to save"):
        discovery.discovery_classes_with_document(
            "bucket", "doc.png", file_bytes=b"not an image"
        )

    def other(*_args, **_kwargs):
        raise ValueError("model refused")

    monkeypatch.setattr(discovery, "_extract_data_from_document", other, raising=False)
    with pytest.raises(Exception, match="Failed to process document doc.png"):
        discovery.discovery_classes_with_document(
            "bucket", "doc.png", file_bytes=b"not an image"
        )


@pytest.mark.unit
def test_resolving_against_default_is_recorded_as_the_revision_documents_run(
    monkeypatch,
):
    """#1782: the write-back is a revision, so documents run what the editor shows.

    A profile and its r1 written before a top-level section existed. Reading it
    resolves the missing section against `default`, which here differs from the
    model's own defaults. The resolved configuration is recorded as r2, made by
    `system`, and published, so a document submitted now runs `default`'s value,
    the head matches the published revision, and r1 is still readable as it was.
    """
    from idp_common.config.configuration_manager import _stored_content_hash

    _env(monkeypatch, with_bucket=True)
    with mock_aws():
        _create_resources()
        manager = ConfigurationManager()
        manager.save_configuration(
            CONFIG_TYPE_CONFIG,
            IDPConfig(summarization={"enabled": False}),
            version="default",
        )
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="mine"), version=PROFILE
        )
        key = {"Configuration": f"{CONFIG_TYPE_CONFIG}#{PROFILE}"}
        head = manager._decompress_item(manager.table.get_item(Key=key)["Item"])
        head.pop("summarization")
        manager.table.put_item(Item=manager._compress_item(head))
        body = manager.revisions.get_body(PROFILE, 1)
        assert body is not None
        body.pop("summarization")
        manager.revisions.put_body(PROFILE, 1, body)
        manager.revisions.update_entry(
            PROFILE, 1, storedHash=_stored_content_hash(head)
        )
        assert manager.head_matches_published(PROFILE) is True

        resolved = manager.get_merged_configuration(PROFILE)
        assert resolved is not None and resolved.summarization.enabled is False

        from idp_common.config.configuration_manager import (
            RESOLVED_AGAINST_DEFAULT_NOTES,
        )

        assert manager.resolve_published_revision(PROFILE) == 2
        newest = manager.list_revisions(PROFILE)[0]
        assert newest["revision"] == 2 and newest["createdBy"] == "system"
        assert newest["notes"] == RESOLVED_AGAINST_DEFAULT_NOTES
        assert manager.head_matches_published(PROFILE) is True
        runs = get_config(as_model=True, version=PROFILE, revision=2)
        assert runs.summarization.enabled is False
        before = get_config(as_model=True, version=PROFILE, revision=1)
        assert before.summarization.enabled is True

        # The next read is a direct one: no further revision.
        manager.get_merged_configuration(PROFILE)
        assert manager.resolve_published_revision(PROFILE) == 2
