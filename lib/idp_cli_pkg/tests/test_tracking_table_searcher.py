# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for `idp_cli.search_tracking_table`, the `idp-cli` helper that scans the IDP
tracking table for documents and reports how long they took.

The module has four responsibilities and the tests are grouped the same way. It
resolves the tracking table's name out of a deployed stack through `idp_sdk`'s
`IDPClient` and builds a DynamoDB client for it; it scans that table for items whose
`PK` contains a substring and whose `ObjectStatus` matches exactly, paginating until
DynamoDB stops handing back a `LastEvaluatedKey`; it turns the returned items into
timing statistics over three different timestamp pairs plus per-stage Lambda metering;
and it renders both the item list and the statistics as Rich tables.

Three things shaped how these tests are written.

The scan is backed by **moto** rather than by a mock, and the table is a real one with
real items in it, because the filter expression is the interesting part of the call and
a `MagicMock` accepts a filter expression that matches nothing. The `api_calls` fixture
from `conftest.py` then reads the submitted `Scan` parameters back off botocore, which
is the only place the *exact* expression and attribute values are visible.

Pagination is tested twice, deliberately. `test_the_scan_loop_returns_every_page`
seeds more than DynamoDB's 1 MB per-page budget so that moto really does split the
result and the loop really does run twice, and
`test_the_last_evaluated_key_is_fed_back_as_the_exclusive_start_key` drives the two
pages explicitly so that the `ExclusiveStartKey` wiring is pinned independently of
moto's paging arithmetic. A loop that returns only the first page is a defect that no
single-page fixture can see, and it is the defect this module is most likely to grow.

The timing fixtures use timestamps whose queue, processing and total durations are all
different from each other and all different across documents — nine distinct numbers
for three documents. A fixture where, say, a document's queue time equals its
processing time cannot tell the three buckets apart, so it would pass even if the
module subtracted the wrong pair of timestamps.

`search_tracking_table` has its own module-level Rich `console`, which the autouse
`unstyled_cli_console` fixture in `conftest.py` does not cover (that one pins
`idp_cli.cli`). The `out` fixture below pins this module's console to an unstyled
200-column console writing into a buffer, so the rendering assertions are about
content and do not break under `FORCE_COLOR` or in an 80-column terminal.
"""

import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from moto import mock_aws
from rich.console import Console

from idp_cli import search_tracking_table as mod

TABLE = "idp-test-TrackingTable"


# --------------------------------------------------------------------------------------
# fixtures and helpers
# --------------------------------------------------------------------------------------


@pytest.fixture
def out(monkeypatch):
    """Pin this module's own Rich console to an unstyled 200-column buffer.

    Returns the buffer; read the rendered text with `out.getvalue()`. Without this the
    assertions in the rendering tests depend on `FORCE_COLOR` and on the terminal
    width, which is exactly the sensitivity `conftest.py`'s `unstyled_cli_console`
    exists to remove for `idp_cli.cli`.
    """
    buffer = io.StringIO()
    monkeypatch.setattr(
        mod, "console", Console(file=buffer, width=200, force_terminal=False)
    )
    return buffer


def flat(rendered: str) -> str:
    """Collapse runs of whitespace, so an assertion survives Rich's line wrapping.

    A Rich table's title is wrapped to the width of the table rather than to the
    console width, and the tables here are as narrow as their content, so a title like
    "Matching Documents (showing first 50)" arrives split across two lines. Collapsing
    whitespace keeps the assertion about the words without pinning where the break
    lands.
    """
    return " ".join(rendered.split())


def build_searcher(
    monkeypatch,
    table_name=TABLE,
    stack_name="idp-test",
    region="us-east-1",
):
    """Construct a `TrackingTableSearcher` with only the stack lookup faked out.

    `IDPClient` is the one thing that would reach CloudFormation, SSM and STS to
    resolve a deployed stack, so it is replaced; everything else — the boto3 session,
    the botocore `Config`, the DynamoDB client — is real. Returns the searcher and the
    `IDPClient` stand-in so a caller can assert how it was constructed.
    """
    resources = SimpleNamespace(documents_table=table_name)
    client = MagicMock()
    client.stack.get_resources.return_value = resources
    idp_client_class = MagicMock(return_value=client)
    monkeypatch.setattr(mod, "IDPClient", idp_client_class)
    searcher = mod.TrackingTableSearcher(stack_name, region=region)
    return searcher, idp_client_class


def create_table(searcher, name=TABLE):
    """Create a tracking-table-shaped DynamoDB table (PK hash, SK range)."""
    searcher.dynamodb.create_table(
        TableName=name,
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


def item(pk, object_key, status="COMPLETED", sk="none", **extra):
    """One tracking-table item in DynamoDB wire format."""
    record = {
        "PK": {"S": pk},
        "SK": {"S": sk},
        "ObjectKey": {"S": object_key},
        "ObjectStatus": {"S": status},
    }
    record.update(extra)
    return record


#: Three documents whose queue, processing and total durations are all distinct, both
#: within a document and across documents, so that a bucket mix-up cannot pass.
#:
#:   doc-a: queue  10s, processing  30s, total  40s
#:   doc-b: queue  20s, processing  50s, total  70s
#:   doc-c: queue   5s, processing 100s, total 105s
TIMED_ITEMS = [
    item(
        "doc#a",
        "doc-a",
        QueuedTime={"S": "2025-01-01T00:00:00"},
        WorkflowStartTime={"S": "2025-01-01T00:00:10"},
        CompletionTime={"S": "2025-01-01T00:00:40"},
    ),
    item(
        "doc#b",
        "doc-b",
        QueuedTime={"S": "2025-01-01T01:00:00"},
        WorkflowStartTime={"S": "2025-01-01T01:00:20"},
        CompletionTime={"S": "2025-01-01T01:01:10"},
    ),
    item(
        "doc#c",
        "doc-c",
        QueuedTime={"S": "2025-01-01T02:00:00"},
        WorkflowStartTime={"S": "2025-01-01T02:00:05"},
        CompletionTime={"S": "2025-01-01T02:01:45"},
    ),
]


def results(items, count=None, pk="doc", object_status="COMPLETED"):
    """A `search_by_pk_and_status`-shaped success payload."""
    return {
        "success": True,
        "count": count if count is not None else len(items),
        "items": items,
        "pk": pk,
        "object_status": object_status,
    }


def bucket(average, median, minimum, min_key, maximum, max_key, stdev, total):
    """One `calc_stats`-shaped statistics bucket, written out for renderer tests.

    The renderers are pure functions of this dict, so the display tests build it
    literally rather than by running the calculator. That keeps a rendering failure
    from being blamed on the arithmetic and lets a test pick durations that land
    squarely inside a chosen `format_duration` band.
    """
    return {
        "average": average,
        "median": median,
        "min": minimum,
        "min_key": min_key,
        "max": maximum,
        "max_key": max_key,
        "stdev": stdev,
        "total": total,
    }


# --------------------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------------------


class TestConstruction:
    """`__init__` resolves the table name and builds the DynamoDB client."""

    def test_the_table_name_comes_from_the_stacks_documents_table(self, monkeypatch):
        with mock_aws():
            searcher, idp_client_class = build_searcher(
                monkeypatch, table_name="resolved-table", region="us-west-2"
            )

        assert searcher.table_name == "resolved-table"
        assert searcher.stack_name == "idp-test"
        assert searcher.region == "us-west-2"
        idp_client_class.assert_called_once_with(
            stack_name="idp-test", region="us-west-2"
        )

    def test_the_dynamodb_client_uses_the_requested_region_and_a_wide_pool(
        self, monkeypatch
    ):
        """The 50-connection pool is the reason this module builds its own client.

        The searcher scans and then fans out over the results, so a client left on
        botocore's default pool of 10 would serialise behind it. If this assertion
        fails the module still works and just runs slower under concurrency, which is
        precisely the kind of regression nothing else here would notice.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch, region="eu-west-2")

        assert searcher.dynamodb.meta.region_name == "eu-west-2"
        assert searcher.dynamodb.meta.config.max_pool_connections == 50

    def test_no_region_leaves_the_session_to_resolve_one_from_the_environment(
        self, monkeypatch
    ):
        with mock_aws():
            searcher, idp_client_class = build_searcher(monkeypatch, region=None)

        assert searcher.region is None
        idp_client_class.assert_called_once_with(stack_name="idp-test", region=None)
        # conftest pins AWS_DEFAULT_REGION, so the session resolves that.
        assert searcher.dynamodb.meta.region_name == "us-east-1"


