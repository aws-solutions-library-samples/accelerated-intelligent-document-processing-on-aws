# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review routing in ``client.config``: DMN in and out, and queues on upload.

``routing_to_dmn`` and ``routing_from_dmn`` convert a configuration's routing
table to and from DMN XML on the caller's machine, with ``idp_common``'s own
converter. ``upload`` hands the configuration manager a lookup of the stack's
production review queues, so a rule that names a queue the stack does not have
is refused when it is saved, as it is from the web UI.

The conversion cases use the real converter. The upload cases replace the
configuration manager (they assert what it is handed) and use a real review
table under moto for the lookup itself.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
import yaml
from moto import mock_aws

from idp_common.config.routing_models import RoutingConfig
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.store import ReviewStore
from idp_sdk import IDPClient, IDPValidationError

pytestmark = pytest.mark.unit

REGION = "us-east-1"
STACK = "idp-routing-test"
REVIEW_TABLE = "idp-routing-test-hitl"

ROUTING = {
    "tables": [
        {
            "id": "review-routing",
            "stage": "after_policy_rule_validation",
            "hit_policy": "FIRST",
            "engine": "builtin",
            "rules": [
                {
                    "id": "compliance-policy-failure",
                    "label": "Failed policy rules",
                    "enabled": True,
                    "conditions": [
                        {
                            "input": "failed_policy_rule_count",
                            "path": "",
                            "aggregate": "any",
                            "test": "> 0",
                        }
                    ],
                    "actions": [{"type": "review_queue", "queue": "compliance"}],
                    "reason": "{failed_policy_rule_count} policy rules failed.",
                },
                {
                    "id": "invoices",
                    "label": "Invoices",
                    "enabled": False,
                    "conditions": [
                        {
                            "input": "document_class",
                            "path": "",
                            "aggregate": "any",
                            "test": '"Invoice"',
                        }
                    ],
                    "actions": [{"type": "review_queue", "queue": "default"}],
                    "reason": "",
                },
            ],
            "default_outcome": {"actions": [], "reason": ""},
        }
    ],
    "max_consecutive_placements": 3,
}


def _routing(section: dict) -> RoutingConfig:
    return RoutingConfig.model_validate(section)


@pytest.fixture
def config():
    """A client with no stack and no region: the conversions need neither."""
    return IDPClient().config


# ------------------------------------------------------------------ DMN out and in


class TestConversion:
    def test_a_routing_table_survives_the_round_trip(self, config):
        xml = config.routing_to_dmn({"routing": ROUTING, "classes": []})

        back = config.routing_from_dmn(xml)

        assert set(back) == {"routing"}
        assert _routing(back["routing"]).tables == _routing(ROUTING).tables
        rules = back["routing"]["tables"][0]["rules"]
        assert [rule["id"] for rule in rules] == [
            "compliance-policy-failure",
            "invoices",
        ]
        assert [rule["enabled"] for rule in rules] == [True, False]
        assert rules[0]["actions"] == [{"type": "review_queue", "queue": "compliance"}]
        assert rules[0]["reason"] == "{failed_policy_rule_count} policy rules failed."
        assert "outcome" not in rules[0]

    def test_the_xml_is_a_dmn_decision_table(self, config):
        xml = config.routing_to_dmn({"routing": ROUTING})

        assert xml.lstrip().startswith("<?xml")
        assert "<decisionTable" in xml and 'hitPolicy="FIRST"' in xml
        assert "failed_policy_rule_count" in xml and "compliance" in xml

    def test_the_routing_section_alone_is_accepted(self, config):
        assert config.routing_to_dmn(ROUTING) == config.routing_to_dmn(
            {"routing": ROUTING}
        )

    @pytest.mark.parametrize("suffix", [".yaml", ".json"])
    def test_a_configuration_file_is_read(self, config, tmp_path, suffix):
        path = tmp_path / f"config{suffix}"
        document = {"routing": ROUTING, "notes": "a preset"}
        path.write_text(
            json.dumps(document) if suffix == ".json" else yaml.safe_dump(document)
        )

        assert config.routing_to_dmn(str(path)) == config.routing_to_dmn(document)

    def test_the_dmn_version_is_the_callers(self, config):
        default = config.routing_to_dmn(ROUTING)
        older = config.routing_to_dmn(ROUTING, dmn_version="1.3")

        assert default != older
        assert config.routing_to_dmn(ROUTING, dmn_version="1.5") == default
        for xml in (default, older):
            assert (
                _routing(config.routing_from_dmn(xml)["routing"]).tables
                == _routing(ROUTING).tables
            )

    def test_bytes_are_read_as_xml(self, config):
        xml = config.routing_to_dmn(ROUTING)

        assert config.routing_from_dmn(xml.encode("utf-8")) == config.routing_from_dmn(
            xml
        )

    def test_the_result_can_be_written_as_a_configuration_file(self, config, tmp_path):
        """What ``routing_from_dmn`` returns is a partial configuration: written to
        a file, ``routing_to_dmn`` reads it back to the same XML."""
        xml = config.routing_to_dmn(ROUTING)
        path = tmp_path / "routing.yaml"
        path.write_text(yaml.safe_dump(config.routing_from_dmn(xml)))

        assert config.routing_to_dmn(str(path)) == xml

    def test_no_aws_call_is_made(self, config):
        def refuse(*args, **kwargs):
            raise AssertionError("a conversion made an AWS client")

        with (
            patch.object(boto3, "client", refuse),
            patch.object(boto3, "resource", refuse),
        ):
            config.routing_from_dmn(config.routing_to_dmn(ROUTING))


