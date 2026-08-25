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
    """Apply ``document.forced_document_class``, if it is set.

    Returns the class applied, or ``None`` when there was nothing to force (no
    class requested, or the document has no pages yet).

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
    forced = getattr(document, "forced_document_class", None)
    if not forced or not getattr(document, "pages", None):
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
