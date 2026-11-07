import time

import pytest
from r5ocr import handler
from r5ocr.geometry import Box
from r5ocr.llm import Usage
from r5ocr.payload import HookRequest
from r5ocr.settings import Settings

pytestmark = pytest.mark.unit

# max_output_tokens: brief's Settings used 16000; real default is 128000, so
# tests use 128000 as the last positional value (per task instructions).
S = Settings("eu.anthropic.claude-sonnet-5", "medium", "M1", 60, 8, 128000)
S0 = Settings("eu.anthropic.claude-sonnet-5", "medium", "M0", 60, 8, 128000)
REQ = HookRequest("S", "T", b"img", "png", 100, 100)
FAR = time.monotonic() + 600


@pytest.fixture(autouse=True)
def _dummy_aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture(autouse=True)
def _page_text(monkeypatch):
    monkeypatch.setattr(
        handler, "transcribe_page", lambda *a, **k: ("PAGE TEXT", Usage(5, 5))
    )


@pytest.fixture()
def _metrics_calls(monkeypatch):
    """Record every handler.metrics.emit(...) call as (name, value, dims)."""
    calls = []
    monkeypatch.setattr(
        handler.metrics,
        "emit",
        lambda name, value=1, **dims: calls.append((name, value, dims)),
    )
    return calls


def _detectors(paddle=None, textract=None):
    return {
        "paddle": lambda img: paddle or [],
        "textract": lambda img, w, h: textract or [],
    }


def test_happy_path_returns_blocks_and_same_text(monkeypatch, _metrics_calls):
    monkeypatch.setattr(
        handler, "read_regions", lambda *a, **k: ({1: "مرحبا", 2: "Hello"}, Usage(2, 2))
    )
    out = handler.process_page(
        REQ,
        S,
        None,
        None,
        FAR,
        _detectors(
            paddle=[Box(10, 10, 60, 20, "paddle")],
            textract=[Box(10, 40, 90, 55, "textract")],
        ),
    )
    assert out["output"]["message"]["content"][0]["text"] == "PAGE TEXT"
    assert [b["Text"] for b in out["textractBlocks"]["Blocks"][1:]] == [
        "مرحبا",
        "Hello",
    ]
    assert out["usage"]["totalTokens"] == 14
    assert any(name == "BoxLines" and value == 2 for name, value, _ in _metrics_calls)


def test_detector_crash_falls_back_to_text_only(monkeypatch):
    def boom(img):
        raise RuntimeError("paddle died")

    out = handler.process_page(
        REQ, S, None, None, FAR, {"paddle": boom, "textract": lambda *a: []}
    )
    assert "textractBlocks" not in out
    assert out["output"]["message"]["content"][0]["text"] == "PAGE TEXT"


def test_paddle_crash_keeps_textract_boxes(monkeypatch, _metrics_calls):
    def boom(img):
        raise RuntimeError("paddle died")

    monkeypatch.setattr(
        handler, "read_regions", lambda *a, **k: ({1: "Hello"}, Usage())
    )
    out = handler.process_page(
        REQ,
        S,
        None,
        None,
        FAR,
        {
            "paddle": boom,
            "textract": lambda img, w, h: [Box(10, 10, 60, 20, "textract")],
        },
    )
    assert [b["Text"] for b in out["textractBlocks"]["Blocks"][1:]] == ["Hello"]
    assert any(
        name == "DetectorDegraded" and dims.get("detector") == "paddle"
        for name, _, dims in _metrics_calls
    )


def test_reader_error_falls_back(monkeypatch, _metrics_calls):
    from r5ocr.reader import ReaderFormatError

    def bad(*a, **k):
        raise ReaderFormatError("x")

    monkeypatch.setattr(handler, "read_regions", bad)
    out = handler.process_page(
        REQ, S, None, None, FAR, _detectors(paddle=[Box(1, 1, 50, 20, "paddle")])
    )
    assert "textractBlocks" not in out
    assert any(name == "BoxesDegraded" for name, _, _ in _metrics_calls)


def test_reader_deadline_exceeded_falls_back_with_boxes_degraded(
    monkeypatch, _metrics_calls
):
    from r5ocr.reader import ReaderDeadlineExceeded

    def bad(*a, **k):
        raise ReaderDeadlineExceeded("not enough time budget left")

    monkeypatch.setattr(handler, "read_regions", bad)
    out = handler.process_page(
        REQ, S, None, None, FAR, _detectors(paddle=[Box(1, 1, 50, 20, "paddle")])
    )
    assert "textractBlocks" not in out
    assert out["output"]["message"]["content"][0]["text"] == "PAGE TEXT"
    assert any(
        name == "BoxesDegraded" and dims.get("reason") == "ReaderDeadlineExceeded"
        for name, _, dims in _metrics_calls
    )


