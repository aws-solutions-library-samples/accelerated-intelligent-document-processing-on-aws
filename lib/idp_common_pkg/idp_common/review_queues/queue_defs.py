# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""System-wide review queue definitions (``QDEF`` / ``Q#<queueId>`` items).

Admins manage queues in HITL › Queue Management. A production document that
raised confidence alerts is routed to the ``Default`` queue when HITL is enabled
in its configuration and the queue is enabled (see :mod:`.routing`); queues have
no confidence threshold of their own. An Admin can move items to any other
queue. A queue's work is assigned to Annotators by an Admin, and an
assignment does not expire — the queue's ``time_limit_minutes`` only schedules a
reminder.

The ``Default`` queue is seeded, enabled, whenever the definitions are read and
it is missing; a stored Default is never overwritten. It is the only queue that
can't be deleted.
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

#: Why an Annotator gives an item back to its queue unfinished (Skip). Recorded in
#: the item's history, so the next reviewer and the Admin can see why it came back.
DEFAULT_SKIP_REASONS = (
    "Needs expert review",
    "Document unclear or incomplete",
    "Conflict of interest",
    "Out of time",
    "Other (see note)",
)

QUEUE_ID_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
#: A queue id as an API argument names it: case and a space are tolerated, and
#: the id is normalised before it is looked up.
QUEUE_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def display_name(queue_id: str) -> str:
    """A readable name for a queue id: ``"loan_review"`` -> ``"Loan Review"``."""
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
    skip_reasons: List[str] = Field(
        default_factory=lambda: list(DEFAULT_SKIP_REASONS),
        min_length=1,
        max_length=20,
    )
    #: When an assigned item is still open after this long, its Annotator is reminded.
    time_limit_minutes: int = Field(default=DEFAULT_TIME_LIMIT_MINUTES, ge=1, le=10080)
    #: Annotators (by email) who work this queue; empty = none (Admins only).
    eligible_annotators: List[str] = Field(default_factory=list, max_length=200)
    #: When off, no documents are added to this queue.
    enabled: bool = True

    @field_validator("id", mode="before")
    @classmethod
    def normalise_id(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("name", "description", mode="before")
    @classmethod
    def strip_text(cls, v: Any) -> Any:
        return "" if v is None else str(v).strip()

    @field_validator("checklist", "reject_reasons", "skip_reasons", mode="before")
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
            "SkipReasons": list(self.skip_reasons),
            "TimeLimitMinutes": self.time_limit_minutes,
            "EligibleAnnotators": list(self.eligible_annotators),
            "Enabled": self.enabled,
        }

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "QueueDefinition":
        queue_id = item.get("QueueId") or str(item.get("SK", ""))[2:]
        enabled = item.get("Enabled")
        return cls(
            id=queue_id,
            name=item.get("Name") or "",
            description=item.get("Description") or "",
            priority=int(item.get("Priority", 50)),
            checklist=list(item.get("Checklist") or []),
            reject_reasons=list(item.get("RejectReasons") or DEFAULT_REJECT_REASONS),
            skip_reasons=list(item.get("SkipReasons") or DEFAULT_SKIP_REASONS),
            time_limit_minutes=int(
                item.get("TimeLimitMinutes", DEFAULT_TIME_LIMIT_MINUTES)
            ),
            eligible_annotators=list(item.get("EligibleAnnotators") or []),
            # A stored ConfidenceThreshold (an older queue setting) is ignored. A
            # definition stored before the switch existed reads as on.
            enabled=True if enabled is None else bool(enabled),
        )


def default_definitions() -> list[QueueDefinition]:
    """The queues a stack always has: only Default, seeded enabled."""
    return [
        QueueDefinition(
            id=DEFAULT_QUEUE_ID,
            name="Default",
            description="Documents with low-confidence fields",
            priority=50,
            checklist=["Check every highlighted field against the page image"],
            enabled=True,
        ),
    ]


class QueuesView:
    """The system-wide queue settings in the shape the service reads.

    ``default_queue`` is the seeded id; the claim cap and lease no longer limit
    anyone because Admins assign the work.
    """

    enabled = True
    default_queue = DEFAULT_QUEUE_ID
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

    @property
    def skip_reasons(self) -> list[str]:
        default = self.queue(DEFAULT_QUEUE_ID)
        return list(default.skip_reasons if default else DEFAULT_SKIP_REASONS)


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
        present = {d.id for d in defs}
        if any(d.id not in present for d in default_definitions()):
            defs.extend(d for d in self.seed_defaults() if d.id not in present)
        defs.sort(key=lambda d: (-d.priority, d.id))
        with self._lock:
            self._cached = defs
            self._loaded_at = self._clock()
        return list(defs)

    def get(self, queue_id: str) -> Optional[QueueDefinition]:
        return QueuesView(self.list()).queue(queue_id)

    def seed_defaults(self) -> list[QueueDefinition]:
        """Write the Default queue if absent; return the seeded definitions."""
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
        if queue_id == DEFAULT_QUEUE_ID:
            raise ReviewValidationError(
                "PROTECTED_QUEUE", "the Default queue can't be deleted"
            )
        self.client.delete_item(keys.qdef_key(queue_id))
        self.invalidate()
