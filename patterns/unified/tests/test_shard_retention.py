# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A succeeded extraction must survive a transient fault in the handler's tail.

Both extraction handlers finish by calling ``Document.serialize_document``, which
with the default ``size_threshold_kb=0`` **always** performs an S3 ``put_object``
and is **not** wrapped in ``try``. So a ``SlowDown``, a ``ServiceUnavailable`` or a
read timeout there is re-raised, classified as ``TransientError``, and retried by
Step Functions — after the extraction has already fully succeeded and been paid for.

That makes anything deleted before that point a hazard, and the two handlers face
different consequences:

* **The shard-merge handler.** ``merge_section_shards`` re-loads every shard from S3
  on entry and raises ``RuntimeError`` if any is absent, so the shards are a
  *precondition*. Deleting them and then hitting a transient fault turns a
  successful merge into a permanent "shard(s) have no persisted result" failure and
  discards every token of shard inference. There is no safe point inside the Lambda
  to delete them at, because whether the *state* succeeded is not observable from
  inside it — so nothing there deletes them, and the working bucket's lifecycle rule
  reclaims them.
* **The in-process handler.** Its checkpoint and per-shard results are an
  *optimisation*: without them a retry re-runs extraction from scratch, costing
  money rather than correctness. So deleting is safe, and it happens last — after
  the serialise that can fail.

These tests drive the real handlers. The window they cover only became reachable
when the cleanup prefix was corrected: while the cleanup addressed a prefix nothing
had been written to, deleting "successfully" was harmless.
"""

from __future__ import annotations

import importlib.util
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from idp_common.config.models import IDPConfig
from idp_common.extraction.runtime import (
    shard_persistence_section_id,
    shard_results_prefix,
)
from idp_common.models import Document, Section, Status

_SRC = os.path.join(os.path.dirname(__file__), "../src/extraction_function")

EXECUTION_ARN = "arn:aws:states:us-east-1:123456789012:execution:idp:abc-123"


def _load(module_name: str, filename: str):
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


shard_runtime = _load("sfn_runtime_handler", "sfn_runtime_handler.py")
extraction_index = _load("extraction_index", "index.py")


class _Context:
    function_name = "extraction"
    memory_limit_in_mb = 2048
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:extraction"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 300_000


def _slow_down() -> botocore.exceptions.ClientError:
    """What S3 returns when it is shedding load. Classified transient, so
    ExtractionStep / ExtractionMergeStep retry it."""
    return botocore.exceptions.ClientError(
        {"Error": {"Code": "SlowDown", "Message": "Please reduce your request rate."}},
        "PutObject",
    )


def _document() -> Document:
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
            Section(
                section_id="2",
                classification="bank-statement",
                page_ids=["2"],
                extraction_result_uri="",
            ),
        ],
    )


def _shard_prefix(section: Section) -> str:
    return shard_results_prefix(
        EXECUTION_ARN,
        shard_persistence_section_id(section.classification, section.page_ids),
    )


def _s3_holding_one_shard(prefix: str) -> MagicMock:
    """A fake S3 that answers a hit for the real shard prefix and nothing else."""
    s3 = MagicMock()
    s3.list_objects_v2.side_effect = lambda Bucket, Prefix: (  # noqa: N803
        {"Contents": [{"Key": f"{prefix}shard_0_0.json"}]} if Prefix == prefix else {}
    )
    return s3


# ---------------------------------------------------------------------------
# The shard-merge handler: nothing is deleted, ever.
# ---------------------------------------------------------------------------


def _drive_merge(monkeypatch, *, serialize_error: BaseException | None):
    document = _document()
    section = next(s for s in document.sections if s.section_id == "2")
    s3 = _s3_holding_one_shard(_shard_prefix(section))

    monkeypatch.setattr(
        shard_runtime, "_load", lambda event: ("working", document, IDPConfig())
    )
    monkeypatch.setattr(
        shard_runtime, "create_document_service", lambda *a, **kw: MagicMock()
    )
    monkeypatch.setattr(shard_runtime, "_get_s3_client", lambda: s3)

    service = MagicMock()
    service.merge_section_shards.side_effect = lambda document, **kw: document
    monkeypatch.setattr(
        shard_runtime,
        "extraction",
        SimpleNamespace(
            ExtractionService=lambda **kw: service,
            S3ShardPersistence=lambda **kw: MagicMock(),
        ),
    )
    if serialize_error is not None:
        monkeypatch.setattr(
            Document,
            "serialize_document",
            lambda self, *a, **kw: (_ for _ in ()).throw(serialize_error),
        )
    else:
        monkeypatch.setattr(
            Document, "serialize_document", lambda self, *a, **kw: {"inline": True}
        )

    event = {"mode": "merge", "section_id": "2", "execution_arn": EXECUTION_ARN}
    return shard_runtime.handler(event, _Context()), s3


@pytest.mark.unit
def test_a_transient_fault_in_the_merge_tail_leaves_the_shards_intact(monkeypatch):
    """The reproduction. A successful merge whose response write hits SlowDown must
    not have destroyed the shards its own retry needs."""
    from idp_common.utils.transient_errors import TransientError

    with pytest.raises(TransientError):
        _drive_merge(monkeypatch, serialize_error=_slow_down())

    # Re-derive the fake so the assertion reads the calls made during the run.
    # (The handler holds the same MagicMock; fetch it back through the module.)
    s3 = shard_runtime._get_s3_client()
    assert not s3.delete_objects.called, (
        "the merge handler deleted the per-shard results and then failed "
        "transiently. ExtractionMergeStep retries TransientError, and "
        "merge_section_shards raises if any shard is absent, so the retry fails "
        "permanently and every token of shard inference is discarded."
    )


@pytest.mark.unit
def test_a_successful_merge_also_leaves_the_shards_intact(monkeypatch):
    """Not deleted on success either, because 'the state succeeded' is not
    observable from inside the Lambda — the response still has to reach Step
    Functions, and the tail can still fail or the function be killed. The working
    bucket's lifecycle rule is what reclaims them."""
    _drive_merge(monkeypatch, serialize_error=None)
    s3 = shard_runtime._get_s3_client()
    assert not s3.delete_objects.called
    assert not s3.list_objects_v2.called, (
        "the merge handler is still listing the shard prefix, which means a cleanup "
        "call has come back. Nothing in this handler may delete them"
    )


