# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Classification confidence: parsing, aggregation, and persistence (GitHub #673).

Covers the three properties the feature rests on:

1. A confidence the model reports is KEPT (it used to be discarded, while
   `docs/classification.md` documented it as supported).
2. An absent confidence reads as NOT SCORED (``None``) everywhere, never as a
   fabricated 1.0 — including through serialization and DynamoDB.
3. A section's score is the MINIMUM of its pages, and unscored if any page is.
"""

from decimal import Decimal
from unittest.mock import Mock

import pytest

from idp_common.classification.models import DocumentClassification, PageClassification
from idp_common.classification.service import (
    aggregate_page_confidence,
    parse_confidence,
)
from idp_common.dynamodb.service import (
    DocumentDynamoDBService,
    serialize_page_classification_signals,
)
from idp_common.models import Document, Page, Section


@pytest.mark.unit
class TestParseConfidence:
    """The tolerant parse of whatever the model actually wrote."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (0.85, 0.85),
            (0, 0.0),
            (1, 1.0),  # a bare 1 in a 0-1 field means certainty, not 1%
            ("0.85", 0.85),
            (95, 0.95),  # percentage, which models emit routinely
            ("95", 0.95),
            ("95%", 0.95),
            (" 0.5 ", 0.5),
            (100, 1.0),
        ],
    )
    def test_parses_usable_values(self, raw, expected):
        assert parse_confidence(raw) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "   ",
            "high",  # verbalized, not numeric
            -0.5,
            101,
            float("nan"),
            float("inf"),
            True,  # a bool is not a confidence
            [0.9],
            {"value": 0.9},
        ],
    )
    def test_unusable_values_are_not_scored_rather_than_errors(self, raw):
        """A malformed confidence must not fail an otherwise-good classification."""
        assert parse_confidence(raw) is None


@pytest.mark.unit
class TestAggregatePageConfidence:
    """Section confidence = min over pages, with None absorbing."""

    def test_minimum_wins(self):
        assert aggregate_page_confidence([0.9, 0.4, 0.8]) == 0.4

    def test_single_page(self):
        assert aggregate_page_confidence([0.7]) == 0.7

    def test_any_unscored_page_makes_the_section_unscored(self):
        # Reporting min(0.9) would present a partial aggregate as a whole-section
        # number, so the section is honestly unscored instead.
        assert aggregate_page_confidence([0.9, None]) is None

    def test_empty_is_unscored(self):
        assert aggregate_page_confidence([]) is None

    def test_zero_is_a_score_not_an_absence(self):
        assert aggregate_page_confidence([0.0, 0.9]) == 0.0


@pytest.mark.unit
class TestPagePersistence:
    """Round-tripping through Document.to_dict / from_dict."""

    def test_confidence_and_reason_round_trip(self):
        doc = Document(id="doc", input_key="doc.pdf")
        doc.pages["1"] = Page(
            page_id="1",
            classification="w2",
            confidence=0.83,
            classification_reason="Box 1 wage labels present",
        )
        payload = doc.to_dict()

        assert payload["pages"]["1"]["confidence"] == 0.83
        assert payload["pages"]["1"]["classification_reason"] == (
            "Box 1 wage labels present"
        )

        restored = Document.from_dict(payload)
        assert restored.pages["1"].confidence == 0.83
        assert restored.pages["1"].classification_reason == (
            "Box 1 wage labels present"
        )

    def test_unscored_page_omits_the_keys_entirely(self):
        """Absence is serialized as absence, so old payloads are unchanged."""
        doc = Document(id="doc", input_key="doc.pdf")
        doc.pages["1"] = Page(page_id="1", classification="w2")
        payload = doc.to_dict()

        assert "confidence" not in payload["pages"]["1"]
        assert "classification_reason" not in payload["pages"]["1"]
        assert Document.from_dict(payload).pages["1"].confidence is None

    def test_legacy_payload_without_confidence_reads_as_unscored(self):
        """A page dict written before #673 must not become a presumed 0.0/1.0."""
        restored = Document.from_dict(
            {
                "id": "doc",
                "input_key": "doc.pdf",
                "pages": {"1": {"page_id": "1", "classification": "w2"}},
                "sections": [
                    {"section_id": "1", "classification": "w2", "page_ids": ["1"]}
                ],
            }
        )
        assert restored.pages["1"].confidence is None
        assert restored.sections[0].confidence is None

    def test_section_confidence_round_trips_and_omits_when_unscored(self):
        doc = Document(id="doc", input_key="doc.pdf")
        doc.sections = [
            Section(
                section_id="1", classification="w2", confidence=0.42, page_ids=["1"]
            ),
            Section(section_id="2", classification="w2", page_ids=["2"]),
        ]
        payload = doc.to_dict()

        assert payload["sections"][0]["confidence"] == 0.42
        assert "confidence" not in payload["sections"][1]

        restored = Document.from_dict(payload)
        assert restored.sections[0].confidence == 0.42
        assert restored.sections[1].confidence is None


