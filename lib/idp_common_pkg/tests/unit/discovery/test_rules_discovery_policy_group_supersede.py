# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Superseding inside a Policy Discovery policy group (#608, step 2).

Like ``test_rules_discovery_policy_group.py``, these drive the real save path —
``RulesDiscovery.discovery_rules_from_document`` reading the document from S3,
then ``_save_rules_to_config`` reading and writing the profile through a real
``ConfigurationManager`` — against moto, and only the model call is replaced.

What is pinned:

* ``replace`` makes an upload's rules the group's whole ruleset, and the class
  keeps its name, description, regex and group;
* ``supersede`` removes exactly the rules the uploader named — a rider rule with a
  superseded rule's name takes that name, every other rider rule appends as before;
* a replace or supersede with nothing to act on (no group, a named rule the group
  does not hold, an upload that produced no rules) fails and writes nothing;
* every save records the upload, the rules it supplied and the rules it superseded
  with the upload that had supplied each, and ``policy_rule_sources`` reads back
  who supplied each current rule;
* rule validation still reads a class that carries the record.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import boto3
import pytest

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.models import IDPConfig
from idp_common.discovery import rules_discovery as rd
from idp_common.discovery.rules_discovery import (
    PolicyGroupChangeRefused,
    RulesDiscovery,
    check_policy_group_change,
    normalize_policy_group_change,
    policy_rule_sources,
)
from tests.unit.discovery.test_rules_discovery_policy_group import (
    DOCS,
    PROFILE,
    TABLE,
    _chapter,
    _load_processor,
    _policy_classes,
    _save_regex,
    moto_profile,
)

UPLOADS_KEY = "x-aws-idp-policy-uploads"
GROUP = "NCCI Manual"


@pytest.fixture
def aws(monkeypatch):
    with moto_profile(monkeypatch) as s3:
        yield s3


def _run(
    s3,
    monkeypatch,
    key: str,
    extracted: List[Dict],
    mode: Optional[str] = None,
    superseded: Optional[List[str]] = None,
    upload_id: Optional[str] = None,
    group: Optional[str] = GROUP,
) -> Dict[str, Any]:
    """One discovery job into ``group``: the model returns ``extracted``."""
    s3.put_object(Bucket=DOCS, Key=key, Body=b"%PDF-1.4 policy")
    d = RulesDiscovery(input_bucket=DOCS, input_prefix=key, config=IDPConfig())
    d.config_manager = ConfigurationManager()
    d.version = PROFILE
    monkeypatch.setattr(d, "_extract_rules", lambda *_a: [dict(c) for c in extracted])
    return d.discovery_rules_from_document(
        DOCS,
        key,
        policy_group=group,
        policy_group_mode=mode,
        superseded_rules=superseded,
        upload_id=upload_id,
    )


def _texts(pc: Dict[str, Any]) -> Dict[str, str]:
    return {n: d["description"] for n, d in pc["rule_properties"].items()}


def _base(aws, monkeypatch) -> None:
    """Two chapters already in the group, uploaded as jobs ``u1`` and ``u2``."""
    _run(
        aws,
        monkeypatch,
        "document/ch1.pdf",
        _chapter({"limit": "≤ 3?", "modifier": "M?"}),
        upload_id="u1",
    )
    _run(
        aws, monkeypatch, "document/ch2.pdf", _chapter({"other": "O?"}), upload_id="u2"
    )


