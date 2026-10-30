# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the harness's recorder control, ``ux_runner/recording.py``.

The recorder's CLI is driven through an injected ``run``, standing in for
``processes.run_in_group``, that records each argv and answers as the real
subcommands do, so the argv shapes are pinned exactly;
``status`` talks to a real Unix socket served in-process; ``kill`` ends a real
sleeping child; and ``render_video`` runs ``ux_recorder.py render`` through an
injected group runner that plays scripted exits, which is how the child's exact
argv and environment, the Polly ``AccessDenied`` fallback and the private render
log are shown without ffmpeg, Pillow or AWS.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import processes, readback, recording  # noqa: E402

HARNESS = Path("/harness")
OUT = Path("/run/ux/recordings")
SESSION = OUT / "idp-ux-nightly-20261014-210311"
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/run/ux/home"}


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = "", *, timed_out: bool = False
) -> processes.Completed:
    return processes.Completed(returncode, stdout, stderr, timed_out, 0.1)


def _scripted_run(
    answers: list[processes.Completed | BaseException],
    calls: list[tuple[list[str], dict[str, Any]]],
) -> Callable[..., processes.Completed]:
    """A ``run_in_group`` that records each call and plays ``answers`` in order."""

    def run(argv: list[str], **kwargs: Any) -> processes.Completed:
        calls.append((list(argv), kwargs))
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return run


@pytest.mark.unit
def test_the_recorder_gets_five_fixed_variables_and_no_credential() -> None:
    assert recording.recorder_env(Path("/run/ux/home")) == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/run/ux/home",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


@pytest.mark.unit
def test_start_runs_the_review_start_subcommand_with_an_exact_argv() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    reply = {"session_dir": str(SESSION), "socket": "/run/ux/r.sock", "pid": 4242}
    run = _scripted_run([_completed(stdout=json.dumps(reply, indent=2))], calls)
    say = "UX test of merge request 812 on the review stack, as Admin."
    got = recording.start(
        harness_root=HARNESS,
        out_dir=OUT,
        stack="idp-ux-nightly",
        persona="Admin",
        flows=["change", "6.1"],
        target_id="TARGET1",
        say=say,
        env=ENV,
        python="/usr/local/bin/python3",
        run=run,
    )
    assert got == reply
    assert calls == [
        (
            [
                "/usr/local/bin/python3",
                "/harness/scripts/ux_recorder.py",
                "start",
                "--kind",
                "review",
                "--stack",
                "idp-ux-nightly",
                "--persona",
                "Admin",
                "--flow",
                "change",
                "--flow",
                "6.1",
                "--target",
                "TARGET1",
                "--port",
                "9222",
                "--out",
                str(OUT),
                "--say",
                say,
            ],
            {"input_text": "", "env": ENV, "cwd": None, "timeout": 60.0},
        )
    ]


@pytest.mark.unit
def test_the_recorders_subcommands_run_in_a_bounded_process_group_by_default() -> None:
    """``run_in_group`` keeps at most its bound of each stream, so a subcommand
    that floods its output costs the orchestrator a bounded amount of memory."""
    import inspect

    for function in (recording.start, recording.stop):
        default = inspect.signature(function).parameters["run"].default
        assert default is processes.run_in_group


def _start(run: Callable[..., processes.Completed]) -> dict[str, Any]:
    return recording.start(
        harness_root=HARNESS,
        out_dir=OUT,
        stack="idp-ux-nightly",
        persona="Admin",
        flows=[],
        target_id="TARGET1",
        say="Start.",
        env=ENV,
        run=run,
    )


@pytest.mark.unit
def test_a_recorder_that_does_not_come_up_raises_with_the_end_of_its_stderr() -> None:
    stderr = "early noise " * 100 + "The recorder did not come up. Its log: boom"
    run = _scripted_run([_completed(returncode=1, stderr=stderr)], [])
    with pytest.raises(recording.RecorderError) as raised:
        _start(run)
    message = str(raised.value)
    assert message.startswith("the recorder did not start (exit 1): ")
    assert message.endswith("Its log: boom")
    assert len(message) <= len("the recorder did not start (exit 1): ") + 400


@pytest.mark.unit
@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (_completed(-9, timed_out=True), "no answer within 60 s"),
        (FileNotFoundError("python3"), "could not be run"),
        (_completed(stdout="recording..."), "not the expected JSON"),
        (_completed(stdout=json.dumps({"session_dir": "x"})), "not the expected JSON"),
    ],
)
def test_a_start_without_a_usable_reply_raises(
    answer: processes.Completed | BaseException, detail: str
) -> None:
    with pytest.raises(recording.RecorderError, match=detail):
        _start(_scripted_run([answer], []))


def _stop(run: Callable[..., processes.Completed]) -> dict[str, Any]:
    return recording.stop(
        harness_root=HARNESS,
        out_dir=OUT,
        session_dir=SESSION,
        say="That ends the UX test.",
        env=ENV,
        python="/usr/local/bin/python3",
        run=run,
    )


STOP_ARGV = [
    "/usr/local/bin/python3",
    "/harness/scripts/ux_recorder.py",
    "stop",
    "--out",
    str(OUT),
    "--session",
    str(SESSION),
    "--say",
    "That ends the UX test.",
]


