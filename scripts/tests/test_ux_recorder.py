# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the UX review recorder (scripts/ux_recorder*.py).

What is worth pinning here is everything a viewer would notice and a live run
would not catch quickly: the WebSocket client speaks RFC 6455 correctly against
Chrome, frames are acknowledged without blocking the reader, paused time never
reaches the video, each segment is paced for a person (pre-roll, action after the
narrator has started, no fast-forward past the ceiling, a settle at the end),
captions line up with the voice, and the daemon's control protocol writes the
files the skill tells the agent to fill in. Nothing here needs AWS, Chrome or a
network; the one ffmpeg test skips when ffmpeg is absent.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
HAS_PIL = importlib.util.find_spec("PIL") is not None

cdp = _load("ux_recorder_cdp")
render = _load("ux_recorder_render")
recorder_cli = _load("ux_recorder")


class FakeSocket:
    def __init__(self, incoming: bytes = b"") -> None:
        self.incoming = bytearray(incoming)
        self.sent = bytearray()

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, n: int) -> bytes:
        chunk = bytes(self.incoming[:n])
        del self.incoming[:n]
        return chunk

    def settimeout(self, _t) -> None:
        pass

    def close(self) -> None:
        pass


def server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    head = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack("!H", n)
    else:
        head.append(127)
        head += struct.pack("!Q", n)
    return bytes(head) + payload


@pytest.mark.unit
class TestWebSocketClient:
    def test_accept_key_matches_the_rfc_6455_example(self):
        assert (
            cdp.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        )

    def test_handshake_accepts_a_correct_server_reply(self):
        class HandshakingSocket(FakeSocket):
            def sendall(self, data: bytes) -> None:
                super().sendall(data)
                text = data.decode("ascii")
                if text.startswith("GET "):
                    nonce = [
                        h
                        for h in text.split("\r\n")
                        if h.startswith("Sec-WebSocket-Key:")
                    ][0].split(": ")[1]
                    self.incoming += (
                        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                        f"Sec-WebSocket-Accept: {cdp.accept_key(nonce)}\r\n\r\n"
                    ).encode("ascii")

        client = cdp.WebSocketClient(
            HandshakingSocket(), "127.0.0.1:9222", "/devtools/page/X"
        )
        client.handshake()

    def test_handshake_rejects_a_wrong_accept(self):
        sock = FakeSocket(
            b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: nope\r\n\r\n"
        )
        client = cdp.WebSocketClient(sock, "h", "/")
        with pytest.raises(cdp.WebSocketError, match="Accept"):
            client.handshake()

    def test_handshake_rejects_a_non_101(self):
        sock = FakeSocket(b"HTTP/1.1 404 Not Found\r\n\r\n")
        with pytest.raises(cdp.WebSocketError, match="101"):
            cdp.WebSocketClient(sock, "h", "/").handshake()

    def test_client_frames_are_masked_text(self):
        sock = FakeSocket()
        cdp.WebSocketClient(sock, "h", "/").send_text("hello")
        sent = bytes(sock.sent)
        assert sent[0] == 0x81
        assert sent[1] & 0x80
        assert sent[1] & 0x7F == 5
        mask, payload = sent[2:6], sent[6:]
        assert bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) == b"hello"

    @pytest.mark.parametrize("size", [5, 200, 70000])
    def test_all_three_payload_lengths_decode(self, size):
        payload = bytes([65]) * size
        sock = FakeSocket(server_frame(cdp.OP_TEXT, payload))
        assert cdp.WebSocketClient(sock, "h", "/").recv_message() == "A" * size

    def test_fragmented_messages_reassemble(self):
        data = server_frame(cdp.OP_TEXT, b"hel", fin=False) + server_frame(
            cdp.OP_CONT, b"lo", fin=True
        )
        assert cdp.WebSocketClient(FakeSocket(data), "h", "/").recv_message() == "hello"

    def test_ping_is_answered_with_a_pong_and_skipped(self):
        data = server_frame(cdp.OP_PING, b"p") + server_frame(cdp.OP_TEXT, b"x")
        sock = FakeSocket(data)
        assert cdp.WebSocketClient(sock, "h", "/").recv_message() == "x"
        assert bytes(sock.sent)[0] == 0x80 | cdp.OP_PONG

    def test_close_frame_yields_none(self):
        sock = FakeSocket(server_frame(cdp.OP_CLOSE, struct.pack("!H", 1000)))
        assert cdp.WebSocketClient(sock, "h", "/").recv_message() is None


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.session = None
        self.replies: dict[str, dict] = {}

    def send_text(self, text: str) -> None:
        msg = json.loads(text)
        self.sent.append(msg)
        reply = self.replies.get(msg["method"])
        if reply is not None and self.session is not None:
            self.session.dispatch({"id": msg["id"], **reply})

    def close(self) -> None:
        pass


def _session(replies: dict[str, dict] | None = None):
    ws = FakeWs()
    ws.replies = replies or {}
    session = cdp.CdpSession(ws)
    ws.session = session
    return ws, session


