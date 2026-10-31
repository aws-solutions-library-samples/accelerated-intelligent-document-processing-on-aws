# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the recorder's click intake (scripts/ux_recorder.py).

In the unattended MR UX test the recorded tab runs the MR's own UI code, so the
intake is pinned from the attacker's side, in three layers. The overlay and its
``__uxRecorderEvent`` binding are installed in an isolated world, which the page's
own script cannot reach, and the viewport that bounds a click's coordinates is
measured there. Every DevTools message is bounded before it is parsed: one longer
than the cap is read off the socket and dropped without ever being held. And a
report that still arrives is untrusted input: one that is malformed, out of bounds
or oversized is refused and counted; a flood is cut off by the rate window and the
session cap; and a report that arrives while a command holds the recorder never
blocks the DevTools reader, which is the thread that delivers that command's
screenshot. What a kept click records is pinned by
``scripts/tests/test_ux_recorder.py``, which must stay green with this module.

Nothing here needs Chrome, ffmpeg, node or a network. The reader thread is played
by a second Python thread, because ``Recorder.lock`` is re-entrant and a report
from the thread already holding it would take the inline path. The transport tests
feed the real ``WebSocketClient`` from a fake socket that makes payload bytes as
they are read, so a message far larger than the cap costs the test no memory.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import queue
import re
import struct
import sys
import threading
import time
import tracemalloc
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cdp = _load("ux_recorder_cdp")
render = _load("ux_recorder_render")
recorder_cli = _load("ux_recorder")

VIEWPORT = {"w": 1600, "h": 1000}
LINK = 'a "Open the set"'


class FakeScreencast:
    def __init__(self) -> None:
        self.count = 0
        self.bytes = 0
        self.stopped = False

    def capture_now(self) -> int:
        self.count += 1
        return self.count

    def ack(self, params: dict[str, Any]) -> None:
        pass

    def handle_frame(self, params: dict[str, Any]) -> int:
        return self.capture_now()

    def stop(self) -> None:
        self.stopped = True


class ReplyAfterAClick(FakeScreencast):
    """A screenshot whose reply arrives on the reader thread behind a click report.

    That is the order Chrome can deliver them in while a ``mark`` holds the
    recorder. The reply is a threading event the reader sets once it has handed
    the report over, so a reader that blocks on the recorder's lock never sets it
    and the capture times out, as the real one does after 15 s.
    """

    def __init__(self, deliver_report: Callable[[], None]) -> None:
        super().__init__()
        self.deliver_report = deliver_report
        self.armed = True
        self.waited: float | None = None

    def capture_now(self) -> int:
        if not self.armed:
            return super().capture_now()
        self.armed = False
        replied = threading.Event()

        def reader() -> None:
            self.deliver_report()
            replied.set()

        started = time.monotonic()
        threading.Thread(target=reader, daemon=True).start()
        arrived = replied.wait(5.0)
        self.waited = time.monotonic() - started
        if not arrived:
            raise recorder_cli.cdp.CdpError("Page.captureScreenshot: no reply")
        return super().capture_now()


