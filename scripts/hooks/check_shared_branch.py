#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Refuse a commit or push that writes straight onto ``develop`` or ``main``.

Registered as a ``PreToolUse`` hook on ``Bash`` in ``.claude/settings.json``,
alongside ``check_commit_text.py``. It reads the hook payload on stdin and
refuses three commands:

* ``git commit`` while the branch the commit would land on is a shared branch.
  That branch is tracked across the segments of one command, so ``git switch -c
  fix/x && git commit`` is allowed and ``git switch develop && git commit`` is
  refused.
* ``git push`` whose destination ref resolves to a shared branch, including the
  forms that do not name it (``git push`` with an upstream, ``git push origin
  HEAD``, ``git push origin @``), the delete form (``git push origin
  :develop``), ``--all`` / ``--mirror``, and a bare push under
  ``push.default=matching``, which pushes every same-named branch.
* ``gh pr merge`` for a pull request that has a **failing** check.

Overrides, because each refusal has a legitimate case: set
``ALLOW_SHARED_BRANCH=1`` for the first two and ``ALLOW_RED_MERGE=1`` for the
third. Both are read from an inline assignment on the command itself
(``ALLOW_SHARED_BRANCH=1 git push origin develop``) and from the environment;
inline is the form to reach for, because a variable exported in one tool call is
gone by the next.

**What this does not cover.** Branch protection is a repository setting, and
enabling it needs repository admin that no token here has (issue #933). Nothing
running on a contributor's machine can stop a merge performed through GitHub's
own Merge button, so this guard bounds *accidental direct writes* from this
checkout and *knowingly merging red* through ``gh``. It is not a substitute for
required status checks, and these are the routes it does not see:

* A merge performed in the browser.
* A pull request whose checks never ran at all (a fork PR gets no GitHub CI
  here, and refusing those would block the only route they have).
* ``git push --no-verify``, which also skips the companion ``pre-push`` hook.
* Commands inside a script file or ``bash -c``: the hook inspects the command
  text it is given, and ``sh deploy.sh`` reveals nothing about what the script
  does. The ``pre-push`` hook is what covers those.
* History written onto a shared branch by anything other than ``git commit`` --
  ``merge``, ``cherry-pick``, ``revert``, ``rebase``, ``am``. Those are local
  until pushed, and the push is what this refuses.

Two behaviours that are deliberate rather than oversights:

* **It does not look at which remote.** Pushing ``develop`` to a personal fork,
  or to a throwaway local repository, is refused too. Telling those apart from
  the real remote would mean trusting a remote name or URL to decide whether a
  guard applies.
* **It keys on the branch *name*.** In a session that also touches an unrelated
  repository whose working branch is ``main``, a commit there is refused. The
  override is the answer for that.

The companion ``scripts/hooks/pre-push`` is a real git ``pre-push`` hook covering
pushes this hook never sees. Install it with ``make install-git-hooks``.

Exit codes: 0 to allow, 2 to block with the reason on stderr. Anything
unexpected -- a malformed payload, a command this cannot parse, git or ``gh``
failing -- allows the command, because a guard that wedges the session is worse
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

#: Override values that mean "yes". Anything else -- including ``0`` and
#: ``false`` -- leaves the guard on, so spelling out a refusal of the override
#: is not read as granting it.
TRUTHY = frozenset({"1", "true", "yes", "y", "on"})

#: Characters that separate one command from the next. Handed to ``shlex`` as
#: punctuation so that a separator *inside quotes* stays part of its string:
#: ``echo 'a; git push origin develop'`` is one command, not two.
PUNCTUATION = ";|&()<>\n"

#: Fallback splitter, used only when ``shlex`` cannot tokenize the command (an
#: unbalanced quote). Quote-blind, which is why it is the fallback.
SEPARATORS = re.compile(r"\|\||&&|[;\n|]")

#: ``git`` options that sit before the subcommand and take a separate value.
GIT_GLOBAL_WITH_VALUE = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--exec-path",
        "--namespace",
        "--config-env",
    }
)

