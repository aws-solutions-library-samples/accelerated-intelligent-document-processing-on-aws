# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``classification.document_shape.pageOrder: printed_page_numbers`` (GitHub #623).

Pages scanned out of order are classified out of order: a two-page form whose pages
are not adjacent in the scan becomes two sections. With the setting on, the page
numbers printed in each page's header or footer are read from the OCR text and,
when every page has one and they form one sequence, classification sees the pages
in that order. The results keep every page's own ID.

The end-to-end tests run the real ``ClassificationService.classify_document``; only
S3 and the model call are faked, and the fake model decides each page from that
page's own text, so the grouping is the service's, not the fixture's.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from idp_common.classification import page_order
from idp_common.classification.page_order import (
    PAGE_ORDER_ISSUE_CODE,
    plan_printed_page_order,
    read_printed_page_number,
)
from idp_common.classification.service import ClassificationService
from idp_common.config.models import DocumentShapeConfig
from idp_common.models import Document, Page, ProcessingIssue, Status
from idp_common.ocr.service import HANDWRITING_LEGEND, HANDWRITING_MARKER


def _class(name: str) -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": name,
        "x-aws-idp-document-type": name,
        "type": "object",
        "description": f"A {name}",
        "properties": {"field": {"type": "string"}},
    }


def _service(page_order_value: Optional[str], **classification: Any):
    shape = {} if page_order_value is None else {"pageOrder": page_order_value}
    config = {
        "classes": [_class("cover"), _class("form_x"), _class("receipt")],
        "classification": {
            "model": "us.amazon.nova-lite-v1:0",
            "system_prompt": "classify",
            "task_prompt": "{CLASS_NAMES_AND_DESCRIPTIONS} <text>{DOCUMENT_TEXT}</text>",
            "granularity": "page",
            "modality": {"text": True, "images": False},
            "document_shape": shape,
            **classification,
        },
    }
    with patch("boto3.Session"):
        return ClassificationService(region="us-west-2", config=config)


# The issue's case: a four-page file scanned in printed order 3, 1, 2, 4. Printed
# pages 2 and 3 are one two-page form; page 3 carries no opening block, so a
# classifier that sees it before page 2 starts a second form at page 2.
SHUFFLED = {
    "1": "FORM X continued\nSchedule B, lines 10 to 20\nTotal 4 items\nPage 3 of 4",
    "2": "COVER LETTER\nDear applicant,\nplease find the enclosed forms.\nPage 1 of 4",
    "3": "FORM X\nApplicant name: A. Person\nAddress: 1 Main St\nPage 2 of 4",
    "4": "RECEIPT\nAmount paid: 25.00\nThank you\nPage 4 of 4",
}


def _page_verdict(text: str) -> Dict[str, str]:
    """What the fake model answers for one page: decided from that page alone."""
    if "FORM X continued" in text:
        return {"class": "form_x", "document_boundary": "continue"}
    if "FORM X" in text:
        return {"class": "form_x", "document_boundary": "start"}
    if "COVER LETTER" in text:
        return {"class": "cover", "document_boundary": "start"}
    if "RECEIPT" in text:
        return {"class": "receipt", "document_boundary": "start"}
    return {"class": "not_a_class", "document_boundary": "start"}


def _reply(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "response": {
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}}
        },
        "metering": {},
    }


def _sent_text(content: List[Dict[str, Any]]) -> str:
    return "\n".join(block.get("text", "") for block in content if "text" in block)


def _document(texts: Dict[str, str]) -> Document:
    doc = Document(id="doc.pdf", input_key="doc.pdf", status=Status.CLASSIFYING)
    for page_id in texts:
        doc.pages[page_id] = Page(
            page_id=page_id,
            parsed_text_uri=f"s3://bucket/doc.pdf/pages/{page_id}/text.txt",
        )
    return doc


def _classify(service, texts, *, verdict=_page_verdict):
    def get_text(uri: str) -> str:
        return texts[uri.split("/")[-2]]

    def invoke(content, config):
        return _reply(verdict(_sent_text(content)))

    with (
        patch("idp_common.s3.get_text_content", side_effect=get_text) as reads,
        patch.object(service, "_invoke_bedrock_model", side_effect=invoke) as calls,
    ):
        result = service.classify_document(_document(texts))
    return result, reads, calls


def _issues(document: Document, code: str) -> List[ProcessingIssue]:
    return [
        issue
        for section in document.sections
        for issue in (section.processing_issues or [])
        if issue.code == code
    ]


def _shape(document: Document):
    return [(s.classification, s.page_ids) for s in document.sections]


