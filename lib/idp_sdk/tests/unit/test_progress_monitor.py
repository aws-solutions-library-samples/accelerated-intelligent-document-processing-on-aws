# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``idp_sdk._core.progress_monitor``.

``ProgressMonitor`` is how the SDK answers "is this batch done yet?". It holds no
state about the pipeline itself: every answer comes from invoking the stack's
``LookupFunction`` Lambda, once per poll, with the list of document keys still
believed to be in flight. The Lambda's per-document status strings are then
sorted into four buckets (``completed`` / ``running`` / ``queued`` / ``failed``),
and a document whose status is *terminal* is cached in ``finished_docs`` so later
polls do not ask about it again.

Two things shaped these tests.

**The classification tables are the product.** Whether ``REDACTED_SUPERSEDED``
counts as done, whether ``OCR`` counts as running rather than queued, and whether
``NOT_FOUND`` counts as failed are the decisions a caller sees — as a percentage
that stalls, a monitor that never returns, or a document reported as failed. The
module's own comments record that an earlier explicit list of "running" states
omitted ``OCR``, ``PREPROCESSING`` and ``POSTPROCESSING``, so ordinary
mid-pipeline documents were reported as *Queued*; the fallthrough that fixed it is
pinned here with exactly those states.

**The cache is a commitment, not an optimisation.** Once a document is cached it
is never asked about again, so anything the cache gets wrong is permanent for the
life of the monitor. The tests therefore drive two consecutive polls and assert on
what the *second* invocation asked for, which is the only way the difference
between "re-queried" and "served from cache" is observable. That is also how the
``NOT_FOUND`` defect below is pinned.

The Lambda is exercised through ``botocore``'s ``Stubber`` against a real
``lambda`` client rather than a ``MagicMock``. The request parameters are the
interesting half of ``_batch_query_documents`` — a payload that omitted
``status_only`` or sent the wrong key name would still satisfy a mock — and
``Stubber`` validates them against the real Lambda API model. ``moto`` cannot
serve this: invoking a Lambda for real needs Docker.
"""

import io
import json

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from idp_sdk._core.progress_monitor import ProgressMonitor

LOOKUP_FUNCTION = "idp-stack-LookupFunction"


def _payload(body: dict) -> StreamingBody:
    """Wrap a dict the way Lambda's ``invoke`` returns one: a readable stream."""
    raw = json.dumps(body).encode()
    return StreamingBody(io.BytesIO(raw), len(raw))


def _expect_batch(stubber, document_ids, results):
    """Queue one batch-query response, asserting the request the monitor sends.

    ``expected_params`` is the load-bearing part: it pins the payload shape the
    LookupFunction contract requires (``object_keys`` plus ``status_only``), so a
    renamed key fails here instead of returning an empty result set at run time.
    """
    stubber.add_response(
        "invoke",
        {"StatusCode": 200, "Payload": _payload({"results": results})},
        {
            "FunctionName": LOOKUP_FUNCTION,
            "InvocationType": "RequestResponse",
            "Payload": json.dumps({"object_keys": document_ids, "status_only": True}),
        },
    )


def _expect_single(stubber, document_id, result, function_error=None):
    """Queue one single-document response for the per-document fallback path."""
    response = {"StatusCode": 200, "Payload": _payload(result)}
    if function_error is not None:
        response["FunctionError"] = function_error
    stubber.add_response(
        "invoke",
        response,
        {
            "FunctionName": LOOKUP_FUNCTION,
            "InvocationType": "RequestResponse",
            "Payload": json.dumps({"object_key": document_id}),
        },
    )


@pytest.fixture
def monitor(aws_credentials):
    """A monitor whose Lambda client is real but has no network behind it."""
    return ProgressMonitor(
        stack_name="idp-stack",
        resources={"LookupFunctionName": LOOKUP_FUNCTION},
        region=aws_credentials,
    )


@pytest.fixture
def stubber(monitor):
    with Stubber(monitor.lambda_client) as active:
        yield active


