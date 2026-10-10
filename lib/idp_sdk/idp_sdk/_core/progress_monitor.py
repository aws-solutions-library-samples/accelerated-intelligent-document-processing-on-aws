# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Progress Monitor Module

Monitors batch processing progress by querying document status via LookupFunction.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

import boto3

from idp_sdk.models.base import (
    TERMINAL_DOCUMENT_STATES,
    DocumentBucket,
    classify_document_state,
)

logger = logging.getLogger(__name__)

# A document in one of these will never change again, so the monitor can stop
# polling it. Taken from `idp_sdk.models.base`, where the four progress buckets
# are defined as a partition of `DocumentState` that an import-time check and an
# offline test both hold to -- so a state added to the enum cannot reach this
# module unclassified. REDACTED_SUPERSEDED is terminal by design: a preprocessing
# hook replaced the original with a redacted copy, so it never reaches COMPLETED,
# and without it here monitoring spins until timeout on every redact-and-stop doc.
_TERMINAL_STATES = frozenset(state.value for state in TERMINAL_DOCUMENT_STATES)

#: How long after a batch was submitted a `NOT_FOUND` document is still read as
#: in flight rather than as a settled failure.
#:
#: `NOT_FOUND` means the tracking table holds no row for the document id, and
#: `idp_sdk.models.base` classifies it as terminal-and-failed on the premise that
#: "a document id with no row in the tracking table will never acquire one by
#: waiting". That premise is false for the first few seconds after an upload: the
#: row is written by QueueSender, which S3 reaches through EventBridge
#: *asynchronously*, so between the upload returning and the row appearing there
#: is a window in which the document legitimately does not exist yet.
#:
#: A monitor that polls inside that window used to latch the document as
#: permanently failed on its very first poll, cache it as terminal, never ask
#: again, and report the whole batch complete-and-failed in under two seconds.
#: Downstream that reads as "nothing was produced" rather than "nothing has
#: started", which is how it cost a CI suite several nights: the batch was
#: declared failed 1.6s after upload, `download-results` found zero files, and
#: the step reported a content assertion miss with no failed execution anywhere.
#:
#: 60s is the latency budget the queue path is allowed before a missing row is
#: genuinely a missing row. It is deliberately generous: the cost of waiting too
#: long is a slower failure, and the cost of not waiting long enough is a false
#: one.
NOT_FOUND_GRACE_SECONDS = float(os.environ.get("IDP_NOT_FOUND_GRACE_SECONDS", "60"))


