# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The segments contract between classification and section forming (#1328).

Every classification path — page-level, packet-level, and the zero-inference
short-circuits — now produces a list of
:class:`~idp_common.classification.models.Segment` and one section builder reads
it. Before that there were six implementations of three ``sectionSplitting``
strategies, one set per granularity, and they had drifted apart.

The tests here pin two things that a refactor of this shape can lose silently:

- **The per-page score survives ``sectionSplitting: page``.** A segment's score
  is the *minimum* across its pages, so building per-page sections from segments
  alone reports every page of a run as being as uncertain as its worst page —
  in the one strategy whose entire purpose is per-page granularity. The builder
  is therefore handed the per-page verdicts explicitly, and a regression here is
  invisible in section *structure*, which is what most tests check.
- **The zero-inference paths keep their asserted confidence.** A single-class
  configuration and a document-name-regex match both synthesize page results at
  confidence 1.0 without writing them onto the pages first, so a builder that
  reads page state back off the document sees "no prediction" and would discard
  the 1.0 as disagreement.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from idp_common.classification.models import (
    DocumentClassification,
    PageClassification,
    Segment,
)
from idp_common.classification.service import ClassificationService
from idp_common.models import Document, Page


def _classes(*names: str) -> List[Dict[str, Any]]:
    return [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": name,
            "x-aws-idp-document-type": name,
            "type": "object",
            "description": f"A {name}",
            "properties": {},
        }
        for name in names
    ]


def _config(
    *,
    classes: Optional[List[str]] = None,
    section_splitting: str = "llm_determined",
    **classification: Any,
) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "model": "us.amazon.nova-2-lite-v1:0",
        "system_prompt": "You classify documents.",
        "task_prompt": (
            "{CLASS_NAMES_AND_DESCRIPTIONS}\n"
            "<document-ocr-data>\n{DOCUMENT_TEXT}\n</document-ocr-data>\n"
            "<document-image>\n{DOCUMENT_IMAGE}\n</document-image>\n"
        ),
        "sectionSplitting": section_splitting,
        "confidence": {"mode": "off"},
    }
    base.update(classification)
    return {
        "classes": _classes(*(classes or ["invoice", "receipt", "letter"])),
        "classification": base,
    }


def _service(**kwargs: Any) -> ClassificationService:
    return ClassificationService(region="us-west-2", config=_config(**kwargs))


def _document(page_count: int) -> Document:
    return Document(
        id="doc-1",
        input_key="doc.pdf",
        pages={
            str(i): Page(page_id=str(i), image_uri=f"s3://b/{i}.jpg")
            for i in range(1, page_count + 1)
        },
    )


def _results(*spec: tuple[str, str, str, Optional[float]]) -> List[PageClassification]:
    """Build page results from ``(page_id, class, boundary, confidence)`` tuples."""
    return [
        PageClassification(
            page_id=page_id,
            classification=DocumentClassification(
                doc_type=doc_type,
                confidence=confidence,
                metadata={"document_boundary": boundary},
            ),
        )
        for page_id, doc_type, boundary, confidence in spec
    ]


# --------------------------------------------------------------------------
# The Segment value type
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestSegment:
    def test_start_and_end_page_read_the_page_list(self):
        segment = Segment(page_ids=["3", "4", "5"], doc_class="invoice")
        assert segment.start_page == "3"
        assert segment.end_page == "5"

    def test_a_single_page_segment_starts_and_ends_on_it(self):
        segment = Segment(page_ids=["7"])
        assert segment.start_page == segment.end_page == "7"

    def test_the_class_is_optional_so_a_splitting_only_stage_needs_no_rework(
        self,
    ):
        """The whole reason ``doc_class`` is Optional — the split-then-classify
        splitting call emits segments with no class at all, and section
        classification fills them in."""
        segment = Segment(page_ids=["1", "2"])
        assert segment.doc_class is None
        assert segment.confidence is None

    def test_to_dict_uses_the_names_the_configuration_and_docs_use(self):
        segment = Segment(page_ids=["1", "2"], doc_class="invoice", confidence=0.8)
        assert segment.to_dict() == {
            "start_page": "1",
            "end_page": "2",
            "page_ids": ["1", "2"],
            "class": "invoice",
            "confidence": 0.8,
            "candidates": [],
        }


