# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A classification issue's message says what happened to the page it names.

``classification_failed``, ``classification_page_no_content`` and
``classification_invalid_class_fallback`` used to say the page was labelled
``unclassified`` (or extracted against the fallback's schema) whatever happened
next. Under ``sectionSplitting: disabled`` the one section gives every page the
winning class, and a document-name regex match pins every page to the matched
class, so the page was neither: it carried the section's class and was extracted
against that class's schema, and the message told the operator the opposite.

Every case runs ``classify_document`` on a real ``ClassificationService``; only
the S3 text read and the model call are replaced, and the issue is produced by
the service's own recorder after its own section builder ran. The relabelled
messages are compared whole, because single words in them ("not", "never")
carry the meaning.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

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


def _service(
    splitting: Optional[str],
    classes: Optional[List[Dict[str, Any]]] = None,
    **classification: Any,
) -> ClassificationService:
    settings: Dict[str, Any] = {
        "model": "us.amazon.nova-lite-v1:0",
        "system_prompt": "classify",
        "task_prompt": "{CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
        "modality": {"text": True, "images": False},
        "confidence": {"mode": "off"},
        "granularity": "page",
        "maxValidationRetries": 1,
        **classification,
    }
    if splitting is not None:
        settings["sectionSplitting"] = splitting
    return ClassificationService(
        region="us-west-2",
        config={
            "classes": classes or _classes("invoice", "receipt"),
            "classification": settings,
        },
    )


def _document(count: int, name: str) -> Document:
    doc = Document(id=name, input_key=name, status=Status.CLASSIFYING)
    for i in range(1, count + 1):
        doc.pages[str(i)] = Page(
            page_id=str(i), parsed_text_uri=f"s3://in/{name}/pages/{i}/text.txt"
        )
    return doc


def _reply(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "response": {
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}}
        },
        "metering": {},
    }


def _blocked() -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "Output blocked by content filtering policy",
            }
        },
        "Converse",
    )


def _classify(
    service: ClassificationService,
    behaviour: Mapping[str, str],
    pages: int = 3,
    answer: str = "invoice",
    name: str = "doc.pdf",
) -> Document:
    """Classify a ``pages``-page document. ``behaviour`` maps a page id to what
    that page meets — ``failed`` (a content-filter block), ``no_content``
    (whitespace-only text) or ``fallback`` (an out-of-vocabulary answer on every
    attempt); every other page answers ``answer``."""

    def text(uri: str) -> str:
        page = uri.split("/")[-2]
        return "   " if behaviour.get(page) == "no_content" else f"PAGE-{page} text"

    def invoke(content, config):
        sent = json.dumps(content)
        page = next((p for p in behaviour if f"PAGE-{p} " in sent), None)
        if page and behaviour[page] == "fallback":
            return _reply({"class": "bank_statement"})
        if page and behaviour[page] == "failed":
            raise _blocked()
        return _reply({"class": answer})

    with (
        patch("idp_common.s3.get_text_content", side_effect=text),
        patch.object(service, "_invoke_bedrock_model", side_effect=invoke),
    ):
        return service.classify_document(_document(pages, name))


def _fallback_settings() -> Dict[str, Any]:
    return {"enforceValidClasses": True, "invalidClassFallback": "receipt"}


def _service_for(case: str, splitting: Optional[str]) -> ClassificationService:
    return _service(splitting, **(_fallback_settings() if case == "fallback" else {}))


def _issues(document: Document) -> List[tuple[Any, ProcessingIssue]]:
    return [
        (section, issue)
        for section in document.sections
        for issue in (section.processing_issues or [])
    ]


def _only_issue(document: Document) -> tuple[Any, ProcessingIssue]:
    found = _issues(document)
    assert len(found) == 1, found
    return found[0]


_CODES = {
    "failed": "classification_failed",
    "no_content": "classification_page_no_content",
    "fallback": "classification_invalid_class_fallback",
}


def _relabelled(case: str, pages: str, count: int, section_class: str) -> str:
    """The whole relabelled message for a page given a configured class."""
    return {
        "failed": (
            f"Classification did not produce a class for {count} page(s) ({pages})."
            f" They are in a section whose class is '{section_class}', so they were"
            " given that class and extracted against its schema with the rest of"
            " the section, although classification never established that they"
            " belong to it."
        ),
        "no_content": (
            f"{count} page(s) ({pages}) had nothing to classify — no usable OCR"
            " text, and no page image was loaded or images are not sent. They are"
            f" in a section whose class is '{section_class}', so they were given"
            " that class and extracted with the rest of the section, with no OCR"
            " text of their own. Check the OCR step for those pages unless they"
            " are genuinely empty."
        ),
        "fallback": (
            "The model returned a class outside the configured vocabulary for"
            f" {count} page(s) ({pages}) after every retry, so the configured"
            " fallback class was assigned. They are in a section whose class is"
            f" '{section_class}', so they were given that class instead and"
            " extraction ran against its schema, not the fallback's. Neither class"
            " is the model's answer for them."
        ),
    }[case]


