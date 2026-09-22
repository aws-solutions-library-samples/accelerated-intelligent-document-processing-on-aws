# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `idp_common.delete_documents`, the deletion path behind
`idp-cli delete` and the UI's delete action.

**This module destroys customer data, so its failure modes are asymmetric.** Deleting
too little leaves orphaned rows and S3 bytes that cost money and confuse the next
listing — recoverable, and visible. Deleting too much is unrecoverable. Both directions
are therefore asserted, and where a selector is broader than its documentation says, the
test pins the actual breadth rather than the intended one.

Four things shape these tests.

**The list-entry cleanup has three fallback strategies and they are asserted
separately.** `delete_list_entries_robust` tries an exact shard+timestamp delete, then a
filtered query of the computed shard, then the two adjacent shards. Each exists because
the one before it misses a real case — a list entry can sit in a neighbouring shard when
the queued time and the list write straddle a 4-hour boundary. A test that only covers
the happy path passes with all three collapsed into one, and the symptom of losing the
fallbacks is an orphaned row in a list nobody notices.

**The output-bucket purge is version-aware on purpose.** That bucket has versioning
enabled and prior runs' output bytes are pinned as noncurrent versions, so a plain
`delete_object` would add a delete marker and leak the bytes forever. The tests assert
that versions *and* delete markers are enumerated and passed to `delete_objects`, and
that batching respects the API's 1000-key limit.

**Partial failure must be reported, not swallowed.** Every step of
`delete_single_document` catches its own exception, appends to `errors`, and continues,
with `success` derived from `errors` at the end. That is the right shape — one failed
step should not abandon the rest of the cleanup — but it means `success` is the only
signal, so it is asserted for each step independently.

**Shard arithmetic is asserted at the boundaries**, since 6 shards of 4 hours means the
interesting inputs are 0, 3, 4, 23 and the invalid ones either side.

No AWS call is made: the table and S3 client are `MagicMock`s throughout.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from idp_common.delete_documents import (
    _delete_run_records,
    _get_adjacent_shards,
    _query_shard_for_object_key,
    _scan_all_document_keys,
    _try_exact_list_deletion,
    calculate_shard,
    delete_documents,
    delete_list_entries_robust,
    delete_single_document,
    get_documents_by_batch,
    get_documents_by_pattern,
)

KEY = "batch-1/invoice.pdf"
QUEUED = "2025-09-10T12:03:27.256164+00:00"


def _condition_parts(condition) -> list:
    """Flatten a boto3 `ConditionBase` into its operators and literal values.

    `repr()` on these objects is just the class and address, so a test that greps the
    repr for a value passes or fails for reasons unrelated to the expression. boto3's
    own `get_expression()` is the stable public accessor, so the tree is walked through
    that instead.
    """
    if not hasattr(condition, "get_expression"):
        return [condition]
    expression = condition.get_expression()
    parts = [expression["operator"]]
    for value in expression["values"]:
        parts.extend(_condition_parts(value))
    return parts


def _table() -> MagicMock:
    table = MagicMock()
    table.get_item.return_value = {}
    table.query.return_value = {"Items": []}
    table.scan.return_value = {"Items": []}
    table.delete_item.return_value = {}
    return table


def _s3(pages: list | None = None) -> MagicMock:
    s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = pages if pages is not None else [{}]
    s3.get_paginator.return_value = paginator
    return s3


