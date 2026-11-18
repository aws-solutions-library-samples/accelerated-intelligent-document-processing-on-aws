# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An HTTP 500 on a packet classification call is retried, end to end (#2036).

Drives the real ``ClassificationService`` (combined call, packet granularity) and
the real ``BedrockClient`` behind it, with only the ``bedrock-runtime`` client's
replies stubbed. The ``"500"`` code with no error type is what botocore builds for a
reply that names no error, and before #2036 the client's retry ladder matched it
against names alone and failed the call, the window and the document on the first
attempt.

A botocore ``Stubber`` rather than moto: moto does not implement ``bedrock-runtime``.
It wraps the client ``BedrockClient`` builds itself, from the region and placeholder
credentials the suite's ``conftest.py`` sets, so the test creates no client of its own.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from botocore.stub import Stubber

from idp_common.bedrock import client as bedrock_client_module
from idp_common.bedrock.client import BedrockClient
from idp_common.classification.service import ClassificationService
from idp_common.models import Document, Page

MODEL = "us.amazon.nova-2-lite-v1:0"
METERING_KEY = f"Classification/bedrock/{MODEL}"

SEGMENTS = [
    {"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "invoice"},
    {"ordinal_start_page": 3, "ordinal_end_page": 4, "type": "receipt"},
]


def _service() -> ClassificationService:
    classes: List[Dict[str, Any]] = [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": name,
            "x-aws-idp-document-type": name,
            "type": "object",
            "description": f"A {name}",
            "properties": {},
        }
        for name in ("invoice", "receipt")
    ]
    return ClassificationService(
        region="us-west-2",
        config={
            "classes": classes,
            "classification": {
                "model": MODEL,
                "system_prompt": "You classify pages.",
                "task_prompt": "page prompt {DOCUMENT_TEXT}",
                "granularity": "packet",
                "confidence": {"mode": "off"},
                "packet": {
                    "system_prompt": "You split packets.",
                    "task_prompt": "Return a segments array. {DOCUMENT_TEXT}",
                    "window_pages": "0",
                },
            },
        },
    )


def _document() -> Document:
    return Document(
        id="doc-1",
        input_key="doc.pdf",
        pages={
            str(i): Page(page_id=str(i), parsed_text_uri=f"s3://b/{i}.txt")
            for i in range(1, 5)
        },
    )


def _converse_reply(text: str) -> Dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
        "metrics": {"latencyMs": 1},
    }


@pytest.mark.unit
class TestAServerErrorOnThePacketCall:
    def setup_method(self):
        self.calls: List[str] = []
        self.backoffs: List[int] = []
        self.metrics: Counter = Counter()

    def _stub_bedrock(self, monkeypatch) -> Stubber:
        """Stub the replies of the client the service reaches Bedrock through."""
        default = bedrock_client_module.default_client
        # A fresh client, built by BedrockClient and restored after the test.
        monkeypatch.setattr(default, "_client", None)
        runtime = default.client
        runtime.meta.events.register(
            "before-parameter-build.bedrock-runtime.Converse",
            lambda **_kwargs: self.calls.append("converse"),
        )
        monkeypatch.setattr(
            default, "_put_metric", lambda name, *a, **k: self.metrics.update([name])
        )
        monkeypatch.setattr(
            BedrockClient,
            "_backoff_within_invocation",
            lambda _self, retry_count: self.backoffs.append(retry_count) or 0.0,
        )
        return Stubber(runtime)

    def test_a_500_is_retried_and_the_document_classified(self, monkeypatch):
        with self._stub_bedrock(monkeypatch) as stubber:
            stubber.add_client_error(
                "converse",
                service_error_code="500",
                service_message="Internal Server Error",
                http_status_code=500,
            )
            stubber.add_response(
                "converse", _converse_reply(json.dumps({"segments": SEGMENTS}))
            )
            with patch("idp_common.s3.get_text_content", return_value="page text"):
                result = _service().packet_classify_document(_document())
            stubber.assert_no_pending_responses()

        assert result.errors == []
        assert [(s.page_ids, s.classification) for s in result.sections] == [
            (["1", "2"], "invoice"),
            (["3", "4"], "receipt"),
        ]
        assert len(self.calls) == 2, "the 500 and one retry"
        assert self.backoffs == [0], "the client's ladder retried, after a backoff"
        # The 500 returned no usage; the reply that succeeded is counted once.
        assert result.metering == {
            METERING_KEY: {
                "inputTokens": 100,
                "outputTokens": 20,
                "totalTokens": 120,
                "requests": 1,
            }
        }
        assert self.metrics["BedrockThrottles"] == 1
        assert self.metrics["BedrockRetrySuccess"] == 1
        assert self.metrics["BedrockNonRetryableErrors"] == 0
