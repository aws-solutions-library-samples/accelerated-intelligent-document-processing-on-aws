# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Policy Discovery uploads that name a policy group accumulate into one class (#608).

These drive the real save path — ``RulesDiscovery.discovery_rules_from_document``
reading the policy document from S3, then ``_save_rules_to_config`` reading and
writing the profile through a real ``ConfigurationManager`` — against moto, and read
the profile back the way the Policy Schema tab and rule validation do. Only the
model call (``_extract_rules``) is replaced, by a fixed ruleset per chapter.

What is pinned:

* uploads naming one group (compared ignoring case and spacing) build ONE policy
  class with a deterministic name and the group stored on it;
* a later upload **appends**: a rule name the class already holds is added under a
  suffixed name and reported, and the earlier rule is left exactly as it was (no
  superseding);
* a regex the user added to the group's class survives later uploads, so one regex
  covers the group, and no "≥2 classes need a regex" warning (#601) appears for a
  profile whose only class is the group;
* uploads naming no group keep their per-upload classes;
* two saves into one profile at once both land (the save lease).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
import pytest
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.models import IDPConfig
from idp_common.discovery import rules_discovery as rd
from idp_common.discovery.rules_discovery import (
    RulesDiscovery,
    normalize_policy_group,
    policy_group_class_name,
)

TABLE = "policy-group-config-table"
BUCKET = "policy-group-config-bucket"
DOCS = "policy-group-docs"
PROFILE = "rules-profile"
GROUP_KEY = "x-aws-idp-policy-group"


def _chapter(rules: Dict[str, str], category: str = "billing_rules") -> List[Dict]:
    return [
        {
            "x-aws-idp-rule-type": category,
            "description": f"{category} from the manual",
            "rule_properties": {
                name: {"description": text, "page": "1"} for name, text in rules.items()
            },
        }
    ]


@pytest.fixture
def aws(monkeypatch):
    for k, v in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "CONFIGURATION_TABLE_NAME": TABLE,
        "CONFIGURATION_BUCKET": BUCKET,
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        s3.create_bucket(Bucket=DOCS)
        ConfigurationManager().save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(), version=PROFILE
        )
        yield s3


def _run(
    s3,
    monkeypatch,
    key: str,
    extracted: List[Dict],
    policy_group: Optional[str] = None,
) -> Dict[str, Any]:
    """One discovery job: the document lands in S3, the model returns ``extracted``."""
    s3.put_object(Bucket=DOCS, Key=key, Body=b"%PDF-1.4 policy")
    d = RulesDiscovery(input_bucket=DOCS, input_prefix=key, config=IDPConfig())
    d.config_manager = ConfigurationManager()
    d.version = PROFILE
    monkeypatch.setattr(d, "_extract_rules", lambda *_a: [dict(c) for c in extracted])
    return d.discovery_rules_from_document(DOCS, key, policy_group=policy_group)


def _policy_classes() -> List[Dict[str, Any]]:
    raw = ConfigurationManager().get_raw_configuration(CONFIG_TYPE_CONFIG, PROFILE)
    return list((raw or {}).get("policy_classes") or [])


def _save_regex(class_name: str, regex: str) -> None:
    """What the Policy Schema tab does when the user adds a regex and saves."""
    manager = ConfigurationManager()
    config = manager.get_configuration(CONFIG_TYPE_CONFIG, version=PROFILE)
    for pc in config.policy_classes:
        if pc["x-aws-idp-policy-type"] == class_name:
            pc["x-aws-idp-document-name-regex"] = regex
    manager.save_configuration(CONFIG_TYPE_CONFIG, config, version=PROFILE)