# --------------------------------------------------------------------------------------
# search_by_pk_and_status
# --------------------------------------------------------------------------------------


class TestSearch:
    """`search_by_pk_and_status` scans the real table through moto."""

    def test_the_scan_matches_on_a_pk_substring_and_an_exact_status(
        self, monkeypatch, out, api_calls
    ):
        """Both halves of the filter have to bite, and the assertion proves each does.

        The seeded table holds one item that matches both conditions, one that matches
        the PK substring but carries a different status, and one with the wanted status
        under a PK the substring does not appear in. Only the first may come back.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)
            create_table(searcher)
            searcher.dynamodb.put_item(
                TableName=TABLE, Item=item("batch#42#a", "wanted.pdf")
            )
            searcher.dynamodb.put_item(
                TableName=TABLE,
                Item=item("batch#42#b", "wrong-status.pdf", status="FAILED"),
            )
            searcher.dynamodb.put_item(
                TableName=TABLE, Item=item("batch#99#c", "wrong-batch.pdf")
            )

            result = searcher.search_by_pk_and_status("batch#42", "COMPLETED")

        assert result["success"] is True
        assert result["count"] == 1
        assert result["pk"] == "batch#42"
        assert result["object_status"] == "COMPLETED"
        assert [i["ObjectKey"]["S"] for i in result["items"]] == ["wanted.pdf"]

        call = api_calls.only("Scan")
        assert call.params["TableName"] == TABLE
        assert (
            call.params["FilterExpression"]
            == "contains(PK, :pk) AND ObjectStatus = :status"
        )
        assert call.params["ExpressionAttributeValues"] == {
            ":pk": {"S": "batch#42"},
            ":status": {"S": "COMPLETED"},
        }
        assert "ExclusiveStartKey" not in call.params

        rendered = out.getvalue()
        assert "Searching tracking table for PK containing 'batch#42'" in rendered
        assert "ObjectStatus='COMPLETED'" in rendered
        assert "Found 1 matching documents" in rendered

    def test_a_search_that_matches_nothing_succeeds_with_a_count_of_zero(
        self, monkeypatch, out
    ):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)
            create_table(searcher)

            result = searcher.search_by_pk_and_status("absent", "COMPLETED")

        assert result == {
            "success": True,
            "count": 0,
            "items": [],
            "pk": "absent",
            "object_status": "COMPLETED",
        }
        assert "Found 0 matching documents" in out.getvalue()

    def test_the_scan_loop_returns_every_page(self, monkeypatch, out, api_calls):
        """Seed past DynamoDB's 1 MB page budget so the loop genuinely runs twice.

        Each item carries ~60 KB of padding, so twenty of them cannot be returned in
        one page and moto hands back a `LastEvaluatedKey`. A loop that returned only
        the first page would report roughly sixteen documents here and would look
        entirely correct on any smaller fixture. If this test starts failing with
        exactly one `Scan`, check the padding first: the claim it makes is only as good
        as the response really exceeding a page.
        """
        padding = "x" * 60_000
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)
            create_table(searcher)
            for index in range(20):
                searcher.dynamodb.put_item(
                    TableName=TABLE,
                    Item=item(
                        f"batch#7#{index:02d}",
                        f"page-{index:02d}.pdf",
                        Pad={"S": padding},
                    ),
                )

            result = searcher.search_by_pk_and_status("batch#7", "COMPLETED")

        scans = api_calls.of("Scan")
        assert len(scans) > 1, (
            "the fixture no longer exceeds one page, so this test would pass even for "
            f"a loop that never paginated (scans={len(scans)})"
        )
        assert "ExclusiveStartKey" not in scans[0].params
        assert set(scans[1].params["ExclusiveStartKey"]) == {"PK", "SK"}

        assert result["count"] == 20
        assert sorted(i["ObjectKey"]["S"] for i in result["items"]) == [
            f"page-{index:02d}.pdf" for index in range(20)
        ]
        assert "Found 20 matching documents" in out.getvalue()

    def test_the_last_evaluated_key_is_fed_back_as_the_exclusive_start_key(
        self, monkeypatch, out
    ):
        """Drive the two pages explicitly, so the wiring is pinned without moto.

        This is the same property as the test above, measured a different way: the
        stand-in records what it was called with, so the second call's
        `ExclusiveStartKey` can be compared to the first response's
        `LastEvaluatedKey` byte for byte, and the third call must not happen at all.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        cursor = {"PK": {"S": "batch#1#a"}, "SK": {"S": "none"}}
        pages = [
            {"Items": [item("batch#1#a", "first.pdf")], "LastEvaluatedKey": cursor},
            {"Items": [item("batch#1#b", "second.pdf")]},
        ]
        seen = []

        def fake_scan(**kwargs):
            seen.append(dict(kwargs))
            return pages[len(seen) - 1]

        monkeypatch.setattr(searcher.dynamodb, "scan", fake_scan)
        result = searcher.search_by_pk_and_status("batch#1", "COMPLETED")

        assert len(seen) == 2
        assert "ExclusiveStartKey" not in seen[0]
        assert seen[1]["ExclusiveStartKey"] == cursor
        assert result["count"] == 2
        assert [i["ObjectKey"]["S"] for i in result["items"]] == [
            "first.pdf",
            "second.pdf",
        ]
        assert "Found 2 matching documents" in out.getvalue()

    def test_a_missing_documents_table_refuses_without_scanning(
        self, monkeypatch, out, api_calls
    ):
        """The refusal has to happen before any API call, not as a failed scan.

        A stack that has no `DocumentsTable` output is a deployment that predates the
        tracking table or a name typo, and either way scanning `None` would surface as
        a botocore parameter-validation error rather than as the message the user can
        act on.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch, table_name=None)
            result = searcher.search_by_pk_and_status("batch#1", "COMPLETED")

        assert result == {
            "success": False,
            "error": "DocumentsTable not found in stack resources",
        }
        assert api_calls.of("Scan") == []
        assert out.getvalue() == ""

    def test_an_empty_documents_table_name_refuses_the_same_way(self, monkeypatch, out):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch, table_name="")
            result = searcher.search_by_pk_and_status("batch#1", "COMPLETED")

        assert result["success"] is False
        assert result["error"] == "DocumentsTable not found in stack resources"
        assert out.getvalue() == ""

    def test_a_failing_scan_is_reported_rather_than_raised(
        self, monkeypatch, out, api_calls
    ):
        """A table that does not exist is the realistic shape of this failure.

        The searcher resolves the name from stack outputs, so a stale or
        wrong-region stack points it at a table that is not there. The method must
        return the failure payload — the CLI renders it — rather than let the
        `ResourceNotFoundException` escape.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch, table_name="never-created")
            result = searcher.search_by_pk_and_status("batch#1", "COMPLETED")

        assert result["success"] is False
        assert "not found" in result["error"].lower()
        assert set(result) == {"success", "error"}
        assert len(api_calls.of("Scan")) == 1
        # The progress line is printed before the scan is attempted.
        assert "Searching tracking table" in out.getvalue()
        assert "Found" not in out.getvalue()

    def test_search_and_statistics_compose_end_to_end(self, monkeypatch, out):
        """One pass through the module as the CLI uses it: scan, then summarise."""
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)
            create_table(searcher)
            for record in TIMED_ITEMS:
                searcher.dynamodb.put_item(TableName=TABLE, Item=record)

            found = searcher.search_by_pk_and_status("doc#", "COMPLETED")
            stats = searcher.calculate_timing_statistics(found)
            searcher.display_results(found, show_details=True)
            searcher.display_timing_statistics(stats)

        assert found["count"] == 3
        assert stats["valid_count"] == 3
        assert stats["processing_time"]["total"] == pytest.approx(180.0)
        rendered = out.getvalue()
        assert "doc-a" in rendered and "doc-c" in rendered
        assert "Timing Statistics:" in rendered


