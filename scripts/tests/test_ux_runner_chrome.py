# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the confined Chrome, ``ux_runner/chrome.py`` and its policy.

The flag list and the managed policy are pinned exactly, and against each other,
because together they are what routes every request through the egress proxy.
The DevTools helpers run against a real HTTP server on an ephemeral port, with an
environment proxy set that would break them if they honoured it. The two
self-tests and the socket audit run against fakes: a scripted CDP session, a
refusal log written by a timer thread, and a ``/proc/net`` directory written
here. No Chrome is started.
"""

from __future__ import annotations

import http.server
import ipaddress
import json
import os
import shutil
import socket
import socketserver
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import chrome, egress, paths, processes  # noqa: E402

POLICY_PATH = SCRIPTS / "ux_runner" / "chrome_policy.json"
BROWSER_UID = 10001
PROXY_UID = 10002
UNSPECIFIED = str(ipaddress.IPv4Address(0))


@pytest.fixture(autouse=True)
def not_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as a user that cannot switch uids, whoever runs the suite.

    CI runs these tests as root, where a real ``chown`` to ``uxproxy`` or
    ``uxbrowser`` would fail on an image without those users. The tests that are
    about the uid switch turn it back on themselves.
    """
    monkeypatch.setattr(processes, "can_switch_user", lambda: False)


class FakeSession:
    """A CDP session answering each method from a table, recording every call."""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        reply = self.replies.get(method, {})
        if isinstance(reply, BaseException):
            raise reply
        return reply


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _DevToolsServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self) -> None:
        self.targets: Any = []
        super().__init__(("127.0.0.1", 0), _DevToolsHandler)

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = int(self.server_address[1])


class _DevToolsHandler(http.server.BaseHTTPRequestHandler):
    """``/json/version`` and ``/json/list``, as Chrome's DevTools HTTP side serves."""

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, _DevToolsServer)
        if self.path == "/json/version":
            payload: Any = {"Browser": "Chrome/150.0.0.0", "Protocol-Version": "1.3"}
        elif self.path == "/json/list":
            payload = server.targets
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return None


@pytest.fixture
def devtools() -> Iterator[_DevToolsServer]:
    server = _DevToolsServer()
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    yield server
    server.shutdown()
    server.server_close()


class _TrapHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _TrapProxy)
        server.hits += 1
        self.rfile.readline()
        self.wfile.write(
            b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )


class _TrapProxy(socketserver.ThreadingTCPServer):
    """An HTTP proxy that answers 502 at once and counts who asked it."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self) -> None:
        self.hits = 0
        super().__init__(("127.0.0.1", 0), _TrapHandler)


@pytest.fixture
def trap_proxy() -> Iterator[_TrapProxy]:
    server = _TrapProxy()
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    yield server
    server.shutdown()
    server.server_close()


class RefusingOpener:
    """An opener whose every request fails at once, as an unreachable port does."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def open(self, url: str, timeout: float | None = None) -> Any:
        self.urls.append(url)
        raise urllib.error.URLError("connection refused")


def _page(target_id: str, url: str = "about:blank") -> dict[str, Any]:
    return {
        "id": target_id,
        "type": "page",
        "url": url,
        "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/{target_id}",
    }


@pytest.mark.unit
def test_the_chrome_flags_are_the_exact_hardened_list_in_order(tmp_path: Path) -> None:
    profile = tmp_path / "chrome" / "profile"
    assert chrome.chrome_flags(profile_dir=profile) == [
        "--headless=new",
        "--remote-debugging-port=9222",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--proxy-server=http://127.0.0.1:3128",
        "--proxy-bypass-list=<-loopback>",
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-domain-reliability",
        "--disable-sync",
        "--disable-extensions",
        "--disable-breakpad",
        "--disable-client-side-phishing-detection",
        "--disable-features=OptimizationHints,MediaRouter,DialMediaRouteProvider,"
        "Translate,AutofillServerCommunication,CertificateTransparencyComponentUpdater,"
        "NetworkTimeServiceQuerying",
        "--metrics-recording-only",
        "--no-pings",
        "--password-store=basic",
        "--mute-audio",
        "--lang=en-US",
        "--disable-dev-shm-usage",
        "--window-size=1600,1143",
        "--screen-info={1600x1143}",
        "--force-device-scale-factor=1",
        "--no-sandbox",
        "about:blank",
    ]


@pytest.mark.unit
def test_a_headed_development_run_drops_headless_and_nothing_else(
    tmp_path: Path,
) -> None:
    headless = chrome.chrome_flags(profile_dir=tmp_path)
    headed = chrome.chrome_flags(profile_dir=tmp_path, headless=False)
    assert headed == headless[1:]
    assert headless[0] == "--headless=new"


@pytest.mark.unit
def test_the_proxy_flags_and_the_managed_policy_route_through_one_proxy(
    tmp_path: Path,
) -> None:
    flags = chrome.chrome_flags(profile_dir=tmp_path)
    proxy_settings = chrome.load_policy()["ProxySettings"]
    assert f"--proxy-server={proxy_settings['ProxyServer']}" in flags
    assert proxy_settings["ProxyServer"] == (
        f"http://{paths.PROXY_HOST}:{paths.PROXY_PORT}"
    )
    assert proxy_settings["ProxyMode"] == "fixed_servers"
    assert proxy_settings["ProxyBypassList"] == "<-loopback>"
    assert "--proxy-bypass-list=<-loopback>" in flags


@pytest.mark.unit
def test_the_flags_never_open_devtools_to_pages_or_offer_a_direct_route(
    tmp_path: Path,
) -> None:
    for headless in (True, False):
        for flag in chrome.chrome_flags(profile_dir=tmp_path, headless=headless):
            assert "--remote-allow-origins" not in flag
            assert "--remote-debugging-address" not in flag
            assert "direct://" not in flag


