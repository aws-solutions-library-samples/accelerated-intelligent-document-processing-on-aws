# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for B1: rollback detection in the update_configuration custom resource.

``_is_rollback_to_older_format`` infers "this reverted (older) Lambda is running
during a stack rollback" by checking whether any stored ``Config#*`` record
carries a ``config_format_version`` NEWER than this code's ``CONFIG_FORMAT_VERSION``.
When true, the handler returns SUCCESS on a parse error so the rollback can
complete instead of wedging in UPDATE_ROLLBACK_FAILED.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.models import CONFIG_FORMAT_VERSION

_REPO_ROOT = Path(__file__).resolve().parents[5]
_LAMBDA_DIR = _REPO_ROOT / "src" / "lambda" / "update_configuration"

TABLE = "ConfigurationTable-test"


@pytest.fixture
def update_config_module(monkeypatch):
    """Import src/lambda/update_configuration/index.py with cfnresponse stubbed."""
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    # cfnresponse is provided by the Lambda runtime, not the package.
    sys.modules.setdefault("cfnresponse", MagicMock())
    monkeypatch.syspath_prepend(str(_LAMBDA_DIR))
    if "index" in sys.modules:
        del sys.modules["index"]
    module = importlib.import_module("index")
    yield module
    sys.modules.pop("index", None)


def _seed(table_name, config_key, format_version):
    """Write a compressed Config record stamped with the given format version."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.Table(table_name)
    item = {"Configuration": config_key, "config_format_version": format_version}
    table.put_item(Item=ConfigurationManager._compress_item(item))


def _make_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@mock_aws
def test_detects_rollback_when_stored_format_is_newer(update_config_module):
    _make_table()
    _seed(TABLE, "Config#default", "0.9")  # newer than the code's own version
    assert update_config_module._is_rollback_to_older_format() is True


@mock_aws
def test_no_rollback_when_stored_format_matches_code(update_config_module):
    _make_table()
    _seed(TABLE, "Config#default", CONFIG_FORMAT_VERSION)  # same as code
    assert update_config_module._is_rollback_to_older_format() is False


@mock_aws
def test_no_rollback_when_stored_format_is_older(update_config_module):
    _make_table()
    _seed(TABLE, "Config#default", "0.5")  # older than code (normal upgrade)
    assert update_config_module._is_rollback_to_older_format() is False


@mock_aws
def test_ignores_non_config_records(update_config_module):
    _make_table()
    # A non-Config record with a high version must not trigger detection.
    _seed(TABLE, "Schema", "9.9")
    assert update_config_module._is_rollback_to_older_format() is False


def test_version_parse_handles_garbage(update_config_module):
    parse = update_config_module._parse_format_version
    assert parse("0.6") == (0, 6)
    assert parse("0.10") == (0, 10)
    assert parse(None) == (0,)
    assert parse("") == (0,)
    assert parse("nonsense") == (0,)
    # ordering sanity
    assert parse("0.7") > parse("0.6")
    assert parse("0.10") > parse("0.6")