# --------------------------------------------------------------------------------------
# display_results
# --------------------------------------------------------------------------------------


class TestDisplayResults:
    """`display_results` renders the summary and, optionally, the item table."""

    def test_a_failed_result_prints_the_error_and_builds_no_table(
        self, monkeypatch, out
    ):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        searcher.display_results(
            {"success": False, "error": "DocumentsTable not found in stack resources"},
            show_details=True,
        )

        rendered = out.getvalue()
        assert "Error: DocumentsTable not found in stack resources" in rendered
        assert "ObjectKey" not in rendered
        assert "Search Results:" not in rendered

    def test_the_summary_reports_the_query_and_the_count(self, monkeypatch, out):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        searcher.display_results(
            results([item("doc#a", "a.pdf")], pk="doc#", object_status="COMPLETED")
        )

        rendered = out.getvalue()
        assert "Search Results:" in rendered
        assert "PK: doc#" in rendered
        assert "ObjectStatus: COMPLETED" in rendered
        assert "Count: 1" in rendered
        # show_details defaults to False, so no per-item table is built.
        assert "Matching Documents" not in flat(rendered)
        assert "a.pdf" not in rendered

    def test_details_list_one_row_per_item(self, monkeypatch, out):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        searcher.display_results(
            results(
                [
                    item("doc#a", "alpha.pdf"),
                    item("doc#b", "beta.pdf", status="FAILED"),
                ]
            ),
            show_details=True,
        )

        rendered = out.getvalue()
        assert "Matching Documents (showing first 50)" in flat(rendered)
        for expected in ("alpha.pdf", "beta.pdf", "doc#a", "doc#b", "FAILED"):
            assert expected in rendered
        assert "Note: Showing first 50" not in rendered

    def test_an_item_missing_its_attributes_renders_as_not_available(
        self, monkeypatch, out
    ):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        searcher.display_results(
            results([{"SK": {"S": "none"}}]),
            show_details=True,
        )

        assert "N/A" in out.getvalue()

    def test_only_the_first_fifty_rows_are_rendered_and_the_rest_are_announced(
        self, monkeypatch, out
    ):
        """Fifty-five items: fifty rows, and a note naming the real total.

        The table is titled "showing first 50" unconditionally, so the only way to
        tell a truncating renderer from one that silently drops rows is to count the
        keys that actually made it into the output and to check the note.
        """
        keys = [f"file-{index:03d}.pdf" for index in range(55)]
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        searcher.display_results(
            results(
                [item(f"doc#{index:03d}", key) for index, key in enumerate(keys)],
            ),
            show_details=True,
        )

        rendered = out.getvalue()
        present = [key for key in keys if key in rendered]
        assert len(present) == 50
        assert present == keys[:50]
        assert "Note: Showing first 50 of 55 results" in rendered

    def test_details_are_skipped_when_nothing_matched(self, monkeypatch, out):
        """`show_details` with a zero count prints the summary and no empty table."""
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        searcher.display_results(results([]), show_details=True)

        rendered = out.getvalue()
        assert "Count: 0" in rendered
        assert "Matching Documents" not in flat(rendered)