# --------------------------------------------------------------------------
# End to end, through the real ClassificationService
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestTheShuffledFormIsRegrouped:
    def test_scan_order_splits_the_form_which_is_the_defect(self):
        """The baseline the setting exists to change, measured on the same
        fixture: in scan order the form's two pages are two sections."""
        result, _, _ = _classify(_service(None), SHUFFLED)
        assert _shape(result) == [
            ("form_x", ["1"]),
            ("cover", ["2"]),
            ("form_x", ["3"]),
            ("receipt", ["4"]),
        ]

    def test_printed_order_puts_both_form_pages_in_one_section(self):
        result, _, calls = _classify(_service("printed_page_numbers"), SHUFFLED)

        # Each section names the ORIGINAL page IDs, and sections are ordered and
        # numbered by their first scan page, as a scan-order run numbers them.
        assert _shape(result) == [
            ("form_x", ["1", "3"]),
            ("cover", ["2"]),
            ("receipt", ["4"]),
        ]
        assert [s.section_id for s in result.sections] == ["1", "2", "3"]
        assert calls.call_count == 4
        assert result.status != Status.FAILED
        assert result.errors == []
        assert not _issues(result, PAGE_ORDER_ISSUE_CODE)

    def test_every_page_keeps_its_own_id_and_its_own_class(self):
        result, _, _ = _classify(_service("printed_page_numbers"), SHUFFLED)

        assert sorted(result.pages) == ["1", "2", "3", "4"]
        for page_id, page in result.pages.items():
            assert page.page_id == page_id
            assert page.parsed_text_uri.endswith(f"/{page_id}/text.txt")
        assert result.pages["1"].classification == "form_x"
        assert result.pages["1"].document_boundary == "continue"
        assert result.pages["2"].classification == "cover"
        assert result.pages["3"].classification == "form_x"
        assert result.pages["3"].document_boundary == "start"
        assert result.pages["4"].classification == "receipt"

    def test_page_offsets_are_computed_on_the_original_ids(self):
        result, _, _ = _classify(_service("printed_page_numbers"), SHUFFLED)
        offsets = {
            s.classification: s.attributes["page_indices"] for s in result.sections
        }
        assert offsets == {"cover": [1], "form_x": [0, 2], "receipt": [3]}

    def test_the_outcome_is_recorded_on_the_document(self):
        result, _, _ = _classify(_service("printed_page_numbers"), SHUFFLED)
        assert result.metadata["page_order"] == {
            "setting": "printed_page_numbers",
            "applied": True,
            "reordered": True,
            "printed_order": ["2", "3", "1", "4"],
        }

    def test_context_pages_are_the_printed_neighbours(self):
        """With one context page either side, printed page 3 is shown printed
        page 2 before it, which it can only be if the order reached the model."""
        seen: Dict[str, str] = {}

        def verdict(text: str) -> Dict[str, str]:
            current = re.search(r"<current-page>(.*?)</current-page>", text, re.S)
            assert current, "context mode labels the page being classified"
            if "Schedule B" in current.group(1):
                seen["printed page 3"] = text.replace(current.group(0), "")
            return _page_verdict(current.group(1))

        service = _service("printed_page_numbers", contextPagesCount="1")
        _classify(service, SHUFFLED, verdict=verdict)
        shown = seen["printed page 3"]
        assert "Applicant name" in shown  # printed page 2, its predecessor
        assert "Amount paid" in shown  # printed page 4, its successor
        assert "Dear applicant" not in shown  # printed page 1 is two away


@pytest.mark.unit
class TestThePacketPathSeesThePrintedOrder:
    PACKET_PROMPT = (
        "{CLASS_NAMES_AND_DESCRIPTIONS} segments <text>{DOCUMENT_TEXT}</text>"
    )

    def _service(self, page_order_value):
        return _service(
            page_order_value,
            granularity="packet",
            packet={
                "system_prompt": "split",
                "task_prompt": self.PACKET_PROMPT,
                "window_pages": "0",
            },
        )

    def test_the_model_reads_the_pages_in_printed_order_and_its_ranges_map_back(self):
        sent: List[str] = []

        def verdict(text: str) -> Dict[str, Any]:
            sent.append(text)
            # Ordinals, as the packet contract asks for: 1 = first page shown.
            return {
                "segments": [
                    {"ordinal_start_page": 1, "ordinal_end_page": 1, "type": "cover"},
                    {"ordinal_start_page": 2, "ordinal_end_page": 3, "type": "form_x"},
                    {
                        "ordinal_start_page": 4,
                        "ordinal_end_page": 4,
                        "type": "receipt",
                    },
                ]
            }

        result, _, calls = _classify(
            self._service("printed_page_numbers"), SHUFFLED, verdict=verdict
        )
        assert calls.call_count == 1
        text = sent[0]
        positions = [
            text.index(marker)
            for marker in ("Dear applicant", "Applicant name", "Schedule B", "Amount")
        ]
        assert positions == sorted(positions)
        assert _shape(result) == [
            ("form_x", ["1", "3"]),
            ("cover", ["2"]),
            ("receipt", ["4"]),
        ]


