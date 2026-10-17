# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test Studio document summaries, computed from the labels in S3.

A test set's labels live in S3 (``<set>/baseline/<doc>/sections/<n>/result.json``);
the queue page reads each document's summary from DynamoDB instead
(:mod:`.test_set_docs`). This module computes those summaries: label source, alert
and field counts, the weakest confidence and its threshold, classes and sections.
The test-set resolver uses it for one page of documents, and the test-set file
copier for a whole set in the background (:func:`summarize_test_set`), so no queue
request has to read a set's labels.

Standard library, boto3 and ``idp_common.evaluation.curve_store`` only, so the
layer-1 Lambdas can import it.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import time
from typing import Any, Optional

from idp_common.evaluation.curve_store import field_child_path, list_child_path

logger = logging.getLogger(__name__)

LABEL_SOURCE_DRAFT = "draft-machine"
LABEL_SOURCE_HUMAN = "reviewed-human"
# Ground truth supplied with the test set. Authoritative — draft labeling will not
# overwrite it — but not review work, so it never counts toward annotation progress.
LABEL_SOURCE_UPLOADED = "uploaded"

#: Documents whose sections are read in one batch.
SUMMARY_PAGE_SIZE = 200

_SECTION_RE = re.compile(r"^(.+)/sections/([^/]+)/result\.json$")