def test_deadline_passed_falls_back(monkeypatch):
    """The reader must never be reached once the deadline has already passed."""
    read_calls = []

    def fake_read_regions(*a, **k):
        read_calls.append(1)
        return {1: "Hello"}, Usage()

    monkeypatch.setattr(handler, "read_regions", fake_read_regions)
    out = handler.process_page(
        REQ,
        S,
        None,
        None,
        time.monotonic() - 1,
        _detectors(paddle=[Box(1, 1, 50, 20, "paddle")]),
    )
    assert "textractBlocks" not in out
    assert read_calls == []


def test_deadline_far_returns_blocks_with_same_stub(monkeypatch):
    """Same stub as above, but with a far deadline the reader IS reached."""
    monkeypatch.setattr(
        handler, "read_regions", lambda *a, **k: ({1: "Hello"}, Usage())
    )
    out = handler.process_page(
        REQ, S, None, None, FAR, _detectors(paddle=[Box(1, 1, 50, 20, "paddle")])
    )
    assert "textractBlocks" in out
    assert [b["Text"] for b in out["textractBlocks"]["Blocks"][1:]] == ["Hello"]


def _spy_detectors(calls):
    def paddle(img):
        calls.append("paddle")
        return [Box(10, 10, 60, 20, "paddle")]

    def textract(img, w, h):
        calls.append("textract")
        return [Box(10, 40, 90, 55, "textract")]

    return {"paddle": paddle, "textract": textract}


def test_textract_skipped_when_remaining_below_its_budget(monkeypatch, _metrics_calls):
    """100 s left < 130 s Textract budget -> Textract skipped, Paddle runs."""
    calls = []
    monkeypatch.setattr(
        handler, "read_regions", lambda *a, **k: ({1: "Hello"}, Usage())
    )
    out = handler.process_page(
        REQ, S, None, None, time.monotonic() + 100, _spy_detectors(calls)
    )
    assert calls == ["paddle"]
    assert out["output"]["message"]["content"][0]["text"] == "PAGE TEXT"
    assert [b["Text"] for b in out["textractBlocks"]["Blocks"][1:]] == ["Hello"]
    assert (
        "DetectorDegraded",
        1,
        {"reason": "DeadlineSkip", "detector": "textract"},
    ) in _metrics_calls


def test_paddle_skipped_when_remaining_below_its_budget(monkeypatch, _metrics_calls):
    """30 s left < 60 s Paddle budget (and < Textract's) -> both skipped,
    step-A text still returned."""
    calls = []
    out = handler.process_page(
        REQ, S, None, None, time.monotonic() + 30, _spy_detectors(calls)
    )
    assert calls == []
    assert "textractBlocks" not in out
    assert out["output"]["message"]["content"][0]["text"] == "PAGE TEXT"
    skipped = {
        dims["detector"]
        for name, _, dims in _metrics_calls
        if name == "DetectorDegraded" and dims.get("reason") == "DeadlineSkip"
    }
    assert skipped == {"paddle", "textract"}


def test_detectors_run_when_remaining_covers_budgets(monkeypatch):
    calls = []
    monkeypatch.setattr(
        handler, "read_regions", lambda *a, **k: ({1: "x", 2: "y"}, Usage())
    )
    handler.process_page(
        REQ, S, None, None, time.monotonic() + 200, _spy_detectors(calls)
    )
    assert calls == ["paddle", "textract"]


def test_textract_unavailable_uses_paddle_for_english(monkeypatch, _metrics_calls):
    from r5ocr.detectors.textract import TextractUnavailable

    def throttled(img, w, h):
        raise TextractUnavailable("ThrottlingException")

    monkeypatch.setattr(
        handler, "read_regions", lambda *a, **k: ({1: "Hello"}, Usage())
    )
    out = handler.process_page(
        REQ,
        S,
        None,
        None,
        FAR,
        {"paddle": lambda img: [Box(10, 10, 60, 20, "paddle")], "textract": throttled},
    )
    assert [b["Text"] for b in out["textractBlocks"]["Blocks"][1:]] == ["Hello"]
    assert any(
        name == "TextractDegraded" and dims.get("reason") == "ThrottlingException"
        for name, _, dims in _metrics_calls
    )


