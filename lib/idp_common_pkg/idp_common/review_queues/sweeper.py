# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The review-queue sweeper: lease expiry and warnings, and self-healing (FR-11, FR-19).

Runs every 5 minutes (reserved concurrency 1). Per shard of ``SweepIndex`` it
visits items whose sweep time has passed:

=================  ==========================================  =================================
Item               Condition                                   Action
=================  ==========================================  =================================
CLAIMED            lease expired                               → OPEN, claim slot −1, notice
CLAIMED            within the warning lead, not yet warned     ``claim_expiring`` notice once
HOLDING            phase 1 SUCCEEDED (tracker event lost)      replay the event to the tracker
HOLDING            phase 1 failed/aborted/timed out            delete the item
HOLDING            phase 1 RUNNING                             recheck in 15 minutes
FINALIZING         finalize execution never started            resend the same token, backoff
FINALIZING         RUNNING                                     recheck in 10 minutes
FINALIZING         SUCCEEDED but still FINALIZING              replay the event to the tracker
FINALIZING         FAILED / TIMED_OUT                          new attempt (≤ 3), else FINALIZE_FAILED
FINALIZING         ABORTED                                     withdraw
any                tracking row missing                        withdraw
=================  ==========================================  =================================

Once per run it also refreshes each registry queue's member snapshot (read by the
Document List filter) and emits ``ReviewQueueDepth``. Every write is conditional
on the item Version; a condition failure means another writer won, so the item
is skipped.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from idp_common.dynamodb.client import DynamoDBTransactionCanceledError
from idp_common.utils.execution_names import execution_arn_for

from . import keys
from .finalize import finalize_execution_name, finalize_token
from .models import QueueRef, ReviewItemRecord, SweepReport
from .queue_defs import ASSIGNMENT_LEASE_MINUTES
from .statuses import QueueItemState
from .tracker import replay_event

logger = logging.getLogger(__name__)

HOLDING_RECHECK_MS = 15 * 60_000
FINALIZE_RECHECK_MS = 10 * 60_000
MAX_FINALIZE_ATTEMPTS = 3
DEADLINE_MARGIN_MS = 30_000
PAGE = 50

OPEN = QueueItemState.OPEN.value
CLAIMED = QueueItemState.CLAIMED.value
HOLDING = QueueItemState.HOLDING.value
FINALIZING = QueueItemState.FINALIZING.value


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _default_metric(
    name: str, value: float = 1, dimensions: Optional[list] = None
) -> None:
    try:
        from idp_common.metrics import put_metric

        put_metric(name, value, dimensions=dimensions)
    except Exception:  # noqa: BLE001 — a metric must not fail the sweep
        logger.debug("metric %s not published", name, exc_info=True)


