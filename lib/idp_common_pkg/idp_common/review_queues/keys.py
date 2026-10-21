# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Key layout of ``HITLTable`` and the opaque list cursors.

Base table (``PK`` / ``SK``):

* ``DOC#<docKey>`` / ``ITEM``: the queue item of a document's current cycle.
* ``DOC#<docKey>`` / ``DRAFT#<cycleId>#<sectionId>``: a section draft.
* ``DOC#<docKey>`` / ``H#<cycleId>#<ms>#<hex4>``: a history entry.
* ``R#<reviewerKey>`` / ``COUNTER``: the reviewer's open-claim counter.
* ``QDEF`` / ``Q#<queueId>``: a queue definition. Queues are system-wide: any
  configuration profile routes to one by id.
* ``QREG`` / ``Q#<queueId>``: the queue registry (with a member snapshot).
* ``QUEUE#<queueId>`` / ``ANNOUNCE``: the "items waiting" debounce.
* ``TSDOC#<testSetId>`` / ``D#<objectKey>``: a Test Studio document's summary
  (labels, alerts, classes) and assignment; its content stays in S3.

Sparse GSIs, present only in the states named:

* ``QueueIndex``: ``QueuePK = Q#<queueId>``, ``QueueSK`` (OPEN, CLAIMED)
* ``OpenIndex``: ``OpenPK = Q#<queueId>``, ``QueueSK`` (OPEN)
* ``OwnerIndex``: ``OwnerPK = R#<reviewerKey>``, ``OwnerSK`` (CLAIMED = assigned);
  ``OwnerPK = RT#<reviewerKey>`` for assigned Test Studio documents
* ``SweepIndex``: ``SweepPK = S#<0-3>``, ``SweepSK`` (HOLDING, CLAIMED, FINALIZING);
  ``SweepPK = TS`` for Test Studio assignment reminders

Every sort key is ``{ms:013d}#{h16}``, where ``h16`` is the first 16 hex digits
of the SHA-256 of the document key: S3 keys reach 1024 bytes and GSI sort keys
are limited to 1024, so the document key itself never goes into one.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from typing import Any, Optional

from .exceptions import ReviewValidationError

ITEM_SK = "ITEM"
COUNTER_SK = "COUNTER"
QDEF_PK = "QDEF"
REGISTRY_PK = "QREG"
TSDOC_OWNER_PREFIX = "RT#"
TSDOC_SWEEP_PK = "TS"
ANNOUNCE_SK = "ANNOUNCE"
SWEEP_SHARDS = 4

QUEUE_INDEX = "QueueIndex"
OPEN_INDEX = "OpenIndex"
OWNER_INDEX = "OwnerIndex"
SWEEP_INDEX = "SweepIndex"

CYCLE_ID_RE = re.compile(r"^[0-9a-f]{21}$")
MAX_CURSOR_LENGTH = 4096
_CURSOR_VERSION = 1
_CURSOR_DONE = "done"


def h16(doc_key: str) -> str:
    """First 16 hex digits of the SHA-256 of ``doc_key``."""
    return hashlib.sha256(doc_key.encode("utf-8")).hexdigest()[:16]


def doc_pk(doc_key: str) -> str:
    return f"DOC#{doc_key}"


def item_key(doc_key: str) -> dict[str, str]:
    return {"PK": doc_pk(doc_key), "SK": ITEM_SK}


def draft_prefix(cycle_id: str) -> str:
    return f"DRAFT#{cycle_id}#"


def draft_sk(cycle_id: str, section_id: str) -> str:
    return f"{draft_prefix(cycle_id)}{section_id}"


def history_prefix(cycle_id: str) -> str:
    return f"H#{cycle_id}#"


def history_sk(cycle_id: str, ms: int, hex4: Optional[str] = None) -> str:
    return f"{history_prefix(cycle_id)}{ms:013d}#{hex4 or secrets.token_hex(2)}"


def owner_pk(reviewer_key: str) -> str:
    return f"R#{reviewer_key}"


def counter_key(reviewer_key: str) -> dict[str, str]:
    return {"PK": owner_pk(reviewer_key), "SK": COUNTER_SK}


def queue_partition(queue: str) -> str:
    return f"Q#{queue}"


def qdef_key(queue: str) -> dict[str, str]:
    return {"PK": QDEF_PK, "SK": queue_partition(queue)}


