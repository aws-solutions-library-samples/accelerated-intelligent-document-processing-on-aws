# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test Studio documents as queue items: summary metadata and assignment.

Every test set is a queue. Each of its documents has one item in
HITLTable, ``PK=TSDOC#<testSetId>``, ``SK=D#<objectKey>``, holding what the
queue page lists (label source, reviewed, alert count, lowest confidence, classes,
section count) and who the document is assigned to. The labels themselves stay in
S3; the summary is rewritten whenever they change, so a queue page is one Query.

An Admin assigns documents to Annotators; nobody claims one themselves. An
assignment lasts until the document is fully reviewed, its Annotator skips it
(``Skipped``: the task is complete, the labels are not reviewed), or an Admin
unassigns it.
While assigned, the item carries ``OwnerPK=RT#<annotator>`` (My Work › Tasks is one
OwnerIndex Query) and ``SweepPK=TS`` at the set's time limit, when the sweeper
sends one reminder.

Standard library and boto3 only, so the layer-1 resolvers can import it.
Notices never raise.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from . import keys

logger = logging.getLogger(__name__)

NOTICE_SOURCE = "test-studio"
#: Bumped when the summary shape changes, so every set is summarized again.
SUMMARY_VERSION = 1
SUMMARY_BUILDING = "BUILDING"
SUMMARY_READY = "READY"
#: A build requested longer ago than this without finishing is requested again.
SUMMARY_BUILD_STALE_SECONDS = 15 * 60
DEFAULT_TIME_LIMIT_MINUTES = 60
MIN_TIME_LIMIT_MINUTES = 1
MAX_TIME_LIMIT_MINUTES = 10080
LABEL_SOURCE_HUMAN = "reviewed-human"

_SUMMARY_FIELDS = (
    ("inputKey", "InputKey"),
    ("labelSource", "LabelSource"),
    ("minConfidence", "MinConfidence"),
    ("confidenceThreshold", "ConfidenceThreshold"),
    ("alertCount", "AlertCount"),
    ("fieldCount", "FieldCount"),
    ("documentClasses", "Classes"),
    ("sections", "Sections"),
    ("reviewedSections", "ReviewedSections"),
)


