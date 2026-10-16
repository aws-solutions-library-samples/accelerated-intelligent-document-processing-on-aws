# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pin the guard that stops two UI make targets corrupting src/ui/node_modules.

Seven `make` targets run a node tool out of `src/ui/node_modules`, and six of them
reinstall it with `npm ci` first — which deletes the tree before rebuilding it, by
design. Two of those targets at once in ONE checkout therefore corrupt each other,
and the loser reports a missing or half-deleted module from whatever package the
other lane was unpacking rather than anything resembling contention. `make
lint-cicd` performs three of them itself, so lint-cicd plus any one UI target is
enough (issue #1447).

`scripts/ui_deps_lock.sh` makes that impossible rather than documented. These tests
cover both halves of the claim:

* the **wiring** — every UI target's expanded recipe goes through the lock, read
  out of `make --dry-run` rather than out of the recipe text, because a spelling is
  not a command; and `ui-test` honours `SKIP_NPM_CI` now that it uses `$(NPM_CI)`
  rather than a hardcoded `npm ci`;
* the **behaviour** — a contended invocation waits, a bounded wait that expires
  runs nothing and fails, a SIGKILLed holder leaves nothing to clean up, a nested
  invocation does not wait on a lock its own ancestor holds, and a checkout where
  locking is impossible runs anyway and says so.

⚠️ Nothing here runs `npm`. The real `npm ci` takes about a minute and mutates a
directory other work in the same checkout is reading; every behavioural test drives
the script over `touch` and `sleep` in a throwaway tree of its own, which is also
what keeps these tests from contending with each other under `pytest -n`.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
LOCK_SCRIPT = REPO_ROOT / "scripts" / "ui_deps_lock.sh"

#: The marker a dry-run line must carry to count as locked.
LOCK_MARKER = "ui_deps_lock.sh"

#: The same thing before make expands it. A negative assertion over recipe TEXT has
#: to look for this spelling: ``$(UI_DEPS_LOCK)`` contains none of the expanded
#: path, so a check for :data:`LOCK_MARKER` alone would pass vacuously.
LOCK_VARIABLE = "$(UI_DEPS_LOCK)"

#: Every target that runs a node tool out of ``src/ui/node_modules``. The five
#: gates from #1447, the fixer beside them, and ``codegen`` — which runs no
#: ``npm ci`` of its own and so was never in the issue's table, but reads the tree
#: a concurrent install would be deleting and is exposed to exactly the half of the
#: hazard that produced the missing-eslint-formatter failure.
UI_TARGETS = (
    "ui-lint",
    "ui-lint-fix",
    "ui-build",
    "ui-build-only",
    "ui-test",
    "codegen",
    "codegen-check",
)

#: The three ``lint-cicd`` performs itself, which is why one other UI target in a
#: second lane is sufficient to collide.
TARGETS_LINT_CICD_PERFORMS = ("ui-lint", "ui-build-only", "codegen-check")

pytestmark = pytest.mark.unit


def _dry_run(target: str, **env_overrides: str) -> list[str]:
    """Every command ``make <target>`` would run, as make itself expands it.

    ``--dry-run`` prints without executing, which is load-bearing: these targets
    reinstall node_modules and run vitest.
    """
    argv = ["make", "--no-print-directory", "--dry-run", target]
    argv += [f"{name}={value}" for name, value in env_overrides.items()]
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, cwd=str(REPO_ROOT), capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (
        f"`make --dry-run {target}` failed:\n{result.stdout}\n{result.stderr}"
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _recipe_text(target: str) -> str:
    """The tab-indented body of ``target`` as written, not expanded."""
    body: list[str] = []
    collecting = False
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{target}:"):
            collecting = True
            continue
        if collecting:
            if line.startswith("\t"):
                body.append(line)
            elif line.strip():
                break
    assert body, f"no recipe found for {target!r} in {MAKEFILE}"
    return "\n".join(body)


# --------------------------------------------------------------------------- #
# The wiring: what make will actually run.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("target", UI_TARGETS)
def test_every_ui_target_takes_the_lock(target: str) -> None:
    lines = _dry_run(target)
    assert any(LOCK_MARKER in line for line in lines), (
        f"`make {target}` runs a node tool in src/ui without taking the "
        f"node_modules lock, so it can corrupt — or be corrupted by — any other UI "
        f"target running in this checkout (#1447). Route it through "
        f"$(UI_DEPS_LOCK).\nExpanded recipe:\n" + "\n".join(lines)
    )


@pytest.mark.parametrize("target", UI_TARGETS)
def test_no_ui_target_runs_npm_outside_the_lock(target: str) -> None:
    """Taking the lock somewhere is not enough; every npm/npx line must be inside it.

    The target that motivated this is ``ui-lint``, whose install and whose eslint
    run are one shell command: locking only the install would leave the read half
    exposed, which is the half that produced the missing-formatter failure.
    """
    unguarded = [
        line
        for line in _dry_run(target)
        if ("npm " in line or "npx " in line) and LOCK_MARKER not in line
    ]
    assert not unguarded, (
        f"`make {target}` runs npm/npx outside the node_modules lock:\n"
        + "\n".join(unguarded)
    )


def test_the_only_declared_unguarded_npm_line_is_the_dev_server() -> None:
    """``ui-start``'s dev server is the one deliberate residual, and it is bounded.

    Holding the lock for a long-lived dev server would block every gate for as long
    as it ran, so ``npm run start`` is outside it while the ``npm ci`` that precedes
    it is inside. This asserts that trade stays exactly one line wide, so a future
    edit cannot quietly widen it.
    """
    unguarded = [
        line
        for line in _dry_run("ui-start", STACK_NAME="unused-dry-run-only")
        if ("npm " in line or "npx " in line) and LOCK_MARKER not in line
    ]
    assert unguarded == ["cd src/ui && npm run start"], (
        "ui-start's unguarded npm lines changed. Exactly one is accounted for — the "
        "long-lived dev server — and these are what is there now:\n"
        + "\n".join(unguarded)
    )


@pytest.mark.parametrize("target", UI_TARGETS)
def test_the_install_is_skippable_in_every_target_that_installs(target: str) -> None:
    """``SKIP_NPM_CI=1`` must reach every install, ``ui-test`` included.

    ``ui-test`` hardcoded ``npm ci --prefer-offline --no-audit`` rather than using
    ``$(NPM_CI)``, so the skip demonstrably could not reach it and any
    install-once-then-skip ordering had to treat it as a special case.
    """
    lines = _dry_run(target, SKIP_NPM_CI="1")
    installs = [line for line in lines if "npm ci" in line]
    assert not installs, (
        f"`make {target} SKIP_NPM_CI=1` still reinstalls node_modules, so the skip "
        f"cannot reach it. Use $(NPM_CI) rather than a literal `npm ci`.\n"
        + "\n".join(installs)
    )


def test_ui_lint_writes_the_checksum_where_it_reads_it() -> None:
    """The one thing wrapping ``ui-lint`` could silently break, pinned.

    ``ui-lint``'s recipe is a single shell. Its lint line used to begin ``cd
    src/ui &&``, and the bare ``.checksum`` written a few lines later landed in
    ``src/ui`` purely by inheriting that cd. Running the lint in a child process
    leaves the shell at the repository root, and a root ``.checksum`` is gitignored
    too — so the write would go somewhere the read never looks, the cache would
    stop hitting, and nothing would be visible. The read and the write have to name
    the same path.
    """
    lines = _dry_run("ui-lint", FORCE="1")
    writes = [line for line in lines if ".checksum" in line and ">" in line]
    assert writes, "ui-lint no longer writes a checksum at all:\n" + "\n".join(lines)
    for line in writes:
        assert "> src/ui/.checksum" in line, (
            "ui-lint writes its checksum somewhere other than src/ui/.checksum, "
            "which is where it reads it from:\n" + line
        )


def test_ui_lint_records_no_checksum_for_a_lint_that_did_not_run() -> None:
    """A wait that expires must not look like a pass on the next invocation.

    ``ui-lint`` writes the checksum on the line after the lint, in the same shell.
    An expired wait exits 1 having run nothing, so the write has to be unreachable
    from it — otherwise the cache would record a clean tree for a lint that never
    happened, and the following run would report a green SKIP. The ``|| exit 1`` on
    the lint line is what makes that so, and it is the kind of thing a later edit
    drops without noticing.
    """
    recipe = _recipe_text("ui-lint")
    locked = [line for line in recipe.splitlines() if LOCK_VARIABLE in line]
    assert len(locked) == 1, f"expected one locked line in ui-lint, got:\n{recipe}"
    assert locked[0].rstrip().rstrip("\\").rstrip().rstrip(";").endswith("|| exit 1"), (
        "ui-lint's lint line no longer aborts the recipe on failure, so a lock wait "
        "that expired — or a failing eslint — would fall through to the checksum "
        f"write and cache a pass for work that did not run:\n{locked[0]}"
    )


def test_lint_cicd_does_not_hold_the_lock_itself() -> None:
    """The anti-deadlock property, asserted where it is decided.

    ``lint-cicd`` invokes three of these targets in sequence. Each takes and
    releases the lock on its own; if ``lint-cicd`` also held it, those three would
    wait on a lock their own parent holds and the gate would stall until the bound
    expired. Nesting is additionally made safe inside the script (``UI_DEPS_LOCK_HELD``,
    covered below), so this is the first of two independent reasons it cannot happen.
    """
    recipe = _recipe_text("lint-cicd")
    for marker in (LOCK_VARIABLE, LOCK_MARKER):
        assert marker not in recipe, (
            "lint-cicd now takes the src/ui node_modules lock itself, and it invokes "
            "three targets that each take it. Hold the lock in the leaf targets only."
        )
    for target in TARGETS_LINT_CICD_PERFORMS:
        assert f"make {target}" in recipe, (
            f"lint-cicd no longer invokes {target}. If the set it performs changed, "
            "TARGETS_LINT_CICD_PERFORMS and the Makefile comment above the UI "
            "section both describe it and both need updating."
        )


# --------------------------------------------------------------------------- #
# The behaviour: drive the real script in a throwaway tree.
# --------------------------------------------------------------------------- #


def _fake_checkout(root: Path) -> Path:
    """A minimal tree the real script can run in, with its own lock file.

    The lock path is derived from the script's own location, so a copy per test is
    what keeps these tests from contending with each other — and from contending
    with a real `make` running in the actual checkout.
    """
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "src" / "ui").mkdir(parents=True, exist_ok=True)
    script = root / "scripts" / "ui_deps_lock.sh"
    shutil.copy2(LOCK_SCRIPT, script)
    return script


def _spawn(script: Path, command: str, **env_overrides: str) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env.pop("UI_DEPS_LOCK_HELD", None)
    env.update(env_overrides)
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(script), command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )


