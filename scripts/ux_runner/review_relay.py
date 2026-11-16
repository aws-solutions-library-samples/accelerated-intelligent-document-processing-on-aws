#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The MR UX test's browser server: chrome-devtools-mcp behind the commit guard.

``agent.mcp_servers`` registers this server, not chrome-devtools-mcp itself, under
the name ``chrome-devtools``, so the review's browser tools keep the ids
``tool_profiles.EXPERT`` grants (``mcp__chrome-devtools__click`` and the rest). It
starts chrome-devtools-mcp as a story flow's relay does (``story_relay.start_child``),
speaks MCP to it as a client that reports no ``roots`` capability, and serves the
browser tools EXPERT grants as the child lists them: the same names, descriptions,
schemas and order, and none of the tools EXPERT denies, whatever the lists say.

Every call passes the first-time-user stories' commit guard
(``story_relay.CommitGuard``) before it reaches the browser, so a control a story may
not press is refused here too: Process Changes on a review-queue item, which finishes
that review with no confirmation, among them. The model is told why
(:data:`COMMIT_POINT_TEXT`), the refusal becomes one line of the log ``--refusals``
names (:class:`RefusalLog`), which the orchestrator copies into the job's
``refusals.jsonl``, and the session goes on. A call the guard lets through is
forwarded with the arguments the model sent and answered with the child's own
answer, a JSON-RPC error included; the guard only reads that answer, for the latest
snapshot and an open dialog.

None of the story relay's other guard rails is here: no action budget, no screenshot
after an action, no cap on images, looks or refusals, no cut snapshot, no stripped
argument and no signed-out detection. Those measure a first-time user; this keeps a
review off the shared stack. What does differ from talking to chrome-devtools-mcp
directly, beside the guard:

* a click names an element of the latest snapshot the model was shown, as a story's
  does, so a uid from an older page is refused rather than tried;
* chrome-devtools-mcp has this relay rather than ``claude`` for its client, and this
  relay reports no roots, so a ``filePath`` can name a file under
  ``--filesystem-root`` alone;
* a call is given ``story_relay.CALL_SECONDS`` and an answer
  ``story_relay.RESULT_BYTES``, and a request over ``story_relay.REQUEST_BYTES`` is
  refused unread.

The client is read on a thread of its own, so a ``ping`` is answered and a
cancellation reaches the browser while a call is running, and a stop signal ends the
wait on the browser and then the server. chrome-devtools-mcp is stopped last.

The guard is as good as the story relay's and no better: it judges a control by its
name, and ``story_relay`` says what a page can do to that, so a control that commits
under a name it has no verb for gets through.

This file is executed by path, under ``python3 -I -B``, and puts ``scripts/`` on
``sys.path`` itself.

Usage
-----
    python3 -I -B scripts/ux_runner/review_relay.py --devtools PATH \\
        --filesystem-root DIR --refusals FILE
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import queue
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0] or ".").resolve() == _HERE:
    sys.path.pop(0)
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

import ux_recorder_mcp  # noqa: E402

from ux_runner import agent, readback, story_relay, tool_profiles  # noqa: E402

SERVER_NAME = "ux-review-browser"
SERVER_VERSION = "1"
#: The name of the guard's refusal log, in the directory the orchestrator gives it.
REFUSAL_LOG = "guard-refusals.jsonl"
#: The ``origin`` a guard refusal carries in the job's ``refusals.jsonl``, beside the
#: proxy's ``page``, ``browser`` and ``selftest``.
REFUSAL_ORIGIN = "guard"
#: The most lines the refusal log holds; refusals past them are not written.
REFUSALS_KEPT = 1000
#: How much of a control's name a refusal line keeps.
LABEL_CHARS = 200
#: The most of the refusal log read back: :data:`REFUSALS_KEPT` lines fit in it.
MAX_REFUSAL_LOG_BYTES = 1024 * 1024
COMMIT_POINT_TEXT = (
    "This would change something on the shared review stack, so it was not done. "
    "Report the step it would take under not_covered, and carry on another way."
)
BROWSER_GONE_TEXT = (
    "The browser has stopped answering, so nothing more can be checked. Write your "
    "report from what you have seen."
)

