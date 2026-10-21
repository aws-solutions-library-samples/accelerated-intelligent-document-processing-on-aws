# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The harness's control of ``scripts/ux_recorder.py``: start, status, stop, render.

The recorder is the same one an interactive ux-test or product-demo session
uses, driven the same way: its ``start``, ``stop`` and ``render`` subcommands, and
its control socket for ``status``. Nothing here re-implements it; this module is
the narrow, testable seam between the orchestrator and that tool.

Properties it is built around
-----------------------------
* **A minimal environment.** The recorder is started with :func:`recorder_env`:
  a ``PATH``, a ``HOME``, a locale and a timezone. It needs no credential, so it
  is given none, and in particular never the job's AWS session or the GitLab
  token.
* **Render is a child of its own, as a user of its own, holding one Polly session
  and nothing else.** Render decodes the page's frames with Pillow and ffmpeg and
  turns the model's narration into speech, so it is the part of the recording that
  handles the most MR-controlled data. In the orchestrator's own process it would
  share an address space and an environment with ``GITLAB_REVIEW_TOKEN``, the
  personas and the job's AWS session, and every ffmpeg it starts would inherit
  them. So :func:`render_video` runs ``ux_recorder.py render`` under ``python3 -I
  -B`` in its own process group with :func:`render_env`: the recorder's variables,
  the region, instance metadata switched off, and at most the three names of the
  one AWS session it is handed, which the orchestrator mints for Polly alone. A
  child of root's could still read the orchestrator's ``/proc/<pid>/environ``,
  which holds all of those, so when the harness can switch users render runs as
  ``uxrender`` (``paths.RENDER_USER``), and the session directory and render's
  home are handed to that user first (:func:`hand_over`). When it cannot switch,
  render runs as the current user, and that bound is the caller's to state.
* **Render's outputs are read back as its own files, never through a link.** Once
  the session directory is render's, whatever it leaves under a name the
  orchestrator reads back is render's to choose, and the orchestrator is root. So
  :func:`read_output` and :func:`copy_output` read through ``readback``, which
  opens the directory without following a link, opens the name relative to that
  descriptor with ``O_NOFOLLOW`` and ``O_NONBLOCK``, and goes on only when
  ``fstat`` of the open file says it is a regular file of render's uid
  (:func:`render_uid`) within its bound; then it reads that descriptor, at most
  the size the check saw. A link to a file of root's, a pipe that would hold the
  read open, a file of another uid and a file big enough to fill the job's disk
  are each refused with a :class:`RecorderError`, and nothing outside the
  directory is opened by name. The egress proxy's outputs are read back the same
  way (``egress``).
* **Stop always finishes.** :func:`stop` passes both ``--out`` and ``--session``,
  which makes the CLI stop a live recorder or, when the daemon is gone, finalise
  the session from what is on disk. When even that fails it kills the daemon and
  tries once more, because a recording that never reaches ``timeline.json`` can
  never be rendered.
* **Render logs stay private, and bounded.** Click labels in the render log are
  page text, so :func:`render_video` writes the child's output to a ``0o600`` file
  the orchestrator never copies into the job's artifacts, and its errors name only
  the exit status. What is written is the bytes ``processes.run_in_group`` kept,
  as render wrote them, at most ``processes.MAX_OUTPUT_BYTES`` of each stream,
  with a line saying how much more was discarded, so a render that floods its
  output costs the orchestrator neither its memory nor the job's disk. Writing
  the decoded text instead would let output that is not UTF-8 triple in size.
* **The recorder's own subcommands are bounded the same way.** :func:`start` and
  :func:`stop` run the recorder's CLI through ``processes.run_in_group``, so its
  output is kept to that bound too, and a subcommand that overruns its timeout is
  killed with its process group. The daemon ``start`` launches is a session of
  its own and outlives it.
* **Narration degrades rather than failing.** When Polly refuses with
  ``AccessDenied``, because the GitLab role's ``polly:SynthesizeSpeech`` grant has
  not reached the live stack yet, the video is rendered once more without
  narration, and without the session, and reported as ``unavailable
  (AccessDenied)``.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

from . import paths, processes, readback

