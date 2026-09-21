# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the shared-branch guard: the PreToolUse hook and the pre-push hook.

The two failure modes pull in opposite directions, as they do for
``check_commit_text.py``. A guard that misses ``git push origin develop`` is
useless; a guard that refuses a legitimate push to a feature branch gets
uninstalled by the first person it inconveniences. So the allow cases here are as
load-bearing as the refusals, and several of them exist because the obvious
implementation gets them wrong: ``git push origin feature/develop-notes`` names a
shared branch as a substring, ``git switch -c fix/x && git commit`` leaves the
shared branch before committing, and a heredoc body containing the line ``git
push origin develop`` is a document rather than a command.

The destination-resolution tests run against a **real temporary repository** with
real branches and upstreams rather than a mocked ``git``, because the cases worth
covering — a bare ``git push`` with an upstream, ``HEAD`` as a refspec, ``--all``
— are precisely the ones where the answer comes from git rather than from the
command line.

Three things are exercised end to end rather than in pieces, because each one
hides a way for the guard to be present and ineffective:

* The pre-push hook invoked by **git**, against a local bare remote.
* The pre-push hook invoked through a **hook runner** at a redirected
  ``core.hooksPath`` that forwards the arguments but not stdin. That is the
  shape a managed developer machine produces, and under it the hook receives no
  ref list at all — so a suite that only ever points ``core.hooksPath`` at the
  repository's own hooks would report a working guard that does nothing.
