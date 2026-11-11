# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""What the review session can read and what it loads, measured on the real CLI.

``scripts/sdlc/ai_mr_review.py`` runs ``claude -p`` with the argv
:func:`claude_command` builds. Whether that argv keeps the session inside the
worktree, and keeps the worktree's own agent configuration from loading, is a
property of the CLI's permission engine, so this test runs the CLI rather than
reading the flags.

No model is called. A scripted Messages API on ``127.0.0.1`` answers each turn
with the next tool call from a fixed list (a ``Read`` of ``/etc/passwd``, a
``Grep`` over ``/proc/self/environ``, ...), and records the tool results the CLI
sends back, which is where a refusal or the file's content shows up. The CLI is
pointed at it with ``ANTHROPIC_BASE_URL``; the review itself runs on Bedrock, and
the permission engine does not depend on the provider. The server listens on the
loopback interface only, on a port the OS picks, for the length of one test.

Runs offline whenever a usable CLI at the pinned version is present: the
``claude`` on ``PATH``, or ``AI_REVIEW_CLAUDE_BIN``. It skips, saying why,
otherwise — neither CI job that runs this suite installs the CLI, so in CI it
skips. ``AI_REVIEW_PROBE_ANY_VERSION=1`` runs it against another version, which is
how to check a version before moving the pin.

Each case has a positive control (a file inside the worktree must be served), so
a CLI that refuses everything cannot pass.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "sdlc" / "ai_mr_review.py"
GITLAB_CI = REPO_ROOT / ".gitlab-ci.yml"
CANARY = "SESSIONSCOPECANARY4417"


