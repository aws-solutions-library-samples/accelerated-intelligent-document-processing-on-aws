# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the classification models module.
"""

import pytest

from idp_common.classification.models import (
    ClassificationResult,
    DocumentClassification,
    DocumentSection,
    DocumentType,
    PageClassification,
)


@pytest.mark.unit
class TestDocumentType:
    """Tests for the DocumentType class."""

    def test_init(self):
        """Test initialization with basic attributes."""
        doc_type = DocumentType(type_name="invoice", description="An invoice document")

        assert doc_type.type_name == "invoice"
        assert doc_type.description == "An invoice document"


@pytest.mark.unit
class TestDocumentClassification:
    """Tests for the DocumentClassification class."""

    def test_init_with_defaults(self):
        """Test initialization with default values."""
        classification = DocumentClassification(doc_type="invoice")

        assert classification.doc_type == "invoice"
        # Unscored by default: a confidence has to come from somewhere (the model
        # returning one, or a caller that can genuinely assert the class).
        assert classification.confidence is None
        assert classification.metadata == {}

    def test_init_with_custom_values(self):
        """Test initialization with custom values."""
        metadata = {"source": "bedrock", "model": "claude-3"}
        classification = DocumentClassification(
            doc_type="receipt", confidence=0.85, metadata=metadata
        )

        assert classification.doc_type == "receipt"
        assert classification.confidence == 0.85
        assert classification.metadata == metadata


@pytest.mark.unit
class TestPageClassification:
    """Tests for the PageClassification class."""

    def test_init_with_required_fields(self):
        """Test initialization with only required fields."""
        classification = DocumentClassification(doc_type="invoice")
        page_classification = PageClassification(
            page_id="1", classification=classification
        )

        assert page_classification.page_id == "1"
        assert page_classification.classification == classification
        assert page_classification.image_uri is None
        assert page_classification.text_uri is None
        assert page_classification.raw_text_uri is None

    def test_init_with_all_fields(self):
        """Test initialization with all fields."""
        classification = DocumentClassification(doc_type="invoice")
        page_classification = PageClassification(
            page_id="1",
            classification=classification,
            image_uri="s3://bucket/image.jpg",
            text_uri="s3://bucket/text.txt",
            raw_text_uri="s3://bucket/raw.json",
        )

        assert page_classification.page_id == "1"
        assert page_classification.classification == classification
        assert page_classification.image_uri == "s3://bucket/image.jpg"
        assert page_classification.text_uri == "s3://bucket/text.txt"
        assert page_classification.raw_text_uri == "s3://bucket/raw.json"

    def test_to_dict(self):
        """Test conversion to dictionary."""
        classification = DocumentClassification(
            doc_type="invoice", confidence=0.9, metadata={"source": "bedrock"}
        )
        page_classification = PageClassification(
            page_id="1",
            classification=classification,
            image_uri="s3://bucket/image.jpg",
            text_uri="s3://bucket/text.txt",
            raw_text_uri="s3://bucket/raw.json",
        )

        result = page_classification.to_dict()

        assert result["page_id"] == "1"
        assert result["class"] == "invoice"
        assert result["confidence"] == 0.9
        assert result["imageUri"] == "s3://bucket/image.jpg"
        assert result["parsedTextUri"] == "s3://bucket/text.txt"
        assert result["rawTextUri"] == "s3://bucket/raw.json"
        assert result["source"] == "bedrock"  # Metadata is flattened into the dict


@pytest.mark.unit
class TestDocumentSection:
    """Tests for the DocumentSection class."""

    def test_init(self):
        """Test initialization with basic attributes."""
        classification = DocumentClassification(doc_type="invoice")
        page1 = PageClassification(page_id="1", classification=classification)
        page2 = PageClassification(page_id="2", classification=classification)

        section = DocumentSection(
            section_id="section-1", classification=classification, pages=[page1, page2]
        )

        assert section.section_id == "section-1"
        assert section.classification == classification
        assert len(section.pages) == 2
        assert section.pages[0] == page1
        assert section.pages[1] == page2

    def test_to_dict(self):
        """Test conversion to dictionary."""
        classification = DocumentClassification(doc_type="invoice")
        page1 = PageClassification(
            page_id="1",
            classification=classification,
            image_uri="s3://bucket/image1.jpg",
        )
        page2 = PageClassification(
            page_id="2",
            classification=classification,
            image_uri="s3://bucket/image2.jpg",
        )

        section = DocumentSection(
            section_id="section-1", classification=classification, pages=[page1, page2]
        )

        result = section.to_dict()

        assert result["id"] == "section-1"
        assert result["class"] == "invoice"
        assert len(result["pages"]) == 2
        assert result["pages"][0]["page_id"] == "1"
        assert result["pages"][1]["page_id"] == "2"


@pytest.mark.unit
class TestClassificationResult:
    """Tests for the ClassificationResult class."""

    def test_init(self):
        """Test initialization with basic attributes."""
        classification = DocumentClassification(doc_type="invoice")
        page1 = PageClassification(page_id="1", classification=classification)
        page2 = PageClassification(page_id="2", classification=classification)

        section = DocumentSection(
            section_id="section-1", classification=classification, pages=[page1, page2]
        )

        metadata = {"processing_time": 1.5, "model": "claude-3"}

        result = ClassificationResult(metadata=metadata, sections=[section])

        assert result.metadata == metadata
        assert len(result.sections) == 1
        assert result.sections[0] == section

    def test_to_dict(self):
        """Test conversion to dictionary."""
        classification = DocumentClassification(doc_type="invoice")
        page1 = PageClassification(page_id="1", classification=classification)
        page2 = PageClassification(page_id="2", classification=classification)

        section = DocumentSection(
            section_id="section-1", classification=classification, pages=[page1, page2]
        )

        metadata = {"processing_time": 1.5, "model": "claude-3"}

        result = ClassificationResult(metadata=metadata, sections=[section])

        dict_result = result.to_dict()

        assert dict_result["metadata"] == metadata
        assert len(dict_result["sections"]) == 1
        assert dict_result["sections"][0]["id"] == "section-1"
        assert dict_result["sections"][0]["class"] == "invoice"
        assert len(dict_result["sections"][0]["pages"]) == 2
