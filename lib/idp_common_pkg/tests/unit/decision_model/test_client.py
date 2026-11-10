# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The decision-model client: the System One contract over Lambda.

The two failure kinds are what callers branch on, so each path that produces one
is pinned here: a refusal (deterministic, never retried) and an unavailable call
(retried only when the request never ran or the environment crashed).
"""

from __future__ import annotations

import io
import json

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from idp_common.decision_model import (
    ChoiceQuestion,
    DecisionModelClient,
    DecisionModelRefused,
    DecisionModelUnavailable,
    NoulQuestion,
    SystemOneRequest,
    client_from_config,
    function_name,
    merge_metering,
    resolve_endpoint,
)

pytestmark = pytest.mark.unit

ARN = "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-decider:live"

ANSWER = {
    "model": "strands-decider-2B-qwen3.5-v1-2610-q8_0",
    "answers": {
        "kind": {
            "type": "choice",
            "choice": "invoice",
            "probabilities": {"invoice": 0.9, "letter": 0.1},
            "confidence": 0.8,
        },
        "ok": {"type": "noul", "noul": 0.7},
    },
    "usage": {"input_tokens": 300, "output_tokens": 2},
    "latency_ms": 1500.0,
}


def _ok(body=ANSWER):
    return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(body).encode())}


def _function_error(error_type, message):
    return {
        "StatusCode": 200,
        "FunctionError": "Unhandled",
        "Payload": io.BytesIO(
            json.dumps({"errorType": error_type, "errorMessage": message}).encode()
        ),
    }


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "Invoke")


class FakeLambda:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _request():
    return SystemOneRequest(
        state="An invoice.",
        questions={
            "kind": ChoiceQuestion(
                instructions="Which type?", criteria={"invoice": None, "letter": None}
            ),
            "ok": NoulQuestion(instructions="Is it legible?"),
        },
    )


def _client(fake, **kw):
    return DecisionModelClient(ARN, lambda_client=fake, sleep=lambda s: None, **kw)


def test_answers_and_metering():
    fake = FakeLambda([_ok()])
    result = _client(fake, function_memory_mb=10240).decide(_request(), "Ctx")
    assert result.response.answers["kind"].choice == "invoice"
    assert result.response.answers["ok"].noul == 0.7
    sent = json.loads(fake.calls[0]["Payload"])
    assert sent["questions"]["kind"]["criteria"] == {"invoice": None, "letter": None}
    assert fake.calls[0]["FunctionName"] == ARN
    units = result.metering["Ctx/decision_model/GENAIIDP-decider"]
    assert units["requests"] == 1
    assert set(units) == {"requests", "gb_seconds"}
    assert units["gb_seconds"] >= 0


def test_over_window_is_refused_and_not_retried():
    fake = FakeLambda(
        [_function_error("ValueError", "prompt of 5000 tokens exceeds 4096")]
    )
    with pytest.raises(DecisionModelRefused, match="5000 tokens"):
        _client(fake).decide(_request(), "Ctx")
    assert len(fake.calls) == 1


def test_throttle_is_retried_then_answers():
    fake = FakeLambda([_client_error("TooManyRequestsException"), _ok()])
    result = _client(fake).decide(_request(), "Ctx")
    assert result.response.model.startswith("strands-decider")
    assert len(fake.calls) == 2


def test_throttle_retries_are_bounded():
    fake = FakeLambda([_client_error("TooManyRequestsException")] * 3)
    with pytest.raises(DecisionModelUnavailable, match="3 attempts"):
        _client(fake, max_retries=2).decide(_request(), "Ctx")
    assert len(fake.calls) == 3


def test_crashed_environment_is_retried():
    fake = FakeLambda([_function_error("Runtime.ExitError", "exited"), _ok()])
    _client(fake).decide(_request(), "Ctx")
    assert len(fake.calls) == 2


def test_read_timeout_is_not_retried():
    fake = FakeLambda([ReadTimeoutError(endpoint_url="x")])
    with pytest.raises(DecisionModelUnavailable, match="timeout_ms"):
        _client(fake).decide(_request(), "Ctx")
    assert len(fake.calls) == 1


def test_access_denied_is_unavailable_and_not_retried():
    fake = FakeLambda([_client_error("AccessDeniedException")])
    with pytest.raises(DecisionModelUnavailable, match="AccessDenied"):
        _client(fake).decide(_request(), "Ctx")
    assert len(fake.calls) == 1


def test_a_response_that_is_not_system_one_is_unavailable():
    fake = FakeLambda([_ok({"output": "hello"})])
    with pytest.raises(DecisionModelUnavailable, match="not a System One"):
        _client(fake).decide(_request(), "Ctx")


def test_decide_many_keeps_order_and_merges_metering():
    fake = FakeLambda([_ok(), _ok(), _ok()])
    results = _client(fake, max_concurrency=2).decide_many([_request()] * 3, "Ctx")
    assert len(results) == 3
    merged = merge_metering(results)
    assert merged["Ctx/decision_model/GENAIIDP-decider"]["requests"] == 3


def test_function_name_forms():
    assert function_name(ARN) == "GENAIIDP-decider"
    assert function_name("GENAIIDP-decider:live") == "GENAIIDP-decider"
    assert function_name("GENAIIDP-decider") == "GENAIIDP-decider"


def test_endpoint_must_carry_the_prefix():
    with pytest.raises(ValueError, match="GENAIIDP-"):
        DecisionModelClient(
            "arn:aws:lambda:us-east-1:1:function:decider", lambda_client=object()
        )


def test_unimplemented_backend_is_refused():
    with pytest.raises(ValueError, match="sagemaker"):
        DecisionModelClient(ARN, backend="sagemaker", lambda_client=object())


def test_endpoint_falls_back_to_the_stacks_own(monkeypatch):
    monkeypatch.setenv("DECISION_MODEL_FUNCTION_ARN", ARN)
    assert resolve_endpoint("") == ARN
    monkeypatch.delenv("DECISION_MODEL_FUNCTION_ARN")
    with pytest.raises(ValueError, match="no decision model endpoint"):
        resolve_endpoint(None)


def test_client_from_config_reads_the_shared_block(monkeypatch):
    from idp_common.config.models import DecisionModelConfig

    cfg = DecisionModelConfig(
        backend="lambda", endpoint=ARN, timeout_ms=60000, max_concurrency=3
    )
    client = client_from_config(cfg, region="us-east-1")
    assert client.function_name == "GENAIIDP-decider"
    assert client.timeout_seconds == 60
    assert client.max_concurrency == 3


def test_request_bounds():
    with pytest.raises(ValueError):
        SystemOneRequest(state="s", questions={}, images=[])
    with pytest.raises(ValueError, match="at most 4 images"):
        SystemOneRequest(
            state="s",
            questions={"q": NoulQuestion(instructions="?")},
            images=["a"] * 5,
        )
    with pytest.raises(ValueError, match="at least 2"):
        ChoiceQuestion(instructions="?", criteria={"only": None})
