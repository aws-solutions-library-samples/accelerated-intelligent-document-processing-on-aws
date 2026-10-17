# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The unknown-queue warning after an Admin changes ``hitl.review_queues``.

Queues are system-wide (HITL › Queue Management); a profile only names the queue
its documents enter. A save naming a queue no definition has is allowed, and the
resolver returns a warning built from :func:`unknown_queue_ids`.
"""

import logging

import pytest

from idp_common.review_queues.config_guard import (
    ReviewQueuesAdminGuard,
    unknown_queue_ids,
    warnings_for,
)


def _rq(queue="default", enabled=True):
    return {"enabled": enabled, "queue": queue}


@pytest.mark.unit
class TestUnknownQueueIds:
    def test_only_a_chosen_unknown_queue_is_reported(self):
        out = unknown_queue_ids(
            [("p", _rq("default"), _rq("claims"))], lambda: ["default", "escalation"]
        )
        assert out == ["claims"]

    def test_a_known_queue_is_not_reported(self):
        out = unknown_queue_ids(
            [("p", _rq("default"), _rq("escalation"))],
            lambda: ["default", "escalation"],
        )
        assert out == []

    def test_comparison_ignores_case_and_ids_are_reported_once(self):
        changes = [("a", _rq(), _rq("Claims")), ("b", _rq(), _rq("claims "))]
        assert unknown_queue_ids(changes, lambda: ["DEFAULT"]) == ["claims"]
        assert unknown_queue_ids(changes, lambda: ["CLAIMS"]) == []

    def test_definitions_are_not_read_when_nothing_was_chosen(self):
        calls = []

        def queue_ids():
            calls.append(1)
            return []

        assert unknown_queue_ids([], queue_ids) == []
        assert unknown_queue_ids([("p", _rq(), _rq(""))], queue_ids) == []
        assert calls == []

    def test_unreadable_definitions_warn_and_report_nothing(self, caplog):
        def queue_ids():
            raise RuntimeError("table gone")

        with caplog.at_level(logging.WARNING):
            out = unknown_queue_ids([("p", _rq(), _rq("claims"))], queue_ids)
        assert out == []
        assert "Queue definitions unreadable (RuntimeError)" in caplog.text

    def test_warning_text(self):
        assert warnings_for(["claims"]) == [
            "Queue 'claims' does not exist in HITL › Queue Management; this "
            "profile's documents go to the Default queue until it is created."
        ]


@pytest.mark.unit
class TestGuardRecordsAdminChanges:
    """What the guard hands :func:`unknown_queue_ids` (``(profile, before, after)``)."""

    def test_admin_change_is_recorded_with_the_dumped_block(self):
        from idp_common.config.models import IDPConfig

        guard = ReviewQueuesAdminGuard(is_admin=True, caller_email="a@x.com")
        after = IDPConfig.model_validate(
            {"hitl": {"review_queues": {"enabled": True, "queue": "claims"}}}
        )
        guard(IDPConfig(), after, "lending")
        assert guard.changes == [
            (
                "lending",
                {"enabled": False, "queue": "default"},
                {"enabled": True, "queue": "claims"},
            )
        ]
        assert unknown_queue_ids(guard.changes, lambda: ["default"]) == ["claims"]

    def test_unchanged_block_is_not_recorded(self):
        from idp_common.config.models import IDPConfig

        guard = ReviewQueuesAdminGuard(is_admin=False, caller_email="a@x.com")
        guard(IDPConfig(), IDPConfig(notes="other edit"), "lending")
        assert guard.changes == []
