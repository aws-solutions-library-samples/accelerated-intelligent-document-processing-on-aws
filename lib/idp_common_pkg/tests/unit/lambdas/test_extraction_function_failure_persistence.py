# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Extraction Lambdas: a failing section must still reach DynamoDB (#1049).

Both extraction entry points persisted the section to DynamoDB *after* the
service call returned — ``index.handler`` for the in-process path and
``sfn_runtime_handler.handler`` in ``merge`` mode for the Distributed Map shard
merge — so on a failure the write never happened and the section's record still
held whatever classification left there. The Sections panel showed nothing: no
status icon, no issue, no hint that this section was the one that failed.

Every extraction failure raises, so this is the whole family:
``ExtractionInputTooLarge``, ``ExtractionImageRejected``,
``ModelInvalidToolUseSequence`` and ``ExtractionOutputIncomplete``.

``ExtractionOutputIncomplete`` (#1032) is why it mattered. It is the one that
deliberately persists a *diagnosis* before raising — under
``extraction.row_shortfall_action: fail`` the partial rows and an error-severity
``extraction_rows_below_ocr_estimate`` issue are written to the section's
``result.json`` — so the artifact that fix preserved was being written everywhere
except the place the UI reads.

These tests load the REAL Lambda modules and pin four things:

1. the section is written, carrying the failure as an error-severity issue;
2. a diagnosis the service had already recorded on the section survives the write
   rather than being replaced by a generic one;
3. the original exception propagates **unchanged** — the Step Functions cause
   still names the rows extracted, the OCR estimate and the remedy, and a persist
   that itself fails does not replace it;
4. the per-shard results are **not** deleted on failure, because a retry re-infers
   only the shards that did not finish (#1014).
"""

from __future__ import annotations

import importlib.util
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from idp_common.config.models import IDPConfig
from idp_common.extraction.failure import EXTRACTION_FAILED_CODE
from idp_common.extraction.runtime import (
    shard_persistence_section_id,
    shard_results_prefix,
)
from idp_common.extraction.service import (
    ExtractionInputTooLarge,
    ExtractionOutputIncomplete,
)
from idp_common.models import Document, ProcessingIssue, Section, Status
from idp_common.utils.transient_errors import TransientError

_SRC = os.path.join(
    os.path.dirname(__file__),
    "../../../../../patterns/unified/src/extraction_function",
)

ROW_SHORTFALL_CODE = "extraction_rows_below_ocr_estimate"

#: The sentence #1044 puts in the exception and the Step Functions cause. It must
#: still be what propagates: this change makes the DynamoDB row show the failure
#: too, it does not make the exception quieter.
_SHORTFALL_CAUSE = (
    "Section 2 extraction is materially incomplete: Extracted 43 rows for "
    "transactions but the section's OCR evidences about 1200. Set "
    "extraction.row_shortfall_action to 'warn' to accept a partial list as success."
)


def _load(module_name: str, filename: str):
    """Load a deployed handler by path, with X-Ray's import-time work stubbed.

    By path because ``patterns/unified/src`` is not a package on the test path;
    the same mechanism the sibling assessment-degradation suite uses.
    """
    recorder = MagicMock()
    recorder.capture.return_value = lambda fn: fn
    xray_core = MagicMock()
    xray_core.patch_all = lambda: None
    xray_core.xray_recorder = recorder
    with patch.dict(
        "sys.modules",
        {"aws_xray_sdk": MagicMock(), "aws_xray_sdk.core": xray_core},
    ):
        spec = importlib.util.spec_from_file_location(
            module_name, os.path.join(_SRC, filename)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


extraction_index = _load("extraction_index", "index.py")
shard_runtime = _load("sfn_runtime_handler", "sfn_runtime_handler.py")


class _Context:
    function_name = "extraction"
    memory_limit_in_mb = 2048
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:extraction"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 300_000


def _document() -> Document:
    """Two sections, so an assertion about which INDEX was written can fail.

    Section ``2`` is the one under test and sits at index 1. Writing the wrong
    index would overwrite a sibling section's record with this one's.
    """
    return Document(
        id="doc.pdf",
        input_bucket="in",
        input_key="doc.pdf",
        output_bucket="out",
        status=Status.CLASSIFYING,
        num_pages=2,
        pages={},
        sections=[
            Section(section_id="1", classification="w2", page_ids=["1"]),
            Section(section_id="2", classification="bank-statement", page_ids=["2"]),
        ],
    )


def _row_shortfall_issue() -> ProcessingIssue:
    """What ``_save_results`` has already put on the section by the time
    ``ExtractionOutputIncomplete`` is raised (#1044)."""
    return ProcessingIssue(
        stage="extraction",
        severity="error",
        code=ROW_SHORTFALL_CODE,
        message=(
            "Extracted 43 rows for transactions but the section's OCR evidences "
            "about 1200."
        ),
        section_id="2",
        details={"extracted_rows": 43, "ocr_estimated_rows": 1200},
    )


def _service_that_fails(error: BaseException, *, record_diagnosis: bool):
    """A stand-in for the real service's in-place mutation before it raises.

    ``process_document_section`` and ``merge_section_shards`` both mutate the
    document they are given and return the same object. ``record_diagnosis``
    selects between the two halves of the family: ``ExtractionOutputIncomplete``
    raises from the tail of ``_save_results``, with the issue and the partial
    result already durable; ``ExtractionInputTooLarge`` raises from the model call
    long before ``_save_results``, so the section carries nothing.
    """

    def _run(document: Document, *args: Any, **kwargs: Any):
        section = next(s for s in document.sections if s.section_id == "2")
        if record_diagnosis:
            section.processing_issues = [_row_shortfall_issue()]
            section.extraction_result_uri = "s3://out/doc.pdf/sections/2/result.json"
        document.errors.append(str(error))
        raise error

    service = MagicMock()
    service.process_document_section.side_effect = _run
    service.merge_section_shards.side_effect = _run
    return service


# ---------------------------------------------------------------------------
# The in-process path: patterns/unified/src/extraction_function/index.py
# ---------------------------------------------------------------------------


@pytest.fixture
def in_process(monkeypatch):
    """``index.handler`` with every outbound dependency stubbed.

    ``WORKING_BUCKET`` is empty so ``serialize_document`` returns the document
    inline instead of compressing it to S3.
    """
    monkeypatch.setenv("WORKING_BUCKET", "")
    monkeypatch.setattr(extraction_index, "get_config", lambda **kw: IDPConfig())
    doc_service = MagicMock()
    monkeypatch.setattr(
        extraction_index, "create_document_service", lambda *a, **kw: doc_service
    )
    return doc_service


def _invoke_in_process(monkeypatch, service):
    monkeypatch.setattr(
        extraction_index,
        "extraction",
        SimpleNamespace(ExtractionService=lambda **kw: service),
    )
    event = {"document": _document().to_dict(), "section_id": "2"}
    return extraction_index.handler(event, _Context())


@pytest.mark.unit
def test_a_row_shortfall_failure_persists_the_section_it_diagnosed(
    in_process, monkeypatch
):
    """The #1032 case end to end at the Lambda boundary.

    Both issues reach the write. The shortfall issue names what was lost and the
    remedy; ``extraction_failed`` says the section FAILED, which the shortfall
    issue alone cannot say — it is written at ``warning`` severity when
    ``row_shortfall_action`` is ``warn`` and the document completes.
    """
    error = ExtractionOutputIncomplete(_SHORTFALL_CAUSE)
    service = _service_that_fails(error, record_diagnosis=True)

    with pytest.raises(ExtractionOutputIncomplete) as raised:
        _invoke_in_process(monkeypatch, service)

    # (3) The exception is untouched: same instance, same sentence.
    assert raised.value is error
    assert str(raised.value) == _SHORTFALL_CAUSE

    # (1) The section was written, at its own index.
    assert in_process.update_document_section.called
    kwargs = in_process.update_document_section.call_args.kwargs
    assert kwargs["document_id"] == "doc.pdf"
    assert kwargs["section_index"] == 1
    persisted = kwargs["section"]
    assert persisted.section_id == "2"

    # (2) The diagnosis survives alongside the failure.
    assert [pi.code for pi in persisted.processing_issues] == [
        ROW_SHORTFALL_CODE,
        EXTRACTION_FAILED_CODE,
    ]
    assert persisted.processing_issues[0].details["extracted_rows"] == 43
    failure = persisted.processing_issues[1]
    assert failure.severity == "error"
    assert failure.stage == "extraction"
    assert "ExtractionOutputIncomplete" in failure.root_cause
    assert "row_shortfall_action" in failure.root_cause

    # The partial result is still referenced, so the Visual Editor can open it.
    assert persisted.extraction_result_uri == "s3://out/doc.pdf/sections/2/result.json"


@pytest.mark.unit
def test_a_failure_that_never_reached_save_results_is_still_visible(
    in_process, monkeypatch
):
    """The rest of the family. ``ExtractionInputTooLarge`` raises from the model
    call, so the section carries no diagnosis of its own — without this the
    section's record would be indistinguishable from one that was never reached."""
    cause = (
        "Section 2's extraction request exceeded the model's input limit. Reduce "
        "the pages per shard or switch to Advanced extraction."
    )
    error = ExtractionInputTooLarge(cause)

    with pytest.raises(ExtractionInputTooLarge):
        _invoke_in_process(
            monkeypatch, _service_that_fails(error, record_diagnosis=False)
        )

    persisted = in_process.update_document_section.call_args.kwargs["section"]
    assert [pi.code for pi in persisted.processing_issues] == [EXTRACTION_FAILED_CODE]
    # The remedy travels in root_cause rather than being paraphrased.
    assert persisted.processing_issues[0].root_cause == (
        f"ExtractionInputTooLarge: {cause}"
    )


@pytest.mark.unit
def test_a_failing_write_does_not_replace_the_original_error(in_process, monkeypatch):
    """The original error is what the user needs. A DynamoDB failure while trying
    to make it more visible must not surface in its place — the Step Functions
    cause would then report an unavailable table instead of the lost rows."""
    in_process.update_document_section.side_effect = RuntimeError(
        "dynamodb unreachable"
    )
    error = ExtractionOutputIncomplete(_SHORTFALL_CAUSE)

    with pytest.raises(ExtractionOutputIncomplete) as raised:
        _invoke_in_process(
            monkeypatch, _service_that_fails(error, record_diagnosis=True)
        )

    assert raised.value is error
    assert "dynamodb" not in str(raised.value)


@pytest.mark.unit
def test_a_transient_failure_does_not_mark_the_section(in_process, monkeypatch):
    """A read timeout is retried by ExtractionStep — eight attempts at 2.5x backoff
    from a 10-second interval, which is most of three hours. Marking the section
    failed for that long and then clearing it is a false alarm, not a diagnosis, and
    the sibling assessment path declines the same case. The retry classification
    itself is untouched: it still surfaces as TransientError."""
    error = botocore.exceptions.ReadTimeoutError(
        endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"
    )

    with pytest.raises(TransientError):
        _invoke_in_process(
            monkeypatch, _service_that_fails(error, record_diagnosis=False)
        )

    assert not in_process.update_document_section.called


@pytest.mark.unit
def test_a_bounded_root_cause_keeps_the_write_under_the_item_limit(
    in_process, monkeypatch
):
    """What reaches the write must be bounded, and still useful.

    An exception echoing document content — a schema-validation failure over a merged
    long list renders each offending row — would otherwise push the section map past
    DynamoDB's 400 KB item ceiling, and this handler swallows that write error
    deliberately, so the section would end up unmarked. The bound's own behaviour is
    covered in tests/unit/test_processing_issue_bounds.py; this asserts the boundary
    the Lambda hands to DynamoDB.
    """
    from idp_common.models import ProcessingIssue as PI

    remedy = "Use Advanced extraction or split the document."
    error = ExtractionInputTooLarge(f"row content: {'x' * 500_000} {remedy}")

    with pytest.raises(ExtractionInputTooLarge):
        _invoke_in_process(
            monkeypatch, _service_that_fails(error, record_diagnosis=False)
        )

    persisted = in_process.update_document_section.call_args.kwargs["section"]
    root_cause = persisted.processing_issues[0].root_cause
    assert len(root_cause.encode("utf-8")) <= PI.MAX_ROOT_CAUSE_BYTES
    # Head and tail both survive: the exception type identifies the failure, and the
    # remedy is the sentence a reader acts on.
    assert root_cause.startswith("ExtractionInputTooLarge: row content:")
    assert root_cause.endswith(remedy)


@pytest.mark.unit
def test_a_successful_extraction_records_no_failure_issue(in_process, monkeypatch):
    """The guard on the other direction: a section that succeeded must not be
    flagged, or every document would show an error in the Sections panel."""
    service = MagicMock()

    def _succeed(document: Document, *a: Any, **kw: Any):
        next(
            s for s in document.sections if s.section_id == "2"
        ).extraction_result_uri = "s3://out/doc.pdf/sections/2/result.json"
        return document

    service.process_document_section.side_effect = _succeed

    result = _invoke_in_process(monkeypatch, service)

    persisted = in_process.update_document_section.call_args.kwargs["section"]
    assert not [
        pi
        for pi in (persisted.processing_issues or [])
        if pi.code == EXTRACTION_FAILED_CODE
    ]
    returned = Document.from_dict(result["document"])
    assert returned.status != Status.FAILED


# ---------------------------------------------------------------------------
# The shard-merge path: sfn_runtime_handler.py, mode="merge"
# ---------------------------------------------------------------------------


@pytest.fixture
def merge_mode(monkeypatch):
    """``sfn_runtime_handler.handler`` in ``merge`` mode, with S3 and DynamoDB
    stubbed. Returns ``(document_service, s3_client)``.

    The working bucket is empty so ``serialize_document`` returns the document
    inline instead of compressing it to S3; in merge mode that value comes from
    ``_load``, not from the environment.
    """
    document = _document()
    monkeypatch.setattr(
        shard_runtime, "_load", lambda event: ("", document, IDPConfig())
    )
    doc_service = MagicMock()
    monkeypatch.setattr(
        shard_runtime, "create_document_service", lambda *a, **kw: doc_service
    )
    # The fake S3 answers a hit for the prefix shards are ACTUALLY written to and
    # nothing else, which is what the real bucket does. A mock returning Contents
    # for any prefix would pass against a cleanup addressing a prefix that has
    # never held an object — the exact defect
    # tests/unit/extraction/test_shard_cleanup_prefix.py exists for.
    section = next(s for s in document.sections if s.section_id == "2")
    shard_prefix = shard_results_prefix(
        "arn:exec",
        shard_persistence_section_id(section.classification, section.page_ids),
    )
    s3_client = MagicMock()
    s3_client.list_objects_v2.side_effect = lambda Bucket, Prefix: (  # noqa: N803
        {"Contents": [{"Key": f"{shard_prefix}shard_0_0.json"}]}
        if Prefix == shard_prefix
        else {}
    )
    monkeypatch.setattr(shard_runtime, "_get_s3_client", lambda: s3_client)
    monkeypatch.setenv("WORKING_BUCKET", "")
    return doc_service, s3_client


def _invoke_merge(monkeypatch, service):
    monkeypatch.setattr(
        shard_runtime,
        "extraction",
        SimpleNamespace(
            ExtractionService=lambda **kw: service,
            S3ShardPersistence=lambda **kw: MagicMock(),
        ),
    )
    return shard_runtime.handler(
        {"mode": "merge", "section_id": "2", "execution_arn": "arn:exec"}, _Context()
    )


@pytest.mark.unit
def test_a_failed_merge_persists_the_section_and_keeps_its_shards(
    merge_mode, monkeypatch
):
    """The merge shares ``_save_results`` with the in-process path, so it shares
    its raising failures. Same guarantee, plus the shard-retention decision."""
    doc_service, s3_client = merge_mode
    error = ExtractionOutputIncomplete(_SHORTFALL_CAUSE)

    with pytest.raises(ExtractionOutputIncomplete) as raised:
        _invoke_merge(monkeypatch, _service_that_fails(error, record_diagnosis=True))

    assert raised.value is error

    kwargs = doc_service.update_document_section.call_args.kwargs
    assert kwargs["section_index"] == 1
    assert [pi.code for pi in kwargs["section"].processing_issues] == [
        ROW_SHORTFALL_CODE,
        EXTRACTION_FAILED_CODE,
    ]

    # (4) The per-shard results are KEPT. merge_section_shards re-loads every shard
    # from S3 on entry and raises if any is absent, so releasing them before
    # re-raising would turn a retryable merge failure (ExtractionMergeStep retries
    # the transient families) into a permanent "shard(s) have no persisted result"
    # on the next attempt.
    assert not s3_client.delete_objects.called


@pytest.mark.unit
def test_a_successful_merge_persists_the_section_and_still_keeps_its_shards(
    merge_mode, monkeypatch
):
    """The other half of the shard decision, and it is the same answer.

    Success is not a safe moment to release them either, because success *here* is
    not the state succeeding: the tail of this branch serialises the document, which
    always writes to S3 and is not wrapped, so a transient fault after a merge that
    already worked puts ExtractionMergeStep into a retry that needs every shard
    present. The working bucket's lifecycle rule is what reclaims them. The window
    itself is reproduced in ``patterns/unified/tests/test_shard_retention.py``.
    """
    doc_service, s3_client = merge_mode
    service = MagicMock()
    service.merge_section_shards.side_effect = lambda document, **kw: document

    _invoke_merge(monkeypatch, service)

    assert doc_service.update_document_section.called
    assert not s3_client.delete_objects.called
