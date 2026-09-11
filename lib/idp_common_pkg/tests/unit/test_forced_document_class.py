# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The forced-class path that makes a class correction actually take effect.

A reviewer correcting a misclassified document and asking for a re-extract is
asserting that the pipeline's own classification is wrong. So the corrected class
has to *override* classification rather than seed it — and it has to survive four
hops to get there: resolver → runner → copier → S3 metadata → Document →
classification step.

Before this existed, the class was pinned only in the test set's baseline. The run
classified from the input document, never read that baseline, and the harvest wrote
the pipeline's own class back over the pin — so the reviewer saw "Re-extracted as
W2" while the document stayed a Bank-Statement with Bank-Statement fields.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from idp_common.classification import apply_forced_document_class
from idp_common.models import Document, Page, Status

_REPO = Path(__file__).resolve().parents[4]


def _load(path: Path, name: str):
    """Import a Lambda module by path (they are not packages)."""
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
class TestDocumentCarriesTheForcedClass:
    def test_it_survives_a_serialization_round_trip(self):
        """Set before OCR, read at classification — so it crosses step
        boundaries as JSON. Dropping it from to_dict would silently re-enable
        classification and lose the correction."""
        doc = Document(id="d.pdf", input_key="d.pdf", forced_document_class="W2")

        restored = Document.from_dict(doc.to_dict())

        assert restored.forced_document_class == "W2"

    def test_it_defaults_to_none_for_an_ordinary_document(self):
        """Every other document must be classified normally."""
        doc = Document(id="d.pdf", input_key="d.pdf")

        assert doc.forced_document_class is None
        assert Document.from_dict(doc.to_dict()).forced_document_class is None

    def test_it_is_read_from_s3_object_metadata(self):
        """The copier stamps `document-class`; this is where it enters."""
        event = {
            "detail": {"bucket": {"name": "in"}, "object": {"key": "run1/d.pdf"}},
            "time": "2026-08-25T00:00:00Z",
        }
        head = {"Metadata": {"document-class": "W2", "config-version": "v3"}}

        with patch("boto3.client") as mock_client:
            mock_client.return_value.head_object.return_value = head
            doc = Document.from_s3_event(event, "out")

        assert doc.forced_document_class == "W2"
        assert doc.config_version == "v3"

    def test_an_object_without_the_metadata_is_unaffected(self):
        event = {
            "detail": {"bucket": {"name": "in"}, "object": {"key": "d.pdf"}},
            "time": "2026-08-25T00:00:00Z",
        }
        with patch("boto3.client") as mock_client:
            mock_client.return_value.head_object.return_value = {"Metadata": {}}
            doc = Document.from_s3_event(event, "out")

        assert doc.forced_document_class is None