@pytest.mark.unit
class TestDynamoDBPersistence:
    """The page/section attributes the API and UI read."""

    def test_signals_serialized_as_decimal(self):
        """DynamoDB rejects floats, so the confidence must be a Decimal."""
        page = Page(
            page_id="1",
            classification="w2",
            confidence=0.83,
            classification_reason="wage boxes",
            document_boundary="start",
        )
        signals = serialize_page_classification_signals(page)

        assert signals["ClassConfidence"] == Decimal("0.83")
        assert isinstance(signals["ClassConfidence"], Decimal)
        assert signals["ClassReason"] == "wage boxes"
        assert signals["Boundary"] == "start"

    def test_unscored_page_adds_no_attributes(self):
        signals = serialize_page_classification_signals(
            Page(page_id="1", classification="w2")
        )
        assert signals == {}

    def test_zero_confidence_is_persisted(self):
        """0.0 is an assertion (an errored page), not an absence."""
        signals = serialize_page_classification_signals(
            Page(page_id="1", classification="unclassified", confidence=0.0)
        )
        assert signals["ClassConfidence"] == Decimal("0.0")

    def test_candidates_round_trip_through_dynamodb(self):
        """Ranked alternatives are stored as maps, with Decimal probabilities."""
        page = Page(
            page_id="1",
            classification="w2",
            confidence=0.8,
            classification_candidates=[
                {"class": "w2", "probability": 0.8},
                {"class": "1099", "probability": 0.15},
            ],
        )
        signals = serialize_page_classification_signals(page)

        assert signals["ClassCandidates"] == [
            {"Class": "w2", "Probability": Decimal("0.8")},
            {"Class": "1099", "Probability": Decimal("0.15")},
        ]

        service = DocumentDynamoDBService(dynamodb_client=Mock())
        doc = service._dynamodb_item_to_document(
            {
                "PK": "doc#a.pdf",
                "SK": "none",
                "ObjectKey": "a.pdf",
                "ObjectStatus": "COMPLETED",
                "Pages": [{"Id": 1, "Class": "w2", **signals}],
            }
        )
        assert doc.pages["1"].classification_candidates == [
            {"class": "w2", "probability": 0.8},
            {"class": "1099", "probability": 0.15},
        ]

    def test_page_without_candidates_stores_and_reads_none(self):
        signals = serialize_page_classification_signals(
            Page(page_id="1", classification="w2", confidence=0.8)
        )
        assert "ClassCandidates" not in signals

        service = DocumentDynamoDBService(dynamodb_client=Mock())
        doc = service._dynamodb_item_to_document(
            {
                "PK": "doc#a.pdf",
                "SK": "none",
                "ObjectKey": "a.pdf",
                "ObjectStatus": "COMPLETED",
                "Pages": [{"Id": 1, "Class": "w2"}],
            }
        )
        assert doc.pages["1"].classification_candidates is None

    def test_item_to_document_reads_confidence_back(self):
        service = DocumentDynamoDBService(dynamodb_client=Mock())
        doc = service._dynamodb_item_to_document(
            {
                "PK": "doc#a.pdf",
                "SK": "none",
                "ObjectKey": "a.pdf",
                "ObjectStatus": "COMPLETED",
                "Pages": [
                    {
                        "Id": 1,
                        "Class": "w2",
                        "ClassConfidence": Decimal("0.77"),
                        "ClassReason": "wage boxes",
                    },
                    {"Id": 2, "Class": "w2"},
                ],
                "Sections": [
                    {
                        "Id": "1",
                        "PageIds": [1, 2],
                        "Class": "w2",
                        "Confidence": Decimal("0.77"),
                    },
                    {"Id": "2", "PageIds": [], "Class": "w2"},
                ],
            }
        )

        assert doc.pages["1"].confidence == 0.77
        assert doc.pages["1"].classification_reason == "wage boxes"
        # Absent attribute => not scored, on both shapes.
        assert doc.pages["2"].confidence is None
        assert doc.pages["2"].classification_reason is None
        assert doc.sections[0].confidence == 0.77
        assert doc.sections[1].confidence is None

    def test_section_update_preserves_class_confidence(self):
        """`update_document_section` replaces the whole section map.

        Extraction calls it per section, so omitting Confidence would ERASE what
        classification stored moments earlier — the same trap ProcessingIssues
        and the exclusion flags already document.
        """
        mock_client = Mock()
        mock_client.update_item.return_value = {}
        service = DocumentDynamoDBService(dynamodb_client=mock_client)

        service.update_document_section(
            document_id="a.pdf",
            section_index=0,
            section=Section(
                section_id="1",
                classification="w2",
                confidence=0.61,
                page_ids=["1"],
            ),
        )

        written = mock_client.update_item.call_args.kwargs[
            "expression_attribute_values"
        ][":section"]
        assert written["Confidence"] == Decimal("0.61")


