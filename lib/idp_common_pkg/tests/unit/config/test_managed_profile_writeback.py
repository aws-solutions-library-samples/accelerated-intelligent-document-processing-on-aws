# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""An unpinned read of a stack-managed profile must not write it back (#1613).

`managed` is the one `IDPConfig` field stored as a head-item attribute
(`Managed`) rather than in the compressed body. The raw read
`get_merged_configuration` merges onto `default` drops every metadata
attribute, so `managed` used to be filled in from `default` — which is not
managed. The merged result then never equalled the stored profile, and the
"persist only when the resolution changed something" write-back fired on every
unpinned read: it persisted `Managed=False` (un-managing the profile) wherever
the role could write the table, and failed with AccessDenied wherever it could
not, which is every document in the queue processor.

The profiles here are seeded from the real `config_library` presets the way the
stack's configuration custom resource seeds them — `merge_config_with_defaults`
then `save_configuration` with a revision bucket configured — rather than from
hand-written dicts, because the defect only exists for the shape the stack
writes. Engine: moto's DynamoDB and S3; nothing here depends on expression
semantics.

Writes are counted at the botocore layer (`before-call` on the table's client),
not by patching a method, so a write issued through any manager method — or any
method added later — is counted.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import boto3
import pytest
import yaml
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG, DEFAULT_VERSION
from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.models import IDPConfig

_REPO_ROOT = Path(__file__).resolve().parents[5]
_LIBRARY = _REPO_ROOT / "config_library"
_DEFAULT_PRESET = _LIBRARY / "unified" / "lending-package-sample" / "config.yaml"
_MANAGED_DIR = _LIBRARY / "managed_config"

TABLE = "managed-writeback-table"
BUCKET = "managed-writeback-bucket"

_DYNAMODB_WRITES = {
    "PutItem",
    "UpdateItem",
    "DeleteItem",
    "BatchWriteItem",
    "TransactWriteItems",
}


def _managed_presets() -> List[str]:
    names = sorted(
        p.name for p in _MANAGED_DIR.iterdir() if (p / "config.yaml").is_file()
    )
    # An empty derivation would collect as a skip, not a failure, and take every
    # parametrised case below with it.
    assert names, f"no managed presets found under {_MANAGED_DIR}"
    return names


MANAGED_PRESETS = _managed_presets()


def _stack_shaped(preset: Path) -> Dict[str, Any]:
    """What `update_configuration`'s handler saves for a preset."""
    body = yaml.safe_load(preset.read_text())
    body.pop("description", None)
    body.pop("pricing", None)
    pattern = "pattern-1" if str(body.get("use_bda")).lower() == "true" else "pattern-2"
    return merge_config_with_defaults(body, pattern=pattern, validate=False)


class _WriteRecorder:
    """Counts DynamoDB write calls on one client; optionally denies PutItem."""

    def __init__(self, client, deny_put: bool = False):
        self.calls: List[str] = []
        self.deny_put = deny_put
        client.meta.events.register("before-call.dynamodb", self._before_call)

    def _before_call(self, model, **_kwargs):
        if model.name in _DYNAMODB_WRITES:
            self.calls.append(model.name)
            if self.deny_put and model.name == "PutItem":
                raise ClientError(
                    {
                        "Error": {
                            "Code": "AccessDeniedException",
                            "Message": "not authorized to perform: dynamodb:PutItem",
                        }
                    },
                    "PutItem",
                )
        return None


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)
    with mock_aws():
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
        mgr = ConfigurationManager()
        assert mgr.revisions.enabled, "the stack seeds with a revision bucket"
        mgr.save_configuration(
            CONFIG_TYPE_CONFIG,
            _stack_shaped(_DEFAULT_PRESET),
            version=DEFAULT_VERSION,
            created_by="stack-deployment",
        )
        yield mgr


def _seed_managed(mgr: ConfigurationManager, name: str) -> None:
    body = _stack_shaped(_MANAGED_DIR / name / "config.yaml")
    assert body.get("managed") is True, f"{name} is not a managed preset"
    mgr.save_configuration(
        CONFIG_TYPE_CONFIG,
        body,
        version=name,
        description=f"Managed config for {name}",
        created_by="stack-deployment",
    )


