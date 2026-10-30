# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test Studio labels: the one writer of a reviewed label, and section drafts.

A Test Studio document's ground truth is one ``result.json`` per section under
``<testSetId>/baseline/<objectKey>/sections/<sectionId>/``. Two resolvers write
it: ``saveTestSetDocumentSection`` (authored ground truth, which has no review
copy) and ``completeSectionReview`` (a document with a review copy). Both go
through :meth:`TestSetLabels.write_reviewed`, so a reviewed label always carries
the same provenance: ``labelSource = reviewed-human``, an ``_editHistory`` entry
with the field diffs (the trail capped at :data:`MAX_EDIT_HISTORY_ENTRIES`), and
the confidence-curve observations the review produces.

A **draft** is a section's saved edits that are not reviewed and start nothing:
``<testSetId>/drafts/<objectKey>/sections/<sectionId>/<revision:06d>-<suffix>.json``
in the test-set bucket (each write has its own object), indexed by a ``Draft#<sectionId>`` attribute on the
document's ``TSDOC`` item. Drafts live outside ``baseline/``, so a published
version, a test run and the label summaries never read them. Writing a reviewed
label deletes that section's draft.

Draft writes are S3 first, then a conditional ``UpdateItem`` on the revision the
editor last saw; a stale write fails ``STALE_DRAFT`` and its object is removed.
Errors are the package's domain exceptions; the resolvers map them to the
builtins the API dispatcher knows.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from botocore.exceptions import ClientError
from pydantic import BaseModel

from . import keys
from .exceptions import ReviewConflict, ReviewItemNotFound, ReviewValidationError
from .testset_summaries import LABEL_SOURCE_HUMAN

logger = logging.getLogger(__name__)

#: The trail on a repeatedly reviewed label is capped; the newest entries are kept.
MAX_EDIT_HISTORY_ENTRIES = 50
#: The largest section label (or draft) accepted, as JSON text.
MAX_SECTION_LABEL_BYTES = 2_000_000
SECTION_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")

__all__ = [
    "LABEL_SOURCE_HUMAN",
    "MAX_EDIT_HISTORY_ENTRIES",
    "MAX_SECTION_LABEL_BYTES",
    "SECTION_ID_RE",
    "LabelEditor",
    "ReviewedLabel",
    "SectionDraft",
    "DraftMove",
    "TestSetLabels",
    "baseline_key",
    "draft_prefix",
    "field_diffs",
    "append_edit_history",
    "drafts_from_item",
]


class LabelEditor(BaseModel):
    """Who is writing, from the verified token."""

    name: str = "unknown"
    email: str = ""


class ReviewedLabel(BaseModel):
    """A reviewed label as written."""

    key: str
    label: dict
    previous: Optional[dict] = None
    #: The section was already reviewed before this write.
    was_reviewed: bool
    #: Confidence-curve observations recorded.
    observations: int = 0


class SectionDraft(BaseModel):
    """One section's draft, as its ``Draft#<sectionId>`` attribute records it."""

    section_id: str
    revision: int
    s3_key: str
    updated_at: str
    updated_by: str = ""

    def view(self, bucket: str) -> dict:
        """The API's ``TestSetSectionDraft``."""
        return {
            "sectionId": self.section_id,
            "revision": self.revision,
            "draftUri": f"s3://{bucket}/{self.s3_key}",
            "updatedAt": self.updated_at,
            "updatedBy": self.updated_by or None,
        }


class DraftMove(BaseModel):
    """A draft that follows its section to a new id after a regroup."""

    from_id: str
    to_id: str
    #: The section's pages after the regroup (0-based).
    page_indices: list[int]
    document_class: Optional[str] = None


def baseline_key(test_set_id: str, object_key: str, section_id: str) -> str:
    return f"{test_set_id}/baseline/{object_key}/sections/{section_id}/result.json"


def draft_prefix(
    test_set_id: str, object_key: str, section_id: Optional[str] = None
) -> str:
    base = f"{test_set_id}/drafts/{object_key}/sections/"
    return f"{base}{section_id}/" if section_id is not None else base