class TickClock:
    """The tick clock of ``test_ux_recorder.py``'s ``_recorder``, counting reads."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.reads = 0

    def __call__(self) -> float:
        self.now += 1.0
        self.reads += 1
        return self.now


class RateClock:
    """A stand-in for ``time.monotonic`` that the test sets, or that steps per read."""

    def __init__(self, now: float = 0.0, step: float = 0.0) -> None:
        self.now = now
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def _recorder(
    tmp_path: Path, rate_clock: Callable[[], float] | None = None
) -> tuple[Any, Path, Path]:
    """``test_ux_recorder.py``'s construction, with both clocks injectable.

    The default rate clock steps ten seconds per read, so every click falls in a
    window of its own and only the test that is about the rate window meets it.
    """
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
    rec = recorder_cli.Recorder(
        session_dir,
        meta,
        clock=TickClock(),
        rate_clock=rate_clock or RateClock(step=10.0),
    )
    rec.screencast = FakeScreencast()
    return rec, session_dir, out_dir


def click_json(**fields: Any) -> str:
    """A click report as the overlay sends it, with ``fields`` replaced."""
    payload: dict[str, Any] = {
        "kind": "click",
        "x": 440,
        "y": 321,
        "button": 0,
        "target": LINK,
        "under": LINK,
        "interactive": LINK,
        "scrollX": 0,
        "scrollY": 0,
    }
    payload.update(fields)
    return json.dumps(payload)


def click_of_length(length: int) -> str:
    """A valid click report exactly ``length`` characters long."""
    padding = length - len(click_json(target=""))
    payload = click_json(target="a" * padding)
    assert len(payload) == length
    return payload


def deliver(rec: Any, payload: Any) -> None:
    """What the DevTools reader does with one ``Runtime.bindingCalled`` event."""
    rec._on_event(
        "Runtime.bindingCalled", {"name": cdp.CLICK_BINDING, "payload": payload}
    )


def clicks_of(rec: Any) -> list[dict[str, Any]]:
    return [e for e in rec.events if e["type"] == "click"]


@contextlib.contextmanager
def held_elsewhere(lock: Any) -> Iterator[None]:
    """``lock`` held by another thread for the duration of the block."""
    taken = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with lock:
            taken.set()
            release.wait(30)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        if not taken.wait(10):
            raise RuntimeError("the holding thread never took the lock")
        yield
    finally:
        release.set()
        holder.join(10)


@pytest.mark.unit
def test_the_intake_bounds_are_pinned_and_the_docstring_explains_them() -> None:
    """Pinned, because each is a bound an MR's page code is held to."""
    assert recorder_cli.CLICK_PAYLOAD_MAX_CHARS == 4096
    assert recorder_cli.CLICK_LABEL_MAX_CHARS == 256
    assert recorder_cli.CLICK_COORD_MAX == 16384
    assert recorder_cli.CLICK_RATE_MAX == 20
    assert recorder_cli.CLICK_RATE_WINDOW_SECONDS == 1.0
    assert recorder_cli.CLICKS_MAX_PER_SESSION == 2000
    assert recorder_cli.CLICK_BACKLOG_MAX == 64
    prose = " ".join((recorder_cli.__doc__ or "").split())
    assert "Why click reports are bounded" in prose
    assert "isolated world" in prose and "MESSAGE_MAX_BYTES" in prose
    assert "could therefore at most draw a bounded, labelled marker" in (prose.lower())


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(click_json(x="440"), id="x-as-text"),
        pytest.param(click_json(x=True), id="x-true"),
        pytest.param(click_json(x=float("nan")), id="x-nan"),
        pytest.param(click_json(x=float("inf")), id="x-inf"),
        pytest.param(click_json(y=float("-inf")), id="y-minus-inf"),
        pytest.param(click_json(x=-1), id="x-negative"),
        pytest.param(click_json(x=1601), id="x-past-the-viewport-width"),
        pytest.param(click_json(y=1000.5), id="y-past-the-viewport-height"),
        pytest.param(click_json(x=None), id="x-missing"),
        pytest.param(click_json(target={"text": "Open"}), id="dict-target"),
        pytest.param(click_json(under=["Open"]), id="list-under"),
        pytest.param(click_json(interactive=7), id="number-interactive"),
        pytest.param(click_json(kind="scroll"), id="another-kind"),
        pytest.param(json.dumps(["click", 440, 321]), id="json-array"),
        pytest.param("not json", id="text"),
        pytest.param(click_of_length(5000), id="5000-characters"),
        pytest.param("[" * 3000, id="nested-3000-deep"),
        pytest.param(None, id="none"),
        pytest.param(b'{"kind": "click", "x": 1, "y": 1}', id="bytes"),
    ],
)
def test_a_malformed_or_out_of_bounds_report_is_refused(payload: Any) -> None:
    assert recorder_cli.parse_click(payload, VIEWPORT) is None


