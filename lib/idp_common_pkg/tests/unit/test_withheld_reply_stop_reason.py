# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A reply Bedrock withholds with a ``stopReason`` is handled as a content-filter
block (follow-up to #2038).

Bedrock reports a withheld reply two ways. #2075 and #2079 handled the first, a
``ValidationException`` carrying ``CONTENT_FILTER_BLOCK_MESSAGE``. The second is a
``200`` whose ``stopReason`` is ``content_filtered`` or ``guardrail_intervened``
(``StopReason`` in the ``bedrock-runtime`` service model), with the reply empty,
cut short, or replaced by the guardrail's own text. Nothing read that stop
reason, so each step read the text as the model's answer:

* classification, page granularity — an empty reply failed the document ("LLM
  returned empty response" in ``document.errors``); any other text became a
  warning-severity invalid-class fallback that did not name the block;
* summarization — an empty reply failed the document; any other text was stored
  as a summary that failed to parse;
* extraction — the text was parsed as the extraction;
* agentic extraction — three rounds with no extraction, then a generic
  "Failed to generate valid structured output" that failed the document.

Each step now raises ``ModelOutputWithheld``, which ``is_content_filter_block``
recognises, where it would have raised a blocked ``ValidationException``.

**Instrument.** The real services and the real ``BedrockClient``, with the
``bedrock-runtime`` client's HTTP replies supplied through botocore's
``before-send`` event so botocore's parser builds the response; the agentic case
uses a real Strands ``Agent`` over a real streaming ``BedrockModel`` with
hand-framed event streams (the harness of ``test_validation_exception_retry_rule``).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

import botocore.session
import pytest
from botocore.awsrequest import AWSResponse

from idp_common.bedrock import client as bedrock_client_module
from idp_common.bedrock.client import BedrockClient
from idp_common.classification.service import ClassificationService
from idp_common.config.models import IDPConfig
from idp_common.config.schema_constants import UNCLASSIFIED_CLASS
from idp_common.extraction import agentic_idp
from idp_common.extraction.failure import EXTRACTION_FAILED_CODE
from idp_common.extraction.service import ExtractionService
from idp_common.lambda_deadline import set_lambda_deadline_epoch
from idp_common.models import Document, Page, Section, Status
from idp_common.summarization.service import SummarizationService
from idp_common.utils import failure_metering
from idp_common.utils.bedrock_utils import (
    WITHHELD_OUTPUT_STOP_REASONS,
    ModelOutputWithheld,
    is_content_filter_block,
    is_guardrail_intervention,
    raise_if_output_withheld,
    withheld_stop_reason,
)
from tests.unit._handler_loading import load_with_xray_stubbed
from tests.unit.utils import test_validation_exception_retry_rule as stream_harness

pytestmark = pytest.mark.unit

_ROOT = os.path.join(os.path.dirname(__file__), "../../../..")


def _service_model_stop_reasons() -> List[str]:
    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    return list(model.shape_for("StopReason").enum)


#: The two withheld-reply stop reasons, named here rather than read from the
#: production tuple (a test derived from the tuple shrinks with it), and checked
#: against the service model's enum below.
_REASONS = ["content_filtered", "guardrail_intervened"]
_GUARDRAIL_TEXT = "Sorry, the model cannot answer this question."
_REPLIES = [
    pytest.param(reason, content, id=f"{reason}-{label}")
    for reason in _REASONS
    for label, content in (
        ("empty", []),
        ("partial", [{"text": '{"class": "invoice"'}]),
        ("text", [{"text": _GUARDRAIL_TEXT}]),
        # Well-formed JSON a parser would accept: the stop reason alone says no.
        ("json", [{"text": '{"class": "invoice", "summary": "x"}'}]),
    )
]


def test_the_stop_reasons_are_in_the_service_model():
    """Both are real stop reasons, both are the production set, and an empty
    list would collect every parametrised test as a skip."""
    assert set(_REASONS) <= set(_service_model_stop_reasons())
    assert sorted(_REASONS) == sorted(WITHHELD_OUTPUT_STOP_REASONS)
    assert len(_REPLIES) == 4 * len(WITHHELD_OUTPUT_STOP_REASONS)


class _Body:
    def __init__(self, payload: Dict[str, Any]):
        self._bytes = json.dumps(payload).encode()

    def stream(self, **_kwargs):
        yield self._bytes


def _guardrail_trace(policy: str, action: str) -> Dict[str, Any]:
    """A Converse guardrail trace with one finding, in the service model's shape
    (``GuardrailTraceAssessment``); botocore's parser drops anything else."""
    if policy == "contentPolicy":
        finding = {
            "contentPolicy": {
                "filters": [
                    {
                        "type": "VIOLENCE",
                        "confidence": "HIGH",
                        "action": action,
                        "detected": True,
                    }
                ]
            }
        }
    else:
        finding = {
            "sensitiveInformationPolicy": {
                "piiEntities": [
                    {
                        "match": "jane@example.com",
                        "type": "EMAIL",
                        "action": action,
                        "detected": True,
                    }
                ],
                "regexes": [],
            }
        }
    return {"guardrail": {"outputAssessments": {"gr-1": [finding]}}}


#: A guardrail that blocked the reply.
_BLOCKED_TRACE = _guardrail_trace("contentPolicy", "BLOCKED")
#: A guardrail that only masked sensitive information in it.
_MASKED_TRACE = _guardrail_trace("sensitiveInformationPolicy", "ANONYMIZED")
_DEFAULT_TRACE = object()


def _reply(
    content: List[Dict[str, Any]], stop_reason: str, trace: Any = _DEFAULT_TRACE
) -> Dict[str, Any]:
    """A Converse response. A guardrail stop carries a BLOCKED trace unless
    ``trace`` says otherwise (``None`` for no trace at all)."""
    if trace is _DEFAULT_TRACE:
        trace = _BLOCKED_TRACE if stop_reason == "guardrail_intervened" else None
    reply: Dict[str, Any] = {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        "metrics": {"latencyMs": 1},
    }
    if trace is not None:
        reply["trace"] = trace
    return reply


@pytest.fixture
def bedrock(monkeypatch):
    """``install(route, withhold=(chosen, reason, content[, trace]))``: every
    Converse request is answered by ``route(system, text)``, except one for which
    ``chosen(text)`` is true, which gets a 200 with ``reason``, ``content`` and
    ``trace`` (see ``_reply``). Returns the list of request texts."""
    set_lambda_deadline_epoch(None)
    default = bedrock_client_module.default_client
    monkeypatch.setattr(default, "_client", None)
    runtime = default.client
    monkeypatch.setattr(default, "_put_metric", lambda *a, **k: None)
    monkeypatch.setattr(
        BedrockClient, "_backoff_within_invocation", lambda _self, _n: 0.0
    )
    sent: List[str] = []
    holder: Dict[str, Any] = {}

    def before_send(request, **_kwargs):
        body = json.loads(request.body)
        system = "".join(b.get("text", "") for b in body.get("system", []))
        text = "".join(
            block.get("text", "")
            for message in body.get("messages", [])
            for block in message.get("content", [])
        )
        sent.append(text)
        chosen, reason, content, *trace = holder["withhold"]
        if chosen is not None and chosen(text):
            payload = _reply(content, reason, *trace)
        else:
            payload = _reply([{"text": holder["route"](system, text)}], "end_turn")
        return AWSResponse(
            request.url, 200, {"content-type": "application/json"}, _Body(payload)
        )

    runtime.meta.events.register("before-send.bedrock-runtime.Converse", before_send)

    def install(route: Callable[[str, str], str], withhold=(None, "", [])):
        holder["route"] = route
        holder["withhold"] = withhold
        return sent

    install.sent = sent  # pyright: ignore[reportFunctionMemberAccess]
    yield install
    runtime.meta.events.unregister("before-send.bedrock-runtime.Converse", before_send)
    set_lambda_deadline_epoch(None)


def _issues(document: Document, code: str):
    return [
        issue
        for section in document.sections
        for issue in (section.processing_issues or [])
        if issue.code == code
    ]


def _classes(*names: str) -> List[Dict[str, Any]]:
    return [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": name,
            "x-aws-idp-document-type": name,
            "type": "object",
            "description": f"A {name}",
            "properties": {"total": {"type": "string"}},
        }
        for name in names
    ]


def _pages(count: int) -> Document:
    doc = Document(
        id="doc.pdf",
        input_key="doc.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.CLASSIFYING,
    )
    for i in range(1, count + 1):
        doc.pages[str(i)] = Page(
            page_id=str(i), parsed_text_uri=f"s3://in/doc.pdf/pages/{i}/text.txt"
        )
    return doc


def _page_text(uri: str) -> str:
    return f"PAGE-{uri.split('/')[-2]} invoice text"


def _input_tokens(metering: Optional[Dict[str, Any]]) -> int:
    """Input tokens across every metering key: each stubbed call bills 10."""
    return sum(
        int(usage.get("inputTokens", 0))
        for usage in (metering or {}).values()
        if isinstance(usage, dict)
    )


def _all_billed(bedrock, metering) -> None:
    """Every call the stub answered, withheld or not, is in the metering."""
    assert bedrock.sent, "no model call was made"
    assert _input_tokens(metering) == 10 * len(bedrock.sent)


# ------------------------------------------------------------------ the matcher


@pytest.mark.parametrize(("reason", "content"), _REPLIES)
def test_the_rule_reads_the_envelope_and_keeps_its_metering(reason, content):
    envelope = {"response": _reply(content, reason), "metering": {"m": {"x": 1}}}
    assert withheld_stop_reason(envelope) == reason
    assert withheld_stop_reason(envelope["response"]) == reason
    with pytest.raises(ModelOutputWithheld) as caught:
        raise_if_output_withheld(envelope)
    assert caught.value.stop_reason == reason
    assert is_content_filter_block(caught.value)
    assert is_guardrail_intervention(caught.value) == (reason == "guardrail_intervened")
    # The call was billed, so its usage travels with the failure.
    assert failure_metering(caught.value) == {"m": {"x": 1}}


def test_the_trace_shapes_are_the_service_model_s():
    """The fixtures above are only evidence if botocore parses them as written."""
    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    actions = model.shape_for("GuardrailSensitiveInformationPolicyAction").enum
    assert "ANONYMIZED" in actions and "BLOCKED" in actions
    assert "BLOCKED" in model.shape_for("GuardrailContentPolicyAction").enum


@pytest.mark.parametrize(
    ("trace", "withheld"),
    [
        pytest.param(_BLOCKED_TRACE, True, id="blocked"),
        pytest.param(_MASKED_TRACE, False, id="masked-only"),
        pytest.param(None, False, id="no-trace"),
        pytest.param(
            {
                "guardrail": {
                    "inputAssessment": {
                        "gr-1": _BLOCKED_TRACE["guardrail"]["outputAssessments"][
                            "gr-1"
                        ][0]
                    }
                }
            },
            True,
            id="blocked-input",
        ),
    ],
)
def test_a_guardrail_stop_is_withheld_only_when_it_blocked(trace, withheld):
    reply = _reply([{"text": "x"}], "guardrail_intervened", trace)
    assert (withheld_stop_reason({"response": reply}) is not None) == withheld


@pytest.mark.parametrize(
    "reason",
    [r for r in _service_model_stop_reasons() if r not in WITHHELD_OUTPUT_STOP_REASONS],
)
def test_every_other_stop_reason_is_an_answer(reason):
    raise_if_output_withheld({"response": _reply([{"text": "x"}], reason)})
    assert withheld_stop_reason(_reply([], reason)) is None


# ------------------------------------------------------------- classification


def _classify_by_page(bedrock, reason, content, *trace) -> Document:
    service = ClassificationService(
        region="us-west-2",
        config={
            "classes": _classes("invoice", "receipt"),
            "classification": {
                "model": "us.amazon.nova-lite-v1:0",
                "system_prompt": "classify",
                "task_prompt": "{CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
                "granularity": "page",
                "modality": {"text": True, "images": False},
            },
        },
    )
    bedrock(
        lambda _s, _t: '{"class": "invoice", "document_boundary": "continue"}',
        withhold=(lambda text: "PAGE-2 " in text, reason, content, *trace),
    )
    with patch("idp_common.s3.get_text_content", side_effect=_page_text):
        return service.classify_document(_pages(3))


@pytest.mark.parametrize(("reason", "content"), _REPLIES)
def test_classification_marks_the_withheld_page_unclassified(bedrock, reason, content):
    result = _classify_by_page(bedrock, reason, content)

    _all_billed(bedrock, result.metering)
    assert result.errors == []
    assert "failed_page_exceptions" not in (result.metadata or {})
    assert [result.pages[p].classification for p in ("1", "2", "3")] == [
        "invoice",
        UNCLASSIFIED_CLASS,
        "invoice",
    ]
    (issue,) = _issues(result, "classification_failed")
    assert issue.severity == "error"
    assert issue.details["page_ids"] == ["2"]
    assert reason in issue.root_cause
    if reason == "guardrail_intervened":
        assert "guardrail" in issue.root_cause
        # It may have stopped the request before the model saw it.
        assert (
            "BedrockGuardrailVersion) blocked the request or withheld the "
            "classification model's reply"
        ) in issue.root_cause
        assert "different classification model" not in issue.root_cause
    else:
        assert "content filter" in issue.root_cause
        assert "different classification model" in issue.root_cause


def test_classification_control_classifies_every_page(bedrock):
    """A 200 that is an answer: the outcome above is the stop reason's doing."""
    result = _classify_by_page(bedrock, "end_turn", [{"text": '{"class": "receipt"}'}])
    assert result.errors == []
    assert result.pages["2"].classification == "receipt"
    assert _issues(result, "classification_failed") == []


def _split_then_classify(bedrock, reason, content) -> Document:
    service = ClassificationService(
        region="us-west-2",
        config={
            "classes": _classes("invoice", "receipt"),
            "classification": {
                "model": "us.amazon.nova-lite-v1:0",
                "system_prompt": "combined system",
                "task_prompt": "{CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
                "granularity": "packet",
                "strategy": "split_then_classify",
                "confidence": {"mode": "off"},
                "modality": {"text": True, "images": False},
                "segmentation": {
                    "packet": {
                        "system_prompt": "splitting packet system",
                        "task_prompt": "Split.\n{BOUNDARY_HINTS}\n{DOCUMENT_TEXT}",
                    },
                },
                "labeling": {
                    "system_prompt": "section classification system",
                    "task_prompt": "LABEL {CLASS_NAMES_AND_DESCRIPTIONS}\n"
                    "{DOCUMENT_TEXT}",
                },
            },
        },
    )

    def route(system: str, _text: str) -> str:
        if system == "splitting packet system":
            return json.dumps(
                {
                    "segments": [
                        {"ordinal_start_page": n, "ordinal_end_page": n}
                        for n in (1, 2, 3)
                    ]
                }
            )
        return '{"class": "invoice"}'

    # Only the labeling call for the third segment: its prompt starts LABEL.
    bedrock(
        route,
        withhold=(
            lambda text: text.startswith("LABEL") and "PAGE-3 " in text,
            reason,
            content,
        ),
    )
    with patch("idp_common.s3.get_text_content", side_effect=_page_text):
        return service.classify_document(_pages(3))


@pytest.mark.parametrize(("reason", "content"), _REPLIES)
def test_split_then_classify_marks_the_withheld_segment_unclassified(
    bedrock, reason, content
):
    result = _split_then_classify(bedrock, reason, content)
    _all_billed(bedrock, result.metering)
    assert result.errors == []
    assert [s.classification for s in result.sections] == [
        "invoice",
        "invoice",
        UNCLASSIFIED_CLASS,
    ]
    (issue,) = _issues(result, "classification_failed")
    assert issue.severity == "error"
    assert reason in issue.root_cause


# -------------------------------------------------------------- summarization


def _summarize(bedrock, withhold, *, sections: bool):
    bedrock(
        lambda _s, _t: json.dumps({"summary": "fine"}),
        withhold=withhold,
    )
    document = _pages(2)
    document.status = Status.SUMMARIZING
    if sections:
        document.sections = [
            Section(section_id=n, classification="invoice", page_ids=[n])
            for n in ("1", "2")
        ]
    store: Dict[str, Any] = {}

    def write(content, bucket, key, **_kw):
        store[key] = content

    service = SummarizationService(
        region="us-west-2",
        config={
            "summarization": {
                "enabled": True,
                "model": "us.amazon.nova-lite-v1:0",
                "system_prompt": "summarize",
                "task_prompt": "Doc: {DOCUMENT_TEXT}\nAttrs: {EXTRACTION_RESULTS}",
            }
        },
    )
    with (
        patch("idp_common.s3.get_text_content", side_effect=_page_text),
        patch("idp_common.s3.write_content", side_effect=write),
        patch(
            "idp_common.s3.get_json_content",
            side_effect=lambda uri: store[uri.split("/", 3)[3]],
        ),
    ):
        result = service.process_document(document)
    return result, store


@pytest.mark.parametrize(("reason", "content"), _REPLIES)
def test_summarization_stubs_the_withheld_section(bedrock, reason, content):
    result, store = _summarize(
        bedrock, (lambda text: "PAGE-2 " in text, reason, content), sections=True
    )
    assert result.status != Status.FAILED
    assert result.errors == []
    _all_billed(bedrock, result.metering)
    combined = store["doc.pdf/summary/summary.json"]
    assert combined["invoice_1"]["summary"].startswith("fine")
    stub = combined["invoice_2"]["summary"]
    assert stub.startswith("No summary was produced")
    if reason == "guardrail_intervened":
        assert "guardrail" in stub
        assert "different summarization model" not in stub
    else:
        assert "content filter" in stub


@pytest.mark.parametrize(("reason", "content"), _REPLIES)
def test_summarization_whole_document_flags_the_stub(bedrock, reason, content):
    result, store = _summarize(
        bedrock, (lambda _text: True, reason, content), sections=False
    )
    assert result.errors == []
    _all_billed(bedrock, result.metering)
    summary = store["doc.pdf/summary/summary.json"]
    assert summary["metadata"]["summarization_skipped"] is True
    assert summary["metadata"]["skip_reason"] == "content_filtered"
    assert reason in summary["metadata"]["error"]


# ----------------------------------------------------------------- assessment


class _Context:
    function_name = "fn"
    memory_limit_in_mb = 2048
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:fn"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 300_000


def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.parametrize(("reason", "content"), _REPLIES)
def test_assessment_degrades_the_withheld_section(
    bedrock, monkeypatch, reason, content
):
    """The real Assessment Lambda over the real ``AssessmentService``."""
    assessment_index = load_with_xray_stubbed(
        "assessment_index_withheld_reply",
        os.path.join(_ROOT, "patterns/unified/src/assessment_function/index.py"),
    )
    config = IDPConfig.model_validate(
        {
            "classes": _classes("invoice"),
            "extraction": {
                "model": "us.amazon.nova-lite-v1:0",
                "confidence": {
                    "model": "us.amazon.nova-lite-v1:0",
                    "system_prompt": "assess",
                    "task_prompt": "{DOCUMENT_CLASS} {EXTRACTION_RESULTS} "
                    "{DOCUMENT_TEXT}",
                },
            },
        }
    )
    monkeypatch.setenv("WORKING_BUCKET", "")
    monkeypatch.setattr(assessment_index, "get_config", lambda **kw: config)
    monkeypatch.setattr(
        assessment_index, "create_document_service", lambda *a, **kw: MagicMock()
    )
    extraction = {
        "document_class": {"type": "invoice"},
        "inference_result": {"total": "1.00"},
        "metadata": {},
    }
    monkeypatch.setattr(assessment_index.s3, "get_json_content", lambda uri: extraction)
    monkeypatch.setattr(assessment_index.s3, "write_content", lambda *a, **kw: None)
    sent = bedrock(
        lambda _s, _t: '{"total": {"confidence": 0.9}}',
        withhold=(lambda _text: True, reason, content),
    )
    document = _pages(1)
    document.status = Status.EXTRACTING
    document.sections = [
        Section(
            section_id="1",
            classification="invoice",
            page_ids=["1"],
            extraction_result_uri="s3://out/doc.pdf/sections/1/result.json",
        )
    ]
    document.pages["1"].image_uri = "s3://in/doc.pdf/pages/1/image.jpg"
    with (
        patch("idp_common.s3.get_text_content", side_effect=_page_text),
        patch("idp_common.s3.get_json_content", return_value=extraction),
        patch("idp_common.image.prepare_image", return_value=_png()),
        patch("idp_common.metrics.put_metric"),
    ):
        result = assessment_index.handler(
            {"document": document.to_dict(), "section_id": "1"}, _Context()
        )

    assert sent, "the confidence model was called"
    out = Document.from_dict(result["document"])
    assert out.status != Status.FAILED
    _all_billed(bedrock, out.metering)
    (issue,) = _issues(out, "assessment_failed_confidence_unavailable")
    assert issue.severity == "error"
    assert reason in issue.root_cause
    if reason == "guardrail_intervened":
        assert "guardrail" in issue.message
        assert "different confidence model" not in issue.message
    else:
        assert "content filter" in issue.message


# ----------------------------------------------------------------- extraction


_INVOICE = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "invoice",
    "x-aws-idp-document-type": "invoice",
    "type": "object",
    "description": "An invoice",
    "properties": {
        "invoice_number": {"type": "string", "description": "The invoice number"}
    },
}


