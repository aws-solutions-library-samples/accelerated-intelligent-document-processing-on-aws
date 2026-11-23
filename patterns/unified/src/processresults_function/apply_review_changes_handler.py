# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Apply a completed review: the first step of the run that follows it.

A thin handler over ``idp_common.review_queues.apply_review.apply_review_changes``.
It writes the reviewer's corrections onto the section results, records the review
on every section, clears the alerts the review answered, and writes the tracking
row. The run then goes on to policy rule validation.

A run that extracts some sections again reaches this step after the extraction
and the assembly. The corrections of those sections are set on their new results,
and the sections keep the alerts the corrections did not answer.

Event: ``{execution_arn, document, execution_input}``.
Returns: ``{document, rule_validation_enabled}``.
"""

import json
import logging
import os
import time

from idp_common import utils
from idp_common.config import get_config
from idp_common.config.feature_flags import policy_rule_validation_enabled
from idp_common.docs_service import create_document_service
from idp_common.models import Document, Status
from idp_common.review_queues.apply_review import apply_review_changes
from idp_common.review_queues.store import create_review_store
from idp_common.utils.transient_errors import raise_if_transient

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Review queues (idp_common.review_queues): None when HITL_TABLE_NAME is unset.
_REVIEW_STORE = create_review_store()

#: The name this step's Lambda usage is metered under.
METERING_CONTEXT = "ApplyReviewChanges"


def handler(event, context):
    """Apply the review this run follows. See ``_handle``.

    A transient cause is re-raised under the name the state's retrier lists, so
    the state retries; the step writes the same objects when it runs again.
    """
    try:
        return _handle(event, context)
    except Exception as e:
        raise_if_transient(e, where="apply review changes")
        logger.error("Applying the review failed", exc_info=True)
        raise


def _handle(event, context):
    start_time = time.time()
    working_bucket = os.environ.get("WORKING_BUCKET")
    document = Document.load_document(event.get("document", {}), working_bucket, logger)
    if _REVIEW_STORE is None:
        # Only a run that follows a review in a queue starts here.
        raise RuntimeError(
            "HITL_TABLE_NAME is not set, so there is no review to apply for "
            f"{document.input_key}"
        )

    config = get_config(
        as_model=True,
        version=getattr(document, "config_version", None),
        revision=getattr(document, "config_revision", None),
    )

    document.status = Status.POSTPROCESSING
    result = apply_review_changes(
        document,
        store=_REVIEW_STORE,
        output_bucket=document.output_bucket or os.environ.get("OUTPUT_BUCKET"),
        execution_arn=event.get("execution_arn", ""),
    )
    logger.info(
        f"Applied review {result.cycle_id}: {result.applied_sections} corrected, "
        f"{result.confirmed_sections} confirmed, "
        f"{result.extracted_sections} extracted again"
    )

    try:
        document.metering = utils.merge_metering_data(
            document.metering,
            utils.calculate_lambda_metering(METERING_CONTEXT, context, start_time),
        )
    except Exception as e:
        logger.warning(f"Failed to add Lambda metering for apply review: {str(e)}")

    # The cleared alerts and counts, and the status.
    create_document_service().update_document(document)

    response = {
        "document": document.serialize_document(
            working_bucket, "apply_review_changes", logger
        ),
        "rule_validation_enabled": policy_rule_validation_enabled(config),
    }
    logger.info(f"Response: {json.dumps(response, default=str)}")
    return response
