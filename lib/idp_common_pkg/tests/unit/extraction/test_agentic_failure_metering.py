# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An agentic run that fails is metered for every model call it completed (#1923).

``structured_output_async`` built its metering only on the success branch. When the
agent loop raised — throttle exhaustion, an invalid tool-use sequence, a context
overflow, a ``max_tokens`` stop with nothing to salvage — or ended without a valid
extraction, every turn the model had already answered was billed and reported
nowhere.

Everything here runs the real ``Agent``, the real ``BedrockModel`` and the real
retry ladder. Only the boto3 ``bedrock-runtime`` client's ``converse_stream`` is
replaced, by a script of Converse stream events (or errors), one entry per model
call, and the ladder's ``asyncio.sleep`` is made instant.

The expected metering is not restated from the stub. ``_per_call_metering`` runs the
unchanged success path over two calls on the same stub and reads back what it
reports; a failed run of ``n`` completed calls must report ``n/2`` of that, under
the same key and in the same units.

Placement: one level above ``agentic_idp/`` and restoring the real ``strands``
package before import, as ``test_invalid_tool_use_sequence.py`` does.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import botocore.exceptions
import pytest
from pydantic import BaseModel

for _name in [
    k for k in list(sys.modules) if k == "strands" or k.startswith("strands.")
]:
    if isinstance(sys.modules[_name], MagicMock):
        del sys.modules[_name]

_agentic = pytest.importorskip(
    "idp_common.extraction.agentic_idp",
    reason="strands-agents not installed (a stub does not count)",
)

from idp_common.bedrock.model_utils import metering_model_id  # noqa: E402
from idp_common.config.models import IDPConfig  # noqa: E402
from idp_common.utils import bedrock_utils  # noqa: E402
from idp_common.utils.metering import failure_metering  # noqa: E402

pytestmark = pytest.mark.unit

_MODEL = "us.anthropic.claude-sonnet-4-6"
_CONTEXT = "Extraction"
_KEY = f"{_CONTEXT}/bedrock/{metering_model_id(_MODEL)}"

#: What the stub reports for every model call. Cache fields included so a unit the
#: failure path dropped would show.
_USAGE = {
    "inputTokens": 1000,
    "outputTokens": 50,
    "totalTokens": 1050,
    "cacheReadInputTokens": 300,
    "cacheWriteInputTokens": 20,
}


class _Invoice(BaseModel):
    invoice_number: str


# --------------------------------------------------------------------------- stub


def _tool_call(payload: dict[str, Any], n: int) -> list[dict[str, Any]]:
    return [
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": f"t{n}", "name": "extraction_tool"}}
            }
        },
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(payload)}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": dict(_USAGE), "metrics": {"latencyMs": 1}}},
    ]


def _text(stop_reason: str = "end_turn") -> list[dict[str, Any]]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "I looked at the document."}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": stop_reason}},
        {"metadata": {"usage": dict(_USAGE), "metrics": {"latencyMs": 1}}},
    ]


def _throttle() -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}},
        "ConverseStream",
    )


def _invalid_tool_use() -> botocore.exceptions.EventStreamError:
    return botocore.exceptions.EventStreamError(
        {
            "Error": {
                "Code": "modelStreamErrorException",
                "Message": "Model produced invalid sequence as part of ToolUse.",
            }
        },
        "ConverseStream",
    )


def _overflow() -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "Input is too long for requested model.",
            }
        },
        "ConverseStream",
    )


VALID = "valid"
INVALID = "invalid"
TEXT = "text"
MAX_TOKENS = "max_tokens"
THROTTLE = "throttle"
BAD_TOOL_USE = "bad_tool_use"
OVERFLOW = "overflow"


class _Script:
    """One entry per ``converse_stream`` call; ``tail`` repeats once it runs out."""

    def __init__(self, steps: list[str], tail: str | None = None) -> None:
        self.steps = list(steps)
        self.tail = tail
        self.calls = 0
        self.answered = 0

    def __call__(self, **_request: Any) -> dict[str, Any]:
        self.calls += 1
        if self.steps:
            step = self.steps.pop(0)
        elif self.tail is not None:
            step = self.tail
        else:
            raise AssertionError("the agent made a model call the script did not plan")
        if step == THROTTLE:
            raise _throttle()
        if step == BAD_TOOL_USE:
            raise _invalid_tool_use()
        if step == OVERFLOW:
            raise _overflow()
        self.answered += 1
        if step == VALID:
            return {
                "stream": _tool_call(
                    {"extraction": {"invoice_number": "INV-1"}}, self.calls
                )
            }
        if step == INVALID:
            return {"stream": _tool_call({"extraction": {}}, self.calls)}
        if step == TEXT:
            return {"stream": _text()}
        if step == MAX_TOKENS:
            return {"stream": _text("max_tokens")}
        raise AssertionError(f"unknown step {step!r}")


async def _instant_sleep(_seconds: float, *args: Any, **kwargs: Any) -> None:
    return None