def _module():
    spec = importlib.util.spec_from_file_location("ai_mr_review", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pinned_version() -> str:
    jobs = yaml.safe_load(GITLAB_CI.read_text(encoding="utf-8"))
    return str(jobs["ai_mr_review"]["variables"]["CLAUDE_CODE_VERSION"])


@pytest.fixture(scope="module")
def cli() -> str:
    binary = os.environ.get("AI_REVIEW_CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        pytest.skip(
            "no `claude` CLI here; set AI_REVIEW_CLAUDE_BIN to an install of the "
            "pinned version to run this measurement"
        )
    version = subprocess.run(  # noqa: S603 - fixed argv
        [binary, "--version"],
        env={"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    found = re.search(r"\d+\.\d+\.\d+", version.stdout)
    if version.returncode != 0 or not found:
        pytest.skip(
            f"{binary} does not run with a minimal environment (exit "
            f"{version.returncode}), so it cannot be pointed at the scripted "
            f"endpoint; set AI_REVIEW_CLAUDE_BIN to a plain npm install"
        )
    pinned = _pinned_version()
    if (
        found.group(0) != pinned
        and os.environ.get("AI_REVIEW_PROBE_ANY_VERSION") != "1"
    ):
        pytest.skip(
            f"{binary} is {found.group(0)} and the CI pin is {pinned}; a measurement "
            f"of another version says nothing about the pin. Set "
            f"AI_REVIEW_CLAUDE_BIN to a {pinned} install, or "
            f"AI_REVIEW_PROBE_ANY_VERSION=1 to measure this one"
        )
    return binary


class _Script:
    """A Messages API that answers turn ``n`` with tool call ``n``."""

    def __init__(self, calls: list[dict]) -> None:
        self.calls = calls
        self.bodies: list[dict] = []
        self._lock = threading.Lock()
        self._turn = 0
        script = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 - http.server's naming
                self._send_json(b"{}")

            def _send_json(self, payload: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:  # noqa: N802 - http.server's naming
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if "count_tokens" in self.path:
                    self._send_json(b'{"input_tokens": 1}')
                    return
                with script._lock:
                    script.bodies.append(body)
                    turn = script._turn
                    if body.get("tools"):
                        script._turn += 1
                self._stream(body, turn if body.get("tools") else len(script.calls))

            def _stream(self, body: dict, turn: int) -> None:
                message = {
                    "id": f"msg_{turn}",
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model", "scripted"),
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
                if turn < len(script.calls):
                    call = script.calls[turn]
                    block = {
                        "type": "tool_use",
                        "id": f"toolu_{turn:04d}",
                        "name": call["name"],
                        "input": {},
                    }
                    delta = {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(call["input"]),
                    }
                    stop = "tool_use"
                else:
                    block = {"type": "text", "text": ""}
                    delta = {"type": "text_delta", "text": "done"}
                    stop = "end_turn"
                events = [
                    ("message_start", {"type": "message_start", "message": message}),
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": block,
                        },
                    ),
                    (
                        "content_block_delta",
                        {"type": "content_block_delta", "index": 0, "delta": delta},
                    ),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    (
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": stop, "stop_sequence": None},
                            "usage": {"output_tokens": 1},
                        },
                    ),
                    ("message_stop", {"type": "message_stop"}),
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for name, data in events:
                    self.wfile.write(
                        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
                    )
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _Script:
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def results(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for body in self.bodies:
            for message in body.get("messages", []):
                content = message.get("content")
                if isinstance(content, list):
                    for item in content:
                        if item.get("type") == "tool_result":
                            found[item["tool_use_id"]] = str(item.get("content"))
        return found


def _run_session(cli: str, worktree: Path, home: Path, calls: list[dict]) -> _Script:
    mod = _module()
    with _Script(calls) as script:
        env = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", ""),
            "ANTHROPIC_BASE_URL": script.url,
            "ANTHROPIC_API_KEY": "sk-scripted",
            "CI": "true",
            "SESSION_SCOPE_CANARY": CANARY,
        }
        result = subprocess.run(  # noqa: S603 - the review's own argv
            mod.claude_command(cli, "scripted session"),
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    assert script.bodies, (
        f"the CLI made no request to the scripted endpoint, so the argv was refused "
        f"before any session started (exit {result.returncode}): "
        f"{result.stderr[-600:]}"
    )
    return script


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "runner"
    worktree = root / "work" / "review"
    worktree.mkdir(parents=True)
    temp = root / "_temp"
    temp.mkdir()
    home = root / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text(f"[default]\n{CANARY}\n")
    return root, worktree, home


@pytest.mark.unit
def test_the_session_reads_only_the_worktree(cli: str, tmp_path: Path) -> None:
    root, worktree, home = _layout(tmp_path)
    (worktree / "inside.txt").write_text(f"inside {CANARY}\n")
    (worktree / "link.txt").symlink_to("/etc/passwd")
    for name in ("dotdot", "relative", "absolute", "grep"):
        (root / "_temp" / f"{name}.txt").write_text(f"temp {CANARY}\n")
    parent = os.getpid()
    cases = [
        ("inside the worktree", "Read", {"file_path": str(worktree / "inside.txt")}),
        ("/etc/passwd", "Read", {"file_path": "/etc/passwd"}),
        ("/proc/self/environ", "Read", {"file_path": "/proc/self/environ"}),
        ("/proc/<ppid>/environ", "Read", {"file_path": f"/proc/{parent}/environ"}),
        (
            "<worktree>/../",
            "Read",
            {"file_path": f"{worktree}/../../_temp/dotdot.txt"},
        ),
        ("../ relative", "Read", {"file_path": "../../_temp/relative.txt"}),
        ("runner _temp", "Read", {"file_path": str(root / "_temp" / "absolute.txt")}),
        ("~/.aws/credentials", "Read", {"file_path": str(home / ".aws/credentials")}),
        ("symlink to /etc/passwd", "Read", {"file_path": str(worktree / "link.txt")}),
        (
            "Grep /etc/passwd",
            "Grep",
            {"pattern": "root:", "path": "/etc/passwd", "output_mode": "content"},
        ),
        (
            "Grep /proc/self/environ",
            "Grep",
            {
                "pattern": "SESSION_SCOPE_CANARY",
                "path": "/proc/self/environ",
                "output_mode": "content",
            },
        ),
        (
            "Grep runner _temp",
            "Grep",
            {"pattern": CANARY, "path": str(root / "_temp"), "output_mode": "content"},
        ),
        ("Glob /etc", "Glob", {"pattern": "passwd", "path": "/etc"}),
    ]
    script = _run_session(
        cli,
        worktree,
        home,
        [{"name": tool, "input": arguments} for _, tool, arguments in cases],
    )
    results = script.results()

    def served(index: int) -> bool:
        text = results.get(f"toolu_{index:04d}", "")
        listed = re.search(r"(?m)^/etc/(?:pam\.d/)?passwd\s*$", text)
        return CANARY in text or "root:x:0" in text or bool(listed)

    assert served(0), f"the control read was refused: {results.get('toolu_0000')!r}"
    leaked = [
        label for index, (label, _, _) in enumerate(cases) if index and served(index)
    ]
    assert not leaked, f"served outside the worktree: {leaked}"
    assert len(results) == len(cases), (
        f"{len(cases) - len(results)} call(s) got no tool result at all"
    )


@pytest.mark.unit
def test_the_worktree_s_agent_configuration_does_not_load(
    cli: str, tmp_path: Path
) -> None:
    """Planted with no removal step, so this measures the flags alone."""
    _, worktree, home = _layout(tmp_path)
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)  # noqa: S603, S607
    planted = {
        "CLAUDE.md": "plain",
        ".claude/CLAUDE.md": "plain",
        "CLAUDE.local.md": "plain",
        "sub/CLAUDE.md": "plain",
        "AGENTS.md": "plain",
        ".claude/rules/r.md": "plain",
        ".claude/rules/deep/r.md": "plain",
        ".claude/skills/s/SKILL.md": "frontmatter",
        ".claude/commands/c.md": "frontmatter",
        ".claude/agents/a.md": "frontmatter",
        ".claude/output-styles/o.md": "frontmatter",
    }
    tokens = {}
    for index, (relative, shape) in enumerate(planted.items()):
        token = f"LOADCANARY{index:02d}X"
        tokens[relative] = token
        path = worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: n{index}\ndescription: {token}\n---\n{token}\n"
            if shape == "frontmatter"
            else f"{token}\n"
        )
    (worktree / "sub" / "file.txt").write_text("CONTROLREAD\n")
    ran = tmp_path / "hook-ran"
    (worktree / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    event: [
                        {
                            "matcher": "*",
                            "hooks": [{"type": "command", "command": f"touch {ran}"}],
                        }
                    ]
                    for event in ("PreToolUse", "SessionStart")
                }
            }
        )
    )
    script = _run_session(
        cli,
        worktree,
        home,
        [{"name": "Read", "input": {"file_path": str(worktree / "sub" / "file.txt")}}],
    )
    sent = json.dumps(script.bodies)
    assert "CONTROLREAD" in sent, "the control read inside the worktree was refused"
    loaded = [relative for relative, token in tokens.items() if token in sent]
    assert not loaded, f"reached the model: {loaded}"
    assert not ran.exists(), "a hook from the worktree's .claude/settings.json ran"
