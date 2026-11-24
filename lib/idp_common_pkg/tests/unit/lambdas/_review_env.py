# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Builders for the handler suites that run against a review-queue deployment.

Used with the ``review_env`` fixture (``conftest.py``): real ``Document`` and
``Section`` objects, section results stored where their URIs say, and review
items written the way the store writes them.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

from idp_common.config.models import IDPConfig
from idp_common.models import Document, Section, Status
from idp_common.review_queues import keys
from idp_common.review_queues.models import DraftRecord, ReviewItemRecord
from idp_common.review_queues.store import item_to_dynamo

REGION = "us-east-1"
BUCKET = "handler-test-output"
KEY = "claims/packet.pdf"
CYCLE = "0123456789abcdef01234"
EXEC = "arn:aws:states:us-east-1:111122223333:execution:sm:run-1"
REVIEWER = "reviewer@example.com"
DISPOSED_AT = "2026-11-16T09:30:00.000Z"

#: What a handler reads the Lambda's memory from, for its metering entry.
LAMBDA_CONTEXT = SimpleNamespace(memory_limit_in_mb=512)

ALERT = {
    "attribute_name": "Total",
    "confidence": 0.4,
    "confidence_threshold": 0.8,
}
INVOICE = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "Invoice",
    "x-aws-idp-document-type": "Invoice",
    "type": "object",
    "properties": {"Vendor": {"type": "string"}, "Total": {"type": "number"}},
}
MACHINE = {
    "inference_result": {"Vendor": "ACME", "Total": 100},
    "explainability_info": [
        {"Total": {"confidence": 0.4, "confidence_threshold": 0.8}}
    ],
    "metadata": {"model": "m"},
}
CORRECTED = {
    "inference_result": {"Vendor": "ACME Corp", "Total": 120},
    "explainability_info": [
        {"Total": {"confidence": 0.4, "confidence_threshold": 0.8}}
    ],
    "metadata": {"model": "m"},
}
_POLICY = {"$id": "Policy", "rule_properties": {}}


def result_key(section_id: str) -> str:
    return f"{KEY}/sections/{section_id}/result.json"


def result_uri(section_id: str) -> str:
    return f"s3://{BUCKET}/{result_key(section_id)}"


def put_json(env, key: str, body: dict) -> None:
    env.s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(body).encode("utf-8"))


def stored_result(env, section_id: str) -> dict:
    body = env.s3.get_object(Bucket=BUCKET, Key=result_key(section_id))["Body"]
    return json.loads(body.read())


def section(env, section_id: str, *, alert: bool = False, result=MACHINE) -> Section:
    """A section whose result is stored where its URI says."""
    if result is not None:
        put_json(env, result_key(section_id), result)
    return Section(
        section_id=section_id,
        classification="Invoice",
        page_ids=[section_id],
        extraction_result_uri=result_uri(section_id) if result is not None else None,
        confidence_threshold_alerts=[dict(ALERT)] if alert else [],
    )


def document(*sections: Section, status: Status = Status.POSTPROCESSING) -> Document:
    return Document(
        id=KEY,
        input_bucket="in",
        input_key=KEY,
        output_bucket=BUCKET,
        status=status,
        num_pages=max(len(sections), 1),
        config_version="default",
        sections=list(sections),
    )


def config(*, hitl: bool = True, policies: bool = False) -> IDPConfig:
    """HITL on at threshold 0.8; ``policies`` adds one policy class."""
    body: dict = {
        "hitl": {"enabled": hitl, "confidence_threshold": 0.8},
        "classes": [INVOICE],
    }
    if policies:
        body["policy_classes"] = [_POLICY]
    return IDPConfig.model_validate(body)


def execution_name(execution_arn: str = EXEC) -> str:
    return execution_arn.rsplit(":", 1)[-1]


def finalizing_item(env, *, disposition: str = "complete", **overrides):
    """The item of a disposed review whose finalize run is ``EXEC``."""
    fields = dict(
        doc_key=KEY,
        cycle_id=CYCLE,
        profile="default",
        queue="default",
        state="FINALIZING",
        version=5,
        enqueued_at="2026-11-16T09:00:00.000Z",
        enqueued_at_ms=1,
        sections=["1"],
        disposition=disposition,
        disposed_by=REVIEWER,
        disposed_at=DISPOSED_AT,
        finalize_token="rvw-" + "ab" * 16,
        finalize_execution_name=execution_name(),
    )
    fields.update(overrides)
    record = ReviewItemRecord(**fields)
    env.store.client.put_item(item_to_dynamo(record))
    return record


def draft(
    env, section_id: str, corrected: dict = CORRECTED, changed=("Vendor", "Total")
):
    """A reviewer's corrected result for one section, and its draft record."""
    key = f"{KEY}/review/drafts/{CYCLE}/{section_id}.json"
    put_json(env, key, corrected)
    record = DraftRecord(
        doc_key=KEY,
        cycle_id=CYCLE,
        section_id=section_id,
        S3Key=key,
        revision=1,
        updated_by=REVIEWER,
        changed_fields=list(changed),
    )
    env.store.client.put_item(
        {
            **record.to_attributes(),
            "PK": keys.doc_pk(KEY),
            "SK": keys.draft_sk(CYCLE, section_id),
        }
    )
    return record


@contextmanager
def swapped(module, **attributes):
    """Replace module attributes for the block (handlers are loaded once)."""
    previous = {name: getattr(module, name) for name in attributes}
    try:
        for name, value in attributes.items():
            setattr(module, name, value)
        yield module
    finally:
        for name, value in previous.items():
            setattr(module, name, value)
