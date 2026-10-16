# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Chrome for Testing for the UX test: flags, policy, launch, two self-tests, audit.

The browser renders the merge request's own UI code, and its DevTools port is
unauthenticated, so it is confined by four things that each hold without the
others, and checked by two behavioural self-tests before anything is recorded.

How it is confined
------------------
* **Flags** (:func:`chrome_flags`). Every request goes to the egress proxy on
  ``127.0.0.1:3128``, loopback included (``<-loopback>``); every host name Chrome
  would resolve itself maps to ``~NOTFOUND``, so a connection that bypasses the
  proxy cannot even find an address; QUIC is off, WebRTC may not use UDP outside
  the proxy, and the background services that phone home (component updates,
  safe browsing, sync, metrics, translation, the network-time query) are off.
  DevTools listens on loopback only, with no ``--remote-allow-origins``, so a web
  page cannot open a DevTools WebSocket to it.
* **The managed policy** (``chrome_policy.json``), installed by ``install.sh``
  where Chrome reads it and checked here before launch (:func:`policy_installed`).
  It repeats the proxy settings, which a policy holds against anything a page or
  a flag could change, and adds what flags cannot say: ``file://``, ``chrome://``
  and ``devtools://`` blocked, and downloads, extensions, password saving,
  DNS-over-HTTPS, browser sign-in, component updates and the spell-check
  dictionary download off. ``chrome://`` matters because chrome-devtools-mcp's own
  navigation allows ``chrome://inspect``, whose network-target discovery opens
  sockets that do not use the proxy.
* **A uid of its own.** Chrome runs as ``uxbrowser``, the proxy as ``uxproxy``.
  ``--no-sandbox`` is required, because the default Docker executor offers neither
  a setuid sandbox nor user namespaces, so the uid, the proxy, the policy and the
  audit are the containment. When Chrome's process group has been killed,
  :func:`kill_strays` kills whatever that uid still runs, so no browser process
  that left the group outlives the audit, and names each one that had left it.
* **The socket audit** (:func:`audit_sockets`, :class:`SocketWatch`). Reading
  ``/proc/<pid>/fd`` of another uid needs ``CAP_SYS_PTRACE``, which Docker drops,
  but the ``/proc/net`` tables are world-readable and carry each socket's owner
  uid, and only Chrome runs as ``BROWSER_UID``. Every socket that uid owns, in the
  TCP, UDP, UDP-Lite, ping and raw tables, must be connected to loopback (the
  proxy, DevTools) or be unconnected and bound to loopback; anything else is a
  violation, and the orchestrator posts nothing when there is one.

The two self-tests
------------------
:func:`verify_policy` navigates to ``file:///etc/hostname``, ``chrome://inspect/``
and ``devtools://devtools/bundled/inspector.html``, each once the page before it
has finished loading, and requires Chrome to answer
``ERR_BLOCKED_BY_ADMINISTRATOR`` for each, which only an enforced policy produces.
chrome-devtools-mcp's ``navigate_page`` refuses most ``chrome:`` URLs of its own
accord, but allows ``chrome://inspect`` and passes ``devtools:`` URLs through, so
for those the policy is the only block. :func:`verify_proxy_path`
navigates to ``http://ux-proxy-selftest.invalid/`` and requires the proxy's refusal
log to show it, which only happens if Chrome's traffic really goes through the
proxy. Both fail closed: a policy path or a proxy flag that this Chrome build does
not honour stops the run instead of leaving a browser that merely looks confined.

What Chrome asks for by itself
------------------------------
Whatever page is loaded, Chrome for Testing calls a few Google hosts on its own
(account sign-in, component updates and their download redirector, the GCM
check-in, the network-time query). The policy and flags switch off what they can
of that, and the proxy refuses whatever is left, as it refuses anything unlisted. Those
refusals are Chrome's, not the page's: :func:`refusal_origin` labels each record
of the refusal log ``selftest``, ``browser`` or ``page``, so the note can report
only what the UI under test asked for, while ``refusals.jsonl`` keeps every record.

