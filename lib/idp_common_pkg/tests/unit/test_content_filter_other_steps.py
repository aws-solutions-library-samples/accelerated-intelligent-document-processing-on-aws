# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A content-filter block in classification, assessment or summarization fails
only the unit it happened in (follow-up to #2038).

#2038 made a block during extraction fail one section rather than the document.
The same block in the other model-calling steps:

* **classification, page granularity** — a page whose call raised went into
  ``document.errors``, so the Lambda re-raised it and the collate step failed the
  document. Now the page is ``unclassified`` with an error-severity
  ``classification_failed`` issue on its section, as an empty page already was
  (except at warning severity), and the document completes.
* **classification, split-then-classify labeling** — a segment whose labeling call
  failed was already labelled ``unclassified`` with ``classification_failed``, but
  also went into ``document.errors``. A block now does only the first.
* **assessment** — already degraded the section (#901), because a block is not
  transient; the issue now names the block instead of advising on input length.
* **summarization** — returns the same stub an input overflow does, so the
  document completes without a summary.

**Instrument.** The real services and the real ``BedrockClient`` behind them, with
the ``bedrock-runtime`` client's HTTP replies supplied through botocore's
``before-send`` event, so botocore's own parser builds the ``ClientError`` from a
``400`` and an error type. The error type is the ``Converse`` operation's
``ValidationException`` error shape from the botocore service model.
"""

from __future__ import annotations

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
from idp_common.lambda_deadline import set_lambda_deadline_epoch
from idp_common.models import Document, Page, ProcessingIssue, Section, Status
from idp_common.summarization.service import SummarizationService
from idp_common.utils.bedrock_utils import CONTENT_FILTER_BLOCK_MESSAGE
from tests.unit._handler_loading import load_with_xray_stubbed

pytestmark = pytest.mark.unit

_ROOT = os.path.join(os.path.dirname(__file__), "../../../..")

#: A refusal of the request rather than of the reply: still fatal everywhere.
_MALFORMED = "Malformed input request, please reformat your input and try again."


def _validation_error_type() -> str:
    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    names = [shape.name for shape in model.operation_model("Converse").error_shapes]
    assert "ValidationException" in names, names
    return "ValidationException"


class _Body:
    def __init__(self, payload: Dict[str, Any]):
        self._bytes = json.dumps(payload).encode()

    def stream(self, **_kwargs):
        yield self._bytes


def _answer(text: str) -> Dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        "metrics": {"latencyMs": 1},
    }


#: What a router returns: the model's text, or ``_Refuse(message)``.
class _Refuse:
    def __init__(self, message: str):
        self.message = message


@pytest.fixture
def bedrock(monkeypatch):
    """Route every Converse request through ``route(system, text)``; returns the
    list of (system, text) each request carried."""
    set_lambda_deadline_epoch(None)
    default = bedrock_client_module.default_client
    monkeypatch.setattr(default, "_client", None)
    runtime = default.client
    monkeypatch.setattr(default, "_put_metric", lambda *a, **k: None)
    monkeypatch.setattr(
        BedrockClient, "_backoff_within_invocation", lambda _self, _n: 0.0
    )
    sent: List[tuple] = []
    holder: Dict[str, Callable[[str, str], Any]] = {}
    error_type = _validation_error_type()

    def before_send(request, **_kwargs):
        body = json.loads(request.body)
        system = "".join(b.get("text", "") for b in body.get("system", []))
        text = "".join(
            block.get("text", "")
            for message in body.get("messages", [])
            for block in message.get("content", [])
        )
        sent.append((system, text))
        reply = holder["route"](system, text)
        if isinstance(reply, _Refuse):
            return AWSResponse(
                request.url,
                400,
                {"x-amzn-ErrorType": error_type, "content-type": "application/json"},
                _Body({"message": reply.message}),
            )
        return AWSResponse(
            request.url,
            200,
            {"content-type": "application/json"},
            _Body(_answer(reply)),
        )

    runtime.meta.events.register("before-send.bedrock-runtime.Converse", before_send)

    def install(route: Callable[[str, str], Any]) -> List[tuple]:
        holder["route"] = route
        return sent

    yield install
    runtime.meta.events.unregister("before-send.bedrock-runtime.Converse", before_send)
    set_lambda_deadline_epoch(None)


def _issues(document: Document, code: str) -> List[ProcessingIssue]:
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


def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _page_text(uri: str) -> str:
    return f"PAGE-{uri.split('/')[-2]} invoice text"


# ---------------------------------------------------------------- classification


def _classify_by_page(
    bedrock,
    refuse_page: str,
    message: str,
    *,
    classes: Optional[List[Dict[str, Any]]] = None,
    **classification: Any,
) -> Document:
    service = ClassificationService(
        region="us-west-2",
        config={
            "classes": classes or _classes("invoice", "receipt"),
            "classification": {
                "model": "us.amazon.nova-lite-v1:0",
                "system_prompt": "classify",
                "task_prompt": "{CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
                "granularity": "page",
                "modality": {"text": True, "images": False},
                **classification,
            },
        },
    )
    bedrock(
        lambda _system, text: (
            _Refuse(message)
            if f"PAGE-{refuse_page} " in text
            else '{"class": "invoice", "document_boundary": "continue"}'
        )
    )
    with patch("idp_common.s3.get_text_content", side_effect=_page_text):
        return service.classify_document(_pages(3))


class TestClassificationByPage:
    def test_the_blocked_page_is_unclassified_and_the_document_is_not_failed(
        self, bedrock
    ):
        result = _classify_by_page(bedrock, "2", CONTENT_FILTER_BLOCK_MESSAGE)

        assert result.errors == []
        assert result.status != Status.FAILED
        # What the classification Lambda re-raises on.
        assert "failed_page_exceptions" not in (result.metadata or {})
        assert [result.pages[p].classification for p in ("1", "2", "3")] == [
            "invoice",
            UNCLASSIFIED_CLASS,
            "invoice",
        ]
        (issue,) = _issues(result, "classification_failed")
        assert issue.severity == "error"
        assert issue.details["page_ids"] == ["2"]
        assert "content filter" in issue.root_cause
        assert CONTENT_FILTER_BLOCK_MESSAGE in issue.root_cause
        section = next(s for s in result.sections if s.section_id == issue.section_id)
        assert section.classification == UNCLASSIFIED_CLASS
        assert section.page_ids == ["2"]
        # Written onto the page's declared fields, which reach DynamoDB.
        assert result.pages["2"].confidence == 0.0

    @pytest.mark.parametrize(("limit", "page"), [(2, "2"), (1, "1")])
    def test_a_page_limited_sample_still_fails_the_document(self, bedrock, limit, page):
        """The rebuild gives every page the sample's class, so a soft failure's
        'unclassified' label would not survive it: the block fails the document."""
        result = _classify_by_page(
            bedrock,
            page,
            CONTENT_FILTER_BLOCK_MESSAGE,
            maxPagesForClassification=limit,
            sectionSplitting="disabled",
        )
        assert any(CONTENT_FILTER_BLOCK_MESSAGE in e for e in result.errors)
        # What the classification Lambda re-raises on.
        assert page in result.metadata["failed_page_exceptions"]

    def test_a_document_name_class_pin_still_fails_the_document(self, bedrock):
        """Under a name match the class is asserted; labelling the page
        'unclassified' would split the asserted document around it."""
        classes = _classes("invoice", "receipt")
        classes[1]["x-aws-idp-document-name-regex"] = "^doc"
        result = _classify_by_page(
            bedrock,
            "2",
            CONTENT_FILTER_BLOCK_MESSAGE,
            classes=classes,
            sectionSplitting="llm_determined",
        )
        assert any(CONTENT_FILTER_BLOCK_MESSAGE in e for e in result.errors)
        assert list(result.metadata["failed_page_exceptions"]) == ["2"]
        assert {s.classification for s in result.sections} == {"receipt"}

    def test_another_validation_exception_still_fails_the_document(self, bedrock):
        """The control: only the block changes outcome. The service records the
        failure; the Lambda re-raises on ``failed_page_exceptions``."""
        result = _classify_by_page(bedrock, "2", _MALFORMED)
        assert any("Malformed input request" in e for e in result.errors)
        assert list(result.metadata["failed_page_exceptions"]) == ["2"]

    def test_the_collate_step_completes_the_document(self, bedrock, monkeypatch):
        """ProcessResults, which fails a document on any ``document.errors``
        entry, completes one whose page was blocked."""
        classified = _classify_by_page(bedrock, "2", CONTENT_FILTER_BLOCK_MESSAGE)
        response = _collate(monkeypatch, classified)
        assert "document" in response


class _Context:
    function_name = "fn"
    memory_limit_in_mb = 2048
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:fn"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 300_000


def _collate(monkeypatch, classified: Document) -> Dict[str, Any]:
    processresults = load_with_xray_stubbed(
        "processresults_index_content_filter",
        os.path.join(_ROOT, "patterns/unified/src/processresults_function/index.py"),
    )
    monkeypatch.setenv("WORKING_BUCKET", "")
    monkeypatch.setattr(processresults, "get_config", lambda **kw: IDPConfig())
    monkeypatch.setattr(processresults, "create_metadata_file", lambda *a, **kw: None)
    doc_service = MagicMock()
    doc_service.get_document.return_value = None
    doc_service.update_document.side_effect = lambda d: d
    monkeypatch.setattr(
        processresults, "create_document_service", lambda *a, **kw: doc_service
    )
    results = []
    for section in classified.sections:
        one = Document.from_dict(classified.to_dict())
        one.sections = [s for s in one.sections if s.section_id == section.section_id]
        one.status = Status.EXTRACTING
        results.append({"section_id": section.section_id, "document": one.to_dict()})
    return processresults.handler(
        {
            "ClassificationResult": {"document": classified.to_dict()},
            "ExtractionResults": results,
            "execution_arn": "arn:aws:states:us-east-1:1:execution:sm:exec",
        },
        _Context(),
    )


def _split_then_classify(
    bedrock, refuse_page: str, message: str, **classification: Any
) -> Document:
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
                    "task_prompt": "{CLASS_NAMES_AND_DESCRIPTIONS}\n{DOCUMENT_TEXT}",
                },
                **classification,
            },
        },
    )

    def route(system: str, text: str):
        if system == "splitting packet system":
            return json.dumps(
                {
                    "segments": [
                        {"ordinal_start_page": n, "ordinal_end_page": n}
                        for n in (1, 2, 3)
                    ]
                }
            )
        assert system == "section classification system", system
        if f"PAGE-{refuse_page} " in text:
            return _Refuse(message)
        return '{"class": "invoice"}'

    bedrock(route)
    with patch("idp_common.s3.get_text_content", side_effect=_page_text):
        return service.classify_document(_pages(3))


class TestSplitThenClassifyLabeling:
    def test_the_blocked_segment_is_unclassified_and_the_document_is_not_failed(
        self, bedrock
    ):
        result = _split_then_classify(bedrock, "3", CONTENT_FILTER_BLOCK_MESSAGE)

        assert result.errors == []
        assert result.status != Status.FAILED
        assert [s.classification for s in result.sections] == [
            "invoice",
            "invoice",
            UNCLASSIFIED_CLASS,
        ]
        (issue,) = _issues(result, "classification_failed")
        assert issue.severity == "error"
        assert issue.details["page_ids"] == ["3"]
        assert "content filter" in issue.root_cause
        assert CONTENT_FILTER_BLOCK_MESSAGE in issue.root_cause

    def test_a_page_limited_sample_still_fails_the_document(self, bedrock):
        result = _split_then_classify(
            bedrock,
            "2",
            CONTENT_FILTER_BLOCK_MESSAGE,
            maxPagesForClassification=2,
            sectionSplitting="disabled",
        )
        assert any(CONTENT_FILTER_BLOCK_MESSAGE in e for e in result.errors)

    def test_another_validation_exception_still_fails_the_document(self, bedrock):
        result = _split_then_classify(bedrock, "3", _MALFORMED)
        assert any("Malformed input request" in e for e in result.errors)


# ---------------------------------------------------------------- assessment


def test_assessment_degrades_the_section_and_names_the_block(bedrock, monkeypatch):
    """The real ``AssessmentService`` raises the block from the real client, and the
    real Assessment Lambda degrades the section with a remedy that fits it."""
    assessment_index = load_with_xray_stubbed(
        "assessment_index_content_filter",
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
    doc_service = MagicMock()
    monkeypatch.setattr(
        assessment_index, "create_document_service", lambda *a, **kw: doc_service
    )
    extraction = {
        "document_class": {"type": "invoice"},
        "inference_result": {"total": "1.00"},
        "metadata": {},
    }
    monkeypatch.setattr(assessment_index.s3, "get_json_content", lambda uri: extraction)
    monkeypatch.setattr(assessment_index.s3, "write_content", lambda *a, **kw: None)
    sent = bedrock(lambda _system, _text: _Refuse(CONTENT_FILTER_BLOCK_MESSAGE))

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
    (issue,) = _issues(out, "assessment_failed_confidence_unavailable")
    assert issue.severity == "error"
    assert CONTENT_FILTER_BLOCK_MESSAGE in issue.root_cause
    assert "content filter" in issue.message
    assert "too long" not in issue.message


# ---------------------------------------------------------------- summarization


def _summarizer() -> SummarizationService:
    return SummarizationService(
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


def _summarize(bedrock, message: Optional[str], *, sections: bool):
    """Summarize a two-page document whose page 2 text is refused with
    ``message``. With ``sections`` each page is its own section, which is the path
    a classified document takes; without, the whole-document path."""
    bedrock(
        lambda _system, text: (
            _Refuse(message)
            if message and "PAGE-2 " in text
            else json.dumps({"summary": "fine"})
        )
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

    with (
        patch("idp_common.s3.get_text_content", side_effect=_page_text),
        patch("idp_common.s3.write_content", side_effect=write),
        patch(
            "idp_common.s3.get_json_content",
            side_effect=lambda uri: store[uri.split("/", 3)[3]],
        ),
    ):
        result = _summarizer().process_document(document)
    return result, store


class TestSummarization:
    def test_a_blocked_section_gets_the_stub_and_the_document_completes(self, bedrock):
        result, store = _summarize(bedrock, CONTENT_FILTER_BLOCK_MESSAGE, sections=True)

        assert result.status != Status.FAILED
        assert result.errors == []
        combined = store["doc.pdf/summary/summary.json"]
        assert combined["invoice_1"]["summary"].startswith("fine")
        assert "content filter" in combined["invoice_2"]["summary"]
        assert "content filter" in store["doc.pdf/summary/summary.md"]

    def test_the_whole_document_path_flags_the_stub(self, bedrock):
        result, store = _summarize(
            bedrock, CONTENT_FILTER_BLOCK_MESSAGE, sections=False
        )

        assert result.status != Status.FAILED
        assert result.errors == []
        summary = store["doc.pdf/summary/summary.json"]
        assert summary["metadata"]["summarization_skipped"] is True
        assert summary["metadata"]["skip_reason"] == "content_filtered"
        assert CONTENT_FILTER_BLOCK_MESSAGE in summary["metadata"]["error"]
        assert "content filter" in summary["summary"]

    def test_another_validation_exception_still_fails_the_step(self, bedrock):
        """The controls. The section path returns the document FAILED, which the
        Summarization Lambda raises on; the whole-document path raises."""
        result, _ = _summarize(bedrock, _MALFORMED, sections=True)
        assert result.status == Status.FAILED
        assert any("Malformed input request" in e for e in result.errors)
        with pytest.raises(Exception, match="Malformed input request"):
            _summarize(bedrock, _MALFORMED, sections=False)

    def test_the_control_summarizes(self, bedrock):
        """The route answers, so the stub above is the block's doing."""
        _result, store = _summarize(bedrock, None, sections=True)
        combined = store["doc.pdf/summary/summary.json"]
        assert combined["invoice_2"]["summary"].startswith("fine")