@pytest.mark.unit
def test_stop_names_both_the_out_dir_and_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[Path] = []
    monkeypatch.setattr(recording, "kill", lambda out_dir, **kw: killed.append(out_dir))
    calls: list[tuple[list[str], dict[str, Any]]] = []
    reply = {"ok": True, "frames": 120, "session_dir": str(SESSION)}
    got = _stop(_scripted_run([_completed(stdout=json.dumps(reply))], calls))
    assert got == reply
    assert [argv for argv, _ in calls] == [STOP_ARGV]
    assert calls[0][1]["env"] == ENV and calls[0][1]["timeout"] == 120.0
    assert killed == []


@pytest.mark.unit
def test_a_failed_stop_kills_the_recorder_and_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[Path] = []
    monkeypatch.setattr(recording, "kill", lambda out_dir, **kw: killed.append(out_dir))
    calls: list[tuple[list[str], dict[str, Any]]] = []
    finalised = {"ok": True, "finalized_from_disk": True, "frames": 80}
    answers: list[processes.Completed | BaseException] = [
        _completed(-9, timed_out=True),
        _completed(stdout=json.dumps(finalised)),
    ]
    assert _stop(_scripted_run(answers, calls)) == finalised
    assert [argv for argv, _ in calls] == [STOP_ARGV, STOP_ARGV]
    assert killed == [OUT]


@pytest.mark.unit
def test_a_stop_that_fails_twice_raises_after_one_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[Path] = []
    monkeypatch.setattr(recording, "kill", lambda out_dir, **kw: killed.append(out_dir))
    answers: list[processes.Completed | BaseException] = [
        _completed(returncode=1, stderr="no session.json"),
        _completed(stdout=json.dumps({"ok": False})),
    ]
    with pytest.raises(recording.RecorderError, match="could not be stopped"):
        _stop(_scripted_run(answers, []))
    assert killed == [OUT]


class _StatusHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _StatusServer)
        server.requests.append(json.loads(self.rfile.readline()))
        reply = {"ok": True, "marks": 3, "clicks_rejected": 1}
        self.wfile.write((json.dumps(reply) + "\n").encode())


class _StatusServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, path: str) -> None:
        self.requests: list[Any] = []
        super().__init__(path, _StatusHandler)


@pytest.fixture
def recorder_socket() -> Iterator[tuple[str, _StatusServer]]:
    directory = tempfile.mkdtemp(dir="/tmp")  # nosec B108 - short AF_UNIX path
    path = os.path.join(directory, "r.sock")
    server = _StatusServer(path)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    yield path, server
    server.shutdown()
    server.server_close()
    shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.unit
def test_status_asks_the_live_recorder_over_its_socket(
    tmp_path: Path, recorder_socket: tuple[str, _StatusServer]
) -> None:
    path, server = recorder_socket
    (tmp_path / "current.json").write_text(
        json.dumps({"session_dir": str(tmp_path / "s"), "socket": path, "pid": 1})
    )
    assert recording.status(tmp_path) == {"ok": True, "marks": 3, "clicks_rejected": 1}
    assert server.requests == [{"cmd": "status"}]


@pytest.mark.unit
@pytest.mark.parametrize(
    "current",
    [None, "{torn", json.dumps(["not", "an", "object"]), json.dumps({"pid": 1})],
)
def test_status_is_none_when_no_recorder_answers(
    tmp_path: Path, current: str | None
) -> None:
    if current is not None:
        (tmp_path / "current.json").write_text(current)
    assert recording.status(tmp_path) is None


@pytest.mark.unit
def test_status_is_none_when_the_socket_is_gone(tmp_path: Path) -> None:
    missing = tmp_path / "gone.sock"
    (tmp_path / "current.json").write_text(json.dumps({"socket": str(missing)}))
    assert recording.status(tmp_path) is None


@pytest.mark.unit
def test_kill_ends_the_recorder_that_current_json_names(tmp_path: Path) -> None:
    child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    try:
        (tmp_path / "current.json").write_text(json.dumps({"pid": child.pid}))
        recording.kill(tmp_path, wait=0.2)
        assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.unit
@pytest.mark.parametrize("pid", [0, 1, -1, True, "4242", None, 3.5])
def test_kill_never_signals_anything_but_one_real_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid: Any
) -> None:
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda target, sig: signalled.append((target, sig)))
    (tmp_path / "current.json").write_text(json.dumps({"pid": pid}))
    recording.kill(tmp_path, wait=0)
    assert signalled == []


@pytest.mark.unit
def test_kill_does_not_wait_for_a_recorder_that_is_already_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(target: int, sig: int) -> None:
        raise ProcessLookupError(target)

    monkeypatch.setattr(os, "kill", gone)
    (tmp_path / "current.json").write_text(json.dumps({"pid": 4242}))
    started = time.monotonic()
    recording.kill(tmp_path, wait=30)
    assert time.monotonic() - started < 5


@pytest.mark.unit
def test_kill_tolerates_a_missing_or_corrupt_current_file(tmp_path: Path) -> None:
    recording.kill(tmp_path, wait=0)
    (tmp_path / "current.json").write_text("{torn")
    recording.kill(tmp_path, wait=0)
    assert (tmp_path / "current.json").read_text() == "{torn"


@pytest.mark.unit
def test_kill_reads_the_file_the_recorder_writes() -> None:
    import ux_recorder

    assert recording._CURRENT_FILE == ux_recorder.CURRENT_FILE