def _head(mgr: ConfigurationManager, name: str) -> Dict[str, Any]:
    return mgr.table.get_item(Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#{name}"})[
        "Item"
    ]


def _listed_managed(mgr: ConfigurationManager, name: str) -> bool:
    return {v["versionName"]: v["managed"] for v in mgr.list_config_versions()}[name]


@pytest.mark.unit
@pytest.mark.parametrize("name", MANAGED_PRESETS)
def test_an_unchanged_managed_profile_is_read_without_any_write(manager, name):
    _seed_managed(manager, name)
    assert _head(manager, name)["Managed"] is True
    writes = _WriteRecorder(manager.table.meta.client)

    for _ in range(3):
        merged = manager.get_merged_configuration(name)
        assert merged is not None
        assert merged.managed is True

    assert writes.calls == []
    assert _head(manager, name)["Managed"] is True
    assert _listed_managed(manager, name) is True


@pytest.mark.unit
@pytest.mark.parametrize("name", MANAGED_PRESETS)
def test_a_read_only_role_attempts_no_write(manager, name, caplog):
    """The queue processor's role can read the table and not write it."""
    _seed_managed(manager, name)
    writes = _WriteRecorder(manager.table.meta.client, deny_put=True)

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            merged = manager.get_merged_configuration(name)
            assert merged is not None
            assert merged.managed is True

    assert writes.calls == []
    assert "Failed to auto-migrate" not in caplog.text
    assert _head(manager, name)["Managed"] is True


@pytest.mark.unit
@pytest.mark.parametrize("name", MANAGED_PRESETS)
def test_a_genuine_backfill_is_written_back_and_keeps_every_head_attribute(
    manager, name
):
    """A field the stored profile lacks is still backfilled and persisted, and
    the write-back preserves `Managed` and every other head attribute."""
    _seed_managed(manager, name)
    manager.set_bda_project_arn(
        name, "arn:aws:bedrock:us-east-1:000000000000:data-automation-project/p"
    )
    manager.activate_version(name)

    # Simulate "a field added since this profile was saved": drop a nested key
    # that `default` carries from the stored body.
    item = manager._decompress_item(_head(manager, name))
    assert item["summarization"].get("model"), "the fixture must remove a real key"
    del item["summarization"]["model"]
    manager.table.put_item(Item=manager._compress_item(item))
    before = _head(manager, name)
    writes = _WriteRecorder(manager.table.meta.client)

    merged = manager.get_merged_configuration(name)

    assert merged is not None
    assert merged.summarization.model, "the absent key must be backfilled"
    assert "PutItem" in writes.calls, "a genuine backfill must still be persisted"
    after = _head(manager, name)
    assert after["Managed"] is True
    for attribute in (
        "IsActive",
        "Description",
        "CreatedAt",
        "BdaProjectArn",
        "BdaSyncStatus",
        "LatestRevision",
        "PublishedRevision",
    ):
        assert attribute in before, f"fixture must set {attribute}"
        assert after.get(attribute) == before[attribute], attribute
    body = json.loads(gzip.decompress(bytes(after["_compressed_config"].value)))
    assert body["summarization"]["model"] == merged.summarization.model
    assert "managed" not in body, "managed belongs on the head, not in the body"
    assert _listed_managed(manager, name) is True

    # And the next read is a direct one.
    writes.calls.clear()
    manager.get_merged_configuration(name)
    assert writes.calls == []