* The **install target**, run for real in a temporary repository, including the
  case where the copy fails. Reporting success after a failed copy is the defect
  that shaped the recipe.
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
    chdir_target,
    decide,
    failing_checks,
    gh_repo_selector,
    inline_config,
    merge_target,
    push_destinations,
    segments,
    split_assignments,
    strip_heredocs,
    subcommand,
    switch_target,
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
    refs: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the hook with ``refs`` on stdin.

    ``cwd`` matters only when ``refs`` is empty: that is the case where the hook
    falls back to asking git about ``HEAD``. The default is a directory that is
    not a repository, so the fallback finds nothing and allows.
    """
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["sh", str(PRE_PUSH), "origin", "git@example.invalid:x/y.git"],
        input=refs,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
    )


def _git_in(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repository with develop, main and a feature branch.

    ``core.hooksPath`` is pointed at an empty directory so that a hook runner
    installed system-wide on the developer's machine does not run against these
    fixture commits. ``push.default`` is pinned because the guard reads it, and
    a contributor whose global config sets ``matching`` would otherwise see
    different answers from these tests than CI does.
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
    git("config", "push.default", "simple")
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
@pytest.mark.parametrize(
    "command",
    [
        "git status;git push origin develop",
        "git status\ngit push origin develop",
        "(git push origin develop)",
        "git status | cat && git push origin develop",
    ],
)
def test_segments_finds_a_push_after_any_separator(command: str) -> None:
    """A separator with no surrounding spaces, a newline, and a subshell."""
    assert ["git", "push", "origin", "develop"] in segments(command)


@pytest.mark.unit
def test_segments_survives_an_unbalanced_quote() -> None:
    """An apostrophe in a message breaks shlex; the segment must still be seen."""
    parsed = segments("git push origin develop -m \"it's fine")
    assert parsed[0][:4] == ["git", "push", "origin", "develop"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "echo 'run: git push origin develop; done'",
        "ssh buildhost 'cd /srv/repo; git push origin develop'",
        "python3 tool.py 'setup; git push origin develop'",
        "grep -rn 'x; git push origin develop' docs/",
    ],
)
def test_a_separator_inside_quotes_does_not_make_a_command(command: str) -> None:
    """Quoted text is an argument. Splitting before tokenizing gets this wrong."""
    for tokens in segments(command):
        assert tokens[:2] != ["git", "push"]


@pytest.mark.unit
def test_heredoc_bodies_are_not_commands() -> None:
    """Writing a document that contains a command must not be running it."""
    command = (
        "cat > docs/release.md <<'EOF'\n"
        "## Publishing\n"
        "\n"
        "git push origin develop\n"
        "\n"
        "EOF\n"
        "echo done"
    )
    stripped = strip_heredocs(command)
    assert "git push origin develop" not in stripped
    assert "cat > docs/release.md" in stripped
    assert "echo done" in stripped
    for tokens in segments(command):
        assert tokens[:2] != ["git", "push"]


@pytest.mark.unit
@pytest.mark.parametrize("opener", ["<<EOF", "<<'EOF'", '<<"EOF"', "<<-EOF"])
def test_every_heredoc_spelling_is_stripped(opener: str) -> None:
    command = f"cat > f {opener}\ngit push origin main\nEOF"
    assert "git push origin main" not in strip_heredocs(command)


@pytest.mark.unit
@pytest.mark.parametrize(
    "first",
    [
        'echo "shift <<Foo left"',
        "echo 'a <<EOF b'",
        'git commit -m "shift <<Foo left"',
        'git log --grep="<<HEREDOC"',
    ],
)
def test_a_quoted_redirection_does_not_start_a_heredoc(first: str) -> None:
    """The mirror image of the finding that made ``segments`` quote-aware.

    A ``<<`` inside quotes opens nothing. Treating it as a heredoc swallows every
    following line as body text — so the ``git push origin develop`` after it is
    never examined, and the guard's most important refusal is skipped by a command
    that merely mentions ``<<`` in a string.
    """
    command = f"{first}\ngit push origin develop"
    assert "git push origin develop" in strip_heredocs(command)
    assert ["git", "push", "origin", "develop"] in segments(command)


@pytest.mark.unit
def test_a_real_heredoc_after_a_quoted_one_still_strips() -> None:
    command = (
        'echo "mentions <<Foo"\n'
        "cat > f <<'EOF'\n"
        "git push origin develop\n"
        "EOF\n"
        "git push origin main"
    )
    stripped = strip_heredocs(command)
    assert "git push origin develop" not in stripped
    assert "git push origin main" in stripped


@pytest.mark.unit
def test_a_here_string_is_not_a_heredoc() -> None:
    """`<<<` takes its data inline, so no following line is body text."""
    command = "cat <<<word\ngit push origin develop"
    assert "git push origin develop" in strip_heredocs(command)


@pytest.mark.unit
def test_an_escaped_quote_does_not_end_a_double_quoted_string() -> None:
    r"""``\"`` keeps the string open, so a ``<<`` after it is still quoted.

    Reading the escape as a closing quote puts the scanner outside the string and
    the ``<<Foo`` back in play as a heredoc opener, which swallows the push on the
    next line. Single quotes have no escapes, so ``'a \'`` genuinely does end
    there and a ``<<`` after it genuinely does open one.
    """
    quoted = 'echo "a \\" b <<Foo"\ngit push origin develop'
    assert "git push origin develop" in strip_heredocs(quoted)

    literal = "echo 'a \\'\ncat <<Foo\ngit push origin develop\nFoo"
    assert "git push origin develop" not in strip_heredocs(literal)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git push origin develop", ["push", "origin", "develop"]),
        ("git -C /tmp/x push origin develop", ["push", "origin", "develop"]),
        ("git -c user.name=x commit -m y", ["commit", "-m", "y"]),
        ("git --no-pager log", ["log"]),
        (
            "git --git-dir=/tmp/x/.git push origin develop",
            ["push", "origin", "develop"],
        ),
        # A separate-value global option must be listed, or its value is read as
        # the subcommand and the push goes unrecognised.
        ("git --namespace ns push origin develop", ["push", "origin", "develop"]),
        ("git --config-env K=V push origin develop", ["push", "origin", "develop"]),
        ("git -C push push origin develop", ["push", "origin", "develop"]),
    ],
)
def test_subcommand_skips_global_options(command: str, expected: list[str]) -> None:
    assert subcommand(segments(command)[0], "git") == expected


@pytest.mark.unit
@pytest.mark.parametrize("command", ["gh pr list", "make commit", "./git push"])
def test_subcommand_returns_none_for_another_program(command: str) -> None:
    assert subcommand(segments(command)[0], "git") is None


@pytest.mark.unit
def test_inline_config_reads_command_line_settings() -> None:
    """`-c push.default=matching` changes the answer and is invisible to config."""
    tokens = segments("git -c push.default=matching -c core.quotePath=false push")[0]
    assert inline_config(tokens) == {
        "push.default": "matching",
        "core.quotePath": "false",
    }


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
        (["switch", "fix/x"], (True, "fix/x")),
        (["switch", "-c", "fix/x"], (True, "fix/x")),
        (["checkout", "-b", "fix/x"], (True, "fix/x")),
        (["checkout", "-b", "fix/x", "origin/develop"], (True, "fix/x")),
        (["checkout", "develop"], (True, "develop")),
        # `--track`/`-t` names no new branch: git takes the remote ref's leaf, so
        # HEAD lands on local `develop`. Reading the positional as-is misses it.
        (["switch", "--track", "origin/develop"], (True, "develop")),
        (["checkout", "-t", "origin/develop"], (True, "develop")),
        (["switch", "--track=direct", "origin/main"], (True, "main")),
        (["switch", "--track", "origin/feature/thing"], (True, "feature/thing")),
        # With an explicit new name the option's value is the branch, not the leaf.
        (["checkout", "-b", "new", "-t", "origin/develop"], (True, "new")),
        # The new name is read from the create option wherever it sits. Taking the
        # first positional instead answers `origin/x` here and lets a commit onto
        # the new `develop` through.
        (["checkout", "-t", "origin/x", "-b", "develop"], (True, "develop")),
        (["switch", "-t", "origin/x", "-c", "main"], (True, "main")),
        (["switch", "--create=develop"], (True, "develop")),
        (["switch", "--orphan", "develop"], (True, "develop")),
        (["checkout", "-B", "main", "origin/main"], (True, "main")),
        # A create flag with no value is a malformed command, and naming no branch
        # is what leaves the following segments unjudged rather than misjudged.
        (["switch", "-c"], (True, None)),
        # Without --track this detaches at the remote ref rather than switching.
        (["checkout", "origin/develop"], (True, "origin/develop")),
        # Moves HEAD somewhere this cannot name: later segments are unjudgeable.
        (["switch", "-"], (True, None)),
        (["checkout", "--detach"], (True, None)),
        # Restores files; HEAD does not move.
        (["checkout", "develop", "--", "a.txt"], (False, None)),
    ],
)
def test_switch_target(args: list[str], expected: tuple[bool, str | None]) -> None:
    assert switch_target(args) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["cd", "/tmp/elsewhere"], Path("/tmp/elsewhere")),
        (["pushd", "/tmp/elsewhere"], Path("/tmp/elsewhere")),
        (["cd", "sub/dir"], Path("/base/sub/dir")),
        (["pushd", "sub"], Path("/base/sub")),
        (["cd"], Path.home()),
        # No directory stack is kept, so these are not followed at all.
        (["cd", "-"], None),
        (["pushd"], None),
        (["popd"], None),
        # Not a directory change.
        (["git", "push", "origin", "develop"], None),
        (["cdx", "/tmp"], None),
    ],
)
def test_chdir_target(argv: list[str], expected: Path | None) -> None:
    """Which directory a later segment is judged against.

    ``cd <repo> && git commit`` has to be judged against ``<repo>``: the payload's
    ``cwd`` is where the command starts, not where the git command runs.
    """
    assert chdir_target(argv, Path("/base")) == expected


@pytest.mark.unit
def test_chdir_target_expands_a_home_relative_path() -> None:
    assert chdir_target(["cd", "~/work"], Path("/base")) == Path.home() / "work"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["merge", "1050"], "1050"),
        (["merge", "--squash", "1050"], "1050"),
        (["merge", "--admin"], None),
        (["merge"], None),
        (["merge", "--repo", "owner/name", "1050"], "1050"),
        (["merge", "--repo=owner/name", "1050"], "1050"),
        (["merge", "-d", "-s", "1050"], "1050"),
    ],
)
def test_merge_target(args: list[str], expected: str | None) -> None:
    assert merge_target(args) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["gh", "pr", "merge", "1050"], None),
        (["gh", "pr", "merge", "--repo", "owner/name", "1050"], "owner/name"),
        (["gh", "pr", "merge", "-R", "owner/name", "1050"], "owner/name"),
        (["gh", "pr", "merge", "--repo=owner/name", "1050"], "owner/name"),
        # gh registers --repo on its root command, so it is valid here too.
        (["gh", "-R", "owner/name", "pr", "merge", "1050"], "owner/name"),
        (["gh", "--repo=owner/name", "pr", "merge", "1050"], "owner/name"),
    ],
)
def test_gh_repo_selector(tokens: list[str], expected: str | None) -> None:
    """Which repository's checks get read.

    Losing the selector asks about a pull request of the same number in whatever
    repository the command runs in — a different question with the same shape of
    answer, so nothing about the result looks wrong.
    """
    assert gh_repo_selector(tokens) == expected


@pytest.mark.unit
def test_a_merge_with_the_repo_before_the_subcommand_is_still_a_merge(
    repo: Path, checks: list[list[str]]
) -> None:
    """``gh -R owner/name pr merge 1050`` is a working gh invocation.

    Reading ``owner/name`` as the subcommand makes it not a merge at all, so the
    check never runs — the same silent shape as reading a flag's value as the pull
    request number.
    """
    checks[0] = ["security-checks / srt"]
    blocked = _bash("gh -R owner/name pr merge 1050", cwd=repo)
    assert decide(blocked) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    "flag",
    [
        "-b",
        "--body",
        "-F",
        "--body-file",
        "-t",
        "--subject",
        "--match-head-commit",
        "-A",
        "--author-email",
    ],
)
def test_a_merge_flags_value_is_not_read_as_the_pull_request(flag: str) -> None:
    """Reading it as the pull request disables the check *silently*.

    ``gh pr checks <that value>`` errors, the error reads as "cannot tell", and the
    merge is allowed with nothing printed. Parametrising over the flags is the
    point: the earlier two cases were ``--squash`` and ``--admin``, neither of
    which takes a value, so the suite could not see this.
    """
    assert merge_target(["merge", flag, "some value", "1050"]) == "1050"
    assert merge_target(["merge", "--squash", flag, "some value", "1050"]) == "1050"


#: A line in ``gh``'s help listing one option. ``gh`` pads the description to a
#: column, so the placeholder that marks an option as value-taking is the token
#: separated from the option by exactly one space -- ``-b, --body text`` takes a
#: value and ``--admin    Use administrator...`` does not.
GH_FLAG_LINE = re.compile(r"^\s+(?:(-\w), )?(--[\w-]+)(?: (?!\s)(\S+))?\s{2,}\S")


def _gh_flags_taking_a_value() -> set[str] | None:
    """Value-taking ``gh pr merge`` options, read from ``gh``'s own help.

    ``None`` when ``gh`` is not installed. The parse is asserted non-vacuous by
    the caller: silently matching nothing would make the comparison below pass for
    every possible value of the constant, which is the failure mode of a derived
    universe that nobody checks.
    """
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["gh", "pr", "merge", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    flags: set[str] = set()
    seen = 0
    for line in completed.stdout.splitlines():
        match = GH_FLAG_LINE.match(line)
        if not match:
            continue
        seen += 1
        if match.group(3) is None:
            continue
        if match.group(1):
            flags.add(match.group(1))
        flags.add(match.group(2))
    assert seen >= 8, (
        f"only {seen} option lines were recognised in `gh pr merge --help`, so "
        "its format has changed and this test can no longer see which options "
        f"take a value. Re-read the help and update GH_FLAG_LINE.\n"
        f"{completed.stdout}"
    )
    return flags


@pytest.mark.unit
def test_every_value_taking_merge_flag_is_listed() -> None:
    """The set has to match ``gh pr merge`` itself, so derive it from the help.

    Both directions fail. An option ``gh`` takes a value for and the constant does
    not list is a **silent** hole: its value is read as the pull request number,
    ``gh pr checks <that value>`` errors, the error reads as "cannot tell", and the
    red merge is allowed with nothing printed. An entry ``gh`` no longer takes a
    value for is stale, and swallowing the following token could equally swallow
    the pull request number.

    Skipped when ``gh`` is absent, which is the only reason this is not a hard
    gate on every machine.
    """
    derived = _gh_flags_taking_a_value()
    if derived is None:
        pytest.skip("gh is not installed")
    listed = set(check_shared_branch.GH_MERGE_FLAGS_WITH_VALUE)
    assert derived == listed, (
        "GH_MERGE_FLAGS_WITH_VALUE does not match `gh pr merge --help`.\n"
        f"  gh takes a value for, not listed: {sorted(derived - listed)}\n"
        f"  listed, gh takes no value for:    {sorted(listed - derived)}"
    )


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
        (["origin", "heads/develop"], {"develop"}),
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
@pytest.mark.parametrize("shorthand", ["HEAD", "@"])
def test_head_shorthands_resolve_to_the_current_branch(
    repo: Path, shorthand: str
) -> None:
    """`@` is git's documented shorthand for HEAD and reaches the same branch."""
    _checkout(repo, "develop")
    assert push_destinations(["origin", shorthand], repo) == {"develop"}


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
@pytest.mark.parametrize("flag", ["--all", "--mirror"])
def test_pushing_every_branch_reports_the_shared_ones(repo: Path, flag: str) -> None:
    assert push_destinations([flag, "origin"], repo) == {"develop", "main"}


