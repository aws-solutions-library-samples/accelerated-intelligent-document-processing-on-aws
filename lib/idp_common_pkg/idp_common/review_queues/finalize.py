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

Both senders here queue through ``idp_common.run_plan.sender``, with a run plan
that says where the run starts: a completed review at applying the review's
changes, or at extraction when the review decided that sections are extracted
again; a rejected one at the rejected end; and a removed or skipped one at
policy rule validation.
"""

from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from idp_common.run_plan.clearing import clear_after_review, clear_for_start
from idp_common.run_plan.models import RunKind, RunPlan
from idp_common.run_plan.sender import (
    EVENT_REVIEW_FINALIZE,
    EVENT_REVIEW_SKIP_RESUBMIT,
    build_run_message,
    send_run,
)
from idp_common.run_plan.steps import (
    APPLY_REVIEW_CHANGES,
    BEGINNING,
    EXTRACTION,
    POLICY_RULE_VALIDATION,
    REJECTED,
)
from idp_common.utils.execution_names import (
    RESUBMIT_TOKEN_ATTRIBUTE,
    RESUBMIT_TOKEN_RE,
    execution_name_for,
)

from .exceptions import ResubmitError, ResubmitNotConfigured
from .models import DraftRecord, ReviewItemRecord
from .statuses import MACHINE_RESULT_DISPOSITIONS

logger = logging.getLogger(__name__)

FINALIZE_TOKEN_RE = re.compile(r"^rvw-[0-9a-f]{32}$")
FINALIZE_EVENT_TYPE = EVENT_REVIEW_FINALIZE
TOKEN_ATTRIBUTE = "ReviewFinalizeToken"  # nosec B105 - DynamoDB attribute name, not a secret


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


#: Message ``EventType`` of a resubmit after a skipped review.
RESUBMIT_EVENT_TYPE = EVENT_REVIEW_SKIP_RESUBMIT

#: The disposition of a completed review.
_COMPLETE = "complete"


class DocumentResubmitter:
    """Puts a tracked document back on the DocumentQueue for another run.

    The run re-uses the document's existing results (OCR, classification,
    extraction and assessment are skipped) and re-runs the tail: summarization,
    evaluation, rule validation and the post-processing hook. Its run plan is a
    ``legacy_review`` one, which starts at the beginning and leaves the skipping
    to each step's own check. Unlike the review finalize message
    (:class:`FinalizeSender`) it keeps the document's metering:
    ``queue_processor`` moves the cumulative usage into the run's baseline.
    """

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

    def _prepare(self, doc_key: str, *, reset_metering: bool) -> Any:
        from idp_common.models import Status

        document = self._documents.get_document(doc_key)
        if document is None:
            raise LookupError("tracking row missing for the resubmit")
        if self._input_bucket:
            document.input_bucket = self._input_bucket
        if self._output_bucket:
            document.output_bucket = self._output_bucket
        document.status = Status.QUEUED
        document.start_time = None
        document.completion_time = None
        document.workflow_execution_arn = None
        if reset_metering:
            document.queued_time = datetime.now(timezone.utc).isoformat()
            document.metering = {}
        return document

    def resubmit(
        self,
        doc_key: str,
        *,
        idempotency_token: Optional[str] = None,
        prefix: str = "hitl_complete",
    ) -> str:
        """Send the document; return the message id.

        Raises :class:`ResubmitNotConfigured` with no queue URL, and
        :class:`ResubmitError` for a missing document or any send failure.
        """
        if not self._queue_url:
            raise ResubmitNotConfigured("QUEUE_URL is not configured")
        if idempotency_token is not None and not RESUBMIT_TOKEN_RE.match(
            idempotency_token
        ):
            raise ValueError("invalid resubmit idempotency token")
        try:
            document = self._prepare(doc_key, reset_metering=False)
            message_id = send_run(
                self._sqs,
                self._queue_url,
                document=document,
                plan=RunPlan(kind=RunKind.LEGACY_REVIEW, start=BEGINNING),
                event_type=RESUBMIT_EVENT_TYPE,
                working_bucket=self._working_bucket,
                prefix=prefix,
                extra_attributes=(
                    {RESUBMIT_TOKEN_ATTRIBUTE: idempotency_token}
                    if idempotency_token
                    else None
                ),
            )
        except (LookupError, ValueError) as e:
            raise ResubmitError(str(e)) from e
        except Exception as e:  # any S3/SQS/serialisation failure is reported
            raise ResubmitError(f"{type(e).__name__}: {e}") from e
        logger.info("event=review_resubmit_sent token=%s", bool(idempotency_token))
        return message_id


class FinalizeSender(DocumentResubmitter):
    """Sends the finalize message for a FINALIZING item."""

    #: The stored-document prefix of a finalize message.
    PREFIX = "review_finalize"

    def plan_for(self, item: ReviewItemRecord) -> RunPlan:
        """The run plan of the run that follows the item's review.

        | Disposition | Kind | Start |
        |---|---|---|
        | ``complete``, with ``restart_step`` ``extraction`` | ``review_completed`` | ``extraction``, for ``restart_sections`` |
        | ``complete``, otherwise | ``review_completed`` | ``apply_review_changes`` |
        | ``removed``, ``skipped`` | ``review_skipped`` | ``policy_rule_validation`` |
        | ``rejected``, or anything else | ``review_rejected`` | ``rejected`` |

        Where a completed review's run starts was decided when the review was
        completed and is read from the item (``restart_step``,
        ``restart_sections``); an item completed before the start was recorded
        has none and applies the review's changes. A run that extracts again
        does not count the document as reviewed.

        A plan is a function of the item alone, so a re-send of the same item
        carries the same plan.
        """
        if item.disposition == _COMPLETE and item.restart_step == EXTRACTION:
            return RunPlan(
                kind=RunKind.REVIEW_COMPLETED,
                start=EXTRACTION,
                sections=list(item.restart_sections),
                cycle_id=item.cycle_id,
            )
        if item.disposition == _COMPLETE:
            return RunPlan(
                kind=RunKind.REVIEW_COMPLETED,
                start=APPLY_REVIEW_CHANGES,
                cycle_id=item.cycle_id,
                reviewed=True,
            )
        if item.disposition in MACHINE_RESULT_DISPOSITIONS:
            return RunPlan(
                kind=RunKind.REVIEW_SKIPPED,
                start=POLICY_RULE_VALIDATION,
                cycle_id=item.cycle_id,
            )
        return RunPlan(
            kind=RunKind.REVIEW_REJECTED, start=REJECTED, cycle_id=item.cycle_id
        )

    def _document_and_plan(self, item: ReviewItemRecord) -> tuple[Any, RunPlan]:
        """The document the finalize run starts from, and its plan.

        A run that goes on to policy rule validation, summarization and
        evaluation produces them again, so their results are cleared on the
        queued document. A run that starts at extraction also has the fields of
        the sections it extracts again cleared. A rejected document runs none
        of them and keeps what it has.

        Fields only: nothing is deleted in S3. The stages write over their own
        outputs, and the role that sends this message may delete only under the
        document's ``review/`` folder.
        """
        try:
            document = self._prepare(item.doc_key, reset_metering=True)
        except LookupError:
            raise LookupError(
                f"tracking row missing for the finalize of {item.cycle_id}"
            ) from None
        plan = self.plan_for(item)
        if plan.start == EXTRACTION:
            # The tracking-row attributes it returns are not removed here: the
            # run writes the row from this document.
            clear_for_start(
                document, EXTRACTION, sections=plan.sections, keep_objects=True
            )
        elif plan.kind in (RunKind.REVIEW_COMPLETED, RunKind.REVIEW_SKIPPED):
            clear_after_review(document)
        return document, plan

    def _extra_attributes(self, item: ReviewItemRecord) -> dict[str, str]:
        return {TOKEN_ATTRIBUTE: item.finalize_token or ""}

    def build_message(self, item: ReviewItemRecord) -> tuple[str, dict[str, Any]]:
        """The body and the attributes ``send`` sends for the item."""
        document, plan = self._document_and_plan(item)
        return build_run_message(
            document=document,
            plan=plan,
            event_type=FINALIZE_EVENT_TYPE,
            working_bucket=self._working_bucket,
            prefix=self.PREFIX,
            extra_attributes=self._extra_attributes(item),
        )

    def send(self, item: ReviewItemRecord) -> str:
        """Send the message; return its id. Raises on failure (the caller logs)."""
        if not is_finalize_token(item.finalize_token):
            raise ValueError("item carries no valid finalize token")
        document, plan = self._document_and_plan(item)
        message_id = send_run(
            self._sqs,
            self._queue_url,
            document=document,
            plan=plan,
            event_type=FINALIZE_EVENT_TYPE,
            working_bucket=self._working_bucket,
            prefix=self.PREFIX,
            extra_attributes=self._extra_attributes(item),
        )
        logger.info("event=review_finalize_sent attempt=%d", item.finalize_attempt)
        return message_id


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