The viewport
------------
New-headless Chrome counts its own window frame inside ``--window-size``, so a
1600x1000 window gives the page less than 1000 pixels of height. The window is
therefore :data:`HEADLESS_FRAME_HEIGHT` taller than :data:`paths.VIEWPORT`, and
the screen as tall as the window, so that the page's viewport is exactly the one
the recording, the click bounds and the prompt assume. :func:`verify_viewport`
measures it where the page's script cannot redefine it and fails closed when it
differs, naming the frame height that would fit; :func:`measure_viewport` returns
the measurement for the run's summary.

Bounds worth knowing
--------------------
* The audit polls (every 5 s by default), so a connection opened and closed
  between two polls is not seen, and a socket in ``TIME_WAIT`` no longer carries
  its owner uid. It is an alarm, not the enforcement; the proxy is the enforcement.
  Start the watch as soon as Chrome is launched and stop it once Chrome has
  exited, so that its passes cover every moment Chrome can open a socket.
* A pass that finds no socket of the browser uid at all is ``unavailable``, not
  ``clean``: a running Chrome always holds its DevTools listening socket, so seeing
  nothing means the audit is not watching Chrome. A merged verdict is
  ``unavailable`` unless some pass saw a socket.
* It needs the uid switch, so it needs root. When the harness cannot switch users
  the audit is ``unavailable``, and the orchestrator treats that as not clean.
* Chrome for Testing 155 crashes (``SIGTRAP``) when it is sent to a
  policy-blocked ``chrome://`` URL while the document the previous navigation
  committed is still being set up: measured in the image, every one of three
  runs that navigated there straight after a page loaded, and none that waited
  for the page to finish. :func:`verify_policy` waits. A navigation the model asks
  for at such a moment would crash the browser in the same way, which ends the
  session like any other Chrome failure; the blocked page is never shown.
* ``/proc/net`` holds the addresses in host byte order, read here as little-endian,
  which is what x86-64 and arm64 are.
* The proxy confines Chrome as configured, not a Chrome that has been subverted.
  Under ``--no-sandbox`` a renderer the MR's page compromises runs as
  ``uxbrowser`` and can open sockets of its own to anything the job container can
  reach; the proxy never sees that traffic, and the audit only samples it.