@pytest.mark.unit
def test_push_default_matching_enumerates_every_branch(repo: Path) -> None:
    """`matching` pushes every same-named branch, not just the current one."""
    _checkout(repo, "feature/thing", "origin/feature/thing")
    assert push_destinations([], repo, inline={"push.default": "matching"}) == {
        "develop",
        "main",
    }
    # Read from config rather than the command line, the answer is the same.
    _git_in(repo, "config", "push.default", "matching")
    assert push_destinations(["origin"], repo) == {"develop", "main"}


@pytest.mark.unit
def test_push_default_simple_does_not_enumerate(repo: Path) -> None:
    _checkout(repo, "feature/thing", "origin/feature/thing")
    assert push_destinations([], repo, inline={"push.default": "simple"}) == {
        "feature/thing"
    }


@pytest.mark.unit
def test_push_default_current_ignores_a_differently_named_upstream(repo: Path) -> None:
    """Measured: on `fix/x` tracking `origin/develop`, `push.default=current`
    pushes `fix/x -> fix/x` and leaves develop alone. Reading the upstream too is
    a false refusal, and this is the setting where it bites -- under `simple` git
    itself refuses a mismatched-name push, so over-approximating is free there.
    """
    _checkout(repo, "feature/thing", "origin/develop")
    assert push_destinations([], repo, inline={"push.default": "current"}) == {
        "feature/thing"
    }


@pytest.mark.unit
@pytest.mark.parametrize("setting", ["upstream", "tracking"])
def test_push_default_upstream_ignores_the_branch_name(
    repo: Path, setting: str
) -> None:
    """The mirror image: these two push to the upstream, whatever HEAD is called."""
    _checkout(repo, "feature/thing", "origin/develop")
    assert push_destinations([], repo, inline={"push.default": setting}) == {"develop"}


@pytest.mark.unit
def test_push_default_simple_still_reads_both(repo: Path) -> None:
    """`simple` requires the two to agree, so the union is the safe reading."""
    _checkout(repo, "feature/thing", "origin/develop")
    assert push_destinations([], repo) == {"feature/thing", "develop"}