def _says_kept_own_class(case: str, message: str) -> bool:
    """The wording for a page that kept the class classification gave it."""
    if case == "fallback":
        return "extraction ran against the fallback's schema" in message
    return "labelled 'unclassified'" in message


_RELABEL_MARK = "They are in a section whose class is"


@pytest.mark.parametrize("case", sorted(_CODES))
class TestTheMessageFollowsThePage:
    def test_disabled_says_the_page_took_the_sections_class(self, case):
        """One section over every page: page 2 is relabelled ``invoice``, and the
        message says so instead of claiming ``unclassified`` or the fallback."""
        result = _classify(_service_for(case, "disabled"), {"2": case})
        section, issue = _only_issue(result)
        assert section.classification == "invoice"
        assert result.pages["2"].classification == "invoice"
        assert issue.code == _CODES[case]
        assert issue.details == {"page_ids": ["2"]}
        assert issue.message == _relabelled(case, "2", 1, "invoice")

    @pytest.mark.parametrize("splitting", [None, "page"])
    def test_other_splitting_keeps_the_pages_own_class(self, case, splitting):
        """Default splitting and per-page sections: page 2 has a section of its
        own class, and the message says that class."""
        result = _classify(_service_for(case, splitting), {"2": case})
        section, issue = _only_issue(result)
        own = "receipt" if case == "fallback" else "unclassified"
        assert section.page_ids == ["2"]
        assert section.classification == own
        assert result.pages["2"].classification == own
        assert issue.code == _CODES[case]
        assert _says_kept_own_class(case, issue.message)
        assert _RELABEL_MARK not in issue.message


class TestDisabledWhenThePagesOwnClassWins:
    """Under ``disabled`` the message follows the outcome, not the setting: when
    the page's own class is the one the section ends up with, nothing was
    relabelled and the original wording is the true one."""

    def test_every_page_failed(self):
        result = _classify(
            _service_for("failed", "disabled"),
            {"1": "failed", "2": "failed", "3": "failed"},
        )
        section, issue = _only_issue(result)
        assert section.classification == "unclassified"
        assert issue.details == {"page_ids": ["1", "2", "3"]}
        assert _says_kept_own_class("failed", issue.message)
        assert _RELABEL_MARK not in issue.message

    def test_the_fallback_wins_the_vote(self):
        """Two of three pages coerced to the fallback ``receipt``: it wins, so
        extraction did run against the fallback's schema."""
        result = _classify(
            _service_for("fallback", "disabled"), {"1": "fallback", "2": "fallback"}
        )
        section, issue = _only_issue(result)
        assert section.classification == "receipt"
        assert issue.details == {"page_ids": ["1", "2"]}
        assert _says_kept_own_class("fallback", issue.message)
        assert _RELABEL_MARK not in issue.message

    def test_each_code_in_one_section_is_worded_for_its_own_pages(self):
        """Two codes on one section: the fallback pages won the vote and keep
        their wording, while the failed page is relabelled ``receipt``. The
        comparison is per code, so one code's pages cannot decide the other's."""
        result = _classify(
            _service("disabled", **_fallback_settings()),
            {"1": "fallback", "2": "fallback", "3": "failed"},
        )
        (section,) = result.sections
        assert section.classification == "receipt"
        by_code = {issue.code: issue for _section, issue in _issues(result)}
        assert set(by_code) == {
            "classification_failed",
            "classification_invalid_class_fallback",
        }
        fallback = by_code["classification_invalid_class_fallback"]
        assert _says_kept_own_class("fallback", fallback.message)
        assert _RELABEL_MARK not in fallback.message
        assert by_code["classification_failed"].message == _relabelled(
            "failed", "3", 1, "receipt"
        )


def test_a_section_class_with_no_schema_says_nothing_was_extracted():
    """``enforceValidClasses`` off: pages 1 and 3 answer ``foo``, which is kept
    as-is. No configured class gets a vote, so the one section takes the first
    page's ``foo``, which has no schema — the page was relabelled, but nothing
    was extracted against anything."""
    result = _classify(
        _service("disabled", enforceValidClasses=False), {"2": "failed"}, answer="foo"
    )
    section, issue = _only_issue(result)
    assert section.classification == "foo"
    assert issue.message == (
        "Classification did not produce a class for 1 page(s) (2). They are in a"
        " section whose class is 'foo', so they were given that class. 'foo' is not"
        " a configured class, so there is no extraction schema for it and nothing"
        " was extracted from the section."
    )


