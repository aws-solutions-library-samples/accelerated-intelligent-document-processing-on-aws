# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A test run captures its configuration in the current config format.

Runs are compared setting by setting. A profile or revision stored before the
0.9 format holds flat ``classification`` keys; captured as stored, every one of
those settings would compare as ``<missing>`` against a run captured in the
nested shape. The capture migrates the body first, and leaves the stored body
alone.
"""

import importlib.util
import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE", "tracking")
os.environ.setdefault("CONFIG_TABLE", "config")
os.environ.setdefault("FILE_COPY_QUEUE_URL", "https://sqs.example/queue")


def _flat_body():
    return {
        "config_format_version": "0.8",
        "classification": {"model": "flat-model", "temperature": 0.0},
    }


@pytest.fixture
def module():
    spec = importlib.util.spec_from_file_location(
        "test_runner_index_migrate", Path(__file__).with_name("index.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_runner_index_migrate"] = mod
    spec.loader.exec_module(mod)
    mod.dynamodb = MagicMock()
    return mod


@pytest.mark.unit
def test_a_pinned_legacy_revision_is_captured_nested(module, monkeypatch):
    stored = _flat_body()
    manager = MagicMock()
    manager.get_revision.return_value = stored
    monkeypatch.setitem(
        sys.modules,
        "idp_common.config.configuration_manager",
        MagicMock(ConfigurationManager=MagicMock(return_value=manager)),
    )

    captured = module._capture_config("config-table", "lending", 5)

    classification = captured["Config"]["classification"]
    assert "model" not in classification
    assert classification["one_stage"]["llm"]["model"] == "flat-model"
    assert captured["Config"]["config_format_version"] == "0.9"
    # The revision body the manager returned is not rewritten.
    assert stored == _flat_body()


@pytest.mark.unit
def test_a_legacy_profile_head_is_captured_nested(module):
    item = {
        "Configuration": "Config#lending",
        "config_format_version": "0.8",
        "classification": {"model": "flat-model", "temperature": Decimal("0")},
    }
    module.dynamodb.Table.return_value.get_item.return_value = {"Item": item}

    captured = module._capture_config("config-table", "lending")

    classification = captured["Config"]["classification"]
    assert "model" not in classification
    assert classification["one_stage"]["llm"]["model"] == "flat-model"
    assert item["classification"] == {
        "model": "flat-model",
        "temperature": Decimal("0"),
    }


@pytest.mark.unit
def test_an_unstamped_body_is_captured_in_the_current_format(module):
    """The stamp states the shape the capture is in, which is the current one."""
    item = {"Configuration": "Config#lending", "notes": "x"}
    module.dynamodb.Table.return_value.get_item.return_value = {"Item": item}

    captured = module._capture_config("config-table", "lending")

    assert captured["Config"]["config_format_version"] == "0.9"
    assert captured["Config"]["notes"] == "x"
    # The stored item is not rewritten.
    assert item == {"Configuration": "Config#lending", "notes": "x"}


@pytest.mark.unit
def test_an_unstamped_configuration_is_captured_the_same_way_either_route(
    module, monkeypatch
):
    """
    A capture must not depend on how the configuration was reached.

    `ConfigurationManager.get_revision()` converts a revision on read, so a
    pinned revision arrives at the capture already stamped, while a profile head
    read straight from DynamoDB does not. Dropping the stamp for an unstamped
    body could therefore only ever see the head path, which gave one
    configuration two captures — and run comparison a difference on that key
    between a run pinned to a revision and a run of the same configuration at the
    head.
    """
    unstamped = {
        "notes": "x",
        "classification": {"one_stage": {"llm": {"model": "M"}}},
    }
    module.dynamodb.Table.return_value.get_item.return_value = {
        "Item": {"Configuration": "Config#lending", **unstamped}
    }
    from_head = module._capture_config("config-table", "lending")["Config"]

    manager = MagicMock()
    # What the manager now returns for an unstamped stored body: converted.
    manager.get_revision.return_value = {**unstamped, "config_format_version": "0.9"}
    monkeypatch.setitem(
        sys.modules,
        "idp_common.config.configuration_manager",
        MagicMock(ConfigurationManager=MagicMock(return_value=manager)),
    )
    from_revision = module._capture_config("config-table", "lending", 5)["Config"]

    assert from_revision["config_format_version"] == "0.9"
    assert {k: v for k, v in from_head.items() if k != "Configuration"} == from_revision


@pytest.mark.unit
def test_an_unstamped_revision_is_captured_in_the_current_format(module, monkeypatch):
    """
    A revision stored before the format stamp existed, as it would arrive from a
    manager that did not convert it. The capture converts whatever it is given,
    so the run records the shape it will later be compared in either way.
    """
    stored = {"notes": "x", "classification": {"model": "flat-model"}}
    manager = MagicMock()
    manager.get_revision.return_value = stored
    monkeypatch.setitem(
        sys.modules,
        "idp_common.config.configuration_manager",
        MagicMock(ConfigurationManager=MagicMock(return_value=manager)),
    )

    captured = module._capture_config("config-table", "lending", 5)["Config"]

    assert captured["config_format_version"] == "0.9"
    assert captured["classification"]["one_stage"]["llm"]["model"] == "flat-model"
    assert "model" not in captured["classification"]
    # The body the manager returned is not rewritten.
    assert stored == {"notes": "x", "classification": {"model": "flat-model"}}


@pytest.mark.unit
def test_a_body_the_migration_cannot_convert_is_recorded_as_stored(module, monkeypatch):
    """The capture is a record for later comparison. A revision that was read
    but cannot be normalised must not fail the test run."""

    def broken(_body):
        raise ValueError("cannot convert")

    monkeypatch.setattr(module, "migrate_config", broken)
    stored = _flat_body()
    manager = MagicMock()
    manager.get_revision.return_value = stored
    monkeypatch.setitem(
        sys.modules,
        "idp_common.config.configuration_manager",
        MagicMock(ConfigurationManager=MagicMock(return_value=manager)),
    )

    captured = module._capture_config("config-table", "lending", 5)

    assert captured["Config"]["classification"]["model"] == "flat-model"