def _extraction_document() -> Document:
    doc = Document(
        id="doc.pdf",
        input_key="doc.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.EXTRACTING,
        num_pages=2,
    )
    for n in ("1", "2"):
        doc.pages[n] = Page(
            page_id=n,
            image_uri=f"s3://in/doc.pdf/pages/{n}/image.jpg",
            parsed_text_uri=f"s3://in/doc.pdf/pages/{n}/parsed.txt",
        )
        doc.sections.append(
            Section(section_id=n, classification="invoice", page_ids=[n])
        )
    return doc


def _extract_simple(bedrock, reason, content):
    """The REAL ``process_document_section`` and the real Simple-mode model call
    for section 2; only the prompt assembly is replaced."""
    sent = bedrock(
        lambda _s, _t: '{"invoice_number": "INV-1"}',
        withhold=(lambda _text: True, reason, content),
    )
    service = ExtractionService(
        config=IDPConfig.model_validate(
            {
                "classes": [_INVOICE],
                "extraction": {"model": "us.amazon.nova-lite-v1:0"},
            }
        )
    )
    document = _extraction_document()
    with (
        patch.object(
            ExtractionService,
            "_prepare_section_context",
            return_value=([{"text": "page text"}], "system prompt"),
        ),
        patch("idp_common.s3.write_content") as write,
    ):
        result = service.process_document_section(document=document, section_id="2")
    written = write.call_args.args[0] if write.call_args else None
    return result, written, sent


