# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""System-wide review queue definitions (``QDEF`` / ``Q#<queueId>`` items).

Admins manage queues in Review Studio › Review Management. A production document that
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
from decimal import Decimal
from typing import Any, Callable, List, Optional

from pydantic import BaseModel, Field, field_validator

from idp_common.dynamodb.client import DynamoDBTransactionCanceledError

from . import keys
from .exceptions import ReviewConflict, ReviewValidationError

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
    #: Bumped by every write (:meth:`QueueDefStore.put`). An update names the version
    #: it was built from and is refused if the stored one has moved on, so two Admins
    #: editing one queue cannot silently overwrite each other. A definition stored
    #: before versions existed reads as 0.
    version: int = Field(default=0, ge=0)

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
        # Who hears that work waits: Annotators, Reviewers (who work every
        # Production queue) and Admins.
        return ["Annotator", "Reviewer", "Admin"]

    @property
    def member_emails(self) -> List[str]:
        return list(self.eligible_annotators)

    def allows(self, email: str) -> bool:
        # Only the listed Annotators work a queue; an empty list means none. Admins
        # and Reviewers work every queue regardless, see QueueDirectory.is_member: a
        # Reviewer may be listed, and listing one changes nothing.
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
            "Version": self.version,
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
            version=_stored_version(item.get("Version"), queue_id),
        )


def _stored_version(raw: Any, queue_id: Any) -> int:
    """A stored ``Version`` as an int; 0 when absent or not a non-negative whole number.

    A malformed one is read as 0 rather than dropping the queue from the listing, and
    the version-0 write condition (:func:`_version_condition`) matches it, so the
    queue stays editable and deletable; the next write stores a well-formed version.
    """
    if raw is None:
        return 0
    if (
        isinstance(raw, (int, Decimal))
        and not isinstance(raw, bool)
        and raw >= 0
        and raw == int(raw)
    ):
        return int(raw)
    logger.warning("event=queue_definition_version_invalid queue=%s", queue_id)
    return 0


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


#: How often an update that names no version re-reads and retries when another write
#: lands between its read and its put.
_UNVERSIONED_ATTEMPTS = 3


#: A transaction cancellation reason: that item's condition was false.
_TXN_CONDITION_FAILED = "ConditionalCheckFailed"
#: A cancellation reason meaning another transaction held the item: a concurrent
#: write, retried like a moved version rather than surfaced as a server fault.
_TXN_CONFLICT = "TransactionConflict"
_TXN_CONTENDED = frozenset({_TXN_CONDITION_FAILED, _TXN_CONFLICT})

#: How often a create or delete re-reads and retries when a concurrent write moves
#: the version it read.
_CONTENDED_ATTEMPTS = 3


def _version_condition(expected: int) -> tuple[str, dict[str, Any]]:
    """The definition exists at version ``expected``: a condition and its values.

    A definition stored before versions existed has no Version and reads as 0, and so
    does one whose Version is malformed (:func:`_stored_version`): not a number, or a
    negative one. Version 0 matches all of those. A positive number that is not whole
    also reads as 0 but is not matched, because a condition expression cannot test
    for a fraction; nothing this code writes stores one.
    """
    if expected != 0:
        return "attribute_exists(PK) AND Version = :expected", {":expected": expected}
    return (
        "attribute_exists(PK) AND (attribute_not_exists(Version)"
        " OR NOT attribute_type(Version, :number) OR Version <= :expected)",
        {":expected": expected, ":number": "N"},
    )


def _retired_check(queue_id: str, retired: Optional[int]) -> dict[str, Any]:
    """A transact check that the retired version of ``queue_id`` is still ``retired``."""
    check: dict[str, Any] = {"Key": keys.qdef_retired_key(queue_id)}
    if retired is None:
        check["ConditionExpression"] = "attribute_not_exists(PK)"
    else:
        check["ConditionExpression"] = "Version = :retired"
        check["ExpressionAttributeValues"] = {":retired": retired}
    return {"ConditionCheck": check}


def _is_condition_failure(exc: Exception) -> bool:
    return "ConditionalCheckFailed" in (getattr(exc, "error_code", "") or "")