@pytest.mark.unit
def test_a_recursion_error_from_the_parser_is_a_refusal_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``RecursionError`` branch, which the nested case above reaches only on
    CPython 3.11 and earlier: 3.12 and later raise ``ValueError`` at that depth,
    and the length cap stops anything deep enough to reach their limit."""

    def too_deep(_text: str) -> Any:
        raise RecursionError("maximum recursion depth exceeded while decoding")

    monkeypatch.setattr(recorder_cli, "json", SimpleNamespace(loads=too_deep))
    assert recorder_cli.parse_click(click_json(), VIEWPORT) is None


@pytest.mark.unit
def test_a_report_at_the_length_cap_is_read_and_one_character_more_is_not() -> None:
    kept = recorder_cli.parse_click(click_of_length(4096), VIEWPORT)
    assert kept is not None and len(kept["target"]) == 256
    assert recorder_cli.parse_click(click_of_length(4097), VIEWPORT) is None


@pytest.mark.unit
def test_a_long_label_is_cut_to_256_and_its_control_characters_blanked() -> None:
    label = 'div "' + "x" * 294 + '"'
    kept = recorder_cli.parse_click(
        click_json(
            target=label,
            under="tab\there\nnew\x1b[31mred\u202eflipped\u2028line",
            interactive=None,
        ),
        VIEWPORT,
    )
    assert kept is not None
    assert len(label) == 300 and kept["target"] == label[:256]
    assert kept["under"] == "tab here new [31mred flipped line"
    assert kept["interactive"] is None


@pytest.mark.unit
def test_a_kept_report_keeps_only_its_position_and_labels_as_they_came() -> None:
    kept = recorder_cli.parse_click(click_json(), VIEWPORT)
    assert kept == {
        "x": 440,
        "y": 321,
        "target": LINK,
        "under": LINK,
        "interactive": LINK,
    }
    assert type(kept["x"]) is int
    fractional = recorder_cli.parse_click(click_json(x=440.5, y=0), VIEWPORT)
    assert fractional is not None and fractional["x"] == 440.5
    assert recorder_cli.parse_click(click_json(x=1600, y=1000), VIEWPORT) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    "viewport",
    [
        pytest.param({}, id="not-measured"),
        pytest.param({"w": 10**9, "h": 10**9}, id="larger-than-any-screen"),
        pytest.param({"w": "1600", "h": "1000"}, id="text"),
        pytest.param({"w": True, "h": True}, id="bool"),
        pytest.param({"w": 0, "h": -5}, id="not-positive"),
        pytest.param({"w": float("nan"), "h": float("inf")}, id="not-finite"),
    ],
)
def test_an_unusable_viewport_bounds_coordinates_by_the_fixed_ceiling(
    viewport: dict[str, Any],
) -> None:
    assert recorder_cli.parse_click(click_json(x=16384, y=2000), viewport) is not None
    assert recorder_cli.parse_click(click_json(x=16385), viewport) is None
    assert recorder_cli.parse_click(click_json(y=10**6), viewport) is None


@pytest.mark.unit
def test_the_twenty_first_click_in_one_window_is_rejected_and_the_window_slides(
    tmp_path: Path,
) -> None:
    rate = RateClock()
    rec, _, _ = _recorder(tmp_path, rate_clock=rate)
    for _ in range(10):
        deliver(rec, click_json())
    rate.now = 0.5
    for _ in range(10):
        deliver(rec, click_json())
    rate.now = 0.9
    deliver(rec, click_json())
    assert len(clicks_of(rec)) == 20 and rec.status()["clicks_rejected"] == 1

    rate.now = 1.0
    for _ in range(11):
        deliver(rec, click_json())
    assert len(clicks_of(rec)) == 30 and rec.status()["clicks_rejected"] == 2

    rate.now = 1.5
    for _ in range(11):
        deliver(rec, click_json())
    assert len(clicks_of(rec)) == 40 and rec.status()["clicks_rejected"] == 3


@pytest.mark.unit
def test_the_session_keeps_at_most_two_thousand_clicks(tmp_path: Path) -> None:
    rec, _, _ = _recorder(tmp_path)
    for _ in range(recorder_cli.CLICKS_MAX_PER_SESSION):
        deliver(rec, click_json())
    deliver(rec, click_json())
    deliver(rec, click_json())
    status = rec.status()
    assert status["clicks"] == 2000 and status["clicks_rejected"] == 2


@pytest.mark.unit
def test_a_report_never_waits_for_a_command_that_holds_the_recorder(
    tmp_path: Path,
) -> None:
    rec, _, _ = _recorder(tmp_path)
    with held_elsewhere(rec.lock):
        started = time.monotonic()
        deliver(rec, click_json())
        returned_after = time.monotonic() - started
        arrived = rec.clock.now
        assert clicks_of(rec) == []
    assert returned_after < 0.5
    assert clicks_of(rec) == []

    note = rec.handle_command({"cmd": "note", "text": "after the click"})
    (click,) = clicks_of(rec)
    assert click["t"] == arrived and arrived < note["t"]
    assert click["seq"] == note["seq"] + 1
    assert click["target"] == LINK and click["x"] == 440


@pytest.mark.unit
def test_a_mark_is_not_stalled_by_a_click_reported_during_its_screenshot(
    tmp_path: Path,
) -> None:
    """The stall this intake exists to remove.

    Before it, the reader blocked on the lock the mark held across its
    screenshot, so the screenshot's reply could not be read, the capture timed out
    and the mark's segment began without its first frame.
    """
    rec, _, _ = _recorder(tmp_path)
    rec.begin()
    screenshot = ReplyAfterAClick(lambda: deliver(rec, click_json()))
    rec.screencast = screenshot
    reply = rec.handle_command({"cmd": "mark", "label": "Open", "say": "We open."})
    assert reply["ok"] and reply["frame"] is not None
    assert screenshot.waited is not None and screenshot.waited < 1.0
    mark = next(e for e in rec.events if e["type"] == "mark")
    (click,) = clicks_of(rec)
    assert click["t"] < mark["t"] and click["seq"] > mark["seq"]
    assert click["interactive"] == LINK


@pytest.mark.unit
def test_a_click_reported_during_the_final_screenshot_reaches_the_timeline(
    tmp_path: Path,
) -> None:
    rec, session_dir, _ = _recorder(tmp_path)
    rec.begin()
    rec.screencast = ReplyAfterAClick(lambda: deliver(rec, click_json(x=17)))
    assert rec.handle_command({"cmd": "stop", "say": "Bye."})["ok"]
    timeline = json.loads((session_dir / "timeline.json").read_text())
    stop = next(e for e in timeline["events"] if e["type"] == "stop")
    clicks = [e for e in timeline["events"] if e["type"] == "click"]
    assert [c["x"] for c in clicks] == [17] and clicks[0]["t"] < stop["t"]


@pytest.mark.unit
def test_a_full_backlog_rejects_the_overflow_and_keeps_the_rest_in_order(
    tmp_path: Path,
) -> None:
    rec, _, _ = _recorder(tmp_path)
    backlog = recorder_cli.CLICK_BACKLOG_MAX
    arrivals = []
    with held_elsewhere(rec.lock):
        for x in range(backlog + 5):
            deliver(rec, click_json(x=x))
            arrivals.append(rec.clock.now)
        assert rec.status()["clicks_rejected"] == 5
    rec.handle_command({"cmd": "status"})
    kept = clicks_of(rec)
    assert [c["x"] for c in kept] == list(range(backlog))
    assert [c["t"] for c in kept] == arrivals[:backlog]
    assert rec.status()["clicks_rejected"] == 5


@pytest.mark.unit
def test_a_report_too_long_to_read_takes_no_place_in_the_backlog(
    tmp_path: Path,
) -> None:
    rec, _, _ = _recorder(tmp_path)
    with held_elsewhere(rec.lock):
        deliver(rec, "x" * (recorder_cli.CLICK_PAYLOAD_MAX_CHARS + 1))
        deliver(rec, 12345)
        reads_while_held = rec.clock.reads
    assert reads_while_held == 0
    rec.handle_command({"cmd": "status"})
    assert clicks_of(rec) == [] and rec.status()["clicks_rejected"] == 2


@pytest.mark.unit
def test_a_click_that_waited_while_paused_is_recorded_without_its_labels(
    tmp_path: Path,
) -> None:
    rec, _, _ = _recorder(tmp_path)
    rec.handle_command({"cmd": "pause"})
    with held_elsewhere(rec.lock):
        deliver(rec, click_json())
        deliver(rec, click_json(interactive=None))
        arrived = rec.clock.now
    rec.handle_command({"cmd": "status"})
    clicks = clicks_of(rec)
    assert [sorted(c) for c in clicks] == [
        ["paused", "seq", "t", "type"],
        ["off_target", "paused", "seq", "t", "type"],
    ]
    assert clicks[1]["t"] == arrived


@pytest.mark.unit
def test_a_click_recorded_as_it_arrives_reads_the_clock_once(tmp_path: Path) -> None:
    """The inline path adds no clock read, which is what keeps the tick-clock
    timings that ``test_ux_recorder.py`` pins where they were."""
    rec, _, _ = _recorder(tmp_path)
    deliver(rec, click_json())
    assert rec.clock.reads == 1 and clicks_of(rec)[0]["t"] == rec.clock.now
    deliver(rec, "not json")
    deliver(rec, click_json(x=True))
    assert rec.clock.reads == 1


@pytest.mark.unit
def test_clicks_rejected_counts_every_kind_of_refusal(tmp_path: Path) -> None:
    rate = RateClock()
    rec, session_dir, _ = _recorder(tmp_path, rate_clock=rate)
    deliver(rec, "not json")
    deliver(rec, click_json(target={"text": "forged"}))
    for _ in range(recorder_cli.CLICK_RATE_MAX + 2):
        deliver(rec, click_json())
    assert rec.status()["clicks_rejected"] == 4

    with held_elsewhere(rec.lock):
        for _ in range(recorder_cli.CLICK_BACKLOG_MAX + 3):
            deliver(rec, click_json())
        assert rec.status()["clicks_rejected"] == 7
    rate.now = 5.0
    rec.handle_command({"cmd": "status"})
    status = rec.status()
    assert status["clicks"] == 40
    assert status["clicks_rejected"] == 7 + recorder_cli.CLICK_BACKLOG_MAX - 20

    rec.handle_command({"cmd": "stop"})
    timeline = json.loads((session_dir / "timeline.json").read_text())
    assert timeline["session"]["clicks_rejected"] == status["clicks_rejected"]


@pytest.mark.unit
def test_a_timeline_of_kept_clicks_renders_the_click_table(tmp_path: Path) -> None:
    """``render.click_table`` turns every coordinate into an ``int``, so a
    coordinate that is not a number used to crash the render after the session.
    Refused reports never reach it, and the kept ones render."""
    rec, session_dir, _ = _recorder(tmp_path)
    rec.begin()
    rec.handle_command({"cmd": "mark", "label": "Open the set", "say": "We open it."})
    deliver(rec, click_json(x=440.5, y=321.25))
    deliver(
        rec,
        click_json(
            x=0,
            y=0,
            target='div "Console"',
            under='div "Console"',
            interactive=None,
        ),
    )
    deliver(
        rec,
        click_json(
            target="\x1b[2J" + "z" * 300,
            under="line\u2028break\x1b[2J",
            interactive=None,
        ),
    )
    deliver(rec, click_json(x={"left": 1}))
    deliver(rec, click_json(y="321"))
    rec.handle_command({"cmd": "stop"})
    timeline = json.loads((session_dir / "timeline.json").read_text())
    events = [render.Event.from_record(e) for e in timeline["events"]]
    table, warnings = render.click_table(events)
    rows = table.splitlines()
    assert len(rows) == 4
    assert "440,321" in rows[1] and LINK in rows[1]
    assert " 0,0 " in rows[2] and "nothing interactive" in rows[2]
    assert "line break [2J" in rows[3]
    assert len(warnings) == 2 and 'div "Console"' in warnings[0]
    rendered = table + "\n".join(warnings)
    assert "\x1b" not in rendered and "\u2028" not in rendered
    assert timeline["session"]["clicks_rejected"] == 2


@pytest.mark.unit
def test_the_rate_window_runs_on_the_monotonic_clock_by_default(tmp_path: Path) -> None:
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    rec = recorder_cli.Recorder(session_dir, {"started": 1.0})
    assert rec.rate_clock is time.monotonic and rec.clicks_rejected == 0


def _frame_header(opcode: int, length: int, *, fin: bool = True) -> bytes:
    """A server frame's header for a payload of ``length`` bytes, unmasked."""
    head = bytearray([(0x80 if fin else 0) | opcode])
    if length < 126:
        head.append(length)
    elif length < 65536:
        head.append(126)
        head += struct.pack("!H", length)
    else:
        head.append(127)
        head += struct.pack("!Q", length)
    return bytes(head)


