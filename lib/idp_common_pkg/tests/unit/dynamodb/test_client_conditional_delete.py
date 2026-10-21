# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Conditional `delete_item`, and DEBUG logging of expected condition failures."""

import logging

import pytest
from moto import mock_aws

from idp_common.dynamodb.client import DynamoDBClient, DynamoDBError

from ._gsi_table import REGION, TABLE, make_table

LOGGER = "idp_common.dynamodb.client"


@pytest.mark.unit
@mock_aws
class TestConditionalDelete:
    def setup_method(self, method):
        make_table()
        self.client = DynamoDBClient(table_name=TABLE, region=REGION)
        self.client.put_item({"PK": "p", "SK": "a"})

    def test_failed_condition_raises_keeps_item_and_logs_debug(self, caplog):
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        with pytest.raises(DynamoDBError) as exc:
            self.client.delete_item(
                {"PK": "p", "SK": "a"},
                condition_expression="attribute_exists(#readAt)",
                expression_attribute_names={"#readAt": "ReadAt"},
            )
        assert exc.value.error_code == "ConditionalCheckFailedException"
        assert self.client.get_item({"PK": "p", "SK": "a"}) is not None
        records = [r for r in caplog.records if "delete_item" in r.getMessage()]
        assert records, "the condition failure was not logged"
        assert {r.levelno for r in records} == {logging.DEBUG}

    def test_true_condition_deletes_and_returns_old(self):
        resp = self.client.delete_item(
            {"PK": "p", "SK": "a"},
            condition_expression="attribute_exists(PK)",
            return_values="ALL_OLD",
        )
        assert resp["Attributes"]["SK"] == "a"
        assert self.client.get_item({"PK": "p", "SK": "a"}) is None

    def test_put_condition_failure_logs_debug(self, caplog):
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        with pytest.raises(DynamoDBError):
            self.client.put_item(
                {"PK": "p", "SK": "a"}, condition_expression="attribute_not_exists(PK)"
            )
        records = [r for r in caplog.records if "put_item" in r.getMessage()]
        assert records and {r.levelno for r in records} == {logging.DEBUG}

    def test_update_condition_failure_logs_debug(self, caplog):
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        with pytest.raises(DynamoDBError):
            self.client.update_item(
                {"PK": "p", "SK": "a"},
                "SET X = :x",
                expression_attribute_values={":x": 1},
                condition_expression="attribute_exists(NoSuchAttr)",
            )
        records = [r for r in caplog.records if "update_item" in r.getMessage()]
        assert records and {r.levelno for r in records} == {logging.DEBUG}

    def test_other_errors_still_log_error(self, caplog):
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        with pytest.raises(DynamoDBError) as exc:
            # Wrong key schema is a ValidationException, not a condition failure.
            self.client.delete_item({"NotAKey": "x"})
        assert exc.value.error_code != "ConditionalCheckFailedException"
        records = [r for r in caplog.records if "delete_item" in r.getMessage()]
        assert any(r.levelno == logging.ERROR for r in records)

    def test_get_item_options(self):
        self.client.put_item({"PK": "p", "SK": "b", "Other": "x"})
        item = self.client.get_item(
            {"PK": "p", "SK": "b"},
            consistent_read=True,
            projection_expression="#o",
            expression_attribute_names={"#o": "Other"},
        )
        assert item == {"Other": "x"}
