# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The sweeper abandons a Send to review item stuck HOLDING (moto)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from idp_common.review_queues.models import SweepReport
from idp_common.review_queues.sweeper import ReviewSweeper

from .test_send_to_review import _row, _send, _tracking, env  # noqa: F401


def _sweeper(env, document_service):  # noqa: F811
    return ReviewSweeper(
        env.store,
        sfn_client=MagicMock(),
        lambda_client=MagicMock(),
        tracker_function_name="tracker",
        state_machine_arn="arn:aws:states:us-east-1:123456789012:stateMachine:sm",
        finalize_sender=MagicMock(),
        document_service=document_service,
        directory=MagicMock(),
        notifier=MagicMock(),
        s3=env.s3,
        output_bucket="out",
        metrics=MagicMock(),
    )


def _stuck_manual_item(env):  # noqa: F811
    """Send, then put the item back to HOLDING as if activation never happened."""
    _row(env, "m.pdf", HITLStatus="Review Completed")
    _send(env, "m.pdf")
    item = env.store.get_item("m.pdf")
    held = item.model_copy(update={"state": "HOLDING", "version": item.version + 1})
    env.store.replace_item(held, expected_version=item.version)
    return env.store.get_item("m.pdf")


@pytest.mark.unit
class TestManualHolding:
    def test_a_stuck_manual_item_is_abandoned(self, env):  # noqa: F811
        item = _stuck_manual_item(env)
        documents = SimpleNamespace(client=SimpleNamespace(table=env.table))
        sweeper = _sweeper(env, documents)
        report = SweepReport()
        sweeper._visit_holding(item, report)
        assert env.store.get_item("m.pdf") is None
        row = _tracking(env, "m.pdf")
        assert row["HITLStatus"] == "Review Completed" and "ReviewCycleId" not in row
        assert report.holds_reaped == 1
        sweeper.sfn.describe_execution.assert_not_called()

    def test_a_phase1_holding_item_keeps_the_old_path(self, env):  # noqa: F811
        item = _stuck_manual_item(env).model_copy(
            update={"origin": None, "phase1_execution_arn": None}
        )
        documents = SimpleNamespace(client=SimpleNamespace(table=env.table))
        sweeper = _sweeper(env, documents)
        sweeper._visit_holding(item, SweepReport())
        # The phase-1 path deletes the item and leaves the tracking row alone.
        assert env.store.get_item("m.pdf") is None
        assert _tracking(env, "m.pdf")["HITLStatus"] == "in_review"