@pytest.mark.unit
class TestCdpSession:
    def test_call_correlates_the_reply_by_id(self):
        ws, session = _session({"Page.enable": {"result": {"ok": 1}}})
        assert session.call("Page.enable") == {"ok": 1}
        assert ws.sent[0]["id"] == 1 and ws.sent[0]["method"] == "Page.enable"

    def test_error_replies_raise_with_the_message(self):
        _, session = _session({"Page.x": {"error": {"message": "nope"}}})
        with pytest.raises(cdp.CdpError, match="nope"):
            session.call("Page.x")

    def test_notify_sends_without_waiting(self):
        ws, session = _session()
        session.notify("Page.screencastFrameAck", {"sessionId": 7})
        assert ws.sent[-1]["params"] == {"sessionId": 7}

    def test_events_reach_the_handler(self):
        seen = []
        ws = FakeWs()
        session = cdp.CdpSession(ws, on_event=lambda m, p: seen.append((m, p)))
        session.dispatch(
            {"method": "Page.screencastVisibilityChanged", "params": {"visible": False}}
        )
        assert seen == [("Page.screencastVisibilityChanged", {"visible": False})]


@pytest.mark.unit
class TestTargets:
    TARGETS = [
        {
            "type": "page",
            "id": "A",
            "url": "https://d1.cloudfront.net/#/x",
            "webSocketDebuggerUrl": "ws://a",
        },
        {
            "type": "page",
            "id": "B",
            "url": "http://127.0.0.1:3000/#/y",
            "webSocketDebuggerUrl": "ws://b",
        },
        {
            "type": "page",
            "id": "C",
            "url": "chrome://newtab",
            "webSocketDebuggerUrl": "ws://c",
        },
        {"type": "background_page", "id": "D", "url": "chrome-extension://abc/bg.html"},
        {"type": "service_worker", "id": "E", "url": "chrome-extension://abc/sw.js"},
    ]

    def test_only_real_tabs_are_candidates(self):
        assert [t["id"] for t in cdp.page_targets(self.TARGETS)] == ["A", "B"]

    def test_url_substring_selects_one(self):
        assert cdp.choose_target(self.TARGETS, url_contains="cloudfront")["id"] == "A"

    def test_target_id_selects_one(self):
        assert cdp.choose_target(self.TARGETS, target_id="B")["id"] == "B"

    def test_ambiguity_lists_candidates(self):
        with pytest.raises(cdp.TargetChoiceError) as exc:
            cdp.choose_target(self.TARGETS)
        assert [c["id"] for c in exc.value.candidates] == ["A", "B"]

    def test_no_match_is_an_error(self):
        with pytest.raises(cdp.TargetChoiceError):
            cdp.choose_target(self.TARGETS, url_contains="nowhere")


@pytest.mark.unit
class TestScreencast:
    def test_frames_are_written_indexed_and_acked_without_blocking(self, tmp_path):
        ws, session = _session()
        clock = iter([100.0, 101.0])
        sc = cdp.Screencast(
            session,
            tmp_path / "frames",
            tmp_path / "frames.jsonl",
            clock=lambda: next(clock),
        )
        seq = sc.handle_frame(
            {
                "data": base64.b64encode(b"jpeg-bytes").decode(),
                "metadata": {"timestamp": 99.9},
                "sessionId": 42,
            }
        )
        assert seq == 1
        assert (tmp_path / "frames" / "000001.jpg").read_bytes() == b"jpeg-bytes"
        record = json.loads((tmp_path / "frames.jsonl").read_text().splitlines()[0])
        assert record == {
            "seq": 1,
            "t": 100.0,
            "t_chrome": 99.9,
            "file": "frames/000001.jpg",
            "forced": False,
        }
        assert ws.sent[-1]["method"] == "Page.screencastFrameAck"
        assert ws.sent[-1]["params"] == {"sessionId": 42}
        assert sc.bytes == len(b"jpeg-bytes")

    def test_forced_capture_is_flagged(self, tmp_path):
        ws, session = _session(
            {
                "Page.captureScreenshot": {
                    "result": {"data": base64.b64encode(b"shot").decode()}
                }
            }
        )
        sc = cdp.Screencast(
            session, tmp_path / "frames", tmp_path / "frames.jsonl", clock=lambda: 5.0
        )
        assert sc.capture_now() == 1
        record = json.loads((tmp_path / "frames.jsonl").read_text().splitlines()[0])
        assert record["forced"] is True

    def test_start_uses_backpressure_and_jpeg(self):
        ws, session = _session({"Page.startScreencast": {"result": {}}})
        cdp.Screencast(
            session,
            Path("/tmp/x-frames-test"),
            Path("/tmp/x-frames-test/i.jsonl"),
            quality=55,
        ).start()
        params = ws.sent[-1]["params"]
        assert (
            params["format"] == "jpeg"
            and params["quality"] == 55
            and params["maxFramesInFlight"] == 2
        )


