#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
ux_recorder.py

Record a browser UX review as a narrated, captioned video.

The review itself is still done by the agent following ``.claude/skills/ux-test.md``:
it drives the debug Chrome through the chrome-devtools MCP server and forms the
usability judgement. This script sits beside that as a sidecar. It attaches a second
DevTools session to the same tab, captures screencast frames while the agent works,
and takes short commands from the agent between steps — ``mark`` with a narration
line before each group of actions, ``note`` for something noticed, ``pause`` and
``resume`` around a long wait, ``stop`` at the end. ``render`` then turns the frames,
the marks and the narration into ``review.mp4`` with Amazon Polly speech and a
subtitle track.

Why a sidecar and not a change to the skill's browser tooling
-------------------------------------------------------------
The MCP server's own screencast writes one continuous file with no timestamps or
marks, and the agent's thinking time between steps would dominate it. Recording
here, with the agent's marks on the same clock as the frames, is what lets
``render`` compress the idle time and pace each segment to its narration.

Why a daemon with a Unix-socket control channel
-----------------------------------------------
The screencast needs a persistent WebSocket, and the agent issues commands as
separate shell invocations minutes apart, so something has to stay alive between
them. A Unix socket under /tmp avoids picking a TCP port (the debug port itself is
a recurring source of collisions), is scoped to the user, and lets ``mark`` block
until its boundary frame has been written so the segment boundary is real.

Why every mark also takes a screenshot
--------------------------------------
Chrome sends a screencast frame only when something on the page changes. A mark on
a static screen would otherwise own no frame at all; the forced capture gives every
segment a crisp first frame taken at the moment the agent said the step began.

Usage (from the repo root; the debug Chrome from the skill must be running):

  ./scripts/ux_recorder.py targets
  ./scripts/ux_recorder.py start --stack <STACK> --persona Admin --flow 5.1 \\
      --url-contains cloudfront --say "We start on the Test Studio sets tab."
  ./scripts/ux_recorder.py mark "Open the annotation queue" --say "We open ..."
  ./scripts/ux_recorder.py pause      # before a long wait
  ./scripts/ux_recorder.py resume
  ./scripts/ux_recorder.py note "Spinner has no label"
  ./scripts/ux_recorder.py stop --say "That ends the review."
  AWS_PROFILE=default ./scripts/ux_recorder.py render --voice Ruth

Everything lands under ``scratch/ux-recordings/<stack>-<timestamp>/`` (gitignored).
Recordings of a live stack show real data; nothing is redacted. Polly receives
only the narration text.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ux_recorder_cdp as cdp  # noqa: E402
import ux_recorder_render as render  # noqa: E402

CURRENT_FILE = "current.json"
SESSION_FILE = "session.json"
EVENTS_FILE = "events.jsonl"
FRAMES_INDEX = "frames.jsonl"
TIMELINE_FILE = "timeline.json"
START_TIMEOUT = 20.0


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_out_dir() -> Path:
    return repo_root() / "scratch" / "ux-recordings"


def session_dir_name(stack: str, now: datetime) -> str:
    safe = (
        "".join(c if c.isalnum() or c in "-_" else "-" for c in stack).strip("-")
        or "stack"
    )
    return f"{safe}-{now.strftime('%Y%m%d-%H%M%S')}"


def socket_path_for(now: datetime, pid: int) -> str:
    return f"/tmp/ux-recorder-{now.strftime('%Y%m%d-%H%M%S')}-{pid}.sock"


