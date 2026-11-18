# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A throttled call in split-then-classify retries the step, not fail the document (#2034).

Split-then-classify makes one section classification call per segment from a thread
pool, and the splitting call makes one call per page or per packet window. Each of
those calls used to be caught on its own: a Bedrock ``ThrottlingException`` that
outlasted the client's retries became a string in ``document.errors``, and
ProcessResults later failed the whole document with a plain ``Exception`` that no
retrier matches. Under load that lost 8 to 15 packets in 100.

A throttle or another transient error (``is_transient_error``) is now raised as
``TransientError``, the name ``ClassificationStep`` retries. Deterministic errors keep
their per-section and per-window handling, which the tests below hold next to each
transient case so that a fix which raised everything would fail them.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    ReadTimeoutError,
)

from idp_common.classification import service as service_module
from idp_common.classification.service import ClassificationService
from idp_common.config.schema_constants import UNCLASSIFIED_CLASS
from idp_common.decision_model import DecisionModelUnavailable
from idp_common.lambda_deadline import set_lambda_deadline_epoch
from idp_common.utils.transient_errors import TransientError
from tests.unit.classification.test_decision_model_section_classification import (
    FakeDecider,
    _split,
)
from tests.unit.classification.test_decision_model_section_classification import (
    _service as _decision_model_service,
)
from tests.unit.classification.test_split_then_classify import (
    _document,
    _page_texts,
    _png,
    _reply,
    _service,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_lambda_deadline():
    """Clear the process-wide Lambda deadline around each test.

    The retry ladders stop at once when a deadline is in the past, and a test
    elsewhere in the same worker can leave one set, which made these counts flaky.
    """
    set_lambda_deadline_epoch(None)
    yield
    set_lambda_deadline_epoch(None)


SPLITTING = ClassificationService.STAGE_SPLITTING
SECTION_CLASSIFICATION = ClassificationService.STAGE_SECTION_CLASSIFICATION


def _client_error(code: str, message: str = "m", operation: str = "Converse"):
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def _throttle() -> ClientError:
    """What ``BedrockClient`` re-raises once its own throttling retries run out."""
    return _client_error(
        "ThrottlingException", "Too many requests, please wait before trying again."
    )


def _text(content: List[Dict[str, Any]]) -> str:
    return "".join(b["text"] for b in content if "text" in b)


def _pages_in(text: str) -> List[str]:
    """The document pages a request carries, in order, once each."""
    found = re.findall(r"\(document page (\d+)\)|PAGE-(\d+) ", text)
    return list(dict.fromkeys(a or b for a, b in found))


def _one_segment_per_page(page_count: int, doc_type: str = "") -> Dict[str, Any]:
    return {
        "segments": [
            {
                "ordinal_start_page": n,
                "ordinal_end_page": n,
                **({"type": doc_type} if doc_type else {}),
            }
            for n in range(1, page_count + 1)
        ]
    }


class _Calls:
    """A fake ``_invoke_bedrock_model`` that records each call's stage and first page.

    ``fail`` maps ``(stage, page_id)`` to the exception that call raises, keyed on
    the first page the request carries: the segment's for section classification,
    the page's for a page-granularity splitting call, the window's first for a
    packet window.
    """

    def __init__(self, fail: Dict[tuple, BaseException]):
        self.fail = fail
        self.calls: List[tuple] = []
        self._lock = threading.Lock()

    def __call__(self, content, config):
        stage = config.get("stage")
        pages = _pages_in(_text(content))
        first = pages[0] if pages else "?"
        with self._lock:
            self.calls.append((stage, first))
        if (stage, first) in self.fail:
            raise self.fail[(stage, first)]
        if stage == SPLITTING:
            if config["task_prompt"].startswith("Split this packet"):
                return _reply(_one_segment_per_page(len(pages)), "Splitting")
            return _reply({"document_boundary": "start"}, "Splitting")
        if stage == SECTION_CLASSIFICATION:
            return _reply({"class": "invoice"}, "SectionClassification")
        return _reply(_one_segment_per_page(len(pages), "invoice"), "Classification")

    def count(self, stage: str) -> int:
        return sum(1 for s, _ in self.calls if s == stage)


def _run(service, document, calls, page_texts=False):
    """Classify ``document``. ``page_texts`` gives each page text naming its page,
    which is how a page-granularity splitting call is attributed to its page."""
    with (
        patch(
            "idp_common.s3.get_text_content",
            side_effect=_page_texts() if page_texts else (lambda uri: "t"),
        ),
        patch("idp_common.image.prepare_image", return_value=_png()),
        patch.object(service, "_invoke_bedrock_model", side_effect=calls),
    ):
        return service.classify_document(document)


# --------------------------------------------------------------------------
# Section classification, LLM method
# --------------------------------------------------------------------------


class TestSectionClassification:
    def test_a_throttled_segment_raises_transient_error(self):
        throttle = _throttle()
        calls = _Calls({(SECTION_CLASSIFICATION, "2"): throttle})
        document = _document(3)

        with pytest.raises(TransientError) as caught:
            _run(_service(granularity="packet"), document, calls)

        assert caught.value.__cause__ is throttle
        assert "segment 2 (pages 2-2)" in str(caught.value)
        assert "ThrottlingException" in str(caught.value)
        # Raised, not recorded: no section error for ProcessResults to fail on.
        assert not any("Section classification failed" in e for e in document.errors)

    def test_a_read_timeout_is_transient_too(self):
        timeout = ReadTimeoutError(endpoint_url="https://bedrock-runtime")
        calls = _Calls({(SECTION_CLASSIFICATION, "1"): timeout})

        with pytest.raises(TransientError) as caught:
            _run(_service(granularity="packet"), _document(2), calls)

        assert caught.value.__cause__ is timeout

    def test_a_deterministic_segment_error_keeps_per_section_handling(self):
        """The control for the tests above: same shape, deterministic code."""
        invalid = _client_error("ValidationException", "The model rejected the input")
        calls = _Calls({(SECTION_CLASSIFICATION, "2"): invalid})

        result = _run(_service(granularity="packet"), _document(3), calls)

        assert [s.classification for s in result.sections] == [
            "invoice",
            UNCLASSIFIED_CLASS,
            "invoice",
        ]
        assert any(
            "Section classification failed for segment 2" in e for e in result.errors
        )
        issues = result.sections[1].processing_issues or []
        assert [i.code for i in issues] == ["classification_failed"]

    def test_the_calls_not_yet_started_are_cancelled(self):
        """The retry makes every call again, so the pool sends no more after a throttle.

        One worker, five segments. The throttled first call cancels the four
        queued behind it. The worker can dequeue the second before the main
        thread cancels, so that call waits until the throttle has been recorded,
        and the bound is "at most two calls" rather than five.
        """
        recorded = threading.Event()
        original_record = service_module._TransientCallFailure.record

        def record(self, where, exc):
            original_record(self, where, exc)
            recorded.set()

        calls = _Calls({(SECTION_CLASSIFICATION, "1"): _throttle()})

        def invoke(content, config):
            stage = config.get("stage")
            if stage == SECTION_CLASSIFICATION and _pages_in(_text(content)) != ["1"]:
                assert recorded.wait(5), "the throttle was never recorded"
            return calls(content, config)

        service = _service(granularity="packet")
        service.max_workers = 1
        with patch.object(service_module._TransientCallFailure, "record", record):
            with pytest.raises(TransientError):
                _run(service, _document(5), invoke)

        assert 1 <= calls.count(SECTION_CLASSIFICATION) <= 2, calls.calls


# --------------------------------------------------------------------------
# The splitting call
# --------------------------------------------------------------------------


def _windowed(**kw):
    """Windows over 7 pages at 3 pages with 1 overlapping: [1-3], [3-5], [5-7]."""
    return _service(
        granularity="packet",
        packet={
            "system_prompt": "packet system",
            "task_prompt": "packet {CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
            "window_pages": "3",
            "overlap_pages": "1",
        },
        **kw,
    )


class TestSplittingCall:
    def test_a_throttled_packet_window_raises_and_sends_no_further_window(self):
        """The issue's second symptom: "Packet window 1/1a (pages 1-9) failed"."""
        throttle = _throttle()
        calls = _Calls({(SPLITTING, "1"): throttle})
        document = _document(7)

        with pytest.raises(TransientError) as caught:
            _run(_windowed(), document, calls)

        assert caught.value.__cause__ is throttle
        assert "Packet window 1/3 (pages 1-3)" in str(caught.value)
        assert calls.count(SPLITTING) == 1
        assert calls.count(SECTION_CLASSIFICATION) == 0
        # The window was not recorded as a failed window for ProcessResults.
        assert not any(") failed: " in e for e in document.errors)

    def test_a_deterministic_window_error_keeps_the_other_windows(self):
        bad = ValueError("No segments found in the classification result")
        calls = _Calls({(SPLITTING, "1"): bad})

        result = _run(_windowed(), _document(7), calls)

        assert calls.count(SPLITTING) == 3
        assert any("Packet window 1/3 (pages 1-3) failed" in e for e in result.errors)

    def test_a_throttled_page_boundary_call_raises(self):
        throttle = _throttle()
        calls = _Calls({(SPLITTING, "2"): throttle})

        with pytest.raises(TransientError) as caught:
            _run(_service(granularity="page"), _document(3), calls, page_texts=True)

        assert caught.value.__cause__ is throttle
        assert "Splitting call for page 2" in str(caught.value)
        assert calls.count(SECTION_CLASSIFICATION) == 0

    def test_a_deterministic_page_boundary_error_is_still_recorded(self):
        calls = _Calls({(SPLITTING, "2"): RuntimeError("unparseable reply")})

        result = _run(
            _service(granularity="page"), _document(3), calls, page_texts=True
        )

        assert any("Splitting call failed for page 2" in e for e in result.errors)


class TestCombinedPacketClassification:
    """Holistic (combined, packet granularity) runs the same window loop.

    Measured on 30 to 60-page packets: "Processing failed for 1 out of 12 sections:
    Packet window 3/3 (pages 37-42) failed: An error occurred (ThrottlingException)
    when calling the Converse operation (reached max retries: 0): Too many tokens,
    please wait before trying again". The windows before it had succeeded.
    """

    @staticmethod
    def _too_many_tokens() -> ClientError:
        # The shape botocore builds: the retry count it reports is its own, and
        # BedrockClient pins botocore to one attempt, so it always reads zero.
        return ClientError(
            {
                "Error": {
                    "Code": "ThrottlingException",
                    "Message": "Too many tokens, please wait before trying again.",
                },
                "ResponseMetadata": {"MaxAttemptsReached": True, "RetryAttempts": 0},
            },
            "Converse",
        )

    def test_a_throttled_last_window_raises_after_the_others_succeeded(self):
        throttle = self._too_many_tokens()
        assert "(reached max retries: 0)" in str(throttle)
        # Windows over 7 pages: [1-3], [3-5], [5-7]. The third throttles.
        calls = _Calls({(ClassificationService.STAGE_CLASSIFICATION, "5"): throttle})
        document = _document(7)

        with pytest.raises(TransientError) as caught:
            _run(_windowed(strategy="combined"), document, calls)

        assert caught.value.__cause__ is throttle
        assert "Packet window 3/3 (pages 5-7)" in str(caught.value)
        assert calls.count(ClassificationService.STAGE_CLASSIFICATION) == 3
        assert not any(") failed: " in e for e in document.errors)

    def test_a_throttled_first_window_sends_no_further_window(self):
        throttle = _throttle()
        calls = _Calls({(ClassificationService.STAGE_CLASSIFICATION, "1"): throttle})

        with pytest.raises(TransientError) as caught:
            _run(_windowed(strategy="combined"), _document(7), calls)

        assert caught.value.__cause__ is throttle
        assert calls.count(ClassificationService.STAGE_CLASSIFICATION) == 1

    def test_a_deterministic_window_error_is_still_recorded(self):
        bad = ValueError("Every segment the model returned was unusable")
        calls = _Calls({(ClassificationService.STAGE_CLASSIFICATION, "5"): bad})

        result = _run(_windowed(strategy="combined"), _document(7), calls)

        assert calls.count(ClassificationService.STAGE_CLASSIFICATION) == 3
        assert any("Packet window 3/3 (pages 5-7) failed" in e for e in result.errors)


# --------------------------------------------------------------------------
# Section classification, decision-model method
# --------------------------------------------------------------------------


class _LambdaErrorDecider(FakeDecider):
    """A decision-model Lambda whose every invocation fails the same way: with a
    Lambda error ``code``, or by raising what ``fail`` builds."""

    def __init__(self, code: str = "", fail=None):
        super().__init__([])
        self.code = code
        self.fail = fail

    def invoke(self, FunctionName, InvocationType, Payload):
        self.requests.append(json.loads(Payload))
        if self.fail is not None:
            raise self.fail()
        raise _client_error(self.code, "Rate exceeded", "Invoke")


def _run_decision_model(decider, page_count=2):
    service = _decision_model_service()
    bedrock, calls = _split(*[(n, n) for n in range(1, page_count + 1)])
    with (
        patch("idp_common.s3.get_text_content", return_value="page text"),
        patch("idp_common.image.prepare_image", return_value=_png()),
        patch.object(service, "_invoke_bedrock_model", side_effect=bedrock),
        patch("idp_common.decision_model.invoker.boto3.client", return_value=decider),
        patch("idp_common.decision_model.invoker.time.sleep", lambda seconds: None),
    ):
        return service.split_then_classify_document(_document(page_count)), calls


class TestDecisionModelSectionClassification:
    def test_a_throttle_that_outlasts_the_client_retries_raises_transient_error(self):
        decider = _LambdaErrorDecider("TooManyRequestsException")

        with pytest.raises(TransientError) as caught:
            _run_decision_model(decider)

        # DecisionModelUnavailable, raised from the Lambda throttle it gave up on.
        assert caught.value.original_type == "DecisionModelUnavailable"
        unavailable = caught.value.__cause__
        assert isinstance(unavailable, DecisionModelUnavailable)
        assert isinstance(unavailable.__cause__, ClientError)
        assert "TooManyRequestsException" in str(caught.value)

    @pytest.mark.parametrize(
        "code",
        [
            "TooManyRequestsException",
            "EC2ThrottledException",
            # Retried by the client but outside the shared transient vocabulary.
            "ServiceException",
            "ResourceConflictException",
            "ResourceNotReadyException",
        ],
    )
    def test_every_code_the_client_retries_retries_the_step_once_exhausted(self, code):
        """The request never ran, so the step's retry re-runs no paid work."""
        decider = _LambdaErrorDecider(code)

        with pytest.raises(TransientError) as caught:
            _run_decision_model(decider, page_count=1)

        assert code in str(caught.value)
        assert caught.value.__cause__.retry_later is True
        assert len(decider.requests) == 5  # the client's own attempts, all used

    def test_a_connection_that_never_opened_retries_the_step_once_exhausted(self):
        decider = _LambdaErrorDecider(
            fail=lambda: ConnectTimeoutError(endpoint_url="https://lambda")
        )

        with pytest.raises(TransientError):
            _run_decision_model(decider, page_count=1)

    @pytest.mark.parametrize(
        "fail",
        [
            lambda: ReadTimeoutError(endpoint_url="https://lambda"),
            lambda: ConnectionClosedError(endpoint_url="https://lambda"),
        ],
        ids=["read-timeout", "connection-closed"],
    )
    def test_a_call_that_may_still_be_running_fails_only_its_section(self, fail):
        """The client does not retry these, because the invocation may still be
        running and billing, and a retry of the whole step would start it again."""
        decider = _LambdaErrorDecider(fail=fail)

        result, calls = _run_decision_model(decider, page_count=1)

        assert result.sections[0].classification == UNCLASSIFIED_CLASS
        assert any("Section classification failed" in e for e in result.errors)
        assert len(decider.requests) == 1
        assert calls == [SPLITTING]

    def test_an_environment_that_keeps_crashing_fails_only_its_section(self):
        decider = FakeDecider(
            [], error={"errorType": "Runtime.ExitError", "errorMessage": "exited"}
        )

        result, _ = _run_decision_model(decider, page_count=1)

        assert result.sections[0].classification == UNCLASSIFIED_CLASS
        assert len(decider.requests) == 5

    def test_a_deterministic_lambda_error_still_fails_only_its_section(self):
        """The control: access denied is not transient and is not retried."""
        result, calls = _run_decision_model(
            _LambdaErrorDecider("AccessDeniedException")
        )

        assert all(s.classification == UNCLASSIFIED_CLASS for s in result.sections)
        assert any("AccessDeniedException" in e for e in result.errors)
        assert calls == [SPLITTING]  # never converted into an LLM call

    def test_a_refusal_still_fails_its_section(self):
        decider = FakeDecider(
            [], error={"errorType": "ValueError", "errorMessage": "over the window"}
        )
        result, _ = _run_decision_model(decider, page_count=1)

        assert result.sections[0].classification == UNCLASSIFIED_CLASS
        assert any("over the window" in e for e in result.errors)