@pytest.mark.unit
def test_tags_only_pushes_have_no_branch_destination(repo: Path) -> None:
    """Measured: `git push origin --tags` sends refs/tags and no branch.

    Refusing it while HEAD sits on develop is a false refusal on an ordinary
    machine, so it is not the documented managed-machine cost either.
    """
    _checkout(repo, "develop", "origin/develop")
    assert push_destinations(["origin", "--tags"], repo) == set()
    assert push_destinations(["--tags"], repo) == set()
    # An explicit refspec alongside --tags is still a branch destination, and
    # --follow-tags sends the branch as well as the tags.
    assert push_destinations(["--tags", "origin", "develop"], repo) == {"develop"}
    assert push_destinations(["--follow-tags", "origin"], repo) == {"develop"}


@pytest.mark.unit
def test_a_switched_head_is_used_instead_of_the_checked_out_branch(repo: Path) -> None:
    _checkout(repo, "feature/thing", "origin/feature/thing")
    assert push_destinations([], repo, head="develop") == {"develop"}
    assert push_destinations(["origin", "HEAD"], repo, head="develop") == {"develop"}


@pytest.mark.unit
def test_an_unresolvable_head_makes_a_bare_push_unknown(repo: Path) -> None:
    _checkout(repo, "develop", "origin/develop")
    assert push_destinations([], repo, head_known=False) is None
    # An explicit refspec needs no HEAD, so it is still resolved.
    assert push_destinations(["origin", "develop"], repo, head_known=False) == {
        "develop"
    }


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
        "git push origin @",
        "git --namespace ns push origin develop",
        "(git push origin develop)",
        "git -c push.default=matching push origin",
    ],
)
def test_pushes_to_a_shared_branch_are_blocked(repo: Path, command: str) -> None:
    _checkout(repo, "develop", "origin/develop")
    result = _run_hook(_bash(command, cwd=repo))
    assert result.returncode == 2, result.stderr
    assert "push directly to" in result.stderr
    assert "ALLOW_SHARED_BRANCH=1" in result.stderr


@pytest.mark.unit
def test_the_refusal_shows_the_inline_override_form(repo: Path) -> None:
    """An exported variable does not survive to the next tool call; inline does."""
    _checkout(repo, "develop", "origin/develop")
    result = _run_hook(_bash("git push origin develop", cwd=repo))
    assert "ALLOW_SHARED_BRANCH=1 <your command>" in result.stderr


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
        # Text that contains a command is not a command.
        "echo 'next: git push origin develop; then tag'",
        "gh pr create --base develop --body 'refuses:\ngit push origin develop\n'",
        "cat > d.md <<'EOF'\nrun: git push origin develop\nEOF",
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
def test_commit_on_a_feature_branch_tracking_develop_is_allowed(repo: Path) -> None:
    """`git checkout -b fix/x origin/develop` sets that upstream, and the commit
    lands on fix/x. Refusing it would refuse the normal way of starting work; the
    bare push from that branch is what has to be refused, and is.
    """
    _checkout(repo, "feature/thing", "origin/develop")
    assert _run_hook(_bash("git commit -m 'fix: thing'", cwd=repo)).returncode == 0
    assert _run_hook(_bash("git push", cwd=repo)).returncode == 2


@pytest.mark.unit
def test_commit_on_a_feature_branch_is_allowed(repo: Path) -> None:
    _checkout(repo, "feature/thing", "origin/feature/thing")
    result = _run_hook(_bash("git commit -m 'fix: thing'", cwd=repo))
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "git switch -c fix/new && git commit -m x",
        "git switch -c fix/new; git commit -m x",
        "git checkout -b fix/new && git add -A && git commit -m x",
        "git stash && git switch -c fix/new && git commit -m x",
        "git switch feature/thing && git commit -m x",
        # HEAD moves somewhere unnameable, so the commit is not judged.
        "git switch - && git commit -m x",
    ],
)
def test_leaving_the_shared_branch_first_is_allowed(repo: Path, command: str) -> None:
    """The remedy the refusal prints, written as one command, must work."""
    _checkout(repo, "develop", "origin/develop")
    result = _run_hook(_bash(command, cwd=repo))
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "git switch develop && git commit -m x",
        "git checkout develop && git commit --allow-empty -m x",
        "git switch main && git commit -m x",
        # The same tracking applies to the push side.
        "git switch develop && git push",
    ],
)
def test_switching_onto_a_shared_branch_first_is_blocked(
    repo: Path, command: str
) -> None:
    _checkout(repo, "feature/thing", "origin/feature/thing")
    result = _run_hook(_bash(command, cwd=repo))
    assert result.returncode == 2, result.stderr


@pytest.mark.unit
def test_commit_on_a_detached_head_is_allowed(repo: Path) -> None:
    _git_in(repo, "switch", "-q", "--detach", "HEAD")
    assert _run_hook(_bash("git commit -m x", cwd=repo)).returncode == 0


@pytest.mark.unit
def test_restoring_a_file_from_a_shared_branch_does_not_move_head(repo: Path) -> None:
    """`git checkout develop -- <path>` takes a file; the commit lands where it was."""
    _checkout(repo, "feature/thing", "origin/feature/thing")
    command = "git checkout develop -- a.txt && git commit -m x"
    assert _run_hook(_bash(command, cwd=repo)).returncode == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "git switch --track origin/develop && git commit -m x",
        "git checkout -t origin/develop && git commit -m x",
        "git switch --track origin/main && git commit -m x",
        "git checkout --track origin/develop && git push",
    ],
)
def test_tracking_a_shared_branch_then_committing_is_blocked(
    repo: Path, command: str
) -> None:
    """``--track``/``-t`` with no new name lands HEAD on the remote ref's leaf.

    This is how a fresh clone gets a local ``develop``, and reading the positional
    as the branch answers ``origin/develop`` — which is in no shared set, so the
    commit that follows was allowed.
    """
    _checkout(repo, "feature/thing", "origin/feature/thing")
    result = _run_hook(_bash(command, cwd=repo))
    assert result.returncode == 2, result.stderr


@pytest.mark.unit
def test_a_tags_only_push_from_a_shared_branch_is_allowed(repo: Path) -> None:
    """``git push origin --tags`` sends refs/tags and writes no branch.

    Refusing it is a false refusal on an ordinary machine, where the guard has the
    ref list and has no reason to fall back to HEAD.
    """
    _checkout(repo, "develop", "origin/develop")
    assert _run_hook(_bash("git push origin --tags", cwd=repo)).returncode == 0
    # `--follow-tags` sends the branch as well as the tags, so it is still refused.
    assert _run_hook(_bash("git push --follow-tags origin", cwd=repo)).returncode == 2