* The Chrome for Testing pin (``install.sh``'s ``CFT_VERSION``) gets no security
  fix until somebody moves it, and nothing audits it the way dep-audit audits the
  npm and Python pins. ``scripts/sdlc/docs/CI_TEST_COVERAGE.md`` says when to.
* DevTools on ``127.0.0.1:9222`` and the proxy on ``127.0.0.1:3128`` are loopback
  only on the assumption that each job has a network namespace of its own, which
  Docker's default bridge network gives it. On a runner that runs jobs with host
  networking, every job on the host shares that loopback.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import json
import os
import shutil
import signal
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import ux_recorder_cdp

from . import egress, processes
from .paths import (
    CDP_HOST,
    CDP_PORT,
    POLICY_FILE,
    POLICY_INSTALL_DIRS,
    POLICY_INSTALL_NAME,
    PROXY_HOST,
    PROXY_PORT,
    VIEWPORT,
    Toolchain,
)
from .processes import ManagedProcess

#: The host :func:`verify_proxy_path` sends Chrome to. ``.invalid`` never
#: resolves, so the only way the proxy can see it is as Chrome's proxied request.
SELFTEST_HOST = "ux-proxy-selftest.invalid"

#: How much taller new-headless Chrome for Testing's window is than the page's
#: viewport, the frame it draws above the page. Measured in the image: a
#: ``--window-size`` of 1600x1000 gave a 1600x857 page (Chrome for Testing
#: 155.0.8059.84, linux-arm64). :func:`verify_viewport` says when it stops fitting.
HEADLESS_FRAME_HEIGHT = 143

#: Hosts Chrome for Testing called on its own in a live run of the image, each
#: refused by the proxy: account sign-in, the GCM check-in, the network-time query,
#: and component updates with the redirector their downloads (the spell-check
#: dictionary's among them) come from. :func:`refusal_origin` labels a refusal of
#: any of them as the browser's own.
BROWSER_BACKGROUND_HOSTS: frozenset[str] = frozenset(
    {
        "accounts.google.com",
        "android.clients.google.com",
        "clients2.google.com",
        "redirector.gvt1.com",
        "update.googleapis.com",
    }
)

#: What :func:`verify_policy` navigates to, one URL per blocked scheme. Each must be
#: answered ``ERR_BLOCKED_BY_ADMINISTRATOR``, the answer the live image gave each of
#: them under the policy; any other answer fails the check, which is all a probe
#: needs.
_POLICY_PROBE_URLS = (
    "file:///etc/hostname",
    "chrome://inspect/",
    "devtools://devtools/bundled/inspector.html",
)
_BLOCKED_BY_POLICY = "ERR_BLOCKED_BY_ADMINISTRATOR"
_POLL_SECONDS = 0.25
_HTTP_TIMEOUT = 2.0
_ERROR_TEXT_LIMIT = 200
#: The isolated world :func:`measure_viewport` reads the viewport in, the
#: harness's own, apart from the recorder's.
_VIEWPORT_WORLD = "ux-harness"
_VIEWPORT_JS = "[window.innerWidth, window.innerHeight, window.devicePixelRatio]"
#: The ``/proc/net`` tables the audit reads, IPv4 and IPv6 each: TCP, UDP and
#: UDP-Lite, then ping and raw. Docker's default ``ping_group_range`` lets any user
#: open a ping socket, and a datagram or raw socket can send to any address with
#: no connection to show for it. A table this kernel does not provide is passed
#: over.
_PROC_NET_TABLES = (
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
#: Index of the owner uid in a ``/proc/net`` table row.
_UID_FIELD = 7
#: Index of the local address in a ``/proc/net`` table row.
_LOCAL_FIELD = 1
#: Index of the remote address in a ``/proc/net`` table row.
_REMOTE_FIELD = 2
#: The process states that run nothing: zombie and dead.
_ENDED_STATES = ("Z", "X")
#: How many times :func:`kill_strays` looks again after killing what it found.
_STRAY_SWEEPS = 5
_STRAY_PAUSE_SECONDS = 0.1
#: The longest process name kept; the kernel's own limit is 15 characters.
_NAME_LIMIT = 32


class ChromeError(Exception):
    """Chrome is not running confined, or cannot be driven; the text says which."""


class CdpLike(Protocol):
    """The one method of ``ux_recorder_cdp.CdpSession`` this module needs."""

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]: ...


def chrome_flags(
    *,
    profile_dir: Path,
    proxy_host: str = PROXY_HOST,
    proxy_port: int = PROXY_PORT,
    cdp_port: int = CDP_PORT,
    window: tuple[int, int] = VIEWPORT,
    headless: bool = True,
) -> list[str]:
    """The hardened command line after the binary, ending with ``about:blank``.

    The order is fixed and pinned by a test. ``window`` is the page's viewport:
    the window and the screen are :data:`HEADLESS_FRAME_HEIGHT` taller, because
    the window holds Chrome's own frame and Chrome shrinks a new window to fit its
    screen. ``headless=False`` drops ``--headless=new`` and nothing else; that is
    for development only, since a headed Chrome needs a display the CI image does
    not have.
    """
    width, height = window
    outer = height + HEADLESS_FRAME_HEIGHT
    flags = [
        "--headless=new",
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--proxy-server=http://{proxy_host}:{proxy_port}",
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
        "--disable-crashpad-for-testing",
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
        f"--window-size={width},{outer}",
        f"--screen-info={{{width}x{outer}}}",
        "--force-device-scale-factor=1",
        "--no-sandbox",
        "about:blank",
    ]
    return flags if headless else flags[1:]


def load_policy(path: Path = POLICY_FILE) -> dict[str, Any]:
    """The managed policy as committed: the single source of what is installed."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ChromeError(f"{path.name} is not a JSON object")
    return data


def policy_installed(
    expected: Mapping[str, Any],
    dirs: Sequence[Path] = POLICY_INSTALL_DIRS,
    name: str = POLICY_INSTALL_NAME,
) -> bool:
    """Whether some managed-policy directory holds an exact copy of ``expected``.

    Equality, not presence: a copy that differs in any key is an install from
    another revision, and the self-tests only prove the two policies that have a
    behavioural test. Which of the two directories this Chrome for Testing build
    reads has not been measured, so either counts here, and :func:`verify_policy`
    is what shows that one is actually read.
    """
    for directory in dirs:
        try:
            installed = json.loads((directory / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(installed, dict) and installed == dict(expected):
            return True
    return False


def browser_env(home: Path) -> dict[str, str]:
    """Chrome's whole environment: no credential, no token, no proxy variable."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }


def _private_dir(path: Path, user: str | None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    if user is not None and processes.can_switch_user():
        shutil.chown(path, user=user, group=user)
    return path


def launch(
    toolchain: Toolchain,
    *,
    run_dir: Path,
    user: str | None,
    log: Path,
    start: Callable[..., ManagedProcess] = processes.start,
) -> ManagedProcess:
    """Start the pinned Chrome as ``user``, its own process group, logging to ``log``.

    The profile and ``HOME`` are fresh ``0o700`` directories under ``run_dir``
    owned by ``user``, so nothing from any other run or any other user's profile
    is loaded, and their parent is only traversable (``0o711``).
    """
    base = run_dir / "chrome"
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o711)  # nosec B103 - traverse-only, no listing
    profile = _private_dir(base / "profile", user)
    home = _private_dir(base / "home", user)
    with log.open("ab") as handle:
        return start(
            [str(toolchain.chrome), *chrome_flags(profile_dir=profile)],
            name="chrome",
            env=browser_env(home),
            user=user,
            stdout=handle,
            stderr=handle,
        )


def _direct_opener() -> urllib.request.OpenerDirector:
    """An opener that ignores ``HTTP_PROXY`` and friends: DevTools is loopback."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_json(opener: urllib.request.OpenerDirector, url: str) -> Any:
    with opener.open(url, timeout=_HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_cdp(
    port: int = CDP_PORT,
    *,
    timeout: float = 30.0,
    opener: urllib.request.OpenerDirector | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll ``/json/version`` until DevTools answers with a JSON object.

    Requests never go through an environment proxy, which would otherwise be
    asked to reach ``127.0.0.1`` on the harness's behalf.
    """
    direct = opener or _direct_opener()
    url = f"http://{CDP_HOST}:{port}/json/version"
    deadline = clock() + timeout
    while True:
        try:
            answer = _fetch_json(direct, url)
        except (OSError, ValueError, http.client.HTTPException):
            answer = None
        if isinstance(answer, dict):
            return answer
        if clock() >= deadline:
            raise ChromeError(
                f"Chrome's DevTools did not answer on port {port} within {timeout:g} s"
            )
        sleep(_POLL_SECONDS)


def first_page_target(
    port: int = CDP_PORT, *, opener: urllib.request.OpenerDirector | None = None
) -> dict[str, Any]:
    """The browser's one page target, which every later step drives and records.

    Exactly one is required: a fresh Chrome opens ``about:blank`` and nothing
    else, so two pages mean something opened one, and the harness would not know
    which of them the recording and the model are looking at.
    """
    direct = opener or _direct_opener()
    try:
        listed = _fetch_json(direct, f"http://{CDP_HOST}:{port}/json/list")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ChromeError(
            f"Chrome's target list could not be read: {type(exc).__name__}"
        ) from None
    if not isinstance(listed, list):
        raise ChromeError("Chrome's target list is not a JSON list")
    pages = ux_recorder_cdp.page_targets([t for t in listed if isinstance(t, dict)])
    if len(pages) != 1:
        raise ChromeError(f"expected exactly one page target, found {len(pages)}")
    target = pages[0]
    url = target.get("webSocketDebuggerUrl")
    if not isinstance(target.get("id"), str) or not (
        isinstance(url, str) and url.startswith("ws://")
    ):
        raise ChromeError("the page target has no id or DevTools WebSocket URL")
    return target


def open_session(ws_url: str) -> Any:
    """A started ``ux_recorder_cdp.CdpSession`` on the page's WebSocket.

    The client holds at most ``ux_recorder_cdp.MESSAGE_MAX_BYTES`` of any one
    message, so the page's console output cannot grow the harness's memory.
    """
    client = ux_recorder_cdp.WebSocketClient.connect(ws_url)
    session = ux_recorder_cdp.CdpSession(client)
    session.start_reader()
    return session


def _bounded(text: str, limit: int) -> str:
    flat = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return flat[:limit]


def evaluate(session: CdpLike, expression: str, *, timeout: float = 10.0) -> Any:
    """Run ``expression`` in the page and return its value, by value.

    A thrown exception raises :class:`ChromeError` carrying at most 200 printable
    characters of what the page reported, because that text is the page's own and
    the error ends up in a log.
    """
    reply = session.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        timeout=timeout,
    )
    details = reply.get("exceptionDetails")
    if details:
        thrown = details.get("exception") or {}
        text = " ".join(
            str(part)
            for part in (details.get("text"), thrown.get("description"))
            if part
        )
        raise ChromeError(
            "the page script failed: " + _bounded(text, _ERROR_TEXT_LIMIT)
        )
    return (reply.get("result") or {}).get("value")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def measure_viewport(session: CdpLike, *, timeout: float = 10.0) -> dict[str, Any]:
    """The page's viewport as ``{"w", "h", "dpr"}``, read where the page cannot lie.

    It is read in an isolated world of the harness's own, where ``innerWidth``,
    ``innerHeight`` and ``devicePixelRatio`` are the browser's values whatever the
    page's script redefines them as, so it can be measured after the MR's code has
    loaded. Anything but three numbers raises :class:`ChromeError`.
    """
    try:
        context = ux_recorder_cdp.isolated_context(
            session, _VIEWPORT_WORLD, timeout=timeout
        )
        reply = session.call(
            "Runtime.evaluate",
            {"expression": _VIEWPORT_JS, "contextId": context, "returnByValue": True},
            timeout=timeout,
        )
    except (ux_recorder_cdp.CdpError, OSError) as exc:
        raise ChromeError(
            "the page's viewport could not be read: "
            + _bounded(str(exc), _ERROR_TEXT_LIMIT)
        ) from None
    value = (reply.get("result") or {}).get("value")
    if not (
        isinstance(value, list) and len(value) == 3 and all(map(_is_number, value))
    ):
        raise ChromeError("the page's viewport could not be read: not three numbers")
    width, height, ratio = value
    return {"w": width, "h": height, "dpr": ratio}