@pytest.mark.unit
class TestClassificationHonoursIt:
    """``apply_forced_document_class`` — the rule that turns the metadata into
    behaviour. Extracted from the Lambda so it can be tested without standing up
    the document service, metering and X-Ray."""

    @staticmethod
    def _doc_with_pages(forced=None, existing=None):
        doc = Document(
            id="d.pdf",
            input_key="d.pdf",
            output_bucket="out",
            status=Status.OCR,
            forced_document_class=forced,
        )
        doc.pages = {"1": Page(page_id="1"), "2": Page(page_id="2")}
        if existing:
            for page in doc.pages.values():
                page.classification = existing
        return doc

    def test_a_forced_class_is_applied_to_every_page(self):
        """All pages, because that is what routes into the classification step's
        pre-existing already-classified skip — rather than adding a second skip
        path that has to be kept in step with the first."""
        doc = self._doc_with_pages(forced="W2")

        applied = apply_forced_document_class(doc)

        assert applied == "W2"
        assert [p.classification for p in doc.pages.values()] == ["W2", "W2"]
        assert all(p.confidence == 1.0 for p in doc.pages.values())

    def test_it_also_builds_the_section_covering_those_pages(self):
        """The half that stamping pages alone misses, and that only a live run
        exposed: the already-classified skip returns the document untouched, so a
        document fresh from OCR reaches extraction with sections == [], produces
        no fields at all, and the run "succeeds" having extracted nothing.
        """
        doc = self._doc_with_pages(forced="W2")
        assert doc.sections == []

        apply_forced_document_class(doc)

        assert len(doc.sections) == 1
        section = doc.sections[0]
        assert section.section_id == "1"
        assert section.classification == "W2"
        assert section.page_ids == ["1", "2"]
        assert section.confidence == 1.0

    def test_one_section_covers_the_whole_document(self):
        """A reviewer corrected the class of a *document*, not of one span of
        pages — the same reading the classification service uses when section
        splitting is disabled."""
        doc = self._doc_with_pages(forced="W2")
        doc.pages["3"] = Page(page_id="3")

        apply_forced_document_class(doc)

        assert len(doc.sections) == 1
        assert doc.sections[0].page_ids == ["1", "2", "3"]

    def test_it_replaces_sections_from_the_wrong_class(self):
        """Re-extracting under a corrected class must not leave the previous
        class's sections behind."""
        from idp_common.models import Section

        doc = self._doc_with_pages(forced="W2")
        doc.sections = [
            Section(section_id="1", classification="Bank-Statement", page_ids=["1"]),
            Section(section_id="2", classification="Bank-Statement", page_ids=["2"]),
        ]

        apply_forced_document_class(doc)

        assert [s.classification for s in doc.sections] == ["W2"]

    def test_it_overrides_an_existing_classification(self):
        """The heart of the fix. The reviewer's request means "the class you
        picked is wrong", so preserving the model's answer would re-derive the
        error and silently discard the correction."""
        doc = self._doc_with_pages(forced="W2", existing="Bank-Statement")

        applied = apply_forced_document_class(doc)

        assert applied == "W2"
        assert all(p.classification == "W2" for p in doc.pages.values())

    def test_without_a_forced_class_nothing_is_touched(self):
        """Every ordinary document must still be classified by the model — and
        must NOT get a section invented for it, which would make the real
        classification step skip its own work."""
        doc = self._doc_with_pages(forced=None)

        applied = apply_forced_document_class(doc)

        assert applied is None
        assert all(p.classification is None for p in doc.pages.values())
        assert doc.sections == []

    def test_an_existing_classification_survives_when_nothing_is_forced(self):
        doc = self._doc_with_pages(forced=None, existing="Bank-Statement")

        assert apply_forced_document_class(doc) is None
        assert all(p.classification == "Bank-Statement" for p in doc.pages.values())

    def test_a_document_with_no_pages_yet_is_a_no_op(self):
        """Called before OCR has produced pages on some paths; must not raise."""
        doc = Document(id="d.pdf", input_key="d.pdf", forced_document_class="W2")
        doc.pages = {}

        assert apply_forced_document_class(doc) is None

    def test_a_document_without_the_attribute_at_all_is_a_no_op(self):
        """Deserialized from an older payload that predates the field."""

        class Legacy:
            pages = {"1": Page(page_id="1")}

        assert apply_forced_document_class(Legacy()) is None


@pytest.mark.unit
class TestCopierStampsIt:
    def test_the_class_becomes_s3_object_metadata(self):
        """Document.from_s3_event reads `document-class`, so the key name is a
        contract between two Lambdas that never import each other."""
        copier = _load(
            _REPO / "src/lambda/test_file_copier/index.py", "test_file_copier_forced"
        )
        mock_s3 = MagicMock()

        with patch.object(copier, "s3", mock_s3):
            copier._copy_files_to_bucket(
                "src-bucket",
                "ts1/input/",
                "dst-bucket",
                "run1/",
                ["d.pdf"],
                config_version="v3",
                submission_source="test-studio",
                test_set_id="ts1",
                forced_document_class="W2",
            )

        metadata = mock_s3.copy_object.call_args.kwargs["Metadata"]
        assert metadata["document-class"] == "W2"
        assert metadata["config-version"] == "v3"
        assert mock_s3.copy_object.call_args.kwargs["MetadataDirective"] == "REPLACE"

    def test_an_ordinary_run_stamps_no_class(self):
        """Only a re-extract forces a class; a normal run must classify."""
        copier = _load(
            _REPO / "src/lambda/test_file_copier/index.py", "test_file_copier_plain"
        )
        mock_s3 = MagicMock()

        with patch.object(copier, "s3", mock_s3):
            copier._copy_files_to_bucket(
                "src-bucket",
                "ts1/input/",
                "dst-bucket",
                "run1/",
                ["d.pdf"],
                config_version="v3",
                test_set_id="ts1",
            )

        assert "document-class" not in mock_s3.copy_object.call_args.kwargs["Metadata"]