def _stubbed(script: _Script):
    """Every bedrock-runtime client created inside the block answers from ``script``."""
    real_client = boto3.session.Session.client

    def _client(self, *args: Any, **kwargs: Any):
        client = real_client(self, *args, **kwargs)
        client.converse_stream = script
        return client

    mp = pytest.MonkeyPatch()
    mp.setenv("AWS_DEFAULT_REGION", "us-west-2")
    mp.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    mp.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY")
    mp.delenv("AWS_SESSION_TOKEN", raising=False)
    mp.delenv("AWS_PROFILE", raising=False)
    mp.setattr(boto3.session.Session, "client", _client)
    mp.setattr(bedrock_utils.asyncio, "sleep", _instant_sleep)
    return mp


def _run(script: _Script):
    mp = _stubbed(script)
    try:
        return asyncio.run(
            _agentic.structured_output_async(
                model_id=_MODEL,
                data_format=_Invoice,
                prompt="Extract the invoice number.",
                config=IDPConfig(),
                context=_CONTEXT,
                strands_throttle_retries=False,
            )
        )
    finally:
        mp.undo()


def _scaled(metering: dict[str, Any], factor: float) -> dict[str, Any]:
    return {
        k: {u: v * factor for u, v in units.items()} for k, units in metering.items()
    }


_PER_CALL: dict[str, Any] | None = None


def _per_call_metering() -> dict[str, Any]:
    """What the success path reports for ONE call, read back from a 2-call run."""
    global _PER_CALL
    if _PER_CALL is None:
        script = _Script([VALID, TEXT])
        result, response = _run(script)
        assert result == _Invoice(invoice_number="INV-1")
        assert script.answered == 2
        metering = {
            k: dict(v) for k, v in response["metering"].items() if not k.startswith("_")
        }
        assert list(metering) == [_KEY]
        # The authority is non-vacuous: it recorded the stubbed usage, every unit.
        assert metering[_KEY] == {u: 2 * v for u, v in _USAGE.items()}
        _PER_CALL = _scaled(metering, 0.5)
    return _PER_CALL


def _expected(calls: int) -> dict[str, Any]:
    return _scaled(_per_call_metering(), calls)


# ----------------------------------------------------------------- failure shapes


FAILURES = [
    pytest.param(
        [TEXT, TEXT, TEXT],
        None,
        ValueError,
        3,
        id="ends-without-an-extraction-after-every-feedback-round",
    ),
    pytest.param(
        [INVALID, MAX_TOKENS],
        None,
        Exception,
        2,
        id="max-tokens-with-nothing-to-salvage",
    ),
    pytest.param(
        [INVALID],
        THROTTLE,
        Exception,
        1,
        id="throttle-ladder-exhausted",
    ),
    pytest.param(
        [INVALID, THROTTLE, INVALID],
        THROTTLE,
        Exception,
        2,
        id="throttle-retried-then-exhausted",
    ),
    pytest.param(
        [INVALID],
        OVERFLOW,
        ValueError,
        1,
        id="context-window-overflow",
    ),
    pytest.param(
        [INVALID, INVALID],
        BAD_TOOL_USE,
        _agentic.ModelInvalidToolUseSequence,
        2,
        id="invalid-tool-use-sequence",
    ),
]


@pytest.mark.parametrize("steps,tail,error_type,answered", FAILURES)
def test_a_failed_run_carries_the_metering_of_every_call_it_completed(
    steps, tail, error_type, answered
):
    expected = _expected(answered)
    script = _Script(steps, tail)
    with pytest.raises(error_type) as info:
        _run(script)
    # The script was consumed as planned, so ``answered`` is a fact about this run
    # rather than an assumption about how the agent loop behaves.
    assert script.answered == answered
    assert failure_metering(info.value) == expected


def test_the_last_failure_keeps_its_own_type():
    """Step Functions and the service match on the class: metering must not wrap it."""
    with pytest.raises(_agentic.ModelInvalidToolUseSequence) as info:
        _run(_Script([INVALID], BAD_TOOL_USE))
    assert type(info.value) is _agentic.ModelInvalidToolUseSequence


def test_a_run_that_completed_no_call_attaches_nothing():
    """No response, no usage: nothing is invented for a call that never answered."""
    script = _Script([], THROTTLE)
    with pytest.raises(Exception) as info:
        _run(script)
    assert script.answered == 0
    assert failure_metering(info.value) == {}


def test_a_second_attach_replaces_rather_than_adds():
    """Each reading is cumulative, so summing two would count earlier calls twice."""
    from idp_common.utils.metering import attach_failure_metering

    error = ValueError("x")
    attach_failure_metering(error, _expected(1))
    attach_failure_metering(error, _expected(2))
    assert failure_metering(error) == _expected(2)
    # A copy: mutating what was read does not change what the exception carries.
    failure_metering(error)[_KEY]["inputTokens"] = 0
    assert failure_metering(error) == _expected(2)