@pytest.mark.unit
class TestReplaceTheGroupsRules:
    def test_the_upload_becomes_the_whole_ruleset_and_the_class_is_kept(
        self, aws, monkeypatch
    ):
        _base(aws, monkeypatch)
        _save_regex("NCCI_Manual", "(?i).*ncci.*")
        result = _run(
            aws,
            monkeypatch,
            "document/amended.pdf",
            _chapter({"limit": "≤ 5?", "new_rule": "N?"}),
            mode="replace",
            upload_id="u3",
        )
        (pc,) = _policy_classes()
        assert _texts(pc) == {"limit": "≤ 5?", "new_rule": "N?"}
        assert pc["x-aws-idp-policy-type"] == "NCCI_Manual"
        assert pc["x-aws-idp-policy-group"] == GROUP
        assert pc["x-aws-idp-document-name-regex"] == "(?i).*ncci.*"
        assert pc["description"] == "billing_rules from the manual"
        # The record says whose rules were replaced.
        last = pc[UPLOADS_KEY][-1]
        assert last["uploadId"] == "u3"
        assert last["document"] == "amended.pdf"
        assert last["mode"] == "replace"
        assert last["rules"] == ["limit", "new_rule"]
        assert last["superseded"] == [
            {"rule": "limit", "uploadId": "u1", "document": "ch1.pdf"},
            {"rule": "modifier", "uploadId": "u1", "document": "ch1.pdf"},
            {"rule": "other", "uploadId": "u2", "document": "ch2.pdf"},
        ]
        assert result["groupChange"]["mode"] == "replace"
        assert [s["rule"] for s in result["groupChange"]["superseded"]] == [
            "limit",
            "modifier",
            "other",
        ]
        # A replaced name is not a collision: no `_2` warning.
        assert not any("→" in w for w in result["warnings"])

    def test_replacing_a_group_that_does_not_exist_writes_nothing(
        self, aws, monkeypatch
    ):
        with pytest.raises(Exception, match="does not exist"):
            _run(aws, monkeypatch, "document/a.pdf", _chapter({"r": "?"}), "replace")
        assert _policy_classes() == []

    def test_an_upload_that_produced_no_rules_replaces_nothing(self, aws, monkeypatch):
        _base(aws, monkeypatch)
        before = _policy_classes()
        with pytest.raises(PolicyGroupChangeRefused, match="produced no rules"):
            _run(aws, monkeypatch, "document/empty.pdf", _chapter({}), "replace")
        assert _policy_classes() == before


@pytest.mark.unit
class TestSupersedeDeclaredRules:
    def test_a_same_named_rider_rule_takes_the_superseded_rules_place(
        self, aws, monkeypatch
    ):
        _base(aws, monkeypatch)
        result = _run(
            aws,
            monkeypatch,
            "document/rider.pdf",
            _chapter({"limit": "≤ 5?", "rider_extra": "E?"}),
            mode="supersede",
            superseded=["limit"],
            upload_id="r1",
        )
        (pc,) = _policy_classes()
        assert _texts(pc) == {
            "modifier": "M?",
            "other": "O?",
            "limit": "≤ 5?",
            "rider_extra": "E?",
        }
        assert pc[UPLOADS_KEY][-1]["superseded"] == [
            {"rule": "limit", "uploadId": "u1", "document": "ch1.pdf"}
        ]
        assert not any("→" in w for w in result["warnings"])

    def test_a_differently_named_rider_rule_supersedes_the_declared_one(
        self, aws, monkeypatch
    ):
        """The amendment reworded the rule; the uploader says which one it replaces."""
        _base(aws, monkeypatch)
        _run(
            aws,
            monkeypatch,
            "document/rider.pdf",
            _chapter({"visit_limit_amended": "≤ 5?"}),
            mode="supersede",
            superseded=["limit", "modifier"],
            upload_id="r1",
        )
        (pc,) = _policy_classes()
        assert _texts(pc) == {"other": "O?", "visit_limit_amended": "≤ 5?"}

    def test_an_undeclared_same_named_rule_still_appends_beside_it(
        self, aws, monkeypatch
    ):
        """Only declared rules are superseded: a matching name is not a declaration."""
        _base(aws, monkeypatch)
        result = _run(
            aws,
            monkeypatch,
            "document/rider.pdf",
            _chapter({"limit": "≤ 5?", "other": "O2?"}),
            mode="supersede",
            superseded=["limit"],
        )
        (pc,) = _policy_classes()
        assert pc["rule_properties"]["other"]["description"] == "O?"
        assert pc["rule_properties"]["other_2"]["description"] == "O2?"
        assert pc["rule_properties"]["limit"]["description"] == "≤ 5?"
        assert any("other → other_2" in w for w in result["warnings"])
        # The record names the rules as saved, so a reader finds `other_2`.
        assert pc[UPLOADS_KEY][-1]["rules"] == ["limit", "other_2"]

    def test_a_declared_rule_the_group_no_longer_holds_fails_and_writes_nothing(
        self, aws, monkeypatch
    ):
        """The resolver checked; the group changed before the job saved."""
        _base(aws, monkeypatch)
        before = _policy_classes()
        with pytest.raises(Exception, match="no rule named 'gone'"):
            _run(
                aws,
                monkeypatch,
                "document/rider.pdf",
                _chapter({"limit": "≤ 5?"}),
                mode="supersede",
                superseded=["limit", "gone"],
            )
        assert _policy_classes() == before

    def test_a_rider_rule_can_itself_be_superseded_and_is_attributed_to_the_rider(
        self, aws, monkeypatch
    ):
        _base(aws, monkeypatch)
        _run(
            aws,
            monkeypatch,
            "document/rider1.pdf",
            _chapter({"limit": "≤ 5?"}),
            mode="supersede",
            superseded=["limit"],
            upload_id="r1",
        )
        _run(
            aws,
            monkeypatch,
            "document/rider2.pdf",
            _chapter({"limit": "≤ 7?"}),
            mode="supersede",
            superseded=["limit"],
            upload_id="r2",
        )
        (pc,) = _policy_classes()
        assert pc[UPLOADS_KEY][-1]["superseded"] == [
            {"rule": "limit", "uploadId": "r1", "document": "rider1.pdf"}
        ]
        assert [e["uploadId"] for e in pc[UPLOADS_KEY]] == ["u1", "u2", "r1", "r2"]

    def test_a_held_lease_blocks_a_supersede_without_writing(self, aws, monkeypatch):
        _base(aws, monkeypatch)
        before = _policy_classes()
        boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE).put_item(
            Item={
                "Configuration": f"PolicyDiscoverySaveLease#{PROFILE}",
                "LeaseToken": "someone-else",
                "LeaseExpiresAt": int(time.time()) + 600,
            }
        )
        monkeypatch.setattr(rd, "POLICY_SAVE_LEASE_WAIT_SECONDS", 0.5)
        with pytest.raises(Exception, match="held by another"):
            _run(
                aws,
                monkeypatch,
                "document/rider.pdf",
                _chapter({"limit": "≤ 5?"}),
                mode="supersede",
                superseded=["limit"],
            )
        assert _policy_classes() == before