# --------------------------------------------------------------------------------------
# calculate_timing_statistics
# --------------------------------------------------------------------------------------


class TestTimingStatistics:
    """`calculate_timing_statistics` is where the module's real logic lives."""

    def test_nothing_to_analyse_is_refused(self, monkeypatch):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        for payload in (
            {"success": False, "error": "boom"},
            results([], count=0),
            {},
        ):
            assert searcher.calculate_timing_statistics(payload) == {
                "success": False,
                "error": "No results to analyze",
            }

    def test_the_three_durations_come_from_the_three_right_timestamp_pairs(
        self, monkeypatch
    ):
        """Processing, queue and total are three different subtractions.

        The fixture's nine durations are all distinct, so a bucket built from the
        wrong pair of timestamps cannot land on the expected numbers by coincidence.
        Processing is `WorkflowStartTime → CompletionTime` (30, 50, 100), queue is
        `QueuedTime → WorkflowStartTime` (10, 20, 5) and total is
        `QueuedTime → CompletionTime` (40, 70, 105).
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results(TIMED_ITEMS), include_metering=False
        )

        assert stats["success"] is True
        assert stats["valid_count"] == 3
        assert stats["missing_data_count"] == 0

        processing = stats["processing_time"]
        assert processing["average"] == pytest.approx(60.0)
        assert processing["median"] == pytest.approx(50.0)
        assert processing["min"] == pytest.approx(30.0)
        assert processing["max"] == pytest.approx(100.0)
        assert processing["total"] == pytest.approx(180.0)
        assert processing["stdev"] == pytest.approx(36.0555, rel=1e-4)

        queue = stats["queue_time"]
        assert queue["average"] == pytest.approx(35.0 / 3)
        assert queue["median"] == pytest.approx(10.0)
        assert queue["min"] == pytest.approx(5.0)
        assert queue["max"] == pytest.approx(20.0)
        assert queue["total"] == pytest.approx(35.0)
        assert queue["stdev"] == pytest.approx(7.6376, rel=1e-4)

        total = stats["total_time"]
        assert total["average"] == pytest.approx(215.0 / 3)
        assert total["median"] == pytest.approx(70.0)
        assert total["min"] == pytest.approx(40.0)
        assert total["max"] == pytest.approx(105.0)
        assert total["total"] == pytest.approx(215.0)

        assert "metering" not in stats
        assert "metering_count" not in stats

    def test_the_extremes_carry_the_object_key_of_the_document_they_came_from(
        self, monkeypatch
    ):
        """`min_key`/`max_key` are what make the statistics actionable.

        Each bucket's extremes belong to a different document here — the fastest to
        process was the one that waited the second-longest in the queue — so a
        renderer that attached one bucket's keys to another bucket's numbers, or that
        reported the first item's key for every extreme, would fail.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results(TIMED_ITEMS), include_metering=False
        )

        assert stats["processing_time"]["min_key"] == "doc-a"
        assert stats["processing_time"]["max_key"] == "doc-c"
        assert stats["queue_time"]["min_key"] == "doc-c"
        assert stats["queue_time"]["max_key"] == "doc-b"
        assert stats["total_time"]["min_key"] == "doc-a"
        assert stats["total_time"]["max_key"] == "doc-c"

    def test_a_single_document_reports_a_zero_standard_deviation(self, monkeypatch):
        """`statistics.stdev` raises on one sample, so the guard has to hold.

        A one-document search is the common case when someone is chasing a single
        slow file, and an unguarded `stdev` would turn that into a traceback.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results([TIMED_ITEMS[0]]), include_metering=False
        )

        assert stats["valid_count"] == 1
        assert stats["processing_time"]["stdev"] == 0
        assert stats["queue_time"]["stdev"] == 0
        assert stats["total_time"]["stdev"] == 0
        assert (
            stats["processing_time"]["min"] == stats["processing_time"]["max"] == 30.0
        )

    def test_an_item_without_a_completion_time_counts_as_missing(self, monkeypatch):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results(
                [
                    TIMED_ITEMS[0],
                    item(
                        "doc#x",
                        "still-running.pdf",
                        QueuedTime={"S": "2025-01-01T03:00:00"},
                        WorkflowStartTime={"S": "2025-01-01T03:00:01"},
                    ),
                ]
            ),
            include_metering=False,
        )

        assert stats["valid_count"] == 1
        assert stats["missing_data_count"] == 1
        assert stats["processing_time"]["total"] == pytest.approx(30.0)
        assert stats["processing_time"]["max_key"] == "doc-a"

    def test_an_unparseable_timestamp_counts_as_missing(self, monkeypatch):
        """The `except` branch is the only thing standing between a bad row and a crash.

        `datetime.fromisoformat` raises on anything it does not recognise, and the
        tracking table is written by several Lambdas, so one stage writing a
        non-ISO timestamp must degrade to a missing-data count rather than abort the
        whole summary.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results(
                [
                    TIMED_ITEMS[0],
                    item(
                        "doc#y",
                        "corrupt.pdf",
                        QueuedTime={"S": "2025-01-01T04:00:00"},
                        WorkflowStartTime={"S": "not a timestamp"},
                        CompletionTime={"S": "2025-01-01T04:00:30"},
                    ),
                ]
            ),
            include_metering=False,
        )

        assert stats["success"] is True
        assert stats["valid_count"] == 1
        assert stats["missing_data_count"] == 1

    def test_no_usable_timestamps_at_all_reports_the_missing_count(self, monkeypatch):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results(
                [
                    item("doc#p", "queued-only.pdf"),
                    item("doc#q", "also-queued.pdf"),
                ]
            )
        )

        assert stats == {
            "success": False,
            "error": (
                "No valid timing data found. 2 items missing required timestamps."
            ),
        }

    def test_a_document_that_never_queued_yields_processing_time_only(
        self, monkeypatch
    ):
        """Without a `QueuedTime` there is no queue or total duration to report.

        Both optional buckets have to be absent rather than present-and-empty,
        because the renderer keys off `in stats` and a zero-sample bucket would make
        `calc_stats` raise on `min()` of an empty list.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results(
                [
                    item(
                        "doc#z",
                        "no-queue.pdf",
                        WorkflowStartTime={"S": "2025-01-01T05:00:00"},
                        CompletionTime={"S": "2025-01-01T05:00:45"},
                    )
                ]
            ),
            include_metering=False,
        )

        assert stats["valid_count"] == 1
        assert stats["processing_time"]["total"] == pytest.approx(45.0)
        assert "queue_time" not in stats
        assert "total_time" not in stats


class TestMetering:
    """Per-stage Lambda metering, which arrives in two different storage shapes."""

    @staticmethod
    def _timed(object_key, pk, metering=None):
        extra = {} if metering is None else {"Metering": metering}
        return item(
            pk,
            object_key,
            QueuedTime={"S": "2025-01-01T00:00:00"},
            WorkflowStartTime={"S": "2025-01-01T00:00:10"},
            CompletionTime={"S": "2025-01-01T00:00:40"},
            **extra,
        )

    def test_metering_stored_as_a_json_string_is_parsed(self, monkeypatch):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        payload = json.dumps(
            {
                "OCR/lambda/duration": {"gb_seconds": 1.5},
                "Extraction/lambda/duration": {"gb_seconds": 4.0},
                "SomeOtherMetric/pages": {"value": 3},
            }
        )
        stats = searcher.calculate_timing_statistics(
            results([self._timed("json.pdf", "doc#j", {"S": payload})])
        )

        assert stats["metering_count"] == 1
        assert set(stats["metering"]) == {"OCR", "Extraction"}
        assert stats["metering"]["OCR"]["total"] == pytest.approx(1.5)
        assert stats["metering"]["OCR"]["min_key"] == "json.pdf"
        assert stats["metering"]["Extraction"]["total"] == pytest.approx(4.0)

    def test_metering_stored_as_a_native_dynamodb_map_is_parsed(self, monkeypatch):
        """The same numbers must come out of the wire-format map.

        DynamoDB items written by `boto3.resource` land as native maps and items
        written as a serialised blob land as strings, so both shapes are live in the
        same table and a reader that handles only one silently reports no metering
        for half the documents.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        native = {
            "M": {
                "Assessment/lambda/duration": {
                    "M": {
                        "gb_seconds": {"N": "2.5"},
                        "unit": {"S": "GB-seconds"},
                    }
                },
                "Classification/lambda/duration": {"M": {"gb_seconds": {"N": "0.75"}}},
            }
        }
        stats = searcher.calculate_timing_statistics(
            results([self._timed("native.pdf", "doc#n", native)])
        )

        assert stats["metering_count"] == 1
        assert set(stats["metering"]) == {"Assessment", "Classification"}
        assert stats["metering"]["Assessment"]["average"] == pytest.approx(2.5)
        assert stats["metering"]["Classification"]["average"] == pytest.approx(0.75)

    def test_malformed_metering_json_is_ignored_rather_than_raising(self, monkeypatch):
        """A truncated blob yields no metering and leaves the timings intact."""
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results([self._timed("bad.pdf", "doc#b", {"S": '{"OCR/lambda/dur'})])
        )

        assert stats["success"] is True
        assert stats["valid_count"] == 1
        assert stats["missing_data_count"] == 0
        assert "metering" not in stats

    def test_metering_in_an_unhandled_attribute_shape_is_ignored(self, monkeypatch):
        """Neither `S` nor `M` — e.g. a NULL attribute — parses to nothing."""
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results([self._timed("null.pdf", "doc#u", {"NULL": True})])
        )

        assert stats["success"] is True
        assert "metering" not in stats
        assert "metering_count" not in stats

    def test_a_zero_gb_seconds_reading_is_skipped(self, monkeypatch):
        """Zero means the stage did not run, so it must not drag an average down."""
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        payload = json.dumps(
            {
                "OCR/lambda/duration": {"gb_seconds": 0},
                "Summarization/lambda/duration": {"gb_seconds": 3.0},
            }
        )
        stats = searcher.calculate_timing_statistics(
            results([self._timed("zero.pdf", "doc#0", {"S": payload})])
        )

        assert set(stats["metering"]) == {"Summarization"}
        assert stats["metering"]["Summarization"]["total"] == pytest.approx(3.0)

    def test_a_stage_with_no_duration_key_contributes_nothing(self, monkeypatch):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        payload = json.dumps({"OCR/bedrock/input_tokens": {"value": 100}})
        stats = searcher.calculate_timing_statistics(
            results([self._timed("tokens.pdf", "doc#t", {"S": payload})])
        )

        assert "metering" not in stats
        assert "metering_count" not in stats

    def test_metering_extremes_name_the_cheapest_and_most_expensive_document(
        self, monkeypatch
    ):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results(
                [
                    self._timed(
                        "cheap.pdf",
                        "doc#c",
                        {"S": json.dumps({"OCR/lambda/duration": {"gb_seconds": 1.0}})},
                    ),
                    self._timed(
                        "dear.pdf",
                        "doc#d",
                        {"S": json.dumps({"OCR/lambda/duration": {"gb_seconds": 9.0}})},
                    ),
                ]
            )
        )

        ocr = stats["metering"]["OCR"]
        assert ocr["min_key"] == "cheap.pdf"
        assert ocr["max_key"] == "dear.pdf"
        assert ocr["average"] == pytest.approx(5.0)
        assert ocr["stdev"] == pytest.approx(5.6568, rel=1e-4)

    def test_include_metering_false_skips_parsing_entirely(self, monkeypatch):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results(
                [
                    self._timed(
                        "present.pdf",
                        "doc#p",
                        {"S": json.dumps({"OCR/lambda/duration": {"gb_seconds": 7.0}})},
                    )
                ]
            ),
            include_metering=False,
        )

        assert "metering" not in stats
        assert "metering_count" not in stats
        assert stats["valid_count"] == 1

    def test_metering_count_overcounts_documents_that_contributed_nothing(
        self, monkeypatch
    ):
        """DEFECT, pinned: `metering_count` counts the wrong thing.

        `search_tracking_table.py:254` increments `metering_count` for any item that
        carries a `Metering` attribute, as long as *some earlier* item has already put
        a reading into `metering_data` — the `any(metering_data.values())` test is
        about the accumulator, not about this item. So the second item here
        contributes no reading at all and is still counted.

        The observable consequence is the "Documents with metering" line in
        `display_timing_statistics`, which reports 2 while the only populated stage
        holds a single sample. It is a reporting error, not a corruption of the
        statistics: averages and totals stay correct because they are computed from
        the per-stage lists. The count is also order-dependent, which is why this test
        puts the contributing item first; swap the two and the count is right.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results(
                [
                    self._timed(
                        "contributes.pdf",
                        "doc#1",
                        {"S": json.dumps({"OCR/lambda/duration": {"gb_seconds": 1.5}})},
                    ),
                    self._timed(
                        "contributes-nothing.pdf",
                        "doc#2",
                        {"S": json.dumps({"OCR/bedrock/input_tokens": {"value": 10}})},
                    ),
                ]
            )
        )

        assert stats["metering_count"] == 2
        assert stats["metering"]["OCR"]["total"] == pytest.approx(1.5)
        assert stats["metering"]["OCR"]["stdev"] == 0  # one sample, not two

    def test_a_malformed_metering_reading_makes_one_document_both_valid_and_missing(
        self, monkeypatch
    ):
        """DEFECT, pinned: the two counters double-count the same document.

        If a `<Stage>/lambda/duration` entry is a bare number rather than an object,
        `search_tracking_table.py:248` calls `.get` on an `int` and raises
        `AttributeError`. That lands in the broad `except` at line 257, which
        increments `missing_data_count` — but `valid_count` was already incremented at
        line 221 for the same item, and its durations are already in the buckets.

        The observable consequence is that `display_timing_statistics` reports one
        valid document *and* one with missing data for a single document whose
        timestamps were fine, so the two numbers no longer add up to the search count.
        The timing figures themselves are unaffected; the metering reading is lost
        silently, and so is every later stage for that item.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        stats = searcher.calculate_timing_statistics(
            results(
                [
                    self._timed(
                        "scalar.pdf", "doc#s", {"S": '{"OCR/lambda/duration": 5}'}
                    )
                ]
            )
        )

        assert stats["valid_count"] == 1
        assert stats["missing_data_count"] == 1
        assert stats["processing_time"]["total"] == pytest.approx(30.0)
        assert "metering" not in stats