def test_a_retried_success_counts_each_call_once():
    """The success path reads the same cumulative total; a resumed retry adds no
    second reading of the calls before it."""
    script = _Script([VALID, THROTTLE, TEXT])
    _result, response = _run(script)
    assert script.answered == 2
    metering = {k: v for k, v in response["metering"].items() if not k.startswith("_")}
    assert {k: dict(v) for k, v in metering.items()} == _expected(2)


# --------------------------------------------------------- the callers that keep it


_ESCALATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "$id": "Invoice",
    "x-aws-idp-document-type": "Invoice",
    "required": ["invoice_number"],
    "properties": {
        "invoice_number": {"type": "string", "pattern": "^INV-[0-9]+$"},
    },
}


def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class _SectionInfo:
    class_label = "Invoice"


def test_a_failed_agentic_escalation_is_metered_and_keeps_the_original():
    """Escalation swallows its failure and the document carries on, so this is the
    path on which the recovered usage reaches the document's cost report."""
    from idp_common.extraction.service import ExtractionService

    svc = ExtractionService(
        region="us-west-2",
        config={
            "extraction": {
                "model": "us.amazon.nova-pro-v1:0",
                "agentic": {
                    "enabled": True,
                    "validation": {
                        "enabled": True,
                        "fail_action": "escalate",
                        "escalation_model": _MODEL,
                    },
                },
            },
            "classes": [_ESCALATION_SCHEMA],
        },
    )
    svc._class_schema = _ESCALATION_SCHEMA

    class _Full(BaseModel):
        invoice_number: str | None = None

    bad = {"invoice_number": "not-an-invoice-id"}
    script = _Script([TEXT, TEXT, TEXT])
    mp = _stubbed(script)
    try:
        fields, _data, meta, metering, _ok = svc._validate_and_maybe_escalate(
            extracted_fields=dict(bad),
            structured_data=_Full(**bad),
            data_model=_Full,
            model_id="us.amazon.nova-pro-v1:0",
            message_prompt="(the document)",
            agentic_images=[],
            custom_instruction=None,
            section_info=_SectionInfo(),
            parsing_succeeded=True,
        )
    finally:
        mp.undo()

    assert script.answered == 3
    assert meta is not None and meta["escalated"] is True
    assert meta["resolved_by_escalation"] is False
    assert fields == bad
    escalation_key = f"ExtractionEscalation/bedrock/{metering_model_id(_MODEL)}"
    assert metering == {escalation_key: _expected(3)[_KEY]}


def _section_failures():
    from idp_common.extraction.service import ExtractionInputTooLarge

    return [
        pytest.param(
            [TEXT, TEXT, TEXT],
            None,
            ValueError,
            "Failed to generate valid structured output",
            3,
            id="no-valid-extraction",
        ),
        # The service re-raises an overflow under its own class; the usage has to
        # be merged before that branch, not only on the generic re-raise.
        pytest.param(
            [INVALID],
            OVERFLOW,
            ExtractionInputTooLarge,
            None,
            1,
            id="context-overflow-re-raised-as-ExtractionInputTooLarge",
        ),
    ]


@pytest.mark.parametrize("steps,tail,error_type,match,answered", _section_failures())
def test_a_section_that_fails_adds_its_usage_to_the_document_before_re_raising(
    steps, tail, error_type, match, answered
):
    """``process_document_section`` mutates the document it was given, so the usage
    has to be on that object when the exception leaves it."""
    from idp_common.extraction.service import ExtractionService
    from idp_common.models import Document, Page, Section, Status

    prior = {"OCR/textract/analyze_document": {"pages": 1}}
    document = Document(
        id="doc",
        input_key="doc.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.EXTRACTING,
        metering=json.loads(json.dumps(prior)),
    )
    document.pages["1"] = Page(
        page_id="1",
        image_uri="s3://in/doc.pdf/1.png",
        parsed_text_uri="s3://in/doc.pdf/1.txt",
    )
    document.sections.append(
        Section(section_id="1", classification="Invoice", page_ids=["1"])
    )
    svc = ExtractionService(
        region="us-west-2",
        config={
            "extraction": {
                "model": _MODEL,
                "agentic": {"enabled": True},
                "task_prompt": "Extract from {DOCUMENT_TEXT}",
            },
            "classes": [
                {
                    "type": "object",
                    "$id": "Invoice",
                    "x-aws-idp-document-type": "Invoice",
                    "properties": {"invoice_number": {"type": "string"}},
                }
            ],
        },
    )

    expected = _expected(answered)
    script = _Script(steps, tail)
    mp = _stubbed(script)
    try:
        with (
            patch("idp_common.s3.get_text_content", return_value="INV-1"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch("idp_common.s3.write_content"),
            patch("idp_common.metrics.put_metric"),
            pytest.raises(error_type, match=match) as info,
        ):
            svc.process_document_section(document, "1")
    finally:
        mp.undo()

    assert type(info.value) is error_type
    assert script.answered == answered
    assert document.metering == {**prior, **expected}