@pytest.mark.unit
class TestUploadsInOneGroupAccumulate:
    def test_three_chapters_make_one_class_holding_every_rule(self, aws, monkeypatch):
        for n, rules in enumerate(
            [{"rule_a": "A?"}, {"rule_b": "B?"}, {"rule_c": "C?", "rule_d": "D?"}],
            start=1,
        ):
            _run(
                aws,
                monkeypatch,
                f"document/20261119_10000{n}_NCCI_Manual_Ch{n}.pdf",
                _chapter(rules),
                policy_group="NCCI Policy Manual",
            )

        classes = _policy_classes()
        assert len(classes) == 1
        (pc,) = classes
        assert pc["x-aws-idp-policy-type"] == "NCCI_Policy_Manual"
        assert pc["$id"] == "NCCI_Policy_Manual"
        assert pc[GROUP_KEY] == "NCCI Policy Manual"
        assert list(pc["rule_properties"]) == ["rule_a", "rule_b", "rule_c", "rule_d"]
        assert pc["rule_properties"]["rule_c"]["description"] == "C?"

    def test_the_group_is_matched_ignoring_case_and_spacing(self, aws, monkeypatch):
        _run(aws, monkeypatch, "document/a.pdf", _chapter({"r1": "1?"}), "NCCI Manual")
        _run(
            aws,
            monkeypatch,
            "document/b.pdf",
            _chapter({"r2": "2?"}),
            "  ncci   MANUAL ",
        )
        (pc,) = _policy_classes()
        assert pc[GROUP_KEY] == "NCCI Manual"  # the first upload's spelling is kept
        assert set(pc["rule_properties"]) == {"r1", "r2"}

    def test_every_rule_class_of_one_upload_goes_into_the_group(self, aws, monkeypatch):
        extracted = _chapter({"r1": "1?"}, "billing") + _chapter({"r1": "dup?"}, "mods")
        result = _run(aws, monkeypatch, "document/a.pdf", extracted, "Manual")
        (pc,) = _policy_classes()
        assert pc["rule_properties"]["r1"]["description"] == "1?"
        assert pc["rule_properties"]["r1_2"]["description"] == "dup?"
        # The job reports the class it saved into, for the "View in Configuration" link.
        assert {r["x-aws-idp-policy-type"] for r in result["rules"]} == {"Manual"}


@pytest.mark.unit
class TestReuploadAppends:
    def test_a_rule_name_already_in_the_group_is_added_beside_it_not_over_it(
        self, aws, monkeypatch
    ):
        _run(aws, monkeypatch, "document/base.pdf", _chapter({"limit": "≤ 3?"}), "M")
        result = _run(
            aws, monkeypatch, "document/amend.pdf", _chapter({"limit": "≤ 5?"}), "M"
        )
        (pc,) = _policy_classes()
        assert pc["rule_properties"]["limit"]["description"] == "≤ 3?"
        assert pc["rule_properties"]["limit_2"]["description"] == "≤ 5?"
        assert any("limit → limit_2" in w for w in result["warnings"])

    def test_the_users_regex_and_description_survive_a_later_upload(
        self, aws, monkeypatch
    ):
        _run(aws, monkeypatch, "document/c1.pdf", _chapter({"r1": "1?"}), "Manual")
        _save_regex("Manual", "(?i).*ncci.*")
        result = _run(
            aws, monkeypatch, "document/c2.pdf", _chapter({"r2": "2?"}), "Manual"
        )
        (pc,) = _policy_classes()
        assert pc["x-aws-idp-document-name-regex"] == "(?i).*ncci.*"
        assert pc["description"] == "billing_rules from the manual"
        assert set(pc["rule_properties"]) == {"r1", "r2"}
        assert result["warnings"] == []


@pytest.mark.unit
class TestRegexRequirementInteraction:
    """#601: rule validation needs a regex on every class once there are ≥2."""

    def test_a_grouped_manual_stays_one_class_and_raises_no_regex_warning(
        self, aws, monkeypatch
    ):
        warnings = []
        for n in range(3):
            warnings += _run(
                aws, monkeypatch, f"document/c{n}.pdf", _chapter({f"r{n}": "?"}), "G"
            )["warnings"]
        assert len(_policy_classes()) == 1
        assert warnings == []

    def test_the_same_chapters_ungrouped_make_three_classes_and_warn(
        self, aws, monkeypatch
    ):
        warnings = []
        for n in range(3):
            warnings += _run(
                aws, monkeypatch, f"document/c{n}.pdf", _chapter({f"r{n}": "?"})
            )["warnings"]
        assert len(_policy_classes()) == 3
        assert any("NONE has a document-matching regex" in w for w in warnings)