def verify_viewport(
    session: CdpLike, expected: tuple[int, int] = VIEWPORT
) -> dict[str, Any]:
    """Require the page's viewport to be ``expected`` at a device pixel ratio of 1.

    Returns the measurement, for the run's summary. A different height means the
    window's frame is no longer :data:`HEADLESS_FRAME_HEIGHT` tall, after a Chrome
    for Testing update or with an infobar Chrome did not show before, and the
    error names the frame height that would fit this Chrome.
    """
    measured = measure_viewport(session)
    width, height = expected
    if (measured["w"], measured["h"], measured["dpr"]) == (width, height, 1):
        return measured
    fits = HEADLESS_FRAME_HEIGHT + height - measured["h"]
    raise ChromeError(
        f"the page's viewport is {measured['w']}x{measured['h']} at "
        f"{measured['dpr']}x, not {width}x{height} at 1x; for this Chrome "
        f"HEADLESS_FRAME_HEIGHT would be {fits}, not {HEADLESS_FRAME_HEIGHT}"
    )


def _wait_until_loaded(
    session: CdpLike,
    *,
    timeout: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Return once the page's current document has finished loading.

    Raises :class:`ChromeError` when it has not within ``timeout`` seconds.
    """
    deadline = clock() + timeout
    while evaluate(session, "document.readyState") != "complete":
        if clock() >= deadline:
            raise ChromeError(
                f"a policy probe's page did not finish loading within {timeout:g} s"
            )
        sleep(_POLL_SECONDS)


def verify_policy(
    session: CdpLike,
    *,
    settle_timeout: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Prove the managed policy is enforced: ``file://``, ``chrome://`` and
    ``devtools://`` blocked.

    Each probe must answer ``ERR_BLOCKED_BY_ADMINISTRATOR``. The message names the
    scheme that was not blocked.

    Each probe starts only once the page the one before it left has finished
    loading, and the check returns only once the last one has. Chrome for Testing
    155 crashes (``SIGTRAP``, measured in the image) when it is sent to a
    policy-blocked ``chrome://`` URL while the document the previous navigation
    committed is still being set up, which two navigations sent back to back
    reliably are. A DevTools connection lost during the check, which is how such
    a crash shows here, is reported as a :class:`ChromeError` naming that.
    """
    try:
        for url in _POLICY_PROBE_URLS:
            _wait_until_loaded(
                session, timeout=settle_timeout, sleep=sleep, clock=clock
            )
            reply = session.call("Page.navigate", {"url": url})
            if _BLOCKED_BY_POLICY not in str(reply.get("errorText") or ""):
                scheme = url.split(":", 1)[0]
                raise ChromeError(
                    f"the managed policy is not in force: {scheme}:// was not blocked"
                )
        _wait_until_loaded(session, timeout=settle_timeout, sleep=sleep, clock=clock)
    except ux_recorder_cdp.CdpError as exc:
        raise ChromeError(
            "the managed policy could not be checked, Chrome's DevTools stopped "
            "answering: " + _bounded(str(exc), _ERROR_TEXT_LIMIT)
        ) from None


def _selftest_refused(refusal_log: Path, owner: int) -> bool:
    return any(
        SELFTEST_HOST in str(record.get("target"))
        for record in egress.read_refusals(refusal_log, owner=owner)
    )


def verify_proxy_path(
    session: CdpLike,
    refusal_log: Path,
    *,
    owner: int,
    timeout: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Prove the browser's traffic reaches the proxy: its refusal must be logged.

    The navigation's own outcome is ignored, since the proxy answers it with a
    refusal page; what counts is that the proxy saw the request. The log is the
    proxy's file, so it is read back as one of ``owner``'s
    (``egress.read_refusals``), and anything else in its place raises
    ``egress.ProxyError`` rather than reading as no refusal yet.
    """
    with contextlib.suppress(ux_recorder_cdp.CdpError, OSError):
        session.call("Page.navigate", {"url": f"http://{SELFTEST_HOST}/"})
    deadline = clock() + timeout
    while not _selftest_refused(refusal_log, owner):
        if clock() >= deadline:
            raise ChromeError("the browser's traffic is not reaching the egress proxy")
        sleep(_POLL_SECONDS)


def _target_host(target: str) -> str | None:
    """The host a refused target names: an absolute URL, or CONNECT's host:port."""
    if "://" in target:
        try:
            host = urllib.parse.urlsplit(target).hostname
        except ValueError:
            return None
    else:
        host = (target.rpartition(":")[0] if ":" in target else target).strip("[]")
    return host.lower().rstrip(".") if host else None


def refusal_origin(record: Mapping[str, Any]) -> str:
    """Whose request one refusal-log record is: ``selftest``, ``browser`` or ``page``.

    ``selftest`` is :func:`verify_proxy_path`'s probe of :data:`SELFTEST_HOST`, and
    ``browser`` a request to one of :data:`BROWSER_BACKGROUND_HOSTS`, which Chrome
    makes by itself. Everything else is ``page``: what the UI under test asked for
    or caused, the only kind a reader of the note can act on. The host is compared
    exactly, after lowercasing and dropping a trailing dot, for a CONNECT target
    (``host:port``) and an absolute plain-HTTP URL alike, so a page cannot pass its
    own request off as Chrome's with a host that merely ends like one of those. A
    page that asks for one of those hosts itself, Google sign-in for instance, is
    labelled ``browser`` all the same, so whatever reports only ``page`` refusals
    has to leave ``refusals.jsonl`` holding every record.
    """
    target = record.get("target")
    host = _target_host(target) if isinstance(target, str) else None
    if host == SELFTEST_HOST:
        return "selftest"
    if host in BROWSER_BACKGROUND_HOSTS:
        return "browser"
    return "page"


def decode_proc_address(hex_addr: str) -> tuple[str, int]:
    """``ADDRESS:PORT`` as ``/proc/net`` prints it, as an address and a port.

    An IPv4 address is 8 hex digits, one little-endian 32-bit word; an IPv6
    address is 32 hex digits, four of them. The port is plain hex.
    """
    address_hex, sep, port_hex = hex_addr.partition(":")
    if not sep:
        raise ValueError("not an ADDRESS:PORT pair")
    raw = bytes.fromhex(address_hex)
    if len(raw) not in (4, 16):
        raise ValueError("not a /proc/net address")
    packed = b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4))
    return str(ipaddress.ip_address(packed)), int(port_hex, 16)


