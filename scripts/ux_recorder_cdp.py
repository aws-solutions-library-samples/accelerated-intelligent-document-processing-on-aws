# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
ux_recorder_cdp.py

Chrome DevTools Protocol plumbing for ``ux_recorder.py``: a minimal WebSocket
client, a request/reply session, target discovery, screencast frame capture and
the synthetic cursor overlay.

Why a vendored WebSocket client rather than ``websockets`` or ``websocket-client``
--------------------------------------------------------------------------------
The recorder is a local developer tool that talks to exactly one server — the
debug Chrome on 127.0.0.1 — over plain TCP with no TLS and no extensions. RFC 6455
for that case is about 150 lines: an HTTP upgrade, masked client frames, three
payload-length encodings, continuation reassembly and ping/pong. Its sibling
``ux_test_session.py`` is deliberately stdlib-only, and every third-party pin in
this repo has to flow through ``make dep-audit`` and the dependency manifests. A
dependency for one local script was not worth that, so the client lives here and
is exercised against a fake socket in ``scripts/tests/test_ux_recorder.py``.

Why a second CDP session on the same tab
----------------------------------------
The chrome-devtools MCP server already holds a session on the page the agent is
driving. Chrome allows several clients per target, so the recorder attaches its
own and never has to route through the MCP server or change how the agent works.
The two sessions do not see each other's commands.

Why screencast plus forced captures
-----------------------------------
``Page.startScreencast`` emits a frame only when the compositor produces a new one,
so a static screen produces nothing. That is what makes idle-time compression
possible downstream, but it also means a ``mark`` on a quiet screen would have no
frame of its own. Every boundary therefore also takes a ``Page.captureScreenshot``,
recorded as a forced frame, so each segment starts on a crisp frame at exactly the
moment the agent said it did.

Why a click on a field is named by its label rather than by what it holds
-------------------------------------------------------------------------
Each click the overlay reports is written to the session's ``events.jsonl`` and
``timeline.json``, and ``render`` prints its labels in the click table and quotes
them in its warnings, so a label must not carry what a person entered. A click made
while the recorder is paused is written with no labels at all (``ux_recorder.py``).

A field is what can hold what was entered: an ``input`` other than a ``submit``,
``reset``, ``button`` or ``image`` button, a ``textarea`` or ``select``, an editable
region (``contenteditable`` other than ``"false"``, or ``role="textbox"``,
``"searchbox"`` or ``"spinbutton"``), an element with ``role="combobox"`` (each role
in any letter case), the Monaco code editor (``.monaco-editor``), and every element
of a ``designMode`` document. A ``combobox`` shows its value, typed or chosen, as its
own text, so it is named the way a ``select`` is. Monaco is named outright because
it draws its lines as plain ``span`` elements beside its input element rather than
inside it, so no other rule reaches them.

A field, and anything inside one, is named by what labels it: its ``aria-label``,
the text its ``aria-labelledby`` points at, its ``<label>``, then its placeholder,
``name``, type or role. Text inside a field is never read, even through
``aria-labelledby``: an id that points inside the field contributes nothing, and
the next of those names it, or the label is empty. Monaco gives its root element
``role="code"`` and puts its ``ariaLabel`` option on its input element rather than
on the root, so a click on the code it draws is logged as ``code`` whatever
``ariaLabel`` the page passes; only a label on the root itself would change that. No
``value`` property is read, which also covers a custom element that keeps typed
text in one. A button-like ``input`` holds nothing typed and is named by its
caption, the ``value`` attribute the page set (``alt`` for an image).

Any other element keeps its ``aria-label`` or its text, leaving out the text of
every field inside it, so a click on a panel that holds a filled textarea does not
read the textarea. That text is gathered node by node. The walk stops once it has
the label's 60 characters or has visited 2,000 nodes, and it queues no more of an
element's children than it has visits left, because a child past that point could
never be reached. So a click on a large chart or table reads a bounded part of it
inside the mousedown listener rather than the whole subtree.

Page text is logged as it appears, and it can repeat what was typed or chosen word
for word: a dropdown trigger that is a plain button, as Cloudscape's ``Select``
trigger is, shows the current choice, a date picker's calendar button names the
selected date in its ``aria-label``, and an autosuggest offers
``Use: "<the typed value>"`` as an option. Two kinds of editor are not recognised:
an element made editable by the ``-webkit-user-modify`` style, which accepts typing
in Chrome while its ``isContentEditable`` stays false, and a code editor other than
Monaco that draws its text as ordinary elements. Text typed into either is page
text.

