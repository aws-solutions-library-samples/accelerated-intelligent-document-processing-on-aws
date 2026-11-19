# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A page-limited sample's section issues reach the full document.

With ``maxPagesForClassification`` the classifier sees the first N pages, and the
full document's one section is rebuilt from that sample. The rebuild carried the
sample's ``errors`` and metering across but not the processing issues on the
sample's sections, so a warning recorded there — a class coerced to the fallback,
a page with nothing to classify — was dropped and the document read as clean.

Every case runs ``classify_document`` on a real ``ClassificationService``; only
the model call is replaced, and the issue is produced by the service's own
recorders from what the model returned.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from idp_common.classification.service import ClassificationService
from idp_common.models import Document, Page, ProcessingIssue, Status

pytestmark = pytest.mark.unit


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


def _service(**classification: Any) -> ClassificationService:
    return ClassificationService(
        region="us-west-2",
        config={
            "classes": _classes("invoice", "receipt"),
            "classification": {
                "model": "us.amazon.nova-lite-v1:0",
                "system_prompt": "classify",
                "task_prompt": "{CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
                "modality": {"text": True, "images": False},
                "confidence": {"mode": "off"},
                "sectionSplitting": "disabled",
                "maxPagesForClassification": "2",
                "maxValidationRetries": 1,
                **classification,
            },
        },
    )


def _document(count: int) -> Document:
    doc = Document(id="doc.pdf", input_key="doc.pdf", status=Status.CLASSIFYING)
    for i in range(1, count + 1):
        doc.pages[str(i)] = Page(
            page_id=str(i), parsed_text_uri=f"s3://in/doc.pdf/pages/{i}/text.txt"
        )
    return doc


def _page_text(uri: str) -> str:
    page = uri.split("/")[-2]
    # Page 9 has whitespace only, which counts as no text at all.
    return "   " if page == "9" else f"PAGE-{page} text"


def _reply(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "response": {
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}}
        },
        "metering": {"Classification/bedrock/nova": {"inputTokens": 10}},
    }


def _classify(
    service: ClassificationService, route, pages: int = 4, document=None
) -> Document:
    sent: List[str] = []

    def invoke(content, config):
        text = json.dumps(content)
        sent.append(text)
        return _reply(route(text, config))

    with (
        patch("idp_common.s3.get_text_content", side_effect=_page_text),
        patch.object(service, "_invoke_bedrock_model", side_effect=invoke),
    ):
        result = service.classify_document(document or _document(pages))
    # The limit was in force: pages past the sample were never sent.
    assert not any(f"PAGE-{n} " in text for text in sent for n in range(3, pages + 1))
    return result


def _issues(document: Document) -> List[ProcessingIssue]:
    return [i for s in document.sections for i in (s.processing_issues or [])]


class TestTheSamplesIssuesAreCarried:
    def test_a_page_coerced_to_the_fallback_is_reported(self):
        """Page 2 of the sample answers a class outside the vocabulary on every
        attempt; the page path records a warning on the sample's section."""
        result = _classify(
            _service(granularity="page"),
            lambda text, _config: {
                "class": "bank_statement" if "PAGE-2 " in text else "invoice"
            },
        )
        (section,) = result.sections
        assert section.page_ids == ["1", "2", "3", "4"]
        assert section.classification == "invoice"
        (issue,) = _issues(result)
        assert (issue.stage, issue.severity, issue.code) == (
            "classification",
            "warning",
            "classification_invalid_class_fallback",
        )
        assert issue.details == {"page_ids": ["2"]}
        assert "bank_statement" in issue.root_cause
        # Points at the section it now sits on, not at the discarded sample's.
        assert issue.section_id == section.section_id
        assert not result.errors

    def test_a_segment_coerced_to_the_fallback_is_reported(self):
        """split_then_classify: the section classification call answers an
        out-of-vocabulary class for the sample's one segment."""

        def route(_text, config):
            if config.get("stage") == "splitting":
                return {"segments": [{"ordinal_start_page": 1, "ordinal_end_page": 2}]}
            return {"class": "mystery"}

        result = _classify(
            _service(
                granularity="packet",
                strategy="split_then_classify",
                enforceValidClasses=True,
                invalidClassFallback="receipt",
            ),
            route,
        )
        (section,) = result.sections
        assert section.page_ids == ["1", "2", "3", "4"]
        assert section.classification == "receipt"
        (issue,) = _issues(result)
        assert (issue.severity, issue.code) == (
            "warning",
            "classification_invalid_class_fallback",
        )
        assert issue.details == {"page_ids": ["1", "2"]}
        assert issue.section_id == section.section_id

    def test_every_issue_of_the_sample_is_carried(self):
        """Two warnings of different codes on the sample's section: a page with
        nothing to classify and a page coerced to the fallback."""
        document = _document(4)
        # The sample's first page has whitespace-only text.
        document.pages["1"].parsed_text_uri = "s3://in/doc.pdf/pages/9/text.txt"
        result = _classify(
            _service(granularity="page"),
            lambda text, _config: {
                "class": "bank_statement" if "PAGE-2 " in text else "invoice"
            },
            document=document,
        )
        (section,) = result.sections
        assert sorted(
            (i.code, i.severity, tuple(i.details["page_ids"])) for i in _issues(result)
        ) == [
            ("classification_invalid_class_fallback", "warning", ("2",)),
            ("classification_page_no_content", "warning", ("1",)),
        ]
        assert {i.section_id for i in _issues(result)} == {section.section_id}

    def test_a_failed_section_classification_is_carried_with_its_error(self):
        """An error-severity issue is carried too, beside the document error that
        fails the document."""

        def route(_text, config):
            if config.get("stage") == "splitting":
                return {"segments": [{"ordinal_start_page": 1, "ordinal_end_page": 2}]}
            raise RuntimeError("model access denied")

        result = _classify(
            _service(granularity="packet", strategy="split_then_classify"), route
        )
        (issue,) = _issues(result)
        assert (issue.severity, issue.code) == ("error", "classification_failed")
        assert "model access denied" in issue.root_cause
        assert any("model access denied" in e for e in result.errors)

    def test_a_clean_sample_adds_nothing(self):
        result = _classify(
            _service(granularity="page"), lambda _text, _config: {"class": "invoice"}
        )
        (section,) = result.sections
        assert section.processing_issues == []
        assert section.page_ids == ["1", "2", "3", "4"]