def _frame(text: str, *, opcode: int = 0x1, fin: bool = True) -> bytes:
    data = text.encode("utf-8")
    return _frame_header(opcode, len(data), fin=fin) + data


def binding_message(payload: str) -> str:
    """The ``Runtime.bindingCalled`` event Chrome sends for one overlay report."""
    return json.dumps(
        {
            "method": "Runtime.bindingCalled",
            "params": {
                "name": cdp.CLICK_BINDING,
                "payload": payload,
                "executionContextId": 7,
            },
        }
    )


class LazySocket:
    """A socket serving ``parts`` in order, where an ``int`` part is that many
    bytes of filler made as they are read and never stored, so a message far past
    the cap costs the test nothing to send."""

    def __init__(self, *parts: bytes | int) -> None:
        self.parts: deque[bytes | int] = deque(parts)
        self.sent = bytearray()

    def recv(self, n: int) -> bytes:
        while self.parts:
            head = self.parts.popleft()
            if isinstance(head, int):
                if head <= 0:
                    continue
                take = min(n, head)
                if head > take:
                    self.parts.appendleft(head - take)
                return b"x" * take
            if not head:
                continue
            if len(head) > n:
                self.parts.appendleft(head[n:])
            return head[:n]
        return b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, _timeout: float | None) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.mark.unit