@pytest.mark.unit
class TestAnUnchangedGroupingIsIdenticalToScanOrder:
    """Evaluation pairs sections by ``section_id``. A file whose pages are out of
    printed order but whose grouping does not depend on it must come out exactly
    as the scan-order run does, section IDs and offsets included."""

    TEXTS = {
        "1": "FORM X\nApplicant name: A. Person\nAddress: 1 Main St\nPage 2 of 3",
        "2": "FORM X continued\nSchedule B, lines 10 to 20\nTotals\nPage 3 of 3",
        "3": "COVER LETTER\nDear applicant,\nplease find the forms.\nPage 1 of 3",
    }

    def test_sections_ids_and_offsets_match(self):
        scan, _, _ = _classify(_service("scan"), self.TEXTS)
        printed, _, _ = _classify(_service("printed_page_numbers"), self.TEXTS)
        assert printed.metadata["page_order"]["reordered"] is True

        def view(doc):
            return [
                (s.section_id, s.classification, s.page_ids, s.attributes)
                for s in doc.sections
            ]

        assert view(printed) == view(scan)
        assert view(scan) == [
            ("1", "form_x", ["1", "2"], {"page_indices": [0, 1]}),
            ("2", "cover", ["3"], {"page_indices": [2]}),
        ]


@pytest.mark.unit
class TestTenOrMorePages:
    def test_page_ids_are_ordered_numerically_not_as_strings(self):
        """At ten pages "10" sorts before "2" as a string. Printed in scan order,
        so a string sort would see a reorder that is not there."""
        texts = {
            str(i): f"COVER LETTER\nline\nmore\nPage {i} of 11" for i in range(1, 12)
        }
        result, _, _ = _classify(_service("printed_page_numbers"), texts)
        assert result.metadata["page_order"]["reordered"] is False

    def test_problem_pages_are_listed_in_scan_order(self):
        texts = {
            str(i): f"COVER LETTER\nline\nmore\nPage {i} of 11" for i in range(1, 12)
        }
        texts["2"] = texts["10"] = "COVER LETTER\nno number\nhere\nat all"
        result, _, _ = _classify(_service("printed_page_numbers"), texts)
        (issue,) = _issues(result, PAGE_ORDER_ISSUE_CODE)
        assert issue.details["problem_page_ids"] == ["2", "10"]


@pytest.mark.unit
class TestUntrustworthyNumbersKeepTheScanOrder:
    @pytest.mark.parametrize(
        ("texts", "reason_code", "problem"),
        [
            pytest.param(
                {**SHUFFLED, "3": SHUFFLED["3"].replace("Page 2 of 4", "Page 1 of 4")},
                "duplicate",
                ["2", "3"],
                id="two-pages-print-1",
            ),
            pytest.param(
                {**SHUFFLED, "4": "RECEIPT\nAmount paid: 25.00\nThank you"},
                "no_number",
                ["4"],
                id="one-page-unnumbered",
            ),
            pytest.param(
                {**SHUFFLED, "4": "Page 4 of 4\nRECEIPT\nAmount paid: 25\n- 5 -"},
                "ambiguous",
                ["4"],
                id="one-page-reads-two-numbers",
            ),
            pytest.param(
                {**SHUFFLED, "4": "RECEIPT\nAmount paid: 25.00\nPage 5 of 5"},
                "inconsistent_totals",
                ["1", "2", "3", "4"],
                id="totals-disagree",
            ),
            pytest.param(
                {
                    **{k: v.replace(" of 4", "") for k, v in SHUFFLED.items()},
                    "4": "RECEIPT\nAmount paid: 25.00\nThank you\nPage 6",
                },
                "gap",
                ["1", "2", "3", "4"],
                id="numbers-skip-5",
            ),
            pytest.param(
                {**SHUFFLED, "4": "RECEIPT\nAmount paid: 25.00\nThank you\nPage 6"},
                "inconsistent_totals",
                ["4"],
                id="a-number-beyond-the-printed-total",
            ),
            pytest.param(
                {**SHUFFLED, "2": "   \n"},
                "no_text",
                ["2"],
                id="a-page-with-no-text",
            ),
        ],
    )
    def test_scan_order_is_kept_and_a_warning_says_why(
        self, texts, reason_code, problem
    ):
        def verdict(text: str) -> Dict[str, str]:
            return _page_verdict(text) if text.strip() else {"class": "cover"}

        baseline, _, _ = _classify(_service("scan"), texts, verdict=verdict)
        result, _, _ = _classify(
            _service("printed_page_numbers"), texts, verdict=verdict
        )

        assert _shape(result) == _shape(baseline)
        issues = _issues(result, PAGE_ORDER_ISSUE_CODE)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.severity == "warning"
        assert issue.stage == "classification"
        assert issue.section_id == result.sections[0].section_id
        assert issue.details == {
            "reason_code": reason_code,
            "problem_page_ids": problem,
        }
        assert "scan order" in issue.message
        assert result.metadata["page_order"]["applied"] is False
        assert result.metadata["page_order"]["reason_code"] == reason_code
        assert not _issues(baseline, PAGE_ORDER_ISSUE_CODE)

    def test_numbers_already_in_scan_order_change_nothing(self):
        in_order = {
            "1": SHUFFLED["2"],
            "2": SHUFFLED["3"],
            "3": SHUFFLED["1"],
            "4": SHUFFLED["4"],
        }
        baseline, _, _ = _classify(_service("scan"), in_order)
        result, _, _ = _classify(_service("printed_page_numbers"), in_order)
        assert _shape(result) == _shape(baseline)
        assert _shape(result) == [
            ("cover", ["1"]),
            ("form_x", ["2", "3"]),
            ("receipt", ["4"]),
        ]
        assert not _issues(result, PAGE_ORDER_ISSUE_CODE)
        assert result.metadata["page_order"]["reordered"] is False