@pytest.mark.parametrize("case", ["no_content", "fallback"])
def test_a_document_name_pin_is_reported_as_a_relabel(case):
    """The document's name matches ``invoice``'s name regex, so every page is
    pinned to ``invoice`` before splitting. Page 2's own result was
    ``unclassified`` (no content) or the fallback, and it was extracted as an
    invoice; the message says so."""
    classes = _classes("invoice", "receipt")
    classes[0]["x-aws-idp-document-name-regex"] = r"^inv-.*"
    service = _service(None, classes=classes, **_fallback_settings())
    result = _classify(service, {"2": case}, name="inv-001.pdf")
    section, issue = _only_issue(result)
    assert section.classification == "invoice"
    assert "2" in section.page_ids
    assert issue.code == _CODES[case]
    assert issue.message == _relabelled(case, "2", 1, "invoice")


def test_a_page_limited_sample_names_the_class_the_document_got():
    """``maxPagesForClassification`` (in force only under ``disabled``): the
    issue recorded on the sample is carried to the rebuilt all-pages section, and
    its message names the class every page was given."""
    service = _service(
        "disabled", maxPagesForClassification="2", **_fallback_settings()
    )
    result = _classify(service, {"2": "fallback"}, pages=4)
    section, issue = _only_issue(result)
    assert section.page_ids == ["1", "2", "3", "4"]
    assert section.classification == "invoice"
    assert issue.code == "classification_invalid_class_fallback"
    assert issue.message == _relabelled("fallback", "2", 1, "invoice")


def test_a_fallback_outside_the_vocabulary_is_not_called_a_schema():
    """``invalidClassFallback`` names a class the configuration does not define.
    Page 1 is blocked and pages 2 and 3 fall back to ``other``, so no configured
    class gets a vote and the section takes page 1's ``unclassified``: pages 2
    and 3 were relabelled, and there was no schema to extract them against."""
    result = _classify(
        _service("disabled", enforceValidClasses=True, invalidClassFallback="other"),
        {"1": "failed", "2": "fallback", "3": "fallback"},
    )
    (section,) = result.sections
    assert section.classification == "unclassified"
    by_code = {issue.code: issue for _section, issue in _issues(result)}
    assert by_code["classification_invalid_class_fallback"].message == (
        "The model returned a class outside the configured vocabulary for 2 page(s)"
        " (2, 3) after every retry, so the configured fallback class was assigned."
        " They are in a section whose class is 'unclassified', so they were given"
        " that class. 'unclassified' is not a configured class, so there is no"
        " extraction schema for it and nothing was extracted from the section."
    )
    assert _says_kept_own_class("failed", by_code["classification_failed"].message)


def _excluding(name: str) -> List[Dict[str, Any]]:
    classes = _classes("invoice", "receipt")
    for cls in classes:
        if cls["$id"] == name:
            cls["x-aws-idp-exclude-from-processing"] = True
    return classes


def test_an_excluded_section_class_says_nothing_was_extracted():
    """``invoice`` is excluded from processing and wins the one section, so the
    blocked page 2 was relabelled ``invoice`` but extraction skipped it."""
    result = _classify(
        _service("disabled", classes=_excluding("invoice")), {"2": "failed"}
    )
    section, issue = _only_issue(result)
    assert (section.classification, section.excluded) == ("invoice", True)
    assert issue.message == (
        "Classification did not produce a class for 1 page(s) (2). They are in a"
        " section whose class is 'invoice', so they were given that class."
        " 'invoice' is excluded from processing by its configuration, so nothing"
        " was extracted from the section."
    )


def test_an_excluded_fallback_is_not_said_to_have_been_extracted():
    """Default splitting: page 2 falls back to ``receipt`` in a section of its
    own, and ``receipt`` is excluded, so no extraction ran against its schema."""
    result = _classify(
        _service(None, classes=_excluding("receipt"), **_fallback_settings()),
        {"2": "fallback"},
    )
    section, issue = _only_issue(result)
    assert (section.classification, section.excluded) == ("receipt", True)
    assert issue.message == (
        "The model returned a class outside the configured vocabulary for 1 page(s)"
        " (2) after every retry, so the configured fallback class was assigned."
        " The stored class is not the model's answer. 'receipt' is excluded from"
        " processing by its configuration, so nothing was extracted from the"
        " section."
    )