@pytest.mark.unit
class TestCursorOverlay:
    def test_overlay_is_installed_now_and_for_new_documents(self):
        ws, session = _session(
            {
                "Runtime.enable": {"result": {}},
                "Runtime.addBinding": {"result": {}},
                "Page.addScriptToEvaluateOnNewDocument": {
                    "result": {"identifier": "s1"}
                },
                "Runtime.evaluate": {"result": {}},
            }
        )
        assert cdp.install_cursor_overlay(session) == "s1"
        methods = [m["method"] for m in ws.sent]
        assert methods == [
            "Runtime.enable",
            "Runtime.addBinding",
            "Page.addScriptToEvaluateOnNewDocument",
            "Runtime.evaluate",
        ]
        assert ws.sent[1]["params"] == {"name": cdp.CLICK_BINDING}
        assert ws.sent[2]["params"]["source"] == cdp.CURSOR_OVERLAY_JS

    def test_overlay_reports_clicks_through_the_binding(self):
        js = cdp.CURSOR_OVERLAY_JS
        assert "window.__uxRecorderEvent" in js and "elementFromPoint" in js
        assert "kind: 'click'" in js and "interactive" in js

    def test_overlay_stays_out_of_hit_testing_and_the_a11y_tree(self):
        js = cdp.CURSOR_OVERLAY_JS
        assert "pointer-events:none" in js
        assert "z-index:2147483647" in js
        assert "aria-hidden" in js
        assert "mousedown" in js and "pointermove" in js


def frames_at(times, start_seq=1):
    return [
        render.Frame(
            seq=start_seq + i,
            t=t,
            file=f"frames/{start_seq + i:06d}.jpg",
            forced=(i == 0),
        )
        for i, t in enumerate(times)
    ]


def ev(seq, t, kind, **kw):
    return render.Event(seq=seq, t=t, type=kind, **kw)


CFG = render.PacingConfig()


@pytest.mark.unit
class TestPauses:
    def test_paused_frames_are_dropped_and_marks_move_to_resume(self):
        events = [
            ev(0, 0, "start"),
            ev(1, 2, "pause"),
            ev(2, 3, "mark", label="in pause"),
            ev(3, 5, "resume"),
            ev(4, 9, "stop"),
        ]
        frames = frames_at([0, 1, 2.5, 4, 6, 8])
        events2, frames2, paused = render.apply_pauses(events, frames)
        assert [f.t for f in frames2] == [0, 1, 6, 8]
        assert next(e for e in events2 if e.type == "mark").t == 5
        assert paused == 3

    def test_unmatched_pause_runs_to_stop(self):
        events = [ev(0, 0, "start"), ev(1, 2, "pause"), ev(2, 9, "stop")]
        assert render.pause_intervals(events) == [(2, 9)]


@pytest.mark.unit
class TestSegments:
    def test_idle_gaps_are_clamped(self):
        events = [ev(0, 0, "start"), ev(1, 40, "stop")]
        segs = render.build_segments(events, frames_at([0, 30]), {}, CFG)
        assert len(segs) == 1
        assert segs[0].total < 40
        assert segs[0].total >= CFG.min_seg
        assert segs[0].durations[0] == CFG.gap_max

    def test_a_quick_mark_never_flashes_by(self):
        events = [ev(0, 0, "start"), ev(1, 0.2, "mark", label="a"), ev(2, 0.4, "stop")]
        segs = render.build_segments(events, frames_at([0, 0.2]), {}, CFG)
        assert all(abs(s.total - CFG.min_seg) < 1e-9 for s in segs)

    def test_the_action_lands_after_the_narrator_has_started(self):
        events = [
            ev(0, 0, "start"),
            ev(1, 10, "mark", label="click"),
            ev(2, 20, "stop"),
        ]
        frames = frames_at([0]) + frames_at([10, 10.2, 10.4], start_seq=2)
        segs = render.build_segments(
            events, frames, {1: ("We click the button.", 3.0)}, CFG
        )
        click = segs[1]
        assert click.durations[0] == pytest.approx(CFG.pre_roll + 1.5)
        assert click.voice_offset == CFG.pre_roll

    def test_voice_longer_than_the_visual_holds_the_final_frame(self):
        events = [ev(0, 0, "start"), ev(1, 1, "stop")]
        segs = render.build_segments(
            events, frames_at([0, 0.5]), {0: ("A long sentence.", 8.0)}, CFG
        )
        seg = segs[0]
        assert seg.total == pytest.approx(CFG.pre_roll + 8.0 + CFG.hold)
        assert seg.hold_added > 0

    def test_a_spinner_is_sped_up_to_the_cap(self):
        times = [round(i * 0.1, 3) for i in range(200)]
        events = [ev(0, 0, "start"), ev(1, 20, "stop")]
        seg = render.build_segments(events, frames_at(times), {}, CFG)[0]
        assert seg.speedup == pytest.approx(19.9 / CFG.cap, rel=1e-6)
        assert seg.total == pytest.approx(
            seg.durations[0] + CFG.cap + CFG.hold, rel=1e-6
        )
        assert len(seg.files) <= 200

    def test_a_narrated_spinner_fits_its_narration_window(self):
        times = [round(i * 0.1, 3) for i in range(150)]
        events = [ev(0, 0, "start"), ev(1, 15, "stop")]
        seg = render.build_segments(
            events, frames_at(times), {0: ("Short line.", 4.0)}, CFG
        )[0]
        motion = sum(seg.durations[1:]) - seg.hold_added
        assert motion == pytest.approx(
            max(4.0 + CFG.hold, 14.9 / CFG.max_speedup), rel=1e-6
        )
        assert seg.total < CFG.cap

    def test_speedup_never_exceeds_the_ceiling(self):
        times = [round(i * 0.1, 3) for i in range(600)]
        events = [ev(0, 0, "start"), ev(1, 60, "stop")]
        seg = render.build_segments(events, frames_at(times), {}, CFG)[0]
        assert seg.speedup == pytest.approx(CFG.max_speedup, rel=1e-6)
        assert seg.total > CFG.cap

    def test_settle_after_the_last_change(self):
        events = [ev(0, 0, "start"), ev(1, 3, "stop")]
        seg = render.build_segments(events, frames_at([0, 1, 2]), {}, CFG)[0]
        assert seg.durations[-1] >= CFG.hold

    def test_a_segment_with_no_frames_borrows_the_previous_state(self):
        events = [ev(0, 0, "start"), ev(1, 5, "mark", label="quiet"), ev(2, 6, "stop")]
        segs = render.build_segments(events, frames_at([0, 1]), {}, CFG)
        assert segs[1].files == ["frames/000002.jpg"]

    def test_cards_take_the_start_and_stop_narration(self):
        events = [
            ev(0, 0, "start", say="Hello."),
            ev(1, 1, "mark", label="m"),
            ev(2, 2, "stop", say="Bye."),
        ]
        narration = {0: ("Hello.", 1.0), 2: ("Bye.", 1.0)}
        segs = render.build_segments(
            events,
            frames_at([0, 1]),
            narration,
            CFG,
            "cards/title.png",
            "cards/end.png",
        )
        assert [s.kind for s in segs] == ["title", "footage", "footage", "end"]
        assert segs[0].narration == "Hello." and segs[-1].narration == "Bye."
        assert segs[1].narration is None
        assert (
            segs[0].total == CFG.card_seconds and segs[-1].total == CFG.card_seconds + 1
        )

    def test_without_cards_the_start_line_narrates_the_first_segment(self):
        events = [ev(0, 0, "start", say="Hello."), ev(1, 2, "stop")]
        segs = render.build_segments(
            events, frames_at([0, 1]), {0: ("Hello.", 1.0)}, CFG
        )
        assert segs[0].narration == "Hello."

    def test_out_start_accumulates(self):
        events = [ev(0, 0, "start"), ev(1, 5, "mark", label="a"), ev(2, 10, "stop")]
        segs = render.build_segments(events, frames_at([0, 5]), {}, CFG)
        assert segs[0].out_start == 0
        assert segs[1].out_start == pytest.approx(segs[0].total)