# --------------------------------------------------------------------------
# Folding per-page verdicts
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestFoldPageVerdicts:
    def test_consecutive_same_class_continue_pages_form_one_segment(self):
        service = _service()
        segments = service._fold_page_verdicts_to_segments(
            _results(
                ("1", "invoice", "start", 0.9),
                ("2", "invoice", "continue", 0.8),
                ("3", "invoice", "continue", 0.95),
            )
        )
        assert len(segments) == 1
        assert segments[0].page_ids == ["1", "2", "3"]
        assert segments[0].doc_class == "invoice"
        # The segment's score is the weakest page in it.
        assert segments[0].confidence == 0.8

    def test_a_start_splits_two_documents_of_the_same_class(self):
        """The case page-level classification exists to catch: two copies of the
        same form filed back to back are two documents, not one."""
        service = _service()
        segments = service._fold_page_verdicts_to_segments(
            _results(
                ("1", "invoice", "start", None),
                ("2", "invoice", "continue", None),
                ("3", "invoice", "start", None),
            )
        )
        assert [s.page_ids for s in segments] == [["1", "2"], ["3"]]

    def test_a_class_change_splits_without_a_start(self):
        service = _service()
        segments = service._fold_page_verdicts_to_segments(
            _results(
                ("1", "invoice", "continue", None),
                ("2", "receipt", "continue", None),
            )
        )
        assert [s.doc_class for s in segments] == ["invoice", "receipt"]

    def test_an_absent_boundary_key_defaults_to_continue(self):
        service = _service()
        results = [
            PageClassification(
                page_id=str(i),
                classification=DocumentClassification(doc_type="invoice", metadata={}),
            )
            for i in (1, 2)
        ]
        segments = service._fold_page_verdicts_to_segments(results)
        assert len(segments) == 1

    def test_a_non_string_boundary_does_not_raise(self):
        """The value comes from model output. A model that answered with a
        number used to take down the whole fold with an AttributeError."""
        service = _service()
        results = [
            PageClassification(
                page_id="1",
                classification=DocumentClassification(
                    doc_type="invoice", metadata={"document_boundary": "start"}
                ),
            ),
            PageClassification(
                page_id="2",
                classification=DocumentClassification(
                    doc_type="invoice", metadata={"document_boundary": 1}
                ),
            ),
        ]
        segments = service._fold_page_verdicts_to_segments(results)
        assert len(segments) == 1

    def test_pages_are_sorted_numerically_before_folding(self):
        service = _service()
        segments = service._fold_page_verdicts_to_segments(
            _results(
                ("10", "invoice", "continue", None),
                ("2", "invoice", "continue", None),
                ("1", "invoice", "start", None),
            )
        )
        assert segments[0].page_ids == ["1", "2", "10"]

    def test_no_results_yields_no_segments(self):
        assert _service()._fold_page_verdicts_to_segments([]) == []