def _run(
    script: Path, command: str, *, timeout: float = 60, **env_overrides: str
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("UI_DEPS_LOCK_HELD", None)
    env.update(env_overrides)
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(script), command],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=timeout,
    )


def _await_file(path: Path, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared within {timeout}s")


def test_the_uncontended_case_runs_in_src_ui_and_says_nothing(tmp_path: Path) -> None:
    """The common case is one serial gate, and it must cost nothing visible."""
    script = _fake_checkout(tmp_path)
    result = _run(script, "pwd; printf 'held=%s\\n' \"$UI_DEPS_LOCK_HELD\"")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == str(tmp_path / "src" / "ui")
    assert "held=1" in result.stdout
    assert result.stderr == "", (
        "an uncontended run printed to stderr; the serial case has to stay quiet or "
        f"every gate log grows a line about a hazard it is not in:\n{result.stderr}"
    )


def test_the_commands_exit_status_is_the_targets_exit_status(tmp_path: Path) -> None:
    script = _fake_checkout(tmp_path)
    assert _run(script, "exit 7").returncode == 7


@pytest.mark.parametrize("argv", [[], [""]])
def test_a_missing_command_is_a_usage_error(tmp_path: Path, argv: list[str]) -> None:
    """Exit 2, not a silent success — an empty expansion must not read as a no-op."""
    script = _fake_checkout(tmp_path)
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(script), *argv], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_a_second_invocation_waits_for_the_first(tmp_path: Path) -> None:
    """The hazard itself: two at once, and the second must not run concurrently.

    Ordered on files rather than on elapsed time. The holder touches ``released``
    as its last act, so the lock it holds outlives that file by the time it takes
    the process to exit; a waiter that ran concurrently would have created its own
    marker first.
    """
    script = _fake_checkout(tmp_path)
    holding = tmp_path / "holding"
    go = tmp_path / "go"
    released = tmp_path / "released"
    waiter_ran = tmp_path / "waiter-ran"

    holder = _spawn(
        script,
        f"touch {holding}; while [ ! -f {go} ]; do sleep 0.02; done; touch {released}",
    )
    try:
        _await_file(holding)
        waiter = _spawn(script, f"touch {waiter_ran}")
        try:
            # The waiter announces the wait before blocking, which is a definite
            # signal that `flock -n` refused rather than a timing guess.
            deadline = time.monotonic() + 30
            announced = ""
            assert waiter.stderr is not None
            while time.monotonic() < deadline:
                announced += waiter.stderr.readline()
                if "Waiting up to" in announced:
                    break
            assert "is in use by another make target" in announced, (
                "the second invocation did not report contention, so it did not "
                f"detect the lock:\n{announced}"
            )
            assert not waiter_ran.exists(), (
                "the second invocation ran its command while the first still held "
                "the lock — which is the corruption this guard exists to prevent"
            )
            go.touch()
            assert waiter.wait(timeout=30) == 0
        finally:
            waiter.kill()
        assert waiter_ran.exists()
        assert waiter_ran.stat().st_mtime_ns >= released.stat().st_mtime_ns
    finally:
        holder.kill()
        holder.wait(timeout=30)


