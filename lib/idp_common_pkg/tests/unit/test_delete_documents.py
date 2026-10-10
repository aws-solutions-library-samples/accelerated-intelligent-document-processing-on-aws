"""
Unit tests for delete_documents module — get_documents_by_batch and get_documents_by_pattern.
"""

from unittest.mock import MagicMock

import pytest

from idp_common.delete_documents import (
    get_documents_by_batch,
    get_documents_by_pattern,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_table(items):
    """Return a mock DynamoDB Table whose scan() yields *items* in one page."""
    table = MagicMock()
    table.scan.return_value = {"Items": items}
    return table


def _doc_item(object_key, status="COMPLETED"):
    return {
        "PK": f"doc#{object_key}",
        "SK": "none",
        "ObjectKey": object_key,
        "Status": status,
    }


# ---------------------------------------------------------------------------
# get_documents_by_batch
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetDocumentsByBatch:
    def test_returns_matching_docs(self):
        items = [
            _doc_item("batch-A/doc1.pdf"),
            _doc_item("batch-A/doc2.pdf"),
            _doc_item("batch-B/doc3.pdf"),
        ]
        table = _make_table(items)
        result = get_documents_by_batch(table, "batch-A")
        assert sorted(result) == ["batch-A/doc1.pdf", "batch-A/doc2.pdf"]

    def test_returns_empty_when_no_match(self):
        table = _make_table([_doc_item("batch-X/doc.pdf")])
        assert get_documents_by_batch(table, "batch-Y") == []

    def test_status_filter_passed_to_scan(self):
        table = _make_table([])
        get_documents_by_batch(table, "batch-A", status_filter="FAILED")
        # Verify scan was called (the Attr condition is built dynamically)
        table.scan.assert_called_once()
        call_kwargs = table.scan.call_args[1]
        assert "FilterExpression" in call_kwargs

    def test_no_status_filter_omits_status_condition(self):
        table = _make_table([])
        get_documents_by_batch(table, "batch-A")
        table.scan.assert_called_once()

    def test_handles_pagination(self):
        """Scan with LastEvaluatedKey triggers a second page."""
        table = MagicMock()
        table.scan.side_effect = [
            {"Items": [_doc_item("b/doc1.pdf")], "LastEvaluatedKey": {"PK": "x"}},
            {"Items": [_doc_item("b/doc2.pdf")]},
        ]
        result = get_documents_by_batch(table, "b")
        assert len(result) == 2
        assert table.scan.call_count == 2

    def test_a_scan_error_reaches_the_caller(self):
        """An empty list has to keep meaning "no documents in that batch".

        The selector returns `List[str]`, so `[]` is the only thing a swallowed fault
        could be reported as, and both callers turn `[]` into a success. The deeper
        per-exception-class assertions are in `test_delete_documents_coverage.py`.
        """
        table = MagicMock()
        table.scan.side_effect = Exception("DynamoDB error")
        with pytest.raises(Exception, match="DynamoDB error"):
            get_documents_by_batch(table, "batch-A")


# ---------------------------------------------------------------------------
# get_documents_by_pattern
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetDocumentsByPattern:
    def test_star_matches_all(self):
        items = [_doc_item("a/doc.pdf"), _doc_item("b/doc.pdf")]
        table = _make_table(items)
        result = get_documents_by_pattern(table, "*")
        assert len(result) == 2

    def test_prefix_star(self):
        items = [
            _doc_item("batch-123/invoice.pdf"),
            _doc_item("batch-123/receipt.pdf"),
            _doc_item("batch-456/invoice.pdf"),
        ]
        table = _make_table(items)
        result = get_documents_by_pattern(table, "batch-123/*")
        assert sorted(result) == ["batch-123/invoice.pdf", "batch-123/receipt.pdf"]

    def test_suffix_star(self):
        items = [
            _doc_item("b/doc.pdf"),
            _doc_item("b/doc.png"),
        ]
        table = _make_table(items)
        result = get_documents_by_pattern(table, "*.pdf")
        assert result == ["b/doc.pdf"]

    def test_middle_star(self):
        items = [
            _doc_item("batch-A/invoice-001.pdf"),
            _doc_item("batch-B/invoice-002.pdf"),
            _doc_item("batch-C/receipt-001.pdf"),
        ]
        table = _make_table(items)
        result = get_documents_by_pattern(table, "*invoice*")
        assert len(result) == 2

    def test_question_mark_wildcard(self):
        items = [
            _doc_item("b/doc1.pdf"),
            _doc_item("b/doc2.pdf"),
            _doc_item("b/doc10.pdf"),
        ]
        table = _make_table(items)
        result = get_documents_by_pattern(table, "b/doc?.pdf")
        assert sorted(result) == ["b/doc1.pdf", "b/doc2.pdf"]

    def test_no_match_returns_empty(self):
        table = _make_table([_doc_item("batch/doc.pdf")])
        assert get_documents_by_pattern(table, "*.xlsx") == []

    def test_status_filter_with_pattern(self):
        items = [
            _doc_item("b/doc1.pdf", status="COMPLETED"),
            _doc_item("b/doc2.pdf", status="FAILED"),
        ]
        # With status_filter, only matching-status items should be in scan results.
        # We simulate DynamoDB filtering by only returning FAILED items.
        table = _make_table([items[1]])
        result = get_documents_by_pattern(table, "b/*", status_filter="FAILED")
        assert result == ["b/doc2.pdf"]

    def test_a_scan_error_reaches_the_caller(self):
        table = MagicMock()
        table.scan.side_effect = Exception("DynamoDB error")
        with pytest.raises(Exception, match="DynamoDB error"):
            get_documents_by_pattern(table, "*")

    def test_handles_pagination(self):
        table = MagicMock()
        table.scan.side_effect = [
            {"Items": [_doc_item("b/doc1.pdf")], "LastEvaluatedKey": {"PK": "x"}},
            {"Items": [_doc_item("b/doc2.pdf")]},
        ]
        result = get_documents_by_pattern(table, "b/*")
        assert len(result) == 2