def walk_confidence(explainability_info):
    """Collect (confidence, confidence_threshold) pairs from explainability_info.

    The shape is nested and irregular — ``{field: {"confidence": 0.9}}`` for
    scalars, nested dicts for compound fields (``PayPeriod.StartDate``), lists of
    such dicts for tables — so walk it and collect every ``confidence`` leaf
    rather than assuming a depth.
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            value = node.get("confidence")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                threshold = node.get("confidence_threshold")
                if not isinstance(threshold, (int, float)) or isinstance(
                    threshold, bool
                ):
                    threshold = None
                found.append((float(value), threshold))
            for key, child in node.items():
                if key != "confidence":
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(explainability_info)
    return found


def absent_field_paths(inference_result):
    """Field *paths* whose extracted value is absent (null, ``""``, or an empty list).

    A field the document does not contain is assessed at confidence 0.0, which is a
    correct reading of a blank box but indistinguishable from real uncertainty once
    reduced to a number. Callers exclude these from headline scores.

    Paths, not bare leaf names: one empty ``Description`` cell would otherwise
    exclude *every* Description score in a 200-row transaction table, understating
    review need on exactly the table-heavy documents this feature targets.

    Paths are keyed by ``field_child_path`` / ``list_child_path`` — the same rule
    ``curve_store.flatten_values`` uses — so they are interchangeable with a stored
    confidence curve's keys and with :func:`walk_confidence_named`'s.

    **Why this is not simply a call to ``flatten_values``.** That function records
    scalar leaves, and an empty list has no leaf to record. An empty list is exactly
    what "the document does not contain this table" looks like, so it has to be
    collected here. Taking the path *rule* from the shared module rather than the
    whole traversal is what keeps the keys aligned without giving up that case.
    """
    absent = set()

    def walk(node, prefix=""):
        if isinstance(node, dict):
            for key, child in node.items():
                walk(child, field_child_path(prefix, key))
        elif isinstance(node, list):
            if not node and prefix:
                absent.add(prefix)
            for index, child in enumerate(node):
                walk(child, list_child_path(prefix, index))
        elif prefix and (node is None or node == ""):
            absent.add(prefix)

    walk(inference_result)
    return absent


def min_confidence(explainability_info, inference_result=None):
    """Lowest confidence among fields the document actually has a value for.

    Returns None when the payload carries no confidence at all (e.g. assessment
    disabled), so callers can distinguish "no confidence data" from "confidence 0".

    Absent fields are excluded: their 0.0 describes the emptiest box on the form,
    not the quality of the extraction, so including it would report 0.0% for a
    document whose populated fields all score above 0.99. The per-field 0.0 stays in
    explainability_info; it just does not define the headline score.
    """
    found = walk_confidence(explainability_info)
    if not found:
        return None
    if inference_result is not None:
        absent = absent_field_paths(inference_result)
        populated = [
            (c, t)
            for c, t, name in walk_confidence_named(explainability_info)
            if name not in absent
        ]
        # An entirely absent extraction falls back to the unfiltered minimum, so it
        # reports 0 rather than "no data".
        if populated:
            return min(c for c, _ in populated)
    return min(c for c, _ in found)


def walk_confidence_named(explainability_info):
    """As :func:`walk_confidence`, plus the field *path* each score belongs to.

    Paths are built by ``field_child_path`` / ``list_child_path``, so they line up
    with :func:`absent_field_paths`' per occurrence rather than per field name —
    see that function for why the distinction matters on tables — and with
    ``curve_store.flatten_confidences``' keys, which is what a caller joining one of
    these paths to a stored confidence curve would need.

    **Why this is not simply a call to ``flatten_confidences``.** That function
    returns path → confidence. This one also has to return the per-field
    ``confidence_threshold`` sitting beside each score, which ``flatten_confidences``
    deliberately skips, because :func:`alert_counts` compares each score against its
    own field's threshold. Taking the path *rule* from the shared module rather than
    the whole traversal is what keeps the keys aligned without giving that up.

    What is shared is how a path is KEYED, then, not which leaves are collected: this
    walk descends into a ``geometry`` or ``confidence_threshold`` subtree, which
    ``flatten_confidences`` skips, so a ``confidence`` leaf nested inside one would be
    reported here and not there. Neither carries one today; the difference is in the
    leaf set rather than in the path shape, and it is the path shape a join needs.
    """
    found = []

    def walk(node, prefix=""):
        if isinstance(node, dict):
            value = node.get("confidence")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                threshold = node.get("confidence_threshold")
                if not isinstance(threshold, (int, float)) or isinstance(
                    threshold, bool
                ):
                    threshold = None
                found.append((float(value), threshold, prefix or None))
            for key, child in node.items():
                if key != "confidence":
                    walk(child, field_child_path(prefix, key))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, list_child_path(prefix, index))

    walk(explainability_info)
    return found


def confidence_threshold(explainability_info, inference_result=None):
    """The configured alert threshold for the weakest field, if it carries one.

    Reported alongside minConfidence so the UI colors against the config's threshold
    rather than hardcoded bands: 0.85 fails under a 0.9 threshold and passes under
    0.8.
    """
    found = walk_confidence_named(explainability_info)
    if not found:
        return None
    if inference_result is not None:
        absent = absent_field_paths(inference_result)
        populated = [f for f in found if f[2] not in absent]
        if populated:
            found = populated
    # Tie to the same field minConfidence reports.
    return min(found, key=lambda triple: triple[0])[1]


# The bar a field is measured against when its own assessment carries no threshold.
# Must match the UI's fallback, or alert counts differ across the API.
DEFAULT_ALERT_THRESHOLD = 0.8


def alert_counts(explainability_info, inference_result=None):
    """(alerts, fields): how many fields fall below their configured threshold.

    The count, not the single lowest score, is what decides a document needs a
    human, and it ranks review work the way an annotator experiences it: eight weak
    fields is more work than one weak field at a lower score.

    Absent fields are excluded, as in :func:`min_confidence` — a blank box is a
    correct reading rather than an alert.
    """
    found = walk_confidence_named(explainability_info)
    if not found:
        return None, None
    if inference_result is not None:
        absent = absent_field_paths(inference_result)
        populated = [f for f in found if f[2] not in absent]
        if populated:
            found = populated
    alerts = sum(
        1
        for confidence, threshold, _ in found
        if confidence
        < (threshold if threshold is not None else DEFAULT_ALERT_THRESHOLD)
    )
    return alerts, len(found)


def attach_label_metadata(
    s3_client: Any,
    test_set_bucket: str,
    documents: list[dict],
    deadline: float | None = None,
) -> int:
    """Add labelSource, alert counts and minConfidence to each document on a page.

    Label state lives inside each section's baseline result.json, so those are read
    here — bounded to one page of documents and fetched concurrently, since the calls
    are pure I/O.

    Alert counts sum across a document's sections, since weak fields in two sections
    is more review work than in one; minConfidence takes the minimum, because it
    names the single weakest field.

    Best-effort per section: an unreadable result.json is skipped rather than failing
    the listing.

    ``deadline`` is a ``time.monotonic()`` value after which no further section is
    read, except the first document's, so a page always yields at least one
    document. Returns how many leading documents the deadline skipped no section of,
    which is all of them when it never passed. A section whose read failed was
    attempted, and counts as best-effort above.
    """
    tasks = []
    for doc in documents:
        for section in doc["sections"]:
            tasks.append((doc, section))
    if not tasks:
        for doc in documents:
            doc["labelSource"] = None
            doc["minConfidence"] = None
            doc["confidenceThreshold"] = None
            doc["alertCount"] = None
            doc["fieldCount"] = None
        return len(documents)

    def read(task):
        doc, section = task
        if (
            deadline is not None
            and doc is not documents[0]
            and time.monotonic() > deadline
        ):
            return doc, section, None, False
        key = section["baselineKey"]
        try:
            body = s3_client.get_object(Bucket=test_set_bucket, Key=key)["Body"].read()
            return doc, section, json.loads(body), True
        except Exception as e:  # noqa: BLE001 — best-effort enrichment
            logger.warning(f"Could not read baseline label {key}: {e}")
            return doc, section, None, True

    per_doc = {
        id(doc): {
            "sources": [],
            "confidences": [],
            "alerts": [],
            "fields": [],
            # The class each section was given. Collected here because these
            # baselines are already being read for label state, so it costs
            # nothing extra — and a reviewer working the queue could not
            # previously see what class a document had been assigned without
            # opening it, which is the whole difficulty with a misclassified
            # document: extraction against the wrong schema can be confidently
            # wrong, so nothing else about the row looks unusual.
            "classes": [],
        }
        for doc, _ in tasks
    }
    unread_docs = set()
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(tasks), 16)
    ) as executor:
        for doc, section, result, attempted in executor.map(read, tasks):
            if not attempted:
                unread_docs.add(id(doc))
                continue
            if not result:
                continue
            # The section's OWN class, distinct from the document-level
            # `documentClasses` badge list: the regrouping board shows a class per
            # section, and the editor only ever loads the section being viewed.
            section_class = (result.get("document_class") or {}).get("type")
            if section_class:
                section["documentClass"] = str(section_class)
            # The section's page grouping, so the page-regrouping editor can show every
            # section at once instead of fetching each result.json again. Free: this
            # file is already open for label state and class, same as `classes` below.
            # Left absent rather than empty when unreadable — an empty grouping would
            # read as "this section has no pages", which is a different claim.
            indices = (result.get("split_document") or {}).get("page_indices")
            if isinstance(indices, list):
                section["pageIndices"] = [
                    int(i)
                    for i in indices
                    if isinstance(i, int) and not isinstance(i, bool)
                ]
            bucket_for_doc = per_doc[id(doc)]
            # A baseline with no labelSource is ground truth supplied when the set was
            # created: authoritative, but not reviewed here. Reporting it as
            # LABEL_SOURCE_UPLOADED rather than reviewed-human keeps overwrite safety
            # (the resolver's ``_existing_label_is_human``) without letting the annotation queue
            # count it as completed review work.
            bucket_for_doc["sources"].append(
                result.get("labelSource") or LABEL_SOURCE_UPLOADED
            )
            inference = result.get("inference_result")
            # Recomputed rather than read back, because a stored minConfidence may
            # predate the exclusion of absent fields and carry a 0.0 from a blank box.
            # The stored value is only a fallback when there is no
            # explainability_info to recompute from.
            confidence = min_confidence(result.get("explainability_info"), inference)
            if confidence is None:
                confidence = result.get("minConfidence")
            if confidence is not None:
                threshold = confidence_threshold(
                    result.get("explainability_info"), inference
                )
                if threshold is None:
                    threshold = result.get("confidenceThreshold")
                bucket_for_doc["confidences"].append((float(confidence), threshold))
            alerts, fields = alert_counts(result.get("explainability_info"), inference)
            if alerts is not None:
                bucket_for_doc["alerts"].append(alerts)
                bucket_for_doc["fields"].append(fields)
            # Reusing section_class from above — same `result`, same iteration.
            if section_class:
                bucket_for_doc["classes"].append(str(section_class))

    for doc in documents:
        collected = per_doc.get(
            id(doc),
            {
                "sources": [],
                "confidences": [],
                "alerts": [],
                "fields": [],
                "classes": [],
            },
        )
        # Distinct classes, order preserved. A single-section document yields one;
        # a packet yields the classes its sections were split into. Reported as a
        # list rather than joined, so the UI decides how to render more than one
        # rather than parsing a string back apart.
        seen_classes = []
        for cls in collected["classes"]:
            if cls not in seen_classes:
                seen_classes.append(cls)
        doc["documentClasses"] = seen_classes
        sources = collected["sources"]
        confidences = collected["confidences"]
        # Sections already reviewed: a document with some but not all is in progress.
        doc["reviewedSections"] = sum(1 for s in sources if s == LABEL_SOURCE_HUMAN)
        # A document counts as reviewed only when every section is.
        if not sources:
            doc["labelSource"] = None
        elif all(s == LABEL_SOURCE_HUMAN for s in sources):
            doc["labelSource"] = LABEL_SOURCE_HUMAN
        elif any(s == LABEL_SOURCE_DRAFT for s in sources):
            doc["labelSource"] = LABEL_SOURCE_DRAFT
        else:
            # Not every section is reviewed, so report a source that is not:
            # taking sources[0] read a half-reviewed document as reviewed whenever
            # its first section happened to be the reviewed one.
            doc["labelSource"] = next(s for s in sources if s != LABEL_SOURCE_HUMAN)
        if confidences:
            worst, threshold = min(confidences, key=lambda pair: pair[0])
            doc["minConfidence"] = worst
            doc["confidenceThreshold"] = threshold
        else:
            doc["minConfidence"] = None
            doc["confidenceThreshold"] = None
        if collected["alerts"]:
            doc["alertCount"] = sum(collected["alerts"])
            doc["fieldCount"] = sum(collected["fields"])
        else:
            doc["alertCount"] = None
            doc["fieldCount"] = None

    return next(
        (i for i, doc in enumerate(documents) if id(doc) in unread_docs),
        len(documents),
    )


def _section_sort_key(section: dict) -> tuple:
    """Numeric section ids in numeric order, so "10" doesn't precede "2"."""
    sid = section["sectionId"]
    return (0, int(sid)) if sid.isdigit() else (1, sid)