def _loopback(address: str) -> bool:
    """Loopback, an IPv4-mapped loopback address included."""
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_loopback


def _endpoint(address: str, port: int) -> str:
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def _violation(
    table: str, local: tuple[str, int], remote: tuple[str, int]
) -> str | None:
    """Why one socket of the browser uid is a violation, or None when it is not.

    Connected to loopback is fine, and so is unconnected (an unspecified remote on
    port 0) and bound to loopback. A connected socket to anything else is reported
    by its remote end. An unconnected socket bound beyond loopback is reported by
    its local end: a datagram socket in that state can ``sendto`` any address and
    never shows a remote one, and a listening one accepts from the network.
    """
    address, port = remote
    if _loopback(address):
        return None
    if not (ipaddress.ip_address(address).is_unspecified and port == 0):
        return f"{table} {_endpoint(address, port)}"
    if _loopback(local[0]):
        return None
    return f"{table} {_endpoint(*local)} (bound, unconnected)"


@dataclass(frozen=True)
class SocketAudit:
    """The browser uid's sockets: ``state`` is clean, violations or unavailable."""

    state: str
    violations: tuple[str, ...]
    sockets: int


def audit_sockets(uid: int | None, proc_net: Path = Path("/proc/net")) -> SocketAudit:
    """Every socket ``uid`` owns, by its two endpoints, from the ``/proc/net`` tables.

    ``unavailable`` when there is no uid to audit (the harness could not switch
    users), no ``proc_net/tcp`` to read, or no socket of ``uid`` in any table: a
    running Chrome always holds its DevTools listening socket, so a pass that sees
    none is not watching Chrome. Otherwise each row owned by ``uid`` is judged by
    :func:`_violation`, and its finding is reported as ``<table> <address>:<port>``.
    """
    if uid is None or not (proc_net / "tcp").is_file():
        return SocketAudit("unavailable", (), 0)
    found: set[str] = set()
    examined = 0
    for table in _PROC_NET_TABLES:
        try:
            rows = (proc_net / table).read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        for row in rows.splitlines()[1:]:
            fields = row.split()
            if len(fields) <= _UID_FIELD:
                continue
            try:
                owner = int(fields[_UID_FIELD])
                local = decode_proc_address(fields[_LOCAL_FIELD])
                remote = decode_proc_address(fields[_REMOTE_FIELD])
            except ValueError:
                continue
            if owner != uid:
                continue
            examined += 1
            violation = _violation(table, local, remote)
            if violation is not None:
                found.add(violation)
    if not examined:
        return SocketAudit("unavailable", (), 0)
    state = "violations" if found else "clean"
    return SocketAudit(state, tuple(sorted(found)), examined)


