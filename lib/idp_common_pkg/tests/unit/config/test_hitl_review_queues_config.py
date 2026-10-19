# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`hitl.review_queues`: model, defaults, bounds and ConfigSchema parity (FR-1-FR-3).

A profile's block only turns routing on and names the system-wide queue its
documents enter; the queues themselves (members, priority, reject reasons) are
defined in HITL › Queue Management and covered by
``tests/unit/review_queues/test_queue_definition_model.py``.
"""

from typing import Any, Iterator

import pytest
import yaml
from pydantic import ValidationError
from tests.unit.notifications._cfn_yaml import REPO_ROOT, load_cfn_yaml

from idp_common.config.models import (
    REVIEW_QUEUE_MEMBER_GROUPS,
    HITLReviewQueuesConfig,
    IDPConfig,
    review_queue_display_name,
)
from idp_common.review_queues.queue_defs import QueueDefinition

_PKG_ROOT = REPO_ROOT / "lib" / "idp_common_pkg"
_BASE_CONFIDENCE = (
    _PKG_ROOT / "idp_common" / "config" / "system_defaults" / "base-confidence.yaml"
)
_UNIFIED_TEMPLATE = REPO_ROOT / "patterns" / "unified" / "template.yaml"
_PARENT_TEMPLATE = REPO_ROOT / "template.yaml"


@pytest.mark.unit
class TestDefaults:
    def test_off_and_pointing_at_the_default_queue(self):
        rq = IDPConfig().hitl.review_queues
        assert rq.enabled is False
        assert rq.queue == "default"
        assert rq.cross_field_problems() == []

    def test_the_block_holds_only_the_routing_switch_and_queue_id(self):
        assert set(HITLReviewQueuesConfig.model_fields) == {"enabled", "queue"}

    def test_system_defaults_yaml_equals_the_model(self):
        data = yaml.safe_load(_BASE_CONFIDENCE.read_text())
        from_yaml = HITLReviewQueuesConfig.model_validate(data["hitl"]["review_queues"])
        assert from_yaml.model_dump() == HITLReviewQueuesConfig().model_dump()


@pytest.mark.unit
class TestQueueId:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (" Claims ", "claims"),
            ("ESCALATION", "escalation"),
            (None, "default"),
            ("", "default"),
            ("   ", "default"),
        ],
    )
    def test_normalised(self, value, expected):
        assert HITLReviewQueuesConfig(queue=value).queue == expected

    @pytest.mark.parametrize("value", ["-x", "a" * 65, "bad/name", ".dot"])
    def test_bad_queue_id_rejected(self, value):
        with pytest.raises(ValidationError):
            HITLReviewQueuesConfig(queue=value)

    def test_stringified_enabled(self):
        assert HITLReviewQueuesConfig(enabled="true").enabled is True
        assert HITLReviewQueuesConfig(enabled="false").enabled is False


@pytest.mark.unit
class TestLoading:
    def test_unknown_queue_id_loads_without_cross_field_problems(self):
        """Whether the queue exists is a warning at save time, not a load error."""
        cfg = IDPConfig.model_validate(
            {"hitl": {"review_queues": {"enabled": True, "queue": "nope"}}}
        )
        assert cfg.hitl.review_queues.queue == "nope"
        assert cfg.hitl.review_queues.cross_field_problems() == []

    def test_keys_of_the_former_per_profile_queue_list_are_ignored(self):
        cfg = IDPConfig.model_validate(
            {
                "hitl": {
                    "review_queues": {
                        "queues": [{"name": "claims"}],
                        "lease_minutes": 30,
                    }
                }
            }
        )
        assert cfg.hitl.review_queues.model_dump() == {
            "enabled": False,
            "queue": "default",
        }


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
    def test_fields_and_defaults(self):
        schema = _review_queues_schema()
        props = schema["properties"]
        fields = HITLReviewQueuesConfig.model_fields
        assert set(props) == set(fields)
        assert props["enabled"]["type"] == "boolean"
        assert props["queue"]["type"] == "string"
        for name in fields:
            assert props[name]["default"] == fields[name].default

    def test_block_is_admin_only_in_the_editor(self):
        assert _review_queues_schema()["adminOnly"] is True

    def test_member_groups_are_cognito_groups(self):
        assert set(QueueDefinition(id="q").member_groups) == set(
            REVIEW_QUEUE_MEMBER_GROUPS
        )
        parent = load_cfn_yaml(_PARENT_TEMPLATE)
        group_names = {
            r["Properties"]["GroupName"]
            for r in parent["Resources"].values()
            if isinstance(r, dict) and r.get("Type") == "AWS::Cognito::UserPoolGroup"
        }
        assert set(REVIEW_QUEUE_MEMBER_GROUPS) <= group_names


@pytest.mark.unit
class TestDisplayName:
    @pytest.mark.parametrize(
        ("queue_id", "expected"),
        [
            ("escalation", "Escalation"),
            ("claims_tier-2", "Claims Tier 2"),
            ("  high.value  ", "High Value"),
            ("default", "Default"),
            ("ACME", "ACME"),
        ],
    )
    def test_readable_name(self, queue_id, expected):
        assert review_queue_display_name(queue_id) == expected

    @pytest.mark.parametrize("queue_id", ["___", "", "-.-"])
    def test_an_id_of_only_separators_is_returned_unchanged(self, queue_id):
        assert review_queue_display_name(queue_id) == queue_id
