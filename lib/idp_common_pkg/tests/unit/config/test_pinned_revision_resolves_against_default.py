# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A pinned revision is resolved against `default` exactly as the profile head is (#1955).

Every document and Test Studio run is pinned to a revision of its profile, and the
pipeline reads that revision through `get_config(version=..., revision=...)`. The
head is resolved against the `default` profile: a key it lacks takes `default`'s
value. A pinned read used to build `IDPConfig` straight from the stored body, so a
setting added in a later release, absent from a revision saved by older code, took
the model's own default instead — empty, for a prompt — while the head of the same
profile read `default`'s value.

These tests drive the real `ConfigurationManager` against moto. A revision "saved
by older code" is made the way an upgrade produces one: save normally, then remove
keys from the stored body (and, where the head must match, from the head too).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable

import boto3
import pytest
from moto import mock_aws

from idp_common.config import get_config
from idp_common.config.configuration_manager import (
    ConfigurationManager,
    _stored_content_hash,
)
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.models import IDPConfig

pytestmark = pytest.mark.unit

TABLE = "pinned-resolution-table"
BUCKET = "pinned-resolution-bucket"
PROFILE = "Mine"


def _env(monkeypatch, region: str = "us-east-1") -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", region)
    monkeypatch.setenv("AWS_REGION", region)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)


def _create_resources(region: str = "us-east-1") -> None:
    boto3.resource("dynamodb", region_name=region).create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    s3 = boto3.client("s3", region_name=region)
    if region == "us-east-1":
        s3.create_bucket(Bucket=BUCKET)
    else:
        s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": region}
        )


def _drop(body: Dict[str, Any], path: str) -> None:
    """Remove the dotted `path` from `body`, as a body written before it existed."""
    *parents, leaf = path.split(".")
    node = body
    for part in parents:
        node = node[part]
    node.pop(leaf, None)  # the head omits a None the body stores