#: A stand-in for ``npm ci`` followed by a node tool reading what it installed.
#: ``npm ci`` deletes node_modules and rebuilds it, so the destructive half is
#: ``rm -rf`` + recreate and the exposed half is a read afterwards. ⚠️ The real
#: thing takes about a minute and mutates a directory other work in this checkout
#: is reading, which is why this is a stub.
_REINSTALL_THEN_READ = (
    "rm -rf {tree}; mkdir -p {tree}; : > {tree}/installed; touch {built}; "
    "while [ ! -f {go} ]; do sleep 0.02; done; "
    '[ -f {tree}/installed ] || {{ echo "MODULE GONE" >&2; exit 3; }}'
)
#: The other lane, caught mid-install: it has deleted the tree and has not yet
#: unpacked the package the first lane needs. That is the state behind all three
#: errors in #1447 — a missing ``esbuild/install.js``, a missing eslint formatter,
#: and an ``ENOTEMPTY`` on a directory the other lane was removing.
_WIPE_MIDWAY = "rm -rf {tree}; mkdir -p {tree}; touch {done}"


def test_the_hazard_is_reproducible_without_the_lock_and_gone_with_it(
    tmp_path: Path,
) -> None:
    """Drive the contended case both ways, over a stub rather than real ``npm ci``.

    Ordered on sentinel files rather than on sleeps, so both directions are
    deterministic: a flaky "the unguarded version fails" assertion would be worse
    than no assertion at all. One lane installs, signals, then re-reads what it
    installed; the other lane wipes and reinstalls in between. Unguarded, the first
    lane's read fails — which is the shape of all three errors in #1447. Guarded,
    the second lane cannot start until the first is finished.
    """
    script = _fake_checkout(tmp_path)

    def lanes(label: str, *, guarded: bool) -> tuple[int, int]:
        tree = tmp_path / f"{label}-modules"
        built = tmp_path / f"{label}-built"
        go = tmp_path / f"{label}-go"
        done = tmp_path / f"{label}-done"
        reader = _REINSTALL_THEN_READ.format(tree=tree, built=built, go=go)
        wiper = _WIPE_MIDWAY.format(tree=tree, done=done)

        def start(command: str) -> subprocess.Popen[str]:
            if guarded:
                return _spawn(script, command)
            # ``["/bin/sh", "-c", command]`` rather than ``shell=True``. That is
            # verbatim what ``shell=True`` does on POSIX — subprocess hardcodes
            # ``/bin/sh`` for it — so the unguarded lane runs the same text through
            # the same shell and this control is unchanged.
            #
            # Spelled as an argv because the two linters reading this line disagree
            # and only one of them can be answered with a ``noqa``. ruff's S602 is
            # silenceable; Bandit's B602 is a different tool reading the same
            # construct, it is not silenced by a ``noqa``, and SRT promotes it to
            # HIGH — so `make lint-cicd` was green while `make srt-scan` failed.
            # Removing the construct settles both tools and leaves nothing in a
            # suppression register to audit later. The absolute path also drops
            # B607 (partial executable path), which the bare ``sh`` carried.
            return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                ["/bin/sh", "-c", command], stderr=subprocess.PIPE, text=True
            )

        # The second lane starts only once the first has finished installing, so
        # which lane wipes first is decided here rather than by the scheduler.
        first = start(reader)
        second = None
        try:
            _await_file(built)
            second = start(wiper)
            if guarded:
                # The second lane is blocked on the lock, which it announces.
                deadline = time.monotonic() + 30
                assert second.stderr is not None
                seen = ""
                while time.monotonic() < deadline and "Waiting up to" not in seen:
                    seen += second.stderr.readline()
                assert "Waiting up to" in seen, seen
                assert not done.exists()
            else:
                _await_file(done)
            go.touch()
            return first.wait(timeout=30), second.wait(timeout=30)
        finally:
            for process in (first, second):
                if process is not None:
                    process.kill()

    first_rc, _ = lanes("unguarded", guarded=False)
    assert first_rc == 3, (
        "the stub did not reproduce the hazard, so the guarded half below proves "
        "nothing. A concurrent wipe-and-reinstall is supposed to cost the other "
        f"lane the module it was reading; it exited {first_rc}."
    )

    first_rc, second_rc = lanes("guarded", guarded=True)
    assert (first_rc, second_rc) == (0, 0), (
        "the same two lanes through the lock still collided "
        f"(first={first_rc}, second={second_rc})"
    )


