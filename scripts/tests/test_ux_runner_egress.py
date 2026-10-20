# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the harness side of the egress proxy, ``ux_runner/egress.py``.

What is pinned is what the proxy is handed and how it is waited for: the UI
environment is decoded without any error ever quoting it, the MR's build is
refused when it holds anything but regular files and directories and is
otherwise made readable to the proxy's uid, and the proxy is started with an
exact argv and a four-variable environment and is killed when it never reports
ready. Its directory is the proxy's once it is handed over, so each file read
back from it is shown refused when a link, a FIFO, an oversized file or another
uid's file is planted in its place. The proxy process itself is a fake here;
``test_ux_test_proxy.py`` runs the real one.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import egress, processes  # noqa: E402

RULES = egress.EgressRules(frozenset({"cdn.jsdelivr.net:443"}))
HARNESS_SCRIPT = Path("/harness/scripts/ux_test_proxy.py")


@pytest.fixture(autouse=True)
def not_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as a user that cannot switch uids, whoever runs the suite.

    CI runs these tests as root, where a real ``chown`` to ``uxproxy`` or
    ``uxbrowser`` would fail on an image without those users. The tests that are
    about the uid switch turn it back on themselves.
    """
    monkeypatch.setattr(processes, "can_switch_user", lambda: False)


class FakeProcess:
    """Stands in for a ``ManagedProcess``: alive until terminated."""

    def __init__(self, *, alive: bool = True) -> None:
        self.running = alive
        self.terminated = 0
        self.pid = 0

    def alive(self) -> bool:
        return self.running

    def terminate(self, grace: float = 10.0) -> int | None:
        self.terminated += 1
        self.running = False
        return 0


class FakeTime:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _fake_start(
    calls: list[dict[str, Any]],
    *,
    process: FakeProcess,
    ready: bool = True,
    log_line: bytes = b"",
) -> Callable[..., Any]:
    """A ``processes.start`` that records its call and plays the proxy's part."""

    def start(
        argv: list[str],
        *,
        name: str,
        env: dict[str, str],
        user: str | None = None,
        stdout: IO[bytes] | None = None,
        stderr: IO[bytes] | None = None,
        **kwargs: Any,
    ) -> FakeProcess:
        calls.append({"argv": list(argv), "name": name, "env": dict(env), "user": user})
        if stdout is not None and log_line:
            stdout.write(log_line)
        if ready:
            Path(argv[argv.index("--ready-file") + 1]).write_text("127.0.0.1:3128\n")
        return process

    return start


def _b64(text: str | bytes) -> str:
    raw = text.encode() if isinstance(text, str) else text
    return base64.b64encode(raw).decode()


@pytest.mark.unit
def test_the_ui_environment_round_trips_through_one_line_base64() -> None:
    text = (
        "VITE_AWS_REGION=us-east-1\n"
        "VITE_INPUT_BUCKET_NAME=idp-ux-nightly-inputbucket-def456\n"
    )
    decoded = egress.ui_env_from_b64(f"  {_b64(text)}\n")
    assert decoded == {
        "VITE_AWS_REGION": "us-east-1",
        "VITE_INPUT_BUCKET_NAME": "idp-ux-nightly-inputbucket-def456",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("VITE_USER_POOL_ID=us-east-1_AbCdEfGhI!", "base64"),
        ("aWRwLXV4LW5pZ2h0bHk", "base64"),
        (_b64(b"VITE_SETTINGS_PARAMETER=idp-ux-nightly-\xff\xfeSettings"), "UTF-8"),
    ],
)
def test_a_malformed_ui_environment_is_refused_without_quoting_it(
    value: str, reason: str
) -> None:
    with pytest.raises(egress.EgressConfigError) as raised:
        egress.ui_env_from_b64(value)
    message = str(raised.value)
    assert reason in message and "UX_TEST_UI_ENV" in message
    assert value not in message
    for fragment in ("us-east-1_AbCdEfGhI", "idp-ux-nightly", "xff", "\\xff"):
        assert fragment not in message
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.unit
def test_a_build_path_that_does_not_exist_is_refused_as_having_no_index(
    tmp_path: Path,
) -> None:
    with pytest.raises(egress.EgressConfigError, match="^the UI build has no index"):
        egress.prepare_site(tmp_path / "missing" / "build")