class TestSetAssignmentError(ValueError):
    """The caller may not work on this document (not assigned to them)."""


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_from_ms(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return (
        datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def time_limit_minutes(meta: Optional[dict]) -> int:
    """The set's reminder time limit in minutes, from its metadata item."""
    raw = (meta or {}).get("TimeLimitMinutes") or (meta or {}).get("ClaimLeaseMinutes")
    try:
        value = int(raw or DEFAULT_TIME_LIMIT_MINUTES)
    except (TypeError, ValueError):
        value = DEFAULT_TIME_LIMIT_MINUTES
    return max(MIN_TIME_LIMIT_MINUTES, min(MAX_TIME_LIMIT_MINUTES, value))


def validate_time_limit(value: Any) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise ValueError("timeLimitMinutes must be a whole number") from None
    if not MIN_TIME_LIMIT_MINUTES <= minutes <= MAX_TIME_LIMIT_MINUTES:
        raise ValueError(
            f"timeLimitMinutes must be {MIN_TIME_LIMIT_MINUTES}-{MAX_TIME_LIMIT_MINUTES}"
        )
    return minutes


def _reviewer_key(email: str) -> str:
    from idp_common.notifications.keys import normalize_recipient_key

    return normalize_recipient_key(email)


def _dynamo(value: Any) -> Any:
    """Floats become Decimal for the boto3 resource API."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: _dynamo(v) for k, v in value.items() if v is not None}
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _conditional_failed(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get(
        "Code"
    ) == "ConditionalCheckFailedException"


class TestSetDocStore:
    """Test Studio document items in HITLTable (a boto3 ``Table``)."""

    def __init__(self, table: Any):
        self.table = table

    # ----------------------------------------------------------------- reads

    def list(self, test_set_id: str) -> list[dict]:
        """Every document item of a set, as plain dicts (camelCase view)."""
        items: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": keys.tsdoc_pk(test_set_id)},
        }
        while True:
            page = self.table.query(**kwargs)
            items.extend(page.get("Items", []))
            if not page.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return [self.view(i) for i in items]

    def get(self, test_set_id: str, object_key: str) -> Optional[dict]:
        item = self.table.get_item(
            Key=keys.tsdoc_key(test_set_id, object_key), ConsistentRead=True
        ).get("Item")
        return self.view(item) if item else None

    def assigned_to(self, email: str) -> list[dict]:
        """Every Test Studio document assigned to ``email``, across all sets."""
        items: list[dict] = []
        kwargs: dict = {
            "IndexName": keys.OWNER_INDEX,
            "KeyConditionExpression": "OwnerPK = :pk",
            "ExpressionAttributeValues": {
                ":pk": keys.tsdoc_owner_pk(_reviewer_key(email))
            },
        }
        while True:
            page = self.table.query(**kwargs)
            items.extend(page.get("Items", []))
            if not page.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return [self.view(i) for i in items]

    @staticmethod
    def view(item: dict) -> dict:
        item = _plain(item)
        out = {
            "testSetId": item.get("TestSetId")
            or str(item.get("PK", ""))[len("TSDOC#") :],
            "objectKey": item.get("ObjectKey") or str(item.get("SK", ""))[2:],
            "reviewed": bool(item.get("Reviewed")),
            "skipped": bool(item.get("Skipped")),
            "assignedTo": item.get("Assignee") or None,
            "assignedAt": iso_from_ms(item.get("AssignedAtMs")),
            "assignedBy": item.get("AssignedBy") or None,
            "updatedAt": item.get("UpdatedAt"),
        }
        for camel, attr in _SUMMARY_FIELDS:
            out[camel] = item.get(attr)
        out["documentClasses"] = out.get("documentClasses") or []
        out["sections"] = out.get("sections") or []
        return out

    # ---------------------------------------------------------------- writes

    def upsert_summary(self, test_set_id: str, doc: dict) -> None:
        """Write a document's summary (from ``get_test_set_documents``), keeping
        its assignment."""
        object_key = doc["objectKey"]
        reviewed = doc.get("labelSource") == LABEL_SOURCE_HUMAN
        names = {
            "#u": "UpdatedAt",
            "#r": "Reviewed",
            "#t": "TestSetId",
            "#o": "ObjectKey",
        }
        values: dict[str, Any] = {
            ":u": iso_from_ms(now_ms()),
            ":r": reviewed,
            ":t": test_set_id,
            ":o": object_key,
        }
        sets = ["#u = :u", "#r = :r", "#t = :t", "#o = :o"]
        removes = []
        for i, (camel, attr) in enumerate(_SUMMARY_FIELDS):
            value = doc.get(camel)
            names[f"#f{i}"] = attr
            if value is None or value == []:
                removes.append(f"#f{i}")
            else:
                values[f":f{i}"] = _dynamo(value)
                sets.append(f"#f{i} = :f{i}")
        expression = "SET " + ", ".join(sets)
        if removes:
            expression += " REMOVE " + ", ".join(removes)
        if reviewed:
            # A fully reviewed document leaves its Annotator's Tasks.
            expression += (
                ", " if removes else " REMOVE "
            ) + "OwnerPK, OwnerSK, SweepPK, SweepSK, Assignee, AssigneeKey"
        self.table.update_item(
            Key=keys.tsdoc_key(test_set_id, object_key),
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def upsert_many(self, test_set_id: str, docs: Iterable[dict]) -> int:
        docs = list(docs)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda d: self.upsert_summary(test_set_id, d), docs))
        return len(docs)

    def sync_set(self, test_set_id: str, docs: Iterable[dict]) -> tuple[int, int]:
        """Make the set's items match ``docs`` (assignments kept, stale items
        deleted). Returns ``(documents, reviewed)``."""
        docs = list(docs)
        self.upsert_many(test_set_id, docs)
        present = {d["objectKey"] for d in docs}
        stale = [
            d["objectKey"]
            for d in self.list(test_set_id)
            if d["objectKey"] not in present
        ]
        if stale:
            self.delete(test_set_id, stale)
        reviewed = sum(1 for d in docs if d.get("labelSource") == LABEL_SOURCE_HUMAN)
        return len(docs), reviewed

    def delete(self, test_set_id: str, object_keys: Iterable[str]) -> None:
        with self.table.batch_writer() as batch:
            for key in object_keys:
                batch.delete_item(Key=keys.tsdoc_key(test_set_id, key))

    def delete_set(self, test_set_id: str) -> int:
        keys_ = [d["objectKey"] for d in self.list(test_set_id)]
        self.delete(test_set_id, keys_)
        return len(keys_)

    def assign(
        self,
        test_set_id: str,
        object_key: str,
        assignee_email: str,
        *,
        assigned_by: str,
        time_limit: int,
    ) -> dict:
        email = assignee_email.strip().lower()
        rk = _reviewer_key(email)
        at = now_ms()
        try:
            response = self.table.update_item(
                Key=keys.tsdoc_key(test_set_id, object_key),
                UpdateExpression=(
                    "SET Assignee = :e, AssigneeKey = :k, AssignedAtMs = :at, "
                    "AssignedBy = :by, OwnerPK = :opk, OwnerSK = :osk, "
                    "SweepPK = :spk, SweepSK = :ssk "
                    # Assigning a skipped document again reopens its task.
                    "REMOVE Skipped, SkippedBy, SkippedAtMs"
                ),
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={
                    ":e": email,
                    ":k": rk,
                    ":at": at,
                    ":by": assigned_by,
                    ":opk": keys.tsdoc_owner_pk(rk),
                    ":osk": f"{at:013d}#{keys.h16(test_set_id + '/' + object_key)}",
                    ":spk": keys.TSDOC_SWEEP_PK,
                    ":ssk": keys.sweep_sk(
                        at + time_limit * 60_000, test_set_id + "/" + object_key
                    ),
                },
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:  # noqa: BLE001
            if _conditional_failed(exc):
                raise ValueError(
                    f"Document '{object_key}' is not in this test set"
                ) from None
            raise
        return self.view(response["Attributes"])

    def unassign(self, test_set_id: str, object_key: str) -> Optional[dict]:
        """Clear the assignment; returns the former view, or None if unassigned."""
        try:
            response = self.table.update_item(
                Key=keys.tsdoc_key(test_set_id, object_key),
                UpdateExpression=(
                    "REMOVE Assignee, AssigneeKey, AssignedAtMs, AssignedBy, "
                    "OwnerPK, OwnerSK, SweepPK, SweepSK"
                ),
                ConditionExpression="attribute_exists(Assignee)",
                ReturnValues="ALL_OLD",
            )
        except Exception as exc:  # noqa: BLE001
            if _conditional_failed(exc):
                return None
            raise
        return self.view(response.get("Attributes") or {})

    def assert_assignee(self, test_set_id: str, object_key: str, caller: str) -> None:
        """Only the document's assignee may save it (Admins included: they assign)."""
        doc = self.get(test_set_id, object_key)
        assignee = (doc or {}).get("assignedTo")
        if not assignee:
            raise TestSetAssignmentError(
                "NOT_ASSIGNED: this document isn't assigned to you; ask an Admin to assign it"
            )
        if assignee != (caller or "").lower():
            raise TestSetAssignmentError(f"Document is assigned to {assignee}")

    def mark_reviewed(self, test_set_id: str, object_key: str) -> None:
        """The document is fully reviewed: record it and end the assignment."""
        self.table.update_item(
            Key=keys.tsdoc_key(test_set_id, object_key),
            UpdateExpression=(
                "SET Reviewed = :t, LabelSource = :h, UpdatedAt = :u REMOVE "
                "Assignee, AssigneeKey, AssignedAtMs, AssignedBy, OwnerPK, OwnerSK, "
                "SweepPK, SweepSK"
            ),
            ExpressionAttributeValues={
                ":t": True,
                ":h": LABEL_SOURCE_HUMAN,
                ":u": iso_from_ms(now_ms()),
            },
        )

    def mark_skipped(self, test_set_id: str, object_key: str, by: str) -> None:
        """The task is complete without a review: record who skipped it and end the
        assignment. The labels are left as they are."""
        try:
            self.table.update_item(
                Key=keys.tsdoc_key(test_set_id, object_key),
                UpdateExpression=(
                    "SET Skipped = :t, SkippedBy = :by, SkippedAtMs = :at, "
                    "UpdatedAt = :u REMOVE Assignee, AssigneeKey, AssignedAtMs, "
                    "AssignedBy, OwnerPK, OwnerSK, SweepPK, SweepSK"
                ),
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={
                    ":t": True,
                    ":by": (by or "").lower() or "unknown",
                    ":at": now_ms(),
                    ":u": iso_from_ms(now_ms()),
                },
            )
        except Exception as exc:  # noqa: BLE001
            if _conditional_failed(exc):
                raise ValueError(
                    f"Document '{object_key}' is not in this test set"
                ) from None
            raise

    def due_reminders(self, until_ms: int, *, limit: int = 100) -> list[dict]:
        """Assigned documents whose time limit has passed; their sweep is cleared."""
        response = self.table.query(
            IndexName=keys.SWEEP_INDEX,
            KeyConditionExpression="SweepPK = :pk AND SweepSK <= :until",
            ExpressionAttributeValues={
                ":pk": keys.TSDOC_SWEEP_PK,
                ":until": keys.sweep_until(until_ms),
            },
            Limit=limit,
        )
        out = []
        for item in response.get("Items", []):
            try:
                self.table.update_item(
                    Key={"PK": item["PK"], "SK": item["SK"]},
                    UpdateExpression="REMOVE SweepPK, SweepSK",
                    ConditionExpression="SweepSK = :s",
                    ExpressionAttributeValues={":s": item["SweepSK"]},
                )
            except Exception as exc:  # noqa: BLE001 — reassigned meanwhile
                if not _conditional_failed(exc):
                    raise
                continue
            out.append(self.view(item))
        return out


# ---------------------------------------------------------------- Inbox notices


def _doc_name(object_key: str) -> str:
    return object_key.rsplit("/", 1)[-1] or object_key


def item_link(test_set_id: str, object_key: str) -> str:
    """Byte parity with the UI's ``testSetItemPath``."""
    from idp_common.notifications.links import encode_uri_component

    return (
        f"/review-queues/test-sets/{encode_uri_component(test_set_id)}"
        f"/items/{encode_uri_component(object_key)}"
    )


def _post(**kwargs: Any) -> None:
    try:
        from idp_common.notifications import notify

        notify(source=NOTICE_SOURCE, **kwargs)
    except Exception:  # noqa: BLE001 — a notice must never fail the assignment
        logger.error("event=test_set_notice_failed", exc_info=True)


def notify_assigned(doc: dict, assigned_by: str) -> None:
    name = _doc_name(doc["objectKey"])
    _post(
        type_id="review.item_reassigned",
        title=name,
        body=(
            f"{assigned_by or 'An Admin'} assigned **{name}** in test set "
            f"**{doc['testSetId']}** to you. Annotate it from My Work › Tasks."
        ),
        link=item_link(doc["testSetId"], doc["objectKey"]),
        users=[doc["assignedTo"]],
        dedupe_key=f"test-set-task:{doc['testSetId']}:{doc['objectKey']}",
        idempotency_key=f"test-set-assigned:{doc['testSetId']}:{doc['objectKey']}:{doc.get('assignedAt')}",
    )


def notify_unassigned(doc: dict) -> None:
    if not doc.get("assignedTo"):
        return
    name = _doc_name(doc["objectKey"])
    _post(
        type_id="review.claim_released",
        title=f"{name}: unassigned",
        body=f"**{name}** in test set **{doc['testSetId']}** is no longer assigned to you.",
        link=item_link(doc["testSetId"], doc["objectKey"]),
        users=[doc["assignedTo"]],
        dedupe_key=f"test-set-task:{doc['testSetId']}:{doc['objectKey']}",
    )


def notify_reminder(doc: dict) -> None:
    if not doc.get("assignedTo"):
        return
    name = _doc_name(doc["objectKey"])
    _post(
        type_id="review.task_reminder",
        title=f"{name}: still to complete",
        body=(
            f"**{name}** in test set **{doc['testSetId']}** was assigned to you and "
            "is still open. Annotate and complete it from My Work › Tasks."
        ),
        link=item_link(doc["testSetId"], doc["objectKey"]),
        users=[doc["assignedTo"]],
        dedupe_key=f"test-set-task:{doc['testSetId']}:{doc['objectKey']}",
        idempotency_key=f"test-set-reminder:{doc['testSetId']}:{doc['objectKey']}:{doc.get('assignedAt')}",
    )


# ------------------------------------------------------- background summary builds


def _metadata_key(test_set_id: str) -> dict:
    return {"PK": f"testset#{test_set_id}", "SK": "metadata"}


def request_summaries(
    tracking_table: Any, sqs_client: Any, queue_url: str, test_set_id: str
) -> bool:
    """Ask the test-set file copier to (re)build a set's summaries in the background.

    Marks the set ``SummaryStatus=BUILDING`` first, conditionally, so many readers
    of a set without summaries send one message rather than one each; a build that
    has not finished within :data:`SUMMARY_BUILD_STALE_SECONDS` is requested again.
    ``SummaryVersion`` is removed, so readers know the summaries are not current.
    Returns True when a message was sent. Never raises.
    """
    now = int(time.time())
    try:
        tracking_table.update_item(
            Key=_metadata_key(test_set_id),
            UpdateExpression=(
                "SET SummaryStatus = :b, SummaryRequestedAt = :now REMOVE SummaryVersion"
            ),
            ConditionExpression=(
                "attribute_exists(PK) AND (attribute_not_exists(SummaryStatus) "
                "OR SummaryStatus <> :b OR SummaryRequestedAt < :stale)"
            ),
            ExpressionAttributeValues={
                ":b": SUMMARY_BUILDING,
                ":now": now,
                ":stale": now - SUMMARY_BUILD_STALE_SECONDS,
            },
        )
    except Exception as exc:  # noqa: BLE001
        if not _conditional_failed(exc):
            logger.warning(
                "event=test_set_summary_request_failed test_set=%s error=%s",
                test_set_id,
                type(exc).__name__,
            )
        return False
    try:
        sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps({"action": "summarize", "testSetId": test_set_id}),
        )
    except Exception:  # noqa: BLE001 — the stale window requests it again
        logger.error(
            "event=test_set_summary_request_failed test_set=%s",
            test_set_id,
            exc_info=True,
        )
        return False
    return True