@pytest.mark.unit
def test_the_managed_policy_is_exactly_the_documented_one() -> None:
    assert json.loads(POLICY_PATH.read_text()) == {
        "URLBlocklist": ["file://*", "chrome://*", "devtools://*"],
        "ProxySettings": {
            "ProxyMode": "fixed_servers",
            "ProxyServer": "http://127.0.0.1:3128",
            "ProxyBypassList": "<-loopback>",
        },
        "DnsOverHttpsMode": "off",
        "BuiltInDnsClientEnabled": False,
        "NetworkPredictionOptions": 2,
        "WebRtcIPHandling": "disable_non_proxied_udp",
        "QuicAllowed": False,
        "BackgroundModeEnabled": False,
        "MetricsReportingEnabled": False,
        "SafeBrowsingProtectionLevel": 0,
        "SearchSuggestEnabled": False,
        "DefaultSearchProviderEnabled": False,
        "SyncDisabled": True,
        "BrowserSignin": 0,
        "ComponentUpdatesEnabled": False,
        "SpellcheckEnabled": False,
        "PasswordManagerEnabled": False,
        "PasswordLeakDetectionEnabled": False,
        "AutofillAddressEnabled": False,
        "AutofillCreditCardEnabled": False,
        "TranslateEnabled": False,
        "DownloadRestrictions": 3,
        "AllowFileSelectionDialogs": False,
        "ExtensionInstallBlocklist": ["*"],
    }
    assert chrome.load_policy() == json.loads(POLICY_PATH.read_text())
    assert paths.POLICY_FILE == POLICY_PATH


@pytest.mark.unit
def test_a_policy_file_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "chrome_policy.json"
    path.write_text('["URLBlocklist"]')
    with pytest.raises(chrome.ChromeError, match="not a JSON object"):
        chrome.load_policy(path)


@pytest.mark.unit
def test_the_policy_never_turns_off_the_devtools_the_harness_drives() -> None:
    policy = chrome.load_policy()
    assert "DeveloperToolsAvailability" not in policy
    assert "RemoteDebuggingAllowed" not in policy


@pytest.mark.unit
def test_policy_installed_needs_an_exact_copy_in_one_of_the_directories(
    tmp_path: Path,
) -> None:
    expected = chrome.load_policy()
    first, second = tmp_path / "cft", tmp_path / "chrome"
    first.mkdir()
    second.mkdir()
    dirs = (first, second)
    name = paths.POLICY_INSTALL_NAME
    assert chrome.policy_installed(expected, dirs, name) is False
    (first / name).write_text("{not json")
    assert chrome.policy_installed(expected, dirs, name) is False
    (first / name).write_text(json.dumps({**expected, "QuicAllowed": True}))
    assert chrome.policy_installed(expected, dirs, name) is False
    (second / name).write_text(json.dumps(expected))
    assert chrome.policy_installed(expected, dirs, name) is True


@pytest.mark.unit
def test_chrome_gets_six_fixed_variables_and_nothing_from_the_job(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert chrome.browser_env(home) == {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }


@pytest.mark.unit
def test_launch_runs_the_toolchain_chrome_with_a_private_profile_and_home(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def start(argv: list[str], **kwargs: Any) -> str:
        handle: IO[bytes] = kwargs["stdout"]
        handle.write(b"chrome started\n")
        calls.append({"argv": list(argv), **kwargs})
        return "process"

    toolchain = paths.Toolchain(tmp_path / "prefix")
    log = tmp_path / "chrome.log"
    result = chrome.launch(
        toolchain, run_dir=tmp_path, user="uxbrowser", log=log, start=start
    )
    profile = tmp_path / "chrome" / "profile"
    home = tmp_path / "chrome" / "home"
    assert result == "process"
    assert calls[0]["argv"][0] == str(toolchain.chrome)
    assert calls[0]["argv"][1:] == chrome.chrome_flags(profile_dir=profile)
    assert calls[0]["name"] == "chrome"
    assert calls[0]["env"] == chrome.browser_env(home)
    assert calls[0]["user"] == "uxbrowser"
    assert calls[0]["stderr"] is calls[0]["stdout"]
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "chrome").stat().st_mode) == 0o711
    assert log.read_bytes() == b"chrome started\n"


@pytest.mark.unit
def test_launch_hands_the_profile_and_home_to_the_browser_user_when_it_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners: list[tuple[str, str | None, str | None]] = []

    def chown(path: Path, user: str | None = None, group: str | None = None) -> None:
        owners.append((Path(path).name, user, group))

    monkeypatch.setattr(processes, "can_switch_user", lambda: True)
    monkeypatch.setattr(shutil, "chown", chown)
    chrome.launch(
        paths.Toolchain(tmp_path),
        run_dir=tmp_path,
        user="uxbrowser",
        log=tmp_path / "chrome.log",
        start=lambda argv, **kwargs: None,
    )
    assert owners == [
        ("profile", "uxbrowser", "uxbrowser"),
        ("home", "uxbrowser", "uxbrowser"),
    ]


@pytest.mark.unit
def test_wait_for_cdp_reaches_devtools_directly_whatever_the_environment_proxy(
    devtools: _DevToolsServer,
    trap_proxy: _TrapProxy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{trap_proxy.server_address[1]}")
    port = devtools.server_address[1]
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.build_opener().open(
            f"http://127.0.0.1:{port}/json/version", timeout=5
        )
    assert trap_proxy.hits == 1
    version = chrome.wait_for_cdp(port, timeout=5)
    assert version["Browser"] == "Chrome/150.0.0.0"
    devtools.targets = [_page("PAGE1")]
    assert chrome.first_page_target(port)["id"] == "PAGE1"
    assert trap_proxy.hits == 1


@pytest.mark.unit
def test_wait_for_cdp_gives_up_after_its_timeout() -> None:
    fake = FakeTime()
    opener = RefusingOpener()
    with pytest.raises(chrome.ChromeError, match="did not answer"):
        chrome.wait_for_cdp(
            9333, timeout=1.0, opener=opener, sleep=fake.sleep, clock=fake.clock
        )
    assert fake.now >= 1.0 and set(fake.sleeps) == {0.25}
    assert set(opener.urls) == {"http://127.0.0.1:9333/json/version"}


@pytest.mark.unit
def test_first_page_target_returns_the_one_real_page(
    devtools: _DevToolsServer,
) -> None:
    devtools.targets = [
        {"id": "ext", "type": "service_worker", "url": "chrome-extension://x/"},
        {"id": "ui", "type": "page", "url": "chrome://newtab/"},
        _page("PAGE1"),
    ]
    target = chrome.first_page_target(devtools.server_address[1])
    assert target["id"] == "PAGE1"
    assert target["webSocketDebuggerUrl"].startswith("ws://")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("targets", "count"),
    [([], 0), ([_page("A"), _page("B", "http://localhost:3000/")], 2)],
)
def test_first_page_target_refuses_anything_but_exactly_one_page(
    devtools: _DevToolsServer, targets: list[dict[str, Any]], count: int
) -> None:
    devtools.targets = targets
    with pytest.raises(chrome.ChromeError, match=f"found {count}"):
        chrome.first_page_target(devtools.server_address[1])