@pytest.fixture
def invocations(monitor):
    """Every ``Invoke`` request the monitor issues, in order, as API kwargs.

    Several claims here are about a call *not* being made. ``Stubber`` turns an
    unexpected call into an exception, but ``get_batch_status`` catches every
    exception and falls back, so the absence of a call is only visible
    indirectly. Counting requests through botocore's own event system states it
    directly.
    """
    seen = []
    monitor.lambda_client.meta.events.register(
        "before-parameter-build.lambda.Invoke",
        lambda params, **kwargs: seen.append(dict(params)),
    )
    return seen


@pytest.mark.unit
class TestConstruction:
    """The monitor refuses to exist without the function it depends on."""

    def test_missing_lookup_function_raises(self, aws_credentials):
        with pytest.raises(ValueError, match="LookupFunctionName not found"):
            ProgressMonitor("idp-stack", {}, region=aws_credentials)

    def test_empty_lookup_function_name_raises(self, aws_credentials):
        """An empty string is what ``StackInfo`` produces for a missing output.

        ``StackInfo.get_resources`` defaults every unmapped output to ``""``, so
        the key is always present and only its emptiness distinguishes a stack
        without a LookupFunction. A truthiness check is therefore required, and a
        ``key in resources`` check would pass a monitor that can never answer.
        """
        with pytest.raises(ValueError, match="LookupFunctionName not found"):
            ProgressMonitor(
                "idp-stack", {"LookupFunctionName": ""}, region=aws_credentials
            )


@pytest.mark.unit
class TestCategorisation:
    """Which bucket each pipeline status lands in."""

    @pytest.mark.parametrize("status", ["COMPLETED", "REDACTED_SUPERSEDED"])
    def test_success_states_count_as_completed(self, monitor, status):
        """``REDACTED_SUPERSEDED`` must count as done, not as a failure.

        A preprocessing hook that redacts a document supersedes the original,
        which therefore never reaches ``COMPLETED``. If it were counted as failed
        the batch would report a failure for a deliberate outcome; if it were
        counted as neither, the batch could never reach 100%.
        """
        summary = _blank_summary(1)
        monitor._categorize_document(
            {"document_id": "a.pdf", "status": status}, summary
        )
        assert [doc["document_id"] for doc in summary["completed"]] == ["a.pdf"]
        assert summary["failed"] == []

    @pytest.mark.parametrize("status", ["FAILED", "ABORTED", "NOT_FOUND"])
    def test_failure_states_count_as_failed(self, monitor, status):
        summary = _blank_summary(1)
        monitor._categorize_document(
            {"document_id": "a.pdf", "status": status}, summary
        )
        assert [doc["document_id"] for doc in summary["failed"]] == ["a.pdf"]

    def test_not_found_is_annotated_with_the_stage_that_should_have_written_it(
        self, monitor
    ):
        """``NOT_FOUND`` gains an explanation the Lambda does not supply.

        The Lambda only reports that no tracking row exists. The monitor turns
        that into an operator-actionable message naming QueueSender, the
        component that writes the row, because "not found" alone sends whoever
        reads it looking in the wrong place.
        """
        summary = _blank_summary(1)
        status = {"document_id": "a.pdf", "status": "NOT_FOUND"}
        monitor._categorize_document(status, summary)
        assert status["error"] == "Document not found in tracking table"
        assert status["failed_step"] == "QueueSender"

    @pytest.mark.parametrize("status", ["QUEUED", "PENDING_UPLOAD", "UNKNOWN"])
    def test_not_started_states_count_as_queued(self, monitor, status):
        summary = _blank_summary(1)
        monitor._categorize_document(
            {"document_id": "a.pdf", "status": status}, summary
        )
        assert [doc["document_id"] for doc in summary["queued"]] == ["a.pdf"]

    @pytest.mark.parametrize(
        "status",
        [
            "OCR",
            "PREPROCESSING",
            "POSTPROCESSING",
            "RULE_VALIDATION_POLICY_CLASSIFICATION",
            "CLASSIFYING",
            "EXTRACTING",
            "HITL_IN_PROGRESS",
            "A_STATUS_THIS_SDK_HAS_NEVER_HEARD_OF",
        ],
    )
    def test_every_other_state_counts_as_running(self, monitor, status):
        """Mid-pipeline states default to running, including unknown future ones.

        The four states named first in this list are the ones an earlier explicit
        allow-list omitted, so documents actively being worked on were displayed
        as *Queued*. The last entry is the point of the fallthrough: a status
        added to the pipeline after this SDK was released must read as in-flight
        rather than as not-started, because a monitor that believes a running
        document is queued will still wait for it but will describe the batch
        wrongly the whole time.
        """
        summary = _blank_summary(1)
        monitor._categorize_document(
            {"document_id": "a.pdf", "status": status}, summary
        )
        assert [doc["document_id"] for doc in summary["running"]] == ["a.pdf"]


