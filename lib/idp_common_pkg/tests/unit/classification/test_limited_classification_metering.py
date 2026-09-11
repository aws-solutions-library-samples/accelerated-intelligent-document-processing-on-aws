# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for metering data transfer in limited classification scenarios.
"""

import pytest

from idp_common.classification.service import ClassificationService
from idp_common.models import Document, Page, Section


@pytest.mark.unit
def test_apply_limited_classification_transfers_metering():
    """Test that metering data is transferred from classified document to original document."""
    # Create classification service with minimal config
    config = {
        "classification": {
            "model": "anthropic.claude-3-sonnet-20240229-v1:0",
            "maxPagesForClassification": 2,
        },
    }
    service = ClassificationService(region="us-east-1", config=config)

    # Create original document with 5 pages
    original_doc = Document(
        id="original-doc",
        pages={
            "1": Page(page_id="1"),
            "2": Page(page_id="2"),
            "3": Page(page_id="3"),
            "4": Page(page_id="4"),
            "5": Page(page_id="5"),
        },
        metering={},
        errors=[],
        metadata={},
    )

    # Create classified document with 2 pages and metering data
    classified_doc = Document(
        id="classified-doc",
        pages={
            "1": Page(page_id="1", classification="invoice"),
            "2": Page(page_id="2", classification="invoice"),
        },
        sections=[
            Section(
                section_id="1",
                classification="invoice",
                page_ids=["1", "2"],
                confidence=0.9,
            )
        ],
        metering={
            "Classification/bedrock/anthropic.claude-3-sonnet": {
                "inputTokens": 1000,
                "outputTokens": 100,
                "totalTokens": 1100,
            }
        },
        errors=["Classification warning"],
        metadata={"processing_time": 2.5},
    )

    # Apply limited classification to all pages
    result_doc = service._apply_limited_classification_to_all_pages(
        original_doc, classified_doc
    )

    # Verify metering data was transferred
    assert result_doc.metering == {
        "Classification/bedrock/anthropic.claude-3-sonnet": {
            "inputTokens": 1000,
            "outputTokens": 100,
            "totalTokens": 1100,
        }
    }

    # Verify errors were transferred
    assert "Classification warning" in result_doc.errors

    # Verify metadata was transferred
    assert result_doc.metadata["processing_time"] == 2.5

    # Verify classification was applied to all pages
    assert len(result_doc.sections) == 1
    assert result_doc.sections[0].classification == "invoice"
    assert len(result_doc.sections[0].page_ids) == 5  # All original pages

    # Verify all pages have the classification
    for page in result_doc.pages.values():
        assert page.classification == "invoice"
        # maxPagesForClassification extrapolates the class from a sample to pages
        # the model never saw, so no page here carries a confidence — it used to
        # claim 1.0 for all five (GitHub #673).
        assert page.confidence is None
    assert result_doc.sections[0].confidence is None


@pytest.mark.unit
def test_apply_limited_classification_merges_existing_metering():
    """Test that metering data is merged with existing metering in original document."""
    config = {"classification": {"model": "anthropic.claude-3-sonnet-20240229-v1:0"}}
    service = ClassificationService(region="us-east-1", config=config)

    # Original document with existing metering
    original_doc = Document(
        id="original-doc",
        pages={"1": Page(page_id="1")},
        metering={
            "OCR/textract/detect_document_text": {
                "pages": 1,
                "cost": 0.001,
            }
        },
    )

    # Classified document with new metering
    classified_doc = Document(
        id="classified-doc",
        pages={"1": Page(page_id="1", classification="receipt")},
        sections=[
            Section(
                section_id="1",
                classification="receipt",
                page_ids=["1"],
                confidence=0.8,
            )
        ],
        metering={
            "Classification/bedrock/anthropic.claude-3-sonnet": {
                "inputTokens": 500,
                "outputTokens": 50,
            }
        },
    )

    # Apply limited classification
    result_doc = service._apply_limited_classification_to_all_pages(
        original_doc, classified_doc
    )

    # Verify both metering entries exist
    assert "OCR/textract/detect_document_text" in result_doc.metering
    assert "Classification/bedrock/anthropic.claude-3-sonnet" in result_doc.metering
    assert result_doc.metering["OCR/textract/detect_document_text"]["pages"] == 1
    assert (
        result_doc.metering["Classification/bedrock/anthropic.claude-3-sonnet"][
            "inputTokens"
        ]
        == 500
    )


@pytest.mark.unit
def test_apply_limited_classification_no_metering_data():
    """Test that method works correctly when classified document has no metering data."""
    config = {"classification": {"model": "anthropic.claude-3-sonnet-20240229-v1:0"}}
    service = ClassificationService(region="us-east-1", config=config)

    original_doc = Document(
        id="original-doc",
        pages={"1": Page(page_id="1")},
    )

    classified_doc = Document(
        id="classified-doc",
        pages={"1": Page(page_id="1", classification="form")},
        sections=[
            Section(
                section_id="1",
                classification="form",
                page_ids=["1"],
                confidence=0.7,
            )
        ],
        metering={},  # Empty metering
    )

    # Should not raise an error
    result_doc = service._apply_limited_classification_to_all_pages(
        original_doc, classified_doc
    )

    # Original metering should be preserved (empty in this case)
    assert result_doc.metering == {}
    assert result_doc.pages["1"].classification == "form"
