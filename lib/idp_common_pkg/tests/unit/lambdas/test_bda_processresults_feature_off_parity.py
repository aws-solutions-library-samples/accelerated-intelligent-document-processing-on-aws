# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Feature-off parity for the BDA-mode ProcessResults handler (IDP-0002).

Each case runs the real handler in
``patterns/unified/src/bda_processresults_function`` through one of its three
return paths (the main BDA path, ``handle_skip_bda`` and the ``metadata.skipped``
path) and compares the response and every ``update_document`` write with a
golden captured **before** the review-queues change.

Replaced, and so outside the comparison: the page-image renderer, the BDA output
copiers (``process_bda_sections`` / ``process_bda_pages``, which here set the
sections directly), the S3 downloads (``download_json`` / ``download_decimal`` /
the job-metadata ``get_object``) and ``metrics.put_metric``. Dropped values:
none; the DynamoDB HITL rows are skipped by ``DB_NAME=""``.
"""

from __future__ import annotations

import io
import json
import os
from unittest.mock import MagicMock

import pytest

from idp_common.config.models import IDPConfig
from idp_common.models import Document, Section, Status
from tests.unit.lambdas._goldens import load_handler, normalise, read_golden, snapshot

HANDLER_DIR = "bda_processresults"
bda_index = load_handler(
    "bda_processresults_parity_index",
    "bda_processresults_function/index.py",
    extra_modules={"pypdfium2": MagicMock()},
)

CASES = (
    "alerts_hitl_on",
    "blueprint_only_hitl_on",
    "skip_bda_path",
    "metadata_skipped_path",
    "no_trigger",
)

_ALERT = {
    "attribute_name": "InvoiceTotal",
    "confidence": 0.62,
    "confidence_threshold": 0.8,
}
_EXEC = "arn:aws:states:us-east-1:111122223333:execution:sm:exec-1"


def _config(hitl_enabled: bool = True) -> IDPConfig:
    return IDPConfig.model_validate(
        {"hitl": {"enabled": hitl_enabled, "confidence_threshold": 0.8}}
    )


def _custom_output(field_confidence: float, bp_confidence: float) -> dict:
    return {
        "matched_blueprint": {"name": "invoice-bp", "confidence": bp_confidence},
        "split_document": {"page_indices": [0]},
        "document_class": {"type": "invoice"},
        "explainability_info": [
            {
                "InvoiceTotal": {
                    "value": "12.00",
                    "confidence": field_confidence,
                    "geometry": [{"page": 1}],
                }
            }
        ],
        "inference_result": {"InvoiceTotal": "12.00"},
    }


def _stored_document(alerts: list) -> Document:
    return Document(
        id="folder/inv.pdf",
        input_bucket="in",
        input_key="folder/inv.pdf",
        output_bucket="out",
        status=Status.POSTPROCESSING,
        num_pages=1,
        config_version="default",
        sections=[
            Section(
                section_id="1",
                classification="invoice",
                page_ids=["1"],
                extraction_result_uri="s3://out/folder/inv.pdf/sections/1/result.json",
                confidence_threshold_alerts=list(alerts),
            )
        ],
    )


def _main_event() -> dict:
    return {
        "BDAResponse": {
            "job_detail": {
                "input_s3_object": {"name": "folder/inv.pdf", "s3_bucket": "in"},
                "output_s3_location": {"s3_bucket": "bda", "name": "jobs/j1/0"},
            }
        },
        "execution_arn": _EXEC,
    }


def _case(case_id: str):
    """Return (config, event, custom_output or None)."""
    if case_id == "alerts_hitl_on":
        return _config(), _main_event(), _custom_output(0.62, 0.95)
    if case_id == "blueprint_only_hitl_on":
        return _config(), _main_event(), _custom_output(0.97, 0.55)
    if case_id == "no_trigger":
        return _config(), _main_event(), _custom_output(0.97, 0.95)
    if case_id == "skip_bda_path":
        return (
            _config(),
            {
                "skip_bda": True,
                "document": _stored_document([_ALERT]).to_dict(),
                "execution_arn": _EXEC,
            },
            None,
        )
    if case_id == "metadata_skipped_path":
        return (
            _config(),
            {
                "metadata": {"skipped": True, "working_bucket": ""},
                "document": _stored_document([_ALERT]).to_dict(),
                "execution_arn": _EXEC,
            },
            None,
        )
    raise KeyError(case_id)


def run_case(case_id: str, review_store=None) -> dict:
    """Run one case against the real handler and return the normalised outcome."""
    config, event, custom_output = _case(case_id)
    patches = []

    def _set(target, name, value):
        patches.append((target, name, getattr(target, name, None)))
        setattr(target, name, value)

    existing = _stored_document([])
    existing.sections = []
    doc_service = MagicMock()
    doc_service.get_document.return_value = existing
    update_calls: list = []
    doc_service.update_document.side_effect = lambda d: update_calls.append(snapshot(d))

    def _sections(bucket, prefix, output_bucket, object_key, document, threshold, cfg):
        document.sections = [
            Section(
                section_id="1",
                classification="invoice",
                page_ids=["1"],
                extraction_result_uri="s3://out/folder/inv.pdf/sections/1/result.json",
            )
        ]
        document.num_pages = 1
        return document

    s3_client = MagicMock()
    job_metadata = {
        "output_metadata": [
            {
                "segment_metadata": [
                    {
                        "custom_output_status": "MATCH",
                        "custom_output_path": "s3://bda/jobs/j1/0/custom_output/0/result.json",
                        "standard_output_path": "s3://bda/jobs/j1/0/standard_output/0/result.json",
                    }
                ]
            }
        ]
    }
    s3_client.get_object.side_effect = lambda **kw: {
        "Body": io.BytesIO(json.dumps(job_metadata).encode())
    }

    env = {"WORKING_BUCKET": "", "DB_NAME": "", "OUTPUT_BUCKET": ""}
    old_env = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update(env)
        _set(bda_index, "get_config", lambda **kw: config)
        _set(bda_index, "create_document_service", lambda *a, **kw: doc_service)
        _set(bda_index, "create_pdf_page_images", lambda *a, **kw: 1)
        _set(bda_index, "process_bda_sections", _sections)
        _set(bda_index, "process_bda_pages", lambda *a, **kw: a[4])
        _set(bda_index, "s3_client", s3_client)
        _set(bda_index, "download_json", lambda b, k: custom_output)
        _set(bda_index, "download_decimal", lambda b, k: custom_output)
        _set(bda_index.metrics, "put_metric", lambda *a, **kw: None)
        if review_store is not None and hasattr(bda_index, "_REVIEW_STORE"):
            _set(bda_index, "_REVIEW_STORE", review_store)
        response = bda_index.handler(event, None)
    finally:
        for target, name, value in reversed(patches):
            setattr(target, name, value)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return normalise({"response": response, "update_document_calls": update_calls})


@pytest.mark.unit
class TestFeatureOffParity:
    @pytest.mark.parametrize("case_id", CASES)
    def test_case(self, case_id):
        assert run_case(case_id) == read_golden(HANDLER_DIR, case_id)