@pytest.mark.unit
class TestConcatAndSrt:
    def test_frame_sequence_places_every_change_on_its_planned_frame(self):
        segs = [
            render.Segment(0, "title", "t", ["cards/title.jpg"], [4.0]),
            render.Segment(
                1, "footage", "x", ["frames/1.jpg", "frames/2.jpg"], [0.5, 1.0]
            ),
        ]
        seq = render.frame_sequence(segs, 30)
        assert len(seq) == round(5.5 * 30)
        assert seq[0] == "cards/title.jpg" and seq[119] == "cards/title.jpg"
        assert seq[120] == "frames/1.jpg" and seq[134] == "frames/1.jpg"
        assert seq[135] == "frames/2.jpg" and seq[-1] == "frames/2.jpg"

    def test_frame_sequence_rounding_does_not_drift(self):
        seg = render.Segment(
            0, "footage", "x", [f"f{i}" for i in range(100)], [0.0333] * 100
        )
        assert len(render.frame_sequence([seg], 30)) == round(3.33 * 30)

    def test_audio_concat_list_quotes_paths(self):
        text = render.audio_concat_list(["/a/b.wav", "/it's.wav"])
        lines = text.splitlines()
        assert lines[0] == "ffconcat version 1.0"
        assert lines[1] == "file '/a/b.wav'" and lines[2] == "file '/it'\\''s.wav'"

    def test_srt_cues_follow_the_voice(self):
        seg = render.Segment(
            1,
            "footage",
            "x",
            ["a"],
            [10.0],
            narration="First one. Second longer one!",
            narration_dur=4.0,
            out_start=20.0,
            voice_offset=0.6,
        )
        srt = render.build_srt([seg])
        blocks = srt.strip().split("\n\n")
        assert len(blocks) == 2
        assert blocks[0].splitlines()[0] == "1"
        assert blocks[0].splitlines()[1].startswith("00:00:20,600 --> ")
        assert blocks[0].splitlines()[2] == "First one."
        starts = [b.splitlines()[1].split(" --> ")[0] for b in blocks]
        ends = [b.splitlines()[1].split(" --> ")[1] for b in blocks]
        assert ends[0] <= starts[1]
        assert "<" not in srt

    def test_segments_without_narration_have_no_cues(self):
        assert render.build_srt([render.Segment(0, "footage", "x", ["a"], [3.0])]) == ""

    def test_srt_timestamp(self):
        assert render.srt_timestamp(3661.5) == "01:01:01,500"

    def test_click_table_flags_clicks_that_hit_nothing_interactive(self):
        events = [
            ev(0, 0, "start"),
            ev(1, 5, "mark", label="Open the set"),
            render.Event(
                2,
                6,
                "click",
                x=440,
                y=321,
                target='a "Set"',
                under='a "Set"',
                interactive='a "Set"',
            ),
            render.Event(
                3,
                9,
                "click",
                x=470,
                y=28,
                target='div "Console"',
                under='div "Console"',
            ),
            ev(4, 12, "stop"),
        ]
        table, warnings = render.click_table(events)
        assert 'a "Set"' in table and "nothing interactive" in table
        assert len(warnings) == 1 and "Open the set" in warnings[0]
        assert 'div "Console"' in warnings[0]
        assert render.click_table([ev(0, 0, "start")]) == ("", [])

    def test_click_frames_hold_a_marked_copy_of_the_pre_click_frame(self):
        frames = frames_at([0.0, 1.0, 1.08, 2.0])
        events = [
            ev(0, 0, "start"),
            render.Event(1, 1.02, "click", x=100, y=50, target="a"),
            ev(2, 3, "stop"),
        ]
        marked = render.click_frames(
            frames,
            events,
            0.7,
            lambda click, frame: f"render/clicks/{click.seq:04d}.jpg",
        )
        files = [f.file for f in marked]
        assert files == [
            "frames/000001.jpg",
            "frames/000002.jpg",
            "render/clicks/0001.jpg",
            "frames/000003.jpg",
            "frames/000004.jpg",
        ]
        inserted = marked[2]
        assert inserted.hold == 0.7 and inserted.t == pytest.approx(1.021)
        durations = render._clamped_durations(marked, 3.0, CFG)
        assert durations[2] == pytest.approx(0.7)

    def test_click_hold_survives_a_speedup(self):
        times = [round(i * 0.1, 3) for i in range(300)]
        frames = frames_at(times)
        frames.insert(
            150,
            render.Frame(
                seq=999, t=15.001, file="render/clicks/0001.jpg", forced=True, hold=0.7
            ),
        )
        events = [ev(0, 0, "start"), ev(1, 30, "stop")]
        seg = render.build_segments(events, frames, {}, CFG)[0]
        index = seg.files.index("render/clicks/0001.jpg")
        assert seg.durations[index] >= 0.7
        assert seg.speedup > 1.5

    def test_click_frames_skip_clicks_without_a_marked_copy(self):
        frames = frames_at([0.0, 1.0])
        events = [render.Event(1, 0.5, "click", x=1, y=1)]
        assert render.click_frames(frames, events, 0.7, lambda c, f: None) == frames

    @pytest.mark.skipif(not HAS_PIL, reason="needs Pillow")
    def test_draw_click_marker_writes_a_same_size_frame(self, tmp_path):
        from PIL import Image

        src = tmp_path / "f.jpg"
        Image.new("RGB", (320, 180), "white").save(src)
        out = render.draw_click_marker(src, tmp_path / "m.jpg", 150, 90, 1.0, 1.0)
        marked = Image.open(out)
        assert marked.size == (320, 180)
        assert marked.getpixel((150, 90)) != (255, 255, 255)

    def test_pacing_table_warns_on_silence_and_ceiling(self):
        quiet = render.Segment(0, "footage", "quiet", ["a"], [9.0], silent_stretch=9.0)
        fast = render.Segment(1, "footage", "fast", ["a"], [3.0], speedup=3.0)
        table, warnings = render.pacing_table([quiet, fast], CFG)
        assert "quiet" in table
        assert any("silent" in w for w in warnings) and any(
            "ceiling" in w for w in warnings
        )