def test_a_binding_message_past_the_cap_is_dropped_without_being_held() -> None:
    """Read off the socket a chunk at a time and kept nowhere: the peak the
    client allocates stays a small fraction of the message, and the next message
    is the one returned."""
    marker = "PAYLOAD-GOES-HERE"
    prefix, suffix = binding_message(marker).split(marker)
    total = cdp.MESSAGE_MAX_BYTES + 1024 * 1024
    after = binding_message(click_json(x=7))
    sock = LazySocket(
        _frame_header(0x1, total),
        prefix.encode(),
        total - len(prefix) - len(suffix),
        suffix.encode(),
        _frame(after),
    )
    client = cdp.WebSocketClient(sock, "127.0.0.1:9222", "/devtools/page/T1")
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()
    baseline, _ = tracemalloc.get_traced_memory()
    try:
        message = client.recv_message()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not tracing:
            tracemalloc.stop()
    assert message == after
    assert client.dropped == 1
    held = peak - baseline
    assert held < 8 * 1024 * 1024, f"held {held} bytes of a {total}-byte message"


@pytest.mark.unit
def test_the_cap_counts_every_fragment_of_one_message() -> None:
    """A message at the cap is kept. One whose fragments are each small but add
    up past it is dropped whole, and a ping between its fragments is still
    answered."""
    cap = 64
    sock = LazySocket(
        _frame("a" * cap),
        _frame("b" * 40, fin=False),
        _frame_header(0x9, 1) + b"p",
        _frame("c" * 25, opcode=0x0),
        _frame("kept"),
    )
    client = cdp.WebSocketClient(sock, "h", "/", max_message_bytes=cap)
    assert client.recv_message() == "a" * cap
    assert client.recv_message() == "kept"
    assert client.dropped == 1
    assert bytes(sock.sent)[0] == 0x80 | 0xA