@pytest.mark.unit
class TestBatchStatus:
    """End-to-end polling behaviour across one and two polls."""

    def test_no_document_ids_is_not_complete_and_queries_nothing(
        self, monitor, stubber, invocations
    ):
        """An empty batch must not report success.

        ``all_complete`` starts ``False`` and the early return leaves it there.
        Reporting an empty batch as complete would let a caller that failed to
        record any document ids conclude its documents had all been processed.
        """
        summary = monitor.get_batch_status([])
        assert summary["all_complete"] is False
        assert summary["total"] == 0
        assert invocations == []
        stubber.assert_no_pending_responses()

    def test_mixed_batch_is_bucketed_and_not_complete(self, monitor, stubber):
        _expect_batch(
            stubber,
            ["done.pdf", "busy.pdf", "waiting.pdf", "broken.pdf"],
            [
                {
                    "object_key": "done.pdf",
                    "status": "COMPLETED",
                    "timing": {"elapsed": {"total": 42000}},
                },
                {"object_key": "busy.pdf", "status": "EXTRACTING"},
                {"object_key": "waiting.pdf", "status": "QUEUED"},
                {
                    "object_key": "broken.pdf",
                    "status": "FAILED",
                    "error": "Bedrock throttled",
                    "failed_step": "Extraction",
                },
            ],
        )

        summary = monitor.get_batch_status(
            ["done.pdf", "busy.pdf", "waiting.pdf", "broken.pdf"]
        )

        assert [doc["document_id"] for doc in summary["completed"]] == ["done.pdf"]
        assert [doc["document_id"] for doc in summary["running"]] == ["busy.pdf"]
        assert [doc["document_id"] for doc in summary["queued"]] == ["waiting.pdf"]
        assert [doc["document_id"] for doc in summary["failed"]] == ["broken.pdf"]
        assert summary["total"] == 4
        assert summary["all_complete"] is False
        stubber.assert_no_pending_responses()

    def test_elapsed_milliseconds_become_seconds(self, monitor, stubber):
        """The Lambda reports milliseconds; the SDK's contract is seconds.

        Passing the raw value through would inflate every duration and every
        average by a factor of 1000, which is the kind of error a reader
        rationalises rather than notices.
        """
        _expect_batch(
            stubber,
            ["a.pdf", "b.pdf"],
            [
                {
                    "object_key": "a.pdf",
                    "status": "COMPLETED",
                    "timing": {"elapsed": {"total": 2500}},
                },
                {"object_key": "b.pdf", "status": "COMPLETED"},
            ],
        )

        summary = monitor.get_batch_status(["a.pdf", "b.pdf"])

        durations = {
            doc["document_id"]: doc["duration"] for doc in summary["completed"]
        }
        assert durations == {"a.pdf": 2.5, "b.pdf": 0}

    def test_aborted_gets_a_user_facing_reason_the_lambda_does_not_send(
        self, monitor, stubber
    ):
        _expect_batch(
            stubber,
            ["stopped.pdf"],
            [{"object_key": "stopped.pdf", "status": "ABORTED"}],
        )

        summary = monitor.get_batch_status(["stopped.pdf"])

        (doc,) = summary["failed"]
        assert doc["error"] == "Aborted by user"
        assert doc["failed_step"] == "N/A"

    def test_failed_without_detail_falls_back_to_placeholders(self, monitor, stubber):
        _expect_batch(
            stubber, ["broken.pdf"], [{"object_key": "broken.pdf", "status": "FAILED"}]
        )

        summary = monitor.get_batch_status(["broken.pdf"])

        (doc,) = summary["failed"]
        assert doc["error"] == "Unknown error"
        assert doc["failed_step"] == "Unknown"

    def test_all_terminal_means_complete(self, monitor, stubber):
        _expect_batch(
            stubber,
            ["a.pdf", "b.pdf"],
            [
                {"object_key": "a.pdf", "status": "COMPLETED"},
                {"object_key": "b.pdf", "status": "FAILED"},
            ],
        )

        summary = monitor.get_batch_status(["a.pdf", "b.pdf"])

        assert summary["all_complete"] is True

    def test_second_poll_asks_only_about_the_unfinished_documents(
        self, monitor, stubber, invocations
    ):
        """The cache must shrink the next request, not just the next answer.

        This is the behaviour the cache exists for — on a thousand-document batch
        the payload would otherwise keep growing back to a thousand keys every
        few seconds. Asserting the *second* request's payload is the only way to
        tell a working cache from one that is populated and then ignored.
        """
        _expect_batch(
            stubber,
            ["done.pdf", "busy.pdf"],
            [
                {"object_key": "done.pdf", "status": "COMPLETED"},
                {"object_key": "busy.pdf", "status": "OCR"},
            ],
        )
        _expect_batch(
            stubber, ["busy.pdf"], [{"object_key": "busy.pdf", "status": "COMPLETED"}]
        )

        first = monitor.get_batch_status(["done.pdf", "busy.pdf"])
        assert first["all_complete"] is False

        second = monitor.get_batch_status(["done.pdf", "busy.pdf"])

        assert sorted(doc["document_id"] for doc in second["completed"]) == [
            "busy.pdf",
            "done.pdf",
        ]
        assert second["all_complete"] is True
        stubber.assert_no_pending_responses()
        assert [json.loads(call["Payload"])["object_keys"] for call in invocations] == [
            ["done.pdf", "busy.pdf"],
            ["busy.pdf"],
        ]

    def test_a_fully_cached_batch_invokes_nothing_at_all(
        self, monitor, stubber, invocations
    ):
        """Once every document is terminal the monitor stops calling Lambda.

        A third poll that still invoked would mean a completed batch keeps
        billing Lambda invocations for as long as the caller keeps polling.
        """
        _expect_batch(
            stubber,
            ["a.pdf", "b.pdf"],
            [
                {"object_key": "a.pdf", "status": "COMPLETED"},
                {"object_key": "b.pdf", "status": "ABORTED"},
            ],
        )

        monitor.get_batch_status(["a.pdf", "b.pdf"])
        stubber.assert_no_pending_responses()

        cached = monitor.get_batch_status(["a.pdf", "b.pdf"])

        assert cached["all_complete"] is True
        assert len(cached["completed"]) == 1
        assert len(cached["failed"]) == 1
        # One invocation in total, made by the first poll.
        assert len(invocations) == 1

    def test_a_document_that_disappears_between_polls_blocks_completion(
        self, monitor, stubber
    ):
        """A document the Lambda stops reporting on is counted in neither bucket.

        ``all_complete`` compares finished-and-terminal against
        ``len(document_ids)``, and a document missing from the response is
        categorised not at all. So the monitor keeps polling forever rather than
        declaring a batch complete on partial information — the safe direction,
        but worth pinning because the alternative (comparing against the
        *returned* count) would silently report success.
        """
        _expect_batch(
            stubber,
            ["a.pdf", "vanishes.pdf"],
            [
                {"object_key": "a.pdf", "status": "COMPLETED"},
                {"object_key": "vanishes.pdf", "status": "OCR"},
            ],
        )
        _expect_batch(stubber, ["vanishes.pdf"], [])

        monitor.get_batch_status(["a.pdf", "vanishes.pdf"])
        second = monitor.get_batch_status(["a.pdf", "vanishes.pdf"])

        assert second["total"] == 2
        assert len(second["completed"]) == 1
        assert second["running"] == []
        assert second["all_complete"] is False

    def test_a_document_that_regresses_after_completing_keeps_its_cached_status(
        self, monitor, stubber, invocations
    ):
        """A terminal state is treated as final even if the pipeline moves on.

        Nothing re-queries a cached document, so if a document is re-submitted
        under the same key and starts processing again, this monitor still
        reports the earlier COMPLETED. That is the intended trade for the cache,
        and it is only correct because the key identifies one processing run;
        pinned so that a change making terminal states re-queryable has to be a
        deliberate one.
        """
        _expect_batch(
            stubber, ["a.pdf"], [{"object_key": "a.pdf", "status": "COMPLETED"}]
        )

        monitor.get_batch_status(["a.pdf"])
        again = monitor.get_batch_status(["a.pdf"])

        assert again["completed"][0]["status"] == "COMPLETED"
        assert again["all_complete"] is True
        assert len(invocations) == 1

    def test_a_redacted_superseded_document_is_cached_and_never_re_queried(
        self, monitor, stubber, invocations
    ):
        """``REDACTED_SUPERSEDED`` must be terminal, or a finished batch polls forever.

        A preprocessing hook can replace an uploaded document with a redacted
        copy and stop; the original then settles in ``REDACTED_SUPERSEDED`` and
        never reaches ``COMPLETED``. It is in ``_TERMINAL_STATES`` for that
        reason, and this test is about the consequence of it *not* being there.

        The distinction is worth stating because it is easy to get wrong in both
        directions. ``_SUCCESS_STATES`` is what makes such a document count
        towards ``all_complete``, and that is covered separately. What
        ``_TERMINAL_STATES`` controls is the ``finished_docs`` cache, so dropping
        the state from it does not hang the caller — it makes every later poll
        re-query a document whose answer cannot change, one Lambda invocation per
        document per poll for as long as the caller keeps polling. The assertion
        that distinguishes the two implementations is therefore the invocation
        count, not the returned summary: the summary is identical either way.

        This test was added because a mutation that removed the state from
        ``_TERMINAL_STATES`` survived the suite — the caching behaviour was
        covered for ``COMPLETED``, ``ABORTED`` and ``NOT_FOUND``, but not for the
        one state whose membership carries a comment explaining why it is there.
        """
        _expect_batch(
            stubber,
            ["redacted.pdf"],
            [{"object_key": "redacted.pdf", "status": "REDACTED_SUPERSEDED"}],
        )

        first = monitor.get_batch_status(["redacted.pdf"])
        assert [doc["document_id"] for doc in first["completed"]] == ["redacted.pdf"]
        assert first["all_complete"] is True
        stubber.assert_no_pending_responses()

        # No second response is queued, so a second query would fail outright;
        # the cache is what makes this poll answerable at all.
        second = monitor.get_batch_status(["redacted.pdf"])

        assert "redacted.pdf" in monitor.finished_docs
        assert [doc["document_id"] for doc in second["completed"]] == ["redacted.pdf"]
        assert second["all_complete"] is True
        assert len(invocations) == 1

    def test_not_found_is_cached_so_an_early_poll_marks_a_document_failed_forever(
        self, monitor, stubber, invocations
    ):
        """DEFECT: ``NOT_FOUND`` is cached as terminal (progress_monitor.py:22-23).

        ``NOT_FOUND`` means only that no tracking row exists *yet*. Between the S3
        upload and the QueueSender Lambda writing the row there is a window in
        which that is the correct answer for a document which will process
        perfectly. Because ``NOT_FOUND`` is in ``_TERMINAL_STATES`` the first poll
        to land in that window caches the document as finished, and
        ``_FAILED_STATES`` then reports it as failed for the entire life of the
        monitor — no later poll ever asks about it again.

        This test pins the current behaviour: the second poll returns COMPLETED
        from the Lambda's point of view, the monitor never asks, and the batch
        reports one failure and ``all_complete`` true. A caller sees a spurious
        failure and, if it is gating on the result, treats a good document as
        bad. Not fixed here — the fix is production code.
        """
        _expect_batch(
            stubber,
            ["racing.pdf"],
            [{"object_key": "racing.pdf", "status": "NOT_FOUND"}],
        )

        first = monitor.get_batch_status(["racing.pdf"])
        assert len(first["failed"]) == 1
        stubber.assert_no_pending_responses()

        # A second poll would find it COMPLETED, but no second invocation is made.
        second = monitor.get_batch_status(["racing.pdf"])

        assert [doc["document_id"] for doc in second["failed"]] == ["racing.pdf"]
        assert second["completed"] == []
        assert second["all_complete"] is True
        assert len(invocations) == 1