#: ``git push`` options that take a separate value, so the value is not mistaken
#: for a remote or a refspec.
PUSH_FLAGS_WITH_VALUE = frozenset(
    {"-o", "--push-option", "--receive-pack", "--exec", "--repo"}
)

#: A bare ``VAR=value`` prefix, which shells apply to the command that follows.
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

#: ``<<EOF``, ``<<'EOF'``, ``<<-"EOF"`` -- the start of a heredoc whose body is
#: data, not commands.
HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

#: Subcommands that move ``HEAD`` to another branch.
SWITCH_SUBCOMMANDS = frozenset({"switch", "checkout"})

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


def upstream_branch(repo: Path, branch: str | None = None) -> str | None:
    """The branch ``branch`` (or ``HEAD``) tracks; ``None`` if unset."""
    ref = f"{branch}@{{upstream}}" if branch else "@{upstream}"
    upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", ref)
    return _branch_part(upstream) if upstream else None


def local_shared_branches(repo: Path) -> set[str] | None:
    """Shared branches that exist locally, or ``None`` if they cannot be read."""
    listed = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    if listed is None:
        return None
    return {b for b in listed.split() if b in SHARED_BRANCHES}


def strip_heredocs(command: str) -> str:
    """Drop heredoc bodies, keeping the line that introduces them.

    A heredoc body is data. Writing a document that contains the line ``git push
    origin develop`` must not be read as running it, which is what happens if
    the body is split on its newlines like any other command text.
    """
    lines = command.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in HEREDOC_START.finditer(line):
            terminator = match.group(2)
            while index < len(lines) and lines[index].strip() != terminator:
                index += 1
            if index < len(lines):  # drop the terminator line as well
                index += 1
    return "\n".join(kept)


def _shlex_tokens(command: str) -> list[str] | None:
    """Tokenize ``command``, keeping separators as their own tokens.

    ``None`` when ``shlex`` cannot (an unbalanced quote, which a heredoc body or
    an apostrophe in a commit message routinely produces).
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=PUNCTUATION)
    lexer.whitespace_split = True
    # Newline is a separator here rather than whitespace, so that two commands on
    # two lines do not merge into one.
    lexer.whitespace = " \t\r"
    try:
        return list(lexer)
    except ValueError:
        return None


def segments(command: str) -> list[list[str]]:
    """``command`` split into shell segments, each tokenized.

    Heredoc bodies are dropped first, then the command is tokenized once with
    quoting honoured and split on separator *tokens*. If that fails, each piece
    of a quote-blind regex split is tokenized on its own, which is the older and
    coarser behaviour.
    """
    text = strip_heredocs(command)
    out: list[list[str]] = []
    tokens = _shlex_tokens(text)
    if tokens is not None:
        current: list[str] = []
        for token in tokens:
            if token and all(character in PUNCTUATION for character in token):
                if current:
                    out.append(current)
                    current = []
                continue
            current.append(token)
        if current:
            out.append(current)
        return out

    for raw in SEPARATORS.split(text):
        piece = raw.strip()
        if not piece:
            continue
        try:
            pieces = shlex.split(piece, comments=True)
        except ValueError:
            pieces = piece.split()
        if pieces:
            out.append(pieces)
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
    recognised as a push. An option that takes a *separate* value has to be
    listed in ``GIT_GLOBAL_WITH_VALUE``, or its value is mistaken for the
    subcommand and the command goes unrecognised.
    """
    if not tokens or tokens[0] != program:
        return None
    rest = tokens[1:]
    while rest:
        arg = rest[0]
        if not arg.startswith("-"):
            return rest
        rest = rest[2:] if arg in GIT_GLOBAL_WITH_VALUE else rest[1:]
    return None


