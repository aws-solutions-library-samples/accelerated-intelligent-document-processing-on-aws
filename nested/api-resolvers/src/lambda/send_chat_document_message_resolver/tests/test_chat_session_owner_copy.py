# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""This resolver's vendored ``chat_session_owner.py``, exercised from here.

The copy is byte-identical to ``idp_common/chat_session_owner.py``
(``scripts/tests/test_chat_session_owner_vendored.py``), and the coverage
ratchet measures each copy from its own resolver's tests. So the copy this
resolver ships is loaded by path and driven through every function, against a
table stub that answers or fails the way DynamoDB would.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

_COPY = next(
    p
    for p in (
        Path(__file__).resolve().parent / "chat_session_owner.py",
        Path(__file__).resolve().parent.parent / "chat_session_owner.py",
    )
    if p.is_file()
)
_spec = importlib.util.spec_from_file_location(
    f"_chat_session_owner_copy_{_COPY.parent.name}", _COPY
)
owner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(owner)  # type: ignore[union-attr]

pytestmark = pytest.mark.unit


def _error(code):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "Op")


LOST = "ConditionalCheckFailedException"


def _owners(item=None, *, update_error=None, put_error=None, after_put=None):
    table = MagicMock()
    answers = [{"Item": item} if item is not None else {}]
    if after_put is not None:
        answers.append({"Item": after_put})
    table.get_item.side_effect = lambda **_: (
        answers.pop(0) if len(answers) > 1 else answers[0]
    )
    if update_error:
        table.update_item.side_effect = _error(update_error)
    if put_error:
        table.put_item.side_effect = _error(put_error)
    return table


def _claim(table, sub="sub-a", *, allow_new=True, stored=False, earlier=False):
    return owner.claim_chat_session(
        table,
        "s1",
        sub,
        expires_after=100,
        allow_new=allow_new,
        has_stored_messages=lambda: stored,
        holds_earlier_record=lambda: earlier,
    )


def test_recorded_owner():
    assert owner.recorded_owner(_owners(), "s1") is None
    assert owner.recorded_owner(_owners({"sessionId": "s1"}), "s1") == ""
    assert owner.recorded_owner(_owners({"ownerSub": "sub-a"}), "s1") == "sub-a"


def test_an_unused_id_is_registered_on_a_write_path_only():
    table = _owners()
    assert _claim(table) is True
    item = table.put_item.call_args.kwargs["Item"]
    assert item[owner.OWNER_ATTRIBUTE] == "sub-a"
    assert item[owner.EXPIRES_ATTRIBUTE] == 100 + owner.EXPIRY_MARGIN_SECONDS
    assert _claim(_owners(), allow_new=False) is False
    assert _claim(_owners(), stored=True) is False


def test_an_earlier_record_registers_an_id_with_history():
    assert _claim(_owners(), stored=True, earlier=True, allow_new=False) is True


def test_the_owner_extends_and_another_user_is_refused():
    table = _owners({"ownerSub": "sub-a"})
    assert _claim(table) is True
    assert (
        table.update_item.call_args.kwargs["ConditionExpression"] == "#owner = :owner"
    )
    assert _claim(_owners({"ownerSub": "sub-a"}), sub="sub-b") is False


def test_empty_arguments_own_nothing():
    assert _claim(_owners(), sub="") is False


def test_a_lost_extend_is_not_owned_and_other_errors_raise():
    assert _claim(_owners({"ownerSub": "sub-a"}, update_error=LOST)) is False
    with pytest.raises(ClientError):
        _claim(_owners({"ownerSub": "sub-a"}, update_error="InternalServerError"))


def test_a_lost_registration_is_owned_only_if_the_winner_was_the_caller():
    assert _claim(_owners(put_error=LOST, after_put={"ownerSub": "sub-a"})) is True
    assert _claim(_owners(put_error=LOST, after_put={"ownerSub": "sub-b"})) is False
    with pytest.raises(ClientError):
        _claim(_owners(put_error="InternalServerError"))


def test_has_stored_messages():
    table = MagicMock()
    table.query.return_value = {"Items": [{"PK": "s1"}]}
    assert owner.has_stored_messages(table, "s1") is True
    table.query.return_value = {"Items": []}
    assert owner.has_stored_messages(table, "s1") is False


def test_holds_session_row():
    table = MagicMock()
    table.get_item.return_value = {"Item": {}}
    assert owner.holds_session_row(table, "user-a", "s1") is True
    table.get_item.return_value = {}
    assert owner.holds_session_row(table, "user-a", "s1") is False
    assert owner.holds_session_row(table, "", "s1") is False


def test_holds_data_results():
    assert owner.holds_data_results(_owners(), "") is False
    assert owner.holds_data_results(_owners(), "s1") is False
    assert owner.holds_data_results(_owners({"ownerSub": "sub-a"}), "s1") is False
    flagged = {"ownerSub": "sub-a", owner.DATA_RESULTS_ATTRIBUTE: 10}
    assert owner.holds_data_results(_owners(flagged), "s1") is True


def test_record_data_results():
    table = _owners()
    assert owner.record_data_results(table, "s1", "sub-a", 10) is True
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["ConditionExpression"] == "#owner = :owner"
    assert "if_not_exists" in kwargs["UpdateExpression"]
    assert owner.record_data_results(_owners(), "", "sub-a", 10) is False
    assert owner.record_data_results(_owners(), "s1", "", 10) is False
    assert (
        owner.record_data_results(_owners(update_error=LOST), "s1", "sub-a", 10)
        is False
    )
    with pytest.raises(ClientError):
        owner.record_data_results(
            _owners(update_error="InternalServerError"), "s1", "sub-a", 10
        )
