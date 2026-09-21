# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``client.document.list()``.

This one is the silent member of its defect class. ``list_documents`` passed
``count=`` to a ``DocumentListResult`` whose field was ``total_count``, and
``batch_id=`` to a ``DocumentInfo`` that had no such field — and pydantic's
default is to *ignore* unknown keyword arguments, so nothing raised. The call
returned a result whose count was ``None`` and whose documents had lost their
batch id, on every invocation. The assertions below therefore check the values
that come out, not that the constructor was reached.
"""

from unittest.mock import Mock, patch

import pytest

from idp_sdk import IDPClient
from idp_sdk.exceptions import IDPProcessingError
from idp_sdk.models import DocumentInfo, DocumentListResult

#: What DocumentProcessor.list_documents returns for one page of the tracking
#: table. `timestamp` defaults to "" for an item that has none, which is why the
#: operation normalises it before pydantic sees it.
STUB_PAGE = {
    "documents": [
        {
            "document_id": "batch-001/invoice1.pdf",
            "status": "COMPLETED",
            "timestamp": "2024-01-15T10:30:00",
            "batch_id": "batch-001",
        },
        {
            "document_id": "adhoc/invoice2.pdf",
            "status": "QUEUED",
            "timestamp": "",
            "batch_id": None,
        },
    ],
    "count": 2,
    "next_token": "dG9rZW4=",
}


def _stub_processor(list_return=None, raises=None):
    instance = Mock()
    if raises is not None:
        instance.list_documents.side_effect = raises
    else:
        instance.list_documents.return_value = list_return
    return instance


@pytest.mark.unit
class TestDocumentList:
    @patch("idp_sdk._core.document_processor.DocumentProcessor")
    def test_count_reports_the_page_size(self, mock_processor):
        mock_processor.return_value = _stub_processor(list_return=STUB_PAGE)

        client = IDPClient(stack_name="test-stack")
        result = client.document.list(limit=50)

        assert isinstance(result, DocumentListResult)
        assert result.count == 2
        assert result.count == len(result.documents)
        assert result.next_token == "dG9rZW4="

    @patch("idp_sdk._core.document_processor.DocumentProcessor")
    def test_count_survives_model_dump(self, mock_processor):
        """The regression that shipped: a serialised result with a null count."""
        mock_processor.return_value = _stub_processor(list_return=STUB_PAGE)

        client = IDPClient(stack_name="test-stack")
        dumped = client.document.list().model_dump()

        assert dumped["count"] == 2
        assert "total_count" not in dumped

    @patch("idp_sdk._core.document_processor.DocumentProcessor")
    def test_batch_id_is_not_dropped(self, mock_processor):
        """pydantic ignores unknown kwargs, so this needs a value assertion."""
        mock_processor.return_value = _stub_processor(list_return=STUB_PAGE)

        client = IDPClient(stack_name="test-stack")
        documents = client.document.list().documents

        assert isinstance(documents[0], DocumentInfo)
        assert documents[0].batch_id == "batch-001"
        assert documents[1].batch_id is None

    @patch("idp_sdk._core.document_processor.DocumentProcessor")
    def test_an_empty_timestamp_becomes_none(self, mock_processor):
        """An item with no timestamp would otherwise fail datetime validation."""
        mock_processor.return_value = _stub_processor(list_return=STUB_PAGE)

        client = IDPClient(stack_name="test-stack")
        documents = client.document.list().documents

        assert documents[0].timestamp is not None
        assert documents[1].timestamp is None

    @patch("idp_sdk._core.document_processor.DocumentProcessor")
    def test_an_empty_page_is_a_count_of_zero(self, mock_processor):
        mock_processor.return_value = _stub_processor(
            list_return={"documents": [], "count": 0}
        )

        client = IDPClient(stack_name="test-stack")
        result = client.document.list()

        assert result.count == 0
        assert result.next_token is None

    @patch("idp_sdk._core.document_processor.DocumentProcessor")
    def test_a_processor_failure_becomes_an_idp_processing_error(self, mock_processor):
        mock_processor.return_value = _stub_processor(
            raises=RuntimeError("scan failed")
        )

        client = IDPClient(stack_name="test-stack")
        with pytest.raises(IDPProcessingError, match="scan failed"):
            client.document.list()


@pytest.mark.unit
def test_document_list_result_requires_a_count():
    """`count` has no default, so a caller that forgets it fails loudly.

    A default of `None` is what turned this into a wrong answer rather than an
    error, so the absence of one is the fix and is worth asserting.
    """
    with pytest.raises(Exception):  # pydantic ValidationError
        DocumentListResult(documents=[])  # pyright: ignore[reportCallIssue]