def _set(body: Dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    node = body
    for part in parents:
        node = node[part]
    node[leaf] = value


def _get(config: IDPConfig, path: str) -> Any:
    node: Any = config.model_dump(mode="python")
    for part in path.split("."):
        node = node[part]
    return node


def _age_revision(
    manager: ConfigurationManager,
    revision: int,
    drop: Iterable[str] = (),
    put: Dict[str, Any] | None = None,
    head_too: bool = True,
) -> None:
    """Make r`revision` of PROFILE look as older code saved it.

    With `head_too` the head is changed identically and the revision's stored-content
    hash re-recorded, so the head is still provably that revision, which is the state
    of a profile untouched since its last save before an upgrade.
    """
    body = manager.revisions.get_body(PROFILE, revision)
    assert body is not None
    for path in drop:
        _drop(body, path)
    for path, value in (put or {}).items():
        _set(body, path, value)
    manager.revisions.put_body(PROFILE, revision, body)
    if not head_too:
        return
    key = {"Configuration": f"{CONFIG_TYPE_CONFIG}#{PROFILE}"}
    head = manager._decompress_item(manager.table.get_item(Key=key)["Item"])
    for path in drop:
        _drop(head, path)
    for path, value in (put or {}).items():
        _set(head, path, value)
    manager.table.put_item(Item=manager._compress_item(head))
    manager.revisions.update_entry(
        PROFILE, revision, storedHash=_stored_content_hash(head)
    )
    assert manager.head_matches_published(PROFILE) is True


def _save_default(manager: ConfigurationManager, **settings: Any) -> None:
    manager.save_configuration(
        CONFIG_TYPE_CONFIG, IDPConfig(**settings), version="default"
    )


def _save_profile(manager: ConfigurationManager, **settings: Any) -> None:
    settings.setdefault("notes", "mine")
    manager.save_configuration(
        CONFIG_TYPE_CONFIG, IDPConfig(**settings), version=PROFILE
    )
    assert manager.resolve_published_revision(PROFILE) == 1


@pytest.fixture
def manager(monkeypatch):
    _env(monkeypatch)
    with mock_aws():
        _create_resources()
        yield ConfigurationManager()


class TestPinnedReadFillsFromDefault:
    def test_a_setting_the_revision_lacks_takes_defaults_value(self, manager):
        """A nested setting and a prompt added after r1 was saved read `default`'s."""
        _save_default(
            manager,
            ocr={"max_tokens": 4321, "task_prompt": "default's OCR prompt"},
        )
        _save_profile(manager)
        _age_revision(
            manager, 1, drop=("ocr.max_tokens", "ocr.task_prompt"), head_too=False
        )

        pinned = get_config(as_model=True, version=PROFILE, revision=1)
        assert pinned.ocr.max_tokens == 4321
        assert pinned.ocr.task_prompt == "default's OCR prompt"
        # The stored body is unchanged: it still lacks both.
        stored = manager.revisions.get_body(PROFILE, 1)
        assert stored is not None
        assert "max_tokens" not in stored["ocr"]
        assert "task_prompt" not in stored["ocr"]

    def test_a_top_level_section_the_revision_lacks_takes_defaults_value(self, manager):
        _save_default(manager, summarization={"enabled": False})
        _save_profile(manager)
        _age_revision(manager, 1, drop=("summarization",), head_too=False)

        pinned = get_config(as_model=True, version=PROFILE, revision=1)
        assert pinned.summarization.enabled is False

    def test_a_saved_value_is_kept_over_defaults(self, manager):
        _save_default(manager, ocr={"max_tokens": 4321}, notes="default's notes")
        _save_profile(manager, ocr={"max_tokens": 2222})

        pinned = get_config(as_model=True, version=PROFILE, revision=1)
        assert pinned.ocr.max_tokens == 2222
        assert pinned.notes == "mine"

    def test_a_stored_null_is_kept_as_set_and_matches_the_head(self, manager):
        """A revision saved before the 10000 output caps stores `max_tokens: null`.

        That is a value the revision holds, not an absent key, so it stays null (the
        model's maximum) on a pinned read, as the same stored null does on the head.
        """
        _save_default(manager)  # the shipped 10000 caps
        _save_profile(manager)
        nulls = {
            "classification.combined.llm.max_tokens": None,
            "evaluation.llm_method.max_tokens": None,
        }
        _age_revision(manager, 1, put=nulls)

        pinned = manager.get_merged_configuration(PROFILE, revision=1)
        head = manager.get_merged_configuration(PROFILE)
        assert pinned is not None and head is not None
        for path in nulls:
            assert _get(pinned, path) is None, path
            assert _get(head, path) is None, path

    def test_an_absent_cap_takes_defaults_cap(self, manager):
        """The same key absent rather than null is filled, on both reads alike."""
        _save_default(manager, ocr={"max_tokens": 3000})
        _save_profile(manager)
        _age_revision(manager, 1, drop=("ocr.max_tokens",))

        pinned = manager.get_merged_configuration(PROFILE, revision=1)
        head = manager.get_merged_configuration(PROFILE)
        assert pinned is not None and head is not None
        assert pinned.ocr.max_tokens == head.ocr.max_tokens == 3000


class TestHeadAndPinnedAgree:
    def test_head_and_published_revision_resolve_identically(self, manager):
        _save_default(
            manager,
            summarization={"enabled": False},
            ocr={"max_tokens": 4321, "task_prompt": "default's OCR prompt"},
        )
        _save_profile(manager, notes="kept")
        _age_revision(
            manager,
            1,
            drop=("summarization", "ocr.max_tokens", "ocr.task_prompt"),
        )

        head = manager.get_merged_configuration(PROFILE)
        pinned = manager.get_merged_configuration(PROFILE, revision=1)
        assert head is not None and pinned is not None
        assert pinned.model_dump(mode="json") == head.model_dump(mode="json")
        assert pinned.summarization.enabled is False
        assert pinned.notes == "kept"
        assert manager.resolve_published_revision(PROFILE) == 1, (
            "neither read records a revision"
        )

    def test_a_restored_revision_runs_as_its_pinned_read(self, manager):
        """Restoring r1 saves what a document pinned to r1 runs, fills included.

        The filled setting has a non-empty model default (10000), so restoring the
        body validated on its own would store that value in the head, where the
        head's own resolution could no longer replace it.
        """
        _save_default(manager, ocr={"max_tokens": 4321})
        _save_profile(manager)
        _age_revision(manager, 1, drop=("ocr.max_tokens",), head_too=False)
        manager.save_configuration(
            CONFIG_TYPE_CONFIG,
            IDPConfig(notes="changed later"),
            version=PROFILE,
        )
        assert manager.resolve_published_revision(PROFILE) == 2

        pinned_r1 = manager.get_merged_configuration(PROFILE, revision=1)
        restored = manager.restore_revision(PROFILE, 1)
        assert restored == 3
        head = manager.get_merged_configuration(PROFILE)
        assert pinned_r1 is not None and head is not None
        assert head.ocr.max_tokens == 4321
        assert head.model_dump(mode="json") == pinned_r1.model_dump(mode="json")


class TestPinnedReadFallbacks:
    def test_without_a_default_the_revision_is_read_on_its_own(self, manager):
        _save_profile(manager)
        _age_revision(manager, 1, drop=("ocr.task_prompt",), head_too=False)

        pinned = manager.get_merged_configuration(PROFILE, revision=1)
        assert pinned is not None
        assert pinned.ocr.task_prompt is None  # the model's own default
        assert pinned.notes == "mine"

    def test_a_resolution_that_fails_validation_returns_the_revision_alone(
        self, manager
    ):
        """Two valid configurations can merge into an invalid one; never worse."""
        _save_default(
            manager, classification={"combined": {"packet": {"window_pages": 10}}}
        )
        _save_profile(
            manager, classification={"combined": {"packet": {"overlap_pages": 15}}}
        )
        _age_revision(
            manager,
            1,
            drop=("classification.combined.packet.window_pages",),
            head_too=False,
        )

        pinned = manager.get_merged_configuration(PROFILE, revision=1)
        assert pinned is not None
        assert pinned.classification.combined.packet.overlap_pages == 15
        assert pinned.classification.combined.packet.window_pages == 20


def test_an_absent_model_key_takes_defaults_regional_model(monkeypatch):
    """On an EU stack an absent model key reads `default`'s, on both reads alike.

    `default` here stores a model id that differs from what the model's
    absent-key translation would give, so the two sources are distinguishable.
    """
    region = "eu-central-1"
    _env(monkeypatch, region)
    with mock_aws():
        _create_resources(region)
        manager = ConfigurationManager()
        _save_default(manager, extraction={"model": "eu.amazon.nova-pro-v1:0"})
        _save_profile(manager)
        translated = IDPConfig().extraction.model
        assert translated.startswith("eu.") and translated != "eu.amazon.nova-pro-v1:0"
        _age_revision(manager, 1, drop=("extraction.model",))

        pinned = manager.get_merged_configuration(PROFILE, revision=1)
        head = manager.get_merged_configuration(PROFILE)
        assert pinned is not None and head is not None
        assert pinned.extraction.model == head.extraction.model
        assert pinned.extraction.model == "eu.amazon.nova-pro-v1:0"
