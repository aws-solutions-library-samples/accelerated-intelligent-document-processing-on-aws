# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Docker browser mode's entry point, ``ux_runner/browser.py``.

The forwarder is a real TCP relay, exercised between two ephemeral ports.
``main()`` runs with every collaborator replaced by a recording fake, so what is
pinned is the order the confinement is put together in (proxy, Chrome, both
self-tests, and only then an optional sign-in), the one JSON line
``run_in_docker.py`` waits for, and the cleanup that runs however the process
ends: on SIGTERM, on a failed self-test and on a cancel during setup.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import browser  # noqa: E402

chrome = browser.chrome
egress = browser.egress
paths = browser.paths
processes = browser.processes
signin = browser.signin

WEB_URL = "https://d1a2b3c4d5e6f7.cloudfront.net/"
SAMPLE_LOGIN = "ux-persona-admin@example.invalid"
SAMPLE_CREDENTIAL = "Ux!sample-credential-9aZ"
UI_ENV_TEXT = (
    "VITE_AWS_REGION=us-east-1\n"
    "VITE_API_BASE_URL=https://abcde12345.execute-api.us-east-1.amazonaws.com/api\n"
)


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            self.request.sendall(data)


class _EchoServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True


def _serve(server: socketserver.BaseServer) -> None:
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()


@pytest.mark.unit
def test_the_forwarder_relays_bytes_both_ways_between_two_ephemeral_ports() -> None:
    echo = _EchoServer(("127.0.0.1", 0), _EchoHandler)
    _serve(echo)
    host, port = echo.server_address[:2]
    relay = browser.forwarder(("127.0.0.1", 0), (str(host), int(port)))
    _serve(relay)
    try:
        with socket.create_connection(
            ("127.0.0.1", relay.server_address[1]), timeout=5
        ) as client:
            for message in (b"GET /json/version HTTP/1.1\r\n\r\n", b"x" * 100_000):
                client.sendall(message)
                received = b""
                while len(received) < len(message):
                    received += client.recv(65536)
                assert received == message
    finally:
        relay.shutdown()
        relay.server_close()
        echo.shutdown()
        echo.server_close()


@pytest.mark.unit
def test_the_forwarder_drops_a_client_whose_target_refuses_the_connection() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    relay = browser.forwarder(("127.0.0.1", 0), ("127.0.0.1", closed_port))
    _serve(relay)
    try:
        with socket.create_connection(
            ("127.0.0.1", relay.server_address[1]), timeout=5
        ) as client:
            assert client.recv(64) == b""
    finally:
        relay.shutdown()
        relay.server_close()


@pytest.mark.unit
def test_run_by_path_it_imports_from_scripts_and_not_from_its_own_directory() -> None:
    script = SCRIPTS / "ux_runner" / "browser.py"
    helped = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert helped.returncode == 0, helped.stderr
    assert "--cdp-listen" in helped.stdout
    probe = (
        "import runpy, sys; sys.path.insert(0, sys.argv[1]); "
        "runpy.run_path(sys.argv[2], run_name='probe'); "
        "print(sys.argv[1] in sys.path, sys.argv[3] in sys.path)"
    )
    answer = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", probe, str(script.parent), str(script), str(SCRIPTS)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert answer.stdout.split() == ["False", "True"], answer.stderr


class FakeProcess:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.pid = 4100

    def terminate(self, grace: float = 10.0) -> int | None:
        self.events.append(f"terminate {self.name}")
        return 0


class FakeSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        return {}

    def close(self) -> None:
        self.events.append("close session")


