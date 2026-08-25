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
    """Stamp ``document.forced_document_class`` onto every page, if it is set.

    Returns the class applied, or ``None`` when there was nothing to force (no
    class requested, or the document has no pages yet).

    Assigning it to every page is deliberate: the classification step already
    skips work when all pages carry a classification, so this routes into that
    existing path rather than adding a second, separately-maintained skip.

    Overwrites an existing page classification on purpose. A document reaching
    here with a forced class has already been classified once — wrongly, which is
    why a human intervened.
    """
    forced = getattr(document, "forced_document_class", None)
    if not forced or not getattr(document, "pages", None):
        return None

    for page in document.pages.values():
        page.classification = forced

    logger.info(
        f"Applied forced class '{forced}' to all {len(document.pages)} page(s) of "
        f"{getattr(document, 'id', '<unknown>')}; classification will be skipped"
    )
    return forced