@pytest.mark.unit
def test_first_page_target_refuses_a_list_that_is_not_a_list(
    devtools: _DevToolsServer,
) -> None:
    devtools.targets = {"targets": [_page("A")]}
    with pytest.raises(chrome.ChromeError, match="not a JSON list"):
        chrome.first_page_target(devtools.server_address[1])


@pytest.mark.unit
def test_first_page_target_refuses_a_page_without_a_devtools_websocket(
    devtools: _DevToolsServer,
) -> None:
    devtools.targets = [{"id": "A", "type": "page", "url": "about:blank"}]
    with pytest.raises(chrome.ChromeError, match="WebSocket"):
        chrome.first_page_target(devtools.server_address[1])


@pytest.mark.unit
def test_first_page_target_reports_an_unreachable_devtools() -> None:
    opener = RefusingOpener()
    with pytest.raises(chrome.ChromeError, match="could not be read"):
        chrome.first_page_target(9333, opener=opener)
    assert opener.urls == ["http://127.0.0.1:9333/json/list"]


def _read_client_frame(sock: socket.socket) -> bytes:
    """One masked client frame's payload, as a DevTools server reads it."""

    def exact(count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return data

    _first, second = exact(2)
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(exact(2), "big")
    mask = exact(4)
    payload = exact(length)
    return bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))


class _WebSocketPeer(socketserver.BaseRequestHandler):
    """Chrome's side of one DevTools WebSocket: the upgrade, then one reply."""

    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _WebSocketServer)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            raw += chunk
        lines = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        server.request_line = lines[0]
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        accept = chrome.ux_recorder_cdp.accept_key(headers["sec-websocket-key"])
        self.request.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
            + accept.encode()
            + b"\r\n\r\n"
        )
        message = json.loads(_read_client_frame(self.request))
        server.received.append(message)
        reply = json.dumps({"id": message["id"], "result": {"product": "Chrome/150"}})
        self.request.sendall(bytes([0x81, len(reply)]) + reply.encode())
        while self.request.recv(4096):
            pass


class _WebSocketServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self) -> None:
        self.request_line = ""
        self.received: list[Any] = []
        super().__init__(("127.0.0.1", 0), _WebSocketPeer)


@pytest.mark.unit
def test_open_session_speaks_cdp_over_the_pages_websocket() -> None:
    server = _WebSocketServer()
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    port = server.server_address[1]
    try:
        session = chrome.open_session(f"ws://127.0.0.1:{port}/devtools/page/T1")
        try:
            assert session.call("Browser.getVersion", timeout=5) == {
                "product": "Chrome/150"
            }
        finally:
            session.close()
    finally:
        server.shutdown()
        server.server_close()
    assert server.request_line == "GET /devtools/page/T1 HTTP/1.1"
    assert server.received == [{"id": 1, "method": "Browser.getVersion", "params": {}}]


@pytest.mark.unit
def test_evaluate_returns_the_value_by_value() -> None:
    session = FakeSession({"Runtime.evaluate": {"result": {"value": {"a": 1}}}})
    assert chrome.evaluate(session, "({a: 1})") == {"a": 1}
    assert session.calls == [
        ("Runtime.evaluate", {"expression": "({a: 1})", "returnByValue": True})
    ]


@pytest.mark.unit
def test_evaluate_raises_on_a_thrown_exception_with_bounded_printable_text() -> None:
    details = {
        "text": "Uncaught",
        "exception": {"description": "TypeError: boom\n    at x " + "y" * 500},
    }
    session = FakeSession({"Runtime.evaluate": {"exceptionDetails": details}})
    with pytest.raises(chrome.ChromeError) as raised:
        chrome.evaluate(session, "boom()")
    message = str(raised.value)
    prefix = "the page script failed: "
    assert message.startswith(prefix + "Uncaught TypeError: boom at x y")
    assert len(message) == len(prefix) + 200
    assert "\n" not in message


LOADED = {"result": {"value": "complete"}}


class NavigatingSession(FakeSession):
    """A session whose ``Page.navigate`` answers by URL, from ``answers``.

    ``document.readyState`` answers from ``states`` in turn, then ``complete``,
    and every call is logged in order, so a test sees what each probe waited for.
    """

    def __init__(
        self, answers: dict[str, dict[str, Any]], states: tuple[str, ...] = ()
    ) -> None:
        super().__init__()
        self.answers = answers
        self.states = list(states)

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "Runtime.evaluate":
            state = self.states.pop(0) if self.states else "complete"
            return {"result": {"value": state}}
        return self.answers.get(str((params or {}).get("url")), {})

    def steps(self) -> list[str]:
        return [
            str((params or {}).get("url")) if method == "Page.navigate" else "ready?"
            for method, params in self.calls
        ]


BLOCKED = {"errorText": "net::ERR_BLOCKED_BY_ADMINISTRATOR"}


DEVTOOLS_PROBE = "devtools://devtools/bundled/inspector.html"
ALL_BLOCKED = {
    "file:///etc/hostname": BLOCKED,
    "chrome://inspect/": BLOCKED,
    DEVTOOLS_PROBE: BLOCKED,
}


@pytest.mark.unit
def test_verify_policy_passes_only_when_file_chrome_and_devtools_urls_are_blocked() -> (
    None
):
    blocked = NavigatingSession(dict(ALL_BLOCKED))
    chrome.verify_policy(blocked)
    assert blocked.steps() == [
        "ready?",
        "file:///etc/hostname",
        "ready?",
        "chrome://inspect/",
        "ready?",
        DEVTOOLS_PROBE,
        "ready?",
    ]
    for reply in ({}, {"errorText": "net::ERR_FILE_NOT_FOUND"}):
        session = FakeSession({"Page.navigate": reply, "Runtime.evaluate": LOADED})
        with pytest.raises(chrome.ChromeError, match="file:// was not blocked"):
            chrome.verify_policy(session)


@pytest.mark.unit
def test_each_policy_probe_waits_for_the_page_before_it_to_finish_loading() -> None:
    """Chrome for Testing 155 crashed in the image every time it was sent to a
    blocked ``chrome://`` URL straight after a page loaded, and never once it had
    waited for that page to finish, so no probe is sent until the last one's page
    is complete."""
    clock = FakeTime()
    session = NavigatingSession(
        dict(ALL_BLOCKED),
        states=("complete", "loading", "interactive", "complete"),
    )
    chrome.verify_policy(session, sleep=clock.sleep, clock=clock.clock)
    assert session.steps() == [
        "ready?",
        "file:///etc/hostname",
        "ready?",
        "ready?",
        "ready?",
        "chrome://inspect/",
        "ready?",
        DEVTOOLS_PROBE,
        "ready?",
    ]
    assert clock.sleeps == [chrome._POLL_SECONDS] * 2


