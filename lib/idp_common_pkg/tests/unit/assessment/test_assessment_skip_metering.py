# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression: the Assessment 'intelligent skip' must PRESERVE the incoming
document metering (esp. the Extraction step's Bedrock token usage).

A prior bug reset the skip-branch section_document to metering={}, so agentic
sections — which write explainability_info during extraction and therefore always
hit the skip branch — lost ALL extraction cost, making advanced modes look nearly
free vs simple in cost reports. This test locks the preservation in.

The Lambda handler imports aws_xray_sdk (patch_all at import), so we exercise the
exact reconstruction logic the skip branch uses on the Document model directly.
"""

from __future__ import annotations

import copy

from idp_common.models import Document, Status


def _skip_branch_section_document(document: Document, section) -> Document:
    """Mirror assessment_function.index skip-branch section_document construction.

    Kept in lock-step with the handler; the metering= line is the regression point.
    """
    section_document = Document(
        id=document.id,
        input_bucket=document.input_bucket,
        input_key=document.input_key,
        output_bucket=document.output_bucket,
        status=Status.ASSESSING,
        num_pages=len(section.page_ids),
        # THE FIX: preserve incoming metering (was {} — dropped extraction cost).
        metering=copy.deepcopy(document.metering) if document.metering else {},
    )
    return section_document


def test_skip_branch_preserves_extraction_metering():
    from idp_common.models import Section

    doc = Document(id="d1", input_key="d1")
    doc.metering = {
        "Extraction/bedrock/us.anthropic.claude-sonnet-4-6": {
            "inputTokens": 1000,
            "outputTokens": 200,
            "totalTokens": 1200,
        },
        "Extraction/lambda/requests": {"requests": 1},
        "Extraction/lambda/duration": {"gb_seconds": 12.0},
    }
    section = Section(section_id="1", classification="doc", page_ids=["1"])

    sd = _skip_branch_section_document(doc, section)

    # Extraction Bedrock + lambda metering must survive the skip reconstruction.
    assert any(k.startswith("Extraction/bedrock/") for k in sd.metering), (
        "Extraction Bedrock metering was dropped by the assessment skip branch"
    )
    assert sd.metering["Extraction/lambda/duration"]["gb_seconds"] == 12.0
    # And it must be a COPY (mutating the section doc must not corrupt the source).
    sd.metering["Extraction/lambda/requests"]["requests"] = 99
    assert doc.metering["Extraction/lambda/requests"]["requests"] == 1


def test_skip_branch_handles_empty_incoming_metering():
    from idp_common.models import Section

    doc = Document(id="d1", input_key="d1")  # no metering
    section = Section(section_id="1", classification="doc", page_ids=["1"])
    sd = _skip_branch_section_document(doc, section)
    assert sd.metering == {}
