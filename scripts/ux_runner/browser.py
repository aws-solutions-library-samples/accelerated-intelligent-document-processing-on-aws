#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The confined browser alone, for an interactive session: the Docker browser mode.

``scripts/ux_runner/run_in_docker.py browser`` runs this inside the UX runner
image. It starts exactly what the CI job confines its browser with and nothing
else: the egress proxy (serving a local build at ``http://localhost:3000``, or
tunnelling to the deployed stack's own web host), Chrome for Testing headless as
``uxbrowser`` with the hardened flags and the managed policy, both behavioural
self-tests, and optionally a persona signed in over DevTools. Then it forwards
DevTools from ``0.0.0.0:9223`` inside the container to Chrome's
``127.0.0.1:9222``, prints one JSON line saying where to connect, and waits.

The host publishes that port on its own loopback only (``-p 127.0.0.1:9222:9223``),
so a host-side chrome-devtools-mcp and an interactive ux-test or product-demo
session can drive the browser on a machine with no local Chrome. The recorder
runs inside the same container through ``docker exec``, because its Unix control
socket cannot cross Docker Desktop's VM and its render needs the image's ffmpeg;
the JSON line carries that command prefix.

The socket audit starts the moment Chrome is launched, before the self-tests and
the sign-in, and its last pass runs after Chrome has exited, so it covers every
moment Chrome could open a socket. On SIGTERM (``run_in_docker.py browser
--stop``, which is ``docker stop``) it stops the forwarder and Chrome, kills
whatever Chrome's uid still runs (``chrome.kill_strays``), writes
``browser-audit.json`` from that audit, naming each such process that had left
Chrome's process group under ``strays``, then stops the proxy; every other
process is killed by process group.

Who can reach the forwarder
---------------------------
The forwarder listens on every interface inside the container, because that is
where Docker delivers a connection to the published port, but it relays only a
connection from loopback or from the container's default gateway
(:func:`container_gateways`). docker-proxy, which carries a connection to the
published loopback port into the container, connects from that gateway address.
Another container on the same Docker network connects from its own address and is
refused, and so is a LAN peer that routes to the bridge, which Docker Engine before
28 allows on Linux (moby#45610). That a Docker setup without docker-proxy in that
path (Docker Desktop's own forwarding, a daemon with the userland proxy off,
rootless Docker) also delivers from the gateway is not established here; where one
does not, its connections are refused like any other, and each refused address is
printed once, so the log names the address to look at. Anything that can connect
from the gateway address itself, which on a Linux host is any process on the host,
can still drive the browser, as anything on the host can through the loopback
publication; the container is meant to be short-lived.

What this is NOT
----------------
It is not the CI job, and it records nothing on its own. It inherits the CI job's
confinement of the browser and adds the forwarder, which is the one way in that
the CI job does not have.

Usage (inside the image; ``run_in_docker.py browser`` builds this)::

    python3 /repo/scripts/ux_runner/browser.py --stack idp-ux-nightly \\
        --web-url https://dxxxxxxxxxxxxx.cloudfront.net/ [--sign-in Admin] \\
        --cdp-listen 0.0.0.0:9223 --out /out/browser

Environment: ``UX_TEST_UI_ENV`` (required), ``UX_TEST_PERSONAS`` (``--sign-in``
only), ``UX_BROWSER_HOST_PORT`` and ``UX_BROWSER_CONTAINER`` (for the JSON line).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import selectors
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
from collections.abc import Callable, Collection
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

_HERE = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0] or ".").resolve() == _HERE:
    sys.path.pop(0)
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

import ux_test_proxy  # noqa: E402

from ux_runner import chrome, egress, paths, processes, signin  # noqa: E402