@pytest.mark.unit
class TestCalculateShard:
    """calculate_shard: 6 four-hour shards per day, and the inputs that are refused."""

    @pytest.mark.parametrize(
        "hour, expected",
        [
            (0, "00"),
            (3, "00"),  # last hour of shard 0
            (4, "01"),  # first hour of shard 1
            (11, "02"),
            (12, "03"),
            (19, "04"),
            (20, "05"),
            (23, "05"),  # last hour of the day
        ],
    )
    def test_each_shard_boundary(self, hour, expected):
        # The boundaries are the whole behaviour: an off-by-one puts a document's list
        # entry in a shard the deleter will not look in, and the row is orphaned.
        date, shard = calculate_shard(f"2025-09-10T{hour:02d}:30:00+00:00")
        assert (date, shard) == ("2025-09-10", expected)

    def test_the_shard_is_zero_padded(self):
        # The PK is built by string concatenation, so "1" instead of "01" silently
        # addresses a shard that does not exist.
        assert calculate_shard("2025-09-10T05:00:00Z")[1] == "01"

    @pytest.mark.parametrize(
        "timestamp, fragment",
        [
            ("", "non-empty string"),
            (None, "non-empty string"),
            (12345, "non-empty string"),
            ("2025-09-10 12:03:27", "missing 'T' separator"),
            ("2025-09-10T1203", "missing ':' separator"),
            ("2025-09-10T24:00:00", "Invalid hour"),
            ("2025-09-10T99:00:00", "Invalid hour"),
        ],
    )
    def test_invalid_timestamps_are_refused_with_a_reason(self, timestamp, fragment):
        # The message matters: this raises into a caller that logs and falls back, so a
        # generic failure would leave no way to tell a malformed record from a miss.
        with pytest.raises(ValueError, match=fragment):
            calculate_shard(timestamp)

    def test_a_non_numeric_hour_is_refused(self):
        with pytest.raises(ValueError):
            calculate_shard("2025-09-10Tab:00:00")


@pytest.mark.unit
class TestAdjacentShards:
    """_get_adjacent_shards: the neighbours searched when the computed shard misses."""

    @pytest.mark.parametrize(
        "shard, expected_suffixes",
        [
            ("00", ["01"]),  # no shard below 0
            ("01", ["00", "02"]),
            ("04", ["03", "05"]),
            ("05", ["04"]),  # no shard above 5
        ],
    )
    def test_the_range_is_clamped_to_the_six_real_shards(
        self, shard, expected_suffixes
    ):
        # Emitting "list#...#s#-1" or "#s#06" would query a PK that cannot exist,
        # turning the fallback into a no-op that still looks like it ran.
        result = _get_adjacent_shards("2025-09-10", shard)
        assert result == [f"list#2025-09-10#s#{s}" for s in expected_suffixes]

    def test_a_non_numeric_shard_yields_no_neighbours(self):
        assert _get_adjacent_shards("2025-09-10", "xx") == []

    def test_neighbours_never_cross_into_another_date(self):
        # A documented limit rather than a bug: an entry that lands in shard 05 of the
        # previous day is not reachable from shard 00 of this one. Pinned so that if
        # cross-date search is ever added, this test says so.
        assert all(
            "2025-09-10" in pk for pk in _get_adjacent_shards("2025-09-10", "00")
        )


@pytest.mark.unit
class TestTryExactListDeletion:
    """_try_exact_list_deletion: ALL_OLD is how a real delete is distinguished."""

    def test_returned_attributes_mean_an_entry_was_deleted(self):
        table = _table()
        table.delete_item.return_value = {"Attributes": {"PK": "p"}}
        assert _try_exact_list_deletion(table, "pk", "sk", KEY) is True
        assert table.delete_item.call_args.kwargs["ReturnValues"] == "ALL_OLD"

    def test_no_attributes_means_nothing_was_there(self):
        # DynamoDB's delete_item succeeds on a missing key, so without ReturnValues the
        # caller could not tell a delete from a no-op and would skip its fallbacks.
        table = _table()
        table.delete_item.return_value = {}
        assert _try_exact_list_deletion(table, "pk", "sk", KEY) is False

    def test_an_error_returns_false_rather_than_raising(self):
        table = _table()
        table.delete_item.side_effect = RuntimeError("throttled")
        assert _try_exact_list_deletion(table, "pk", "sk", KEY) is False