def test_the_wait_is_bounded_and_the_bound_runs_nothing(tmp_path: Path) -> None:
    """A guard that can block forever would be a 50-minute CI timeout waiting."""
    script = _fake_checkout(tmp_path)
    holding = tmp_path / "holding"
    go = tmp_path / "go"
    never = tmp_path / "never-ran"

    holder = _spawn(
        script, f"touch {holding}; while [ ! -f {go} ]; do sleep 0.02; done"
    )
    try:
        _await_file(holding)
        started = time.monotonic()
        result = _run(
            script, f"touch {never}", UI_DEPS_LOCK_WAIT_SECONDS="1", timeout=60
        )
        waited = time.monotonic() - started
    finally:
        go.touch()
        holder.kill()
        holder.wait(timeout=30)

    assert result.returncode == 1, result.stderr
    assert "NOTHING RAN" in result.stderr
    assert "UI_DEPS_LOCK_WAIT_SECONDS" in result.stderr, (
        "the timeout message must name the knob that raises the bound; a bare "
        f"failure here is indistinguishable from the gate's own:\n{result.stderr}"
    )
    assert not never.exists(), (
        "the bound expired and the command ran anyway, which is the corruption the "
        "bound exists to refuse"
    )
    assert waited < 30, f"the 1s bound was not honoured; waited {waited:.1f}s"


