#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The UX recorder's four session commands as a stdio MCP server.

The unattended MR UX test (``scripts/sdlc/ai_ux_test.py``) runs Claude Code with
no built-in tools at all, so the model cannot run ``ux_recorder.py mark ...`` the
way the interactive skill (``.claude/skills/ux-test.md``) has it do. This server
gives it the four commands a recorded session needs, ``mark``, ``note``,
``pause`` and ``resume``, as MCP tools, and forwards each one over the recorder's
existing Unix socket, which it finds through the ``current.json`` the recorder
keeps in its ``--out`` directory. Claude Code names them
``mcp__ux-recorder__<tool>``, which is what ``scripts/ux_runner/tool_profiles.py``
grants.

What this is NOT
----------------
**It is not the recorder's controller.** The harness starts, stops and renders
the recording through ``ux_recorder.py``'s own command line, and ``start``,
``stop``, ``status`` and ``render`` are not tools here, so nothing the model is
talked into can end the recording early, render it or point it at another tab.
Nor is it a way into the recorder for anything but these four commands: it builds
every command it sends from fixed keys and cleaned values, and never forwards a
key the model supplied.

Three properties it is built around
-----------------------------------
1. **The model never needs a shell.** ``Bash`` would grant far more than four
   recorder commands, and the session runs beside a browser showing the MR's own
   code, so a shell is the first thing a prompt-injected page would ask for.
2. **What it forwards is bounded.** The model's words become the video's chapter
   titles, the narration Polly speaks and the chapter list in the MR note, so a
   label, a narration line and a note are cut to ``MAX_LABEL_CHARS``,
   ``MAX_SAY_CHARS`` and ``MAX_NOTE_CHARS`` with control characters blanked and
   whitespace collapsed, and a session gets at most ``MAX_MARKS`` marks and
   ``MAX_NOTES`` notes.
3. **A pause ends on its own.** A pause cuts its stretch from the video, so a
   pause the model never resumes would cut the rest of the session. All pauses
   together get ``PAUSE_BUDGET_SECONDS``, and each is resumed by a timer after
   ``PAUSE_LIMIT_SECONDS`` or after what is left of that budget, whichever is
   shorter. An explicit ``resume`` cancels the timer, and once the budget is spent
   ``pause`` is refused.

Why a hand-written JSON-RPC loop
--------------------------------
The stdio transport is newline-delimited JSON-RPC 2.0, one message per line, and
four tools need only ``initialize``, ``ping``, ``tools/list`` and ``tools/call``.
The MCP SDK would be a third-party pin in a harness that otherwise runs on a bare
``python3`` under ``env -i``. Only JSON-RPC is written to stdout, because the
client reads every stdout line as a message; diagnostics go to stderr.

The server runs as ``python3 -I -B``. ``-I`` leaves the script's own directory off
``sys.path``, so it puts it back before anything imports ``ux_recorder``; ``-B``
keeps that import from writing bytecode into the pinned harness, which
``PYTHONDONTWRITEBYTECODE`` cannot do under ``-I``. The import is deferred to the
first command (see :class:`RecorderLink`), so tests can inject fakes without it.

Usage
-----
    python3 -I -B scripts/ux_recorder_mcp.py --out <the recorder's --out directory>
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

SERVER_NAME = "ux-recorder"
SERVER_VERSION = "1"
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MAX_LABEL_CHARS = 80
MAX_SAY_CHARS = 400
MAX_NOTE_CHARS = 500
MAX_MARKS = 40
MAX_NOTES = 60
PAUSE_LIMIT_SECONDS = 120.0
PAUSE_BUDGET_SECONDS = 300.0

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

RECORDER_DOWN = "the recorder is not running; carry on without marks"
MARKS_SPENT = f"all {MAX_MARKS} marks are used; carry on without marks"
NOTES_SPENT = f"all {MAX_NOTES} notes are used; carry on without notes"
PAUSE_BUDGET_SPENT = (
    f"the {PAUSE_BUDGET_SECONDS:.0f} s pause budget is used up; carry on without "
    "pausing"
)

