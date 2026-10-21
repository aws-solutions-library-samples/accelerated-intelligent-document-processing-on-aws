# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bounds and normalisation of a system-wide queue definition (``QDEF`` item)."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from idp_common.review_queues.queue_defs import (
    DEFAULT_REJECT_REASONS,
    QueueDefinition,
    QueuesView,
    default_definitions,
)
from tests.unit.notifications._cfn_yaml import REPO_ROOT, load_cfn_yaml


def _queue(**kw) -> QueueDefinition:
    return QueueDefinition(**{"id": "q", **kw})


@pytest.mark.unit
class TestDefaults:
    def test_only_the_default_queue_is_seeded_and_it_starts_disabled(self):
        defs = default_definitions()
        assert [(q.id, q.name, q.priority, q.enabled) for q in defs] == [
            ("default", "Default", 50, False),
        ]
        assert defs[0].confidence_threshold == 0.8
        assert defs[0].reject_reasons == list(DEFAULT_REJECT_REASONS)
        assert len(DEFAULT_REJECT_REASONS) == 5

    def test_a_new_queue_is_enabled_with_the_default_threshold(self):
        q = _queue()
        assert (q.enabled, q.confidence_threshold) == (True, 0.8)

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
            confidence_threshold=0.65,
            enabled=False,
        )
        item = q.to_item()
        assert (item["ConfidenceThreshold"], item["Enabled"]) == (
            Decimal("0.65"),
            False,
        )
        assert QueueDefinition.from_item(item) == q

    def test_a_stored_item_without_the_new_fields(self):
        """Stored before the switch existed: Default reads as off, others as on."""
        for queue_id, enabled in (("default", False), ("claims", True)):
            item = _queue(id=queue_id).to_item()
            del item["ConfidenceThreshold"], item["Enabled"]
            q = QueueDefinition.from_item(item)
            assert (q.enabled, q.confidence_threshold) == (enabled, 0.8)


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

    @pytest.mark.parametrize("value", [-0.01, 1.01, "high"])
    def test_confidence_threshold(self, value):
        with pytest.raises(ValidationError):
            _queue(confidence_threshold=value)

    def test_reject_reason_bounds(self):
        with pytest.raises(ValidationError):
            _queue(reject_reasons=[f"r{i}" for i in range(21)])
        with pytest.raises(ValidationError):
            _queue(reject_reasons=[])


@pytest.mark.unit
class TestMembers:
    def test_member_groups_are_annotator_and_admin(self):
        assert _queue().member_groups == ["Annotator", "Admin"]

    def test_member_groups_are_cognito_groups(self):
        parent = load_cfn_yaml(REPO_ROOT / "template.yaml")
        group_names = {
            r["Properties"]["GroupName"]
            for r in parent["Resources"].values()
            if isinstance(r, dict) and r.get("Type") == "AWS::Cognito::UserPoolGroup"
        }
        assert set(_queue().member_groups) <= group_names

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
