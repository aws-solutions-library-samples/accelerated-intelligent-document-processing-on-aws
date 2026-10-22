# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the UX test browser's egress proxy, ``scripts/ux_test_proxy.py``.

The proxy is the confined Chrome's only network path, so what is pinned here is
what it lets through and what it refuses, with the reason it records. A real
``EgressProxy`` runs in-process on an ephemeral port with an injected resolver
and an injected upstream ``connect`` that always reaches a local echo server, so
a tunnel is exercised end to end with no name lookup and no outbound socket. The
static host list is pinned exactly, because a host added to it becomes reachable
by the MR's own page code; that pin is the registry's count ratchet for it.

The proxy reads each request itself, so how it reads is pinned as well as what it
decides: a request ``http.server`` cannot parse, a head over the byte cap, one that
stalls or trickles, a connection beyond the cap and a body nobody reads are each
refused or closed, and recorded, with nothing relayed; a Host header never changes
a decision; and nothing after a head is consumed with it, so pipelined requests
and the first bytes of a tunnel survive. The oversized heads are one byte over the
cap, so the proxy reads every byte it was sent and its answer is not lost to a
reset. The last tests run the executable itself under ``python3 -I -B``, as
``ux_runner/egress.py`` starts it, to show it reports readiness, counts a refusal
and writes its stats on SIGTERM.
"""

from __future__ import annotations

import ast
import http.client
import ipaddress
import json
import select
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ux_test_proxy as proxy  # noqa: E402

PROXY_SCRIPT = SCRIPTS / "ux_test_proxy.py"
STACK_HOST = "abcde12345.execute-api.us-east-1.amazonaws.com"
PUBLIC_V4 = "52.95.110.1"
PUBLIC_V6 = "2600:1f18::1"
UNSPECIFIED = str(ipaddress.IPv4Address(0))
INDEX_HTML = b"<!doctype html><title>UX</title>"
APP_JS = b"console.log('ux');\n"
#: A complete request for another file, sent where a body belongs.
SMUGGLED = b"GET http://localhost:3000/assets/app.js HTTP/1.1\r\nHost: x\r\n\r\n"


def _ui_env() -> dict[str, str]:
    """A parsed UX_TEST_UI_ENV in the WebUITestEnvFile shape, every id fake."""
    return {
        "VITE_USER_POOL_ID": "us-east-1_AbCdEfGhI",
        "VITE_USER_POOL_CLIENT_ID": "1a2b3c4d5e6f7g8h9i0j1k2l3m",
        "VITE_IDENTITY_POOL_ID": "us-east-1:0a1b2c3d-1111-2222-3333-4e5f6a7b8c9d",
        "VITE_API_BASE_URL": f"https://{STACK_HOST}/api",
        "VITE_STREAM_URL": (
            "https://abcdefghijklmnopqrstuvwxyz012345.lambda-url.us-east-1.on.aws/"
        ),
        "VITE_AWS_REGION": "us-east-1",
        "VITE_ENABLE_QUICK_START_WIDGET": "true",
        "VITE_SETTINGS_PARAMETER": "idp-ux-nightly-Settings",
        "VITE_TEST_SET_BUCKET_NAME": "idp-ux-nightly-testsetbucket-abc123",
        "VITE_INPUT_BUCKET_NAME": "idp-ux-nightly-inputbucket-def456",
        "VITE_LATENCY_OPTIONS": "[60, 120, 300, 600, 900, 1800, 3600]",
        "VITE_DEFAULT_MAX_LATENCY": "60",
        "UX_TEST_BUCKETS": (
            "idp-ux-nightly-outputbucket-ghi789,idp-ux-nightly-workingbucket-jkl012"
        ),
    }


def _answer(address: str, port: int) -> tuple[Any, ...]:
    """One getaddrinfo answer for ``address``, as the system resolver shapes it."""
    if ":" in address:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))


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


@pytest.fixture
def echo_address() -> Iterator[tuple[str, int]]:
    server = _EchoServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    host, port = server.server_address[:2]
    yield str(host), int(port)
    server.shutdown()
    server.server_close()


def _make_site(root: Path) -> Path:
    """A static build: an index, assets of several types, and two symlinks."""
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_bytes(INDEX_HTML)
    (root / "assets" / "app.js").write_bytes(APP_JS)
    (root / "assets" / "style.css").write_text("body { margin: 0 }\n")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "data.bin").write_bytes(b"\x00\x01")
    (root / "link.html").symlink_to(root / "index.html")
    (root / "linkdir").symlink_to(root / "assets", target_is_directory=True)
    return root


@dataclass
class Running:
    """One in-process proxy and what its injected collaborators observed."""

    server: proxy.EgressProxy
    port: int
    log_path: Path
    connects: list[tuple[str, int]]
    lookups: list[str]

    def records(self) -> list[dict[str, Any]]:
        """The refusal log's records, in order."""
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def reasons(self) -> list[str]:
        """The reasons the refusal log recorded, in order."""
        return [record["reason"] for record in self.records()]