@pytest.mark.unit
def test_a_probe_page_that_never_finishes_loading_fails_the_check_closed() -> None:
    clock = FakeTime()
    session = NavigatingSession(
        {"file:///etc/hostname": BLOCKED}, states=("complete", *["loading"] * 400)
    )
    with pytest.raises(chrome.ChromeError, match="did not finish loading within 3 s"):
        chrome.verify_policy(
            session, settle_timeout=3.0, sleep=clock.sleep, clock=clock.clock
        )
    assert "chrome://inspect/" not in session.steps()


@pytest.mark.unit
def test_devtools_lost_during_the_policy_check_is_a_chrome_error_saying_so() -> None:
    """A crashed Chrome closes the DevTools connection, which the session reports
    as a CdpError; the check reports that as the browser failing it."""
    lost = chrome.ux_recorder_cdp.CdpError("Page.navigate: connection closed")
    session = FakeSession({"Page.navigate": lost, "Runtime.evaluate": LOADED})
    with pytest.raises(chrome.ChromeError) as raised:
        chrome.verify_policy(session)
    assert str(raised.value) == (
        "the managed policy could not be checked, Chrome's DevTools stopped "
        "answering: Page.navigate: connection closed"
    )


@pytest.mark.unit
def test_verify_policy_fails_when_chrome_pages_still_load() -> None:
    """``chrome://inspect`` is the page chrome-devtools-mcp lets the model open,
    and its network-target discovery opens sockets that do not use the proxy."""
    session = NavigatingSession({"file:///etc/hostname": BLOCKED})
    with pytest.raises(chrome.ChromeError, match="chrome:// was not blocked"):
        chrome.verify_policy(session)
    assert session.steps()[-1] == "chrome://inspect/"


@pytest.mark.unit
def test_verify_policy_fails_when_devtools_pages_still_load() -> None:
    """chrome-devtools-mcp's ``navigate_page`` passes a ``devtools:`` URL through,
    so the policy is the only thing between the model and a DevTools front end."""
    session = NavigatingSession(
        {"file:///etc/hostname": BLOCKED, "chrome://inspect/": BLOCKED}
    )
    with pytest.raises(chrome.ChromeError, match="devtools:// was not blocked"):
        chrome.verify_policy(session)
    assert session.steps()[-1] == DEVTOOLS_PROBE


@pytest.mark.unit
def test_the_policy_blocks_every_scheme_the_browser_could_be_driven_out_by() -> None:
    """And every blocked scheme has a probe, so none is blocked on paper only."""
    policy = chrome.load_policy()
    assert {"file://*", "chrome://*", "devtools://*"} <= set(policy["URLBlocklist"])
    probed = {f"{url.split(':', 1)[0]}://*" for url in chrome._POLICY_PROBE_URLS}
    assert probed == set(policy["URLBlocklist"])


@pytest.mark.unit
def test_the_policy_and_flags_turn_off_what_chrome_calls_by_itself(
    tmp_path: Path,
) -> None:
    """Measured in the image, before and after, with the proxy refusing them:
    ``DefaultSearchProviderEnabled`` stops the start-up query to
    ``www.google.com/async/folae``, ``PasswordLeakDetectionEnabled`` the leak check
    a submitted password sets off, and ``NetworkTimeServiceQuerying`` the
    network-time query to clients2.google.com. The account list, the GCM check-in,
    one component update check and the spell-check dictionary keep coming with
    sign-in, component updates and spell check all off, which is why
    :data:`chrome.BROWSER_BACKGROUND_HOSTS` names their hosts."""
    policy = chrome.load_policy()
    assert policy["DefaultSearchProviderEnabled"] is False
    assert policy["PasswordLeakDetectionEnabled"] is False
    assert policy["BrowserSignin"] == 0
    assert policy["ComponentUpdatesEnabled"] is False
    assert policy["SpellcheckEnabled"] is False
    features = next(
        flag.split("=", 1)[1].split(",")
        for flag in chrome.chrome_flags(profile_dir=tmp_path)
        if flag.startswith("--disable-features=")
    )
    assert "NetworkTimeServiceQuerying" in features


@pytest.mark.unit
def test_verify_proxy_path_passes_once_the_proxy_logs_the_selftest_refusal(
    tmp_path: Path,
) -> None:
    log = tmp_path / "refusals.jsonl"
    line = {
        "t": "2026-10-14T21:03:11Z",
        "method": "GET",
        "target": f"http://{chrome.SELFTEST_HOST}/",
        "reason": "plain_http_not_allowed",
    }

    def write() -> None:
        log.write_text("not json\n" + json.dumps(line) + "\n")

    timer = threading.Timer(0.2, write)
    timer.start()
    session = FakeSession(
        {"Page.navigate": chrome.ux_recorder_cdp.CdpError("Page.navigate: no reply")}
    )
    try:
        chrome.verify_proxy_path(session, log, owner=os.geteuid(), timeout=10.0)
    finally:
        timer.cancel()
    assert session.calls == [
        ("Page.navigate", {"url": "http://ux-proxy-selftest.invalid/"})
    ]


@pytest.mark.unit
def test_verify_proxy_path_fails_closed_when_the_refusal_never_appears(
    tmp_path: Path,
) -> None:
    log = tmp_path / "refusals.jsonl"
    log.write_text(json.dumps({"target": "clients2.google.com:443"}) + "\n")
    fake = FakeTime()
    with pytest.raises(chrome.ChromeError, match="not reaching the egress proxy"):
        chrome.verify_proxy_path(
            FakeSession(),
            log,
            owner=os.geteuid(),
            timeout=2.0,
            sleep=fake.sleep,
            clock=fake.clock,
        )
    assert fake.now >= 2.0


