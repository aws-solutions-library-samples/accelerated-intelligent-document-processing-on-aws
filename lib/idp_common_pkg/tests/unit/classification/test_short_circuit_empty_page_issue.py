# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A page with nothing to classify is reported when classification is skipped.

When the model runs, a page whose text is blank with ``modality.images`` off is
reported on its section as ``classification_page_no_content``. A document whose
name matches a class's ``x-aws-idp-document-name-regex``, or a single-class
configuration, under ``sectionSplitting: disabled`` or ``page`` skips the model
entirely, and such a page was extracted with no issue at all.

Every case runs ``classify_document`` on a real ``ClassificationService``; only the
S3 text read and the model call are replaced. The expected message is the one the
service itself produces when the model runs with the class pinned (the regex path
under ``llm_determined``), not a copy of the wording.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from idp_common.classification.service import ClassificationService
from idp_common.models import Document, Page, Status

pytestmark = pytest.mark.unit


def _classes(*names: str, regex_for: Optional[str] = None) -> List[Dict[str, Any]]:
    out = []
    for name in names:
        schema: Dict[str, Any] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": name,
            "x-aws-idp-document-type": name,
            "type": "object",
            "description": f"A {name}",
            "properties": {"total": {"type": "string"}},
        }
        if name == regex_for:
            schema["x-aws-idp-document-name-regex"] = "^inv-"
        out.append(schema)
    return out


def _service(
    splitting: str,
    images: bool = False,
    classes: Optional[List[Dict[str, Any]]] = None,
    **classification: Any,
) -> ClassificationService:
    settings: Dict[str, Any] = {
        "model": "us.amazon.nova-lite-v1:0",
        "system_prompt": "classify",
        "task_prompt": "{CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
        "modality": {"text": True, "images": images},
        "confidence": {"mode": "off"},
        "granularity": "page",
        "sectionSplitting": splitting,
        **classification,
    }
    return ClassificationService(
        region="us-west-2",
        config={
            "classes": classes or _classes("invoice", "receipt", regex_for="invoice"),
            "classification": settings,
        },
    )


def _document(name: str, pages: int = 3) -> Document:
    doc = Document(id=name, input_key=name, status=Status.CLASSIFYING)
    for i in range(1, pages + 1):
        doc.pages[str(i)] = Page(
            page_id=str(i), parsed_text_uri=f"s3://in/{name}/pages/{i}/text.txt"
        )
    return doc


def _reply() -> Dict[str, Any]:
    payload = {"class": "invoice", "document_boundary": "continue"}
    return {
        "response": {
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}}
        },
        "metering": {},
    }


def _run(service, name="inv-1.pdf", blank=("2",), text_error=None, pages=3):
    reads: List[str] = []

    def text(uri: str) -> str:
        reads.append(uri)
        page = uri.split("/")[-2]
        if text_error and page in text_error:
            raise RuntimeError("AccessDenied")
        return " \n " if page in blank else f"PAGE-{page} text"

    with (
        patch("idp_common.s3.get_text_content", side_effect=text),
        patch.object(service, "_invoke_bedrock_model", return_value=_reply()) as invoke,
    ):
        document = service.classify_document(_document(name, pages))
    issues = [
        (section, issue)
        for section in document.sections
        for issue in (section.processing_issues or [])
    ]
    return document, issues, invoke.call_count, reads


@pytest.mark.parametrize("splitting", ["disabled", "page"])
def test_a_blank_page_is_reported_when_the_name_regex_skips_the_model(splitting):
    document, issues, calls, _ = _run(_service(splitting))
    assert calls == 0  # the short-circuit is still taken
    [(section, issue)] = issues
    assert issue.code == "classification_page_no_content"
    assert issue.severity == "warning"
    assert section.page_ids == (["1", "2", "3"] if splitting == "disabled" else ["2"])
    assert section.classification == "invoice"
    assert "(2)" in issue.message
    # Still a warning only: the document is not failed for it.
    assert document.errors == []