def inline_config(tokens: list[str]) -> dict[str, str]:
    """``-c key=value`` settings given before the subcommand.

    ``git -c push.default=matching push`` changes what a bare push targets, and
    a command-line setting is invisible to ``git config --get``.
    """
    settings: dict[str, str] = {}
    rest = tokens[1:]
    while rest:
        arg = rest[0]
        if not arg.startswith("-"):
            break
        if arg == "-c" and len(rest) > 1:
            key, _, value = rest[1].partition("=")
            if key:
                settings[key] = value
            rest = rest[2:]
            continue
        rest = rest[2:] if arg in GIT_GLOBAL_WITH_VALUE else rest[1:]
    return settings


def command_repo(tokens: list[str], base: Path) -> Path:
    """The repository the git command in ``tokens`` acts on.

    ``git -C other push`` targets ``other``, not the directory the command runs
    in, and ``subcommand`` has already had to parse past that option to find the
    subcommand.
    """
    repo = base
    rest = tokens[1:]
    while rest:
        arg = rest[0]
        if not arg.startswith("-"):
            break
        value: str | None = None
        if arg in GIT_GLOBAL_WITH_VALUE and len(rest) > 1:
            value = rest[1]
            rest = rest[2:]
        else:
            if "=" in arg:
                arg, _, value = arg.partition("=")
            rest = rest[1:]
        if value and arg in ("-C", "--git-dir"):
            candidate = Path(value)
            repo = candidate if candidate.is_absolute() else repo / candidate
    return repo


def switch_target(args: list[str]) -> tuple[bool, str | None]:
    """``(moves_head, branch)`` for a ``git switch`` / ``git checkout``.

    ``(False, None)`` when HEAD does not move -- ``git checkout <tree> -- <path>``
    restores files. ``(True, None)`` when it moves somewhere this cannot name
    (``git switch -``, ``--detach``), which makes later segments unjudgeable
    rather than judged against the wrong branch.
    """
    rest = args[1:]  # args[0] is "switch" or "checkout"
    if "--" in rest:
        return False, None
    for arg in rest:
        if not arg.startswith("-"):
            # The first positional is the branch in every form that moves HEAD,
            # including `switch -c <new>` and `checkout -b <new> <start-point>`,
            # because the new name is that option's value.
            return True, arg
    return True, None


def effective_push_default(repo: Path, inline: dict[str, str]) -> str:
    """``push.default``, honouring a ``-c`` setting on the command line."""
    if "push.default" in inline:
        return inline["push.default"]
    return _git(repo, "config", "--get", "push.default") or "simple"


def push_destinations(
    args: list[str],
    repo: Path,
    head: str | None = None,
    head_known: bool = True,
    inline: dict[str, str] | None = None,
) -> set[str] | None:
    """Branch names ``git push <args>`` would write to.

    ``None`` means "could not tell", which allows the push. An empty set means
    the push was understood and targets nothing shared. ``head`` overrides the
    checked-out branch when an earlier segment of the same command switched.
    """
    positionals: list[str] = []
    pushes_everything = False
    rest = list(args)
    while rest:
        arg = rest[0]
        if arg == "--":
            positionals.extend(rest[1:])
            break
        if arg.startswith("-"):
            if arg in ("--all", "--mirror"):
                pushes_everything = True
            rest = rest[2:] if arg in PUSH_FLAGS_WITH_VALUE else rest[1:]
            continue
        positionals.append(arg)
        rest = rest[1:]

    if pushes_everything:
        # Every local branch goes, so any shared branch that exists locally is a
        # destination whether or not it was named.
        return local_shared_branches(repo)

    refspecs = positionals[1:]  # positionals[0], if present, is the remote
    if not refspecs:
        if not head_known:
            return None
        if effective_push_default(repo, inline or {}) == "matching":
            # `matching` pushes every local branch that already exists on the
            # remote under the same name. Enumerating the local ones over-counts
            # a shared branch the remote does not have, which is the safe
            # direction for a guard.
            return local_shared_branches(repo)
        # No refspec: git pushes HEAD to its upstream, or to the same-named
        # branch on the remote. Both readings matter, so take both.
        branch = head if head is not None else current_branch(repo)
        names = {branch} if branch else set()
        upstream = upstream_branch(repo, head)
        if upstream:
            names.add(upstream)
        return names

    destinations: set[str] = set()
    for refspec in refspecs:
        spec = refspec.removeprefix("+")
        source, _, destination = spec.rpartition(":")
        name = _strip_ref_prefix(destination)
        if not name:
            continue
        if name in ("HEAD", "@") and not source:
            # `git push origin HEAD` (or `@`) pushes to HEAD's own branch name.
            branch = head if head is not None else current_branch(repo)
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
    """Whether ``name`` is set to a value meaning "yes".

    An inline assignment wins over the environment, so ``ALLOW_SHARED_BRANCH=0``
    on the command turns the override off for that command.
    """
    value = inline[name] if name in inline else os.environ.get(name)
    return value is not None and value.strip().lower() in TRUTHY