DEFAULT_STACK = "idp-ux-nightly"
DEFAULT_CDP_LISTEN = "0.0.0.0:9223"
DEFAULT_OUT = "/out/browser"
DEFAULT_CONTAINER = "ux-browser"
RECORDER_OUT = "/out/recordings"
_SOCKET_TIMEOUT = 60.0
_CHUNK = 64 * 1024
#: The kernel's IPv4 routing table, which names each route's gateway.
_ROUTE_TABLE = Path("/proc/net/route")
#: How many distinct refused addresses are printed before the forwarder goes quiet.
_REFUSALS_PRINTED = 16


def container_gateways(route_table: Path = _ROUTE_TABLE) -> frozenset[str]:
    """The gateways of this container's IPv4 default routes, from ``route_table``.

    A row of ``/proc/net/route`` whose destination is ``00000000`` is a default
    route, and its gateway is printed in host byte order, read here as
    little-endian (x86-64 and arm64). Empty when the table cannot be read, which
    leaves the forwarder relaying loopback only.
    """
    try:
        rows = route_table.read_text(encoding="ascii", errors="replace").splitlines()
    except OSError:
        return frozenset()
    found: set[str] = set()
    for row in rows[1:]:
        fields = row.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        try:
            raw = bytes.fromhex(fields[2])
        except ValueError:
            continue
        if len(raw) == 4 and any(raw):
            found.add(str(ipaddress.IPv4Address(raw[::-1])))
    return frozenset(found)


def peer_filter(gateways: Collection[str]) -> Callable[[str], bool]:
    """Admit a peer address that is loopback or one of ``gateways``, and no other.

    An IPv4-mapped IPv6 address is read as the IPv4 address it carries, and an
    address that does not parse is refused.
    """
    admitted = frozenset(gateways)

    def admit(host: str) -> bool:
        try:
            address = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            return False
        mapped = address.ipv4_mapped if address.version == 6 else None
        if mapped is not None:
            address = mapped
        return address.is_loopback or str(address) in admitted

    return admit