@pytest.mark.unit
class TestQueryShardForObjectKey:
    """_query_shard_for_object_key: filtered query, fully paginated."""

    def test_items_are_collected_across_every_page(self):
        # A single-page test passes with the pagination loop deleted, and a truncated
        # result here means list entries survive the delete.
        table = _table()
        table.query.side_effect = [
            {"Items": [{"SK": "a"}], "LastEvaluatedKey": {"PK": "p"}},
            {"Items": [{"SK": "b"}], "LastEvaluatedKey": {"PK": "p2"}},
            {"Items": [{"SK": "c"}]},
        ]
        assert [i["SK"] for i in _query_shard_for_object_key(table, "pk", KEY)] == [
            "a",
            "b",
            "c",
        ]

    def test_the_continuation_key_is_passed_back(self):
        table = _table()
        table.query.side_effect = [
            {"Items": [], "LastEvaluatedKey": {"PK": "cursor"}},
            {"Items": []},
        ]
        _query_shard_for_object_key(table, "pk", KEY)
        assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {
            "PK": "cursor"
        }

    def test_the_filter_matches_both_the_attribute_and_the_sort_key(self):
        # Two shapes exist in the table: older entries carry ObjectKey as an attribute,
        # newer ones encode it in the SK as "#id#<key>". Matching only one leaves the
        # other orphaned, which is why the filter is an OR.
        table = _table()
        _query_shard_for_object_key(table, "pk", KEY)
        kwargs = table.query.call_args.kwargs
        assert "ObjectKey = :obj_key" in kwargs["FilterExpression"]
        assert "contains(SK, :obj_id)" in kwargs["FilterExpression"]
        assert kwargs["ExpressionAttributeValues"] == {
            ":obj_key": KEY,
            ":obj_id": f"#id#{KEY}",
        }

    def test_an_error_returns_an_empty_list(self):
        table = _table()
        table.query.side_effect = RuntimeError("no")
        assert _query_shard_for_object_key(table, "pk", KEY) == []


@pytest.mark.unit
class TestDeleteListEntriesRobust:
    """The three-strategy cascade, asserted one strategy at a time."""

    def test_strategy_one_short_circuits_the_rest(self):
        # An exact hit must not go on to query shards: those are scans with filters and
        # cost real capacity on every delete.
        table = _table()
        table.delete_item.return_value = {"Attributes": {}}
        assert delete_list_entries_robust(table, KEY, {"QueuedTime": QUEUED}) is True
        table.query.assert_not_called()

    def test_the_exact_key_is_built_from_the_queued_time(self):
        table = _table()
        table.delete_item.return_value = {"Attributes": {}}
        delete_list_entries_robust(table, KEY, {"QueuedTime": QUEUED})
        assert table.delete_item.call_args.kwargs["Key"] == {
            "PK": "list#2025-09-10#s#03",
            "SK": f"ts#{QUEUED}#id#{KEY}",
        }

    def test_initial_event_time_is_the_fallback_timestamp(self):
        # Documents queued before QueuedTime was written carry only InitialEventTime.
        table = _table()
        table.delete_item.return_value = {"Attributes": {}}
        delete_list_entries_robust(table, KEY, {"InitialEventTime": QUEUED})
        assert table.delete_item.call_args.kwargs["Key"]["PK"] == "list#2025-09-10#s#03"

    def test_queued_time_wins_when_both_are_present(self):
        table = _table()
        table.delete_item.return_value = {"Attributes": {}}
        delete_list_entries_robust(
            table,
            KEY,
            {"QueuedTime": QUEUED, "InitialEventTime": "2025-01-01T00:00:00Z"},
        )
        assert table.delete_item.call_args.kwargs["Key"]["PK"] == "list#2025-09-10#s#03"

    def test_strategy_two_deletes_what_the_shard_query_finds(self):
        table = _table()
        # Exact delete misses, then the query finds one entry which deletes cleanly.
        table.delete_item.side_effect = [{}, {"Attributes": {"x": 1}}]
        table.query.return_value = {
            "Items": [{"PK": "list#2025-09-10#s#03", "SK": "ts#...#id#x"}]
        }
        assert delete_list_entries_robust(table, KEY, {"QueuedTime": QUEUED}) is True

    def test_strategy_three_searches_the_adjacent_shards(self):
        # This is the case the whole cascade exists for: the list write landed one
        # shard over from where the queued time computes to.
        table = _table()
        table.delete_item.side_effect = [{}, {"Attributes": {"x": 1}}]
        table.query.side_effect = [
            {"Items": []},  # computed shard: nothing
            {"Items": [{"PK": "list#2025-09-10#s#02", "SK": "sk"}]},  # neighbour: hit
            {"Items": []},
        ]
        assert delete_list_entries_robust(table, KEY, {"QueuedTime": QUEUED}) is True
        queried = [
            c.kwargs["KeyConditionExpression"] for c in table.query.call_args_list
        ]
        assert len(queried) >= 2, "the adjacent shards were never searched"

    def test_no_metadata_means_no_strategy_runs_at_all(self):
        """With no metadata the function cannot do anything, and says so by returning False.

        Worth pinning explicitly because the name says "robust": every strategy is
        gated on `document_metadata`, so a document whose tracking record was already
        gone leaves its list entry behind permanently. The caller records this as
        `list_entries: False` with no error, so it is reported but not as a failure.
        """
        table = _table()
        assert delete_list_entries_robust(table, KEY, None) is False
        table.delete_item.assert_not_called()
        table.query.assert_not_called()

    def test_metadata_without_any_timestamp_runs_no_strategy(self):
        table = _table()
        assert delete_list_entries_robust(table, KEY, {"Status": "COMPLETED"}) is False
        table.delete_item.assert_not_called()

    def test_an_unparseable_timestamp_is_survived(self):
        # A malformed record must not abort the surrounding document delete.
        table = _table()
        assert (
            delete_list_entries_robust(table, KEY, {"QueuedTime": "garbage"}) is False
        )

    def test_a_delete_failure_on_a_found_entry_is_reported_as_not_deleted(self):
        table = _table()
        table.delete_item.side_effect = [{}, RuntimeError("throttled")]
        table.query.side_effect = [
            {"Items": [{"PK": "p", "SK": "s"}]},
            {"Items": []},
            {"Items": []},
        ]
        assert delete_list_entries_robust(table, KEY, {"QueuedTime": QUEUED}) is False


