# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
The two routing operations of the configuration resolver, and the warnings a save
returns.

``getReviewQueueReferences`` lists the routing rules that send documents to a
review queue, for an Admin about to delete or disable it. ``convertRoutingDmn``
turns a routing section into DMN XML and back. Both answer input they cannot
take with a ``ValueError`` that says what is wrong, which the API returns as a
400: never the ``{success: false}`` payload of the configuration operations, and
never a server fault. ``updateConfiguration`` returns, beside its success flag,
what the routing rules of the saved configuration will not do. The handler runs
against the real ``ConfigurationManager`` on moto.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.models import IDPConfig
from idp_common.config.routing_models import RoutingConfig, review_queue_of
from idp_common.config.routing_validation import routing_warnings
from idp_common.decision_tables.dmn import MAX_DMN_BYTES

os.environ.setdefault("CONFIGURATION_TABLE_NAME", "test-config-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

pytestmark = pytest.mark.unit

TABLE = "routing-ops-config"
BUCKET = "routing-ops-config-bucket"


def _load_index():
    name = "config_resolver_routing_ops_index"
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()


def _routing(queue="claims", rule_id="big", label="Big loans"):
    return {
        "tables": [
            {
                "rules": [
                    {
                        "id": rule_id,
                        "label": label,
                        "conditions": [
                            {
                                "input": "field_value",
                                "path": "Invoice:Total",
                                "test": "> 10000",
                            }
                        ],
                        "actions": [{"type": "review_queue", "queue": queue}],
                    }
                ]
            }
        ]
    }


def _call(field, args, group="Admin"):
    return index.handler(
        {
            "info": {"fieldName": field},
            "arguments": args,
            "identity": {
                "claims": {"cognito:groups": [group], "email": "someone@example.com"}
            },
        },
        None,
    )


@pytest.fixture
def stack(monkeypatch):
    """A configuration table and bucket with three profiles."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)
    monkeypatch.delenv("HITL_TABLE_NAME", raising=False)
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        # An unscoped caller: scope is another file's subject.
        monkeypatch.setattr(
            index, "_get_user_allowed_config_versions", lambda email, sub="": None
        )
        monkeypatch.setattr(index, "_REVIEW_STORE", None)
        manager = ConfigurationManager()
        manager.save_configuration(
            CONFIG_TYPE_CONFIG,
            IDPConfig(routing={"tables": [{"rules": []}]}),
            "default",
        )
        for profile, queue in (("lending", "claims"), ("mortgage", "approvals")):
            manager.save_configuration(
                CONFIG_TYPE_CONFIG, IDPConfig(routing=_routing(queue)), version=profile
            )
        yield manager


class TestQueueReferences:
    def test_the_rules_that_name_the_queue(self, stack):
        page = _call("getReviewQueueReferences", {"queueId": "claims"})

        assert page["nextToken"] is None
        (reference,) = page["items"]
        assert reference["profile"] == "lending"
        assert (reference["ruleId"], reference["ruleLabel"]) == ("big", "Big loans")
        assert reference["published"] is True
        assert isinstance(reference["revision"], int)
        assert set(reference) == {
            "profile",
            "revision",
            "published",
            "ruleId",
            "ruleLabel",
        }

    def test_a_queue_nobody_references(self, stack):
        assert _call("getReviewQueueReferences", {"queueId": "empty"}) == {
            "items": [],
            "nextToken": None,
        }

    def test_the_queue_id_is_matched_in_its_stored_form(self, stack):
        page = _call("getReviewQueueReferences", {"queueId": "  Claims "})

        assert [r["profile"] for r in page["items"]] == ["lending"]

    def test_it_pages_over_profiles(self, stack):
        """Three profiles, two a page: default and lending, then mortgage."""
        first = _call("getReviewQueueReferences", {"queueId": "approvals", "limit": 2})
        second = _call(
            "getReviewQueueReferences",
            {"queueId": "approvals", "limit": 2, "nextToken": first["nextToken"]},
        )

        assert (first["items"], first["nextToken"]) == ([], "lending")
        assert [r["profile"] for r in second["items"]] == ["mortgage"]
        assert second["nextToken"] is None

    @pytest.mark.parametrize("group", ["Author", "Viewer", "Reviewer", "Annotator"])
    def test_only_an_admin_may_ask(self, stack, group):
        with pytest.raises(Exception, match="Unauthorized: .*getReviewQueueReferences"):
            _call("getReviewQueueReferences", {"queueId": "claims"}, group)

    @pytest.mark.parametrize(
        "args",
        [
            {},
            {"queueId": ""},
            {"queueId": "   "},
            {"queueId": 7},
            {"queueId": "claims", "limit": 0},
            {"queueId": "claims", "limit": "ten"},
            {"queueId": "claims", "limit": True},
            {"queueId": "claims", "nextToken": 4},
        ],
    )
    def test_a_bad_argument_is_a_bad_request(self, stack, args):
        with pytest.raises(ValueError, match="BAD_ARGUMENT") as refused:
            _call("getReviewQueueReferences", args)

        assert type(refused.value) is ValueError


class TestConvertRoutingDmn:
    def _to_dmn(self, routing=None, group="Admin", **more):
        args = {"direction": "to_dmn", "content": json.dumps(routing or _routing())}
        args.update(more)
        return _call("convertRoutingDmn", args, group)

    def _from_dmn(self, xml, group="Admin"):
        return _call(
            "convertRoutingDmn", {"direction": "from_dmn", "content": xml}, group
        )

    def test_a_routing_section_becomes_dmn_xml(self, stack):
        result = self._to_dmn()

        assert result["routing"] is None
        assert result["xml"].lstrip().startswith("<?xml")
        assert "https://www.omg.org/spec/DMN/20230324/MODEL/" in result["xml"]
        assert "Big loans" in result["xml"]

    def test_the_older_form_is_written_when_asked(self, stack):
        """The form Camunda Modeler opens."""
        result = self._to_dmn(dmnVersion="1.3")

        assert "https://www.omg.org/spec/DMN/20191111/MODEL/" in result["xml"]

    def test_dmn_xml_becomes_a_routing_section(self, stack):
        xml = self._to_dmn()["xml"]

        result = self._from_dmn(xml)

        assert result["xml"] is None
        routing = RoutingConfig.model_validate(json.loads(result["routing"]))
        (rule,) = routing.tables[0].rules
        assert (rule.id, rule.label, review_queue_of(rule.actions)) == (
            "big",
            "Big loans",
            "claims",
        )
        assert rule.conditions[0].test == "> 10000"

    def test_there_and_back_gives_the_same_table(self, stack):
        original = RoutingConfig.model_validate(_routing())

        back = json.loads(self._from_dmn(self._to_dmn()["xml"])["routing"])

        assert RoutingConfig.model_validate(back).tables == original.tables

    @pytest.mark.parametrize("group", ["Admin", "Author", "Viewer"])
    def test_everyone_who_can_open_a_configuration_may_convert(self, stack, group):
        assert self._to_dmn(group=group)["xml"]

    @pytest.mark.parametrize("group", ["Reviewer", "Annotator"])
    def test_a_queue_worker_may_not(self, stack, group):
        with pytest.raises(Exception, match="Unauthorized: .*convertRoutingDmn"):
            self._to_dmn(group=group)

    def test_nothing_is_saved(self, stack):
        before = stack.get_configuration(CONFIG_TYPE_CONFIG, "lending").routing

        self._from_dmn(self._to_dmn(_routing("elsewhere"))["xml"])

        assert stack.get_configuration(CONFIG_TYPE_CONFIG, "lending").routing == before

    def test_xml_with_a_dtd_is_refused(self, stack):
        xml = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE definitions [<!ENTITY a "aaaa">]>\n'
            '<definitions xmlns="https://www.omg.org/spec/DMN/20230324/MODEL/"/>'
        )

        with pytest.raises(ValueError, match="INVALID_DMN") as refused:
            self._from_dmn(xml)

        assert type(refused.value) is ValueError

    @pytest.mark.parametrize(
        "xml", ["<not-dmn/>", "not xml at all", "<definitions><unclosed></definitions>"]
    )
    def test_xml_that_is_not_a_decision_table_is_refused(self, stack, xml):
        with pytest.raises(ValueError, match="INVALID_DMN"):
            self._from_dmn(xml)

    @pytest.mark.parametrize(
        "content",
        ["{not json", "[]", '"text"', json.dumps({"tables": "none"})],
    )
    def test_content_that_is_not_a_routing_section_is_refused(self, stack, content):
        with pytest.raises(ValueError, match="INVALID_ROUTING") as refused:
            _call("convertRoutingDmn", {"direction": "to_dmn", "content": content})

        assert type(refused.value) is ValueError

    def test_a_section_that_cannot_be_written_is_refused(self, stack):
        """A DMN file holds one decision table."""
        two = {"tables": [{"rules": []}, {"id": "second", "rules": []}]}

        with pytest.raises(ValueError, match="INVALID_ROUTING: .*exactly one table"):
            self._to_dmn(two)

    def test_a_dmn_version_that_cannot_be_written_is_refused(self, stack):
        with pytest.raises(ValueError, match="INVALID_ROUTING: .*1.1"):
            self._to_dmn(dmnVersion="1.1")

    @pytest.mark.parametrize("direction", ["both", "", None, "TO_DMN"])
    def test_another_direction_is_a_bad_request(self, stack, direction):
        with pytest.raises(ValueError, match="BAD_ARGUMENT: direction"):
            _call("convertRoutingDmn", {"direction": direction, "content": "<x/>"})

    @pytest.mark.parametrize("content", [None, "", "   ", 5])
    def test_no_content_is_a_bad_request(self, stack, content):
        with pytest.raises(ValueError, match="BAD_ARGUMENT: content"):
            _call("convertRoutingDmn", {"direction": "from_dmn", "content": content})

    @pytest.mark.parametrize("direction", ["to_dmn", "from_dmn"])
    def test_content_over_the_limit_is_refused_before_it_is_parsed(
        self, stack, direction, monkeypatch
    ):
        parsed = []
        monkeypatch.setattr(
            index.routing_dmn, "import_xml", lambda xml: parsed.append("xml")
        )
        monkeypatch.setattr(index.json, "loads", lambda text: parsed.append("json"))

        with pytest.raises(ValueError, match="TOO_LARGE"):
            _call(
                "convertRoutingDmn",
                {"direction": direction, "content": "x" * (MAX_DMN_BYTES + 1)},
            )

        assert parsed == []


_POLICY = {
    "$id": "invoice_controls",
    "x-aws-idp-policy-type": "invoice_controls",
    "rule_properties": {
        "total_matches_lines": {"description": "The total equals the line sum"}
    },
}
_SAVED = "Configuration updated successfully"


def _policy_routing():
    """A routing section whose one routing rule tests a policy rule result."""
    return {
        "tables": [
            {
                "rules": [
                    {
                        "id": "by-count",
                        "conditions": [
                            {"input": "failed_policy_rule_count", "test": "> 0"}
                        ],
                        "actions": [{"type": "review_queue", "queue": "claims"}],
                    }
                ]
            }
        ]
    }


def _save(delta, version="lending", group="Admin"):
    return _call(
        "updateConfiguration",
        {"versionName": version, "customConfig": json.dumps(delta)},
        group,
    )


def _stored_warnings(manager, profile="lending"):
    """The warnings of the profile as it is stored: one, about the routing rule."""
    warnings = routing_warnings(manager.get_configuration(CONFIG_TYPE_CONFIG, profile))
    (warning,) = warnings
    assert "rule 'by-count'" in warning
    assert "never matches" in warning
    return warnings


class TestSaveWarnings:
    """``updateConfiguration`` returns what the saved routing rules will not do.

    No profile of the fixture holds a policy, so policy rule validation does not
    run there and a routing rule that tests its result never matches.
    """

    def test_a_save_returns_them(self, stack):
        result = _save({"routing": _policy_routing()})

        assert result == {
            "success": True,
            "message": _SAVED,
            "warnings": _stored_warnings(stack),
        }

    def test_a_save_with_nothing_to_warn_about_returns_none(self, stack):
        """``None`` and not an empty list, as the response type's other fields."""
        result = _save({"notes": "an unrelated change"})

        assert result == {"success": True, "message": _SAVED, "warnings": None}

    def test_save_as_version_returns_them(self, stack):
        result = _save(
            {"routing": _policy_routing(), "saveAsVersion": True}, version="audited"
        )

        assert result["success"] is True
        assert result["warnings"] == _stored_warnings(stack, "audited")

    def test_a_save_that_turns_policy_rule_validation_off_returns_them(self, stack):
        """An Author may not change routing, and is still told what it stops doing."""
        stack.save_configuration(
            CONFIG_TYPE_CONFIG,
            IDPConfig(policy_classes=[_POLICY], routing=_policy_routing()),
            version="audited",
        )

        while_on = _save({"notes": "an unrelated change"}, "audited", "Author")
        once_off = _save({"rule_validation": {"enabled": False}}, "audited", "Author")

        assert while_on["warnings"] is None
        assert once_off["success"] is True
        assert once_off["warnings"] == _stored_warnings(stack, "audited")

    def test_reset_to_default_returns_none(self, stack):
        stack.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(routing=_policy_routing()), "default"
        )

        result = _save({"resetToDefault": True})

        assert result == {"success": True, "message": _SAVED, "warnings": None}
        # The reset did copy the default, which a save would have warned about.
        _stored_warnings(stack)