def _is_transaction_conflict(exc: Exception) -> bool:
    """A single write refused because a transaction held the item."""
    return "TransactionConflict" in (getattr(exc, "error_code", "") or "")


def _unknown_queue(queue_id: str) -> ReviewValidationError:
    return ReviewValidationError("UNKNOWN_QUEUE", f"queue {queue_id!r} does not exist")


def _queue_changed(queue_id: str) -> ReviewConflict:
    return ReviewConflict(
        "QUEUE_CHANGED",
        f"queue {queue_id!r} was changed by someone else after you opened it; "
        "reload it and make your change again",
    )


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

    def list(self, *, fresh: bool = False) -> list[QueueDefinition]:
        """Every definition, Default seeded if missing.

        Served from the per-process cache for up to ``ttl`` seconds, unless ``fresh``:
        then the cache is bypassed and the read is strongly consistent, so it returns
        every write that has completed (what an Admin edits a queue from needs that).
        """
        with self._lock:
            if (
                not fresh
                and self._cached is not None
                and self._clock() - self._loaded_at < self._ttl
            ):
                return list(self._cached)
        items: list[dict[str, Any]] = []
        for page in self.client.query_pages(
            "PK = :pk",
            expression_attribute_values={":pk": keys.QDEF_PK},
            consistent_read=True if fresh else None,
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
        """Write the Default queue if absent; return the definitions as stored.

        When Default already exists (a listing that missed it, or a concurrent seed)
        the stored definition is returned rather than the seed, so a listing never
        reports the seed's settings and version 1 for a Default that has been edited.
        """
        out = []
        for definition in default_definitions():
            definition = definition.model_copy(update={"version": 1})
            try:
                self.client.put_item(
                    definition.to_item(),
                    condition_expression="attribute_not_exists(PK)",
                )
            except Exception as exc:  # noqa: BLE001
                if _is_condition_failure(exc):
                    logger.debug("queue %s already defined", definition.id)
                    definition = self._stored_or(definition)
                else:
                    # Not written; listed anyway so the Default queue stays usable.
                    logger.warning(
                        "event=queue_default_seed_failed queue=%s", definition.id
                    )
            out.append(definition)
        return out

    def _stored_or(self, seed: QueueDefinition) -> QueueDefinition:
        """The stored definition of ``seed``'s queue, or ``seed`` if it can't be read."""
        try:
            return self.read_current(seed.id) or seed
        except Exception:  # noqa: BLE001 — listed from the seed rather than not at all
            logger.warning("event=queue_default_read_failed queue=%s", seed.id)
            return seed

    def put(
        self,
        definition: QueueDefinition,
        *,
        create: bool,
        expected_version: Optional[int] = None,
    ) -> QueueDefinition:
        """Write a definition and return it as stored, with its new ``version``.

        A create writes version 1 — or, for an id whose queue was deleted, one more
        than that queue's last version — and is refused if the queue exists
        (``QUEUE_EXISTS``). An update writes one more than the version it replaces and
        is refused if the queue does not exist (``UNKNOWN_QUEUE``). Given
        ``expected_version`` — the version the caller's definition was built from — an
        update is also refused if the stored version is no longer that one
        (``QUEUE_CHANGED``, a :class:`ReviewConflict`): another write overtook the
        caller's read, and writing anyway would silently undo it. Without
        ``expected_version`` an update replaces whatever is stored, as it always did.
        """
        if create:
            return self._create(definition)
        if expected_version is not None:
            return self._replace(definition, expected_version)
        # No version named: replace what is stored now. Retried, so a write that lands
        # between this read and the put moves the version on instead of reusing it.
        for _ in range(_UNVERSIONED_ATTEMPTS):
            current = self.read_current(definition.id)
            if current is None:
                raise _unknown_queue(definition.id)
            try:
                return self._replace(definition, current.version)
            except ReviewConflict:
                continue
        raise _queue_changed(definition.id)

    def _create(self, definition: QueueDefinition) -> QueueDefinition:
        """Write a new queue, continuing from the version of one deleted under its id.

        One transaction writes the definition and checks that the retired version it
        read is still the one stored, so a delete that completes in between cannot
        leave the new queue at a version a form from before it already holds.
        """
        for _ in range(_CONTENDED_ATTEMPTS):
            retired = self._retired_version(definition.id)
            stored = definition.model_copy(update={"version": (retired or 0) + 1})
            try:
                self.client.transact_write_items(
                    [
                        {
                            "Put": {
                                "Item": stored.to_item(),
                                "ConditionExpression": "attribute_not_exists(PK)",
                            }
                        },
                        _retired_check(definition.id, retired),
                    ]
                )
            except DynamoDBTransactionCanceledError as exc:
                if exc.reason_code(0) == _TXN_CONDITION_FAILED:
                    raise ReviewValidationError(
                        "QUEUE_EXISTS", f"queue {definition.id!r} already exists"
                    ) from None
                if _TXN_CONTENDED & {exc.reason_code(0), exc.reason_code(1)}:
                    continue  # deleted again meanwhile: read the new retired version
                raise
            self.invalidate()
            return stored
        raise _queue_changed(definition.id)

    def _retired_version(self, queue_id: str) -> Optional[int]:
        """The last version of a queue deleted under this id, or None."""
        item = self.client.get_item(
            keys.qdef_retired_key(queue_id), consistent_read=True
        )
        return int(item.get("Version") or 0) if item else None

    def _replace(self, definition: QueueDefinition, expected: int) -> QueueDefinition:
        """Write ``definition`` over stored version ``expected``, as ``expected + 1``."""
        stored = definition.model_copy(update={"version": expected + 1})
        condition, values = _version_condition(expected)
        try:
            self.client.put_item(
                stored.to_item(),
                condition_expression=condition,
                expression_attribute_values=values,
            )
        except Exception as exc:  # noqa: BLE001
            if not (_is_condition_failure(exc) or _is_transaction_conflict(exc)):
                raise
            self.invalidate()
            if self.read_current(definition.id) is None:
                raise _unknown_queue(definition.id) from None
            raise _queue_changed(definition.id) from None
        self.invalidate()
        return stored

    def read_current(self, queue_id: str) -> Optional[QueueDefinition]:
        """The definition as stored now: a strongly consistent read, past the cache.

        Unlike :meth:`get` it does not seed the Default queue.
        """
        item = self.client.get_item(
            keys.qdef_key(queue_id.strip().lower()), consistent_read=True
        )
        return QueueDefinition.from_item(item) if item else None

    def delete(self, queue_id: str) -> None:
        """Delete a definition, keeping its last version under ``QDEFGONE``.

        The delete and the retired version are one transaction, conditional on the
        version read, so a create under this id always continues past every version
        the id has had, and the retired version is never lowered. A write that lands
        in between, or holds either item in another transaction, is retried.
        """
        # Compared as it is looked up: another spelling of Default is still Default.
        queue_id = queue_id.strip().lower()
        if queue_id == DEFAULT_QUEUE_ID:
            raise ReviewValidationError(
                "PROTECTED_QUEUE", "the Default queue can't be deleted"
            )
        try:
            for _ in range(_CONTENDED_ATTEMPTS):
                current = self.read_current(queue_id)
                if current is None:
                    return
                # Never lowered: a definition stored without a Version (by an older
                # release) under an id that was retired before reads as 0. Only a
                # delete writes the retired version, and a concurrent one fails this
                # one's condition on the definition, so the value read stays current.
                last = max(current.version, self._retired_version(current.id) or 0)
                condition, values = _version_condition(current.version)
                try:
                    self.client.transact_write_items(
                        [
                            {
                                "Delete": {
                                    "Key": keys.qdef_key(current.id),
                                    "ConditionExpression": condition,
                                    "ExpressionAttributeValues": values,
                                }
                            },
                            {
                                "Put": {
                                    "Item": {
                                        **keys.qdef_retired_key(current.id),
                                        "QueueId": current.id,
                                        "Version": last,
                                    },
                                }
                            },
                        ]
                    )
                    return
                except DynamoDBTransactionCanceledError as exc:
                    if not _TXN_CONTENDED & {exc.reason_code(0), exc.reason_code(1)}:
                        raise
            raise _queue_changed(queue_id)
        finally:
            self.invalidate()