@pytest.mark.unit
@pytest.mark.parametrize("planted", ["symlink", "other uid"])
def test_verify_proxy_path_reads_the_log_only_as_the_proxys_own_file(
    tmp_path: Path, planted: str
) -> None:
    """The proxy's directory is uxproxy's, so a link there to a file that does
    name the self-test host, or a file of another uid's, is refused at once rather
    than passing the check or being waited out as a refusal not logged yet."""
    outside = tmp_path / "root-only.jsonl"
    record = {"target": f"http://{chrome.SELFTEST_HOST}/", "reason": "x"}
    outside.write_text(json.dumps(record) + "\n", encoding="utf-8")
    proxy_dir = tmp_path / "proxy"
    proxy_dir.mkdir()
    log = proxy_dir / "refusals.jsonl"
    owner = os.geteuid()
    if planted == "symlink":
        log.symlink_to(outside)
        why = "it is a symbolic link, which is never followed"
    else:
        log.write_text(outside.read_text(encoding="utf-8"), encoding="utf-8")
        owner += 1
        why = f"it belongs to uid {owner - 1}, not to the proxy's uid {owner}"
    fake = FakeTime()
    with pytest.raises(egress.ProxyError) as raised:
        chrome.verify_proxy_path(
            FakeSession(),
            log,
            owner=owner,
            timeout=2.0,
            sleep=fake.sleep,
            clock=fake.clock,
        )
    assert str(raised.value) == f"the proxy's refusals.jsonl is refused: {why}"
    assert fake.sleeps == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("hex_addr", "decoded"),
    [
        ("0100007F:2406", ("127.0.0.1", 9222)),
        ("00000000:0000", (UNSPECIFIED, 0)),
        ("00000000000000000000000001000000:0050", ("::1", 80)),
        ("0000000000000000FFFF00000100007F:01BB", ("::ffff:127.0.0.1", 443)),
        ("181F0026000000000000000001000000:01BB", ("2600:1f18::1", 443)),
    ],
)
def test_decode_proc_address_reads_little_endian_words_and_a_hex_port(
    hex_addr: str, decoded: tuple[str, int]
) -> None:
    assert chrome.decode_proc_address(hex_addr) == decoded


@pytest.mark.unit
@pytest.mark.parametrize("hex_addr", ["0100007F", "0100:2406", "XYZ00000:0000"])
def test_decode_proc_address_refuses_anything_else(hex_addr: str) -> None:
    with pytest.raises(ValueError):
        chrome.decode_proc_address(hex_addr)


def _proc_hex(address: str, port: int) -> str:
    """``address:port`` as ``/proc/net`` prints it: little-endian words, hex port."""
    packed = ipaddress.ip_address(address).packed
    words = b"".join(packed[i : i + 4][::-1] for i in range(0, len(packed), 4))
    return f"{words.hex().upper()}:{port:04X}"


def _write_table(
    proc_net: Path, table: str, rows: list[tuple[str, int, str, int, int]]
) -> None:
    """One ``/proc/net`` table: (local, local port, remote, remote port, uid) rows."""
    header = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
        "retrnsmt   uid  timeout inode"
    )
    lines = [header]
    for index, (local, lport, remote, rport, uid) in enumerate(rows):
        lines.append(
            f"{index:4d}: {_proc_hex(local, lport)} {_proc_hex(remote, rport)} 01 "
            f"00000000:00000000 00:00000000 00000000 {uid:5d}        0 "
            f"{4000 + index} 1 0000000000000000 20 4 30 10 -1"
        )
    (proc_net / table).write_text("\n".join(lines) + "\n")


@pytest.fixture
def proc_net(tmp_path: Path) -> Path:
    root = tmp_path / "net"
    root.mkdir()
    _write_table(
        root,
        "tcp",
        [
            ("127.0.0.1", 9222, UNSPECIFIED, 0, BROWSER_UID),
            ("127.0.0.1", 50000, "127.0.0.1", 3128, BROWSER_UID),
            ("10.0.2.15", 50001, "52.95.110.34", 443, PROXY_UID),
        ],
    )
    _write_table(
        root,
        "tcp6",
        [
            ("::1", 9222, "::", 0, BROWSER_UID),
            ("::ffff:127.0.0.1", 50002, "::ffff:127.0.0.1", 3128, BROWSER_UID),
        ],
    )
    _write_table(root, "udp", [("127.0.0.1", 5353, UNSPECIFIED, 0, BROWSER_UID)])
    _write_table(root, "udp6", [])
    return root


@pytest.mark.unit
def test_listening_unconnected_and_loopback_sockets_of_the_browser_are_clean(
    proc_net: Path,
) -> None:
    audit = chrome.audit_sockets(BROWSER_UID, proc_net)
    assert audit == chrome.SocketAudit("clean", (), 5)


@pytest.mark.unit
def test_a_browser_socket_to_any_other_address_is_a_violation_by_table(
    proc_net: Path,
) -> None:
    _write_table(
        proc_net,
        "udp6",
        [
            ("2600:1f18::2", 50003, "2600:1f18::1", 443, BROWSER_UID),
            ("2600:1f18::2", 50004, "2600:1f18::1", 443, BROWSER_UID),
        ],
    )
    with (proc_net / "tcp").open("a") as handle:
        handle.write(
            f"  10: {_proc_hex('10.0.2.15', 50010)} {_proc_hex('52.95.110.34', 443)} "
            f"01 00000000:00000000 00:00000000 00000000 {BROWSER_UID:5d}        0 "
            "4999 1 0000000000000000 20 4 30 10 -1\n"
        )
    audit = chrome.audit_sockets(BROWSER_UID, proc_net)
    assert audit.state == "violations"
    assert audit.violations == ("tcp 52.95.110.34:443", "udp6 [2600:1f18::1]:443")
    assert audit.sockets == 8


@pytest.mark.unit
def test_malformed_rows_and_an_unreadable_table_are_passed_over(proc_net: Path) -> None:
    with (proc_net / "udp").open("a") as handle:
        handle.write("   7: too few fields\n")
        handle.write(
            f"   8: {_proc_hex('127.0.0.1', 1)} {_proc_hex('127.0.0.1', 2)} 07 "
            "00000000:00000000 00:00000000 00000000 notauid 0 1 1\n"
        )
        handle.write(
            f"   9: {_proc_hex('127.0.0.1', 1)} ZZZZ:0050 07 "
            f"00000000:00000000 00:00000000 00000000 {BROWSER_UID} 0 1 1\n"
        )
    os.remove(proc_net / "udp6")
    (proc_net / "udp6").mkdir()
    assert chrome.audit_sockets(BROWSER_UID, proc_net) == chrome.SocketAudit(
        "clean", (), 5
    )


@pytest.mark.unit
def test_sockets_of_other_uids_are_not_the_browsers(proc_net: Path) -> None:
    audit = chrome.audit_sockets(PROXY_UID, proc_net)
    assert audit.state == "violations"
    assert audit.violations == ("tcp 52.95.110.34:443",)
    assert audit.sockets == 1


