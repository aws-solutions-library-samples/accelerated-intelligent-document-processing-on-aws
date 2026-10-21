# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The harness side of the egress proxy: its inputs, its launch, its outputs.

``scripts/ux_test_proxy.py`` is the confined browser's only network path, and it
is deliberately self-contained, so everything it needs is prepared here and handed
to it as files and argv: the stack's UI environment decoded from
``UX_TEST_UI_ENV``, the MR's build checked and made readable, the rules written
where the proxy's user can read them, and the proxy itself started as ``uxproxy``
and waited for. Its refusal log and stats are read back from here too.

Properties it is built around
-----------------------------
* **No value ever reaches an error message.** ``UX_TEST_UI_ENV`` holds the review
  stack's pool ids, hosts and bucket names, and every error raised here names the
  variable or the path inside the build, never the value, and is raised
  ``from None`` so a decoding error's own text, which quotes the bytes it
  choked on, is not chained onto it.
* **The build is static files, nothing else.** :func:`prepare_site` refuses a
  build holding a symlink or anything that is not a regular file or directory,
  and a build whose own path runs through a symlink, because the build directory
  arrives as an artifact of a job that ran the MR's own ``npm ci`` and ``vite
  build``, inside the MR's checkout, and a symlink in it or on the way to it would
  let the proxy serve whatever it points at.
* **The proxy's environment is three fixed names.** No AWS credential, no token,
  no ``*_PROXY`` variable, and ``python3 -I -B`` on top, so the process the
  confinement rests on holds nothing worth taking, reads nothing it was not given
  on its command line and writes no bytecode into the pinned harness. ``-B`` is on
  the command line because ``-I`` ignores every ``PYTHON*`` variable, so
  ``PYTHONDONTWRITEBYTECODE`` would do nothing there.
* **It is ready when it says so.** The proxy writes its bound address to a ready
  file only after it is listening, and :func:`launch_proxy` waits for that file
  rather than for a fixed time, so Chrome is never started against a port nobody
  is listening on yet.
* **Its directory is its own, and read back as its own.** ``run_dir/proxy`` is
  made fresh, and the rules and the log the proxy's output goes to are written in
  it before it is handed to ``uxproxy``, so this process, which is root, never
  writes there by name once anything else could. From then on whatever is under
  the names read back, the ready file, the refusal log, the stats and the log, is
  the proxy's to choose, so every read goes through ``readback``: the directory and
  the name are opened without following a link or waiting on a pipe, and only a
  regular file of the uid the directory was handed to (:attr:`ProxyRun.owner`),
  within its bound, is read, and then at most the size that was checked. A link, a
  FIFO, another uid's file or an oversized one there is refused with a
  :class:`ProxyError` naming it, and nothing outside the directory is read.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from ux_test_proxy import EgressConfigError, EgressRules, parse_ui_env

from . import paths, processes, readback
from .processes import ManagedProcess

#: The proxy's whole environment.
_PROXY_ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "TZ": "UTC",
}
_POLL_SECONDS = 0.05
_DISPLAY_LIMIT = 200
#: The files of the proxy's directory, as ``launch_proxy`` names them.
READY_FILE = "ready"
REFUSAL_LOG = "refusals.jsonl"
STATS_FILE = "stats.json"
LOG_FILE = "proxy.log"
#: The most of the refusal log read back: what ``ux_test_proxy.RefusalLog`` can
#: write, 5000 lines of a 200-character target and a 32-character method, each
#: character escaped to at most 12 bytes, comes to under 15 MB.
MAX_REFUSAL_LOG_BYTES = 16 * 1024 * 1024
#: The most of the stats file read back; the proxy writes a few hundred bytes.
MAX_STATS_BYTES = 64 * 1024
#: The most of the ready file read back; the proxy writes ``host:port``.
MAX_READY_BYTES = 1024
#: The most of the log read back, which is only ever read when the proxy did not
#: start, and then holds its start-up error.
MAX_LOG_BYTES = 1024 * 1024


class ProxyError(Exception):
    """The egress proxy did not start, so nothing that depends on it may.

    Also raised when a file the proxy left in its directory is refused on the way
    back (``readback``).
    """


#: The proxy, as a refusal of one of its files names it.
_PROXY = readback.Writer("the proxy's", "directory", ProxyError)


def ui_env_from_b64(value: str) -> dict[str, str]:
    """Decode ``UX_TEST_UI_ENV``: one-line base64 of UTF-8 ``KEY=VALUE`` text.

    Surrounding whitespace is ignored; anything else that is not base64 is refused.
    """
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise EgressConfigError("UX_TEST_UI_ENV is not valid base64") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise EgressConfigError("UX_TEST_UI_ENV is not UTF-8 text") from None
    return parse_ui_env(text)


def _display(relative: str) -> str:
    """A path from the build, printable: control characters blanked, bounded."""
    shown = "".join(char if char.isprintable() else "?" for char in relative)
    return shown[:_DISPLAY_LIMIT]


def _raise_walk_error(error: OSError) -> None:
    raise EgressConfigError("the UI build cannot be read") from None