INSTRUCTIONS = (
    "Call mark before each group of actions, note for something noticed in "
    "passing, and pause and resume around a long wait; the harness starts, stops "
    "and renders the recording."
)

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "mark",
        "description": (
            "Start a chapter of the recording before each group of actions; the "
            "label is the chapter title and say is one or two plain spoken "
            "sentences."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "maxLength": MAX_LABEL_CHARS,
                    "description": "The chapter title, a few words.",
                },
                "say": {
                    "type": "string",
                    "maxLength": MAX_SAY_CHARS,
                    "description": "One or two plain spoken sentences of narration.",
                },
            },
            "required": ["label"],
            "additionalProperties": False,
        },
    },
    {
        "name": "note",
        "description": "Record something noticed in passing, without a new chapter.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "maxLength": MAX_NOTE_CHARS,
                    "description": "What was noticed.",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pause",
        "description": (
            "Stop capturing around a long wait so it is cut from the video; it "
            f"resumes on its own after {PAUSE_LIMIT_SECONDS:.0f} s."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "resume",
        "description": "Resume capturing after a pause.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
)


class InvalidParams(Exception):
    """A request whose params cannot be acted on; answered with -32602."""


class MethodNotFound(Exception):
    """A request for a method this server does not have; answered with -32601."""


def clean(value: Any, limit: int) -> str:
    """``value`` as bounded single-line text, or ``""`` when it is not a string.

    Every character ``str.isprintable`` refuses becomes a space, which covers the
    control and format characters, then whitespace collapses to single spaces and
    the ends are stripped, and the result is cut to ``limit`` characters.
    """
    if not isinstance(value, str):
        return ""
    text = "".join(char if char.isprintable() else " " for char in value)
    return " ".join(text.split())[:limit].rstrip()


def start_timer(seconds: float, callback: Callable[[], None]) -> threading.Timer:
    """Start ``callback`` after ``seconds`` on a daemon ``threading.Timer``.

    A daemon thread, so a pending auto-resume never keeps the server alive after
    the client has gone.
    """
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    timer.start()
    return timer


def _is_request_id(value: Any) -> bool:
    """Whether ``value`` can be a JSON-RPC id: a string, an integer or null."""
    if value is None or isinstance(value, str):
        return True
    return isinstance(value, int) and not isinstance(value, bool)


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


class RecorderLink:
    """The recorder's control socket, looked up through its ``current.json``.

    The socket is looked up on every command rather than once, because the
    recorder writes ``current.json`` when it starts and removes it when it stops,
    which is how a recorder that is not running is recognised. ``send`` and
    ``read_current`` default to ``ux_recorder.send_command`` and
    ``ux_recorder.read_current``, imported on the first command that needs them.
    """

    def __init__(
        self,
        out_dir: Path,
        *,
        send: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        read_current: Callable[[Path], dict[str, Any] | None] | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self._send = send
        self._read_current = read_current

    def command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The recorder's reply to ``payload``.

        Anything that stops the command reaching the recorder, from a missing
        ``current.json`` to a refused connection or an unreadable reply, is the
        same ``{'ok': False}`` reply naming ``RECORDER_DOWN``, because the model
        can do only one thing about any of them: carry on without the recorder.
        """
        try:
            if self._send is None or self._read_current is None:
                import ux_recorder

                self._send = self._send or ux_recorder.send_command
                self._read_current = self._read_current or ux_recorder.read_current
            current = self._read_current(self.out_dir)
            socket_path = current.get("socket") if isinstance(current, dict) else None
            if not isinstance(socket_path, str) or not socket_path:
                return {"ok": False, "error": RECORDER_DOWN}
            reply = self._send(socket_path, payload)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            print(f"ux-recorder-mcp: {type(exc).__name__}: {exc}", file=sys.stderr)
            return {"ok": False, "error": RECORDER_DOWN}
        if not isinstance(reply, dict):
            return {"ok": False, "error": RECORDER_DOWN}
        return reply


class Adapter:
    """MCP request handling, and the bounds on what reaches the recorder.

    ``clock`` measures pauses against ``PAUSE_BUDGET_SECONDS`` and ``timer``
    starts the auto-resume, so tests can drive both. Tool calls and the
    auto-resume share one lock, because the timer fires on its own thread.
    """

    def __init__(
        self,
        link: RecorderLink,
        *,
        clock: Callable[[], float] = time.monotonic,
        timer: Callable[[float, Callable[[], None]], Any] = start_timer,
    ) -> None:
        self.link = link
        self.clock = clock
        self.timer = timer
        self.marks = 0
        self.notes = 0
        self.paused_seconds = 0.0
        self._paused_at: float | None = None
        self._pause_timer: Any = None
        self._pause_generation = 0
        self._lock = threading.RLock()

    def handle(self, message: Any) -> dict[str, Any] | None:
        """The reply to one decoded message, or None when it is a notification."""
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "Invalid Request: not an object")
        request_id = message.get("id")
        if not _is_request_id(request_id):
            return _error(None, INVALID_REQUEST, "Invalid Request: bad id")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return _error(request_id, INVALID_REQUEST, "Invalid Request: no method")
        if "id" not in message:
            return None
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "Invalid params: not an object")
        try:
            result = self._dispatch(method, params)
        except InvalidParams as exc:
            return _error(request_id, INVALID_PARAMS, f"Invalid params: {exc}")
        except MethodNotFound:
            return _error(
                request_id, METHOD_NOT_FOUND, f"Method not found: {method[:80]}"
            )
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            return _error(
                request_id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}"
            )
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {
                "protocolVersion": (
                    requested
                    if requested in PROTOCOL_VERSIONS
                    else PROTOCOL_VERSIONS[-1]
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": list(TOOLS)}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                raise InvalidParams("tools/call needs a tool name")
            return self.call_tool(name, params.get("arguments"))
        raise MethodNotFound(method)

    def call_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        """Run one tool and wrap the reply as an MCP tool result.

        An unknown tool, or arguments that are not an object, raise
        :class:`InvalidParams`. Everything else, including a refusal, is a result
        whose text is the reply as JSON and which is an error when the reply is
        not ``ok``.
        """
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise InvalidParams("arguments must be an object")
        tools = {
            "mark": self._mark,
            "note": self._note,
            "pause": self._pause,
            "resume": self._resume,
        }
        run = tools.get(name)
        if run is None:
            raise InvalidParams(f"unknown tool {name[:80]!r}")
        with self._lock:
            reply = run(arguments)
        return {
            "content": [{"type": "text", "text": json.dumps(reply)}],
            "isError": not reply.get("ok"),
        }

    def _mark(self, arguments: dict[str, Any]) -> dict[str, Any]:
        label = clean(arguments.get("label"), MAX_LABEL_CHARS)
        if not label:
            reply = {"ok": False, "error": "mark needs a label"}
        elif self.marks >= MAX_MARKS:
            reply = {"ok": False, "error": MARKS_SPENT}
        else:
            say = clean(arguments.get("say"), MAX_SAY_CHARS)
            reply = self.link.command(
                {"cmd": "mark", "label": label, "say": say or None}
            )
            if reply.get("ok"):
                self.marks += 1
        return {**reply, "marks_left": MAX_MARKS - self.marks}

    def _note(self, arguments: dict[str, Any]) -> dict[str, Any]:
        text = clean(arguments.get("text"), MAX_NOTE_CHARS)
        if not text:
            return {"ok": False, "error": "note needs text"}
        if self.notes >= MAX_NOTES:
            return {"ok": False, "error": NOTES_SPENT}
        reply = self.link.command({"cmd": "note", "text": text})
        if reply.get("ok"):
            self.notes += 1
        return reply

    def _pause(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Pause the recorder and start the timer that resumes it.

        The timer runs for ``PAUSE_LIMIT_SECONDS`` or whatever is left of the
        budget, whichever is shorter. A pause while one is already running is
        refused here rather than forwarded, since forwarding it could only start a
        second timer.
        """
        if self._paused_at is not None:
            return {"ok": False, "error": "already paused"}
        budget_left = PAUSE_BUDGET_SECONDS - self.paused_seconds
        if budget_left <= 0:
            return {"ok": False, "error": PAUSE_BUDGET_SPENT}
        reply = self.link.command({"cmd": "pause"})
        if not reply.get("ok"):
            return reply
        seconds = min(PAUSE_LIMIT_SECONDS, budget_left)
        self._paused_at = self.clock()
        self._pause_generation += 1
        generation = self._pause_generation
        self._pause_timer = self.timer(seconds, lambda: self._auto_resume(generation))
        return {**reply, "resumes_in": round(seconds, 1)}

    def _resume(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._end_pause()
        return self.link.command({"cmd": "resume"})

    def _end_pause(self) -> None:
        """Cancel the auto-resume and add the pause to the time spent paused."""
        if self._pause_timer is not None:
            self._pause_timer.cancel()
            self._pause_timer = None
        if self._paused_at is not None:
            self.paused_seconds += max(0.0, self.clock() - self._paused_at)
            self._paused_at = None
        self._pause_generation += 1

    def _auto_resume(self, generation: int) -> None:
        """Resume the pause ``generation`` belongs to, if it is still running.

        A timer that fires after its pause has already ended, by an explicit
        ``resume`` that raced it, finds a newer generation and does nothing.
        """
        with self._lock:
            if generation != self._pause_generation or self._paused_at is None:
                return
            self._pause_timer = None
            self._end_pause()
            self.link.command({"cmd": "resume"})


def serve(stdin: TextIO, stdout: TextIO, adapter: Adapter) -> int:
    """Answer newline-delimited JSON-RPC from ``stdin`` until EOF.

    One reply line per request, none for a notification, and nothing else on
    ``stdout``. A line that is not JSON is answered with a parse error whose id is
    null, as JSON-RPC requires when the id cannot be read.
    """
    while True:
        line = stdin.readline()
        if not line:
            return 0
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            reply = _error(None, PARSE_ERROR, "Parse error")
        else:
            reply = adapter.handle(message)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="the recorder's --out directory, where it keeps current.json",
    )
    args = parser.parse_args(argv)
    if isinstance(sys.stdin, io.TextIOWrapper):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    return serve(sys.stdin, sys.stdout, Adapter(RecorderLink(args.out)))


if __name__ == "__main__":
    sys.exit(main())
