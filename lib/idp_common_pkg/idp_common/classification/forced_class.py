# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Applying a reviewer-supplied class instead of classifying.

When a reviewer corrects a misclassified document and asks for it to be
re-extracted, they are asserting that the pipeline's own classification is wrong.
So the class they chose has to *override* classification, not seed it — running
the model again would re-derive the same wrong answer and silently discard the
correction.

Lives here rather than inline in the classification Lambda so the rule is
testable without standing up the whole handler (document service, metering,
X-Ray), and so the same rule is available to any other caller that needs it.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def apply_forced_document_class(document) -> Optional[str]:
    """Apply ``document.forced_sections`` or ``document.forced_document_class``.

    Returns what was forced — the class for a whole document, or the classes of a
    forced grouping in section order — or ``None`` when there was nothing to force
    (neither is set, or the document has no pages yet). A forced grouping takes
    precedence; see :func:`_apply_forced_sections`.

    Does **two** things, and the second is easy to overlook: it stamps the class
    onto every page *and* builds the single section that covers them. Stamping
    pages alone is not enough — it routes into the classification step's
    already-classified skip, which returns the document untouched and therefore
    with ``sections == []`` for a document fresh from OCR. Extraction then has
    nothing to extract, the run completes with no output, and the symptom is a
    document that produced a summary and no fields at all.

    One section for the whole document is the right reading of "this document is
    a W2": the reviewer corrected the class of a document, not of one span of
    pages. It mirrors what the classification service already does when section
    splitting is disabled.

    Overwrites any existing page classification on purpose. A document reaching
    here has already been classified once — wrongly, which is why a human
    intervened.
    """
    if not getattr(document, "pages", None):
        return None
    grouping = getattr(document, "forced_sections", None)
    if grouping:
        return _apply_forced_sections(document, grouping)
    forced = getattr(document, "forced_document_class", None)
    if not forced:
        return None

    for page in document.pages.values():
        page.classification = forced
        page.confidence = 1.0

    # Imported here so this module stays importable in contexts that only need
    # the rule (the Lambda already has the model, tests may not).
    from idp_common.models import Section

    document.sections = [
        Section(
            section_id="1",
            classification=forced,
            confidence=1.0,
            page_ids=list(document.pages.keys()),
        )
    ]

    logger.info(
        f"Applied forced class '{forced}' to all {len(document.pages)} page(s) of "
        f"{getattr(document, 'id', '<unknown>')} and created one section covering "
        f"them; classification will be skipped"
    )
    return forced


def _apply_forced_sections(document, encoded: str) -> str:
    """Build exactly the sections a forced grouping names, without classifying.

    Test Studio's section-scoped re-extract sends the document's whole ground-truth
    grouping, with one section's class corrected, so that every other section keeps
    its pages, its class and its id. Forcing only the corrected section's pages and
    classifying the rest would let the model regroup the others, and the review
    flow keys on this run's copy of the document section by section.

    Each section carries its ground truth's 0-based page indices as
    ``attributes["page_indices"]``, which extraction writes out as
    ``split_document.page_indices``, so the harvested label records the same pages
    in the same reading order. Pipeline page ids are 1-based (index + 1).

    Raises ``ValueError`` when the grouping is malformed or names a page the
    document does not have. Failing the document is the only safe answer: the
    alternative, classifying it, is the rewrite of every section this exists to
    avoid. A page no section names keeps no class, and the classification step
    still skips the model because something was forced.
    """
    from idp_common.forced_sections import decode_forced_sections
    from idp_common.models import Section

    grouping = decode_forced_sections(encoded) or []
    sections = []
    for entry in grouping:
        page_ids = [str(index + 1) for index in entry["page_indices"]]
        missing = [pid for pid in page_ids if pid not in document.pages]
        if missing:
            raise ValueError(
                f"Forced section '{entry['section_id']}' names page(s) "
                f"{', '.join(missing)}, which {getattr(document, 'id', 'the document')} "
                f"does not have ({len(document.pages)} page(s))"
            )
        for pid in page_ids:
            page = document.pages[pid]
            page.classification = entry["classification"]
            page.confidence = 1.0
        sections.append(
            Section(
                section_id=entry["section_id"],
                classification=entry["classification"],
                confidence=1.0,
                page_ids=page_ids,
                attributes={"page_indices": list(entry["page_indices"])},
            )
        )
    document.sections = sections
    applied = ", ".join(entry["classification"] for entry in grouping)
    logger.info(
        f"Applied a forced grouping of {len(sections)} section(s) ({applied}) to "
        f"{getattr(document, 'id', '<unknown>')}; classification will be skipped"
    )
    return applied