@pytest.mark.unit
def test_the_audit_is_unavailable_without_a_uid_or_a_tcp_table(
    proc_net: Path, tmp_path: Path
) -> None:
    assert chrome.audit_sockets(None, proc_net).state == "unavailable"
    assert chrome.audit_sockets(BROWSER_UID, tmp_path / "absent").state == "unavailable"
    os.remove(proc_net / "tcp")
    assert chrome.audit_sockets(BROWSER_UID, proc_net) == chrome.SocketAudit(
        "unavailable", (), 0
    )


@pytest.mark.unit
def test_the_watch_merges_every_pass_so_one_violation_marks_the_run() -> None:
    passes = [
        chrome.SocketAudit("clean", (), 3),
        chrome.SocketAudit("violations", ("tcp 52.95.110.34:443",), 4),
    ]
    seen: list[int | None] = []
    names: list[str] = []

    def audit(uid: int | None) -> chrome.SocketAudit:
        seen.append(uid)
        names.append(threading.current_thread().name)
        return passes.pop(0) if passes else chrome.SocketAudit("clean", (), 2)

    watch = chrome.SocketWatch(BROWSER_UID, interval=0.01, audit=audit).start()
    deadline = time.monotonic() + 10
    while len(seen) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    result = watch.stop()
    assert result == chrome.SocketAudit("violations", ("tcp 52.95.110.34:443",), 4)
    assert set(seen) == {BROWSER_UID}
    assert "socket-audit" in names


@pytest.mark.unit
def test_the_watch_is_unavailable_only_when_every_pass_was() -> None:
    unavailable = chrome.SocketAudit("unavailable", (), 0)
    watch = chrome.SocketWatch(None, interval=60, audit=lambda uid: unavailable)
    assert watch.start().stop() == unavailable
    answers = iter([unavailable, chrome.SocketAudit("clean", (), 1)])
    mixed = chrome.SocketWatch(None, interval=60, audit=lambda uid: next(answers))
    assert mixed.start().stop() == chrome.SocketAudit("clean", (), 1)


@pytest.mark.unit
@pytest.mark.parametrize("viewport", [(1600, 1000), (1280, 720)])
def test_the_window_and_screen_are_the_viewport_plus_chromes_own_frame(
    tmp_path: Path, viewport: tuple[int, int]
) -> None:
    """The live image gave a 1600x857 page for ``--window-size=1600,1000``: the
    window holds Chrome's frame, and Chrome fits a new window into its screen, so
    both are made taller by the frame and the page is the viewport asked for."""
    frame = chrome.HEADLESS_FRAME_HEIGHT
    assert frame == 143, (
        "HEADLESS_FRAME_HEIGHT is the frame measured in the image (1000 - 857); it "
        "is audited when Chrome for Testing is bumped, against verify_viewport"
    )
    width, height = viewport
    flags = chrome.chrome_flags(profile_dir=tmp_path, window=viewport)
    assert f"--window-size={width},{height + frame}" in flags
    assert f"--screen-info={{{width}x{height + frame}}}" in flags
    assert paths.VIEWPORT == (1600, 1000)


class ViewportSession(FakeSession):
    """A page whose own world claims one viewport and whose isolated world reads
    the browser's, so a test can tell which one was measured."""

    CONTEXT = 31

    def __init__(self, real: list[Any], claimed: list[Any] | None = None) -> None:
        super().__init__()
        self.real = real
        self.claimed = claimed if claimed is not None else [16384, 16384, 1]

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "MAIN"}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": self.CONTEXT}
        if method == "Runtime.evaluate":
            world = (params or {}).get("contextId") == self.CONTEXT
            return {"result": {"value": self.real if world else self.claimed}}
        return {}


@pytest.mark.unit
def test_the_viewport_is_measured_where_the_page_cannot_redefine_it() -> None:
    session = ViewportSession([1600, 1000, 1])
    assert chrome.verify_viewport(session) == {"w": 1600, "h": 1000, "dpr": 1}
    worlds = [
        params for method, params in session.calls if method != "Runtime.evaluate"
    ]
    assert worlds == [None, {"frameId": "MAIN", "worldName": "ux-harness"}]
    (evaluated,) = [p for m, p in session.calls if m == "Runtime.evaluate"]
    assert evaluated is not None and evaluated["contextId"] == ViewportSession.CONTEXT
    assert evaluated["returnByValue"] is True


@pytest.mark.unit
def test_a_viewport_off_by_the_frame_fails_closed_naming_the_frame_that_fits() -> None:
    with pytest.raises(chrome.ChromeError) as raised:
        chrome.verify_viewport(ViewportSession([1600, 857, 1]))
    message = str(raised.value)
    assert "1600x857" in message and "not 1600x1000" in message
    assert "HEADLESS_FRAME_HEIGHT would be 286, not 143" in message
    with pytest.raises(chrome.ChromeError, match="at 2x"):
        chrome.verify_viewport(ViewportSession([1600, 1000, 2]))
    assert chrome.measure_viewport(ViewportSession([1280, 720, 1])) == {
        "w": 1280,
        "h": 720,
        "dpr": 1,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [None, {"w": 1600}, [1600, 1000], [1600, "1000", 1], [True, 1000, 1]],
)
def test_a_viewport_that_is_not_three_numbers_is_an_error(value: Any) -> None:
    with pytest.raises(chrome.ChromeError, match="viewport could not be read"):
        chrome.measure_viewport(ViewportSession(value))


