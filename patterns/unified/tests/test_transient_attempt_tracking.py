# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A step retried after a transient failure is not shown as FAILED, and the failed
attempt's usage is recorded (#2122).

When classification or summarization fails transiently the handler raises
``TransientError`` and Step Functions runs the step again. Writing ``FAILED`` to the
tracking record first showed a document that was about to succeed as failed, in the
Web UI and to anything polling its status, for as long as the retries lasted. And the
model calls the attempt had completed were billed but recorded nowhere, so an
execution that ended unsuccessfully under-reported its spend: the workflow tracker
reports such a run from the tracking record's ``Metering`` (#1951).

Each test drives the real handler and the real service down to
``idp_common.bedrock.invoke_model``, with the tracking table in moto and read back
through the real document service, so the assertions are about the record a user and
the tracker would see. A deterministic failure at the same call is the control: it
must still be persisted as FAILED, now with the usage of the calls that completed.
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import boto3
import moto
import pytest
from botocore.exceptions import ClientError

from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.models import IDPConfig
from idp_common.docs_service import create_document_service
from idp_common.models import Document, Page, Section, Status
from idp_common.utils import attach_failure_metering
from idp_common.utils.transient_errors import TransientError

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[1] / "src"
_PRESET = (
    Path(__file__).resolve().parents[3]
    / "config_library/unified/lending-package-sample/config.yaml"
)
REGION = "us-east-1"
TRACKING = "tracking-table"
KEY = "run1/packet.pdf"

#: What the document had spent before this step (OCR), which must be neither lost
#: nor counted twice by the record the failed attempt writes.
EARLIER_STEPS = {"OCR/textract/analyze_document": {"pages": 2}}
#: Input tokens the fake model reports for every call it answers.
TOKENS = 100

ThrottlingException = type("ThrottlingException", (ClientError,), {})


def _throttle() -> BaseException:
    return ThrottlingException(
        {"Error": {"Code": "ThrottlingException", "Message": "Too many tokens"}},
        "Converse",
    )


def _deterministic() -> BaseException:
    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": "malformed input"}},
        "Converse",
    )


def _load(rel: str, name: str):
    path = _SRC / rel
    with (
        patch.dict(os.environ, {"AWS_XRAY_SDK_ENABLED": "false"}),
        patch("aws_xray_sdk.core.patch_all"),
    ):
        if str(path.parent) not in sys.path:
            sys.path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _preset_config(**classification) -> IDPConfig:
    """The shipped preset on the system defaults, with one small class."""
    import yaml

    preset = yaml.safe_load(_PRESET.read_text(encoding="utf-8"))
    preset.setdefault("classification", {}).update(classification)
    preset["classes"] = [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "Payslip",
            "x-aws-idp-document-type": "Payslip",
            "type": "object",
            "description": "An employee's pay statement.",
            "properties": {
                "YTDNetPay": {"type": "string", "description": "Net pay to date."}
            },
        }
    ]
    return IDPConfig.model_validate(merge_config_with_defaults(preset))


@pytest.fixture(scope="module")
def config() -> IDPConfig:
    return _preset_config()


