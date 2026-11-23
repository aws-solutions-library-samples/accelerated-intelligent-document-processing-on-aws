# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A resubmit after a skipped review is named from its idempotency token.

Bulk Skip All Reviews re-sends the same resubmit when a job step is retried; the
token makes the second send collapse into ``ExecutionAlreadyExists``. It only names
the execution: the run is never treated as a review finalize run.
"""

import json
from unittest.mock import patch

import pytest
from test_idempotent_start import (  # noqa: F401 - pytest fixture
    FINALIZE_TOKEN,
    MESSAGE_ID,
    _record,
    index_module,
)

RESUBMIT_TOKEN = "rsb-" + "fedcba9876543210" * 2


def _resubmit_record(token, message_id=MESSAGE_ID, finalize=None):
    record = _record(message_id=message_id)
    attributes = {
        "ResubmitIdempotencyToken": {"stringValue": token, "dataType": "String"}
    }
    if finalize:
        attributes["ReviewFinalizeToken"] = {
            "stringValue": finalize,
            "dataType": "String",
        }
    record["messageAttributes"] = attributes
    return record


class TestResubmitToken:
    def test_a_valid_token_is_returned(self, index_module):  # noqa: F811
        assert (
            index_module.resubmit_token_of(_resubmit_record(RESUBMIT_TOKEN))
            == RESUBMIT_TOKEN
        )

    @pytest.mark.parametrize("token", ["rsb-short", FINALIZE_TOKEN, 3])
    def test_an_invalid_token_is_ignored_with_a_warning(
        self,
        index_module,  # noqa: F811
        token,
    ):
        with patch.object(index_module.logger, "warning") as warning:
            assert index_module.resubmit_token_of(_resubmit_record(token)) is None
        warning.assert_called_once_with(
            "Ignoring an invalid ResubmitIdempotencyToken message attribute"
        )

    def test_the_same_token_names_both_sends_alike_and_adds_no_review_block(
        self,
        index_module,  # noqa: F811
    ):
        index_module.process_message(_resubmit_record(RESUBMIT_TOKEN, "m-1"))
        index_module.process_message(_resubmit_record(RESUBMIT_TOKEN, "m-2"))
        calls = index_module.sfn.start_execution.call_args_list
        names = [c.kwargs["name"] for c in calls]
        assert names[0] == names[1]
        assert names[0].endswith(RESUBMIT_TOKEN)
        assert "review" not in json.loads(calls[0].kwargs["input"])

    def test_a_finalize_token_wins_over_a_resubmit_token(
        self,
        index_module,  # noqa: F811
    ):
        index_module.process_message(
            _resubmit_record(RESUBMIT_TOKEN, finalize=FINALIZE_TOKEN)
        )
        kwargs = index_module.sfn.start_execution.call_args.kwargs
        assert kwargs["name"].endswith(FINALIZE_TOKEN)
        assert json.loads(kwargs["input"])["review"] == {
            "finalizeToken": FINALIZE_TOKEN
        }
