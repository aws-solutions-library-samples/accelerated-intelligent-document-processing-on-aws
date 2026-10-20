# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Start and kill the UX test's processes by process group, never one by one.

Everything the harness starts has children it does not start: ``claude`` spawns
both MCP servers, and Chrome spawns renderer, GPU and network-service processes.
Killing a leader leaves those running, and a browser left running after the job
step is a signed-in session with nobody watching it. So every process here starts
as the leader of its own session (``start_new_session=True``), and every kill is
``os.killpg`` of that group.

When the group dies
-------------------
* **On a timeout.** :func:`run_in_group` sends SIGTERM to the group, gives it ten
  seconds, then SIGKILL, so a hung session costs a bounded amount of job time.
* **On a job cancel.** GitLab cancels a job by sending SIGTERM to it.
  :func:`install_sigterm_handler` turns that signal into :class:`Terminated`, an
  exception raised in the main thread, so every ``finally`` between the signal and
  ``main`` runs: the ``claude`` group, Chrome and the proxy are killed by the same
  cleanup a normal run uses instead of by a second code path nobody exercises.
  It derives from ``BaseException`` so an ``except Exception`` in between cannot
  swallow a cancel.
* **After a normal exit.** MCP servers can survive their client, so
  :func:`run_in_group` SIGKILLs the group in ``finally`` whatever happened.

Why the prompt goes on stdin
----------------------------
One ``argv`` element is capped at 128 KiB on Linux (``MAX_ARG_STRLEN``), which a
prompt carrying an MR diff and two skill files can exceed, and ``argv`` is readable
by every process on the host through ``/proc/<pid>/cmdline``. Nothing that carries a
payload travels in ``argv`` here; :func:`run_in_group` takes it as ``input_text``.

Bounds worth knowing
--------------------
* A descendant that calls ``setsid`` itself leaves the group and is not reached by
  any kill here. For the browser that is closed elsewhere: once Chrome's group is
  killed and its uid's processes have had a few seconds to exit by themselves,
  ``chrome.kill_strays`` kills every process that uid still runs and names each one
  that had left the group, which the orchestrator treats as a run that is not
  clean, and the socket audit, which reads sockets by uid, watched such a
  process's sockets for as long as it ran. For every other group the job
  container's teardown is the backstop.
* :func:`run_in_group` waits for end-of-file on the child's output, so a descendant
  that inherited those pipes and outlives the leader keeps it waiting until the
  timeout, which then reports ``timed_out``.
* :func:`run_in_group` keeps at most :data:`MAX_OUTPUT_BYTES` of each of the
  child's stdout and stderr, the first that many, and goes on reading and
  discarding the rest, so a child that floods its output neither fills this
  process's memory nor blocks on a full pipe, and :class:`Completed` counts what
  was discarded. A caller that writes the output to a file writes the kept bytes
  (:attr:`Completed.stdout_bytes`), at most that much; the decoded text can be
  three times as long, because each byte of it that is not valid UTF-8 decodes
  to a three-byte U+FFFD. Render, ``claude`` and the recorder's own subcommands
  all run this way.
* The uid switch in :func:`start` and :func:`run_in_group` needs root. Without it
  the process starts as the current user and the caller records that, because the
  socket audit then cannot tell Chrome's sockets from anybody else's, and a child
  of the same uid can read this process's ``/proc/<pid>/environ``.