def _merge(results: Sequence[SocketAudit]) -> SocketAudit:
    """One verdict over every pass: any violation wins, then nothing seen is
    unavailable, whether every pass was unavailable or none saw a socket."""
    violations = tuple(sorted({v for result in results for v in result.violations}))
    sockets = max((result.sockets for result in results), default=0)
    if any(result.state == "violations" for result in results):
        state = "violations"
    elif sockets == 0 or all(result.state == "unavailable" for result in results):
        state = "unavailable"
    else:
        state = "clean"
    return SocketAudit(state, violations, sockets)


class SocketWatch:
    """:func:`audit_sockets` every ``interval`` seconds on a thread, merged at stop.

    One pass at the end would only see the sockets open at that instant, so the
    watch keeps auditing while Chrome runs and :meth:`stop` reports the union.
    Start it right after Chrome is launched and stop it after Chrome has exited:
    the final pass then finds no socket, which the merge leaves out of account as
    long as an earlier pass saw one.
    """

    def __init__(
        self,
        uid: int | None,
        *,
        interval: float = 5.0,
        audit: Callable[[int | None], SocketAudit] = audit_sockets,
    ) -> None:
        self.uid = uid
        self.interval = interval
        self._audit = audit
        self._results: list[SocketAudit] = []
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._verdict: SocketAudit | None = None

    def start(self) -> SocketWatch:
        self._thread = threading.Thread(
            target=self._run, name="socket-audit", daemon=True
        )
        self._thread.start()
        return self

    def _run(self) -> None:
        while True:
            self._results.append(self._audit(self.uid))
            if self._stopping.wait(self.interval):
                return

    def stop(self) -> SocketAudit:
        """Stop the thread, audit once more and merge every pass, the first time.

        Every later call returns that same verdict without auditing again, so the
        normal path and a cleanup path can both stop the watch.
        """
        if self._verdict is None:
            self._stopping.set()
            if self._thread is not None:
                self._thread.join()
            self._verdict = _merge([*self._results, self._audit(self.uid)])
        return self._verdict