@pytest.mark.unit
class TestProvenance:
    def test_sources_follow_appends_and_supersessions(self, aws, monkeypatch):
        _base(aws, monkeypatch)
        _run(
            aws,
            monkeypatch,
            "document/rider.pdf",
            _chapter({"limit": "≤ 5?"}),
            mode="supersede",
            superseded=["limit"],
            upload_id="r1",
        )
        # A rule the user adds by hand in the Policy Schema tab has no upload.
        manager = ConfigurationManager()
        config = manager.get_configuration(CONFIG_TYPE_CONFIG, version=PROFILE)
        config.policy_classes[0]["rule_properties"]["by_hand"] = {
            "type": "string",
            "description": "H?",
        }
        manager.save_configuration(CONFIG_TYPE_CONFIG, config, version=PROFILE)

        (pc,) = _policy_classes()
        sources = policy_rule_sources(pc)
        assert {n: s["uploadId"] for n, s in sources.items()} == {
            "limit": "r1",
            "modifier": "u1",
            "other": "u2",
        }
        assert sources["limit"]["document"] == "rider.pdf"
        assert sources["limit"]["savedAt"]

    def test_a_superseded_rule_re_added_by_hand_is_not_attributed_to_its_old_upload(
        self, aws, monkeypatch
    ):
        _base(aws, monkeypatch)
        _run(
            aws,
            monkeypatch,
            "document/rider.pdf",
            _chapter({"visit_limit": "≤ 5?"}),
            mode="supersede",
            superseded=["limit"],
            upload_id="r1",
        )
        manager = ConfigurationManager()
        config = manager.get_configuration(CONFIG_TYPE_CONFIG, version=PROFILE)
        config.policy_classes[0]["rule_properties"]["limit"] = {
            "type": "string",
            "description": "typed back in?",
        }
        manager.save_configuration(CONFIG_TYPE_CONFIG, config, version=PROFILE)
        (pc,) = _policy_classes()
        sources = policy_rule_sources(pc)
        assert "limit" not in sources
        assert sources["visit_limit"]["uploadId"] == "r1"

    def test_an_ungrouped_upload_records_itself_on_its_class(self, aws, monkeypatch):
        _run(
            aws,
            monkeypatch,
            "document/solo.pdf",
            _chapter({"r1": "1?", "r2": "2?"}),
            upload_id="j9",
            group=None,
        )
        (pc,) = _policy_classes()
        (entry,) = pc[UPLOADS_KEY]
        assert entry["uploadId"] == "j9"
        assert entry["document"] == "solo.pdf"
        assert entry["mode"] == "append"
        assert entry["rules"] == ["r1", "r2"]
        assert entry["superseded"] == []

    def test_a_malformed_record_is_read_as_no_record(self):
        pc = {
            "rule_properties": {"a": {}, "b": {}},
            UPLOADS_KEY: ["junk", {"rules": ["a"], "superseded": ["junk"]}],
        }
        assert set(policy_rule_sources(pc)) == {"a"}
        assert policy_rule_sources({"rule_properties": {"a": {}}, UPLOADS_KEY: 3}) == {}

    def test_a_recorded_rule_the_class_no_longer_holds_has_no_source(self):
        """Deleted by hand in the Policy Schema tab: still in the record, not in the class."""
        pc = {
            "rule_properties": {"kept": {}},
            UPLOADS_KEY: [{"uploadId": "u1", "rules": ["kept", "deleted"]}],
        }
        assert set(policy_rule_sources(pc)) == {"kept"}

    def test_rule_validation_still_reads_a_class_carrying_the_record(
        self, aws, monkeypatch
    ):
        from idp_common.rule_validation.service import RuleValidationService

        _base(aws, monkeypatch)
        config = ConfigurationManager().get_configuration(
            CONFIG_TYPE_CONFIG, version=PROFILE
        )
        metadata = RuleValidationService._get_rule_metadata(
            None,  # type: ignore[arg-type] - reads only its arguments
            config.model_dump(),
            "NCCI_Manual",
        )
        assert sorted(m["description"] for m in metadata) == ["M?", "O?", "≤ 3?"]