@pytest.mark.unit
class TestDeleteRunRecords:
    """_delete_run_records: every run item, paginated, idempotent."""

    def test_all_run_items_are_deleted_and_counted(self):
        table = _table()
        table.query.return_value = {
            "Items": [
                {"PK": "doc#k", "SK": "run#1"},
                {"PK": "doc#k", "SK": "run#2"},
            ]
        }
        assert _delete_run_records(table, "k") == 2
        assert table.delete_item.call_count == 2

    def test_pagination_is_followed(self):
        table = _table()
        table.query.side_effect = [
            {
                "Items": [{"PK": "doc#k", "SK": "run#1"}],
                "LastEvaluatedKey": {"PK": "c"},
            },
            {"Items": [{"PK": "doc#k", "SK": "run#2"}]},
        ]
        assert _delete_run_records(table, "k") == 2
        assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"PK": "c"}

    def test_a_document_with_no_runs_returns_zero(self):
        # Idempotence: the delete path calls this unconditionally.
        assert _delete_run_records(_table(), "k") == 0

    def test_one_failed_item_does_not_stop_the_others(self):
        table = _table()
        table.query.return_value = {
            "Items": [
                {"PK": "doc#k", "SK": "run#1"},
                {"PK": "doc#k", "SK": "run#2"},
            ]
        }
        table.delete_item.side_effect = [RuntimeError("no"), None]
        assert _delete_run_records(table, "k") == 1

    def test_only_run_items_are_projected(self):
        # The projection keeps the query cheap; run items can be numerous.
        table = _table()
        _delete_run_records(table, "k")
        assert table.query.call_args.kwargs["ProjectionExpression"] == "PK, SK"


