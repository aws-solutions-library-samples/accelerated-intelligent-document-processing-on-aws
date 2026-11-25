# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A throttle burst driven through the real classification and Bedrock stack (#2034).

The other #2034 tests replace ``_invoke_bedrock_model``. These do not: the real
``ClassificationService`` calls the real ``BedrockClient``, whose retry ladder talks to
a real botocore ``bedrock-runtime`` client. Only urllib3's ``urlopen`` is replaced, so
botocore parses each scripted HTTP 429 into the same ``ThrottlingException`` a
tokens-per-minute quota produces in production ("Too many tokens, please wait before
trying again", with botocore's "(reached max retries: 0)"), and nothing leaves the
process. The ladder's sleeps are patched out.

Each scenario runs the document twice, the way the state machine does:

1. A burst throttles every call twice, which the client's ladder absorbs, and one
   call is throttled past the ladder's seven retries. That call's failure must reach
   the classification step's boundary as ``TransientError``, the name
   ``ClassificationStep`` retries. The old code recorded it on a section, returned
   the document and let ProcessResults fail it.
2. The retry classifies a fresh copy of the document, as the step's retry reloads the
   OCR output, with the same burst and no call past the ladder. It completes with the
   right sections, and its metering counts each answered call exactly once: throttled
   attempts are not metered, and nothing from the aborted attempt carries over.
"""

from __future__ import annotations

import io
import json
import re
import threading
from collections import Counter
from typing import Callable, Dict, List, Optional
from unittest.mock import patch

import boto3
import pytest
import urllib3
from botocore.config import Config
from botocore.httpsession import URLLib3Session

from idp_common import bedrock
from idp_common.bedrock.client import DEFAULT_MAX_RETRIES, BedrockClient
from idp_common.lambda_deadline import set_lambda_deadline_epoch
from idp_common.utils.transient_errors import TransientError
from tests.unit.classification.test_split_then_classify import (
    _document,
    _png,
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


#: botocore's real transport, captured at import, before the suite's autouse
#: no-outbound fixture replaces it for each test. It is put back only while urllib3's
#: ``urlopen`` is replaced, so nothing can leave the process.
_REAL_SEND = URLLib3Session.send

MODEL = "us.amazon.nova-2-lite-v1:0"
TOO_MANY_TOKENS = "Too many tokens, please wait before trying again."

#: Every call is throttled this many times before it is answered: a burst the
#: client's ladder absorbs.
BURST = 2
#: Throttles that outlast the ladder: one attempt plus every retry it allows.
PAST_THE_LADDER = DEFAULT_MAX_RETRIES + 1


class _FakeBedrockRuntime:
    """Scripted HTTP answers for ``bedrock-runtime`` Converse, keyed per call.

    A call is identified by the system prompt its stage sends and the document pages
    its request carries. ``throttles(key)`` says how many 429s that call gets before
    its answer. Thread-safe, because section classification calls arrive from a pool.
    """

    def __init__(
        self,
        answer: Callable[[str, List[str]], dict],
        throttles: Callable[[tuple], int],
    ):
        self.answer = answer
        self.throttles = throttles
        self.sent: Counter = Counter()
        self.answered: Counter = Counter()
        self._lock = threading.Lock()

    def urlopen(self, pool, method, url, body=None, *args, **kwargs):
        request = json.loads(body)
        system = "".join(b.get("text", "") for b in request.get("system", []))
        text = "".join(
            block.get("text", "")
            for message in request["messages"]
            for block in message["content"]
        )
        pages = list(dict.fromkeys(re.findall(r"\(document page (\d+)\)", text)))
        key = (system, pages[0] if pages else "?")
        with self._lock:
            self.sent[key] += 1
            throttled = self.sent[key] <= self.throttles(key)
            if not throttled:
                self.answered[key] += 1
        if throttled:
            return urllib3.HTTPResponse(
                body=io.BytesIO(json.dumps({"message": TOO_MANY_TOKENS}).encode()),
                status=429,
                headers={
                    "content-type": "application/json",
                    "x-amzn-ErrorType": "ThrottlingException:http://internal.amazon.com/",
                },
                preload_content=False,
            )
        reply = {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": json.dumps(self.answer(system, pages))}],
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110},
            "metrics": {"latencyMs": 1},
        }
        return urllib3.HTTPResponse(
            body=io.BytesIO(json.dumps(reply).encode()),
            status=200,
            headers={"content-type": "application/json"},
            preload_content=False,
        )


def _runtime_client():
    """A real client, pinned to one botocore attempt as ``BedrockClient.client`` is."""
    return boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="AKIA" + "IOSFODNN7EXAMPLE",
        aws_secret_access_key="wJalrXUtnFEMI/K7MDENG" + "/bPxRfiCYEXAMPLEKEY",
        config=Config(retries={"total_max_attempts": 1, "mode": "standard"}),
    )


def _classify(service, page_count: int, fake: _FakeBedrockRuntime, monkeypatch):
    """Classify a fresh document through the real Bedrock client and ``fake``."""

    def urlopen(pool, method, url, body=None, *args, **kwargs):
        return fake.urlopen(pool, method, url, body, *args, **kwargs)

    monkeypatch.setattr(urllib3.connectionpool.HTTPConnectionPool, "urlopen", urlopen)
    monkeypatch.setattr(URLLib3Session, "send", _REAL_SEND)
    with (
        patch.object(bedrock.default_client, "_client", _runtime_client()),
        patch.object(BedrockClient, "_backoff_within_invocation", lambda s, n: 0.0),
        patch.object(BedrockClient, "_put_metric", lambda s, *a, **k: None),
        patch("idp_common.s3.get_text_content", return_value="page text"),
        patch("idp_common.image.prepare_image", return_value=_png()),
    ):
        return service.classify_document(_document(page_count))


def _requests(metering: Dict[str, Dict[str, float]], context: str) -> float:
    return metering[f"{context}/bedrock/{MODEL}"]["requests"]


def _input_tokens(metering: Dict[str, Dict[str, float]], context: str) -> float:
    return metering[f"{context}/bedrock/{MODEL}"]["inputTokens"]


class TestSplitThenClassifySectionPool:
    """Three two-page segments, labeled concurrently by section classification."""

    LABELS = {"1": "invoice", "3": "receipt", "5": "invoice"}

    def _answer(self, system: str, pages: List[str]) -> dict:
        if system.startswith("splitting packet system"):
            return {
                "segments": [
                    {"ordinal_start_page": a, "ordinal_end_page": a + 1}
                    for a in (1, 3, 5)
                ]
            }
        assert system.startswith("section classification system"), system
        return {"class": self.LABELS[pages[0]]}

    @staticmethod
    def _throttles(past_the_ladder: Optional[str]):
        def throttles(key: tuple) -> int:
            system, first_page = key
            if system.startswith("splitting"):
                return BURST
            return PAST_THE_LADDER if first_page == past_the_ladder else BURST

        return throttles

    def test_a_throttle_past_the_ladder_retries_the_step_and_the_retry_completes(
        self, monkeypatch
    ):
        service = _service(granularity="packet", labeling={"pages": "all"})

        # Attempt 1: segment 2 (pages 3-4) is throttled past the ladder.
        first = _FakeBedrockRuntime(self._answer, self._throttles(past_the_ladder="3"))
        with pytest.raises(TransientError) as caught:
            _classify(service, 6, first, monkeypatch)
        assert "segment 2 (pages 3-4)" in str(caught.value)
        assert "(reached max retries: 0)" in str(caught.value)
        assert TOO_MANY_TOKENS in str(caught.value)
        # The throttled call used every attempt the ladder allows. Every other call
        # that was sent absorbed its burst and was answered. A segment the pool had
        # not started when the throttle was recorded is cancelled and never sent,
        # because the step's retry makes it anyway, so it is not counted here.
        throttled = ("section classification system", "3")
        assert first.sent[throttled] == PAST_THE_LADDER
        assert first.answered[throttled] == 0
        others = {k: n for k, n in first.sent.items() if k != throttled}
        assert ("splitting packet system", "1") in others
        assert all(n == BURST + 1 for n in others.values()), first.sent
        assert all(first.answered[k] == 1 for k in others), first.answered

        # Attempt 2, the step's retry: a fresh document, the burst and nothing more.
        second = _FakeBedrockRuntime(
            self._answer, self._throttles(past_the_ladder=None)
        )
        result = _classify(service, 6, second, monkeypatch)

        assert [(s.page_ids, s.classification) for s in result.sections] == [
            (["1", "2"], "invoice"),
            (["3", "4"], "receipt"),
            (["5", "6"], "invoice"),
        ]
        assert not result.errors
        assert all(n == BURST + 1 for n in second.sent.values()), second.sent
        # One metered request per answered call; throttled attempts are not metered,
        # and the aborted attempt's calls are not carried into this document.
        assert _requests(result.metering, "Splitting") == 1
        assert _requests(result.metering, "SectionClassification") == 3
        assert _input_tokens(result.metering, "SectionClassification") == 300
        assert sum(second.answered.values()) == 4


class TestHolisticPacketWindows:
    """Combined packet classification over seven pages in windows of three with one
    overlapping: [1-3], [3-5], [5-7], sent one after another."""

    TRUTH = {
        "1": "invoice",
        "2": "invoice",
        "3": "receipt",
        "4": "receipt",
        "5": "receipt",
        "6": "invoice",
        "7": "invoice",
    }

    def _answer(self, system: str, pages: List[str]) -> dict:
        assert system.startswith("packet system"), system
        segments: List[dict] = []
        for ordinal, page_id in enumerate(pages, start=1):
            doc_class = self.TRUTH[page_id]
            if segments and segments[-1]["type"] == doc_class:
                segments[-1]["ordinal_end_page"] = ordinal
            else:
                segments.append(
                    {
                        "ordinal_start_page": ordinal,
                        "ordinal_end_page": ordinal,
                        "type": doc_class,
                    }
                )
        return {"segments": segments}

    @staticmethod
    def _throttles(past_the_ladder: Optional[str]):
        def throttles(key: tuple) -> int:
            return PAST_THE_LADDER if key[1] == past_the_ladder else BURST

        return throttles

    def _service(self):
        return _service(
            granularity="packet",
            strategy="combined",
            packet={
                "system_prompt": "packet system",
                "task_prompt": "packet {CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
                "window_pages": "3",
                "overlap_pages": "1",
            },
        )

    def test_a_last_window_throttled_past_the_ladder_retries_the_step(
        self, monkeypatch
    ):
        """The measured shape: "Packet window 3/3 (pages 37-42) failed: ... (reached
        max retries: 0): Too many tokens", after the earlier windows succeeded."""
        service = self._service()

        first = _FakeBedrockRuntime(self._answer, self._throttles(past_the_ladder="5"))
        with pytest.raises(TransientError) as caught:
            _classify(service, 7, first, monkeypatch)
        assert "Packet window 3/3 (pages 5-7)" in str(caught.value)
        assert "(reached max retries: 0)" in str(caught.value)
        assert first.answered == Counter(
            {("packet system", "1"): 1, ("packet system", "3"): 1}
        )
        assert first.sent[("packet system", "5")] == PAST_THE_LADDER

        second = _FakeBedrockRuntime(
            self._answer, self._throttles(past_the_ladder=None)
        )
        result = _classify(service, 7, second, monkeypatch)

        assert [(s.page_ids, s.classification) for s in result.sections] == [
            (["1", "2"], "invoice"),
            (["3", "4", "5"], "receipt"),
            (["6", "7"], "invoice"),
        ]
        assert not result.errors
        assert all(n == BURST + 1 for n in second.sent.values()), second.sent
        assert _requests(result.metering, "Classification") == 3
        assert _input_tokens(result.metering, "Classification") == 300
