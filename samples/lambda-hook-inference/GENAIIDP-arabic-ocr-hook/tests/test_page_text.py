import boto3
import pytest
from botocore.stub import ANY, Stubber
from r5ocr.page_text import transcribe_page
from r5ocr.payload import HookRequest
from r5ocr.settings import Settings

pytestmark = pytest.mark.unit

S = Settings("eu.anthropic.claude-sonnet-5", "medium", "M1", 60, 8, 128000)


@pytest.fixture(autouse=True)
def _dummy_aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def test_transcribe_page_uses_idp_prompts_and_image():
    c = boto3.client("bedrock-runtime", region_name="eu-central-1")
    s = Stubber(c)
    s.add_response(
        "converse",
        {
            "output": {"message": {"role": "assistant", "content": [{"text": "نص"}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            "stopReason": "end_turn",
            "metrics": {"latencyMs": 1},
        },
        {
            "modelId": S.reader_model_id,
            "system": [{"text": "SYS"}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"text": "TASK"},
                        {"image": {"format": "png", "source": {"bytes": ANY}}},
                    ],
                }
            ],
            "additionalModelRequestFields": {
                "max_tokens": 128000,
                "output_config": {"effort": "medium"},
            },
        },
    )
    with s:
        text, _ = transcribe_page(
            c, S, HookRequest("SYS", "TASK", b"\x89PNG", "png", 1, 1)
        )
    s.assert_no_pending_responses()
    assert text == "نص"