@pytest.mark.unit
class TestGroupChangeArguments:
    @pytest.mark.parametrize(
        "group, mode, names, expected",
        [
            (None, None, None, ("append", [])),
            (None, "", [], ("append", [])),
            ("G", "append", None, ("append", [])),
            ("G", "replace", None, ("replace", [])),
            ("G", "supersede", ["a", "b", "a"], ("supersede", ["a", "b"])),
        ],
    )
    def test_accepted(self, group, mode, names, expected):
        assert normalize_policy_group_change(group, mode, names) == expected

    @pytest.mark.parametrize(
        "group, mode, names, match",
        [
            ("G", "merge", None, "must be one of"),
            ("G", 3, None, "must be one of"),
            (None, "replace", None, "needs a policyGroup"),
            (None, "supersede", ["a"], "needs a policyGroup"),
            ("G", "supersede", None, "needs supersededRules"),
            ("G", "supersede", [], "needs supersededRules"),
            ("G", "replace", ["a"], "applies only to policyGroupMode 'supersede'"),
            ("G", None, ["a"], "applies only to policyGroupMode 'supersede'"),
            ("G", "supersede", "a", "must be a list"),
            ("G", "supersede", ["a", ""], "must be a list"),
            ("G", "supersede", ["a", 1], "must be a list"),
        ],
    )
    def test_refused(self, group, mode, names, match):
        with pytest.raises(ValueError, match=match):
            normalize_policy_group_change(group, mode, names)

    def test_rule_names_are_matched_exactly(self):
        classes = [
            {"x-aws-idp-policy-group": "G", "rule_properties": {"Visit_Limit": {}}}
        ]
        check_policy_group_change(classes, "g", "supersede", ["Visit_Limit"])
        with pytest.raises(ValueError, match="no rule named 'visit_limit'"):
            check_policy_group_change(classes, "G", "supersede", ["visit_limit"])

    def test_a_rule_held_only_by_another_class_is_not_the_groups(self):
        classes = [
            {"x-aws-idp-policy-group": "G", "rule_properties": {"mine": {}}},
            {"x-aws-idp-policy-type": "other", "rule_properties": {"theirs": {}}},
        ]
        with pytest.raises(PolicyGroupChangeRefused, match="no rule named 'theirs'"):
            check_policy_group_change(classes, "G", "supersede", ["theirs"])

    def test_append_needs_no_existing_group(self):
        check_policy_group_change([], "G", "append", [])

    def test_two_classes_carrying_the_group_are_refused(self):
        classes = [
            {"x-aws-idp-policy-group": "G", "x-aws-idp-policy-type": "a"},
            {"x-aws-idp-policy-group": "g", "x-aws-idp-policy-type": "b"},
        ]
        # Typed, so the discovery job treats it as final rather than retrying it.
        with pytest.raises(
            PolicyGroupChangeRefused, match="carried by 2 policy classes"
        ):
            check_policy_group_change(classes, "G", "replace", [])


