# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review queue sweeper: runs ``idp_common.review_queues.sweeper`` every 5 minutes.

A thin handler. It expires leases and sends lease warnings, heals lost or failed
finalize runs and lost tracker events, and refreshes the queue registry's member
snapshot. Reserved concurrency 1 makes it the single sweep writer.
"""

import logging
import os
import time

from idp_common.review_queues.models import SweepReport
from idp_common.review_queues.store import create_review_store

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Module scope; the store creates no client until it is queried, and the sweeper
# (with its boto3 clients) is built on the first invocation.
_STORE = create_review_store()
_SWEEPER = {}


def _sweeper():
    if "s" not in _SWEEPER:
        import boto3

        import idp_common.s3 as s3_module
        from idp_common.docs_service import create_document_service
        from idp_common.review_queues import notify
        from idp_common.review_queues.directory import QueueDirectory
        from idp_common.review_queues.service import create_finalize_sender
        from idp_common.review_queues.sweeper import ReviewSweeper

        documents = create_document_service(mode="dynamodb")
        _SWEEPER["s"] = ReviewSweeper(
            _STORE,
            sfn_client=boto3.client("stepfunctions"),
            lambda_client=boto3.client("lambda"),
            tracker_function_name=os.environ["WORKFLOW_TRACKER_FUNCTION_NAME"],
            state_machine_arn=os.environ["STATE_MACHINE_ARN"],
            finalize_sender=create_finalize_sender(documents),
            document_service=documents,
            directory=QueueDirectory(),
            notifier=notify,
            s3=s3_module,
            output_bucket=os.environ.get("OUTPUT_BUCKET", ""),
        )
    return _SWEEPER["s"]


def handler(event, context):
    if _STORE is None:
        logger.info("HITL_TABLE_NAME is not set; nothing to sweep")
        return SweepReport().model_dump()
    now_ms = int(time.time() * 1000)
    deadline_ms = now_ms + context.get_remaining_time_in_millis()
    report = _sweeper().run(deadline_ms=deadline_ms)
    logger.info("Review queue sweep: %s", report.model_dump())
    return report.model_dump()