# --------------------------------------------------------------------------
# Section forming — the three strategies, both granularities
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestSectionsFromSegments:
    def test_llm_determined_makes_one_section_per_segment(self):
        service = _service(section_splitting="llm_determined")
        document = _document(3)
        segments = [
            Segment(page_ids=["1", "2"], doc_class="invoice", confidence=0.8),
            Segment(page_ids=["3"], doc_class="receipt", confidence=0.7),
        ]
        document = service._sections_from_segments(document, segments)

        assert [s.section_id for s in document.sections] == ["1", "2"]
        assert [s.page_ids for s in document.sections] == [["1", "2"], ["3"]]
        assert [s.confidence for s in document.sections] == [0.8, 0.7]
        # Pages agree with the section they ended up in.
        assert document.pages["2"].classification == "invoice"
        assert document.pages["3"].classification == "receipt"

    def test_page_strategy_preserves_each_pages_own_score(self):
        """⚠️ The regression the refactor nearly shipped. Two pages of the same
        class fold into ONE segment whose score is their minimum; splitting that
        segment per page must not stamp the minimum onto both."""
        service = _service(section_splitting="page")
        document = _document(3)
        results = _results(
            ("1", "invoice", "start", 0.95),
            ("2", "invoice", "continue", 0.90),
            ("3", "receipt", "start", 0.85),
        )
        document = service._apply_section_splitting_strategy(document, results)

        assert len(document.sections) == 3
        assert [s.confidence for s in document.sections] == [0.95, 0.90, 0.85]
        assert [s.classification for s in document.sections] == [
            "invoice",
            "invoice",
            "receipt",
        ]

    def test_page_strategy_without_per_page_verdicts_uses_the_segment_score(self):
        """A packet-level call has no per-page signal, so every page of a
        segment legitimately shares the segment's score."""
        service = _service(section_splitting="page")
        document = _document(2)
        document = service._sections_from_segments(
            document,
            [Segment(page_ids=["1", "2"], doc_class="invoice", confidence=0.6)],
        )
        assert [s.confidence for s in document.sections] == [0.6, 0.6]

    def test_disabled_votes_across_pages_weighted_by_segment_length(self):
        service = _service(section_splitting="disabled")
        document = _document(3)
        results = _results(
            ("1", "invoice", "start", 0.9),
            ("2", "invoice", "continue", 0.8),
            ("3", "receipt", "start", 0.7),
        )
        document = service._apply_section_splitting_strategy(document, results)

        assert len(document.sections) == 1
        assert document.sections[0].classification == "invoice"
        assert document.sections[0].page_ids == ["1", "2", "3"]
        # Page 3 predicted something else, so its score says nothing about the
        # class it has now been given.
        assert document.pages["3"].confidence is None
        # And an unscored page absorbs the section's aggregate.
        assert document.sections[0].confidence is None

    def test_disabled_breaks_a_tie_on_the_earliest_segment(self):
        service = _service(section_splitting="disabled")
        document = _document(2)
        results = _results(
            ("1", "receipt", "start", None),
            ("2", "invoice", "start", None),
        )
        document = service._apply_section_splitting_strategy(document, results)
        assert document.sections[0].classification == "receipt"

    def test_disabled_ignores_classes_outside_the_vocabulary(self):
        """An unclassifiable page must not win the vote for the whole file."""
        service = _service(section_splitting="disabled")
        document = _document(3)
        results = _results(
            ("1", "unclassified", "start", None),
            ("2", "unclassified", "continue", None),
            ("3", "invoice", "start", 0.9),
        )
        document = service._apply_section_splitting_strategy(document, results)
        assert document.sections[0].classification == "invoice"

    def test_disabled_falls_back_to_the_first_segment_when_nothing_is_votable(self):
        service = _service(section_splitting="disabled")
        document = _document(2)
        results = _results(
            ("1", "error (backoff/retry)", "start", None),
            ("2", "error (backoff/retry)", "continue", None),
        )
        document = service._apply_section_splitting_strategy(document, results)
        assert document.sections[0].classification == "error (backoff/retry)"

    def test_a_segment_covering_no_page_of_the_document_is_skipped(self):
        """A model that invents page 40 of a 12-page packet must not take the
        other segments down with it."""
        service = _service(section_splitting="llm_determined")
        document = _document(2)
        document = service._sections_from_segments(
            document,
            [
                Segment(page_ids=["1", "2"], doc_class="invoice"),
                Segment(page_ids=["40"], doc_class="receipt"),
            ],
        )
        assert len(document.sections) == 1
        assert document.sections[0].page_ids == ["1", "2"]

    def test_section_ids_stay_dense_when_a_segment_is_skipped(self):
        service = _service(section_splitting="llm_determined")
        document = _document(2)
        document = service._sections_from_segments(
            document,
            [
                Segment(page_ids=["99"], doc_class="receipt"),
                Segment(page_ids=["1", "2"], doc_class="invoice"),
            ],
        )
        assert [s.section_id for s in document.sections] == ["1"]

    def test_an_overlapping_page_produces_exactly_one_per_page_section(self):
        """Windowed packet classification deliberately overlaps adjacent
        windows, so the same page can appear in two windows' segments."""
        service = _service(section_splitting="page")
        document = _document(3)
        document = service._sections_from_segments(
            document,
            [
                Segment(page_ids=["1", "2"], doc_class="invoice"),
                Segment(page_ids=["2", "3"], doc_class="invoice"),
            ],
        )
        assert [s.page_ids for s in document.sections] == [["1"], ["2"], ["3"]]

    def test_no_segments_leaves_sections_untouched(self):
        service = _service()
        document = _document(1)
        document.sections = []
        assert service._sections_from_segments(document, []).sections == []


# --------------------------------------------------------------------------
# The zero-inference short-circuits
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestZeroInferencePaths:
    """A single-class configuration and a document-name-regex match assert the
    class with no model call. Both synthesize page results rather than writing
    them onto the pages, so a builder that reads page state back off the
    document would see "no prediction" and discard the asserted 1.0."""

    @pytest.mark.parametrize("strategy", ["disabled", "page", "llm_determined"])
    def test_a_single_class_configuration_keeps_its_asserted_confidence(self, strategy):
        service = _service(classes=["invoice"], section_splitting=strategy)
        document = _document(1 if strategy == "llm_determined" else 3)
        document = service._create_single_class_sections(document)

        assert document.sections
        for section in document.sections:
            assert section.classification == "invoice"
            assert section.confidence == 1.0
        for page in document.pages.values():
            assert page.classification == "invoice"
            assert page.confidence == 1.0

    def test_disabled_gives_one_section_over_every_page(self):
        service = _service(classes=["invoice"], section_splitting="disabled")
        document = service._create_single_class_sections(_document(4))
        assert len(document.sections) == 1
        assert document.sections[0].page_ids == ["1", "2", "3", "4"]

    def test_page_gives_one_section_per_page(self):
        service = _service(classes=["invoice"], section_splitting="page")
        document = service._create_single_class_sections(_document(4))
        assert [s.page_ids for s in document.sections] == [
            ["1"],
            ["2"],
            ["3"],
            ["4"],
        ]

    def test_a_regex_matched_class_is_used_rather_than_the_sole_class(self):
        service = _service(section_splitting="disabled")
        document = service._create_single_class_sections(
            _document(2), class_name="receipt"
        )
        assert document.sections[0].classification == "receipt"
        assert document.sections[0].confidence == 1.0


