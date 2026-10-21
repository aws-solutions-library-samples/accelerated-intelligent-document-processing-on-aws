# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""All DynamoDB access for review queues, through ``DynamoDBClient``.

Queue items are written whole: :meth:`ReviewStore.replace_item` puts the full
record under an optimistic ``Version`` condition and derives the sparse GSI keys
from its state (:func:`index_attributes`), so a state and its index placement
can't disagree. Drafts, history, counters and the registry are separate items in
the same table (see :mod:`.keys`).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Mapping, Optional, Sequence

from idp_common.dynamodb.client import (
    CONDITIONAL_CHECK_FAILED,
    DynamoDBClient,
    DynamoDBError,
)

from . import keys
from .models import DraftRecord, HistoryEntry, QueueRef, ReviewItemRecord, to_dynamo
from .queue_defs import QueueDefStore
from .statuses import TERMINAL_ITEM_STATES, QueueItemState

logger = logging.getLogger(__name__)

#: History entries kept per cycle; further entries are dropped (HistoryTruncated).
MAX_HISTORY_PER_CYCLE = 200

_INDEX_ATTRIBUTES = (
    "QueuePK",
    "QueueSK",
    "OpenPK",
    "OwnerPK",
    "OwnerSK",
    "SweepPK",
    "SweepSK",
)
_NAMES = {"#st": "State", "#v": "Version"}


def index_attributes(record: ReviewItemRecord) -> dict[str, str]:
    """The GSI key attributes an item carries in its current state."""
    state = record.state
    out: dict[str, str] = {}
    if state in (QueueItemState.OPEN.value, QueueItemState.CLAIMED.value):
        out["QueuePK"] = record.ref.partition
        out["QueueSK"] = keys.queue_sk(record.enqueued_at_ms, record.doc_key)
    if state == QueueItemState.OPEN.value:
        out["OpenPK"] = record.ref.partition
    if state == QueueItemState.CLAIMED.value and record.owner_key:
        out["OwnerPK"] = keys.owner_pk(record.owner_key)
        out["OwnerSK"] = keys.owner_sk(
            record.claimed_at_ms or record.enqueued_at_ms, record.doc_key
        )
    if (
        state
        in (
            QueueItemState.HOLDING.value,
            QueueItemState.CLAIMED.value,
            QueueItemState.FINALIZING.value,
        )
        and record.sweep_at_ms is not None
    ):
        out.update(keys.sweep_keys(record.sweep_at_ms, record.doc_key))
    return out


def item_to_dynamo(record: ReviewItemRecord) -> dict[str, Any]:
    item = keys.item_key(record.doc_key)
    item.update(record.to_attributes())
    item.update(index_attributes(record))
    return item


def is_condition_failure(exc: Exception) -> bool:
    return isinstance(exc, DynamoDBError) and exc.error_code == CONDITIONAL_CHECK_FAILED


