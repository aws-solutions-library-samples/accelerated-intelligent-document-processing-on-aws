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
  refused -- as is ``git switch --track origin/develop && git commit``, where git
  names the new local branch after the remote ref's leaf.
* ``git push`` whose destination ref resolves to a shared branch, including the
  forms that do not name it (``git push`` with an upstream, ``git push origin
  HEAD``, ``git push origin @``), the delete form (``git push origin
  :develop``), ``--all`` / ``--mirror``, and a bare push under
  ``push.default=matching``, which pushes every same-named branch. The reading of
  a bare push follows ``push.default``, so the two settings that pick one of the
  branch name and the upstream do not also get the other; and ``--tags`` with no
  refspec has no branch destination at all.
* ``gh pr merge`` for a pull request that has a **failing** check. The pull request
  is read past the options that take a separate value, and ``--repo`` is honoured
  on either side of the subcommand, because misreading either one leaves the check
  unable to run and so allows the merge with nothing printed.

It also *says something without refusing anything*, in one case: when another session
has been running git commands in the same working directory, or when the branch there
moved between two of this session's commands. That is advice rather than a verdict, and
why it is only advice is in the co-tenancy item of the uncovered list below.

Overrides, because each refusal has a legitimate case: set
``ALLOW_SHARED_BRANCH=1`` for the first two and ``ALLOW_RED_MERGE=1`` for the
third. Both are read from an inline assignment on the command itself
(``ALLOW_SHARED_BRANCH=1 git push origin develop``) and from the environment.
Inline is the form to reach for, because it applies to one command: a variable
exported in the assistant's Bash tool is gone by the next call, but one exported
by a shell profile, an IDE or a CI runner persists and turns the check off for
**every** command in that environment. Because that is invisible by construction,
an honoured override prints a line to stderr saying which check it disabled.

The two halves do not accept identical spellings, and the difference is in the
safe direction. This one lower-cases and strips, so ``TrUe``, ``YeS`` and ``" 1"``
count; ``pre-push`` matches eleven enumerated words and so does not. The stricter
reader is the one that can still stop a push, so an override this half honours may
be refused by the other -- a visible refusal with a remedy, rather than a check
silently skipped.

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
* Anything that reaches ``git`` other than as the first word of a segment: a
  script file (``sh deploy.sh``), ``bash -c``, ``eval``, ``xargs``, a wrapper that
  takes options of its own (``env``/``nice``/``sudo``), an absolute path
  (``/usr/bin/git``), or a shell function shadowing ``git``. This reads the
  command text it is given, and none of those spell out what will run. They are
  not shapes anyone types by accident; the ``pre-push`` hook is what covers them.
  A leading run of shell keywords, plus ``time`` and ``command``, *is* stripped --
  see ``SHELL_KEYWORDS`` -- because ``if make test; then git push origin develop;
  fi`` is an ordinary thing to type and puts ``then`` first in the segment.
* A **git alias** that runs a shell command: ``git -c alias.p='!git push origin
  develop' p`` has ``git`` as its first word and ``p`` as its subcommand, so
  nothing here recognises a push. Reading aliases would mean resolving
  configuration this does not read.
* ``git checkout <branch> --`` with nothing after the ``--``. A trailing ``--``
  normally introduces a pathspec, which makes the command a file restore, and
  that reading is the useful one; git treats this particular spelling as a branch
  switch, so a commit after it is judged against the branch HEAD was on.
* ``cd -``, bare ``pushd`` and ``popd``. A plain ``cd <path>`` or ``pushd <path>``
  earlier in the same command *is* followed, but those three depend on a directory
  stack this does not keep, so a segment after one of them is judged against the
  directory in force before it. That misses in **either** direction depending on
  which way the stack was moving: returning to a shared-branch checkout is not
  seen, and returning out of one refuses a commit that was fine.
* History written onto a shared branch by anything other than ``git commit`` --
  ``merge``, ``cherry-pick``, ``revert``, ``rebase``, ``am``. Those are local
  until pushed, and the push is what this refuses.
