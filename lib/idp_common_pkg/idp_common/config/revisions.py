# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Configuration Profile revision history.

A **Configuration Profile** is the named entity users manage (`default`,
`Production`, `lending`) — the RBAC object, the document-visibility partition,
and the activation target. A **Revision** is an immutable numbered snapshot of
one profile's configuration, cut on every save.

Why the split: before revisions existed, the only way to keep a previous
configuration was to mint a *new profile name* (`usecaseA_v1`, `usecaseA_v2`, …),
and each new name is a new RBAC object an admin has to grant. Revisions give
lineage a home that is *not* an access-control object, so a scoped Author can
iterate inside their own profile without an admin and without destroying the
previous config.

Storage
-------
- **Bodies** live in S3 at ``config_revisions/<profile>/<nnnnnn>.json.gz``.
  They are deliberately NOT DynamoDB items: ``ConfigurationTable`` is
  HASH-only, so listing requires a Scan, and DynamoDB bills a Scan on full
  item size regardless of ``ProjectionExpression``. Keeping multi-hundred-KB
  config blobs out of the table keeps every scan cheap. Because the revision
  number is part of the key, each object is write-once — immutability comes
  from the key, not from S3 object versioning (which the bucket also has).
- **Metadata** lives in ONE DynamoDB index item per profile,
  ``ConfigRevIndex#<profile>``, holding a list of small entries. Listing a
  profile's history is therefore a single ``get_item`` — no scan at all. The
  ``ConfigRevIndex#`` prefix deliberately does not match the
  ``begins_with(Configuration, "Config#")`` filter used to list profiles, so a
  revision can never leak into the profile list (which feeds the scope-filtered
  version dropdowns).
- **Counters** (``LatestRevision``, ``PublishedRevision``) live on the profile
  head item, so a single read of the head tells a consumer which revision is
  current.

Concurrency
-----------
Revision numbers are allocated with an atomic ``ADD`` on the head item, so two
simultaneous saves get distinct numbers rather than one silently overwriting the
other. The hot path (appending a new entry) uses DynamoDB's native
``list_append``, which cannot lose a concurrent append. The rare
read-modify-write operations (label, delete, prune) are guarded by an
``IndexSeq`` counter with one retry.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# DynamoDB key prefix for the per-profile revision index item. Chosen so it does
# NOT match begins_with(Configuration, "Config#") — see module docstring.
REVISION_INDEX_PREFIX = "ConfigRevIndex"

# S3 key prefix for revision bodies inside the Configuration bucket.
REVISION_S3_PREFIX = "config_revisions"

# Default number of revisions retained per profile. Published, labeled, and
# test-run-pinned revisions are retained regardless of this cap.
DEFAULT_REVISION_CAP = 20

# Profile names are used verbatim inside S3 keys, so they are restricted to the
# same character class the API validates (letters, digits, dot, dash,
# underscore). This makes path traversal (`../`) structurally impossible.
_SAFE_PROFILE_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

