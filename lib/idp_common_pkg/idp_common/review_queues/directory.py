# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which queues exist and who may work them.

Queues are system-wide definitions in HITLTable (:mod:`.queue_defs`), not
part of any configuration profile. Their work goes to Annotators: an Admin works
every queue, and an Annotator works a queue whose ``eligible_annotators`` is empty
or names them. Reads fail closed: a definition read failure raises
``ReviewAccessDenied("CONFIG_UNAVAILABLE")``.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from idp_common.config_scope import scope_allows

from .exceptions import ReviewAccessDenied
from .models import CallerContext, QueueRef
from .queue_defs import QueueDefinition, QueueDefStore, QueuesView

logger = logging.getLogger(__name__)

ANNOTATOR_GROUP = "Annotator"


def _default_store() -> QueueDefStore:
    from idp_common.dynamodb.client import DynamoDBClient

    table = os.environ.get("HITL_TABLE_NAME", "")
    lock = threading.Lock()
    holder: dict[str, Any] = {}

    def client():
        with lock:
            if "c" not in holder:
                holder["c"] = DynamoDBClient(table_name=table)
            return holder["c"]

    return QueueDefStore(client)


class QueueDirectory:
    """The system-wide queues and their membership."""

    def __init__(self, queue_defs: Optional[QueueDefStore] = None, **_ignored: Any):
        self._defs = queue_defs
        self._lock = threading.Lock()

    @property
    def queue_defs(self) -> QueueDefStore:
        with self._lock:
            if self._defs is None:
                self._defs = _default_store()
            return self._defs

    def _view(self) -> QueuesView:
        try:
            return QueuesView(self.queue_defs.list())
        except ReviewAccessDenied:
            raise
        except Exception as exc:  # noqa: BLE001 — fail closed on any read failure
            logger.warning(
                "Rejecting review-queue access: queue definitions unavailable (%s)",
                type(exc).__name__,
            )
            raise ReviewAccessDenied(
                "CONFIG_UNAVAILABLE", "the queue definitions could not be read"
            ) from None

    def invalidate(self) -> None:
        self.queue_defs.invalidate()

    # -------------------------------------------------------------- queries

    def queues_config(self, profile: Optional[str] = None) -> QueuesView:
        """The system-wide queues (``profile`` is accepted for older call sites)."""
        return self._view()

    def definition(self, queue: str) -> Optional[QueueDefinition]:
        return self._view().queue(queue)

    def in_scope(self, caller: CallerContext, profile: str) -> bool:
        if caller.is_admin or caller.allowed_versions is None or not profile:
            return True
        return scope_allows(caller.allowed_versions, profile)

    def is_member(self, caller: CallerContext, profile: str, queue: str) -> bool:
        """Admins work every queue; Annotators the queues they are eligible for."""
        if caller.is_admin:
            return True
        definition = self._view().queue(queue)
        if definition is None or ANNOTATOR_GROUP not in caller.groups:
            return False
        return definition.allows(caller.email)

    def queues_for(self, caller: CallerContext) -> list[tuple[QueueRef, Any, Any]]:
        """``(ref, definition, view)`` for every queue the caller works."""
        view = self._view()
        return [
            (QueueRef(queue=d.id), d, view)
            for d in view.queues
            if self.is_member(caller, "", d.id)
        ]

    def members(self, profile: str, queue: str) -> tuple[list[str], list[str]]:
        """``(groups, emails)`` told when work arrives in a queue: nobody.

        Only Annotators have an Inbox, and they hear about an item when it is
        assigned to them; Admins see waiting work on HITL › Queue Management.
        """
        return [], []