@pytest.mark.unit
def test_a_cd_earlier_in_the_command_decides_which_repository(
    repo: Path, tmp_path: Path
) -> None:
    """The payload's ``cwd`` is where the command starts, not where git runs."""
    _checkout(repo, "develop", "origin/develop")
    outside = tmp_path / "outside"
    outside.mkdir()

    # From a directory that is not a repository there is nothing to judge.
    assert _run_hook(_bash("git commit -m x", cwd=outside)).returncode == 0
    for command in (
        f"cd {repo} && git commit -m x",
        f"pushd {repo} && git commit -m x",
        "cd ../work && git commit -m x",
    ):
        assert _run_hook(_bash(command, cwd=outside)).returncode == 2, command


@pytest.mark.unit
def test_a_directory_stack_is_not_guessed_at(repo: Path, tmp_path: Path) -> None:
    """``cd -`` and ``popd`` are not followed, and that over-refuses.

    The judgement stays with the directory in force before them — here the
    shared-branch repository — so the commit is refused even though the command
    had returned to a directory where it would have been fine. Over-refusal is the
    safe direction and is overridable; guessing at a stack this does not keep
    would not be.
    """
    _checkout(repo, "develop", "origin/develop")
    outside = tmp_path / "outside"
    outside.mkdir()
    for command in (
        f"cd {repo} && cd - && git commit -m x",
        f"pushd {repo} && popd && git commit -m x",
    ):
        assert _run_hook(_bash(command, cwd=outside)).returncode == 2, command


@pytest.mark.unit
def test_the_commands_own_repository_option_decides_which_repo(
    repo: Path, tmp_path: Path
) -> None:
    """`git -C other commit` is judged against `other`, not the current directory."""
    other = tmp_path / "other"
    other.mkdir()
    for args in (
        ("init", "-q", "-b", "feature/x"),
        # As in the `repo` fixture: keep any system-wide hook runner from
        # rejecting these fixture commits, which would leave HEAD unborn.
        ("config", "core.hooksPath", str(tmp_path / "empty-hooks")),
        ("config", "user.email", "dev@example.invalid"),
        ("config", "user.name", "Test"),
        ("config", "commit.gpgsign", "false"),
    ):
        _git_in(other, *args)
    (other / "f.txt").write_text("x\n", encoding="utf-8")
    _git_in(other, "add", "-A")
    _git_in(other, "commit", "-q", "-m", "initial")

    _checkout(repo, "develop", "origin/develop")
    assert _run_hook(_bash(f"git -C {other} commit -m x", cwd=repo)).returncode == 0
    payload = _bash(f"git --git-dir={other}/.git commit -m x", cwd=repo)
    assert _run_hook(payload).returncode == 0

    _git_in(other, "switch", "-q", "-C", "main")
    assert _run_hook(_bash(f"git -C {other} commit -m x", cwd=repo)).returncode == 2


@pytest.mark.unit
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "y", "on"])
def test_an_affirmative_override_allows_the_command(repo: Path, value: str) -> None:
    """Inline is the only form available mid-session, so it has to work."""
    _checkout(repo, "develop", "origin/develop")
    command = f"ALLOW_SHARED_BRANCH={value} git push origin develop"
    assert _run_hook(_bash(command, cwd=repo)).returncode == 0


@pytest.mark.unit
@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_a_negative_override_leaves_the_guard_on(repo: Path, value: str) -> None:
    """Spelling out a refusal of the override must not read as granting it."""
    _checkout(repo, "develop", "origin/develop")
    command = f"ALLOW_SHARED_BRANCH={value} git push origin develop"
    assert _run_hook(_bash(command, cwd=repo)).returncode == 2


@pytest.mark.unit
def test_an_inline_override_beats_the_environment(repo: Path) -> None:
    _checkout(repo, "develop", "origin/develop")
    result = _run_hook(
        _bash("ALLOW_SHARED_BRANCH=0 git push origin develop", cwd=repo),
        env={"ALLOW_SHARED_BRANCH": "1"},
    )
    assert result.returncode == 2, result.stderr


