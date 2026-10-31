# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`DynamoDBClient.batch_get_items`: chunking, de-duplication and retries."""

from unittest import mock

import pytest
from moto import mock_aws

from idp_common.dynamodb.client import (
    DynamoDBClient,
    DynamoDBError,
    DynamoDBUnprocessedKeysError,
)

from ._gsi_table import REGION, TABLE, make_table


@pytest.mark.unit
@mock_aws
class TestBatchGet:
    def setup_method(self, method):
        make_table()
        self.client = DynamoDBClient(table_name=TABLE, region=REGION)

    def test_230_keys_take_three_chunks(self):
        self.client.batch_write_items(
            put_items=[{"PK": "p", "SK": f"{i:03d}"} for i in range(230)]
        )
        keys = [{"PK": "p", "SK": f"{i:03d}"} for i in range(230)]
        with mock.patch.object(
            self.client.dynamodb,
            "batch_get_item",
            wraps=self.client.dynamodb.batch_get_item,
        ) as spy:
            items = self.client.batch_get_items(keys)
        assert len(items) == 230
        assert [
            len(c.kwargs["RequestItems"][TABLE]["Keys"]) for c in spy.call_args_list
        ] == [100, 100, 30]

    def test_duplicates_are_collapsed(self):
        self.client.put_item({"PK": "p", "SK": "a"})
        with mock.patch.object(
            self.client.dynamodb,
            "batch_get_item",
            wraps=self.client.dynamodb.batch_get_item,
        ) as spy:
            items = self.client.batch_get_items(
                [{"PK": "p", "SK": "a"}, {"PK": "p", "SK": "a"}]
            )
        assert len(items) == 1
        assert len(spy.call_args.kwargs["RequestItems"][TABLE]["Keys"]) == 1

    def test_options_are_passed(self):
        self.client.put_item({"PK": "p", "SK": "a", "Other": "x"})
        with mock.patch.object(
            self.client.dynamodb,
            "batch_get_item",
            wraps=self.client.dynamodb.batch_get_item,
        ) as spy:
            items = self.client.batch_get_items(
                [{"PK": "p", "SK": "a"}],
                consistent_read=True,
                projection_expression="#sk",
                expression_attribute_names={"#sk": "SK"},
            )
        request = spy.call_args.kwargs["RequestItems"][TABLE]
        assert request["ConsistentRead"] is True
        assert items == [{"SK": "a"}]

    def test_unprocessed_keys_are_retried(self):
        unprocessed = {TABLE: {"Keys": [{"PK": "p", "SK": "b"}]}}
        fake = mock.Mock(
            side_effect=[
                {
                    "Responses": {TABLE: [{"PK": "p", "SK": "a"}]},
                    "UnprocessedKeys": unprocessed,
                },
                {"Responses": {TABLE: [{"PK": "p", "SK": "b"}]}, "UnprocessedKeys": {}},
            ]
        )
        with (
            mock.patch.object(self.client.dynamodb, "batch_get_item", fake),
            mock.patch("idp_common.dynamodb.client.time.sleep") as sleep,
        ):
            items = self.client.batch_get_items(
                [{"PK": "p", "SK": "a"}, {"PK": "p", "SK": "b"}]
            )
        assert sorted(i["SK"] for i in items) == ["a", "b"]
        assert fake.call_args_list[1].kwargs["RequestItems"] == unprocessed
        assert sleep.call_count == 1

    def test_exhausted_retries_raise(self):
        unprocessed = {TABLE: {"Keys": [{"PK": "p", "SK": "b"}]}}
        fake = mock.Mock(return_value={"Responses": {}, "UnprocessedKeys": unprocessed})
        with (
            mock.patch.object(self.client.dynamodb, "batch_get_item", fake),
            mock.patch("idp_common.dynamodb.client.time.sleep"),
        ):
            with pytest.raises(DynamoDBError) as exc:
                self.client.batch_get_items([{"PK": "p", "SK": "b"}], max_attempts=2)
        assert exc.value.error_code == "UnprocessedKeys"
        assert fake.call_count == 2

    def test_exhausted_retries_carry_the_rows_already_read(self):
        unprocessed = {TABLE: {"Keys": [{"PK": "p", "SK": "b"}]}}
        fake = mock.Mock(
            side_effect=[
                {
                    "Responses": {TABLE: [{"PK": "p", "SK": "a"}]},
                    "UnprocessedKeys": unprocessed,
                },
                {
                    "Responses": {TABLE: [{"PK": "p", "SK": "c"}]},
                    "UnprocessedKeys": unprocessed,
                },
            ]
        )
        keys = [{"PK": "p", "SK": s} for s in ("a", "b", "c")]
        with (
            mock.patch.object(self.client.dynamodb, "batch_get_item", fake),
            mock.patch("idp_common.dynamodb.client.time.sleep"),
        ):
            with pytest.raises(DynamoDBUnprocessedKeysError) as exc:
                self.client.batch_get_items(keys, max_attempts=2)
        assert isinstance(exc.value, DynamoDBError)
        assert exc.value.error_code == "UnprocessedKeys"
        # Rows from every attempt, not only the first or the last.
        assert exc.value.items == [{"PK": "p", "SK": "a"}, {"PK": "p", "SK": "c"}]

    def test_empty_keys_return_empty(self):
        assert self.client.batch_get_items([]) == []
