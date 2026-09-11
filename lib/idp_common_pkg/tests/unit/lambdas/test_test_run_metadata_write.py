# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What `_store_test_run_metadata` actually writes.

This function had no test at all, which is how it shipped reading `purpose` out
of the *caller's* scope — a `NameError` on every call. Because its `except`
re-raises, that failed every test run start outright, and the failure reached a
deployed stack. `basedpyright` caught it in CI; nothing in the suite did.

The `Purpose` attribute already had tests, but only for its READERS
(`_is_draft_labeling_run`, `_awaiting_metrics` in the results resolver). Testing
both sides of a persisted field is the lesson: a reader test passes happily
against a value nothing ever wrote.
"""

import importlib.util
import os
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../../..")

_ENV = {
    "TRACKING_TABLE": "test-table",
    "CONFIG_TABLE": "test-config-table",
    "FILE_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1/q",
    "AWS_REGION": "us-east-1",
}


@pytest.fixture
def test_runner():
    """Load the flat Lambda `index.py` by path, with env set at import time."""
    with patch.dict(os.environ, _ENV):
        with patch("boto3.client"), patch("boto3.resource"):
            spec = importlib.util.spec_from_file_location(
                "test_runner_index_metadata",
                os.path.join(
                    _REPO_ROOT, "nested/api-resolvers/src/lambda/test_runner/index.py"
                ),
            )
            if spec is None or spec.loader is None:
                raise ImportError("Could not load test_runner/index.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod


def _write(test_runner, **kwargs):
    """Call the writer against a stub table and return the item it stored."""
    table = MagicMock()
    with patch.object(test_runner, "dynamodb") as ddb:
        ddb.Table.return_value = table
        test_runner._store_test_run_metadata(
            "tracking-table",
            "run-1",
            "set-1",
            "Set One",
            {"Config": {}},
            [],
            **kwargs,
        )
    assert table.put_item.called, "metadata was never written"
    return table.put_item.call_args.kwargs["Item"]


@pytest.mark.unit
class TestStoreTestRunMetadata:
    def test_writes_the_run_without_raising(self, test_runner):
        """The regression itself: this call raised NameError before the fix."""
        item = _write(test_runner)

        assert item["PK"] == "testrun#run-1"
        assert item["Status"] == "QUEUED"

    def test_persists_a_draft_labeling_purpose(self, test_runner):
        # The value the results resolver reads to decide that a run can never
        # have accuracy metrics. If nothing writes it, that decision silently
        # falls back to matching free-text Context.
        item = _write(test_runner, purpose="draft-labeling")

        assert item["Purpose"] == "draft-labeling"

    def test_defaults_to_scoring_rather_than_omitting_the_purpose(self, test_runner):
        # An absent Purpose sends the reader to its Context fallback, so the
        # default has to be explicit rather than missing.
        item = _write(test_runner)

        assert item["Purpose"] == "scoring"

    def test_purpose_is_independent_of_the_user_typed_context(self, test_runner):
        # Context is a free-text label a user can type. A scoring run whose
        # context happens to mention labeling must still record itself as scoring.
        item = _write(test_runner, context="Draft labeling run", purpose="scoring")

        assert item["Purpose"] == "scoring"
        assert item["Context"] == "Draft labeling run"

    def test_optional_attributes_are_omitted_when_not_supplied(self, test_runner):
        item = _write(test_runner)

        assert "Context" not in item
        assert "ConfigVersion" not in item
        assert "TestSetVersion" not in item