@pytest.mark.unit
class TestDeleteSingleDocument:
    """delete_single_document: the whole per-document cleanup and its error reporting."""

    def test_a_dry_run_deletes_nothing(self):
        # The safety valve. It must return before the first mutating call, not merely
        # report differently at the end.
        table = _table()
        s3 = _s3()
        result = delete_single_document(KEY, table, s3, "in", "out", dry_run=True)
        s3.delete_object.assert_not_called()
        s3.delete_objects.assert_not_called()
        table.delete_item.assert_not_called()
        assert result["success"] is True
        assert result["deleted"]["input_file"] is False

    def test_a_dry_run_still_reads_the_metadata(self):
        # It has to, to report anything useful -- and a read is safe.
        table = _table()
        delete_single_document(KEY, table, _s3(), "in", "out", dry_run=True)
        table.get_item.assert_called_once()

    def test_the_input_object_is_deleted_by_key(self):
        table = _table()
        s3 = _s3()
        result = delete_single_document(KEY, table, s3, "in", "out")
        s3.delete_object.assert_called_once_with(Bucket="in", Key=KEY)
        assert result["deleted"]["input_file"] is True

    def test_every_output_version_and_delete_marker_is_purged(self):
        # The output bucket is versioned and prior runs' bytes are pinned as noncurrent
        # versions, so a versionless delete would add a marker and leak them forever.
        # Delete markers are included for the same reason: one left behind keeps the
        # key alive.
        s3 = _s3(
            [
                {
                    "Versions": [{"Key": "k1", "VersionId": "v1"}],
                    "DeleteMarkers": [{"Key": "k1", "VersionId": "dm1"}],
                }
            ]
        )
        result = delete_single_document(KEY, _table(), s3, "in", "out")
        sent = s3.delete_objects.call_args.kwargs["Delete"]["Objects"]
        assert sent == [
            {"Key": "k1", "VersionId": "v1"},
            {"Key": "k1", "VersionId": "dm1"},
        ]
        assert result["deleted"]["output_files"] == 2

    def test_an_entry_without_a_version_id_is_skipped(self):
        s3 = _s3([{"Versions": [{"Key": "k1"}, {"Key": "k2", "VersionId": "v2"}]}])
        result = delete_single_document(KEY, _table(), s3, "in", "out")
        assert result["deleted"]["output_files"] == 1

    def test_deletions_are_batched_at_the_api_limit(self):
        # delete_objects rejects more than 1000 keys, and a document with many runs can
        # exceed that. Exercised just over the boundary so an off-by-one shows up.
        versions = [{"Key": f"k{i}", "VersionId": f"v{i}"} for i in range(1001)]
        s3 = _s3([{"Versions": versions}])
        result = delete_single_document(KEY, _table(), s3, "in", "out")
        batches = [
            c.kwargs["Delete"]["Objects"] for c in s3.delete_objects.call_args_list
        ]
        assert [len(b) for b in batches] == [1000, 1]
        assert result["deleted"]["output_files"] == 1001

    def test_the_output_purge_is_paginated(self):
        s3 = _s3(
            [
                {"Versions": [{"Key": "a", "VersionId": "v1"}]},
                {"Versions": [{"Key": "b", "VersionId": "v2"}]},
            ]
        )
        result = delete_single_document(KEY, _table(), s3, "in", "out")
        assert result["deleted"]["output_files"] == 2

    def test_the_output_prefix_is_the_object_key_unanchored(self):
        """The purge prefix is the bare object key, with no trailing delimiter.

        See #1133. S3 prefixes are not path-aware, so purging `invoice.pdf` also
        matches every key beginning with that string — `invoice.pdf.bak/…`,
        `invoice.pdf-v2/…` — and those versions are deleted with it. Asserted as the
        literal prefix sent, because this is the one call in the module that can destroy
        another document's data and nothing else in the suite looks at it.
        """
        s3 = _s3()
        delete_single_document("invoice.pdf", _table(), s3, "in", "out")
        paginate = s3.get_paginator.return_value.paginate
        assert paginate.call_args.kwargs == {"Bucket": "out", "Prefix": "invoice.pdf"}

    def test_the_document_record_is_deleted_when_metadata_was_found(self):
        table = _table()
        table.get_item.return_value = {
            "Item": {"PK": f"doc#{KEY}", "QueuedTime": QUEUED}
        }
        table.delete_item.return_value = {"Attributes": {}}
        result = delete_single_document(KEY, table, _s3(), "in", "out")
        assert result["deleted"]["document_record"] is True
        assert {"PK": f"doc#{KEY}", "SK": "none"} in [
            c.kwargs.get("Key") for c in table.delete_item.call_args_list
        ]

    def test_no_metadata_means_no_document_record_delete(self):
        # Nothing to delete, and issuing one would be a wasted write on every retry.
        table = _table()
        result = delete_single_document(KEY, table, _s3(), "in", "out")
        assert result["deleted"]["document_record"] is False

    @pytest.mark.parametrize(
        "failing, fragment",
        [
            ("get_item", "Error getting document metadata"),
            ("delete_object", "Error deleting from input bucket"),
        ],
    )
    def test_each_step_reports_its_own_failure(self, failing, fragment):
        table, s3 = _table(), _s3()
        target = table if failing == "get_item" else s3
        getattr(target, failing).side_effect = RuntimeError("boom")
        result = delete_single_document(KEY, table, s3, "in", "out")
        assert result["success"] is False
        assert any(fragment in e for e in result["errors"])

    def test_an_output_purge_failure_does_not_abandon_the_rest(self):
        # The ordering matters: if an S3 failure aborted the function, the DynamoDB rows
        # would survive and the document would keep appearing in listings.
        table = _table()
        table.get_item.return_value = {"Item": {"PK": f"doc#{KEY}"}}
        s3 = _s3()
        s3.get_paginator.side_effect = RuntimeError("no access")
        result = delete_single_document(KEY, table, s3, "in", "out")
        assert result["success"] is False
        assert result["deleted"]["document_record"] is True, (
            "the tracking record was left behind after an S3 failure"
        )

    def test_success_is_true_only_when_no_step_errored(self):
        result = delete_single_document(KEY, _table(), _s3(), "in", "out")
        assert result["errors"] == []
        assert result["success"] is True