@pytest.mark.unit
class TestTheSettingOff:
    @pytest.mark.parametrize("value", [None, "scan", ""])
    def test_off_reads_no_page_numbers_and_records_nothing(self, value):
        service = _service(value)
        assert service.page_order == "scan"
        with patch.object(
            ClassificationService, "_read_page_texts_for_order"
        ) as order_reads:
            result, reads, _ = _classify(service, SHUFFLED)
        order_reads.assert_not_called()
        # One read per page, all by the classifier itself.
        assert reads.call_count == 4
        assert "page_order" not in result.metadata
        assert len(result.sections) == 4

    def test_the_value_is_case_insensitive(self):
        assert DocumentShapeConfig(pageOrder="Printed_Page_Numbers").pageOrder == (
            "printed_page_numbers"
        )

    def test_an_unrecognised_value_is_refused(self):
        with pytest.raises(ValueError, match="pageOrder must be one of"):
            DocumentShapeConfig(pageOrder="printed")

    def test_a_single_page_document_is_left_alone(self):
        service = _service("printed_page_numbers")
        with patch.object(
            ClassificationService, "_read_page_texts_for_order"
        ) as order_reads:
            result, _, _ = _classify(service, {"1": SHUFFLED["2"]})
        order_reads.assert_not_called()
        assert _shape(result) == [("cover", ["1"])]


@pytest.mark.unit
class TestClassificationIssuesNameTheOriginalPages:
    def test_a_fallback_issue_raised_in_printed_order_lists_scan_page_ids(self):
        """Printed page 3 (scan page 1) gets an out-of-vocabulary class. The
        recorder runs on the printed-order view, where that page is page 3; the
        issue must still name page 1, which is what the page viewer shows."""

        def verdict(text: str) -> Dict[str, str]:
            if "FORM X continued" in text:
                return {"class": "bogus", "document_boundary": "continue"}
            return _page_verdict(text)

        service = _service("printed_page_numbers", maxValidationRetries=0)
        result, _, _ = _classify(service, SHUFFLED, verdict=verdict)
        issues = _issues(result, "classification_invalid_class_fallback")
        assert len(issues) == 1
        issue = issues[0]
        assert issue.details["page_ids"] == ["1"]
        assert "(1)" in issue.message
        assert "(3)" not in issue.message
        assert issue.root_cause.endswith("(pages 1)")
        owner = next(s for s in result.sections if s.section_id == issue.section_id)
        assert "1" in owner.page_ids