def registry_key(queue: str) -> dict[str, str]:
    return {"PK": REGISTRY_PK, "SK": queue_partition(queue)}


def announce_key(queue: str) -> dict[str, str]:
    return {"PK": f"QUEUE#{queue}", "SK": ANNOUNCE_SK}


def tsdoc_pk(test_set_id: str) -> str:
    return f"TSDOC#{test_set_id}"


def tsdoc_key(test_set_id: str, object_key: str) -> dict[str, str]:
    return {"PK": tsdoc_pk(test_set_id), "SK": f"D#{object_key}"}


def tsdoc_owner_pk(reviewer_key: str) -> str:
    return f"{TSDOC_OWNER_PREFIX}{reviewer_key}"


def _time_sk(ms: int, doc_key: str) -> str:
    return f"{int(ms):013d}#{h16(doc_key)}"


def queue_sk(enqueued_ms: int, doc_key: str) -> str:
    return _time_sk(enqueued_ms, doc_key)


def owner_sk(claimed_ms: int, doc_key: str) -> str:
    return _time_sk(claimed_ms, doc_key)


def sweep_shard(doc_key: str) -> int:
    return int(h16(doc_key), 16) % SWEEP_SHARDS


def sweep_pk(shard: int) -> str:
    return f"S#{shard}"


def sweep_sk(sweep_at_ms: int, doc_key: str) -> str:
    return _time_sk(sweep_at_ms, doc_key)


def sweep_until(until_ms: int) -> str:
    """Upper bound for ``SweepSK <= :until`` covering every key due by ``until_ms``."""
    return f"{int(until_ms):013d}#~"


def sweep_keys(sweep_at_ms: int, doc_key: str) -> dict[str, str]:
    return {
        "SweepPK": sweep_pk(sweep_shard(doc_key)),
        "SweepSK": sweep_sk(sweep_at_ms, doc_key),
    }


def new_cycle_id(now_ms: int, rand_hex8: Optional[str] = None) -> str:
    """A cycle id: 13 decimal digits of milliseconds and 8 hex digits (21 in all)."""
    return f"{int(now_ms):013d}{rand_hex8 or secrets.token_hex(4)}"


def _bad_cursor() -> ReviewValidationError:
    return ReviewValidationError("BAD_CURSOR", "nextToken is not valid")


def encode_cursor(payload: dict[str, Any]) -> str:
    """Opaque ``nextToken``: base64url JSON without padding."""
    raw = json.dumps({"v": _CURSOR_VERSION, **payload}, separators=(",", ":"))
    token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
    if len(token) > MAX_CURSOR_LENGTH:
        raise ReviewValidationError("BAD_CURSOR", "the page cursor is too large")
    return token


def encode_single_cursor(last_evaluated_key: dict[str, Any]) -> str:
    return encode_cursor({"k": last_evaluated_key})


def encode_multi_cursor(positions: dict[str, Any]) -> str:
    """``positions`` maps a queue partition to its last key or ``None`` (exhausted)."""
    return encode_cursor(
        {"q": {p: (_CURSOR_DONE if k is None else k) for p, k in positions.items()}}
    )


def decode_cursor(token: str, *, multi: bool) -> Any:
    """Decode a cursor; any malformed input raises ``BAD_CURSOR``.

    Single-queue cursors return the ``LastEvaluatedKey`` dict. Multi-queue cursors
    return ``{partition: key-dict | None}`` (``None`` = that queue is exhausted).
    """
    if not isinstance(token, str) or not token or len(token) > MAX_CURSOR_LENGTH:
        raise _bad_cursor()
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (binascii.Error, ValueError, UnicodeError):
        raise _bad_cursor() from None
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise _bad_cursor()
    if not multi:
        key = payload.get("k")
        if not isinstance(key, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in key.items()
        ):
            raise _bad_cursor()
        return key
    positions = payload.get("q")
    if not isinstance(positions, dict):
        raise _bad_cursor()
    out: dict[str, Any] = {}
    for partition, key in positions.items():
        if not isinstance(partition, str) or not partition.startswith("Q#"):
            raise _bad_cursor()
        if key == _CURSOR_DONE:
            out[partition] = None
        elif isinstance(key, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in key.items()
        ):
            out[partition] = key
        else:
            raise _bad_cursor()
    return out