@pytest.mark.unit
class TestUngroupedUploadsAreUnchanged:
    def test_each_upload_keeps_its_own_randomly_suffixed_class(self, aws, monkeypatch):
        _run(
            aws,
            monkeypatch,
            "document/20261119_100001_Policy.pdf",
            _chapter({"a": "?"}),
        )
        _run(
            aws,
            monkeypatch,
            "document/20261119_100002_Policy.pdf",
            _chapter({"a": "?"}),
        )
        classes = _policy_classes()
        assert len(classes) == 2
        names = [pc["x-aws-idp-policy-type"] for pc in classes]
        assert all(
            n.startswith("Policy_") and len(n) == len("Policy_") + 8 for n in names
        )
        assert names[0] != names[1]
        assert all(GROUP_KEY not in pc for pc in classes)

    def test_a_colliding_name_is_suffixed_and_reported_back_to_the_job(
        self, aws, monkeypatch
    ):
        # Two categories in one ungrouped upload share the filename-derived name.
        extracted = _chapter({"a": "?"}, "one") + _chapter({"b": "?"}, "two")
        result = _run(aws, monkeypatch, "document/Policy.pdf", extracted)
        names = [pc["x-aws-idp-policy-type"] for pc in _policy_classes()]
        assert names[1] == f"{names[0]}_2"
        assert [r["x-aws-idp-policy-type"] for r in result["rules"]] == names

    def test_an_ungrouped_upload_never_joins_a_group(self, aws, monkeypatch):
        _run(aws, monkeypatch, "document/a.pdf", _chapter({"a": "?"}), "Manual")
        _run(aws, monkeypatch, "document/Manual.pdf", _chapter({"b": "?"}))
        classes = _policy_classes()
        assert len(classes) == 2
        assert set(classes[0]["rule_properties"]) == {"a"}


@pytest.mark.unit
class TestGroupIdentity:
    def test_a_name_collision_changes_the_created_name_not_the_target(
        self, aws, monkeypatch
    ):
        # An unrelated class already holds the name the group would derive.
        manager = ConfigurationManager()
        config = manager.get_configuration(CONFIG_TYPE_CONFIG, version=PROFILE)
        config.policy_classes = [
            {"x-aws-idp-policy-type": "Manual", "$id": "Manual", "rule_properties": {}}
        ]
        manager.save_configuration(CONFIG_TYPE_CONFIG, config, version=PROFILE)

        _run(aws, monkeypatch, "document/c1.pdf", _chapter({"r1": "?"}), "Manual")
        _run(aws, monkeypatch, "document/c2.pdf", _chapter({"r2": "?"}), "Manual")
        unrelated, group = _policy_classes()
        assert unrelated["rule_properties"] == {}
        assert group["x-aws-idp-policy-type"] == "Manual_2"
        assert set(group["rule_properties"]) == {"r1", "r2"}

    def test_two_classes_claiming_one_group_fail_the_job_and_save_nothing(
        self, aws, monkeypatch
    ):
        manager = ConfigurationManager()
        config = manager.get_configuration(CONFIG_TYPE_CONFIG, version=PROFILE)
        config.policy_classes = [
            {"x-aws-idp-policy-type": n, GROUP_KEY: "Manual", "rule_properties": {}}
            for n in ("A", "B")
        ]
        manager.save_configuration(CONFIG_TYPE_CONFIG, config, version=PROFILE)
        before = _policy_classes()

        with pytest.raises(Exception, match="carried by 2 policy classes"):
            _run(aws, monkeypatch, "document/c.pdf", _chapter({"r": "?"}), "manual")
        assert _policy_classes() == before

    def test_without_a_profile_to_save_into_the_rules_carry_the_group_name(
        self, aws, monkeypatch
    ):
        # The local/notebook construction: config given, nothing persisted.
        aws.put_object(Bucket=DOCS, Key="document/c.pdf", Body=b"%PDF")
        d = RulesDiscovery(input_bucket=DOCS, input_prefix="k", config=IDPConfig())
        monkeypatch.setattr(d, "_extract_rules", lambda *_a: _chapter({"r": "?"}))
        result = d.discovery_rules_from_document(
            DOCS, "document/c.pdf", policy_group="NCCI Manual"
        )
        assert [r["x-aws-idp-rule-type"] for r in result["rules"]] == ["NCCI_Manual"]

    def test_an_invalid_group_is_refused_before_the_model_runs(self, aws, monkeypatch):
        d = RulesDiscovery(input_bucket=DOCS, input_prefix="k.pdf", config=IDPConfig())
        d.config_manager = ConfigurationManager()
        d.version = PROFILE
        called = []
        monkeypatch.setattr(d, "_extract_rules", lambda *a: called.append(a))
        with pytest.raises(ValueError, match="policyGroup"):
            d.discovery_rules_from_document(DOCS, "k.pdf", policy_group="a/b")
        assert called == []