@pytest.mark.unit
class TestFailuresAreReportedAgainstTheOriginalPages:
    def test_a_failed_page_is_named_by_its_own_id(self):
        """Printed page 3 is scan page 1. Its model call fails; the error the
        handler fails the document on, and the exception map it re-raises from,
        must both say page 1."""

        def invoke(content, config):
            if "FORM X continued" in _sent_text(content):
                raise RuntimeError("model unavailable")
            return _reply(_page_verdict(_sent_text(content)))

        service = _service("printed_page_numbers")
        with (
            patch(
                "idp_common.s3.get_text_content",
                side_effect=lambda uri: SHUFFLED[uri.split("/")[-2]],
            ),
            patch.object(service, "_invoke_bedrock_model", side_effect=invoke),
        ):
            result = service.classify_document(_document(SHUFFLED))

        assert result.errors == ["Error classifying page 1: model unavailable"]
        assert list(result.metadata["failed_page_exceptions"]) == ["1"]

    def test_a_raised_failure_still_maps_back_and_keeps_earlier_sections(self):
        from idp_common.models import Section

        service = _service("printed_page_numbers")
        document = _document(SHUFFLED)
        kept = [
            ProcessingIssue(
                stage=stage,
                severity="warning",
                code=f"{stage}_earlier",
                message="pages (2)",
                details={"page_ids": ["2"]},
            )
            for stage in ("classification", "extraction")
        ]
        earlier = [
            Section(
                section_id="1",
                classification="cover",
                page_ids=["2"],
                processing_issues=list(kept),
            ),
            # Out of first-page order on purpose: a run that raised must not
            # reorder or renumber what it never produced.
            Section(section_id="2", classification="form_x", page_ids=["1"]),
        ]
        document.sections = list(earlier)
        with (
            patch(
                "idp_common.s3.get_text_content",
                side_effect=lambda uri: SHUFFLED[uri.split("/")[-2]],
            ),
            patch.object(
                service,
                "_invoke_bedrock_model",
                side_effect=lambda content, config: _reply(
                    _page_verdict(_sent_text(content))
                ),
            ),
            patch.object(
                ClassificationService,
                "_apply_section_splitting_strategy",
                side_effect=RuntimeError("splitting broke"),
            ),
            pytest.raises(RuntimeError, match="splitting broke"),
        ):
            service.classify_document(document)

        assert document.status == Status.FAILED
        assert document.errors == [
            "Error classifying all document pages: splitting broke"
        ]
        assert [(s.section_id, s.page_ids) for s in document.sections] == [
            ("1", ["2"]),
            ("2", ["1"]),
        ]
        # Issues written before this run already named original pages.
        assert [
            (i.message, i.details) for i in document.sections[0].processing_issues
        ] == [("pages (2)", {"page_ids": ["2"]})] * 2
        assert sorted(document.pages) == ["1", "2", "3", "4"]
        assert document.pages["1"].page_id == "1"


@pytest.mark.unit
def test_a_transient_text_read_retries_the_step_instead_of_keeping_scan_order():
    """A throttled read would otherwise become a 'no text' fallback, and the
    retry could then classify the same file in a different order."""
    from botocore.exceptions import ClientError

    throttled = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "GetObject",
    )
    service = _service("printed_page_numbers")
    with (
        patch("idp_common.s3.get_text_content", side_effect=throttled),
        patch.object(service, "_invoke_bedrock_model") as calls,
        pytest.raises(Exception) as raised,
    ):
        service.classify_document(_document(SHUFFLED))
    assert "ThrottlingException" in repr(raised.value) or "slow down" in str(
        raised.value
    )
    calls.assert_not_called()


@pytest.mark.unit
def test_a_repeated_fallback_record_replaces_the_earlier_one():
    service = _service("printed_page_numbers")
    texts = {**SHUFFLED, "4": "RECEIPT\nAmount paid: 25.00\nThank you"}
    result, _, _ = _classify(service, texts)
    plan = plan_printed_page_order(list(texts), texts)
    service._record_page_order_not_applied(result, plan)
    assert len(_issues(result, PAGE_ORDER_ISSUE_CODE)) == 1


@pytest.mark.unit
def test_translated_issue_page_ids_are_sorted_numerically():
    issue = ProcessingIssue(
        stage="classification",
        severity="warning",
        code="x",
        message="2 page(s) (1, 2)",
        details={"page_ids": ["1", "2"]},
    )
    ClassificationService._translate_issue_pages(issue, {"1": "10", "2": "9"})
    assert issue.details["page_ids"] == ["9", "10"]
    assert issue.message == "2 page(s) (9, 10)"


@pytest.mark.unit
class TestTranslatingPageReferences:
    TO_ORIGINAL = {"1": "2", "2": "3", "3": "1", "4": "4"}

    @pytest.mark.parametrize(
        ("text", "listed", "expected"),
        [
            ("Error classifying page 3: boom", None, "Error classifying page 1: boom"),
            ("window (pages 2-3) failed", None, "window (pages 1, 3) failed"),
            ("page 31 is not a view page", None, "page 31 is not a view page"),
            ("pages 3-9 out of range", None, "pages 3-9 out of range"),
            ("1 page(s) (3, 1)", ("3, 1", "1, 2"), "1 page(s) (1, 2)"),
            # The count beside the list is not a page ID, and a rewritten
            # reference is not rewritten a second time.
            ("1 page(s) (1)", ("1", "2"), "1 page(s) (2)"),
            ("x (pages 3) and page 3", ("3", "1"), "x (pages 1) and page 1"),
            # A longer number that merely starts with the listed one is not it.
            ("see (12) and (1)", ("1", "2"), "see (12) and (2)"),
            ("(3, 12) then (3)", ("3", "1"), "(3, 12) then (1)"),
        ],
    )
    def test_one_pass_rewrites_each_reference_once(self, text, listed, expected):
        assert (
            ClassificationService._translate_page_refs(text, self.TO_ORIGINAL, listed)
            == expected
        )


