import io
import json
import re
import time

import numpy as np
import pytest
from PIL import Image
from r5ocr import reader
from r5ocr.geometry import Box, intersection
from r5ocr.llm import Usage
from r5ocr.payload import HookRequest
from r5ocr.reader import (
    ReaderDeadlineExceeded,
    ReaderFormatError,
    _label_position,
    parse_reply,
)
from r5ocr.regions import Region
from r5ocr.settings import Settings

pytestmark = pytest.mark.unit


def _png(w=400, h=400):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def _noisy_png(w, h, seed=0):
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, "PNG")
    return buf.getvalue()


def test_parse_plain_json():
    assert parse_reply(
        '{"lines":[{"id":1,"text":"مرحبا"},{"id":2,"text":""}]}', {1, 2}
    ) == {1: "مرحبا", 2: ""}


def test_parse_fenced_and_ignores_unknown_ids():
    txt = '```json\n{"lines":[{"id":1,"text":"a"},{"id":9,"text":"b"}]}\n```'
    assert parse_reply(txt, {1}) == {1: "a"}


def test_parse_garbage_raises():
    with pytest.raises(ReaderFormatError):
        parse_reply("sorry, I cannot", {1})


def test_parse_reply_ignores_braces_in_surrounding_prose():
    text = 'Here is {example} output: {"lines":[{"id":1,"text":"hi"}]} (see {note})'
    assert parse_reply(text, {1}) == {1: "hi"}


def test_label_position_case_a_above_left_when_there_is_room():
    e = Box(100, 100, 200, 150, "paddle")
    assert _label_position(e, 20.0, 16.0, 800, 600) == (80.0, 84.0)


def test_label_position_case_b_left_vertically_centred_when_no_room_above():
    e = Box(300, 0, 500, 30, "paddle")
    lx, ly = _label_position(e, 20.0, 16.0, 800, 600)
    assert (lx, ly) == (280.0, 7.0)
    label_rect = Box(lx, ly, lx + 20.0, ly + 16.0, "label")
    assert intersection(label_rect, e) == 0.0


def test_label_position_case_c_right_vertically_centred_when_no_room_left():
    e = Box(0, 100, 50, 150, "paddle")
    lx, ly = _label_position(e, 20.0, 16.0, 800, 600)
    assert (lx, ly) == (50.0, 117.0)
    label_rect = Box(lx, ly, lx + 20.0, ly + 16.0, "label")
    assert intersection(label_rect, e) == 0.0


def test_label_position_case_d_last_resort_clipped_to_zero():
    e = Box(0, 0, 790, 20, "paddle")
    assert _label_position(e, 20.0, 16.0, 800, 600) == (0.0, 0.0)


def test_read_regions_retries_once_then_raises(monkeypatch):
    calls = []

    def fake_converse(client, model_id, system, content, effort, max_tokens):
        calls.append(content)
        return "not json", Usage(1, 1)

    monkeypatch.setattr(reader, "converse", fake_converse)
    settings = Settings("m", None, "M2", 60, 8, 128000)
    req = HookRequest("SYS", "TASK", b"not-a-real-png", "png", 100, 100)
    regions = [Region(1, {}, Box(0, 0, 10, 10, "paddle"))]

    with pytest.raises(ReaderFormatError):
        reader.read_regions(object(), settings, req, regions)

    assert len(calls) == 2


def test_m3_batches_of_eight(monkeypatch):
    calls = []

    def fake_converse(client, model_id, system, content, effort, max_tokens):
        calls.append(content)
        ids = [
            int(i)
            for item in content
            if "text" in item
            for i in re.findall(r"Region (\d+):", item["text"])
        ]
        reply = {"lines": [{"id": i, "text": f"t{i}"} for i in ids]}
        return json.dumps(reply), Usage(1, 1)

    monkeypatch.setattr(reader, "converse", fake_converse)
    settings = Settings("m", None, "M3", 60, 8, 128000)
    req = HookRequest("SYS", "TASK", _png(400, 400), "png", 400, 400)
    regions = [
        Region(i, {}, Box(10, 10 + (i - 1) * 20, 100, 25 + (i - 1) * 20, "paddle"))
        for i in range(1, 18)
    ]

    texts, usage = reader.read_regions(object(), settings, req, regions)

    assert len(calls) == 3
    assert set(texts) == set(range(1, 18))
    assert isinstance(usage, Usage)


def test_read_regions_raises_when_deadline_inside_budget(monkeypatch):
    """not enough time left for another call -> raise before calling converse."""
    calls = []

    def fake_converse(*a, **k):
        calls.append(1)
        return "irrelevant", Usage()

    monkeypatch.setattr(reader, "converse", fake_converse)
    settings = Settings("m", None, "M1", 60, 8, 128000)
    req = HookRequest("SYS", "TASK", _png(400, 400), "png", 400, 400)
    regions = [Region(1, {}, Box(10, 10, 100, 40, "paddle"))]

    deadline = time.monotonic() + 5  # only 5s of budget left
    with pytest.raises(ReaderDeadlineExceeded):
        reader.read_regions(
            object(), settings, req, regions, deadline=deadline, min_call_budget_s=130
        )
    assert calls == []