class TestWhatCannotBeConverted:
    @pytest.mark.parametrize(
        "configuration",
        [{}, {"classes": []}, {"routing": None}, {"routing": {}}, {"routing": []}],
    )
    def test_a_configuration_with_no_routing_section(self, config, configuration):
        with pytest.raises(IDPValidationError, match="no routing section"):
            config.routing_to_dmn(configuration)

    @pytest.mark.parametrize("configuration", [None, 7, ["routing"]])
    def test_something_that_is_not_a_configuration(self, config, configuration):
        with pytest.raises(IDPValidationError, match="must be a mapping"):
            config.routing_to_dmn(configuration)

    def test_a_routing_section_that_is_not_valid_says_where(self, config):
        broken = json.loads(json.dumps(ROUTING))
        broken["tables"][0]["rules"][0]["conditions"] = "none"

        with pytest.raises(IDPValidationError, match="not valid") as refused:
            config.routing_to_dmn({"routing": broken})

        assert "tables.0.rules.0.conditions" in str(refused.value)

    def test_a_dmn_version_that_is_not_supported(self, config):
        with pytest.raises(IDPValidationError, match="cannot be written"):
            config.routing_to_dmn(ROUTING, dmn_version="9.9")

    def test_a_file_that_does_not_exist(self, config, tmp_path):
        with pytest.raises(IDPValidationError, match="Failed to load file"):
            config.routing_to_dmn(str(tmp_path / "missing.yaml"))

    @pytest.mark.parametrize("xml", ["", "   ", b"", None, 7])
    def test_empty_xml(self, config, xml):
        with pytest.raises(IDPValidationError, match="empty"):
            config.routing_from_dmn(xml)

    @pytest.mark.parametrize(
        "xml",
        [
            "<notDmn/>",
            "this is not xml",
            '<!DOCTYPE d [<!ENTITY e "x">]><definitions>&e;</definitions>',
        ],
        ids=["another-document", "not-xml", "a-doctype"],
    )
    def test_xml_that_is_not_a_decision_table(self, config, xml):
        with pytest.raises(IDPValidationError, match="cannot be read"):
            config.routing_from_dmn(xml)


# ------------------------------------------------------------------ upload


def _create_review_table():
    boto3.resource("dynamodb", region_name=REGION).create_table(
        TableName=REVIEW_TABLE,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


class _Upload:
    """One ``upload`` with the configuration manager replaced; what it was handed."""

    def __init__(self, tmp_path, monkeypatch, resources):
        self.asked_for_resources = []
        self.client = IDPClient(stack_name=STACK, region=REGION)

        def resources_of(stack_name=None):
            self.asked_for_resources.append(stack_name)
            if isinstance(resources, Exception):
                raise resources
            return resources

        monkeypatch.setattr(self.client, "_get_stack_resources", resources_of)
        monkeypatch.setattr(
            self.client.config, "_configure_config_env", lambda name: "config-table"
        )
        self.path = tmp_path / "config.yaml"

    def run(self, configuration: dict):
        self.path.write_text(yaml.safe_dump(configuration))
        with patch(
            "idp_common.config.configuration_manager.ConfigurationManager"
        ) as manager_class:
            manager = manager_class.return_value
            manager.get_configuration.return_value = {"version": "claims"}
            manager.handle_update_custom_configuration.return_value = True
            manager.resolve_published_revision.return_value = 2
            self.result = self.client.config.upload(
                config_file=str(self.path), config_profile="claims", validate=False
            )
            self.calls = manager.handle_update_custom_configuration.call_args_list
        return self

    @property
    def lookup(self):
        (call,) = self.calls
        return call.kwargs["routing_queue_lookup"]


@pytest.fixture
def upload(tmp_path, monkeypatch, aws_credentials):
    def make(resources):
        return _Upload(tmp_path, monkeypatch, resources)

    return make


class TestUploadChecksQueues:
    @mock_aws
    def test_a_stack_with_review_queues_gets_a_lookup_of_its_queues(self, upload):
        _create_review_table()
        ReviewStore(REVIEW_TABLE, region=REGION).queue_defs.put(
            QueueDefinition(id="compliance", name="Compliance", enabled=False),
            create=True,
        )

        done = upload({"HITLTable": REVIEW_TABLE}).run({"routing": ROUTING})

        assert done.result.success is True
        assert done.asked_for_resources == [STACK]
        found = done.lookup("compliance")
        assert (found.id, found.name, found.enabled) == (
            "compliance",
            "Compliance",
            False,
        )
        assert done.lookup("no-such-queue") is None
        # The Default queue always exists: it is seeded on first use.
        assert done.lookup("default").id == "default"

    @pytest.mark.parametrize("resources", [{"HITLTable": ""}, {}])
    def test_a_stack_with_no_review_queues_table_gets_none(self, upload, resources):
        done = upload(resources).run({"routing": ROUTING})

        assert done.result.success is True
        assert done.lookup is None

    def test_a_configuration_with_no_routing_section_does_not_read_the_stack(
        self, upload
    ):
        done = upload({"HITLTable": REVIEW_TABLE}).run({"classes": []})

        assert done.result.success is True
        assert done.lookup is None
        assert done.asked_for_resources == []

    def test_a_stack_that_cannot_be_read_fails_the_upload(self, upload):
        """The queues could not be checked, so nothing is saved unchecked."""
        from idp_sdk import IDPStackError

        done = upload(IDPStackError("Stack is not in a valid state")).run(
            {"routing": ROUTING}
        )

        assert done.result.success is False
        assert "not in a valid state" in done.result.error
        assert done.calls == []