@pytest.mark.unit
def test_chapters_are_the_footage_segments_as_m_ss_and_labels(tmp_path: Path) -> None:
    segments = [
        {"index": 0, "kind": "title", "label": "Title", "out_start": 0.0},
        {"index": 1, "kind": "footage", "label": "Open the document", "out_start": 4.2},
        {"index": 2, "kind": "footage", "label": "Rotate page 2", "out_start": 31.9},
        {"index": 3, "kind": "footage", "label": "Save and reload", "out_start": 100},
        {"index": 4, "kind": "end", "label": "Findings", "out_start": 130.0},
        {"index": 5, "kind": "footage", "label": None, "out_start": 140.0},
        {"index": 6, "kind": "footage", "label": "No start"},
        {"index": 7, "kind": "footage", "label": "Unbounded", "out_start": math.inf},
        {"index": 8, "kind": "footage", "label": "Not a time", "out_start": math.nan},
    ]
    (tmp_path / "segments.json").write_text(json.dumps({"segments": segments}))
    assert recording.chapters(tmp_path, owner=os.geteuid()) == [
        ("0:04", "Open the document"),
        ("0:31", "Rotate page 2"),
        ("1:40", "Save and reload"),
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [None, "{torn", "[]", '{"segments": "x"}', "\xff\xfe{", "[" * 100_000],
)
def test_chapters_are_empty_without_a_usable_segments_file(
    tmp_path: Path, content: str | None
) -> None:
    if content is not None:
        (tmp_path / "segments.json").write_bytes(content.encode("latin-1"))
    assert recording.chapters(tmp_path, owner=os.geteuid()) == []


SECRET = "root-only-value-7f3a"