@pytest.mark.unit
def test_environment_override_allows_the_command(repo: Path) -> None:
    _checkout(repo, "develop", "origin/develop")
    result = _run_hook(
        _bash("git push origin develop", cwd=repo), env={"ALLOW_SHARED_BRANCH": "1"}
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "env", "expected"),
    [
        (
            "ALLOW_SHARED_BRANCH=1 git push origin develop",
            {},
            "this push's destination",
        ),
        ("git push origin develop", {"ALLOW_SHARED_BRANCH": "1"}, "this push's"),
        ("git commit -m x", {"ALLOW_SHARED_BRANCH": "1"}, "the branch this commits"),
        ("ALLOW_RED_MERGE=1 gh pr merge 1050", {}, "this pull request's checks"),
        ("gh pr merge 1050", {"ALLOW_RED_MERGE": "1"}, "this pull request's checks"),
    ],
)
def test_an_honoured_override_says_which_check_it_turned_off(
    repo: Path, command: str, env: dict[str, str], expected: str
) -> None:
    """An override in the *environment* applies to every command in it.

    A shell profile, an IDE or a CI runner can export one, and then the guard is
    believed on and is off — which is the failure this whole guard exists to avoid,
    reintroduced one level up. It costs a line on stderr to say so, and the command
    still runs.
    """
    _checkout(repo, "develop", "origin/develop")
    result = _run_hook(_bash(command, cwd=repo), env=env)
    assert result.returncode == 0, result.stderr
    assert expected in result.stderr
    assert "is set" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    ["git push origin feature/thing", "git commit -m x", "git status"],
)
def test_an_allowed_command_with_no_override_is_silent(
    repo: Path, command: str
) -> None:
    """The waiver line must mark an override, not every allowed command."""
    _checkout(repo, "feature/thing", "origin/feature/thing")
    result = _run_hook(
        _bash(command, cwd=repo),
        env={"ALLOW_SHARED_BRANCH": "", "ALLOW_RED_MERGE": ""},
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


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
# --------------------------------------------------------------------------- #
@pytest.fixture
def checks(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Substitute the check reader; the list is what it will report as failing."""
    holder: list[list[str]] = [[]]
    monkeypatch.setattr(
        check_shared_branch, "failing_checks", lambda pr, repo, selector=None: holder[0]
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
    monkeypatch.setattr(
        check_shared_branch, "failing_checks", lambda pr, repo, selector=None: None
    )
    assert decide(_bash("gh pr merge 1050", cwd=repo)) is None


@pytest.mark.unit
def test_other_gh_pr_commands_do_not_read_checks(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `gh pr merge` should pay for a network call."""

    def _fail(_pr: str | None, _repo: Path, _selector: str | None = None) -> list[str]:
        raise AssertionError("failing_checks called for a non-merge command")

    monkeypatch.setattr(check_shared_branch, "failing_checks", _fail)
    for command in ("gh pr view 1050", "gh pr create --base develop", "gh pr list"):
        assert decide(_bash(command, cwd=repo)) is None


# --------------------------------------------------------------------------- #
# reading the checks for real, against a substituted `gh`
#
# Everything above monkeypatches failing_checks, which leaves the function that
# actually decides a red merge — its argv, its handling of gh's exit code, and
# the bucket it filters on — unexercised. A fake `gh` on PATH covers it without
# a network call.
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Install a stub ``gh`` on PATH; returns a setter for its output and status."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = bin_dir / "argv.txt"

    def install(stdout: str, exit_code: int = 0) -> Path:
        script = bin_dir / "gh"
        script.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" > {argv_log}\n'
            f"cat <<'GH_STUB_OUT'\n{stdout}\nGH_STUB_OUT\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return argv_log

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return install


@pytest.mark.unit
def test_failing_checks_reports_only_the_failing_bucket(
    tmp_path: Path, fake_gh
) -> None:  # noqa: ANN001
    """gh exits non-zero whenever anything is not passing, and still prints JSON.

    That is why the exit code is ignored: reading it as "could not tell" would
    silently stop the guard refusing any red merge.
    """
    payload = json.dumps(
        [
            {"bucket": "fail", "name": "SRT Security Review"},
            {"bucket": "pending", "name": "Lint, Type Check, and Test"},
            {"bucket": "pass", "name": "Dependency Audit (SCA)"},
            {"bucket": "skipping", "name": "Test Results"},
        ]
    )
    argv_log = fake_gh(payload, exit_code=1)
    assert failing_checks("1055", tmp_path) == ["SRT Security Review"]
    assert argv_log.read_text(encoding="utf-8").strip() == (
        "pr checks 1055 --json bucket,name"
    )


@pytest.mark.unit
def test_failing_checks_is_empty_when_everything_passes(
    tmp_path: Path, fake_gh
) -> None:  # noqa: ANN001
    fake_gh(json.dumps([{"bucket": "pass", "name": "developer_tests"}]), exit_code=0)
    assert failing_checks(None, tmp_path) == []


@pytest.mark.unit
def test_failing_checks_omits_the_pr_argument_when_there_is_none(
    tmp_path: Path, fake_gh
) -> None:  # noqa: ANN001
    argv_log = fake_gh("[]", exit_code=0)
    assert failing_checks(None, tmp_path) == []
    assert (
        argv_log.read_text(encoding="utf-8").strip() == "pr checks --json bucket,name"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "exit_code"),
    [
        ("gh: not authenticated. Run `gh auth login`.", 4),
        ("", 1),
        ("{}", 0),
        ('{"error": "no checks"}', 0),
        ("<html>proxy error</html>", 0),
    ],
)
def test_failing_checks_returns_none_when_it_cannot_read(
    tmp_path: Path,
    fake_gh,  # noqa: ANN001
    stdout: str,
    exit_code: int,
) -> None:
    """Unreadable is not "nothing failing": None allows, [] would too, but the
    distinction is what the merge branch documents as its residual."""
    fake_gh(stdout, exit_code=exit_code)
    assert failing_checks("1", tmp_path) is None


@pytest.mark.unit
def test_a_red_merge_is_blocked_through_the_real_reader(
    repo: Path,
    fake_gh,  # noqa: ANN001
) -> None:
    """decide() and failing_checks() together, with no monkeypatching between."""
    fake_gh(json.dumps([{"bucket": "fail", "name": "developer_tests"}]), exit_code=1)
    reason = decide(_bash("gh pr merge 1050 --squash", cwd=repo))
    assert reason is not None
    assert "developer_tests" in reason


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
    assert "the refs git supplied" in result.stderr


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
def test_pre_push_allows_an_empty_push_from_a_feature_branch(repo: Path) -> None:
    """Nothing to push and HEAD is not shared: there is nothing to refuse."""
    _checkout(repo, "feature/thing", "origin/feature/thing")
    assert _run_pre_push("", cwd=repo).returncode == 0


@pytest.mark.unit
def test_pre_push_allows_an_empty_push_outside_a_repository(tmp_path: Path) -> None:
    """The fallback can read nothing, so it allows rather than wedging."""
    assert _run_pre_push("", cwd=tmp_path).returncode == 0


@pytest.mark.unit
@pytest.mark.parametrize("branch", ["develop", "main"])
def test_pre_push_falls_back_to_head_when_given_no_refs(
    repo: Path, branch: str
) -> None:
    """No ref list is indistinguishable from a runner that dropped stdin.

    Exiting 0 there is what made this hook inert on a machine with a system-wide
    ``core.hooksPath``, so it judges by HEAD instead and says that it did.
    """
    _checkout(repo, branch, f"origin/{branch}")
    result = _run_pre_push("", cwd=repo)
    assert result.returncode == 1, result.stdout
    assert branch in result.stderr
    assert "the ref list was not supplied" in result.stderr


@pytest.mark.unit
def test_pre_push_fallback_sees_a_shared_upstream(repo: Path) -> None:
    """A feature branch tracking develop: a bare push would go to develop."""
    _checkout(repo, "feature/thing", "origin/develop")
    result = _run_pre_push("", cwd=repo)
    assert result.returncode == 1, result.stdout
    assert "develop" in result.stderr


@pytest.mark.unit
def test_pre_push_fallback_honours_the_override(repo: Path) -> None:
    _checkout(repo, "develop", "origin/develop")
    result = _run_pre_push("", env={"ALLOW_SHARED_BRANCH": "1"}, cwd=repo)
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
@pytest.mark.parametrize("value", ["1", "true", "yes", "y", "on"])
def test_pre_push_honours_an_affirmative_override(value: str) -> None:
    result = _run_pre_push(
        f"refs/heads/x {ONE} refs/heads/develop {ZERO}\n",
        env={"ALLOW_SHARED_BRANCH": value},
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_pre_push_ignores_a_negative_override(value: str) -> None:
    result = _run_pre_push(
        f"refs/heads/x {ONE} refs/heads/develop {ZERO}\n",
        env={"ALLOW_SHARED_BRANCH": value},
    )
    assert result.returncode == 1, result.stdout


@pytest.mark.unit
def test_pre_push_says_when_the_override_turned_it_off() -> None:
    """The same reasoning as the PreToolUse half's waiver line.

    ``ALLOW_SHARED_BRANCH`` exported by a shell profile, an IDE or a CI runner
    disables this hook for every push in that environment, and nothing else would
    reveal it.
    """
    result = _run_pre_push(
        f"refs/heads/x {ONE} refs/heads/develop {ZERO}\n",
        env={"ALLOW_SHARED_BRANCH": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "ALLOW_SHARED_BRANCH is set" in result.stderr


@pytest.mark.unit
def test_pre_push_names_each_branch_once(repo: Path) -> None:
    """HEAD on develop *tracking* origin/develop is one branch named twice.

    The fallback reads HEAD and its upstream, so this is the reachable duplicate
    and it read "Refusing to push directly to: develop develop".
    """
    _checkout(repo, "develop", "origin/develop")
    result = _run_pre_push("", cwd=repo)
    assert result.returncode == 1, result.stdout
    refusal = next(
        line for line in result.stderr.splitlines() if "Refusing to push" in line
    )
    assert refusal.count("develop") == 1, refusal


@pytest.mark.unit
def test_pre_push_names_each_branch_once_when_the_refs_repeat() -> None:
    """Belt and braces, as with the missing trailing newline: git sends one line
    per destination ref and so does not repeat one, but the ref list is input and
    the message should not degrade if it ever does."""
    refs = (
        f"refs/heads/develop {ONE} refs/heads/develop {ZERO}\n"
        f"refs/heads/x {ONE} refs/heads/develop {ZERO}\n"
        f"refs/heads/y {ONE} refs/heads/main {ZERO}\n"
    )
    result = _run_pre_push(refs)
    assert result.returncode == 1, result.stdout
    refusal = next(
        line for line in result.stderr.splitlines() if "Refusing to push" in line
    )
    assert refusal.count("develop") == 1, refusal
    assert refusal.count("main") == 1, refusal


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

    hook_dir = _install_hook_into(repo)
    # The fixture points core.hooksPath at an empty directory to keep any
    # system-wide hook runner out of the way; aim it at the repository's own.
    _git_in(repo, "config", "core.hooksPath", str(hook_dir))
    _git_in(repo, "remote", "set-url", "origin", str(bare))

    refused = _git_in(repo, "push", "origin", "develop")
    assert refused.returncode != 0, refused.stdout
    assert "Refusing to push directly to" in refused.stderr
    assert "the refs git supplied" in refused.stderr

    _checkout(repo, "feature/thing")
    allowed = _git_in(repo, "push", "origin", "feature/thing")
    assert allowed.returncode == 0, allowed.stderr


def _install_hook_into(repo: Path) -> Path:
    """Copy the tracked hook into ``repo``'s hooks directory; return that dir."""
    common = Path(_git_in(repo, "rev-parse", "--git-common-dir").stdout.strip())
    if not common.is_absolute():
        common = repo / common
    hook_dir = common / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    installed = hook_dir / "pre-push"
    installed.write_text(PRE_PUSH.read_text(encoding="utf-8"), encoding="utf-8")
    installed.chmod(0o755)
    return hook_dir


def _install_runner_that_drops_stdin(repo: Path, tmp_path: Path) -> Path:
    """Put ``repo`` behind a hook runner that forwards argv but not stdin.

    The shape a managed developer machine produces: ``core.hooksPath`` points at a
    directory of hook runners belonging to a security tool, which chain to the
    repository's own hook. Returns the local bare repository ``origin`` now points
    at, so a caller can check whether the remote moved.
    """
    bare = tmp_path / "remote.git"
    if not bare.exists():
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True
        )
    _git_in(repo, "remote", "set-url", "origin", str(bare))
    hook_dir = _install_hook_into(repo)

    runners = tmp_path / "runners"
    runners.mkdir(exist_ok=True)
    runner = runners / "pre-push"
    runner.write_text(
        "#!/bin/sh\n"
        "# Stands in for a managed hook runner: its own checks, then the\n"
        "# repository's hook with the arguments but WITHOUT stdin.\n"
        f'"{hook_dir}/pre-push" "$@" < /dev/null\n'
        "exit $?\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    _git_in(repo, "config", "core.hooksPath", str(runners))
    return bare


@pytest.mark.unit
def test_pre_push_still_refuses_through_a_runner_that_drops_stdin(
    tmp_path: Path, repo: Path
) -> None:
    """The shape a system-wide ``core.hooksPath`` produces.

    A managed developer machine may point ``core.hooksPath`` at a directory of
    hook runners belonging to a security tool. Those runners chain to the
    repository's own hook and forward its arguments, but not its stdin — so the
    hook sees no ref list. This is the configuration under which the hook used to
    exit 0 and let a direct push to develop through, with ``make
    install-git-hooks`` reporting success.
    """
    bare = _install_runner_that_drops_stdin(repo, tmp_path)

    _checkout(repo, "develop", "origin/develop")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-q", "-m", "second")

    refused = _git_in(repo, "push", "origin", "develop")
    assert refused.returncode != 0, refused.stdout
    assert "Refusing to push directly to" in refused.stderr
    assert "the ref list was not supplied" in refused.stderr
    # The remote must not have moved.
    assert (
        "develop"
        not in _git_in(bare, "for-each-ref", "--format=%(refname:short)").stdout
    )

    # And the override still gets through the runner.
    allowed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repo), "push", "origin", "develop"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "ALLOW_SHARED_BRANCH": "1"},
    )
    assert allowed.returncode == 0, allowed.stderr


@pytest.mark.unit
def test_the_head_fallback_is_wrong_in_both_directions(
    tmp_path: Path, repo: Path
) -> None:
    """What the ``HEAD`` fallback costs, measured rather than asserted in prose.

    With no ref list the hook substitutes a different question, and substituting a
    question is wrong in both directions. The header of ``scripts/hooks/pre-push``
    states both; this is what keeps that statement true, since the under-refusal is
    the half a reader would not think to look for.

    The under-refusal is why the ``PreToolUse`` half matters on such a machine: it
    resolves the destination from the command line and refuses the same command, so
    the residual gap is a push from a plain shell.
    """
    bare = _install_runner_that_drops_stdin(repo, tmp_path)

    # Over-refusal: nothing shared is being written, and it is refused anyway.
    _checkout(repo, "develop", "origin/develop")
    over = _git_in(repo, "push", "origin", "feature/thing")
    assert over.returncode != 0, over.stdout
    assert "the ref list was not supplied" in over.stderr

    # Under-refusal: develop IS the destination, and the push goes through.
    _checkout(repo, "feature/thing", "origin/feature/thing")
    under = _git_in(repo, "push", "origin", "HEAD:refs/heads/develop")
    assert under.returncode == 0, under.stderr
    assert (
        "develop" in _git_in(bare, "for-each-ref", "--format=%(refname:short)").stdout
    ), "the push was allowed but the remote did not move; the premise has changed"

    # The PreToolUse half is what refuses that command, and the other three forms.
    for command in (
        "git push origin HEAD:refs/heads/develop",
        "git push origin HEAD:develop",
        "git push origin develop",
        "git push --all origin",
    ):
        assert _run_hook(_bash(command, cwd=repo)).returncode == 2, command


@pytest.mark.unit
def test_pre_push_is_executable_and_a_posix_shell_script() -> None:
    assert os.access(PRE_PUSH, os.X_OK), f"{PRE_PUSH} is not executable"
    assert PRE_PUSH.read_text(encoding="utf-8").startswith("#!/bin/sh")


# --------------------------------------------------------------------------- #
# the install target, run for real
# --------------------------------------------------------------------------- #
def _recipe_makefile(destination: Path) -> None:
    """Write a minimal Makefile carrying the shipped install-git-hooks recipe.

    The recipe is extracted from the repository's own Makefile rather than
    restated, so this tests the text that ships.
    """
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.startswith("install-git-hooks:")
    )
    end = start + 1
    while end < len(lines) and lines[end].startswith("\t"):
        end += 1
    recipe = "\n".join(lines[start:end])
    assert "\n\t" in recipe, "no recipe body found for install-git-hooks"
    (destination / "Makefile").write_text(
        f"YELLOW=\nGREEN=\nNC=\n\n{recipe}\n", encoding="utf-8"
    )