"""

from __future__ import annotations

import contextlib
import os
import select
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import IO

#: The most :func:`run_in_group` keeps of each of a child's stdout and stderr.
#: ``claude``'s whole answer is one JSON object on stdout, far smaller than this.
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
#: How much one read takes from a pipe.
_READ_BYTES = 64 * 1024
#: How much one write puts into the child's stdin: no more than a pipe takes
#: atomically, so a write the selector says is possible never blocks.
_WRITE_BYTES = getattr(select, "PIPE_BUF", 512)
#: How long a group that overran its timeout gets to exit on SIGTERM before SIGKILL.
_DRAIN_TERM_SECONDS = 10.0
#: How long the pipes of an overrun group get to close after SIGKILL.
_DRAIN_KILL_SECONDS = 30.0
#: How long a group gets to exit on SIGTERM when a signal interrupts the wait.
_INTERRUPT_GRACE_SECONDS = 5.0
#: How long :meth:`ManagedProcess.terminate` waits to reap a leader after SIGKILL.
_REAP_SECONDS = 5.0


class Terminated(BaseException):
    """Raised in the main thread by the SIGTERM handler.

    A ``BaseException`` rather than an ``Exception``, so it passes through the
    ``except Exception`` blocks that guard individual steps and reaches the
    cleanup that owns the processes, the same way ``KeyboardInterrupt`` does.
    """


def install_sigterm_handler() -> Callable[[], None]:
    """Make SIGTERM raise :class:`Terminated`, and return the undo.

    Only the first SIGTERM raises. A repeat that arrives while the cleanup it
    started is still killing process groups is absorbed, because raising again
    would abort that cleanup halfway and leave exactly the processes it exists to
    kill; GitLab's follow-up SIGKILL remains the backstop. The returned function
    puts back whatever handler was installed before, or the default when the
    previous one was not installed from Python.
    """
    fired = False

    def handler(signum: int, frame: FrameType | None) -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        raise Terminated(f"received signal {signum}")

    previous = signal.signal(signal.SIGTERM, handler)

    def restore() -> None:
        signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)

    return restore


def can_switch_user() -> bool:
    """Whether :func:`start` can run a process as another user: POSIX, as root."""
    return os.name == "posix" and os.geteuid() == 0


def kill_group(pid: int, sig: int | None = None) -> None:
    """Signal the process group led by ``pid``, tolerating one that is gone.

    ``sig`` defaults to SIGKILL, looked up when the group is signalled rather than
    when this module is imported: Windows has no SIGKILL, and a Windows host
    imports this module through ``ai_ux_test.py --collect-only``, which signals
    nothing. ``ProcessLookupError`` means nothing is left to signal and
    ``PermissionError`` means the group is not ours to signal; neither is a reason
    to abandon the rest of a cleanup. A ``pid`` of 0 or 1 is refused outright:
    ``os.killpg(0, ...)`` signals the caller's own group, which under xdist
    includes the test runner, and group 1 is init's. Neither can be a group this
    module started.
    """
    if pid <= 1:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL if sig is None else sig)


@dataclass
class ManagedProcess:
    """A long-running process the harness started as a group leader."""

    name: str
    popen: subprocess.Popen[bytes]

    @property
    def pid(self) -> int:
        """The leader's pid, which is also the group id."""
        return self.popen.pid

    def alive(self) -> bool:
        """Whether the leader has not exited yet."""
        return self.popen.poll() is None

    def terminate(self, grace: float = 10.0) -> int | None:
        """SIGTERM the group, wait up to ``grace`` seconds, then SIGKILL the group.

        The SIGKILL is sent even when the leader has already exited, because a
        leader's exit says nothing about its children, which stay members of the
        group after it is gone. Returns the leader's exit code, or ``None`` when it
        is still unreaped five seconds after the SIGKILL.
        """
        kill_group(self.pid, signal.SIGTERM)
        try:
            code: int | None = self.popen.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            code = None
        kill_group(self.pid, signal.SIGKILL)
        if code is None:
            try:
                code = self.popen.wait(timeout=_REAP_SECONDS)
            except subprocess.TimeoutExpired:
                code = None
        return code


