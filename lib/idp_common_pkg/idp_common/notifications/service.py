# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``NotificationService`` (post, read, mutate), its factory and ``notify``."""

import functools
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

import idp_common.notifications.recipients as _recipients
from idp_common.dynamodb.client import (
    DynamoDBClient,
    DynamoDBError,
    DynamoDBTransactionCanceledError,
)
from idp_common.dynamodb.ttl import calculate_ttl
from idp_common.metrics import put_metric
from idp_common.notifications.catalogue import list_types, require_type
from idp_common.notifications.exceptions import (
    NotificationNotFoundError,
    NotificationValidationError,
)
from idp_common.notifications.keys import (
    decode_cursor,
    dedupe_sk,
    encode_cursor,
    idempotency_sk,
    message_sk,
    new_notification_id,
    partition_key,
    recipient_hash,
)
from idp_common.notifications.models import (
    DEFAULT_LIST_LIMIT,
    Delivery,
    MessagePage,
    MetadataValue,
    MutationResult,
    NotificationRequest,
    NotificationSettings,
    PostResult,
    Preferences,
    Severity,
    TypeWithMute,
    clamp_list_limit,
    metadata_json,
)
from idp_common.notifications.settings import ActiveProfileSettingsProvider
from idp_common.notifications.store import (
    NotificationStore,
    to_inbox_message,
    transaction_index,
)

logger = logging.getLogger(__name__)

_DEDUPE_ATTEMPTS = 3
_CONDITION_FAILED = "ConditionalCheckFailed"


