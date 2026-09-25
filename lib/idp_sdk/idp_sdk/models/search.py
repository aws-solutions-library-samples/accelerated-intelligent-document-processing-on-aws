# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Search operation models.

Every field here is one the knowledge-base Lambda's response actually carries,
so a populated ``SearchResult`` never holds a field that is structurally always
``None``. The response shape is ``{"results": [{"answer", "confidence",
"citations": [...]}]}``; see ``idp_sdk._core.search_processor``.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SearchDocumentReference:
    """The document location a citation points at."""

    document_id: str
    #: Section number within the document, when the citation resolves to one.
    section_id: Optional[int] = None
    #: 1-based page number within the document, when the citation resolves to one.
    page: Optional[int] = None


@dataclass
class SearchCitation:
    """One passage the answer was grounded in, plus where it came from."""

    document: SearchDocumentReference
    text: str
    #: Retrieval confidence for this passage, when the knowledge base reports one.
    confidence: Optional[float] = None


@dataclass
class SearchResult:
    """Result from a knowledge base query."""

    answer: str
    citations: List[SearchCitation] = field(default_factory=list)
    #: Answer confidence. ``None`` when the query matched nothing, so that "no
    #: answer" is distinguishable from "an answer the model is not sure about".
    confidence: Optional[float] = None
    next_token: Optional[str] = None