@pytest.mark.unit
class TestBatchQueryFailureFallback:
    """What happens when the one batched invocation does not come back."""

    def test_a_lambda_function_error_falls_back_to_per_document_queries(
        self, monitor, stubber
    ):
        """A batch-level failure must not fail the poll.

        ``_batch_query_documents`` raises on ``FunctionError``; the caller catches
        it and re-asks one document at a time. Losing that fallback would turn a
        single bad document in a batch payload into a poll that returns nothing
        for any of them.
        """
        stubber.add_response(
            "invoke",
            {
                "StatusCode": 200,
                "FunctionError": "Unhandled",
                "Payload": _payload({"errorMessage": "batch handler blew up"}),
            },
            {
                "FunctionName": LOOKUP_FUNCTION,
                "InvocationType": "RequestResponse",
                "Payload": json.dumps(
                    {"object_keys": ["a.pdf", "b.pdf"], "status_only": True}
                ),
            },
        )
        _expect_single(
            stubber, "a.pdf", {"status": "COMPLETED", "NumSections": 3, "Duration": 12}
        )
        _expect_single(stubber, "b.pdf", {"status": "RUNNING", "CurrentStep": "OCR"})

        summary = monitor.get_batch_status(["a.pdf", "b.pdf"])

        (completed,) = summary["completed"]
        assert completed["document_id"] == "a.pdf"
        assert completed["num_sections"] == 3
        (running,) = summary["running"]
        assert running["current_step"] == "OCR"
        assert summary["all_complete"] is False
        stubber.assert_no_pending_responses()

    def test_the_fallback_still_caches_terminal_documents(self, monitor, stubber):
        """The fallback must populate the cache too, or every poll re-fans-out.

        If only the batch path cached, a stack whose batch handler is broken
        would issue one invocation per document per poll indefinitely.
        """
        stubber.add_client_error("invoke", service_error_code="ServiceException")
        _expect_single(stubber, "a.pdf", {"status": "COMPLETED"})

        monitor.get_batch_status(["a.pdf"])
        assert "a.pdf" in monitor.finished_docs
        stubber.assert_no_pending_responses()

        cached = monitor.get_batch_status(["a.pdf"])
        assert cached["all_complete"] is True

    def test_a_single_lookup_that_errors_is_reported_running_and_retried(
        self, monitor, stubber
    ):
        """An ``ERROR`` status is non-terminal, so it is asked about again.

        ``get_document_status`` converts any Lambda-level failure into a status of
        ``ERROR``, which is in none of the state sets and so falls through to
        *running*. That is the right outcome — a lookup failure says nothing about
        the document — and it is deliberately not cached, so the next poll retries.
        """
        stubber.add_client_error("invoke", service_error_code="ServiceException")
        _expect_single(
            stubber,
            "a.pdf",
            {"errorMessage": "handler timed out"},
            function_error="Unhandled",
        )

        summary = monitor.get_batch_status(["a.pdf"])

        (doc,) = summary["running"]
        assert doc["status"] == "ERROR"
        assert doc["error"] == "handler timed out"
        assert monitor.finished_docs == {}

    def test_one_document_raising_does_not_lose_the_rest_of_the_poll(
        self, monitor, stubber, monkeypatch
    ):
        """A per-document lookup that raises leaves that document as UNKNOWN.

        This handler cannot be reached through the ordinary call graph:
        ``get_document_status`` wraps its whole body in ``except Exception`` and
        returns an ``ERROR`` status instead of raising, so the fallback loop's own
        ``try`` is currently unreachable from production code. The behaviour it
        implements is still the one a caller depends on — a poll over fifty
        documents must not be lost because one lookup raised — so it is driven
        here by making the single-document lookup raise for one key only.

        A failure here would mean a single unexpected exception in a lookup
        discards the status of every other document in the same poll.
        """
        stubber.add_client_error("invoke", service_error_code="ServiceException")

        real_get_document_status = monitor.get_document_status

        def flaky(doc_id):
            if doc_id == "poison.pdf":
                raise RuntimeError("lookup exploded")
            return real_get_document_status(doc_id)

        monkeypatch.setattr(monitor, "get_document_status", flaky)
        _expect_single(stubber, "fine.pdf", {"status": "COMPLETED"})

        summary = monitor.get_batch_status(["poison.pdf", "fine.pdf"])

        assert [doc["document_id"] for doc in summary["completed"]] == ["fine.pdf"]
        assert summary["queued"] == [
            {
                "document_id": "poison.pdf",
                "status": "UNKNOWN",
                "error": "lookup exploded",
            }
        ]
        assert summary["all_complete"] is False