def test_the_message_is_the_one_the_pinned_model_path_records():
    # Under llm_determined a regex-matched multi-page document runs the model with
    # the class pinned; with every page continuing, it is one section of the
    # matched class, the same section `disabled` builds without the model.
    _, pinned, calls, _ = _run(_service("llm_determined"))
    assert calls == 2  # pages 1 and 3; page 2 had nothing to send
    _, skipped, calls, _ = _run(_service("disabled"))
    assert calls == 0
    [(_, expected)] = pinned
    [(_, actual)] = skipped
    assert (actual.code, actual.severity, actual.message, actual.root_cause) == (
        expected.code,
        expected.severity,
        expected.message,
        expected.root_cause,
    )


@pytest.mark.parametrize("splitting", ["disabled", "page"])
def test_a_single_class_configuration_reports_it_too(splitting):
    service = _service(splitting, classes=_classes("invoice"))
    _, issues, calls, _ = _run(service, name="anything.pdf")
    assert calls == 0
    [(section, issue)] = issues
    assert issue.code == "classification_page_no_content"
    assert section.classification == "invoice"


def test_no_blank_page_means_no_issue():
    _, issues, _, reads = _run(_service("disabled"), blank=())
    assert issues == []
    assert len(reads) == 3


def test_with_images_on_no_page_is_read_and_none_is_reported():
    # With images on the model path sends the page image, so a blank-text page
    # is not a no-content page there either; nothing is read to find out.
    _, issues, calls, reads = _run(_service("disabled", images=True))
    assert calls == 0
    assert issues == []
    assert reads == []


def test_packet_granularity_reads_nothing():
    # The model path records this issue only at page granularity.
    _, issues, _, reads = _run(_service("disabled", granularity="packet"))
    assert issues == []
    assert reads == []


def test_a_failed_text_read_is_not_reported_and_does_not_fail_the_document():
    document, issues, calls, _ = _run(_service("disabled"), blank=(), text_error=("2",))
    assert calls == 0
    assert issues == []
    assert document.errors == []


def test_split_then_classify_reads_nothing():
    # The model path records this issue only under the combined strategy.
    _, issues, calls, reads = _run(_service("disabled", strategy="split_then_classify"))
    assert calls == 0
    assert issues == []
    assert reads == []


def test_a_non_bedrock_backend_reads_nothing():
    # Only classify_page_bedrock detects an empty page.
    service = _service("disabled")
    service.backend = "sagemaker"
    _, issues, _, reads = _run(service)
    assert issues == []
    assert reads == []


def test_a_page_with_no_text_uri_is_not_reported():
    # On the model path such a page fails the document rather than being empty;
    # the short-circuit neither reports it nor starts failing the document.
    service = _service("disabled")
    document = _document("inv-1.pdf")
    document.pages["2"].parsed_text_uri = None
    with (
        patch("idp_common.s3.get_text_content", return_value="PAGE text"),
        patch.object(service, "_invoke_bedrock_model", return_value=_reply()),
    ):
        document = service.classify_document(document)
    assert [i for s in document.sections for i in (s.processing_issues or [])] == []
    assert document.errors == []


def test_under_a_page_limit_only_the_sampled_pages_are_read():
    # maxPagesForClassification: 1 makes the model path classify page 1 alone and
    # extrapolate, so a blank page 3 is not reported there; nor is it here.
    service = _service("disabled", maxPagesForClassification=1)
    _, issues, calls, reads = _run(service, blank=("3",), pages=5)
    assert calls == 0
    assert issues == []
    assert [uri.split("/")[-2] for uri in reads] == ["1"]
    _, issues, _, _ = _run(service, blank=("1",), pages=5)
    assert [i.code for _, i in issues] == ["classification_page_no_content"]


def test_the_parsed_text_is_read_as_on_the_model_path():
    service = _service("disabled")
    document = _document("inv-1.pdf")
    for page in document.pages.values():
        page.raw_text_uri = "s3://in/raw/" + page.page_id + "/raw.json"
    reads: List[str] = []

    def text(uri: str) -> str:
        reads.append(uri)
        return "PAGE text"

    with (
        patch("idp_common.s3.get_text_content", side_effect=text),
        patch.object(service, "_invoke_bedrock_model", return_value=_reply()),
    ):
        service.classify_document(document)
    assert sorted(reads) == sorted(p.parsed_text_uri for p in document.pages.values())