def _shared_branch_remedy(action: str) -> str:
    return (
        f"Changes reach {' and '.join(sorted(SHARED_BRANCHES))} through a pull "
        "request, so that the lint, type-check, test, security and dependency "
        "gates run and a reviewer sees the change.\n"
        "Work on a branch instead:\n"
        "  git switch -c feature/<name>    # or fix/<name>, docs/<name>\n"
        f"or re-run the command with the override in front of it, to {action} "
        "here deliberately:\n"
        f"  {ALLOW_SHARED_BRANCH}=1 <your command>\n"
        "The inline form is the one to use: a variable exported in one tool call "
        "is gone by the next.\n"
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

    base = repo_root(payload)

    # The branch a commit would land on, tracked across segments: `None` means
    # "unchanged, ask git", a string is where an earlier `git switch` moved, and
    # `head_known` goes False when a switch moved somewhere unnameable.
    head: str | None = None
    head_known = True

    for tokens in segments(command):
        inline, argv = split_assignments(tokens)
        if not argv:
            continue

        git_args = subcommand(argv, "git")
        if git_args:
            repo = command_repo(argv, base)

            if git_args[0] in SWITCH_SUBCOMMANDS:
                moves_head, target = switch_target(git_args)
                if moves_head:
                    head = target
                    head_known = target is not None
                continue

            if git_args[0] == "commit":
                if _allowed(ALLOW_SHARED_BRANCH, inline) or not head_known:
                    continue
                branch = head if head is not None else current_branch(repo)
                if branch in SHARED_BRANCHES:
                    return (
                        f"Blocked: this would commit onto {branch}, which is a "
                        "shared branch.\n\n" + _shared_branch_remedy("commit")
                    )
                continue

            if git_args[0] == "push":
                if _allowed(ALLOW_SHARED_BRANCH, inline):
                    continue
                destinations = push_destinations(
                    git_args[1:],
                    repo,
                    head=head,
                    head_known=head_known,
                    inline=inline_config(argv),
                )
                if destinations is None:
                    continue
                shared = sorted(destinations & SHARED_BRANCHES)
                if shared:
                    return (
                        f"Blocked: this would push directly to {', '.join(shared)} "
                        "on the remote.\n\n" + _shared_branch_remedy("push")
                    )
                continue

        gh_args = subcommand(argv, "gh")
        if gh_args and gh_args[:2] == ["pr", "merge"]:
            if _allowed(ALLOW_RED_MERGE, inline):
                continue
            failures = failing_checks(merge_target(gh_args[1:]), base)
            if not failures:
                continue
            return (
                "Blocked: this pull request has failing checks.\n\n"
                + "\n".join(f"  - {name}" for name in failures)
                + "\n\nNo check on this repository is a *required* status check, so "
                "GitHub will let this merge proceed — which is exactly how 12 HIGH "
                "security findings reached develop once before (see the header of "
                ".github/workflows/security-checks.yml).\n"
                "Fix the failures, or re-run with the override in front of the "
                "command if the failure is known-unrelated and you are merging "
                f"deliberately:\n  {ALLOW_RED_MERGE}=1 <your command>\n"
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