def test_page_text_failure_propagates(monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("ThrottlingException: rate exceeded")

    monkeypatch.setattr(handler, "transcribe_page", fail)
    with pytest.raises(RuntimeError, match="ThrottlingException"):
        handler.process_page(REQ, S, None, None, FAR, _detectors())


def test_empty_region_text_is_dropped(monkeypatch):
    monkeypatch.setattr(handler, "read_regions", lambda *a, **k: ({1: ""}, Usage()))
    out = handler.process_page(
        REQ, S, None, None, FAR, _detectors(paddle=[Box(1, 1, 50, 20, "paddle")])
    )
    assert "textractBlocks" not in out


def test_duplicate_text_on_same_row_emits_one_line(monkeypatch):
    """two regions on the same row, both read as the same text -> one LINE."""
    left = Box(10, 10, 50, 30, "paddle")
    right = Box(600, 10, 650, 30, "paddle")
    monkeypatch.setattr(
        handler,
        "read_regions",
        lambda *a, **k: ({1: "Duplicate", 2: "Duplicate"}, Usage()),
    )
    out = handler.process_page(
        REQ, S, None, None, FAR, _detectors(paddle=[left, right])
    )
    lines = out["textractBlocks"]["Blocks"][1:]
    assert [b["Text"] for b in lines] == ["Duplicate"]


def test_m0_uses_detector_text_and_never_calls_reader(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("read_regions must not be called in M0")

    monkeypatch.setattr(handler, "read_regions", boom)
    out = handler.process_page(
        REQ,
        S0,
        None,
        None,
        FAR,
        _detectors(
            paddle=[Box(10, 10, 60, 20, "paddle", "مرحبا")],
            textract=[Box(10, 40, 90, 55, "textract", "Hello")],
        ),
    )
    assert [b["Text"] for b in out["textractBlocks"]["Blocks"][1:]] == [
        "مرحبا",
        "Hello",
    ]


def test_m0_non_arabic_paddle_text_prefers_textract():
    out = handler.process_page(
        REQ,
        S0,
        None,
        None,
        FAR,
        _detectors(
            paddle=[Box(10, 10, 60, 20, "paddle", "Hello")],
            textract=[Box(10, 10, 60, 20, "textract", "Hello World")],
        ),
    )
    assert [b["Text"] for b in out["textractBlocks"]["Blocks"][1:]] == ["Hello World"]


def test_m0_paddle_only_non_arabic_falls_back_to_paddle():
    out = handler.process_page(
        REQ,
        S0,
        None,
        None,
        FAR,
        _detectors(paddle=[Box(10, 10, 60, 20, "paddle", "ABC")]),
    )
    assert [b["Text"] for b in out["textractBlocks"]["Blocks"][1:]] == ["ABC"]


def test_m0_region_without_text_is_skipped():
    out = handler.process_page(
        REQ,
        S0,
        None,
        None,
        FAR,
        _detectors(paddle=[Box(10, 10, 60, 20, "paddle", "")]),
    )
    assert "textractBlocks" not in out


def test_m0_arabic_members_joined_right_to_left():
    # Direct probe: build_regions merges two disjoint (iou=0, no containment)
    # PaddleOCR word boxes into one region only via a spanning Textract box,
    # so that box is added here purely to make the two Paddle words share a
    # region (its own text is irrelevant to this test).
    big_req = HookRequest("S", "T", b"img", "png", 1000, 1000)
    out = handler.process_page(
        big_req,
        S0,
        None,
        None,
        FAR,
        _detectors(
            paddle=[
                Box(200, 10, 300, 30, "paddle", "الأولى"),
                Box(310, 10, 420, 30, "paddle", "المرحلة"),
            ],
            textract=[Box(195, 8, 425, 32, "textract", "x")],
        ),
    )
    assert [b["Text"] for b in out["textractBlocks"]["Blocks"][1:]] == [
        "المرحلة الأولى"
    ]


def _stub_lambda_handler_deps(monkeypatch):
    monkeypatch.setenv("READER_MODEL_ID", "eu.anthropic.claude-sonnet-5")
    monkeypatch.setattr(handler.boto3, "client", lambda *a, **k: object())
    monkeypatch.setattr(handler.textract_det, "make_client", lambda *a, **k: object())
    monkeypatch.setattr(handler, "parse_request", lambda event, s3: REQ)


def test_lambda_handler_computes_deadline_from_context(monkeypatch, _metrics_calls):
    handler._CLIENTS.clear()
    _stub_lambda_handler_deps(monkeypatch)

    captured = {}

    def fake_process_page(
        req, settings, bedrock, textract, deadline, detectors=None, reader_client=None
    ):
        captured["deadline"] = deadline
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": "x"}]}},
            "usage": {},
        }

    monkeypatch.setattr(handler, "process_page", fake_process_page)

    class FakeContext:
        def get_remaining_time_in_millis(self):
            return 900000

    before = time.monotonic()
    out = handler.lambda_handler({}, FakeContext())
    assert out["output"]["message"]["content"][0]["text"] == "x"
    assert abs(captured["deadline"] - (before + 840)) <= 2
    assert any(name == "Pages" for name, _, _ in _metrics_calls)


def test_lambda_handler_caches_boto3_clients_across_invocations(
    monkeypatch, _metrics_calls
):
    handler._CLIENTS.clear()
    monkeypatch.setenv("READER_MODEL_ID", "eu.anthropic.claude-sonnet-5")

    client_calls = []
    monkeypatch.setattr(
        handler.boto3,
        "client",
        lambda *a, **k: (client_calls.append((a, k)), object())[1],
    )
    make_client_calls = []
    monkeypatch.setattr(
        handler.textract_det,
        "make_client",
        lambda *a, **k: (make_client_calls.append((a, k)), object())[1],
    )
    monkeypatch.setattr(handler, "parse_request", lambda event, s3: REQ)
    monkeypatch.setattr(
        handler,
        "process_page",
        lambda *a, **k: {
            "output": {"message": {"role": "assistant", "content": [{"text": "x"}]}},
            "usage": {},
        },
    )

    class FakeContext:
        def get_remaining_time_in_millis(self):
            return 900000

    handler.lambda_handler({}, FakeContext())
    handler.lambda_handler({}, FakeContext())

    # Default READ_METHOD is M0, which never calls the reader: bedrock-runtime
    # and s3 are each created exactly once, not twice, across both
    # invocations, and no "bedrock-reader" client is created at all.
    assert len(client_calls) == 2
    assert len(make_client_calls) == 1
    assert "bedrock-reader" not in handler._CLIENTS
    assert sum(1 for name, _, _ in _metrics_calls if name == "Pages") == 2


def test_lambda_handler_creates_reader_client_when_read_method_is_m1(
    monkeypatch, _metrics_calls
):
    handler._CLIENTS.clear()
    monkeypatch.setenv("READER_MODEL_ID", "eu.anthropic.claude-sonnet-5")
    monkeypatch.setenv("READ_METHOD", "M1")

    client_calls = []
    monkeypatch.setattr(
        handler.boto3,
        "client",
        lambda *a, **k: (client_calls.append((a, k)), object())[1],
    )
    monkeypatch.setattr(handler.textract_det, "make_client", lambda *a, **k: object())
    monkeypatch.setattr(handler, "parse_request", lambda event, s3: REQ)
    monkeypatch.setattr(
        handler,
        "process_page",
        lambda *a, **k: {
            "output": {"message": {"role": "assistant", "content": [{"text": "x"}]}},
            "usage": {},
        },
    )

    class FakeContext:
        def get_remaining_time_in_millis(self):
            return 900000

    handler.lambda_handler({}, FakeContext())
    handler.lambda_handler({}, FakeContext())

    # With READ_METHOD=M1 the reader client IS created, and only once.
    assert len(client_calls) == 3
    assert "bedrock-reader" in handler._CLIENTS


def test_lambda_handler_defaults_remaining_time_when_context_is_none(monkeypatch):
    handler._CLIENTS.clear()
    _stub_lambda_handler_deps(monkeypatch)

    captured = {}

    def fake_process_page(
        req, settings, bedrock, textract, deadline, detectors=None, reader_client=None
    ):
        captured["deadline"] = deadline
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": "x"}]}},
            "usage": {},
        }

    monkeypatch.setattr(handler, "process_page", fake_process_page)

    before = time.monotonic()
    handler.lambda_handler({}, None)
    # remaining defaults to 840s when context is None; safety margin (60s
    # default) is still subtracted -> deadline ~= before + 780.
    assert abs(captured["deadline"] - (before + 780)) <= 2