def _first_symlink(path: Path) -> Path | None:
    """The first component of ``path``, taken as spelled, that is a symlink.

    Each prefix is checked with ``lstat`` before ``..`` is applied, because
    ``os.path.abspath`` drops ``x/..`` lexically while the kernel follows ``x``
    first. A relative path starts from the working directory, which
    ``os.getcwd`` already reports with its symlinks resolved. A component that
    does not exist ends the walk; the caller's own checks report a missing build.
    """
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            return None
        if stat.S_ISLNK(mode):
            return current
    return None


def prepare_site(build_dir: Path) -> Path:
    """Check the MR's build and make it readable by the proxy's user.

    It must be reached through no symlink at all: in CI the build sits in the MR's
    checkout, whose ``src``, ``src/ui`` and ``build`` components the MR could
    commit as symlinks, and the proxy would then serve whatever directory they
    lead to. That includes ``/tmp`` and ``/var`` on macOS, so a development run
    there passes the resolved path. It must hold ``index.html``, and every entry
    under it must be a regular file or a directory: a symlink, a FIFO, a socket
    or a device is refused, naming its path relative to the build. Then, best
    effort, every directory gets ``a+rx`` and every file ``a+r`` (and ``a+x``
    where any execute bit was already set), and every entry loses its group and
    other write bits, which is ``chmod -R a+rX,go-w``: the proxy runs as
    ``uxproxy``, and a build extracted as root with a restrictive umask would
    otherwise be unreadable to it, while one extracted at GitLab's umask 0000
    would let the browser's uid change what the proxy serves. A ``chmod`` that
    fails, as on a read-only bind mount, is left for the proxy's own start-up
    check to report. Returns the resolved build path.
    """
    link = _first_symlink(build_dir)
    if link is not None:
        raise EgressConfigError(
            f"the UI build is reached through a symlink: {_display(str(link))}"
        )
    root = build_dir.resolve()
    if not root.is_dir() or not (root / "index.html").is_file():
        raise EgressConfigError("the UI build has no index.html")
    directories = [root]
    files: list[tuple[Path, int]] = []
    for current, names, filenames in os.walk(
        root, followlinks=False, onerror=_raise_walk_error
    ):
        for name in [*names, *filenames]:
            entry = Path(current) / name
            relative = _display(entry.relative_to(root).as_posix())
            try:
                mode = entry.lstat().st_mode
            except OSError:
                raise EgressConfigError(
                    f"the UI build cannot be read: {relative}"
                ) from None
            if stat.S_ISLNK(mode):
                raise EgressConfigError(f"the UI build holds a symlink: {relative}")
            if stat.S_ISDIR(mode):
                directories.append(entry)
            elif stat.S_ISREG(mode):
                files.append((entry, mode))
            else:
                raise EgressConfigError(
                    f"the UI build holds something that is not a file: {relative}"
                )
    for directory in directories:
        _set_mode(directory, 0o555)
    for file, mode in files:
        _set_mode(file, 0o444 | (0o111 if mode & 0o111 else 0))
    return root


