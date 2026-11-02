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
import logging
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


@pytest.mark.unit
class TestDiscoverySaveWithoutHistory:
    """A process with no `CONFIGURATION_BUCKET`: what the deployed Lambdas were."""

    @pytest.fixture(autouse=True)
    def _aws(self, monkeypatch):
        _env(monkeypatch, with_bucket=True)
        with mock_aws():
            _create_resources()
            _create_profile_as_copy_of_default()
            monkeypatch.delenv("CONFIGURATION_BUCKET")
            yield

    def test_the_divergence_the_report_describes(self):
        _replace_mode_clear()
        _discovery_merge("Form-I-9")

        assert _head_class_ids() == ["Form-I-9"]
        assert ConfigurationManager().resolve_published_revision(PROFILE) == 1

    def test_the_save_says_documents_keep_running_the_published_revision(self, caplog):
        with caplog.at_level(
            logging.WARNING, logger="idp_common.config.configuration_manager"
        ):
            _discovery_merge("Form-I-9")

        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "without recording a revision" in r.getMessage()
        ]
        assert len(warnings) == 1, caplog.text
        assert f"'{PROFILE}'" in warnings[0]
        assert "CONFIGURATION_BUCKET" in warnings[0]
        assert "r1" in warnings[0]


@pytest.mark.unit
def test_no_warning_for_a_profile_that_never_had_history(monkeypatch, caplog):
    """Without history, documents run the head, so a save there diverges from nothing."""
    _env(monkeypatch, with_bucket=False)
    with mock_aws():
        _create_resources()
        manager = ConfigurationManager()
        with caplog.at_level(
            logging.WARNING, logger="idp_common.config.configuration_manager"
        ):
            manager.save_configuration(
                CONFIG_TYPE_CONFIG, IDPConfig(notes="first"), version=PROFILE
            )
            manager.save_configuration(
                CONFIG_TYPE_CONFIG, IDPConfig(notes="second"), version=PROFILE
            )
    assert "without recording a revision" not in caplog.text
