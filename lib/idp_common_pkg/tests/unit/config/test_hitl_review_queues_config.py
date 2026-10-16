# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`hitl.review_queues`: model, defaults, bounds and ConfigSchema parity (FR-1-FR-3)."""

from typing import Any, Iterator

import pytest
import yaml
from pydantic import ValidationError
from tests.unit.notifications._cfn_yaml import REPO_ROOT, load_cfn_yaml

from idp_common.config.models import (
    DEFAULT_REJECT_REASONS,
    REVIEW_QUEUE_MEMBER_GROUPS,
    HITLReviewQueuesConfig,
    IDPConfig,
    ReviewQueueDefinition,
)

_PKG_ROOT = REPO_ROOT / "lib" / "idp_common_pkg"
_BASE_CONFIDENCE = (
    _PKG_ROOT / "idp_common" / "config" / "system_defaults" / "base-confidence.yaml"
)
_UNIFIED_TEMPLATE = REPO_ROOT / "patterns" / "unified" / "template.yaml"
_PARENT_TEMPLATE = REPO_ROOT / "template.yaml"


def _queue(**kw) -> ReviewQueueDefinition:
    return ReviewQueueDefinition(**{"name": "q", **kw})


@pytest.mark.unit
class TestDefaults:
    def test_two_queues_with_their_groups(self):
        rq = IDPConfig().hitl.review_queues
        assert [q.name for q in rq.queues] == ["default", "escalation"]
        assert rq.queues[0].member_groups == ["QueueReviewer"]
        assert rq.queues[1].member_groups == ["Admin"]
        assert rq.enabled is False
        assert (rq.max_claims_per_reviewer, rq.lease_minutes) == (10, 30)
        assert rq.reject_reasons == list(DEFAULT_REJECT_REASONS)
        assert len(rq.reject_reasons) == 5
        assert rq.cross_field_problems() == []

    def test_system_defaults_yaml_equals_the_model(self):
        data = yaml.safe_load(_BASE_CONFIDENCE.read_text())
        from_yaml = HITLReviewQueuesConfig.model_validate(data["hitl"]["review_queues"])
        assert from_yaml.model_dump() == HITLReviewQueuesConfig().model_dump()


@pytest.mark.unit
class TestCoercion:
    @pytest.mark.parametrize("value", ["10", 10.0, "30.0"])
    def test_whole_numbers_parse(self, value):
        assert HITLReviewQueuesConfig(lease_minutes=value).lease_minutes == int(
            float(value)
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("max_claims_per_reviewer", 0),
            ("max_claims_per_reviewer", 51),
            ("lease_minutes", 4),
            ("lease_minutes", 481),
            ("lease_minutes", 10.5),
            ("lease_minutes", "abc"),
        ],
    )
    def test_out_of_bounds_rejected(self, field, value):
        with pytest.raises(ValidationError):
            HITLReviewQueuesConfig(**{field: value})

    @pytest.mark.parametrize("value", [101, -1, "abc"])
    def test_priority_bounds(self, value):
        with pytest.raises(ValidationError):
            _queue(priority=value)

    def test_stringified_enabled(self):
        assert HITLReviewQueuesConfig(enabled="true").enabled is True
        assert HITLReviewQueuesConfig(enabled="false").enabled is False


@pytest.mark.unit
class TestMembers:
    def test_unknown_group_rejected(self):
        with pytest.raises(ValidationError, match="member_groups"):
            _queue(member_groups=["Reviewer"])

    def test_groups_stripped_and_deduped(self):
        assert _queue(
            member_groups=["Admin ", "Admin", "QueueReviewer"]
        ).member_groups == [
            "Admin",
            "QueueReviewer",
        ]

    def test_email_normalised_and_deduped(self):
        q = _queue(member_emails=[" Alice@X.com ", "alice@x.com"])
        assert q.member_emails == ["alice@x.com"]

    def test_invalid_email_rejected(self):
        with pytest.raises(ValidationError):
            _queue(member_emails=["not-an-email"])

    def test_too_many_emails_rejected(self):
        with pytest.raises(ValidationError):
            _queue(member_emails=[f"u{i}@x.com" for i in range(101)])


