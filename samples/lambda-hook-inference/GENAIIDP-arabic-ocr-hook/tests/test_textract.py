import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
from r5ocr.detectors.textract import (
    TextractUnavailable,
    detect,
    make_client,
    split_line_on_gaps,
)
from r5ocr.geometry import Box

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _dummy_aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def _word(i, left, width, top=0.1, h=0.02, text="x"):
    return {
        "BlockType": "WORD",
        "Id": f"w{i}",
        "Text": text,
        "Geometry": {
            "BoundingBox": {"Left": left, "Top": top, "Width": width, "Height": h}
        },
    }


def test_split_line_on_gaps_separates_segments():
    line = Box(0, 0, 100, 10, "textract")
    words = [
        Box(0, 0, 10, 10, "t", "a"),
        Box(12, 0, 25, 10, "t", "b"),
        Box(70, 0, 90, 10, "t", "c"),
    ]
    segs = split_line_on_gaps(line, words)
    assert [(s.x0, s.x1) for s in segs] == [(0, 25), (70, 90)]
    assert [s.text for s in segs] == ["a b", "c"]


def test_detect_returns_pixel_boxes_per_segment():
    c = boto3.client("textract", region_name="eu-central-1")
    s = Stubber(c)
    line = {
        "BlockType": "LINE",
        "Id": "l1",
        "Text": "a b",
        "Geometry": {
            "BoundingBox": {"Left": 0.1, "Top": 0.1, "Width": 0.8, "Height": 0.02}
        },
        "Relationships": [{"Type": "CHILD", "Ids": ["w1", "w2"]}],
    }
    s.add_response(
        "detect_document_text",
        {"Blocks": [line, _word(1, 0.1, 0.1, text="a"), _word(2, 0.7, 0.2, text="b")]},
        {"Document": {"Bytes": b"img"}},
    )
    with s:
        boxes = detect(c, b"img", 1000, 1000)
    s.assert_no_pending_responses()
    assert [(b.x0, b.x1) for b in boxes] == [
        pytest.approx((100.0, 200.0)),
        pytest.approx((700.0, 900.0)),
    ]
    assert all(b.source == "textract" for b in boxes)
    assert [b.text for b in boxes] == ["a", "b"]


def test_detect_line_without_children_keeps_line_text():
    c = boto3.client("textract", region_name="eu-central-1")
    s = Stubber(c)
    line = {
        "BlockType": "LINE",
        "Id": "l1",
        "Text": "no children here",
        "Geometry": {
            "BoundingBox": {"Left": 0.1, "Top": 0.1, "Width": 0.8, "Height": 0.02}
        },
    }
    s.add_response(
        "detect_document_text",
        {"Blocks": [line]},
        {"Document": {"Bytes": b"img"}},
    )
    with s:
        boxes = detect(c, b"img", 1000, 1000)
    s.assert_no_pending_responses()
    assert [b.text for b in boxes] == ["no children here"]


def test_detect_throttling_raises_unavailable():
    c = boto3.client("textract", region_name="eu-central-1")
    s = Stubber(c)
    s.add_client_error("detect_document_text", "ProvisionedThroughputExceededException")
    with s, pytest.raises(TextractUnavailable):
        detect(c, b"img", 10, 10)
    s.assert_no_pending_responses()


def test_detect_network_error_raises_unavailable():
    class _FakeClient:
        def detect_document_text(self, **kwargs):
            raise EndpointConnectionError(
                endpoint_url="https://textract.eu-central-1.amazonaws.com"
            )

    with pytest.raises(TextractUnavailable, match="EndpointConnectionError"):
        detect(_FakeClient(), b"img", 10, 10)


def test_make_client_is_bounded():
    """tight timeouts and at most 3 adaptive attempts, so the worst case
    (3 x 40 s + backoff) fits the handler's 130 s Textract budget."""
    cfg = make_client(3).meta.config
    assert (cfg.connect_timeout, cfg.read_timeout) == (10, 30)
    assert cfg.retries["mode"] == "adaptive"
    assert cfg.retries["total_max_attempts"] == 3


def test_make_client_clamps_attempts_to_three():
    assert make_client(8).meta.config.retries["total_max_attempts"] == 3


def test_unavailable_reason_is_the_bare_error_code():
    c = boto3.client("textract", region_name="eu-central-1")
    s = Stubber(c)
    s.add_client_error("detect_document_text", "InvalidParameterException")
    with s, pytest.raises(TextractUnavailable) as info:
        detect(c, b"img", 10, 10)
    assert str(info.value) == "InvalidParameterException"