@pytest.fixture
def start_proxy(
    tmp_path: Path, echo_address: tuple[str, int]
) -> Iterator[Callable[..., Running]]:
    servers: list[proxy.EgressProxy] = []

    def factory(
        *,
        answers: dict[str, list[str]] | None = None,
        hosts: frozenset[str] | None = None,
        with_site: bool = True,
        max_tunnels: int = 64,
        max_connections: int | None = None,
        resolver_error: OSError | None = None,
        connect_error: OSError | None = None,
        tunnel_idle: float = 5.0,
        request_timeout: float = 5.0,
    ) -> Running:
        table = answers if answers is not None else {STACK_HOST: [PUBLIC_V4]}
        index = len(servers)
        log_path = tmp_path / f"refusals-{index}.jsonl"
        site = proxy.Site(_make_site(tmp_path / f"site-{index}")) if with_site else None
        connects: list[tuple[str, int]] = []
        lookups: list[str] = []

        def resolver(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
            lookups.append(host)
            if resolver_error is not None:
                raise resolver_error
            return [_answer(address, port) for address in table.get(host, [])]

        def connect(address: str, port: int, timeout: float) -> socket.socket:
            connects.append((address, port))
            if connect_error is not None:
                raise connect_error
            return socket.create_connection(echo_address, timeout)

        listed = (
            frozenset({f"{STACK_HOST}:443", "cdn.jsdelivr.net:443"})
            if hosts is None
            else hosts
        )
        server = proxy.EgressProxy(
            ("127.0.0.1", 0),
            rules=proxy.EgressRules(listed),
            site=site,
            log=proxy.RefusalLog(log_path),
            resolver=resolver,
            connect=connect,
            max_tunnels=max_tunnels,
            tunnel_idle=tunnel_idle,
            max_connections=max_connections,
            request_timeout=request_timeout,
        )
        running = Running(
            server=server,
            port=server.server_address[1],
            log_path=log_path,
            connects=connects,
            lookups=lookups,
        )
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        servers.append(server)
        return running

    yield factory
    for server in servers:
        server.shutdown()
        server.server_close()


def _read_head(sock: socket.socket) -> bytes:
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    return head


def _status(head: bytes) -> int:
    return int(head.split(b" ", 2)[1]) if head else 0


def _tunnel(
    port: int, target: str, host: str | None = None
) -> tuple[int, socket.socket]:
    """Send a CONNECT; the status and the still-open client socket.

    The Host header names the target unless ``host`` says otherwise.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(
        f"CONNECT {target} HTTP/1.1\r\nHost: {host or target}\r\n\r\n".encode()
    )
    return _status(_read_head(sock)), sock


def _read_to_close(sock: socket.socket) -> bytes:
    """Everything the proxy sends until it closes; a reset counts as the close."""
    data = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except ConnectionResetError:
            return data
        if not chunk:
            return data
        data += chunk


def _exchange(port: int, request: str | bytes, *, half_close: bool = False) -> bytes:
    """Send ``request`` on a new connection and read until the proxy closes it.

    With ``half_close`` the client ends its side right after sending, as a client
    that gives up on a request does.
    """
    payload = request.encode() if isinstance(request, str) else request
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(payload)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        return _read_to_close(sock)


def _raw(
    port: int, request: str | bytes, *, half_close: bool = False
) -> tuple[int, bytes, bytes]:
    """Send one request and read until the proxy closes: status, head, body."""
    data = _exchange(port, request, half_close=half_close)
    head, _, body = data.partition(b"\r\n\r\n")
    return _status(head), head, body


def _keep_alive(port: int) -> http.client.HTTPConnection:
    """A connection the proxy has answered once and is holding open for more."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "http://localhost:3000/")
    response = conn.getresponse()
    assert response.status == 200 and response.read() == INDEX_HTML
    return conn


def _get(port: int, url: str) -> tuple[int, dict[str, str], bytes]:
    """One GET through the proxy on its own connection: status, headers, body."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", url)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


@pytest.mark.unit
def test_the_static_hosts_are_pinned_to_the_monaco_cdn_alone() -> None:
    assert proxy.EGRESS_ALLOWED_STATIC_HOSTS == ("cdn.jsdelivr.net:443",), (
        "EGRESS_ALLOWED_STATIC_HOSTS is pinned: every host on it is reachable by "
        "the MR's own page code, so the list is audited when it changes, here and "
        "in its gate_exemptions.json entry, rather than grown by a one-word edit"
    )


@pytest.mark.unit
def test_the_listen_address_is_the_one_paths_and_the_managed_policy_name() -> None:
    from ux_runner import paths

    assert proxy.DEFAULT_LISTEN == "127.0.0.1:3128"
    assert proxy.DEFAULT_LISTEN == f"{paths.PROXY_HOST}:{paths.PROXY_PORT}"
    assert f"http://{proxy.LOCAL_UI_HOST}:{proxy.LOCAL_UI_PORT}" == paths.UI_ORIGIN
    policy = json.loads((SCRIPTS / "ux_runner" / "chrome_policy.json").read_text())
    assert policy["ProxySettings"]["ProxyServer"] == f"http://{proxy.DEFAULT_LISTEN}"


@pytest.mark.unit
def test_the_proxy_imports_nothing_but_the_standard_library() -> None:
    tree = ast.parse(PROXY_SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import cannot work under python3 -I"
            imported.add((node.module or "").split(".")[0])
    outside = sorted(imported - set(sys.stdlib_module_names) - {"__future__"})
    assert outside == [], f"ux_test_proxy.py must stay stdlib-only: {outside}"


@pytest.mark.unit
def test_a_listed_host_is_tunnelled_to_exactly_the_address_that_was_checked(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy()
    status, sock = _tunnel(running.port, f"{STACK_HOST}:443")
    with sock:
        assert status == 200
        sock.sendall(b"ping through the tunnel")
        echoed = b""
        while len(echoed) < len(b"ping through the tunnel"):
            echoed += sock.recv(64)
    assert echoed == b"ping through the tunnel"
    assert running.connects == [(PUBLIC_V4, 443)]
    assert running.server.stats()["connect_allowed"] == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "target",
    [
        "169.254.169.254:80",
        "169.254.169.254:443",
        "[fd00:ec2::254]:443",
        "127.0.0.1:9222",
        "localhost:9222",
        "evil.example:443",
        "no-port.example",
        f"{STACK_HOST}:https",
        "http://cdn.jsdelivr.net:443",
        f"{PUBLIC_V4}:443",
    ],
)
def test_unlisted_and_internal_targets_are_refused_before_any_lookup(
    start_proxy: Callable[..., Running], target: str
) -> None:
    running = start_proxy()
    status, sock = _tunnel(running.port, target)
    sock.close()
    assert status == 403
    assert running.reasons() == ["host_not_allowed"]
    assert running.lookups == [] and running.connects == []


@pytest.mark.unit
def test_a_listed_host_on_another_port_is_refused_for_its_port(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy()
    status, sock = _tunnel(running.port, f"{STACK_HOST}:8443")
    sock.close()
    assert status == 403
    assert running.reasons() == ["port_not_allowed"]
    assert running.connects == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "addresses",
    [[PUBLIC_V4, "10.0.0.5"], ["::ffff:127.0.0.1"], ["169.254.169.254"], []],
    ids=["one-private-answer", "mapped-loopback", "metadata", "no-answer"],
)
def test_a_listed_host_resolving_to_any_internal_address_is_refused(
    start_proxy: Callable[..., Running], addresses: list[str]
) -> None:
    running = start_proxy(answers={STACK_HOST: addresses})
    status, sock = _tunnel(running.port, f"{STACK_HOST}:443")
    sock.close()
    assert status == 403
    assert running.reasons() == ["resolves_to_non_public_address"]
    assert running.connects == []


@pytest.mark.unit
def test_an_aws_service_name_answering_a_vpc_endpoint_address_is_tunnelled(
    start_proxy: Callable[..., Running],
) -> None:
    """An interface endpoint with private DNS answers the service's public name
    with its own private address inside the VPC, as SSM's does on the CI runners."""
    running = start_proxy(answers={STACK_HOST: ["10.0.12.34"]})
    status, sock = _tunnel(running.port, f"{STACK_HOST}:443")
    sock.close()
    assert status == 200
    assert running.connects == [("10.0.12.34", 443)]
    assert running.reasons() == []


@pytest.mark.unit
def test_a_listed_name_outside_aws_answering_a_vpc_endpoint_address_is_refused(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy(
        hosts=frozenset({"cdn.jsdelivr.net:443"}),
        answers={"cdn.jsdelivr.net": ["10.0.12.34"]},
    )
    status, sock = _tunnel(running.port, "cdn.jsdelivr.net:443")
    sock.close()
    assert status == 403
    assert running.reasons() == ["resolves_to_non_public_address"]
    assert running.connects == []


@pytest.mark.unit
def test_a_failed_lookup_is_an_upstream_failure_rather_than_a_refusal(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy(resolver_error=socket.gaierror("no such name"))
    status, sock = _tunnel(running.port, f"{STACK_HOST}:443")
    sock.close()
    assert status == 502
    stats = running.server.stats()
    assert stats["upstream_failed"] == 1 and stats["refused"] == 0


@pytest.mark.unit
def test_an_upstream_that_refuses_the_connection_is_a_502_not_a_refusal(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy(connect_error=ConnectionRefusedError("refused"))
    status, sock = _tunnel(running.port, f"{STACK_HOST}:443")
    sock.close()
    assert status == 502
    assert running.connects == [(PUBLIC_V4, 443)]
    stats = running.server.stats()
    assert stats["upstream_failed"] == 1 and stats["connect_allowed"] == 0
    assert running.reasons() == []


@pytest.mark.unit
def test_an_idle_tunnel_is_closed_after_tunnel_idle_seconds(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy(tunnel_idle=0.2)
    status, sock = _tunnel(running.port, f"{STACK_HOST}:443")
    with sock:
        assert status == 200
        sock.settimeout(5)
        started = time.monotonic()
        assert sock.recv(64) == b""
        assert time.monotonic() - started < 4


@pytest.mark.unit
def test_a_tunnel_beyond_the_cap_is_refused_with_503(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy(max_tunnels=1)
    first_status, first = _tunnel(running.port, f"{STACK_HOST}:443")
    with first:
        assert first_status == 200
        second_status, second = _tunnel(running.port, f"{STACK_HOST}:443")
        second.close()
    assert second_status == 503
    assert running.reasons() == ["too_many_tunnels"]


@pytest.mark.unit
def test_a_connect_is_judged_by_its_target_and_never_by_its_host_header(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy()
    status, sock = _tunnel(running.port, "evil.example:443", host=f"{STACK_HOST}:443")
    sock.close()
    assert status == 403
    assert running.reasons() == ["host_not_allowed"]
    assert running.lookups == [] and running.connects == []
    status, sock = _tunnel(running.port, f"{STACK_HOST}:443", host="evil.example:443")
    sock.close()
    assert status == 200
    assert running.lookups == [STACK_HOST]
    assert running.connects == [(PUBLIC_V4, 443)]


@pytest.mark.unit
def test_bytes_sent_right_after_a_connect_head_are_the_tunnels_first_bytes(
    start_proxy: Callable[..., Running],
) -> None:
    """A client may write its TLS hello without waiting for the 200; the head read
    must not have swallowed it."""
    running = start_proxy()
    request = f"CONNECT {STACK_HOST}:443 HTTP/1.1\r\nHost: {STACK_HOST}:443\r\n\r\n"
    with socket.create_connection(("127.0.0.1", running.port), timeout=5) as sock:
        sock.sendall(request.encode() + b"early hello")
        data = b""
        while not data.endswith(b"early hello"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    head, _, relayed = data.partition(b"\r\n\r\n")
    assert _status(head) == 200
    assert relayed == b"early hello"


@pytest.mark.unit
def test_an_ip_address_is_never_tunnelled_to_even_by_a_list_that_names_it(
    start_proxy: Callable[..., Running],
) -> None:
    rules = proxy.EgressRules(frozenset({f"{PUBLIC_V4}:443", "0x34.0x5f.0x6e.0x1:443"}))
    assert not rules.permits(PUBLIC_V4, 443)
    assert not rules.permits("0x34.0x5f.0x6e.0x1", 443)
    running = start_proxy(
        hosts=frozenset({f"{PUBLIC_V4}:443"}), answers={PUBLIC_V4: [PUBLIC_V4]}
    )
    status, sock = _tunnel(running.port, f"{PUBLIC_V4}:443")
    sock.close()
    assert status == 403
    assert running.reasons() == ["host_not_allowed"]
    assert running.lookups == [] and running.connects == []


@pytest.mark.unit
def test_resolve_public_prefers_the_first_ipv4_answer_and_falls_back_to_ipv6() -> None:
    def answering(*addresses: str) -> Callable[..., list[Any]]:
        return lambda host, port, **kwargs: [_answer(a, port) for a in addresses]

    assert proxy.resolve_public("h", 443, answering(PUBLIC_V6, PUBLIC_V4)) == PUBLIC_V4
    assert proxy.resolve_public("h", 443, answering(PUBLIC_V6)) == PUBLIC_V6
    assert proxy.resolve_public("h", 443, answering()) is None
    assert proxy.resolve_public("h", 443, answering(PUBLIC_V4, "fe80::1")) is None
    unix_answer = (socket.AF_UNIX, socket.SOCK_STREAM, 0, "", (PUBLIC_V4, 443))
    assert proxy.resolve_public("h", 443, lambda *a, **k: [unix_answer]) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.8.9.10",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        UNSPECIFIED,
        "224.0.0.1",
        "::1",
        "::",
        "fd00:ec2::254",
        "fe80::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "64:ff9b::7f00:1",
        "64:ff9b::a9fe:a9fe",
        "::7f00:1",
        "not-an-address",
    ],
)
def test_internal_special_and_malformed_addresses_are_not_public(address: str) -> None:
    assert proxy.is_public_address(address) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "address", [PUBLIC_V4, PUBLIC_V6, "::ffff:52.95.110.1", "64:ff9b::345f:6e01"]
)
def test_global_unicast_addresses_are_public(address: str) -> None:
    assert proxy.is_public_address(address) is True


@pytest.mark.unit
def test_the_vpc_endpoint_networks_are_pinned_to_rfc_1918_and_the_shared_range() -> (
    None
):
    assert proxy.VPC_ENDPOINT_ALLOWED_NETWORKS == tuple(
        ipaddress.IPv4Network(network)
        for network in (
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "100.64.0.0/10",
        )
    ), (
        "VPC_ENDPOINT_ALLOWED_NETWORKS is pinned: an AWS service name resolving into "
        "any range on it is reachable by the MR's own page code, so the list is "
        "audited when it changes, here and in its gate_exemptions.json entry"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "address",
    ["10.0.12.34", "172.31.255.254", "192.168.1.1", "100.64.0.1", "::ffff:10.0.0.1"],
)
def test_rfc_1918_and_shared_addresses_are_vpc_endpoint_addresses(address: str) -> None:
    assert proxy.is_vpc_endpoint_address(address) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "169.254.169.254",
        "172.32.0.1",
        "100.128.0.1",
        UNSPECIFIED,
        "224.0.0.1",
        PUBLIC_V4,
        "::1",
        "fd00:ec2::254",
        "fe80::1",
        "64:ff9b::a00:1",
        "not-an-address",
    ],
)
def test_loopback_metadata_link_local_and_ipv6_are_never_vpc_endpoint_addresses(
    address: str,
) -> None:
    assert proxy.is_vpc_endpoint_address(address) is False


@pytest.mark.unit
def test_an_aws_name_may_answer_only_vpc_endpoint_addresses_and_never_a_mix() -> None:
    def answering(*addresses: str) -> Callable[..., list[Any]]:
        return lambda host, port, **kwargs: [_answer(a, port) for a in addresses]

    endpoint = answering("10.0.12.34", "10.0.40.7")
    assert proxy.resolve_public("h", 443, endpoint, vpc_endpoint=True) == "10.0.12.34"
    assert proxy.resolve_public("h", 443, endpoint) is None
    public = answering(PUBLIC_V4)
    assert proxy.resolve_public("h", 443, public, vpc_endpoint=True) == PUBLIC_V4
    for mixed in (
        (PUBLIC_V4, "10.0.0.5"),
        ("10.0.0.5", "127.0.0.1"),
        ("10.0.0.5", "169.254.169.254"),
        ("10.0.0.5", "fd00:ec2::254"),
    ):
        assert (
            proxy.resolve_public("h", 443, answering(*mixed), vpc_endpoint=True) is None
        ), mixed


@pytest.mark.unit
def test_the_local_build_is_served_at_localhost_3000_with_its_headers(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy()
    status, headers, body = _get(running.port, "http://localhost:3000/")
    assert status == 200
    assert body == INDEX_HTML
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Content-Length"] == str(len(INDEX_HTML))
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert running.server.stats()["served"] == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/assets/app.js", "text/javascript; charset=utf-8"),
        ("/assets/style.css", "text/css; charset=utf-8"),
        ("/logo.png", "image/png"),
        ("/data.bin", "application/octet-stream"),
        ("/index.html?cache=1#top", "text/html; charset=utf-8"),
    ],
)
def test_assets_are_served_with_the_content_type_of_their_suffix(
    start_proxy: Callable[..., Running], path: str, content_type: str
) -> None:
    running = start_proxy()
    status, headers, _body = _get(running.port, f"http://localhost:3000{path}")
    assert status == 200
    assert headers["Content-Type"] == content_type


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "/../etc/passwd",
        "/%2e%2e/x",
        "/assets/%2E%2E/index.html",
        "/index.html%00",
        "/assets%5capp.js",
        "/link.html",
        "/linkdir/app.js",
        "/assets/",
        "/assets",
        "/index.html/app.js",
        "//index.html",
        "/missing.js",
    ],
)
def test_nothing_outside_the_builds_regular_files_is_served(
    start_proxy: Callable[..., Running], path: str
) -> None:
    running = start_proxy()
    status, _head, _body = _raw(
        running.port,
        f"GET http://localhost:3000{path} HTTP/1.1\r\nHost: localhost:3000\r\n\r\n",
    )
    assert status == 404
    assert running.reasons() == ["path_not_served"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "http://localhost:3001/",
        "https://localhost:3000/",
        "http://user@localhost:3000/",
        "http://localhost:99999/",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:9222/json/list",
    ],
)
def test_plain_http_to_anything_but_the_local_build_is_refused(
    start_proxy: Callable[..., Running], url: str
) -> None:
    running = start_proxy()
    status, head, body = _raw(running.port, f"GET {url} HTTP/1.1\r\nHost: x\r\n\r\n")
    assert status == 403
    assert b"Connection: close" in head
    assert body == b"refused by the UX test egress proxy: plain_http_not_allowed"
    assert running.reasons() == ["plain_http_not_allowed"]