def _iso(now: datetime) -> str:
    """ISO-8601 UTC with millisecond precision and a ``Z`` suffix."""
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class NotificationService:
    """Posts messages to recipients and serves a recipient's own Inbox."""

    def __init__(
        self,
        *,
        store: NotificationStore,
        user_pool_id: str | None = None,
        region: str | None = None,
        settings_provider: Callable[[], NotificationSettings] | None = None,
        group_lister: Callable[[str, str], Iterable[dict]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        fanout_workers: int = 8,
    ):
        self.store = store
        self.user_pool_id = user_pool_id
        self.region = region
        self._settings_provider = settings_provider
        self._group_lister = group_lister
        self._clock = clock
        self._fanout_workers = fanout_workers

    # ------------------------------------------------------------------- post

    def _settings(self) -> NotificationSettings:
        if self._settings_provider is None:
            self._settings_provider = ActiveProfileSettingsProvider(region=self.region)
        return self._settings_provider()

    def _lister(self) -> Callable[[str, str], Iterable[dict]]:
        if self._group_lister is not None:
            return self._group_lister
        region = self.region

        def lister(pool: str, group: str) -> Iterable[dict]:
            return _recipients.list_group_members(
                pool, group, client=_recipients._cognito_client(region)
            )

        return lister

    def post(self, request: NotificationRequest) -> PostResult:
        """Deliver ``request`` to every recipient. Per-recipient failures are
        logged and reported as ``failed``; they never stop the other recipients."""
        notification_type = require_type(request.type_id)
        severity: Severity = request.severity or notification_type.default_severity
        settings = self._settings()
        recipients, truncated = _recipients.expand_recipients(
            request.users,
            request.groups,
            user_pool_id=self.user_pool_id,
            lister=self._lister() if request.groups else None,
            max_recipients=_recipients.MAX_RECIPIENTS,
        )

        muted_by_pk = self.store.get_preferences_items(
            [partition_key(r) for r in recipients]
        )
        deliveries: list[Delivery] = []
        targets: list[str] = []
        for rk in recipients:
            if request.type_id in muted_by_pk.get(partition_key(rk), []):
                deliveries.append(
                    Delivery(recipient_hash=recipient_hash(rk), outcome="muted")
                )
            else:
                targets.append(rk)

        if len(targets) == 1:
            deliveries.append(
                self._deliver_one(targets[0], request, severity, settings, None)
            )
        elif targets:
            with ThreadPoolExecutor(
                max_workers=min(self._fanout_workers, len(targets))
            ) as pool:
                deliveries.extend(
                    pool.map(
                        lambda rk: self._deliver_one(
                            rk, request, severity, settings, self.store.thread_client()
                        ),
                        targets,
                    )
                )
        return PostResult(
            deliveries=deliveries,
            recipients_considered=len(recipients),
            truncated=truncated,
        )

    def _deliver_one(
        self,
        rk: str,
        request: NotificationRequest,
        severity: Severity,
        settings: NotificationSettings,
        client: DynamoDBClient | None,
    ) -> Delivery:
        rhash = recipient_hash(rk)
        pk = partition_key(rk)
        try:
            now = self._clock()
            notification_id = new_notification_id(int(now.timestamp() * 1000))
            expires_after = calculate_ttl(settings.retention_days, now=now)
            created_at = _iso(now)
            item: dict = {
                "PK": pk,
                "SK": message_sk(notification_id),
                "Type": request.type_id,
                "Title": request.title,
                "Body": request.body,
                "Severity": severity,
                "Source": request.source,
                "CreatedAt": created_at,
                "ExpiresAfter": expires_after,
                "UnreadPK": pk,
            }
            if request.link:
                item["Link"] = request.link
            if request.dedupe_key:
                item["DedupeKey"] = request.dedupe_key
            if request.metadata is not None:
                item["MetadataJson"] = metadata_json(request.metadata)

            if not request.dedupe_key and not request.idempotency_key:
                self.store.put_message(item, client=client)
            elif self._write_keyed(pk, item, request, expires_after, client):
                return Delivery(recipient_hash=rhash, outcome="duplicate")
        except Exception:
            logger.error(
                "event=inbox_delivery_failed recipient=%s type=%s",
                rhash,
                request.type_id,
                exc_info=True,
            )
            return Delivery(recipient_hash=rhash, outcome="failed")

        try:
            self.store.trim(
                pk, settings.max_per_user, int(now.timestamp()), client=client
            )
        except Exception:
            logger.error(
                "event=inbox_cap_trim_failed recipient=%s", rhash, exc_info=True
            )
            put_metric("NotificationCapTrimFailures", 1)
        return Delivery(
            recipient_hash=rhash, notification_id=notification_id, outcome="delivered"
        )

    def _write_keyed(
        self,
        pk: str,
        item: dict,
        request: NotificationRequest,
        expires_after: int,
        client: DynamoDBClient | None,
    ) -> bool:
        """Write with dedupe and/or idempotency. ``True`` means duplicate."""
        isk = (
            idempotency_sk(request.idempotency_key) if request.idempotency_key else None
        )
        dsk = dedupe_sk(request.dedupe_key) if request.dedupe_key else None
        index = transaction_index(idempotency=isk is not None, dedupe=dsk is not None)
        for _attempt in range(_DEDUPE_ATTEMPTS):
            old = self.store.get_pointer(pk, dsk, client=client) if dsk else None
            try:
                self.store.write_transactional(
                    pk=pk,
                    message_item=item,
                    idempotency_sk=isk,
                    dedupe_sk=dsk,
                    old_sk=old,
                    expires_after=expires_after,
                    client=client,
                )
                return False
            except DynamoDBTransactionCanceledError as e:
                if (
                    "marker" in index
                    and e.reason_code(index["marker"]) == _CONDITION_FAILED
                ):
                    return True
                if (
                    "pointer" in index
                    and e.reason_code(index["pointer"]) == _CONDITION_FAILED
                ):
                    continue
                raise
        raise DynamoDBError("dedupe contention", "DedupeContention")

    # ------------------------------------------------------------------ reads

    def _now(self) -> datetime:
        return self._clock()

    def list_messages(
        self,
        rk: str,
        *,
        read: bool | None = None,
        types: Sequence[str] | None = None,
        severities: Sequence[str] | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        next_token: str | None = None,
    ) -> MessagePage:
        pk = partition_key(rk)
        after_id = decode_cursor(next_token) if next_token else None
        now = int(self._now().timestamp())
        limit = clamp_list_limit(limit)
        items, has_more = self.store.list_page(
            pk,
            read=read,
            types=types,
            severities=severities,
            limit=limit,
            after_id=after_id,
            now=now,
        )
        messages = [to_inbox_message(i) for i in items]
        return MessagePage(
            items=messages,
            next_token=encode_cursor(messages[-1].id)
            if has_more and messages
            else None,
            # Exact, not the UnreadIndex count: this is what the badges show, and
            # the UI lists again right after the caller's own mark/delete. The GSI
            # lags those writes, so it put the old count back on the badge. The
            # per-user cap keeps this one-partition query small.
            unread_count=self.store.unread_count_consistent(pk, now),
        )

    def unread_count(self, rk: str, *, consistent: bool = False) -> int:
        pk = partition_key(rk)
        now = int(self._now().timestamp())
        if consistent:
            return self.store.unread_count_consistent(pk, now)
        return self.store.unread_count_index(pk, now)

    # -------------------------------------------------------------- mutations

    @staticmethod
    def _unique_ids(ids: Sequence[str]) -> list[str]:
        unique = list(dict.fromkeys(ids))
        if not unique:
            raise NotificationValidationError("ids must not be empty")
        return unique

    def mark(self, rk: str, ids: Sequence[str], *, read: bool) -> MutationResult:
        pk = partition_key(rk)
        now = self._now()
        now_iso, now_epoch = _iso(now), int(now.timestamp())
        updated = 0
        not_found: list[str] = []
        for msg_id in self._unique_ids(ids):
            if self.store.set_read(pk, msg_id, read, now_iso, now_epoch=now_epoch):
                updated += 1
            else:
                not_found.append(msg_id)
        if not updated:
            raise NotificationNotFoundError("No matching messages")
        return MutationResult(
            updated=updated,
            not_found_ids=not_found,
            unread_count=self.store.unread_count_consistent(pk, now_epoch),
        )

    def mark_all_read(self, rk: str) -> MutationResult:
        pk = partition_key(rk)
        now = self._now()
        now_iso, now_epoch = _iso(now), int(now.timestamp())
        updated = 0
        for sk in self.store.live_message_sks(pk, now_epoch, read=False):
            msg_id = sk[len("N#") :]
            if self.store.set_read(pk, msg_id, True, now_iso, now_epoch=now_epoch):
                updated += 1
        return MutationResult(
            updated=updated,
            not_found_ids=[],
            unread_count=self.store.unread_count_consistent(pk, now_epoch),
        )

    def delete(self, rk: str, ids: Sequence[str]) -> MutationResult:
        pk = partition_key(rk)
        now_epoch = int(self._now().timestamp())
        unique = self._unique_ids(ids)
        existing = self.store.existing_ids(pk, unique, now_epoch)
        if not existing:
            raise NotificationNotFoundError("No matching messages")
        found = [i for i in unique if i in existing]
        self.store.delete_messages(pk, [message_sk(i) for i in found])
        return MutationResult(
            updated=len(found),
            not_found_ids=[i for i in unique if i not in existing],
            unread_count=self.store.unread_count_consistent(pk, now_epoch),
        )

    def clear_read(self, rk: str) -> MutationResult:
        pk = partition_key(rk)
        now_epoch = int(self._now().timestamp())
        sks = self.store.live_message_sks(pk, now_epoch, read=True)
        self.store.delete_messages(pk, sks)
        return MutationResult(
            updated=len(sks),
            not_found_ids=[],
            unread_count=self.store.unread_count_consistent(pk, now_epoch),
        )

    # ------------------------------------------------------------ preferences

    def get_preferences(self, rk: str) -> Preferences:
        muted, updated_at = self.store.get_preferences(partition_key(rk))
        muted_set = set(muted)
        return Preferences(
            types=[
                TypeWithMute(
                    id=t.id,
                    display_name=t.display_name,
                    description=t.description,
                    default_severity=t.default_severity,
                    muted=t.id in muted_set,
                )
                for t in list_types()
            ],
            muted_types=muted,
            updated_at=updated_at,
        )

    def update_preferences(self, rk: str, muted_types: Sequence[str]) -> Preferences:
        for type_id in muted_types:
            require_type(type_id)
        self.store.put_preferences(
            partition_key(rk), sorted(set(muted_types)), _iso(self._now())
        )
        return self.get_preferences(rk)


def create_notification_service(
    *,
    table_name: str | None = None,
    user_pool_id: str | None = None,
    region: str | None = None,
) -> NotificationService | None:
    """The service, or ``None`` when the Inbox table isn't configured (headless)."""
    table_name = table_name or os.environ.get("NOTIFICATIONS_TABLE_NAME", "")
    if not table_name:
        logger.debug("Notifications table not configured; Inbox disabled")
        return None
    user_pool_id = user_pool_id or os.environ.get("USER_POOL_ID") or None
    store = NotificationStore(table_name=table_name, region=region)
    return NotificationService(store=store, user_pool_id=user_pool_id, region=region)


@functools.lru_cache(maxsize=1)
def _default_service() -> NotificationService | None:
    return create_notification_service()


def notify(
    *,
    type_id: str,
    title: str,
    source: str,
    body: str = "",
    users: Sequence[str] = (),
    groups: Sequence[str] = (),
    severity: Severity | None = None,
    link: str | None = None,
    dedupe_key: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict[str, MetadataValue] | None = None,
    service: NotificationService | None = None,
) -> PostResult | None:
    """Post an Inbox message. **Never raises**: a failure to notify must not
    fail the producer. Failures are logged at ERROR and counted in the
    ``NotificationPostFailures`` metric; ``None`` means nothing was posted."""
    try:
        service = service or _default_service()
        if service is None:
            logger.debug("Inbox not configured; notification %s skipped", type_id)
            return None
        request = NotificationRequest(
            type_id=type_id,
            title=title,
            body=body,
            severity=severity,
            link=link,
            source=source,
            dedupe_key=dedupe_key,
            idempotency_key=idempotency_key,
            metadata=metadata,
            users=list(users),
            groups=list(groups),
        )
        result = service.post(request)
    except Exception:
        logger.error(
            "event=inbox_post_failed type=%s source=%s", type_id, source, exc_info=True
        )
        put_metric("NotificationPostFailures", 1)
        return None
    if result.failed > 0:
        logger.error(
            "event=inbox_post_partial_failure type=%s source=%s failed=%d delivered=%d",
            type_id,
            source,
            result.failed,
            result.delivered,
        )
        put_metric("NotificationPostFailures", result.failed)
    return result