* **Another session standing in the same working directory.** Every answer above is
  about a *branch or a destination*; none is about who else is in the directory.
  Several assistant sessions share the repository root here -- one working tree, not a
  worktree each -- so a ``git switch`` by either moves the tree under the other with no
  refusal and no warning, because nothing unusual happened as far as git is concerned.
  A session can then test a branch it did not check out or commit a file another
  session edited, with every gate green. This one is *reported* rather than refused:
  see ``tenancy_notices``, which says when another session's id was the last to run a
  git command here and when the branch moved between this session's commands. Refusing
  would mean refusing the legitimate case -- one session switching branches on purpose
  -- which nothing available to a per-command hook can tell apart from a collision. The
  convention that actually avoids it is a ``git worktree`` per session (#1087).

Two behaviours that are deliberate rather than oversights:

* **It does not look at which remote.** Pushing ``develop`` to a personal fork,
  or to a throwaway local repository, is refused too. Telling those apart from
  the real remote would mean trusting a remote name or URL to decide whether a
  guard applies.
* **It keys on the branch *name*.** In a session that also touches an unrelated
  repository whose working branch is ``main``, a commit there is refused. The
  override is the answer for that.

The two override variables are not registered in
``scripts/tests/gate_exemptions.json``, and that is a decision rather than an
omission. That registry governs places where a **gate** is turned off for a file,
a line or a rule, and it exists because such a reason is written once and then
outlives the thing it described. These are per-invocation switches on a local
convention, decided by whoever runs the command and recorded nowhere: registering
them would put an entry in the registry that no ratchet can test and no audit can
act on, which is the opposite of what that file is for. What they *can* do
quietly -- being exported once and disabling everything afterwards -- is handled
where it happens: both halves say on stderr when an override turns a check off.

The companion ``scripts/hooks/pre-push`` is a real git ``pre-push`` hook covering
pushes this hook never sees. Install it with ``make install-git-hooks``. ⚠️ On a
machine with a system-wide ``core.hooksPath`` that hook cannot resolve a
destination at all and judges by ``HEAD`` instead, so **this** half is the only
one checking destinations there; its header says what that costs in both
directions.

Exit codes: 0 to allow, 2 to block with the reason on stderr. Anything
unexpected -- a malformed payload, a command this cannot parse, git or ``gh``
failing -- allows the command, because a guard that wedges the session is worse
than one that misses a case. ``check_commit_text.py`` documents the same choice.
Failing open is about what cannot be *read*, not about where the command runs: an
explicit refspec names its destination on the command line, so ``git push origin
develop`` is refused even in a directory that is not a repository.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess  # noqa: S404 - fixed argv, no shell; see _git/_gh
import sys
import time
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

#: ``gh`` options that may sit *before* the subcommand and take a separate value.
#: ``--repo`` is registered on gh's root command, so ``gh -R owner/name pr merge
#: 1055`` is a working form -- and reading ``owner/name`` as the subcommand leaves
#: the command unrecognised, which allows a red merge with nothing printed.
GH_GLOBAL_WITH_VALUE = frozenset({"-R", "--repo"})

#: ``gh pr merge`` options that take a separate value, so the value is not
#: mistaken for the pull request number. Getting this wrong is silent: ``gh pr
#: checks <some flag's value>`` errors, which reads as "cannot tell" and allows
#: the merge with nothing printed.
GH_MERGE_FLAGS_WITH_VALUE = frozenset(
    {
        "-b",
        "--body",
        "-F",
        "--body-file",
        "-t",
        "--subject",
        "--match-head-commit",
        "-A",
        "--author-email",
        "-R",
        "--repo",
    }
)

#: Words a shell may put in front of a command without changing what runs. A
#: compound statement's keywords land in the same segment as the command they
#: introduce -- ``if make test; then git push origin develop; fi`` tokenizes with
#: ``then`` as the segment's first word -- and a segment whose first word is not
#: ``git`` was not examined at all. ``!``, ``{`` and ``}`` do the same.
#:
#: Stripping these can only ever *expose* a command, never hide one, because only
#: a **leading** run of them is removed: ``echo if git push origin develop`` starts
#: with ``echo`` and is left alone. ``time`` and ``command`` are here for the same
#: reason; the wrappers that take options of their own (``env``, ``nice``,
#: ``sudo``, ``xargs``, ``eval``) are not, and stay in the header's uncovered list.
SHELL_KEYWORDS = frozenset(
    {
        "!",
        "{",
        "}",
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "while",
        "until",
        "for",
        "do",
        "done",
        "case",
        "esac",
        "select",
        "coproc",
        "time",
        "command",
    }
)

#: A bare ``VAR=value`` prefix, which shells apply to the command that follows.
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

#: ``<<EOF``, ``<<'EOF'``, ``<<-"EOF"`` -- the start of a heredoc whose body is
#: data, not commands.
HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

#: Subcommands that move ``HEAD`` to another branch.
SWITCH_SUBCOMMANDS = frozenset({"switch", "checkout"})

#: Subcommands after which it is worth saying that somebody else is standing in this
#: working directory: the two that write history and the two that move the tree.
#: Deliberately not every git command -- the notice costs a file read and a `git
#: rev-parse` per invocation, and `git status` in a loop should not pay for it.
TENANCY_SUBCOMMANDS = frozenset({"commit", "push", "switch", "checkout"})

#: Where the co-tenancy note is kept, inside the working tree's own git directory.
TENANCY_FILE = "idp-session-tenancy.json"

#: ``switch`` / ``checkout`` options whose value is the name of the branch being
#: created, and so the branch HEAD ends up on.
CREATE_FLAGS = frozenset(
    {"-c", "-C", "--create", "--force-create", "-b", "-B", "--orphan"}
)

#: Per-subprocess timeout. A single command can spend several of these -- one
#: `git` call per reading, plus a `gh pr checks` -- so the total can exceed the
#: 15 s the hook is given in ``.claude/settings.json``. Being killed there is not
#: a hazard: the harness treats a hook that does not answer as no answer, which
#: allows the command, the same direction every other unknown takes here. The
#: numbers are deliberately not equal, because one subprocess hanging for the whole
#: budget should still leave the hook able to print its reason.
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


def _heredoc_delimiters(line: str, quote: str | None) -> tuple[list[str], str | None]:
    """Delimiters ``line`` opens a heredoc with, and the quote state it ends in.

    Only an **unquoted** ``<<`` opens one. ``git commit -m "shift <<Foo left"``
    contains no heredoc, and reading it as one swallows the rest of the command --
    which is how a quote-blind stripper turns a following ``git push origin
    develop`` into text that is never examined. The quote state is threaded from
    line to line because a quoted string may span them.
    """
    delimiters: list[str] = []
    index = 0
    while index < len(line):
        character = line[index]
        if quote:
            if quote == '"' and character == "\\":
                # Inside double quotes a backslash escapes the next character, so
                # an escaped quote does not end the string. Single quotes have no
                # escapes at all, which is why this is conditional on which.
                index += 2
                continue
            if character == quote:
                quote = None
            index += 1
            continue
        if character in ("'", '"'):
            quote = character
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if line.startswith("<<", index):
            match = HEREDOC_START.match(line, index)
            if match:
                delimiters.append(match.group(2))
                index = match.end()
                continue
            index += 2  # `<<<` is a here-string: no body follows
            continue
        index += 1
    return delimiters, quote


def strip_heredocs(command: str) -> str:
    """Drop heredoc bodies, keeping the line that introduces them.

    A heredoc body is data. Writing a document that contains the line ``git push
    origin develop`` must not be read as running it, which is what happens if
    the body is split on its newlines like any other command text.
    """
    lines = command.splitlines()
    kept: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        delimiters, quote = _heredoc_delimiters(line, quote)
        for terminator in delimiters:
            while index < len(lines) and lines[index].strip() != terminator:
                index += 1
            if index < len(lines):  # drop the terminator line as well
                index += 1
        if delimiters:
            # The body was data, so whatever quoting it contained does not carry
            # into the commands that follow it.
            quote = None
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
    # shlex treats `#` as a comment by default, and its skip consumes the
    # *newline* as well -- so `git status  # note` followed by `git push origin
    # develop` came back as ONE segment whose first word is `git status`'s, and the
    # push went unexamined. It also fired mid-word, where no shell starts a
    # comment: `echo v1.0#beta && git push origin develop` lost everything after
    # `v1.0`. Leaving comments in costs nothing here: a `#` token is not a command
    # and does not begin one, so it can only ever be an argument this ignores.
    lexer.commenters = ""
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


def strip_shell_keywords(tokens: list[str]) -> list[str]:
    """Drop a leading run of ``SHELL_KEYWORDS`` from a segment.

    Applied before anything else reads the segment, so it helps the ``cd``, ``git``
    and ``gh`` readings alike: ``if true; then cd /other; fi`` moves the directory
    the rest of the command is judged against, just as it does in the shell.
    """
    index = 0
    while index < len(tokens) and tokens[index] in SHELL_KEYWORDS:
        index += 1
    return tokens[index:]


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


def subcommand(
    tokens: list[str],
    program: str,
    flags_with_value: frozenset[str] = GIT_GLOBAL_WITH_VALUE,
) -> list[str] | None:
    """Arguments from ``program``'s subcommand on, or ``None`` if not that program.

    Global options before the subcommand are skipped, so ``git -C dir push`` is
    recognised as a push and ``gh -R owner/name pr merge`` as a merge. An option
    that takes a *separate* value has to be listed in ``flags_with_value``, or its
    value is mistaken for the subcommand and the command goes unrecognised -- which
    is a silent way to stop checking it.
    """
    if not tokens or tokens[0] != program:
        return None
    rest = tokens[1:]
    while rest:
        arg = rest[0]
        if not arg.startswith("-"):
            return rest
        rest = rest[2:] if arg in flags_with_value else rest[1:]
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
        # A pathspec follows, so this is a file restore. `git checkout develop --`
        # with nothing after it does switch branches, and is read here as a
        # restore; a bare trailing `--` is not a shape anyone types by accident.
        return False, None
    if any(arg in ("--detach", "-d") for arg in rest):
        # `git switch --detach develop` names a branch but lands on no branch, so a
        # commit after it is not on develop. Reading the positional would refuse it
        # -- a false refusal, and the kind that makes a guard look wrong.
        return True, None
    tracks = any(arg == "-t" or arg.startswith("--track") for arg in rest)
    # A create option's *value* is the branch, so it is read from the option
    # wherever the option sits -- and the whole argument list is searched before
    # any positional is considered, because in `git checkout -t origin/x -b
    # develop` the start point comes first. A value-less create flag is a
    # malformed command; naming no branch leaves the following segments unjudged
    # rather than judged against the wrong one.
    for index, arg in enumerate(rest):
        if arg in CREATE_FLAGS:
            return True, rest[index + 1] if index + 1 < len(rest) else None
        flag, separator, attached = arg.partition("=")
        if separator and flag in CREATE_FLAGS:
            return True, attached or None
    for arg in rest:
        if not arg.startswith("-"):
            if tracks:
                # `git switch --track origin/develop` and `git checkout -t
                # origin/develop` name no new branch: git takes the remote ref's
                # leaf, so HEAD lands on local `develop`.
                return True, _branch_part(arg)
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
    tags_only = False
    rest = list(args)
    while rest:
        arg = rest[0]
        if arg == "--":
            positionals.extend(rest[1:])
            break
        if arg.startswith("-"):
            if arg in ("--all", "--mirror"):
                pushes_everything = True
            if arg == "--tags":
                tags_only = True
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
        if tags_only:
            # `--tags` with no refspec sends refs/tags and no branch at all, so
            # there is no destination to judge. `--follow-tags` is different: it
            # sends the branch as well, so it is deliberately not in this test.
            return set()
        if not head_known:
            return None
        default = effective_push_default(repo, inline or {})
        if default == "matching":
            # `matching` pushes every local branch that already exists on the
            # remote under the same name. Enumerating the local ones over-counts
            # a shared branch the remote does not have, which is the safe
            # direction for a guard.
            return local_shared_branches(repo)
        # With no refspec the destination depends on push.default. Reading both
        # the branch name and the upstream is right only for `simple`, where git
        # requires them to agree; for the two settings that pick one reading, the
        # other is a false refusal. `current` is the one that bites, because
        # `simple` makes git itself refuse a mismatched-name push.
        branch = head if head is not None else current_branch(repo)
        upstream = upstream_branch(repo, head)
        if default == "current":
            return {branch} if branch else set()
        if default in ("upstream", "tracking"):
            return {upstream} if upstream else set()
        return {name for name in (branch, upstream) if name}

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


def failing_checks(
    pr: str | None, repo: Path, selector: str | None = None
) -> list[str] | None:
    """Names of failing checks on ``pr``, or ``None`` if they cannot be read.

    ``gh pr checks`` exits non-zero when anything is pending or failing and still
    prints the JSON, so the exit code is deliberately ignored in favour of the
    ``bucket`` field. Only ``fail`` counts: ``pending`` is the steady state for
    this repository's path-filtered workflows and its one conditional check, so
    refusing on pending would refuse every merge.

    ``selector`` is the ``--repo`` the merge named, forwarded so that the checks
    read belong to the pull request being merged rather than to whatever
    repository ``repo`` happens to be.
    """
    argv = ["gh", "pr", "checks"]
    if pr:
        argv.append(pr)
    if selector:
        argv += ["--repo", selector]
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


def gh_repo_selector(tokens: list[str]) -> str | None:
    """The ``--repo``/``-R`` a ``gh`` command names, or ``None`` for "this one".

    The whole command is searched, because gh registers the option on its root
    command and so accepts it on either side of the subcommand: ``gh -R
    owner/name pr merge 1055`` and ``gh pr merge -R owner/name 1055`` are the same
    request. Losing it means the checks are read for a pull request of that number
    in whatever repository the command happens to run in, which is a different
    question with the same shape of answer.

    All four spellings count, including a short option with its value **attached**.
    Measured on gh 2.88.1: ``gh -Rcli/cli pr view 1`` answers about ``cli/cli``, and
    ``gh -Rowner/definitely-not-real pr view 1`` fails naming that repository, so
    the value is parsed rather than discarded. An earlier reading of ``gh pr checks
    -Rfoo/bar``'s "argument required when using the `--repo` flag" as a rejection of
    the attached form was wrong: that message is about the missing pull request
    argument, which ``gh pr checks`` requires as soon as ``--repo`` is given.
    """
    rest = list(tokens)
    while rest:
        arg = rest[0]
        flag, separator, attached = arg.partition("=")
        if separator and flag in GH_GLOBAL_WITH_VALUE:
            return attached or None
        if arg in GH_GLOBAL_WITH_VALUE and len(rest) > 1:
            return rest[1]
        if (
            len(arg) > 2
            and not arg.startswith("--")
            and arg[:2] in GH_GLOBAL_WITH_VALUE
        ):
            return arg[2:]
        rest = rest[1:]
    return None


def merge_target(args: list[str]) -> str | None:
    """The pull request a ``gh pr merge`` names, or ``None`` for the branch's own.

    A flag's *value* must not be read as the pull request. ``gh pr merge --subject
    "chore: x" 1055`` would otherwise be checked as pull request ``chore: x``,
    and since ``gh pr checks`` errors on that, the failure reads as "cannot tell"
    and the merge is allowed with nothing printed. Same class as
    ``PUSH_FLAGS_WITH_VALUE``.
    """
    rest = list(args[1:])  # args[0] is "merge"
    while rest:
        arg = rest[0]
        if arg.startswith("-"):
            if "=" not in arg and arg in GH_MERGE_FLAGS_WITH_VALUE and len(rest) > 1:
                rest = rest[2:]
                continue
            rest = rest[1:]
            continue
        return arg
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
        "Reach for the inline form: a variable exported in the assistant's Bash "
        "tool is gone by the next call (one exported by a shell profile, an IDE or "
        "a CI runner is not, and disables this for every command in that "
        "environment).\n"
        "Note this guard is a local convention, not an enforced one: branch "
        "protection on this repository is off and enabling it needs repository "
        "admin (issue #933)."
    )