@pytest.mark.unit
def test_without_a_build_even_the_local_origin_is_refused(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy(with_site=False)
    status, _head, _body = _raw(
        running.port, "GET http://localhost:3000/ HTTP/1.1\r\nHost: x\r\n\r\n"
    )
    assert status == 403
    assert running.reasons() == ["plain_http_not_allowed"]


@pytest.mark.unit
def test_an_origin_form_request_is_not_a_proxy_request(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy()
    status, _head, _body = _raw(running.port, "GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    assert status == 403
    assert running.reasons() == ["not_a_proxy_request"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "url", "status"),
    [
        ("POST", "http://localhost:3000/", 405),
        ("PUT", "http://localhost:3000/index.html", 405),
        ("DELETE", "http://example.com/", 403),
        ("OPTIONS", "http://example.com/", 403),
        ("PROPFIND", "http://localhost:3000/", 405),
        ("POST", "http://[x]/", 403),
    ],
)
def test_every_method_but_get_and_head_is_refused_and_logged(
    start_proxy: Callable[..., Running], method: str, url: str, status: int
) -> None:
    running = start_proxy()
    got, head, _body = _raw(
        running.port,
        f"{method} {url} HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n",
    )
    assert got == status
    assert (b"Allow: GET, HEAD" in head) is (status == 405)
    assert running.reasons() == ["method_not_allowed"]


@pytest.mark.unit
def test_head_sends_the_headers_of_the_file_and_no_body(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy()
    status, head, body = _raw(
        running.port,
        "HEAD http://localhost:3000/ HTTP/1.1\r\nHost: localhost:3000\r\n"
        "Connection: close\r\n\r\n",
    )
    assert status == 200
    assert f"Content-Length: {len(INDEX_HTML)}".encode() in head
    assert body == b""


@pytest.mark.unit
def test_two_requests_share_one_keep_alive_connection(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy()
    conn = http.client.HTTPConnection("127.0.0.1", running.port, timeout=5)
    conn.request("GET", "http://localhost:3000/")
    first = conn.getresponse()
    assert first.read() == INDEX_HTML
    sock = conn.sock
    conn.request("GET", "http://localhost:3000/assets/app.js")
    second = conn.getresponse()
    assert second.status == 200 and second.read() == APP_JS
    assert conn.sock is sock
    conn.close()


@pytest.mark.unit
def test_two_pipelined_requests_are_both_answered_in_order(
    start_proxy: Callable[..., Running],
) -> None:
    """Both heads arrive in one write, so reading the first must leave the second.

    The first says ``Content-Length: 0``, which is no body and keeps the
    connection open.
    """
    running = start_proxy()
    data = _exchange(
        running.port,
        "GET http://localhost:3000/ HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
        "GET http://localhost:3000/assets/app.js HTTP/1.1\r\nHost: x\r\n"
        "Connection: close\r\n\r\n",
    )
    assert data.count(b"HTTP/1.1 200 OK\r\n") == 2
    assert data.index(INDEX_HTML) < data.index(APP_JS)
    assert running.server.stats()["served"] == 2


@pytest.mark.unit
def test_an_absolute_form_get_is_judged_by_its_url_and_never_by_its_host_header(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy()
    refused, _head, _body = _raw(
        running.port, "GET http://example.com/ HTTP/1.1\r\nHost: localhost:3000\r\n\r\n"
    )
    served, _head, body = _raw(
        running.port,
        "GET http://localhost:3000/ HTTP/1.1\r\nHost: evil.example\r\n"
        "Connection: close\r\n\r\n",
    )
    assert refused == 403
    assert served == 200 and body == INDEX_HTML
    assert running.reasons() == ["plain_http_not_allowed"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("request_bytes", "status"),
    [
        (b"\x00\x01garbage\r\n\r\n", 400),
        (b"\r\n", 400),
        (b" \t \r\nHost: x\r\n\r\n", 400),
        (f"CONNECT {STACK_HOST}:443\r\nHost: {STACK_HOST}:443\r\n\r\n".encode(), 400),
        (b"GET http://localhost:3000/ HTTX/1.1\r\nHost: x\r\n\r\n", 400),
        (b"GET http://localhost:3000/ HTTP/1.1 extra\r\nHost: x\r\n\r\n", 400),
        (b"GET http://localhost:3000/ HTTP/2.0\r\nHost: x\r\n\r\n", 505),
        (f"CONNECT {STACK_HOST}:443 HTTP/2.0\r\n\r\n".encode(), 505),
        (b"GET http://[x]/ HTTP/1.1\r\nHost: x\r\n\r\n", 400),
        (b"GET http://[abc/ HTTP/1.1\r\nHost: x\r\n\r\n", 400),
        (
            b"GET http://localhost:3000/ HTTP/1.1\r\n" + b"X-H: v\r\n" * 101 + b"\r\n",
            431,
        ),
    ],
    ids=[
        "garbage",
        "empty-request-line",
        "blank-request-line",
        "connect-without-a-version",
        "unknown-protocol",
        "four-words",
        "http-2",
        "http-2-connect-to-a-listed-host",
        "bad-bracketed-host",
        "unclosed-bracket",
        "101-headers",
    ],
)
def test_a_request_that_cannot_be_parsed_is_refused_recorded_and_closed(
    start_proxy: Callable[..., Running], request_bytes: bytes, status: int
) -> None:
    running = start_proxy()
    got, head, body = _raw(running.port, request_bytes)
    assert got == status
    assert b"Connection: close" in head
    assert body == b"refused by the UX test egress proxy: malformed_request"
    assert running.reasons() == ["malformed_request"]
    assert running.lookups == [] and running.connects == []
    assert running.server.stats()["served"] == 0


@pytest.mark.unit
def test_a_refusal_records_the_method_and_target_its_request_line_names(
    start_proxy: Callable[..., Running],
) -> None:
    """Even when ``http.server`` refuses the line before it names a command."""
    running = start_proxy()
    _raw(running.port, f"CONNECT {STACK_HOST}:443\r\nHost: x\r\n\r\n")
    [record] = running.records()
    assert (record["method"], record["target"]) == ("CONNECT", f"{STACK_HOST}:443")
    assert record["reason"] == "malformed_request"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("prefix", "status"),
    [
        (b"GET http://localhost:3000/", 414),
        (b"GET http://localhost:3000/ HTTP/1.1\r\nX-Pad: ", 431),
    ],
    ids=["request-line", "header-line"],
)
def test_a_head_one_byte_over_the_limit_is_refused(
    start_proxy: Callable[..., Running], prefix: bytes, status: int
) -> None:
    running = start_proxy()
    payload = prefix + b"a" * (proxy.HEAD_LIMIT + 1 - len(prefix))
    got, head, _body = _raw(running.port, payload)
    assert got == status
    assert b"Connection: close" in head
    assert running.reasons() == ["malformed_request"]
    assert running.server.stats()["served"] == 0


@pytest.mark.unit
def test_a_head_with_one_line_too_many_is_refused_without_waiting_for_its_end(
    start_proxy: Callable[..., Running],
) -> None:
    """Its empty line never comes, so a refusal at the deadline would be a 408."""
    running = start_proxy()
    payload = (
        b"GET http://localhost:3000/ HTTP/1.1\r\n" + b"X: v\r\n" * proxy.HEAD_LINES
    )
    started = time.monotonic()
    status, _head, _body = _raw(running.port, payload)
    assert status == 431
    assert time.monotonic() - started < 4
    assert running.reasons() == ["malformed_request"]


@pytest.mark.unit
def test_a_head_of_exactly_the_limit_is_served(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy()
    prefix = b"GET http://localhost:3000/ HTTP/1.1\r\nConnection: close\r\nX-Pad: "
    suffix = b"\r\n\r\n"
    payload = prefix + b"a" * (proxy.HEAD_LIMIT - len(prefix) - len(suffix)) + suffix
    assert len(payload) == proxy.HEAD_LIMIT
    status, _head, body = _raw(running.port, payload)
    assert status == 200 and body == INDEX_HTML
    assert running.reasons() == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "partial",
    [
        b"GET http://localhost:3000/ HTTP/1.1\r\nHost: x\r\n",
        b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03",
        b"CONNECT evil.exa",
    ],
    ids=["headers-never-end", "tls-hello-to-the-proxy-port", "half-a-request-line"],
)
def test_a_head_that_stalls_part_way_is_refused_at_the_deadline(
    start_proxy: Callable[..., Running], partial: bytes
) -> None:
    running = start_proxy(request_timeout=0.3)
    started = time.monotonic()
    status, head, _body = _raw(running.port, partial)
    assert status == 408
    assert b"Connection: close" in head
    assert time.monotonic() - started < 4
    assert running.reasons() == ["request_timeout"]
    assert running.lookups == [] and running.connects == []


@pytest.mark.unit
def test_a_client_trickling_its_head_is_held_to_one_deadline_for_all_of_it(
    start_proxy: Callable[..., Running],
) -> None:
    """One byte every 0.1 s would take six seconds to send this head.

    The deadline is the head's, not each read's, so the proxy answers 408 at
    half a second however steadily the bytes keep coming.
    """
    running = start_proxy(request_timeout=0.5)
    request = b"GET http://localhost:3000/ HTTP/1.1\r\nHost: localhost:3000\r\n\r\n"
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", running.port), timeout=5) as sock:
        for index in range(len(request)):
            readable, _, _ = select.select([sock], [], [], 0.1)
            if readable:
                break
            sock.sendall(request[index : index + 1])
        data = _read_to_close(sock)
    assert _status(data.partition(b"\r\n\r\n")[0]) == 408
    assert time.monotonic() - started < 3
    assert running.reasons() == ["request_timeout"]


@pytest.mark.unit
@pytest.mark.parametrize("after_a_response", [False, True], ids=["fresh", "kept-alive"])
def test_a_connection_that_sends_nothing_is_closed_quietly_at_the_deadline(
    start_proxy: Callable[..., Running], after_a_response: bool
) -> None:
    """Idle is how a keep-alive connection ends, so it is not a refusal."""
    running = start_proxy(request_timeout=0.3)
    started = time.monotonic()
    if after_a_response:
        conn = _keep_alive(running.port)
        assert conn.sock is not None
        assert _read_to_close(conn.sock) == b""
        conn.close()
    else:
        with socket.create_connection(("127.0.0.1", running.port), timeout=5) as sock:
            assert _read_to_close(sock) == b""
    assert time.monotonic() - started < 4
    assert running.reasons() == []
    assert running.server.stats()["refused"] == 0


@pytest.mark.unit
def test_reading_a_head_takes_it_through_its_empty_line_and_nothing_more() -> None:
    """Bare line feeds end lines as well as CRLF, as ``http.server`` reads them."""
    client, server = socket.socketpair()
    with client, server:
        client.sendall(b"GET http://x.example/ HTTP/1.1\nHost: x\n\nNEXT REQUEST")
        head = proxy._read_head(server, 5.0)
        assert head == b"GET http://x.example/ HTTP/1.1\nHost: x\n\n"
        assert server.recv(64) == b"NEXT REQUEST"


@pytest.mark.unit
def test_an_empty_first_line_is_a_head_on_its_own() -> None:
    client, server = socket.socketpair()
    with client, server:
        client.sendall(b"\r\nGET http://x.example/ HTTP/1.1\r\n\r\n")
        assert proxy._read_head(server, 5.0) == b"\r\n"


@pytest.mark.unit
def test_nothing_is_taken_off_the_socket_once_the_deadline_has_passed() -> None:
    client, server = socket.socketpair()
    with client, server:
        client.sendall(b"GET http://x.example/ HTTP/1.1\r\n\r\n")
        assert proxy._read_head(server, 0.0) == b""
        assert server.recv(64) == b"GET http://x.example/ HTTP/1.1\r\n\r\n"


@pytest.mark.unit
def test_taking_bytes_stops_at_what_is_there_when_the_peer_has_gone() -> None:
    client, server = socket.socketpair()
    with server:
        client.sendall(b"abc")
        client.close()
        assert proxy._take(server, 10) == b"abc"


@pytest.mark.unit
def test_a_client_that_resets_part_way_through_a_head_is_recorded_as_malformed(
    start_proxy: Callable[..., Running],
) -> None:
    """The answer cannot be delivered; the record must still be made, and the
    proxy must go on serving."""
    running = start_proxy()
    sock = socket.create_connection(("127.0.0.1", running.port), timeout=5)
    sock.sendall(b"GET http://localhost:3000/ HTTP/1.1\r\nHost: x\r\n")
    time.sleep(0.2)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()
    deadline = time.monotonic() + 5
    while not running.reasons() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert running.reasons() == ["malformed_request"]
    status, _headers, body = _get(running.port, "http://localhost:3000/")
    assert status == 200 and body == INDEX_HTML


@pytest.mark.unit
def test_a_head_cut_short_by_the_client_closing_is_refused_as_malformed(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy()
    status, _head, body = _raw(
        running.port,
        "GET http://localhost:3000/ HTTP/1.1\r\nHost: x\r\n",
        half_close=True,
    )
    assert status == 400
    assert body == b"refused by the UX test egress proxy: malformed_request"
    assert running.reasons() == ["malformed_request"]


@pytest.mark.unit
def test_a_connection_beyond_the_cap_is_closed_unread_and_recorded(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy(max_connections=1)
    held = _keep_alive(running.port)
    with socket.create_connection(("127.0.0.1", running.port), timeout=5) as extra:
        assert _read_to_close(extra) == b""
    [record] = running.records()
    assert record["reason"] == "too_many_connections"
    assert (record["method"], record["target"]) == ("", "")
    held.close()
    assert running.server.connection_slots.acquire(timeout=5)
    running.server.connection_slots.release()
    status, _headers, body = _get(running.port, "http://localhost:3000/")
    assert status == 200 and body == INDEX_HTML


@pytest.mark.unit
def test_a_connection_whose_thread_cannot_start_gives_its_slot_back(
    start_proxy: Callable[..., Running],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Otherwise every such failure would shrink the cap until nothing connects."""
    running = start_proxy(max_connections=1)

    def cannot_start(*args: Any) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(socketserver.ThreadingMixIn, "process_request", cannot_start)
    with socket.create_connection(("127.0.0.1", running.port), timeout=5) as sock:
        assert _read_to_close(sock) == b""
    monkeypatch.undo()
    status, _headers, body = _get(running.port, "http://localhost:3000/")
    assert status == 200 and body == INDEX_HTML
    assert "RuntimeError while serving a request" in capsys.readouterr().err
    assert running.reasons() == []


@pytest.mark.unit
def test_the_connection_cap_is_twice_the_tunnel_cap_unless_given(
    start_proxy: Callable[..., Running],
) -> None:
    running = start_proxy(max_tunnels=1)
    held = [_keep_alive(running.port), _keep_alive(running.port)]
    with socket.create_connection(("127.0.0.1", running.port), timeout=5) as extra:
        assert _read_to_close(extra) == b""
    for conn in held:
        conn.close()
    assert running.reasons() == ["too_many_connections"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("framing", "body"),
    [
        (f"Content-Length: {len(SMUGGLED)}", SMUGGLED),
        (
            "Transfer-Encoding: chunked",
            f"{len(SMUGGLED):x}\r\n".encode() + SMUGGLED + b"\r\n0\r\n\r\n",
        ),
    ],
    ids=["content-length", "chunked"],
)
def test_a_get_that_says_a_body_follows_is_answered_and_its_connection_closed(
    start_proxy: Callable[..., Running], framing: str, body: bytes
) -> None:
    """The body is never read, so it must never be read as the next request."""
    running = start_proxy()
    head = f"GET http://localhost:3000/ HTTP/1.1\r\nHost: x\r\n{framing}\r\n\r\n"
    data = _exchange(running.port, head.encode() + body)
    assert data.count(b"HTTP/1.1 ") == 1
    response_head, _, served = data.partition(b"\r\n\r\n")
    assert _status(response_head) == 200 and served == INDEX_HTML
    assert b"Connection: close" in response_head
    stats = running.server.stats()
    assert stats["served"] == 1 and stats["refused"] == 0


@pytest.mark.unit
def test_stack_rules_name_exactly_the_review_stacks_own_hosts() -> None:
    """Each bucket on its regional host and on the global one.

    The global host is what botocore presigns for a us-east-1 client, which is how
    the backend's ``getFilePresignedUrl`` hands the document viewer and the test
    studio their file URLs; without it every one of those fetches is refused.
    """
    rules = proxy.stack_rules(_ui_env(), "idp-ux-nightly")
    assert rules.hosts == {
        "cognito-idp.us-east-1.amazonaws.com:443",
        "cognito-identity.us-east-1.amazonaws.com:443",
        "ssm.us-east-1.amazonaws.com:443",
        f"{STACK_HOST}:443",
        "abcdefghijklmnopqrstuvwxyz012345.lambda-url.us-east-1.on.aws:443",
        "idp-ux-nightly-inputbucket-def456.s3.us-east-1.amazonaws.com:443",
        "idp-ux-nightly-inputbucket-def456.s3.amazonaws.com:443",
        "idp-ux-nightly-testsetbucket-abc123.s3.us-east-1.amazonaws.com:443",
        "idp-ux-nightly-testsetbucket-abc123.s3.amazonaws.com:443",
        "idp-ux-nightly-outputbucket-ghi789.s3.us-east-1.amazonaws.com:443",
        "idp-ux-nightly-outputbucket-ghi789.s3.amazonaws.com:443",
        "idp-ux-nightly-workingbucket-jkl012.s3.us-east-1.amazonaws.com:443",
        "idp-ux-nightly-workingbucket-jkl012.s3.amazonaws.com:443",
        "cdn.jsdelivr.net:443",
    }
    assert rules.permits("idp-ux-nightly-outputbucket-ghi789.s3.amazonaws.com", 443)
    assert not rules.permits("s3.amazonaws.com", 443)
    assert not rules.permits("s3.us-east-1.amazonaws.com", 443)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("region", "commercial"),
    [
        ("us-west-2", True),
        ("eu-central-1", True),
        ("ap-southeast-5", True),
        ("mx-central-1", True),
        ("us-gov-west-1", False),
        ("us-gov-east-1", False),
        ("cn-north-1", False),
        ("us-iso-east-1", False),
        ("us-isob-east-1", False),
        ("eu-isoe-west-1", False),
    ],
)
def test_a_buckets_global_host_is_listed_only_in_the_commercial_partition(
    region: str, commercial: bool
) -> None:
    """The global host belongs to the commercial partition, so for a stack in any
    other partition it would name a bucket of the same name that anybody can own."""
    env = {"VITE_AWS_REGION": region, "VITE_INPUT_BUCKET_NAME": "idp-ux-nightly-in-1"}
    hosts = proxy.stack_rules(env, "idp-ux-nightly").hosts
    assert ("idp-ux-nightly-in-1.s3.amazonaws.com:443" in hosts) is commercial
    assert sum(host.startswith("idp-ux-nightly-in-1.") for host in hosts) == (
        2 if commercial else 1
    )


@pytest.mark.unit
def test_stack_rules_add_the_web_host_and_a_cognito_domain_when_given() -> None:
    env = {
        "VITE_AWS_REGION": "us-gov-west-1",
        "VITE_COGNITO_DOMAIN": "idp-ux.auth.us-gov-west-1.amazoncognito.com",
    }
    rules = proxy.stack_rules(
        env, "idp-ux-nightly", web_url="https://d1a2b3c4d5e6f7.cloudfront.net/"
    )
    assert rules.hosts == {
        "cognito-idp.us-gov-west-1.amazonaws.com:443",
        "cognito-identity.us-gov-west-1.amazonaws.com:443",
        "idp-ux.auth.us-gov-west-1.amazoncognito.com:443",
        "d1a2b3c4d5e6f7.cloudfront.net:443",
        "cdn.jsdelivr.net:443",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("VITE_INPUT_BUCKET_NAME", "otherstack-inputbucket-def456"),
        ("VITE_TEST_SET_BUCKET_NAME", "idp-ux-nightly.testsetbucket"),
        ("UX_TEST_BUCKETS", "idp-ux-nightly-fine-bucket,evilcorp-exfil-bucket"),
        ("VITE_API_BASE_URL", f"http://{STACK_HOST}/api"),
        (
            "VITE_API_BASE_URL",
            "https://abcde12345.execute-api.eu-west-1.amazonaws.com/",
        ),
        ("VITE_STREAM_URL", "https://evil.example.com/"),
        ("VITE_AWS_REGION", "us-east"),
        ("VITE_COGNITO_DOMAIN", "login.evil.example.com"),
        (
            "VITE_API_BASE_URL",
            "https://abcde12345.execute-api.us-east-1.amazonaws.com:99999/",
        ),
        (
            "VITE_API_BASE_URL",
            "https://abcde12345.execute-api.us-east-1.amazonaws.com:8443/",
        ),
    ],
)
def test_stack_rules_refuse_a_malformed_value_naming_the_key_never_the_value(
    key: str, bad: str
) -> None:
    env = {**_ui_env(), key: bad}
    with pytest.raises(proxy.EgressConfigError) as raised:
        proxy.stack_rules(env, "idp-ux-nightly")
    message = str(raised.value)
    assert key in message
    for part in [bad, *bad.split(",")]:
        assert part not in message


@pytest.mark.unit
@pytest.mark.parametrize(
    "stack", ["", "9starts-with-a-digit", "has_underscore", "x" * 129]
)
def test_stack_rules_refuse_a_name_that_is_not_a_stack_name(stack: str) -> None:
    with pytest.raises(proxy.EgressConfigError, match="not a CloudFormation stack"):
        proxy.stack_rules(_ui_env(), stack)


@pytest.mark.unit
@pytest.mark.parametrize(
    "web_url",
    [
        "http://d1a2b3c4d5e6f7.cloudfront.net/",
        f"https://{PUBLIC_V4}/",
        "https://0x34.0x5f.0x6e.0x1/",
        f"https://[{PUBLIC_V6}]/",
    ],
)
def test_stack_rules_refuse_a_web_url_that_is_not_https_to_a_host_name(
    web_url: str,
) -> None:
    with pytest.raises(proxy.EgressConfigError, match="web URL") as raised:
        proxy.stack_rules(_ui_env(), "idp-ux-nightly", web_url=web_url)
    assert web_url.split("/")[2] not in str(raised.value)


@pytest.mark.unit
def test_parse_ui_env_keeps_values_verbatim_and_skips_blank_and_comment_lines() -> None:
    text = (
        "# from WebUITestEnvFile\n\n"
        "VITE_A=plain\r\n"
        'VITE_B="quoted" = kept\n'
        "  # indented comment\n"
        "UX_TEST_BUCKETS=a,b\n"
    )
    assert proxy.parse_ui_env(text) == {
        "VITE_A": "plain",
        "VITE_B": '"quoted" = kept',
        "UX_TEST_BUCKETS": "a,b",
    }


@pytest.mark.unit
@pytest.mark.parametrize("line", ["lower=value", "NO_EQUALS_SIGN", " VITE_A=x"])
def test_parse_ui_env_refuses_a_bad_line_by_number_alone(line: str) -> None:
    with pytest.raises(proxy.EgressConfigError) as raised:
        proxy.parse_ui_env(f"VITE_OK=1\n{line}\n")
    assert "line 2" in str(raised.value)
    assert line.strip() not in str(raised.value)


@pytest.mark.unit
def test_rules_round_trip_through_their_json_file_sorted() -> None:
    rules = proxy.EgressRules(frozenset({"b.example.com:443", "a.example.com:443"}))
    data = rules.to_json()
    assert data == {"version": 1, "hosts": ["a.example.com:443", "b.example.com:443"]}
    assert proxy.EgressRules.from_json(json.loads(json.dumps(data))) == rules


@pytest.mark.unit
@pytest.mark.parametrize(
    "data",
    [
        {"version": 2, "hosts": []},
        {"version": 1, "hosts": "a.example.com:443"},
        {"version": 1, "hosts": ["a.example.com:80"]},
        {"version": 1, "hosts": ["Upper.Example.com:443"]},
        {"version": 1, "hosts": ["[::1]:443"]},
        {"version": 1, "hosts": [443]},
        {"version": 1, "hosts": [f"{PUBLIC_V4}:443"]},
        {"version": 1, "hosts": ["127.1:443"]},
        {"version": 1, "hosts": ["0x7f.0x1:443"]},
    ],
)
def test_a_rules_file_the_harness_could_not_have_written_is_refused(
    data: dict[str, Any],
) -> None:
    with pytest.raises(proxy.EgressConfigError):
        proxy.EgressRules.from_json(data)


@pytest.mark.unit
def test_permits_reads_a_host_case_blind_with_one_trailing_dot() -> None:
    rules = proxy.EgressRules(frozenset({"a.example.com:443"}))
    assert rules.permits("A.Example.COM.", 443)
    assert not rules.permits("a.example.com..", 443)
    assert not rules.permits("a.example.com", 80)


@pytest.mark.unit
def test_the_refusal_log_writes_bounded_lines_up_to_its_limit_and_keeps_counting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "refusals.jsonl"
    log = proxy.RefusalLog(path, limit=2)
    log.record("CONNECT", "x" * 500, "host_not_allowed")
    log.record("GET", "http://example.com/", "plain_http_not_allowed")
    log.record("GET", "http://example.com/", "plain_http_not_allowed")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 2
    assert set(records[0]) == {"t", "method", "target", "reason"}
    assert records[0]["target"] == "x" * 200
    assert time.strptime(records[0]["t"], "%Y-%m-%dT%H:%M:%SZ")
    assert log.counts == {"host_not_allowed": 1, "plain_http_not_allowed": 2}


@pytest.mark.unit
def test_the_refusal_log_keeps_no_query_or_fragment_of_a_target(
    tmp_path: Path,
) -> None:
    """A presigned URL carries its signature and session token in its query."""
    path = tmp_path / "refusals.jsonl"
    log = proxy.RefusalLog(path)
    log.record(
        "GET",
        "http://bucket.example/key.pdf?X-Amz-Signature=deadbeef&X-Amz-Security-Token=x",
        "plain_http_not_allowed",
    )
    log.record("GET", "/index.html#fragment", "path_not_served")
    targets = [json.loads(line)["target"] for line in path.read_text().splitlines()]
    assert targets == ["http://bucket.example/key.pdf", "/index.html"]


@pytest.mark.unit
def test_an_unwritable_refusal_log_stops_writing_and_keeps_counting(
    tmp_path: Path,
) -> None:
    log = proxy.RefusalLog(tmp_path / "missing-dir" / "refusals.jsonl")
    log.record("CONNECT", "evil.example:443", "host_not_allowed")
    log.record("CONNECT", "evil.example:443", "host_not_allowed")
    assert log.counts == {"host_not_allowed": 2}
    assert not (tmp_path / "missing-dir").exists()


@pytest.mark.unit
def test_a_reason_outside_the_vocabulary_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        proxy.RefusalLog(None).record("GET", "/", "because")


@pytest.mark.unit
def test_the_executable_reports_readiness_counts_a_refusal_and_writes_stats_on_sigterm(
    tmp_path: Path,
) -> None:
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"version": 1, "hosts": ["cdn.jsdelivr.net:443"]}))
    ready = tmp_path / "ready"
    stats = tmp_path / "stats.json"
    refusals = tmp_path / "refusals.jsonl"
    argv = [sys.executable, "-I", "-B", str(PROXY_SCRIPT), "--listen", "127.0.0.1:0"]
    argv += ["--rules", str(rules), "--refusal-log", str(refusals)]
    argv += ["--stats", str(stats), "--ready-file", str(ready)]
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        host, _, port = ready.read_text().strip().rpartition(":")
        assert host == "127.0.0.1"
        status, sock = _tunnel(int(port), "evil.example:443")
        sock.close()
        assert status == 403
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    written = json.loads(stats.read_text())
    assert written["refused"] == 1
    assert written["by_reason"] == {"host_not_allowed": 1}
    assert not list(tmp_path.glob(".*.partial"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "listen",
    [
        "0.0.0.0:3128",
        "10.0.0.1:3128",
        "[::1]:3128",
        "::1:3128",
        "localhost:3128",
        "127.0.0.1:99999",
        "127.0.0.1",
    ],
)
def test_the_executable_refuses_a_listen_address_that_is_not_ipv4_loopback(
    tmp_path: Path, listen: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"version": 1, "hosts": []}))
    assert proxy.main(["--listen", listen, "--rules", str(rules)]) == 2
    assert "--listen" in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rules_text", "extra", "message"),
    [
        (None, [], "--rules cannot be read"),
        ("{not json", [], "--rules is not JSON"),
        ('{"version": 1, "hosts": ["evil.example:80"]}', [], "port 443"),
        ('{"version": 1, "hosts": []}', ["--max-tunnels", "0"], "must be positive"),
        ('{"version": 1, "hosts": []}', ["--tunnel-idle", "0"], "must be positive"),
    ],
)
def test_the_executable_refuses_bad_rules_and_limits_before_listening(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    rules_text: str | None,
    extra: list[str],
    message: str,
) -> None:
    rules = tmp_path / "rules.json"
    if rules_text is not None:
        rules.write_text(rules_text)
    ready = tmp_path / "ready"
    argv = [
        "--listen",
        "127.0.0.1:0",
        "--rules",
        str(rules),
        "--ready-file",
        str(ready),
    ]
    assert proxy.main([*argv, *extra]) == 2
    assert message in capsys.readouterr().err
    assert not ready.exists()


@pytest.mark.unit
def test_a_build_with_a_readable_index_is_served_from_where_it_is(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.html").write_bytes(INDEX_HTML)
    assert proxy._site(tmp_path) == proxy.Site(tmp_path)


@pytest.mark.unit
def test_a_build_swapped_for_a_symlink_after_start_up_is_not_followed(
    tmp_path: Path,
) -> None:
    """In CI the build sits in the MR's checkout, which another uid could once
    write to; replacing the build with a link after the proxy started must not
    move the site to wherever the link points."""
    build = tmp_path / "build"
    build.mkdir()
    (build / "index.html").write_bytes(INDEX_HTML)
    site = proxy.Site(build)
    assert site.resolve("/") == Path(site.real_root) / "index.html"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "index.html").write_bytes(b"not the build")
    build.rename(tmp_path / "build-before")
    build.symlink_to(elsewhere, target_is_directory=True)
    assert site.resolve("/") is None
    assert site.resolve("/index.html") is None


@pytest.mark.unit
def test_the_executable_refuses_a_build_without_a_readable_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"version": 1, "hosts": []}))
    (tmp_path / "build").mkdir()
    argv = ["--listen", "127.0.0.1:0", "--rules", str(rules)]
    assert proxy.main([*argv, "--build-dir", str(tmp_path / "build")]) == 2
    assert "index.html" in capsys.readouterr().err
