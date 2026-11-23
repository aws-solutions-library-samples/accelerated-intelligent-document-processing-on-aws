# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bulk Skip All Reviews / Send to review request contracts and job ids."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from idp_common.review_queues import keys
from idp_common.review_queues.bulk_models import (
    MAX_SELECTED_KEYS,
    SKIP_OUTCOMES,
    BulkMode,
    BulkOutcome,
    BulkSkipRequest,
    DocumentOutcome,
    SendToReviewRequest,
    outcome_counts,
)

REASON = "Duplicate document"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _profile(**kw):
    base = dict(
        mode="PROFILE",
        profile="claims",
        submitted_from=T0,
        submitted_to=T0 + timedelta(days=7),
        reason=REASON,
    )
    base.update(kw)
    return BulkSkipRequest(**base)


@pytest.mark.unit
class TestBulkSkipRequest:
    def test_selected_mode_dedupes_keys_in_order(self):
        req = BulkSkipRequest(
            mode="SELECTED", object_keys=["b", "a", "b"], reason=REASON
        )
        assert req.object_keys == ["b", "a"]
        assert req.mode is BulkMode.SELECTED and req.resubmit is True

    @pytest.mark.parametrize(
        "kw",
        [
            {"object_keys": []},
            {"object_keys": ["k"] * 0 + [str(i) for i in range(MAX_SELECTED_KEYS + 1)]},
            {"object_keys": ["a"], "profile": "p"},
            {"object_keys": [""]},
        ],
    )
    def test_selected_mode_refusals(self, kw):
        with pytest.raises(ValidationError):
            BulkSkipRequest(mode="SELECTED", reason=REASON, **kw)

    def test_profile_mode_accepts_a_range_up_to_365_days(self):
        assert _profile(submitted_to=T0 + timedelta(days=365)).profile == "claims"

    @pytest.mark.parametrize(
        "kw",
        [
            {"submitted_to": T0 + timedelta(days=366)},
            {"submitted_to": T0},
            {"submitted_from": datetime(2026, 1, 1)},
            {"submitted_from": None},
            {"profile": " "},
            {"object_keys": ["a"]},
        ],
    )
    def test_profile_mode_refusals(self, kw):
        with pytest.raises(ValidationError):
            _profile(**kw)

    @pytest.mark.parametrize("kw", [{"reason": "nope"}, {"note": "x" * 1001}])
    def test_reason_and_note_refusals(self, kw):
        with pytest.raises(ValidationError):
            _profile(**kw)

    def test_note_is_trimmed_and_blank_is_none(self):
        assert _profile(note="  hi  ").note == "hi"
        assert _profile(note="   ").note is None

    def test_client_request_id_shape(self):
        with pytest.raises(ValidationError):
            _profile(client_request_id="bad id!")
        assert _profile(client_request_id="a-1").client_request_id == "a-1"


@pytest.mark.unit
class TestSendToReviewRequest:
    def test_reason_must_be_a_send_reason(self):
        with pytest.raises(ValidationError):
            SendToReviewRequest(object_keys=["a"], reason=REASON)
        assert SendToReviewRequest(object_keys=["a"], reason="Spot check").reason


@pytest.mark.unit
class TestOutcomes:
    def test_outcome_values_are_pinned(self):
        # The web UI mirrors this list (bulk-outcomes.ts); change both together.
        assert [o.value for o in BulkOutcome] == [
            "SKIPPED",
            "SENT",
            "NOT_PERMITTED",
            "NOT_PENDING",
            "ALREADY_FINISHED",
            "HELD_BY_OTHER",
            "TEST_STUDIO_EXCLUDED",
            "NOT_FINISHED_PROCESSING",
            "ALREADY_IN_REVIEW",
            "REVIEW_QUEUE_OFF",
            "RESUBMIT_FAILED",
            "FAILED",
            "NOT_ATTEMPTED",
        ]

    def test_counts_include_zeros(self):
        counts = outcome_counts(
            [DocumentOutcome(object_key="a", outcome=BulkOutcome.SKIPPED)],
            SKIP_OUTCOMES,
        )
        assert len(counts) == len(SKIP_OUTCOMES)
        assert counts[0].count == 1 and all(c.count == 0 for c in counts[1:])


@pytest.mark.unit
class TestJobIds:
    def test_deterministic_with_a_client_request_id(self):
        a = keys.new_bulk_job_id("me@x", "req-1")
        assert a == keys.new_bulk_job_id("me@x", "req-1")
        assert a != keys.new_bulk_job_id("you@x", "req-1")
        assert keys.BULK_JOB_ID_RE.match(a)

    def test_random_without(self):
        assert keys.new_bulk_job_id("me") != keys.new_bulk_job_id("me")
        assert keys.BULK_JOB_ID_RE.match(keys.new_bulk_job_id("me"))