@pytest.mark.parametrize(("reason", "content"), _REPLIES)
def test_simple_extraction_fails_only_the_withheld_section(bedrock, reason, content):
    document, written, sent = _extract_simple(bedrock, reason, content)
    assert len(sent) == 1, "one model call, not retried"
    assert document.errors == []
    (issue,) = _issues(document, EXTRACTION_FAILED_CODE)
    assert issue.severity == "error"
    assert issue.section_id == "2"
    assert reason in issue.root_cause
    assert written is not None
    assert written["inference_result"] == {}
    assert written["metadata"]["content_filtered"] is True
    # The withheld call was billed.
    _all_billed(bedrock, document.metering)


def test_simple_extraction_control_extracts(bedrock):
    document, written, _ = _extract_simple(
        bedrock, "end_turn", [{"text": '{"invoice_number": "INV-9"}'}]
    )
    assert _issues(document, EXTRACTION_FAILED_CODE) == []
    assert written["inference_result"] == {"invoice_number": "INV-9"}


# ------------------------------------------------------- agentic extraction

#: The streaming harness of the retry-rule tests, re-exported as a fixture.
stream_bedrock = stream_harness.bedrock


def _withheld_stream(reason: str) -> bytes:
    event = stream_harness._event
    return b"".join(
        [
            event("messageStart", {"role": "assistant"}),
            event(
                "contentBlockDelta",
                {"delta": {"text": _GUARDRAIL_TEXT}, "contentBlockIndex": 0},
            ),
            event("contentBlockStop", {"contentBlockIndex": 0}),
            event("messageStop", {"stopReason": reason}),
            stream_harness._METADATA,
        ]
    )


