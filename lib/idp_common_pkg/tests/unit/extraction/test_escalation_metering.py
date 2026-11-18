# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A Simple-mode escalation call is metered whatever becomes of its answer.

When the escalation model's answer could not be parsed, the failure handler
returned `{}` in place of the call's metering, so a call that was made and billed
reached neither the section's nor the document's usage.

Drives `ExtractionService._validate_simple_result` with the real
`BedrockClient.invoke_model` and only the boto3 `bedrock-runtime` client stubbed.
The expected metering is what that client meters for one call on the same stub,
so the assertion does not restate the client's units.
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from idp_common.bedrock.client import BedrockClient
from idp_common.bedrock.model_utils import metering_model_id
from idp_common.config.models import IDPConfig
from idp_common.extraction.service import ExtractionService, SectionInfo

pytestmark = pytest.mark.unit

_PRIMARY = "us.amazon.nova-2-lite-v1:0"
_ESCALATION = "us.anthropic.claude-sonnet-4-6"
# Written out rather than imported from the service, so renaming the context
# there fails here. Advanced-mode escalation meters under the same literal
# (test_agentic_failure_metering, test_field_subset_escalation): one context for
# both modes is what keeps escalation on one row of a cost report.
_CONTEXT = "ExtractionEscalation"
_KEY = f"{_CONTEXT}/bedrock/{metering_model_id(_ESCALATION)}"
_PRIOR = {f"Extraction/bedrock/{_PRIMARY}": {"inputTokens": 50, "outputTokens": 9}}

_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": "string"},
        "amount": {"type": "number"},
        "due_date": {"type": "string", "format": "date"},
    },
    "required": ["invoice_number", "amount", "due_date"],
}
# `due_date` fails `format: date`, so it is the one field escalated.
_ORIGINAL = {"invoice_number": "INV-1", "amount": 10.0, "due_date": "March 15th"}


def _stub_client(reply: str) -> BedrockClient:
    client = BedrockClient(region="us-east-1", metrics_enabled=False)
    client._client = MagicMock()
    client._client.converse.return_value = {
        "output": {"message": {"content": [{"text": reply}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 40, "outputTokens": 6, "totalTokens": 46},
    }
    return client


def _one_call_metering() -> Dict[str, Any]:
    metering = _stub_client("{}").invoke_model(
        model_id=_ESCALATION,
        system_prompt="s",
        content=[{"text": "t"}],
        context=_CONTEXT,
    )["metering"]
    assert list(metering) == [_KEY]
    assert metering[_KEY]["inputTokens"] == 40
    return metering


def _service() -> ExtractionService:
    cfg = IDPConfig(
        **{
            "extraction": {
                "model": _PRIMARY,
                "agentic": {"enabled": False},
                "validation": {
                    "enabled": True,
                    "fail_action": "escalate",
                    "escalation_model": _ESCALATION,
                },
            }
        }
    )
    svc = ExtractionService(config=cfg)
    svc._class_schema = _SCHEMA
    svc._class_label = "invoice"
    return svc


def _run(reply: str) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], int]:
    client = _stub_client(reply)
    metering = json.loads(json.dumps(_PRIOR))
    with patch("idp_common.bedrock.invoke_model", client.invoke_model):
        fields, meta, _ok = _service()._validate_simple_result(
            extracted_fields=dict(_ORIGINAL),
            content=[{"text": "doc"}],
            system_prompt="sys",
            model_id=_PRIMARY,
            metering=metering,
            section_info=SectionInfo(
                class_label="invoice",
                sorted_page_ids=["1"],
                page_indices=[0],
                output_bucket="b",
                output_key="k",
                output_uri="s3://b/k",
                start_page=1,
                end_page=1,
            ),
            parsing_succeeded=True,
        )
    assert meta is not None
    return fields, meta, metering, client._client.converse.call_count


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("I could not find those fields.", id="not-json"),
        pytest.param('{"due_date": ', id="truncated-json"),
        pytest.param("[1, 2]", id="json-but-not-an-object"),
        pytest.param('{"due_date": "also not a date"}', id="answer-rejected"),
        pytest.param('{"due_date": "2024-03-15"}', id="answer-kept"),
    ],
)
def test_the_escalation_call_is_metered_whatever_its_answer(reply: str) -> None:
    fields, meta, metering, calls = _run(reply)
    assert calls == 1
    assert meta["escalated"] is True
    assert metering == {**_PRIOR, **_one_call_metering()}


@pytest.mark.parametrize("reply", ["I could not find those fields.", '{"due_date": '])
def test_an_unparseable_answer_still_keeps_the_original_extraction(reply: str) -> None:
    """The metering fix changes what is billed, not what is kept."""
    fields, meta, _metering, _calls = _run(reply)
    assert fields == _ORIGINAL
    assert meta["escalation_kept"] is False
    assert meta["escalation_decision"].startswith("escalation call failed: ")


def test_a_call_that_raised_meters_nothing() -> None:
    """No response, no usage to record: the prior metering is unchanged."""
    client = _stub_client("{}")
    client._client.converse.side_effect = ValueError("no response")
    metering = json.loads(json.dumps(_PRIOR))
    svc = _service()
    with patch("idp_common.bedrock.invoke_model", client.invoke_model):
        svc._validate_simple_result(
            extracted_fields=dict(_ORIGINAL),
            content=[{"text": "doc"}],
            system_prompt="sys",
            model_id=_PRIMARY,
            metering=metering,
            section_info=SectionInfo(
                class_label="invoice",
                sorted_page_ids=["1"],
                page_indices=[0],
                output_bucket="b",
                output_key="k",
                output_uri="s3://b/k",
                start_page=1,
                end_page=1,
            ),
            parsing_succeeded=True,
        )
    assert metering == _PRIOR
