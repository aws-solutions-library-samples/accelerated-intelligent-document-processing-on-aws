# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Helpers for authoring pipeline-hook Lambdas.

A pipeline hook is a Lambda the unified workflow invokes at an extension point
(``preprocessing``, ``postOcr``, ``postClassification``, ``postExtraction``,
``postRuleValidation``, ``postSummarization``, ``postprocessing``) via the host's
pipeline-hooks dispatcher. A hook may simply observe, or it may hand a MODIFIED
:class:`~idp_common.models.Document` back for the next step to consume.

``preprocessing`` (first) and ``postprocessing`` (last) are standalone
single-hook points; the ``post*`` step points in between each hold a list. At
``postprocessing`` there is no next step — the returned document IS the workflow
output, which the host's tracker persists to the tracking table, reporting, and
the UI.

This module makes the safe round-trip two calls::

    from idp_common.hooks import load_hook_document, updated_document_result

    def lambda_handler(event, context):
        document = load_hook_document(event)          # resolves compressed refs
        document.sections[0].classification = "Invoice"
        return updated_document_result(document, myField="whatever")

Why go through these helpers rather than editing the raw event dict:

- The ``document`` in the event is usually a **compressed reference** (a small
  ``{compressed: true, s3_uri, ...}`` wrapper), not the document itself.
  :func:`load_hook_document` resolves either shape.
- Returning a document you built from scratch silently drops ``metering``,
  ``metering_baseline``, ``errors``, ``hitl_metadata``, and
  ``processing_issues``. Load → mutate → return keeps them.
- ``metering`` holds only this run's own usage; ``metering_baseline`` holds the
  usage of the document's earlier runs (set at run start when a document is
  reprocessed). The tracking table stores their sum and reporting gets
  ``metering`` alone, so a hook that returns a rebuilt document must carry both
  over, or the document's earlier usage is lost from its total.
- The dispatcher only honors a document returned under the ``updatedDocument``
  key; :func:`updated_document_result` spells that contract for you.

See ``docs/feature-platform.md`` (Pipeline hooks) for the full contract,
including which mutations propagate at which hook point.
"""

from typing import Any, Dict, Optional

from idp_common.models import Document

__all__ = [
    "UPDATED_DOCUMENT_KEY",
    "load_hook_document",
    "updated_document_result",
]

#: The response key the dispatcher reads a modified document from. Returning any
#: other shape leaves the pipeline's document untouched (the read-only
#: contract), which is why this is deliberately not ``"document"``.
UPDATED_DOCUMENT_KEY = "updatedDocument"


def load_hook_document(
    event: Dict[str, Any], working_bucket: Optional[str] = None
) -> Document:
    """Resolve a hook event's ``document`` to a full :class:`Document`.

    Handles both payload shapes transparently: a compressed reference (resolved
    from the working bucket) and an inline document dict.

    Args:
        event: The hook invocation event from the dispatcher.
        working_bucket: Bucket holding compressed documents. Defaults to the
            ``WORKING_BUCKET`` env var, which the host sets on feature hook
            Lambdas that import the ``<MainStackName>-WorkingBucketName``
            export.

    Returns:
        The document, with all sections/pages/metering intact.

    Raises:
        ValueError: If the event carries no usable document, or if a compressed
            reference cannot be resolved because no working bucket is known.
    """
    import os

    raw = event.get("document")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Hook event has no 'document' payload to load")

    bucket = working_bucket or os.environ.get("WORKING_BUCKET") or ""
    if raw.get("compressed") is True and not bucket:
        raise ValueError(
            "Hook event carries a compressed document reference but no working "
            "bucket was given (pass working_bucket= or set WORKING_BUCKET)"
        )
    return Document.load_document(raw, bucket)


def updated_document_result(
    document: Document,
    working_bucket: Optional[str] = None,
    size_threshold_kb: int = 200,
    **extra: Any,
) -> Dict[str, Any]:
    """Build a hook response that hands ``document`` to the next workflow step.

    Small documents are returned inline (the dispatcher spills them to S3
    itself); larger ones are compressed here, which keeps the hook's synchronous
    response well under Lambda's 6 MB limit no matter the document size.

    Args:
        document: The modified document.
        working_bucket: Bucket for compression. Defaults to the
            ``WORKING_BUCKET`` env var. Without one, the document is always
            returned inline.
        size_threshold_kb: Compress above this size. The default keeps typical
            documents inline (cheaper, one fewer S3 round-trip) while spilling
            large ones.
        **extra: Additional keys merged into the response — your own result
            fields, and/or ``halt=True`` at the ``preprocessing`` point.

    Returns:
        A response dict carrying the document under
        :data:`UPDATED_DOCUMENT_KEY`, plus ``extra``.

    Raises:
        ValueError: If ``extra`` collides with the reserved document key.
    """
    import os

    if UPDATED_DOCUMENT_KEY in extra:
        raise ValueError(
            f"{UPDATED_DOCUMENT_KEY!r} is set from the document argument; pass "
            "the document positionally rather than as a keyword"
        )

    bucket = working_bucket or os.environ.get("WORKING_BUCKET") or ""
    if bucket:
        # serialize_document compresses above the threshold and returns a plain
        # dict below it — exactly the two shapes the dispatcher accepts.
        payload = document.serialize_document(
            bucket, "hook", size_threshold_kb=size_threshold_kb
        )
    else:
        payload = document.to_dict()

    return {UPDATED_DOCUMENT_KEY: payload, **extra}