@pytest.mark.unit
class TestConcurrentSavesIntoOneProfile:
    def test_two_chapters_saved_at_once_both_land(self, aws, monkeypatch):
        """Each save reads, then pauses before writing, so unguarded saves interleave."""
        real_get = ConfigurationManager.get_configuration

        def slow_get(self, *a, **k):
            result = real_get(self, *a, **k)
            # Event.wait rather than time.sleep: the suite budgets real sleeps, and
            # this pause is the window in which an unguarded second save reads.
            threading.Event().wait(0.3)
            return result

        monkeypatch.setattr(ConfigurationManager, "get_configuration", slow_get)
        errors: List[BaseException] = []

        def save(n: int) -> None:
            try:
                d = RulesDiscovery(
                    input_bucket=DOCS, input_prefix="k", config=IDPConfig()
                )
                d.config_manager = ConfigurationManager()
                d.version = PROFILE
                d._save_rules_to_config(_chapter({f"r{n}": "?"}), policy_group="G")
            except BaseException as e:  # noqa: BLE001 - surfaced by the assert below
                errors.append(e)

        threads = [threading.Thread(target=save, args=(n,)) for n in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert errors == []
        (pc,) = _policy_classes()
        assert set(pc["rule_properties"]) == {"r1", "r2"}

    def test_the_lease_is_released_after_a_save(self, aws, monkeypatch):
        _run(aws, monkeypatch, "document/a.pdf", _chapter({"r": "?"}), "G")
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        item = table.get_item(
            Key={"Configuration": f"PolicyDiscoverySaveLease#{PROFILE}"}
        ).get("Item")
        assert item is None

    def test_a_held_lease_fails_the_save_rather_than_writing_unguarded(
        self, aws, monkeypatch
    ):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        table.put_item(
            Item={
                "Configuration": f"PolicyDiscoverySaveLease#{PROFILE}",
                "LeaseToken": "someone-else",
                "LeaseExpiresAt": int(time.time()) + 600,
            }
        )
        monkeypatch.setattr(rd, "POLICY_SAVE_LEASE_WAIT_SECONDS", 0.5)
        with pytest.raises(Exception, match="held by another Policy Discovery save"):
            _run(aws, monkeypatch, "document/a.pdf", _chapter({"r": "?"}), "G")
        assert _policy_classes() == []
        # Not released by a save that never held it.
        assert (
            table.get_item(
                Key={"Configuration": f"PolicyDiscoverySaveLease#{PROFILE}"}
            )["Item"]["LeaseToken"]
            == "someone-else"
        )

    def test_an_expired_lease_is_taken_over(self, aws, monkeypatch):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        table.put_item(
            Item={
                "Configuration": f"PolicyDiscoverySaveLease#{PROFILE}",
                "LeaseToken": "crashed",
                "LeaseExpiresAt": int(time.time()) - 1,
            }
        )
        _run(aws, monkeypatch, "document/a.pdf", _chapter({"r": "?"}), "G")
        assert len(_policy_classes()) == 1

    def test_a_save_whose_lease_was_taken_over_leaves_the_new_holders_lease(self, aws):
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        key = {"Configuration": f"PolicyDiscoverySaveLease#{PROFILE}"}
        d = RulesDiscovery(input_bucket=DOCS, input_prefix="k", config=IDPConfig())
        d.config_manager = ConfigurationManager()
        with d._policy_save_lease(PROFILE):
            # This holder outlived its lease and another save took it over.
            table.put_item(
                Item={**key, "LeaseToken": "new-holder", "LeaseExpiresAt": 2**40}
            )
        assert table.get_item(Key=key)["Item"]["LeaseToken"] == "new-holder"

    def test_the_lease_item_is_not_listed_as_a_profile(self, aws, monkeypatch):
        _run(aws, monkeypatch, "document/a.pdf", _chapter({"r": "?"}), "G")
        names = {
            v["versionName"] for v in ConfigurationManager().list_config_versions()
        }
        assert names == {PROFILE}


@pytest.mark.unit
class TestPolicyGroupNames:
    @pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
    def test_empty_means_no_group(self, value):
        assert normalize_policy_group(value) is None

    def test_whitespace_is_normalised(self):
        assert normalize_policy_group("  NCCI \t Manual ") == "NCCI Manual"

    @pytest.mark.parametrize(
        "value", ["a/b", "-x", "_x", ".x", "x" * 65, "<b>", "Ünïcode", 3, ["a"]]
    )
    def test_invalid_names_are_refused(self, value):
        with pytest.raises(ValueError, match="policyGroup"):
            normalize_policy_group(value)

    def test_the_longest_name_is_accepted(self):
        assert normalize_policy_group("x" * 64) == "x" * 64

    @pytest.mark.parametrize(
        "group, name",
        [
            ("NCCI Policy Manual", "NCCI_Policy_Manual"),
            ("v2.1 - draft", "v2_1_draft"),
            ("a", "a"),
        ],
    )
    def test_class_names_are_deterministic(self, group, name):
        assert policy_group_class_name(group) == name
        assert policy_group_class_name(group) == policy_group_class_name(group)


_PROCESSOR_DIR = (
    Path(__file__).resolve().parents[5] / "src" / "lambda" / "discovery_processor"
)


def _load_processor(monkeypatch):
    """The discovery processor Lambda's ``index``, imported from its own file.

    ``aws_requests_auth`` is a dependency of that Lambda's AppSync path only and is
    not installed for the library suite, so a stand-in module is registered first.
    """
    stub = types.ModuleType("aws_requests_auth.aws_auth")
    stub.AWSRequestsAuth = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aws_requests_auth", types.ModuleType("x"))
    monkeypatch.setitem(sys.modules, "aws_requests_auth.aws_auth", stub)
    spec = importlib.util.spec_from_file_location(
        "discovery_processor_index", _PROCESSOR_DIR / "index.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
class TestTheDiscoveryJobCarriesTheGroup:
    """The queue message's ``policyGroup`` reaches ``RulesDiscovery`` and the status."""

    def _handle(self, monkeypatch, message: Dict[str, Any]):
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        module = _load_processor(monkeypatch)
        calls: Dict[str, Any] = {}
        statuses: List[Dict[str, Any]] = []

        class FakeRulesDiscovery:
            version = "rules-profile"

            def __init__(self, **kwargs):
                calls["init"] = kwargs

            def discovery_rules_from_document(self, **kwargs):
                calls["run"] = kwargs
                return {
                    "status": "SUCCESS",
                    "rules": [
                        {
                            "x-aws-idp-policy-type": "NCCI_Manual",
                            "rule_properties": {"r": {}},
                        }
                    ],
                    "warnings": [],
                }

        monkeypatch.setattr(module, "RulesDiscovery", FakeRulesDiscovery)
        monkeypatch.setattr(module, "_wait_for_s3_object", lambda *a, **k: None)
        monkeypatch.setattr(
            module, "update_job_status", lambda *a, **k: statuses.append(k)
        )
        body = {
            "jobId": "j1",
            "documentKey": "document/c2.pdf",
            "bucket": DOCS,
            "version": "rules-profile",
            "discoveryType": "rules",
            **message,
        }
        result = module.handler(
            {"Records": [{"messageId": "m1", "body": json.dumps(body)}]},
            None,
        )
        assert result["batchItemFailures"] == []
        return calls, statuses

    def test_a_grouped_message_runs_the_discovery_into_its_group(self, monkeypatch):
        calls, statuses = self._handle(monkeypatch, {"policyGroup": "NCCI Manual"})
        assert calls["run"]["policy_group"] == "NCCI Manual"
        final = statuses[-1]["status_message"]
        assert "policy group 'NCCI Manual'" in final
        assert "NCCI_Manual" in final

    def test_an_ungrouped_message_runs_ungrouped(self, monkeypatch):
        calls, statuses = self._handle(monkeypatch, {})
        assert calls["run"]["policy_group"] is None
        assert "appended to policy_classes" in statuses[-1]["status_message"]
