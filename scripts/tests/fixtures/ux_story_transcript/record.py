# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Record the Claude Code session files ``test_ux_test_stories_transcript.py`` reads.

Run it inside the UX runner image, offline, from the repository root, with the
image tag ``run_in_docker.py`` builds for the checkout:

    docker run --rm --network none -v "$PWD/scripts:/repo/scripts" \\
        idp-ux-runner:<tag> python3 -B /repo/scripts/tests/fixtures/ux_story_transcript/record.py

It runs the pinned Claude Code TUI three times in a pseudo-terminal, each time
with a fresh ``HOME``, against a fake Anthropic Messages API on loopback and the
stand-in story server ``fake_story_server.py``, and writes beside itself:

* ``unassisted.jsonl``, a session started as the pair command ``run_in_docker.py``
  prints it (``--bare``, ``--strict-mcp-config``, ``--tools=``, the story tool
  granted and the brief in ``--append-system-prompt-file``), whose opening
  message is typed once the server is up: one tool call and its result, a
  summary, then Ctrl-C;
* ``assisted.jsonl``, the same session, then a message a person typed after the
  summary, then ``/exit``;
* ``positional.jsonl``, a session started as ``agent.interactive_argv`` starts
  one, with the opening message as its last argument after ``--``, left with
  Ctrl-C once it has answered;
* ``probe.json``, what the pinned CLI sent: for the pair command, the built-in
  tools its requests offered, the story tools, and whether the file's text
  reached the system prompt; for the positional opening message, the story tools
  its first turn offered; whether ``--bare`` still applies the ``env`` of the
  user's ``settings.json`` (one ``-p`` request whose API address and key come from
  there alone); and, for each recorded session, the usage of every reply the fake
  API served.

No network, credential, stack or product is involved: the API key is a fake that
nothing outside this container accepts, and every message is a fixed sentence.
Each session file is kept line for line, with two kinds of value replaced so that
a re-recording differs only where the CLI did: every UUID by a fixed one, the same
UUID by the same one throughout, and the session's temporary directory by
``/tmp/ux-transcript``.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import queue
import re
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CLAUDE = Path("/opt/ux-runner/node_modules/.bin/claude")
VERSIONS = Path("/opt/ux-runner/VERSIONS.json")
TOOL = "mcp__ux-story__report_done"
MODEL = "claude-opus-5-5"
FAKE_KEY = "recorded-fixture-value-that-no-api-accepts"
KICKOFF = "KICKOFF: a recorded opening message for one story flow."
TYPED = "KEEP_GOING: a message a person typed after the opening one."
OPENING = "Begin the task described in your instructions."
MARKER = "PAIR_PROMPT_MARKER: the brief and the task arrive in this file."
SUMMARY = "SUMMARY: I reported the task done; this is one tester's account."
NOTED = "NOTED: carrying on from the typed message."
UNTOOLED = "UNTOOLED: no story tool was offered with this message."
SIDE = "ok"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SESSION_SECONDS = 120.0
STARTUP_SECONDS = 8.0


class FakeApi(ThreadingHTTPServer):
    """The Messages API on loopback, answering each request from a fixed script."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.served: queue.Queue[str] = queue.Queue()
        self.requests: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.number = 0

    def next_number(self) -> int:
        with self.lock:
            self.number += 1
            return self.number


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _has_tool_result(content: object) -> bool:
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _last_user(body: dict[str, Any]) -> object:
    """The content of the request's last user message; a system one may follow it."""
    for message in reversed(body.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return message.get("content")
    return None


def scripted(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str]:
    """``(name, content blocks, stop reason)`` of the reply to one request."""
    tools = [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)]
    last = _last_user(body)
    said = _text_of(last)
    if "<session>" in said:
        return "side", [{"type": "text", "text": SIDE}], "end_turn"
    if TOOL not in tools:
        return "untooled", [{"type": "text", "text": UNTOOLED}], "end_turn"
    if _has_tool_result(last):
        return "summary", [{"type": "text", "text": SUMMARY}], "end_turn"
    if TYPED in said:
        return "noted", [{"type": "text", "text": NOTED}], "end_turn"
    call = {
        "type": "tool_use",
        "id": "toolu_recorded_0001",
        "name": TOOL,
        "input": {
            "evidence": "The page says the task is done.",
            "stopped_at_commit_point": False,
        },
    }
    return "tool", [call], "tool_use"