def _config(section_splitting="llm_determined"):
    """Minimal two-class classification config."""
    return {
        "classes": [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "invoice",
                "x-aws-idp-document-type": "invoice",
                "type": "object",
                "description": "An invoice",
                "properties": {},
            },
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "receipt",
                "x-aws-idp-document-type": "receipt",
                "type": "object",
                "description": "A receipt",
                "properties": {},
            },
        ],
        "classification": {
            "model": "anthropic.claude-3-sonnet-20240229-v1:0",
            "system_prompt": "classify",
            "task_prompt": "{CLASS_NAMES_AND_DESCRIPTIONS}{DOCUMENT_TEXT}",
            "classificationMethod": "multimodalPageLevelClassification",
            "sectionSplitting": section_splitting,
        },
    }


def _document(page_count):
    from idp_common.models import Status

    doc = Document(id="packet.pdf", input_key="packet.pdf", status=Status.CLASSIFYING)
    for page_id in (str(i) for i in range(1, page_count + 1)):
        doc.pages[page_id] = Page(
            page_id=page_id,
            image_uri=f"s3://b/image{page_id}.jpg",
            parsed_text_uri=f"s3://b/text{page_id}.txt",
        )
    return doc


def _service(config):
    from unittest.mock import patch

    from idp_common.classification.service import ClassificationService

    with patch("boto3.Session"):
        return ClassificationService(
            region="us-west-2", config=config, backend="bedrock"
        )


def _classify_document_with(service, doc, confidence_by_page, boundary="continue"):
    """Run classify_document with per-page confidences supplied by the caller."""
    from unittest.mock import patch

    def fake_classify_page(page_id, *args, **kwargs):
        return PageClassification(
            page_id=page_id,
            classification=DocumentClassification(
                doc_type="invoice",
                confidence=confidence_by_page[page_id],
                metadata={"document_boundary": boundary},
            ),
        )

    with patch.object(service, "classify_page", side_effect=fake_classify_page):
        return service.classify_document(doc)