def _draft_key(
    test_set_id: str, object_key: str, section_id: str, revision: int
) -> str:
    """A new draft object's key. Each write has its own (the revision and a random
    suffix), so a write that loses the revision race never overwrites the draft it
    lost to."""
    suffix = secrets.token_hex(4)
    return (
        f"{draft_prefix(test_set_id, object_key, section_id)}"
        f"{revision:06d}-{suffix}.json"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def field_diffs(previous: Optional[dict], saved: dict) -> dict:
    """Field path → ``{originalValue, newValue}`` for every value a review changed.

    Uses the same flattening as the confidence curve, so "changed" means the same
    thing to the audit trail and to the calibration signal.
    """
    if not previous:
        return {}
    try:
        from idp_common.evaluation.curve_store import flatten_values

        before = flatten_values(previous.get("inference_result") or {})
        after = flatten_values(saved.get("inference_result") or {})
    except Exception as e:  # noqa: BLE001 — provenance must not fail the save
        logger.warning(f"Could not diff review changes: {e}")
        return {}

    diffs = {}
    for path in set(before) | set(after):
        original = before.get(path)
        new = after.get(path)
        if original != new:
            diffs[path] = {"originalValue": original, "newValue": new}
    return diffs


def append_edit_history(
    previous: Optional[dict], saved: dict, editor: LabelEditor, source: str
) -> None:
    """Record who changed what, in the label itself (``_editHistory``).

    The ground-truth editor reads provenance from the label JSON, so a review
    recorded only in DynamoDB would be invisible to it. The field-level diff is
    taken against the label being replaced.
    """
    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "editedBy": editor.name or "unknown",
        "editedByEmail": editor.email or "",
        "source": source,
    }
    diffs = field_diffs(previous, saved)
    if diffs:
        entry["baselineEdits"] = {
            "changedFields": list(diffs.keys()),
            "changeCount": len(diffs),
            "diffs": diffs,
        }

    # The trail is server-owned: seed it from the stored label, since a client that
    # does not round-trip _editHistory would otherwise erase every prior entry.
    # Taking the longer of the two also stops a client from truncating it.
    stored = (previous or {}).get("_editHistory")
    incoming = saved.get("_editHistory")
    history = stored if isinstance(stored, list) else []
    if isinstance(incoming, list) and len(incoming) > len(history):
        history = incoming
    saved["_editHistory"] = [*history, entry][-MAX_EDIT_HISTORY_ENTRIES:]


def drafts_from_item(item: Optional[dict]) -> list[SectionDraft]:
    """The section drafts a ``TSDOC`` item records."""
    return [SectionDraft(**entry) for entry in keys.tsdoc_draft_entries(item)]