@pytest.mark.unit
@pytest.mark.parametrize(
    "frames",
    [
        pytest.param(_frame("orphan", opcode=0x0), id="continuation-without-a-start"),
        pytest.param(_frame_header(0x9, 126) + b"p" * 126, id="control-over-125"),
        pytest.param(_frame_header(0x9, 1, fin=False) + b"p", id="fragmented-control"),
        pytest.param(_frame("one", fin=False) + _frame("two"), id="message-in-message"),
        pytest.param(_frame("?", opcode=0x3), id="reserved-opcode"),
    ],
)
def test_a_protocol_violation_ends_the_connection_rather_than_raising(
    frames: bytes,
) -> None:
    """Before, these raised out of ``recv_message`` and killed the reader thread
    without marking the session closed, so every pending call waited out its
    timeout."""
    client = cdp.WebSocketClient(LazySocket(frames, _frame("never read")), "h", "/")
    assert client.recv_message() is None
    assert client.recv_message() is None


@pytest.mark.unit
def test_the_reader_drops_an_oversized_report_and_records_the_next_click(
    tmp_path: Path,
) -> None:
    rec, _, _ = _recorder(tmp_path)
    cap = 16 * 1024
    sock = LazySocket(
        _frame(binding_message(click_json(target="t" * cap))),
        _frame("[1, 2, 3]"),
        _frame("{not json"),
        _frame("[" * 3000 + "]" * 3000),
        _frame(binding_message(click_json(x=9))),
    )
    client = recorder_cli.cdp.WebSocketClient(sock, "h", "/", max_message_bytes=cap)
    session = recorder_cli.cdp.CdpSession(client, on_event=rec._on_event)
    rec.cdp = session
    session.start_reader()
    assert session.closed.wait(10), "the reader stopped before the stream ended"
    assert [c["x"] for c in clicks_of(rec)] == [9]
    status = rec.status()
    assert status["cdp_messages_dropped"] == 1 and status["clicks_rejected"] == 0