@pytest.mark.unit
def test_a_build_without_an_index_is_refused(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    with pytest.raises(egress.EgressConfigError, match="index.html"):
        egress.prepare_site(tmp_path)


@pytest.mark.unit
def test_a_build_holding_a_symlink_is_refused_naming_its_relative_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "hostname").symlink_to("/etc/hostname")
    with pytest.raises(egress.EgressConfigError) as raised:
        egress.prepare_site(tmp_path)
    assert "symlink" in str(raised.value)
    assert "assets/hostname" in str(raised.value)


@pytest.mark.unit
def test_a_build_holding_a_symlinked_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "etc").symlink_to("/etc", target_is_directory=True)
    with pytest.raises(egress.EgressConfigError, match="symlink: etc"):
        egress.prepare_site(tmp_path)


def _elsewhere(tmp_path: Path) -> Path:
    """A real build somewhere the MR should not be able to point the proxy at."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "index.html").write_text("<html>not the MR's build</html>")
    return target


@pytest.mark.unit
def test_a_build_root_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    """The walk starts below the root, so the root itself is checked first."""
    build = tmp_path / "checkout" / "src" / "ui" / "build"
    build.parent.mkdir(parents=True)
    build.symlink_to(_elsewhere(tmp_path), target_is_directory=True)
    with pytest.raises(egress.EgressConfigError) as raised:
        egress.prepare_site(build)
    assert str(raised.value) == (f"the UI build is reached through a symlink: {build}")


@pytest.mark.unit
def test_a_build_under_a_symlinked_directory_of_the_checkout_is_refused(
    tmp_path: Path,
) -> None:
    (_elsewhere(tmp_path) / "build").mkdir()
    (tmp_path / "elsewhere" / "build" / "index.html").write_text("<html></html>")
    checkout = tmp_path / "checkout"
    (checkout / "src").mkdir(parents=True)
    (checkout / "src" / "ui").symlink_to(
        tmp_path / "elsewhere", target_is_directory=True
    )
    with pytest.raises(egress.EgressConfigError, match="through a symlink: .*/src/ui$"):
        egress.prepare_site(checkout / "src" / "ui" / "build")


@pytest.mark.unit
def test_a_symlink_spelled_away_with_dot_dot_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ui/x/../build`` reads as ``ui/build`` lexically, but the kernel follows x.

    So a check of ``os.path.abspath`` would pass a legitimate-looking ``ui/build``
    while the proxy was handed the build beside x's target.
    """
    other = _elsewhere(tmp_path)
    (other / "sub").mkdir()
    (other / "build").mkdir()
    (other / "build" / "index.html").write_text("<html>not the MR's build</html>")
    ui = tmp_path / "checkout" / "ui"
    (ui / "build").mkdir(parents=True)
    (ui / "build" / "index.html").write_text("<html>the MR's build</html>")
    (ui / "x").symlink_to(other / "sub", target_is_directory=True)
    monkeypatch.chdir(ui)
    spelled = Path("x") / ".." / "build"
    assert Path(os.path.abspath(spelled)) == ui.resolve() / "build"
    assert spelled.resolve() == (other / "build").resolve()
    with pytest.raises(egress.EgressConfigError, match="through a symlink: .*/ui/x$"):
        egress.prepare_site(spelled)


