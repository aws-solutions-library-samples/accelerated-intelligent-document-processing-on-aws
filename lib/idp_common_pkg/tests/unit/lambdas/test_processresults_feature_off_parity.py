# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Feature-off parity for the Pipeline-mode ProcessResults handler (IDP-0002).

Each case runs the real handler in ``patterns/unified/src/processresults_function``
and compares its response and every ``update_document`` write with a golden
captured from the handler **before** the review-queues change. With
``hitl.review_queues.enabled`` false (the default) the handler must produce
exactly what it did, and must never touch the review store.

Frozen or dropped values: none. The handler's only clock read is inside
``create_metadata_file``, which is replaced by a recorder here; the calls it
receives are part of the golden.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from idp_common.config.models import IDPConfig
from idp_common.models import Document, Section, Status
from tests.unit.lambdas._goldens import load_handler, normalise, read_golden, snapshot

HANDLER_DIR = "processresults"
processresults_index = load_handler(
    "processresults_parity_index", "processresults_function/index.py"
)

CASES = ("alerts_hitl_on", "no_alerts_hitl_on", "alerts_hitl_off")

_ALERT = {
    "attribute_name": "InvoiceTotal",
    "confidence": 0.62,
    "confidence_threshold": 0.8,
}


def _config(hitl_enabled: bool) -> IDPConfig:
    return IDPConfig.model_validate(
        {"hitl": {"enabled": hitl_enabled, "confidence_threshold": 0.8}}
    )


def _section_result(section_id: str, alerts: list) -> dict:
    doc = Document(
        id="folder/inv.pdf",
        input_bucket="in",
        input_key="folder/inv.pdf",
        output_bucket="out",
        status=Status.EXTRACTING,
        sections=[
            Section(
                section_id=section_id,
                classification="invoice",
                page_ids=[section_id],
                extraction_result_uri=f"s3://out/folder/inv.pdf/sections/{section_id}/result.json",
                confidence_threshold_alerts=list(alerts),
            )
        ],
    )
    doc.metering = {"bedrock/extraction": {"inputTokens": 10 * int(section_id)}}
    return {"document": doc.to_dict()}


def _base_document() -> Document:
    return Document(
        id="folder/inv.pdf",
        input_bucket="in",
        input_key="folder/inv.pdf",
        output_bucket="out",
        status=Status.CLASSIFYING,
        num_pages=2,
        config_version="default",
    )


def _case_inputs(case_id: str):
    if case_id == "alerts_hitl_on":
        return _config(True), [_section_result("1", [_ALERT]), _section_result("2", [])]
    if case_id == "no_alerts_hitl_on":
        return _config(True), [_section_result("1", []), _section_result("2", [])]
    if case_id == "alerts_hitl_off":
        return _config(False), [
            _section_result("1", [_ALERT]),
            _section_result("2", []),
        ]
    raise KeyError(case_id)


def run_case(case_id: str, review_store=None) -> dict:
    """Run one case against the real handler and return the normalised outcome."""
    config, results = _case_inputs(case_id)
    patches = []

    def _set(target, name, value):
        patches.append((target, name, getattr(target, name, None)))
        setattr(target, name, value)

    doc_service = MagicMock()
    doc_service.get_document.return_value = None
    update_calls: list = []
    doc_service.update_document.side_effect = lambda d: update_calls.append(snapshot(d))
    metadata_calls: list = []
    old_wb = os.environ.get("WORKING_BUCKET")
    try:
        os.environ["WORKING_BUCKET"] = ""
        _set(processresults_index, "get_config", lambda **kw: config)
        _set(
            processresults_index,
            "create_document_service",
            lambda *a, **kw: doc_service,
        )
        _set(
            processresults_index,
            "create_metadata_file",
            lambda *a, **kw: metadata_calls.append([list(a), kw]),
        )
        if review_store is not None and hasattr(processresults_index, "_REVIEW_STORE"):
            _set(processresults_index, "_REVIEW_STORE", review_store)
        response = processresults_index.handler(
            {
                "ClassificationResult": {"document": _base_document().to_dict()},
                "ExtractionResults": results,
                "execution_arn": "arn:aws:states:us-east-1:111122223333:execution:sm:exec-1",
            },
            None,
        )
    finally:
        for target, name, value in reversed(patches):
            setattr(target, name, value)
        if old_wb is None:
            os.environ.pop("WORKING_BUCKET", None)
        else:
            os.environ["WORKING_BUCKET"] = old_wb
    return normalise(
        {
            "response": response,
            "update_document_calls": update_calls,
            "create_metadata_file_calls": metadata_calls,
        }
    )


@pytest.mark.unit
class TestFeatureOffParity:
    @pytest.mark.parametrize("case_id", CASES)
    def test_case(self, case_id):
        assert run_case(case_id) == read_golden(HANDLER_DIR, case_id)
