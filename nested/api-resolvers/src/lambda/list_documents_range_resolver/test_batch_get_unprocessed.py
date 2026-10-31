# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""listDocumentsByDateRange resubmits BatchGetItem ``UnprocessedKeys``, boundedly.

BatchGetItem may hold keys back (throttling, or the 16 MB response cap) and return
them as ``UnprocessedKeys``. The resolver must resubmit them, with backoff, a
bounded number of times, and then fail the request rather than spin until the
function times out or return a page silently missing documents.

The table is moto's; only the hold-back is injected, by a wrapper around the real
client that moves some returned items into ``UnprocessedKeys``. The wrapper caps
its own call count, so an unbounded loop in the code under test fails the test
instead of hanging it.
"""

import importlib.util
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE_NAME", "IDP-TrackingTable")

TABLE = "IDP-TrackingTable"
# Far above the resolver's own bound; reaching it means the loop is unbounded.
STUB_CALL_CAP = 40


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "list_documents_range_index_unprocessed", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["list_documents_range_index_unprocessed"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()


class _HoldBack:
    """A real moto client whose batch_get_item holds keys back on chosen calls.

    ``hold(call_number, keys_in_request)`` returns how many of the returned items
    to move into UnprocessedKeys on that call.
    """

    def __init__(self, real, hold):
        self._real = real
        self._hold = hold
        self.calls = []

    def batch_get_item(self, RequestItems):
        self.calls.append(len(RequestItems[TABLE]["Keys"]))
        if len(self.calls) > STUB_CALL_CAP:
            raise AssertionError(
                f"batch_get_item called more than {STUB_CALL_CAP} times: "
                "the UnprocessedKeys loop is unbounded"
            )
        response = self._real.batch_get_item(RequestItems=RequestItems)
        items = response["Responses"][TABLE]
        held = min(self._hold(len(self.calls), len(items)), len(items))
        if held:
            kept, back = items[: len(items) - held], items[len(items) - held :]
            response["Responses"][TABLE] = kept
            response["UnprocessedKeys"] = {
                TABLE: {"Keys": [{"PK": i["PK"], "SK": i["SK"]} for i in back]}
            }
        return response


@pytest.fixture
def table_with_docs(monkeypatch):
    with mock_aws():
        real = boto3.client("dynamodb", region_name="us-east-1")
        real.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        keys = [f"run/doc-{n:03d}.pdf" for n in range(150)]
        for key in keys:
            real.put_item(
                TableName=TABLE,
                Item={
                    "PK": {"S": f"doc#{key}"},
                    "SK": {"S": "none"},
                    "ObjectKey": {"S": key},
                    "ObjectStatus": {"S": "COMPLETED"},
                    "InitialEventTime": {"S": "2026-01-01T01:00:00Z"},
                },
            )
        sleeps = []
        monkeypatch.setattr(index.time, "sleep", sleeps.append)

        def install(hold):
            stub = _HoldBack(real, hold)
            monkeypatch.setattr(index.boto3, "client", lambda *a, **k: stub)
            return stub

        yield keys, install, sleeps


def _assert_refused_for_unprocessed_keys(error):
    # The stub's own cap is an AssertionError: reaching it means the loop never
    # gave up, which is the defect, not the refusal.
    assert not isinstance(error, AssertionError), str(error)
    assert type(error).__name__ == "UnprocessedKeysError", repr(error)
    # Not a ValueError: the dispatcher would report that as the caller's 400.
    assert not isinstance(error, ValueError)


def _record_jitter(monkeypatch):
    drawn = []
    real_uniform = index.random.uniform

    def uniform(low, high):
        drawn.append((low, high))
        return real_uniform(low, high)

    monkeypatch.setattr(index.random, "uniform", uniform)
    return drawn


def _cap(attempt):
    return min(1.0, 0.05 * 2**attempt)


@pytest.mark.unit
def test_transiently_held_back_keys_are_resubmitted_and_returned(
    table_with_docs, monkeypatch
):
    keys, install, sleeps = table_with_docs
    drawn = _record_jitter(monkeypatch)
    # Hold back 30 items on calls 1-3 (chunk 1) and 5-6 (chunk 2).
    stub = install(lambda call, n: 30 if call in (1, 2, 3, 5, 6) else 0)

    documents = index._batch_get_documents(TABLE, keys)

    assert sorted(documents) == sorted(keys)
    # Chunk 1 (100 keys): calls 1, 2, 3 hold back, call 4 completes.
    # Chunk 2 (50 keys): calls 5, 6 hold back, call 7 completes.
    assert stub.calls == [100, 30, 30, 30, 50, 30, 30]
    # One backoff between each resubmission, growing and capped per attempt.
    assert len(sleeps) == 5
    for attempt, slept in zip([1, 2, 3, 1, 2], sleeps):
        assert 0 <= slept <= _cap(attempt)
    # Full jitter: each delay is drawn uniformly from zero to that attempt's cap.
    assert drawn == [(0, _cap(a)) for a in [1, 2, 3, 1, 2]]


@pytest.mark.unit
def test_keys_that_stay_unprocessed_fail_the_request_after_the_bound(
    table_with_docs, monkeypatch
):
    keys, install, sleeps = table_with_docs
    drawn = _record_jitter(monkeypatch)
    stub = install(lambda call, n: 1 if n else 0)

    with pytest.raises(Exception) as caught:
        index._batch_get_documents(TABLE, keys[:10])

    _assert_refused_for_unprocessed_keys(caught.value)
    assert "1 document records" in str(caught.value)
    assert len(stub.calls) == index.BATCH_GET_MAX_ATTEMPTS
    assert len(sleeps) == index.BATCH_GET_MAX_ATTEMPTS - 1
    # The backoff grows to, and stops at, one second (0.05 * 2**5 would be 1.6).
    assert [high for _, high in drawn] == [0.1, 0.2, 0.4, 0.8, 1.0]


@pytest.mark.unit
def test_the_handler_fails_rather_than_returning_a_short_page(
    table_with_docs, monkeypatch
):
    keys, install, _ = table_with_docs
    install(lambda call, n: 1 if n else 0)
    entries = [
        {
            "PK": "list#2026-01-01#s#00",
            "SK": f"ts#2026-01-01T01:00:00#id#{k}",
            "ObjectKey": k,
        }
        for k in keys[:5]
    ]
    served = iter([entries])
    monkeypatch.setattr(index, "_query_shard", lambda *a, **k: next(served, []))
    monkeypatch.setenv("TRACKING_TABLE_NAME", TABLE)
    event = {
        "info": {"fieldName": "listDocumentsByDateRange"},
        "arguments": {
            "startDateTime": "2026-01-01T00:00:00.000Z",
            "endDateTime": "2026-01-01T03:00:00.000Z",
        },
        "identity": {"claims": {"cognito:groups": ["Admin"], "email": "a@e.com"}},
    }

    with pytest.raises(Exception) as caught:
        index.handler(event, None)

    _assert_refused_for_unprocessed_keys(caught.value)
