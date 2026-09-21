# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the shared-branch guard: the PreToolUse hook and the pre-push hook.

The two failure modes pull in opposite directions, as they do for
``check_commit_text.py``. A guard that misses ``git push origin develop`` is
useless; a guard that refuses a legitimate push to a feature branch gets
uninstalled by the first person it inconveniences. So the allow cases here are as
load-bearing as the refusals, and several of them exist because the obvious
implementation gets them wrong: ``git push origin feature/develop-notes`` names a
shared branch as a substring, ``git status && git log`` contains no push at all,
and ``git commit`` on a feature branch is the overwhelmingly common case.

The destination-resolution tests run against a **real temporary repository** with
real branches and upstreams rather than a mocked ``git``, because the cases worth
covering — a bare ``git push`` with an upstream, ``HEAD`` as a refspec, ``--all``
— are precisely the ones where the answer comes from git rather than from the
command line.

Both hooks are also asserted to be *reachable*: the PreToolUse one registered in
``.claude/settings.json``, and the pre-push one installable by the documented
``make`` target. A correct script nothing invokes protects nothing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "scripts" / "hooks" / "check_shared_branch.py"
PRE_PUSH = REPO_ROOT / "scripts" / "hooks" / "pre-push"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
MAKEFILE = REPO_ROOT / "Makefile"

sys.path.insert(0, str(HOOK.parent))

import check_shared_branch  # noqa: E402
from check_shared_branch import (  # noqa: E402
    decide,
    merge_target,
    push_destinations,
    segments,
    split_assignments,
    subcommand,
)

