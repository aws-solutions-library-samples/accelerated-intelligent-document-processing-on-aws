# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reprocessing must not reopen a finished legacy review (class of #1568).

Both ProcessResults handlers (``patterns/unified/src/processresults_function`` and
``.../bda_processresults_function``) read the document's stored ``HITLStatus`` and,
when the run raises a legacy HITL trigger, reset it to ``PendingReview`` unless the
review is already finished. Finishing a legacy review itself triggers a reprocess
(Complete, Process Changes, the last section, Skip All Reviews), so this is the check
that stops a finished review from reopening on the run its own completion started.

The check enumerated four spellings (``LEGACY_DONE``) instead of applying the finished
rule ``process_changes_resolver`` and ``complete_section_review`` use, so ``COMPLETED``
— the spelling the earlier SageMaker A2I flow stored, which retained rows still carry
— read as unfinished and the review was reopened.

Each case seeds the stored state with the real writer (``DocumentDynamoDBService``
over a ``moto`` tracking table; ``tracker.apply_overrides`` for the two review-queue
outcomes), runs the real handler with the real document service, and reads the
stored ``HITLStatus`` back from the table. ``moto`` stands in for DynamoDB here; the
property measured is the handler's branch, not expression-language behaviour.

What is deliberately **not** preserved, and pinned here so a later widening is a
visible decision:

* every pending or in-progress spelling, and a document with no status at all — a
  review is still owed, so the run must ask for one;
* ``complete`` and ``rejected`` written by a review queue, when the run is in the
  legacy flow (the Default queue was turned off after the review). Under review
  queues, reprocessing a finished document starts a fresh review cycle, and the
  legacy flow keeps that: the document is asked for a review again rather than
  carrying a queue outcome the legacy reviewer UI does not work from.