@pytest.mark.unit
class TestTheDiscoveryJobCarriesTheGroupChange:
    """The queue message's mode and superseded rules reach the save and the status."""

    def _handle(self, monkeypatch, message: Dict[str, Any], change: Dict[str, Any]):
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        module = _load_processor(monkeypatch)
        calls: Dict[str, Any] = {}
        statuses: List[Dict[str, Any]] = []

        class FakeRulesDiscovery:
            version = PROFILE

            def __init__(self, **kwargs):
                pass

            def discovery_rules_from_document(self, **kwargs):
                calls["run"] = kwargs
                return {
                    "status": "SUCCESS",
                    "rules": [
                        {
                            "x-aws-idp-policy-type": "NCCI_Manual",
                            "rule_properties": {"limit": {}, "extra": {}},
                        }
                    ],
                    "warnings": [],
                    "groupChange": change,
                }

        monkeypatch.setattr(module, "RulesDiscovery", FakeRulesDiscovery)
        monkeypatch.setattr(module, "_wait_for_s3_object", lambda *a, **k: None)
        monkeypatch.setattr(
            module, "update_job_status", lambda *a, **k: statuses.append(k)
        )
        body = {
            "jobId": "job-7",
            "documentKey": "document/rider.pdf",
            "bucket": DOCS,
            "version": PROFILE,
            "discoveryType": "rules",
            "policyGroup": GROUP,
            **message,
        }
        result = module.handler(
            {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}, None
        )
        assert result["batchItemFailures"] == []
        return calls["run"], statuses[-1]["status_message"]

    def test_a_supersede_message_reaches_the_save_and_names_what_it_superseded(
        self, monkeypatch
    ):
        run, status = self._handle(
            monkeypatch,
            {"policyGroupMode": "supersede", "supersededRules": ["limit"]},
            {
                "mode": "supersede",
                "rules": ["limit", "extra"],
                "superseded": [
                    {"rule": "limit", "uploadId": "u1", "document": "ch1.pdf"}
                ],
            },
        )
        assert run["policy_group_mode"] == "supersede"
        assert run["superseded_rules"] == ["limit"]
        assert run["upload_id"] == "job-7"
        assert "Superseded as declared: limit (from ch1.pdf)." in status

    def test_a_replace_message_says_how_many_rules_it_replaced(self, monkeypatch):
        run, status = self._handle(
            monkeypatch,
            {"policyGroupMode": "replace"},
            {
                "mode": "replace",
                "rules": ["limit", "extra"],
                "superseded": [
                    {"rule": f"r{n}", "uploadId": "u1", "document": None}
                    for n in range(12)
                ],
            },
        )
        assert run["policy_group_mode"] == "replace"
        assert run["superseded_rules"] is None
        assert "replaced the 12 rules of policy group 'NCCI Manual'" in status
        assert "r9 and 2 more." in status
        assert "r10" not in status

    def test_a_repeated_delivery_says_nothing_was_saved_again(self, monkeypatch):
        """Not "replaced the N rules": this delivery changed nothing."""
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        module = _load_processor(monkeypatch)
        statuses: List[Dict[str, Any]] = []

        class FakeRulesDiscovery:
            version = PROFILE

            def __init__(self, **kwargs):
                pass

            def discovery_rules_from_document(self, **kwargs):
                return {
                    "status": "SUCCESS",
                    "rules": [
                        {
                            "x-aws-idp-policy-type": "NCCI_Manual",
                            "rule_properties": {"r": {}},
                        }
                    ],
                    "warnings": ["already saved ..."],
                    "groupChange": {
                        "mode": "replace",
                        "rules": ["r"],
                        "superseded": [{"rule": "a"}],
                    },
                    "alreadySaved": True,
                }

        monkeypatch.setattr(module, "RulesDiscovery", FakeRulesDiscovery)
        monkeypatch.setattr(module, "_wait_for_s3_object", lambda *a, **k: None)
        monkeypatch.setattr(
            module, "update_job_status", lambda *a, **k: statuses.append(k)
        )
        body = {
            "jobId": "job-1",
            "documentKey": "document/amended.pdf",
            "bucket": DOCS,
            "version": PROFILE,
            "discoveryType": "rules",
            "policyGroup": GROUP,
            "policyGroupMode": "replace",
        }
        module.handler(
            {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}, None
        )
        final = statuses[-1]["status_message"]
        assert "already saved to policy class 'NCCI_Manual'" in final
        assert "nothing was saved again" in final
        assert "replaced" not in final

    def test_an_append_message_reads_as_before(self, monkeypatch):
        run, status = self._handle(
            monkeypatch, {}, {"mode": "append", "rules": ["limit"], "superseded": []}
        )
        assert run["policy_group_mode"] is None
        assert "appended to policy group 'NCCI Manual'" in status
        assert "Superseded" not in status