# --------------------------------------------------------------------------------------
# the two DynamoDB parsers
# --------------------------------------------------------------------------------------


class TestDynamoDbParsers:
    """`_parse_dynamodb_map` and `_parse_dynamodb_value` are near-duplicates.

    They differ in exactly one respect and it is worth stating: for a type descriptor
    neither recognises, `_parse_dynamodb_map` drops the key from its result entirely
    while `_parse_dynamodb_value` returns `None`. Inside a list that turns an
    unsupported element into a `None` hole that shifts nothing, and inside a map it
    makes the key vanish, so a consumer using `in` sees a different answer from one
    using `.get`. Both are reachable from real metering payloads, because neither
    function handles binary (`B`), string sets (`SS`) or number sets (`NS`).
    """

    @pytest.fixture
    def searcher(self, monkeypatch):
        with mock_aws():
            built, _ = build_searcher(monkeypatch)
        return built

    def test_parse_map_handles_every_supported_descriptor(self, searcher):
        parsed = searcher._parse_dynamodb_map(
            {
                "text": {"S": "hello"},
                "number": {"N": "42"},
                "nested": {"M": {"inner": {"S": "deep"}, "count": {"N": "2"}}},
                "list": {
                    "L": [
                        {"S": "a"},
                        {"N": "1.5"},
                        {"BOOL": False},
                        {"NULL": True},
                        {"M": {"k": {"S": "v"}}},
                        {"L": [{"S": "nested-list"}]},
                    ]
                },
                "flag": {"BOOL": True},
                "nothing": {"NULL": True},
            }
        )

        assert parsed == {
            "text": "hello",
            "number": 42.0,
            "nested": {"inner": "deep", "count": 2.0},
            "list": ["a", 1.5, False, None, {"k": "v"}, ["nested-list"]],
            "flag": True,
            "nothing": None,
        }
        # `N` is always widened to float, even for an integral value.
        assert isinstance(parsed["number"], float)

    def test_parse_map_silently_drops_a_descriptor_it_does_not_know(self, searcher):
        """An unsupported type is not an error and not a `None` — the key disappears.

        `B`, `SS` and `NS` are all legal DynamoDB types and none is handled, so a
        caller cannot distinguish "the attribute was absent" from "the attribute was a
        string set". Pinned because the omission is silent by construction.
        """
        parsed = searcher._parse_dynamodb_map(
            {
                "kept": {"S": "yes"},
                "binary": {"B": "Zm9v"},
                "string_set": {"SS": ["a", "b"]},
                "number_set": {"NS": ["1", "2"]},
            }
        )

        assert parsed == {"kept": "yes"}
        assert "binary" not in parsed
        assert "string_set" not in parsed

    def test_parse_map_of_nothing_is_an_empty_dict(self, searcher):
        assert searcher._parse_dynamodb_map({}) == {}

    def test_parse_value_handles_every_supported_descriptor(self, searcher):
        assert searcher._parse_dynamodb_value({"S": "text"}) == "text"
        assert searcher._parse_dynamodb_value({"N": "7"}) == 7.0
        assert isinstance(searcher._parse_dynamodb_value({"N": "7"}), float)
        assert searcher._parse_dynamodb_value({"M": {"a": {"N": "3"}}}) == {"a": 3.0}
        assert searcher._parse_dynamodb_value(
            {"L": [{"S": "x"}, {"L": [{"BOOL": True}]}]}
        ) == ["x", [True]]
        assert searcher._parse_dynamodb_value({"BOOL": False}) is False
        assert searcher._parse_dynamodb_value({"NULL": True}) is None

    def test_parse_value_returns_none_for_a_descriptor_it_does_not_know(self, searcher):
        """The fall-through that is the one difference between the two functions."""
        assert searcher._parse_dynamodb_value({"B": "Zm9v"}) is None
        assert searcher._parse_dynamodb_value({"SS": ["a"]}) is None
        assert searcher._parse_dynamodb_value({}) is None

    def test_the_two_functions_disagree_about_an_unknown_descriptor(self, searcher):
        """State the divergence as an assertion so a future unification notices it."""
        unknown = {"SS": ["a", "b"]}
        assert searcher._parse_dynamodb_value(unknown) is None
        assert searcher._parse_dynamodb_map({"key": unknown}) == {}