@pytest.mark.unit
class TestDeleteDocuments:
    """delete_documents: the batch wrapper and continue_on_error."""

    def test_counts_are_tallied_across_documents(self):
        table = _table()
        s3 = _s3()
        result = delete_documents(["a", "b"], table, s3, "in", "out")
        assert result["total_count"] == 2
        assert result["deleted_count"] == 2
        assert result["failed_count"] == 0
        assert result["success"] is True

    def test_an_empty_list_succeeds_vacuously(self):
        result = delete_documents([], _table(), _s3(), "in", "out")
        assert result == {
            "success": True,
            "deleted_count": 0,
            "failed_count": 0,
            "total_count": 0,
            "results": [],
            "dry_run": False,
        }

    def test_continue_on_error_processes_every_document(self):
        # The default, and the right one for a bulk delete: one inaccessible object
        # should not strand the rest of the batch half-deleted.
        table = _table()
        s3 = _s3()
        s3.delete_object.side_effect = [RuntimeError("no"), None, None]
        result = delete_documents(["a", "b", "c"], table, s3, "in", "out")
        assert result["failed_count"] == 1
        assert result["deleted_count"] == 2
        assert len(result["results"]) == 3

    def test_continue_on_error_false_stops_at_the_first_failure(self):
        table = _table()
        s3 = _s3()
        s3.delete_object.side_effect = [RuntimeError("no"), None]
        result = delete_documents(
            ["a", "b", "c"], table, s3, "in", "out", continue_on_error=False
        )
        assert result["failed_count"] == 1
        assert len(result["results"]) == 1, "it kept going after the first failure"

    def test_the_dry_run_flag_is_reported_and_propagated(self):
        s3 = _s3()
        result = delete_documents(["a"], _table(), s3, "in", "out", dry_run=True)
        assert result["dry_run"] is True
        s3.delete_object.assert_not_called()

    def test_an_unexpected_error_is_recorded_per_document(self):
        # delete_single_document catches its own step failures, so reaching this handler
        # means something outside those steps broke. It still must not lose the key.
        table = _table()
        table.get_item.side_effect = None
        s3 = _s3()
        s3.get_paginator.side_effect = None
        result = delete_documents(["a"], table, s3, "in", "out")
        assert result["results"][0]["object_key"] == "a"


@pytest.mark.unit
class TestScanAllDocumentKeys:
    """_scan_all_document_keys: paginated scan with an optional status filter."""

    def test_every_page_is_collected(self):
        table = _table()
        table.scan.side_effect = [
            {"Items": [{"ObjectKey": "a"}], "LastEvaluatedKey": {"PK": "c"}},
            {"Items": [{"ObjectKey": "b"}]},
        ]
        assert len(_scan_all_document_keys(table)) == 2
        assert table.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == {"PK": "c"}

    def test_without_a_filter_only_document_items_are_selected(self):
        # The scan is table-wide, so without the doc# condition it would also return
        # list and run items and the caller would try to delete them as documents.
        table = _table()
        _scan_all_document_keys(table)
        parts = _condition_parts(table.scan.call_args.kwargs["FilterExpression"])
        assert "begins_with" in parts
        assert "doc#" in parts
        assert "AND" not in parts, "an unrequested condition was added"

    def test_a_status_filter_is_ANDed_onto_the_document_condition(self):
        table = _table()
        _scan_all_document_keys(table, status_filter="FAILED")
        parts = _condition_parts(table.scan.call_args.kwargs["FilterExpression"])
        assert parts[0] == "AND"
        assert "doc#" in parts, "the doc# restriction was replaced rather than extended"
        assert "FAILED" in parts