@pytest.mark.unit
class TestARefusalIsFinalAndASavedJobIsNotSavedTwice:
    """SQS redelivers a failed message; neither a refusal nor a saved job may act twice."""

    def test_a_refusal_keeps_its_type_and_skips_the_model(self, aws, monkeypatch):
        _base(aws, monkeypatch)
        calls: List[str] = []
        s3 = aws
        s3.put_object(Bucket=DOCS, Key="document/rider.pdf", Body=b"%PDF-1.4")
        d = RulesDiscovery(
            input_bucket=DOCS, input_prefix="document/rider.pdf", config=IDPConfig()
        )
        d.config_manager = ConfigurationManager()
        d.version = PROFILE
        monkeypatch.setattr(d, "_extract_rules", lambda *_a: calls.append("model"))
        with pytest.raises(PolicyGroupChangeRefused, match="no rule named 'gone'"):
            d.discovery_rules_from_document(
                DOCS,
                "document/rider.pdf",
                policy_group=GROUP,
                policy_group_mode="supersede",
                superseded_rules=["gone"],
            )
        assert calls == []

    def test_a_refusal_at_save_time_keeps_its_type(self, aws, monkeypatch):
        """The group changed after the pre-check: the save-time refusal is typed too."""
        d = RulesDiscovery(input_bucket=DOCS, input_prefix="k", config=IDPConfig())
        d.config_manager = ConfigurationManager()
        d.version = PROFILE
        with pytest.raises(PolicyGroupChangeRefused, match="does not exist"):
            d._save_rules_to_config(
                _chapter({"r": "?"}), policy_group=GROUP, policy_group_mode="replace"
            )

    def test_a_redelivered_job_is_not_saved_or_extracted_again(self, aws, monkeypatch):
        _base(aws, monkeypatch)
        _run(
            aws, monkeypatch, "document/ch3.pdf", _chapter({"r3": "?"}), upload_id="j3"
        )
        before = _policy_classes()
        calls: List[str] = []
        aws.put_object(Bucket=DOCS, Key="document/ch3.pdf", Body=b"%PDF-1.4")
        d = RulesDiscovery(
            input_bucket=DOCS, input_prefix="document/ch3.pdf", config=IDPConfig()
        )
        d.config_manager = ConfigurationManager()
        d.version = PROFILE
        monkeypatch.setattr(
            d,
            "_extract_rules",
            lambda *_a: calls.append("model") or _chapter({"r3": "?"}),
        )
        result = d.discovery_rules_from_document(
            DOCS, "document/ch3.pdf", policy_group=GROUP, upload_id="j3"
        )
        assert calls == []
        assert _policy_classes() == before
        assert result["alreadySaved"] is True
        assert result["rules"][0]["x-aws-idp-policy-type"] == "NCCI_Manual"
        assert any("already saved" in w for w in result["warnings"])

    def test_a_second_save_of_one_job_under_the_lease_writes_nothing(self, aws):
        """Two deliveries racing past the pre-check: the lease-held check stops one."""
        d = RulesDiscovery(input_bucket=DOCS, input_prefix="k", config=IDPConfig())
        d.config_manager = ConfigurationManager()
        d.version = PROFILE
        d._save_rules_to_config(
            _chapter({"a": "?"}), policy_group=GROUP, upload_id="j1"
        )
        d._save_rules_to_config(
            _chapter({"a": "?"}), policy_group=GROUP, upload_id="j1"
        )
        (pc,) = _policy_classes()
        assert list(pc["rule_properties"]) == ["a"]
        assert [e["uploadId"] for e in pc[UPLOADS_KEY]] == ["j1"]

    def test_a_redelivered_ungrouped_job_does_not_duplicate_its_class(
        self, aws, monkeypatch
    ):
        first = _run(
            aws,
            monkeypatch,
            "document/solo.pdf",
            _chapter({"r": "?"}),
            upload_id="j5",
            group=None,
        )
        second = _run(
            aws,
            monkeypatch,
            "document/solo.pdf",
            _chapter({"r": "?"}),
            upload_id="j5",
            group=None,
        )
        classes = _policy_classes()
        assert len(classes) == 1
        assert first["alreadySaved"] is False
        assert second["alreadySaved"] is True
        assert (
            second["rules"][0]["x-aws-idp-policy-type"]
            == classes[0]["x-aws-idp-policy-type"]
        )

    def test_a_duplicate_caught_under_the_lease_is_reported_as_already_saved(
        self, aws, monkeypatch
    ):
        """The pre-model check missed it (a racing delivery): the result still says so."""
        _base(aws, monkeypatch)
        monkeypatch.setattr(
            RulesDiscovery, "_current_policy_classes", lambda self, v: []
        )
        result = _run(
            aws,
            monkeypatch,
            "document/ch1.pdf",
            _chapter({"limit": "≤ 3?"}),
            upload_id="u1",
        )
        assert result["alreadySaved"] is True
        (pc,) = _policy_classes()
        assert [e["uploadId"] for e in pc[UPLOADS_KEY]] == ["u1", "u2"]

    def test_saves_without_a_job_id_are_never_taken_for_repeats(self, aws):
        d = RulesDiscovery(input_bucket=DOCS, input_prefix="k", config=IDPConfig())
        d.config_manager = ConfigurationManager()
        d.version = PROFILE
        d._save_rules_to_config(_chapter({"a": "?"}), policy_group=GROUP)
        d._save_rules_to_config(_chapter({"b": "?"}), policy_group=GROUP)
        (pc,) = _policy_classes()
        assert list(pc["rule_properties"]) == ["a", "b"]


