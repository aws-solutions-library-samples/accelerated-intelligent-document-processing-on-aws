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
from botocore.exceptions import ReadTimeoutError

from idp_common.decision_model import (
    ChoiceQuestion,
    DecisionModelClient,
    DecisionModelRefused,
    DecisionModelUnavailable,
    NoulAnswer,
    NoulQuestion,
    SystemOneRequest,
    client_from_config,
    function_name,
    merge_metering,
    resolve_endpoint,
)
from tests.unit._botocore_errors import named_reply, service_error
from tests.unit.decision_model.window_contract import (  # noqa: F401 - autouse
    no_oversize_requests,
    window_refusal,
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


#: Lambda's model declares no ``AccessDeniedException`` for ``Invoke``; IAM answers
#: it with a 403. Every other code here takes the status the model declares.
_UNMODELLED_STATUS = {"AccessDeniedException": 403}


def _client_error(code):
    """``code`` as a real Lambda client raises it from ``Invoke`` (#2050)."""
    reply = named_reply(code, _UNMODELLED_STATUS.get(code), service="lambda")
    return service_error(reply, "Invoke", "lambda")


class FakeLambda:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        refusal = window_refusal(kwargs["Payload"])
        if refusal:
            return refusal
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


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(
        "idp_common.decision_model.invoker.time.sleep", lambda seconds: None
    )


def _client(fake, **kw):
    return DecisionModelClient(ARN, lambda_client=fake, metrics_enabled=False, **kw)


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


class StateKeyedLambda:
    """Answers each request with the outcome named by its state, so a result can
    be traced to its request whatever order worker threads send them in."""

    def __init__(self, outcomes):
        self.outcomes = outcomes

    def invoke(self, **kwargs):
        refusal = window_refusal(kwargs["Payload"])
        if refusal:
            return refusal
        outcome = self.outcomes[json.loads(kwargs["Payload"])["state"]]()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.parametrize("concurrency", [1, 3])
def test_decide_each_puts_each_failure_in_its_requests_slot(concurrency):
    """A caller can say WHICH request failed, and the others still answer."""
    fake = StateKeyedLambda(
        {
            "a": _ok,
            "b": lambda: _function_error(
                "ValueError", "prompt of 5000 tokens exceeds 4096"
            ),
            "c": lambda: _client_error("AccessDeniedException"),
        }
    )
    requests = [
        SystemOneRequest(state=s, questions={"ok": NoulQuestion(instructions="Ok?")})
        for s in ("a", "b", "c")
    ]
    results = _client(fake, max_concurrency=concurrency).decide_each(requests, "Ctx")

    assert isinstance(results[0].response.answers["ok"], NoulAnswer)
    assert isinstance(results[1], DecisionModelRefused)
    assert "5000 tokens" in str(results[1])
    assert isinstance(results[2], DecisionModelUnavailable)


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


def test_an_empty_endpoint_is_refused_by_name():
    for empty in ("", "  ", None):
        with pytest.raises(ValueError, match="decision_model.endpoint is empty"):
            resolve_endpoint(empty)
    assert resolve_endpoint(f"  {ARN} ") == ARN


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


def _metrics(client):
    seen = []
    client._put_metric = lambda name, value, unit="Count": seen.append(name)
    return seen


@pytest.mark.parametrize(
    "outcome",
    [
        _function_error("ValueError", "over the window"),
        _client_error("AccessDeniedException"),
        ReadTimeoutError(endpoint_url="x"),
        _ok({"output": "not system one"}),
    ],
    ids=["refused", "non-retryable", "read-timeout", "malformed"],
)
def test_every_failure_records_the_failure_metric_once(outcome):
    from idp_common.decision_model import DecisionModelError

    client = _client(FakeLambda([outcome]))
    seen = _metrics(client)
    with pytest.raises(DecisionModelError):
        client.decide(_request(), "Ctx")
    assert seen == ["DecisionModelRequestsFailed"]


def test_retry_exhaustion_records_the_failure_metric_once():
    client = _client(
        FakeLambda([_client_error("TooManyRequestsException")] * 2), max_retries=1
    )
    seen = _metrics(client)
    with pytest.raises(DecisionModelUnavailable):
        client.decide(_request(), "Ctx")
    assert seen == ["DecisionModelRequestsFailed"]


def test_a_connection_that_never_opened_is_retried():
    from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError

    fake = FakeLambda(
        [
            ConnectTimeoutError(endpoint_url="x"),
            EndpointConnectionError(endpoint_url="x"),
            _ok(),
        ]
    )
    _client(fake).decide(_request(), "Ctx")
    assert len(fake.calls) == 3


def test_a_closed_connection_is_unavailable_and_not_retried():
    from botocore.exceptions import ConnectionClosedError

    fake = FakeLambda([ConnectionClosedError(endpoint_url="x")])
    with pytest.raises(DecisionModelUnavailable, match="ConnectionClosedError"):
        _client(fake).decide(_request(), "Ctx")
    assert len(fake.calls) == 1


def test_an_empty_question_is_refused_at_the_contract():
    for empty in ("", "   ", {}, []):
        with pytest.raises(ValueError, match="must not be empty"):
            NoulQuestion(instructions=empty)
    with pytest.raises(ValueError, match="must not be empty"):
        ChoiceQuestion(instructions="", criteria={"a": None, "b": None})


def test_the_input_modes_are_the_configs():
    from idp_common.config.models import DECISION_MODEL_INPUTS
    from idp_common.decision_model import INPUT_MODES

    assert INPUT_MODES is DECISION_MODEL_INPUTS


# A failure that outlasts the retries keeps what made it retryable as its
# ``__cause__``, so ``is_transient_error`` can tell a throttle from a refusal and the
# classification step is retried rather than the section failed (#2034).


def test_a_throttle_past_the_retries_is_transient():
    from idp_common.utils.transient_errors import is_transient_error

    last = _client_error("TooManyRequestsException")
    fake = FakeLambda([_client_error("TooManyRequestsException"), last])
    with pytest.raises(DecisionModelUnavailable) as caught:
        _client(fake, max_retries=1).decide(_request(), "Ctx")
    assert caught.value.__cause__ is last
    assert is_transient_error(caught.value)


def test_a_connection_that_never_opened_past_the_retries_is_transient():
    from botocore.exceptions import ConnectTimeoutError

    from idp_common.utils.transient_errors import is_transient_error

    fake = FakeLambda([ConnectTimeoutError(endpoint_url="x")] * 2)
    with pytest.raises(DecisionModelUnavailable) as caught:
        _client(fake, max_retries=1).decide(_request(), "Ctx")
    assert isinstance(caught.value.__cause__, ConnectTimeoutError)
    assert is_transient_error(caught.value)


def test_no_time_left_for_a_throttle_retry_is_transient(monkeypatch):
    from idp_common.utils.transient_errors import is_transient_error

    monkeypatch.setattr(
        "idp_common.decision_model.invoker.attempt_cannot_finish", lambda *a: True
    )
    fake = FakeLambda([_client_error("TooManyRequestsException")])
    with pytest.raises(DecisionModelUnavailable, match="no time left") as caught:
        _client(fake).decide(_request(), "Ctx")
    assert is_transient_error(caught.value)


def test_a_crashed_environment_past_the_retries_is_not_transient():
    """The control: a function error carries no exception, so nothing is chained, and
    an environment that keeps crashing on one request is not retried as a step."""
    from idp_common.utils.transient_errors import is_transient_error

    fake = FakeLambda(
        [_function_error("Runtime.ExitError", "exited") for _ in range(2)]
    )
    with pytest.raises(DecisionModelUnavailable) as caught:
        _client(fake, max_retries=1).decide(_request(), "Ctx")
    assert caught.value.__cause__ is None
    assert not is_transient_error(caught.value)


@pytest.mark.parametrize(
    "outcome",
    [
        "TooManyRequestsException",
        "ServiceException",
        "EC2ThrottledException",
        "ResourceConflictException",
        "ResourceNotReadyException",
        "connect-timeout",
    ],
)
def test_a_request_that_never_ran_is_retry_later_once_retries_run_out(outcome):
    """#2034: the caller may retry its whole step, because nothing ran."""
    from botocore.exceptions import ConnectTimeoutError

    def fail():
        if outcome == "connect-timeout":
            return ConnectTimeoutError(endpoint_url="x")
        return _client_error(outcome)

    fake = FakeLambda([fail() for _ in range(2)])
    with pytest.raises(DecisionModelUnavailable) as caught:
        _client(fake, max_retries=1).decide(_request(), "Ctx")
    assert caught.value.retry_later is True


@pytest.mark.parametrize(
    "outcomes",
    [
        [_function_error("Runtime.ExitError", "exited") for _ in range(2)],
        [ReadTimeoutError(endpoint_url="x")],
        [_client_error("AccessDeniedException")],
        [_ok({"output": "not system one"})],
    ],
    ids=["crashed-environment", "read-timeout", "non-retryable", "malformed"],
)
def test_a_call_that_may_have_run_is_never_retry_later(outcomes):
    """The invocation may have run, or may still be running and billing."""
    fake = FakeLambda(outcomes)
    with pytest.raises(DecisionModelUnavailable) as caught:
        _client(fake, max_retries=1).decide(_request(), "Ctx")
    assert caught.value.retry_later is False


def test_a_closed_connection_is_never_retry_later():
    from botocore.exceptions import ConnectionClosedError

    fake = FakeLambda([ConnectionClosedError(endpoint_url="x")])
    with pytest.raises(DecisionModelUnavailable) as caught:
        _client(fake).decide(_request(), "Ctx")
    assert caught.value.retry_later is False