def _run_agentic(stream_bedrock, monkeypatch, steps: str):
    from pydantic import BaseModel

    class Invoice(BaseModel):
        invoice_number: str

    for reason in _REASONS:
        monkeypatch.setitem(
            stream_harness._STEPS,
            reason[0].upper(),
            lambda r=reason: _withheld_stream(r),
        )
    make_agent, sent = stream_bedrock
    agent = make_agent(steps)
    result = asyncio.run(
        agentic_idp._invoke_agent_for_extraction(
            agent=agent,
            prompt_content=[{"text": "extract"}],
            data_format=Invoice,
            max_extraction_retries=3,
        )
    )
    return result, sent


@pytest.mark.parametrize("reason", _REASONS)
def test_agentic_extraction_raises_the_block_after_regenerating(
    stream_bedrock, monkeypatch, reason
):
    """Three withheld turns: the feedback rounds regenerate the reply, as the retry
    decorator does for a blocked ValidationException, and the last is raised as
    the block, which the service turns into one failed section."""
    letter = reason[0].upper()
    with pytest.raises(ModelOutputWithheld) as caught:
        _run_agentic(stream_bedrock, monkeypatch, letter * 3)
    assert caught.value.stop_reason == reason
    assert is_content_filter_block(caught.value)
    assert stream_bedrock[1] == [letter] * 3