def test_no_document_text_in_logs_or_metrics(monkeypatch, capsys, caplog):
    """Neither step-A text nor detector/reader text may reach logs or EMF output
    on degraded paths."""
    from r5ocr.detectors.textract import TextractUnavailable
    from r5ocr.reader import ReaderFormatError

    secret_page = "SECRETPAGETEXT"
    secret_box = "SECRETBOXTEXT"
    monkeypatch.setattr(
        handler, "transcribe_page", lambda *a, **k: (secret_page, Usage(1, 1))
    )

    def bad_reader(*a, **k):
        raise ReaderFormatError(f"bad {secret_box}")

    monkeypatch.setattr(handler, "read_regions", bad_reader)

    def crash(img):
        raise RuntimeError(f"paddle died on {secret_box}")

    def throttled(img, w, h):
        raise TextractUnavailable("ThrottlingException")

    with caplog.at_level("DEBUG"):
        # detector crash + Textract degraded, then reader failure
        handler.process_page(
            REQ, S, None, None, FAR, {"paddle": crash, "textract": throttled}
        )
        handler.process_page(
            REQ,
            S,
            None,
            None,
            FAR,
            {
                "paddle": lambda img: [Box(1, 1, 50, 20, "paddle", secret_box)],
                "textract": lambda img, w, h: [],
            },
        )
        # deadline skip
        handler.process_page(
            REQ, S, None, None, time.monotonic() + 10, _spy_detectors([])
        )
    captured = capsys.readouterr()
    everything = captured.out + captured.err + caplog.text
    assert "BoxesDegraded" in everything and "DetectorDegraded" in everything
    assert secret_page not in everything
    assert secret_box not in everything