class ReviewSweeper:
    def __init__(
        self,
        store: Any,
        *,
        sfn_client: Any,
        lambda_client: Any,
        tracker_function_name: str,
        state_machine_arn: str,
        finalize_sender: Any,
        document_service: Any,
        directory: Any,
        notifier: Any,
        s3: Any,
        output_bucket: str,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        metrics: Callable[..., None] = _default_metric,
        service_factory: Optional[Callable[[], Any]] = None,
    ):
        self.store = store
        self.sfn = sfn_client
        self.lambda_client = lambda_client
        self.tracker_function_name = tracker_function_name
        self.state_machine_arn = state_machine_arn
        self.finalize_sender = finalize_sender
        self.document_service = document_service
        self.directory = directory
        self.notify = notifier
        self.s3 = s3
        self.output_bucket = output_bucket
        self.clock = clock
        self.metric = metrics
        self._service_factory = service_factory

    # -------------------------------------------------------------- helpers

    def _now_ms(self) -> int:
        return int(self.clock().timestamp() * 1000)

    def _service(self):
        if self._service_factory is not None:
            return self._service_factory()
        from .service import ReviewQueueService

        return ReviewQueueService(
            self.store,
            self.directory,
            notifier=self.notify,
            finalize_sender=self.finalize_sender,
            clock=self.clock,
            s3=self.s3,
            output_bucket=self.output_bucket,
            document_service=self.document_service,
        )

    def _write(
        self, old: ReviewItemRecord, new: ReviewItemRecord, *, extra_txn=()
    ) -> bool:
        new.version = old.version + 1
        try:
            self.store.replace_item(
                new, expected_version=old.version, extra_txn=extra_txn
            )
            return True
        except DynamoDBTransactionCanceledError:
            logger.info("event=review_sweep_skipped reason=concurrent_writer")
            return False

    def _describe(self, arn: str) -> Optional[dict]:
        try:
            return self.sfn.describe_execution(executionArn=arn)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if (
                code == "ExecutionDoesNotExist"
                or type(exc).__name__ == "ExecutionDoesNotExist"
            ):
                return None
            raise

    def _replay(self, describe: dict) -> None:
        self.lambda_client.invoke(
            FunctionName=self.tracker_function_name,
            InvocationType="Event",
            Payload=json.dumps(replay_event(describe), default=str).encode("utf-8"),
        )

    # ----------------------------------------------------------------- run

    def run(self, *, deadline_ms: int, max_items: int = 500) -> SweepReport:
        report = SweepReport()
        stop_at = deadline_ms - DEADLINE_MARGIN_MS
        now_ms = self._now_ms()
        for shard in range(keys.SWEEP_SHARDS):
            start = None
            while True:
                if report.visited >= max_items or self._now_ms() >= stop_at:
                    report.backlog += self._remaining(shard, now_ms, start)
                    break
                items, start = self.store.query_sweep(
                    shard, now_ms, limit=PAGE, start_key=start
                )
                for item in items:
                    if report.visited >= max_items or self._now_ms() >= stop_at:
                        report.backlog += 1
                        continue
                    report.visited += 1
                    try:
                        self._visit(item, report)
                    except Exception:  # noqa: BLE001 — one item never stops the sweep
                        logger.error(
                            "event=review_sweep_item_failed state=%s",
                            item.state,
                            exc_info=True,
                        )
                if not start:
                    break
        if self._now_ms() < stop_at:
            self._remind_test_set_assignments(now_ms, report)
        if self._now_ms() < stop_at:
            self._refresh_registry(report)
        self.metric("ReviewSweepBacklog", report.backlog)
        logger.info("event=review_sweep_done %s", json.dumps(report.model_dump()))
        return report

    def _remaining(self, shard: int, now_ms: int, start: Optional[dict]) -> int:
        try:
            count = 0
            while True:
                items, start = self.store.query_sweep(
                    shard, now_ms, limit=PAGE, start_key=start
                )
                count += len(items)
                if not start or count > 1000:
                    return count
        except Exception:  # noqa: BLE001
            return 0

    def _visit(self, item: ReviewItemRecord, report: SweepReport) -> None:
        if (
            self.document_service is not None
            and self.document_service.get_document(item.doc_key) is None
        ):
            if self._service().withdraw(item.doc_key, "document_missing"):
                report.withdrawn += 1
            return
        if item.state == CLAIMED:
            self._visit_claimed(item, report)
        elif item.state == HOLDING:
            self._visit_holding(item, report)
        elif item.state == FINALIZING:
            self._visit_finalizing(item, report)

    def _visit_claimed(self, item: ReviewItemRecord, report: SweepReport) -> None:
        now = self.clock()
        now_ms = int(now.timestamp() * 1000)
        lease = item.lease_expires_at_ms or 0
        if (item.lease_minutes or 0) >= ASSIGNMENT_LEASE_MINUTES:
            # An Admin assignment: it never expires. Remind the Annotator once when
            # the queue's time limit has passed, then stop sweeping it.
            new = item.model_copy(deep=True)
            new.sweep_at_ms = None
            if self._write(item, new):
                report.warned += 1
                self.notify.task_reminder(item)
            return
        if lease <= now_ms:
            new = item.model_copy(deep=True)
            owner_key, owner_email = item.owner_key, item.owner_email
            new.state = OPEN
            new.owner_key = None
            new.owner_email = None
            new.claimed_at = None
            new.claimed_at_ms = None
            new.lease_expires_at_ms = None
            new.lease_warned_for_ms = None
            new.sweep_at_ms = None
            extra = []
            if owner_key:
                extra.append(
                    self.store.counter_txn_item(owner_key, -1, now_iso=_iso(now))
                )
            if new.history_count < 200:
                new.history_count += 1
                extra.append(
                    self.store.history_txn_item(
                        item.doc_key,
                        item.cycle_id,
                        "lease_expired",
                        None,
                        {"owner": owner_email},
                        item.expires_after,
                        now_ms=now_ms,
                        now_iso=_iso(now),
                    )
                )
            else:
                new.history_truncated = True
            if self._write(item, new, extra_txn=extra):
                report.expired += 1
                self.metric("ReviewLeaseExpiries", 1)
                self.notify.claim_expired(item)
            return
        if item.lease_warned_for_ms != lease:
            new = item.model_copy(deep=True)
            new.lease_warned_for_ms = lease
            new.sweep_at_ms = lease
            if self._write(item, new):
                report.warned += 1
                self.notify.claim_expiring(item)
            return
        # Already warned: look again when the lease runs out.
        new = item.model_copy(deep=True)
        new.sweep_at_ms = lease
        self._write(item, new)

    def _reschedule(self, item: ReviewItemRecord, delay_ms: int) -> None:
        new = item.model_copy(deep=True)
        new.sweep_at_ms = self._now_ms() + delay_ms
        self._write(item, new)

    def _visit_holding(self, item: ReviewItemRecord, report: SweepReport) -> None:
        describe = (
            self._describe(item.phase1_execution_arn)
            if item.phase1_execution_arn
            else None
        )
        status = (describe or {}).get("status")
        if status == "RUNNING":
            self._reschedule(item, HOLDING_RECHECK_MS)
            return
        if status == "SUCCEEDED":
            # Reschedule first: the replayed tracker event re-reads the item, so
            # its activation must see this write, not race it.
            self._reschedule(item, HOLDING_RECHECK_MS)
            self._replay(describe)
            report.holds_healed += 1
            self.metric("ReviewHoldsHealed", 1)
            return
        # Never started, failed, aborted or timed out: nothing will activate it.
        try:
            self.store.delete_item(item.doc_key, expected_version=item.version)
            report.holds_reaped += 1
            self.metric("ReviewHoldsReaped", 1)
        except Exception:  # noqa: BLE001 — another writer moved it on
            logger.info("event=review_sweep_skipped reason=hold_changed")

    def _visit_finalizing(self, item: ReviewItemRecord, report: SweepReport) -> None:
        name = item.finalize_execution_name or ""
        describe = (
            self._describe(execution_arn_for(self.state_machine_arn, name))
            if name
            else None
        )
        status = (describe or {}).get("status")
        if describe is None:
            # Never started (message lost, or still waiting in the DocumentQueue):
            # resend the SAME token; executions collapse by name.
            try:
                self.finalize_sender.send(item)
            except Exception:  # noqa: BLE001
                logger.error("event=review_finalize_resend_failed", exc_info=True)
            new = item.model_copy(deep=True)
            new.finalize_resend_count = item.finalize_resend_count + 1
            backoff_min = min(10 * 2**item.finalize_resend_count, 120)
            new.sweep_at_ms = self._now_ms() + backoff_min * 60_000
            if self._write(item, new):
                report.finalize_resent += 1
                self.metric("ReviewFinalizeResent", 1)
            return
        if status == "RUNNING":
            self._reschedule(item, FINALIZE_RECHECK_MS)
            return
        if status == "SUCCEEDED":
            self._reschedule(item, FINALIZE_RECHECK_MS)
            self._replay(describe)
            report.finalize_healed += 1
            self.metric("ReviewFinalizeHealed", 1)
            return
        if status == "ABORTED":
            if self._service().withdraw(item.doc_key, "aborted"):
                report.withdrawn += 1
            return
        # FAILED or TIMED_OUT: a new attempt with a new token, or give up.
        new = item.model_copy(deep=True)
        if item.finalize_attempt >= MAX_FINALIZE_ATTEMPTS:
            new.state = QueueItemState.FINALIZE_FAILED.value
            new.sweep_at_ms = None
            if self._write(item, new):
                report.finalize_failed += 1
                self.metric("ReviewFinalizeFailed", 1)
                logger.error(
                    "event=review_finalize_failed attempts=%d", item.finalize_attempt
                )
            return
        new.finalize_attempt = item.finalize_attempt + 1
        new.finalize_token = finalize_token(
            item.doc_key, item.cycle_id, new.finalize_attempt
        )
        new.finalize_execution_name = finalize_execution_name(
            item.doc_key, new.finalize_token
        )
        new.finalize_resend_count = 0
        new.sweep_at_ms = self._now_ms() + FINALIZE_RECHECK_MS
        if self._write(item, new):
            report.finalize_retried += 1
            self.metric("ReviewFinalizeRetried", 1)
            try:
                self.finalize_sender.send(new)
            except Exception:  # noqa: BLE001 — the next sweep resends
                logger.error("event=review_finalize_send_failed", exc_info=True)

    def _remind_test_set_assignments(self, now_ms: int, report: SweepReport) -> None:
        """Remind Annotators of Test Studio documents still open past the set's
        time limit (one reminder per assignment; nothing expires)."""
        try:
            import boto3

            from .test_set_docs import TestSetDocStore, notify_reminder

            table = boto3.resource("dynamodb").Table(self.store.table_name)
            for doc in TestSetDocStore(table).due_reminders(now_ms):
                report.warned += 1
                notify_reminder(doc)
        except Exception:  # noqa: BLE001 — reminders never stop the sweep
            logger.warning("event=test_set_reminders_failed", exc_info=True)

    def _refresh_registry(self, report: SweepReport) -> None:
        now_iso = _iso(self.clock())
        try:
            rows = self.store.list_registry()
        except Exception:  # noqa: BLE001
            logger.warning("event=review_registry_list_failed", exc_info=True)
            return
        for row in rows:
            partition = str(row.get("SK", ""))
            try:
                ref = QueueRef.from_partition(partition)
            except ValueError:
                continue
            try:
                groups, emails = self.directory.members(ref.profile, ref.queue)
            except Exception:  # noqa: BLE001 — keep the snapshot when config is unreadable
                logger.warning("event=review_registry_refresh_skipped", exc_info=True)
                continue
            if sorted(groups) != sorted(row.get("MemberGroups") or []) or sorted(
                e.lower() for e in emails
            ) != sorted(str(e).lower() for e in (row.get("MemberEmails") or [])):
                self.store.put_registry(
                    ref, member_groups=groups, member_emails=emails, now_iso=now_iso
                )
                report.registry_refreshed += 1
            try:
                depth = self.store.count_queue(ref, open_only=False)
                self.metric(
                    "ReviewQueueDepth",
                    depth,
                    dimensions=[
                        {"Name": "Profile", "Value": ref.profile},
                        {"Name": "Queue", "Value": ref.queue},
                    ],
                )
            except Exception:  # noqa: BLE001
                logger.debug("depth metric skipped", exc_info=True)
