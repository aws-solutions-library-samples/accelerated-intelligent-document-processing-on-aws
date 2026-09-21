#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Refuse a commit or push that writes straight onto ``develop`` or ``main``.

Registered as a ``PreToolUse`` hook on ``Bash`` in ``.claude/settings.json``,
alongside ``check_commit_text.py``. It reads the hook payload on stdin and
refuses three commands:

* ``git commit`` while ``HEAD`` is a shared branch, or while ``HEAD``'s upstream
  is one — the second case catches a local branch named something else that
  tracks ``origin/develop``.
* ``git push`` whose destination ref resolves to a shared branch, including the
  forms that do not name it (``git push`` with an upstream, ``git push origin
  HEAD``), the delete form (``git push origin :develop``) and ``--all`` /
  ``--mirror``, which push every branch.
* ``gh pr merge`` for a pull request that has a **failing** check.

Overrides, because each refusal has a legitimate case: set
``ALLOW_SHARED_BRANCH=1`` for the first two and ``ALLOW_RED_MERGE=1`` for the
third, either in the environment or inline on the command itself
(``ALLOW_SHARED_BRANCH=1 git push origin develop``). Inline is supported because
that is the only form available mid-session.

**What this does not do.** Branch protection is a repository setting, and
enabling it needs repository admin that no token here has (issue #933). Nothing
running on a contributor's machine can stop a merge performed through GitHub's
own Merge button, so this guard bounds *accidental direct writes* from this
checkout and *knowingly merging red* through ``gh``. It is not a substitute for
required status checks, and two residuals follow from that: a pull request whose
checks never ran at all is not refused here (a fork PR gets no GitHub CI, and
refusing those would block the only route they have), and neither is a merge
performed in the browser.

The companion ``scripts/hooks/pre-push`` is a real git ``pre-push`` hook covering
pushes this hook never sees — anything run outside the assistant's Bash tool,
including the ``git push`` inside ``make commit``. Install it with ``make
install-git-hooks``.

Exit codes: 0 to allow, 2 to block with the reason on stderr. Anything
unexpected — a malformed payload, a command this cannot parse, git or ``gh``
failing — allows the command, because a guard that wedges the session is worse
than one that misses a case. ``check_commit_text.py`` documents the same choice.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess  # noqa: S404 - fixed argv, no shell; see _git/_gh
import sys
from pathlib import Path

#: Branches that changes reach through a pull request rather than directly.
SHARED_BRANCHES = frozenset({"develop", "main"})

#: Environment (or inline) variable that permits a direct commit or push.
ALLOW_SHARED_BRANCH = "ALLOW_SHARED_BRANCH"

#: Environment (or inline) variable that permits merging a pull request whose
#: checks are failing.
ALLOW_RED_MERGE = "ALLOW_RED_MERGE"

#: Shell operators that separate one command from the next. Splitting on these
#: keeps ``git status && git push origin develop`` from being read as one
#: ``git status``.
SEPARATORS = re.compile(r"\|\||&&|[;\n|]")

#: ``git`` options that sit before the subcommand and take a separate value.
GIT_GLOBAL_WITH_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--exec-path"}
)

#: ``git push`` options that take a separate value, so the value is not mistaken
#: for a remote or a refspec.
PUSH_FLAGS_WITH_VALUE = frozenset(
    {"-o", "--push-option", "--receive-pack", "--exec", "--repo"}
)

#: A bare ``VAR=value`` prefix, which shells apply to the command that follows.
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

_TIMEOUT_SECONDS = 8


