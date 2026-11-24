# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routing step: where a document goes after policy rule validation.

A thin handler over ``idp_common.review_queues.routing_step.decide``. It answers
``hold`` (the document was placed in a review queue), ``continue`` or
``rejected``, which the state machine's ``CheckRoutingDecision`` reads.

Event: ``{execution_arn, document, execution_input}``.
Returns: ``{document, decision, reason, queue, cycle_id, rule_id}``.
"""

import json
import logging
import os
import time

from idp_common import utils
from idp_common.config import get_config
from idp_common.docs_service import create_document_service
from idp_common.models import Document
from idp_common.review_queues.routing_step import decide
from idp_common.review_queues.store import create_review_store
from idp_common.utils.transient_errors import raise_if_transient

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Review queues (idp_common.review_queues): None when HITL_TABLE_NAME is unset
# (headless), which keeps every profile on the legacy HITL flow.
_REVIEW_STORE = create_review_store()

#: The name this step's Lambda usage is metered under.
METERING_CONTEXT = "Routing"


def processing_mode(execution_input):
    """The mode the run takes: the plan's, else the one the run started with."""
    run_plan = execution_input.get("runPlan") or {}
    if run_plan.get("mode") in ("pipeline", "bda"):
        return run_plan["mode"]
    wrapper = execution_input.get("document") or {}
    return "bda" if wrapper.get("use_bda") is True else "pipeline"


def handler(event, context):
    """Decide what the run does next. See ``_handle``.

    A transient cause is re-raised under the name the state's retrier lists, so
    the state retries; anything else fails the run, which can be restarted.
    """
    try:
        return _handle(event, context)
    except Exception as e:
        raise_if_transient(e, where="review routing")
        logger.error("Review routing failed", exc_info=True)
        raise


def _handle(event, context):
    start_time = time.time()
    working_bucket = os.environ.get("WORKING_BUCKET")
    document = Document.load_document(event.get("document", {}), working_bucket, logger)

    # The configuration at the revision the document is pinned to.
    config = get_config(
        as_model=True,
        version=getattr(document, "config_version", None),
        revision=getattr(document, "config_revision", None),
    )
    # Absent on a run that started before the state machine passed it.
    execution_input = event.get("execution_input") or {}
    run_plan = execution_input.get("runPlan")

    decision = decide(
        document,
        config,
        mode=processing_mode(execution_input),
        execution_arn=event.get("execution_arn", ""),
        run_plan=run_plan,
        store=_REVIEW_STORE,
        document_service=create_document_service(),
        output_bucket=document.output_bucket or os.environ.get("OUTPUT_BUCKET"),
    )

    try:
        document.metering = utils.merge_metering_data(
            document.metering,
            utils.calculate_lambda_metering(METERING_CONTEXT, context, start_time),
        )
    except Exception as e:
        logger.warning(f"Failed to add Lambda metering for routing: {str(e)}")

    response = {
        "document": document.serialize_document(working_bucket, "routing", logger),
        "decision": decision.decision,
        "reason": decision.reason,
        "queue": decision.queue,
        "cycle_id": decision.cycle_id,
        "rule_id": decision.rule_id,
    }
    logger.info(f"Response: {json.dumps(response, default=str)}")
    return response