@pytest.mark.unit
def test_an_unmanaged_profile_stays_unmanaged_when_default_is_managed(manager):
    """The field default, not `default`'s value, fills a profile with no
    `Managed` attribute and no body key — which is what the validated read the
    write-back compares against does. Inheriting it from `default` would mark a
    user profile stack-managed, and write that back."""
    default_item = _head(manager, DEFAULT_VERSION)
    default_item["Managed"] = True
    manager.table.put_item(Item=default_item)
    manager.save_configuration(
        CONFIG_TYPE_CONFIG,
        _stack_shaped(_LIBRARY / "unified" / "rvl-cdip" / "config.yaml"),
        version="mine",
    )
    head = _head(manager, "mine")
    del head["Managed"]
    manager.table.put_item(Item=head)
    writes = _WriteRecorder(manager.table.meta.client)

    merged = manager.get_merged_configuration("mine")

    assert merged is not None
    assert merged.managed is False
    assert writes.calls == []
    assert "Managed" not in _head(manager, "mine")


@pytest.mark.unit
def test_a_body_managed_key_with_no_head_attribute_is_honoured(manager):
    """`update_configuration`'s migration-mode writer leaves `managed` in the
    body and sets no `Managed` attribute; the validated read honours the body
    key, so the merge must too or every read writes the profile back."""
    name = MANAGED_PRESETS[0]
    _seed_managed(manager, name)
    item = manager._decompress_item(_head(manager, name))
    del item["Managed"]
    item["managed"] = True
    manager.table.put_item(Item=manager._compress_item(item))
    writes = _WriteRecorder(manager.table.meta.client)

    merged = manager.get_merged_configuration(name)

    assert merged is not None
    assert merged.managed is True
    assert writes.calls == []


@pytest.mark.unit
def test_the_head_attribute_wins_over_a_body_key(manager):
    """The validated read takes `Managed` over a body `managed`, so the merge
    must too: letting the body key win writes `Managed=False` back."""
    name = MANAGED_PRESETS[0]
    _seed_managed(manager, name)
    item = manager._decompress_item(_head(manager, name))
    assert item["Managed"] is True
    item["managed"] = False
    manager.table.put_item(Item=manager._compress_item(item))
    writes = _WriteRecorder(manager.table.meta.client)

    merged = manager.get_merged_configuration(name)

    assert merged is not None
    assert merged.managed is True
    assert writes.calls == []
    assert _head(manager, name)["Managed"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "stored, expected", [("false", False), ("False", False), ("true", True)]
)
def test_a_string_valued_attribute_is_coerced_like_the_validated_read(
    manager, stored, expected
):
    """No writer in this tree stores `Managed` as a string, but a hand-edited
    item can; it must be read as the validated read reads it (Pydantic's bool
    coercion), not by Python truthiness, or the read writes the profile back
    with the opposite flag."""
    name = MANAGED_PRESETS[0]
    _seed_managed(manager, name)
    head = _head(manager, name)
    head["Managed"] = stored
    manager.table.put_item(Item=head)
    validated = manager.get_configuration(CONFIG_TYPE_CONFIG, name)
    assert validated is not None and validated.managed is expected
    writes = _WriteRecorder(manager.table.meta.client)

    merged = manager.get_merged_configuration(name)

    assert merged is not None
    assert merged.managed is expected
    assert writes.calls == []
    assert _head(manager, name)["Managed"] == stored


@pytest.mark.unit
def test_get_raw_configuration_still_omits_managed(manager):
    """⚠️ Deliberately NOT mapped back in the public raw read. Its result is the
    web UI editor's `Custom` delta, which Save as Profile and Save as default
    merge into what they send back, and `handle_update_custom_configuration`
    does not strip `managed` — so a copy of a managed profile would itself
    become managed. `get_merged_configuration` reads `Managed` through the
    private `_read_raw_configuration` instead."""
    name = MANAGED_PRESETS[0]
    _seed_managed(manager, name)

    raw = manager.get_raw_configuration(CONFIG_TYPE_CONFIG, name)

    assert raw is not None
    assert "managed" not in raw
    assert "Managed" not in raw
    read = manager._read_raw_configuration(CONFIG_TYPE_CONFIG, name)
    assert read is not None
    assert read[0] == raw
    assert read[1]["Managed"] is True


@pytest.mark.unit
def test_the_field_default_used_for_an_absent_attribute_is_false():
    """Pins the value the absent-attribute branch relies on."""
    assert IDPConfig.model_fields["managed"].default is False