def usage_for(number: int) -> dict[str, int]:
    """The usage the fake API reports for its ``number``-th reply."""
    return {
        "input_tokens": 3 + number,
        "cache_creation_input_tokens": 1000 * number,
        "cache_read_input_tokens": 700 * number,
        "output_tokens": 20 + number,
    }


def _system_text(system: object) -> str:
    return system if isinstance(system, str) else _text_of(system)


class _Handler(BaseHTTPRequestHandler):
    server: FakeApi

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, value: object) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.server.paths.append(f"GET {self.path}")
        self._json(404, {"type": "error", "error": {"type": "not_found_error"}})

    def do_POST(self) -> None:
        size = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(size) or b"{}")
        self.server.paths.append(
            f"POST {self.path} keys={sorted(body)} model={body.get('model')} "
            f"tools={[t.get('name') for t in body.get('tools') or []]} "
            f"messages={len(body.get('messages') or [])} "
            f"last={str((body.get('messages') or [{}])[-1])[:200]}"
        )
        path = self.path.split("?", 1)[0]
        if path.endswith("/count_tokens"):
            self._json(200, {"input_tokens": 100})
            return
        if not path.endswith("/v1/messages"):
            self._json(404, {"type": "error", "error": {"type": "not_found_error"}})
            return
        name, content, stop = scripted(body)
        number = self.server.next_number()
        usage = usage_for(number)
        identifier = f"msg_recorded_{number:04d}"
        tools = [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)]
        with self.server.lock:
            self.server.requests.append(
                {
                    "reply": name,
                    "builtin_tools": sorted(
                        str(t) for t in tools if not str(t).startswith("mcp__")
                    ),
                    "story_tools": sorted(
                        str(t) for t in tools if str(t).startswith("mcp__")
                    ),
                    "system_has_marker": MARKER in _system_text(body.get("system")),
                }
            )
            if name != "side":
                self.server.replies.append(
                    {"reply": name, "id": identifier, "usage": usage}
                )
        message = {
            "id": identifier,
            "type": "message",
            "role": "assistant",
            "model": str(body.get("model") or MODEL),
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage,
        }
        if body.get("stream"):
            self._stream(message)
        else:
            self._json(200, message)
        self.server.served.put(name)

    def _event(self, kind: str, data: dict[str, Any]) -> None:
        payload = json.dumps({"type": kind, **data})
        self.wfile.write(f"event: {kind}\ndata: {payload}\n\n".encode())
        self.wfile.flush()

    def _stream(self, message: dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("request-id", message["id"].replace("msg_", "req_"))
        self.end_headers()
        usage = dict(message["usage"])
        start = {**message, "content": [], "stop_reason": None}
        start["usage"] = {**usage, "output_tokens": 1}
        self._event("message_start", {"message": start})
        for index, block in enumerate(message["content"]):
            if block["type"] == "text":
                self._event(
                    "content_block_start",
                    {"index": index, "content_block": {"type": "text", "text": ""}},
                )
                self._event(
                    "content_block_delta",
                    {
                        "index": index,
                        "delta": {"type": "text_delta", "text": block["text"]},
                    },
                )
            else:
                opened = {**block, "input": {}}
                self._event(
                    "content_block_start", {"index": index, "content_block": opened}
                )
                self._event(
                    "content_block_delta",
                    {
                        "index": index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(block["input"]),
                        },
                    },
                )
            self._event("content_block_stop", {"index": index})
        self._event(
            "message_delta",
            {
                "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": usage["output_tokens"]},
            },
        )
        self._event("message_stop", {})


def _home(root: Path, cwd: Path) -> Path:
    """A fresh HOME whose Claude Code state has onboarding and the folder's trust done."""
    home = root / "home"
    home.mkdir()
    state = {
        "hasCompletedOnboarding": True,
        "theme": "dark",
        "customApiKeyResponses": {"approved": [FAKE_KEY[-20:]], "rejected": []},
        "projects": {
            str(cwd): {
                "hasTrustDialogAccepted": True,
                "hasCompletedProjectOnboarding": True,
                "allowedTools": [],
            }
        },
    }
    (home / ".claude.json").write_text(json.dumps(state), encoding="utf-8")
    return home