class ReviewStore:
    """Data access for the HITLTable."""

    def __init__(
        self,
        table_name: str,
        region: Optional[str] = None,
        client: Optional[DynamoDBClient] = None,
        client_factory: Optional[Callable[[], DynamoDBClient]] = None,
    ):
        self.table_name = table_name
        self._client_factory = client_factory or (
            lambda: DynamoDBClient(table_name=table_name, region=region)
        )
        self._client = client
        self._lock = threading.Lock()
        self._local = threading.local()
        self._queue_defs: Optional[QueueDefStore] = None

    @property
    def queue_defs(self) -> QueueDefStore:
        """The system-wide queue definitions in this table (cached briefly)."""
        if self._queue_defs is None:
            with self._lock:
                if self._queue_defs is None:
                    self._queue_defs = QueueDefStore(lambda: self.client)
        return self._queue_defs

    @property
    def client(self) -> DynamoDBClient:
        """Built on first use, never at construction (no client at import)."""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = self._client_factory()
        return self._client

    def thread_client(self) -> DynamoDBClient:
        """A client owned by the calling thread (boto3 resources aren't thread-safe)."""
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._client_factory()
            self._local.client = client
        return client

    # ------------------------------------------------------------------ items

    def get_item(
        self, doc_key: str, *, client: Optional[DynamoDBClient] = None
    ) -> Optional[ReviewItemRecord]:
        raw = (client or self.client).get_item(
            keys.item_key(doc_key), consistent_read=True
        )
        return None if raw is None else ReviewItemRecord.from_item(raw)

    def put_holding(
        self,
        record: ReviewItemRecord,
        *,
        supersede: Optional[ReviewItemRecord] = None,
        history: Optional[dict[str, Any]] = None,
    ) -> None:
        """Write a HOLDING item for a phase-1 execution (D14).

        Without ``supersede``: create, or overwrite an item of the same
        execution (retry) or a terminal one. With ``supersede``: replace the old
        live cycle in one transaction, releasing its owner's claim slot.
        """
        item = item_to_dynamo(record)
        if supersede is None:
            values: dict[str, Any] = {":arn": record.phase1_execution_arn or ""}
            terminal = sorted(s.value for s in TERMINAL_ITEM_STATES)
            for i, state in enumerate(terminal):
                values[f":t{i}"] = state
            in_list = ", ".join(f":t{i}" for i in range(len(terminal)))
            self.client.put_item(
                item,
                condition_expression=(
                    "attribute_not_exists(PK) OR Phase1ExecutionArn = :arn "
                    f"OR #st IN ({in_list})"
                ),
                expression_attribute_names={"#st": "State"},
                expression_attribute_values=values,
            )
            return
        txn: list[dict[str, Any]] = [
            {
                "Put": {
                    "Item": item,
                    "ConditionExpression": "#v = :old",
                    "ExpressionAttributeNames": {"#v": "Version"},
                    "ExpressionAttributeValues": {":old": supersede.version},
                }
            }
        ]
        if supersede.state == QueueItemState.CLAIMED.value and supersede.owner_key:
            txn.append(
                self.counter_txn_item(
                    supersede.owner_key, -1, now_iso=record.enqueued_at
                )
            )
        if history:
            txn.append(history)
        self.client.transact_write_items(txn)

    def replace_txn_item(
        self,
        record: ReviewItemRecord,
        *,
        expected_version: int,
        condition: Optional[str] = None,
        values: Optional[Mapping[str, Any]] = None,
        names: Optional[Mapping[str, str]] = None,
    ) -> dict[str, Any]:
        """A transact ``Put`` of the whole item, conditional on its Version."""
        expr = "#v = :expected"
        if condition:
            expr = f"{expr} AND ({condition})"
        all_values = {":expected": expected_version, **to_dynamo(dict(values or {}))}
        all_names = {**_NAMES, **dict(names or {})}
        # Only names the expression uses may be sent.
        used_names = {k: v for k, v in all_names.items() if k in expr}
        return {
            "Put": {
                "Item": item_to_dynamo(record),
                "ConditionExpression": expr,
                "ExpressionAttributeNames": used_names,
                "ExpressionAttributeValues": all_values,
            }
        }

    def replace_item(
        self,
        record: ReviewItemRecord,
        *,
        expected_version: int,
        condition: Optional[str] = None,
        values: Optional[Mapping[str, Any]] = None,
        names: Optional[Mapping[str, str]] = None,
        extra_txn: Sequence[dict[str, Any]] = (),
        client: Optional[DynamoDBClient] = None,
    ) -> None:
        """Replace the item (``record.version`` must already be the new value).

        Index 0 of the transaction is always the item, so callers read
        ``reason_code(0)`` for the item's condition and ``reason_code(1..)`` for
        ``extra_txn``.
        """
        txn = [
            self.replace_txn_item(
                record,
                expected_version=expected_version,
                condition=condition,
                values=values,
                names=names,
            ),
            *extra_txn,
        ]
        (client or self.client).transact_write_items(txn)

    def transition(
        self,
        doc_key: str,
        *,
        expected_version: int,
        set_: Mapping[str, Any],
        remove: Sequence[str] = (),
        condition: Optional[str] = None,
        values: Optional[Mapping[str, Any]] = None,
        extra_txn: Sequence[dict[str, Any]] = (),
    ) -> None:
        """Targeted update of some attributes (Version + 1), in one transaction."""
        names: dict[str, str] = {"#v": "Version"}
        vals: dict[str, Any] = {
            ":expected": expected_version,
            ":next": expected_version + 1,
        }
        sets = ["#v = :next"]
        for i, (name, value) in enumerate(set_.items()):
            names[f"#s{i}"] = name
            vals[f":s{i}"] = value
            sets.append(f"#s{i} = :s{i}")
        expr = "SET " + ", ".join(sets)
        if remove:
            for i, name in enumerate(remove):
                names[f"#r{i}"] = name
            expr += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(remove)))
        cond = "#v = :expected"
        if condition:
            cond = f"{cond} AND ({condition})"
            if "#st" in condition:
                names["#st"] = "State"
        vals.update(dict(values or {}))
        txn = [
            {
                "Update": {
                    "Key": keys.item_key(doc_key),
                    "UpdateExpression": expr,
                    "ConditionExpression": cond,
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": to_dynamo(vals),
                }
            },
            *extra_txn,
        ]
        self.client.transact_write_items(txn)

    def delete_item(self, doc_key: str, *, expected_version: int) -> None:
        self.client.delete_item(
            keys.item_key(doc_key),
            condition_expression="#v = :v",
            expression_attribute_names={"#v": "Version"},
            expression_attribute_values={":v": expected_version},
        )

    # ---------------------------------------------------------------- queries

    def _query(
        self,
        index: str,
        hash_name: str,
        hash_value: str,
        *,
        limit: Optional[int],
        start_key: Optional[dict[str, Any]],
        range_condition: str = "",
        values: Optional[dict[str, Any]] = None,
        client: Optional[DynamoDBClient] = None,
    ) -> tuple[list[ReviewItemRecord], Optional[dict[str, Any]]]:
        vals = {":h": hash_value, **(values or {})}
        response = (client or self.client).query(
            f"{hash_name} = :h{range_condition}",
            None,
            vals,
            limit,
            start_key,
            index_name=index,
            scan_index_forward=True,
        )
        records = [ReviewItemRecord.from_item(i) for i in response.get("Items", [])]
        return records, response.get("LastEvaluatedKey")

    def query_queue(
        self,
        ref: QueueRef,
        *,
        open_only: bool,
        limit: int,
        start_key: Optional[dict[str, Any]] = None,
        client: Optional[DynamoDBClient] = None,
    ) -> tuple[list[ReviewItemRecord], Optional[dict[str, Any]]]:
        """Items of a queue, oldest first (OPEN only, or OPEN and CLAIMED)."""
        if open_only:
            return self._query(
                keys.OPEN_INDEX,
                "OpenPK",
                ref.partition,
                limit=limit,
                start_key=start_key,
                client=client,
            )
        return self._query(
            keys.QUEUE_INDEX,
            "QueuePK",
            ref.partition,
            limit=limit,
            start_key=start_key,
            client=client,
        )

    def count_queue(
        self, ref: QueueRef, *, open_only: bool, client: Optional[DynamoDBClient] = None
    ) -> int:
        index, name = (
            (keys.OPEN_INDEX, "OpenPK") if open_only else (keys.QUEUE_INDEX, "QueuePK")
        )
        return (client or self.client).query_count(
            f"{name} = :h",
            expression_attribute_values={":h": ref.partition},
            index_name=index,
        )

    def query_owner(
        self,
        reviewer_key: str,
        *,
        limit: Optional[int] = None,
        start_key: Optional[dict[str, Any]] = None,
        client: Optional[DynamoDBClient] = None,
    ) -> tuple[list[ReviewItemRecord], Optional[dict[str, Any]]]:
        return self._query(
            keys.OWNER_INDEX,
            "OwnerPK",
            keys.owner_pk(reviewer_key),
            limit=limit,
            start_key=start_key,
            client=client,
        )

    def all_owned(
        self, reviewer_key: str, *, client: Optional[DynamoDBClient] = None
    ) -> list[ReviewItemRecord]:
        out: list[ReviewItemRecord] = []
        start = None
        while True:
            page, start = self.query_owner(
                reviewer_key, limit=100, start_key=start, client=client
            )
            out.extend(page)
            if not start:
                return out

    def count_owner(self, reviewer_key: str) -> int:
        return self.client.query_count(
            "OwnerPK = :h",
            expression_attribute_values={":h": keys.owner_pk(reviewer_key)},
            index_name=keys.OWNER_INDEX,
        )

    def query_sweep(
        self,
        shard: int,
        until_ms: int,
        *,
        limit: int,
        start_key: Optional[dict[str, Any]] = None,
    ) -> tuple[list[ReviewItemRecord], Optional[dict[str, Any]]]:
        return self._query(
            keys.SWEEP_INDEX,
            "SweepPK",
            keys.sweep_pk(shard),
            limit=limit,
            start_key=start_key,
            range_condition=" AND SweepSK <= :until",
            values={":until": keys.sweep_until(until_ms)},
        )

    def in_flight(self, queue: str, states: Sequence[str]) -> list[ReviewItemRecord]:
        """A queue's items in ``states`` among HOLDING, CLAIMED and FINALIZING.

        Those states carry sweep keys, so every sweep shard is read in full and
        filtered by queue; only in-flight items are on that index.
        """
        wanted = set(states)
        out: list[ReviewItemRecord] = []
        for shard in range(keys.SWEEP_SHARDS):
            start: Optional[dict[str, Any]] = None
            while True:
                page, start = self.query_sweep(
                    shard, 10**13 - 1, limit=100, start_key=start
                )
                out.extend(r for r in page if r.queue == queue and r.state in wanted)
                if not start:
                    break
        return out

    # --------------------------------------------------------------- counters

    def counter(self, reviewer_key: str) -> int:
        raw = self.client.get_item(keys.counter_key(reviewer_key), consistent_read=True)
        return int((raw or {}).get("ActiveClaims", 0))

    def counter_txn_item(
        self,
        reviewer_key: str,
        delta: int,
        *,
        cap: Optional[int] = None,
        now_iso: str,
    ) -> dict[str, Any]:
        update: dict[str, Any] = {
            "Key": keys.counter_key(reviewer_key),
            "UpdateExpression": "SET UpdatedAt = :now ADD ActiveClaims :d",
            "ExpressionAttributeValues": {":now": now_iso, ":d": delta},
        }
        if cap is not None:
            update["ConditionExpression"] = (
                "attribute_not_exists(ActiveClaims) OR ActiveClaims < :cap"
            )
            update["ExpressionAttributeValues"][":cap"] = cap
        return {"Update": update}

    def decrement_counter(self, reviewer_key: str, *, now_iso: str) -> None:
        self.client.update_item(
            keys.counter_key(reviewer_key),
            "SET UpdatedAt = :now ADD ActiveClaims :d",
            expression_attribute_values={":now": now_iso, ":d": -1},
            return_values="NONE",
        )

    def repair_counter(
        self, reviewer_key: str, actual: int, *, idle_before_iso: str, now_iso: str
    ) -> bool:
        """Lower the counter to ``actual`` if it is higher and idle (never raises it)."""
        try:
            self.client.update_item(
                keys.counter_key(reviewer_key),
                "SET ActiveClaims = :actual, UpdatedAt = :now",
                expression_attribute_values={
                    ":actual": actual,
                    ":now": now_iso,
                    ":idle": idle_before_iso,
                },
                condition_expression="ActiveClaims > :actual AND UpdatedAt < :idle",
                return_values="NONE",
            )
            return True
        except DynamoDBError as exc:
            if is_condition_failure(exc):
                return False
            raise

    # --------------------------------------------------------------- registry

    def put_registry(
        self,
        ref: QueueRef,
        *,
        member_groups: Sequence[str],
        member_emails: Sequence[str],
        now_iso: str,
    ) -> None:
        self.client.update_item(
            keys.registry_key(ref.queue),
            "SET FirstSeenAt = if_not_exists(FirstSeenAt, :now), LastSeenAt = :now, "
            "MemberGroups = :g, MemberEmails = :e, MembersUpdatedAt = :now, "
            "QueueProfile = :p, QueueName = :q",
            expression_attribute_values={
                ":now": now_iso,
                ":g": list(member_groups),
                ":e": list(member_emails),
                ":p": ref.profile,
                ":q": ref.queue,
            },
            return_values="NONE",
        )

    def list_registry(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in self.client.query_pages(
            "PK = :pk", expression_attribute_values={":pk": keys.REGISTRY_PK}
        ):
            out.extend(page.get("Items", []))
        return out

    def try_announce(
        self, ref: QueueRef, now_ms: int, *, debounce_ms: int = 600_000
    ) -> bool:
        try:
            self.client.put_item(
                {
                    **keys.announce_key(ref.queue),
                    "LastAnnouncedAtMs": now_ms,
                },
                condition_expression="attribute_not_exists(PK) OR LastAnnouncedAtMs < :cutoff",
                expression_attribute_values={":cutoff": now_ms - debounce_ms},
            )
            return True
        except DynamoDBError as exc:
            if is_condition_failure(exc):
                return False
            raise

    # ----------------------------------------------------------------- drafts

    def get_draft(
        self, doc_key: str, cycle_id: str, section_id: str
    ) -> Optional[DraftRecord]:
        raw = self.client.get_item(
            {"PK": keys.doc_pk(doc_key), "SK": keys.draft_sk(cycle_id, section_id)},
            consistent_read=True,
        )
        return None if raw is None else DraftRecord.from_item(raw)

    def draft_txn_item(
        self, draft: DraftRecord, *, expected_revision: Optional[int]
    ) -> dict[str, Any]:
        item = {
            "PK": keys.doc_pk(draft.doc_key),
            "SK": keys.draft_sk(draft.cycle_id, draft.section_id),
            **draft.to_attributes(),
        }
        if expected_revision is None:
            return {
                "Put": {"Item": item, "ConditionExpression": "attribute_not_exists(PK)"}
            }
        return {
            "Put": {
                "Item": item,
                "ConditionExpression": "Revision = :r",
                "ExpressionAttributeValues": {":r": expected_revision},
            }
        }

    def list_drafts(self, doc_key: str, cycle_id: str) -> list[DraftRecord]:
        out: list[DraftRecord] = []
        for page in self.client.query_pages(
            "PK = :pk AND begins_with(SK, :p)",
            expression_attribute_values={
                ":pk": keys.doc_pk(doc_key),
                ":p": keys.draft_prefix(cycle_id),
            },
            consistent_read=True,
        ):
            out.extend(DraftRecord.from_item(i) for i in page.get("Items", []))
        return out

    # ---------------------------------------------------------------- history

    def history_txn_item(
        self,
        doc_key: str,
        cycle_id: str,
        event: str,
        actor: Optional[str],
        details: Mapping[str, Any],
        expires_after: Optional[int],
        *,
        now_ms: int,
        now_iso: str,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "PK": keys.doc_pk(doc_key),
            "SK": keys.history_sk(cycle_id, now_ms),
            "At": now_iso,
            "Event": event,
            "Details": dict(details),
        }
        if actor:
            item["Actor"] = actor
        if expires_after:
            item["ExpiresAfter"] = expires_after
        return {"Put": {"Item": to_dynamo(item)}}

    def list_history(
        self, doc_key: str, cycle_id: str, limit: int = 200
    ) -> list[HistoryEntry]:
        response = self.client.query(
            "PK = :pk AND begins_with(SK, :p)",
            None,
            {":pk": keys.doc_pk(doc_key), ":p": keys.history_prefix(cycle_id)},
            limit,
            scan_index_forward=True,
        )
        return [HistoryEntry.from_item(i) for i in response.get("Items", [])]

    # ------------------------------------------------------------------ purge

    def purge_document(self, doc_key: str, *, now_iso: str) -> int:
        """Delete every item of the document; release a CLAIMED owner's slot first."""
        record = self.get_item(doc_key)
        if record and record.state == QueueItemState.CLAIMED.value and record.owner_key:
            self.decrement_counter(record.owner_key, now_iso=now_iso)
        delete_keys: list[dict[str, Any]] = []
        for page in self.client.query_pages(
            "PK = :pk",
            expression_attribute_values={":pk": keys.doc_pk(doc_key)},
            projection_expression="PK, SK",
        ):
            delete_keys.extend(
                {"PK": i["PK"], "SK": i["SK"]} for i in page.get("Items", [])
            )
        if delete_keys:
            self.client.batch_write_items(delete_keys=delete_keys)
        return len(delete_keys)


def create_review_store(
    table_name: Optional[str] = None, region: Optional[str] = None
) -> Optional[ReviewStore]:
    """The store for ``HITL_TABLE_NAME``, or ``None`` when it isn't set."""
    name = (
        table_name if table_name is not None else os.environ.get("HITL_TABLE_NAME", "")
    )
    if not name:
        logger.debug("HITL_TABLE_NAME is not set; review queues are unavailable")
        return None
    return ReviewStore(name, region=region)