class ProgressMonitor:
    """Monitors document processing progress"""

    def __init__(self, stack_name: str, resources: Dict[str, str], region: str = None):
        """
        Initialize progress monitor

        Args:
            stack_name: Name of the CloudFormation stack
            resources: Dictionary of stack resources
            region: AWS region (optional)
        """
        self.stack_name = stack_name
        self.resources = resources
        self.region = region
        self.lambda_client = boto3.client("lambda", region_name=region)
        self.lookup_function = resources.get("LookupFunctionName", "")

        # Track finished documents to avoid redundant queries
        self.finished_docs = {}  # {doc_id: status_info}

        if not self.lookup_function:
            raise ValueError("LookupFunctionName not found in stack resources")

    def get_batch_status(
        self,
        document_ids: List[str],
        batch_started_at: Optional[datetime] = None,
    ) -> Dict:
        """
        Get status of all documents in batch using optimized batch query

        Uses batch Lambda invocation and caches finished documents to reduce API calls.

        Args:
            document_ids: List of document IDs to check
            batch_started_at: When the batch was submitted, if known. Within
                `NOT_FOUND_GRACE_SECONDS` of it, a `NOT_FOUND` document is
                reported as queued instead of failed and is not cached, because
                the QueueSender row it is missing may still be on its way. Pass
                `None` -- the default -- when the submission time is unknown or
                irrelevant, and `NOT_FOUND` settles immediately as it always
                did; that is what keeps a status query against a mistyped or
                long-gone document id answering at once instead of hanging for a
                minute.

        Returns:
            Dictionary with status summary
        """
        status_summary = {
            "completed": [],
            "running": [],
            "queued": [],
            "failed": [],
            "all_complete": False,
            "total": len(document_ids),
        }

        # If no document IDs provided, return empty status (not complete)
        if not document_ids:
            logger.warning("No document IDs provided for batch status check")
            return status_summary

        not_found_is_terminal = self._not_found_has_settled(batch_started_at)

        # Separate finished (cached) from active (need to query) documents
        docs_to_query = []
        for doc_id in document_ids:
            if doc_id in self.finished_docs:
                # Use cached status
                cached = self.finished_docs[doc_id]
                self._categorize_document(cached, status_summary, not_found_is_terminal)
            else:
                docs_to_query.append(doc_id)

        # If all docs are finished, return cached results
        if not docs_to_query:
            logger.debug("All documents finished (using cache)")
            finished = len(status_summary["completed"]) + len(status_summary["failed"])
            status_summary["all_complete"] = finished == len(document_ids)
            return status_summary

        logger.debug(
            f"Querying {len(docs_to_query)} active documents ({len(self.finished_docs)} cached)"
        )

        # Batch query active documents
        try:
            statuses = self._batch_query_documents(docs_to_query)

            for status in statuses:
                self._categorize_document(status, status_summary, not_found_is_terminal)

                # Cache finished documents (terminal states). The cache is a
                # commitment -- a cached document is never queried again -- so a
                # NOT_FOUND still inside its grace window must stay out of it,
                # or the grace window would only ever apply to the first poll.
                if self._is_settled(status["status"], not_found_is_terminal):
                    self.finished_docs[status["document_id"]] = status

        except Exception as e:
            logger.error(f"Error in batch query: {e}", exc_info=True)
            # Fall back to individual queries if batch fails
            for doc_id in docs_to_query:
                try:
                    status = self.get_document_status(doc_id)
                    self._categorize_document(
                        status, status_summary, not_found_is_terminal
                    )

                    if self._is_settled(status["status"], not_found_is_terminal):
                        self.finished_docs[status["document_id"]] = status
                except Exception as e:
                    logger.error(f"Error getting status for {doc_id}: {e}")
                    status_summary["queued"].append(
                        {"document_id": doc_id, "status": "UNKNOWN", "error": str(e)}
                    )

        # Check if all complete
        finished = len(status_summary["completed"]) + len(status_summary["failed"])
        status_summary["all_complete"] = finished == len(document_ids)

        return status_summary

    def _batch_query_documents(self, document_ids: List[str]) -> List[Dict]:
        """
        Query multiple documents in a single Lambda invocation

        Args:
            document_ids: List of document IDs to query

        Returns:
            List of document status dictionaries
        """
        # Invoke Lambda with batch request (status_only=True includes timing, excludes Step Functions)
        response = self.lambda_client.invoke(
            FunctionName=self.lookup_function,
            InvocationType="RequestResponse",
            Payload=json.dumps(
                {
                    "object_keys": document_ids,
                    "status_only": True,  # Includes status + timing, excludes processingDetail
                }
            ),
        )

        # Parse response
        payload = response["Payload"].read()
        result = json.loads(payload)

        # Handle Lambda error
        if response.get("FunctionError"):
            logger.error(f"Batch Lambda error: {result}")
            raise Exception(result.get("errorMessage", "Unknown batch query error"))

        # Extract results from batch response
        batch_results = result.get("results", [])

        # Convert to standard format
        statuses = []
        for doc_result in batch_results:
            # Extract timing info if available
            timing = doc_result.get("timing", {})
            elapsed = timing.get("elapsed", {})

            status_value = doc_result.get("status", "UNKNOWN")
            status = {
                "document_id": doc_result.get("object_key"),
                "status": status_value,
                "workflow_arn": "",
                "start_time": "",
                "end_time": "",
                "duration": elapsed.get("total", 0) / 1000.0
                if elapsed.get("total")
                else 0,  # Convert ms to seconds
            }

            # Add error info for failed/aborted documents
            if status_value == "ABORTED":
                status["error"] = "Aborted by user"
                status["failed_step"] = "N/A"
            elif status_value == "FAILED":
                status["error"] = doc_result.get("error", "Unknown error")
                status["failed_step"] = doc_result.get("failed_step", "Unknown")

            statuses.append(status)

        return statuses

    @staticmethod
    def _not_found_has_settled(batch_started_at: Optional[datetime]) -> bool:
        """Return whether a missing tracking row can now be called a failure.

        True when the batch was submitted longer ago than
        `NOT_FOUND_GRACE_SECONDS`, or when its submission time is unknown.

        A naive timestamp is read as UTC rather than rejected: the batch
        metadata this is derived from is written with `datetime.now(timezone.utc)`
        and so is aware, but it round-trips through JSON and an older batch
        document may not carry an offset. Guessing UTC is right for every writer
        in this codebase, and the alternative -- treating it as unparseable and
        settling immediately -- would reintroduce the race it exists to close.

        Args:
            batch_started_at: Batch submission time, or None if not known.

        Returns:
            True if `NOT_FOUND` should be reported as a settled failure.
        """
        if batch_started_at is None:
            return True
        if batch_started_at.tzinfo is None:
            batch_started_at = batch_started_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - batch_started_at).total_seconds()
        # A clock skew that dates the batch in the future gives a negative age,
        # which falls on the in-grace side. That is the safe direction: it delays
        # a verdict rather than declaring a document failed that was never
        # looked for, and the caller's own polling deadline still bounds the wait.
        return age >= NOT_FOUND_GRACE_SECONDS

    @staticmethod
    def _is_settled(status_value: str, not_found_is_terminal: bool) -> bool:
        """Return whether this status will never change, so it can be cached."""
        if status_value == "NOT_FOUND" and not not_found_is_terminal:
            return False
        return status_value in _TERMINAL_STATES

    def _categorize_document(
        self,
        status: Dict,
        status_summary: Dict,
        not_found_is_terminal: bool = True,
    ):
        """
        Categorize a document status into the appropriate summary bucket

        Args:
            status: Document status dictionary
            status_summary: Status summary dictionary to update
            not_found_is_terminal: When False, a `NOT_FOUND` document is reported
                as queued rather than failed, because its QueueSender row may
                still be in flight. Defaults to True, which is the unconditional
                behaviour every caller had before the grace window existed.
        """
        status_value = status["status"]
        bucket = classify_document_state(status_value)

        if bucket is DocumentBucket.FAILED and status_value == "NOT_FOUND":
            if not not_found_is_terminal:
                # Still inside the grace window: the document has been accepted
                # but no tracking row exists yet, which is what "queued" means.
                # Reported without an `error`, so a transient state cannot be
                # displayed as a failure or counted as one.
                status_summary[DocumentBucket.QUEUED.value].append(status)
                return
            # NOT_FOUND is treated as failed - document was never tracked in DynamoDB
            status["error"] = "Document not found in tracking table"
            status["failed_step"] = "QueueSender"

        status_summary[bucket.value].append(status)

    def get_document_status(self, doc_id: str) -> Dict:
        """
        Get detailed status of a single document

        Args:
            doc_id: Document identifier (object key)

        Returns:
            Dictionary with document status information
        """
        try:
            # Invoke LookupFunction Lambda
            payload_request = {"object_key": doc_id}

            response = self.lambda_client.invoke(
                FunctionName=self.lookup_function,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload_request),
            )

            # Parse response
            payload = response["Payload"].read()
            result = json.loads(payload)

            # Handle Lambda error
            if response.get("FunctionError"):
                logger.error(f"Lambda error for {doc_id}: {result}")
                return {
                    "document_id": doc_id,
                    "status": "ERROR",
                    "error": result.get("errorMessage", "Unknown error"),
                }

            # Extract status information (note: Lambda returns lowercase 'status')
            status = result.get("status", "UNKNOWN")

            doc_status = {
                "document_id": doc_id,
                "status": status,
                "workflow_arn": result.get("WorkflowExecutionArn", ""),
                "start_time": result.get("StartTime", ""),
                "end_time": result.get("EndTime", ""),
                "duration": result.get("Duration", 0),
            }

            # Add status-specific fields
            if status == "RUNNING":
                doc_status["current_step"] = result.get("CurrentStep", "Unknown")
            elif status == "ABORTED":
                # Aborted documents show user-friendly message
                doc_status["error"] = result.get("Error", "Aborted by user")
                doc_status["failed_step"] = result.get("FailedStep", "N/A")
            elif status == "FAILED":
                doc_status["error"] = result.get("Error", "Unknown error")
                doc_status["failed_step"] = result.get("FailedStep", "Unknown")
            elif status == "COMPLETED":
                doc_status["num_sections"] = result.get("NumSections", 0)

            return doc_status

        except Exception as e:
            logger.error(f"Error querying document status for {doc_id}: {e}")
            return {"document_id": doc_id, "status": "ERROR", "error": str(e)}

    def get_recent_completions(self, status_data: Dict, limit: int = 5) -> List[Dict]:
        """
        Get most recent completions

        Args:
            status_data: Status data from get_batch_status
            limit: Maximum number to return

        Returns:
            List of recently completed documents
        """
        completed = status_data.get("completed", [])

        # Sort by end_time (most recent first)
        sorted_completed = sorted(
            completed, key=lambda x: x.get("end_time", ""), reverse=True
        )

        return sorted_completed[:limit]

    def calculate_statistics(self, status_data: Dict) -> Dict:
        """
        Calculate batch statistics

        Args:
            status_data: Status data from get_batch_status

        Returns:
            Dictionary with statistics
        """
        total = status_data["total"]
        completed = len(status_data["completed"])
        failed = len(status_data["failed"])
        running = len(status_data["running"])
        queued = len(status_data["queued"])

        # Calculate average duration for completed documents
        durations = [
            doc.get("duration", 0)
            for doc in status_data["completed"]
            if doc.get("duration", 0) > 0
        ]

        avg_duration = sum(durations) / len(durations) if durations else 0

        # Calculate completion percentage
        finished = completed + failed
        completion_pct = (finished / total * 100) if total > 0 else 0

        # Calculate success rate
        success_rate = (completed / finished * 100) if finished > 0 else 0

        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "running": running,
            "queued": queued,
            "completion_percentage": completion_pct,
            "success_rate": success_rate,
            "avg_duration_seconds": avg_duration,
            "all_complete": status_data["all_complete"],
        }

    def get_failed_documents(self, status_data: Dict) -> List[Dict]:
        """
        Get list of failed documents with error details

        Args:
            status_data: Status data from get_batch_status

        Returns:
            List of failed documents with error information
        """
        failed = status_data.get("failed", [])

        result = []
        for doc in failed:
            # Check if document was aborted vs failed
            status = doc.get("status", "FAILED")
            if status == "ABORTED":
                error = "Aborted by user"
                failed_step = "N/A"
            else:
                error = doc.get("error", "Unknown error")
                failed_step = doc.get("failed_step", "Unknown")

            result.append(
                {
                    "document_id": doc["document_id"],
                    "error": error,
                    "failed_step": failed_step,
                }
            )

        return result
