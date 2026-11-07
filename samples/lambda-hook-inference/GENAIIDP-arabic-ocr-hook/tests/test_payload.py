import base64
import io

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from PIL import Image
from r5ocr.payload import parse_request
from r5ocr.settings import Settings

pytestmark = pytest.mark.unit


def _png(w=40, h=20):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def _event(image_block):
    return {
        "modelId": "LambdaHook",
        "system": [{"text": "SYS"}],
        "messages": [
            {"role": "user", "content": [{"text": "TASK"}, {"image": image_block}]}
        ],
        "inferenceConfig": {"temperature": 0.0},
        "context": "OCR",
    }


def test_inline_bytes_base64():
    ev = _event(
        {"format": "png", "source": {"bytes": base64.b64encode(_png()).decode()}}
    )
    req = parse_request(ev, s3_client=None)
    assert (
        req.system_prompt,
        req.task_prompt,
        req.image_format,
        req.width,
        req.height,
    ) == ("SYS", "TASK", "png", 40, 20)


def test_s3_location(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    s3 = boto3.client("s3", region_name="eu-central-1")
    stub = Stubber(s3)
    data = _png(10, 30)
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(data), len(data))},
        {"Bucket": "wb", "Key": "temp/lambdahook/x.png"},
    )
    image_block = {
        "format": "png",
        "source": {"s3Location": {"uri": "s3://wb/temp/lambdahook/x.png"}},
    }
    with stub:
        req = parse_request(_event(image_block), s3)
    stub.assert_no_pending_responses()
    assert (req.width, req.height) == (10, 30)


def test_missing_image_raises():
    ev = _event({"format": "png", "source": {}})
    with pytest.raises(ValueError, match="image"):
        parse_request(ev, s3_client=None)


def test_settings_defaults(monkeypatch):
    for name in (
        "READER_EFFORT",
        "READ_METHOD",
        "TEXTRACT_MAX_ATTEMPTS",
        "MAX_OUTPUT_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("READER_MODEL_ID", "eu.anthropic.claude-sonnet-5")
    s = Settings.from_env()
    assert (
        s.reader_effort,
        s.read_method,
        s.textract_max_attempts,
    ) == ("medium", "M0", 3)
    assert s.max_output_tokens == 128000


def test_read_method_lowercase_is_uppercased(monkeypatch):
    monkeypatch.setenv("READER_MODEL_ID", "eu.anthropic.claude-sonnet-5")
    monkeypatch.setenv("READ_METHOD", "m2")
    s = Settings.from_env()
    assert s.read_method == "M2"


def test_read_method_invalid_raises(monkeypatch):
    monkeypatch.setenv("READER_MODEL_ID", "eu.anthropic.claude-sonnet-5")
    monkeypatch.setenv("READ_METHOD", "X9")
    with pytest.raises(ValueError, match="READ_METHOD"):
        Settings.from_env()
