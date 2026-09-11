# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Test run ids are unique even when two runs start in the same second (#879).

A run id is ``<test set name>-YYYYMMDD-HHMMSS``. Two runs of one test set
submitted within a second — an A/B harness launching both arms back to back,
two users clicking Run — used to get the same id: the second metadata write
overwrote the first, and both copier messages staged into the same input
prefix, so every document attached to one run.

The properties under test:

- the metadata write refuses to overwrite an existing run;
- a collision advances the id to the next second and the run that is actually
  created is the one the caller and the copier are told about;
- the id keeps its shape, because the UI and the GSI backfill parse it out of
  object keys;
- the retry is bounded.
"""

import importlib.util
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE", "tracking")
os.environ.setdefault("CONFIG_TABLE", "config")
os.environ.setdefault("FILE_COPY_QUEUE_URL", "https://sqs.example/queue")

SUBMITTED_AT = datetime(2026, 9, 11, 13, 11, 20)

# What the UI (documents-table-config.tsx) and the GSI backfill Lambda expect.
ID_SHAPE = re.compile(r"^[^/]+-\d{8}-\d{6}$")


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _FrozenDatetime(datetime):
    @classmethod
    def utcnow(cls):
        return SUBMITTED_AT


@pytest.fixture
def index():
    """The resolver with AWS clients replaced and the clock stopped."""
    module = _load("test_runner_index_ids")
    module.sqs = MagicMock()
    module.dynamodb = MagicMock()
    module.datetime = _FrozenDatetime
    module._get_test_set = MagicMock(
        return_value={"name": "RealKIE-FCC-Verified", "fileCount": 40}
    )
    module._active_config_version = MagicMock(return_value="rk-adv-off")
    module._capture_config = MagicMock(return_value={"Config": {"notes": "x"}})
    module._published_revision = MagicMock(return_value=2)
    module._store_test_run_metadata = MagicMock()
    return module


def _event():
    return {
        "info": {"fieldName": "startTestRun"},
        "arguments": {"input": {"testSetId": "realkie-fcc-verified"}},
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
    }


def _conditional_failure():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
        "PutItem",
    )


@pytest.mark.unit
class TestMetadataWrite:
    def test_never_overwrites_an_existing_run(self):
        module = _load("test_runner_index_ids_write")
        table = MagicMock()
        module.dynamodb = MagicMock()
        module.dynamodb.Table.return_value = table

        module._store_test_run_metadata(
            "tracking", "set-20260911-131120", "s", "set", {}, []
        )

        assert (
            table.put_item.call_args.kwargs["ConditionExpression"]
            == "attribute_not_exists(PK)"
        )

    def test_an_existing_run_is_reported_as_a_taken_id(self):
        module = _load("test_runner_index_ids_taken")
        table = MagicMock()
        table.put_item.side_effect = _conditional_failure()
        module.dynamodb = MagicMock()
        module.dynamodb.Table.return_value = table

        with pytest.raises(module.TestRunIdTaken):
            module._store_test_run_metadata(
                "tracking", "set-20260911-131120", "s", "set", {}, []
            )

    def test_other_write_failures_still_propagate_as_themselves(self):
        """Only a collision is retried; a throttling or access error is not."""
        module = _load("test_runner_index_ids_other")
        table = MagicMock()
        table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem"
        )
        module.dynamodb = MagicMock()
        module.dynamodb.Table.return_value = table

        with pytest.raises(ClientError):
            module._store_test_run_metadata(
                "tracking", "set-20260911-131120", "s", "set", {}, []
            )


@pytest.mark.unit
class TestCollision:
    def test_an_uncontested_id_is_the_submission_second(self, index):
        result = index.handler(_event(), None)

        assert result["testRunId"] == "RealKIE-FCC-Verified-20260911-131120"
        assert ID_SHAPE.match(result["testRunId"])

    def test_a_collision_moves_the_id_to_the_next_second(self, index):
        index._store_test_run_metadata.side_effect = [index.TestRunIdTaken(), None]

        result = index.handler(_event(), None)

        attempted = [c.args[1] for c in index._store_test_run_metadata.call_args_list]
        assert attempted == [
            "RealKIE-FCC-Verified-20260911-131120",
            "RealKIE-FCC-Verified-20260911-131121",
        ]
        # The caller and the copier are told about the run that was created.
        assert result["testRunId"] == "RealKIE-FCC-Verified-20260911-131121"
        body = json.loads(index.sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["testRunId"] == "RealKIE-FCC-Verified-20260911-131121"

    def test_the_retried_id_keeps_the_shape_consumers_parse(self, index):
        index._store_test_run_metadata.side_effect = [index.TestRunIdTaken()] * 3 + [
            None
        ]

        result = index.handler(_event(), None)

        assert result["testRunId"] == "RealKIE-FCC-Verified-20260911-131123"
        assert ID_SHAPE.match(result["testRunId"])

    def test_the_retry_is_bounded(self, index):
        index._store_test_run_metadata.side_effect = index.TestRunIdTaken()

        with pytest.raises(RuntimeError, match="unique test run id"):
            index.handler(_event(), None)

        assert (
            index._store_test_run_metadata.call_count == index._MAX_TEST_RUN_ID_ATTEMPTS
        )
        index.sqs.send_message.assert_not_called()

    def test_nothing_is_queued_until_the_id_is_reserved(self, index):
        """The copier must never receive an id whose metadata was not written."""
        order = []
        index._store_test_run_metadata.side_effect = lambda *a, **k: order.append(
            "store"
        )
        index.sqs.send_message.side_effect = lambda **k: order.append("queue")

        index.handler(_event(), None)

        assert order == ["store", "queue"]