def list_baseline_sections(
    s3_client: Any,
    bucket: str,
    test_set_id: str,
    *,
    object_key: Optional[str] = None,
) -> dict[str, list[dict]]:
    """``objectKey -> [{sectionId, baselineKey}]`` for every labeled document, from
    one listing of the set's ``baseline/`` prefix (only ``object_key``'s own
    sections when given, so one document never lists the whole set)."""
    prefix = f"{test_set_id}/baseline/"
    list_prefix = f"{prefix}{object_key}/sections/" if object_key else prefix
    out: dict[str, list[dict]] = {}
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
        for obj in page.get("Contents", []):
            match = _SECTION_RE.match(obj["Key"][len(prefix) :])
            if not match:
                continue
            doc_name, section_id = match.groups()
            out.setdefault(doc_name, []).append(
                {"sectionId": section_id, "baselineKey": obj["Key"]}
            )
    for sections in out.values():
        sections.sort(key=_section_sort_key)
    return out


def list_input_documents(s3_client: Any, bucket: str, test_set_id: str) -> list[dict]:
    """Every document under the set's ``input/`` prefix (no sections yet)."""
    prefix = f"{test_set_id}/input/"
    documents = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue  # folder placeholder objects
            documents.append(
                {
                    "objectKey": key[len(prefix) :],
                    "inputKey": key,
                    "size": obj.get("Size"),
                    "lastModified": obj["LastModified"].isoformat(),
                    "sections": [],
                }
            )
    return documents


