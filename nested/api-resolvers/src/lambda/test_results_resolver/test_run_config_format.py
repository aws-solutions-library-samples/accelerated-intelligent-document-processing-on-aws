# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A stored run's configuration is read back in the current config format.

Runs are compared setting by setting. A run captured before the 0.9 format holds
flat ``classification`` keys, so read back as captured, every moved setting would
compare as ``<missing>`` against a newer run. The read converts first, and leaves
the stored item alone.

The conversion has to behave the same on both storage shapes, because the two are
read by one code path and compared against each other: a run records its
configuration as a gzipped attribute, and runs created before that stored it
inline under ``Config``.
"""

import gzip
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _flat_capture():
    """A capture in the pre-0.9 layout, as a release before the change wrote it.

    Deliberately carries no ``Decimal``. Production writes the compressed blob
    with ``test_runner._json_default`` (which emits a JSON number) and reads it
    back with ``parse_float=Decimal``; the helper below serialises more simply, so
    a ``Decimal`` here would land in the fixture as a string and the fixture would
    stop being the shape production stores. Decimal symmetry across the two
    storage shapes is covered where it belongs, by
    ``test_runner/test_captured_config_size.py::
    TestTheResultsResolverReadsBothStorageShapes``.
    """
    return {
        "Config": {
            "config_format_version": "0.8",
            "classification": {"model": "flat-model", "temperature": 0},
        }
    }


@pytest.fixture
def module(monkeypatch):
    monkeypatch.setenv("TRACKING_TABLE", "tracking")
    spec = importlib.util.spec_from_file_location(
        "test_results_resolver_index", Path(__file__).with_name("index.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_results_resolver_index"] = mod
    spec.loader.exec_module(mod)
    mod.dynamodb = MagicMock()
    return mod


def _compressed_item(capture):
    return {
        "_config_storage": "compressed",
        "_compressed_config": gzip.compress(
            json.dumps(capture, separators=(",", ":")).encode("utf-8")
        ),
    }


def _inline_item(capture):
    """The legacy shape: the whole capture stored inline under ``Config``."""
    return {"Config": capture}


def _read_back(module, item):
    module.dynamodb.Table.return_value.get_item.return_value = {"Item": item}
    return module._get_test_run_config("run-1")


@pytest.mark.unit
@pytest.mark.parametrize("as_item", [_compressed_item, _inline_item])
def test_a_pre_0_9_run_is_read_back_nested(module, as_item):
    capture = _flat_capture()
    item = as_item(capture)

    body = _read_back(module, item)["Config"]

    assert "model" not in body["classification"]
    assert body["classification"]["one_stage"]["llm"]["model"] == "flat-model"
    assert body["config_format_version"] == "0.9"
    # The stored capture is not rewritten.
    assert capture == _flat_capture()


@pytest.mark.unit
@pytest.mark.parametrize("as_item", [_compressed_item, _inline_item])
def test_an_unstamped_run_is_read_back_in_the_current_format(module, as_item):
    """
    The stamp must not depend on when the run was captured.

    The capture side always stamps (``test_runner._current_format``), so stripping
    the stamp for a capture that recorded none would leave a run captured from an
    unstamped configuration before that change reading back stripped, and one
    captured after keeping it — one configuration, two shapes, differing by
    capture date.
    """
    capture = {"Config": {"notes": "x"}}

    body = _read_back(module, as_item(capture))["Config"]

    assert body["config_format_version"] == "0.9"
    assert body["notes"] == "x"


@pytest.mark.unit
def test_a_capture_the_migration_cannot_convert_is_returned_as_captured(
    module, monkeypatch
):
    """Reading a run's results must not fail over its configuration record."""

    def refuse(_body):
        raise ValueError("cannot convert")

    monkeypatch.setattr(module, "migrate_config", refuse)

    body = _read_back(module, _compressed_item(_flat_capture()))["Config"]

    assert body["classification"]["model"] == "flat-model"


@pytest.mark.unit
def test_a_run_with_no_captured_configuration_reads_back_empty(module):
    assert _read_back(module, {"PK": "testrun#run-1"}) == {}