def _git(repo: Path, *args: str) -> str | None:
    """Run a read-only ``git`` command in ``repo``; ``None`` if it cannot.

    Every caller treats ``None`` as "unknown", which allows the command.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _strip_ref_prefix(ref: str) -> str:
    """``refs/heads/develop`` and ``heads/develop`` both name ``develop``."""
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def _branch_part(upstream: str) -> str:
    """``origin/develop`` -> ``develop``; a bare name is returned unchanged."""
    return upstream.split("/", 1)[1] if "/" in upstream else upstream


def current_branch(repo: Path) -> str | None:
    """The checked-out branch, or ``None`` on a detached HEAD."""
    name = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return None if name in (None, "HEAD") else name


def upstream_branch(repo: Path) -> str | None:
    """The branch ``HEAD`` tracks, without its remote; ``None`` if unset."""
    upstream = _git(
        repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    return _branch_part(upstream) if upstream else None


def segments(command: str) -> list[list[str]]:
    """``command`` split into shell segments, each tokenized.

    ``shlex`` raises on unbalanced quotes, which a heredoc body routinely
    produces; that segment falls back to a whitespace split rather than
    discarding the whole command.
    """
    out: list[list[str]] = []
    for raw in SEPARATORS.split(command):
        piece = raw.strip()
        if not piece:
            continue
        try:
            tokens = shlex.split(piece, comments=True)
        except ValueError:
            tokens = piece.split()
        if tokens:
            out.append(tokens)
    return out


def split_assignments(tokens: list[str]) -> tuple[dict[str, str], list[str]]:
    """Peel leading ``VAR=value`` assignments off a segment.

    ``ALLOW_SHARED_BRANCH=1 git push origin develop`` has to be read as an
    override rather than as an unrecognised command, because setting a variable
    inline is the only way to do it mid-session.
    """
    overrides: dict[str, str] = {}
    if not tokens:
        return overrides, []
    index = 0
    for index, token in enumerate(tokens):  # noqa: B007 - index used after loop
        match = ASSIGNMENT.match(token)
        if not match:
            return overrides, tokens[index:]
        overrides[match.group(1)] = match.group(2)
    return overrides, tokens[index + 1 :]


def subcommand(tokens: list[str], program: str) -> list[str] | None:
    """Arguments after ``program``'s subcommand, or ``None`` if not that program.

    Global options before the subcommand are skipped, so ``git -C dir push`` is
    recognised as a push.
    """
    if not tokens or tokens[0] != program:
        return None
    rest = tokens[1:]
    while rest:
        token = rest[0]
        if not token.startswith("-"):
            return rest
        rest = rest[2:] if token in GIT_GLOBAL_WITH_VALUE else rest[1:]
    return None


def push_destinations(args: list[str], repo: Path) -> set[str] | None:
    """Branch names ``git push <args>`` would write to.

    ``None`` means "could not tell", which allows the push. An empty set means
    the push was understood and targets nothing shared.
    """
    positionals: list[str] = []
    pushes_everything = False
    rest = list(args)
    while rest:
        token = rest[0]
        if token == "--":
            positionals.extend(rest[1:])
            break
        if token.startswith("-"):
            if token in ("--all", "--mirror"):
                pushes_everything = True
            rest = rest[2:] if token in PUSH_FLAGS_WITH_VALUE else rest[1:]
            continue
        positionals.append(token)
        rest = rest[1:]

    if pushes_everything:
        # Every local branch goes, so any shared branch that exists locally is a
        # destination whether or not it was named.
        listed = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        if listed is None:
            return None
        return {b for b in listed.split() if b in SHARED_BRANCHES}

    refspecs = positionals[1:]  # positionals[0], if present, is the remote
    if not refspecs:
        # No refspec: git pushes HEAD to its upstream, or to the same-named
        # branch on the remote. Both readings matter, so take both.
        return {b for b in (current_branch(repo), upstream_branch(repo)) if b}

    destinations: set[str] = set()
    for refspec in refspecs:
        spec = refspec.removeprefix("+")
        source, _, destination = spec.rpartition(":")
        name = _strip_ref_prefix(destination)
        if not name:
            continue
        if name == "HEAD" and not source:
            # `git push origin HEAD` pushes to HEAD's own branch name.
            branch = current_branch(repo)
            if branch:
                destinations.add(branch)
            continue
        destinations.add(name)
    return destinations


def failing_checks(pr: str | None, repo: Path) -> list[str] | None:
    """Names of failing checks on ``pr``, or ``None`` if they cannot be read.

    ``gh pr checks`` exits non-zero when anything is pending or failing and still
    prints the JSON, so the exit code is deliberately ignored in favour of the
    ``bucket`` field. Only ``fail`` counts: ``pending`` is the steady state for
    this repository's path-filtered workflows and its one conditional check, so
    refusing on pending would refuse every merge.
    """
    argv = ["gh", "pr", "checks"]
    if pr:
        argv.append(pr)
    argv += ["--json", "bucket,name"]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        checks = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(checks, list):
        return None
    return [
        str(check.get("name", "?"))
        for check in checks
        if isinstance(check, dict) and check.get("bucket") == "fail"
    ]


def merge_target(args: list[str]) -> str | None:
    """The PR a ``gh pr merge`` names, or ``None`` for "the current branch's"."""
    for token in args[1:]:  # args[0] is "merge"
        if not token.startswith("-"):
            return token
    return None


def repo_root(payload: dict[str, object]) -> Path:
    """The working tree the command will run in.

    The payload's ``cwd`` is preferred over ``CLAUDE_PROJECT_DIR`` so that a
    worktree is resolved as itself rather than as the main checkout.
    """
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]


def _allowed(name: str, inline: dict[str, str]) -> bool:
    return bool(inline.get(name) or os.environ.get(name))


def _shared_branch_remedy(action: str) -> str:
    return (
        f"Changes reach {' and '.join(sorted(SHARED_BRANCHES))} through a pull "
        "request, so that the lint, type-check, test, security and dependency "
        "gates run and a reviewer sees the change.\n"
        "Work on a branch instead:\n"
        "  git switch -c feature/<name>    # or fix/<name>, docs/<name>\n"
        f"or re-run with {ALLOW_SHARED_BRANCH}=1 to {action} here deliberately.\n"
        "Note this guard is a local convention, not an enforced one: branch "
        "protection on this repository is off and enabling it needs repository "
        "admin (issue #933)."
    )


def decide(payload: dict[str, object]) -> str | None:
    """The reason to block ``payload``'s command, or ``None`` to allow it."""
    tool_input = payload.get("tool_input")
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str) or not command.strip():
        return None

    repo = repo_root(payload)

    for tokens in segments(command):
        inline, argv = split_assignments(tokens)
        if not argv:
            continue

        git_args = subcommand(argv, "git")
        if git_args and git_args[0] == "commit":
            if _allowed(ALLOW_SHARED_BRANCH, inline):
                continue
            branch = current_branch(repo)
            upstream = upstream_branch(repo)
            shared = {b for b in (branch, upstream) if b in SHARED_BRANCHES}
            if shared:
                where = branch or "a detached HEAD"
                tracking = (
                    f" (tracking {upstream})" if upstream and upstream != branch else ""
                )
                return (
                    f"Blocked: this would commit onto {where}{tracking}, which is a "
                    "shared branch.\n\n" + _shared_branch_remedy("commit")
                )
            continue

        if git_args and git_args[0] == "push":
            if _allowed(ALLOW_SHARED_BRANCH, inline):
                continue
            destinations = push_destinations(git_args[1:], repo)
            if destinations is None:
                continue
            shared = sorted(destinations & SHARED_BRANCHES)
            if shared:
                return (
                    f"Blocked: this would push directly to {', '.join(shared)} on the "
                    "remote.\n\n" + _shared_branch_remedy("push")
                )
            continue

        gh_args = subcommand(argv, "gh")
        if gh_args and gh_args[:2] == ["pr", "merge"]:
            if _allowed(ALLOW_RED_MERGE, inline):
                continue
            failures = failing_checks(merge_target(gh_args[1:]), repo)
            if not failures:
                continue
            return (
                "Blocked: this pull request has failing checks.\n\n"
                + "\n".join(f"  - {name}" for name in failures)
                + "\n\nNo check on this repository is a *required* status check, so "
                "GitHub will let this merge proceed — which is exactly how 12 HIGH "
                "security findings reached develop once before (see the header of "
                ".github/workflows/security-checks.yml).\n"
                "Fix the failures, or re-run with "
                f"{ALLOW_RED_MERGE}=1 if the failure is known-unrelated and you are "
                "merging deliberately.\n"
                "Only concluded failures are counted here; a pending or skipped check "
                "is not one, and a pull request whose checks never ran is not refused "
                "at all."
            )

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return 0

    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0

    try:
        reason = decide(payload)
    except Exception:  # noqa: BLE001 - a guard must not wedge the session
        return 0

    if reason is None:
        return 0

    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