@pytest.mark.unit
class TestTheProcessorDoesNotRetryARefusal:
    def _handle(self, monkeypatch, error: Exception):
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        module = _load_processor(monkeypatch)
        statuses: List[tuple] = []

        class FakeRulesDiscovery:
            version = PROFILE

            def __init__(self, **kwargs):
                pass

            def discovery_rules_from_document(self, **kwargs):
                raise error

        monkeypatch.setattr(module, "RulesDiscovery", FakeRulesDiscovery)
        monkeypatch.setattr(module, "_wait_for_s3_object", lambda *a, **k: None)
        monkeypatch.setattr(
            module, "update_job_status", lambda *a, **k: statuses.append((a, k))
        )
        body = {
            "jobId": "job-9",
            "documentKey": "document/rider.pdf",
            "bucket": DOCS,
            "version": PROFILE,
            "discoveryType": "rules",
            "policyGroup": GROUP,
            "policyGroupMode": "supersede",
            "supersededRules": ["gone"],
        }
        result = module.handler(
            {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}, None
        )
        return result, statuses

    def test_a_refusal_fails_the_job_and_is_not_handed_back_to_sqs(self, monkeypatch):
        result, statuses = self._handle(
            monkeypatch, PolicyGroupChangeRefused("has no rule named 'gone'")
        )
        assert result["batchItemFailures"] == []
        failed = [a for a, _k in statuses if len(a) > 1 and a[1] == "FAILED"]
        assert failed and "has no rule named 'gone'" in failed[-1][2]

    def test_any_other_failure_is_still_retried(self, monkeypatch):
        result, _ = self._handle(monkeypatch, RuntimeError("Bedrock throttled"))
        assert result["batchItemFailures"] == [{"itemIdentifier": "m1"}]