# --------------------------------------------------------------------------
# The page-number reader
# --------------------------------------------------------------------------

BODY = "\n".join(f"Body line {i} with no page marker" for i in range(1, 9))


@pytest.mark.unit
class TestReadingAPrintedPageNumber:
    @pytest.mark.parametrize(
        ("footer", "number", "total"),
        [
            ("Page 3 of 7", 3, 7),
            ("PAGE 3 OF 7", 3, 7),
            ("page 3/7", 3, 7),
            ("Pg. 3 of 7", 3, 7),
            ("Form 1040 (2023)   Page 3", 3, None),
            ("| Form W-2 | Page 3 of 7 |", 3, 7),
            ("Page 3", 3, None),
            ("p. 3", 3, None),
            ("3 of 7", 3, 7),
            ("- 3 -", 3, None),
            ("— 3 —", 3, None),
            ("3", 3, None),
            ("3/7", 3, 7),
            ("**Page 3 of 7**", 3, 7),
            ("## Page 3", 3, None),
            ("**- 3 -**", 3, None),
            ("| 3 |", 3, None),
            ("> 3 of 7", 3, 7),
        ],
    )
    def test_common_formats_in_the_footer(self, footer, number, total):
        reading = read_printed_page_number(f"{BODY}\n{footer}")
        assert (reading.number, reading.total) == (number, total)

    def test_blank_lines_do_not_count_towards_the_zone(self):
        assert (
            read_printed_page_number(f"{BODY}\nlast body line\n\n\n\n\n- 4 -").number
            == 4
        )
        assert read_printed_page_number(f"\n\n\n\nPage 2\n{BODY}").number == 2

    def test_a_header_number_is_read(self):
        assert read_printed_page_number(f"Page 2 of 9\n{BODY}").number == 2

    # The OCR step appends notes after the page text. They are not printed on
    # the page, so a multi-line footer must still be read with a note below it.
    FOOTER = "Rev. 01/2026\nPage 2 of 5\nConfidential"
    SIGNATURE_NOTE = (
        "--- OCR signature detections ---\n"
        "The OCR engine flagged 1 region on this page as containing a signature.\n"
        "- Signature 1: bottom right, confidence 98.1"
    )

    @pytest.mark.parametrize(
        "notes",
        [
            pytest.param(lambda: [HANDWRITING_LEGEND], id="handwriting"),
            pytest.param(
                lambda: [TestReadingAPrintedPageNumber.SIGNATURE_NOTE], id="signature"
            ),
            pytest.param(
                lambda: [
                    HANDWRITING_LEGEND,
                    TestReadingAPrintedPageNumber.SIGNATURE_NOTE,
                ],
                id="both",
            ),
        ],
    )
    def test_appended_ocr_notes_do_not_take_the_footer_zone(self, notes):
        text = "\n\n".join([f"{BODY}\n{self.FOOTER}", *notes()])
        reading = read_printed_page_number(text)
        assert (reading.number, reading.total) == (2, 5)

    @pytest.mark.parametrize(
        "footer, number, total",
        [
            (f"Page 2 {HANDWRITING_MARKER}", 2, None),
            (f"2 {HANDWRITING_MARKER}", 2, None),
            (f"Rev. 01/2026 Page 2 {HANDWRITING_MARKER}", 2, None),
            (f"Page 2 of 5 {HANDWRITING_MARKER}", 2, 5),
            (f"**Page 2** {HANDWRITING_MARKER}", 2, None),
        ],
    )
    def test_a_page_number_marked_as_handwriting_is_read(self, footer, number, total):
        """ocr.mark_handwriting puts "(HANDWRITING)" after a number Textract typed
        as handwriting (a hand-numbered packet, a stamp). It is still the number."""
        text = "\n\n".join([f"{BODY}\n{footer}", HANDWRITING_LEGEND])
        reading = read_printed_page_number(text)
        assert (reading.number, reading.total) == (number, total)

    def test_the_marker_page_order_removes_is_the_one_ocr_writes(self):
        """page_order keeps its own copy of the marker so classification does
        not import from ocr. This ties the copy to the constant: renaming
        HANDWRITING_MARKER fails here rather than silently breaking page-number
        reading."""
        assert page_order._HANDWRITING_MARKER_RE.fullmatch(f" {HANDWRITING_MARKER}")
        assert page_order._HANDWRITING_MARKER_RE.fullmatch(HANDWRITING_MARKER)

    def test_a_note_header_inside_the_page_text_is_not_a_note(self):
        """Only a header after a blank line starts a note; the same text inside a
        paragraph is page text."""
        text = f"{BODY}\nsee --- OCR handwriting markers --- below\n{self.FOOTER}"
        assert read_printed_page_number(text).number == 2

    @pytest.mark.parametrize(
        "line",
        [
            "Continued on page 4",
            "see page 4 of the instructions",
            "Refer to page 4",
            "2024",
            "Page 0",
            "Page 8 of 7",
            "Total: 4",
            "12/2025",
            "Invoice 4 of the series dated 1/2",
        ],
    )
    def test_references_years_and_impossible_numbers_are_not_page_numbers(self, line):
        assert read_printed_page_number(f"{BODY}\n{line}").number is None

    @pytest.mark.parametrize(
        "line",
        [
            "continued on  page 3",
            "as shown upon page 3",
            "Continued on Page 3",
            "Balance carried from previous page 1",
            "see Page 3",
            "Continued on reverse - page 2",
        ],
    )
    def test_a_page_named_inside_a_sentence_is_not_this_pages_number(self, line):
        assert read_printed_page_number(f"{BODY}\n{line}").number is None

    @pytest.mark.parametrize(
        ("line", "number"),
        [
            ("Form MTPC-PA-001 Rev. 01/2026 • Page 1", 1),
            ("Form 1040 (2023) Page 2", 2),
            ("Statement 0000667 | PAGE 3", 3),
            ("Continued on reverse - Page 2", 2),
        ],
    )
    def test_a_page_label_after_a_separator_is_read(self, line, number):
        assert read_printed_page_number(f"{BODY}\n{line}").number == number

    def test_two_different_totals_on_one_page_are_ambiguous(self):
        reading = read_printed_page_number(f"Page 2 of 4\n{BODY}\nPage 2 of 5")
        assert reading.number is None
        assert reading.ambiguous

    def test_every_numbering_on_a_line_is_read(self):
        reading = read_printed_page_number(f"{BODY}\nPage 2 of 4    Page 3 of 4")
        assert reading.number is None
        assert reading.candidates == (2, 3)

    def test_numbers_in_the_body_are_never_read(self):
        body = "Title\nSubtitle\nIntro\n7\nPage 5 of 9\n- 6 -\nOutro\nEnd\nFin"
        assert read_printed_page_number(body).number is None

    @pytest.mark.parametrize("bare", ["12", "3/5"])
    def test_an_explicit_number_wins_over_a_bare_one(self, bare):
        reading = read_printed_page_number(f"Page 4 of 9\n{BODY}\n{bare}")
        assert (reading.number, reading.candidates) == (4, (4,))

    def test_two_explicit_numbers_are_ambiguous(self):
        reading = read_printed_page_number(f"Page 4 of 9\n{BODY}\nPage 5 of 9")
        assert reading.number is None
        assert reading.candidates == (4, 5)

    def test_the_same_number_in_header_and_footer_is_one_reading(self):
        assert read_printed_page_number(f"Page 4 of 9\n{BODY}\n- 4 -").number == 4

    @pytest.mark.parametrize("text", [None, "", "  \n\t"])
    def test_no_text(self, text):
        assert read_printed_page_number(text).has_text is False

    def test_the_zone_is_the_configured_number_of_lines(self):
        """Pinned to the constant, so a change to the zone is a decision."""
        assert page_order.ZONE_LINES == 3
        inside = f"a\nb\nPage 2\n{BODY}"
        outside = f"a\nb\nc\nPage 2\n{BODY}"
        assert read_printed_page_number(inside).number == 2
        assert read_printed_page_number(outside).number is None


