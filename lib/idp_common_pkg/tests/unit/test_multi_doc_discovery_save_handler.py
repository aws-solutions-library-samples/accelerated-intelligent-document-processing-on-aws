# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the multi-document discovery save step puts in the job record the UI shows.

``save_handler`` rebuilds each discovered class from the analysis step's output and
writes the classes and the reflection report into the job record. The labels are
class names, and the text that replaces a failed reflection report is plain text.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from idp_common.config.class_names import is_valid_class_name

pytestmark = pytest.mark.unit

REMOTE = "remote.example"


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "src" / "lambda" / "multi_doc_discovery").is_dir():
            return parent
    raise RuntimeError("repo root not found")


@pytest.fixture
def save_handler(monkeypatch):
    status = types.ModuleType("appsync_status")
    status.update_status = MagicMock()
    monkeypatch.setitem(sys.modules, "appsync_status", status)
    path = _find_repo_root() / "src/lambda/multi_doc_discovery/save_handler.py"
    spec = importlib.util.spec_from_file_location("mdd_save_handler", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, status.update_status


def test_labels_and_a_failed_reflection_reach_the_job_record_as_plain_text(
    save_handler,
):
    mod, update_status = save_handler
    agent = MagicMock()
    agent.reflect.side_effect = RuntimeError(
        f"model said: <img src=x> ![p](https://{REMOTE}/a.png) see https://{REMOTE}/b"
    )
    event = {
        "jobId": "j1",
        "bucket": "b",
        "analyzeResults": [
            {
                "clusterId": 0,
                "classification": f"Bank Statement [x](https://{REMOTE}/c)",
                "jsonSchema": {"type": "object"},
                "documentCount": 2,
            }
        ],
    }
    with (
        patch.object(mod, "BedrockClient"),
        patch.object(mod, "DiscoveryAgent", return_value=agent),
        patch.object(mod.boto3, "client"),
    ):
        out = mod.handler(event, None)

    report = out["reflectionReport"]
    assert report.startswith("Reflection failed: model said:")
    assert REMOTE not in report
    assert "<img" not in report
    classes = json.loads(out["discoveredClassesJson"])
    assert classes[0]["classification"] == "Bank-Statement-x"
    assert is_valid_class_name(classes[0]["classification"])
    kwargs = update_status.call_args.kwargs
    assert kwargs["reflection_report"] == report
    assert kwargs["discovered_classes"] == out["discoveredClassesJson"]
