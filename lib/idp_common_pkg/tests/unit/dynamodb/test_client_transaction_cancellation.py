# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`transact_write_items` cancellation surfaces per-item reason codes."""

import copy
import logging

import pytest
from moto import mock_aws

from idp_common.dynamodb import DynamoDBTransactionCanceledError
from idp_common.dynamodb.client import DynamoDBClient, DynamoDBError

from ._gsi_table import REGION, TABLE, make_table

LOGGER = "idp_common.dynamodb.client"


@pytest.mark.unit
@mock_aws
class TestTransactionCancellation:
    def setup_method(self, method):
        make_table()
        self.client = DynamoDBClient(table_name=TABLE, region=REGION)
        self.client.put_item({"PK": "p", "SK": "exists"})

    def test_second_condition_failure_gives_its_reason_code(self, caplog):
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        items = [
            {
                "Put": {
                    "Item": {"PK": "p", "SK": "new"},
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Put": {
                    "Item": {"PK": "p", "SK": "exists"},
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
        ]
        with pytest.raises(DynamoDBTransactionCanceledError) as exc:
            self.client.transact_write_items(items)
        assert isinstance(exc.value, DynamoDBError)
        assert exc.value.error_code == "TransactionCanceledException"
        assert exc.value.reason_code(1) == "ConditionalCheckFailed"
        assert exc.value.reason_code(0) == "None"
        assert exc.value.reason_code(7) == "None"
        records = [
            r for r in caplog.records if "transact_write_items" in r.getMessage()
        ]
        assert records and {r.levelno for r in records} == {logging.DEBUG}
        assert self.client.get_item({"PK": "p", "SK": "new"}) is None

    def test_condition_check_gets_table_name_and_payload_is_untouched(self):
        items = [
            {
                "ConditionCheck": {
                    "Key": {"PK": "p", "SK": "exists"},
                    "ConditionExpression": "attribute_exists(PK)",
                }
            },
            {"Put": {"Item": {"PK": "p", "SK": "other"}}},
        ]
        snapshot = copy.deepcopy(items)
        self.client.transact_write_items(items)
        assert items == snapshot, "the caller's list was mutated"
        assert self.client.get_item({"PK": "p", "SK": "other"}) is not None

    def test_condition_check_failure_is_reported(self):
        items = [
            {
                "ConditionCheck": {
                    "Key": {"PK": "p", "SK": "missing"},
                    "ConditionExpression": "attribute_exists(PK)",
                }
            },
            {"Put": {"Item": {"PK": "p", "SK": "other"}}},
        ]
        with pytest.raises(DynamoDBTransactionCanceledError) as exc:
            self.client.transact_write_items(items)
        assert exc.value.reason_code(0) == "ConditionalCheckFailed"


@pytest.mark.unit
class TestReasonCode:
    def test_missing_code_is_none(self):
        err = DynamoDBTransactionCanceledError("x", cancellation_reasons=[{}, None])  # type: ignore[list-item]
        assert err.reason_code(0) == "None"
        assert err.reason_code(1) == "None"
        assert err.reason_code(-1) == "None"