@pytest.mark.unit
def test_a_viewport_with_no_isolated_world_is_an_error_not_a_guess() -> None:
    refusing = FakeSession(
        {"Page.getFrameTree": chrome.ux_recorder_cdp.CdpError("no frame tree")}
    )
    with pytest.raises(chrome.ChromeError, match="no frame tree"):
        chrome.measure_viewport(refusing)
    assert [method for method, _ in refusing.calls] == ["Page.getFrameTree"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("record", "origin"),
    [
        ({"method": "GET", "target": "http://ux-proxy-selftest.invalid/"}, "selftest"),
        ({"method": "CONNECT", "target": "accounts.google.com:443"}, "browser"),
        ({"method": "CONNECT", "target": "update.googleapis.com:443"}, "browser"),
        ({"method": "CONNECT", "target": "redirector.gvt1.com:443"}, "browser"),
        ({"method": "CONNECT", "target": "android.clients.google.com:443"}, "browser"),
        ({"method": "CONNECT", "target": "www.google.com:443"}, "browser"),
        (
            {"method": "CONNECT", "target": "passwordsleakcheck-pa.googleapis.com:443"},
            "browser",
        ),
        (
            {"method": "GET", "target": "http://clients2.google.com/time/1/current"},
            "browser",
        ),
        ({"method": "CONNECT", "target": "Accounts.Google.COM.:443"}, "browser"),
        (
            {"method": "CONNECT", "target": "evil-accounts.google.com.example:443"},
            "page",
        ),
        (
            {"method": "CONNECT", "target": "accounts.google.com.evil.example:443"},
            "page",
        ),
        ({"method": "CONNECT", "target": "fonts.googleapis.com:443"}, "page"),
        ({"method": "CONNECT", "target": "google.com:443"}, "page"),
        ({"method": "CONNECT", "target": "maps.google.com:443"}, "page"),
        (
            {"method": "GET", "target": "http://169.254.169.254/latest/meta-data/"},
            "page",
        ),
        ({"method": "CONNECT", "target": "[2600:1f18::1]:443"}, "page"),
        ({"method": "GET", "target": "http://[x]/"}, "page"),
        ({"method": "GET", "target": 42}, "page"),
        ({}, "page"),
    ],
)
def test_each_refusal_is_labelled_as_the_selftests_chromes_or_the_pages(
    record: dict[str, Any], origin: str
) -> None:
    assert chrome.refusal_origin(record) == origin


@pytest.mark.unit
def test_the_hosts_chrome_calls_by_itself_are_the_ones_the_live_image_refused() -> None:
    assert chrome.BROWSER_BACKGROUND_HOSTS == frozenset(
        {
            "accounts.google.com",
            "android.clients.google.com",
            "clients2.google.com",
            "passwordsleakcheck-pa.googleapis.com",
            "redirector.gvt1.com",
            "update.googleapis.com",
            "www.google.com",
        }
    )
    assert chrome.SELFTEST_HOST not in chrome.BROWSER_BACKGROUND_HOSTS


@pytest.mark.unit
def test_a_browser_that_saw_no_socket_at_all_is_unavailable_not_clean(
    tmp_path: Path,
) -> None:
    """A running Chrome always holds its DevTools listening socket, so a pass that
    sees no socket of the browser uid is not watching Chrome."""
    root = tmp_path / "net"
    root.mkdir()
    _write_table(root, "tcp", [("10.0.2.15", 50001, "52.95.110.34", 443, PROXY_UID)])
    assert chrome.audit_sockets(BROWSER_UID, root) == chrome.SocketAudit(
        "unavailable", (), 0
    )
    nothing = chrome.SocketAudit("clean", (), 0)
    watch = chrome.SocketWatch(BROWSER_UID, interval=60, audit=lambda uid: nothing)
    assert watch.start().stop() == chrome.SocketAudit("unavailable", (), 0)


@pytest.mark.unit
def test_an_unconnected_socket_bound_beyond_loopback_is_a_violation(
    proc_net: Path,
) -> None:
    """``sendto`` from a datagram socket bound to the wildcard leaves no remote
    address in the table, and a listening socket on it accepts from the network."""
    _write_table(
        proc_net,
        "udp",
        [
            ("127.0.0.1", 5353, UNSPECIFIED, 0, BROWSER_UID),
            (UNSPECIFIED, 5353, UNSPECIFIED, 0, BROWSER_UID),
            ("10.0.2.15", 41000, UNSPECIFIED, 0, BROWSER_UID),
        ],
    )
    _write_table(proc_net, "tcp6", [("::", 9223, "::", 0, BROWSER_UID)])
    audit = chrome.audit_sockets(BROWSER_UID, proc_net)
    assert audit.state == "violations"
    assert audit.violations == (
        "tcp6 [::]:9223 (bound, unconnected)",
        f"udp {UNSPECIFIED}:5353 (bound, unconnected)",
        "udp 10.0.2.15:41000 (bound, unconnected)",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("table", "row", "violation"),
    [
        (
            "icmp",
            (UNSPECIFIED, 7, UNSPECIFIED, 0),
            f"icmp {UNSPECIFIED}:7 (bound, unconnected)",
        ),
        ("icmp", ("10.0.2.15", 7, "8.8.8.8", 0), "icmp 8.8.8.8:0"),
        ("icmp6", ("::", 7, "2600:1f18::1", 0), "icmp6 [2600:1f18::1]:0"),
        (
            "raw",
            (UNSPECIFIED, 1, UNSPECIFIED, 0),
            f"raw {UNSPECIFIED}:1 (bound, unconnected)",
        ),
        ("raw6", ("::", 58, "::", 0), "raw6 [::]:58 (bound, unconnected)"),
        ("udplite", ("10.0.2.15", 4000, "52.95.110.34", 53), "udplite 52.95.110.34:53"),
        ("udplite6", ("::", 4000, "2600:1f18::1", 53), "udplite6 [2600:1f18::1]:53"),
    ],
)
def test_ping_raw_and_udp_lite_sockets_of_the_browser_are_audited_too(
    proc_net: Path, table: str, row: tuple[str, int, str, int], violation: str
) -> None:
    """A ping socket needs no privilege under Docker's default ping_group_range,
    and none of these shows up in the TCP or UDP tables."""
    _write_table(proc_net, table, [(*row, BROWSER_UID)])
    audit = chrome.audit_sockets(BROWSER_UID, proc_net)
    assert audit.state == "violations"
    assert audit.violations == (violation,)
    assert audit.sockets == 6


@pytest.mark.unit
def test_every_table_a_socket_can_hide_in_is_read() -> None:
    assert chrome._PROC_NET_TABLES == (
        "tcp",
        "tcp6",
        "udp",
        "udp6",
        "udplite",
        "udplite6",
        "icmp",
        "icmp6",
        "raw",
        "raw6",
    )


@pytest.mark.unit
def test_stopping_the_watch_twice_returns_one_verdict_and_audits_once() -> None:
    """The orchestrator stops the watch after Chrome exits and again in its
    cleanup; the second stop must neither audit nor change the verdict."""
    passes: list[int | None] = []

    def audit(uid: int | None) -> chrome.SocketAudit:
        passes.append(uid)
        return chrome.SocketAudit("clean", (), 3)

    watch = chrome.SocketWatch(BROWSER_UID, interval=60, audit=audit).start()
    first = watch.stop()
    audited = len(passes)
    assert watch.stop() is first
    assert len(passes) == audited and first == chrome.SocketAudit("clean", (), 3)