@pytest.mark.unit
class TestSectionAggregationEndToEnd:
    """Section confidence as produced by a real classify_document run."""

    def test_llm_determined_section_takes_its_weakest_page(self):
        service = _service(_config("llm_determined"))
        result = _classify_document_with(
            service, _document(3), {"1": 0.9, "2": 0.35, "3": 0.8}
        )

        assert len(result.sections) == 1  # all "continue" -> one section
        assert result.sections[0].confidence == 0.35
        assert [result.pages[p].confidence for p in ("1", "2", "3")] == [0.9, 0.35, 0.8]

    def test_one_unscored_page_leaves_the_section_unscored(self):
        service = _service(_config("llm_determined"))
        result = _classify_document_with(
            service, _document(3), {"1": 0.9, "2": None, "3": 0.8}
        )

        assert result.sections[0].confidence is None

    def test_per_page_sections_carry_their_own_page_score(self):
        service = _service(_config("page"))
        result = _classify_document_with(
            service, _document(3), {"1": 0.9, "2": 0.35, "3": None}
        )

        assert [s.confidence for s in result.sections] == [0.9, 0.35, None]

    def test_boundaries_split_the_aggregate_per_section(self):
        service = _service(_config("llm_determined"))
        # Every page starts a new document, so each section is its own page.
        result = _classify_document_with(
            service, _document(3), {"1": 0.9, "2": 0.35, "3": 0.8}, boundary="start"
        )

        assert [s.confidence for s in result.sections] == [0.9, 0.35, 0.8]

    def test_disabled_voting_keeps_the_winners_scores_and_drops_the_losers(self):
        """`sectionSplitting: disabled` forces every page to the voted class.

        A page that predicted something else now carries a class it did not
        predict, so its score (which was about a different class) cannot be
        reported for this one — it becomes unscored, which makes the section
        unscored too. Every page used to be rewritten to 1.0, asserting
        certainty precisely where the pages disagreed.
        """
        from unittest.mock import patch

        service = _service(_config("disabled"))
        doc = _document(3)
        classes = {"1": "invoice", "2": "invoice", "3": "receipt"}
        confidences = {"1": 0.9, "2": 0.6, "3": 0.95}

        def fake_classify_page(page_id, *args, **kwargs):
            return PageClassification(
                page_id=page_id,
                classification=DocumentClassification(
                    doc_type=classes[page_id],
                    confidence=confidences[page_id],
                    metadata={"document_boundary": "continue"},
                ),
            )

        with patch.object(service, "classify_page", side_effect=fake_classify_page):
            result = service.classify_document(doc)

        assert len(result.sections) == 1
        assert result.sections[0].classification == "invoice"  # 2 votes vs 1
        assert result.pages["1"].confidence == 0.9
        assert result.pages["2"].confidence == 0.6
        # Voted "receipt", stored as "invoice": its 0.95 does not describe that.
        assert result.pages["3"].confidence is None
        assert result.sections[0].confidence is None

    def test_unscored_run_writes_no_section_confidence_at_all(self):
        """The default today: no prompt asks for a score, so nothing is invented."""
        service = _service(_config("llm_determined"))
        result = _classify_document_with(service, _document(2), {"1": None, "2": None})

        assert result.sections[0].confidence is None
        assert "confidence" not in result.to_dict()["sections"][0]


@pytest.mark.unit
class TestApplyPageResult:
    """The shared copy of a page result onto the declared Page fields."""

    def _page_result(self, **metadata):
        return PageClassification(
            page_id="1",
            classification=DocumentClassification(
                doc_type="w2", confidence=0.66, metadata=metadata
            ),
        )

    def test_copies_confidence_reason_and_boundary(self):
        from idp_common.classification.service import ClassificationService

        page = Page(page_id="1")
        ClassificationService._apply_page_result(
            page,
            self._page_result(
                classification_reason="wage boxes", document_boundary="Start"
            ),
        )

        assert page.classification == "w2"
        assert page.confidence == 0.66
        assert page.classification_reason == "wage boxes"
        # Normalized to lower case, as the boundary consumers expect.
        assert page.document_boundary == "start"

    def test_cache_hit_and_fresh_result_produce_the_same_page(self):
        """A cache hit used to drop document_boundary entirely."""
        from idp_common.classification.service import ClassificationService

        result = self._page_result(
            classification_reason="wage boxes", document_boundary="start"
        )
        fresh, cached = Page(page_id="1"), Page(page_id="1")
        ClassificationService._apply_page_result(fresh, result)
        ClassificationService._apply_page_result(cached, result)

        assert fresh == cached
        assert cached.document_boundary == "start"