A click into any field counts as interactive, so ``INTERACTIVE`` contains
``FIELD``. It also names every role it accepts, because ``closest()`` compares
attribute strings and knows nothing of the ARIA role hierarchy.
``menuitemcheckbox`` and ``menuitemradio`` descend from ``menuitem``, and without
them a click on a checkable item in a Cloudscape ``ButtonDropdown`` reads as hitting
nothing interactive. ``treeitem`` is left out: a Cloudscape ``TreeView`` item does
nothing when its text is clicked, only its expand toggle does, so a click there
earns the warning.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    """Handshake failure or protocol violation on the CDP socket."""


class CdpError(Exception):
    """Chrome answered a command with an error object."""


def accept_key(nonce: str) -> str:
    """The Sec-WebSocket-Accept value a server must return for ``nonce``.

    SHA-1 is not a choice here: RFC 6455 section 4.2.2 defines the handshake
    token as SHA-1 over the client nonce concatenated with a fixed GUID, and
    Chrome answers nothing else. It protects nothing — the digest is echoed back
    over a loopback connection to prove the peer speaks WebSocket rather than
    HTTP — so ``usedforsecurity=False`` states what the call is actually for.
    """
    digest = hashlib.sha1(  # nosec B324 - RFC 6455 handshake token, not security
        (nonce + WS_GUID).encode("ascii"), usedforsecurity=False
    ).digest()
    return base64.b64encode(digest).decode("ascii")


class WebSocketClient:
    """A blocking RFC 6455 client sufficient for CDP on localhost."""

    def __init__(self, sock: Any, host: str, path: str) -> None:
        self._sock = sock
        self._host = host
        self._path = path
        self._send_lock = threading.Lock()
        self._closed = False

    @classmethod
    def connect(cls, url: str, timeout: float = 10.0) -> WebSocketClient:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "ws":
            raise WebSocketError(f"only ws:// is supported, got {url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        sock = socket.create_connection((host, port), timeout=timeout)
        client = cls(sock, f"{host}:{port}", path)
        client.handshake()
        sock.settimeout(None)
        return client

    def handshake(self, nonce: str | None = None) -> None:
        nonce = nonce or base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {self._host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {nonce}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode("ascii"))
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            raw += chunk
            if len(raw) > 65536:
                raise WebSocketError("handshake response too large")
        head, _, _rest = raw.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        status = lines[0].split(" ")
        if len(status) < 2 or status[1] != "101":
            raise WebSocketError(f"expected 101 Switching Protocols, got {lines[0]!r}")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = accept_key(nonce)
        if headers.get("sec-websocket-accept") != expected:
            raise WebSocketError("Sec-WebSocket-Accept mismatch")

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self._sock.sendall(bytes(header) + masked)

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise WebSocketError("connection closed")
            buf += chunk
        return bytes(buf)

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = self._read_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read_exact(8))
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv_message(self) -> str | bytes | None:
        """Next complete message, or None once the peer has closed."""
        fragments: list[bytes] = []
        message_opcode: int | None = None
        while True:
            if self._closed:
                return None
            try:
                fin, opcode, payload = self._recv_frame()
            except (WebSocketError, OSError):
                self._closed = True
                return None
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._closed = True
                try:
                    self._send_frame(OP_CLOSE, payload[:2])
                except OSError:
                    pass
                return None
            if opcode in (OP_TEXT, OP_BINARY):
                message_opcode = opcode
                fragments = [payload]
            elif opcode == OP_CONT:
                if message_opcode is None:
                    raise WebSocketError("continuation frame without a start")
                fragments.append(payload)
            else:
                raise WebSocketError(f"unsupported opcode {opcode}")
            if fin:
                data = b"".join(fragments)
                if message_opcode == OP_TEXT:
                    return data.decode("utf-8")
                return data

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
            except OSError:
                pass
        try:
            self._sock.close()
        except OSError:
            pass