@pytest.mark.parametrize("reason", _REASONS)
def test_agentic_extraction_regenerated_reply_is_kept(
    stream_bedrock, monkeypatch, reason
):
    """A turn the model then answers ends the run the way it always did."""
    letter = reason[0].upper()
    (response, result), sent = _run_agentic(stream_bedrock, monkeypatch, letter + "OO")
    assert result is None
    assert response.stop_reason == "end_turn"
    assert sent == [letter, "O", "O"]


_REQUIRED_INVOICE = {**_INVOICE, "required": ["invoice_number"]}


def _escalate(bedrock, withhold):
    from idp_common.extraction.validation import validate_extraction

    sent = bedrock(lambda _s, _t: '{"invoice_number": "FROM-ESCALATION"}', withhold)
    service = ExtractionService(
        config=IDPConfig.model_validate(
            {"classes": [_INVOICE], "extraction": {"model": "us.amazon.nova-lite-v1:0"}}
        )
    )
    service._class_schema = _REQUIRED_INVOICE
    original = {"invoice_number": None}
    report = validate_extraction(original, _REQUIRED_INVOICE)
    assert report.failed_top_level_fields == {"invoice_number"}
    result = service._escalate_simple_fields(
        extracted_fields=dict(original),
        report=report,
        escalation_model="us.amazon.nova-pro-v1:0",
        content=[{"text": "page text"}],
        system_prompt="system",
        section_info=MagicMock(class_label="invoice", section_id="1"),
    )
    return result, sent