class OsSpy:
    """``readback``'s ``os``, recording each ``open`` and counting each ``read``.

    Installed as ``readback.os`` alone, the module every read-back of render's
    outputs goes through, so what it records is what the read-back asked for and
    nothing the test runner does at the same time.
    """

    def __init__(self) -> None:
        self.opened: list[tuple[str, int | None]] = []
        self.descriptors: list[int] = []
        self.reads = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def open(
        self, path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        self.opened.append((os.fspath(path), dir_fd))
        descriptor = os.open(path, flags, mode, dir_fd=dir_fd)
        self.descriptors.append(descriptor)
        return descriptor

    def read(self, descriptor: int, length: int) -> bytes:
        self.reads += 1
        return os.read(descriptor, length)


class CheckedSmaller(OsSpy):
    """An ``os`` whose ``fstat`` reports the size a file had when it was checked."""

    def __init__(self, size: int) -> None:
        super().__init__()
        self.size = size

    def fstat(self, descriptor: int) -> os.stat_result:
        found = tuple(os.fstat(descriptor))
        return os.stat_result((*found[:6], self.size, *found[7:10]))


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> OsSpy:
    watched = OsSpy()
    monkeypatch.setattr(readback, "os", watched)
    return watched


def _session(tmp_path: Path) -> Path:
    session = tmp_path / "session"
    session.mkdir()
    return session


def _copy_video(session: Path, owner: int) -> int | None:
    """The mp4's read-back as the orchestrator makes it, into a file beside it."""
    target = session.parent / "copied.mp4"
    return recording.copy_output(
        session,
        recording.VIDEO_FILE,
        lambda: target.open("wb"),
        owner=owner,
        limit=recording.MAX_VIDEO_BYTES,
    )


def _read_chapters(session: Path, owner: int) -> list[tuple[str, str]]:
    return recording.chapters(session, owner=owner)


READ_BACKS = [
    pytest.param(
        recording.VIDEO_FILE, recording.MAX_VIDEO_BYTES, _copy_video, id="mp4"
    ),
    pytest.param(
        recording.SEGMENTS_FILE,
        recording.MAX_SEGMENTS_BYTES,
        _read_chapters,
        id="segments",
    ),
]


def _outside(tmp_path: Path) -> Path:
    """A file outside the session that a valid segment list could be read from."""
    outside = tmp_path / "outside" / "root-only.json"
    outside.parent.mkdir()
    segment = {"kind": "footage", "label": SECRET, "out_start": 1.0}
    outside.write_text(json.dumps({"segments": [segment]}), encoding="utf-8")
    return outside


def _promptly(call: Callable[[], object], fifo: Path) -> BaseException | None:
    """What ``call`` raised, or None; a failing test rather than a hung one.

    A read-side open of a FIFO that does not ask for ``O_NONBLOCK`` waits for a
    writer. So the call runs on a thread, and when it has not returned in ten
    seconds the FIFO is opened for writing, which releases it, and the test fails.
    """
    outcome: list[BaseException | None] = []

    def target() -> None:
        try:
            call()
        except BaseException as error:
            outcome.append(error)
        else:
            outcome.append(None)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(10)
    if worker.is_alive():
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(10)
        pytest.fail(f"reading {fifo.name} back waited for a writer on the FIFO")
    return outcome[0]


@pytest.mark.unit
@pytest.mark.parametrize(("name", "limit", "read_back"), READ_BACKS)
def test_a_symlink_in_place_of_an_output_is_refused_and_its_target_never_opened(
    tmp_path: Path,
    spy: OsSpy,
    name: str,
    limit: int,
    read_back: Callable[[Path, int], object],
) -> None:
    """Render owns the session directory and the orchestrator reading it is root,
    so a link followed there would hand root's own files to the job's artifacts."""
    session = _session(tmp_path)
    (session / name).symlink_to(_outside(tmp_path))
    with pytest.raises(recording.RecorderError) as raised:
        read_back(session, os.geteuid())
    assert str(raised.value) == (
        f"render's {name} is refused: it is a symbolic link, which is never followed"
    )
    assert spy.opened == [(str(session), None), (name, spy.descriptors[0])]
    assert spy.reads == 0
    assert not (tmp_path / "copied.mp4").exists()


@pytest.mark.unit
@pytest.mark.parametrize(("name", "limit", "read_back"), READ_BACKS)
def test_a_fifo_in_place_of_an_output_is_refused_without_waiting_for_a_writer(
    tmp_path: Path,
    spy: OsSpy,
    name: str,
    limit: int,
    read_back: Callable[[Path, int], object],
) -> None:
    session = _session(tmp_path)
    os.mkfifo(session / name)
    raised = _promptly(lambda: read_back(session, os.geteuid()), session / name)
    assert isinstance(raised, recording.RecorderError)
    assert str(raised) == f"render's {name} is refused: it is not a regular file"
    assert spy.opened == [(str(session), None), (name, spy.descriptors[0])]
    assert spy.reads == 0
    assert not (tmp_path / "copied.mp4").exists()


@pytest.mark.unit
@pytest.mark.parametrize(("name", "limit", "read_back"), READ_BACKS)
def test_an_output_over_its_bound_is_refused_before_a_byte_is_read(
    tmp_path: Path,
    spy: OsSpy,
    name: str,
    limit: int,
    read_back: Callable[[Path, int], object],
) -> None:
    """The file is sparse, so the bound is passed without writing it."""
    session = _session(tmp_path)
    with (session / name).open("wb") as handle:
        handle.truncate(limit + 1)
    with pytest.raises(recording.RecorderError) as raised:
        read_back(session, os.geteuid())
    assert str(raised.value) == (
        f"render's {name} is refused: it is {limit + 1} bytes, over its bound of "
        f"{limit}"
    )
    assert spy.reads == 0
    assert not (tmp_path / "copied.mp4").exists()


@pytest.mark.unit
@pytest.mark.parametrize(("name", "limit", "read_back"), READ_BACKS)
def test_an_output_of_any_uid_but_renders_is_refused(
    tmp_path: Path,
    spy: OsSpy,
    name: str,
    limit: int,
    read_back: Callable[[Path, int], object],
) -> None:
    """A hard link render made to another user's file is that user's file."""
    session = _session(tmp_path)
    (session / name).write_text(json.dumps({"segments": []}), encoding="utf-8")
    mine = os.geteuid()
    with pytest.raises(recording.RecorderError) as raised:
        read_back(session, mine + 1)
    assert str(raised.value) == (
        f"render's {name} is refused: it belongs to uid {mine}, not to render's "
        f"uid {mine + 1}"
    )
    assert spy.reads == 0


@pytest.mark.unit
@pytest.mark.parametrize(("name", "limit", "read_back"), READ_BACKS)
def test_a_directory_in_place_of_an_output_is_refused(
    tmp_path: Path,
    spy: OsSpy,
    name: str,
    limit: int,
    read_back: Callable[[Path, int], object],
) -> None:
    session = _session(tmp_path)
    (session / name).mkdir()
    with pytest.raises(recording.RecorderError) as raised:
        read_back(session, os.geteuid())
    assert str(raised.value) == f"render's {name} is refused: it is not a regular file"
    assert spy.reads == 0


@pytest.mark.unit
@pytest.mark.parametrize(("name", "limit", "read_back"), READ_BACKS)
def test_a_session_directory_that_is_a_link_is_refused_before_anything_in_it(
    tmp_path: Path,
    spy: OsSpy,
    name: str,
    limit: int,
    read_back: Callable[[Path, int], object],
) -> None:
    elsewhere = _session(tmp_path)
    (elsewhere / name).write_text(_outside(tmp_path).read_text(), encoding="utf-8")
    link = tmp_path / "linked-session"
    link.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(recording.RecorderError) as raised:
        read_back(link, os.geteuid())
    assert str(raised.value) == (
        "render's session directory is refused: it is a symbolic link, which is "
        "never followed"
    )
    assert spy.opened == [(str(link), None)]
    assert spy.reads == 0


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["file", "fifo"])
def test_a_session_directory_that_is_not_one_is_refused(
    tmp_path: Path, kind: str
) -> None:
    session = tmp_path / "session"
    if kind == "fifo":
        os.mkfifo(session)
    else:
        session.write_bytes(b"")
    raised = _promptly(
        lambda: recording.read_output(
            session, recording.SEGMENTS_FILE, owner=os.geteuid(), limit=16
        ),
        session,
    )
    assert isinstance(raised, recording.RecorderError)
    assert str(raised) == "render's session directory is refused: it is not a directory"


@pytest.mark.unit
def test_an_output_is_read_and_copied_from_the_descriptor_that_was_checked(
    tmp_path: Path, spy: OsSpy
) -> None:
    session = _session(tmp_path)
    (session / recording.VIDEO_FILE).write_bytes(b"\0\1mp4")
    assert _copy_video(session, os.geteuid()) == 5
    assert (tmp_path / "copied.mp4").read_bytes() == b"\0\1mp4"
    assert spy.opened == [
        (str(session), None),
        (recording.VIDEO_FILE, spy.descriptors[0]),
    ]
    owner = os.geteuid()
    read = recording.read_output(session, recording.VIDEO_FILE, owner=owner, limit=5)
    assert read == b"\0\1mp4"
    with pytest.raises(recording.RecorderError, match="over its bound of 4$"):
        recording.read_output(session, recording.VIDEO_FILE, owner=owner, limit=4)