"""

from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from idp_common.config.models import IDPConfig
from idp_common.dynamodb.client import DynamoDBClient
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, Section, Status
from idp_common.review_queues import tracker
from idp_common.review_queues.models import ReviewItemRecord
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.statuses import LegacyHitlStatus, QueueItemState
from idp_common.review_queues.store import ReviewStore
from tests.unit.lambdas._goldens import load_handler
from tests.unit.review_queues._table import create_review_table

processresults_index = load_handler(
    "processresults_reprocess_index", "processresults_function/index.py"
)
bda_index = load_handler(
    "bda_processresults_reprocess_index",
    "bda_processresults_function/index.py",
    extra_modules={"pypdfium2": MagicMock()},
)

REGION = "us-east-1"
TRACKING = "reprocess-tracking-table"
OBJECT_KEY = "folder/inv.pdf"
EXEC = "arn:aws:states:us-east-1:111122223333:execution:sm:exec-2"
PENDING = LegacyHitlStatus.PENDING_REVIEW.value
STALE_PENDING = ["9"]
STALE_COMPLETED = ["8"]
ALERT = {
    "attribute_name": "InvoiceTotal",
    "confidence": 0.62,
    "confidence_threshold": 0.8,
}


def _legacy(value):
    return ("legacy", value)


def _queue(outcome):
    return ("queue", outcome)


#: (stored state, how it was written) -> HITLStatus after a legacy-flow reprocess.
#: Every LegacyHitlStatus member appears, so a new spelling cannot go unclassified.
LEGACY_FLOW_EXPECTED = {
    "none": (None, PENDING),
    "PendingReview": (_legacy(LegacyHitlStatus.PENDING_REVIEW.value), PENDING),
    "Review Pending": (_legacy(LegacyHitlStatus.REVIEW_PENDING.value), PENDING),
    "InProgress": (_legacy(LegacyHitlStatus.IN_PROGRESS.value), PENDING),
    "ReviewInProgress": (_legacy(LegacyHitlStatus.REVIEW_IN_PROGRESS.value), PENDING),
    "Completed": (_legacy(LegacyHitlStatus.COMPLETED.value), "Completed"),
    "Review Completed": (
        _legacy(LegacyHitlStatus.REVIEW_COMPLETED.value),
        "Review Completed",
    ),
    "Skipped": (_legacy(LegacyHitlStatus.SKIPPED.value), "Skipped"),
    "Review Skipped": (
        _legacy(LegacyHitlStatus.REVIEW_SKIPPED.value),
        "Review Skipped",
    ),
    "COMPLETED (A2I)": (("raw", "COMPLETED"), "COMPLETED"),
    "queue complete": (_queue("complete"), PENDING),
    "queue rejected": (_queue("rejected"), PENDING),
}


def test_every_legacy_spelling_is_classified():
    seeded = {
        spec[1]
        for spec, _ in LEGACY_FLOW_EXPECTED.values()
        if spec is not None and spec[0] == "legacy"
    }
    assert seeded == {m.value for m in LegacyHitlStatus}


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # nosec B105 - moto dummy
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")  # nosec B105 - moto dummy
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("TRACKING_TABLE", TRACKING)
    monkeypatch.setenv("WORKING_BUCKET", "")
    monkeypatch.setenv("OUTPUT_BUCKET", "")
    monkeypatch.setenv("DB_NAME", "")
    with mock_aws():
        boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=TRACKING,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        review_table = create_review_table(region=REGION)
        yield SimpleNamespace(
            service=DocumentDynamoDBService(
                dynamodb_client=DynamoDBClient(table_name=TRACKING, region=REGION)
            ),
            review_table=review_table,
        )


def _store(aws, default_enabled):
    store = ReviewStore(aws.review_table, region=REGION)
    store.queue_defs.put(
        QueueDefinition(id="default", name="Default", enabled=default_enabled),
        create=True,
    )
    return store


def _seed(service, spec):
    """Write the stored state the way its real writer does."""
    doc = Document(
        id=OBJECT_KEY,
        input_bucket="in",
        input_key=OBJECT_KEY,
        output_bucket="out",
        status=Status.COMPLETED,
        num_pages=1,
        config_version="default",
    )
    if spec is not None:
        # Stale lists no section of this run produces: a reset rewrites them, a
        # preserved review leaves them. The status alone cannot tell a preserved
        # ``PendingReview`` from one the run reset to the same value.
        doc.hitl_sections_pending = list(STALE_PENDING)
        doc.hitl_sections_completed = list(STALE_COMPLETED)
        kind, value = spec
        if kind == "legacy":
            doc.hitl_status = value
        elif kind == "queue":
            ctx = tracker.TrackerReviewContext(
                kind="finalize",
                item=ReviewItemRecord(
                    doc_key=OBJECT_KEY,
                    cycle_id="c1",
                    profile="default",
                    queue="default",
                    state=QueueItemState.FINALIZING.value,
                    enqueued_at="2026-10-24T00:00:00Z",
                    enqueued_at_ms=0,
                    disposition="rejected" if value == "rejected" else "complete",
                ),
                token="t",
                outcome=value,
            )
            doc, _ = tracker.apply_overrides(ctx, doc)
    service.update_document(doc)
    if spec is not None and spec[0] == "raw":
        # No live writer stores this spelling any more; retained rows still do.
        boto3.client("dynamodb", region_name=REGION).update_item(
            TableName=TRACKING,
            Key={"PK": {"S": f"doc#{OBJECT_KEY}"}, "SK": {"S": "none"}},
            UpdateExpression="SET HITLStatus = :s",
            ExpressionAttributeValues={":s": {"S": spec[1]}},
        )


def _stored():
    """(HITLStatus, HITLSectionsPending, HITLSectionsCompleted) as stored."""
    item = (
        boto3.resource("dynamodb", region_name=REGION)
        .Table(TRACKING)
        .get_item(Key={"PK": f"doc#{OBJECT_KEY}", "SK": "none"})["Item"]
    )
    return (
        item.get("HITLStatus"),
        item.get("HITLSectionsPending"),
        item.get("HITLSectionsCompleted"),
    )


def _config():
    return IDPConfig.model_validate(
        {"hitl": {"enabled": True, "confidence_threshold": 0.8}}
    )


def _swap(patches, target, name, value):
    patches.append((target, name, getattr(target, name)))
    setattr(target, name, value)


def run_pipeline(service, store):
    section_doc = Document(
        id=OBJECT_KEY,
        input_bucket="in",
        input_key=OBJECT_KEY,
        output_bucket="out",
        status=Status.EXTRACTING,
        sections=[
            Section(
                section_id="1",
                classification="invoice",
                page_ids=["1"],
                confidence_threshold_alerts=[dict(ALERT)],
            )
        ],
    )
    base = Document(
        id=OBJECT_KEY,
        input_bucket="in",
        input_key=OBJECT_KEY,
        output_bucket="out",
        status=Status.CLASSIFYING,
        num_pages=1,
        config_version="default",
    )
    patches: list = []
    try:
        _swap(patches, processresults_index, "get_config", lambda **kw: _config())
        _swap(
            patches,
            processresults_index,
            "create_document_service",
            lambda *a, **kw: service,
        )
        _swap(
            patches, processresults_index, "create_metadata_file", lambda *a, **k: None
        )
        _swap(patches, processresults_index, "_REVIEW_STORE", store)
        return processresults_index.handler(
            {
                "ClassificationResult": {"document": base.to_dict()},
                "ExtractionResults": [{"document": section_doc.to_dict()}],
                "execution_arn": EXEC,
            },
            None,
        )
    finally:
        for target, name, value in reversed(patches):
            setattr(target, name, value)


def run_bda(service, store):
    custom_output = {
        "matched_blueprint": {"name": "invoice-bp", "confidence": 0.95},
        "split_document": {"page_indices": [0]},
        "document_class": {"type": "invoice"},
        "explainability_info": [
            {
                "InvoiceTotal": {
                    "value": "12.00",
                    "confidence": 0.62,
                    "geometry": [{"page": 1}],
                }
            }
        ],
        "inference_result": {"InvoiceTotal": "12.00"},
    }
    job_metadata = {
        "output_metadata": [
            {
                "segment_metadata": [
                    {
                        "custom_output_status": "MATCH",
                        "custom_output_path": "s3://bda/j/0/custom_output/0/result.json",
                        "standard_output_path": "s3://bda/j/0/standard_output/0/result.json",
                    }
                ]
            }
        ]
    }
    s3_client = MagicMock()
    s3_client.get_object.side_effect = lambda **kw: {
        "Body": io.BytesIO(json.dumps(job_metadata).encode())
    }

    def _sections(bucket, prefix, output_bucket, object_key, document, threshold, cfg):
        document.sections = [
            Section(section_id="1", classification="invoice", page_ids=["1"])
        ]
        document.num_pages = 1
        return document

    patches: list = []
    try:
        _swap(patches, bda_index, "get_config", lambda **kw: _config())
        _swap(patches, bda_index, "create_document_service", lambda *a, **kw: service)
        _swap(patches, bda_index, "create_pdf_page_images", lambda *a, **kw: 1)
        _swap(patches, bda_index, "process_bda_sections", _sections)
        _swap(patches, bda_index, "process_bda_pages", lambda *a, **kw: a[4])
        _swap(patches, bda_index, "s3_client", s3_client)
        _swap(patches, bda_index, "download_json", lambda b, k: custom_output)
        _swap(patches, bda_index, "download_decimal", lambda b, k: custom_output)
        _swap(patches, bda_index.metrics, "put_metric", lambda *a, **kw: None)
        _swap(patches, bda_index, "_REVIEW_STORE", store)
        return bda_index.handler(
            {
                "BDAResponse": {
                    "job_detail": {
                        "input_s3_object": {"name": OBJECT_KEY, "s3_bucket": "in"},
                        "output_s3_location": {"s3_bucket": "bda", "name": "j/0"},
                    }
                },
                "execution_arn": EXEC,
            },
            None,
        )
    finally:
        for target, name, value in reversed(patches):
            setattr(target, name, value)


RUNNERS = {"pipeline": run_pipeline, "bda": run_bda}


@pytest.mark.unit
@pytest.mark.parametrize("handler", sorted(RUNNERS))
@pytest.mark.parametrize("case", sorted(LEGACY_FLOW_EXPECTED))
@pytest.mark.parametrize("store_mode", ["no_store", "default_disabled"])
def test_legacy_flow_reprocess(aws, handler, case, store_mode):
    """Legacy flow: a finished legacy review survives; anything else asks for one."""
    spec, expected = LEGACY_FLOW_EXPECTED[case]
    _seed(aws.service, spec)
    store = None if store_mode == "no_store" else _store(aws, default_enabled=False)
    response = RUNNERS[handler](aws.service, store)
    # The run did raise a legacy trigger, so the preserved cases are not vacuous.
    assert response["hitl_triggered"] is True
    status, pending, completed = _stored()
    assert status == expected
    if expected == PENDING:
        # Asked for a review again: the run's own alerting section is pending.
        assert (pending, completed) == (["1"], [])
    else:
        assert (pending, completed) == (STALE_PENDING, STALE_COMPLETED)


@pytest.mark.unit
@pytest.mark.parametrize("handler", sorted(RUNNERS))
@pytest.mark.parametrize("case", sorted(LEGACY_FLOW_EXPECTED))
def test_review_queue_flow_does_not_touch_the_legacy_status(aws, handler, case):
    """Review queues on: the legacy reset never runs; routing holds a new cycle."""
    spec, _ = LEGACY_FLOW_EXPECTED[case]
    _seed(aws.service, spec)
    before = _stored()
    response = RUNNERS[handler](aws.service, _store(aws, default_enabled=True))
    assert response["hitl_triggered"] is False
    assert response.get("review_hold") is True
    assert _stored() == before


@pytest.mark.unit
def test_runs_against_the_handlers_in_this_checkout():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", "..", "..", ".."))
    for module in (processresults_index, bda_index):
        assert os.path.abspath(str(module.__file__)).startswith(
            os.path.join(root, "patterns", "unified", "src") + os.sep
        )