_MAX_LABEL_LEN = 100
_MAX_NOTES_LEN = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_numbers(node: Any) -> Any:
    """
    Coerce every number to ``float`` so a fingerprint survives a round-trip.

    A configuration reaches these fingerprints by two routes that disagree about
    the type of the same value: straight from a save (JSON, so ``float``/``int``)
    or read back from DynamoDB, whose only numeric type is ``Decimal``. Because
    ``json.dumps`` cannot serialize ``Decimal`` and falls back to ``default=str``,
    ``temperature: 0.0`` hashed as the number ``0.0`` on one route and the
    *string* ``"0.0"`` on the other — one configuration with two fingerprints,
    which defeats the whole point of a fingerprint. Normalizing to ``float``
    gives one rendering per value regardless of route, and also collapses the
    ``int``/``float`` split (``0`` and ``0.0`` are the same setting).

    A number and its string spelling now hash *differently*, where ``default=str``
    previously rendered ``Decimal("0.1")`` and the string ``"0.1"`` identically.
    That is a tightening rather than a loss: a quoted numeric in a config is a
    different value from an unquoted one, and the routes that produce a ``Decimal``
    never produce a string.

    Two collapses are deliberate. ``0`` and ``0.0`` become one value, because they
    are one setting. And values differing only past ~17 significant digits — or
    beyond ``2**53`` — hash alike, since ``float`` cannot separate them; no
    sampling parameter, token limit or class threshold lives anywhere near that
    range, which is why the precision is not worth preserving here.
    """
    # bool is an int subclass, so this test must come first or True becomes 1.0
    # and stops being distinguishable from the number.
    if isinstance(node, bool):
        return node
    if isinstance(node, (int, float, Decimal)):
        return float(node)
    if isinstance(node, dict):
        return {key: _canonical_numbers(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_canonical_numbers(value) for value in node]
    return node


def class_fingerprint(config_dict: Dict[str, Any]) -> str:
    """
    Stable hash of the document classes in a configuration.

    A revision that changes document classes invalidates a synced BDA project,
    so each revision records this fingerprint against the day a consumer compares
    the published revision's fingerprint with the one last synced to decide
    whether a BDA resync is required. Nothing does that yet: the value is recorded
    and surfaced (the SDK's ``ConfigRevisionInfo``, the revision list API) but no
    code path compares two of them, so BDA resync is not currently driven by it.
    """
    classes = _canonical_numbers(config_dict.get("classes"))
    canonical = json.dumps(classes, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# Configuration that changes what a confidence number MEANS. Two revisions with
# the same fingerprint produce comparable confidences; a model swap or an
# assessment change does not, which is why the fingerprint is recorded per
# revision (see the confidence-curve note in docs/configuration-profiles.md).
_CONFIDENCE_RELEVANT_PATHS = (
    ("assessment",),
    ("extraction", "model"),
    ("extraction", "temperature"),
    ("extraction", "top_k"),
    ("extraction", "top_p"),
)


def confidence_fingerprint(config_dict: Dict[str, Any]) -> str:
    """
    Stable hash of the configuration that determines confidence semantics.

    A revision that only edits, say, a classification prompt keeps the same
    fingerprint, so measurements taken under the previous revision remain
    comparable. A revision that swaps the extraction model does not.

    Sampling parameters make numeric normalization load-bearing here rather than
    merely tidy: ``temperature`` and ``top_p`` are exactly the values that arrive
    as ``float`` from a save and ``Decimal`` from DynamoDB. See
    :func:`_canonical_numbers`.
    """
    subset: Dict[str, Any] = {}
    for path in _CONFIDENCE_RELEVANT_PATHS:
        node: Any = config_dict
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        subset[".".join(path)] = _canonical_numbers(node)
    canonical = json.dumps(subset, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _coerce_int(value: Any, default: int = 0) -> int:
    """DynamoDB returns numbers as Decimal; normalize to int for callers/JSON."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ConfigRevisionStore:
    """
    Reads and writes Configuration Profile revisions.

    The store is *optional infrastructure*: when no configuration bucket is
    configured (``CONFIGURATION_BUCKET`` unset, e.g. an older deployment or a
    unit test that does not exercise history), ``enabled`` is False and every
    method degrades to a no-op or empty result. Callers must never fail a
    configuration save because history could not be recorded.
    """

    def __init__(
        self,
        table: Any,
        bucket: Optional[str] = None,
        cap: Optional[int] = None,
        s3_client: Optional[Any] = None,
    ):
        self.table = table
        self.bucket = (
            bucket if bucket is not None else os.environ.get("CONFIGURATION_BUCKET", "")
        )
        if cap is None:
            cap = _coerce_int(
                os.environ.get("CONFIG_REVISION_CAP"), DEFAULT_REVISION_CAP
            )
        self.cap = max(1, cap)
        self._s3 = s3_client
        if not self.bucket:
            logger.info(
                "CONFIGURATION_BUCKET is not set; configuration revision history is disabled"
            )

    # ----- plumbing ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.bucket)

    @property
    def s3(self) -> Any:
        if self._s3 is None:
            self._s3 = boto3.client("s3")
        return self._s3

    @staticmethod
    def _safe_profile(profile: str) -> str:
        if not profile or not _SAFE_PROFILE_RE.match(profile):
            raise ValueError(f"Invalid configuration profile name: {profile!r}")
        return profile

    @classmethod
    def index_key(cls, profile: str) -> Dict[str, str]:
        return {
            "Configuration": f"{REVISION_INDEX_PREFIX}#{cls._safe_profile(profile)}"
        }

    @classmethod
    def body_key(cls, profile: str, revision: int) -> str:
        return f"{REVISION_S3_PREFIX}/{cls._safe_profile(profile)}/{int(revision):06d}.json.gz"

    @staticmethod
    def _head_key(profile: str) -> Dict[str, str]:
        return {"Configuration": f"Config#{profile}"}

    # ----- allocation -------------------------------------------------------

    def next_number(self, profile: str) -> int:
        """
        Atomically allocate the next revision number for a profile.

        Requires the profile head item to already exist — the condition prevents
        creating a phantom ``Config#<profile>`` item carrying only a counter,
        which would otherwise show up in the profile list as a version with no
        configuration.
        """
        self._safe_profile(profile)
        response = self.table.update_item(
            Key=self._head_key(profile),
            UpdateExpression="ADD LatestRevision :one",
            ConditionExpression="attribute_exists(Configuration)",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return _coerce_int(response.get("Attributes", {}).get("LatestRevision"), 1)

    def set_published(self, profile: str, revision: int) -> None:
        """Point the profile head at the revision that reflects its content."""
        try:
            self.table.update_item(
                Key=self._head_key(profile),
                UpdateExpression="SET PublishedRevision = :n",
                ConditionExpression="attribute_exists(Configuration)",
                ExpressionAttributeValues={":n": int(revision)},
            )
        except ClientError as e:
            logger.warning(f"Could not set PublishedRevision for {profile}: {e}")

    # ----- bodies -----------------------------------------------------------

    def put_body(
        self, profile: str, revision: int, config_dict: Dict[str, Any]
    ) -> Tuple[str, int]:
        """Write a revision body to S3. Returns (key, uncompressed size)."""
        key = self.body_key(profile, revision)
        payload = json.dumps(config_dict, default=str, separators=(",", ":")).encode(
            "utf-8"
        )
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=gzip.compress(payload),
            ContentType="application/json",
            ContentEncoding="gzip",
        )
        return key, len(payload)

    def get_body(self, profile: str, revision: int) -> Optional[Dict[str, Any]]:
        """Read a revision body from S3, or None if it is gone."""
        if not self.enabled:
            return None
        key = self.body_key(profile, revision)
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
            raw = response["Body"].read()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                logger.warning(f"Revision body not found: s3://{self.bucket}/{key}")
                return None
            raise
        try:
            return json.loads(gzip.decompress(raw).decode("utf-8"))
        except (OSError, gzip.BadGzipFile):
            # Tolerate an uncompressed body (defensive; all writes are gzipped).
            return json.loads(raw.decode("utf-8"))

    def delete_body(self, profile: str, revision: int) -> None:
        try:
            self.s3.delete_object(
                Bucket=self.bucket, Key=self.body_key(profile, revision)
            )
        except ClientError as e:
            logger.warning(f"Could not delete revision body {profile} r{revision}: {e}")

    # ----- index ------------------------------------------------------------

    def _read_index_item(self, profile: str) -> Dict[str, Any]:
        try:
            response = self.table.get_item(Key=self.index_key(profile))
        except ClientError as e:
            logger.warning(f"Could not read revision index for {profile}: {e}")
            return {}
        return response.get("Item") or {}

    @staticmethod
    def _normalize(entry: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "revision": _coerce_int(entry.get("revision")),
            "createdAt": entry.get("createdAt"),
            "createdBy": entry.get("createdBy"),
            "label": entry.get("label"),
            "notes": entry.get("notes"),
            "sizeBytes": _coerce_int(entry.get("sizeBytes")),
            "classFingerprint": entry.get("classFingerprint"),
            "confidenceFingerprint": entry.get("confidenceFingerprint"),
            "pinned": bool(entry.get("pinned", False)),
        }

    def list(self, profile: str) -> List[Dict[str, Any]]:
        """All retained revisions for a profile, newest first."""
        if not self.enabled:
            return []
        item = self._read_index_item(profile)
        entries = [self._normalize(e) for e in item.get("Revisions", [])]
        entries.sort(key=lambda e: e["revision"], reverse=True)
        return entries

    def append_index(self, profile: str, entry: Dict[str, Any]) -> None:
        """
        Append one entry using DynamoDB's native list_append.

        list_append on the server side cannot lose a concurrent append, which a
        read-modify-write of the whole list could.
        """
        self.table.update_item(
            Key=self.index_key(profile),
            UpdateExpression=(
                "SET Revisions = list_append(if_not_exists(Revisions, :empty), :new), "
                "UpdatedAt = :ts ADD IndexSeq :one"
            ),
            ExpressionAttributeValues={
                ":empty": [],
                ":new": [entry],
                ":ts": _now(),
                ":one": 1,
            },
        )

    def _rewrite_index(self, profile: str, mutate) -> bool:
        """
        Read-modify-write the entry list, guarded by IndexSeq with one retry.

        `mutate(entries)` returns the new list, or None to abort with no write.
        """
        for attempt in (1, 2):
            item = self._read_index_item(profile)
            entries = list(item.get("Revisions", []))
            seq = _coerce_int(item.get("IndexSeq"))
            updated = mutate(entries)
            if updated is None:
                return False
            try:
                self.table.update_item(
                    Key=self.index_key(profile),
                    UpdateExpression="SET Revisions = :r, UpdatedAt = :ts, IndexSeq = :next",
                    ConditionExpression="attribute_not_exists(IndexSeq) OR IndexSeq = :seq",
                    ExpressionAttributeValues={
                        ":r": updated,
                        ":ts": _now(),
                        ":seq": seq,
                        ":next": seq + 1,
                    },
                )
                return True
            except ClientError as e:
                if (
                    e.response.get("Error", {}).get("Code")
                    != "ConditionalCheckFailedException"
                ):
                    raise
                logger.info(
                    f"Revision index for {profile} changed underneath us "
                    f"(attempt {attempt}); retrying"
                )
        logger.warning(
            f"Gave up rewriting revision index for {profile} after a conflict"
        )
        return False

    def update_entry(self, profile: str, revision: int, **changes: Any) -> bool:
        """Apply metadata changes (label, notes, pinned) to one revision entry."""
        if not self.enabled:
            return False
        target = int(revision)

        def mutate(entries):
            found = False
            for entry in entries:
                if _coerce_int(entry.get("revision")) == target:
                    entry.update(changes)
                    found = True
            return entries if found else None

        return self._rewrite_index(profile, mutate)

    def mark_pinned(self, profile: str, revision: int) -> bool:
        """
        Mark a revision as pinned by a test run so retention never deletes it.

        A comparison between two test runs is only interpretable while both
        runs' configurations still exist.
        """
        return self.update_entry(profile, revision, pinned=True)

    def remove_entry(self, profile: str, revision: int) -> bool:
        target = int(revision)

        def mutate(entries):
            remaining = [e for e in entries if _coerce_int(e.get("revision")) != target]
            return remaining if len(remaining) != len(entries) else None

        return self._rewrite_index(profile, mutate)

    # ----- lifecycle --------------------------------------------------------

    def cut(
        self,
        profile: str,
        config_dict: Dict[str, Any],
        created_by: Optional[str] = None,
        notes: Optional[str] = None,
        label: Optional[str] = None,
        publish: bool = True,
    ) -> Optional[int]:
        """
        Record `config_dict` as the profile's next revision.

        Returns the revision number, or None when history is disabled.
        """
        if not self.enabled:
            return None
        revision = self.next_number(profile)
        _, size = self.put_body(profile, revision, config_dict)
        entry = {
            "revision": revision,
            "createdAt": _now(),
            "createdBy": created_by or "system",
            "label": (label or "")[:_MAX_LABEL_LEN] or None,
            "notes": (notes or "")[:_MAX_NOTES_LEN] or None,
            "sizeBytes": size,
            "classFingerprint": class_fingerprint(config_dict),
            "confidenceFingerprint": confidence_fingerprint(config_dict),
            "pinned": False,
        }
        self.append_index(profile, entry)
        if publish:
            self.set_published(profile, revision)
        logger.info(
            f"Cut revision r{revision} of configuration profile '{profile}' "
            f"({size:,} bytes uncompressed)"
        )
        self.prune(profile, published=revision if publish else None)
        return revision

    def prune(self, profile: str, published: Optional[int] = None) -> List[int]:
        """
        Enforce the retention cap. Returns the revision numbers deleted.

        Always kept: the published revision, anything a user labeled, and
        anything a test run pinned. A count-based cap cannot be expressed as an
        S3 lifecycle rule, which is why this runs in the application.
        """
        if not self.enabled:
            return []
        entries = self.list(profile)  # newest first
        keep_floor = self.cap
        deletable = []
        for position, entry in enumerate(entries):
            if position < keep_floor:
                continue
            if entry["pinned"] or entry["label"]:
                continue
            if published is not None and entry["revision"] == int(published):
                continue
            deletable.append(entry["revision"])

        for revision in deletable:
            if self.remove_entry(profile, revision):
                self.delete_body(profile, revision)
                logger.info(f"Pruned revision r{revision} of profile '{profile}'")
        return deletable

    def delete(self, profile: str, revision: int) -> bool:
        """Delete one revision (metadata + body)."""
        if not self.enabled:
            return False
        if self.remove_entry(profile, revision):
            self.delete_body(profile, revision)
            logger.info(f"Deleted revision r{revision} of profile '{profile}'")
            return True
        return False

    def delete_profile(self, profile: str) -> None:
        """Drop all revision history for a profile (called when it is deleted)."""
        if not self.enabled:
            return
        for entry in self.list(profile):
            self.delete_body(profile, entry["revision"])
        try:
            self.table.delete_item(Key=self.index_key(profile))
        except ClientError as e:
            logger.warning(f"Could not delete revision index for {profile}: {e}")