def read_current(out_dir: Path) -> dict[str, Any] | None:
    path = out_dir / CURRENT_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_current(out_dir: Path, data: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / CURRENT_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")


def clear_current(out_dir: Path, session_dir: Path | None = None) -> None:
    path = out_dir / CURRENT_FILE
    if not path.exists():
        return
    if session_dir is not None:
        current = read_current(out_dir)
        if current and Path(current.get("session_dir", "")) != Path(session_dir):
            return
    path.unlink(missing_ok=True)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def send_command(
    socket_path: str, command: dict[str, Any], timeout: float = 30.0
) -> dict[str, Any]:
    """One JSON line to the daemon, one JSON line back."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall((json.dumps(command) + "\n").encode("utf-8"))
        buffer = b""
        while b"\n" not in buffer:
            chunk = client.recv(65536)
            if not chunk:
                break
            buffer += chunk
    line = buffer.split(b"\n", 1)[0].decode("utf-8")
    if not line:
        raise RuntimeError("recorder closed the connection without replying")
    return json.loads(line)


class Recorder:
    """Daemon-side state: the CDP session, the frames and the event log."""

    def __init__(
        self, session_dir: Path, meta: dict[str, Any], clock: Any = time.time
    ) -> None:
        self.session_dir = Path(session_dir)
        self.meta = meta
        self.clock = clock
        self.events: list[dict[str, Any]] = []
        self.seq = 0
        self.paused = False
        self.visible = True
        self.viewport: dict[str, Any] = {}
        self.cdp: cdp.CdpSession | None = None
        self.screencast: cdp.Screencast | None = None
        self.overlay_id: str | None = None
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.started = float(meta.get("started") or clock())
        self.stop_reason: str | None = None
        self.finalized = False
        self.events_path = self.session_dir / EVENTS_FILE

    def attach(self) -> None:
        ws = cdp.WebSocketClient.connect(self.meta["ws_url"])
        self.cdp = cdp.CdpSession(ws, on_event=self._on_event)
        self.cdp.start_reader()
        self.cdp.call("Page.enable")
        try:
            self.cdp.call("Page.bringToFront", timeout=5)
        except cdp.CdpError:
            pass
        if self.meta.get("cursor", True):
            self.overlay_id = cdp.install_cursor_overlay(self.cdp)
        try:
            result = self.cdp.call(
                "Runtime.evaluate",
                {
                    "expression": "({w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio, url: location.href})",
                    "returnByValue": True,
                },
                timeout=10,
            )
            self.viewport = result.get("result", {}).get("value", {}) or {}
        except cdp.CdpError:
            self.viewport = {}
        self.screencast = cdp.Screencast(
            self.cdp,
            self.session_dir / "frames",
            self.session_dir / FRAMES_INDEX,
            clock=self.clock,
            quality=int(self.meta.get("quality", 70)),
            max_width=int(self.meta.get("max_width", 1600)),
            every_nth=int(self.meta.get("every_nth", 1)),
        )
        self.screencast.start()

    def _on_event(self, method: str, params: dict[str, Any]) -> None:
        if method == "Page.screencastFrame" and self.screencast is not None:
            if self.paused:
                self.screencast.ack(params)
            else:
                self.screencast.handle_frame(params)
        elif method == "Page.screencastVisibilityChanged":
            visible = bool(params.get("visible", True))
            self.visible = visible
            self._record("visibility", visible=visible)
        elif (
            method == "Runtime.bindingCalled"
            and params.get("name") == cdp.CLICK_BINDING
        ):
            self._record_click(params.get("payload") or "")

    def _record_click(self, payload: str) -> None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict) or data.get("kind") != "click":
            return
        self._record(
            "click",
            x=data.get("x"),
            y=data.get("y"),
            target=data.get("target"),
            under=data.get("under"),
            interactive=data.get("interactive"),
            paused=True if self.paused else None,
        )

    def _record(self, kind: str, **fields: Any) -> dict[str, Any]:
        with self.lock:
            event: dict[str, Any] = {"seq": self.seq, "t": self.clock(), "type": kind}
            self.seq += 1
            event.update({k: v for k, v in fields.items() if v is not None})
            self.events.append(event)
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
            return event

    def _capture(self) -> int | None:
        if self.screencast is None or self.paused:
            return None
        try:
            return self.screencast.capture_now()
        except cdp.CdpError:
            return None

    def begin(self) -> dict[str, Any]:
        frame = self._capture()
        return self._record(
            "start", label="Start", say=self.meta.get("say"), frame=frame
        )

    def status(self) -> dict[str, Any]:
        last_mark = next(
            (e for e in reversed(self.events) if e["type"] == "mark"), None
        )
        return {
            "ok": True,
            "session_dir": str(self.session_dir),
            "pid": os.getpid(),
            "uptime": round(self.clock() - self.started, 1),
            "frames": self.screencast.count if self.screencast else 0,
            "bytes": self.screencast.bytes if self.screencast else 0,
            "paused": self.paused,
            "visible": self.visible,
            "marks": sum(1 for e in self.events if e["type"] == "mark"),
            "clicks": sum(1 for e in self.events if e["type"] == "click"),
            "clicks_off_target": sum(
                1
                for e in self.events
                if e["type"] == "click" and not e.get("interactive")
            ),
            "last_mark": last_mark.get("label") if last_mark else None,
            "page_url": self.meta.get("page_url"),
            "connected": bool(self.cdp and not self.cdp.closed.is_set()),
        }

    def handle_command(self, command: dict[str, Any]) -> dict[str, Any]:
        name = command.get("cmd")
        with self.lock:
            if name == "status":
                return self.status()
            if name == "mark":
                label = (command.get("label") or "").strip()
                if not label:
                    return {"ok": False, "error": "mark needs a label"}
                frame = self._capture()
                event = self._record(
                    "mark", label=label, say=command.get("say"), frame=frame
                )
                return {
                    "ok": True,
                    "seq": event["seq"],
                    "t": event["t"],
                    "frame": frame,
                    "frames": self.screencast.count if self.screencast else 0,
                }
            if name == "note":
                text = (command.get("text") or "").strip()
                if not text:
                    return {"ok": False, "error": "note needs text"}
                event = self._record("note", text=text)
                return {"ok": True, "seq": event["seq"], "t": event["t"]}
            if name == "pause":
                if self.paused:
                    return {"ok": False, "error": "already paused"}
                event = self._record("pause")
                self.paused = True
                return {"ok": True, "seq": event["seq"], "t": event["t"]}
            if name == "resume":
                if not self.paused:
                    return {"ok": False, "error": "not paused"}
                self.paused = False
                event = self._record("resume")
                self._capture()
                return {"ok": True, "seq": event["seq"], "t": event["t"]}
            if name == "stop":
                if self.paused:
                    self.paused = False
                    self._record("resume")
                frame = self._capture()
                event = self._record("stop", say=command.get("say"), frame=frame)
                self.finalize("stop")
                self.stop_event.set()
                return {
                    "ok": True,
                    "seq": event["seq"],
                    "t": event["t"],
                    "session_dir": str(self.session_dir),
                    "frames": self.screencast.count if self.screencast else 0,
                }
            return {"ok": False, "error": f"unknown command {name!r}"}

    def timeline(self, reason: str) -> dict[str, Any]:
        frames = self.screencast.count if self.screencast else 0
        frame_bytes = self.screencast.bytes if self.screencast else 0
        session = {
            "stack": self.meta.get("stack"),
            "persona": self.meta.get("persona"),
            "flows": self.meta.get("flows") or [],
            "started": self.started,
            "ended": self.clock(),
            "page_url": self.meta.get("page_url"),
            "target_id": self.meta.get("target_id"),
            "browser": self.meta.get("browser"),
            "viewport": self.viewport,
            "cursor_overlay": bool(self.meta.get("cursor", True)),
            "capture": {
                k: self.meta.get(k) for k in ("quality", "max_width", "every_nth")
            },
            "stop_reason": reason,
        }
        return {
            "version": 1,
            "session": session,
            "events": list(self.events),
            "frames_file": FRAMES_INDEX,
            "frame_count": frames,
            "frame_bytes": frame_bytes,
        }

    def finalize(self, reason: str) -> None:
        with self.lock:
            if self.finalized:
                return
            self.finalized = True
            self.stop_reason = reason
            if (
                self.screencast is not None
                and self.cdp is not None
                and not self.cdp.closed.is_set()
            ):
                self.screencast.stop()
                cdp.remove_cursor_overlay(self.cdp, self.overlay_id)
            if self.cdp is not None:
                self.cdp.close()
            write_session_outputs(self.session_dir, self.timeline(reason), self.meta)
            clear_current(self.session_dir.parent, self.session_dir)
            sock = self.meta.get("socket")
            if sock:
                Path(sock).unlink(missing_ok=True)


def write_session_outputs(
    session_dir: Path, timeline: dict[str, Any], meta: dict[str, Any]
) -> None:
    """timeline.json plus the editable narration and report skeletons."""
    (session_dir / TIMELINE_FILE).write_text(
        json.dumps(timeline, indent=2), encoding="utf-8"
    )
    events = [render.Event.from_record(e) for e in timeline["events"]]
    narration_path = session_dir / "narration.md"
    if not narration_path.exists():
        narration_path.write_text(render.write_narration_md(events), encoding="utf-8")
    review_path = session_dir / "review.md"
    if not review_path.exists():
        started = float(timeline["session"].get("started") or time.time())
        review_path.write_text(
            render.review_skeleton(
                str(meta.get("stack", "?")),
                str(meta.get("persona", "?")),
                datetime.fromtimestamp(started).strftime("%Y-%m-%d"),
                list(meta.get("flows") or []),
            ),
            encoding="utf-8",
        )


def finalize_from_disk(
    session_dir: Path, reason: str = "daemon_dead"
) -> dict[str, Any]:
    """Rebuild timeline.json from the append-only logs when the daemon is gone."""
    meta = json.loads((session_dir / SESSION_FILE).read_text(encoding="utf-8"))
    events_path = session_dir / EVENTS_FILE
    events = [
        json.loads(line)
        for line in (
            events_path.read_text(encoding="utf-8").splitlines()
            if events_path.exists()
            else []
        )
        if line.strip()
    ]
    frames_path = session_dir / FRAMES_INDEX
    frame_lines = (
        frames_path.read_text(encoding="utf-8").splitlines()
        if frames_path.exists()
        else []
    )
    frames = [json.loads(line) for line in frame_lines if line.strip()]
    last_t = max(
        [f["t"] for f in frames]
        + [e["t"] for e in events]
        + [float(meta.get("started") or time.time())]
    )
    if not any(e["type"] == "stop" for e in events):
        events.append(
            {
                "seq": (events[-1]["seq"] + 1) if events else 0,
                "t": last_t,
                "type": "stop",
            }
        )
    timeline = {
        "version": 1,
        "session": {
            "stack": meta.get("stack"),
            "persona": meta.get("persona"),
            "flows": meta.get("flows") or [],
            "started": meta.get("started"),
            "ended": last_t,
            "page_url": meta.get("page_url"),
            "target_id": meta.get("target_id"),
            "browser": meta.get("browser"),
            "viewport": {},
            "cursor_overlay": bool(meta.get("cursor", True)),
            "capture": {k: meta.get(k) for k in ("quality", "max_width", "every_nth")},
            "stop_reason": reason,
        },
        "events": events,
        "frames_file": FRAMES_INDEX,
        "frame_count": len(frames),
        "frame_bytes": sum(
            Path(session_dir / f["file"]).stat().st_size
            for f in frames
            if (session_dir / f["file"]).exists()
        ),
    }
    write_session_outputs(session_dir, timeline, meta)
    clear_current(session_dir.parent, session_dir)
    sock = meta.get("socket")
    if sock:
        Path(sock).unlink(missing_ok=True)
    return timeline


def _make_handler(recorder: Recorder) -> type[socketserver.StreamRequestHandler]:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            line = self.rfile.readline()
            if not line:
                return
            try:
                command = json.loads(line.decode("utf-8"))
                reply = recorder.handle_command(command)
            except Exception as exc:
                reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.wfile.write((json.dumps(reply) + "\n").encode("utf-8"))

    return Handler


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def cmd_serve(args: argparse.Namespace) -> int:
    session_dir = Path(args.session).resolve()
    meta = json.loads((session_dir / SESSION_FILE).read_text(encoding="utf-8"))
    recorder = Recorder(session_dir, meta)

    def log(message: str) -> None:
        print(f"{datetime.now().strftime('%H:%M:%S')} {message}", flush=True)

    log(f"attaching to {meta.get('page_url')}")
    recorder.attach()
    recorder.begin()
    socket_path = meta["socket"]
    Path(socket_path).unlink(missing_ok=True)
    server = _Server(socket_path, _make_handler(recorder))
    server_thread = threading.Thread(
        target=server.serve_forever, name="control", daemon=True
    )
    server_thread.start()
    write_current(
        session_dir.parent,
        {
            "session_dir": str(session_dir),
            "socket": socket_path,
            "pid": os.getpid(),
            "started": recorder.started,
            "stack": meta.get("stack"),
            "persona": meta.get("persona"),
        },
    )
    log(f"recording; control socket {socket_path}")

    def on_signal(signum: int, _frame: Any) -> None:
        log(f"signal {signum}; stopping")
        recorder.stop_reason = recorder.stop_reason or "signal"
        recorder.stop_event.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    reason = "stop"
    while not recorder.stop_event.wait(0.5):
        if recorder.cdp is not None and recorder.cdp.closed.is_set():
            reason = "target_closed"
            log("DevTools connection closed (tab or Chrome gone)")
            break
    if recorder.stop_reason == "signal":
        reason = "signal"
    server.shutdown()
    server.server_close()
    recorder.finalize(reason)
    log(
        f"finalized ({reason}); {recorder.screencast.count if recorder.screencast else 0} frames"
    )
    return 0


def _browser_version(port: int) -> str | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=3
        ) as resp:
            return json.loads(resp.read().decode("utf-8")).get("Browser")
    except OSError:
        return None


def _print_targets(targets: list[dict[str, Any]]) -> None:
    for target in cdp.page_targets(targets):
        print(
            f"{target.get('id')}  {target.get('url')}  [{(target.get('title') or '')[:50]}]"
        )


def cmd_targets(args: argparse.Namespace) -> int:
    try:
        targets = cdp.list_targets(args.port)
    except OSError as exc:
        print(
            f"No debug Chrome answering on 127.0.0.1:{args.port} ({exc}). "
            "Launch it as described in .claude/skills/ux-test.md step 2.",
            file=sys.stderr,
        )
        return 2
    pages = cdp.page_targets(targets)
    if not pages:
        print("No page targets; open the stack's URL in the debug Chrome first.")
        return 1
    _print_targets(targets)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve() if args.out else default_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    current = read_current(out_dir)
    if (
        current
        and pid_alive(current.get("pid"))
        and Path(current.get("socket", "")).exists()
    ):
        print(
            f"A recording is already in progress ({current.get('session_dir')}). "
            "Run ux_recorder.py stop first.",
            file=sys.stderr,
        )
        return 1
    if current:
        clear_current(out_dir)
    try:
        targets = cdp.list_targets(args.port)
    except OSError as exc:
        print(
            f"No debug Chrome answering on 127.0.0.1:{args.port} ({exc}). "
            "Launch it as described in .claude/skills/ux-test.md step 2.",
            file=sys.stderr,
        )
        return 2
    try:
        target = cdp.choose_target(targets, args.url_contains, args.target)
    except cdp.TargetChoiceError as exc:
        print(f"{exc}. Candidates:", file=sys.stderr)
        for candidate in exc.candidates:
            print(f"  {candidate.get('id')}  {candidate.get('url')}", file=sys.stderr)
        return 2

    now = datetime.now()
    session_dir = out_dir / session_dir_name(args.stack, now)
    session_dir.mkdir(parents=True, exist_ok=False)
    meta = {
        "stack": args.stack,
        "persona": args.persona,
        "flows": args.flow or [],
        "target_id": target.get("id"),
        "page_url": target.get("url"),
        "page_title": target.get("title"),
        "ws_url": target["webSocketDebuggerUrl"],
        "browser": _browser_version(args.port),
        "port": args.port,
        "quality": args.quality,
        "max_width": args.max_width,
        "every_nth": args.every_nth,
        "cursor": not args.no_cursor,
        "say": args.say,
        "socket": socket_path_for(now, os.getpid()),
        "started": time.time(),
    }
    (session_dir / SESSION_FILE).write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    log_path = session_dir / "recorder.log"
    with log_path.open("ab") as log_fh:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "serve",
                "--session",
                str(session_dir),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            cwd=str(repo_root()),
        )
    deadline = time.time() + START_TIMEOUT
    status: dict[str, Any] | None = None
    while time.time() < deadline:
        if process.poll() is not None:
            break
        if Path(meta["socket"]).exists():
            try:
                status = send_command(meta["socket"], {"cmd": "status"}, timeout=5)
                break
            except (OSError, RuntimeError, json.JSONDecodeError):
                pass
        time.sleep(0.25)
    if not status or not status.get("ok"):
        print("The recorder did not come up. Its log:", file=sys.stderr)
        print(
            log_path.read_text(encoding="utf-8", errors="replace")[-4000:],
            file=sys.stderr,
        )
        if process.poll() is None:
            process.terminate()
        return 1
    print(
        json.dumps(
            {
                "session_dir": str(session_dir),
                "socket": meta["socket"],
                "pid": process.pid,
                "page_url": meta["page_url"],
                "frames": status.get("frames"),
                "visible": status.get("visible"),
                "next": 'ux_recorder.py mark "<label>" --say "<narration>" before each step; stop when done',
            },
            indent=2,
        )
    )
    return 0


def _resolve_session(args: argparse.Namespace) -> tuple[Path, dict[str, Any] | None]:
    out_dir = (
        Path(args.out).resolve() if getattr(args, "out", None) else default_out_dir()
    )
    if getattr(args, "session", None):
        session_dir = Path(args.session).resolve()
        current = read_current(out_dir)
        if current and Path(current.get("session_dir", "")) == session_dir:
            return session_dir, current
        return session_dir, None
    current = read_current(out_dir)
    if not current:
        raise SystemExit(
            "No recording in progress — run ux_recorder.py start first "
            f"(looked for {out_dir / CURRENT_FILE})."
        )
    return Path(current["session_dir"]), current


def _send_or_explain(
    current: dict[str, Any] | None, session_dir: Path, command: dict[str, Any]
) -> dict[str, Any]:
    if not current:
        raise SystemExit(f"No live recorder for {session_dir}; is it already stopped?")
    try:
        return send_command(current["socket"], command)
    except (OSError, RuntimeError) as exc:
        if not pid_alive(current.get("pid")):
            raise SystemExit(
                f"The recorder process (pid {current.get('pid')}) is gone. "
                "Run ux_recorder.py stop to finalize what was captured."
            ) from exc
        raise SystemExit(
            f"Could not reach the recorder at {current['socket']}: {exc}"
        ) from exc


def _print_reply(reply: dict[str, Any]) -> int:
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1


def cmd_mark(args: argparse.Namespace) -> int:
    session_dir, current = _resolve_session(args)
    return _print_reply(
        _send_or_explain(
            current, session_dir, {"cmd": "mark", "label": args.label, "say": args.say}
        )
    )


def cmd_note(args: argparse.Namespace) -> int:
    session_dir, current = _resolve_session(args)
    return _print_reply(
        _send_or_explain(current, session_dir, {"cmd": "note", "text": args.text})
    )


def cmd_pause(args: argparse.Namespace) -> int:
    session_dir, current = _resolve_session(args)
    return _print_reply(_send_or_explain(current, session_dir, {"cmd": "pause"}))


def cmd_resume(args: argparse.Namespace) -> int:
    session_dir, current = _resolve_session(args)
    return _print_reply(_send_or_explain(current, session_dir, {"cmd": "resume"}))


def cmd_status(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve() if args.out else default_out_dir()
    current = read_current(out_dir)
    if not current:
        print(
            json.dumps({"ok": True, "recording": False})
            if args.json
            else "No recording in progress."
        )
        return 0
    alive = pid_alive(current.get("pid"))
    try:
        reply = (
            send_command(current["socket"], {"cmd": "status"}, timeout=5)
            if alive
            else {"ok": False}
        )
    except (OSError, RuntimeError):
        reply = {"ok": False}
    reply.update(
        {
            "recording": True,
            "pid_alive": alive,
            "session_dir": current.get("session_dir"),
        }
    )
    if args.json:
        print(json.dumps(reply))
        return 0
    if not reply.get("ok"):
        print(
            f"Recorder for {current.get('session_dir')} is not responding (pid alive: {alive}). Run stop to finalize."
        )
        return 1
    print(
        f"recording {reply['session_dir']}\n"
        f"  frames {reply['frames']}  ({reply['bytes'] / 1e6:.1f} MB)  marks {reply['marks']}  "
        f"clicks {reply.get('clicks', 0)} ({reply.get('clicks_off_target', 0)} on nothing interactive)  "
        f"uptime {reply['uptime']}s\n"
        f"  paused {reply['paused']}  tab visible {reply['visible']}  connected {reply['connected']}\n"
        f"  last mark: {reply['last_mark']}"
    )
    if not reply["visible"]:
        print(
            "  ⚠️  the tab is hidden or covered; no frames arrive until it is visible again"
        )
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    session_dir, current = _resolve_session(args)
    if current and pid_alive(current.get("pid")):
        try:
            reply = send_command(
                current["socket"], {"cmd": "stop", "say": args.say}, timeout=60
            )
        except (OSError, RuntimeError):
            reply = None
        if reply and reply.get("ok"):
            reply["next"] = (
                f"fill {session_dir / 'review.md'}, then: ux_recorder.py render {session_dir}"
            )
            return _print_reply(reply)
    if not (session_dir / SESSION_FILE).exists():
        raise SystemExit(f"{session_dir} has no session.json; nothing to finalize")
    timeline = finalize_from_disk(session_dir)
    print(
        json.dumps(
            {
                "ok": True,
                "finalized_from_disk": True,
                "session_dir": str(session_dir),
                "frames": timeline["frame_count"],
                "stop_reason": timeline["session"]["stop_reason"],
            }
        )
    )
    return 0


def _latest_session(out_dir: Path) -> Path | None:
    candidates = sorted(
        (p for p in out_dir.iterdir() if p.is_dir() and (p / TIMELINE_FILE).exists()),
        key=lambda p: p.stat().st_mtime,
    )
    return candidates[-1] if candidates else None


def cmd_render(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve() if args.out else default_out_dir()
    if args.session_dir:
        session_dir = Path(args.session_dir).resolve()
    else:
        latest = _latest_session(out_dir) if out_dir.exists() else None
        if latest is None:
            raise SystemExit(
                f"No finished sessions under {out_dir}; run start … stop first."
            )
        session_dir = latest
    if not (session_dir / TIMELINE_FILE).exists():
        raise SystemExit(
            f"{session_dir} has no timeline.json; run ux_recorder.py stop first."
        )
    tools = render.check_tools()
    missing = [name for name in ("ffmpeg", "ffprobe") if not tools.get(name)]
    if missing:
        raise SystemExit(
            f"missing {', '.join(missing)}; install with: brew install ffmpeg"
        )
    if not args.no_narration and not tools.get("boto3") and not args.dry_run:
        raise SystemExit(
            "boto3 is required for narration; pip install boto3 or use --no-narration"
        )
    opts = render.RenderOptions(
        voice=args.voice,
        region=args.region,
        engine=args.engine,
        no_narration=args.no_narration,
        no_cards=args.no_cards,
        plain_text=args.plain_text,
        dry_run=args.dry_run,
        captions="none" if args.no_captions else "embed",
        pacing=render.PacingConfig(
            click_hold=args.click_hold,
            gap_max=args.gap_max,
            pre_roll=args.pre_roll,
            hold=args.hold,
            min_seg=args.min_seg,
            max_speedup=args.max_speedup,
            cap=args.cap,
            fps=args.fps,
            card_seconds=args.card_seconds,
        ),
    )
    print(f"rendering {session_dir}")
    render.render_session(session_dir, opts)
    return 0


def cmd_deps(_args: argparse.Namespace) -> int:
    tools = render.check_tools()
    missing = False
    for name, version in tools.items():
        marker = "✅" if version else "❌"
        print(f"{marker} {name}: {version or 'missing'}")
        missing = missing or not version
    if not tools.get("ffmpeg") or not tools.get("ffprobe"):
        print("   install with: brew install ffmpeg")
    if not tools.get("boto3"):
        print("   narration needs boto3 (make setup installs it)")
    if not tools.get("Pillow"):
        print(
            "   title/end cards need Pillow (make setup installs it); render will skip cards without it"
        )
    return 1 if (not tools.get("ffmpeg") or not tools.get("ffprobe")) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_out(p: argparse.ArgumentParser) -> None:
        p.add_argument("--out", help="recordings root (default scratch/ux-recordings)")

    def add_session(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--session", help="session directory (default: the recording in progress)"
        )
        add_out(p)

    p = sub.add_parser("targets", help="list page targets in the debug Chrome")
    p.add_argument("--port", type=int, default=9222)
    p.set_defaults(func=cmd_targets)

    p = sub.add_parser("start", help="attach to a tab and begin recording")
    p.add_argument("--stack", required=True)
    p.add_argument("--persona", required=True)
    p.add_argument(
        "--flow", action="append", help="flow id from scripts/ux_flows.yaml; repeatable"
    )
    p.add_argument("--url-contains", help="pick the tab whose URL contains this")
    p.add_argument("--target", help="pick the tab by DevTools target id (see targets)")
    p.add_argument("--port", type=int, default=9222)
    p.add_argument("--quality", type=int, default=70, help="JPEG quality 1-100")
    p.add_argument("--max-width", type=int, default=1600)
    p.add_argument(
        "--every-nth", type=int, default=1, help="keep every n-th screencast frame"
    )
    p.add_argument(
        "--no-cursor", action="store_true", help="do not inject the synthetic cursor"
    )
    p.add_argument("--say", help="narration for the title card")
    add_out(p)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("mark", help="begin a new segment; say what happens next")
    p.add_argument("label")
    p.add_argument("--say", help="one or two sentences of narration")
    add_session(p)
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("note", help="record an observation without starting a segment")
    p.add_argument("text")
    add_session(p)
    p.set_defaults(func=cmd_note)

    p = sub.add_parser(
        "pause", help="stop capturing until resume; the gap is cut from the video"
    )
    add_session(p)
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", help="resume capturing after pause")
    add_session(p)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser(
        "status", help="frames, marks, visibility of the recording in progress"
    )
    p.add_argument("--json", action="store_true")
    add_out(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("stop", help="finish recording and write the timeline")
    p.add_argument("--say", help="narration for the end card")
    add_session(p)
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("render", help="synthesize narration and encode review.mp4")
    p.add_argument(
        "session_dir", nargs="?", help="default: the most recent finished session"
    )
    p.add_argument(
        "--voice",
        default="Ruth",
        help="Polly generative voice (Ruth, Matthew, Stephen, Danielle, Joanna, Salli, Tiffany)",
    )
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--engine", default="generative")
    p.add_argument("--no-narration", action="store_true")
    p.add_argument("--no-cards", action="store_true")
    p.add_argument(
        "--no-captions",
        action="store_true",
        help="do not embed the subtitle track (review.srt is still written)",
    )
    p.add_argument(
        "--plain-text",
        action="store_true",
        help="send narration to Polly as plain text instead of SSML",
    )
    p.add_argument("--gap-max", type=float, default=1.5)
    p.add_argument(
        "--click-hold",
        type=float,
        default=0.7,
        help="seconds to hold the pre-click frame with the click marker drawn on it",
    )
    p.add_argument("--pre-roll", type=float, default=0.6)
    p.add_argument("--hold", type=float, default=1.2)
    p.add_argument("--min-seg", type=float, default=2.5)
    p.add_argument("--max-speedup", type=float, default=3.0)
    p.add_argument("--cap", type=float, default=12.0)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--card-seconds", type=float, default=4.0)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the pacing table and commands; no Polly, no ffmpeg",
    )
    add_out(p)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("deps", help="check ffmpeg, ffprobe, boto3 and Pillow")
    p.set_defaults(func=cmd_deps)

    p = sub.add_parser("serve", help=argparse.SUPPRESS)
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
