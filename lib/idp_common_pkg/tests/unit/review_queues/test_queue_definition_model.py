# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bounds and normalisation of a system-wide queue definition (``QDEF`` item)."""

import pytest
from pydantic import ValidationError

from idp_common.review_queues.queue_defs import (
    DEFAULT_REJECT_REASONS,
    QueueDefinition,
    QueuesView,
    default_definitions,
)


def _queue(**kw) -> QueueDefinition:
    return QueueDefinition(**{"id": "q", **kw})


@pytest.mark.unit
class TestDefaults:
    def test_seeded_queues(self):
        defs = default_definitions()
        assert [(q.id, q.name, q.priority) for q in defs] == [
            ("default", "Default", 50),
            ("escalation", "Escalation", 90),
        ]
        assert all(q.reject_reasons == list(DEFAULT_REJECT_REASONS) for q in defs)
        assert len(DEFAULT_REJECT_REASONS) == 5

    def test_name_defaults_to_a_display_form_of_the_id(self):
        assert _queue(id="loan_review").name == "Loan Review"

    def test_item_round_trip(self):
        q = _queue(
            id="claims",
            priority=70,
            checklist=["a"],
            reject_reasons=["r"],
            time_limit_minutes=15,
            eligible_annotators=["a@x.com"],
        )
        assert QueueDefinition.from_item(q.to_item()) == q


@pytest.mark.unit
class TestBounds:
    @pytest.mark.parametrize("value", [101, -1, "abc"])
    def test_priority(self, value):
        with pytest.raises(ValidationError):
            _queue(priority=value)

    @pytest.mark.parametrize("value", [0, 10081])
    def test_time_limit(self, value):
        with pytest.raises(ValidationError):
            _queue(time_limit_minutes=value)

    @pytest.mark.parametrize("value", ["", "-x", "a" * 65, "bad/name", "has space"])
    def test_bad_id_rejected(self, value):
        with pytest.raises(ValidationError):
            _queue(id=value)

    def test_reject_reason_bounds(self):
        with pytest.raises(ValidationError):
            _queue(reject_reasons=[f"r{i}" for i in range(21)])
        with pytest.raises(ValidationError):
            _queue(reject_reasons=[])


@pytest.mark.unit
class TestMembers:
    def test_member_groups_are_annotator_and_admin(self):
        assert _queue().member_groups == ["Annotator", "Admin"]

    def test_emails_normalised_and_deduped(self):
        q = _queue(eligible_annotators=[" Alice@X.com ", "alice@x.com"])
        assert q.eligible_annotators == ["alice@x.com"]
        assert q.member_emails == ["alice@x.com"]

    def test_invalid_email_rejected(self):
        with pytest.raises(ValidationError, match="eligible_annotators"):
            _queue(eligible_annotators=["not-an-email"])

    def test_too_many_emails_rejected(self):
        with pytest.raises(ValidationError):
            _queue(eligible_annotators=[f"u{i}@x.com" for i in range(201)])


@pytest.mark.unit
class TestQueuesView:
    def test_lookup_is_case_insensitive(self):
        view = QueuesView(default_definitions())
        assert view.queue("DEFAULT").id == "default"
        assert view.queue("nope") is None
        assert view.queue(None) is None

    def test_reject_reasons_come_from_the_default_queue(self):
        view = QueuesView([_queue(id="default", reject_reasons=["only"])])
        assert view.reject_reasons == ["only"]
        assert QueuesView([]).reject_reasons == list(DEFAULT_REJECT_REASONS)