@pytest.mark.unit
def test_an_absent_output_is_none_and_no_target_is_opened(tmp_path: Path) -> None:
    session = _session(tmp_path)
    targets: list[str] = []

    def opener() -> Any:
        targets.append("opened")
        return (tmp_path / "copied.mp4").open("wb")

    owner = os.geteuid()
    copied = recording.copy_output(
        session, recording.VIDEO_FILE, opener, owner=owner, limit=16
    )
    assert copied is None
    read = recording.read_output(
        session, recording.SEGMENTS_FILE, owner=owner, limit=16
    )
    assert read is None
    assert recording.read_output(tmp_path / "gone", "x", owner=owner, limit=16) is None
    assert targets == []


@pytest.mark.unit
def test_a_file_that_grows_after_its_check_is_read_only_as_far_as_it_was_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process render left behind can still be writing to the file, so the size
    the check saw, which is within the bound, is all that is ever read."""
    session = _session(tmp_path)
    (session / recording.VIDEO_FILE).write_bytes(b"checked-and-then-more")
    monkeypatch.setattr(readback, "os", CheckedSmaller(len(b"checked")))
    read = recording.read_output(
        session, recording.VIDEO_FILE, owner=os.geteuid(), limit=64
    )
    assert read == b"checked"


@pytest.mark.unit
@pytest.mark.parametrize("name", ["", ".", "..", "../review.mp4", "a/review.mp4"])
def test_a_read_back_names_one_file_directly_in_the_session(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(ValueError, match="is not the name of a file in the session"):
        recording.read_output(tmp_path, name, owner=os.geteuid(), limit=16)


@pytest.mark.unit
def test_render_uid_is_the_users_when_render_runs_as_it_and_this_processes_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []

    def ids(user: str) -> tuple[int, int]:
        asked.append(user)
        return 10003, 20003

    monkeypatch.setattr(recording, "_ids_of", ids)
    monkeypatch.setattr(processes, "can_switch_user", lambda: True)
    assert recording.render_uid("uxrender") == 10003
    assert recording.render_uid(None) == os.geteuid()
    monkeypatch.setattr(processes, "can_switch_user", lambda: False)
    assert recording.render_uid("uxrender") == os.geteuid()
    assert asked == ["uxrender"]


@pytest.mark.unit
def test_render_uid_names_install_sh_when_the_user_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_such_user(user: str) -> tuple[int, int]:
        raise KeyError(user)

    monkeypatch.setattr(recording, "_ids_of", no_such_user)
    monkeypatch.setattr(processes, "can_switch_user", lambda: True)
    with pytest.raises(recording.RecorderError) as raised:
        recording.render_uid("uxrender")
    assert str(raised.value) == (
        "render's user uxrender does not exist; scripts/ux_runner/install.sh creates it"
    )


@pytest.mark.unit
def test_the_segments_file_is_the_one_render_writes_beside_the_mp4() -> None:
    render = (SCRIPTS / "ux_recorder_render.py").read_text(encoding="utf-8")
    assert f'(session_dir / "{recording.SEGMENTS_FILE}").write_text(' in render


@pytest.mark.unit
def test_write_review_md_writes_the_file_render_reads_its_findings_from(
    tmp_path: Path,
) -> None:
    path = recording.write_review_md(tmp_path, "## Findings\n- Rotate has no label\n")
    assert path == tmp_path / "review.md"
    assert path.read_text() == "## Findings\n- Rotate has no label\n"


RENDER_HOME = Path("/run/ux/render-home")
SCOPED = {
    "AWS_ACCESS_KEY_ID": "ASIAPOLLYKEY0EXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "polly-scoped-key-material",
    "AWS_SESSION_TOKEN": "polly-scoped-session-value",
}
ACCESS_DENIED = (
    "Traceback (most recent call last):\n"
    "botocore.exceptions.ClientError: An error occurred (AccessDeniedException) "
    "when calling the SynthesizeSpeech operation: User: arn:aws:sts::123456789012:"
    "assumed-role/idp-sdlc-GitLab/ux-test-812-render is not authorized\n"
)


class FakeRunner:
    """``processes.run_in_group`` as render_video uses it, playing scripted exits.

    Each answer is ``(returncode, stdout, stderr, timed_out)``; a zero exit writes
    the video, as the real render does, unless ``write_video`` is off.
    """

    def __init__(
        self,
        answers: list[tuple[int, str, str, bool]],
        *,
        write_video: bool = True,
        raises: BaseException | None = None,
    ) -> None:
        self.answers = answers
        self.write_video = write_video
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append({"argv": list(argv), **kwargs})
        if self.raises is not None:
            raise self.raises
        code, out, err, timed_out = self.answers.pop(0)
        if code == 0 and not timed_out and self.write_video:
            session = Path(argv[argv.index("render") + 1])
            (session / recording.VIDEO_FILE).write_bytes(b"\0")
        return processes.Completed(
            code,
            out,
            err,
            timed_out,
            1.0,
            stdout_bytes=out.encode(),
            stderr_bytes=err.encode(),
        )


def _render(
    tmp_path: Path,
    runner: FakeRunner,
    *,
    narration: bool = True,
    aws: dict[str, str] | None = None,
    missing: list[str] | None = None,
) -> tuple[Path | None, str]:
    return recording.render_video(
        tmp_path,
        harness_root=HARNESS,
        voice="Ruth",
        region="us-east-1",
        narration=narration,
        aws=SCOPED if aws is None else aws,
        home=RENDER_HOME,
        log_path=tmp_path / "render.log",
        timeout=900.0,
        python="/usr/local/bin/python3",
        runner=runner,
        missing_tools=lambda env: list(missing or []),
    )


@pytest.mark.unit
def test_render_gets_the_recorders_variables_the_region_and_one_session_only() -> None:
    env = recording.render_env(RENDER_HOME, region="us-east-1", aws=SCOPED)
    assert env == {
        **recording.recorder_env(RENDER_HOME),
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        **SCOPED,
    }
    assert set(env) == {
        "PATH",
        "HOME",
        "LANG",
        "TZ",
        "PYTHONDONTWRITEBYTECODE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_EC2_METADATA_DISABLED",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "name", ["GITLAB_REVIEW_TOKEN", "UX_TEST_PERSONAS", "AWS_PROFILE", "HTTPS_PROXY"]
)
def test_render_env_refuses_any_name_but_the_three_of_an_aws_session(
    name: str,
) -> None:
    with pytest.raises(recording.RecorderError) as raised:
        recording.render_env(
            RENDER_HOME, region="us-east-1", aws={**SCOPED, name: "value-9f2c"}
        )
    assert name in str(raised.value)
    assert "value-9f2c" not in str(raised.value)


@pytest.mark.unit
def test_render_env_drops_an_empty_session_name_rather_than_sending_it() -> None:
    env = recording.render_env(
        RENDER_HOME, region="us-east-1", aws={**SCOPED, "AWS_SESSION_TOKEN": ""}
    )
    assert "AWS_SESSION_TOKEN" not in env
    assert env["AWS_ACCESS_KEY_ID"] == SCOPED["AWS_ACCESS_KEY_ID"]


@pytest.mark.unit
def test_a_narrated_render_runs_the_recorders_render_isolated_in_its_own_group(
    tmp_path: Path,
) -> None:
    runner = FakeRunner([(0, "rendering\n", "", False)])
    assert _render(tmp_path, runner) == (tmp_path / recording.VIDEO_FILE, "on")
    (call,) = runner.calls
    assert call["argv"] == [
        "/usr/local/bin/python3",
        "-I",
        "-B",
        "/harness/scripts/ux_recorder.py",
        "render",
        str(tmp_path),
        "--voice",
        "Ruth",
        "--region",
        "us-east-1",
        "--engine",
        "generative",
    ]
    assert call["env"] == recording.render_env(
        RENDER_HOME, region="us-east-1", aws=SCOPED
    )
    assert call["cwd"] == RENDER_HOME
    assert call["timeout"] == 900.0
    assert call["input_text"] == ""


@pytest.mark.unit
def test_render_without_narration_is_handed_no_aws_session(tmp_path: Path) -> None:
    runner = FakeRunner([(0, "", "", False)])
    assert _render(tmp_path, runner, narration=False) == (
        tmp_path / recording.VIDEO_FILE,
        "off",
    )
    (call,) = runner.calls
    assert call["argv"][-1] == "--no-narration"
    assert not {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    } & set(call["env"])
    assert call["env"]["AWS_EC2_METADATA_DISABLED"] == "true"


@pytest.mark.unit
def test_a_search_path_replaces_only_the_childs_path(tmp_path: Path) -> None:
    """A host whose ffmpeg is outside the fixed PATH, as Homebrew's is on a Mac."""
    found = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    env = recording.render_env(
        RENDER_HOME, region="us-east-1", aws=SCOPED, search_path=found
    )
    plain = recording.render_env(RENDER_HOME, region="us-east-1", aws=SCOPED)
    assert env == {**plain, "PATH": found}
    runner = FakeRunner([(1, "", ACCESS_DENIED, False), (0, "", "", False)])
    recording.render_video(
        tmp_path,
        harness_root=HARNESS,
        voice="Ruth",
        region="us-east-1",
        narration=True,
        aws=SCOPED,
        home=RENDER_HOME,
        log_path=tmp_path / "render.log",
        timeout=900.0,
        search_path=found,
        runner=runner,
        missing_tools=lambda env: [],
    )
    assert [call["env"]["PATH"] for call in runner.calls] == [found, found]


