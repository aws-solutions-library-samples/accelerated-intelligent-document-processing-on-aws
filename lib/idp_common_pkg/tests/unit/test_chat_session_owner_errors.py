# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``idp_common.chat_session_owner``: error and empty-argument branches.

The moto suite (``test_chat_session_owner.py``) cannot make DynamoDB fail in the
middle of a write, so these use a table stub that raises the ``ClientError`` a
real call would. Two shapes matter: a lost condition, which answers "no", and
any other error, which must be raised so an unanswered question is never read
as "yes".
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from idp_common import chat_session_owner as owner_mod


def _error(code):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "UpdateItem")


def _table(*, item=None, update_error=None, put_error=None):
    table = MagicMock()
    table.get_item.return_value = {"Item": item} if item is not None else {}
    if update_error:
        table.update_item.side_effect = update_error
    if put_error:
        table.put_item.side_effect = put_error
    return table


def _claim(table, sub="sub-a"):
    return owner_mod.claim_chat_session(
        table,
        "s1",
        sub,
        expires_after=100,
        allow_new=True,
        has_stored_messages=lambda: False,
        holds_earlier_record=lambda: False,
    )


@pytest.mark.unit
class TestExtend:
    def test_a_row_taken_over_between_read_and_write_is_not_owned(self):
        table = _table(
            item={"sessionId": "s1", "ownerSub": "sub-a"},
            update_error=_error("ConditionalCheckFailedException"),
        )
        assert _claim(table) is False

    def test_any_other_error_is_raised(self):
        table = _table(
            item={"sessionId": "s1", "ownerSub": "sub-a"},
            update_error=_error("ProvisionedThroughputExceededException"),
        )
        with pytest.raises(ClientError):
            _claim(table)


@pytest.mark.unit
def test_register_raises_an_error_other_than_a_lost_condition():
    table = _table(put_error=_error("InternalServerError"))
    with pytest.raises(ClientError):
        _claim(table)


@pytest.mark.unit
class TestEmptyArguments:
    def test_no_user_holds_no_session_row(self):
        table = MagicMock()
        assert owner_mod.holds_session_row(table, "", "s1") is False
        table.get_item.assert_not_called()

    def test_no_session_holds_no_data_results(self):
        table = MagicMock()
        assert owner_mod.holds_data_results(table, "") is False
        table.get_item.assert_not_called()

    @pytest.mark.parametrize("sid,sub", [("", "sub-a"), ("s1", "")])
    def test_nothing_is_recorded_without_a_session_and_an_owner(self, sid, sub):
        table = MagicMock()
        assert owner_mod.record_data_results(table, sid, sub, 10) is False
        table.update_item.assert_not_called()


@pytest.mark.unit
class TestRecordDataResults:
    def test_a_lost_condition_records_nothing(self):
        table = _table(update_error=_error("ConditionalCheckFailedException"))
        assert owner_mod.record_data_results(table, "s1", "sub-a", 10) is False

    def test_any_other_error_is_raised(self):
        table = _table(update_error=_error("InternalServerError"))
        with pytest.raises(ClientError):
            owner_mod.record_data_results(table, "s1", "sub-a", 10)
