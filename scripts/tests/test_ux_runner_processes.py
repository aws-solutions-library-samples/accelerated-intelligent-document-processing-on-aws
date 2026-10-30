# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the UX runner's process groups (scripts/ux_runner/processes.py).

The claim worth pinning is that nothing the harness starts can outlive it: not on
a timeout, not on a job cancel, and not after a normal exit when a child left a
grandchild behind. Each of those is driven with real child processes of the
running interpreter, POSIX only, because the property is about what the kernel
does with a process group and a fake would only show that the code calls
``killpg``. A grandchild counts as gone once it has no process-table entry, or
(on Linux) once it is a zombie, because whether an orphan is reaped promptly
depends on whatever runs as pid 1 in the container, not on this module. The
bound on captured output is driven the same way, by real children that write
past it.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import processes  # noqa: E402

#: A child that starts a sleeping grandchild with its own stdio closed, prints
#: the grandchild's pid (and writes it to argv[2] when given), then sleeps for
#: argv[1] seconds.
SPAWNS_A_GRANDCHILD = textwrap.dedent(
    """
    import subprocess, sys, time
    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if len(sys.argv) > 2:
        with open(sys.argv[2] + ".tmp", "w") as handle:
            handle.write(str(grandchild.pid))
        import os
        os.replace(sys.argv[2] + ".tmp", sys.argv[2])
    print(grandchild.pid, flush=True)
    time.sleep(float(sys.argv[1]))
    """
)

#: A child that starts a grandchild in a session of its own, still holding the
#: child's stdout and stderr, writes the grandchild's pid to argv[1], and sleeps.
ESCAPES_THE_GROUP = textwrap.dedent(
    """
    import os, subprocess, sys, time
    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    with open(sys.argv[1] + ".tmp", "w") as handle:
        handle.write(str(grandchild.pid))
    os.replace(sys.argv[1] + ".tmp", sys.argv[1])
    time.sleep(60)
    """
)

#: A child that ignores SIGTERM, says so, and sleeps.
IGNORES_SIGTERM = textwrap.dedent(
    """
    import signal, sys, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.stdout.write("ready\\n")
    sys.stdout.flush()
    time.sleep(60)
    """
)

#: A child that writes argv[1] bytes of ``o`` to stdout and of ``e`` to stderr,
#: interleaved, in chunks far larger than a pipe holds, and exits 0.
FLOODS_BOTH_STREAMS = textwrap.dedent(
    """
    import os, sys
    left = int(sys.argv[1])
    while left:
        size = min(left, 1 << 20)
        os.write(1, b"o" * size)
        os.write(2, b"e" * size)
        left -= size
    """
)


