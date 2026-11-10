# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The bulk review worker: steps, hand-offs, dead letters and retries (stub service)."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_INDEX = Path(__file__).with_name("index.py")
DLQ = "arn:aws:sqs:us-east-1:123456789012:jobs-dlq"


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setenv("JOB_DLQ_ARN", DLQ)
    monkeypatch.setenv("MAX_RECEIVES", "5")
    name = "bulk_review_worker_index_under_test"
    spec = importlib.util.spec_from_file_location(name, _INDEX)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module._SERVICE = MagicMock()
    yield module
    sys.modules.pop(name, None)


def _record(source="arn:aws:sqs:us-east-1:123456789012:jobs", receives=1):
    return {
        "messageId": "m-1",
        "body": json.dumps({"jobId": "brj-1", "wakeToken": "w"}),
        "eventSourceARN": source,
        "attributes": {"ApproximateReceiveCount": str(receives)},
    }


CONTEXT = SimpleNamespace(get_remaining_time_in_millis=lambda: 800_000)


@pytest.mark.unit
class TestWorker:
    def test_a_step_that_continues_schedules_its_next_wake_up(self, mod):
        mod._SERVICE.run_job_step.return_value = 60
        assert mod.handler({"Records": [_record()]}, CONTEXT) == {
            "batchItemFailures": []
        }
        job, wake, remaining = mod._SERVICE.run_job_step.call_args.args
        assert (job, wake, remaining()) == ("brj-1", "w", 800.0)
        mod._SERVICE.schedule.assert_called_once_with("brj-1", 60)

    def test_a_finished_step_schedules_nothing(self, mod):
        mod._SERVICE.run_job_step.return_value = None
        mod.handler({"Records": [_record()]}, CONTEXT)
        mod._SERVICE.schedule.assert_not_called()

    def test_a_dead_letter_fails_its_job(self, mod):
        mod.handler({"Records": [_record(source=DLQ)]}, CONTEXT)
        mod._SERVICE.fail_job.assert_called_once_with("brj-1", "dead_lettered")
        mod._SERVICE.run_job_step.assert_not_called()

    def test_an_early_failure_is_retried(self, mod):
        mod._SERVICE.run_job_step.side_effect = RuntimeError("boom")
        out = mod.handler({"Records": [_record(receives=2)]}, CONTEXT)
        assert out == {"batchItemFailures": [{"itemIdentifier": "m-1"}]}
        mod._SERVICE.fail_job.assert_not_called()

    def test_the_last_receive_fails_the_job(self, mod):
        mod._SERVICE.run_job_step.side_effect = RuntimeError("boom")
        out = mod.handler({"Records": [_record(receives=5)]}, CONTEXT)
        assert out == {"batchItemFailures": []}
        mod._SERVICE.fail_job.assert_called_once_with("brj-1", "worker_error")

    def test_a_malformed_message_is_dropped(self, mod):
        record = {**_record(), "body": "not json"}
        assert mod.handler({"Records": [record]}, CONTEXT) == {"batchItemFailures": []}

    def test_queue_depth_adds_visible_and_delayed(self, mod, monkeypatch):
        monkeypatch.setenv("QUEUE_URL", "q")
        mod._SQS = MagicMock()
        mod._SQS.get_queue_attributes.return_value = {
            "Attributes": {
                "ApproximateNumberOfMessages": "7",
                "ApproximateNumberOfMessagesDelayed": "3",
            }
        }
        assert mod.queue_depth() == 10

    def test_send_job_caps_the_delay(self, mod, monkeypatch):
        monkeypatch.setenv("JOB_QUEUE_URL", "jq")
        mod._SQS = MagicMock()
        mod.send_job("brj-1", "t", 5000)
        assert mod._SQS.send_message.call_args.kwargs["DelaySeconds"] == 900