@pytest.mark.unit
class TestNarrationText:
    def test_split_for_polly_respects_the_limit_on_sentences(self):
        text = " ".join(f"Sentence number {i}." for i in range(400))
        chunks = render.split_for_polly(text, limit=500)
        assert all(len(c) <= 500 for c in chunks)
        assert all(c.endswith(".") for c in chunks)
        assert " ".join(chunks).split() == text.split()

    def test_an_oversize_sentence_is_hard_split(self):
        text = "word " * 300
        chunks = render.split_for_polly(text.strip(), limit=100)
        assert len(chunks) > 1 and all(len(c) <= 100 for c in chunks)

    def test_ssml_escapes_and_paces(self):
        ssml = render.to_ssml("Fish & chips. Then <more>!")
        assert ssml.startswith('<speak><prosody rate="95%">')
        assert "Fish &amp; chips." in ssml and "&lt;more&gt;" in ssml
        assert '<break time="350ms"/>' in ssml

    def test_cache_key_is_deterministic_and_voice_sensitive(self):
        a = render.narration_cache_key("generative", "Ruth", "hi")
        assert a == render.narration_cache_key("generative", "Ruth", "hi")
        assert a != render.narration_cache_key("generative", "Matthew", "hi")
        assert a != render.narration_cache_key("generative", "Ruth", "hi ")

    def test_synthesize_uses_generative_24k_and_caches(self, tmp_path):
        client = MagicMock()
        client.synthesize_speech.return_value = {"AudioStream": io.BytesIO(b"mp3")}
        out = render.synthesize(
            "Hello there.", tmp_path, "Ruth", client=client, log=lambda m: None
        )
        kwargs = client.synthesize_speech.call_args.kwargs
        assert kwargs["Engine"] == "generative" and kwargs["SampleRate"] == "24000"
        assert kwargs["TextType"] == "ssml" and kwargs["Text"].startswith("<speak>")
        assert out.read_bytes() == b"mp3"
        render.synthesize(
            "Hello there.", tmp_path, "Ruth", client=client, log=lambda m: None
        )
        assert client.synthesize_speech.call_count == 1

    def test_plain_text_mode(self, tmp_path):
        client = MagicMock()
        client.synthesize_speech.return_value = {"AudioStream": io.BytesIO(b"mp3")}
        render.synthesize(
            "Hi.", tmp_path, "Ruth", plain_text=True, client=client, log=lambda m: None
        )
        assert client.synthesize_speech.call_args.kwargs["TextType"] == "text"

    def test_narration_md_roundtrip_and_override(self):
        events = [
            ev(0, 0, "start", say="Hello."),
            ev(1, 1, "mark", label="Open queue", say="We open it."),
            ev(2, 2, "stop"),
        ]
        md = render.write_narration_md(events)
        parsed = render.parse_narration_md(md)
        assert parsed == {0: "Hello.", 1: "We open it.", 2: ""}

    def test_malformed_heading_is_rejected(self):
        with pytest.raises(ValueError, match="## <seq>"):
            render.parse_narration_md("## Open queue\ntext")