def _proc_entry(
    root: Path,
    pid: int,
    *,
    uid: int,
    group: int,
    state: str = "S",
    name: str = "chrome",
) -> None:
    entry = root / str(pid)
    entry.mkdir()
    (entry / "stat").write_text(
        f"{pid} ({name}) {state} 1 {group} {group} 0 -1 4194560 0", encoding="ascii"
    )
    (entry / "status").write_text(
        f"Name:\t{name}\nState:\t{state}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n",
        encoding="ascii",
    )


class FakeKill:
    """``os.kill`` over a fake ``/proc``: a killed process's entry goes away."""

    def __init__(self, root: Path, *, unkillable: tuple[int, ...] = ()) -> None:
        self.root = root
        self.unkillable = unkillable
        self.sent: list[tuple[int, int]] = []

    def __call__(self, pid: int, sig: int) -> None:
        self.sent.append((pid, sig))
        if pid not in self.unkillable:
            shutil.rmtree(self.root / str(pid))


@pytest.mark.unit
def test_kill_strays_kills_every_browser_process_and_names_those_outside_the_group(
    tmp_path: Path,
) -> None:
    """A group kill never reaches a process that called ``setsid``, so after it the
    sweep finds the uid's processes by ``/proc``: the group's own are being killed
    already, and anything else left the group and would outlive the audit."""
    import signal

    chrome_group = 4100
    _proc_entry(tmp_path, 4100, uid=BROWSER_UID, group=chrome_group)
    _proc_entry(tmp_path, 4180, uid=BROWSER_UID, group=4180, name="chrome_crashpad")
    _proc_entry(tmp_path, 4190, uid=BROWSER_UID, group=chrome_group, state="Z")
    _proc_entry(tmp_path, 4200, uid=PROXY_UID, group=4200, name="python3")
    (tmp_path / "self").mkdir()
    kill = FakeKill(tmp_path)
    strays = chrome.kill_strays(
        BROWSER_UID, chrome_group, proc=tmp_path, kill=kill, sleep=lambda s: None
    )
    assert strays == ("pid 4180 (chrome_crashpad)",)
    assert sorted(kill.sent) == [(4100, signal.SIGKILL), (4180, signal.SIGKILL)]
    assert (tmp_path / "4200").is_dir(), "the proxy's process is not the browser's"


@pytest.mark.unit
def test_kill_strays_reads_the_uid_from_status_not_the_entrys_owner(
    tmp_path: Path,
) -> None:
    """An undumpable process's ``/proc/<pid>`` belongs to root, so ownership of the
    directory would hide exactly the process that went out of its way to hide."""
    _proc_entry(tmp_path, 5100, uid=BROWSER_UID, group=5100, name="hidden")
    kill = FakeKill(tmp_path)
    assert chrome.kill_strays(
        BROWSER_UID, 4100, proc=tmp_path, kill=kill, sleep=lambda s: None
    ) == ("pid 5100 (hidden)",)


@pytest.mark.unit
def test_kill_strays_keeps_sweeping_and_stops_after_a_bounded_number_of_passes(
    tmp_path: Path,
) -> None:
    _proc_entry(tmp_path, 6100, uid=BROWSER_UID, group=6100)
    kill = FakeKill(tmp_path, unkillable=(6100,))
    pauses: list[float] = []
    assert chrome.kill_strays(
        BROWSER_UID, 4100, proc=tmp_path, kill=kill, sleep=pauses.append
    ) == ("pid 6100 (chrome)",)
    assert len(kill.sent) == chrome._STRAY_SWEEPS
    assert pauses == [chrome._STRAY_PAUSE_SECONDS] * chrome._STRAY_SWEEPS


@pytest.mark.unit
def test_kill_strays_does_nothing_without_a_uid_or_a_proc_directory(
    tmp_path: Path,
) -> None:
    _proc_entry(tmp_path, 7100, uid=BROWSER_UID, group=7100)
    kill = FakeKill(tmp_path)
    assert chrome.kill_strays(None, 4100, proc=tmp_path, kill=kill) == ()
    assert (
        chrome.kill_strays(BROWSER_UID, 4100, proc=tmp_path / "absent", kill=kill) == ()
    )
    assert kill.sent == []


@pytest.mark.unit
def test_the_flags_leave_crashpad_running_because_disabling_it_breaks_the_network(
    tmp_path: Path,
) -> None:
    """Measured in the image: with ``--disable-crashpad-for-testing`` Chrome for
    Testing 155 crash-loops its network service, without it Chrome navigates and
    its traffic reaches the proxy. Crash reporting stays off by other means."""
    for headless in (True, False):
        flags = chrome.chrome_flags(profile_dir=tmp_path, headless=headless)
        assert "--disable-crashpad-for-testing" not in flags, (
            "--disable-crashpad-for-testing makes Chrome for Testing 155's network "
            "service crash-loop ('Crashing due to FD ownership violation'), so every "
            "navigation fails net::ERR_ABORTED, nothing reaches the egress proxy and "
            "verify_proxy_path fails the run"
        )
        assert "--disable-breakpad" in flags
    assert chrome.load_policy()["MetricsReportingEnabled"] is False


@pytest.mark.unit
def test_a_process_that_vanishes_or_reads_malformed_is_passed_over(
    tmp_path: Path,
) -> None:
    """Between the listing and the read a process can exit, and ``/proc`` text
    that does not parse is no process the sweep can name or kill by group."""
    _proc_entry(tmp_path, 8100, uid=BROWSER_UID, group=8100)
    (tmp_path / "8100" / "status").unlink()
    _proc_entry(tmp_path, 8200, uid=BROWSER_UID, group=8200)
    (tmp_path / "8200" / "stat").write_text(
        "8200 (chrome) S 1 not-a-group 0", encoding="ascii"
    )
    _proc_entry(tmp_path, 8300, uid=BROWSER_UID, group=8300)
    (tmp_path / "8300" / "stat").write_text("8300 (chrome", encoding="ascii")
    assert chrome._process_of(tmp_path / "8100", BROWSER_UID) is None
    assert chrome._process_of(tmp_path / "8200", BROWSER_UID) is None
    assert chrome._process_of(tmp_path / "8300", BROWSER_UID) is None
    kill = FakeKill(tmp_path)
    assert chrome.kill_strays(BROWSER_UID, 4100, proc=tmp_path, kill=kill) == ()
    assert kill.sent == []


@pytest.mark.unit
def test_a_proc_directory_that_cannot_be_listed_has_no_processes(
    tmp_path: Path,
) -> None:
    not_a_directory = tmp_path / "proc"
    not_a_directory.write_text("", encoding="ascii")
    assert chrome._pid_entries(not_a_directory) == []
