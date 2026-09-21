# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Search operations.

These build the real ``SearchResult`` from a stubbed knowledge-base response
rather than asserting on a mock's call arguments. That distinction is the point
of the module: ``query()`` used to construct ``SearchCitation(document=...)`` and
``SearchResult(...)`` with keywords neither dataclass declared, so every call
raised ``TypeError`` — and because the whole body sits inside
``except Exception: raise IDPProcessingError(...)``, the caller saw a plausible
"Failed to query knowledge base" instead of a signature bug. A test that only
checked ``processor.query`` was called with the right arguments would still pass
against that.
"""

from unittest.mock import Mock, patch

import pytest

from idp_sdk import IDPClient
from idp_sdk.exceptions import IDPProcessingError
from idp_sdk.models import SearchCitation, SearchDocumentReference, SearchResult

#: What SearchProcessor.query returns for a question with one grounded answer.
#: Mirrors the shape the processor builds from the knowledge-base Lambda payload
#: (see idp_sdk._core.search_processor.SearchProcessor.query).
STUB_RESPONSE = {
    "question": "What is the total amount on invoice INV-12345?",
    "results": [
        {
            "answer": "The total amount is $4,821.50.",
            "confidence": 0.93,
            "citations": [
                {
                    "document_id": "batch-001/invoice1.pdf",
                    "section_id": 2,
                    "page": 3,
                    "text": "Total due: $4,821.50",
                    "confidence": 0.88,
                },
                {
                    # A citation the knowledge base could not localise: every
                    # optional field absent, which must not raise.
                    "text": "Amounts are stated in USD.",
                },
            ],
        }
    ],
    "count": 1,
}


def _client_with(query_return=None, query_raises=None):
    """An IDPClient whose SearchProcessor is stubbed."""
    instance = Mock()
    if query_raises is not None:
        instance.query.side_effect = query_raises
    else:
        instance.query.return_value = query_return
    return instance


@pytest.mark.unit
class TestSearchQuery:
    @patch("idp_sdk._core.search_processor.SearchProcessor")
    def test_query_returns_a_populated_search_result(self, mock_processor):
        mock_processor.return_value = _client_with(query_return=STUB_RESPONSE)

        client = IDPClient(stack_name="test-stack")
        result = client.search.query(question="What is the total amount?")

        assert isinstance(result, SearchResult)
        assert result.answer == "The total amount is $4,821.50."
        assert result.confidence == 0.93
        assert result.next_token is None
        assert len(result.citations) == 2

    @patch("idp_sdk._core.search_processor.SearchProcessor")
    def test_citations_carry_a_document_reference(self, mock_processor):
        """`citation.document.document_id` is the documented access path."""
        mock_processor.return_value = _client_with(query_return=STUB_RESPONSE)

        client = IDPClient(stack_name="test-stack")
        result = client.search.query(question="q")

        first = result.citations[0]
        assert isinstance(first, SearchCitation)
        assert isinstance(first.document, SearchDocumentReference)
        assert first.document.document_id == "batch-001/invoice1.pdf"
        assert first.document.section_id == 2
        assert first.document.page == 3
        assert first.text == "Total due: $4,821.50"
        assert first.confidence == 0.88

    @patch("idp_sdk._core.search_processor.SearchProcessor")
    def test_a_citation_without_location_or_confidence_is_accepted(
        self, mock_processor
    ):
        mock_processor.return_value = _client_with(query_return=STUB_RESPONSE)

        client = IDPClient(stack_name="test-stack")
        result = client.search.query(question="q")

        unlocalised = result.citations[1]
        assert unlocalised.document.document_id == ""
        assert unlocalised.document.section_id is None
        assert unlocalised.document.page is None
        assert unlocalised.confidence is None

    @patch("idp_sdk._core.search_processor.SearchProcessor")
    def test_no_results_returns_an_empty_result_rather_than_raising(
        self, mock_processor
    ):
        """An empty answer is a legitimate outcome, not an error."""
        mock_processor.return_value = _client_with(
            query_return={"question": "q", "results": [], "count": 0}
        )

        client = IDPClient(stack_name="test-stack")
        result = client.search.query(question="q")

        assert result.answer == ""
        assert result.citations == []
        # None, not 0.0: "no answer" must be distinguishable from "an answer the
        # model scored at zero".
        assert result.confidence is None

    @patch("idp_sdk._core.search_processor.SearchProcessor")
    def test_next_token_is_carried_through_on_both_paths(self, mock_processor):
        populated = dict(STUB_RESPONSE, next_token="dG9rZW4=")
        mock_processor.return_value = _client_with(query_return=populated)

        client = IDPClient(stack_name="test-stack")
        assert client.search.query(question="q").next_token == "dG9rZW4="

        mock_processor.return_value = _client_with(
            query_return={"question": "q", "results": [], "next_token": "dG9rZW4="}
        )
        assert client.search.query(question="q").next_token == "dG9rZW4="

    @patch("idp_sdk._core.search_processor.SearchProcessor")
    def test_query_passes_its_arguments_through(self, mock_processor):
        instance = _client_with(query_return=STUB_RESPONSE)
        mock_processor.return_value = instance

        client = IDPClient(stack_name="test-stack")
        client.search.query(
            question="q", document_ids=["a.pdf"], limit=5, next_token="tok"
        )

        instance.query.assert_called_once_with(
            question="q", document_ids=["a.pdf"], limit=5, next_token="tok"
        )

    @patch("idp_sdk._core.search_processor.SearchProcessor")
    def test_a_processor_failure_becomes_an_idp_processing_error(self, mock_processor):
        mock_processor.return_value = _client_with(
            query_raises=RuntimeError("knowledge base unavailable")
        )

        client = IDPClient(stack_name="test-stack")
        with pytest.raises(IDPProcessingError, match="knowledge base unavailable"):
            client.search.query(question="q")


@pytest.mark.unit
class TestSearchModels:
    """The models must be constructible from what the operation has available.

    Held separately from the operation tests so the constraint is stated on the
    models themselves: a later field addition without a default would break
    ``query()`` and these say so directly.
    """

    def test_search_result_needs_only_an_answer(self):
        assert SearchResult(answer="a").citations == []

    def test_document_reference_needs_only_a_document_id(self):
        reference = SearchDocumentReference(document_id="d.pdf")
        assert reference.section_id is None and reference.page is None

    def test_citation_needs_a_document_and_text(self):
        citation = SearchCitation(
            document=SearchDocumentReference(document_id="d.pdf"), text="t"
        )
        assert citation.confidence is None