def _env(home: Path, port: int) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_API_KEY": FAKE_KEY,
        "ANTHROPIC_MODEL": MODEL,
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def _config(root: Path) -> Path:
    path = root / "mcp.json"
    server = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-I", "-B", str(HERE / "fake_story_server.py")],
        "env": {},
    }
    path.write_text(json.dumps({"mcpServers": {"ux-story": server}}), encoding="utf-8")
    return path


class Terminal:
    """One process in a pseudo-terminal, its output drained so it never blocks."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path) -> None:
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(cwd)
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
            os.execve(argv[0], argv, env)
        self.pid = pid
        self.fd = fd
        self.output = bytearray()
        self.code: int | None = None
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            self.output += data

    def type(self, text: str) -> None:
        for character in text:
            os.write(self.fd, character.encode("utf-8"))
            time.sleep(0.02)

    def exited(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.code = os.waitstatus_to_exitcode(status)
                return True
            time.sleep(0.2)
        return False

    def end(self) -> None:
        if self.code is None and not self.exited(0):
            for keys in ("\x03", "\x03"):
                self.type(keys)
                time.sleep(1.0)
            if not self.exited(15):
                os.kill(self.pid, 9)
                self.exited(5)


def _wait(api: FakeApi, name: str, seconds: float = SESSION_SECONDS) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if api.served.get(timeout=1.0) == name:
                return
        except queue.Empty:
            continue
    raise SystemExit(f"the session never reached the {name!r} reply")


def _transcript(home: Path) -> Path:
    found = sorted((home / ".claude" / "projects").glob("*/*.jsonl"))
    if len(found) != 1:
        raise SystemExit(f"expected one session file under {home}, found {found}")
    return found[0]


def session(
    api: FakeApi,
    argv: Callable[[Path], list[str]],
    drive: Callable[[Terminal], None],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Run one session; its normalised session file, the requests and the replies."""
    with api.lock:
        api.requests.clear()
        api.replies.clear()
    root = Path(tempfile.mkdtemp(prefix="ux-transcript-"))
    cwd = root / "work"
    cwd.mkdir(mode=0o700)
    home = _home(root, cwd)
    terminal = Terminal(argv(root), _env(home, api.server_address[1]), cwd)
    try:
        drive(terminal)
    except SystemExit:
        shown = bytes(terminal.output[-4000:]).decode("utf-8", "replace")
        print(shown, file=sys.stderr)
        print(
            json.dumps({"paths": api.paths, "requests": api.requests}), file=sys.stderr
        )
        raise
    finally:
        terminal.end()
    with api.lock:
        text = normalised(_transcript(home), root)
        return text, list(api.requests), list(api.replies)


def positional(root: Path) -> list[str]:
    """``agent.interactive_argv``'s shape: the opening message after ``--``."""
    return [
        str(CLAUDE),
        "--bare",
        "--strict-mcp-config",
        "--mcp-config",
        str(_config(root)),
        "--permission-mode",
        "manual",
        "--tools",
        "",
        "--allowedTools",
        TOOL,
        "--disallowedTools",
        "Bash",
        "Read",
        "--",
        KICKOFF,
    ]


def paired(root: Path) -> list[str]:
    """The pair command's shape, as ``run_in_docker.pair_command`` prints it."""
    prompt = root / "pair-prompt.md"
    prompt.write_text(MARKER + "\n", encoding="utf-8")
    return [
        str(CLAUDE),
        "--bare",
        "--strict-mcp-config",
        "--mcp-config",
        str(_config(root)),
        "--tools=",
        "--allowedTools",
        TOOL,
        "--permission-mode",
        "manual",
        "--append-system-prompt-file",
        str(prompt),
    ]


