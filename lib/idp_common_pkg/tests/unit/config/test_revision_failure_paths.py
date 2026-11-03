# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The failure branches of the revision invariant (#1778, #1782).

Each of these is a path a profile save or read takes when part of recording a
revision cannot be done: a counter that cannot be read, a head that cannot be put
back, a published revision with no index entry, a write-back that fails for a
reason other than the refusal. Each must leave the read or the save in a defined
state rather than raise somewhere unexpected.
"""

from __future__ import annotations

import logging

import boto3
import pytest
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.models import IDPConfig

TABLE = "revision-failure-table"
BUCKET = "revision-failure-bucket"


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)
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
        yield ConfigurationManager()


class _Refusing:
    def __init__(self, table):
        self._table = table

    def __getattr__(self, name):
        return getattr(self._table, name)

    def get_item(self, **_kwargs):
        raise RuntimeError("table unreadable")

    def put_item(self, **_kwargs):
        raise RuntimeError("table unwritable")


@pytest.mark.unit
def test_an_unreadable_counter_reads_as_unknown(manager, monkeypatch):
    monkeypatch.setattr(manager, "table", _Refusing(manager.table))
    assert manager._published_counter("p") is None


@pytest.mark.unit
def test_there_is_nothing_to_restore_without_the_previous_head(manager):
    assert manager._restore_head("p", None) is False


@pytest.mark.unit
def test_a_restore_that_cannot_write_reports_it(manager, monkeypatch, caplog):
    monkeypatch.setattr(manager, "table", _Refusing(manager.table))
    with caplog.at_level(logging.ERROR):
        assert manager._restore_head("p", {"Configuration": "Config#p"}) is False
    assert "Could not restore configuration profile 'p'" in caplog.text


@pytest.mark.unit
def test_a_published_revision_with_no_entry_cannot_be_compared(manager):
    manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="p")
    manager.revisions.delete("p", 1)
    assert manager.resolve_published_revision("p") == 1
    assert manager.head_matches_published("p") is None


@pytest.mark.unit
def test_recording_a_profile_that_cannot_be_resolved_records_nothing(
    manager, monkeypatch
):
    manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(notes="x"), version="p")
    monkeypatch.setattr(manager, "get_merged_configuration", lambda _v: None)
    assert manager.record_resolution_against_default("p") is None
    assert manager.resolve_published_revision("p") == 1


@pytest.mark.unit
def test_a_new_profile_with_no_default_still_passes_the_write_guard(manager):
    seen = []
    manager.handle_update_custom_configuration(
        custom_config={"notes": "first", "saveAsVersion": True},
        version="p",
        write_guard=lambda current, new, version: seen.append(version),
    )
    assert seen == ["p"]