@pytest.mark.unit
class TestReportHelpers:
    REPORT = (
        "🖱️  UX review — s, Admin, 2026-09-11\n\nLooked at\n  ✅ 5.1 fine\n\n"
        "Findings                                    (ranked)\n  5.1  Button unclear → label it\n\n  6.2  Spinner forever → add timeout\n\n"
        "Functional breakage\n  none\n"
    )

    def test_findings_are_extracted_until_the_next_header(self):
        assert render.findings_from_review(self.REPORT) == [
            "5.1  Button unclear → label it",
            "6.2  Spinner forever → add timeout",
        ]

    def test_wrap_lines(self):
        assert render.wrap_lines("one two three four", 9) == [
            "one two",
            "three",
            "four",
        ]

    @pytest.mark.skipif(not HAS_PIL, reason="needs Pillow")
    def test_missing_glyphs_get_ascii_stand_ins(self):
        from PIL import ImageFont

        font = ImageFont.load_default(size=20)
        assert render.drawable_text("A \u2192 B", font) in ("A -> B", "A \u2192 B")
        assert render.drawable_text("plain", font) == "plain"

    def test_chapters_replacement_is_idempotent(self):
        skeleton = render.review_skeleton("s", "Admin", "2026-09-11", ["5.1"])
        segs = [
            render.Segment(0, "title", "Title", ["t"], [4.0]),
            render.Segment(1, "footage", "Open", ["a"], [3.0], out_start=4.0),
        ]
        once = render.replace_chapters(skeleton, render.chapters_block(segs))
        twice = render.replace_chapters(once, render.chapters_block(segs))
        assert once == twice
        assert "00:04  Open" in once
        assert once.count(render.CHAPTERS_BEGIN) == 1


@pytest.mark.unit
class TestFfmpegArgv:
    def test_captions_are_embedded_as_an_english_mov_text_track(self):
        argv = render.ffmpeg_argv("v.txt", "a.txt", "r.srt", "out.mp4", 1280, 720, 30)
        assert "-c:s" in argv and argv[argv.index("-c:s") + 1] == "mov_text"
        assert "language=eng" in argv
        assert "-disposition:s:0" not in argv
        assert "-map" in argv and "2:s" in argv
        assert "libx264" in argv and "yuv420p" in argv and "aac" in argv
        assert "-shortest" not in argv
        assert argv[argv.index("-framerate") + 1] == "30" and argv.count("concat") == 1

    def test_no_captions(self):
        argv = render.ffmpeg_argv("v", "a", "r.srt", "o", 2, 2, 30, captions="none")
        assert "2:s" not in argv and "-c:s" not in argv

    def test_audio_segment_offsets_the_voice_by_the_pre_roll(self):
        argv = render.audio_segment_argv("n.mp3", 5.0, 0.6, "a.wav")
        assert "adelay=600:all=1,apad=whole_dur=5.000" in argv
        silence = render.audio_segment_argv(None, 5.0, 0.0, "a.wav")
        assert "anullsrc=r=24000:cl=mono" in silence


class FakeScreencast:
    def __init__(self) -> None:
        self.count = 0
        self.bytes = 0
        self.stopped = False

    def capture_now(self) -> int:
        self.count += 1
        return self.count

    def ack(self, params) -> None:
        pass

    def handle_frame(self, params) -> int:
        return self.capture_now()

    def stop(self) -> None:
        self.stopped = True


def _recorder(tmp_path):
    out_dir = tmp_path / "ux-recordings"
    session_dir = out_dir / "stack-20260911-120000"
    session_dir.mkdir(parents=True)
    meta = {
        "stack": "stack",
        "persona": "Admin",
        "flows": ["5.1"],
        "socket": str(tmp_path / "c.sock"),
        "started": 1000.0,
        "say": "Hello.",
    }
    (session_dir / "session.json").write_text(json.dumps(meta))
    recorder_cli.write_current(
        out_dir, {"session_dir": str(session_dir), "socket": meta["socket"], "pid": 1}
    )
    clock = {"t": 1000.0}

    def tick():
        clock["t"] += 1.0
        return clock["t"]

    rec = recorder_cli.Recorder(session_dir, meta, clock=tick)
    rec.screencast = FakeScreencast()
    return rec, session_dir, out_dir