class FakeRelay:
    """The forwarder: served on its own thread, so serving is an Event, not a step."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.served = threading.Event()

    def serve_forever(self) -> None:
        self.served.set()

    def shutdown(self) -> None:
        self.events.append("shutdown forwarder")

    def server_close(self) -> None:
        return None


@dataclass
class Recorded:
    """What the fakes saw during one ``main()`` run."""

    events: list[str]
    seen: dict[str, Any]
    session: FakeSession
    relays: list[FakeRelay]


@pytest.fixture
def fake_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Recorded]:
    """Replace every collaborator of ``main()`` with a recorder of what it was asked."""

    def install(
        *,
        failing: str | None = None,
        failure: BaseException | None = None,
        strays: tuple[str, ...] = (),
    ) -> Recorded:
        events: list[str] = []
        seen: dict[str, Any] = {}
        session = FakeSession(events)
        relays: list[FakeRelay] = []

        def relay(*args: Any, **kwargs: Any) -> FakeRelay:
            relays.append(FakeRelay(events))
            return relays[-1]

        def step(name: str, value: Any = None) -> Callable[..., Any]:
            def fake(*args: Any, **kwargs: Any) -> Any:
                events.append(name)
                seen[name] = (args, kwargs)
                if name == failing and failure is not None:
                    raise failure
                return value(*args, **kwargs) if callable(value) else value

            return fake

        def proxy_run(**kwargs: Any) -> Any:
            return egress.ProxyRun(
                process=FakeProcess("proxy", events),
                ready_file=tmp_path / "ready",
                refusal_log=tmp_path / "refusals.jsonl",
                stats_file=tmp_path / "stats.json",
                log_file=tmp_path / "proxy.log",
                owner=os.geteuid(),
            )

        class FakeWatch:
            def __init__(self, uid: int | None) -> None:
                seen["watch uid"] = uid

            def start(self) -> FakeWatch:
                events.append("watch start")
                return self

            def stop(self) -> Any:
                events.append("watch stop")
                return chrome.SocketAudit("clean", (), 4)

        def stop_waiting() -> None:
            events.append("wait")
            raise processes.Terminated("received signal 15")

        def kill_strays(uid: int | None, group: int | None) -> tuple[str, ...]:
            events.append("kill strays")
            seen["kill strays"] = (uid, group)
            return strays

        monkeypatch.setattr(
            processes,
            "install_sigterm_handler",
            lambda: lambda: events.append("restore"),
        )
        monkeypatch.setattr(processes, "can_switch_user", lambda: True)
        monkeypatch.setattr(chrome, "policy_installed", step("policy", True))
        monkeypatch.setattr(egress, "launch_proxy", step("proxy", proxy_run))
        monkeypatch.setattr(
            chrome,
            "launch",
            step("chrome", lambda *a, **k: FakeProcess("chrome", events)),
        )
        monkeypatch.setattr(chrome, "wait_for_cdp", step("wait_for_cdp", {}))
        monkeypatch.setattr(
            chrome,
            "first_page_target",
            step(
                "first_page_target",
                {
                    "id": "T1",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/T1",
                },
            ),
        )
        monkeypatch.setattr(chrome, "open_session", step("open_session", session))
        monkeypatch.setattr(chrome, "verify_policy", step("verify_policy"))
        monkeypatch.setattr(chrome, "verify_proxy_path", step("verify_proxy_path"))
        monkeypatch.setattr(signin, "sign_in", step("sign_in"))
        monkeypatch.setattr(browser, "forwarder", step("forwarder", relay))
        monkeypatch.setattr(browser, "wait_for_stop", stop_waiting)
        monkeypatch.setattr(chrome, "SocketWatch", FakeWatch)
        monkeypatch.setattr(chrome, "kill_strays", kill_strays)
        monkeypatch.setenv(
            "UX_TEST_UI_ENV", base64.b64encode(UI_ENV_TEXT.encode()).decode()
        )
        monkeypatch.setenv("UX_BROWSER_HOST_PORT", "9333")
        monkeypatch.setenv("UX_BROWSER_CONTAINER", "ux-browser-test")
        persona = {"Admin": {"username": SAMPLE_LOGIN, "password": SAMPLE_CREDENTIAL}}
        monkeypatch.setenv(
            "UX_TEST_PERSONAS", base64.b64encode(json.dumps(persona).encode()).decode()
        )
        toolchain = paths.Toolchain(tmp_path / "prefix")
        toolchain.chrome.parent.mkdir(parents=True)
        toolchain.chrome.write_text("")
        return Recorded(events=events, seen=seen, session=session, relays=relays)

    return install


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--stack",
        "idp-ux-nightly",
        "--cdp-listen",
        "127.0.0.1:0",
        "--out",
        str(tmp_path / "out"),
        "--toolchain-prefix",
        str(tmp_path / "prefix"),
        *extra,
    ]


@pytest.mark.unit
def test_main_confines_before_it_serves_then_audits_and_cleans_up_on_sigterm(
    tmp_path: Path,
    fake_world: Callable[..., Recorded],
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = fake_world()
    assert browser.main(_argv(tmp_path, "--web-url", WEB_URL)) == 0
    assert world.events == [
        "policy",
        "proxy",
        "chrome",
        "watch start",
        "wait_for_cdp",
        "first_page_target",
        "open_session",
        "verify_policy",
        "verify_proxy_path",
        "close session",
        "forwarder",
        "wait",
        "shutdown forwarder",
        "terminate chrome",
        "kill strays",
        "watch stop",
        "terminate proxy",
        "restore",
    ]
    assert world.seen["kill strays"] == (paths.BROWSER_UID, 4100)
    assert world.relays[0].served.wait(5)
    assert world.session.calls == [("Page.navigate", {"url": WEB_URL})]
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0].startswith("cdp forwarder: relays connections from loopback")
    ready = json.loads(lines[1])
    assert ready == {
        "cdp": "http://127.0.0.1:9333",
        "recorder": "docker exec ux-browser-test python3 /repo/scripts/ux_recorder.py",
        "recorder_out": "--out /out/recordings",
    }
    audit = json.loads((tmp_path / "out" / "browser-audit.json").read_text())
    assert audit == {"state": "clean", "violations": [], "sockets": 4, "strays": []}
    assert world.seen["watch uid"] == paths.BROWSER_UID


@pytest.mark.unit
def test_a_browser_process_that_left_chromes_group_is_killed_and_named_in_the_audit(
    tmp_path: Path,
    fake_world: Callable[..., Recorded],
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_world(strays=("pid 4180 (chrome_crashpad)",))
    assert browser.main(_argv(tmp_path, "--web-url", WEB_URL)) == 0
    audit = json.loads((tmp_path / "out" / "browser-audit.json").read_text())
    assert audit["strays"] == ["pid 4180 (chrome_crashpad)"]
    assert "socket audit: clean, 1 stray process(es) killed" in capsys.readouterr().out


@pytest.mark.unit
def test_main_tunnels_only_to_the_stack_and_its_web_host_as_the_two_users(
    tmp_path: Path, fake_world: Callable[..., Recorded]
) -> None:
    world = fake_world()
    browser.main(_argv(tmp_path, "--web-url", WEB_URL))
    proxy_kwargs = world.seen["proxy"][1]
    assert proxy_kwargs["build_dir"] is None
    assert proxy_kwargs["user"] == paths.PROXY_USER
    hosts = proxy_kwargs["rules"].hosts
    assert "d1a2b3c4d5e6f7.cloudfront.net:443" in hosts
    assert "abcde12345.execute-api.us-east-1.amazonaws.com:443" in hosts
    assert world.seen["chrome"][1]["user"] == paths.BROWSER_USER
    run_dir = proxy_kwargs["run_dir"]
    assert world.seen["chrome"][1]["run_dir"] == run_dir
    assert not Path(run_dir).exists()


@pytest.mark.unit
def test_main_signs_in_only_when_asked_and_only_after_both_self_tests(
    tmp_path: Path, fake_world: Callable[..., Recorded]
) -> None:
    world = fake_world()
    build = tmp_path / "build"
    build.mkdir()
    (build / "index.html").write_text("<html></html>")
    assert (
        browser.main(_argv(tmp_path, "--build-dir", str(build), "--sign-in", "Admin"))
        == 0
    )
    events = world.events
    assert events.index("verify_proxy_path") < events.index("sign_in")
    assert events.index("sign_in") < events.index("close session")
    args, kwargs = world.seen["sign_in"]
    assert args[1].name == "Admin"
    assert kwargs["base_url"] == "http://localhost:3000/"
    assert world.seen["proxy"][1]["build_dir"] == build.resolve()
    assert world.session.calls == []


@pytest.mark.unit
def test_without_sign_in_no_persona_is_read_or_signed_in(
    tmp_path: Path, fake_world: Callable[..., Recorded], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = fake_world()
    monkeypatch.delenv("UX_TEST_PERSONAS")
    assert browser.main(_argv(tmp_path, "--web-url", WEB_URL)) == 0
    assert "sign_in" not in world.events


@pytest.mark.unit
def test_a_failed_self_test_is_a_failed_setup_that_still_cleans_up(
    tmp_path: Path,
    fake_world: Callable[..., Recorded],
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = fake_world(
        failing="verify_policy",
        failure=chrome.ChromeError("the managed policy is not in force"),
    )
    assert browser.main(_argv(tmp_path, "--web-url", WEB_URL)) == 1
    out = capsys.readouterr().out
    assert "FAILED: ChromeError: the managed policy is not in force" in out
    assert "{" not in out
    assert world.events[-6:] == [
        "close session",
        "terminate chrome",
        "kill strays",
        "watch stop",
        "terminate proxy",
        "restore",
    ]
    assert "forwarder" not in world.events
    assert not (tmp_path / "out" / "browser-audit.json").exists()


@pytest.mark.unit
def test_a_cancel_during_setup_fails_and_still_cleans_up(
    tmp_path: Path,
    fake_world: Callable[..., Recorded],
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = fake_world(failing="wait_for_cdp", failure=processes.Terminated("15"))
    assert browser.main(_argv(tmp_path, "--web-url", WEB_URL)) == 1
    assert "FAILED: cancelled during setup" in capsys.readouterr().out
    assert world.events[-5:] == [
        "terminate chrome",
        "kill strays",
        "watch stop",
        "terminate proxy",
        "restore",
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("unset", "message"),
    [
        ("UX_TEST_UI_ENV", "UX_TEST_UI_ENV is not set"),
        ("UX_TEST_PERSONAS", "UX_TEST_PERSONAS"),
    ],
)
def test_a_missing_variable_fails_before_any_process_starts(
    tmp_path: Path,
    fake_world: Callable[..., Recorded],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unset: str,
    message: str,
) -> None:
    world = fake_world()
    monkeypatch.delenv(unset)
    assert (
        browser.main(_argv(tmp_path, "--web-url", WEB_URL, "--sign-in", "Admin")) == 1
    )
    out = capsys.readouterr().out
    assert out.startswith("FAILED: ") and message in out
    assert "proxy" not in world.events and "chrome" not in world.events


@pytest.mark.unit
@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--sign-in", "Operator"], "names a persona UX_TEST_PERSONAS lacks"),
        (["--cdp-listen", "nowhere"], "--cdp-listen must be HOST:PORT"),
    ],
)
def test_a_bad_option_fails_the_setup_before_any_process_starts(
    tmp_path: Path,
    fake_world: Callable[..., Recorded],
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    message: str,
) -> None:
    world = fake_world()
    assert browser.main([*_argv(tmp_path, "--web-url", WEB_URL), *extra]) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAILED: ") and message in out
    assert "proxy" not in world.events


@pytest.mark.unit
def test_an_uninstalled_policy_fails_the_setup(
    tmp_path: Path,
    fake_world: Callable[..., Recorded],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = fake_world()
    monkeypatch.setattr(chrome, "policy_installed", lambda expected: False)
    assert browser.main(_argv(tmp_path, "--web-url", WEB_URL)) == 1
    assert "the managed policy is not installed" in capsys.readouterr().out
    assert world.events == ["restore"]


@pytest.mark.unit
def test_an_audit_that_cannot_be_written_fails_after_cleaning_up(
    tmp_path: Path,
    fake_world: Callable[..., Recorded],
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = fake_world()
    (tmp_path / "out").write_text("a file where the directory should be")
    assert browser.main(_argv(tmp_path, "--web-url", WEB_URL)) == 1
    assert "FAILED: the socket audit could not be written" in capsys.readouterr().out
    assert world.events[-5:] == [
        "terminate chrome",
        "kill strays",
        "watch stop",
        "terminate proxy",
        "restore",
    ]


@pytest.mark.unit
def test_a_missing_chrome_fails_before_any_process_starts(
    tmp_path: Path,
    fake_world: Callable[..., Recorded],
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = fake_world()
    argv = _argv(tmp_path, "--web-url", WEB_URL)
    argv[argv.index("--toolchain-prefix") + 1] = str(tmp_path / "empty-prefix")
    assert browser.main(argv) == 1
    assert "Chrome for Testing is not installed" in capsys.readouterr().out
    assert world.events == ["restore"]


@pytest.mark.unit
def test_a_web_url_is_required_unless_a_build_is_served() -> None:
    with pytest.raises(SystemExit) as raised:
        browser.parse_args(["--stack", "idp-ux-nightly"])
    assert raised.value.code == 2
    assert browser.parse_args(["--build-dir", "/out/build"]).web_url is None


@pytest.mark.unit
def test_the_defaults_are_the_documented_container_contract() -> None:
    args = browser.parse_args(["--web-url", WEB_URL])
    assert args.stack == "idp-ux-nightly"
    assert args.cdp_listen == "0.0.0.0:9223"
    assert args.out == "/out/browser"
    assert args.sign_in is None and args.build_dir is None


@pytest.mark.unit
def test_the_watch_spans_chromes_whole_life_from_launch_to_after_it_exits(
    tmp_path: Path, fake_world: Callable[..., Recorded]
) -> None:
    """A socket opened during the self-tests or the sign-in, or while Chrome is
    shutting down, is inside the audited window."""
    world = fake_world()
    browser.main(_argv(tmp_path, "--build-dir", _build(tmp_path), "--sign-in", "Admin"))
    events = world.events
    assert events.index("chrome") + 1 == events.index("watch start")
    for step in ("wait_for_cdp", "verify_policy", "verify_proxy_path", "sign_in"):
        assert events.index("watch start") < events.index(step), step
    assert events.index("terminate chrome") < events.index("watch stop")
    assert events.count("watch stop") == 1


def _build(tmp_path: Path) -> str:
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    (build / "index.html").write_text("<html></html>")
    return str(build)


@pytest.mark.unit
def test_main_relays_only_loopback_and_the_containers_gateway(
    tmp_path: Path,
    fake_world: Callable[..., Recorded],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = fake_world()
    monkeypatch.setattr(
        browser, "container_gateways", lambda: frozenset({"172.17.0.1"})
    )
    assert browser.main(_argv(tmp_path, "--web-url", WEB_URL)) == 0
    admit = world.seen["forwarder"][1]["admit"]
    assert admit("172.17.0.1") and admit("127.0.0.1")
    assert not admit("172.17.0.3"), "another container on the same network"
    assert not admit("192.168.1.20"), "a LAN peer routed to the bridge"
    out = capsys.readouterr().out
    assert "relays connections from loopback and 172.17.0.1" in out


class _CountingEcho(_EchoServer):
    def __init__(self) -> None:
        self.connections = 0
        super().__init__(("127.0.0.1", 0), _CountedEchoHandler)


class _CountedEchoHandler(_EchoHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _CountingEcho)
        server.connections += 1
        super().handle()


def _closed_without_a_reply(client: socket.socket) -> bool:
    """Whether the peer closed without sending a byte.

    A close that finds the request still unread goes out as a reset rather than
    a FIN, depending on which side of the race the request arrived, and both say
    the same thing here.
    """
    try:
        return client.recv(64) == b""
    except ConnectionResetError:
        return True


@pytest.mark.unit
def test_the_forwarder_closes_a_peer_it_does_not_admit_before_reaching_the_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    echo = _CountingEcho()
    _serve(echo)
    host, port = echo.server_address[:2]
    relay = browser.forwarder(
        ("127.0.0.1", 0), (str(host), int(port)), admit=lambda peer: False
    )
    _serve(relay)
    try:
        for _ in range(2):
            with socket.create_connection(
                ("127.0.0.1", relay.server_address[1]), timeout=5
            ) as client:
                client.sendall(b"GET /json/version HTTP/1.1\r\n\r\n")
                assert _closed_without_a_reply(client)
    finally:
        relay.shutdown()
        relay.server_close()
        echo.shutdown()
        echo.server_close()
    assert echo.connections == 0
    refused = [line for line in capsys.readouterr().out.splitlines() if line]
    assert refused == ["cdp forwarder: refused a connection from 127.0.0.1"]


ROUTE_HEADER = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU"
    "\tWindow\tIRTT"
)


@pytest.mark.unit
def test_the_gateway_is_read_from_the_default_route_in_host_byte_order(
    tmp_path: Path,
) -> None:
    table = tmp_path / "route"
    table.write_text(
        "\n".join(
            [
                ROUTE_HEADER,
                "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0",
                "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0",
                "eth1\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0",
                "eth2\t00000000\tnothex\t0003\t0\t0\t0\t00000000\t0\t0\t0",
                "short",
            ]
        )
        + "\n"
    )
    assert browser.container_gateways(table) == frozenset({"172.17.0.1"})
    assert browser.container_gateways(tmp_path / "absent") == frozenset()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("peer", "admitted"),
    [
        ("127.0.0.1", True),
        ("127.0.0.53", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("172.17.0.1", True),
        ("::ffff:172.17.0.1", True),
        ("172.17.0.2", False),
        ("10.0.0.7", False),
        ("fe80::1%eth0", False),
        ("not-an-address", False),
        ("", False),
    ],
)
def test_the_peer_filter_admits_loopback_and_the_gateway_only(
    peer: str, admitted: bool
) -> None:
    assert browser.peer_filter({"172.17.0.1"})(peer) is admitted


@pytest.mark.unit
def test_without_a_gateway_only_loopback_is_relayed_and_the_line_says_so() -> None:
    admit = browser.peer_filter(browser.container_gateways(Path("/nonexistent/route")))
    assert admit("127.0.0.1") and not admit("172.17.0.1")
    assert "loopback only" in browser._admission_line(frozenset())