@pytest.mark.unit
def test_the_render_argv_is_one_the_recorders_own_parser_takes(tmp_path: Path) -> None:
    import ux_recorder

    for narrated in (True, False):
        argv = recording.render_argv(
            harness_root=HARNESS,
            session_dir=tmp_path,
            voice="Ruth",
            region="us-west-2",
            narrated=narrated,
            python="/usr/local/bin/python3",
        )
        args = ux_recorder.build_parser().parse_args(argv[argv.index("render") :])
        assert args.func is ux_recorder.cmd_render
        assert Path(args.session_dir) == tmp_path
        assert (args.voice, args.region, args.engine) == (
            "Ruth",
            "us-west-2",
            "generative",
        )
        assert args.no_narration is (not narrated)


@pytest.mark.unit
def test_the_video_file_is_the_one_render_writes_for_a_review_session() -> None:
    import ux_recorder_render

    assert recording.VIDEO_FILE == ux_recorder_render.output_stem("review") + ".mp4"


@pytest.mark.unit
def test_render_refuses_to_start_without_all_four_tools(tmp_path: Path) -> None:
    runner = FakeRunner([])
    with pytest.raises(recording.RecorderError) as raised:
        _render(tmp_path, runner, missing=["ffprobe", "Pillow"])
    assert str(raised.value) == (
        "render needs ffmpeg, ffprobe, boto3 and Pillow; missing: ffprobe, Pillow"
    )
    assert runner.calls == []


