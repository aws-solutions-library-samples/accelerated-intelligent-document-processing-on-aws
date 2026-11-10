# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bulk review worker: advances bulk Skip All Reviews jobs from their SQS queue.

Each message is ``{"jobId", "wakeToken"}``. A step runs until the invocation is
nearly out of time, the document queue is deep (pacing), or the job is done; the
job then hands itself to its next wake-up (``BulkSkipService.schedule``). The job's
dead-letter queue is mapped here too: a dead-lettered message fails its job, so a
job always reaches a final state.
"""

import json
import logging
import os
from typing import Any, Optional

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_DYNAMODB = boto3.resource("dynamodb")
_SQS = boto3.client("sqs")
_SCOPE_CACHE: dict = {}
_SERVICE: Any = None


def _scope_resolver(meta: dict) -> Optional[list]:
    """The requester's current ``allowedConfigVersions`` (``None`` = unscoped)."""
    from idp_common.config_scope import resolve_allowed_config_versions

    return resolve_allowed_config_versions(
        str(meta.get("RequesterEmail") or ""),
        users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
        dynamodb=_DYNAMODB,
        caller_sub=None,
        cache=_SCOPE_CACHE,
    )


def queue_depth() -> int:
    """Visible plus delayed messages on the document queue (pacing input)."""
    attributes = _SQS.get_queue_attributes(
        QueueUrl=os.environ["QUEUE_URL"],
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesDelayed",
        ],
    ).get("Attributes", {})
    return int(attributes.get("ApproximateNumberOfMessages", 0)) + int(
        attributes.get("ApproximateNumberOfMessagesDelayed", 0)
    )


def send_job(job_id: str, wake_token: str, delay_seconds: int) -> None:
    _SQS.send_message(
        QueueUrl=os.environ["JOB_QUEUE_URL"],
        MessageBody=json.dumps({"jobId": job_id, "wakeToken": wake_token}),
        DelaySeconds=max(0, min(900, int(delay_seconds))),
        MessageAttributes={
            "EventType": {"DataType": "String", "StringValue": "BulkReviewJob"}
        },
    )


def _service():
    """Built on first use (module scope, so warm invocations reuse it)."""
    global _SERVICE
    if _SERVICE is None:
        from idp_common.docs_service import create_document_service
        from idp_common.dynamodb.client import DynamoDBClient
        from idp_common.review_queues import notify
        from idp_common.review_queues.bulk_jobs import BulkJobStore, BulkSkipService
        from idp_common.review_queues.bulk_selection import PendingReviewQuery
        from idp_common.review_queues.review_skip import SkipDeps
        from idp_common.review_queues.service import (
            create_resubmitter,
            create_review_queue_service,
        )

        hitl_table = os.environ["HITL_TABLE_NAME"]
        tracking = DynamoDBClient(table_name=os.environ["TRACKING_TABLE"])
        documents = create_document_service(mode="dynamodb")
        review_service = create_review_queue_service()

        def deps():
            return SkipDeps(
                tracking_table=_DYNAMODB.Table(os.environ["TRACKING_TABLE"]),
                get_document=documents.get_document,
                review_service=review_service,
                store=review_service.store,
                resubmitter=create_resubmitter(documents),
            )

        _SERVICE = BulkSkipService(
            deps,
            BulkJobStore(DynamoDBClient(table_name=hitl_table)),
            PendingReviewQuery(tracking),
            tracking_client=tracking,
            job_sender=send_job,
            retention_days=int(os.environ.get("DATA_RETENTION_IN_DAYS") or 365),
            scope_resolver=_scope_resolver,
            queue_depth=queue_depth,
            max_concurrent_workflows=int(
                os.environ.get("MAX_CONCURRENT_WORKFLOWS") or 100
            ),
            notifier=notify.bulk_job_finished,
        )
    return _SERVICE


def _is_dead_letter(record: dict) -> bool:
    dlq = os.environ.get("JOB_DLQ_ARN", "")
    return bool(dlq) and record.get("eventSourceARN") == dlq


def handler(event, context):
    failures = []
    max_receives = int(os.environ.get("MAX_RECEIVES") or 5)
    for record in event.get("Records") or []:
        message_id = record.get("messageId", "")
        try:
            body = json.loads(record.get("body") or "{}")
            job_id = str(body.get("jobId") or "")
            wake = str(body.get("wakeToken") or "")
        except (TypeError, ValueError):
            logger.warning("Rejecting a malformed bulk job message")
            continue
        if not job_id:
            logger.warning("Rejecting a bulk job message with no job id")
            continue
        service = _service()
        if _is_dead_letter(record):
            service.fail_job(job_id, "dead_lettered")
            continue
        try:
            delay = service.run_job_step(
                job_id,
                wake,
                lambda: context.get_remaining_time_in_millis() / 1000.0,
            )
            if delay is not None:
                service.schedule(job_id, delay)
        except Exception:
            logger.error("event=bulk_job_step_failed", exc_info=True)
            receives = int(
                (record.get("attributes") or {}).get("ApproximateReceiveCount") or 1
            )
            if receives >= max_receives:
                service.fail_job(job_id, "worker_error")
            else:
                failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