@pytest.mark.unit
class TestRecorderCommands:
    def test_mark_note_pause_resume_and_stop_write_the_session_files(self, tmp_path):
        rec, session_dir, out_dir = _recorder(tmp_path)
        rec.begin()
        assert rec.handle_command({"cmd": "mark", "label": "Open", "say": "We open."})[
            "ok"
        ]
        assert rec.handle_command({"cmd": "note", "text": "spinner"})["ok"]
        assert rec.handle_command({"cmd": "pause"})["ok"]
        assert rec.handle_command({"cmd": "pause"}) == {
            "ok": False,
            "error": "already paused",
        }
        assert rec.handle_command({"cmd": "resume"})["ok"]
        status = rec.handle_command({"cmd": "status"})
        assert (
            status["marks"] == 1
            and status["last_mark"] == "Open"
            and status["paused"] is False
        )
        reply = rec.handle_command({"cmd": "stop", "say": "Bye."})
        assert reply["ok"] and rec.stop_event.is_set()
        timeline = json.loads((session_dir / "timeline.json").read_text())
        kinds = [e["type"] for e in timeline["events"]]
        assert kinds == ["start", "mark", "note", "pause", "resume", "stop"]
        assert timeline["session"]["stop_reason"] == "stop"
        assert (
            timeline["events"][0]["say"] == "Hello."
            and timeline["events"][-1]["say"] == "Bye."
        )
        assert all(
            e["frame"]
            for e in timeline["events"]
            if e["type"] in ("start", "mark", "stop")
        )
        assert (session_dir / "narration.md").exists()
        review = (session_dir / "review.md").read_text()
        assert (
            "UX review — stack, Admin, 1970" in review
            or "UX review — stack, Admin," in review
        )
        assert render.CHAPTERS_BEGIN in review
        assert not (out_dir / "current.json").exists()

    def test_bad_commands_are_rejected(self, tmp_path):
        rec, _, _ = _recorder(tmp_path)
        assert rec.handle_command({"cmd": "mark", "label": ""}) == {
            "ok": False,
            "error": "mark needs a label",
        }
        assert rec.handle_command({"cmd": "note", "text": " "}) == {
            "ok": False,
            "error": "note needs text",
        }
        assert rec.handle_command({"cmd": "resume"}) == {
            "ok": False,
            "error": "not paused",
        }
        assert rec.handle_command({"cmd": "dance"})["ok"] is False

    def test_no_frames_are_written_while_paused(self, tmp_path):
        rec, _, _ = _recorder(tmp_path)
        rec.begin()
        rec.handle_command({"cmd": "pause"})
        before = rec.screencast.count
        rec._on_event("Page.screencastFrame", {"data": "", "sessionId": 1})
        assert rec.screencast.count == before

    def test_clicks_from_the_binding_land_in_the_timeline(self, tmp_path):
        rec, _, _ = _recorder(tmp_path)
        payload = json.dumps(
            {
                "kind": "click",
                "x": 440,
                "y": 321,
                "target": 'a "UX Review Root Zip"',
                "under": 'a "UX Review Root Zip"',
                "interactive": 'a "UX Review Root Zip"',
            }
        )
        binding = recorder_cli.cdp.CLICK_BINDING
        rec._on_event("Runtime.bindingCalled", {"name": binding, "payload": payload})
        rec._on_event("Runtime.bindingCalled", {"name": "other", "payload": payload})
        rec._on_event("Runtime.bindingCalled", {"name": binding, "payload": "not json"})
        clicks = [e for e in rec.events if e["type"] == "click"]
        assert len(clicks) == 1
        assert clicks[0]["x"] == 440 and clicks[0]["interactive"].startswith("a ")
        assert rec.status()["clicks"] == 1 and rec.status()["clicks_off_target"] == 0

    def test_visibility_changes_are_recorded(self, tmp_path):
        rec, _, _ = _recorder(tmp_path)
        rec._on_event("Page.screencastVisibilityChanged", {"visible": False})
        assert rec.visible is False and rec.events[-1]["type"] == "visibility"

    def test_finalize_from_disk_when_the_daemon_died(self, tmp_path):
        rec, session_dir, out_dir = _recorder(tmp_path)
        rec.begin()
        rec.handle_command({"cmd": "mark", "label": "Open"})
        (session_dir / "frames.jsonl").write_text(
            json.dumps(
                {"seq": 1, "t": 1001.0, "file": "frames/000001.jpg", "forced": True}
            )
            + "\n"
        )
        timeline = recorder_cli.finalize_from_disk(session_dir)
        assert timeline["session"]["stop_reason"] == "daemon_dead"
        assert timeline["events"][-1]["type"] == "stop"
        assert (session_dir / "timeline.json").exists()
        assert not (out_dir / "current.json").exists()


@pytest.mark.unit
class TestCliPlumbing:
    def test_session_dir_name_is_safe_and_stamped(self):
        from datetime import datetime

        name = recorder_cli.session_dir_name(
            "IDP dev/4", datetime(2026, 9, 11, 13, 5, 9)
        )
        assert name == "IDP-dev-4-20260911-130509"

    def test_missing_current_points_at_start(self, tmp_path):
        args = MagicMock(session=None, out=str(tmp_path))
        with pytest.raises(SystemExit, match="ux_recorder.py start"):
            recorder_cli._resolve_session(args)

    def test_current_json_lifecycle(self, tmp_path):
        recorder_cli.write_current(
            tmp_path, {"session_dir": str(tmp_path / "s"), "pid": 1}
        )
        assert recorder_cli.read_current(tmp_path)["pid"] == 1
        recorder_cli.clear_current(tmp_path, tmp_path / "other")
        assert recorder_cli.read_current(tmp_path) is not None
        recorder_cli.clear_current(tmp_path, tmp_path / "s")
        assert recorder_cli.read_current(tmp_path) is None

    def test_socket_path_is_short_enough_for_macos(self):
        from datetime import datetime

        assert len(recorder_cli.socket_path_for(datetime.now(), 99999)) < 100


