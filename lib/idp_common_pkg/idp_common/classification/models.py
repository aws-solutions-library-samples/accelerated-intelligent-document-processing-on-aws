# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Data models for document classification.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DocumentType:
    """Document type definition with name, description, and optional regex patterns."""

    type_name: str
    """The name of the document type."""

    description: str
    """Description of the document type."""

    document_name_regex: Optional[str] = None
    """Optional regex pattern to match against document ID/name. When matched, all pages are classified as this type (single-class configurations only)."""

    document_page_content_regex: Optional[str] = None
    """Optional regex pattern to match against page content text. When matched during multi-modal page-level classification, the page is classified as this type."""

    excluded: bool = False
    """When True, sections classified as this document type are skipped by
    downstream services (extraction, assessment, summarization, rule_validation,
    evaluation). Use for classes whose pages contain only static content such
    as instructions, legal boilerplate, or cover pages."""

    exclusion_reason: Optional[str] = None
    """Optional short reason/category for why this class is excluded
    (e.g., "instructions", "legal", "cover-page"). Used for UI badges and
    evaluation report annotations."""

    # Private compiled regex patterns (not included in init)
    _compiled_name_regex: Optional[re.Pattern] = field(default=None, init=False)
    """Compiled regex pattern for document name matching."""

    _compiled_content_regex: Optional[re.Pattern] = field(default=None, init=False)
    """Compiled regex pattern for page content matching."""

    def __post_init__(self):
        """Compile regex patterns after initialization."""
        import logging

        logger = logging.getLogger(__name__)

        # Compile document name regex
        if self.document_name_regex:
            try:
                self._compiled_name_regex = re.compile(self.document_name_regex)
                logger.debug(
                    f"Compiled document name regex for class '{self.type_name}': {self.document_name_regex}"
                )
            except re.error as e:
                logger.error(
                    f"Invalid document name regex pattern for class '{self.type_name}': {self.document_name_regex} - Error: {e}"
                )
                self._compiled_name_regex = None

        # Compile document page content regex
        if self.document_page_content_regex:
            try:
                self._compiled_content_regex = re.compile(
                    self.document_page_content_regex
                )
                logger.debug(
                    f"Compiled page content regex for class '{self.type_name}': {self.document_page_content_regex}"
                )
            except re.error as e:
                logger.error(
                    f"Invalid page content regex pattern for class '{self.type_name}': {self.document_page_content_regex} - Error: {e}"
                )
                self._compiled_content_regex = None


@dataclass
class DocumentClassification:
    """Classification result for a document or page."""

    doc_type: str
    """The classified document type."""

    confidence: Optional[float] = None
    """Confidence in ``doc_type``, in ``[0.0, 1.0]``, or ``None`` for NOT SCORED.

    ``None`` is the default because most paths produce no score: the model has
    to be asked for one and has to return it (GitHub #673). Callers that can
    genuinely assert the class — a document-name regex match, a single-class
    configuration, a page-content regex match — pass ``1.0`` explicitly, and an
    errored page carries ``0.0``. See :attr:`idp_common.models.Page.confidence`
    for why a defaulted ``1.0`` was removed."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Additional metadata for the classification."""


@dataclass
class PageClassification:
    """Classification result for a single page."""

    page_id: str
    """The ID of the page."""

    classification: DocumentClassification
    """The classification result for the page."""

    image_uri: Optional[str] = None
    """URI of the page image, if available."""

    text_uri: Optional[str] = None
    """URI of the page text, if available."""

    raw_text_uri: Optional[str] = None
    """URI of the raw text, if available."""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "page_id": self.page_id,
            "class": self.classification.doc_type,
            "confidence": self.classification.confidence,
            "imageUri": self.image_uri,
            "parsedTextUri": self.text_uri,
            "rawTextUri": self.raw_text_uri,
            **self.classification.metadata,
        }


@dataclass
class DocumentSection:
    """A section of consecutive pages with the same classification."""

    section_id: str
    """The ID of the section."""

    classification: DocumentClassification
    """The classification result for the section."""

    pages: List[PageClassification]
    """The pages in the section."""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "id": self.section_id,
            "class": self.classification.doc_type,
            "pages": [page.to_dict() for page in self.pages],
        }


@dataclass
class Segment:
    """One contiguous run of pages forming a single document instance.

    **The intermediate result every classification path produces**, and the only
    thing section forming reads. Before this existed, each path built
    ``document.sections`` itself: the page-level path folded per-page
    ``start``/``continue`` verdicts, the packet path turned the model's segment
    list into sections, and each had its own copy of all three
    ``sectionSplitting`` strategies — six implementations of three behaviours,
    which is how the two paths came to disagree about what
    ``sectionSplitting: disabled`` means.

    ``doc_class`` is **optional on purpose**. A segmentation-only stage (the
    first half of two-stage classification) emits segments with no class, and a
    labeling stage fills them in. Nothing downstream of section forming needs to
    know which produced it.

    Page identity is carried as ``page_ids``, not as an integer range, because
    page IDs are strings throughout the pipeline and a packet's pages are not
    guaranteed to be a contiguous integer run — ``maxPagesForClassification``
    and windowing both produce documents whose page set is a subset.
    ``start_page``/``end_page`` are reads over it, matching the names the
    configuration and documentation use.
    """

    page_ids: List[str]
    """Ordered page IDs in this segment. Never empty."""

    doc_class: Optional[str] = None
    """The segment's document class, or None when it has not been labeled yet."""

    confidence: Optional[float] = None
    """Confidence in ``doc_class``, or None for not scored."""

    candidates: List[Dict[str, Any]] = field(default_factory=list)
    """Ranked runner-up classes with probabilities, when the model reported them.

    Carried on the segment rather than being dropped at the parse, because
    ``confidence.mode: topk`` *asks* for them and they are the thing that makes
    the score interpretable — "80% W-2, 15% 1099" says what else the pages could
    have been, which a bare number does not. The page-level path has persisted
    these onto ``Page.classification_candidates`` since GitHub #673; a
    packet-level call asks for them per segment and must not be the path that
    buys the output tokens and discards the answer.
    """

    @property
    def start_page(self) -> str:
        """First page ID in the segment."""
        return self.page_ids[0]

    @property
    def end_page(self) -> str:
        """Last page ID in the segment."""
        return self.page_ids[-1]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "start_page": self.start_page,
            "end_page": self.end_page,
            "page_ids": list(self.page_ids),
            "class": self.doc_class,
            "confidence": self.confidence,
            "candidates": list(self.candidates),
        }


@dataclass
class ClassificationResult:
    """Result of a document classification operation."""

    metadata: Dict[str, Any]
    """Metadata about the classification operation."""

    sections: List[DocumentSection]
    """Sections of classified pages."""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation for API responses."""
        return {
            "metadata": self.metadata,
            "sections": [section.to_dict() for section in self.sections],
        }