@pytest.mark.unit
class TestGetDocumentStatus:
    """The single-document lookup, which the fallback and the CLI both use."""

    def test_status_specific_fields_are_added_per_status(self, monitor, stubber):
        _expect_single(
            stubber,
            "a.pdf",
            {
                "status": "COMPLETED",
                "NumSections": 4,
                "WorkflowExecutionArn": "arn:aws:states:us-east-1:1:execution:x",
                "StartTime": "2026-01-01T00:00:00Z",
                "EndTime": "2026-01-01T00:01:00Z",
                "Duration": 60,
            },
        )

        status = monitor.get_document_status("a.pdf")

        assert status == {
            "document_id": "a.pdf",
            "status": "COMPLETED",
            "workflow_arn": "arn:aws:states:us-east-1:1:execution:x",
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:01:00Z",
            "duration": 60,
            "num_sections": 4,
        }

    def test_aborted_and_failed_carry_error_detail(self, monitor, stubber):
        _expect_single(
            stubber, "stopped.pdf", {"status": "ABORTED", "FailedStep": "Extraction"}
        )
        _expect_single(
            stubber,
            "broken.pdf",
            {"status": "FAILED", "Error": "AccessDenied", "FailedStep": "OCR"},
        )

        aborted = monitor.get_document_status("stopped.pdf")
        failed = monitor.get_document_status("broken.pdf")

        assert aborted["error"] == "Aborted by user"
        assert aborted["failed_step"] == "Extraction"
        assert failed["error"] == "AccessDenied"
        assert failed["failed_step"] == "OCR"

    def test_an_absent_status_field_reads_as_unknown(self, monitor, stubber):
        _expect_single(stubber, "a.pdf", {})
        assert monitor.get_document_status("a.pdf")["status"] == "UNKNOWN"

    def test_a_transport_failure_becomes_an_error_status_not_an_exception(
        self, monitor, stubber
    ):
        """Callers poll in a loop; a raised exception would end the loop.

        Returning an ``ERROR`` status instead keeps the poll alive and leaves the
        document in the running bucket to be retried.
        """
        stubber.add_client_error(
            "invoke", service_error_code="TooManyRequestsException"
        )

        status = monitor.get_document_status("a.pdf")

        assert status["document_id"] == "a.pdf"
        assert status["status"] == "ERROR"
        assert "TooManyRequestsException" in status["error"]