#: What ``render`` needs, in the spelling of ``ux_recorder_render.check_tools``.
_RENDER_TOOLS = ("ffmpeg", "ffprobe", "boto3", "Pillow")
#: The executables among them, looked up on the child's ``PATH``.
_RENDER_BINARIES = ("ffmpeg", "ffprobe")
#: The Python packages among them: (importable name, name in the message).
_RENDER_PACKAGES = (("boto3", "boto3"), ("PIL", "Pillow"))
#: The names of the one AWS session render may be handed.
_AWS_ENV_NAMES = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
#: ``ux_recorder_render.output_stem("review")`` plus ``.mp4``, the file a review
#: session renders to, named here so render is never imported into this process.
VIDEO_FILE = "review.mp4"
#: The segment list render writes beside the mp4, which :func:`chapters` reads.
SEGMENTS_FILE = "segments.json"
#: The largest mp4 :func:`copy_output` is asked to copy out of a session, over ten
#: times the 95 MiB the job uploads by default: a bound on the job's disk, not on
#: the video, whose upload limit is ``ai_ux_test.py --max-upload-mb``.
MAX_VIDEO_BYTES = 1024 * 1024 * 1024
#: The largest ``segments.json`` :func:`chapters` reads. Forty marks and the two
#: cards come to tens of KiB.
MAX_SEGMENTS_BYTES = 1024 * 1024
#: ``ux_recorder.CURRENT_FILE``, read here without importing the recorder, so
#: :func:`kill` works even when the recorder module cannot be imported.
_CURRENT_FILE = "current.json"
_STDERR_TAIL = 400
_KILL_POLL_SECONDS = 0.1


class RecorderError(Exception):
    """The recorder could not be started, stopped or rendered; the text says which."""


#: Render, as a refusal of one of its outputs names it.
_RENDER = readback.Writer("render's", "session directory", RecorderError)


def recorder_env(home: Path) -> dict[str, str]:
    """The recorder's whole environment."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def render_env(
    home: Path,
    *,
    region: str,
    aws: Mapping[str, str],
    search_path: str | None = None,
) -> dict[str, str]:
    """Render's whole environment: the recorder's, the region, and one AWS session.

    ``aws`` may carry only ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY`` and
    ``AWS_SESSION_TOKEN``, and anything else is refused, so a caller cannot widen
    what render holds by passing a whole environment. Instance metadata is
    switched off, so a render handed no session fails to narrate rather than
    finding the host's role, and ``home`` should be an empty private directory,
    so no shared config or credentials file is found either. ``search_path``
    replaces the recorder's ``PATH``, for a host whose ffmpeg is elsewhere.
    """
    foreign = sorted(set(aws) - set(_AWS_ENV_NAMES))
    if foreign:
        raise RecorderError(
            "render takes only AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and "
            f"AWS_SESSION_TOKEN; refused: {', '.join(foreign)}"
        )
    env = recorder_env(home)
    if search_path:
        env["PATH"] = search_path
    env.update(
        AWS_REGION=region,
        AWS_DEFAULT_REGION=region,
        AWS_EC2_METADATA_DISABLED="true",
    )
    env.update({name: aws[name] for name in _AWS_ENV_NAMES if aws.get(name)})
    return env


def render_tools_missing(env: Mapping[str, str]) -> list[str]:
    """Which of ffmpeg, ffprobe, boto3 and Pillow render would not find.

    Found without running anything in this process, which holds the job's
    secrets: the two binaries on the child's own ``PATH``, and the two packages
    as import specs of this interpreter, which is the one render runs on.
    """
    missing = [
        name
        for name in _RENDER_BINARIES
        if shutil.which(name, path=env.get("PATH", "")) is None
    ]
    missing += [
        shown
        for module, shown in _RENDER_PACKAGES
        if not importlib.util.find_spec(module)
    ]
    return [name for name in _RENDER_TOOLS if name in missing]


def _recorder_script(harness_root: Path) -> Path:
    return harness_root / "scripts" / "ux_recorder.py"


def _invoke(
    run: Callable[..., processes.Completed],
    argv: Sequence[str],
    env: Mapping[str, str],
    timeout: float,
) -> processes.Completed | str:
    """Run one recorder subcommand; a string says why it could not complete.

    ``run`` is :func:`processes.run_in_group`, so the subcommand's output is kept
    to that function's bound, its stdin is empty, and one that overruns
    ``timeout`` is killed with its process group.
    """
    try:
        completed = run(
            list(argv), input_text="", env=dict(env), cwd=None, timeout=timeout
        )
    except OSError as exc:
        return f"could not be run: {type(exc).__name__}"
    if completed.timed_out:
        return f"no answer within {timeout:g} s"
    return completed


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _tail(text: str) -> str:
    return text.strip()[-_STDERR_TAIL:]


def start(
    *,
    harness_root: Path,
    out_dir: Path,
    stack: str,
    persona: str,
    flows: Sequence[str],
    target_id: str,
    say: str,
    env: Mapping[str, str],
    python: str = sys.executable,
    port: int = paths.CDP_PORT,
    run: Callable[..., processes.Completed] = processes.run_in_group,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Start recording the page target ``target_id``; returns the recorder's reply.

    The reply is the ``start`` subcommand's JSON, which names the session
    directory, the control socket and the daemon's pid.
    """
    argv = [
        python,
        str(_recorder_script(harness_root)),
        "start",
        "--kind",
        "review",
        "--stack",
        stack,
        "--persona",
        persona,
    ]
    for flow in flows:
        argv += ["--flow", flow]
    argv += [
        "--target",
        target_id,
        "--port",
        str(port),
        "--out",
        str(out_dir),
        "--say",
        say,
    ]
    completed = _invoke(run, argv, env, timeout)
    if isinstance(completed, str):
        raise RecorderError(f"the recorder did not start: {completed}")
    if completed.returncode != 0:
        raise RecorderError(
            f"the recorder did not start (exit {completed.returncode}): "
            f"{_tail(completed.stderr or '')}"
        )
    reply = _json_object(completed.stdout or "")
    if reply is None or not all(
        key in reply for key in ("session_dir", "socket", "pid")
    ):
        raise RecorderError("the recorder's start reply is not the expected JSON")
    return reply