@pytest.mark.unit
class TestGetDocumentsByBatch:
    """get_documents_by_batch: the selector that feeds a bulk delete."""

    def test_matching_documents_are_returned(self):
        table = _table()
        table.scan.return_value = {
            "Items": [
                {"ObjectKey": "batch-1/a.pdf"},
                {"ObjectKey": "batch-2/b.pdf"},
            ]
        }
        assert get_documents_by_batch(table, "batch-1") == ["batch-1/a.pdf"]

    def test_the_batch_id_is_matched_as_a_SUBSTRING_not_a_prefix(self):
        """`batch_id in object_key` — anywhere in the key, despite the docstring.

        See #1133. The parameter is documented as "Batch ID prefix" and the match is an
        unanchored substring, so `batch-1` also selects `batch-10/…`, `batch-11/…` and
        `archive/batch-1x/…`. The result feeds `delete_documents`, so the consequence of
        the mismatch is deleting documents the caller did not ask for.

        Asserted in the direction that is true, with the surprising members spelled out
        individually, so that narrowing the match to a prefix is a visible behaviour
        change rather than a silent one.
        """
        table = _table()
        table.scan.return_value = {
            "Items": [
                {"ObjectKey": "batch-1/a.pdf"},
                {"ObjectKey": "batch-10/b.pdf"},
                {"ObjectKey": "archive/batch-1x/c.pdf"},
                {"ObjectKey": "batch-2/d.pdf"},
            ]
        }
        assert get_documents_by_batch(table, "batch-1") == [
            "batch-1/a.pdf",
            "batch-10/b.pdf",
            "archive/batch-1x/c.pdf",
        ]

    def test_an_item_with_no_object_key_is_skipped(self):
        table = _table()
        table.scan.return_value = {"Items": [{"PK": "doc#x"}]}
        assert get_documents_by_batch(table, "batch") == []

    def test_a_scan_error_returns_an_empty_list(self):
        # Returning [] means the caller deletes nothing, which is the safe direction for
        # a selector feeding a delete -- but it is indistinguishable from "no matches".
        table = _table()
        table.scan.side_effect = RuntimeError("throttled")
        assert get_documents_by_batch(table, "batch") == []

    def test_the_status_filter_is_forwarded(self):
        table = _table()
        get_documents_by_batch(table, "b", status_filter="COMPLETED")
        parts = _condition_parts(table.scan.call_args.kwargs["FilterExpression"])
        assert "COMPLETED" in parts


@pytest.mark.unit
class TestGetDocumentsByPattern:
    """get_documents_by_pattern: fnmatch globbing."""

    @pytest.mark.parametrize(
        "pattern, expected",
        [
            ("batch-1/*", ["batch-1/a.pdf"]),
            ("*/invoice*.pdf", ["batch-2/invoice-9.pdf"]),
            ("*2025*", ["2025/old.pdf"]),
            ("batch-?/a.pdf", ["batch-1/a.pdf"]),
            ("nomatch*", []),
        ],
    )
    def test_each_documented_pattern_form(self, pattern, expected):
        table = _table()
        table.scan.return_value = {
            "Items": [
                {"ObjectKey": "batch-1/a.pdf"},
                {"ObjectKey": "batch-2/invoice-9.pdf"},
                {"ObjectKey": "2025/old.pdf"},
            ]
        }
        assert get_documents_by_pattern(table, pattern) == expected

    def test_a_star_does_not_stop_at_a_slash(self):
        # fnmatch is not glob: `*` spans path separators. That makes `batch-*` broader
        # than a shell user expects, and this selector also feeds a delete.
        table = _table()
        table.scan.return_value = {"Items": [{"ObjectKey": "batch-1/sub/deep.pdf"}]}
        assert get_documents_by_pattern(table, "batch-1/*") == ["batch-1/sub/deep.pdf"]

    def test_a_scan_error_returns_an_empty_list(self):
        table = _table()
        table.scan.side_effect = RuntimeError("no")
        assert get_documents_by_pattern(table, "*") == []
