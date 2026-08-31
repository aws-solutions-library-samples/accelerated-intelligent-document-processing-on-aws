# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Configuration-revision pinning on a test run.

Why this matters: a run records WHICH configuration produced its numbers. Before
revisions it could only record a profile name, so a later save silently changed
what that name meant and two runs of "the same config" were not comparable.

The properties under test:

- the resolved revision is recorded on the run and stamped onto the copied
  documents, whether or not the caller named one;
- an explicitly requested revision wins;
- the revision is pinned against retention, because a comparison is only
  interpretable while both runs' configurations still exist;
- a profile with no history still runs, recording no revision.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE", "tracking")
os.environ.setdefault("CONFIG_TABLE", "config")
os.environ.setdefault("FILE_COPY_QUEUE_URL", "https://sqs.example/queue")


@pytest.fixture
def index():
    """Load the resolver fresh, with AWS clients replaced."""
    spec = importlib.util.spec_from_file_location(
        "test_runner_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_runner_index"] = module
    spec.loader.exec_module(module)

    module.sqs = MagicMock()
    module.dynamodb = MagicMock()
    module._get_test_set = MagicMock(
        return_value={"name": "w2-set", "fileCount": 3, "activeReference": 2}
    )
    module._active_config_version = MagicMock(return_value="lending")
    module._capture_config = MagicMock(return_value={"Config": {"notes": "x"}})
    module._published_revision = MagicMock(return_value=7)
    module._store_test_run_metadata = MagicMock()
    return module


def _event(input_data):
    return {
        "info": {"fieldName": "startTestRun"},
        "arguments": {"input": input_data},
        "identity": {"claims": {"cognito:groups": ["Admin"], "email": "a@example.com"}},
    }


def _sqs_body(index):
    return json.loads(index.sqs.send_message.call_args.kwargs["MessageBody"])


@pytest.mark.unit
class TestRevisionPinning:
    def test_the_resolved_revision_is_recorded_and_stamped(self, index):
        index.handler(_event({"testSetId": "w2-set"}), None)

        # Recorded on the run…
        assert index._store_test_run_metadata.call_args.kwargs["config_revision"] == 7
        # …and stamped onto the documents the copier stages.
        assert _sqs_body(index)["configRevision"] == 7

    def test_an_explicit_revision_wins_over_the_current_one(self, index):
        index.handler(
            _event({"testSetId": "w2-set", "configVersion": "lending", "configRevision": 3}),
            None,
        )

        assert _sqs_body(index)["configRevision"] == 3
        # No need to ask what the current revision is when the caller named one.
        index._published_revision.assert_not_called()

    def test_the_captured_config_comes_from_the_pinned_revision(self, index):
        index.handler(
            _event({"testSetId": "w2-set", "configVersion": "lending", "configRevision": 3}),
            None,
        )

        index._capture_config.assert_called_once_with("config", "lending", 3)

    def test_a_profile_with_no_history_runs_without_a_revision(self, index):
        index._published_revision.return_value = None

        index.handler(_event({"testSetId": "w2-set"}), None)

        assert "configRevision" not in _sqs_body(index)
        assert index._store_test_run_metadata.call_args.kwargs["config_revision"] is None

    def test_the_run_still_records_the_profile(self, index):
        index.handler(_event({"testSetId": "w2-set"}), None)
        assert _sqs_body(index)["configVersion"] == "lending"


@pytest.mark.unit
class TestRetentionPin:
    def test_capturing_a_revision_marks_it_against_pruning(self, monkeypatch):
        """
        Retention keeps only the last N revisions; a run's configuration must
        outlive that window or its comparison becomes unreadable.
        """
        spec = importlib.util.spec_from_file_location(
            "test_runner_index_pin", Path(__file__).with_name("index.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["test_runner_index_pin"] = module
        spec.loader.exec_module(module)

        manager = MagicMock()
        manager.get_revision.return_value = {"notes": "pinned body"}
        fake_manager_cls = MagicMock(return_value=manager)
        monkeypatch.setitem(
            sys.modules,
            "idp_common.config.configuration_manager",
            MagicMock(ConfigurationManager=fake_manager_cls),
        )
        module.dynamodb = MagicMock()

        captured = module._capture_config("config-table", "lending", 5)

        assert captured["Config"] == {"notes": "pinned body"}
        manager.get_revision.assert_called_once_with("lending", 5)
        manager.mark_revision_pinned.assert_called_once_with("lending", 5)

    def test_the_captured_body_carries_no_floats(self, monkeypatch):
        """
        A revision body is JSON, so it carries Python floats. The captured config
        goes straight into the run's DynamoDB item, and the DynamoDB resource
        client rejects floats — "Float types are not supported. Use Decimal types
        instead." — which failed every startTestRun that pinned a revision.
        """
        spec = importlib.util.spec_from_file_location(
            "test_runner_index_floats", Path(__file__).with_name("index.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["test_runner_index_floats"] = module
        spec.loader.exec_module(module)

        manager = MagicMock()
        manager.get_revision.return_value = {
            "extraction": {"temperature": 0.0, "top_p": 0.95},
            "classes": [{"name": "W2", "threshold": 0.8}],
        }
        monkeypatch.setitem(
            sys.modules,
            "idp_common.config.configuration_manager",
            MagicMock(ConfigurationManager=MagicMock(return_value=manager)),
        )
        module.dynamodb = MagicMock()

        captured = module._capture_config("config-table", "lending", 5)

        def floats(node):
            if isinstance(node, float):
                return [node]
            if isinstance(node, dict):
                return [f for v in node.values() for f in floats(v)]
            if isinstance(node, list):
                return [f for v in node for f in floats(v)]
            return []

        assert floats(captured) == [], "captured config still contains float values"
        # And the values survive, as Decimal.
        assert str(captured["Config"]["extraction"]["temperature"]) == "0.0"

    def test_an_unavailable_revision_falls_back_to_the_profile(self, monkeypatch):
        """
        Capture is for the run RECORD, not for processing: a pruned revision
        should still leave a usable record rather than failing the run. (The
        pipeline itself refuses to process a pinned revision it cannot read.)
        """
        spec = importlib.util.spec_from_file_location(
            "test_runner_index_fallback", Path(__file__).with_name("index.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["test_runner_index_fallback"] = module
        spec.loader.exec_module(module)

        manager = MagicMock()
        manager.get_revision.return_value = None
        monkeypatch.setitem(
            sys.modules,
            "idp_common.config.configuration_manager",
            MagicMock(ConfigurationManager=MagicMock(return_value=manager)),
        )
        table = MagicMock()
        table.get_item.return_value = {"Item": {"Configuration": "Config#lending"}}
        module.dynamodb = MagicMock()
        module.dynamodb.Table.return_value = table
        module._decompress_config_item = MagicMock(return_value={"notes": "head"})

        captured = module._capture_config("config-table", "lending", 99)

        assert captured["Config"] == {"notes": "head"}
        manager.mark_revision_pinned.assert_not_called()