@pytest.fixture
def tracking(monkeypatch):
    """A moto tracking table, the one the handlers and the page cache write to."""
    for name, value in {
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - moto placeholder
        "TRACKING_TABLE": TRACKING,
        "DOCUMENT_TRACKING_MODE": "dynamodb",
        "AWS_XRAY_SDK_ENABLED": "false",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with moto.mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
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
        yield create_document_service()


def _document(status: Status, sections: bool) -> Document:
    doc = Document(
        id=KEY,
        input_key=KEY,
        input_bucket="in",
        output_bucket="out",
        status=status,
        workflow_execution_arn="arn:aws:states:us-east-1:111122223333:execution:sm:x",
    )
    doc.metering = copy.deepcopy(EARLIER_STEPS)
    doc.pages = {
        str(n): Page(
            page_id=str(n),
            image_uri=f"s3://out/{KEY}/pages/{n}/image.jpg",
            raw_text_uri=f"s3://out/{KEY}/pages/{n}/raw.json",
            parsed_text_uri=f"s3://out/{KEY}/pages/{n}/result.json",
        )
        for n in (1, 2)
    }
    if sections:
        doc.sections = [
            Section(section_id=str(n), classification="Payslip", page_ids=[str(n)])
            for n in (1, 2)
        ]
    return doc


class Model:
    """Stands in for ``invoke_model``: fails the call ``fails`` selects with
    ``fault``, after every other call has been answered, and answers the rest
    with ``text`` and ``TOKENS`` input tokens under the caller's context."""

    def __init__(self, text: str, fault=None, fails=None):
        self.text = text
        self.fault = fault
        self.fails = fails or (lambda kwargs: False)
        self.answered = 0
        self.raised = 0
        self._lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        with self._lock:
            if self.fault is not None and self.fails(kwargs):
                self.raised += 1
                raise self.fault
            self.answered += 1
        context = kwargs.get("context") or "Model"
        return {
            "response": {"output": {"message": {"content": [{"text": self.text}]}}},
            "metering": {f"{context}/bedrock/m": {"inputTokens": TOKENS}},
        }


def _about_page(page: str):
    def selects(kwargs) -> bool:
        text = "".join(
            block.get("text", "")
            for block in kwargs.get("content") or []
            if isinstance(block, dict)
        )
        return f"<page-number>{page}</page-number>" in text or f"PAGE {page}" in text

    return selects


def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@contextlib.contextmanager
def _storage():
    """Page text and images from memory; result writes and metrics dropped."""
    with (
        patch(
            "idp_common.s3.get_text_content",
            side_effect=lambda uri: f"PAGE {uri.split('/pages/')[1].split('/')[0]} text",
        ),
        patch("idp_common.s3.get_binary_content", return_value=_png()),
        patch("idp_common.image.get_binary_content", return_value=_png()),
        patch("idp_common.s3.get_json_content", return_value={"summary": "ok"}),
        patch("idp_common.s3.write_content"),
        patch("idp_common.metrics.put_metric"),
        patch.object(Document, "serialize_document", return_value={}),
    ):
        yield


def _bedrock_tokens(document: Document) -> float:
    return sum(
        float(units.get("inputTokens", 0))
        for key, units in (document.metering or {}).items()
        if "/bedrock/" in key
    )


def _recorded(service) -> Document:
    row = service.get_document(KEY, consistent_read=True)
    assert row is not None, "the handler never wrote the tracking record"
    return row


def _earlier_steps_kept(row: Document) -> bool:
    return row.metering.get("OCR/textract/analyze_document") == {"pages": 2}


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def summarization():
    return _load("summarization_function/index.py", "summarization_attempt_tracking")


def _summarize(module, config, model, sections=True):
    document = _document(Status.ASSESSING, sections)
    with (
        patch.object(module.Document, "load_document", return_value=document),
        patch.object(module, "get_config", return_value=config),
        patch.object(module, "calculate_lambda_metering", return_value={}),
        patch("idp_common.bedrock.invoke_model", model),
        _storage(),
    ):
        return module.handler({"document": {"id": KEY}}, None)


class TestSummarization:
    def test_a_throttled_section_leaves_the_document_summarizing_with_its_usage(
        self, summarization, config, tracking
    ):
        model = Model('{"summary": "ok"}', _throttle(), _about_page("2"))

        with pytest.raises(TransientError):
            _summarize(summarization, config, model)

        row = _recorded(tracking)
        assert model.raised == 1 and model.answered == 1
        assert row.status == Status.SUMMARIZING
        # Section 1's call was billed; it is on the record, once.
        assert _bedrock_tokens(row) == TOKENS * model.answered
        assert _earlier_steps_kept(row)

    def test_usage_the_failed_section_carried_is_recorded_too(
        self, summarization, config, tracking
    ):
        """A call can fail after a billed reply, and then carries that usage."""
        fault = attach_failure_metering(
            _throttle(), {"Summarization/bedrock/m": {"inputTokens": 7}}
        )
        model = Model('{"summary": "ok"}', fault, _about_page("2"))

        with pytest.raises(TransientError):
            _summarize(summarization, config, model)

        assert _bedrock_tokens(_recorded(tracking)) == TOKENS + 7

    def test_a_throttled_whole_document_call_is_not_marked_failed(
        self, summarization, config, tracking
    ):
        """The path with no sections raises the raw error, so only the handler's
        own check keeps the FAILED status off it."""
        model = Model('{"summary": "ok"}', _throttle(), lambda kwargs: True)

        with pytest.raises(TransientError):
            _summarize(summarization, config, model, sections=False)

        row = _recorded(tracking)
        assert model.raised == 1
        assert row.status == Status.SUMMARIZING
        assert _bedrock_tokens(row) == 0
        assert _earlier_steps_kept(row)

    def test_a_deterministic_failure_is_still_persisted_as_failed(
        self, summarization, config, tracking
    ):
        model = Model('{"summary": "ok"}', _deterministic(), _about_page("2"))

        with pytest.raises(Exception) as caught:
            _summarize(summarization, config, model)

        assert not isinstance(caught.value, TransientError)
        row = _recorded(tracking)
        assert row.status == Status.FAILED
        # The section that completed is counted on the failed document too.
        assert _bedrock_tokens(row) == TOKENS * model.answered == TOKENS
        assert _earlier_steps_kept(row)


# ---------------------------------------------------------------------------
# Classification, page granularity (the preset's default)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def classification():
    return _load("classification_function/index.py", "classification_attempt_tracking")


def _classify(module, config, model):
    document = _document(Status.OCR, sections=False)
    with (
        patch.object(module.Document, "load_document", return_value=document),
        patch.object(module, "get_config", return_value=config),
        patch.object(module, "calculate_lambda_metering", return_value={}),
        patch.object(module, "metrics"),
        patch("idp_common.bedrock.invoke_model", model),
        _storage(),
    ):
        return module.handler(
            {"OCRResult": {"document": {}}, "execution_arn": "arn:x"}, None
        )


@contextlib.contextmanager
def _one_worker(module):
    """One pool worker, so every other call has completed when the last one fails.

    ``region`` is read when the module is imported, before the fixture sets it, and
    the page cache's table is opened in that region.
    """
    with patch.object(module, "MAX_WORKERS", 1), patch.object(module, "region", REGION):
        yield


class TestPageClassification:
    def test_a_throttled_page_leaves_the_document_classifying_with_its_usage(
        self, classification, config, tracking
    ):
        model = Model('{"class": "Payslip"}', _throttle(), _about_page("2"))

        with _one_worker(classification), pytest.raises(TransientError):
            _classify(classification, config, model)

        row = _recorded(tracking)
        assert model.raised == 1 and model.answered == 1
        assert row.status == Status.CLASSIFYING
        # The document as the attempt started it, not the half-classified one.
        assert not row.pages["2"].classification
        assert _bedrock_tokens(row) == TOKENS
        assert _earlier_steps_kept(row)

    def test_the_retry_counts_the_failed_attempts_page_once(
        self, classification, config, tracking
    ):
        """Page 1 is answered from the page cache on the retry, which carries its
        usage; recording the failed attempt must not add it a second time."""
        failing = Model('{"class": "Payslip"}', _throttle(), _about_page("2"))
        with _one_worker(classification), pytest.raises(TransientError):
            _classify(classification, config, failing)

        retry = Model('{"class": "Payslip"}')
        with _one_worker(classification):
            _classify(classification, config, retry)

        row = _recorded(tracking)
        assert retry.answered == 1  # page 2 only; page 1 came from the cache
        assert row.status != Status.FAILED
        assert _bedrock_tokens(row) == TOKENS * (failing.answered + retry.answered)
        assert _earlier_steps_kept(row)

    def test_a_deterministic_page_failure_still_writes_the_classified_document(
        self, classification, config, tracking
    ):
        """The control. The page-level path writes the document it classified,
        failed page included, before raising; it never set FAILED itself (the
        failed execution does). That write is unchanged for a hard failure."""
        model = Model('{"class": "Payslip"}', _deterministic(), _about_page("2"))

        with _one_worker(classification), pytest.raises(ClientError) as caught:
            _classify(classification, config, model)

        assert not isinstance(caught.value, TransientError)
        row = _recorded(tracking)
        assert row.pages["2"].classification == "error (backoff/retry)"
        assert _bedrock_tokens(row) == TOKENS
        assert _earlier_steps_kept(row)


# ---------------------------------------------------------------------------
# Classification, split-then-classify (the path that raises from a pool)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def split_config() -> IDPConfig:
    return _preset_config(
        strategy="split_then_classify",
        split_then_classify={"splitting": {"llm": {"granularity": "page"}}},
    )


class TestSplitThenClassify:
    def test_a_throttled_segment_records_the_splitting_and_labelling_usage(
        self, classification, split_config, tracking
    ):
        def fails(kwargs) -> bool:
            # The second segment's labelling call; every splitting call succeeds.
            return kwargs.get("context") == "SectionClassification" and _about_page(
                "2"
            )(kwargs)

        model = Model(
            '{"document_boundary": "start", "class": "Payslip"}', _throttle(), fails
        )

        with _one_worker(classification), pytest.raises(TransientError):
            _classify(classification, split_config, model)

        row = _recorded(tracking)
        # Two splitting calls and the first segment's labelling call.
        assert model.raised == 1 and model.answered == 3
        assert row.status == Status.CLASSIFYING
        assert _bedrock_tokens(row) == TOKENS * model.answered
        assert _earlier_steps_kept(row)