def mark_summaries_ready(
    tracking_table: Any,
    test_set_id: str,
    documents: int,
    reviewed: int,
) -> None:
    """Record a finished build: the queue reads its documents from DynamoDB."""
    tracking_table.update_item(
        Key=_metadata_key(test_set_id),
        UpdateExpression=(
            "SET SummaryVersion = :v, SummaryStatus = :r, SummaryCount = :n, "
            "QueueReviewedDocs = :rev, QueueCountsAt = :at"
        ),
        ConditionExpression="attribute_exists(PK)",
        ExpressionAttributeValues={
            ":v": SUMMARY_VERSION,
            ":r": SUMMARY_READY,
            ":n": int(documents),
            ":rev": int(reviewed),
            ":at": iso_from_ms(now_ms()),
        },
    )


def assignment_counts(store: TestSetDocStore, test_set_id: str) -> dict[str, int]:
    """The set's queue counts from its document items (one Query, four attributes).

    Completed documents (reviewed or skipped) are in none of them.
    ``pending``: not completed and unassigned. ``assigned``: assigned, no section
    reviewed yet. ``in_progress``: assigned with some sections reviewed.
    ``annotators``: distinct assignees.
    """
    counts = {"pending": 0, "assigned": 0, "in_progress": 0}
    annotators: set[str] = set()
    kwargs: dict = {
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": keys.tsdoc_pk(test_set_id)},
        "ProjectionExpression": "Assignee, Reviewed, Skipped, ReviewedSections",
    }
    while True:
        page = store.table.query(**kwargs)
        for item in page.get("Items", []):
            if item.get("Reviewed") or item.get("Skipped"):
                continue
            assignee = item.get("Assignee")
            if not assignee:
                counts["pending"] += 1
            elif int(item.get("ReviewedSections") or 0) > 0:
                counts["in_progress"] += 1
                annotators.add(assignee)
            else:
                counts["assigned"] += 1
                annotators.add(assignee)
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    counts["annotators"] = len(annotators)
    return counts


def store_assigned_count(
    tracking_table: Any, store: TestSetDocStore, test_set_id: str
) -> None:
    """Recount the set's queue counts onto its metadata (``QueuePendingDocs``,
    ``QueueAssignedDocs``, ``QueueInProgressDocs``, ``QueueAnnotators``), so
    HITL › Queue Management lists every set from the metadata alone. Never raises."""
    try:
        counts = assignment_counts(store, test_set_id)
        tracking_table.update_item(
            Key=_metadata_key(test_set_id),
            UpdateExpression=(
                "SET QueuePendingDocs = :p, QueueAssignedDocs = :a, "
                "QueueInProgressDocs = :i, QueueAnnotators = :n"
            ),
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={
                ":p": counts["pending"],
                ":a": counts["assigned"],
                ":i": counts["in_progress"],
                ":n": counts["annotators"],
            },
        )
    except Exception:  # noqa: BLE001 — the next assignment or build recounts
        logger.warning(
            "event=test_set_assigned_count_failed test_set=%s",
            test_set_id,
            exc_info=True,
        )