@pytest.mark.parametrize("cfg", ["_TRANSCRIBE_CONFIG", "_READER_CONFIG"])
def test_bedrock_clients_make_exactly_one_attempt(cfg):
    # botocore's "max_attempts" counts RETRIES (3 -> 4 attempts); only
    # total_max_attempts=1 gives the single attempt the deadline math assumes.
    client = handler.boto3.client(
        "bedrock-runtime", region_name="eu-central-1", config=getattr(handler, cfg)
    )
    assert client.meta.config.retries["total_max_attempts"] == 1


def _m0_with_page(monkeypatch, page, dets):
    monkeypatch.setattr(handler, "transcribe_page", lambda *a, **k: (page, Usage(5, 5)))
    return handler.process_page(REQ, S0, None, None, FAR, detectors=dets)


def test_m0_mixed_row_keeps_latin_words_paddle_cannot_read(monkeypatch):
    # Real licence row: both detectors box the whole row; PaddleOCR's Arabic
    # recognizer reads only the Arabic label, Textract reads the Latin value
    # too (and the Arabic in reversed word order).
    dets = {
        "paddle": lambda img: [Box(5, 10, 95, 20, "paddle", "الرقم التسلسلي")],
        "textract": lambda img, w, h: [
            Box(6, 10, 94, 20, "textract", "Serial No. UTA 7712 0045 التسلسلي الرقم")
        ],
    }
    page = "Serial No. UTA 7712 0045 الرقم التسلسلي"
    out = _m0_with_page(monkeypatch, page, dets)
    texts = [
        b["Text"] for b in out["textractBlocks"]["Blocks"] if b["BlockType"] == "LINE"
    ]
    assert texts == ["Serial No. UTA 7712 0045 الرقم التسلسلي"]


def test_m0_drops_textract_misreading_on_top_of_arabic(monkeypatch):
    dets = {
        "paddle": lambda img: [Box(10, 10, 90, 20, "paddle", "شارع الياسمين")],
        "textract": lambda img, w, h: [Box(12, 11, 88, 19, "textract", "jlw Ibwls")],
    }
    out = _m0_with_page(monkeypatch, "شارع الياسمين", dets)
    texts = [
        b["Text"] for b in out["textractBlocks"]["Blocks"] if b["BlockType"] == "LINE"
    ]
    assert texts == ["شارع الياسمين"]


def test_m0_restores_arabic_digits_from_the_transcription(monkeypatch):
    dets = {
        "paddle": lambda img: [
            Box(10, 10, 90, 20, "paddle", "شارع الياسمين ، حي النخيل")
        ],
        "textract": lambda img, w, h: [],
    }
    page = "العنوان: شارع الياسمين ١٢، حي النخيل"
    out = _m0_with_page(monkeypatch, page, dets)
    texts = [
        b["Text"] for b in out["textractBlocks"]["Blocks"] if b["BlockType"] == "LINE"
    ]
    assert texts == ["شارع الياسمين ١٢، حي النخيل"]
