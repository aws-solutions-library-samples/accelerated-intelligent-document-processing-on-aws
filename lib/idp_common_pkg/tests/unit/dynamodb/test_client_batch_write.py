# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`DynamoDBClient.batch_write_items`: chunking, de-duplication and retries."""

from unittest import mock

import pytest
from moto import mock_aws

from idp_common.dynamodb.client import DynamoDBClient, DynamoDBError

from ._gsi_table import REGION, TABLE, make_table


@pytest.mark.unit
@mock_aws
class TestBatchWrite:
    def setup_method(self, method):
        make_table()
        self.client = DynamoDBClient(table_name=TABLE, region=REGION)

    def _count(self) -> int:
        return self.client.query_count(
            "PK = :pk", expression_attribute_values={":pk": "p"}
        )

    def test_sixty_deletes_take_three_calls(self):
        self.client.batch_write_items(
            put_items=[{"PK": "p", "SK": f"{i:03d}"} for i in range(60)]
        )
        assert self._count() == 60
        with mock.patch.object(
            self.client.dynamodb,
            "batch_write_item",
            wraps=self.client.dynamodb.batch_write_item,
        ) as spy:
            self.client.batch_write_items(
                delete_keys=[{"PK": "p", "SK": f"{i:03d}"} for i in range(60)]
            )
        assert spy.call_count == 3
        assert [len(c.kwargs["RequestItems"][TABLE]) for c in spy.call_args_list] == [
            25,
            25,
            10,
        ]
        assert self._count() == 0

    def test_duplicate_delete_keys_are_removed(self):
        self.client.put_item({"PK": "p", "SK": "a"})
        with mock.patch.object(
            self.client.dynamodb,
            "batch_write_item",
            wraps=self.client.dynamodb.batch_write_item,
        ) as spy:
            self.client.batch_write_items(
                delete_keys=[{"PK": "p", "SK": "a"}, {"PK": "p", "SK": "a"}]
            )
        assert len(spy.call_args.kwargs["RequestItems"][TABLE]) == 1
        assert self._count() == 0

    def test_unprocessed_items_are_retried(self):
        unprocessed = {TABLE: [{"DeleteRequest": {"Key": {"PK": "p", "SK": "x"}}}]}
        fake = mock.Mock(
            side_effect=[{"UnprocessedItems": unprocessed}, {"UnprocessedItems": {}}]
        )
        with (
            mock.patch.object(self.client.dynamodb, "batch_write_item", fake),
            mock.patch("idp_common.dynamodb.client.time.sleep") as sleep,
        ):
            self.client.batch_write_items(delete_keys=[{"PK": "p", "SK": "x"}])
        assert fake.call_count == 2
        assert fake.call_args_list[1].kwargs["RequestItems"] == unprocessed
        assert sleep.call_count == 1

    def test_exhausted_retries_raise(self):
        unprocessed = {TABLE: [{"DeleteRequest": {"Key": {"PK": "p", "SK": "x"}}}]}
        fake = mock.Mock(return_value={"UnprocessedItems": unprocessed})
        with (
            mock.patch.object(self.client.dynamodb, "batch_write_item", fake),
            mock.patch("idp_common.dynamodb.client.time.sleep"),
        ):
            with pytest.raises(DynamoDBError) as exc:
                self.client.batch_write_items(
                    delete_keys=[{"PK": "p", "SK": "x"}], max_attempts=3
                )
        assert exc.value.error_code == "UnprocessedItems"
        assert fake.call_count == 3

    def test_puts_and_deletes_in_one_call(self):
        self.client.put_item({"PK": "p", "SK": "old"})
        self.client.batch_write_items(
            put_items=[{"PK": "p", "SK": "new"}],
            delete_keys=[{"PK": "p", "SK": "old"}],
        )
        assert self.client.get_item({"PK": "p", "SK": "new"}) is not None
        assert self.client.get_item({"PK": "p", "SK": "old"}) is None

    def test_empty_input_is_a_no_op(self):
        with mock.patch.object(self.client.dynamodb, "batch_write_item") as spy:
            self.client.batch_write_items()
        spy.assert_not_called()