def _env() -> dict[str, str]:
    return {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return False
    return "\nState:\tZ" in status


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return _gone(pid)


def _first_pid(text: str) -> int:
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, "the child printed no grandchild pid"
    return int(lines[0])


@pytest.mark.unit
def test_run_in_group_feeds_the_input_on_stdin_and_returns_it_on_stdout() -> None:
    completed = processes.run_in_group(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        input_text="a prompt\nwith ⚠️ two lines",
        env=_env(),
        cwd=None,
        timeout=60,
    )
    assert completed.stdout == "a prompt\nwith ⚠️ two lines"
    assert completed.returncode == 0
    assert completed.timed_out is False
    assert completed.elapsed >= 0


@pytest.mark.unit
def test_run_in_group_with_no_time_limit_waits_for_the_child() -> None:
    """``math.inf`` is no deadline: the capture waits on its pipes and the child
    without a timeout, since ``select`` and ``wait`` take no infinite one. A
    local story run has no deadline, and an infinite timeout once reached
    ``select`` there and failed the run after its last flow."""
    completed = processes.run_in_group(
        [
            sys.executable,
            "-c",
            "import sys, time; time.sleep(0.3); sys.stdout.write(sys.stdin.read())",
        ],
        input_text="read after a pause",
        env=_env(),
        cwd=None,
        timeout=math.inf,
    )
    assert (completed.returncode, completed.timed_out) == (0, False)
    assert completed.stdout == "read after a pause"


@pytest.mark.unit
def test_run_in_group_reports_exit_code_and_stderr() -> None:
    completed = processes.run_in_group(
        [sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"],
        input_text="",
        env=_env(),
        cwd=None,
        timeout=60,
    )
    assert (completed.returncode, completed.stderr, completed.timed_out) == (
        3,
        "bad",
        False,
    )


@pytest.mark.unit
def test_run_in_group_runs_in_the_given_directory(tmp_path: Path) -> None:
    completed = processes.run_in_group(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        input_text="",
        env=_env(),
        cwd=tmp_path,
        timeout=60,
    )
    assert Path(completed.stdout.strip()).resolve() == tmp_path.resolve()


@pytest.mark.unit
def test_a_timeout_kills_the_whole_group_including_the_grandchild() -> None:
    completed = processes.run_in_group(
        [sys.executable, "-c", SPAWNS_A_GRANDCHILD, "60"],
        input_text="",
        env=_env(),
        cwd=None,
        timeout=3,
    )
    grandchild = _first_pid(completed.stdout)
    try:
        assert completed.timed_out is True
        assert completed.returncode == -signal.SIGTERM
        assert _gone_within(grandchild, 5), "the grandchild outlived the timeout"
    finally:
        processes.kill_group(grandchild)


@pytest.mark.unit
def test_a_grandchild_left_behind_by_a_normal_exit_is_killed() -> None:
    completed = processes.run_in_group(
        [sys.executable, "-c", SPAWNS_A_GRANDCHILD, "0"],
        input_text="",
        env=_env(),
        cwd=None,
        timeout=60,
    )
    grandchild = _first_pid(completed.stdout)
    try:
        assert completed.returncode == 0
        assert completed.timed_out is False
        assert _gone_within(grandchild, 5), "the grandchild outlived its parent"
    finally:
        processes.kill_group(grandchild)


@pytest.mark.unit
def test_a_timed_out_child_that_ignores_sigterm_is_sigkilled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(processes, "_DRAIN_TERM_SECONDS", 0.5)
    completed = processes.run_in_group(
        [sys.executable, "-c", IGNORES_SIGTERM],
        input_text="",
        env=_env(),
        cwd=None,
        timeout=3,
    )
    assert completed.timed_out is True
    assert completed.returncode == -signal.SIGKILL
    assert completed.stdout == "ready\n", "output before the kill is kept"


@pytest.mark.unit
def test_pipes_held_by_a_descendant_that_left_the_group_cannot_hang_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processes, "_DRAIN_TERM_SECONDS", 0.5)
    monkeypatch.setattr(processes, "_DRAIN_KILL_SECONDS", 0.5)
    pid_file = tmp_path / "escaped.pid"
    completed = processes.run_in_group(
        [sys.executable, "-c", ESCAPES_THE_GROUP, str(pid_file)],
        input_text="",
        env=_env(),
        cwd=None,
        timeout=3,
    )
    escaped = int(pid_file.read_text(encoding="utf-8"))
    try:
        assert completed.timed_out is True
        assert (completed.stdout, completed.stderr) == ("", "")
        assert completed.returncode == -signal.SIGTERM, "the leader is still reaped"
        assert not _gone(escaped), "a descendant in its own session is out of reach"
    finally:
        processes.kill_group(escaped)


@pytest.mark.unit
def test_terminate_reports_none_for_a_leader_it_cannot_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        processes.os, "killpg", lambda pid, sig: sent.append((pid, sig))
    )

    class Unreapable:
        pid = 4242

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("leader", timeout or 0)

    stuck = processes.ManagedProcess(
        name="stuck", popen=cast("subprocess.Popen[bytes]", Unreapable())
    )
    assert stuck.terminate(grace=0.1) is None
    assert sent == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def _signal_once_written(path: Path, thread_id: int) -> None:
    deadline = time.monotonic() + 30
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    signal.pthread_kill(thread_id, signal.SIGTERM)


