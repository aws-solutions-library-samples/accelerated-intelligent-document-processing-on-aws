# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The review queue sweeper handler: wiring, deadline, and the no-table path.

The sweep itself is ``idp_common.review_queues.sweeper`` and is tested there; this
suite pins what the handler adds: it does nothing without ``HITL_TABLE_NAME``, it
hands the sweeper a deadline taken from the Lambda's remaining time, and it builds
the sweeper once per container from its environment.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from idp_common.review_queues.models import SweepReport

_INDEX = Path(__file__).with_name("index.py")
_ENV = {
    "HITL_TABLE_NAME": "hitl-table",
    "WORKFLOW_TRACKER_FUNCTION_NAME": "tracker-fn",
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:sm",
    "OUTPUT_BUCKET": "output-bucket",
}


def _load(env):
    """Import ``index.py`` under ``env`` (its store is decided at import)."""
    name = "review_queue_sweeper_index_under_test"
    with patch.dict(os.environ, env):
        for key in set(_ENV) - set(env):
            os.environ.pop(key, None)
        spec = importlib.util.spec_from_file_location(name, _INDEX)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


class _Context:
    def __init__(self, remaining_ms):
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self.remaining_ms


@pytest.fixture
def module():
    with patch.dict(os.environ, _ENV):
        yield _load(_ENV)


@pytest.mark.unit
class TestNoTable:
    def test_returns_an_empty_report_and_builds_no_sweeper(self):
        module = _load({})
        assert module._STORE is None
        with patch.object(module, "_sweeper") as sweeper:
            result = module.handler({}, _Context(60_000))
        assert result == SweepReport().model_dump()
        sweeper.assert_not_called()


@pytest.mark.unit
class TestSweep:
    def test_runs_with_a_deadline_from_the_remaining_time(self, module):
        fake = MagicMock()
        fake.run.return_value = SweepReport(visited=3, expired=1)
        with (
            patch.object(module, "_sweeper", return_value=fake),
            patch.object(module.time, "time", return_value=1_000.0),
        ):
            result = module.handler({}, _Context(45_000))
        fake.run.assert_called_once_with(deadline_ms=1_000_000 + 45_000)
        assert result == SweepReport(visited=3, expired=1).model_dump()

    def test_sweeper_is_built_once_from_the_environment(self, module):
        import idp_common.review_queues.sweeper as sweeper_module

        built = MagicMock(name="ReviewSweeper")
        with (
            patch.object(sweeper_module, "ReviewSweeper", built),
            patch("idp_common.docs_service.create_document_service") as documents,
            patch(
                "idp_common.review_queues.service.create_finalize_sender"
            ) as finalize_sender,
            patch("boto3.client") as client,
        ):
            first = module._sweeper()
            second = module._sweeper()

        assert first is second is built.return_value
        built.assert_called_once()
        args, kwargs = built.call_args
        assert args == (module._STORE,)
        assert kwargs["tracker_function_name"] == "tracker-fn"
        assert kwargs["state_machine_arn"] == _ENV["STATE_MACHINE_ARN"]
        assert kwargs["output_bucket"] == "output-bucket"
        assert kwargs["document_service"] is documents.return_value
        documents.assert_called_once_with(mode="dynamodb")
        finalize_sender.assert_called_once_with(documents.return_value)
        assert kwargs["finalize_sender"] is finalize_sender.return_value
        assert sorted(c.args[0] for c in client.call_args_list) == [
            "lambda",
            "stepfunctions",
        ]