def _waived(name: str, what: str) -> str:
    """The line printed when an override turns a check off.

    An override exported by a shell profile, an IDE or a CI runner applies to
    every command in that environment, and a guard that is believed on and is
    actually off is the failure this whole file exists to avoid. So saying so
    costs one line and is worth it.
    """
    return f"shared-branch guard: {name} is set, so {what} is not checked here."


def _tenancy_file(repo: Path) -> Path | None:
    """Where this working tree's co-tenancy note lives, or ``None`` if unknown.

    ``--absolute-git-dir`` answers per *working tree*: in a worktree it is
    ``<main>/.git/worktrees/<name>``, which is what makes the record about the
    directory somebody is standing in rather than about the repository. Inside the git
    directory rather than in a temp directory on purpose — a predictable name under
    ``/tmp`` is writable by anything on the host, and this one travels with the
    checkout, is never tracked, and disappears with it.
    """
    git_dir = _git(repo, "rev-parse", "--absolute-git-dir")
    return Path(git_dir) / TENANCY_FILE if git_dir else None


def _read_tenancy(path: Path) -> dict[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_tenancy(path: Path, session: str, branch: str | None) -> None:
    """Record who is standing here and on what branch. Failure is not reported.

    A guard that cannot write its note still has to allow the command, so every error
    here is swallowed: the cost is a missed notice next time, not a blocked command.
    """
    payload = {
        "session": session,
        "branch": branch or "",
        "wrote": str(int(time.time())),
    }
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()


def tenancy_notices(
    payload: dict[str, object], repo: Path, branch_after: str | None
) -> list[str]:
    """Advisory lines about somebody else standing in this working directory.

    The guard's other answers are about *branches and destinations*; this one is about
    the directory. Several assistant sessions share the repository root here — one
    working directory, not a worktree each — so a ``git switch`` by either yanks the
    tree out from under the other, silently, because from git's point of view nothing
    unusual happened. A session can then test a branch it did not check out, or commit a
    file another session edited, with every gate green (#1087).

    **This never refuses, and that is a design decision rather than caution.** Refusing
    a switch when the tree is not where this session left it would fire on the entirely
    legitimate case of one session deliberately switching branches, and nothing available
    here distinguishes the two: the hook is one subprocess per command with no memory
    beyond this file. So it reports, and the two things it can report are both facts
    rather than guesses — another session's id was the last to run a git command in this
    directory, and the branch moved between this session's last command and this one.

    ``branch_after`` is where the command in hand will leave HEAD, so a session that
    switches branches itself does not then report the move to itself next time.
    """
    session = payload.get("session_id")
    if not isinstance(session, str) or not session:
        # With no session id there is nothing to compare: two sessions would look like
        # one, and reporting either way would be a guess. Silence is the honest answer.
        return []

    path = _tenancy_file(repo)
    if path is None:
        return []

    previous = _read_tenancy(path)
    standing_on = current_branch(repo)
    _write_tenancy(path, session, branch_after if branch_after else standing_on)

    if previous is None:
        return []

    lines: list[str] = []
    other = previous.get("session")
    if isinstance(other, str) and other and other != session:
        lines.append(
            f"shared-branch guard: another session ({other[:8]}) last ran a git command "
            f"in {repo}. A switch by either of you moves this tree under the other, and "
            "nothing refuses it — work in `git worktree add <path> -b <branch>` instead "
            "of sharing one checkout."
        )
    was = previous.get("branch") or None
    if was != standing_on:
        lines.append(
            f"shared-branch guard: {repo} was on {was or 'a detached HEAD'} when this "
            f"session last ran a git command here and is now on "
            f"{standing_on or 'a detached HEAD'}. Nothing here moved it, so check what "
            "you are about to commit or test is the branch you think it is."
        )
    return lines


def chdir_target(argv: list[str], base: Path) -> Path | None:
    """Where a ``cd`` / ``pushd`` segment moves to, or ``None`` if not one.

    Only ``cd``/``pushd`` with an explicit path, and bare ``cd``, are read.
    ``cd -``, bare ``pushd`` and ``popd`` depend on a directory stack this does
    not keep, so they are left untracked rather than guessed at: a later segment
    is then judged against the directory in force before them, which over-refuses
    rather than under-refuses when that directory is the shared-branch one.
    """
    if argv[0] not in ("cd", "pushd"):
        return None
    if len(argv) == 1:
        return Path.home() if argv[0] == "cd" else None
    path = argv[1]
    if path.startswith("-"):
        return None
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else base / candidate


def decide(payload: dict[str, object], notices: list[str] | None = None) -> str | None:
    """The reason to block ``payload``'s command, or ``None`` to allow it.

    ``notices`` collects lines to print alongside an allowed command -- currently
    only that an override turned a check off.
    """
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

    # The working tree to say something about co-tenancy for, and the notice is left to
    # the end of the loop because what it reports -- the branch this command leaves HEAD
    # on -- is only known once every segment has been read.
    tenancy_repo: Path | None = None

    def note(line: str) -> None:
        if notices is not None and line not in notices:
            notices.append(line)

    for tokens in segments(command):
        # Keywords first, then assignments: a shell accepts them in that order
        # (`then ALLOW_SHARED_BRANCH=1 git push ...`).
        inline, argv = split_assignments(strip_shell_keywords(tokens))
        if not argv:
            continue

        moved = chdir_target(argv, base)
        if moved is not None:
            base = moved
            continue

        git_args = subcommand(argv, "git")
        if git_args:
            repo = command_repo(argv, base)
            if git_args[0] in TENANCY_SUBCOMMANDS and tenancy_repo is None:
                tenancy_repo = repo

            if git_args[0] in SWITCH_SUBCOMMANDS:
                moves_head, target = switch_target(git_args)
                if moves_head:
                    head = target
                    head_known = target is not None
                continue

            if git_args[0] == "commit":
                if _allowed(ALLOW_SHARED_BRANCH, inline):
                    note(_waived(ALLOW_SHARED_BRANCH, "the branch this commits onto"))
                    continue
                if not head_known:
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
                    note(_waived(ALLOW_SHARED_BRANCH, "this push's destination"))
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

        gh_args = subcommand(argv, "gh", GH_GLOBAL_WITH_VALUE)
        if gh_args and gh_args[:2] == ["pr", "merge"]:
            if _allowed(ALLOW_RED_MERGE, inline):
                note(_waived(ALLOW_RED_MERGE, "this pull request's checks"))
                continue
            failures = failing_checks(
                merge_target(gh_args[1:]), base, gh_repo_selector(argv)
            )
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

    if tenancy_repo is not None:
        for line in tenancy_notices(
            payload, tenancy_repo, head if head_known else None
        ):
            note(line)

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return 0

    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0

    notices: list[str] = []
    try:
        reason = decide(payload, notices)
    except Exception:  # noqa: BLE001 - a guard must not wedge the session
        return 0

    if reason is None:
        for line in notices:
            print(line, file=sys.stderr)
        return 0

    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