# --------------------------------------------------------------------------------------
# display_timing_statistics
# --------------------------------------------------------------------------------------


class TestDisplayTimingStatistics:
    """`display_timing_statistics` renders the three duration tables and metering."""

    def test_a_failed_stats_dict_prints_the_error_and_renders_nothing_else(
        self, monkeypatch, out
    ):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        searcher.display_timing_statistics(
            {"success": False, "error": "No results to analyze"}
        )

        rendered = out.getvalue()
        assert "Error: No results to analyze" in rendered
        assert "Timing Statistics:" not in rendered

    def test_each_duration_band_is_formatted_in_its_own_unit(self, monkeypatch, out):
        """`format_duration` has three branches and each gets a duration inside it.

        Under a minute it prints seconds, under an hour it prints minutes with the
        seconds in brackets, and above that hours with the seconds in brackets. The
        three buckets here are given 30 s, 90 s and 7200 s averages so that one
        render exercises all three, and each expected string is asserted literally —
        a unit swap or a lost decimal place is the failure this catches.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        searcher.display_timing_statistics(
            {
                "success": True,
                "valid_count": 3,
                "missing_data_count": 0,
                "processing_time": bucket(
                    30.0, 30.0, 12.5, "fast.pdf", 45.0, "slow.pdf", 8.0, 90.0
                ),
                "queue_time": bucket(
                    90.0, 90.0, 90.0, "a.pdf", 90.0, "a.pdf", 0, 270.0
                ),
                "total_time": bucket(
                    7200.0, 7200.0, 3600.0, "a.pdf", 10800.0, "b.pdf", 100.0, 21600.0
                ),
            }
        )

        rendered = out.getvalue()
        assert "Valid documents: 3" in rendered
        assert "Processing Time (WorkflowStartTime → CompletionTime):" in rendered
        assert "Queue Time (QueuedTime → WorkflowStartTime):" in rendered
        assert "Total Time (QueuedTime → CompletionTime):" in rendered
        # < 60s -> seconds
        assert "30.00s" in rendered
        assert "12.50s" in rendered
        # < 3600s -> minutes, seconds in brackets
        assert "1.50m (90.0s)" in rendered
        # >= 3600s -> hours, seconds in brackets
        assert "2.00h (7200.0s)" in rendered
        assert "1.00h (3600.0s)" in rendered
        assert "3.00h (10800.0s)" in rendered
        # min/max keys are attached to their own rows
        assert "fast.pdf" in rendered and "slow.pdf" in rendered

    def test_the_standard_deviation_row_is_omitted_when_it_is_zero(
        self, monkeypatch, out
    ):
        """One document has no spread, and a "Std Dev 0.00s" row would be noise.

        The decision is taken once per section, so the three buckets here are given a
        zero, a non-zero and a zero standard deviation respectively: exactly one
        "Std Dev" row may appear, in the queue table, and the processing and total
        tables must each be six rows rather than seven. A renderer that took the
        decision once for the whole report would put the row in all three or in none.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        searcher.display_timing_statistics(
            {
                "success": True,
                "valid_count": 1,
                "missing_data_count": 0,
                "processing_time": bucket(30.0, 30.0, 30.0, "a", 30.0, "a", 0, 30.0),
                "queue_time": bucket(10.0, 10.0, 10.0, "a", 10.0, "a", 4.25, 10.0),
                "total_time": bucket(40.0, 40.0, 40.0, "a", 40.0, "a", 0, 40.0),
            }
        )

        rendered = out.getvalue()
        assert rendered.count("Std Dev") == 1
        assert "4.25s" in rendered
        assert "Missing data" not in rendered
        assert "Total Time (QueuedTime → CompletionTime):" in rendered
        assert "40.00s" in rendered

    def test_missing_documents_are_announced_only_when_there_are_some(
        self, monkeypatch, out
    ):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        searcher.display_timing_statistics(
            {
                "success": True,
                "valid_count": 4,
                "missing_data_count": 2,
                "processing_time": bucket(30.0, 30.0, 30.0, "a", 30.0, "a", 0, 120.0),
            }
        )

        rendered = out.getvalue()
        assert "Valid documents: 4" in rendered
        assert "Missing data: 2" in rendered

    def test_the_metering_section_prints_per_stage_totals_and_a_cost_estimate(
        self, monkeypatch, out
    ):
        """The cost line is arithmetic on a published rate, so pin the exact figures.

        At $0.0000166667 per GB-second a 1200 GB-second total is $0.0200 and a 300
        GB-second average is $0.005000 per document, at the four and six decimal
        places the module formats them to. A rate typo or a per-stage/per-document
        mix-up changes those strings.
        """
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        searcher.display_timing_statistics(
            {
                "success": True,
                "valid_count": 4,
                "missing_data_count": 0,
                "metering_count": 4,
                "processing_time": bucket(30.0, 30.0, 30.0, "a", 30.0, "a", 0, 120.0),
                "metering": {
                    "OCR": bucket(
                        300.0,
                        280.0,
                        100.0,
                        "cheap.pdf",
                        500.0,
                        "dear.pdf",
                        45.5,
                        1200.0,
                    ),
                    "Extraction": bucket(
                        10.0, 10.0, 10.0, "only.pdf", 10.0, "only.pdf", 0, 40.0
                    ),
                },
            }
        )

        rendered = out.getvalue()
        assert "Lambda Metering (GB-seconds by Stage):" in rendered
        assert "Documents with metering: 4" in rendered
        assert "OCR:" in rendered
        assert "Extraction:" in rendered
        assert "300.00" in rendered and "1200.00" in rendered
        assert "cheap.pdf" in rendered and "dear.pdf" in rendered
        assert "Estimated cost: $0.0200 total, $0.005000 avg per document" in rendered
        # Extraction's single sample: 40 GB-s total, 10 GB-s average.
        assert "Estimated cost: $0.0007 total, $0.000167 avg per document" in rendered
        # Std Dev appears for OCR (45.5) and not for Extraction (0).
        assert rendered.count("Std Dev") == 1
        assert "45.50" in rendered

    def test_an_empty_metering_map_renders_no_metering_section(self, monkeypatch, out):
        with mock_aws():
            searcher, _ = build_searcher(monkeypatch)

        searcher.display_timing_statistics(
            {
                "success": True,
                "valid_count": 1,
                "missing_data_count": 0,
                "metering": {},
                "processing_time": bucket(30.0, 30.0, 30.0, "a", 30.0, "a", 0, 30.0),
            }
        )

        assert "Lambda Metering" not in out.getvalue()
