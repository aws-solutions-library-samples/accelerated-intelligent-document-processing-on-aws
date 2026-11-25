# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ``policyGroup`` argument of ``uploadDiscoveryDocument`` (#608).

A Policy Discovery upload may name a policy group; uploads naming the same group
append their rules to one policy class. The resolver validates the name (with the
rule ``idp_common`` saves under), refuses it on a classes discovery, and carries it
onto the job record and the queue message the discovery job reads. A request that
names no group produces exactly the record and message it always has.
"""

import importlib
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

DISCOVERY_BUCKET = "discovery-bucket"

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _event(arguments):
    return {
        "info": {"fieldName": "uploadDiscoveryDocument"},
        "arguments": arguments,
        "identity": {
            "claims": {
                "cognito:groups": ["Admin"],
                "email": "admin@example.com",
                "sub": "11111111-2222-3333-4444-555555555555",
            }
        },
    }


@pytest.fixture
def index(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("DISCOVERY_BUCKET", DISCOVERY_BUCKET)
    monkeypatch.setenv("DISCOVERY_TRACKING_TABLE", "tracking-table")
    monkeypatch.setenv("DISCOVERY_QUEUE_URL", "https://sqs.example/queue")
    import index as module

    importlib.reload(module)
    table = mock.MagicMock()
    table.get_item.return_value = {}
    monkeypatch.setattr(module, "dynamodb", mock.MagicMock(Table=lambda _n: table))
    monkeypatch.setattr(module, "sqs_client", mock.MagicMock())
    module._test_table = table
    return module


def _upload(index, **extra):
    arguments = {
        "fileName": "NCCI Manual Ch2.pdf",
        "bucket": DISCOVERY_BUCKET,
        "version": "rules-profile",
        "discoveryType": "rules",
        **extra,
    }
    return index.handler(_event(arguments), None)


def _job_item(index):
    return index._test_table.put_item.call_args.kwargs["Item"]


def _message(index):
    return json.loads(index.sqs_client.send_message.call_args.kwargs["MessageBody"])


class TestPolicyGroupArgument:
    def test_a_named_group_reaches_the_job_record_and_the_message(self, index):
        _upload(index, policyGroup="  NCCI   Manual ")
        assert _job_item(index)["policyGroup"] == "NCCI Manual"
        assert _message(index)["policyGroup"] == "NCCI Manual"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_no_group_leaves_record_and_message_as_they_were(self, index, value):
        extra = {} if value is None else {"policyGroup": value}
        _upload(index, **extra)
        assert "policyGroup" not in _job_item(index)
        assert "policyGroup" not in _message(index)

    @pytest.mark.parametrize("value", ["manual/ch1", "-manual", "x" * 65, 7])
    def test_an_invalid_group_is_refused_before_anything_is_created(self, index, value):
        with pytest.raises(ValueError, match="policyGroup"):
            _upload(index, policyGroup=value)
        index._test_table.put_item.assert_not_called()
        index.sqs_client.send_message.assert_not_called()

    def test_a_group_on_a_classes_discovery_is_refused(self, index):
        with pytest.raises(ValueError, match="only to Policy Discovery"):
            _upload(index, discoveryType="classes", policyGroup="NCCI Manual")
        index._test_table.put_item.assert_not_called()

    def test_a_group_with_replace_is_refused_before_anything_is_cleared(
        self, index, monkeypatch
    ):
        # Replace empties every policy class in the profile, the group's included.
        clear = mock.MagicMock()
        monkeypatch.setattr(index, "_clear_version_schema", clear)
        monkeypatch.setattr(index, "_active_version", lambda: "production")
        with pytest.raises(ValueError, match="saveMode 'replace'"):
            _upload(index, saveMode="replace", policyGroup="NCCI Manual")
        clear.assert_not_called()
        index._test_table.put_item.assert_not_called()

    def test_replace_without_a_group_is_still_allowed(self, index, monkeypatch):
        clear = mock.MagicMock()
        monkeypatch.setattr(index, "_clear_version_schema", clear)
        monkeypatch.setattr(index, "_active_version", lambda: "production")
        _upload(index, saveMode="replace")
        clear.assert_called_once_with("rules-profile", discovery_type="rules")

    def test_a_group_with_page_ranges_is_refused(self, index):
        # Page ranges become classes jobs here, which would drop the group.
        with pytest.raises(ValueError, match="pageRanges"):
            _upload(index, policyGroup="NCCI Manual", pageRanges=["1-3"])
        index._test_table.put_item.assert_not_called()