def start(
    argv: Sequence[str],
    *,
    name: str,
    env: Mapping[str, str],
    cwd: Path | None = None,
    user: str | None = None,
    stdin: int | None = subprocess.DEVNULL,
    stdout: int | IO[bytes] | None = subprocess.DEVNULL,
    stderr: int | IO[bytes] | None = subprocess.DEVNULL,
) -> ManagedProcess:
    """Start ``argv`` as the leader of a new session, as ``user`` when possible.

    ``env`` is the whole environment: nothing is inherited from this process, so
    a caller that forgets a variable gets a process without it rather than one
    holding the job's credentials. With ``user`` given and :func:`can_switch_user`
    true, the process runs as that user and its same-named group, with every
    supplementary group dropped. With ``user`` given and no way to switch, it runs
    as the current user, and recording that is the caller's job. ``None`` for
    ``user``, ``group`` and ``extra_groups`` is ``Popen``'s own "leave as is".
    """
    switching = user is not None and can_switch_user()
    popen = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        list(argv),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=dict(env),
        cwd=cwd,
        start_new_session=True,
        user=user if switching else None,
        group=user if switching else None,
        extra_groups=[] if switching else None,
    )
    return ManagedProcess(name=name, popen=popen)


@dataclass(frozen=True)
class Completed:
    """What :func:`run_in_group` observed. ``elapsed`` is in seconds.

    ``stdout`` and ``stderr`` are the first bytes of each stream, up to the bound
    the run was given, decoded as text; ``stdout_bytes`` and ``stderr_bytes`` are
    those bytes as read, which is what a caller writing them to a file writes, so
    the file stays within the bound; ``stdout_dropped`` and ``stderr_dropped``
    count the bytes read past it and discarded.
    """

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    elapsed: float
    stdout_dropped: int = 0
    stderr_dropped: int = 0
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""


@dataclass
class _Kept:
    """The first bytes of one output stream, up to a bound, and a count of the rest."""

    data: bytearray = field(default_factory=bytearray)
    dropped: int = 0

    def add(self, chunk: bytes, limit: int) -> None:
        """Keep as much of ``chunk`` as fits under ``limit`` and count the rest."""
        kept = chunk[: max(limit - len(self.data), 0)]
        self.data += kept
        self.dropped += len(chunk) - len(kept)

    def text(self) -> str:
        """The kept bytes as ``Popen``'s text mode reads them: UTF-8 with errors
        replaced, and universal newlines."""
        decoded = self.data.decode("utf-8", errors="replace")
        return decoded.replace("\r\n", "\n").replace("\r", "\n")