def test_a_killed_holder_leaves_nothing_to_clean_up(tmp_path: Path) -> None:
    """Stale-lock recovery, which is a property of flock rather than of this script.

    The whole process group is killed, not just the shell: a SIGKILLed `make` leaves
    its children behind, and those children inherited the locked descriptor. The
    kernel drops the lock when the last of them goes, so the next run acquires it
    with no timeout, no message and no cleanup step — and the leftover lock file is
    still there, still empty, still harmless.
    """
    script = _fake_checkout(tmp_path)
    holding = tmp_path / "holding"
    holder = _spawn(script, f"touch {holding}; sleep 120")
    _await_file(holding)
    os.killpg(os.getpgid(holder.pid), signal.SIGKILL)
    holder.wait(timeout=30)

    lock_file = tmp_path / ".ui-deps.lock"
    assert lock_file.exists(), "the lock file should outlive the holder"
    assert lock_file.stat().st_size == 0, "the lock file must carry no state"

    result = _run(script, "echo recovered", UI_DEPS_LOCK_WAIT_SECONDS="1", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "recovered" in result.stdout
    assert "Waiting up to" not in result.stderr, (
        "the next run had to wait for a dead holder's lock, so a killed make does "
        f"poison the tree after all:\n{result.stderr}"
    )


def test_a_nested_invocation_does_not_wait_on_its_own_ancestor(tmp_path: Path) -> None:
    """The second, independent reason ``lint-cicd`` cannot deadlock against itself.

    ``lint-cicd`` holds no lock today, so nesting does not arise — but a target
    gaining a wrapper later is an ordinary edit, and the failure it would cause is a
    gate that hangs until the bound expires. The holder exports
    ``UI_DEPS_LOCK_HELD``, so a nested invocation runs directly.
    """
    script = _fake_checkout(tmp_path)
    inner_ran = tmp_path / "inner-ran"
    result = _run(
        script,
        f"{script} 'touch {inner_ran}'",
        UI_DEPS_LOCK_WAIT_SECONDS="2",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert inner_ran.exists(), "the nested invocation deadlocked against its parent"
    assert "Waiting up to" not in result.stderr, result.stderr


def test_a_checkout_that_cannot_lock_runs_anyway_and_says_so(tmp_path: Path) -> None:
    """Fail open, loudly. macOS ships no ``flock(1)``.

    A guard that refuses to run the gate where it cannot lock buys nothing — the
    gate still has to run, and the hazard needs two lanes. What is not acceptable is
    running without the guard silently, which is the state this whole issue was in.
    """
    script = _fake_checkout(tmp_path)
    ran = tmp_path / "ran"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for tool in ("touch", "dirname"):
        resolved = shutil.which(tool)
        assert resolved, f"{tool} is needed to drive this test"
        (stub_bin / tool).symlink_to(resolved)

    result = _run(script, f"touch {ran}", PATH=str(stub_bin))
    assert result.returncode == 0, result.stderr
    assert ran.exists(), "the gate must still run where locking is impossible"
    assert "cannot lock" in result.stderr
    assert "1447" in result.stderr, (
        "the fail-open note must point at the hazard it is no longer guarding"
    )