def status(out_dir: Path) -> dict[str, Any] | None:
    """The live recorder's ``status`` reply, or ``None`` when it is not answering."""
    import ux_recorder

    try:
        current = ux_recorder.read_current(out_dir)
        if not isinstance(current, dict) or not isinstance(current.get("socket"), str):
            return None
        reply = ux_recorder.send_command(
            current["socket"], {"cmd": "status"}, timeout=5
        )
    except (OSError, RuntimeError, ValueError):
        return None
    return reply if isinstance(reply, dict) else None


def stop(
    *,
    harness_root: Path,
    out_dir: Path,
    session_dir: Path,
    say: str,
    env: Mapping[str, str],
    python: str = sys.executable,
    run: Callable[..., processes.Completed] = processes.run_in_group,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Stop the recording and write its timeline; returns the recorder's reply.

    A failed attempt kills the daemon (:func:`kill`) and is retried once, which
    then finalises the session from disk.
    """
    argv = [
        python,
        str(_recorder_script(harness_root)),
        "stop",
        "--out",
        str(out_dir),
        "--session",
        str(session_dir),
        "--say",
        say,
    ]
    problem = ""
    for attempt in range(2):
        completed = _invoke(run, argv, env, timeout)
        if isinstance(completed, str):
            problem = completed
        else:
            reply = _json_object(completed.stdout or "")
            if completed.returncode == 0 and reply is not None and reply.get("ok"):
                return reply
            problem = f"exit {completed.returncode}: {_tail(completed.stderr or '')}"
        if attempt == 0:
            kill(out_dir)
    raise RecorderError(f"the recorder could not be stopped ({problem})")


def kill(out_dir: Path, *, wait: float = 10.0) -> None:
    """SIGTERM the live recorder and wait up to ``wait`` s for it to finalise.

    The recorder finalises on SIGTERM and then removes ``current.json``, which is
    what is waited for. Every error is tolerated, because this runs in cleanup;
    and a pid that is not a positive integer above 1 is never signalled, since
    ``os.kill`` of 0 or -1 signals a whole process group or every process.
    """
    current_file = out_dir / _CURRENT_FILE
    try:
        current = json.loads(current_file.read_text(encoding="utf-8"))
        pid = current.get("pid") if isinstance(current, dict) else None
    except (OSError, ValueError):
        return
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + wait
    while current_file.exists() and time.monotonic() < deadline:
        time.sleep(_KILL_POLL_SECONDS)


def _render_user(user: str | None) -> str | None:
    """Who render runs as when asked to run as ``user``: that user when the
    harness can switch users, and otherwise ``None``, this process's own user."""
    return user if user is not None and processes.can_switch_user() else None


def render_uid(user: str | None) -> int:
    """The uid render writes its outputs as when :func:`render_video` gets ``user``.

    That is the uid :func:`hand_over` gives the session to, or this process's own
    when render runs as this process's user, and every read-back of an output
    expects it as the file's owner.
    """
    owner = _render_user(user)
    if owner is None:
        return os.geteuid()
    try:
        return _ids_of(owner)[0]
    except KeyError:
        raise RecorderError(
            f"render's user {owner} does not exist; scripts/ux_runner/install.sh "
            "creates it"
        ) from None


def read_output(
    session_dir: Path, name: str, *, owner: int, limit: int
) -> bytes | None:
    """The bytes of render's ``name`` in ``session_dir``, or None when it has none.

    Read through :func:`readback.read`, so anything but a regular file of
    ``owner``'s directly in the directory, at most ``limit`` bytes, raises
    :class:`RecorderError`, and at most the size the check saw is read, so a file
    still growing cannot carry the read past its bound.
    """
    return readback.read(session_dir, name, writer=_RENDER, owner=owner, limit=limit)


def copy_output(
    session_dir: Path,
    name: str,
    open_target: Callable[[], IO[bytes]],
    *,
    owner: int,
    limit: int,
) -> int | None:
    """Copy render's ``name`` into the file ``open_target`` opens; the bytes copied.

    The source is checked as :func:`read_output` checks it, and ``open_target`` is
    called only once it has passed, so neither a refused output, which raises
    :class:`RecorderError`, nor an absent one, which returns ``None``, leaves a
    target behind. The copy is read from the checked descriptor, never reopened by
    path, and is at most the size the check saw (:func:`readback.copy`).
    """
    return readback.copy(
        session_dir, name, open_target, writer=_RENDER, owner=owner, limit=limit
    )


def chapters(session_dir: Path, *, owner: int) -> list[tuple[str, str]]:
    """``(m:ss, label)`` for each footage segment of the rendered video, in order.

    Read from ``segments.json``, which render writes beside the mp4, through
    :func:`read_output` with ``owner`` as render's uid, so anything in its place
    that is not render's own regular file within :data:`MAX_SEGMENTS_BYTES` raises
    :class:`RecorderError`. The title and end cards are not chapters. ``[]`` when
    there is no such file, or its content is not a usable segment list.
    """
    raw = read_output(session_dir, SEGMENTS_FILE, owner=owner, limit=MAX_SEGMENTS_BYTES)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return []
    segments = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(segments, list):
        return []
    marks: list[tuple[str, str]] = []
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("kind") != "footage":
            continue
        offset = segment.get("out_start")
        label = segment.get("label")
        if isinstance(offset, bool) or not isinstance(offset, (int, float)):
            continue
        if not math.isfinite(offset) or not isinstance(label, str):
            continue
        minutes, seconds = divmod(int(max(offset, 0)), 60)
        marks.append((f"{minutes}:{seconds:02d}", label))
    return marks


def write_review_md(session_dir: Path, text: str) -> Path:
    """Write ``review.md``, whose Findings render puts on the video's end card."""
    path = session_dir / "review.md"
    path.write_text(text, encoding="utf-8")
    return path


def _ids_of(user: str) -> tuple[int, int]:
    """``user``'s uid and the gid of its same-named group, which install.sh makes."""
    import grp
    import pwd

    return pwd.getpwnam(user).pw_uid, grp.getgrnam(user).gr_gid


def hand_over(
    path: Path,
    user: str,
    *,
    ids: Callable[[str], tuple[int, int]] = _ids_of,
    chown: Callable[..., None] | None = None,
) -> None:
    """Give ``path`` and everything under it to ``user`` and its same-named group.

    A symlink is changed itself, never followed, so a link inside the tree hands
    over nothing outside it. Render writes its mp4, its segments and its working
    files into the session directory and rewrites ``review.md`` there, so the
    whole tree must be its own; root, which reads the results back, is not
    affected by who owns them. ``chown`` defaults to ``os.chown``, looked up only
    here, because Windows, which imports this module for ``--collect-only``, has
    none.
    """
    change = chown if chown is not None else os.chown
    uid, gid = ids(user)
    entries = [path]
    for current, names, files in os.walk(path, followlinks=False):
        entries.extend(Path(current) / name for name in [*names, *files])
    for entry in entries:
        change(entry, uid, gid, follow_symlinks=False)


def _private_log(path: Path) -> Callable[[str | bytes], None]:
    def log(message: str | bytes) -> None:
        data = message.encode("utf-8") if isinstance(message, str) else message
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "ab") as handle:
            handle.write(data.rstrip(b"\n") + b"\n")

    return log