ZERO = "0" * 40
ONE = "1" * 40


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _run_hook(
    payload: object, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def _bash(command: str, cwd: Path | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return payload


def _run_pre_push(
    refs: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["sh", str(PRE_PUSH), "origin", "git@example.invalid:x/y.git"],
        input=refs,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repository with develop, main and a feature branch.

    ``core.hooksPath`` is pointed at an empty directory so the ambient
    git-defender installation on a managed machine does not run against these
    fixture commits.
    """
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(work), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-q", "-b", "develop")
    git("config", "core.hooksPath", str(hooks))
    git("remote", "add", "origin", "git@example.invalid:x/y.git")
    git("config", "user.email", "dev@example.invalid")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    (work / "a.txt").write_text("hi\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "initial")
    git("branch", "main")
    git("branch", "feature/thing")
    # A remote-tracking ref without a real remote: enough for @{upstream}.
    git("update-ref", "refs/remotes/origin/develop", "HEAD")
    git("update-ref", "refs/remotes/origin/feature/thing", "HEAD")
    return work


def _checkout(repo: Path, branch: str, upstream: str | None = None) -> None:
    """Check out ``branch``, optionally tracking ``upstream`` (``origin/<name>``).

    The upstream is written as config rather than set with ``git branch
    --set-upstream-to``, which refuses a remote-tracking ref that no fetch
    refspec produced.
    """

    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    git("switch", "-q", branch)
    if upstream:
        remote, _, name = upstream.partition("/")
        git("config", f"branch.{branch}.remote", remote)
        git("config", f"branch.{branch}.merge", f"refs/heads/{name}")


# --------------------------------------------------------------------------- #
# command parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_segments_splits_on_shell_operators() -> None:
    parsed = segments("git status && git push origin develop")
    assert parsed == [["git", "status"], ["git", "push", "origin", "develop"]]


@pytest.mark.unit
def test_segments_survives_an_unbalanced_quote() -> None:
    """A heredoc body routinely breaks shlex; the segment must still be seen."""
    parsed = segments("git push origin develop -m \"it's fine")
    assert parsed[0][:4] == ["git", "push", "origin", "develop"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git push origin develop", ["push", "origin", "develop"]),
        ("git -C /tmp/x push origin develop", ["push", "origin", "develop"]),
        ("git -c user.name=x commit -m y", ["commit", "-m", "y"]),
        ("git --no-pager log", ["log"]),
    ],
)
def test_subcommand_skips_global_options(command: str, expected: list[str]) -> None:
    assert subcommand(segments(command)[0], "git") == expected


@pytest.mark.unit
@pytest.mark.parametrize("command", ["gh pr list", "make commit", "./git push"])
def test_subcommand_returns_none_for_another_program(command: str) -> None:
    assert subcommand(segments(command)[0], "git") is None


@pytest.mark.unit
def test_inline_assignments_are_peeled_off() -> None:
    overrides, argv = split_assignments(
        ["ALLOW_SHARED_BRANCH=1", "FOO=bar", "git", "push"]
    )
    assert overrides == {"ALLOW_SHARED_BRANCH": "1", "FOO": "bar"}
    assert argv == ["git", "push"]


@pytest.mark.unit
def test_split_assignments_tolerates_an_empty_segment() -> None:
    assert split_assignments([]) == ({}, [])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["merge", "1050"], "1050"),
        (["merge", "--squash", "1050"], "1050"),
        (["merge", "--admin"], None),
        (["merge"], None),
    ],
)
def test_merge_target(args: list[str], expected: str | None) -> None:
    assert merge_target(args) == expected


# --------------------------------------------------------------------------- #
# push destination resolution, against a real repository
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["origin", "develop"], {"develop"}),
        (["origin", "main"], {"main"}),
        (["-u", "origin", "develop"], {"develop"}),
        (["--force", "origin", "develop"], {"develop"}),
        (["--force-with-lease", "origin", "main"], {"main"}),
        (["origin", "+develop"], {"develop"}),
        (["origin", "HEAD:develop"], {"develop"}),
        (["origin", "feature/thing:develop"], {"develop"}),
        (["origin", "HEAD:refs/heads/main"], {"main"}),
        # Deleting a shared branch remotely is a write to it.
        (["origin", ":develop"], {"develop"}),
        # A value-taking option's value must not be read as a refspec.
        (["-o", "develop", "origin", "feature/thing"], {"feature/thing"}),
        # Names that merely contain a shared branch name are not one.
        (["origin", "feature/develop-notes"], {"feature/develop-notes"}),
        (["origin", "mainline"], {"mainline"}),
    ],
)
def test_push_destinations_from_explicit_refspecs(
    repo: Path, args: list[str], expected: set[str]
) -> None:
    assert push_destinations(args, repo) == expected


@pytest.mark.unit
def test_bare_push_resolves_through_the_checked_out_branch(repo: Path) -> None:
    _checkout(repo, "develop", "origin/develop")
    assert push_destinations([], repo) == {"develop"}


@pytest.mark.unit
def test_bare_push_on_a_feature_branch_targets_nothing_shared(repo: Path) -> None:
    _checkout(repo, "feature/thing", "origin/feature/thing")
    assert push_destinations([], repo) == {"feature/thing"}


@pytest.mark.unit
def test_bare_push_sees_a_feature_branch_tracking_develop(repo: Path) -> None:
    """The case a branch-name-only check misses: renamed local, shared upstream."""
    _checkout(repo, "feature/thing", "origin/develop")
    assert push_destinations([], repo) == {"feature/thing", "develop"}


@pytest.mark.unit
def test_push_head_resolves_to_the_current_branch(repo: Path) -> None:
    _checkout(repo, "develop")
    assert push_destinations(["origin", "HEAD"], repo) == {"develop"}


@pytest.mark.unit
@pytest.mark.parametrize("flag", ["--all", "--mirror"])
def test_pushing_every_branch_reports_the_shared_ones(repo: Path, flag: str) -> None:
    assert push_destinations([flag, "origin"], repo) == {"develop", "main"}


# --------------------------------------------------------------------------- #
# end-to-end: the hook's exit code and message
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "git push origin develop",
        "git push origin main",
        "git push --force origin develop",
        "git push -u origin HEAD:develop",
        "git status && git push origin develop",
    ],
)
def test_pushes_to_a_shared_branch_are_blocked(repo: Path, command: str) -> None:
    result = _run_hook(_bash(command, cwd=repo))
    assert result.returncode == 2, result.stderr
    assert "push directly to" in result.stderr
    assert "ALLOW_SHARED_BRANCH=1" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "git push origin feature/thing",
        "git push -u origin feature/develop-notes",
        "git status",
        "git log --oneline -5",
        "gh pr create --base develop --title x --body y",
        # Reading about a shared branch is not writing to one.
        "git diff develop",
        "git grep -n 'push origin develop'",
        "git switch -c fix/thing",
    ],
)
def test_ordinary_commands_are_allowed(repo: Path, command: str) -> None:
    _checkout(repo, "feature/thing", "origin/feature/thing")
    result = _run_hook(_bash(command, cwd=repo))
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_commit_on_a_shared_branch_is_blocked(repo: Path) -> None:
    _checkout(repo, "develop", "origin/develop")
    result = _run_hook(_bash("git commit -m 'fix: thing'", cwd=repo))
    assert result.returncode == 2, result.stderr
    assert "commit onto develop" in result.stderr


@pytest.mark.unit
def test_commit_on_a_feature_branch_tracking_develop_is_blocked(repo: Path) -> None:
    _checkout(repo, "feature/thing", "origin/develop")
    result = _run_hook(_bash("git commit -m 'fix: thing'", cwd=repo))
    assert result.returncode == 2, result.stderr
    assert "tracking develop" in result.stderr


@pytest.mark.unit
def test_commit_on_a_feature_branch_is_allowed(repo: Path) -> None:
    _checkout(repo, "feature/thing", "origin/feature/thing")
    result = _run_hook(_bash("git commit -m 'fix: thing'", cwd=repo))
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "ALLOW_SHARED_BRANCH=1 git push origin develop",
        "ALLOW_SHARED_BRANCH=1 git commit -m x",
    ],
)
def test_inline_override_allows_the_command(repo: Path, command: str) -> None:
    """Inline is the only form available mid-session, so it has to work."""
    _checkout(repo, "develop", "origin/develop")
    result = _run_hook(_bash(command, cwd=repo))
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_environment_override_allows_the_command(repo: Path) -> None:
    _checkout(repo, "develop", "origin/develop")
    result = _run_hook(
        _bash("git push origin develop", cwd=repo), env={"ALLOW_SHARED_BRANCH": "1"}
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Read", "tool_input": {"file_path": "x"}},
        {"tool_name": "Bash", "tool_input": {"command": ""}},
        {"tool_name": "Bash"},
        {"tool_name": "Bash", "tool_input": None},
        {},
        [],
        "not an object",
    ],
)
def test_hook_allows_rather_than_wedging_on_anything_unexpected(
    payload: object,
) -> None:
    assert _run_hook(payload).returncode == 0


@pytest.mark.unit
def test_hook_allows_on_malformed_stdin() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(HOOK)],
        input="{not json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_hook_allows_when_the_destination_cannot_be_resolved(tmp_path: Path) -> None:
    """Unknown destination means allow: this is a safety net, not the only control.

    A bare ``git push`` outside a repository is the case where git can answer
    nothing. An explicit ``git push origin develop`` is still refused there,
    because the destination is on the command line and needs no repository.
    """
    assert _run_hook(_bash("git push", cwd=tmp_path)).returncode == 0
    assert _run_hook(_bash("git push origin develop", cwd=tmp_path)).returncode == 2


# --------------------------------------------------------------------------- #
# merging a red pull request
#
# These call decide() in-process so the check state can be substituted: the real
# reader is a network call to `gh pr checks`, which a unit test must not make.
# --------------------------------------------------------------------------- #
@pytest.fixture
def checks(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Substitute the check reader; the list is what it will report as failing."""
    holder: list[list[str]] = [[]]
    monkeypatch.setattr(
        check_shared_branch, "failing_checks", lambda pr, repo: holder[0]
    )
    return holder


@pytest.mark.unit
def test_merging_with_a_failing_check_is_blocked(
    repo: Path, checks: list[list[str]]
) -> None:
    checks[0] = ["developer_tests", "srt_security_review"]
    reason = decide(_bash("gh pr merge 1050 --squash", cwd=repo))
    assert reason is not None
    assert "developer_tests" in reason
    assert "srt_security_review" in reason
    assert "ALLOW_RED_MERGE=1" in reason


@pytest.mark.unit
def test_merging_with_no_failing_check_is_allowed(
    repo: Path, checks: list[list[str]]
) -> None:
    checks[0] = []
    assert decide(_bash("gh pr merge 1050 --squash", cwd=repo)) is None


@pytest.mark.unit
def test_merging_red_is_allowed_with_the_override(
    repo: Path, checks: list[list[str]]
) -> None:
    checks[0] = ["developer_tests"]
    assert decide(_bash("ALLOW_RED_MERGE=1 gh pr merge 1050", cwd=repo)) is None


@pytest.mark.unit
def test_unreadable_checks_do_not_block_a_merge(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No token, no network, or a fork PR with no checks at all: allow and say so.

    This is the guard's main residual and it is deliberate — refusing a pull
    request whose checks never ran would block every fork contribution, which
    gets no GitHub CI here.
    """
    monkeypatch.setattr(check_shared_branch, "failing_checks", lambda pr, repo: None)
    assert decide(_bash("gh pr merge 1050", cwd=repo)) is None


@pytest.mark.unit
def test_other_gh_pr_commands_do_not_read_checks(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `gh pr merge` should pay for a network call."""

    def _fail(_pr: str | None, _repo: Path) -> list[str]:
        raise AssertionError("failing_checks called for a non-merge command")

    monkeypatch.setattr(check_shared_branch, "failing_checks", _fail)
    for command in ("gh pr view 1050", "gh pr create --base develop", "gh pr list"):
        assert decide(_bash(command, cwd=repo)) is None


# --------------------------------------------------------------------------- #
# the pre-push git hook
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize("branch", ["develop", "main"])
def test_pre_push_refuses_a_shared_destination(branch: str) -> None:
    result = _run_pre_push(f"refs/heads/x {ONE} refs/heads/{branch} {ZERO}\n")
    assert result.returncode == 1, result.stdout
    assert branch in result.stderr
    assert "ALLOW_SHARED_BRANCH=1" in result.stderr


@pytest.mark.unit
def test_pre_push_refuses_a_delete_of_a_shared_branch() -> None:
    """A delete sends an all-zero local sha and still names the remote ref."""
    result = _run_pre_push(f"(delete) {ZERO} refs/heads/main {ONE}\n")
    assert result.returncode == 1, result.stdout


@pytest.mark.unit
def test_pre_push_refuses_a_batch_containing_a_shared_branch() -> None:
    refs = (
        f"refs/heads/feature/a {ONE} refs/heads/feature/a {ZERO}\n"
        f"refs/heads/develop {ONE} refs/heads/develop {ZERO}\n"
    )
    result = _run_pre_push(refs)
    assert result.returncode == 1, result.stdout
    assert "develop" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "remote_ref",
    [
        "refs/heads/feature/thing",
        "refs/heads/feature/develop-notes",
        "refs/heads/mainline",
        "refs/tags/v0.6.10",
    ],
)
def test_pre_push_allows_everything_else(remote_ref: str) -> None:
    result = _run_pre_push(f"refs/heads/x {ONE} {remote_ref} {ZERO}\n")
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_pre_push_allows_an_empty_push() -> None:
    assert _run_pre_push("").returncode == 0


@pytest.mark.unit
def test_pre_push_honours_the_override() -> None:
    result = _run_pre_push(
        f"refs/heads/x {ONE} refs/heads/develop {ZERO}\n",
        env={"ALLOW_SHARED_BRANCH": "1"},
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_pre_push_refuses_a_real_push_when_installed(
    tmp_path: Path, repo: Path
) -> None:
    """End to end against real refs, not hand-written stdin.

    The refs a push produces are git's answer, not the command line's, so the
    only way to know the hook reads them correctly is to let git supply them.
    The remote is a local bare repository, so this needs no network.
    """
    bare = tmp_path / "remote.git"
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True
    )

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    hook_dir = Path(git("rev-parse", "--git-common-dir").stdout.strip())
    if not hook_dir.is_absolute():
        hook_dir = repo / hook_dir
    hook_dir = hook_dir / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "pre-push").write_text(
        PRE_PUSH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (hook_dir / "pre-push").chmod(0o755)
    # The fixture points core.hooksPath at an empty directory to keep the ambient
    # git-defender install out of the way; aim it at the repository's own hooks.
    git("config", "core.hooksPath", str(hook_dir))
    git("remote", "set-url", "origin", str(bare))

    refused = git("push", "origin", "develop")
    assert refused.returncode != 0, refused.stdout
    assert "Refusing to push directly to" in refused.stderr

    allowed = git("push", "origin", "feature/thing")
    assert allowed.returncode == 0, allowed.stderr


@pytest.mark.unit
def test_pre_push_is_executable_and_a_posix_shell_script() -> None:
    assert os.access(PRE_PUSH, os.X_OK), f"{PRE_PUSH} is not executable"
    assert PRE_PUSH.read_text(encoding="utf-8").startswith("#!/bin/sh")


# --------------------------------------------------------------------------- #
# both halves are reachable
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_hook_is_registered_as_a_pretooluse_hook_on_bash() -> None:
    """A correct script nothing invokes protects nothing."""
    assert SETTINGS.is_file(), f"{SETTINGS} is missing; the hook is not registered"
    hooks = json.loads(SETTINGS.read_text(encoding="utf-8")).get("hooks", {})
    commands = [
        hook.get("command", "")
        for entry in hooks.get("PreToolUse", [])
        if "Bash" in (entry.get("matcher") or "")
        for hook in entry.get("hooks", [])
    ]
    assert any("check_shared_branch.py" in c for c in commands), (
        "no PreToolUse hook on Bash runs scripts/hooks/check_shared_branch.py; "
        f"found {commands}"
    )
    # The pre-existing guard must not have been displaced by this one.
    assert any("check_commit_text.py" in c for c in commands), (
        f"check_commit_text.py is no longer registered; found {commands}"
    )


@pytest.mark.unit
def test_pre_push_has_an_install_target() -> None:
    """An uninstalled hook protects nobody, so the documented route must exist."""
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^install-git-hooks:", makefile, re.MULTILINE), (
        "no install-git-hooks target in the Makefile"
    )
    assert "scripts/hooks/pre-push" in makefile