@pytest.mark.unit
class TestDerivedViews:
    """Read-only summaries computed from an already-collected status dict."""

    def test_recent_completions_are_newest_first_and_limited(self, monitor):
        status_data = {
            "completed": [
                {"document_id": "old.pdf", "end_time": "2026-01-01T00:00:00Z"},
                {"document_id": "new.pdf", "end_time": "2026-01-03T00:00:00Z"},
                {"document_id": "mid.pdf", "end_time": "2026-01-02T00:00:00Z"},
            ]
        }

        recent = monitor.get_recent_completions(status_data, limit=2)

        assert [doc["document_id"] for doc in recent] == ["new.pdf", "mid.pdf"]

    def test_a_completion_without_an_end_time_sorts_last(self, monitor):
        """Missing timestamps must not crash the sort or jump the queue.

        The batch path never populates ``end_time``, so an empty string is the
        normal case rather than an edge case; it sorts below every real ISO
        timestamp, which puts unknown-time documents at the bottom of the
        "recently completed" display instead of the top.
        """
        status_data = {
            "completed": [
                {"document_id": "no-time.pdf"},
                {"document_id": "timed.pdf", "end_time": "2026-01-01T00:00:00Z"},
            ]
        }

        recent = monitor.get_recent_completions(status_data)

        assert [doc["document_id"] for doc in recent] == ["timed.pdf", "no-time.pdf"]

    def test_statistics_over_a_mixed_batch(self, monitor):
        status_data = {
            "total": 8,
            "completed": [
                {"duration": 10.0},
                {"duration": 30.0},
                {"duration": 0},  # unmeasured; must not drag the average down
            ],
            "failed": [{"duration": 5.0}],
            "running": [{}, {}],
            "queued": [{}, {}],
            "all_complete": False,
        }

        stats = monitor.calculate_statistics(status_data)

        assert stats["completed"] == 3
        assert stats["failed"] == 1
        assert stats["running"] == 2
        assert stats["queued"] == 2
        assert stats["completion_percentage"] == pytest.approx(50.0)
        assert stats["success_rate"] == pytest.approx(75.0)
        # Only the two measured durations: a zero means "not measured", and
        # averaging it in would understate every batch's mean duration.
        assert stats["avg_duration_seconds"] == pytest.approx(20.0)
        assert stats["all_complete"] is False

    def test_statistics_on_an_empty_batch_do_not_divide_by_zero(self, monitor):
        stats = monitor.calculate_statistics(_blank_summary(0))

        assert stats["completion_percentage"] == 0
        assert stats["success_rate"] == 0
        assert stats["avg_duration_seconds"] == 0

    def test_failed_documents_report_abort_as_an_abort(self, monitor):
        """An aborted document is not a defect, and must not read like one.

        ``get_failed_documents`` re-derives the abort wording from the status
        rather than trusting whatever ``error`` the upstream dict carries, so a
        user cancellation is never presented as a pipeline failure — even when a
        stale error string is still attached.
        """
        status_data = {
            "failed": [
                {
                    "document_id": "stopped.pdf",
                    "status": "ABORTED",
                    "error": "States.Timeout",
                    "failed_step": "Extraction",
                },
                {
                    "document_id": "broken.pdf",
                    "status": "FAILED",
                    "error": "ValidationException",
                    "failed_step": "Classification",
                },
                {"document_id": "bare.pdf"},
            ]
        }

        failed = monitor.get_failed_documents(status_data)

        assert failed == [
            {
                "document_id": "stopped.pdf",
                "error": "Aborted by user",
                "failed_step": "N/A",
            },
            {
                "document_id": "broken.pdf",
                "error": "ValidationException",
                "failed_step": "Classification",
            },
            {
                "document_id": "bare.pdf",
                "error": "Unknown error",
                "failed_step": "Unknown",
            },
        ]


def _blank_summary(total: int) -> dict:
    """The summary skeleton ``get_batch_status`` builds before bucketing."""
    return {
        "completed": [],
        "running": [],
        "queued": [],
        "failed": [],
        "all_complete": False,
        "total": total,
    }
