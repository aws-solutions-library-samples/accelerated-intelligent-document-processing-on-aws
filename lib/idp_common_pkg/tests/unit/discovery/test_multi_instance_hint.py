# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#765: Discovery asks how many records of the class the sample holds, strips
the answer from the schema, and returns a suggestion — never a config write."""

import json
from unittest.mock import MagicMock, patch

import pytest

from idp_common.config.models import IDPConfig
from idp_common.discovery.classes_discovery import (
    DISCOVERY_INSTANCE_COUNT_KEY,
    ClassesDiscovery,
    build_multi_instance_hint,
    pop_instance_count,
)

pytestmark = pytest.mark.unit


def _schema(**extra):
    base = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "Paystub",
        "x-aws-idp-document-type": "Paystub",
        "type": "object",
        "description": "A pay statement",
        "properties": {"employee": {"type": "string", "description": "name"}},
    }
    base.update(extra)
    return base


class TestPopInstanceCount:
    def test_pops_a_top_level_integer(self):
        s = _schema(**{DISCOVERY_INSTANCE_COUNT_KEY: 3})
        assert pop_instance_count(s) == 3
        assert DISCOVERY_INSTANCE_COUNT_KEY not in s

    def test_removes_a_misplaced_property_so_it_never_becomes_a_field(self):
        s = _schema()
        s["properties"][DISCOVERY_INSTANCE_COUNT_KEY] = {"type": "integer"}
        assert pop_instance_count(s) is None
        assert DISCOVERY_INSTANCE_COUNT_KEY not in s["properties"]

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2", 2),
            (2.0, 2),
            (0, None),
            (True, None),
            ("x", None),
            (1.5, None),
            (None, None),
        ],
    )
    def test_coercion(self, raw, expected):
        assert (
            pop_instance_count(_schema(**{DISCOVERY_INSTANCE_COUNT_KEY: raw}))
            == expected
        )


class TestBuildHint:
    def test_one_record_is_no_hint(self):
        assert build_multi_instance_hint(_schema(), 1) is None
        assert build_multi_instance_hint(_schema(), None) is None

    def test_several_records_name_the_class_the_setting_and_the_alternative(self):
        h = build_multi_instance_hint(_schema(), 3)
        assert h["instance_count"] == 3 and h["class_name"] == "Paystub"
        assert h["already_multi_instance"] is False
        assert "x-aws-idp-multi-instance" in h["message"]
        assert "section splitting" in h["message"]
        assert "did not change the class" in h["message"]

    def test_already_flagged_class_says_so(self):
        h = build_multi_instance_hint(_schema(**{"x-aws-idp-multi-instance": True}), 2)
        assert h["already_multi_instance"] is True
        assert "already enabled" in h["message"]


@pytest.fixture
def service():
    with (
        patch("boto3.resource"),
        patch("idp_common.bedrock.BedrockClient"),
        patch("idp_common.discovery.classes_discovery.ConfigurationReader") as reader,
        patch("idp_common.discovery.classes_discovery.ConfigurationManager"),
        patch.dict("os.environ", {"CONFIGURATION_TABLE_NAME": "t"}),
    ):
        reader.return_value.get_merged_configuration.return_value = IDPConfig()
        svc = ClassesDiscovery(
            input_bucket="b", input_prefix="doc.pdf", region="us-west-2"
        )
        svc.bedrock_client = MagicMock()
        svc.bedrock_client.invoke_model.return_value = {"response": {}}
        yield svc


def _run(svc, reply: dict, save=False):
    with (
        patch(
            "idp_common.discovery.classes_discovery.S3Util.get_bytes",
            return_value=b"%PDF",
        ),
        patch(
            "idp_common.bedrock.extract_text_from_response",
            return_value=json.dumps(reply),
        ),
        patch.object(svc, "_merge_and_save_class") as merge,
    ):
        result = svc.discovery_classes_with_document(
            "b", "doc.pdf", save_to_config=save
        )
    return result, merge


def test_prompt_asks_for_the_count_with_the_probe_wording(service):
    _run(service, _schema(**{DISCOVERY_INSTANCE_COUNT_KEY: 1}))
    prompt = "".join(
        c.get("text", "")
        for c in service.bedrock_client.invoke_model.call_args.kwargs["content"]
    )
    assert DISCOVERY_INSTANCE_COUNT_KEY in prompt
    assert "Do not count pages, sections or repeated headers" in prompt
    assert "DIAGNOSTIC METADATA" in prompt


def test_several_records_return_a_hint_and_leave_the_schema_clean(service):
    result, merge = _run(
        service, _schema(**{DISCOVERY_INSTANCE_COUNT_KEY: 3}), save=True
    )
    assert result["status"] == "SUCCESS"
    assert DISCOVERY_INSTANCE_COUNT_KEY not in result["schema"]
    assert DISCOVERY_INSTANCE_COUNT_KEY not in json.dumps(merge.call_args.args[0])
    hint = result["multi_instance_hint"]
    assert hint["instance_count"] == 3 and hint["class_name"] == "Paystub"
    # the class itself was NOT flagged
    assert "x-aws-idp-multi-instance" not in result["schema"]


def test_one_record_returns_no_hint(service):
    result, _ = _run(service, _schema(**{DISCOVERY_INSTANCE_COUNT_KEY: 1}))
    assert result["multi_instance_hint"] is None


def test_reply_without_the_count_still_succeeds_with_no_hint(service):
    result, _ = _run(service, _schema())
    assert result["status"] == "SUCCESS" and result["multi_instance_hint"] is None


def test_hint_reflects_a_flag_the_merge_carried_forward(service):
    """Re-discovering a class the author already flagged (#764 keeps the flag):
    the hint must say the setting is already on rather than ask again."""

    def carry(new_class):
        new_class["x-aws-idp-multi-instance"] = True

    with (
        patch(
            "idp_common.discovery.classes_discovery.S3Util.get_bytes",
            return_value=b"%PDF",
        ),
        patch(
            "idp_common.bedrock.extract_text_from_response",
            return_value=json.dumps(_schema(**{DISCOVERY_INSTANCE_COUNT_KEY: 2})),
        ),
        patch.object(service, "_merge_and_save_class", side_effect=carry),
    ):
        result = service.discovery_classes_with_document(
            "b", "doc.pdf", save_to_config=True
        )
    assert result["multi_instance_hint"]["already_multi_instance"] is True