@pytest.mark.unit
def test_a_build_holding_a_fifo_is_refused_naming_it(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(egress.EgressConfigError, match="not a file: pipe"):
        egress.prepare_site(tmp_path)


@pytest.mark.unit
def test_a_valid_build_is_made_readable_by_every_user_and_returned_resolved(
    tmp_path: Path,
) -> None:
    build = tmp_path / "build"
    (build / "assets").mkdir(parents=True)
    index = build / "index.html"
    script = build / "assets" / "tool.sh"
    plain = build / "assets" / "app.js"
    index.write_text("<html></html>")
    script.write_text("#!/bin/sh\n")
    plain.write_text("console.log(1);\n")
    os.chmod(index, 0o600)
    os.chmod(plain, 0o600)
    os.chmod(script, 0o700)
    os.chmod(build / "assets", 0o700)
    os.chmod(build, 0o700)

    assert egress.prepare_site(build / "assets" / "..") == build.resolve()

    def mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    assert mode(build) & 0o555 == 0o555
    assert mode(build / "assets") & 0o555 == 0o555
    assert mode(index) & 0o444 == 0o444 and mode(index) & 0o111 == 0
    assert mode(plain) & 0o444 == 0o444 and mode(plain) & 0o111 == 0
    assert mode(script) & 0o555 == 0o555


@pytest.mark.unit
def test_a_build_extracted_world_writable_is_left_writable_by_its_owner_alone(
    tmp_path: Path,
) -> None:
    """GitLab extracts artifacts at umask 0000, and the proxy serves this tree to
    the browser, whose uid must not be able to change it while it is served."""
    build = tmp_path / "build"
    (build / "assets").mkdir(parents=True)
    index = build / "index.html"
    script = build / "assets" / "tool.sh"
    index.write_text("<html></html>")
    script.write_text("#!/bin/sh\n")
    os.chmod(index, 0o666)  # nosec B103 - reproduces GitLab's umask-0000 extraction
    os.chmod(script, 0o777)  # nosec B103 - reproduces GitLab's umask-0000 extraction
    os.chmod(build / "assets", 0o777)  # nosec B103 - reproduces GitLab's umask-0000 extraction
    os.chmod(build, 0o777)  # nosec B103 - reproduces GitLab's umask-0000 extraction

    egress.prepare_site(build)

    def mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    assert (mode(build), mode(build / "assets")) == (0o755, 0o755)
    assert (mode(index), mode(script)) == (0o644, 0o755)


@pytest.mark.unit
def test_the_rules_file_is_the_rules_json_readable_by_every_user(
    tmp_path: Path,
) -> None:
    path = egress.write_rules(tmp_path / "rules.json", RULES)
    assert json.loads(path.read_text()) == {
        "version": 1,
        "hosts": ["cdn.jsdelivr.net:443"],
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.unit
def test_launch_proxy_runs_the_proxy_isolated_with_an_exact_argv_and_three_variables(
    tmp_path: Path,
) -> None:
    """``-B`` on the command line, because ``-I`` ignores every ``PYTHON*``
    variable and ``PYTHONDONTWRITEBYTECODE`` in the environment would do nothing."""
    calls: list[dict[str, Any]] = []
    process = FakeProcess()
    build = tmp_path / "build"
    run = egress.launch_proxy(
        run_dir=tmp_path,
        rules=RULES,
        build_dir=build,
        user="uxproxy",
        python="/usr/local/bin/python3",
        script=HARNESS_SCRIPT,
        start=_fake_start(calls, process=process),
    )
    proxy_dir = tmp_path / "proxy"
    assert calls == [
        {
            "argv": [
                "/usr/local/bin/python3",
                "-I",
                "-B",
                str(HARNESS_SCRIPT),
                "--listen",
                "127.0.0.1:3128",
                "--rules",
                str(proxy_dir / "rules.json"),
                "--build-dir",
                str(build),
                "--refusal-log",
                str(proxy_dir / "refusals.jsonl"),
                "--stats",
                str(proxy_dir / "stats.json"),
                "--ready-file",
                str(proxy_dir / "ready"),
            ],
            "name": "egress-proxy",
            "env": {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "TZ": "UTC",
            },
            "user": "uxproxy",
        }
    ]
    assert run.process is process and process.terminated == 0
    assert run.refusal_log == proxy_dir / "refusals.jsonl"
    assert run.stats_file == proxy_dir / "stats.json"
    assert run.ready_file == proxy_dir / "ready"
    assert run.log_file == proxy_dir / "proxy.log"
    assert run.owner == os.geteuid()
    assert json.loads((proxy_dir / "rules.json").read_text()) == RULES.to_json()
    assert stat.S_IMODE(proxy_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((proxy_dir / "proxy.log").stat().st_mode) == 0o600


@pytest.mark.unit
def test_launch_proxy_without_a_build_runs_the_shipped_script_in_stack_mode(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []
    egress.launch_proxy(
        run_dir=tmp_path,
        rules=RULES,
        build_dir=None,
        user=None,
        start=_fake_start(calls, process=FakeProcess()),
    )
    argv = calls[0]["argv"]
    assert "--build-dir" not in argv
    assert argv[:4] == [
        sys.executable,
        "-I",
        "-B",
        str(SCRIPTS / "ux_test_proxy.py"),
    ]
    assert Path(argv[3]).is_file()
    assert calls[0]["user"] is None


@pytest.mark.unit
def test_launch_proxy_hands_its_directory_to_the_proxy_user_when_it_can_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything this process writes there, the rules and the log, is written
    before the directory is the proxy's, so nothing the proxy could plant is ever
    followed by root's own writes; the log is the proxy's, as its other files are."""
    proxy_dir = tmp_path / "proxy"
    owners: list[tuple[Path, str | None, str | None, list[str]]] = []
    calls: list[dict[str, Any]] = []

    def chown(path: Path, user: str | None = None, group: str | None = None) -> None:
        assert calls == [], "the proxy started before its directory was handed over"
        held = sorted(entry.name for entry in proxy_dir.iterdir())
        owners.append((Path(path), user, group, held))

    monkeypatch.setattr(processes, "can_switch_user", lambda: True)
    monkeypatch.setattr(shutil, "chown", chown)
    run = egress.launch_proxy(
        run_dir=tmp_path,
        rules=RULES,
        build_dir=None,
        user="uxproxy",
        start=_fake_start(calls, process=FakeProcess()),
    )
    held = ["proxy.log", "rules.json"]
    assert owners == [
        (proxy_dir / "proxy.log", "uxproxy", "uxproxy", held),
        (proxy_dir, "uxproxy", "uxproxy", held),
    ]
    assert run.owner == proxy_dir.lstat().st_uid
    assert calls[0]["user"] == "uxproxy"


@pytest.mark.unit
def test_launch_proxy_refuses_a_directory_it_did_not_make_and_writes_nothing_there(
    tmp_path: Path,
) -> None:
    """A directory that is already there may be one an earlier proxy owned, and a
    link it left under ``rules.json`` would have root write the rules through it."""
    outside = tmp_path / "root-only.txt"
    outside.write_text("untouched\n", encoding="utf-8")
    proxy_dir = tmp_path / "proxy"
    proxy_dir.mkdir()
    (proxy_dir / "rules.json").symlink_to(outside)
    calls: list[dict[str, Any]] = []
    with pytest.raises(FileExistsError):
        egress.launch_proxy(
            run_dir=tmp_path,
            rules=RULES,
            build_dir=None,
            user=None,
            start=_fake_start(calls, process=FakeProcess()),
        )
    assert outside.read_text(encoding="utf-8") == "untouched\n"
    assert sorted(entry.name for entry in proxy_dir.iterdir()) == ["rules.json"]
    assert calls == []


@pytest.mark.unit
def test_launch_proxy_kills_a_proxy_that_never_reports_ready(tmp_path: Path) -> None:
    process = FakeProcess()
    fake = FakeTime()
    with pytest.raises(egress.ProxyError, match="^the egress proxy did not start"):
        egress.launch_proxy(
            run_dir=tmp_path,
            rules=RULES,
            build_dir=None,
            user=None,
            start=_fake_start([], process=process, ready=False),
            timeout=1.0,
            sleep=fake.sleep,
            clock=fake.clock,
        )
    assert process.terminated == 1
    assert fake.now >= 1.0
    assert fake.sleeps and max(fake.sleeps) < 1.0


@pytest.mark.unit
def test_launch_proxy_gives_up_at_once_on_a_proxy_that_exits_and_says_why(
    tmp_path: Path,
) -> None:
    process = FakeProcess(alive=False)
    fake = FakeTime()
    with pytest.raises(egress.ProxyError) as raised:
        egress.launch_proxy(
            run_dir=tmp_path,
            rules=RULES,
            build_dir=None,
            user=None,
            start=_fake_start(
                [],
                process=process,
                ready=False,
                log_line=b"ux_test_proxy: --rules is not JSON\n",
            ),
            sleep=fake.sleep,
            clock=fake.clock,
        )
    assert str(raised.value) == (
        "the egress proxy did not start: ux_test_proxy: --rules is not JSON"
    )
    assert process.terminated == 1
    assert fake.sleeps == []


@pytest.mark.unit
def test_read_refusals_tolerates_a_missing_file_skips_bad_lines_and_stops_at_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "refusals.jsonl"
    owner = os.geteuid()
    assert egress.read_refusals(path, owner=owner) == []
    assert egress.read_refusals(tmp_path / "gone" / "x.jsonl", owner=owner) == []
    good = {"t": "2026-10-14T21:03:11Z", "method": "CONNECT", "reason": "x"}
    deep = "[" * 100_000 + "]" * 100_000
    path.write_text(
        "\n".join(
            [
                json.dumps(good),
                "not json",
                "[1, 2]",
                deep,
                json.dumps(good),
                '{"torn": ',
            ]
        )
    )
    assert egress.read_refusals(path, owner=owner) == [good, good]
    assert egress.read_refusals(path, owner=owner, limit=1) == [good]


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "{not json",
        "[1, 2]",
        '"text"',
        pytest.param("[" * 30_000, id="nested-past-the-recursion-limit"),
    ],
)
def test_read_stats_is_empty_for_a_missing_or_corrupt_file(
    tmp_path: Path, content: str | None
) -> None:
    path = tmp_path / "stats.json"
    if content is not None:
        path.write_text(content)
    assert egress.read_stats(path, owner=os.geteuid()) == {}


@pytest.mark.unit
def test_read_stats_returns_what_the_proxy_wrote(tmp_path: Path) -> None:
    written = {"served": 3, "connect_allowed": 2, "refused": 1, "upstream_failed": 0}
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({**written, "by_reason": {"host_not_allowed": 1}}))
    assert egress.read_stats(path, owner=os.geteuid()) == {
        **written,
        "by_reason": {"host_not_allowed": 1},
    }


SELFTEST_RECORD = {
    "t": "2026-10-14T21:03:11Z",
    "method": "GET",
    "target": "http://ux-proxy-selftest.invalid/",
    "reason": "plain_http_not_allowed",
    "served": 1,
}

PROXY_OUTPUTS = [
    pytest.param(egress.READY_FILE, egress.MAX_READY_BYTES, id="ready"),
    pytest.param(egress.LOG_FILE, egress.MAX_LOG_BYTES, id="log"),
    pytest.param(egress.REFUSAL_LOG, egress.MAX_REFUSAL_LOG_BYTES, id="refusals"),
    pytest.param(egress.STATS_FILE, egress.MAX_STATS_BYTES, id="stats"),
]


def _outside(tmp_path: Path) -> Path:
    """A file of root's, outside the proxy's directory, that every reader would
    take for a good answer: a ready line, a log line, a refusal and the stats."""
    outside = tmp_path / "root-only.json"
    outside.write_text(json.dumps(SELFTEST_RECORD) + "\n", encoding="utf-8")
    return outside


def _plant(path: Path, kind: str, limit: int, outside: Path) -> None:
    """Put ``kind`` where the proxy's file ``path`` is, as the proxy's uid could."""
    path.unlink(missing_ok=True)
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        with path.open("wb") as handle:
            handle.truncate(limit + 1)


def _read_back(
    tmp_path: Path,
    name: str,
    plant: Callable[[Path], None],
    *,
    owner: int | None = None,
) -> BaseException | None:
    """Plant under the proxy's ``name`` and read it back as the harness does.

    The ready file is read while ``launch_proxy`` waits, and the log once the
    proxy has exited without becoming ready, so both are planted by the fake
    proxy as it starts and must kill it; the refusal log and the stats are read
    by their own readers. ``owner`` overrides the uid the directory reads as
    handed to. A FIFO that held the read open would hang the test, so the read
    runs on a thread, and one still waiting after ten seconds fails it.
    """
    outcome: list[BaseException | None] = []
    proxy_dir = tmp_path / "proxy"

    def launch() -> None:
        process = FakeProcess(alive=name == egress.READY_FILE)

        def start(argv: list[str], **kwargs: Any) -> FakeProcess:
            plant(proxy_dir / name)
            return process

        fake = FakeTime()
        try:
            egress.launch_proxy(
                run_dir=tmp_path,
                rules=RULES,
                build_dir=None,
                user=None,
                start=start,
                timeout=1.0,
                sleep=fake.sleep,
                clock=fake.clock,
            )
        finally:
            assert process.terminated == 1, "the proxy was left running"

    def read() -> None:
        proxy_dir.mkdir()
        plant(proxy_dir / name)
        reader = (
            egress.read_refusals if name == egress.REFUSAL_LOG else egress.read_stats
        )
        reader(proxy_dir / name, owner=os.geteuid() if owner is None else owner)

    def target() -> None:
        try:
            if name in (egress.READY_FILE, egress.LOG_FILE):
                launch()
            else:
                read()
        except BaseException as error:
            outcome.append(error)
        else:
            outcome.append(None)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(10)
    if worker.is_alive():
        fifo = proxy_dir / name
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(10)
        pytest.fail(f"reading the proxy's {name} back waited for a writer")
    return outcome[0]


def _refusal(name: str, why: str) -> str:
    """The text a refused proxy file fails with, as each reader reports it."""
    refused = f"the proxy's {name} is refused: {why}"
    if name in (egress.READY_FILE, egress.LOG_FILE):
        return f"the egress proxy did not start: {refused}"
    return refused


@pytest.mark.unit
@pytest.mark.parametrize(("name", "limit"), PROXY_OUTPUTS)
def test_a_symlink_in_place_of_a_proxy_file_is_refused_and_never_followed(
    tmp_path: Path, name: str, limit: int
) -> None:
    """The directory is uxproxy's once it is handed over, and this process is
    root, so a link followed there would read root's own files."""
    outside = _outside(tmp_path)
    raised = _read_back(
        tmp_path, name, lambda path: _plant(path, "symlink", limit, outside)
    )
    assert isinstance(raised, egress.ProxyError)
    assert str(raised) == _refusal(
        name, "it is a symbolic link, which is never followed"
    )
    assert "selftest" not in str(raised)


@pytest.mark.unit
@pytest.mark.parametrize(("name", "limit"), PROXY_OUTPUTS)
def test_a_fifo_in_place_of_a_proxy_file_is_refused_without_waiting_for_a_writer(
    tmp_path: Path, name: str, limit: int
) -> None:
    outside = _outside(tmp_path)
    raised = _read_back(
        tmp_path, name, lambda path: _plant(path, "fifo", limit, outside)
    )
    assert isinstance(raised, egress.ProxyError)
    assert str(raised) == _refusal(name, "it is not a regular file")


@pytest.mark.unit
@pytest.mark.parametrize(("name", "limit"), PROXY_OUTPUTS)
def test_a_proxy_file_over_its_bound_is_refused_before_it_is_read(
    tmp_path: Path, name: str, limit: int
) -> None:
    """The file is sparse, so the bound is passed without writing it."""
    outside = _outside(tmp_path)
    raised = _read_back(
        tmp_path, name, lambda path: _plant(path, "big", limit, outside)
    )
    assert isinstance(raised, egress.ProxyError)
    assert str(raised) == _refusal(
        name, f"it is {limit + 1} bytes, over its bound of {limit}"
    )


@pytest.mark.unit
@pytest.mark.parametrize(("name", "limit"), PROXY_OUTPUTS)
def test_a_proxy_file_of_any_uid_but_the_proxys_is_refused(
    tmp_path: Path, name: str, limit: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard link the proxy made to another user's file is that user's file."""
    mine = os.geteuid()
    monkeypatch.setattr(egress, "_hand_over", lambda *args: mine + 1)

    def plant(path: Path) -> None:
        path.write_text(json.dumps(SELFTEST_RECORD) + "\n", encoding="utf-8")

    raised = _read_back(tmp_path, name, plant, owner=mine + 1)
    assert isinstance(raised, egress.ProxyError)
    assert str(raised) == _refusal(
        name, f"it belongs to uid {mine}, not to the proxy's uid {mine + 1}"
    )


@pytest.mark.unit
def test_the_proxys_directory_itself_is_refused_when_it_is_a_link(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / egress.STATS_FILE).write_text(json.dumps({"served": 1}))
    link = tmp_path / "proxy"
    link.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(egress.ProxyError) as raised:
        egress.read_stats(link / egress.STATS_FILE, owner=os.geteuid())
    assert str(raised.value) == (
        "the proxy's directory is refused: it is a symbolic link, which is never "
        "followed"
    )
