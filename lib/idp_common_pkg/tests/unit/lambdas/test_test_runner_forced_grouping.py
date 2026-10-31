# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The test runner carries a forced section grouping to the file copier (#1699).

A section-scoped re-extract hands the runner the document's whole grouping, with
one section's class corrected, as ``documentSections``. The runner owns run
creation, so the value has to survive into the copier's queue message, where the
copier stamps it as object metadata. Dropping it here would let the pipeline
classify the document from scratch and regroup every section.

Driven through the real ``handler`` with the persistence helpers stubbed, so the
message checked is the one the handler builds.
"""

import importlib.util
import json
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
    with patch.dict(os.environ, _ENV):
        with patch("boto3.client"), patch("boto3.resource"):
            spec = importlib.util.spec_from_file_location(
                "test_runner_index_forced_grouping",
                os.path.join(
                    _REPO_ROOT, "nested/api-resolvers/src/lambda/test_runner/index.py"
                ),
            )
            if spec is None or spec.loader is None:
                raise ImportError("Could not load test_runner/index.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod


def _start(test_runner, extra):
    """Run startTestRun as the re-extract resolver invokes it; return the message."""
    sqs = MagicMock()
    run_input = {
        "testSetId": "ts1",
        "objectKeys": ["packet.pdf"],
        "draftLabeling": True,
        "configVersion": "v3",
        **extra,
    }
    with (
        patch.dict(os.environ, _ENV),
        patch.object(test_runner, "sqs", sqs),
        patch.object(
            test_runner,
            "_get_test_set",
            return_value={"name": "Set One", "fileCount": 1},
        ),
        patch.object(test_runner, "_require_profile"),
        patch.object(test_runner, "_published_revision", return_value=2),
        patch.object(test_runner, "_capture_config", return_value={"Config": {}}),
        patch.object(test_runner, "_reserve_test_run_id", return_value="run-1"),
    ):
        test_runner.handler(
            {"info": {"fieldName": "startTestRun"}, "arguments": {"input": run_input}},
            None,
        )
    assert sqs.send_message.called, "no copy message was sent"
    return json.loads(sqs.send_message.call_args.kwargs["MessageBody"])


@pytest.mark.unit
class TestRunnerForwardsTheGrouping:
    def test_a_forced_grouping_reaches_the_copier_message(self, test_runner):
        grouping = '[["1","Payslip","0-1"],["2","W2","2"]]'

        message = _start(test_runner, {"documentSections": grouping})

        assert message["documentSections"] == grouping
        assert "documentClass" not in message

    def test_a_whole_document_class_is_forwarded_as_before(self, test_runner):
        message = _start(test_runner, {"documentClass": "W2"})

        assert message["documentClass"] == "W2"
        assert "documentSections" not in message

    def test_an_ordinary_run_forces_nothing(self, test_runner):
        message = _start(test_runner, {})

        assert "documentSections" not in message
        assert "documentClass" not in message


@pytest.mark.unit
class TestRunnerRefusesAGroupingItWasNotBuiltFor:
    """``startTestRun`` is a public operation, and its input objects are validated
    only shallowly, so the runner checks the grouping itself: it describes one
    document's ground truth, for a draft-labeling run, and must decode."""

    GROUPING = '[["1","Payslip","0-1"],["2","W2","2"]]'

    @pytest.mark.parametrize(
        "extra, reason",
        [
            ({"draftLabeling": False}, "draft-labeling run of exactly one document"),
            (
                {"objectKeys": ["a.pdf", "b.pdf"]},
                "draft-labeling run of exactly one document",
            ),
            ({"objectKeys": []}, "draft-labeling run of exactly one document"),
            ({"documentSections": '[["1","W2","0-"]]'}, "Invalid documentSections"),
        ],
    )
    def test_it_is_refused_before_anything_is_queued(self, test_runner, extra, reason):
        sqs = MagicMock()
        run_input = {
            "testSetId": "ts1",
            "objectKeys": ["packet.pdf"],
            "draftLabeling": True,
            "documentSections": self.GROUPING,
            **extra,
        }
        with (
            patch.dict(os.environ, _ENV),
            patch.object(test_runner, "sqs", sqs),
            patch.object(
                test_runner,
                "_get_test_set",
                return_value={"name": "Set One", "fileCount": 2},
            ),
            patch.object(test_runner, "_reserve_test_run_id") as reserve,
        ):
            with pytest.raises(ValueError, match=reason):
                test_runner.handler(
                    {
                        "info": {"fieldName": "startTestRun"},
                        "arguments": {"input": run_input},
                    },
                    None,
                )

        sqs.send_message.assert_not_called()
        reserve.assert_not_called()