_FAILED_TEXTS: dict[str, str] = {
    "size": story_relay.TOO_LARGE_TEXT,
    "timeout": story_relay.TIMED_OUT_TEXT,
}
_POLL_SECONDS = 0.1
_READ_BYTES = 64 * 1024
_CANCELLED_IDS_MAX = 1000
_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_LOG_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_APPEND


class GuardLogError(Exception):
    """The refusal log could not be read back as this relay's own file."""


_WRITER = readback.Writer("the review relay's", "directory", GuardLogError)


@dataclass(frozen=True)
class Answer:
    """What one ``tools/call`` is answered with: a tool result, or a JSON-RPC error."""

    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None


class RefusalLog:
    """The guard's refusals, one JSON line each, in a file this relay creates.

    The file is created owner-only and never over an existing file or through a
    link, so what the orchestrator reads back is this relay's alone. A line holds
    the time, the tool, the refusal and its label, the control's name as the page
    wrote it, on one line and cut to :data:`LABEL_CHARS`. Writing stops at
    :data:`REFUSALS_KEPT` lines, and a write that fails stops it too: the log is
    evidence for the summary, and the refusal itself never depends on it.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.written = 0
        self._clock = clock
        self._fd: int | None = os.open(path, _LOG_FLAGS, 0o600)

    def record(self, *, tool: str, refused: str, label: str | None) -> None:
        """Append one refusal, while there is room and the file takes it."""
        if self._fd is None or self.written >= REFUSALS_KEPT:
            return
        line = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())),
            "tool": tool,
            "refused": refused,
            "label": ux_recorder_mcp.clean(label, LABEL_CHARS) or None,
        }
        data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
        view = memoryview(data)
        try:
            while view:
                view = view[os.write(self._fd, view) :]
        except OSError:
            self.close()
            return
        self.written += 1

    def close(self) -> None:
        """Close the file; later refusals are not written."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def read_refusals(path: Path) -> list[dict[str, Any]]:
    """The refusal log's records, read back as this process's own file.

    ``[]`` when there is none. A link, a pipe, another uid's file or one over
    :data:`MAX_REFUSAL_LOG_BYTES` raises :class:`GuardLogError`, and a line that is
    not a JSON object is skipped, so a torn last line costs only itself.
    """
    raw = readback.read(
        path.parent,
        path.name,
        writer=_WRITER,
        owner=os.geteuid(),
        limit=MAX_REFUSAL_LOG_BYTES,
    )
    records: list[dict[str, Any]] = []
    for line in (raw or b"").decode("utf-8", errors="replace").splitlines():
        if len(records) >= REFUSALS_KEPT:
            break
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def with_origin(record: Mapping[str, Any]) -> dict[str, Any]:
    """One refusal-log record as ``refusals.jsonl`` lists it, its origin beside it."""
    return {**record, "origin": REFUSAL_ORIGIN}


def served_tools(listed: object) -> dict[str, dict[str, Any]]:
    """The tools the relay serves: those of the child's listing EXPERT grants.

    Each keeps every field the child gave it and its place in the child's order. A
    tool with no input schema is not served, and a name listed twice is served as
    first listed.
    """
    tools = listed.get("tools") if isinstance(listed, Mapping) else None
    found: dict[str, dict[str, Any]] = {}
    for tool in tools if isinstance(tools, list) else ():
        if not isinstance(tool, Mapping) or not isinstance(tool.get("name"), str):
            continue
        name = tool["name"]
        if name in found or not isinstance(tool.get("inputSchema"), Mapping):
            continue
        granted = tool_profiles.mcp_tool(tool_profiles.CHROME_DEVTOOLS_SERVER, name)
        if tool_profiles.is_granted(granted):
            found[name] = json.loads(json.dumps(tool))
    return found


def _texts_of(result: Mapping[str, Any]) -> list[str]:
    content = result.get("content")
    return [
        item["text"]
        for item in (content if isinstance(content, list) else ())
        if isinstance(item, Mapping)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]