def normalised(source: Path, root: Path) -> str:
    """The session file with its UUIDs and its temporary directory made fixed."""
    names: dict[str, str] = {}

    def fixed(match: re.Match[str]) -> str:
        found = match.group(0)
        if found not in names:
            names[found] = (
                f"{len(names) + 1:08x}-cafe-4bad-8bad-ffffffff{len(names) + 1:04x}"
            )
        return names[found]

    def walk(value: object) -> object:
        if isinstance(value, str):
            return UUID_RE.sub(fixed, value.replace(str(root), "/tmp/ux-transcript"))
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    lines = source.read_text(encoding="utf-8").splitlines()
    return "".join(
        json.dumps(walk(json.loads(line)), separators=(",", ":"), ensure_ascii=False)
        + "\n"
        for line in lines
        if line.strip()
    )


def bare_applies_settings_env(api: FakeApi) -> bool:
    """Whether ``claude -p --bare`` reaches the API named only by settings.json's env."""
    with api.lock:
        api.requests.clear()
    root = Path(tempfile.mkdtemp(prefix="ux-transcript-"))
    cwd = root / "work"
    cwd.mkdir(mode=0o700)
    home = _home(root, cwd)
    (home / ".claude").mkdir(exist_ok=True)
    env = _env(home, api.server_address[1])
    settings = {
        "env": {
            name: env.pop(name) for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY")
        }
    }
    (home / ".claude" / "settings.json").write_text(json.dumps(settings))
    subprocess.run(  # nosec B603 - fixed argv, no shell
        [str(CLAUDE), "-p", "--bare", "--", OPENING],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=SESSION_SECONDS,
        check=False,
    )
    with api.lock:
        return any(r["reply"] != "side" for r in api.requests)


def _main_turns(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in requests if r["reply"] != "side"]


def main() -> int:
    if not CLAUDE.exists():
        raise SystemExit(f"{CLAUDE} is missing; run this inside the UX runner image")
    api = FakeApi()
    threading.Thread(target=api.serve_forever, daemon=True).start()

    def opened(terminal: Terminal) -> None:
        time.sleep(STARTUP_SECONDS)
        terminal.type(OPENING)
        time.sleep(0.5)
        terminal.type("\r")
        _wait(api, "summary")
        time.sleep(3.0)

    def assisted(terminal: Terminal) -> None:
        opened(terminal)
        terminal.type(TYPED)
        time.sleep(0.5)
        terminal.type("\r")
        _wait(api, "noted")
        time.sleep(3.0)
        terminal.type("/exit")
        time.sleep(1.0)
        terminal.type("\r")
        terminal.exited(20)

    def answered(terminal: Terminal) -> None:
        _wait(api, "untooled")
        time.sleep(3.0)

    first, first_requests, first_replies = session(api, paired, opened)
    (HERE / "unassisted.jsonl").write_text(first, encoding="utf-8")
    second, _, second_replies = session(api, paired, assisted)
    (HERE / "assisted.jsonl").write_text(second, encoding="utf-8")
    third, third_requests, third_replies = session(api, positional, answered)
    (HERE / "positional.jsonl").write_text(third, encoding="utf-8")
    versions = json.loads(VERSIONS.read_text(encoding="utf-8"))
    pair_turns = _main_turns(first_requests)
    positional_turns = _main_turns(third_requests)
    facts = {
        "claude_code": versions.get("claude_code"),
        "pair_command": {
            "turns": len(pair_turns),
            "builtin_tools": sorted(
                {t for r in pair_turns for t in r["builtin_tools"]}
            ),
            "story_tools": sorted({t for r in pair_turns for t in r["story_tools"]}),
            "system_has_marker": bool(pair_turns)
            and all(r["system_has_marker"] for r in pair_turns),
        },
        "positional_opening": {
            "first_turn_story_tools": positional_turns[0]["story_tools"]
            if positional_turns
            else None,
        },
        "bare_applies_settings_env": bare_applies_settings_env(api),
        "served": {
            "unassisted": first_replies,
            "assisted": second_replies,
            "positional": third_replies,
        },
    }
    (HERE / "probe.json").write_text(
        json.dumps(facts, indent=2) + "\n", encoding="utf-8"
    )
    shown = ("pair_command", "positional_opening", "bare_applies_settings_env")
    print(json.dumps({key: facts[key] for key in shown}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
