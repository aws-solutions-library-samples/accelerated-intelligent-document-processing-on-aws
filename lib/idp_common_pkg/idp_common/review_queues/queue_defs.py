# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""System-wide review queue definitions (``QDEF`` / ``Q#<queueId>`` items).

Admins create queues once in HITL › Queue Management; any configuration profile
routes its low-confidence documents to one by id (``hitl.review_queues.queue``),
and several profiles may share a queue. A queue's work is assigned to Annotators
by an Admin: nobody claims it themselves, and an assignment does not expire — the
queue's ``time_limit_minutes`` only schedules a reminder.

``Default`` and ``Escalation`` queues are seeded the first time the definitions
are read on a stack that has none.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, List, Optional

from pydantic import BaseModel, Field, field_validator

from . import keys
from .exceptions import ReviewValidationError

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_ID = "default"
ESCALATION_QUEUE_ID = "escalation"
DEFAULT_TIME_LIMIT_MINUTES = 60
#: An assignment holds the item until it is completed or an Admin unassigns it;
#: the claim machinery's lease is set this far out so nothing expires.
ASSIGNMENT_LEASE_MINUTES = 10 * 365 * 24 * 60
#: Admins assign work, so the per-reviewer claim cap no longer limits anyone.
ASSIGNMENT_CAP = 100_000

DEFAULT_REJECT_REASONS = (
    "Illegible or poor-quality document",
    "Wrong document type",
    "Pages missing or out of order",
    "Duplicate document",
    "Other (see note)",
)

QUEUE_ID_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def display_name(queue_id: str) -> str:
    """A readable name for a queue id: ``"escalation"`` -> ``"Escalation"``."""
    words = re.split(r"[\s_.-]+", queue_id.strip())
    return " ".join(w[:1].upper() + w[1:] for w in words if w) or queue_id