@pytest.mark.unit
def test_the_tool_check_looks_on_the_childs_path_and_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def started(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the tool check started a process in the orchestrator")

    monkeypatch.setattr(subprocess, "Popen", started)
    monkeypatch.setattr(
        recording.importlib.util,
        "find_spec",
        lambda name: None if name == "PIL" else object(),
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        (tools / name).write_text("#!/bin/sh\n")
        os.chmod(tools / name, 0o755)
    assert recording.render_tools_missing({"PATH": str(tools)}) == ["Pillow"]
    assert recording.render_tools_missing({"PATH": str(tmp_path / "empty")}) == [
        "ffmpeg",
        "ffprobe",
        "Pillow",
    ]


@pytest.mark.unit
def test_polly_access_denied_renders_once_more_without_narration_or_session(
    tmp_path: Path,
) -> None:
    runner = FakeRunner([(1, "", ACCESS_DENIED, False), (0, "", "", False)])
    path, state = _render(tmp_path, runner)
    assert (path, state) == (
        tmp_path / recording.VIDEO_FILE,
        "unavailable (AccessDenied)",
    )
    first, second = runner.calls
    assert "--no-narration" not in first["argv"]
    assert second["argv"] == [*first["argv"], "--no-narration"]
    assert first["env"]["AWS_SESSION_TOKEN"] == SCOPED["AWS_SESSION_TOKEN"]
    assert second["env"] == recording.render_env(
        RENDER_HOME, region="us-east-1", aws={}
    )
    log = (tmp_path / "render.log").read_text(encoding="utf-8")
    assert "AccessDeniedException" in log
    assert log.endswith(
        "Polly answered AccessDenied; rendering once more without narration\n"
    )


@pytest.mark.unit
def test_a_retry_without_narration_that_also_fails_is_a_failure(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        [(1, "", ACCESS_DENIED, False), (1, "", "ffmpeg exited 1", False)]
    )
    with pytest.raises(recording.RecorderError) as raised:
        _render(tmp_path, runner)
    assert str(raised.value) == "render failed (exit 1); its log is private"
    assert len(runner.calls) == 2


@pytest.mark.unit
def test_access_denied_without_narration_is_a_plain_failure(tmp_path: Path) -> None:
    runner = FakeRunner(
        [(1, "", "PermissionError: AccessDenied writing the mp4", False)]
    )
    with pytest.raises(recording.RecorderError, match=r"^render failed \(exit 1\)"):
        _render(tmp_path, runner, narration=False)
    assert len(runner.calls) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "SystemExit: no frames outside paused intervals; nothing to render",
        "ValueError: role/idp-sdlc-GitLab in account 123456789012",
    ],
)
def test_any_other_render_failure_names_only_its_exit_status(
    tmp_path: Path, stderr: str
) -> None:
    runner = FakeRunner([(1, "", stderr, False)])
    with pytest.raises(recording.RecorderError) as raised:
        _render(tmp_path, runner)
    assert str(raised.value) == "render failed (exit 1); its log is private"
    assert stderr in (tmp_path / "render.log").read_text(encoding="utf-8")


@pytest.mark.unit
def test_a_render_past_its_timeout_fails_without_a_retry(tmp_path: Path) -> None:
    """The group is killed by run_in_group; a timed-out narration is not retried."""
    runner = FakeRunner([(-9, "", ACCESS_DENIED, True)])
    with pytest.raises(recording.RecorderError) as raised:
        _render(tmp_path, runner)
    assert str(raised.value) == "render did not finish within 900 s"
    assert len(runner.calls) == 1


@pytest.mark.unit
def test_a_render_that_cannot_be_started_is_a_failure(tmp_path: Path) -> None:
    runner = FakeRunner([], raises=FileNotFoundError("/usr/local/bin/python3"))
    with pytest.raises(recording.RecorderError) as raised:
        _render(tmp_path, runner)
    assert str(raised.value) == "render could not be started: FileNotFoundError"


@pytest.mark.unit
def test_a_clean_exit_that_wrote_no_video_returns_no_path(tmp_path: Path) -> None:
    runner = FakeRunner([(0, "", "", False)], write_video=False)
    assert _render(tmp_path, runner) == (None, "on")


@pytest.mark.unit
@pytest.mark.parametrize("left", ["fifo", "dangling link"])
def test_whatever_a_clean_exit_left_by_the_videos_name_goes_to_the_read_back(
    tmp_path: Path, left: str
) -> None:
    """The read-back refuses a pipe or a link there by name (``copy_output``), so
    it is not mistaken for render having written no video at all."""
    video = tmp_path / recording.VIDEO_FILE
    if left == "fifo":
        os.mkfifo(video)
    else:
        video.symlink_to(tmp_path / "nowhere.mp4")
    runner = FakeRunner([(0, "", "", False)], write_video=False)
    assert _render(tmp_path, runner) == (video, "on")


@pytest.mark.unit
def test_the_render_log_goes_to_a_private_file(tmp_path: Path) -> None:
    clicks = 'clicks: button#rotate "Rotate 90°"\n'
    runner = FakeRunner([(0, clicks, "", False)])
    _render(tmp_path, runner)
    log = tmp_path / "render.log"
    assert log.read_text(encoding="utf-8") == clicks
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