def summarize_test_set(
    s3_client: Any,
    bucket: str,
    test_set_id: str,
    *,
    object_keys: Optional[set[str]] = None,
) -> list[dict]:
    """Every document of a set (or only ``object_keys``) with its summary.

    Lists ``input/`` and ``baseline/`` once each, then reads the sections' labels a
    page at a time. Not bounded by a request's time limit: callers run it in the
    background.
    """
    documents = list_input_documents(s3_client, bucket, test_set_id)
    if object_keys is not None:
        documents = [d for d in documents if d["objectKey"] in object_keys]
    sections = list_baseline_sections(s3_client, bucket, test_set_id)
    for doc in documents:
        doc["sections"] = sections.get(doc["objectKey"], [])
    for start in range(0, len(documents), SUMMARY_PAGE_SIZE):
        attach_label_metadata(
            s3_client, bucket, documents[start : start + SUMMARY_PAGE_SIZE]
        )
    return documents


def summarize_document(
    s3_client: Any, bucket: str, test_set_id: str, object_key: str
) -> dict:
    """One document's summary, reading only its own sections."""
    doc = {
        "objectKey": object_key,
        "inputKey": f"{test_set_id}/input/{object_key}",
        "sections": list_baseline_sections(
            s3_client, bucket, test_set_id, object_key=object_key
        ).get(object_key, []),
    }
    attach_label_metadata(s3_client, bucket, [doc])
    return doc
