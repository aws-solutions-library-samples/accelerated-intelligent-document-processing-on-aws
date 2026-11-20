# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``policyGroupMode`` and ``supersededRules`` on ``uploadDiscoveryDocument`` (#608).

A grouped Policy Discovery upload may replace the group's rules or supersede named
rules. The resolver validates the shape with the rule ``idp_common`` saves under,
then reads the profile (a real ``ConfigurationManager`` over moto here) and refuses
a replace or supersede the group cannot satisfy — before any URL is minted or job
created — and only after the caller's profile scope has been checked.
"""

import importlib
import json
import sys
from pathlib import Path
from unittest import mock

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.unit

DISCOVERY_BUCKET = "discovery-bucket"
CONFIG_TABLE = "supersede-config-table"
PROFILE = "rules-profile"

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _event(arguments, groups=("Admin",)):
    return {
        "info": {"fieldName": "uploadDiscoveryDocument"},
        "arguments": arguments,
        "identity": {
            "claims": {
                "cognito:groups": list(groups),
                "email": "author@example.com",
                "sub": "11111111-2222-3333-4444-555555555555",
            }
        },
    }


@pytest.fixture
def index(monkeypatch):
    for k, v in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "DISCOVERY_BUCKET": DISCOVERY_BUCKET,
        "DISCOVERY_TRACKING_TABLE": "tracking-table",
        "DISCOVERY_QUEUE_URL": "https://sqs.example/queue",
        "CONFIGURATION_TABLE_NAME": CONFIG_TABLE,
        "CONFIGURATION_BUCKET": "supersede-config-bucket",
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=CONFIG_TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(
            Bucket="supersede-config-bucket"
        )
        from idp_common.config.configuration_manager import ConfigurationManager

        ConfigurationManager().save_raw_configuration(
            "Config",
            {
                "policy_classes": [
                    {
                        "x-aws-idp-policy-type": "NCCI_Manual",
                        "x-aws-idp-policy-group": "NCCI Manual",
                        "rule_properties": {
                            "visit_limit": {"type": "string", "description": "≤ 3?"},
                            "modifier_25": {"type": "string", "description": "M?"},
                        },
                    }
                ]
            },
            version=PROFILE,
        )
        import index as module

        importlib.reload(module)
        table = mock.MagicMock()
        table.get_item.return_value = {}
        monkeypatch.setattr(module, "dynamodb", mock.MagicMock(Table=lambda _n: table))
        monkeypatch.setattr(module, "sqs_client", mock.MagicMock())
        module._test_table = table
        yield module


def _upload(index, groups=("Admin",), **extra):
    arguments = {
        "fileName": "rider.pdf",
        "bucket": DISCOVERY_BUCKET,
        "version": PROFILE,
        "discoveryType": "rules",
        "policyGroup": "NCCI Manual",
        **extra,
    }
    return index.handler(_event(arguments, groups), None)


def _job_item(index):
    return index._test_table.put_item.call_args.kwargs["Item"]


def _message(index):
    return json.loads(index.sqs_client.send_message.call_args.kwargs["MessageBody"])


def _nothing_created(index):
    index._test_table.put_item.assert_not_called()
    index.sqs_client.send_message.assert_not_called()


class TestAcceptedChanges:
    def test_a_supersede_reaches_the_job_record_and_the_message(self, index):
        _upload(
            index,
            policyGroupMode="supersede",
            supersededRules=["visit_limit", "visit_limit"],
        )
        for record in (_job_item(index), _message(index)):
            assert record["policyGroupMode"] == "supersede"
            assert record["supersededRules"] == ["visit_limit"]

    def test_a_replace_reaches_the_job_record_and_the_message(self, index):
        _upload(index, policyGroupMode="replace")
        for record in (_job_item(index), _message(index)):
            assert record["policyGroupMode"] == "replace"
            assert "supersededRules" not in record

    def test_the_group_is_matched_ignoring_case_and_spacing(self, index):
        _upload(
            index,
            policyGroup=" ncci  manual ",
            policyGroupMode="supersede",
            supersededRules=["modifier_25"],
        )
        assert _message(index)["supersededRules"] == ["modifier_25"]

    @pytest.mark.parametrize("extra", [{}, {"policyGroupMode": "append"}])
    def test_an_append_carries_no_change_fields(self, index, extra):
        _upload(index, **extra)
        for record in (_job_item(index), _message(index)):
            assert record["policyGroup"] == "NCCI Manual"
            assert "policyGroupMode" not in record
            assert "supersededRules" not in record


class TestRefusedBeforeAnythingIsCreated:
    def test_a_rule_the_group_does_not_hold_is_named_in_the_refusal(self, index):
        with pytest.raises(ValueError, match="no rule named 'visit_limt'"):
            _upload(
                index,
                policyGroupMode="supersede",
                supersededRules=["visit_limit", "visit_limt"],
            )
        _nothing_created(index)

    @pytest.mark.parametrize(
        "extra",
        [
            {"policyGroupMode": "supersede", "supersededRules": ["gone"]},
            {"policyGroup": "Other Manual", "policyGroupMode": "replace"},
        ],
    )
    def test_a_refusal_reaches_the_dispatcher_as_a_plain_value_error(
        self, index, extra
    ):
        """The dispatcher keys its 400 on the class NAME `ValueError`, not a subclass."""
        with pytest.raises(ValueError) as caught:
            _upload(index, **extra)
        assert type(caught.value).__name__ == "ValueError"
        _nothing_created(index)

    def test_rule_names_are_exact(self, index):
        with pytest.raises(ValueError, match="no rule named 'Visit_Limit'"):
            _upload(index, policyGroupMode="supersede", supersededRules=["Visit_Limit"])
        _nothing_created(index)

    @pytest.mark.parametrize("mode", ["replace", "supersede"])
    def test_a_group_the_profile_does_not_hold(self, index, mode):
        extra = {"supersededRules": ["visit_limit"]} if mode == "supersede" else {}
        with pytest.raises(ValueError, match="does not exist"):
            _upload(index, policyGroup="Other Manual", policyGroupMode=mode, **extra)
        _nothing_created(index)

    @pytest.mark.parametrize(
        "extra, match",
        [
            ({"policyGroupMode": "merge"}, "must be one of"),
            ({"policyGroupMode": "supersede"}, "needs supersededRules"),
            (
                {"policyGroupMode": "replace", "supersededRules": ["visit_limit"]},
                "applies only to policyGroupMode 'supersede'",
            ),
            ({"supersededRules": ["visit_limit"]}, "applies only to"),
            (
                {"policyGroupMode": "supersede", "supersededRules": "visit_limit"},
                "must be a list",
            ),
            (
                {"policyGroup": None, "policyGroupMode": "replace"},
                "needs a policyGroup",
            ),
        ],
    )
    def test_a_malformed_change(self, index, extra, match):
        with pytest.raises(ValueError, match=match):
            _upload(index, **extra)
        _nothing_created(index)

    def test_an_out_of_scope_caller_is_refused_before_the_profile_is_read(
        self, index, monkeypatch
    ):
        """The refusal above names the group's rules, so it is for in-scope callers."""
        from idp_common.config import configuration_manager

        read = mock.MagicMock(side_effect=AssertionError("profile was read"))
        monkeypatch.setattr(
            configuration_manager.ConfigurationManager,
            "get_raw_configuration",
            read,
        )
        monkeypatch.setattr(index, "_caller_config_scope", lambda _e: ["elsewhere"])
        with pytest.raises(Exception, match="not in your allowed scope"):
            _upload(
                index,
                groups=("Author",),
                policyGroupMode="supersede",
                supersededRules=["anything"],
            )
        read.assert_not_called()
        _nothing_created(index)
