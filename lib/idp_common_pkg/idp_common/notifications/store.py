# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""All DynamoDB access for the Inbox, through ``idp_common.dynamodb.DynamoDBClient``.

Table layout (one partition per recipient, ``PK = USER#<recipient key>``):

* ``N#<id>``: a message. ``UnreadPK`` (= PK) exists only while it is unread,
  which is what puts it in the sparse ``UnreadIndex``.
* ``D#<hash>``: dedupe pointer naming the message that currently holds a key.
* ``I#<hash>``: idempotency marker for one (event, recipient).
* ``PREFS``: the recipient's muted types.
"""

import logging
import threading
from typing import Any, Callable, Iterable, Sequence

from idp_common.dynamodb.client import (
    CONDITIONAL_CHECK_FAILED,
    DynamoDBClient,
    DynamoDBError,
)
from idp_common.notifications.keys import (
    MESSAGE_PREFIX,
    PARTITION_PREFIX,
    PREFS_SK,
    message_sk,
    recipient_hash,
)
from idp_common.notifications.models import InboxMessage, TrimReport

logger = logging.getLogger(__name__)

UNREAD_INDEX = "UnreadIndex"

_NAMES_EXP = {"#exp": "ExpiresAfter"}
_NAMES_READ = {"#readAt": "ReadAt"}


def to_inbox_message(item: dict[str, Any]) -> InboxMessage:
    """Map a stored message item to the API model."""
    return InboxMessage(
        id=str(item["SK"])[len(MESSAGE_PREFIX) :],
        type=str(item.get("Type", "")),
        title=str(item.get("Title", "")),
        body=str(item.get("Body", "")),
        severity=item.get("Severity", "info"),
        link=item.get("Link"),
        source=str(item.get("Source", "")),
        created_at=str(item.get("CreatedAt", "")),
        read="ReadAt" in item,
        read_at=item.get("ReadAt"),
        metadata=item.get("MetadataJson"),
    )


def _in_clause(prefix: str, values: Sequence[str], out: dict[str, Any]) -> str:
    placeholders = []
    for i, value in enumerate(values):
        name = f":{prefix}{i}"
        out[name] = value
        placeholders.append(name)
    return ", ".join(placeholders)


def transaction_index(*, idempotency: bool, dedupe: bool) -> dict[str, int]:
    """Transact-item positions of the marker and the pointer in
    :meth:`NotificationStore.write_transactional` (marker first, then pointer)."""
    index: dict[str, int] = {}
    if idempotency:
        index["marker"] = 0
    if dedupe:
        index["pointer"] = len(index)
    return index


class NotificationStore:
    """Data access for the NotificationsTable."""

    def __init__(
        self,
        *,
        table_name: str,
        region: str | None = None,
        client: DynamoDBClient | None = None,
        client_factory: Callable[[], DynamoDBClient] | None = None,
    ):
        self.table_name = table_name
        self._client_factory = client_factory or (
            lambda: DynamoDBClient(table_name=table_name, region=region)
        )
        self.client = client or self._client_factory()
        self._local = threading.local()

    def thread_client(self) -> DynamoDBClient:
        """A client owned by the calling thread (boto3 resources are not thread-safe)."""
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._client_factory()
            self._local.client = client
        return client

    # ------------------------------------------------------------------ writes

    def put_message(
        self, item: dict[str, Any], *, client: DynamoDBClient | None = None
    ) -> None:
        (client or self.client).put_item(
            item, condition_expression="attribute_not_exists(PK)"
        )

    def write_transactional(
        self,
        *,
        pk: str,
        message_item: dict[str, Any],
        idempotency_sk: str | None = None,
        dedupe_sk: str | None = None,
        old_sk: str | None = None,
        expires_after: int,
        created_at: str | None = None,
        client: DynamoDBClient | None = None,
    ) -> dict[str, int]:
        """Write a message with its marker and/or dedupe pointer atomically.

        Returns the transact-item index of ``"marker"`` and ``"pointer"`` (when
        present) so the caller can read ``reason_code`` on cancellation.
        """
        items: list[dict[str, Any]] = []
        index = transaction_index(
            idempotency=idempotency_sk is not None, dedupe=dedupe_sk is not None
        )
        new_sk = message_item["SK"]
        if idempotency_sk is not None:
            items.append(
                {
                    "Put": {
                        "Item": {
                            "PK": pk,
                            "SK": idempotency_sk,
                            "CreatedAt": created_at or message_item.get("CreatedAt"),
                            "ExpiresAfter": expires_after,
                        },
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
            )
        if dedupe_sk is not None:
            pointer: dict[str, Any] = {
                "Item": {
                    "PK": pk,
                    "SK": dedupe_sk,
                    "CurrentSK": new_sk,
                    "ExpiresAfter": expires_after,
                }
            }
            if old_sk is None:
                pointer["ConditionExpression"] = "attribute_not_exists(PK)"
            else:
                pointer["ConditionExpression"] = "CurrentSK = :old"
                pointer["ExpressionAttributeValues"] = {":old": old_sk}
            items.append({"Put": pointer})
            if old_sk is not None:
                items.append({"Delete": {"Key": {"PK": pk, "SK": old_sk}}})
        items.append(
            {
                "Put": {
                    "Item": message_item,
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
        )
        (client or self.client).transact_write_items(items)
        return index

    def get_pointer(
        self, pk: str, dedupe_sk: str, *, client: DynamoDBClient | None = None
    ) -> str | None:
        item = (client or self.client).get_item(
            {"PK": pk, "SK": dedupe_sk}, consistent_read=True
        )
        return None if item is None else item.get("CurrentSK")

    # ------------------------------------------------------------------- reads

    def list_page(
        self,
        pk: str,
        *,
        read: bool | None,
        types: Sequence[str] | None,
        severities: Sequence[str] | None,
        limit: int,
        after_id: str | None,
        now: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Newest-first live messages after ``after_id``; ``(items, has_more)``."""
        values: dict[str, Any] = {":pk": pk, ":now": now}
        names: dict[str, str] = dict(_NAMES_EXP)
        if after_id is None:
            key_condition = "PK = :pk AND begins_with(SK, :n)"
            values[":n"] = MESSAGE_PREFIX
            skip_sk = None
        else:
            # BETWEEN is inclusive, so the cursor item itself is skipped below.
            key_condition = "PK = :pk AND SK BETWEEN :lo AND :hi"
            values[":lo"] = MESSAGE_PREFIX
            values[":hi"] = skip_sk = message_sk(after_id)
        filters = ["#exp > :now"]
        if read is True:
            filters.append("attribute_exists(#readAt)")
            names.update(_NAMES_READ)
        elif read is False:
            filters.append("attribute_not_exists(#readAt)")
            names.update(_NAMES_READ)
        if types:
            names["#type"] = "Type"
            filters.append(f"#type IN ({_in_clause('t', types, values)})")
        if severities:
            names["#sev"] = "Severity"
            filters.append(f"#sev IN ({_in_clause('s', severities, values)})")

        matches: list[dict[str, Any]] = []
        for response in self.client.query_pages(
            key_condition,
            expression_attribute_names=names,
            expression_attribute_values=values,
            scan_index_forward=False,
            filter_expression=" AND ".join(filters),
            page_size=max(limit * 2, 25),
        ):
            for item in response.get("Items", []):
                if item.get("SK") == skip_sk:
                    continue
                matches.append(item)
            if len(matches) > limit:
                break
        return matches[:limit], len(matches) > limit

    def unread_count_index(self, pk: str, now: int) -> int:
        """Cheap (eventually consistent) unread count from the sparse GSI."""
        return self.client.query_count(
            "UnreadPK = :pk",
            expression_attribute_names=dict(_NAMES_EXP),
            expression_attribute_values={":pk": pk, ":now": now},
            index_name=UNREAD_INDEX,
            filter_expression="#exp > :now",
        )

    def unread_count_consistent(self, pk: str, now: int) -> int:
        """Exact unread count from the base table (after the caller's own mutation)."""
        return self.client.query_count(
            "PK = :pk AND begins_with(SK, :n)",
            expression_attribute_names={**_NAMES_EXP, **_NAMES_READ},
            expression_attribute_values={":pk": pk, ":n": MESSAGE_PREFIX, ":now": now},
            filter_expression="attribute_not_exists(#readAt) AND #exp > :now",
            consistent_read=True,
        )

    def live_message_sks(self, pk: str, now: int, *, read: bool | None) -> list[str]:
        filters = ["#exp > :now"]
        names = {**_NAMES_EXP, "#sk": "SK"}
        if read is not None:
            names.update(_NAMES_READ)
            filters.append(
                "attribute_exists(#readAt)" if read else "attribute_not_exists(#readAt)"
            )
        sks: list[str] = []
        for response in self.client.query_pages(
            "PK = :pk AND begins_with(SK, :n)",
            expression_attribute_names=names,
            expression_attribute_values={":pk": pk, ":n": MESSAGE_PREFIX, ":now": now},
            filter_expression=" AND ".join(filters),
            projection_expression="#sk",
            consistent_read=True,
        ):
            sks.extend(str(i["SK"]) for i in response.get("Items", []))
        return sks

    def existing_ids(self, pk: str, ids: Iterable[str], now: int) -> set[str]:
        keys = [{"PK": pk, "SK": message_sk(i)} for i in ids]
        if not keys:
            return set()
        items = self.client.batch_get_items(
            keys,
            consistent_read=True,
            projection_expression="SK, #exp",
            expression_attribute_names=dict(_NAMES_EXP),
        )
        return {
            str(i["SK"])[len(MESSAGE_PREFIX) :]
            for i in items
            if int(i.get("ExpiresAfter", 0)) > now
        }

    # ---------------------------------------------------------------- mutations

    def set_read(
        self, pk: str, msg_id: str, read: bool, now_iso: str, *, now_epoch: int
    ) -> bool:
        """Mark one message read/unread. ``False`` if it doesn't exist (or expired)."""
        names = {**_NAMES_EXP, **_NAMES_READ}
        values: dict[str, Any] = {":nowEpoch": now_epoch}
        if read:
            update = "SET #readAt = if_not_exists(#readAt, :now) REMOVE UnreadPK"
            values[":now"] = now_iso
        else:
            update = "REMOVE #readAt SET UnreadPK = :pk"
            values[":pk"] = pk
        try:
            self.client.update_item(
                {"PK": pk, "SK": message_sk(msg_id)},
                update,
                expression_attribute_names=names,
                expression_attribute_values=values,
                return_values="NONE",
                condition_expression="attribute_exists(PK) AND #exp > :nowEpoch",
            )
        except DynamoDBError as e:
            if e.error_code == CONDITIONAL_CHECK_FAILED:
                return False
            raise
        return True

    def delete_messages(self, pk: str, sks: Sequence[str]) -> None:
        self.client.batch_write_items(delete_keys=[{"PK": pk, "SK": sk} for sk in sks])

    def trim(
        self, pk: str, cap: int, now: int, *, client: DynamoDBClient | None = None
    ) -> TrimReport:
        """Enforce the per-user cap: expired first, then oldest read, then oldest unread.

        Every live delete is conditional on the read state it was chosen for, so
        concurrent trims converge instead of over-deleting.
        """
        c = client or self.client
        rows: list[dict[str, Any]] = []
        for response in c.query_pages(
            "PK = :pk AND begins_with(SK, :n)",
            expression_attribute_names={
                **_NAMES_EXP,
                **_NAMES_READ,
                "#sk": "SK",
                "#type": "Type",
            },
            expression_attribute_values={":pk": pk, ":n": MESSAGE_PREFIX},
            projection_expression="#sk, #readAt, #exp, #type",
            scan_index_forward=True,
            consistent_read=True,
        ):
            rows.extend(response.get("Items", []))

        expired = [r for r in rows if int(r.get("ExpiresAfter", 0)) <= now]
        live = [r for r in rows if int(r.get("ExpiresAfter", 0)) > now]
        excess = max(0, len(live) - cap)
        report = TrimReport()
        if expired:
            c.batch_write_items(
                delete_keys=[{"PK": pk, "SK": r["SK"]} for r in expired]
            )
            report.expired_deleted = len(expired)
        if not excess:
            return report

        read_rows = [r for r in live if "ReadAt" in r]
        unread_rows = [r for r in live if "ReadAt" not in r]
        victims = (read_rows + unread_rows)[:excess]
        for row in victims:
            was_read = "ReadAt" in row
            condition = (
                "attribute_exists(PK) AND attribute_exists(#readAt)"
                if was_read
                else "attribute_exists(PK) AND attribute_not_exists(#readAt)"
            )
            try:
                c.delete_item(
                    {"PK": pk, "SK": row["SK"]},
                    condition_expression=condition,
                    expression_attribute_names=dict(_NAMES_READ),
                )
            except DynamoDBError as e:
                if e.error_code == CONDITIONAL_CHECK_FAILED:
                    continue
                raise
            if was_read:
                report.read_evicted += 1
            else:
                report.unread_evicted += 1
                logger.warning(
                    "event=inbox_cap_evicted_unread recipient=%s id=%s type=%s cap=%d",
                    recipient_hash(pk[len(PARTITION_PREFIX) :]),
                    str(row["SK"])[len(MESSAGE_PREFIX) :],
                    row.get("Type", ""),
                    cap,
                )
        return report

    # -------------------------------------------------------------- preferences

    def get_preferences_items(self, pks: Sequence[str]) -> dict[str, list[str]]:
        if not pks:
            return {}
        items = self.client.batch_get_items(
            [{"PK": pk, "SK": PREFS_SK} for pk in pks],
            projection_expression="PK, MutedTypes",
        )
        return {str(i["PK"]): list(i.get("MutedTypes") or []) for i in items}

    def get_preferences(self, pk: str) -> tuple[list[str], str | None]:
        item = self.client.get_item({"PK": pk, "SK": PREFS_SK}, consistent_read=True)
        if item is None:
            return [], None
        return list(item.get("MutedTypes") or []), item.get("UpdatedAt")

    def put_preferences(self, pk: str, muted: Sequence[str], now_iso: str) -> None:
        self.client.put_item(
            {"PK": pk, "SK": PREFS_SK, "MutedTypes": list(muted), "UpdatedAt": now_iso}
        )