def _missing(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound")


def _conditional_failed(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class TestSetLabels:
    """Reviewed labels and section drafts of Test Studio documents.

    ``s3_client`` reaches the test-set ``bucket``. ``doc_table`` is HITLTable (the
    ``TSDOC`` items that index drafts); ``curve_table`` is the tracking table (the
    confidence curve). Either may be None, which turns off what needs it.
    """

    __test__ = False  # not a pytest class, despite the name

    def __init__(
        self,
        s3_client: Any,
        bucket: str,
        doc_table: Any = None,
        curve_table: Any = None,
    ):
        self.s3 = s3_client
        self.bucket = bucket
        self.doc_table = doc_table
        self.curve_table = curve_table

    # ----------------------------------------------------------------- labels

    def _read_json(self, key: str) -> Optional[dict]:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as e:
            if _missing(e):
                return None
            raise
        value = json.loads(body)
        return value if isinstance(value, dict) else None

    def read_label(
        self, test_set_id: str, object_key: str, section_id: str
    ) -> Optional[dict]:
        """A section's label, or None when it has none."""
        return self._read_json(baseline_key(test_set_id, object_key, section_id))

    def write_reviewed(
        self,
        test_set_id: str,
        object_key: str,
        section_id: str,
        data: Optional[dict],
        *,
        editor: LabelEditor,
        source: str,
        require_existing: bool,
        skip_if_reviewed: bool,
    ) -> Optional[ReviewedLabel]:
        """Write a section's label as reviewed by ``editor``.

        ``data`` None confirms the stored label as it is. Sets ``labelSource`` to
        ``reviewed-human``, appends the ``_editHistory`` entry, writes the label,
        then records the curve observations and deletes the section's draft (both
        best-effort, logged at WARNING).

        Returns None, writing nothing, when there is nothing to confirm or
        ``skip_if_reviewed`` and the label is already reviewed.

        Raises:
            ReviewItemNotFound: ``NO_LABEL`` when ``require_existing`` and the
                section has no label.
        """
        key = baseline_key(test_set_id, object_key, section_id)
        try:
            previous = self._read_json(key)
        except (ClientError, ValueError) as e:
            if require_existing:
                raise
            # The label being replaced is evidence, not a precondition: write anyway.
            logger.warning(f"Could not read the label being replaced at {key}: {e}")
            previous = None
        if require_existing and previous is None:
            raise ReviewItemNotFound(
                "NO_LABEL", f"section {section_id} of '{object_key}' has no labels"
            )
        was_reviewed = (previous or {}).get("labelSource") == LABEL_SOURCE_HUMAN
        if skip_if_reviewed and was_reviewed:
            return None
        if data is None:
            if previous is None:
                logger.info(f"No label to confirm at s3://{self.bucket}/{key}")
                return None
            # Confirming asserts the current values are the ground truth, so the same
            # provenance, revision entry and curve observations apply.
            label = json.loads(json.dumps(previous))
        else:
            label = dict(data)
        label["labelSource"] = LABEL_SOURCE_HUMAN
        append_edit_history(previous, label, editor, source)
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(label, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info(f"Wrote a reviewed label to s3://{self.bucket}/{key}")
        observations = self.record_curve_observations(test_set_id, previous, label)
        try:
            self.delete_draft(test_set_id, object_key, section_id)
        except Exception as e:  # noqa: BLE001 — the label is written; a stale draft re-applies the same edits
            logger.warning(
                f"Could not delete the draft of {test_set_id}/{object_key}#{section_id}: {e}"
            )
        return ReviewedLabel(
            key=key,
            label=label,
            previous=previous,
            was_reviewed=was_reviewed,
            observations=observations,
        )

    def record_curve_observations(
        self, test_set_id: str, previous: Optional[dict], saved: dict
    ) -> int:
        """Record confidence→accuracy observations from one review.

        A field the reviewer left alone was predicted correctly; a changed one was
        not. Best-effort: the curve is an optimization and never fails a save.
        Returns how many observations were accepted.
        """
        if not previous or self.curve_table is None:
            return 0
        try:
            from idp_common.evaluation.curve_store import (
                CurveStore,
                observations_from_baseline_review,
            )

            observations = observations_from_baseline_review(previous, saved)
            if not observations:
                return 0
            # Keyed by the config that produced these labels and by the revision
            # family that drafted them (#698), so a later config or model change
            # starts a new curve instead of inheriting this one.
            label_meta = previous.get("metadata") or {}
            accepted = CurveStore(self.curve_table).add_observations(
                test_set_id,
                observations,
                config_version=label_meta.get("config_version"),
                source="review",
                fingerprint=label_meta.get("confidence_fingerprint") or None,
            )
            logger.info(
                f"Recorded {accepted} confidence-curve observation(s) for test set "
                f"{test_set_id} from review"
            )
            return int(accepted or 0)
        except Exception as e:  # noqa: BLE001 — never break a review over the curve
            logger.warning(
                f"Could not record confidence-curve observations for {test_set_id}: {e}"
            )
            return 0

    # ----------------------------------------------------------------- drafts

    def _doc_item(self, test_set_id: str, object_key: str) -> Optional[dict]:
        if self.doc_table is None:
            return None
        return self.doc_table.get_item(
            Key=keys.tsdoc_key(test_set_id, object_key), ConsistentRead=True
        ).get("Item")

    def drafts(self, test_set_id: str, object_key: str) -> list[SectionDraft]:
        """The document's section drafts."""
        return drafts_from_item(self._doc_item(test_set_id, object_key))

    def save_draft(
        self,
        test_set_id: str,
        object_key: str,
        section_id: str,
        data: dict,
        base_revision: Optional[int],
        *,
        editor: LabelEditor,
    ) -> SectionDraft:
        """Save one section's edits as a draft: not reviewed, and nothing else changes.

        ``base_revision`` is the draft revision the editor last loaded (0 for none);
        None takes whatever is current. The label, the document's summary, the queue
        counts and the confidence curve are left alone.

        Raises:
            ReviewValidationError: ``BAD_ARGUMENT``, ``DRAFT_TOO_LARGE`` or
                ``NOT_IN_SET``.
            ReviewItemNotFound: ``NO_LABEL`` when the section has no label.
            ReviewConflict: ``STALE_DRAFT`` when a newer draft was saved meanwhile.
        """
        if not SECTION_ID_RE.fullmatch(section_id or ""):
            raise ReviewValidationError("BAD_ARGUMENT", "invalid section id")
        if not isinstance(data, dict):
            raise ReviewValidationError("BAD_ARGUMENT", "data must be a JSON object")
        body = json.dumps(data, indent=2)
        if len(body) > MAX_SECTION_LABEL_BYTES:
            raise ReviewValidationError(
                "DRAFT_TOO_LARGE", "the section's edits are too large"
            )
        if self.doc_table is None:
            raise ReviewValidationError("NOT_IN_SET", "drafts are not configured")
        item = self._doc_item(test_set_id, object_key)
        if item is None:
            raise ReviewValidationError(
                "NOT_IN_SET", f"'{object_key}' is not in this test set"
            )
        if self.read_label(test_set_id, object_key, section_id) is None:
            raise ReviewItemNotFound(
                "NO_LABEL", f"section {section_id} of '{object_key}' has no labels"
            )

        attr = keys.tsdoc_draft_attr(section_id)
        current = next(
            (d for d in drafts_from_item(item) if d.section_id == section_id), None
        )
        base = (
            int(base_revision)
            if base_revision is not None
            else (current.revision if current else 0)
        )
        draft = SectionDraft(
            section_id=section_id,
            revision=base + 1,
            s3_key=_draft_key(test_set_id, object_key, section_id, base + 1),
            updated_at=_now(),
            updated_by=editor.email or editor.name,
        )
        self.s3.put_object(
            Bucket=self.bucket,
            Key=draft.s3_key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )
        try:
            self.doc_table.update_item(
                Key=keys.tsdoc_key(test_set_id, object_key),
                UpdateExpression="SET #d = :d",
                ConditionExpression=(
                    "attribute_exists(PK) AND "
                    "(attribute_not_exists(#d) OR #d.Revision = :cur)"
                ),
                ExpressionAttributeNames={"#d": attr},
                ExpressionAttributeValues={
                    ":d": {
                        "Revision": draft.revision,
                        "S3Key": draft.s3_key,
                        "UpdatedAt": draft.updated_at,
                        "UpdatedBy": draft.updated_by,
                    },
                    ":cur": base,
                },
            )
        except ClientError as e:
            if not _conditional_failed(e):
                raise
            # Lost the race: the object this write made indexes nothing.
            self._delete_objects([draft.s3_key])
            raise ReviewConflict(
                "STALE_DRAFT", "a newer draft of this section was saved"
            ) from None
        if current and current.s3_key != draft.s3_key:
            self._delete_objects([current.s3_key])
        logger.info(
            f"Saved draft revision {draft.revision} of "
            f"{test_set_id}/{object_key}#{section_id}"
        )
        return draft

    def delete_draft(self, test_set_id: str, object_key: str, section_id: str) -> None:
        """Delete a section's draft: its attribute and its objects."""
        if self.doc_table is not None:
            try:
                self.doc_table.update_item(
                    Key=keys.tsdoc_key(test_set_id, object_key),
                    UpdateExpression="REMOVE #d",
                    ConditionExpression="attribute_exists(PK)",
                    ExpressionAttributeNames={"#d": keys.tsdoc_draft_attr(section_id)},
                )
            except ClientError as e:
                if not _conditional_failed(e):
                    raise
        self._delete_prefix(draft_prefix(test_set_id, object_key, section_id))

    def delete_document_drafts(self, test_set_id: str, object_key: str) -> None:
        """Delete every draft of a document: its attributes (when its item exists)
        and every object under its draft prefix."""
        item = self._doc_item(test_set_id, object_key)
        attrs = [keys.tsdoc_draft_attr(d.section_id) for d in drafts_from_item(item)]
        if item is not None and attrs:
            names = {f"#d{i}": a for i, a in enumerate(attrs)}
            try:
                self.doc_table.update_item(
                    Key=keys.tsdoc_key(test_set_id, object_key),
                    UpdateExpression="REMOVE " + ", ".join(names),
                    ConditionExpression="attribute_exists(PK)",
                    ExpressionAttributeNames=names,
                )
            except ClientError as e:
                if not _conditional_failed(e):
                    raise
        self._delete_prefix(draft_prefix(test_set_id, object_key))

    def move_drafts(
        self,
        test_set_id: str,
        object_key: str,
        moves: list[DraftMove],
        removed: list[str],
    ) -> None:
        """Make drafts follow their sections through a regroup.

        Each moved draft is rewritten at its new section id, with the section's new
        ``split_document.page_indices`` and ``document_class.type``, so applying it
        does not restore the old grouping. A removed section's draft is deleted. One
        ``UpdateItem`` sets the moved attributes and removes the stale ones.
        """
        item = self._doc_item(test_set_id, object_key)
        existing = {d.section_id: d for d in drafts_from_item(item)}
        if item is None or not existing:
            return
        sets: dict[str, dict] = {}
        for move in moves:
            draft = existing.get(move.from_id)
            if draft is None:
                continue
            content = self._read_json(draft.s3_key) or {}
            split = content.get("split_document")
            content["split_document"] = {
                **(split if isinstance(split, dict) else {}),
                "page_indices": list(move.page_indices),
            }
            if move.document_class is not None:
                document_class = content.get("document_class")
                content["document_class"] = {
                    **(document_class if isinstance(document_class, dict) else {}),
                    "type": move.document_class,
                }
            revision = (
                max(
                    draft.revision,
                    existing[move.to_id].revision if move.to_id in existing else 0,
                )
                + 1
            )
            moved = SectionDraft(
                section_id=move.to_id,
                revision=revision,
                s3_key=_draft_key(test_set_id, object_key, move.to_id, revision),
                updated_at=draft.updated_at,
                updated_by=draft.updated_by,
            )
            self.s3.put_object(
                Bucket=self.bucket,
                Key=moved.s3_key,
                Body=json.dumps(content, indent=2).encode("utf-8"),
                ContentType="application/json",
            )
            sets[move.to_id] = {
                "Revision": moved.revision,
                "S3Key": moved.s3_key,
                "UpdatedAt": moved.updated_at,
                "UpdatedBy": moved.updated_by,
            }
        stale = [sid for sid in existing if sid not in sets]
        if not sets and not stale:
            return
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        clauses = []
        for i, (sid, value) in enumerate(sets.items()):
            names[f"#s{i}"] = keys.tsdoc_draft_attr(sid)
            values[f":s{i}"] = value
            clauses.append(f"#s{i} = :s{i}")
        expression = ("SET " + ", ".join(clauses)) if clauses else ""
        removes = []
        for i, sid in enumerate(stale):
            names[f"#r{i}"] = keys.tsdoc_draft_attr(sid)
            removes.append(f"#r{i}")
        if removes:
            expression += (" " if expression else "") + "REMOVE " + ", ".join(removes)
        kwargs: dict[str, Any] = {
            "Key": keys.tsdoc_key(test_set_id, object_key),
            "UpdateExpression": expression,
            "ExpressionAttributeNames": names,
        }
        if values:
            kwargs["ExpressionAttributeValues"] = values
        self.doc_table.update_item(**kwargs)
        kept = {v["S3Key"] for v in sets.values()}
        self._delete_objects(
            [d.s3_key for d in existing.values() if d.s3_key not in kept]
        )
        if removed:
            logger.info(
                f"Dropped the drafts of removed sections {removed} of "
                f"{test_set_id}/{object_key}"
            )

    # -------------------------------------------------------------- S3 helpers

    def _delete_objects(self, keys_to_delete: list[str]) -> None:
        """Best-effort delete of draft objects that no longer index anything."""
        for key in keys_to_delete:
            try:
                self.s3.delete_object(Bucket=self.bucket, Key=key)
            except ClientError as e:
                logger.warning(f"Could not delete s3://{self.bucket}/{key}: {e}")

    def _delete_prefix(self, prefix: str) -> None:
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects:
                self.s3.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": objects, "Quiet": True}
                )