# --------------------------------------------------------------------------
# Parsing a packet call's segment array
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestParseModelSegments:
    def test_ordinals_resolve_to_page_ids(self):
        service = _service()
        document = _document(4)
        segments = service._parse_model_segments(
            [
                {"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "invoice"},
                {"ordinal_start_page": 3, "ordinal_end_page": 4, "type": "receipt"},
            ],
            document,
        )
        assert [s.page_ids for s in segments] == [["1", "2"], ["3", "4"]]

    def test_a_window_maps_ordinals_positionally_onto_its_own_pages(self):
        """A windowed packet call sees a slice, so its ordinal 1 is not the
        document's page 1. The mapping is positional rather than an arithmetic
        offset, because a window's pages are not guaranteed to be a contiguous
        integer run — maxPagesForClassification already produces documents whose
        page set is a subset."""
        service = _service()
        document = _document(10)
        segments = service._parse_model_segments(
            [{"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "invoice"}],
            document,
            window_page_ids=["6", "7", "8", "9", "10"],
        )
        assert segments[0].page_ids == ["6", "7"]

    def test_a_window_with_non_contiguous_pages_still_maps_correctly(self):
        service = _service()
        document = _document(10)
        segments = service._parse_model_segments(
            [{"ordinal_start_page": 2, "ordinal_end_page": 3, "type": "invoice"}],
            document,
            window_page_ids=["1", "4", "7"],
        )
        assert segments[0].page_ids == ["4", "7"]

    def test_an_ordinal_past_the_end_of_the_window_is_dropped(self):
        service = _service()
        segments = service._parse_model_segments(
            [{"ordinal_start_page": 1, "ordinal_end_page": 9, "type": "invoice"}],
            _document(10),
            window_page_ids=["1", "2"],
        )
        # Only the two ordinals the window actually held resolve.
        assert segments[0].page_ids == ["1", "2"]

    def test_string_ordinals_are_accepted(self):
        service = _service()
        segments = service._parse_model_segments(
            [{"ordinal_start_page": "1", "ordinal_end_page": "2", "type": "invoice"}],
            _document(2),
        )
        assert segments[0].page_ids == ["1", "2"]

    @pytest.mark.parametrize(
        "segment",
        [
            {"ordinal_end_page": 2, "type": "invoice"},
            {"ordinal_start_page": 1, "type": "invoice"},
            {"ordinal_start_page": 1, "ordinal_end_page": 2},
            {"ordinal_start_page": "x", "ordinal_end_page": 2, "type": "invoice"},
            {"ordinal_start_page": 3, "ordinal_end_page": 1, "type": "invoice"},
            {"ordinal_start_page": 40, "ordinal_end_page": 41, "type": "invoice"},
            "not an object",
        ],
    )
    def test_a_malformed_segment_is_skipped_not_fatal(self, segment):
        service = _service()
        segments = service._parse_model_segments(
            [
                segment,
                {"ordinal_start_page": 1, "ordinal_end_page": 1, "type": "receipt"},
            ],
            _document(2),
        )
        assert [s.doc_class for s in segments] == ["receipt"]

    def test_an_unknown_class_is_kept_rather_than_dropped(self):
        """Long-standing behaviour: warn and use it anyway. Dropping the segment
        would lose its boundary too."""
        service = _service()
        segments = service._parse_model_segments(
            [{"ordinal_start_page": 1, "ordinal_end_page": 1, "type": "mystery"}],
            _document(1),
        )
        assert segments[0].doc_class == "mystery"

    def test_a_reported_confidence_is_parsed(self):
        service = _service()
        segments = service._parse_model_segments(
            [
                {
                    "ordinal_start_page": 1,
                    "ordinal_end_page": 1,
                    "type": "invoice",
                    "confidence": 0.75,
                }
            ],
            _document(1),
        )
        assert segments[0].confidence == 0.75

    def test_a_forced_class_leaves_the_segment_unscored(self):
        """The operator's regex match asserts the class, so the model's number
        does not describe it."""
        service = _service()
        segments = service._parse_model_segments(
            [
                {
                    "ordinal_start_page": 1,
                    "ordinal_end_page": 1,
                    "type": "invoice",
                    "confidence": 0.75,
                }
            ],
            _document(1),
            forced_class="invoice",
        )
        assert segments[0].confidence is None
