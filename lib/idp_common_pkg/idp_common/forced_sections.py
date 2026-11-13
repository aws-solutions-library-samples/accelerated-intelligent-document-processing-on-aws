# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A document's section grouping, forced onto a pipeline run instead of classified.

Test Studio re-extracts one section of a multi-section document by re-running the
whole document with its grouping fixed: every section keeps the pages and class its
ground truth records, and only the targeted section's class is replaced. The
classification step then builds exactly those sections instead of asking the model,
and the harvest writes back only the targeted one.

The grouping travels as S3 object user metadata (``document-sections``) on the copy
of the document the run processes, the same channel as the whole-document
``document-class``. That channel is small — S3 allows 2 KB of user metadata per
object, keys and values together — so the encoding is compact (page runs as
``a-b``), ASCII-only, and capped at :data:`MAX_ENCODED_BYTES`. Callers encode with
:func:`encode_forced_sections`, which refuses a grouping that does not fit, and the
pipeline decodes with :func:`decode_forced_sections`, which refuses anything
malformed rather than guessing: a grouping it cannot read must fail the document,
because silently classifying it instead would rewrite every section.

Standard library only. The API resolvers import this module, and their Lambda layer
carries ``idp_common`` without the classification dependencies.
"""

import json
import re
from typing import Any, Dict, List, Optional, Sequence

#: The S3 user-metadata key the test-set file copier stamps and
#: ``Document.from_s3_event`` reads.
METADATA_KEY = "document-sections"

#: Room left for the grouping within S3's 2 KB user-metadata limit, after the
#: other keys the copier writes (config version and revision, submission source,
#: test-set id).
MAX_ENCODED_BYTES = 1400

#: Upper bound on the pages a grouping may name. A range is expanded on decode, so
#: without one a short string such as ``0-999999999`` would expand to a billion
#: pages.
MAX_PAGES = 5000

#: Upper bound on sections, matching the resolver's per-document maximum.
MAX_SECTIONS = 200

_SECTION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# The class vocabulary the test-set resolver accepts (validate_document_class).
_CLASS_RE = re.compile(r"[a-zA-Z0-9 _-]{1,100}")
_PAGES_RE = re.compile(r"\d+(-\d+)?(,\d+(-\d+)?)*")


class ForcedSectionsError(ValueError):
    """A forced grouping that cannot be encoded or decoded."""


def is_valid_document_class(value: Any) -> bool:
    """True for a class name a forced class or grouping may carry.

    The same vocabulary as a forced grouping's classes, for the whole-document
    ``document-class`` metadata value: up to 100 ASCII letters, digits, spaces,
    hyphens and underscores, the shape ``test_set_resolver.validate_document_class``
    enforces on the API. The test runner checks a forwarded ``documentClass`` with
    this before queueing it.
    """
    return isinstance(value, str) and bool(_CLASS_RE.fullmatch(value))


def _compress(indices: Sequence[int]) -> str:
    """``[0, 1, 2, 5, 4]`` -> ``"0-2,5,4"``: ascending runs only, order kept.

    A section's page list is its reading order as well as its membership, so a run
    is collapsed only when it ascends by one; any other order is written out.
    """
    parts: List[str] = []
    start = prev = None
    for index in indices:
        if prev is not None and index == prev + 1:
            prev = index
            continue
        if start is not None:
            parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = index
    if start is not None:
        parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def _expand(pages: str, budget: int) -> List[int]:
    """Inverse of :func:`_compress`, refusing more than ``budget`` pages."""
    out: List[int] = []
    for part in pages.split(","):
        if "-" in part:
            low_s, high_s = part.split("-", 1)
            low, high = int(low_s), int(high_s)
            if high <= low:
                raise ForcedSectionsError(f"page range '{part}' does not ascend")
            if len(out) + (high - low + 1) > budget:
                raise ForcedSectionsError(f"more than {MAX_PAGES} pages")
            out.extend(range(low, high + 1))
        else:
            if len(out) + 1 > budget:
                raise ForcedSectionsError(f"more than {MAX_PAGES} pages")
            out.append(int(part))
    return out


def _check(sections: Sequence[Dict[str, Any]]) -> None:
    """The rules both directions enforce, so an encoded grouping always decodes."""
    if not sections:
        raise ForcedSectionsError("a forced grouping needs at least one section")
    if len(sections) > MAX_SECTIONS:
        raise ForcedSectionsError(
            f"{len(sections)} sections; the maximum is {MAX_SECTIONS}"
        )
    seen_ids = set()
    seen_pages: Dict[int, str] = {}
    for section in sections:
        section_id = section["section_id"]
        classification = section["classification"]
        indices = section["page_indices"]
        if not isinstance(section_id, str) or not _SECTION_ID_RE.fullmatch(section_id):
            raise ForcedSectionsError(f"invalid section id {section_id!r}")
        if section_id in seen_ids:
            raise ForcedSectionsError(f"section '{section_id}' appears more than once")
        seen_ids.add(section_id)
        if not isinstance(classification, str) or not _CLASS_RE.fullmatch(
            classification
        ):
            raise ForcedSectionsError(
                f"section '{section_id}' has an invalid class {classification!r}"
            )
        if not isinstance(indices, list) or not indices:
            raise ForcedSectionsError(f"section '{section_id}' has no pages")
        for index in indices:
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise ForcedSectionsError(
                    f"section '{section_id}' has an invalid page index {index!r}"
                )
            if index in seen_pages:
                raise ForcedSectionsError(
                    f"page index {index} is in both section '{seen_pages[index]}' "
                    f"and section '{section_id}'"
                )
            seen_pages[index] = section_id
    if len(seen_pages) > MAX_PAGES:
        raise ForcedSectionsError(f"more than {MAX_PAGES} pages")


def encode_forced_sections(sections: Sequence[Dict[str, Any]]) -> str:
    """Encode a grouping for the ``document-sections`` metadata value.

    ``sections`` is a list of ``{"section_id", "classification", "page_indices"}``
    with 0-based page indices in reading order — the shape a test-set baseline
    records in ``split_document.page_indices``. Raises :class:`ForcedSectionsError`
    when the grouping is malformed or does not fit in :data:`MAX_ENCODED_BYTES`.
    """
    _check(sections)
    encoded = json.dumps(
        [
            [s["section_id"], s["classification"], _compress(s["page_indices"])]
            for s in sections
        ],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    if len(encoded.encode("ascii")) > MAX_ENCODED_BYTES:
        raise ForcedSectionsError(
            f"the grouping needs {len(encoded)} bytes of object metadata; the "
            f"maximum is {MAX_ENCODED_BYTES}"
        )
    return encoded


def decode_forced_sections(value: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    """Decode a ``document-sections`` value; ``None`` or empty means no grouping.

    Raises :class:`ForcedSectionsError` on anything malformed. There is no lenient
    reading: the caller's alternative to a forced grouping is classifying the
    document, which would replace every section's ground truth.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_ENCODED_BYTES:
        raise ForcedSectionsError("forced grouping is not a string of the right size")
    try:
        raw = json.loads(value)
    except ValueError as e:
        raise ForcedSectionsError(f"forced grouping is not JSON: {e}") from e
    if not isinstance(raw, list):
        raise ForcedSectionsError("forced grouping is not a list")
    sections: List[Dict[str, Any]] = []
    budget = MAX_PAGES
    for entry in raw:
        if (
            not isinstance(entry, list)
            or len(entry) != 3
            or not all(isinstance(part, str) for part in entry)
        ):
            raise ForcedSectionsError(f"malformed section entry {entry!r}")
        section_id, classification, pages = entry
        if not _PAGES_RE.fullmatch(pages):
            raise ForcedSectionsError(
                f"section '{section_id}' has malformed pages {pages!r}"
            )
        indices = _expand(pages, budget)
        budget -= len(indices)
        sections.append(
            {
                "section_id": section_id,
                "classification": classification,
                "page_indices": indices,
            }
        )
    _check(sections)
    return sections
