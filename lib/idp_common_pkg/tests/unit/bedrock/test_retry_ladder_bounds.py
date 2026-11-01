# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the retry ladders in ``idp_common.bedrock.client`` retry, and how often (#1706).

**A dropped connection is retried like a read timeout.** The exception is not built
here: a real ``bedrock-runtime`` client is driven with only urllib3's ``urlopen``
replaced, so botocore's own ``httpsession.send`` turns urllib3's ``ProtocolError``
into whatever it raises for a closed connection, and the test asserts that the
ladder retries *that*. A botocore upgrade that changed the class would then be
measured rather than assumed.
"""

from __future__ import annotations

import io
import json
from collections import Counter
from unittest.mock import patch

import boto3
import pytest
import urllib3
from botocore.config import Config
from botocore.httpsession import URLLib3Session
from urllib3.exceptions import ProtocolError

from idp_common.bedrock.client import BedrockClient
from idp_common.lambda_deadline import set_lambda_deadline_epoch

#: botocore's real transport, captured at import — before the suite's autouse
#: no-outbound fixture replaces it for each test. The test below needs botocore's own
#: ``send``, because that is where a urllib3 failure is rewrapped into botocore's
#: exception; it puts the real ``send`` back only while urllib3's ``urlopen`` is
#: replaced, so nothing can leave the process.
_REAL_SEND = URLLib3Session.send

_CONVERSE_OK = {
    "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    "metrics": {"latencyMs": 1},
}


@pytest.fixture(autouse=True)
def _no_sleep_no_deadline():
    set_lambda_deadline_epoch(None)
    with patch.object(BedrockClient, "_backoff_within_invocation", lambda self, n: 0.0):
        yield
    set_lambda_deadline_epoch(None)


def _recording_client(metrics: Counter) -> BedrockClient:
    client = BedrockClient(region="us-east-1")
    client._put_metric = lambda name, *a, **k: metrics.update([name])  # type: ignore[method-assign]
    return client


def _botocore_runtime_client():
    """A real client: real serialisation, real error wrapping, one botocore attempt
    (the setting ``BedrockClient.client`` uses, so this ladder is the only one)."""
    return boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        config=Config(retries={"total_max_attempts": 1, "mode": "standard"}),
    )


@pytest.mark.unit
@pytest.mark.parametrize("drops", [1, 3])
def test_a_dropped_connection_is_retried_like_a_read_timeout(drops, monkeypatch):
    sent: list[str] = []
    raised: list[BaseException] = []

    def urlopen(pool, method, url, *args, **kwargs):
        sent.append(url)
        if len(sent) <= drops:
            raise ProtocolError("Connection aborted.", ConnectionResetError(104))
        return urllib3.HTTPResponse(
            body=io.BytesIO(json.dumps(_CONVERSE_OK).encode()),
            status=200,
            headers={"content-type": "application/json"},
            preload_content=False,
        )

    runtime = _botocore_runtime_client()

    def record_failure(caught_exception=None, **_kwargs):
        if caught_exception is not None:
            raised.append(caught_exception)

    # botocore announces every attempt's outcome on this event, so what the ladder
    # was handed is read from botocore rather than inferred from the stub.
    runtime.meta.events.register("needs-retry.bedrock-runtime.Converse", record_failure)

    metrics: Counter = Counter()
    client = _recording_client(metrics)
    client._client = runtime
    monkeypatch.setattr(urllib3.connectionpool.HTTPConnectionPool, "urlopen", urlopen)
    monkeypatch.setattr(URLLib3Session, "send", _REAL_SEND)
    result = client._invoke_with_retry(
        model_id="us.amazon.nova-lite-v1:0",
        converse_params={
            "modelId": "us.amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": [{"text": "hi"}]}],
            "system": [{"text": "s"}],
            "inferenceConfig": {"maxTokens": 10},
            "additionalModelRequestFields": {},
        },
        retry_count=0,
        max_retries=4,
        request_start_time=0.0,
    )

    # The failure botocore produced is the one botocore names for a closed connection.
    assert [type(e).__name__ for e in raised] == ["ConnectionClosedError"] * drops
    assert len(sent) == drops + 1, (
        f"{len(sent)} sends for {drops} dropped connection(s)"
    )
    assert result["response"]["stopReason"] == "end_turn"
    assert metrics["BedrockTimeouts"] == drops
    assert metrics["BedrockUnexpectedErrors"] == 0
    assert metrics["BedrockRequestsFailed"] == 0