@pytest.fixture
def installable(tmp_path: Path) -> Path:
    """A repository holding the tracked hook and the real install recipe."""
    project = tmp_path / "project"
    (project / "scripts" / "hooks").mkdir(parents=True)
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "init", "-q", str(project)], check=True, capture_output=True
    )
    (project / "scripts" / "hooks" / "pre-push").write_text(
        PRE_PUSH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    _recipe_makefile(project)
    return project


def _make(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["make", "install-git-hooks"],
        cwd=str(project),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.unit
def test_install_target_installs_an_executable_hook(installable: Path) -> None:
    result = _make(installable)
    assert result.returncode == 0, result.stdout + result.stderr
    installed = installable / ".git" / "hooks" / "pre-push"
    assert installed.is_file()
    assert os.access(installed, os.X_OK)
    assert installed.read_text(encoding="utf-8") == PRE_PUSH.read_text(encoding="utf-8")
    assert "Installed" in result.stdout


@pytest.mark.unit
def test_install_target_is_idempotent(installable: Path) -> None:
    _make(installable)
    result = _make(installable)
    assert result.returncode == 0, result.stdout + result.stderr
    backups = list((installable / ".git" / "hooks").glob("pre-push.bak*"))
    assert backups == [], f"a no-op re-install made a backup: {backups}"


@pytest.mark.unit
def test_install_target_never_overwrites_an_existing_backup(installable: Path) -> None:
    """The user's own hook must survive a later upgrade of the tracked one."""
    hooks = installable / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-push").write_text("#!/bin/sh\necho MINE\n", encoding="utf-8")

    first = _make(installable)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "MINE" in (hooks / "pre-push.bak").read_text(encoding="utf-8")
    assert "REPLACES" in first.stdout

    # The tracked hook changes, and the guard is re-installed over itself.
    tracked = installable / "scripts" / "hooks" / "pre-push"
    tracked.write_text(
        tracked.read_text(encoding="utf-8") + "\n# a later revision\n", encoding="utf-8"
    )
    second = _make(installable)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "MINE" in (hooks / "pre-push.bak").read_text(encoding="utf-8"), (
        "the user's original hook was overwritten by a later install"
    )
    assert (hooks / "pre-push.bak.1").is_file()


@pytest.mark.unit
def test_install_target_fails_loudly_when_the_hooks_path_is_unusable(
    installable: Path,
) -> None:
    """Printing success after a failed copy is what shaped this recipe.

    The obstruction is a ``hooks`` path that is a regular file, so ``mkdir -p``
    cannot proceed. That fails for **root** as well, which a read-only directory
    does not — and CI runs as root, so a permissions-only version of this test
    would pass locally and prove nothing where it matters.
    """
    git_dir = installable / ".git"
    hooks = git_dir / "hooks"
    if hooks.is_dir():
        for child in hooks.iterdir():
            child.unlink()
        hooks.rmdir()
    hooks.write_text("not a directory\n", encoding="utf-8")

    result = _make(installable)
    assert result.returncode != 0, result.stdout
    assert "Installed" not in result.stdout


@pytest.mark.unit
@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores the read-only bit, so cp would succeed"
)
def test_install_target_fails_loudly_on_a_read_only_hooks_directory(
    installable: Path,
) -> None:
    """The original shape of the defect: three permission errors, then success."""
    hooks = installable / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hooks.chmod(0o500)
    try:
        result = _make(installable)
    finally:
        hooks.chmod(0o700)
    assert result.returncode != 0, result.stdout
    assert "Installed" not in result.stdout


@pytest.mark.unit
def test_install_target_says_when_hooks_are_redirected_elsewhere(
    installable: Path, tmp_path: Path
) -> None:
    """Installed and effective are different claims, so it must not conflate them."""
    elsewhere = tmp_path / "runners"
    elsewhere.mkdir()
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(installable), "config", "core.hooksPath", str(elsewhere)],
        check=True,
        capture_output=True,
    )
    result = _make(installable)
    assert result.returncode == 0, result.stdout + result.stderr
    assert str(elsewhere) in result.stdout
    assert "chain" in result.stdout
    assert "judges by HEAD" in result.stdout


@pytest.mark.unit
def test_install_target_reports_plainly_when_hooks_are_not_redirected(
    installable: Path,
) -> None:
    """``core.hooksPath`` is set explicitly here because the developer's machine
    may set it system-wide, in which case the warning above is the correct answer
    for every repository and this case could never be reached."""
    hooks = installable / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(installable), "config", "core.hooksPath", str(hooks)],
        check=True,
        capture_output=True,
    )
    result = _make(installable)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Installed" in result.stdout
    assert "core.hooksPath" not in result.stdout


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