@pytest.mark.unit
@pytest.mark.parametrize(("out", "err"), [(b"x", b"z"), (b"\xff", b"\xfe")])
def test_a_render_that_floods_its_output_leaves_a_bounded_private_log(
    tmp_path: Path, out: bytes, err: bytes
) -> None:
    """A real child writes 3 MiB to each stream through the real group runner:
    ``render.log`` gets only what the bound kept of each, with a line saying how
    much more was discarded, so neither this process's memory nor the job's disk
    pays for the rest. Output that is not UTF-8 is written as the bytes kept, not
    as their decoding, which is three bytes for every one of them."""
    flood = 3 * 1024 * 1024
    limit = 4096
    home = tmp_path / "render-home"
    home.mkdir()
    writes = (
        "import os\n"
        f"for _ in range({flood // 65536}):\n"
        f"    os.write(1, {out!r} * 65536)\n"
        f"    os.write(2, {err!r} * 65536)\n"
    )

    def runner(argv: list[str], **kwargs: Any) -> processes.Completed:
        return processes.run_in_group(
            [sys.executable, "-c", writes], output_limit=limit, **kwargs
        )

    path, state = recording.render_video(
        tmp_path,
        harness_root=HARNESS,
        voice="Ruth",
        region="us-east-1",
        narration=False,
        aws={},
        home=home,
        log_path=tmp_path / "render.log",
        timeout=60.0,
        runner=runner,
        missing_tools=lambda env: [],
    )
    assert (path, state) == (None, "off")
    log = (tmp_path / "render.log").read_bytes()
    assert log.count(out) == limit and log.count(err) == limit
    for name in ("stdout", "stderr"):
        assert (
            f"render's {name}: {flood - limit} bytes past the capture bound "
            "discarded".encode()
        ) in log
    assert len(log) < 2 * limit + 400


def _render_as(
    tmp_path: Path,
    runner: FakeRunner,
    give: Callable[[Path, str], None],
    *,
    user: str | None = "uxrender",
) -> tuple[Path | None, str]:
    return recording.render_video(
        tmp_path,
        harness_root=HARNESS,
        voice="Ruth",
        region="us-east-1",
        narration=True,
        aws=SCOPED,
        home=tmp_path / "render-home",
        log_path=tmp_path / "render.log",
        timeout=900.0,
        user=user,
        runner=runner,
        missing_tools=lambda env: [],
        give=give,
    )


@pytest.mark.unit
def test_render_runs_as_its_own_user_once_the_session_and_home_are_handed_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child of root's can read root's ``/proc/<pid>/environ``, which holds the
    token, the personas and the job's AWS session; one of uxrender's cannot."""
    monkeypatch.setattr(processes, "can_switch_user", lambda: True)
    handed: list[tuple[Path, str]] = []
    runner = FakeRunner([(0, "", "", False)])
    _render_as(tmp_path, runner, lambda path, user: handed.append((path, user)))
    assert handed == [(tmp_path, "uxrender"), (tmp_path / "render-home", "uxrender")]
    assert [call["user"] for call in runner.calls] == ["uxrender"]


@pytest.mark.unit
def test_the_retry_without_narration_runs_as_the_same_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processes, "can_switch_user", lambda: True)
    runner = FakeRunner([(1, "", ACCESS_DENIED, False), (0, "", "", False)])
    _render_as(tmp_path, runner, lambda path, user: None)
    assert [call["user"] for call in runner.calls] == ["uxrender", "uxrender"]


@pytest.mark.unit
def test_render_runs_as_the_current_user_when_the_harness_cannot_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processes, "can_switch_user", lambda: False)
    handed: list[tuple[Path, str]] = []
    runner = FakeRunner([(0, "", "", False)])
    _render_as(tmp_path, runner, lambda path, user: handed.append((path, user)))
    assert handed == []
    assert [call["user"] for call in runner.calls] == [None]


@pytest.mark.unit
def test_a_session_that_cannot_be_handed_over_is_not_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processes, "can_switch_user", lambda: True)

    def no_such_user(path: Path, user: str) -> None:
        raise KeyError(user)

    runner = FakeRunner([(0, "", "", False)])
    with pytest.raises(recording.RecorderError) as raised:
        _render_as(tmp_path, runner, no_such_user)
    assert "install.sh" in str(raised.value) and "KeyError" in str(raised.value)
    assert runner.calls == []


@pytest.mark.unit
def test_hand_over_changes_every_entry_itself_and_follows_no_link(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "session"
    (tree / "frames").mkdir(parents=True)
    (tree / "frames" / "000001.jpg").write_bytes(b"\xff\xd8")
    (tree / "timeline.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    (tree / "link").symlink_to(outside, target_is_directory=True)
    calls: list[tuple[Path, int, int, bool]] = []

    def chown(path: Path, uid: int, gid: int, *, follow_symlinks: bool) -> None:
        calls.append((Path(path), uid, gid, follow_symlinks))

    recording.hand_over(tree, "uxrender", ids=lambda user: (10003, 10003), chown=chown)
    assert sorted(path for path, *_ in calls) == sorted(
        [
            tree,
            tree / "frames",
            tree / "frames" / "000001.jpg",
            tree / "timeline.json",
            tree / "link",
        ]
    )
    assert {(uid, gid, follow) for _, uid, gid, follow in calls} == {
        (10003, 10003, False)
    }


@pytest.mark.unit
def test_render_is_handed_to_the_users_uid_and_its_same_named_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``install.sh`` makes each tool's user with ``--user-group``, so the group a
    session is handed to is looked up by the user's own name, not its primary gid."""
    import grp
    import pwd

    asked: list[tuple[str, str]] = []

    class Entry:
        pw_uid = 10003
        gr_gid = 20003

    def by_user(name: str) -> Entry:
        asked.append(("user", name))
        return Entry()

    def by_group(name: str) -> Entry:
        asked.append(("group", name))
        return Entry()

    monkeypatch.setattr(pwd, "getpwnam", by_user)
    monkeypatch.setattr(grp, "getgrnam", by_group)
    assert recording._ids_of("uxrender") == (10003, 20003)
    assert asked == [("user", "uxrender"), ("group", "uxrender")]