def render_argv(
    *,
    harness_root: Path,
    session_dir: Path,
    voice: str,
    region: str,
    narrated: bool,
    python: str = sys.executable,
) -> list[str]:
    """The ``ux_recorder.py render`` command line for one attempt.

    ``-I`` keeps ``PYTHONPATH``, the user site and the working directory off
    ``sys.path`` (the recorder puts its own directory there itself), and ``-B`` is
    spelled out because ``-I`` ignores ``PYTHONDONTWRITEBYTECODE``.
    """
    argv = [python, "-I", "-B", str(_recorder_script(harness_root)), "render"]
    argv += [str(session_dir), "--voice", voice, "--region", region]
    argv += ["--engine", "generative"]
    return argv + ([] if narrated else ["--no-narration"])


def _render_failure(completed: processes.Completed, timeout: float) -> str:
    if completed.timed_out:
        return f"render did not finish within {timeout:.0f} s"
    return f"render failed (exit {completed.returncode}); its log is private"


def render_video(
    session_dir: Path,
    *,
    harness_root: Path,
    voice: str,
    region: str,
    narration: bool,
    aws: Mapping[str, str],
    home: Path,
    log_path: Path,
    timeout: float,
    search_path: str | None = None,
    user: str | None = None,
    python: str = sys.executable,
    runner: Callable[..., processes.Completed] = processes.run_in_group,
    missing_tools: Callable[[Mapping[str, str]], list[str]] = render_tools_missing,
    give: Callable[[Path, str], None] = hand_over,
) -> tuple[Path | None, str]:
    """Render the session's mp4 in a child process; returns it and the narration state.

    The state is ``on``, ``off`` (asked for none) or ``unavailable
    (AccessDenied)``. The child gets :func:`render_env`, holding ``aws`` only when
    it narrates: that is the session it calls Polly with, minted for Polly alone.
    Its output goes to the private log, the bytes ``runner`` kept of each stream
    (``processes.MAX_OUTPUT_BYTES``) and a line for each saying how much more was
    discarded, and a failure is a :class:`RecorderError` naming only the exit
    status or the timeout, because render's own text can carry a role ARN or page
    text. The retry after an ``AccessDenied`` is told so
    on the child's stderr, and runs with no session at all. The returned path is
    ``None`` when the child exited cleanly and left nothing by the video's name.
    What it did leave is only looked for here, never followed or opened: it is
    checked when it is read back, by :func:`copy_output`, so a link or a pipe in
    its place is refused there rather than reported as no video. ``search_path``
    is the child's ``PATH`` when given. With ``user``
    given and a harness that can switch users, the session directory and ``home``
    are handed to that user and render runs as it; otherwise render runs as this
    process's user.
    """
    env = render_env(
        home, region=region, aws=aws if narration else {}, search_path=search_path
    )
    missing = missing_tools(env)
    if missing:
        raise RecorderError(
            "render needs ffmpeg, ffprobe, boto3 and Pillow; missing: "
            + ", ".join(missing)
        )
    owner = _render_user(user)
    if owner is not None:
        try:
            give(session_dir, owner)
            give(home, owner)
        except (OSError, KeyError) as exc:
            raise RecorderError(
                f"the session could not be handed to {owner} for render "
                f"({type(exc).__name__}); scripts/ux_runner/install.sh creates it"
            ) from None
    log = _private_log(log_path)

    def once(narrated: bool, child_env: Mapping[str, str]) -> processes.Completed:
        argv = render_argv(
            harness_root=harness_root,
            session_dir=session_dir,
            voice=voice,
            region=region,
            narrated=narrated,
            python=python,
        )
        try:
            completed = runner(
                argv,
                input_text="",
                env=child_env,
                cwd=home,
                timeout=timeout,
                user=owner,
            )
        except OSError as exc:
            raise RecorderError(
                f"render could not be started: {type(exc).__name__}"
            ) from None
        for name, stream, dropped in (
            ("stdout", completed.stdout_bytes, completed.stdout_dropped),
            ("stderr", completed.stderr_bytes, completed.stderr_dropped),
        ):
            if stream.strip():
                log(stream)
            if dropped:
                log(
                    f"render's {name}: {dropped} bytes past the capture bound discarded"
                )
        return completed

    def video() -> Path | None:
        path = session_dir / VIDEO_FILE
        return path if os.path.lexists(path) else None

    completed = once(narration, env)
    if completed.returncode == 0 and not completed.timed_out:
        return video(), "on" if narration else "off"
    if not narration or completed.timed_out or "AccessDenied" not in completed.stderr:
        raise RecorderError(_render_failure(completed, timeout))
    log("Polly answered AccessDenied; rendering once more without narration")
    completed = once(
        False, render_env(home, region=region, aws={}, search_path=search_path)
    )
    if completed.returncode == 0 and not completed.timed_out:
        return video(), "unavailable (AccessDenied)"
    raise RecorderError(_render_failure(completed, timeout))
