# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""listDocumentsByDateRange bounds the work a caller-supplied range can buy.

The range is decomposed into one DynamoDB Query per ``HOURS_PER_SHARD`` window and
those queries run sequentially, so the requested span — not the number of
documents in it — sets the invocation's cost. The collection loop does stop early
once ``limit`` entries are found, which is why this only bites on a SPARSE range;
every window before the deployment existed is sparse, and a caller chooses the
window.

Three properties here:

* the refusal happens, names the maximum, and is a ``ValueError`` so the
  dispatcher reports it as **400 BadRequest** rather than letting the request run
  past the dispatcher's 20s read timeout into an unexplained 504;
* it happens **before** anything is generated or read, because the partition list
  is itself part of what is being bounded;
* the cap's own premise still holds — the worst-case query count for
  ``MAX_RANGE_DAYS`` days, recomputed from ``SHARDS_PER_DAY``, still fits the
  measured work budget. That assertion is the point: the day cap and the query
  budget are separate numbers, and raising either has to be justified against the
  measurement instead of silently multiplying what one request can spend.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE_NAME", "IDP-TrackingTable")


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "list_documents_range_index_cap", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["list_documents_range_index_cap"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()

_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(*, days=None, hours=None, start=_START, **extra_args):
    end = start + timedelta(days=days or 0, hours=hours or 0)
    args = {
        "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    args.update(extra_args)
    return {
        "info": {"fieldName": "listDocumentsByDateRange"},
        "arguments": args,
        "identity": {"claims": {"cognito:groups": ["Admin"], "email": "a@e.com"}},
    }


@pytest.fixture
def instrumented(monkeypatch):
    """Count shard reads and serve nothing, so every shard in range is walked."""
    reads = []
    monkeypatch.setattr(
        index, "_query_shard", lambda *a, **k: reads.append(a[1:3]) or []
    )
    monkeypatch.setattr(index, "_batch_get_documents", lambda *a, **k: {})
    monkeypatch.setattr(index, "dynamodb", MagicMock())
    monkeypatch.setenv("TRACKING_TABLE_NAME", "IDP-TrackingTable")
    return reads


@pytest.mark.unit
class TestTheCapsPremise:
    """The derivation, not the code path: does MAX_RANGE_DAYS still fit?"""

    def test_the_day_cap_still_fits_the_measured_query_budget(self):
        # Worst case for a span of N days: the start is floored to a shard
        # boundary, adding one shard on top of N * SHARDS_PER_DAY.
        worst_case = index.MAX_RANGE_DAYS * index.SHARDS_PER_DAY + 1

        assert worst_case <= index.MAX_SHARD_QUERIES_PER_REQUEST, (
            f"{index.MAX_RANGE_DAYS} days x {index.SHARDS_PER_DAY} shards/day = "
            f"{worst_case} sequential DynamoDB Queries, over the "
            f"{index.MAX_SHARD_QUERIES_PER_REQUEST}-query budget derived from the "
            "dispatcher's 20s read timeout. Re-measure the per-query cost before "
            "raising either number."
        )

    def test_the_worst_case_is_really_the_worst_case(self):
        """The bound above has to hold for the least convenient start time."""
        worst_start = _START.replace(
            hour=index.HOURS_PER_SHARD - 1, minute=59, second=59
        )
        end = worst_start + timedelta(days=index.MAX_RANGE_DAYS)

        projected = index._projected_shard_queries(worst_start, end)

        assert projected == index.MAX_RANGE_DAYS * index.SHARDS_PER_DAY + 1

    def test_the_functions_timeout_leaves_room_for_the_budget(self):
        """The template's Timeout has to sit above the self-imposed stop.

        A Timeout below the shard budget would make the budget unreachable and
        bring back the unlabelled failure it exists to prevent; one far above it
        buys orphaned work after the dispatcher has stopped listening.
        """
        template = (Path(__file__).resolve().parents[3] / "template.yaml").read_text()
        block = template.split("ListDocumentsByDateRangeResolverFunction:", 1)[1]
        block = block.split("LogGroup:\n    Type: AWS::Logs::LogGroup", 1)[0]
        timeout = int(
            next(
                line.split("Timeout:")[1]
                for line in block.splitlines()
                if line.strip().startswith("Timeout:")
            )
        )

        assert index._SHARD_LOOP_BUDGET_SECONDS < timeout <= 60, (
            f"Timeout {timeout}s vs a {index._SHARD_LOOP_BUDGET_SECONDS}s shard "
            "budget: the function must outlive its own stop, and must not outlive "
            "the dispatcher's 20s window by enough to matter"
        )

    @pytest.mark.parametrize("hours", [0, 1, 3, 4, 5, 23, 24, 25, 100, 1000])
    def test_the_o1_projection_matches_what_is_actually_iterated(self, hours):
        """The bound is enforced on a count the generator never produces."""
        end = _START + timedelta(hours=hours)

        assert index._projected_shard_queries(_START, end) == len(
            index._shard_pks_for_range(_START, end)
        )


@pytest.mark.unit
class TestAnOverlongRangeIsRefused:
    def test_a_ten_year_range_is_refused(self, instrumented):
        with pytest.raises(ValueError) as excinfo:
            index.handler(_event(days=3650), None)

        assert "maximum is 365 days" in str(excinfo.value)

    def test_the_refusal_names_the_maximum_and_what_was_asked_for(self, instrumented):
        with pytest.raises(ValueError) as excinfo:
            index.handler(_event(days=3650), None)

        message = str(excinfo.value)
        assert "3650 days requested" in message
        assert f"maximum is {index.MAX_RANGE_DAYS} days" in message

    def test_the_refusal_precedes_every_shard_read(self, instrumented):
        with pytest.raises(ValueError):
            index.handler(_event(days=3650), None)

        assert instrumented == [], (
            "the range was refused but partitions were queried anyway — the "
            "refusal has to be free, or it is not a denial-of-service control"
        )

    def test_the_refusal_precedes_generating_the_partition_list(self, monkeypatch):
        """A range spanning centuries must not first materialize its shard list.

        21.9 million tuples does not fit the function's 512 MB, so generating
        before checking turns an over-long range into a MemoryError 500 instead of
        a legible 400.
        """
        monkeypatch.setattr(
            index,
            "_shard_pks_for_range",
            lambda *a, **k: pytest.fail("generated the partition list first"),
        )
        monkeypatch.setattr(index, "dynamodb", MagicMock())

        with pytest.raises(ValueError, match="too large"):
            index.handler(_event(days=365_000), None)

    def test_the_refusal_is_a_valueerror_so_the_dispatcher_reports_400(
        self, instrumented
    ):
        """Not a bare Exception: http_api_dispatcher maps errorType ValueError to
        400/BadRequest and everything else to 500/InternalError."""
        with pytest.raises(ValueError):
            index.handler(_event(days=3650), None)


@pytest.mark.unit
class TestTheBoundaryMatchesTheClient:
    """DateRangeModal.tsx refuses `end - start > 365 days`; so must this."""

    def test_exactly_the_maximum_is_accepted(self, instrumented):
        result = index.handler(_event(days=index.MAX_RANGE_DAYS), None)

        assert result["Documents"] == []
        assert len(instrumented) == index.MAX_RANGE_DAYS * index.SHARDS_PER_DAY + 1

    def test_one_second_over_the_maximum_is_refused(self, instrumented):
        event = _event(days=index.MAX_RANGE_DAYS)
        end = _START + timedelta(days=index.MAX_RANGE_DAYS, seconds=1)
        event["arguments"]["endDateTime"] = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        with pytest.raises(ValueError, match="too large"):
            index.handler(event, None)

    def test_a_typical_range_is_untouched(self, instrumented):
        """The longest window the document list's own presets offer."""
        result = index.handler(_event(days=30), None)

        assert result["Documents"] == []
        assert len(instrumented) == 181


class _Context:
    """A Lambda context double with a controllable remaining-time clock."""

    def __init__(self, remaining_ms):
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self._remaining_ms


@pytest.mark.unit
class TestTheWorkBudgetStopsAnInvocationThatIsStillInsideTheCap:
    """The cap bounds the worst case; this bounds the actual one.

    A cap derived from a measured per-query cost is only as good as the
    measurement, so an in-cap range on a throttled or non-empty table must still
    answer inside the dispatcher's window instead of running to the Lambda Timeout.
    """

    def test_a_shrinking_invocation_clock_stops_the_walk(self, instrumented):
        result = index.handler(
            _event(days=200), _Context(index._SHARD_LOOP_TIME_RESERVE_MS - 1)
        )

        assert instrumented == [], "stopped, but only after querying"
        assert result["nextToken"], "stopped early without a way to resume"

    def test_the_query_budget_stops_the_walk(self, instrumented, monkeypatch):
        monkeypatch.setattr(index, "MAX_SHARD_QUERIES_PER_REQUEST", 5)

        result = index.handler(_event(days=200), None)

        assert len(instrumented) == 5
        assert result["nextToken"], "stopped early without a way to resume"

    def test_the_wall_clock_budget_stops_the_walk(self, instrumented, monkeypatch):
        monkeypatch.setattr(index, "_SHARD_LOOP_BUDGET_SECONDS", 0.0)

        result = index.handler(_event(days=200), None)

        assert instrumented == []
        assert result["nextToken"]

    def test_the_resume_token_names_the_first_unread_shard(
        self, instrumented, monkeypatch
    ):
        monkeypatch.setattr(index, "MAX_SHARD_QUERIES_PER_REQUEST", 7)

        result = index.handler(_event(days=200), None)

        assert index._deserialize_next_token(result["nextToken"]) == (7, 0)

    def test_an_in_budget_range_carries_no_token(self, instrumented):
        result = index.handler(_event(days=1), _Context(60_000))

        assert result["nextToken"] is None


@pytest.mark.unit
class TestPaginationDoesNotDropTheRestOfTheRange:
    def test_a_page_that_exactly_fills_still_returns_a_token(self, monkeypatch):
        """The `else` branch advances the index and the `while` goes false without
        the mid-shard `break` that sets a token, so an exactly-`limit`-sized page
        used to make every later shard unreachable."""
        entries = [{"PK": "p", "SK": "s", "ObjectKey": f"{i}.pdf"} for i in range(5)]
        monkeypatch.setattr(
            index, "_query_shard", lambda *a, **k: entries if a[2] == 0 else []
        )
        monkeypatch.setattr(
            index,
            "_batch_get_documents",
            lambda t, keys: {k: {"ObjectKey": k} for k in keys},
        )
        monkeypatch.setattr(index, "dynamodb", MagicMock())

        result = index.handler(_event(days=2, limit=5), None)

        assert len(result["Documents"]) == 5
        assert result["nextToken"], (
            "the page filled exactly and the remaining shards became unreachable"
        )
        assert index._deserialize_next_token(result["nextToken"]) == (1, 0)


@pytest.mark.unit
class TestTheLimitArgumentIsClamped:
    def test_an_oversized_limit_cannot_raise_the_page_size(self, monkeypatch):
        entries = [{"PK": "p", "SK": "s", "ObjectKey": f"{i}.pdf"} for i in range(500)]
        monkeypatch.setattr(
            index, "_query_shard", lambda *a, **k: entries if a[2] == 0 else []
        )
        monkeypatch.setattr(
            index,
            "_batch_get_documents",
            lambda t, keys: {k: {"ObjectKey": k} for k in keys},
        )
        monkeypatch.setattr(index, "dynamodb", MagicMock())

        result = index.handler(_event(hours=1, limit=10_000), None)

        assert len(result["Documents"]) == index.MAX_PAGE_SIZE

    def test_a_negative_limit_does_not_silently_empty_the_page(self, monkeypatch):
        """`len(collected) < -1` is false on entry, so the loop never ran and an
        empty page was indistinguishable from an empty range."""
        entries = [{"PK": "p", "SK": "s", "ObjectKey": "a.pdf"}]
        monkeypatch.setattr(
            index, "_query_shard", lambda *a, **k: entries if a[2] == 0 else []
        )
        monkeypatch.setattr(
            index,
            "_batch_get_documents",
            lambda t, keys: {k: {"ObjectKey": k} for k in keys},
        )
        monkeypatch.setattr(index, "dynamodb", MagicMock())

        result = index.handler(_event(hours=1, limit=-1), None)

        assert [d["ObjectKey"] for d in result["Documents"]] == ["a.pdf"]
