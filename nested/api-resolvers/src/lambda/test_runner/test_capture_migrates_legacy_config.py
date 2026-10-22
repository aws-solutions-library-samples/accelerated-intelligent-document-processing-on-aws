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
def test_a_body_without_a_version_stamp_is_not_given_one(module):
    item = {"Configuration": "Config#lending", "notes": "x"}
    module.dynamodb.Table.return_value.get_item.return_value = {"Item": item}

    captured = module._capture_config("config-table", "lending")

    assert captured["Config"] == item