@pytest.mark.unit
class TestShape:
    def test_twenty_one_queues_rejected(self):
        with pytest.raises(ValidationError):
            HITLReviewQueuesConfig(queues=[{"name": f"q{i}"} for i in range(21)])

    @pytest.mark.parametrize("name", ["", "-x", "a" * 65, "bad/name"])
    def test_bad_queue_name_rejected(self, name):
        with pytest.raises(ValidationError):
            _queue(name=name)

    def test_reject_reason_bounds(self):
        with pytest.raises(ValidationError):
            HITLReviewQueuesConfig(reject_reasons=[f"r{i}" for i in range(21)])
        with pytest.raises(ValidationError):
            HITLReviewQueuesConfig(reject_reasons=[])

    def test_queue_lookup_is_case_insensitive(self):
        assert HITLReviewQueuesConfig().queue("DEFAULT").name == "default"
        assert HITLReviewQueuesConfig().queue("nope") is None


@pytest.mark.unit
class TestCrossFieldProblemsDoNotBlockLoading:
    def test_duplicate_names_load_and_are_reported(self):
        cfg = IDPConfig.model_validate(
            {
                "hitl": {
                    "review_queues": {
                        "queues": [
                            {"name": "default"},
                            {"name": "Default"},
                            {"name": "escalation"},
                        ]
                    }
                }
            }
        )
        assert any(
            "more than once" in p for p in cfg.hitl.review_queues.cross_field_problems()
        )

    def test_missing_default_queue_loads_and_is_reported(self):
        cfg = IDPConfig.model_validate(
            {"hitl": {"review_queues": {"default_queue": "nope"}}}
        )
        assert any(
            "default_queue" in p for p in cfg.hitl.review_queues.cross_field_problems()
        )

    def test_same_default_and_escalation(self):
        rq = HITLReviewQueuesConfig(escalation_queue="Default")
        assert (
            "default_queue and escalation_queue must differ"
            in rq.cross_field_problems()
        )


def _find_hitl_schema(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        hitl = node.get("hitl")
        if isinstance(hitl, dict) and isinstance(hitl.get("properties"), dict):
            yield hitl
        for value in node.values():
            yield from _find_hitl_schema(value)
    elif isinstance(node, list):
        for value in node:
            yield from _find_hitl_schema(value)


def _review_queues_schema() -> dict:
    schemas = list(_find_hitl_schema(load_cfn_yaml(_UNIFIED_TEMPLATE)))
    assert schemas, "hitl ConfigSchema not found"
    return schemas[0]["properties"]["review_queues"]


@pytest.mark.unit
class TestSchemaParity:
    def test_bounds_and_defaults(self):
        props = _review_queues_schema()["properties"]
        fields = HITLReviewQueuesConfig.model_fields
        for name in ("max_claims_per_reviewer", "lease_minutes"):
            meta = {type(m).__name__: m for m in fields[name].metadata}
            assert props[name]["minimum"] == meta["Ge"].ge
            assert props[name]["maximum"] == meta["Le"].le
            assert props[name]["default"] == fields[name].default
            assert props[name]["type"] == "number"
        prio = props["queues"]["items"]["properties"]["priority"]
        pmeta = {
            type(m).__name__: m
            for m in ReviewQueueDefinition.model_fields["priority"].metadata
        }
        assert (prio["minimum"], prio["maximum"], prio["default"]) == (
            pmeta["Ge"].ge,
            pmeta["Le"].le,
            ReviewQueueDefinition.model_fields["priority"].default,
        )

    def test_member_group_enum(self):
        enum = _review_queues_schema()["properties"]["queues"]["items"]["properties"][
            "member_groups"
        ]["items"]["enum"]
        assert set(enum) == set(REVIEW_QUEUE_MEMBER_GROUPS)
        parent = load_cfn_yaml(_PARENT_TEMPLATE)
        group_names = {
            r["Properties"]["GroupName"]
            for r in parent["Resources"].values()
            if isinstance(r, dict) and r.get("Type") == "AWS::Cognito::UserPoolGroup"
        }
        assert set(enum) <= group_names
