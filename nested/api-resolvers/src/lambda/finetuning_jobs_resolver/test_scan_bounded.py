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
                "id": f"{self.calls}-{i}",
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
        """The clamp bounds the SCAN, so an oversized limit stops at the page cap
        rather than at the requested number. With a token outstanding the collected
        rows are all returned (see the faithful-token tests below), so assert on the
        clamp and the work done rather than on the response length."""
        table = endless(items_per_page=3)

        result = index.list_finetuning_jobs({"limit": 10_000}, None)

        assert index._clamped_limit(10_000) == index.MAX_PAGE_SIZE
        assert table.calls == index.MAX_SCAN_PAGES, (
            "an oversized limit must be bounded by the page cap, not honoured"
        )
        assert result["nextToken"]

    def test_an_absent_limit_gets_the_default(self):
        assert index._clamped_limit(None) == index.DEFAULT_PAGE_SIZE

    def test_a_non_positive_limit_is_raised_to_one(self):
        assert index._clamped_limit(0) == 1
        assert index._clamped_limit(-5) == 1


class TestThePageCapIsDerivedAndTheTokenIsFaithful:
    def test_the_page_cap_matches_the_read_budget_of_the_group_gated_sibling(self):
        """An `ANY` operation must not buy more capacity than a group-gated one.

        Measured cost of one filtered Scan page against a real tracking table:
        113 RCU mean, 142 max. The date-range cap allows 2,191 eventually-consistent
        Queries ~= 1,100 RCU per request; this is the same budget. If either number
        moves, this says so rather than leaving the parity as a claim in a comment.
        """
        measured_rcu_per_page = 113
        date_range_budget_rcu = 2191 * 0.5

        assert index.MAX_SCAN_PAGES * measured_rcu_per_page <= (
            date_range_budget_rcu * 1.2
        ), (
            f"{index.MAX_SCAN_PAGES} pages x {measured_rcu_per_page} RCU exceeds the "
            f"~{date_range_budget_rcu:.0f} RCU the group-gated date-range operation "
            "allows — and this operation's policy is ANY"
        )

    def test_nothing_collected_is_dropped_when_a_token_is_returned(self):
        """A token that resumes AFTER the last scanned page cannot return rows
        truncated out of this response, so truncating with one outstanding loses them
        from every page — a result that looks faithfully paginable and is not."""
        table = _EndlessTable(items_per_page=3)
        index.dynamodb.Table = lambda _name: table  # noqa: E731

        result = index.list_finetuning_jobs({"limit": 4}, None)

        assert result["nextToken"], "expected an outstanding token"
        # Two pages of 3 reach len(items) >= 4; all 6 must come back, not 4.
        assert len(result["items"]) == 6, (
            f"collected 6 jobs, returned {len(result['items'])} — the rest are "
            "returned by no page at all"
        )

    def test_the_scan_stops_as_soon_as_the_page_is_satisfied(self):
        table = _EndlessTable(items_per_page=3)
        index.dynamodb.Table = lambda _name: table  # noqa: E731

        index.list_finetuning_jobs({"limit": 4}, None)

        assert table.calls == 2, (
            "kept scanning past the point the caller's page was satisfied"
        )

    def test_a_completed_scan_is_still_truncated_to_the_page_size(self):
        """With no token outstanding the result set is complete and correctly
        ordered, so returning the newest `limit` is faithful."""

        class _OnePage:
            calls = 0

            def scan(self, **kwargs):
                _OnePage.calls += 1
                return {
                    "Items": [
                        {
                            "PK": f"finetuning#{i}",
                            "SK": "metadata",
                            "id": str(i),
                            "jobName": "j",
                            "testSetId": "t",
                            "baseModelId": "m",
                            "status": "COMPLETED",
                            "createdAt": f"2026-01-{i + 1:02d}T00:00:00Z",
                        }
                        for i in range(9)
                    ]
                }

        index.dynamodb.Table = lambda _name: _OnePage()  # noqa: E731

        result = index.list_finetuning_jobs({"limit": 4}, None)

        assert result["nextToken"] is None
        assert len(result["items"]) == 4
        # Newest first, so the highest createdAt.
        assert result["items"][0]["jobId"] == "8"

    def test_the_functions_timeout_leaves_room_for_the_scan_reserve(self):
        """Same premise check as the date-range resolver's: the function must
        outlive its own reserve without outliving the dispatcher's window by enough
        to matter."""
        from pathlib import Path

        template = (
            Path(__file__).resolve().parents[5] / "template.yaml"
        ).read_text()
        block = template.split("  FinetuningJobsResolverFunction:", 1)[1].split(
            "\n  Fine", 1
        )[0][:3000]
        timeout = int(
            next(
                line.split("Timeout:")[1]
                for line in block.splitlines()
                if line.strip().startswith("Timeout:")
            )
        )

        assert (index.SCAN_TIME_RESERVE_MS / 1000) < timeout <= 30, (
            f"Timeout {timeout}s: must exceed the {index.SCAN_TIME_RESERVE_MS}ms "
            "reserve, and must not outlive the dispatcher's 20s window by enough to "
            "buy orphaned work"
        )