class QueueDefinition(BaseModel):
    """One system-wide production review queue."""

    id: str = Field(pattern=QUEUE_ID_PATTERN)
    name: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=300)
    priority: int = Field(default=50, ge=0, le=100)
    checklist: List[str] = Field(default_factory=list, max_length=20)
    reject_reasons: List[str] = Field(
        default_factory=lambda: list(DEFAULT_REJECT_REASONS),
        min_length=1,
        max_length=20,
    )
    #: When an assigned item is still open after this long, its Annotator is reminded.
    time_limit_minutes: int = Field(default=DEFAULT_TIME_LIMIT_MINUTES, ge=1, le=10080)
    #: Annotators (by email) who work this queue; empty = none (Admins only).
    eligible_annotators: List[str] = Field(default_factory=list, max_length=200)

    @field_validator("id", mode="before")
    @classmethod
    def normalise_id(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("name", "description", mode="before")
    @classmethod
    def strip_text(cls, v: Any) -> Any:
        return "" if v is None else str(v).strip()

    @field_validator("checklist", "reject_reasons", mode="before")
    @classmethod
    def clean_list(cls, v: Any) -> List[str]:
        items = [str(x).strip() for x in (v or []) if str(x).strip()]
        return list(dict.fromkeys(items))

    @field_validator("eligible_annotators", mode="before")
    @classmethod
    def clean_emails(cls, v: Any) -> List[str]:
        emails = []
        for item in v or []:
            email = str(item).strip().lower()
            if not _EMAIL_RE.match(email):
                raise ValueError("eligible_annotators must contain email addresses")
            emails.append(email)
        return list(dict.fromkeys(emails))

    def model_post_init(self, __context: Any) -> None:
        if not self.name:
            object.__setattr__(self, "name", display_name(self.id))

    # Compatibility with the claim/registry code, which reads these per queue.
    @property
    def member_groups(self) -> List[str]:
        return ["Annotator", "Admin"]

    @property
    def member_emails(self) -> List[str]:
        return list(self.eligible_annotators)

    def allows(self, email: str) -> bool:
        # Only the listed Annotators work a queue; an empty list means none (Admins
        # still work every queue, see QueueDirectory.is_member).
        return email.lower() in self.eligible_annotators

    def to_item(self) -> dict[str, Any]:
        return {
            **keys.qdef_key(self.id),
            "QueueId": self.id,
            "Name": self.name,
            "Description": self.description,
            "Priority": self.priority,
            "Checklist": list(self.checklist),
            "RejectReasons": list(self.reject_reasons),
            "TimeLimitMinutes": self.time_limit_minutes,
            "EligibleAnnotators": list(self.eligible_annotators),
        }

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "QueueDefinition":
        return cls(
            id=item.get("QueueId") or str(item.get("SK", ""))[2:],
            name=item.get("Name") or "",
            description=item.get("Description") or "",
            priority=int(item.get("Priority", 50)),
            checklist=list(item.get("Checklist") or []),
            reject_reasons=list(item.get("RejectReasons") or DEFAULT_REJECT_REASONS),
            time_limit_minutes=int(
                item.get("TimeLimitMinutes", DEFAULT_TIME_LIMIT_MINUTES)
            ),
            eligible_annotators=list(item.get("EligibleAnnotators") or []),
        )


def default_definitions() -> list[QueueDefinition]:
    return [
        QueueDefinition(
            id=DEFAULT_QUEUE_ID,
            name="Default",
            description="Documents with low-confidence fields",
            priority=50,
            checklist=["Check every highlighted field against the page image"],
        ),
        QueueDefinition(
            id=ESCALATION_QUEUE_ID,
            name="Escalation",
            description="Items an Admin moved here for closer review",
            priority=90,
        ),
    ]


class QueuesView:
    """The system-wide queue settings in the shape the service reads.

    ``default_queue`` and ``escalation_queue`` are the seeded ids; the claim cap and
    lease no longer limit anyone because Admins assign the work.
    """

    enabled = True
    default_queue = DEFAULT_QUEUE_ID
    escalation_queue = ESCALATION_QUEUE_ID
    max_claims_per_reviewer = ASSIGNMENT_CAP
    lease_minutes = ASSIGNMENT_LEASE_MINUTES

    def __init__(self, queues: list[QueueDefinition]):
        self.queues = queues

    def queue(self, queue_id: Optional[str]) -> Optional[QueueDefinition]:
        if not queue_id:
            return None
        wanted = queue_id.strip().lower()
        return next((q for q in self.queues if q.id == wanted), None)

    @property
    def reject_reasons(self) -> list[str]:
        default = self.queue(DEFAULT_QUEUE_ID)
        return list(default.reject_reasons if default else DEFAULT_REJECT_REASONS)


class QueueDefStore:
    """Queue definitions in HITLTable, cached briefly per process."""

    def __init__(
        self,
        client_factory: Callable[[], Any],
        *,
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client_factory = client_factory
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Optional[list[QueueDefinition]] = None
        self._loaded_at = 0.0

    @property
    def client(self):
        return self._client_factory()

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def list(self) -> list[QueueDefinition]:
        with self._lock:
            if self._cached is not None and self._clock() - self._loaded_at < self._ttl:
                return list(self._cached)
        items: list[dict[str, Any]] = []
        for page in self.client.query_pages(
            "PK = :pk", expression_attribute_values={":pk": keys.QDEF_PK}
        ):
            items.extend(page.get("Items", []))
        defs = []
        for item in items:
            try:
                defs.append(QueueDefinition.from_item(item))
            except ValueError:
                logger.warning("event=queue_definition_invalid sk=%s", item.get("SK"))
        if not defs:
            defs = self.seed_defaults()
        defs.sort(key=lambda d: (-d.priority, d.id))
        with self._lock:
            self._cached = defs
            self._loaded_at = self._clock()
        return list(defs)

    def get(self, queue_id: str) -> Optional[QueueDefinition]:
        return QueuesView(self.list()).queue(queue_id)

    def seed_defaults(self) -> list[QueueDefinition]:
        """Write the Default and Escalation queues if absent; return them."""
        out = []
        for definition in default_definitions():
            try:
                self.client.put_item(
                    definition.to_item(),
                    condition_expression="attribute_not_exists(PK)",
                )
            except Exception:  # noqa: BLE001 — already seeded, or seeded concurrently
                logger.debug("queue %s already defined", definition.id)
            out.append(definition)
        return out

    def put(self, definition: QueueDefinition, *, create: bool) -> QueueDefinition:
        try:
            self.client.put_item(
                definition.to_item(),
                condition_expression=(
                    "attribute_not_exists(PK)" if create else "attribute_exists(PK)"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "error_code", "") or ""
            if "ConditionalCheckFailed" in code:
                raise ReviewValidationError(
                    "QUEUE_EXISTS" if create else "UNKNOWN_QUEUE",
                    f"queue {definition.id!r} "
                    + ("already exists" if create else "does not exist"),
                ) from None
            raise
        self.invalidate()
        return definition

    def delete(self, queue_id: str) -> None:
        if queue_id in (DEFAULT_QUEUE_ID, ESCALATION_QUEUE_ID):
            raise ReviewValidationError(
                "PROTECTED_QUEUE", "the Default and Escalation queues can't be deleted"
            )
        self.client.delete_item(keys.qdef_key(queue_id))
        self.invalidate()
