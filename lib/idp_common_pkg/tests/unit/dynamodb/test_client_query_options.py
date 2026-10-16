# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Keyword options on `DynamoDBClient.query`, plus `query_pages` / `query_count`."""

from unittest import mock

import pytest
from moto import mock_aws

from idp_common.dynamodb.client import DynamoDBClient

from ._gsi_table import REGION, TABLE, make_table


def _seed(client: DynamoDBClient, n: int = 5) -> None:
    for i in range(n):
        item = {"PK": "p", "SK": f"N#{i}", "Flag": "y" if i % 2 == 0 else "n"}
        if i < 2:
            item["UnreadPK"] = "p"
        client.put_item(item)


@pytest.mark.unit
@mock_aws
class TestQueryOptions:
    def setup_method(self, method):
        make_table()
        self.client = DynamoDBClient(table_name=TABLE, region=REGION)
        _seed(self.client)

    def test_index_name_returns_only_items_in_the_sparse_index(self):
        resp = self.client.query(
            "UnreadPK = :pk",
            expression_attribute_values={":pk": "p"},
            index_name="UnreadIndex",
        )
        assert sorted(i["SK"] for i in resp["Items"]) == ["N#0", "N#1"]

    def test_scan_index_forward_false_is_descending(self):
        resp = self.client.query(
            "PK = :pk",
            expression_attribute_values={":pk": "p"},
            scan_index_forward=False,
        )
        assert [i["SK"] for i in resp["Items"]] == [f"N#{i}" for i in range(4, -1, -1)]

    def test_filtered_query_pages_collects_every_match(self):
        pages = list(
            self.client.query_pages(
                "PK = :pk",
                expression_attribute_values={":pk": "p", ":y": "y"},
                filter_expression="Flag = :y",
                page_size=1,
            )
        )
        assert len(pages) > 1, "page_size=1 must force several pages"
        matches = [i["SK"] for p in pages for i in p.get("Items", [])]
        assert matches == ["N#0", "N#2", "N#4"]

    def test_select_count_returns_count_and_no_items(self):
        resp = self.client.query(
            "PK = :pk", expression_attribute_values={":pk": "p"}, select="COUNT"
        )
        assert resp["Count"] == 5
        assert "Items" not in resp

    def test_query_count_sums_across_pages(self):
        spy = mock.patch.object(
            self.client.table, "query", wraps=self.client.table.query
        )
        with spy as wrapped:
            # Force paging through the underlying limit by passing page_size via
            # query_pages; query_count itself uses no limit, so wrap query_pages.
            original = self.client.query_pages

            def paged(*args, **kwargs):
                kwargs["page_size"] = 2
                return original(*args, **kwargs)

            with mock.patch.object(self.client, "query_pages", side_effect=paged):
                total = self.client.query_count(
                    "PK = :pk", expression_attribute_values={":pk": "p"}
                )
        assert total == 5
        assert wrapped.call_count >= 3
        assert all(c.kwargs["Select"] == "COUNT" for c in wrapped.call_args_list)

    def test_projection_expression_limits_attributes(self):
        resp = self.client.query(
            "PK = :pk",
            expression_attribute_values={":pk": "p"},
            projection_expression="SK",
        )
        assert all(set(i) == {"SK"} for i in resp["Items"])

    def test_consistent_read_is_passed_through(self):
        with mock.patch.object(
            self.client.table, "query", wraps=self.client.table.query
        ) as wrapped:
            self.client.query(
                "PK = :pk",
                expression_attribute_values={":pk": "p"},
                consistent_read=True,
            )
        assert wrapped.call_args.kwargs["ConsistentRead"] is True

    def test_unset_options_are_not_sent(self):
        with mock.patch.object(
            self.client.table, "query", wraps=self.client.table.query
        ) as wrapped:
            self.client.query("PK = :pk", expression_attribute_values={":pk": "p"})
        sent = set(wrapped.call_args.kwargs)
        assert sent == {"KeyConditionExpression", "ExpressionAttributeValues"}

    def test_legacy_positional_call_still_works(self):
        resp = self.client.query("PK = :pk", None, {":pk": "p"})
        assert resp["Count"] == 5

    def test_query_pages_stops_at_max_pages(self):
        pages = list(
            self.client.query_pages(
                "PK = :pk",
                expression_attribute_values={":pk": "p"},
                page_size=1,
                max_pages=2,
            )
        )
        assert len(pages) == 2