@pytest.mark.unit
class TestPlanningAnOrder:
    @staticmethod
    def _texts(numbers: Dict[str, str]) -> Dict[str, str]:
        return {pid: f"{BODY}\n{footer}" for pid, footer in numbers.items()}

    def test_a_shuffled_sequence_is_sorted(self):
        plan = plan_printed_page_order(
            ["1", "2", "3", "4"],
            self._texts({"1": "3", "2": "1", "3": "2", "4": "4"}),
        )
        assert plan.order == ["2", "3", "1", "4"]
        assert plan.numbers == {"1": 3, "2": 1, "3": 2, "4": 4}

    def test_a_run_need_not_start_at_one(self):
        plan = plan_printed_page_order(
            ["1", "2", "3"], self._texts({"1": "- 6 -", "2": "- 5 -", "3": "- 7 -"})
        )
        assert plan.order == ["2", "1", "3"]

    def test_bare_numerals_must_run_from_one(self):
        """A form's box values land on lines of their own in OCR text, and a
        consecutive pair of them is not a page sequence."""
        plan = plan_printed_page_order(
            ["1", "2"], self._texts({"1": "239", "2": "238"})
        )
        assert plan.order is None
        assert plan.reason_code == "unanchored"
        assert plan.problem_page_ids == ["1", "2"]

    def test_bare_numerals_from_one_are_sorted(self):
        plan = plan_printed_page_order(
            ["1", "2", "3"], self._texts({"1": "2", "2": "3", "3": "1"})
        )
        assert plan.order == ["3", "1", "2"]

    def test_one_explicit_page_does_not_anchor_bare_neighbours(self):
        plan = plan_printed_page_order(
            ["1", "2"], self._texts({"1": "Page 7", "2": "8"})
        )
        assert plan.reason_code == "unanchored"
        assert plan.problem_page_ids == ["2"]

    def test_a_packet_of_documents_numbered_from_one_is_not_reordered(self):
        plan = plan_printed_page_order(
            ["1", "2", "3", "4", "5"],
            self._texts(
                {
                    "1": "Page 1 of 2",
                    "2": "Page 2 of 2",
                    "3": "Page 1 of 3",
                    "4": "Page 2 of 3",
                    "5": "Page 3 of 3",
                }
            ),
        )
        assert plan.order is None
        assert plan.reason_code == "inconsistent_totals"

    def test_the_reason_names_the_pages(self):
        plan = plan_printed_page_order(
            ["1", "2"], self._texts({"1": "Page 1", "2": "no number here"})
        )
        assert plan.order is None
        assert plan.reason_code == "no_number"
        assert plan.problem_page_ids == ["2"]
        assert re.search(r"page\(s\) 2\b", plan.reason or "")

    def test_a_long_list_is_shortened_in_the_reason(self):
        ids = [str(i) for i in range(1, 16)]
        plan = plan_printed_page_order(ids, {pid: "nothing" for pid in ids})
        assert plan.problem_page_ids == ids
        assert "and 5 more" in (plan.reason or "")