@dataclass(frozen=True)
class _Process:
    """One live process: its pid, its name as the kernel shows it, its group."""

    pid: int
    name: str
    group: int


def _process_of(entry: Path, uid: int) -> _Process | None:
    """The process ``entry`` (a ``/proc/<pid>``) is, when ``uid`` runs it and it lives.

    The uid is read from ``status``, real, effective and saved alike, rather than
    from the directory's owner, which is root for a process that made itself
    undumpable. A zombie or a dead process runs nothing and is left out, and so is
    an entry that vanishes while it is read.
    """
    try:
        stat_text = (entry / "stat").read_text(encoding="ascii", errors="replace")
        status = (entry / "status").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    close = stat_text.rfind(")")
    fields = stat_text[close + 1 :].split() if close >= 0 else []
    if len(fields) < 3 or fields[0] in _ENDED_STATES:
        return None
    owners = next(
        (line.split()[1:4] for line in status.splitlines() if line.startswith("Uid:")),
        [],
    )
    if str(uid) not in owners:
        return None
    try:
        group = int(fields[2])
        pid = int(entry.name)
    except ValueError:
        return None
    name = _bounded(stat_text[stat_text.find("(") + 1 : close], _NAME_LIMIT)
    return _Process(pid, name, group)


def _pid_entries(proc: Path) -> list[Path]:
    try:
        return [e for e in proc.iterdir() if e.name.isascii() and e.name.isdigit()]
    except OSError:
        return []