class _Forwarder(socketserver.ThreadingTCPServer):
    """A TCP relay to one fixed target; one daemon thread per admitted connection."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(
        self,
        listen: tuple[str, int],
        target: tuple[str, int],
        admit: Callable[[str], bool],
    ) -> None:
        self.target = target
        self.admit = admit
        self.refused: set[str] = set()
        super().__init__(listen, _RelayHandler)

    def verify_request(self, request: Any, client_address: Any) -> bool:
        """Relay only a peer ``admit`` accepts; print each refused address once.

        Runs on the serving thread before a connection gets a thread of its own,
        and a refused connection is closed without a byte reaching the target.
        """
        host = str(client_address[0])
        if self.admit(host):
            return True
        if host not in self.refused and len(self.refused) < _REFUSALS_PRINTED:
            self.refused.add(host)
            print(f"cdp forwarder: refused a connection from {host}", flush=True)
        return False


def _relay(left: socket.socket, right: socket.socket) -> None:
    """Copy bytes both ways until either side closes."""
    left.settimeout(_SOCKET_TIMEOUT)
    right.settimeout(_SOCKET_TIMEOUT)
    with selectors.DefaultSelector() as selector:
        selector.register(left, selectors.EVENT_READ, (left, right))
        selector.register(right, selectors.EVENT_READ, (right, left))
        while True:
            for key, _events in selector.select():
                source, sink = key.data
                try:
                    chunk = source.recv(_CHUNK)
                    if not chunk:
                        return
                    sink.sendall(chunk)
                except OSError:
                    return


class _RelayHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        target = cast(_Forwarder, self.server).target
        try:
            upstream = socket.create_connection(target, timeout=10.0)
        except OSError:
            return
        with upstream:
            _relay(self.request, upstream)


def forwarder(
    listen: tuple[str, int],
    target: tuple[str, int],
    *,
    admit: Callable[[str], bool] | None = None,
) -> socketserver.ThreadingTCPServer:
    """A bound TCP relay from ``listen`` to ``target``; the caller serves it.

    ``admit`` decides, by the peer's address, which connections are relayed. By
    default that is :func:`peer_filter` over :func:`container_gateways`.
    """
    return _Forwarder(listen, target, admit or peer_filter(container_gateways()))


def wait_for_stop() -> None:
    """Block until a signal handler raises; :class:`processes.Terminated` ends it."""
    threading.Event().wait()


def _address(value: str, option: str) -> tuple[str, int]:
    host, sep, port_text = value.rpartition(":")
    if not sep or not host or not port_text.isdigit() or int(port_text) > 65535:
        raise ValueError(f"{option} must be HOST:PORT")
    return host, int(port_text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--stack", default=DEFAULT_STACK, help="review stack (default: %(default)s)"
    )
    parser.add_argument(
        "--web-url",
        help="the stack's web URL, by host name (an IP address is refused); "
        "required unless --build-dir is given",
    )
    parser.add_argument(
        "--build-dir", help="serve this built UI at http://localhost:3000 instead"
    )
    parser.add_argument(
        "--sign-in", metavar="PERSONA", help="sign this UX_TEST_PERSONAS persona in"
    )
    parser.add_argument(
        "--cdp-listen",
        default=DEFAULT_CDP_LISTEN,
        help="where the DevTools forwarder listens (default: %(default)s)",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help="where browser-audit.json goes (default: %(default)s)",
    )
    parser.add_argument(
        "--toolchain-prefix",
        help=f"the toolchain prefix (default: ${paths.PREFIX_ENV} or "
        f"{paths.DEFAULT_PREFIX})",
    )
    args = parser.parse_args(argv)
    if not args.web_url and not args.build_dir:
        parser.error("--web-url is required unless --build-dir is given")
    return args


def _ready_line(environ: dict[str, str]) -> str:
    """The one line ``run_in_docker.py`` waits for: where to connect, how to record."""
    host_port = environ.get("UX_BROWSER_HOST_PORT", "")
    port = host_port if host_port.isdigit() else str(paths.CDP_PORT)
    container = environ.get("UX_BROWSER_CONTAINER") or DEFAULT_CONTAINER
    return json.dumps(
        {
            "cdp": f"http://{paths.CDP_HOST}:{port}",
            "recorder": f"docker exec {container} python3 /repo/scripts/ux_recorder.py",
            "recorder_out": f"--out {RECORDER_OUT}",
        }
    )


def _persona(name: str, environ: dict[str, str]) -> signin.Persona:
    encoded = environ.get("UX_TEST_PERSONAS", "")
    if not encoded:
        raise signin.SignInError("--sign-in needs UX_TEST_PERSONAS")
    personas = signin.personas_from_env(encoded)
    if name not in personas:
        raise signin.SignInError("--sign-in names a persona UX_TEST_PERSONAS lacks")
    return personas[name]


def _admission_line(gateways: Collection[str]) -> str:
    """The line that says which peers the forwarder will relay."""
    if not gateways:
        return "cdp forwarder: relays connections from loopback only (no gateway found)"
    return (
        "cdp forwarder: relays connections from loopback and "
        f"{', '.join(sorted(gateways))} (the container's gateway) only"
    )


def _stop_browser(
    browser: processes.ManagedProcess, uid: int | None
) -> tuple[str, ...]:
    """Kill Chrome's process group, then every process its uid still runs.

    Returns the processes that had left the group, which a group kill never
    reaches and which would otherwise outlive the audit.
    """
    group = browser.pid
    browser.terminate()
    return chrome.kill_strays(uid, group)


def main(argv: list[str] | None = None) -> int:
    """Start the confined browser, print where to drive it, and wait for SIGTERM.

    Exit 0 after a clean stop, with ``browser-audit.json`` written; exit 1, with a
    ``FAILED:`` line, when the setup does not complete. The socket watch starts as
    soon as Chrome is launched, and its verdict is taken only after Chrome has
    been terminated.
    """
    args = parse_args(argv)
    environ = dict(os.environ)
    restore = processes.install_sigterm_handler()
    run_dir = Path(tempfile.mkdtemp(prefix="ux-browser-"))
    proxy: egress.ProxyRun | None = None
    browser: processes.ManagedProcess | None = None
    relay: socketserver.ThreadingTCPServer | None = None
    watch: chrome.SocketWatch | None = None
    uid: int | None = None
    try:
        try:
            listen = _address(args.cdp_listen, "--cdp-listen")
            toolchain = paths.toolchain(args.toolchain_prefix)
            if "chrome" in toolchain.missing():
                raise chrome.ChromeError("Chrome for Testing is not installed")
            if not chrome.policy_installed(chrome.load_policy()):
                raise chrome.ChromeError("the managed policy is not installed")
            encoded = environ.get("UX_TEST_UI_ENV", "")
            if not encoded:
                raise ux_test_proxy.EgressConfigError("UX_TEST_UI_ENV is not set")
            ui_env = egress.ui_env_from_b64(encoded)
            persona = _persona(args.sign_in, environ) if args.sign_in else None
            rules = ux_test_proxy.stack_rules(ui_env, args.stack, web_url=args.web_url)
            site = egress.prepare_site(Path(args.build_dir)) if args.build_dir else None
            base_url = paths.UI_ORIGIN + "/" if site is not None else args.web_url
            os.chmod(run_dir, 0o711)  # nosec B103 - traverse-only, no listing
            proxy = egress.launch_proxy(
                run_dir=run_dir, rules=rules, build_dir=site, user=paths.PROXY_USER
            )
            uid = paths.BROWSER_UID if processes.can_switch_user() else None
            browser = chrome.launch(
                toolchain,
                run_dir=run_dir,
                user=paths.BROWSER_USER,
                log=run_dir / "chrome.log",
            )
            watch = chrome.SocketWatch(uid).start()
            chrome.wait_for_cdp()
            target = chrome.first_page_target()
            session = chrome.open_session(target["webSocketDebuggerUrl"])
            try:
                chrome.verify_policy(session)
                chrome.verify_proxy_path(session, proxy.refusal_log, owner=proxy.owner)
                if persona is not None:
                    signin.sign_in(session, persona, base_url=base_url)
                else:
                    session.call("Page.navigate", {"url": base_url})
            finally:
                session.close()
            gateways = container_gateways()
            server = forwarder(
                listen, (paths.CDP_HOST, paths.CDP_PORT), admit=peer_filter(gateways)
            )
            threading.Thread(
                target=server.serve_forever, name="cdp-forwarder", daemon=True
            ).start()
            relay = server
        except processes.Terminated:
            print("FAILED: cancelled during setup", flush=True)
            return 1
        except Exception as exc:
            print(f"FAILED: {type(exc).__name__}: {exc}", flush=True)
            return 1
        print(_admission_line(gateways), flush=True)
        print(_ready_line(environ), flush=True)
        try:
            wait_for_stop()
        except processes.Terminated:
            pass
        relay.shutdown()
        relay.server_close()
        relay = None
        strays = _stop_browser(browser, uid)
        browser = None
        audit = watch.stop()
        watch = None
        out = Path(args.out)
        record = {**asdict(audit), "strays": list(strays)}
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / "browser-audit.json").write_text(
                json.dumps(record, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            print(f"FAILED: the socket audit could not be written: {exc}", flush=True)
            return 1
        killed = f", {len(strays)} stray process(es) killed" if strays else ""
        print(f"socket audit: {audit.state}{killed}", flush=True)
        return 0
    finally:
        if relay is not None:
            relay.shutdown()
            relay.server_close()
        if browser is not None:
            _stop_browser(browser, uid)
        if watch is not None:
            watch.stop()
        if proxy is not None:
            proxy.process.terminate()
        shutil.rmtree(run_dir, ignore_errors=True)
        restore()


if __name__ == "__main__":
    sys.exit(main())