@pytest.mark.unit
def test_sigterm_while_waiting_raises_terminated_and_kills_the_group(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "grandchild.pid"
    previous = signal.getsignal(signal.SIGTERM)
    restore = processes.install_sigterm_handler()
    sender = threading.Thread(
        target=_signal_once_written, args=(pid_file, threading.get_ident())
    )
    raised = False
    try:
        sender.start()
        try:
            processes.run_in_group(
                [sys.executable, "-c", SPAWNS_A_GRANDCHILD, "60", str(pid_file)],
                input_text="",
                env=_env(),
                cwd=None,
                timeout=60,
            )
        except processes.Terminated:
            raised = True
    finally:
        try:
            sender.join()
        finally:
            restore()
    assert raised, "SIGTERM did not surface as Terminated"
    assert signal.getsignal(signal.SIGTERM) == previous
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    try:
        assert _gone_within(grandchild, 5), "the grandchild outlived the cancel"
    finally:
        processes.kill_group(grandchild)


@pytest.mark.unit
def test_only_the_first_sigterm_raises_so_the_cleanup_can_finish() -> None:
    restore = processes.install_sigterm_handler()
    caught = 0
    try:
        for _ in range(2):
            try:
                signal.raise_signal(signal.SIGTERM)
            except processes.Terminated:
                caught += 1
    finally:
        restore()
    assert caught == 1


@pytest.mark.unit
def test_restore_falls_back_to_the_default_when_no_python_handler_was_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[Any] = []
    monkeypatch.setattr(
        processes.signal, "signal", lambda signum, handler: installed.append(handler)
    )
    restore = processes.install_sigterm_handler()
    restore()
    assert installed[-1] is signal.SIG_DFL


@pytest.mark.unit
def test_terminate_sigkills_a_child_that_ignores_sigterm() -> None:
    child = processes.start(
        [sys.executable, "-c", IGNORES_SIGTERM],
        name="stubborn",
        env=_env(),
        stdout=subprocess.PIPE,
    )
    stream = child.popen.stdout
    assert stream is not None
    try:
        assert stream.readline().strip() == b"ready"
        assert child.alive()
        code = child.terminate(grace=0.5)
    finally:
        processes.kill_group(child.pid)
        stream.close()
    assert code == -signal.SIGKILL
    assert not child.alive()


@pytest.mark.unit
def test_terminate_returns_the_exit_code_of_a_leader_that_already_exited() -> None:
    child = processes.start(
        [sys.executable, "-c", "raise SystemExit(4)"], name="quick", env=_env()
    )
    child.popen.wait(timeout=60)
    assert child.terminate(grace=1.0) == 4


@pytest.mark.unit
def test_start_switches_user_only_when_it_can_and_always_starts_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_popen(argv: list[str], **kwargs: Any) -> object:
        seen.append({"argv": argv, **kwargs})
        return object()

    monkeypatch.setattr(processes.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(processes, "can_switch_user", lambda: False)
    processes.start(["/bin/true"], name="unswitched", env={"A": "1"}, user="uxbrowser")
    monkeypatch.setattr(processes, "can_switch_user", lambda: True)
    started = processes.start(
        ["/bin/true"], name="switched", env={"A": "1"}, user="uxbrowser"
    )
    processes.start(["/bin/true"], name="nobody", env={})

    def identity(call: dict[str, Any]) -> tuple[Any, Any, Any]:
        return call["user"], call["group"], call["extra_groups"]

    assert [call["argv"] for call in seen] == [["/bin/true"]] * 3
    assert all(call["start_new_session"] is True for call in seen)
    assert identity(seen[0]) == (None, None, None), "no switch without root"
    assert identity(seen[1]) == ("uxbrowser", "uxbrowser", [])
    assert identity(seen[2]) == (None, None, None), "no switch without a user"
    assert seen[0]["env"] == {"A": "1"}
    assert seen[0]["stdin"] == subprocess.DEVNULL
    assert started.name == "switched"


@pytest.mark.unit
def test_run_in_group_switches_user_only_when_it_can_and_always_starts_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Render runs this way as its own unprivileged user, so that it cannot read
    the orchestrator's ``/proc/<pid>/environ``; claude runs this way as root."""
    seen: list[dict[str, Any]] = []

    class FakePopen:
        """A child that has written ``out`` and exited, with no stdin or stderr."""

        pid = 424242
        returncode = 0
        stdin = stderr = None

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            seen.append({"argv": argv, **kwargs})
            reader, writer = os.pipe()
            os.write(writer, b"out")
            os.close(writer)
            self.stdout = os.fdopen(reader, "rb")

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(processes.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(processes, "kill_group", lambda pid, sig=None: None)

    def run(**kwargs: Any) -> processes.Completed:
        return processes.run_in_group(
            ["/bin/true"],
            input_text="",
            env={"A": "1"},
            cwd=None,
            timeout=5.0,
            **kwargs,
        )

    monkeypatch.setattr(processes, "can_switch_user", lambda: False)
    run(user="uxrender")
    monkeypatch.setattr(processes, "can_switch_user", lambda: True)
    completed = run(user="uxrender")
    run()

    def identity(call: dict[str, Any]) -> tuple[Any, Any, Any]:
        return call["user"], call["group"], call["extra_groups"]

    assert identity(seen[0]) == (None, None, None), "no switch without root"
    assert identity(seen[1]) == ("uxrender", "uxrender", [])
    assert identity(seen[2]) == (None, None, None), "no switch without a user"
    assert all(call["start_new_session"] is True for call in seen)
    assert all(call["env"] == {"A": "1"} for call in seen)
    assert (completed.returncode, completed.stdout) == (0, "out")


@pytest.mark.unit
def test_can_switch_user_needs_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(processes.os, "geteuid", lambda: 1000)
    assert processes.can_switch_user() is False
    monkeypatch.setattr(processes.os, "geteuid", lambda: 0)
    assert processes.can_switch_user() is (os.name == "posix")


@pytest.mark.unit
def test_kill_group_tolerates_a_gone_group_and_refuses_pids_zero_and_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[int, int]] = []

    def fake_killpg(pid: int, sig: int) -> None:
        sent.append((pid, sig))
        if pid == 4242:
            raise ProcessLookupError
        if pid == 4343:
            raise PermissionError

    monkeypatch.setattr(processes.os, "killpg", fake_killpg)
    processes.kill_group(0)
    processes.kill_group(1)
    processes.kill_group(-5)
    processes.kill_group(4242)
    processes.kill_group(4343, signal.SIGTERM)
    assert sent == [(4242, signal.SIGKILL), (4343, signal.SIGTERM)]


@pytest.mark.unit
def test_output_past_the_bound_is_read_and_discarded_and_counted() -> None:
    """3 MiB on each stream, far more than a pipe holds and than the bound: the
    child is never left blocked on a full pipe, only the first ``output_limit``
    bytes of each stream are kept, and the rest is counted rather than kept."""
    flood = 3 * 1024 * 1024
    limit = 4096
    completed = processes.run_in_group(
        [sys.executable, "-c", FLOODS_BOTH_STREAMS, str(flood)],
        input_text="",
        env=_env(),
        cwd=None,
        timeout=60,
        output_limit=limit,
    )
    assert (completed.returncode, completed.timed_out) == (0, False)
    assert completed.stdout == "o" * limit
    assert completed.stderr == "e" * limit
    assert (completed.stdout_bytes, completed.stderr_bytes) == (
        b"o" * limit,
        b"e" * limit,
    )
    assert (completed.stdout_dropped, completed.stderr_dropped) == (
        flood - limit,
        flood - limit,
    )


@pytest.mark.unit
def test_output_within_the_bound_is_kept_whole_and_nothing_is_counted() -> None:
    completed = processes.run_in_group(
        [sys.executable, "-c", FLOODS_BOTH_STREAMS, "4096"],
        input_text="",
        env=_env(),
        cwd=None,
        timeout=60,
        output_limit=4096,
    )
    assert (len(completed.stdout), len(completed.stderr)) == (4096, 4096)
    assert (completed.stdout_dropped, completed.stderr_dropped) == (0, 0)


@pytest.mark.unit
def test_the_default_bound_is_the_one_every_caller_gets() -> None:
    import inspect

    parameter = inspect.signature(processes.run_in_group).parameters["output_limit"]
    assert parameter.default == processes.MAX_OUTPUT_BYTES == 8 * 1024 * 1024


@pytest.mark.unit
def test_a_prompt_larger_than_a_pipe_is_fed_while_the_output_is_read() -> None:
    """The child echoes stdin as it reads it, so writing the whole prompt before
    reading anything would fill both pipes and hang until the timeout."""
    prompt = "line of the prompt \r\n" * 100_000
    completed = processes.run_in_group(
        [
            sys.executable,
            "-c",
            "import os\nwhile chunk := os.read(0, 65536):\n    os.write(1, chunk)\n",
        ],
        input_text=prompt,
        env=_env(),
        cwd=None,
        timeout=60,
    )
    assert completed.timed_out is False
    assert completed.stdout == prompt.replace("\r\n", "\n")
    assert completed.stdout_bytes == prompt.encode()
    assert completed.stdout_dropped == 0


@pytest.mark.unit
def test_a_child_that_closes_its_stdin_early_is_not_an_error() -> None:
    completed = processes.run_in_group(
        [sys.executable, "-c", "import os; os.close(0); print('done', flush=True)"],
        input_text="x" * (4 * 1024 * 1024),
        env=_env(),
        cwd=None,
        timeout=60,
    )
    assert (completed.returncode, completed.stdout, completed.timed_out) == (
        0,
        "done\n",
        False,
    )


@pytest.mark.unit
def test_a_child_that_closes_its_output_and_keeps_running_times_out() -> None:
    """End of file on both pipes is not the end of the run: the child must also
    exit, and one that does not is killed when the timeout comes."""
    completed = processes.run_in_group(
        [
            sys.executable,
            "-c",
            "import os, sys, time\n"
            "sys.stdout.write('said\\n'); sys.stdout.flush()\n"
            "os.close(1); os.close(2); time.sleep(60)\n",
        ],
        input_text="",
        env=_env(),
        cwd=None,
        timeout=2,
    )
    assert completed.timed_out is True
    assert completed.returncode == -signal.SIGTERM
    assert completed.stdout == "said\n"