class WorldSession:
    """A page with one isolated world, answering ``Runtime.evaluate`` by world.

    In the recorder's context the viewport is the browser's real one; in the
    page's own world it is the one a hostile page pretends to have, as large as
    the recorder's fixed ceiling, so a test can tell which world was read.
    """

    CONTEXT = 7

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params or {})))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1"}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": self.CONTEXT}
        if method == "Page.addScriptToEvaluateOnNewDocument":
            return {"identifier": "s1"}
        if method == "Runtime.evaluate":
            size = 1600 if (params or {}).get("contextId") == self.CONTEXT else 16384
            return {"result": {"value": {"w": size, "h": size * 5 // 8, "dpr": 1}}}
        return {}


@pytest.mark.unit
def test_neither_the_binding_nor_the_overlay_ever_enters_the_pages_own_world() -> None:
    session = WorldSession()
    assert cdp.install_cursor_overlay(session) == "s1"
    cdp.remove_cursor_overlay(session, "s1")
    world = cdp.OVERLAY_WORLD
    methods = [method for method, _ in session.calls]
    assert methods.count("Runtime.addBinding") == 1
    for method, params in session.calls:
        if method == "Runtime.addBinding":
            assert params == {"name": cdp.CLICK_BINDING, "executionContextName": world}
        elif method in (
            "Page.addScriptToEvaluateOnNewDocument",
            "Page.createIsolatedWorld",
        ):
            assert params["worldName"] == world, method
        elif method == "Runtime.evaluate":
            assert params.get("contextId") == WorldSession.CONTEXT, params
    evaluated = [p["expression"] for m, p in session.calls if m == "Runtime.evaluate"]
    assert evaluated == [cdp.CURSOR_OVERLAY_JS, cdp.CURSOR_REMOVE_JS]


@pytest.mark.unit
def test_the_viewport_is_read_in_the_recorders_world_or_not_at_all() -> None:
    session = WorldSession()
    measured = cdp.measure_viewport(session)
    ((_, params),) = [(m, p) for m, p in session.calls if m == "Runtime.evaluate"]
    assert params == {
        "expression": cdp.VIEWPORT_JS,
        "contextId": WorldSession.CONTEXT,
        "returnByValue": True,
    }
    assert recorder_cli.parse_click(click_json(x=1601), measured) is None

    class NoFrame(WorldSession):
        def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> dict[str, Any]:
            answer = super().call(method, params, timeout)
            return {} if method == "Page.getFrameTree" else answer

    class Refused(WorldSession):
        def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> dict[str, Any]:
            answer = super().call(method, params, timeout)
            if method == "Runtime.evaluate":
                raise cdp.CdpError("Runtime.evaluate: no reply")
            return answer

    no_frame, refused = NoFrame(), Refused()
    assert cdp.measure_viewport(no_frame) == {}
    assert cdp.measure_viewport(refused) == {}
    assert [m for m, _ in no_frame.calls] == ["Page.getFrameTree"], (
        "with no frame there is no isolated world, so nothing may be evaluated"
    )
    assert [m for m, _ in refused.calls] == [
        "Page.getFrameTree",
        "Page.createIsolatedWorld",
        "Runtime.evaluate",
    ]


class QueueWs:
    """A DevTools WebSocket whose replies come back through the reader thread."""

    def __init__(self, session: WorldSession) -> None:
        self.session = session
        self.inbox: queue.Queue[str | None] = queue.Queue()
        self.sent: list[dict[str, Any]] = []

    def send_text(self, text: str) -> None:
        message = json.loads(text)
        self.sent.append(message)
        result = self.session.call(message["method"], message.get("params"))
        self.inbox.put(json.dumps({"id": message["id"], "result": result}))

    def recv_message(self) -> str | None:
        return self.inbox.get()

    def close(self) -> None:
        self.inbox.put(None)


@pytest.mark.unit
def test_attach_bounds_clicks_by_the_viewport_its_own_world_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec, _, _ = _recorder(tmp_path)
    rec.meta["ws_url"] = "ws://127.0.0.1:9222/devtools/page/T1"
    ws = QueueWs(WorldSession())
    monkeypatch.setattr(
        recorder_cli.cdp.WebSocketClient,
        "connect",
        staticmethod(lambda url, timeout=10.0: ws),
    )
    rec.attach()
    try:
        deliver(rec, click_json(x=1601))
        deliver(rec, click_json(x=1600, y=1000))
    finally:
        rec.cdp.close()
    assert [c["x"] for c in clicks_of(rec)] == [1600]
    assert rec.status()["clicks_rejected"] == 1
    evaluations = [m["params"] for m in ws.sent if m["method"] == "Runtime.evaluate"]
    assert len(evaluations) == 2
    assert all(p.get("contextId") == WorldSession.CONTEXT for p in evaluations)
    methods = [m["method"] for m in ws.sent]
    assert methods.index("Page.enable") < methods.index("Runtime.addBinding"), (
        "the overlay's script for new documents runs only once the Page domain is "
        "enabled, so without it no click after the first navigation is reported"
    )


@pytest.mark.unit
def test_the_overlay_acts_only_on_events_the_browser_marks_trusted() -> None:
    """A script can dispatch a ``mousedown`` the overlay's listener receives, so
    every input listener goes through ``trusted``. The node harness in
    ``test_ux_recorder.py`` runs the overlay with plain-object events, which is
    why the test is ``!== false``: in a browser every event has the property."""
    js = cdp.CURSOR_OVERLAY_JS
    assert "const trusted = (e) => e.isTrusted !== false;" in js
    listeners = re.findall(r"addEventListener\('(\w+)', (\w+), true\)", js)
    assert listeners == [
        ("pointermove", "onMove"),
        ("mousemove", "onMove"),
        ("mousedown", "onDown"),
    ]
    on_move = re.search(r"const onMove = \(e\) => \{ (.*?) \};", js)
    on_down = re.search(r"const onDown = \(e\) => \{\n\s*(.*?)\n", js)
    assert on_move and on_move.group(1).startswith("if (trusted(e))")
    assert on_down and on_down.group(1) == "if (!trusted(e)) { return; }"


@pytest.mark.unit
def test_a_label_is_bounded_however_long_the_pages_tag_or_id() -> None:
    js = cdp.CURSOR_OVERLAY_JS
    assert "el.tagName.toLowerCase().slice(0, LABEL_LENGTH)" in js
    assert "'#' + String(el.id).slice(0, LABEL_LENGTH)" in js
    prose = " ".join((cdp.__doc__ or "").split())
    assert "Why the overlay runs in an isolated world" in prose
    assert "Why a message has a size bound" in prose
