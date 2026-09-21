# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Search operations for IDP SDK."""

from typing import List, Optional

from idp_sdk.exceptions import IDPProcessingError
from idp_sdk.models import SearchCitation, SearchDocumentReference, SearchResult


class SearchOperation:
    """Knowledge base query and search operations."""

    def __init__(self, client):
        self._client = client

    def query(
        self,
        question: str,
        document_ids: Optional[List[str]] = None,
        limit: int = 10,
        next_token: Optional[str] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> SearchResult:
        """Query knowledge base with natural language question.

        Args:
            question: Natural language question
            document_ids: Optional list of document IDs to search within
            limit: Maximum number of results to return (default: 10)
            next_token: Pagination token from previous request
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            SearchResult with answer, confidence, and citations. When the
            knowledge base returns no result, ``answer`` is empty and
            ``confidence`` is ``None`` — ``next_token`` is still carried through
            so pagination past an empty page works.
        """
        from idp_sdk._core.search_processor import SearchProcessor

        name = self._client._require_stack(stack_name)

        try:
            processor = SearchProcessor(stack_name=name, region=self._client._region)
            result = processor.query(
                question=question,
                document_ids=document_ids,
                limit=limit,
                next_token=next_token,
            )

            # The knowledge base answers one question at a time; `results` is a
            # list for forward compatibility and today holds at most one entry.
            items = result.get("results") or []
            if not items:
                return SearchResult(
                    answer="",
                    citations=[],
                    confidence=None,
                    next_token=result.get("next_token"),
                )

            item = items[0]
            citations = [
                SearchCitation(
                    document=SearchDocumentReference(
                        document_id=c.get("document_id", ""),
                        section_id=c.get("section_id"),
                        page=c.get("page"),
                    ),
                    text=c.get("text", ""),
                    confidence=c.get("confidence"),
                )
                for c in item.get("citations", [])
            ]

            return SearchResult(
                answer=item.get("answer", ""),
                citations=citations,
                confidence=item.get("confidence"),
                next_token=result.get("next_token"),
            )

        except Exception as e:
            raise IDPProcessingError(f"Failed to query knowledge base: {e}") from e
