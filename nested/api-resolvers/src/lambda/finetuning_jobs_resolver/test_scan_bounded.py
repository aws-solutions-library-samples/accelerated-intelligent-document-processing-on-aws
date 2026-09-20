# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`listFinetuningJobs` bounds the filtered scan that finds the jobs.

The filter is sparse against the TrackingTable, which holds a row per document in
the deployment, so "scan until `LastEvaluatedKey` is absent" costs the whole
document history — and the operation's declared policy is `ANY`, meaning any
authenticated caller, including one in no group, which self-signup produces. The
request is a single cheap HTTP call.

`nextToken` was already accepted as an `ExclusiveStartKey` but never returned, so
there was no way for a caller to ask for less than everything, and no way for the
resolver to hand back a resume point after stopping.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE", "IDP-TrackingTable")

pytestmark = pytest.mark.unit


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "finetuning_jobs_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["finetuning_jobs_index"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()


class _EndlessTable:
    """A table whose scan never runs out of pages."""

    def __init__(self, items_per_page=0):
        self.calls = 0
        self._items_per_page = items_per_page

    def scan(self, **kwargs):
        self.calls += 1
        items = [
            {
                "PK": f"finetuning#{self.calls}-{i}",
                "SK": "metadata",
                "jobId": f"{self.calls}-{i}",
                "jobName": "j",
                "testSetId": "t",
                "baseModelId": "m",
                "status": "COMPLETED",
                "createdAt": "2026-01-01T00:00:00Z",
            }
            for i in range(self._items_per_page)
        ]
        return {"Items": items, "LastEvaluatedKey": {"PK": f"p{self.calls}", "SK": "s"}}


class _Context:
    def __init__(self, remaining_ms):
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self._remaining_ms


@pytest.fixture
def endless(monkeypatch):
    def _configure(items_per_page=0):
        table = _EndlessTable(items_per_page)
        monkeypatch.setattr(index.dynamodb, "Table", lambda _name: table)
        return table

    return _configure


class TestTheScanIsBounded:
    def test_the_page_cap_stops_an_endless_scan(self, endless):
        table = endless()

        result = index.list_finetuning_jobs({}, None)

        assert table.calls == index.MAX_SCAN_PAGES
        assert result["nextToken"], "stopped without a way to resume"

    def test_a_shrinking_invocation_clock_stops_the_scan(self, endless):
        table = endless()

        result = index.list_finetuning_jobs(
            {}, _Context(index.SCAN_TIME_RESERVE_MS - 1)
        )

        assert table.calls == 1, "should stop after the first page"
        assert result["nextToken"]

    def test_a_finite_scan_still_returns_no_token(self, monkeypatch):
        class _OnePage:
            def scan(self, **kwargs):
                return {"Items": []}

        monkeypatch.setattr(index.dynamodb, "Table", lambda _name: _OnePage())

        result = index.list_finetuning_jobs({}, None)

        assert result["nextToken"] is None
        assert result["items"] == []

    def test_the_resume_token_is_a_usable_exclusive_start_key(self, endless):
        endless()
        first = index.list_finetuning_jobs({}, None)

        captured = {}

        class _Recording:
            def scan(self, **kwargs):
                captured.update(kwargs)
                return {"Items": []}

        index.dynamodb.Table = lambda _name: _Recording()  # noqa: E731
        index.list_finetuning_jobs({"nextToken": first["nextToken"]}, None)

        assert captured["ExclusiveStartKey"] == {
            "PK": f"p{index.MAX_SCAN_PAGES}",
            "SK": "s",
        }


class TestThePageSizeIsClampedNotDefaulted:
    def test_an_oversized_limit_cannot_raise_the_page_size(self, endless):
        endless(items_per_page=3)

        result = index.list_finetuning_jobs({"limit": 10_000}, None)

        assert len(result["items"]) == index.MAX_PAGE_SIZE

    def test_an_absent_limit_gets_the_default(self):
        assert index._clamped_limit(None) == index.DEFAULT_PAGE_SIZE

    def test_a_non_positive_limit_is_raised_to_one(self):
        assert index._clamped_limit(0) == 1
        assert index._clamped_limit(-5) == 1