class _Capture:
    """A child's stdin fed and its stdout and stderr read, each kept to ``limit``.

    One selector serves the three pipes, as ``Popen.communicate`` does on POSIX, so
    a prompt larger than a pipe holds is written while the child's output is read
    and neither end can block the other. Unlike ``communicate``, each stream keeps
    only its first ``limit`` bytes; whatever follows is read, counted and dropped,
    so the child is never left blocked on a full pipe either.
    """

    def __init__(
        self, popen: subprocess.Popen[bytes], payload: bytes, limit: int
    ) -> None:
        self._popen = popen
        self._payload = memoryview(payload)
        self._sent = 0
        self._limit = limit
        self.stdout = _Kept()
        self.stderr = _Kept()
        self._selector = (
            selectors.PollSelector()
            if hasattr(selectors, "PollSelector")
            else selectors.SelectSelector()
        )
        if popen.stdin is not None:
            if payload:
                self._selector.register(
                    popen.stdin, selectors.EVENT_WRITE, (popen.stdin, None)
                )
            else:
                self._close(popen.stdin)
        for stream, kept in ((popen.stdout, self.stdout), (popen.stderr, self.stderr)):
            if stream is not None:
                self._selector.register(stream, selectors.EVENT_READ, (stream, kept))

    def _close(self, stream: IO[bytes]) -> None:
        with contextlib.suppress(KeyError):
            self._selector.unregister(stream)
        with contextlib.suppress(OSError):
            stream.close()

    def _feed(self, stream: IO[bytes]) -> None:
        chunk = self._payload[self._sent : self._sent + _WRITE_BYTES]
        try:
            self._sent += os.write(stream.fileno(), chunk)
        except BrokenPipeError:
            self._close(stream)
            return
        if self._sent >= len(self._payload):
            self._close(stream)

    def _read(self, stream: IO[bytes], kept: _Kept) -> None:
        chunk = os.read(stream.fileno(), _READ_BYTES)
        if chunk:
            kept.add(chunk, self._limit)
        else:
            self._close(stream)

    def _serve(self, stream: IO[bytes], kept: _Kept | None) -> None:
        """Write to ``stream`` when it is the child's stdin, else read from it."""
        if kept is None:
            self._feed(stream)
        else:
            self._read(stream, kept)

    def finish(self, deadline: float) -> bool:
        """Feed and read until every pipe is closed and the child has exited, or
        until ``deadline`` on the monotonic clock; whether it got there.

        A pipe still open at the deadline stays registered, so a later call
        carries on where this one stopped.
        """
        while self._selector.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            for key, _ in self._selector.select(left):
                self._serve(*key.data)
        try:
            self._popen.wait(timeout=max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            return False
        return True


def _drain_after_timeout(popen: subprocess.Popen[bytes], capture: _Capture) -> None:
    """Collect what a group that overran still writes, killing it on the way.

    SIGTERM first, so a process that handles it can flush what it has; SIGKILL if
    ten seconds are not enough; and no more waiting if even thirty seconds after
    SIGKILL the pipes have not closed, which only a descendant that left the group
    can cause, in which case what was read by then is what is reported. The leader
    is long dead by then, so it is reaped without waiting and its real exit code
    is reported.
    """
    kill_group(popen.pid, signal.SIGTERM)
    if capture.finish(time.monotonic() + _DRAIN_TERM_SECONDS):
        return
    kill_group(popen.pid, signal.SIGKILL)
    if not capture.finish(time.monotonic() + _DRAIN_KILL_SECONDS):
        popen.poll()


def _close_pipes(popen: subprocess.Popen[bytes]) -> None:
    """Close whatever pipes an interrupted capture left open."""
    for stream in (popen.stdin, popen.stdout, popen.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()


def run_in_group(
    argv: Sequence[str],
    *,
    input_text: str,
    env: Mapping[str, str],
    cwd: Path | None,
    timeout: float,
    user: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    output_limit: int = MAX_OUTPUT_BYTES,
) -> Completed:
    """Run ``argv`` to completion in its own process group, ``input_text`` on stdin.

    The group is killed on a timeout (SIGTERM, then SIGKILL), on any
    ``BaseException`` raised while waiting (:class:`Terminated`,
    ``KeyboardInterrupt``: SIGTERM, five seconds, SIGKILL, then the exception
    propagates), and after every exit, because MCP servers can survive the
    ``claude`` process that started them. A timed-out run reports ``timed_out``
    and the leader's exit code, or -9 when the leader could not be reaped.
    ``user`` is honoured as :func:`start` honours it: that user and its group,
    with no supplementary groups, when :func:`can_switch_user`, and the current
    user otherwise. Of each of stdout and stderr the first ``output_limit``
    bytes are kept and the rest is read and discarded (:class:`Completed`).
    """
    started = clock()
    switching = user is not None and can_switch_user()
    popen = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
        start_new_session=True,
        user=user if switching else None,
        group=user if switching else None,
        extra_groups=[] if switching else None,
    )
    timed_out = False
    try:
        try:
            capture = _Capture(
                popen, input_text.encode("utf-8", errors="replace"), output_limit
            )
            if not capture.finish(time.monotonic() + timeout):
                timed_out = True
                _drain_after_timeout(popen, capture)
        except BaseException:
            kill_group(popen.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                popen.wait(timeout=_INTERRUPT_GRACE_SECONDS)
            kill_group(popen.pid, signal.SIGKILL)
            raise
    finally:
        kill_group(popen.pid, signal.SIGKILL)
        _close_pipes(popen)
    returncode = popen.returncode if popen.returncode is not None else -9
    return Completed(
        returncode=returncode,
        stdout=capture.stdout.text(),
        stderr=capture.stderr.text(),
        timed_out=timed_out,
        elapsed=clock() - started,
        stdout_dropped=capture.stdout.dropped,
        stderr_dropped=capture.stderr.dropped,
        stdout_bytes=bytes(capture.stdout.data),
        stderr_bytes=bytes(capture.stderr.data),
    )