@pytest.mark.unit
def test_the_issue_code_constant_is_the_one_the_service_writes():
    """The service spells the code as a literal (so the benchmark harness's
    discovery reads it) and filters on the constant; the two must agree."""
    import inspect

    source = inspect.getsource(ClassificationService._record_page_order_not_applied)
    assert f'code="{PAGE_ORDER_ISSUE_CODE}"' in source


# --------------------------------------------------------------------------
# validate_config (what idp-cli config-validate reports)
# --------------------------------------------------------------------------


def _validated(page_order_value: Optional[str], **top: Any) -> Dict[str, Any]:
    from idp_common.config.merge_utils import validate_config

    config: Dict[str, Any] = {"classes": [_class("cover")], **top}
    if page_order_value is not None:
        config["classification"] = {"document_shape": {"pageOrder": page_order_value}}
    return validate_config(config)


def _page_order_findings(result: Dict[str, Any]) -> List[str]:
    return [m for m in result["warnings"] + result["errors"] if "pageOrder" in m]


@pytest.mark.unit
class TestValidateConfig:
    @pytest.mark.parametrize("value", [None, "scan", "printed_page_numbers"])
    def test_a_usable_setting_is_valid_and_not_reported(self, value):
        result = _validated(value)
        assert result["valid"] is True
        assert _page_order_findings(result) == []

    def test_a_misspelt_value_is_an_error(self):
        result = _validated("printed")
        assert result["valid"] is False
        assert any("pageOrder must be one of" in e for e in result["errors"])

    def test_no_ocr_text_is_a_warning(self):
        result = _validated("printed_page_numbers", ocr={"backend": "none"})
        assert result["valid"] is True
        assert len(_page_order_findings(result)) == 1
        assert "ocr.backend is 'none'" in _page_order_findings(result)[0]

    def test_bda_mode_is_a_warning(self):
        result = _validated("printed_page_numbers", use_bda=True)
        assert result["valid"] is True
        assert len(_page_order_findings(result)) == 1
        assert "use_bda is true" in _page_order_findings(result)[0]

    def test_scan_with_no_ocr_text_is_not_reported(self):
        result = _validated("scan", ocr={"backend": "none"})
        assert _page_order_findings(result) == []

    def test_the_system_default_is_scan(self):
        result = _validated(None)
        shape = result["merged_config"]["classification"]["document_shape"]
        assert shape["pageOrder"] == "scan"
