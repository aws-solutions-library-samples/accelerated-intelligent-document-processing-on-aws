import boto3
import pytest
from botocore.stub import ANY, Stubber
from r5ocr.llm import Usage, converse

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _dummy_aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def _resp(text):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {"inputTokens": 10, "outputTokens": 3, "totalTokens": 13},
        "stopReason": "end_turn",
        "metrics": {"latencyMs": 5},
    }


def test_converse_sends_effort_and_no_sampling_params():
    c = boto3.client("bedrock-runtime", region_name="eu-central-1")
    s = Stubber(c)
    s.add_response(
        "converse",
        _resp("hello"),
        {
            "modelId": "eu.anthropic.claude-sonnet-5",
            "system": [{"text": "S"}],
            "messages": [{"role": "user", "content": [{"text": "T"}]}],
            "additionalModelRequestFields": {
                "max_tokens": 100,
                "output_config": {"effort": "medium"},
            },
        },
    )
    with s:
        text, usage = converse(
            c, "eu.anthropic.claude-sonnet-5", "S", [{"text": "T"}], "medium", 100
        )
    s.assert_no_pending_responses()
    assert text == "hello"
    assert usage.as_dict() == {
        "inputTokens": 10,
        "outputTokens": 3,
        "totalTokens": 13,
    }


def test_converse_without_effort_omits_field():
    c = boto3.client("bedrock-runtime", region_name="eu-central-1")
    s = Stubber(c)
    s.add_response(
        "converse",
        _resp("x"),
        {
            "modelId": "m",
            "system": [{"text": "S"}],
            "messages": ANY,
            "inferenceConfig": {"maxTokens": 5},
        },
    )
    with s:
        converse(c, "m", "S", [{"text": "T"}], None, 5)
    s.assert_no_pending_responses()


def test_converse_anthropic_without_effort_sends_only_max_tokens():
    c = boto3.client("bedrock-runtime", region_name="eu-central-1")
    s = Stubber(c)
    s.add_response(
        "converse",
        _resp("x"),
        {
            "modelId": "eu.anthropic.claude-sonnet-5",
            "system": [{"text": "S"}],
            "messages": ANY,
            "additionalModelRequestFields": {"max_tokens": 5},
        },
    )
    with s:
        converse(c, "eu.anthropic.claude-sonnet-5", "S", [{"text": "T"}], None, 5)
    s.assert_no_pending_responses()


def test_usage_add():
    assert (Usage(1, 2) + Usage(3, 4)).as_dict()["totalTokens"] == 10
