# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Record the chrome-devtools-mcp answers ``test_ux_story_relay.py`` replays.

Run it inside the UX runner image, offline, from the repository root, with the
image tag ``run_in_docker.py`` builds for the checkout:

    docker run --rm --network none -v "$PWD/scripts:/repo/scripts" \\
        idp-ux-runner:<tag> python3 -B /repo/scripts/tests/fixtures/ux_story_relay/record.py

It serves the pages in ``pages/`` (and a generated long list) through the harness's
own egress proxy, so Chrome reaches them exactly as it reaches an MR's build,
starts the pinned Chrome for Testing with ``chrome.chrome_flags``, and starts the
pinned chrome-devtools-mcp exactly as ``agent.mcp_servers`` starts it. It speaks to
the server as the relay does, with no ``roots`` capability, and writes three files
beside itself: ``tools_list.json`` and ``tools_list_default_flags.json``, the
server's ``tools/list`` under the harness's flags and under none, and
``session.json``, every recorded call's answer. No stack, no sign-in and no network
is involved, and no answer keeps a screenshot's pixels: each image's data is
replaced, because the suite substitutes a synthetic JPEG of its own.
"""

from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2]
sys.path.insert(0, str(SCRIPTS))

from ux_runner import agent, chrome, paths  # noqa: E402

BASE = "http://localhost:3000"
IMAGE_DATA = "(a recorded JPEG, not kept)"
LONG_ROWS = 150
PROTOCOL = "2025-06-18"
CLIENT = {"name": "ux-story", "version": "1"}


class Client:
    """A stdio MCP client of one server, keeping every message both ways."""

    def __init__(self, argv: list[str], log: Path) -> None:
        self._log = log.open("wb")
        self.proc = subprocess.Popen(  # nosec B603 - fixed argv, no shell
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            env={},
        )
        self.inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.next_id = 1
        self.server_requests: list[str] = []
        self.notifications: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        stream = self.proc.stdout
        if stream is None:
            return
        for raw in stream:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict):
                self.inbox.put(message)
        self.inbox.put(None)

    def send(self, message: dict[str, Any]) -> None:
        stream = self.proc.stdin
        if stream is None:
            raise SystemExit("the server has no stdin")
        stream.write((json.dumps(message) + "\n").encode())
        stream.flush()

    def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0
    ) -> dict[str, Any] | None:
        request_id = self.next_id
        self.next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.answer(request_id, timeout)

    def answer(self, request_id: int, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.inbox.get(timeout=deadline - time.monotonic())
            except queue.Empty:
                break
            if message is None:
                return None
            if "method" in message:
                self._served(message)
                continue
            if message.get("id") == request_id:
                return message
        return None

    def _served(self, message: dict[str, Any]) -> None:
        if "id" not in message:
            self.notifications.append(str(message.get("method")))
            return
        self.server_requests.append(str(message.get("method")))
        if message.get("method") == "ping":
            self.send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
        else:
            self.send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def handshake(self) -> dict[str, Any] | None:
        hello = self.request(
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT},
        )
        self.notify("notifications/initialized")
        return hello


def text_of(answer: dict[str, Any] | None) -> str:
    result = (answer or {}).get("result") or {}
    return "\n".join(
        item.get("text", "")
        for item in result.get("content", [])
        if item.get("type") == "text"
    )


def uid_of(text: str, role: str, name: str) -> str:
    found = re.search(rf'uid=(\S+) {re.escape(role)} "{re.escape(name)}"', text)
    if found is None:
        raise SystemExit(f"no {role} {name!r} in the snapshot")
    return found.group(1)


def without_pixels(answer: dict[str, Any] | None) -> dict[str, Any] | None:
    if answer is None:
        return None
    kept = json.loads(json.dumps(answer))
    for item in (kept.get("result") or {}).get("content", []):
        if item.get("type") == "image":
            item["data"] = IMAGE_DATA
    return kept


def write(name: str, value: object) -> None:
    text = json.dumps(value, indent=1, ensure_ascii=False) + "\n"
    (HERE / name).write_text(text, encoding="utf-8")


def site(root: Path) -> Path:
    built = root / "site"
    shutil.copytree(HERE / "pages", built)
    rows = "\n".join(
        f'      <tr><td><a href="#doc-{n}">sample-{n:03d}.pdf</a></td>'
        "<td>COMPLETED</td><td>lending-package-sample</td></tr>"
        for n in range(1, LONG_ROWS + 1)
    )
    (built / "long.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        "<title>Document List</title>\n</head>\n<body>\n<main>\n"
        f"  <h1>Documents ({LONG_ROWS})</h1>\n  <table>\n"
        "    <thead><tr><th>Object name</th><th>Status</th><th>Configuration</th>"
        f"</tr></thead>\n    <tbody>\n{rows}\n    </tbody>\n  </table>\n</main>\n"
        "</body>\n</html>\n",
        encoding="utf-8",
    )
    return built


def wait_for_file(path: Path, what: str) -> None:
    for _ in range(200):
        if path.exists():
            return
        time.sleep(0.1)
    raise SystemExit(f"{what} did not start")


def wait_for_cdp() -> str:
    for _ in range(200):
        try:
            with urllib.request.urlopen(  # nosec B310 - a fixed loopback http URL
                f"http://{paths.CDP_HOST}:{paths.CDP_PORT}/json/version", timeout=1
            ) as answer:
                return str(json.loads(answer.read()).get("Browser"))
        except OSError:
            time.sleep(0.1)
    raise SystemExit("chrome did not start")


def record(client: Client) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    def call(label: str, tool: str, arguments: dict[str, Any]) -> str:
        answer = client.request("tools/call", {"name": tool, "arguments": arguments})
        calls[label] = {
            "tool": tool,
            "arguments": arguments,
            "answer": without_pixels(answer),
        }
        return text_of(answer)

    call(
        "navigate_documents",
        "navigate_page",
        {"type": "url", "url": f"{BASE}/documents.html"},
    )
    page = call("snapshot_documents", "take_snapshot", {})
    call("snapshot_documents_verbose", "take_snapshot", {"verbose": True})
    call("screenshot_jpeg", "take_screenshot", {"format": "jpeg", "quality": 70})
    call("click_refresh", "click", {"uid": uid_of(page, "button", "Refresh")})
    call(
        "click_refresh_with_snapshot",
        "click",
        {"uid": uid_of(page, "button", "Refresh"), "includeSnapshot": True},
    )
    call("hover_reprocess", "hover", {"uid": uid_of(page, "button", "Reprocess")})
    search = uid_of(page, "searchbox", "Find documents")
    call("fill_filter", "fill", {"uid": search, "value": "lending"})
    call("type_text", "type_text", {"text": " package"})
    call("press_key", "press_key", {"key": "Tab"})
    call(
        "fill_form",
        "fill_form",
        {
            "elements": [
                {"uid": search, "value": "rvl"},
                {
                    "uid": uid_of(page, "checkbox", "Show Test Studio runs"),
                    "value": "true",
                },
            ]
        },
    )
    call(
        "drag",
        "drag",
        {
            "from_uid": uid_of(page, "StaticText", "Status"),
            "to_uid": uid_of(page, "StaticText", "Configuration"),
        },
    )
    call("wait_for", "wait_for", {"text": ["Refresh"]})
    call(
        "click_user_menu",
        "click",
        {
            "uid": uid_of(page, "button", "new-analyst (Author)"),
            "includeSnapshot": True,
        },
    )
    call(
        "click_add_mapping",
        "click",
        {"uid": uid_of(page, "button", "Add prefix mapping")},
    )
    call("snapshot_dialog_open", "take_snapshot", {})
    call("click_stale_uid", "click", {"uid": "999_999"})
    call(
        "click_unknown_argument",
        "click",
        {"uid": uid_of(page, "button", "Refresh"), "bogus": 1},
    )
    call(
        "screenshot_file_path",
        "take_screenshot",
        {"format": "jpeg", "filePath": "shot.jpg"},
    )
    call(
        "navigate_file_url",
        "navigate_page",
        {"type": "url", "url": "file:///etc/hostname"},
    )
    call(
        "navigate_dialogs",
        "navigate_page",
        {"type": "url", "url": f"{BASE}/dialogs.html"},
    )
    page = call("snapshot_dialogs", "take_snapshot", {})
    call("click_discard", "click", {"uid": uid_of(page, "button", "Discard changes")})
    call("snapshot_while_confirm_open", "take_snapshot", {})
    call(
        "screenshot_while_confirm_open",
        "take_screenshot",
        {"format": "jpeg", "quality": 70},
    )
    call("handle_confirm_dismiss", "handle_dialog", {"action": "dismiss"})
    call(
        "click_summary",
        "click",
        {"uid": uid_of(page, "button", "Show processing summary")},
    )
    call("handle_alert_accept", "handle_dialog", {"action": "accept"})
    call("click_rename", "click", {"uid": uid_of(page, "button", "Rename")})
    call("handle_prompt_dismiss", "handle_dialog", {"action": "dismiss"})
    call("handle_no_dialog", "handle_dialog", {"action": "accept"})
    call(
        "navigate_signin",
        "navigate_page",
        {"type": "url", "url": f"{BASE}/signin.html"},
    )
    call("snapshot_signin", "take_snapshot", {})
    call(
        "navigate_account",
        "navigate_page",
        {"type": "url", "url": f"{BASE}/account.html"},
    )
    call("snapshot_account", "take_snapshot", {})
    call("navigate_long", "navigate_page", {"type": "url", "url": f"{BASE}/long.html"})
    call("snapshot_long", "take_snapshot", {})
    call("navigate_back", "navigate_page", {"type": "back"})
    return calls


def cancellation(client: Client) -> dict[str, Any]:
    request_id = client.next_id
    client.next_id += 1
    client.send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "wait_for",
                "arguments": {"text": ["never appears"], "timeout": 15000},
            },
        }
    )
    time.sleep(1.0)
    client.notify(
        "notifications/cancelled", {"requestId": request_id, "reason": "recorded"}
    )
    late = client.answer(request_id, 25.0)
    after = client.request("ping")
    return {
        "cancelled_request_answered": late is not None,
        "ping_after_cancel": after.get("result") if after else None,
    }


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="ux-relay-record-"))
    served = site(work)
    rules = work / "rules.json"
    rules.write_text(json.dumps({"version": 1, "hosts": []}), encoding="utf-8")
    ready = work / "proxy.ready"
    proxy = subprocess.Popen(  # nosec B603 - fixed argv, no shell
        [
            sys.executable,
            "-I",
            "-B",
            str(SCRIPTS / "ux_test_proxy.py"),
            "--rules",
            str(rules),
            "--build-dir",
            str(served),
            "--ready-file",
            str(ready),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for_file(ready, "the egress proxy")
    toolchain = paths.toolchain()
    profile = work / "profile"
    profile.mkdir()
    flags = chrome.chrome_flags(profile_dir=profile)
    browser = subprocess.Popen(  # nosec B603 - fixed argv, no shell
        [str(toolchain.chrome), *flags],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    browser_version = wait_for_cdp()
    home = work / "home"
    fs_root = work / "mcp-files"
    home.mkdir(mode=0o700)
    fs_root.mkdir(mode=0o700)
    servers = agent.mcp_servers(
        toolchain=toolchain,
        recorder_adapter=work / "unused",
        recorder_out=work / "unused",
        python=sys.executable,
        home=home,
        fs_root=fs_root,
    )
    entry = servers["chrome-devtools"]
    argv = [entry["command"], *entry["args"]]
    client = Client(argv, work / "devtools.log")
    hello = client.handshake()
    listed = client.request("tools/list")
    calls = record(client)
    cancelled = cancellation(client)
    stream = client.proc.stdin
    closed_at = time.monotonic()
    if stream is not None:
        stream.close()
    try:
        exit_code: int | None = client.proc.wait(timeout=20)
        exit_seconds: float | None = round(time.monotonic() - closed_at, 2)
    except subprocess.TimeoutExpired:
        client.proc.kill()
        exit_code, exit_seconds = None, None
    written = sorted(
        str(path.relative_to(work))
        for directory in (home, fs_root)
        for path in directory.rglob("*")
    )
    bare = Client(
        argv[: argv.index(str(toolchain.devtools_mcp)) + 1], work / "default.log"
    )
    bare.handshake()
    default_listed = bare.request("tools/list")
    bare.proc.kill()
    browser.terminate()
    proxy.terminate()
    write("tools_list.json", (listed or {}).get("result"))
    write("tools_list_default_flags.json", (default_listed or {}).get("result"))
    write(
        "session.json",
        {
            "recorded": {
                "versions": toolchain.versions(),
                "browser": browser_version,
                "chrome_flags": [flag.replace(str(work), "<work>") for flag in flags],
                "devtools_argv": [part.replace(str(work), "<work>") for part in argv],
                "pages": sorted(path.name for path in served.glob("*.html")),
                "image_data": IMAGE_DATA,
            },
            "initialize": (hello or {}).get("result"),
            "calls": calls,
            "observed": {
                **cancelled,
                "server_requests": client.server_requests,
                "server_notifications": client.notifications,
                "exit_after_stdin_close": {
                    "returncode": exit_code,
                    "seconds": exit_seconds,
                },
                "files_written_under_home_and_root": written,
            },
        },
    )
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