def _page_of(texts: Sequence[str]) -> story_relay.Snapshot | None:
    """The last snapshot ``texts`` hold, every element of it shown, or None."""
    latest: story_relay.Snapshot | None = None
    for text in texts:
        parts = story_relay.split_section(text)
        if parts is not None:
            latest = story_relay.cut_snapshot(
                parts[1], nodes=sys.maxsize, chars=sys.maxsize
            )
    return latest


def _said(text: str) -> Answer:
    return Answer(result={"content": [{"type": "text", "text": text}], "isError": True})


def _child_error(error: object) -> dict[str, Any]:
    """The child's JSON-RPC error as it gave it, or one saying it gave none."""
    if isinstance(error, Mapping):
        code, message = error.get("code"), error.get("message")
        if isinstance(code, int) and not isinstance(code, bool):
            if isinstance(message, str):
                return dict(error)
    return {
        "code": ux_recorder_mcp.INTERNAL_ERROR,
        "message": "chrome-devtools-mcp answered with an error of no known shape",
    }


class ReviewRelay(story_relay.CommitGuard):
    """The review's browser: the child's granted tools, each call judged first.

    ``child`` is None when chrome-devtools-mcp never started, and then nothing is
    served. Calls arrive one at a time from the server's main thread.
    """

    commit_point_text = COMMIT_POINT_TEXT
    browser_gone_text = BROWSER_GONE_TEXT

    def __init__(
        self,
        child: story_relay.Child | None,
        *,
        listed: object,
        refusals: RefusalLog,
        call_seconds: float = story_relay.CALL_SECONDS,
    ) -> None:
        super().__init__(child, call_seconds=call_seconds)
        self.refusals = refusals
        self._tools = served_tools(listed)

    def tools(self) -> list[dict[str, Any]]:
        """The tool definitions the relay serves, as the child listed them."""
        return [json.loads(json.dumps(tool)) for tool in self._tools.values()]

    def serves(self, name: str) -> bool:
        """Whether ``name`` is one of the tools the relay serves."""
        return name in self._tools

    def call(self, name: str, arguments: Mapping[str, Any]) -> Answer:
        """Judge one call, then forward it, or refuse it and log the refusal."""
        if self.over is not None:
            return _said(self.browser_gone_text)
        guarded = self.judge(name, arguments)
        if guarded is not None:
            refused, label, text = guarded
            self.refusals.record(tool=name, refused=refused, label=label)
            return _said(text)
        outcome = self._ask(name, arguments)
        if outcome.kind == "gone":
            self._died(name)
            return _said(self.browser_gone_text)
        message = outcome.message or {}
        result = message.get("result")
        if outcome.kind != "result" or not isinstance(result, Mapping):
            self._page_stale = True
            if outcome.kind in ("result", "error"):
                return Answer(error=_child_error(message.get("error")))
            return _said(_FAILED_TEXTS.get(outcome.kind, story_relay.CANCELLED_TEXT))
        texts = _texts_of(result)
        self.dialog = story_relay.open_dialog(texts)
        self._shown(name, _page_of(texts))
        return Answer(result=result)


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _is_request_id(value: Any) -> bool:
    if value is None or isinstance(value, str):
        return True
    return isinstance(value, int) and not isinstance(value, bool)