def test_m3_deadline_expires_after_first_batch(monkeypatch):
    """the deadline check runs before EVERY M3 batch call, not just once."""
    calls = []

    def fake_converse(client, model_id, system, content, effort, max_tokens):
        calls.append(content)
        ids = [
            int(i)
            for item in content
            if "text" in item
            for i in re.findall(r"Region (\d+):", item["text"])
        ]
        reply = {"lines": [{"id": i, "text": f"t{i}"} for i in ids]}
        return json.dumps(reply), Usage(1, 1)

    monkeypatch.setattr(reader, "converse", fake_converse)
    # First deadline check (before batch 1) passes with room to spare; the
    # second (before batch 2) has fallen inside the budget and must raise
    # before batch 2's converse call is made.
    ticks = iter([900.0, 970.0])
    monkeypatch.setattr(reader.time, "monotonic", lambda: next(ticks))
    settings = Settings("m", None, "M3", 60, 8, 128000)
    req = HookRequest("SYS", "TASK", _png(400, 400), "png", 400, 400)
    regions = [
        Region(i, {}, Box(10, 10 + (i - 1) * 20, 100, 25 + (i - 1) * 20, "paddle"))
        for i in range(1, 10)
    ]

    with pytest.raises(ReaderDeadlineExceeded):
        reader.read_regions(
            object(), settings, req, regions, deadline=1000.0, min_call_budget_s=50
        )

    assert len(calls) == 1


def test_m1_sends_overlay_image_and_lists_ids(monkeypatch):
    calls = []

    def fake_converse(client, model_id, system, content, effort, max_tokens):
        calls.append(content)
        reply = {"lines": [{"id": 1, "text": "t1"}, {"id": 2, "text": "t2"}]}
        return json.dumps(reply), Usage(1, 1)

    monkeypatch.setattr(reader, "converse", fake_converse)
    settings = Settings("m", None, "M1", 60, 8, 128000)
    img_bytes = _png(400, 400)
    req = HookRequest("SYS", "TASK", img_bytes, "png", 400, 400)
    regions = [
        Region(1, {}, Box(10, 10, 100, 40, "paddle")),
        Region(2, {}, Box(10, 60, 100, 90, "paddle")),
    ]

    texts, usage = reader.read_regions(object(), settings, req, regions)

    assert len(calls) == 1
    content = calls[0]
    assert len(content) == 2
    assert "text" in content[0]
    assert "[1, 2]" in content[0]["text"]
    assert "image" in content[1]
    assert content[1]["image"]["format"] == "jpeg"
    assert content[1]["image"]["source"]["bytes"] != img_bytes
    assert texts == {1: "t1", 2: "t2"}


def test_m1_overlay_of_large_noisy_page_is_jpeg_within_bedrock_limits(monkeypatch):
    calls = []

    def fake_converse(client, model_id, system, content, effort, max_tokens):
        calls.append(content)
        reply = {"lines": [{"id": 1, "text": "t1"}]}
        return json.dumps(reply), Usage(1, 1)

    monkeypatch.setattr(reader, "converse", fake_converse)
    settings = Settings("m", None, "M1", 60, 8, 128000)
    w, h = 2025, 3600
    png_bytes = _noisy_png(w, h, seed=42)
    # Sanity: the raw PNG this scan produces really does overflow the limit
    # this test exercises, so the JPEG re-encode path is actually exercised.
    assert len(png_bytes) > reader.MAX_IMAGE_BYTES
    req = HookRequest("SYS", "TASK", png_bytes, "png", w, h)
    regions = [Region(1, {}, Box(100, 100, 500, 200, "paddle"))]

    reader.read_regions(object(), settings, req, regions)

    content = calls[0]
    image = content[1]["image"]
    assert image["format"] == "jpeg"
    assert len(image["source"]["bytes"]) <= reader.MAX_IMAGE_BYTES


def test_m2_sends_original_image_and_normalised_boxes(monkeypatch):
    calls = []

    def fake_converse(client, model_id, system, content, effort, max_tokens):
        calls.append(content)
        reply = {"lines": [{"id": 1, "text": "t1"}, {"id": 2, "text": "t2"}]}
        return json.dumps(reply), Usage(1, 1)

    monkeypatch.setattr(reader, "converse", fake_converse)
    settings = Settings("m", None, "M2", 60, 8, 128000)
    img_bytes = b"not-a-real-png-but-thats-fine-for-m2"
    req = HookRequest("SYS", "TASK", img_bytes, "png", 1000, 1000)
    regions = [
        Region(1, {}, Box(0, 0, 100, 100, "paddle")),
        Region(2, {}, Box(500, 500, 600, 600, "paddle")),
    ]

    texts, usage = reader.read_regions(object(), settings, req, regions)

    content = calls[0]
    assert content[1]["image"] == {"format": "png", "source": {"bytes": img_bytes}}
    expected_boxes = [
        {"id": 1, "box": [0, 0, 100, 100]},
        {"id": 2, "box": [500, 500, 600, 600]},
    ]
    assert json.dumps(expected_boxes) in content[0]["text"]
    assert texts == {1: "t1", 2: "t2"}
