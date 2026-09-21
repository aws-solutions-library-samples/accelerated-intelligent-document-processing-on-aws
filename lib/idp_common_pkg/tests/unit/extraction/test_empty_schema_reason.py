# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An empty extraction schema has three causes, and only one of them is a fault.

``ExtractionService._get_class_schema`` returns ``{}`` both for a class the
configuration contains and gave no attributes, and for a label the configuration
does not contain at all; ``_prepare_section_context`` routes both to
``_handle_empty_schema``, which used to set one ``skipped_due_to_empty_attributes``
flag for every case. Downstream — specifically the confidence pass's carve-out —
could then only treat them identically, and it chose silence, because the common
case is an ordinary blank page.

The case that silence was wrong for is a section carrying a **named class the
configuration in force does not have**: a class renamed or deleted while documents
were in flight, or an old document reprocessed under a newer configuration. Those
sections completed green, with no fields and no signal of any kind.

So the stub now records WHY (``metadata.empty_schema_reason``), and extraction
reports the fault itself — at the stage that discovered it and can name the
remedy — rather than leaving it to a later stage whose ``root_cause`` would point
somewhere else.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from idp_common.config.models import IDPConfig
from idp_common.empty_schema import (
    EMPTY_SCHEMA_CLASS_NOT_CONFIGURED,
    EMPTY_SCHEMA_NO_ATTRIBUTES,
    EMPTY_SCHEMA_REASON_KEY,
    EMPTY_SCHEMA_UNCLASSIFIED,
)
from idp_common.extraction.service import ExtractionService, SectionInfo
from idp_common.models import Document, ProcessingIssue, Section, Status

_ATTRIBUTE_LESS_CLASS = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "cover_sheet",
    "x-aws-idp-document-type": "cover_sheet",
    "type": "object",
    "description": "A cover sheet, deliberately with nothing to extract",
}


def _service(*, fallback: str | None = None) -> ExtractionService:
    classification: dict = {}
    if fallback is not None:
        classification["invalidClassFallback"] = fallback
    cfg = IDPConfig(
        **{
            "classes": [_ATTRIBUTE_LESS_CLASS],
            **({"classification": classification} if classification else {}),
        }
    )
    return ExtractionService(config=cfg)


def _document() -> Document:
    doc = Document(
        id="doc.pdf",
        input_key="doc.pdf",
        input_bucket="input-bucket",
        output_bucket="output-bucket",
        status=Status.EXTRACTING,
    )
    doc.sections.append(Section(section_id="1", classification="x", page_ids=["1"]))
    return doc


def _section_info(class_label: str) -> SectionInfo:
    return SectionInfo(
        class_label=class_label,
        sorted_page_ids=["1"],
        page_indices=[1],
        output_bucket="output-bucket",
        output_key="doc.pdf/sections/1/result.json",
        output_uri="s3://output-bucket/doc.pdf/sections/1/result.json",
        start_page=1,
        end_page=1,
    )


def _run(service: ExtractionService, class_label: str):
    """Drive ``_handle_empty_schema`` and return (section, written stub)."""
    document = _document()
    section = document.sections[0]
    section.classification = class_label
    with patch("idp_common.s3.write_content") as mock_write:
        service._handle_empty_schema(
            document, section, _section_info(class_label), "1", 0.0
        )
    written = mock_write.call_args.args[0]
    return section, written


@pytest.mark.unit
class TestTheReasonIsRecorded:
    def test_configured_class_with_no_attributes(self):
        """The authoring choice. Nothing was expected, so nothing is missing."""
        section, written = _run(_service(), "cover_sheet")

        assert written["metadata"][EMPTY_SCHEMA_REASON_KEY] == (
            EMPTY_SCHEMA_NO_ATTRIBUTES
        )
        assert not section.processing_issues

    def test_unclassified_sentinel(self):
        """Classification determined no class. It owns the report, not extraction:
        only classification can tell a blank page from a page whose
        classification errored, and the severities differ."""
        section, written = _run(_service(), "unclassified")

        assert written["metadata"][EMPTY_SCHEMA_REASON_KEY] == (
            EMPTY_SCHEMA_UNCLASSIFIED
        )
        assert not section.processing_issues

    def test_configured_fallback_class_counts_as_unclassified(self):
        """A deployment that renames the fallback has not changed what it means."""
        _section, written = _run(_service(fallback="needs_review"), "needs_review")

        assert written["metadata"][EMPTY_SCHEMA_REASON_KEY] == (
            EMPTY_SCHEMA_UNCLASSIFIED
        )

    def test_named_class_absent_from_configuration_is_reported(self):
        """The fault: the section's fields were never extracted and, before this,
        nothing said so."""
        section, written = _run(_service(), "bank_statement")

        assert written["metadata"][EMPTY_SCHEMA_REASON_KEY] == (
            EMPTY_SCHEMA_CLASS_NOT_CONFIGURED
        )
        issues = [
            issue
            for issue in section.processing_issues
            if issue.code == "extraction_class_not_configured"
        ]
        assert len(issues) == 1
        issue = issues[0]
        assert issue.stage == "extraction"
        assert issue.severity == "error"
        assert issue.section_id == "1"
        assert "bank_statement" in issue.message
        assert "configuration" in issue.root_cause

        # Also written into the stub's metadata, which is what the Visual Editor's
        # Processing Report tab renders.
        assert [pi["code"] for pi in written["metadata"]["processing_issues"]] == [
            "extraction_class_not_configured"
        ]

    def test_reporting_preserves_issues_from_other_stages(self):
        """The DynamoDB writer replaces the whole section map, so a classification
        issue on the same section must survive being annotated here."""
        document = _document()
        section = document.sections[0]
        section.classification = "bank_statement"
        section.processing_issues = [
            ProcessingIssue(
                stage="classification",
                severity="warning",
                code="classification_page_no_content",
                message="blank page",
            )
        ]

        with patch("idp_common.s3.write_content"):
            _service()._handle_empty_schema(
                document, section, _section_info("bank_statement"), "1", 0.0
            )

        assert [i.code for i in section.processing_issues] == [
            "classification_page_no_content",
            "extraction_class_not_configured",
        ]

    def test_the_legacy_flag_is_still_set_in_every_case(self):
        """Stored results and existing readers key on it, and it is true for all
        three causes: the effective schema had no attributes. The reason refines
        it rather than replacing it."""
        for class_label in ("cover_sheet", "unclassified", "bank_statement"):
            _section, written = _run(_service(), class_label)
            assert written["metadata"]["skipped_due_to_empty_attributes"] is True
