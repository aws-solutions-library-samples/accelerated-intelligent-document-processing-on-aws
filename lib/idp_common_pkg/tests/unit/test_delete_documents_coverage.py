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

Five things shape these tests.

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

**An empty selection has one meaning, so a failure cannot use it.** The two selectors
return `List[str]`, and `[]` is the ordinary answer for "that batch holds no documents"
— which is why a handler that returned `[]` for a missing selector or a throttled scan
reported a failure as a successful no-op. `TestASelectorFailureIsNotReportedAsSuccess`
asserts the outcome the *caller* sees for each class of failure, not merely that
something was raised somewhere inside, and derives its DynamoDB faults from the
botocore service model rather than from a hand-written error shape.

No AWS call is made: the table and S3 client are `MagicMock`s throughout.
"""

from __future__ import annotations

from functools import lru_cache
from unittest.mock import MagicMock

import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

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


#: Output keys belonging to ``KEY``. The purge is scoped to the document, so a fixture
#: key has to be one of the document's own outputs for the mechanics tests to exercise
#: the mechanics rather than the breadth — the unanchored prefix used to delete any key
#: at all, which is what #1133 was about. Shapes taken from what the pipeline writes:
#: ``{input_key}/sections/...``, ``{input_key}/summary/...``, ``{input_key}/runs/...``.
OUT1 = f"{KEY}/sections/1/result.json"
OUT2 = f"{KEY}/summary/summary.json"


def _own_outputs(count: int) -> list:
    """``count`` distinct output keys under ``KEY``."""
    return [f"{KEY}/runs/r{i}/manifest.json" for i in range(count)]


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
                    "Versions": [{"Key": OUT1, "VersionId": "v1"}],
                    "DeleteMarkers": [{"Key": OUT1, "VersionId": "dm1"}],
                }
            ]
        )
        result = delete_single_document(KEY, _table(), s3, "in", "out")
        sent = s3.delete_objects.call_args.kwargs["Delete"]["Objects"]
        assert sent == [
            {"Key": OUT1, "VersionId": "v1"},
            {"Key": OUT1, "VersionId": "dm1"},
        ]
        assert result["deleted"]["output_files"] == 2

    def test_an_entry_without_a_version_id_is_skipped(self):
        s3 = _s3([{"Versions": [{"Key": OUT1}, {"Key": OUT2, "VersionId": "v2"}]}])
        result = delete_single_document(KEY, _table(), s3, "in", "out")
        assert result["deleted"]["output_files"] == 1

    def test_deletions_are_batched_at_the_api_limit(self):
        # delete_objects rejects more than 1000 keys, and a document with many runs can
        # exceed that. Exercised just over the boundary so an off-by-one shows up.
        versions = [
            {"Key": key, "VersionId": f"v{i}"}
            for i, key in enumerate(_own_outputs(1001))
        ]
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
                {"Versions": [{"Key": OUT1, "VersionId": "v1"}]},
                {"Versions": [{"Key": OUT2, "VersionId": "v2"}]},
            ]
        )
        result = delete_single_document(KEY, _table(), s3, "in", "out")
        assert result["deleted"]["output_files"] == 2

    def test_a_sibling_sharing_the_key_as_a_byte_prefix_is_not_purged(self):
        """The assertion that matters: what the purge leaves alone (#1133).

        This block deletes **all object versions and delete markers** it finds, which is
        deliberate for the target document — the output bucket is versioned and prior
        runs' bytes are pinned as noncurrent versions. It means a prefix collision
        destroyed a sibling document's entire output history, not just its current
        objects.

        ⚠️ **An S3 `Prefix` is a byte prefix, not a path segment.** `Prefix=` is still the
        bare key, so the server does the coarse narrowing and the listing is unchanged;
        what decides deletion is a client-side predicate. Asserted by naming the sibling
        keys, because a count would pass while deleting the wrong two objects.
        """
        s3 = _s3(
            [
                {
                    "Versions": [
                        {
                            "Key": "invoice.pdf/sections/1/result.json",
                            "VersionId": "v1",
                        },
                        {"Key": "invoice.pdf", "VersionId": "v2"},
                        {
                            "Key": "invoice.pdf.bak/sections/1/result.json",
                            "VersionId": "v3",
                        },
                        {
                            "Key": "invoice.pdf-v2/summary/summary.json",
                            "VersionId": "v4",
                        },
                    ],
                    "DeleteMarkers": [
                        {
                            "Key": "invoice.pdf/runs/r0/manifest.json",
                            "VersionId": "dm1",
                        },
                        {
                            "Key": "invoice.pdf.bak/runs/r0/manifest.json",
                            "VersionId": "dm2",
                        },
                    ],
                }
            ]
        )

        result = delete_single_document("invoice.pdf", _table(), s3, "in", "out")

        sent = {
            o["Key"] for o in s3.delete_objects.call_args.kwargs["Delete"]["Objects"]
        }
        assert sent == {
            "invoice.pdf/sections/1/result.json",
            "invoice.pdf",
            "invoice.pdf/runs/r0/manifest.json",
        }
        for sibling in (
            "invoice.pdf.bak/sections/1/result.json",
            "invoice.pdf-v2/summary/summary.json",
            "invoice.pdf.bak/runs/r0/manifest.json",
        ):
            assert sibling not in sent, (
                f"{sibling} belongs to another document and would be DESTROYED, "
                "including its noncurrent versions"
            )
        assert result["deleted"]["output_files"] == 3

    def test_the_listing_prefix_is_still_the_bare_key(self):
        """The narrowing is client-side on purpose. A `Prefix` with a delimiter would
        miss anything written AT the document's own key, and the document's own key is
        included in what belongs to it — so the server-side prefix stays coarse and the
        predicate decides."""
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

    def test_a_batch_id_selects_only_documents_under_that_batch(self):
        """The assertion that matters here is what is **not** selected (#1133).

        This result feeds `delete_documents`, so an over-match destroys documents the
        caller did not name and the failure direction is unrecoverable. The selector was
        an unanchored substring — `batch-1` also took `batch-10/…` and
        `archive/batch-1x/…` — while the parameter was documented as a prefix.

        ⚠️ **Matching the old docstring literally would not have fixed it.**
        `"batch-10/b.pdf".startswith("batch-1")` is true, so a bare prefix test still
        takes the neighbouring batch. The delimiter is what makes it correct, and that is
        the layout's own meaning: every site that writes a batch id into a key writes
        `f"{batch_id}/..."`.

        The negative members are spelled out individually rather than asserted as a
        count, so a future change that re-broadens the match names which key it took.
        """
        table = _table()
        table.scan.return_value = {
            "Items": [
                {"ObjectKey": "batch-1/a.pdf"},
                {"ObjectKey": "batch-1/nested/deep.pdf"},
                {"ObjectKey": "batch-10/b.pdf"},
                {"ObjectKey": "archive/batch-1x/c.pdf"},
                {"ObjectKey": "batch-2/d.pdf"},
                {"ObjectKey": "prefixed-batch-1/e.pdf"},
            ]
        }

        selected = get_documents_by_batch(table, "batch-1")

        assert selected == ["batch-1/a.pdf", "batch-1/nested/deep.pdf"]
        for not_selected in (
            "batch-10/b.pdf",
            "archive/batch-1x/c.pdf",
            "batch-2/d.pdf",
            "prefixed-batch-1/e.pdf",
        ):
            assert not_selected not in selected, (
                f"{not_selected} would be DELETED by a request for batch-1"
            )

    def test_an_empty_batch_id_is_refused_rather_than_selecting_anything(self):
        """The largest case the delimiter closes, and the one worth naming.

        The empty string is a substring of every key, so ``batch_id in object_key`` was
        **universally true**: measured against a four-key table, ``batch_id=""`` selected
        all four — including a key sharing no prefix with anything — and every selected
        key was passed to the deleter. Requiring the delimiter makes it select none,
        since no key starts with ``"/"``.

        ⚠️ **Reachability is narrower than "an empty argument deletes everything", and
        the distinction is worth keeping straight.** Both shipped entry points refuse a
        falsy selector *before* this function is reached — the SDK raises
        ``IDPConfigurationError("Must specify either batch_id or pattern")`` when both
        are falsy, and the CLI counts selectors with ``if x`` and exits 1 at zero. So
        the unbounded selection was reachable by a **direct call** to this public
        library function, not from ``idp-cli delete-documents`` or
        ``client.batch.delete_documents``.

        It is still worth a named test rather than a footnote: the guard lives in a
        different layer from the defect, so a refactor that moves or drops it re-opens
        an unbounded delete, and this function is exported for callers who have no
        guard at all.

        The refusal is now what closes it, and it is the stronger form: no key can be
        returned at all, and the scan is never issued, so neither reading of an empty
        selector — "everything" or "nothing" — can reach the deleter. `""` is not a
        batch and a caller who wants every document asks with `pattern="*"`.
        """
        table = _table()
        table.scan.return_value = {
            "Items": [
                {"ObjectKey": "batch-1/a.pdf"},
                {"ObjectKey": "batch-10/b.pdf"},
                {"ObjectKey": "archive/batch-1x/c.pdf"},
                {"ObjectKey": "other/d.pdf"},
            ]
        }

        with pytest.raises(ValueError, match="batch_id"):
            get_documents_by_batch(table, "")
        table.scan.assert_not_called()

    def test_a_batch_id_given_with_a_trailing_slash_behaves_the_same(self):
        """`--batch-id batch-1/` is an easy thing to type and must not select nothing."""
        table = _table()
        table.scan.return_value = {"Items": [{"ObjectKey": "batch-1/a.pdf"}]}
        assert get_documents_by_batch(table, "batch-1/") == ["batch-1/a.pdf"]

    def test_the_substring_behaviour_is_still_available_explicitly(self):
        """Narrowing the batch selector removes no capability: a caller who wants the
        broad match asks for it by pattern, where the breadth is visible in what they
        typed rather than implied by a parameter documented as a prefix."""
        table = _table()
        table.scan.return_value = {
            "Items": [
                {"ObjectKey": "batch-1/a.pdf"},
                {"ObjectKey": "batch-10/b.pdf"},
            ]
        }
        assert get_documents_by_pattern(table, "*batch-1*") == [
            "batch-1/a.pdf",
            "batch-10/b.pdf",
        ]

    def test_an_item_with_no_object_key_is_skipped(self):
        table = _table()
        table.scan.return_value = {"Items": [{"PK": "doc#x"}]}
        assert get_documents_by_batch(table, "batch") == []

    def test_a_scan_error_reaches_the_caller_instead_of_reading_as_no_matches(self):
        # Deleting nothing is the safe direction and a raise keeps it -- nothing is
        # selected either way. What `[]` cannot do is tell the caller apart from "no
        # matches", which is why the fault is not converted into one.
        table = _table()
        table.scan.side_effect = RuntimeError("throttled")
        with pytest.raises(RuntimeError, match="throttled"):
            get_documents_by_batch(table, "batch")

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

    def test_a_scan_error_reaches_the_caller_instead_of_reading_as_no_matches(self):
        table = _table()
        table.scan.side_effect = RuntimeError("no")
        with pytest.raises(RuntimeError, match="no"):
            get_documents_by_pattern(table, "*")


# ---------------------------------------------------------------------------
# What the caller is told when a selection cannot be made (#1187)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _dynamodb_client():
    """A DynamoDB client built offline, for its **modelled** exception classes.

    No call is made and no credential is used: botocore builds the client, and its
    `exceptions` factory, from the bundled service model alone. Placeholder credentials
    are passed explicitly because the gated pytest wrapper strips the AWS environment.
    """
    import botocore.session

    return botocore.session.get_session().create_client(
        "dynamodb",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def _scan_error_codes() -> list[str]:
    """Every error shape the service model declares for ``Scan``.

    Derived, not listed. A hand-written set of codes records a belief about what
    DynamoDB raises, and the decision under test here — that no class arising in the
    scan may be turned into an empty selection — is only as good as the set it was made
    against. Five today, and every one of them is an operational fault rather than
    anything a caller could have got right: three throttles, a missing table, and a
    server-side error. The generic codes botocore does not model per operation
    (`AccessDeniedException`, `ValidationException`) arrive as the same `ClientError`.
    """
    model = _dynamodb_client().meta.service_model
    return sorted(shape.name for shape in model.operation_model("Scan").error_shapes)


def _modelled_scan_error(code: str) -> ClientError:
    """The exception boto3 itself raises for ``code`` on a ``Scan``."""
    return _dynamodb_client().exceptions.from_code(code)(
        {"Error": {"Code": code, "Message": f"{code} from the service"}}, "Scan"
    )


def _numeric_object_key():
    """The value the resource layer yields for an ``ObjectKey`` stored as a number.

    `Table.scan` deserializes attributes before the caller sees them, so a record whose
    key was written as `N` arrives as a `Decimal` and neither selector can match it.
    Taken from boto3's own deserializer rather than invented, because the point of the
    fixture is that the failure is one the table can really produce.
    """
    return TypeDeserializer().deserialize({"N": "5"})


def _populated_table() -> MagicMock:
    """A table holding two documents of `batch-2`, so a selector has work to do."""
    table = _table()
    table.scan.return_value = {
        "Items": [{"ObjectKey": "batch-2/a.pdf"}, {"ObjectKey": "batch-2/b.pdf"}]
    }
    return table


@pytest.mark.unit
class TestASelectorFailureIsNotReportedAsSuccess:
    """The outcome a caller reads when the selection could not be made.

    The defect (#1187) was not that something raised — it did, inside the selector —
    but that the raise was converted into `[]` and the deleter then returned
    `success=True, deleted_count=0, total_count=0`. Measured on the code before this
    change, against a two-document table with `batch_id=None`: the selector logged
    `'NoneType' object has no attribute 'rstrip'` at ERROR and returned `[]`, and the
    delete reported exactly that success.

    So every assertion here is about what the caller gets back, driven the way a direct
    library caller drives it — select, then delete what was selected — and each class
    of failure is covered separately, since narrowing a handler one class at a time is
    how the other classes quietly lose their guarantee. The reachable path is the direct
    one: the CLI and the SDK both refuse a falsy selector in their own layer first.
    """

    def _select_and_delete(self, table, s3, batch_id=..., pattern=...):
        """Select, then delete the selection — one operation from where the caller sits.

        That is where the wrong signal showed: the selector's ERROR line went to the log
        while the *return value* of the pair said the delete had succeeded.
        """
        if pattern is ...:
            keys = get_documents_by_batch(table, batch_id)
        else:
            keys = get_documents_by_pattern(table, pattern)
        return delete_documents(keys, table, s3, "in", "out")

    def test_a_missing_batch_selector_is_refused_rather_than_reported_as_a_no_op(self):
        table, s3 = _populated_table(), _s3()

        with pytest.raises(TypeError, match="batch_id"):
            self._select_and_delete(table, s3, batch_id=None)

        assert s3.delete_object.call_count == 0
        assert table.delete_item.call_count == 0

    def test_a_missing_pattern_selector_is_refused_rather_than_reported_as_a_no_op(
        self,
    ):
        table, s3 = _populated_table(), _s3()

        with pytest.raises(TypeError, match="pattern"):
            self._select_and_delete(table, s3, pattern=None)

        assert s3.delete_object.call_count == 0

    @pytest.mark.parametrize(
        "bad",
        [None, 0, 7, b"batch-2", ["batch-2"], {"batch_id": "batch-2"}],
        ids=["none", "zero", "int", "bytes", "list", "dict"],
    )
    def test_any_non_string_selector_is_refused_by_both_selectors(self, bad):
        """A `batch_id` read out of a record can be any of these, not only `None`.

        `bytes` is the one worth spelling out: before this it raised a `TypeError` in
        the batch selector and was swallowed like the rest, while `0` raised an
        `AttributeError` — two classes and one silent `[]`, which is why the refusal is
        by type at the entry rather than by class at the handler.
        """
        for call in (get_documents_by_batch, get_documents_by_pattern):
            table = _populated_table()
            with pytest.raises(TypeError, match="must be a str"):
                call(table, bad)
            table.scan.assert_not_called()

    def test_an_empty_selector_is_refused_by_both_selectors(self):
        for call, name in (
            (get_documents_by_batch, "batch_id"),
            (get_documents_by_pattern, "pattern"),
        ):
            table = _populated_table()
            with pytest.raises(ValueError, match=name):
                call(table, "")
            table.scan.assert_not_called()

    def test_a_batch_that_holds_nothing_still_reports_success(self):
        """The refusal must not swallow the ordinary answer.

        An empty batch is not an error — cleaning up after a run that produced nothing
        is a legitimate no-op — and that is the whole reason `[]` cannot also stand for
        a failure. If this test and the ones above cannot both pass, the fix is wrong.
        """
        result = self._select_and_delete(_populated_table(), _s3(), batch_id="batch-9")

        assert result["success"] is True
        assert (result["deleted_count"], result["total_count"]) == (0, 0)

    @pytest.mark.parametrize("code", _scan_error_codes())
    def test_every_fault_the_service_model_declares_for_scan_reaches_the_caller(
        self, code
    ):
        """A throttle, a missing table or a server error is not "the batch is empty".

        Each of these used to be logged and returned as `[]`, which both callers report
        as a success: the SDK as `BatchDeletionResult(success=True, deleted_count=0)`,
        the CLI as "No documents found for batch: …" with exit 0. The error code is
        asserted too, because a caller that wants to retry a throttle needs to be able
        to tell it from a missing table.
        """
        table, s3 = _populated_table(), _s3()
        table.scan.side_effect = _modelled_scan_error(code)

        with pytest.raises(ClientError) as raised:
            self._select_and_delete(table, s3, batch_id="batch-2")

        assert raised.value.response["Error"]["Code"] == code
        assert s3.delete_object.call_count == 0

    def test_a_client_side_botocore_failure_reaches_the_caller(self):
        """The other family: the request never got as far as a service error.

        `EndpointConnectionError` is a `BotoCoreError`, not a `ClientError`, so a
        handler narrowed to service errors alone would still convert this one into an
        empty selection.
        """
        table = _populated_table()
        table.scan.side_effect = EndpointConnectionError(
            endpoint_url="https://dynamodb.us-east-1.amazonaws.com/"
        )

        with pytest.raises(BotoCoreError):
            self._select_and_delete(table, _s3(), batch_id="batch-2")

    def test_a_fault_part_way_through_pagination_is_not_reported_as_an_empty_batch(
        self,
    ):
        """Losing pages already collected is the worst-reported version of this.

        Measured before the change: the first page's keys were discarded with the
        exception and `[]` came back, so a batch of any size read as empty whenever the
        second page was throttled.
        """
        table, s3 = _populated_table(), _s3()
        table.scan.side_effect = [
            {
                "Items": [{"ObjectKey": "batch-2/a.pdf"}],
                "LastEvaluatedKey": {"PK": "x"},
            },
            _modelled_scan_error("ProvisionedThroughputExceededException"),
        ]

        with pytest.raises(ClientError):
            self._select_and_delete(table, s3, batch_id="batch-2")

        assert s3.delete_object.call_count == 0

    @pytest.mark.parametrize(
        "call, selector, failing_class",
        [
            (get_documents_by_batch, "batch-2", AttributeError),
            (get_documents_by_pattern, "batch-2/*", TypeError),
        ],
        ids=["batch", "pattern"],
    )
    def test_a_fault_in_the_filter_loop_is_not_reported_as_a_complete_delete(
        self, call, selector, failing_class
    ):
        """The partial selection, which is the only case that deleted anything.

        A record whose `ObjectKey` was stored as a number comes back as a `Decimal`, and
        neither predicate can take one. The old handler sat *outside* the loop but
        returned the list built so far, so of two matching documents the first was
        selected, deleted and reported as a complete success — measured: one key
        returned, one `delete_object`, `success=True`. Both predicates are covered
        because they fail with different classes on the same record.
        """
        table, s3 = _populated_table(), _s3()
        table.scan.return_value = {
            "Items": [
                {"ObjectKey": "batch-2/a.pdf"},
                {"ObjectKey": _numeric_object_key()},
                {"ObjectKey": "batch-2/b.pdf"},
            ]
        }

        with pytest.raises(failing_class):
            keys = call(table, selector)
            delete_documents(keys, table, s3, "in", "out")

        assert s3.delete_object.call_count == 0