@pytest.mark.parametrize("reason", _REASONS)
def test_simple_escalation_keeps_the_original_over_a_withheld_reply(bedrock, reason):
    """A withheld escalation reply is not merged, however well it parses."""
    (fields, _report, metering, escalated, _why), sent = _escalate(
        bedrock,
        (lambda _t: True, reason, [{"text": '{"invoice_number": "WITHHELD"}'}]),
    )
    assert len(sent) == 1
    assert fields == {"invoice_number": None}
    assert escalated is False
    assert metering, "the billed call's usage is kept"


def test_simple_escalation_control_merges_an_answer(bedrock):
    (fields, _r, _m, escalated, _why), _sent = _escalate(bedrock, (None, "", []))
    assert escalated is True
    assert fields["invoice_number"] == "FROM-ESCALATION"


# ------------------------------------------- a guardrail that only masked PII


def test_classification_reads_a_masked_only_guardrail_reply_as_the_answer(bedrock):
    """A guardrail that anonymised a match stops with ``guardrail_intervened``
    too; its reply is the model's answer and the page is classified."""
    result = _classify_by_page(
        bedrock,
        "guardrail_intervened",
        [{"text": '{"class": "receipt"}'}],
        _MASKED_TRACE,
    )
    assert result.errors == []
    assert result.pages["2"].classification == "receipt"
    assert _issues(result, "classification_failed") == []


