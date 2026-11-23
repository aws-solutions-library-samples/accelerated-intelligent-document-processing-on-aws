# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The evaluation step meters its own Lambda invocation, as the other step Lambdas do.

The OCR, classification, extraction, assessment and summarization Lambdas add
``<context>/lambda/requests`` and ``<context>/lambda/duration`` to the document's
metering before they return, so a cost report prices their invocation and
GB-seconds. The evaluation Lambda recorded neither, so its Lambda cost was absent
from reports.

The handler is driven with a real ``Document`` and the real
``calculate_lambda_metering``; only the clock, the configuration, the baseline,
the evaluation service and the AWS clients are replaced. The clock is pinned so
the expected GB-seconds follow from Lambda's own pricing unit (allocated memory in
GB times seconds), not from the function under test.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from idp_common.models import Document

PATTERN_ROOT = Path(__file__).resolve().parents[1]
HANDLER_PATH = PATTERN_ROOT / "src" / "evaluation_function" / "index.py"

_MEMORY_MB = 2048
_ELAPSED_S = 3.0
# 2 GB allocated for 3 s.
_EXPECTED = {
    "Evaluation/lambda/requests": {"requests": 1},
    "Evaluation/lambda/duration": {"gb_seconds": 6.0},
}
_PRIOR = {"Extraction/bedrock/us.amazon.nova-2-lite-v1:0": {"inputTokens": 50}}


@pytest.fixture
def handler_module(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("WORKING_BUCKET", "working-bucket")

    import idp_common.docs_service as docs_service

    service = Mock()
    service.update_document.side_effect = lambda document: document

    with patch.object(docs_service, "create_document_service", return_value=service):
        spec = importlib.util.spec_from_file_location(
            "evaluation_function_metering_under_test", HANDLER_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
            module._document_service = service
            yield module
        finally:
            sys.modules.pop(spec.name, None)


def _document() -> Document:
    document = Document(id="doc-1", input_key="set/doc-1.pdf")
    document.metering = dict(_PRIOR)
    return document


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs, status",
    [
        pytest.param({}, "COMPLETED", id="evaluated"),
        pytest.param({"new_error": "comparison failed"}, "FAILED", id="eval-errors"),
        pytest.param({"baseline": False}, "NO_BASELINE", id="no-baseline"),
        pytest.param({"enabled": False}, "DISABLED", id="disabled"),
    ],
)
def test_every_outcome_meters_the_invocation(handler_module, kwargs, status):
    document = _document()
    returned = _run(handler_module, document, **kwargs)
    assert document.evaluation_status == status
    # Asserted on the serialized return payload, not only on the object: metering
    # added after the payload is built would never reach the cost report.
    assert returned == [{**_PRIOR, **_EXPECTED}]


def _run(handler_module, document, **kwargs):
    """Drive the handler with a clock that reads +3 s after its first read."""
    reads = {"n": 0}

    def now():
        reads["n"] += 1
        return 1000.0 if reads["n"] == 1 else 1000.0 + _ELAPSED_S

    # The handler and lambda_metering share the stdlib ``time`` module, so one
    # patch drives both the start time and the end time.
    with patch.object(handler_module.time, "time", side_effect=now):
        return _drive(handler_module, document, **kwargs)


def _drive(handler_module, document, *, enabled=True, baseline=True, new_error=None):
    config = MagicMock()
    config.evaluation.enabled = enabled

    def evaluate_document(actual_document, expected_document, store_results):
        if new_error:
            actual_document.errors.append(new_error)
        return actual_document

    service = Mock()
    service.evaluate_document.side_effect = evaluate_document
    lambda_client = Mock()
    lambda_client.invoke.return_value = {
        "Payload": SimpleNamespace(read=lambda: b'{"statusCode": 200}')
    }
    returned: list[dict] = []

    def serialize(self, *_args, **_kwargs):
        # What the step returns is what workflow_tracker later reads, so capture
        # the metering at the moment the return payload is built.
        returned.append(dict(self.metering))
        return {"id": "doc-1"}

    with (
        patch.object(
            handler_module, "extract_document_from_event", return_value=document
        ),
        patch.object(handler_module, "get_config", return_value=config),
        patch.object(
            handler_module,
            "load_baseline_document",
            return_value=Document(id="baseline") if baseline else None,
        ),
        patch.object(
            handler_module.evaluation, "EvaluationService", return_value=service
        ),
        patch.object(handler_module.boto3, "client", return_value=lambda_client),
        patch.object(Document, "serialize_document", serialize),
    ):
        handler_module.handler(
            {"document": {"id": "doc-1"}},
            SimpleNamespace(memory_limit_in_mb=_MEMORY_MB),
        )
    return returned