def _set_mode(path: Path, bits: int) -> None:
    """Add ``bits`` to ``path``'s mode and take away its group and other write bits."""
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
        os.chmod(path, (mode | bits) & ~(stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        return


def write_rules(path: Path, rules: EgressRules) -> Path:
    """Write the ``--rules`` file, readable by every user (``0o644``)."""
    path.write_text(json.dumps(rules.to_json(), indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)
    return path


@dataclass(frozen=True)
class ProxyRun:
    """A started proxy, the files it reads and writes, and whose files they are.

    ``owner`` is the uid ``run_dir/proxy`` was handed to, which is the proxy's
    when the harness can switch users and this process's otherwise; every file
    read back from the directory must belong to it.
    """

    process: ManagedProcess
    ready_file: Path
    refusal_log: Path
    stats_file: Path
    log_file: Path
    owner: int


def _read(path: Path, *, owner: int, limit: int) -> bytes | None:
    """``path``, a file of the proxy's directory, read back through ``readback``."""
    return readback.read(
        path.parent, path.name, writer=_PROXY, owner=owner, limit=limit
    )


def _is_ready(run: ProxyRun) -> bool:
    """Whether the proxy has written its ready file. One refused on the way back
    raises :class:`ProxyError`, saying the proxy did not start and why."""
    try:
        raw = _read(run.ready_file, owner=run.owner, limit=MAX_READY_BYTES)
    except ProxyError as refused:
        raise ProxyError(f"the egress proxy did not start: {refused}") from None
    return raw is not None and bool(raw.strip())


def _last_line(run: ProxyRun) -> str:
    """The proxy's last log line, bounded, for the error that says it failed.

    A log refused on the way back gives the refusal instead, so the error still
    says something true about why there is no line.
    """
    try:
        raw = _read(run.log_file, owner=run.owner, limit=MAX_LOG_BYTES)
    except ProxyError as refused:
        return _display(str(refused))
    lines = (raw or b"").decode("utf-8", errors="replace").splitlines()
    last = next((line for line in reversed(lines) if line.strip()), "")
    return _display(last.strip())


def _new_log(path: Path) -> IO[bytes]:
    """Create the proxy's log, which must not exist yet, readable by its owner."""
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_NOFOLLOW,
        0o600,
    )
    return os.fdopen(descriptor, "ab")


def _hand_over(proxy_dir: Path, log_file: Path, user: str | None) -> int:
    """Give the log and then the directory to ``user`` when the harness can switch
    users; the uid the directory belongs to afterwards, which is the uid every
    file read back from it must have."""
    if user is not None and processes.can_switch_user():
        shutil.chown(log_file, user=user, group=user)
        shutil.chown(proxy_dir, user=user, group=user)
    return proxy_dir.lstat().st_uid


def _wait_until_ready(
    run: ProxyRun,
    *,
    timeout: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> bool:
    """Whether the proxy wrote its ready file before ``timeout`` or its exit."""
    deadline = clock() + timeout
    while not _is_ready(run):
        if not run.process.alive() or clock() >= deadline:
            return False
        sleep(_POLL_SECONDS)
    return True


def launch_proxy(
    *,
    run_dir: Path,
    rules: EgressRules,
    build_dir: Path | None,
    user: str | None,
    python: str = sys.executable,
    script: Path = paths.SCRIPTS_DIR / "ux_test_proxy.py",
    start: Callable[..., ManagedProcess] = processes.start,
    timeout: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ProxyRun:
    """Start the proxy as ``user`` on ``127.0.0.1:3128`` and wait until it listens.

    Its files live in ``run_dir/proxy``, which is made here, so nothing in it
    predates this call (``mkdir`` raises ``FileExistsError`` when it exists). The
    rules and the log its stdout and stderr go to are written there first; then
    the log and the directory are handed to ``user`` when the harness can switch
    users, so the proxy can write its stats and its ready and refusal files
    there, and nothing more is written there by this process. Without
    ``build_dir`` it runs in deployed-stack mode and serves nothing itself. If the
    ready file has not appeared within ``timeout`` seconds, or the process exits
    first, the process group is killed and :class:`ProxyError` is raised with the
    proxy's last log line; a ready file refused on the way back, or anything else
    raised while waiting, kills the group too.
    """
    proxy_dir = run_dir / "proxy"
    proxy_dir.mkdir(mode=0o700)
    os.chmod(proxy_dir, 0o700)
    rules_file = write_rules(proxy_dir / "rules.json", rules)
    ready_file = proxy_dir / READY_FILE
    refusal_log = proxy_dir / REFUSAL_LOG
    stats_file = proxy_dir / STATS_FILE
    log_file = proxy_dir / LOG_FILE
    argv = [
        python,
        "-I",
        "-B",
        str(script),
        "--listen",
        f"{paths.PROXY_HOST}:{paths.PROXY_PORT}",
        "--rules",
        str(rules_file),
    ]
    if build_dir is not None:
        argv += ["--build-dir", str(build_dir)]
    argv += [
        "--refusal-log",
        str(refusal_log),
        "--stats",
        str(stats_file),
        "--ready-file",
        str(ready_file),
    ]
    with _new_log(log_file) as log:
        owner = _hand_over(proxy_dir, log_file, user)
        process = start(
            argv,
            name="egress-proxy",
            env=dict(_PROXY_ENV),
            user=user,
            stdout=log,
            stderr=log,
        )
    run = ProxyRun(
        process=process,
        ready_file=ready_file,
        refusal_log=refusal_log,
        stats_file=stats_file,
        log_file=log_file,
        owner=owner,
    )
    try:
        ready = _wait_until_ready(run, timeout=timeout, sleep=sleep, clock=clock)
    except BaseException:
        process.terminate()
        raise
    if not ready:
        process.terminate()
        detail = _last_line(run)
        raise ProxyError(
            "the egress proxy did not start" + (f": {detail}" if detail else "")
        )
    return run


def read_refusals(path: Path, *, owner: int, limit: int = 5000) -> list[dict[str, Any]]:
    """The refusal log's records, at most ``limit``; ``[]`` when there is none.

    Read back as a file of ``owner``'s within :data:`MAX_REFUSAL_LOG_BYTES`
    (:class:`ProxyRun`), so a link, a FIFO, another uid's file or an oversized one
    raises :class:`ProxyError`. A line that is not a JSON object is skipped rather
    than fatal: the log is evidence for a summary, and a torn last line must not
    lose the rest.
    """
    raw = _read(path, owner=owner, limit=MAX_REFUSAL_LOG_BYTES)
    if raw is None:
        return []
    records: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if len(records) >= limit:
            break
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def read_stats(path: Path, *, owner: int) -> dict[str, Any]:
    """The proxy's totals, or ``{}`` when it wrote none or wrote something else.

    Read back as :func:`read_refusals` reads, within :data:`MAX_STATS_BYTES`, so
    anything but a regular file of ``owner``'s there raises :class:`ProxyError`.
    """
    raw = _read(path, owner=owner, limit=MAX_STATS_BYTES)
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}