def _initialized(params: Mapping[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    versions = ux_recorder_mcp.PROTOCOL_VERSIONS
    return {
        "protocolVersion": requested if requested in versions else versions[-1],
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _called(
    relay: ReviewRelay, request_id: Any, params: Mapping[str, Any]
) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or not relay.serves(name):
        shown = name[:80] if isinstance(name, str) else type(name).__name__
        return _error(
            request_id,
            ux_recorder_mcp.INVALID_PARAMS,
            f"Invalid params: no tool {shown!r}",
        )
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error(
            request_id,
            ux_recorder_mcp.INVALID_PARAMS,
            "Invalid params: arguments must be an object",
        )
    answer = relay.call(name, arguments)
    if answer.error is not None:
        return {"jsonrpc": "2.0", "id": request_id, "error": dict(answer.error)}
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(answer.result or {})}


def handle(relay: ReviewRelay, message: Any) -> dict[str, Any] | None:
    """The reply to one client message, or None for a notification.

    An exception while handling a call is an internal error, and the call it was
    handling does not reach the browser after it.
    """
    if not isinstance(message, dict):
        return _error(
            None, ux_recorder_mcp.INVALID_REQUEST, "Invalid Request: not an object"
        )
    if "id" not in message:
        return None
    request_id = message["id"]
    if not _is_request_id(request_id):
        return _error(None, ux_recorder_mcp.INVALID_REQUEST, "Invalid Request: bad id")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        return _error(
            request_id, ux_recorder_mcp.INVALID_REQUEST, "Invalid Request: no method"
        )
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(
            request_id,
            ux_recorder_mcp.INVALID_PARAMS,
            "Invalid params: not an object",
        )
    try:
        if method == "initialize":
            result: dict[str, Any] = _initialized(params)
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": relay.tools()}
        elif method == "tools/call":
            return _called(relay, request_id, params)
        else:
            return _error(
                request_id,
                ux_recorder_mcp.METHOD_NOT_FOUND,
                f"Method not found: {method[:80]}",
            )
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        return _error(
            request_id,
            ux_recorder_mcp.INTERNAL_ERROR,
            f"Internal error: {type(error).__name__}",
        )
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _id_key(value: Any) -> tuple[str, Any] | None:
    if value is None or isinstance(value, str):
        return type(value).__name__, value
    if isinstance(value, int) and not isinstance(value, bool):
        return "int", value
    return None


class Client:
    """The client on stdio: read on a thread of its own, answered from the main one.

    ``notifications/cancelled`` for the request being handled tells the relay to stop
    waiting on the browser and drops its answer, and one for a request still queued
    drops that request unanswered. A ``ping`` and a line that is not JSON are
    answered at once, every other notification is dropped, and a line longer than
    ``limit`` bytes is never parsed: it is answered as an invalid request.
    """

    def __init__(
        self,
        read: Callable[[int], bytes],
        stream: Any,
        relay: story_relay.CommitGuard,
        *,
        limit: int = story_relay.REQUEST_BYTES,
    ) -> None:
        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.broken = False
        self._read = read
        self._stream = stream
        self._relay = relay
        self._limit = limit
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._current: tuple[str, Any] | None = None
        self._current_cancelled = False
        self._cancelled: set[tuple[str, Any]] = set()
        self._thread = threading.Thread(
            target=self._run, name="client-reader", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def send(self, reply: Mapping[str, Any]) -> None:
        """Write one reply line; once the client has gone, nothing more is written."""
        with self._write_lock:
            if self.broken:
                return
            try:
                self._stream.write(json.dumps(reply) + "\n")
                self._stream.flush()
            except (BrokenPipeError, ConnectionResetError, ValueError):
                self.broken = True

    def begin(self, request_id: Any) -> bool:
        """Mark ``request_id`` as the one being handled; False when it was cancelled."""
        key = _id_key(request_id)
        with self._lock:
            if key is not None and key in self._cancelled:
                self._cancelled.discard(key)
                return False
            self._current = key
            self._current_cancelled = False
            self._relay.begin()
        return True

    def finish(self) -> bool:
        """The request being handled is done; whether the client cancelled it."""
        with self._lock:
            cancelled = self._current_cancelled
            self._current = None
            self._current_cancelled = False
        return cancelled

    def _cancel(self, request_id: Any) -> None:
        key = _id_key(request_id)
        if key is None:
            return
        with self._lock:
            if self._current is not None and key == self._current:
                self._current_cancelled = True
                self._relay.cancel()
            elif len(self._cancelled) < _CANCELLED_IDS_MAX:
                self._cancelled.add(key)

    def _run(self) -> None:
        buffer = bytearray()
        discarding = False
        while True:
            try:
                chunk = self._read(_READ_BYTES)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                break
            buffer += chunk
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    if len(buffer) > self._limit:
                        buffer.clear()
                        if not discarding:
                            discarding = True
                            self.messages.put(("size", None))
                    break
                line = bytes(buffer[:newline])
                del buffer[: newline + 1]
                if discarding or len(line) > self._limit:
                    if not discarding:
                        self.messages.put(("size", None))
                    discarding = False
                    continue
                self._take(line)
        if buffer.strip() and not discarding:
            self._take(bytes(buffer))
        self.messages.put(("eof", None))

    def _take(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            message = json.loads(line.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            self.send(_error(None, ux_recorder_mcp.PARSE_ERROR, "Parse error"))
            return
        if isinstance(message, dict) and isinstance(message.get("method"), str):
            method = message["method"]
            if "id" not in message:
                if method == "notifications/cancelled":
                    params = message.get("params")
                    if isinstance(params, dict):
                        self._cancel(params.get("requestId"))
                return
            if method == "ping" and _is_request_id(message["id"]):
                self.send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
                return
        self.messages.put(("message", message))


def serve(relay: ReviewRelay, client: Client, *, stopping: threading.Event) -> None:
    """Handle the client's requests one at a time until EOF, a stop or a gone client."""
    relay.stop_when(stopping.is_set)
    client.start()
    while not stopping.is_set() and not client.broken:
        try:
            kind, message = client.messages.get(timeout=_POLL_SECONDS)
        except queue.Empty:
            continue
        if kind == "eof":
            return
        if kind == "size":
            client.send(
                _error(
                    None,
                    ux_recorder_mcp.INVALID_REQUEST,
                    "Invalid Request: the request is too large",
                )
            )
            continue
        request_id = message.get("id") if isinstance(message, dict) else None
        if not client.begin(request_id):
            continue
        try:
            reply = handle(relay, message)
        finally:
            cancelled = client.finish()
        if reply is not None and not cancelled:
            client.send(reply)


def _say(message: str) -> None:
    print(f"review-relay: {' '.join(str(message).split())[:400]}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_relay.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--devtools",
        required=True,
        type=Path,
        help="the pinned chrome-devtools-mcp, which this server starts",
    )
    parser.add_argument(
        "--filesystem-root",
        required=True,
        type=Path,
        help="chrome-devtools-mcp's working directory and its only file root",
    )
    parser.add_argument(
        "--refusals",
        required=True,
        type=Path,
        help="the refusal log this server creates; an existing file is refused",
    )
    return parser


def _catch_signals(stopping: threading.Event) -> dict[int, Any]:
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for number in _STOP_SIGNALS:
            previous[number] = signal.signal(number, lambda *_: stopping.set())
    return previous


def _restore_signals(previous: Mapping[int, Any]) -> None:
    for number, handler in previous.items():
        signal.signal(number, signal.SIG_DFL if handler is None else handler)


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    read: Callable[[int], bytes] | None = None,
    stream: Any = None,
) -> int:
    """Serve the review's browser until the client goes or a stop signal arrives.

    A refusal log that cannot be created stops the server before chrome-devtools-mcp
    starts, since a refusal it could not record would go unseen. A child that cannot
    be started leaves the server serving no tool, with a line on stderr, where
    Claude Code keeps an MCP server's log.
    """
    args = _parser().parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        refusals = RefusalLog(args.refusals)
    except OSError as error:
        _say(f"--refusals {args.refusals}: {error.strerror or type(error).__name__}")
        return 1
    try:
        child, listed = story_relay.start_child(
            args.devtools,
            home=Path(env.get("HOME") or args.filesystem_root),
            fs_root=args.filesystem_root,
            search_path=env.get("PATH") or agent.DEFAULT_SEARCH_PATH,
            client_name=SERVER_NAME,
        )
    except (OSError, story_relay.RelayError) as error:
        _say(f"the browser could not start: {error}")
        child, listed = None, {}
    relay = ReviewRelay(child, listed=listed, refusals=refusals)
    if read is None:
        read = functools.partial(os.read, sys.stdin.fileno())
    client = Client(read, sys.stdout if stream is None else stream, relay)
    stopping = threading.Event()
    previous = _catch_signals(stopping)
    try:
        serve(relay, client, stopping=stopping)
    finally:
        relay.close()
        refusals.close()
        _restore_signals(previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
