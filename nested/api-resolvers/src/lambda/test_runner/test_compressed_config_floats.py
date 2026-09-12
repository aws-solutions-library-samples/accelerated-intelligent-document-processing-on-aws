# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A compressed configuration must inflate to Decimals, never floats (#892).

The captured configuration is written straight into the run's DynamoDB item, and
the DynamoDB resource client refuses Python floats. The revision path already
parses with ``parse_float=Decimal``; the compressed path did not, so a compressed
profile carrying ``temperature: 0.0`` (the shipped ``ocr-benchmark`` preset does)
failed every ``startTestRun`` at submit with "Float types are not supported".
"""

import gzip
import importlib.util
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE", "tracking")
os.environ.setdefault("CONFIG_TABLE", "config")
os.environ.setdefault("FILE_COPY_QUEUE_URL", "https://sqs.example/queue")


@pytest.fixture
def index():
    spec = importlib.util.spec_from_file_location(
        "test_runner_index_floats", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_runner_index_floats"] = module
    spec.loader.exec_module(module)
    return module


def _compressed_item(config: dict) -> dict:
    return {
        "Configuration": "Config#ocr-benchmark",
        "IsActive": True,
        "_config_storage": "compressed",
        "_compressed_config": gzip.compress(json.dumps(config).encode("utf-8")),
    }


def _walk(o):
    if isinstance(o, dict):
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)
    else:
        yield o


@pytest.mark.unit
def test_compressed_config_inflates_floats_as_decimal(index):
    item = _compressed_item(
        {
            "criteria_validation": {"temperature": 0.0, "top_p": 0.01},
            "extraction": {"model": "us.anthropic.claude-sonnet-5", "max_tokens": 4096},
            "classes": [{"name": "Invoice", "threshold": 0.95}],
        }
    )
    full = index._decompress_config_item(item)
    assert full["criteria_validation"]["temperature"] == Decimal("0.0")
    assert full["criteria_validation"]["top_p"] == Decimal("0.01")
    assert full["classes"][0]["threshold"] == Decimal("0.95")
    # ints stay ints (DynamoDB is happy with either, but nothing should widen)
    assert full["extraction"]["max_tokens"] == 4096
    assert not any(isinstance(v, float) for v in _walk(full)), (
        "a float survived decompression — the DynamoDB resource client will "
        "reject the run's metadata write"
    )
    # metadata fields are preserved alongside the inflated body
    assert full["Configuration"] == "Config#ocr-benchmark"
    assert full["IsActive"] is True


@pytest.mark.unit
def test_compressed_config_serialises_for_dynamodb(index):
    """The concrete failure: boto3's TypeSerializer must accept the result."""
    from boto3.dynamodb.types import TypeSerializer

    full = index._decompress_config_item(
        _compressed_item({"criteria_validation": {"temperature": 0.0, "top_p": 0.01}})
    )
    body = {k: v for k, v in full.items() if not k.startswith("_")}
    TypeSerializer().serialize(body)  # raised TypeError before the fix


@pytest.mark.unit
def test_legacy_inline_item_is_returned_unchanged(index):
    item = {"Configuration": "Config#x", "extraction": {"temperature": Decimal("0")}}
    assert index._decompress_config_item(item) is item