class CdpSession:
    """Request/reply correlation and event dispatch over one WebSocket."""

    def __init__(
        self,
        ws: Any,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._ws = ws
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self.on_event = on_event
        self.closed = threading.Event()
        self._reader: threading.Thread | None = None

    def start_reader(self) -> None:
        self._reader = threading.Thread(
            target=self._reader_loop, name="cdp-reader", daemon=True
        )
        self._reader.start()

    def _reader_loop(self) -> None:
        while True:
            message = self._ws.recv_message()
            if message is None:
                break
            if isinstance(message, bytes):
                continue
            try:
                self.dispatch(json.loads(message))
            except json.JSONDecodeError:
                continue
        self.closed.set()
        with self._lock:
            for event, slot in self._pending.values():
                slot["error"] = {"message": "connection closed"}
                event.set()

    def dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message:
            with self._lock:
                entry = self._pending.get(message["id"])
            if entry is None:
                return
            event, slot = entry
            slot.update(message)
            event.set()
            return
        method = message.get("method")
        if method and self.on_event:
            self.on_event(method, message.get("params") or {})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a command without waiting for its reply.

        Used from the reader thread, where blocking on a reply would deadlock:
        the reply can only be read by the thread that is waiting for it.
        """
        with self._lock:
            call_id = self._next_id
            self._next_id += 1
        self._ws.send_text(
            json.dumps({"id": call_id, "method": method, "params": params or {}})
        )

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        if self.closed.is_set():
            raise CdpError(f"{method}: connection closed")
        done = threading.Event()
        slot: dict[str, Any] = {}
        with self._lock:
            call_id = self._next_id
            self._next_id += 1
            self._pending[call_id] = (done, slot)
        self._ws.send_text(
            json.dumps({"id": call_id, "method": method, "params": params or {}})
        )
        if not done.wait(timeout):
            with self._lock:
                self._pending.pop(call_id, None)
            raise CdpError(f"{method}: no reply within {timeout}s")
        with self._lock:
            self._pending.pop(call_id, None)
        if "error" in slot:
            raise CdpError(f"{method}: {slot['error'].get('message', slot['error'])}")
        return slot.get("result", {})

    def close(self) -> None:
        self._ws.close()


def list_targets(port: int = 9222, host: str = "127.0.0.1") -> list[dict[str, Any]]:
    """Every debuggable target the browser advertises at ``/json``."""
    with urllib.request.urlopen(f"http://{host}:{port}/json", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def page_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only real tabs: no extensions, no browser UI, no chrome:// pages."""
    return [
        t
        for t in targets
        if t.get("type") == "page"
        and not t.get("url", "").startswith(
            ("chrome://", "chrome-extension://", "devtools://")
        )
    ]


class TargetChoiceError(Exception):
    def __init__(self, message: str, candidates: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.candidates = candidates


def choose_target(
    targets: list[dict[str, Any]],
    url_contains: str | None = None,
    target_id: str | None = None,
) -> dict[str, Any]:
    """Exactly one page target, or a TargetChoiceError listing the candidates."""
    pages = page_targets(targets)
    if target_id:
        matches = [t for t in pages if t.get("id") == target_id]
        if not matches:
            raise TargetChoiceError(f"no page target with id {target_id}", pages)
        return matches[0]
    if url_contains:
        pages = [t for t in pages if url_contains in t.get("url", "")]
    if not pages:
        raise TargetChoiceError("no page target matches", page_targets(targets))
    if len(pages) > 1:
        raise TargetChoiceError(
            "several page targets match; narrow with --url-contains or --target", pages
        )
    return pages[0]


class Screencast:
    """Writes screencast and forced frames to disk with a shared clock."""

    def __init__(
        self,
        session: CdpSession,
        frames_dir: Path,
        index_path: Path,
        clock: Callable[[], float] = time.time,
        quality: int = 70,
        max_width: int = 1600,
        every_nth: int = 1,
    ) -> None:
        self.session = session
        self.frames_dir = Path(frames_dir)
        self.index_path = Path(index_path)
        self.clock = clock
        self.quality = quality
        self.max_width = max_width
        self.every_nth = every_nth
        self.count = 0
        self.bytes = 0
        self.last_t: float | None = None
        self._lock = threading.Lock()
        self.frames_dir.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        self.session.call(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": self.quality,
                "maxWidth": self.max_width,
                "maxHeight": self.max_width,
                "everyNthFrame": self.every_nth,
                "maxFramesInFlight": 2,
            },
        )

    def stop(self) -> None:
        try:
            self.session.call("Page.stopScreencast", timeout=5)
        except CdpError:
            pass

    def handle_frame(self, params: dict[str, Any]) -> int:
        data = base64.b64decode(params["data"])
        metadata = params.get("metadata") or {}
        seq = self._write(data, metadata.get("timestamp"), forced=False)
        self.ack(params)
        return seq

    def ack(self, params: dict[str, Any]) -> None:
        """Acknowledge a frame so Chrome keeps sending; never blocks the reader."""
        session_id = params.get("sessionId")
        if session_id is not None:
            try:
                self.session.notify(
                    "Page.screencastFrameAck", {"sessionId": session_id}
                )
            except OSError:
                pass

    def capture_now(self) -> int:
        result = self.session.call(
            "Page.captureScreenshot",
            {"format": "jpeg", "quality": self.quality, "optimizeForSpeed": True},
            timeout=15,
        )
        return self._write(base64.b64decode(result["data"]), None, forced=True)

    def _write(self, data: bytes, t_chrome: float | None, forced: bool) -> int:
        with self._lock:
            self.count += 1
            seq = self.count
            t = self.clock()
            name = f"frames/{seq:06d}.jpg"
            (self.frames_dir / f"{seq:06d}.jpg").write_bytes(data)
            record = {
                "seq": seq,
                "t": t,
                "t_chrome": t_chrome,
                "file": name,
                "forced": forced,
            }
            with self.index_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            self.bytes += len(data)
            self.last_t = t
        return seq


CURSOR_OVERLAY_JS = r"""
(() => {
  if (window.__uxRecorderCursor) { return; }
  const ARROW = "data:image/svg+xml;utf8," + encodeURIComponent(
    "<svg xmlns='http://www.w3.org/2000/svg' width='22' height='30' viewBox='0 0 22 30'>" +
    "<path d='M2 2 L2 24 L8 18.5 L12.5 28 L16.5 26.2 L12 16.8 L20 16.8 Z' fill='#111' stroke='#fff' stroke-width='1.6' stroke-linejoin='round'/></svg>");
  const state = { cursor: null, style: null, x: -100, y: -100, seen: false };
  const ensure = () => {
    if (state.cursor || !document.body) { return; }
    const style = document.createElement('style');
    style.id = '__ux-recorder-style';
    style.textContent =
      '#__ux-recorder-cursor{position:fixed;left:0;top:0;width:22px;height:30px;pointer-events:none;' +
      'z-index:2147483647;background:url("' + ARROW + '") no-repeat;opacity:0;transition:opacity .15s;}' +
      '.__ux-recorder-ripple{position:fixed;width:44px;height:44px;margin:-22px 0 0 -22px;border-radius:50%;' +
      'border:3px solid #e8712b;background:rgba(232,113,43,.25);pointer-events:none;z-index:2147483646;' +
      'animation:__uxr .55s ease-out forwards;}' +
      '@keyframes __uxr{from{transform:scale(.35);opacity:.9}to{transform:scale(1.6);opacity:0}}';
    document.head.appendChild(style);
    const cursor = document.createElement('div');
    cursor.id = '__ux-recorder-cursor';
    cursor.setAttribute('aria-hidden', 'true');
    document.body.appendChild(cursor);
    state.cursor = cursor;
    state.style = style;
    if (state.seen) { place(state.x, state.y); }
  };
  const place = (x, y) => {
    state.x = x; state.y = y; state.seen = true;
    if (!state.cursor) { ensure(); }
    if (!state.cursor) { return; }
    state.cursor.style.transform = 'translate(' + (x - 2) + 'px,' + (y - 2) + 'px)';
    state.cursor.style.opacity = '1';
  };
  const ripple = (x, y) => {
    if (!document.body) { return; }
    const r = document.createElement('div');
    r.className = '__ux-recorder-ripple';
    r.setAttribute('aria-hidden', 'true');
    r.style.left = x + 'px';
    r.style.top = y + 'px';
    document.body.appendChild(r);
    setTimeout(() => r.remove(), 600);
  };
  const FIELD = 'input:not([type="submit" i]):not([type="reset" i]):not([type="button" i]):not([type="image" i]),' +
    'textarea,select,[contenteditable]:not([contenteditable="false" i]),[role="textbox" i],[role="searchbox" i],' +
    '[role="spinbutton" i],[role="combobox" i],.monaco-editor';
  const INTERACTIVE = FIELD + ',a,button,input,summary,[role="button"],[role="link"],[role="option"],' +
    '[role="radio"],[role="checkbox"],[role="tab"],[role="menuitem"],[role="menuitemcheckbox"],' +
    '[role="menuitemradio"],[role="switch"]';
  const LABEL_LENGTH = 60;
  const WALK_LIMIT = 2000;
  const attr = (el, name) => (el.getAttribute && el.getAttribute(name)) || '';
  const fieldOf = (el) => el.closest(FIELD) || (document.designMode === 'on' ? document.documentElement : null);
  const textOf = (root) => {
    if (fieldOf(root)) { return ''; }
    let text = '';
    let budget = WALK_LIMIT;
    const pending = [root];
    while (pending.length && budget > 0 && text.trim().length < LABEL_LENGTH) {
      const node = pending.pop();
      budget -= 1;
      if (node.nodeType === 3) {
        text = (text + node.nodeValue).replace(/\s+/g, ' ');
      } else if (node.nodeType === 1 && !node.matches(FIELD)) {
        for (let i = Math.min(node.childNodes.length, budget) - 1; i >= 0; i -= 1) { pending.push(node.childNodes[i]); }
      }
    }
    return text.trimStart().slice(0, LABEL_LENGTH);
  };
  const caption = (el) => {
    const type = el.localName === 'input' ? attr(el, 'type').toLowerCase() : '';
    return /^(submit|reset|button|image)$/.test(type) ? attr(el, type === 'image' ? 'alt' : 'value') || type : '';
  };
  const fieldName = (field) => {
    const textsOf = (els) => els.filter(Boolean).map(textOf).join(' ').trim();
    const labelledBy = attr(field, 'aria-labelledby').split(/\s+/).filter(Boolean);
    return attr(field, 'aria-label') || textsOf(labelledBy.map((id) => document.getElementById(id))) ||
      textsOf(Array.from(field.labels || [])) || attr(field, 'placeholder') || attr(field, 'name') ||
      (typeof field.type === 'string' ? field.type : '') || attr(field, 'role');
  };
  const describe = (el) => {
    if (!el || !el.tagName || !el.closest) { return null; }
    const field = fieldOf(el);
    const label = field ? fieldName(field) : attr(el, 'aria-label') || textOf(el) || caption(el);
    return el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + ' "' +
      String(label).replace(/\s+/g, ' ').trim().slice(0, LABEL_LENGTH) + '"';
  };
  const report = (e) => {
    if (typeof window.__uxRecorderEvent !== 'function') { return; }
    try {
      const under = document.elementFromPoint(e.clientX, e.clientY);
      const hit = under && under.closest ? under.closest(INTERACTIVE) || fieldOf(under) : null;
      window.__uxRecorderEvent(JSON.stringify({
        kind: 'click', x: e.clientX, y: e.clientY, button: e.button,
        target: describe(e.target), under: describe(under), interactive: describe(hit),
        scrollX: window.scrollX, scrollY: window.scrollY
      }));
    } catch (err) {}
  };
  const onMove = (e) => place(e.clientX, e.clientY);
  const onDown = (e) => { place(e.clientX, e.clientY); ripple(e.clientX, e.clientY); report(e); };
  document.addEventListener('pointermove', onMove, true);
  document.addEventListener('mousemove', onMove, true);
  document.addEventListener('mousedown', onDown, true);
  if (document.body) { ensure(); } else { document.addEventListener('DOMContentLoaded', ensure, { once: true }); }
  window.__uxRecorderCursor = {
    remove() {
      document.removeEventListener('pointermove', onMove, true);
      document.removeEventListener('mousemove', onMove, true);
      document.removeEventListener('mousedown', onDown, true);
      if (state.cursor) { state.cursor.remove(); }
      if (state.style) { state.style.remove(); }
      delete window.__uxRecorderCursor;
    }
  };
})();
"""

CURSOR_REMOVE_JS = "window.__uxRecorderCursor && window.__uxRecorderCursor.remove();"


CLICK_BINDING = "__uxRecorderEvent"


def install_cursor_overlay(session: CdpSession) -> str | None:
    """Inject the cursor now and on every future document; returns the script id.

    The overlay reports each mousedown through a CDP binding, so the recorder can
    log where a click really landed and what was under it. That is how a ripple
    drawn away from the intended element is diagnosed: the timeline names the
    element that was actually hit.
    """
    session.call("Runtime.enable")
    session.call("Runtime.addBinding", {"name": CLICK_BINDING})
    result = session.call(
        "Page.addScriptToEvaluateOnNewDocument", {"source": CURSOR_OVERLAY_JS}
    )
    session.call("Runtime.evaluate", {"expression": CURSOR_OVERLAY_JS})
    return result.get("identifier")


def remove_cursor_overlay(session: CdpSession, identifier: str | None) -> None:
    try:
        if identifier:
            session.call(
                "Page.removeScriptToEvaluateOnNewDocument",
                {"identifier": identifier},
                timeout=5,
            )
        session.call("Runtime.evaluate", {"expression": CURSOR_REMOVE_JS}, timeout=5)
    except CdpError:
        pass