@pytest.mark.unit
def test_the_merge_handler_has_no_shard_cleanup_left_in_it():
    """Stated on the module rather than only on one run, so a cleanup reintroduced
    on a branch these tests do not exercise still fails."""
    assert not hasattr(shard_runtime, "_cleanup_shards"), (
        "sfn_runtime_handler regained a shard cleanup. There is no safe point in "
        "this handler to delete per-shard results: merge_section_shards requires "
        "every shard present, and a retry can follow a merge that already succeeded."
    )


# ---------------------------------------------------------------------------
# The in-process handler: deleted, but only after the serialise that can fail.
# ---------------------------------------------------------------------------


@pytest.fixture
def in_process(monkeypatch):
    monkeypatch.setenv("WORKING_BUCKET", "working")
    config = IDPConfig()
    config.extraction.agentic.enabled = True
    config.extraction.agentic.max_concurrent_batches = 2
    monkeypatch.setattr(extraction_index, "get_config", lambda **kw: config)
    monkeypatch.setattr(
        extraction_index, "create_document_service", lambda *a, **kw: MagicMock()
    )
    document = _document()
    document.workflow_execution_arn = EXECUTION_ARN
    section = next(s for s in document.sections if s.section_id == "2")
    s3 = _s3_holding_one_shard(_shard_prefix(section))
    # `load_extraction_checkpoint` catches `client.exceptions.NoSuchKey`, so the fake
    # has to expose a real exception class there — a MagicMock attribute is not
    # catchable and would surface as a TypeError from the except clause itself.
    s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
    s3.get_object.side_effect = s3.exceptions.NoSuchKey("no checkpoint")
    monkeypatch.setattr(extraction_index, "_get_s3_client", lambda: s3)

    service = MagicMock()
    service.process_document_section.side_effect = lambda document, **kw: document
    monkeypatch.setattr(
        extraction_index,
        "extraction",
        SimpleNamespace(ExtractionService=lambda **kw: service),
    )
    return document, s3


def _drive_in_process(monkeypatch, document, *, serialize_error: BaseException | None):
    if serialize_error is not None:
        monkeypatch.setattr(
            Document,
            "serialize_document",
            lambda self, *a, **kw: (_ for _ in ()).throw(serialize_error),
        )
    else:
        monkeypatch.setattr(
            Document, "serialize_document", lambda self, *a, **kw: {"inline": True}
        )
    event = {"document": document.to_dict(), "section_id": "2"}
    return extraction_index.handler(event, _Context())