def kill_strays(
    uid: int | None,
    group: int | None,
    *,
    proc: Path = Path("/proc"),
    kill: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, ...]:
    """SIGKILL every process ``uid`` still runs, and name each one outside ``group``.

    Called once Chrome's process group ``group`` has been killed. A process of the
    browser's uid still in that group is one the group kill has not finished with
    yet; one outside it left the group with ``setsid``, which a group kill never
    reaches, and it would otherwise run on unwatched once the socket audit stops.
    Each such process is reported as ``pid <n> (<name>)``, and the caller treats
    any as a run that is not clean. The sweep repeats until the uid runs nothing,
    a few times at most, so a stray that forks while it is being killed is caught
    too. ``()`` when there is no uid to sweep, which is when the harness could not
    switch users, or no ``/proc`` to read.
    """
    if uid is None or not proc.is_dir():
        return ()
    strays: dict[int, str] = {}
    for _ in range(_STRAY_SWEEPS):
        found = [
            process
            for entry in _pid_entries(proc)
            if (process := _process_of(entry, uid)) is not None
        ]
        if not found:
            break
        for process in found:
            if process.group != group:
                strays.setdefault(process.pid, process.name)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                kill(process.pid, signal.SIGKILL)
        sleep(_STRAY_PAUSE_SECONDS)
    return tuple(f"pid {pid} ({name})" for pid, name in sorted(strays.items()))