def _make_session(tmp_path: Path, with_narration: bool) -> Path:
    from PIL import Image

    session_dir = tmp_path / "stack-20260911-120000"
    (session_dir / "frames").mkdir(parents=True)
    colours = ["#ff0000", "#00ff00", "#0000ff", "#ffff00"]
    times = [1000.0, 1000.5, 1010.0, 1010.3]
    lines = []
    for i, (colour, t) in enumerate(zip(colours, times), start=1):
        Image.new("RGB", (321, 181), colour).save(
            session_dir / "frames" / f"{i:06d}.jpg"
        )
        lines.append(
            json.dumps(
                {"seq": i, "t": t, "file": f"frames/{i:06d}.jpg", "forced": i in (1, 3)}
            )
        )
    (session_dir / "frames.jsonl").write_text("\n".join(lines) + "\n")
    events = [
        {
            "seq": 0,
            "t": 1000.0,
            "type": "start",
            "label": "Start",
            "say": "Hello there." if with_narration else None,
            "frame": 1,
        },
        {
            "seq": 1,
            "t": 1010.0,
            "type": "mark",
            "label": "Open",
            "say": "We open the queue." if with_narration else None,
            "frame": 3,
        },
        {
            "seq": 2,
            "t": 1011.0,
            "type": "stop",
            "say": "Bye now." if with_narration else None,
            "frame": 4,
        },
    ]
    timeline = {
        "version": 1,
        "session": {
            "stack": "stack",
            "persona": "Admin",
            "flows": ["5.1"],
            "started": 1000.0,
            "ended": 1011.0,
        },
        "events": events,
        "frames_file": "frames.jsonl",
        "frame_count": 4,
        "frame_bytes": 1,
    }
    (session_dir / "timeline.json").write_text(json.dumps(timeline))
    (session_dir / "review.md").write_text(
        render.review_skeleton("stack", "Admin", "2026-09-11", ["5.1"]).replace(
            "Findings                                    (ranked; suggestion, not a demand)\n  \n",
            "Findings                                    (ranked; suggestion, not a demand)\n  5.1  Button unclear → label it\n",
        )
    )
    return session_dir


def _fake_polly_client(tmp_path: Path):
    mp3 = tmp_path / "tone.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1.5",
            "-ar",
            "24000",
            "-ac",
            "1",
            str(mp3),
        ],
        check=True,
    )
    client = MagicMock()
    client.synthesize_speech.side_effect = lambda **kw: {
        "AudioStream": io.BytesIO(mp3.read_bytes())
    }
    return client


@pytest.mark.unit
@pytest.mark.skipif(not (HAS_FFMPEG and HAS_PIL), reason="needs ffmpeg and Pillow")
class TestRenderEndToEnd:
    def test_dry_run_writes_plans_but_no_video(self, tmp_path):
        session_dir = _make_session(tmp_path, with_narration=True)
        out = render.render_session(
            session_dir, render.RenderOptions(dry_run=True), log=lambda m: None
        )
        assert out is None
        assert (session_dir / "segments.json").exists()
        assert (session_dir / "review.srt").exists()
        assert (session_dir / "render" / "sequence.txt").exists()
        assert not (session_dir / "render" / "seq").exists()
        assert not (session_dir / "review.mp4").exists()
        assert (session_dir / "cards" / "title.jpg").exists()

    def test_full_render_produces_a_captioned_mp4_of_the_planned_length(self, tmp_path):
        session_dir = _make_session(tmp_path, with_narration=True)
        client = _fake_polly_client(tmp_path)
        out = render.render_session(
            session_dir,
            render.RenderOptions(),
            log=lambda m: None,
            client_factory=lambda region: client,
        )
        assert out is not None and out.exists()
        plan = json.loads((session_dir / "segments.json").read_text())
        assert render.probe_duration(out) == pytest.approx(
            plan["total_seconds"], abs=0.35
        )
        assert plan["width"] % 2 == 0 and plan["height"] % 2 == 0
        streams = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        info = json.loads(streams)["streams"]
        subs = [s for s in info if s["codec_type"] == "subtitle"]
        assert subs and subs[0]["codec_name"] == "mov_text"
        assert subs[0]["tags"]["language"] == "eng"
        assert client.synthesize_speech.call_count == 3
        assert "00:00  Title" in (session_dir / "review.md").read_text()
        assert not (session_dir / "render" / "seq").exists()
        assert len(list((session_dir / "narration").glob("*.mp3"))) == 3

    def test_render_without_narration_has_no_subtitle_stream(self, tmp_path):
        session_dir = _make_session(tmp_path, with_narration=False)
        out = render.render_session(
            session_dir,
            render.RenderOptions(no_narration=True, no_cards=True),
            log=lambda m: None,
        )
        info = json.loads(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )["streams"]
        assert not [s for s in info if s["codec_type"] == "subtitle"]
        assert not (session_dir / "cards").exists()
