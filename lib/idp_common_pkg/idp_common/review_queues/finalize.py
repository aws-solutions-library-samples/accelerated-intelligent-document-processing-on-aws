# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Durable finalize: the message that starts phase 2, and applying corrections.

A Complete or Reject moves the item to FINALIZING with a ``FinalizeToken``; the
:class:`FinalizeSender` puts the document on the DocumentQueue with that token as
a message attribute. ``queue_processor`` names the execution from the token, so
a re-send (by the sweeper) collapses into ``ExecutionAlreadyExists`` instead of
starting a second run. The message resets ``queued_time`` and ``metering``, so
the finalize run counts only its own usage; the tracker merges the phase-1
snapshot kept on the item.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from idp_common.utils.execution_names import execution_name_for

from .models import DraftRecord, ReviewItemRecord

logger = logging.getLogger(__name__)

FINALIZE_TOKEN_RE = re.compile(r"^rvw-[0-9a-f]{32}$")
FINALIZE_EVENT_TYPE = "ReviewFinalize"
TOKEN_ATTRIBUTE = "ReviewFinalizeToken"


def finalize_token(doc_key: str, cycle_id: str, attempt: int) -> str:
    digest = hashlib.sha256(
        f"{doc_key}|{cycle_id}|{attempt}".encode("utf-8")
    ).hexdigest()
    return "rvw-" + digest[:32]


def finalize_execution_name(doc_key: str, token: str) -> str:
    name = execution_name_for(doc_key, token)
    assert name is not None  # a token is never empty
    return name


def is_finalize_token(value: Any) -> bool:
    return isinstance(value, str) and bool(FINALIZE_TOKEN_RE.match(value))


class FinalizeSender:
    """Sends the finalize message for a FINALIZING item."""

    def __init__(
        self,
        sqs_client: Any,
        queue_url: str,
        working_bucket: Optional[str],
        document_service: Any,
        *,
        input_bucket: Optional[str] = None,
        output_bucket: Optional[str] = None,
    ):
        self._sqs = sqs_client
        self._queue_url = queue_url
        self._working_bucket = working_bucket
        self._documents = document_service
        self._input_bucket = input_bucket
        self._output_bucket = output_bucket

    def build_message(self, item: ReviewItemRecord) -> tuple[str, dict[str, Any]]:
        from idp_common.models import Status

        document = self._documents.get_document(item.doc_key)
        if document is None:
            raise LookupError(
                f"tracking row missing for the finalize of {item.cycle_id}"
            )
        if self._input_bucket:
            document.input_bucket = self._input_bucket
        if self._output_bucket:
            document.output_bucket = self._output_bucket
        document.status = Status.QUEUED
        document.start_time = None
        document.completion_time = None
        document.workflow_execution_arn = None
        document.queued_time = datetime.now(timezone.utc).isoformat()
        document.metering = {}
        if self._working_bucket:
            body = document.serialize_document(self._working_bucket, "review_finalize")
        else:
            body = document.to_dict()
        attributes = {
            "EventType": {"DataType": "String", "StringValue": FINALIZE_EVENT_TYPE},
            "ObjectKey": {"DataType": "String", "StringValue": item.doc_key},
            TOKEN_ATTRIBUTE: {
                "DataType": "String",
                "StringValue": item.finalize_token or "",
            },
        }
        return json.dumps(body, default=str), attributes

    def send(self, item: ReviewItemRecord) -> str:
        """Send the message; return its id. Raises on failure (the caller logs)."""
        if not is_finalize_token(item.finalize_token):
            raise ValueError("item carries no valid finalize token")
        body, attributes = self.build_message(item)
        response = self._sqs.send_message(
            QueueUrl=self._queue_url, MessageBody=body, MessageAttributes=attributes
        )
        logger.info("event=review_finalize_sent attempt=%d", item.finalize_attempt)
        return str(response.get("MessageId", ""))


def apply_corrections(
    document: Any, drafts: Sequence[DraftRecord], s3: Any, output_bucket: str
) -> int:
    """Copy each section's latest draft onto its result URI; return the count.

    Idempotent (a copy of the same object twice gives the same result). Drafts for
    sections the document doesn't have are skipped with a WARNING.
    """
    from idp_common.utils import parse_s3_uri

    targets: dict[str, str] = {}
    for section in getattr(document, "sections", None) or []:
        if section.extraction_result_uri:
            targets[str(section.section_id)] = section.extraction_result_uri
    jobs = []
    for draft in drafts:
        uri = targets.get(draft.section_id)
        if uri is None:
            logger.warning(
                "event=review_draft_section_missing section=%s", draft.section_id
            )
            continue
        bucket, key = parse_s3_uri(uri)
        jobs.append((draft.s3_key, bucket, key))
    if not jobs:
        return 0

    def copy(job: tuple[str, str, str]) -> None:
        src_key, dst_bucket, dst_key = job
        s3.copy_object(output_bucket, src_key, dst_bucket, dst_key)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(copy, jobs))
    logger.info("event=review_corrections_applied sections=%d", len(jobs))
    return len(jobs)