@pytest.mark.parametrize("trace", [_MASKED_TRACE, None], ids=["masked", "no-trace"])
def test_simple_extraction_keeps_a_masked_only_guardrail_reply(bedrock, trace):
    bedrock(
        lambda _s, _t: "unused",
        withhold=(
            lambda _text: True,
            "guardrail_intervened",
            [{"text": '{"invoice_number": "INV-{EMAIL}"}'}],
            trace,
        ),
    )
    service = ExtractionService(
        config=IDPConfig.model_validate(
            {"classes": [_INVOICE], "extraction": {"model": "us.amazon.nova-lite-v1:0"}}
        )
    )
    with (
        patch.object(
            ExtractionService,
            "_prepare_section_context",
            return_value=([{"text": "page text"}], "system prompt"),
        ),
        patch("idp_common.s3.write_content") as write,
    ):
        document = service.process_document_section(
            document=_extraction_document(), section_id="2"
        )
    assert _issues(document, EXTRACTION_FAILED_CODE) == []
    assert write.call_args.args[0]["inference_result"] == {
        "invoice_number": "INV-{EMAIL}"
    }


# ------------------------------------------------ batched confidence assessment


class _BatchedService:
    """``assess_results`` for the batcher: the batch whose first row is ``"3"``
    gets a withheld reply, raised the way ``AssessmentService`` raises it, every
    time it is asked."""

    def __init__(self, reason: str):
        self.reason = reason
        self.calls: List[List[str]] = []

    def assess_results(self, *, extraction_results, **_kw):
        from idp_common.assessment.service import AssessmentCoreResult

        txs = extraction_results.get("txs", [])
        self.calls.append([r["amount"] for r in txs])
        envelope = {
            "response": _reply([], "content_filtered")
            if self.reason == "content_filtered"
            else _reply([{"text": _GUARDRAIL_TEXT}], self.reason),
            "metering": {"Assessment/bedrock/m": {"inputTokens": 10}},
        }
        if txs and txs[0]["amount"] == "3":
            raise_if_output_withheld(envelope)
        return AssessmentCoreResult(
            enhanced_assessment={
                "account": {"confidence": 0.9},
                "txs": [{"amount": {"confidence": 0.9}} for _ in txs],
            },
            metering={"Assessment/bedrock/m": {"inputTokens": 10}},
        )


@pytest.mark.parametrize("reason", _REASONS)
def test_one_withheld_confidence_batch_leaves_only_its_rows_unscored(reason):
    from idp_common.assessment.batching import assess_results_batched

    service = _BatchedService(reason)
    rows = [{"amount": str(i)} for i in range(9)]
    out = assess_results_batched(
        service,
        class_label="statement",
        extraction_results={"account": "A", "txs": rows},
        document_text="t",
        page_images=[],
        batch_size=3,
    )
    scored = [
        row.get("amount", {}).get("confidence") for row in out["assessment"]["txs"]
    ]
    assert scored[:3] == [0.9] * 3 and scored[6:] == [0.9] * 3
    assert all(value is None for value in scored[3:6])
    assert "withheld" in out["assessment"]["txs"][4]["amount"]["confidence_reason"]
    assert out["assessment"]["account"]["confidence"] == 0.9
    # Every call was billed, the withheld batch and its recovery attempts too.
    assert _input_tokens(out["metering"]) == 10 * len(service.calls)
    assert len(service.calls) > 3, "the recovery rung re-offered the withheld rows"