@pytest.mark.unit
def test_a_transient_fault_in_the_extraction_tail_leaves_the_resume_state(
    in_process, monkeypatch
):
    """The consequence here is money rather than correctness — a retry re-infers the
    whole section instead of resuming — but there is no reason to pay it."""
    from idp_common.utils.transient_errors import TransientError

    document, s3 = in_process
    with pytest.raises(TransientError):
        _drive_in_process(monkeypatch, document, serialize_error=_slow_down())

    assert not s3.delete_objects.called, (
        "the extraction handler released the per-shard results before the serialise "
        "that failed, so the ExtractionStep retry has nothing to resume from and "
        "re-infers a section that had already succeeded"
    )
    assert not s3.delete_object.called, (
        "the whole-section checkpoint was deleted before the serialise that failed"
    )


@pytest.mark.unit
def test_a_fully_successful_extraction_does_release_its_resume_state(
    in_process, monkeypatch
):
    """The cleanup is moved, not removed: on a clean run the objects are still
    reclaimed, and from the prefix they were actually written to."""
    document, s3 = in_process
    response = _drive_in_process(monkeypatch, document, serialize_error=None)

    assert response["section_id"] == "2"
    section = next(s for s in document.sections if s.section_id == "2")
    assert s3.delete_objects.called, "a clean run must still release its shards"
    assert s3.list_objects_v2.call_args.kwargs["Prefix"] == _shard_prefix(section)
    assert s3.delete_object.called, "a clean run must still release its checkpoint"


@pytest.mark.unit
def test_a_refused_delete_does_not_fail_the_invocation_it_runs_at_the_end_of(
    in_process, monkeypatch
):
    """Putting the cleanup LAST is only safe if the cleanup cannot raise.

    It now runs after the response has been built, so an exception escaping it would
    fail a Lambda whose extraction, persistence and serialise had all succeeded —
    re-creating, from the other direction, the very hazard moving it was meant to
    close. Both helpers swallow and log; this drives the deployed handler with an S3
    that refuses every delete and requires the response back anyway.
    """
    document, s3 = in_process
    s3.delete_objects.side_effect = _slow_down()
    s3.delete_object.side_effect = _slow_down()

    response = _drive_in_process(monkeypatch, document, serialize_error=None)

    assert response["section_id"] == "2"
    assert s3.delete_objects.called and s3.delete_object.called


@pytest.mark.unit
def test_nothing_in_the_cleanup_escapes_even_before_the_first_api_call(
    in_process, monkeypatch
):
    """The test above injects the client, so it cannot see the client *acquisition*.

    That is the gap the shape of these helpers invites: an import and a
    ``_get_s3_client()`` sitting above the ``try`` look like setup rather than work,
    and they are the two steps a fixture that hands in a fake client never exercises.
    A misconfigured region raises from exactly there. Both helpers must swallow it,
    so this replaces ``_get_s3_client`` itself with something that raises — after the
    checkpoint load has already used it, so the handler reaches the cleanup normally.
    """
    document, s3 = in_process
    state = {"serialised": False, "asked_after": 0}

    def _serialize(self, *a, **kw):
        state["serialised"] = True
        return {"inline": True}

    def _client(*, poisoned_after_serialise=True):
        # The handler uses S3 several times before the tail (checkpoint load and
        # save), so the fault has to be keyed on the serialise having happened
        # rather than on a call count.
        if state["serialised"] and poisoned_after_serialise:
            state["asked_after"] += 1
            raise RuntimeError("NoRegionError: you must specify a region")
        return s3

    monkeypatch.setattr(Document, "serialize_document", _serialize)
    monkeypatch.setattr(extraction_index, "_get_s3_client", _client)

    response = extraction_index.handler(
        {"document": document.to_dict(), "section_id": "2"}, _Context()
    )

    assert response["section_id"] == "2", (
        "a raise while acquiring the S3 client in the cleanup failed an invocation "
        "whose extraction, persistence and serialise had all succeeded. Both cleanup "
        "helpers must have the client acquisition INSIDE their try."
    )
    assert state["asked_after"] == 2, (
        "expected both cleanup helpers to ask for a client after the serialise; got "
        f"{state['asked_after']} request(s), so this test asserted less than it reads"
    )
