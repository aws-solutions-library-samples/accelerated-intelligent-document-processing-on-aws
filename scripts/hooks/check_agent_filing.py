#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Refuse ``gh issue create`` from a subagent's worktree.

Registered as a ``PreToolUse`` hook on ``Bash`` in ``.claude/settings.json``,
alongside ``check_commit_text.py`` and ``check_shared_branch.py``.

**Why this is a hook rather than a sentence in a brief.** The backlog loop
(``.claude/skills/work-the-backlog.md``) delegates one issue per subagent, and an
agent reading unfamiliar code to verify its own change finds things. Filing them
is the coordinator's call, for a reason that was measured rather than assumed:
one run filed **15 issues against 1 closed** before anyone looked at the ratio,
and after filing was told to stop, **eight more appeared in ninety seconds** --
seven of them a sweep of a package an agent had opened only to check its own
change's callers. Nothing in that sweep was wrong. The loop had simply become an
issue generator with a fix loop attached, and the backlog nearly doubled over one
18.5-hour run (7 closed, 24 filed).

The control in force until now was a paragraph each fixer had to copy verbatim
into its reviewer's brief. That is a request propagated by transcription: it
survives only as long as every agent in a chain both reads it and passes it on,
and the measured breach is what it looks like when one does not. A hook refuses
the command instead.

**The discriminator is ``cwd``, because the coordinator may file and agents may
not.** Hooks are registered per session, not per agent, so this one sees the
coordinator's ``gh`` calls too. Fixer and reviewer agents work in a worktree
under the run's prefix (``$HOME/wt/<runId>/<slug>``, a convention section 5 of
that skill makes computable precisely so that it can be checked), while the
coordinator works in the main checkout. So a ``gh issue create`` from inside a
worktree prefix is refused and one from anywhere else is allowed. Set
``BACKLOG_WORKTREE_PREFIX`` to override the default of ``$HOME/wt``.

That boundary is approximate in one direction and exact in the other: an agent
that leaves its worktree before filing is not refused, and the coordinator is
never refused. The inexact direction is the safe one -- this raises the bar on
the measured accident (an agent filing from the tree it is working in) without
ever blocking the actor the skill says owns the decision.

Override: ``ALLOW_AGENT_FILING=1``, read from an inline assignment on the command
itself and from the environment. Inline is the form to reach for, because it
applies to one command. An honoured override prints a line to stderr naming the
check it disabled, for the same reason the sibling hooks do: a variable exported
by a shell profile or a runner persists and turns the check off for every command
in that environment, which is invisible by construction.

**What this does not cover.** It is not a capability boundary, and the skill is
explicit that no text denylist on a shell command is one -- ``python3 -c``,
variable indirection, a wrapper script or base64 all reach ``gh`` without
spelling it here, and an agent that ``cd``s out of its worktree first is outside
the discriminator. What it buys is the naive case, which is the case that
actually happened, plus an audit trail. The severe-finding carve-out in the skill
stands: silent data loss or a security control that does not fire goes to the
coordinator immediately, and the coordinator files it.

Run the tests with ``make test-hooks``.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

#: Shell operators that separate one command from the next. A single tool call
#: often carries several, and only the segments that invoke ``gh`` matter.
SEGMENT_SEPARATORS = {"&&", "||", "|", ";", "&", "\n"}

#: Leading words that are shell keywords rather than the command being run.
#: ``if make test; then gh issue create ...; fi`` puts ``then`` first in a
#: segment, and that is an ordinary thing to type.
SHELL_KEYWORDS = {
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "do",
    "done",
    "while",
    "until",
    "for",
    "case",
    "esac",
    "{",
    "}",
    "(",
    ")",
    "!",
    "time",
    "command",
}

#: Values that count as turning the override on. Lower-cased and stripped before
#: comparison, matching ``check_shared_branch.py``'s reader rather than
#: ``pre-push``'s stricter enumeration.
AFFIRMATIVE = {"1", "true", "yes", "y", "on"}

OVERRIDE = "ALLOW_AGENT_FILING"


def _affirmative(value: str | None) -> bool:
    return value is not None and value.strip().lower() in AFFIRMATIVE


def _segments(command: str) -> list[list[str]]:
    """Split a command into segments and tokenise each one.

    Returns an empty list when the command cannot be lexed. A guard that cannot
    read its input allows the command: wedging the session is worse than missing
    a case, which is the choice both sibling hooks make.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []

    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in SEGMENT_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _strip_leading_noise(tokens: list[str]) -> tuple[list[str], bool]:
    """Drop shell keywords and inline assignments from the front of a segment.

    Returns the remaining tokens and whether an inline ``ALLOW_AGENT_FILING``
    assignment turned the override on.
    """
    override = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in SHELL_KEYWORDS:
            index += 1
            continue
        if "=" in token and not token.startswith("="):
            name, _, value = token.partition("=")
            if name == OVERRIDE and _affirmative(value):
                override = True
            if name.replace("_", "").isalnum():
                index += 1
                continue
        break
    return tokens[index:], override


def _is_gh_issue_create(tokens: list[str]) -> bool:
    """True when the segment invokes ``gh issue create``.

    ``--repo``/``-R`` is registered on gh's root command, so it is accepted on
    either side of the subcommand: ``gh -R owner/name issue create`` is valid.
    Reading ``owner/name`` as the subcommand would leave the command
    unrecognised and so allow it, which is the same misreading
    ``check_shared_branch.py`` guards against for ``gh pr merge``.
    """
    if not tokens or tokens[0] != "gh":
        return False

    #: Root options that take a separate value, so the following token is that
    #: value rather than a subcommand.
    takes_value = {"--repo", "-R", "--hostname"}

    words: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in takes_value:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        words.append(token)
        index += 1

    return words[:2] == ["issue", "create"]


def _worktree_prefix() -> Path | None:
    configured = os.environ.get("BACKLOG_WORKTREE_PREFIX")
    if configured:
        return Path(configured)
    home = os.environ.get("HOME")
    return Path(home) / "wt" if home else None


def _inside_worktree_prefix(cwd: str | None) -> Path | None:
    """Return the prefix when ``cwd`` sits under it, else ``None``.

    Roots are compared by equality after resolution rather than with
    ``is_relative_to`` alone, because a resolved path that merely *looks* nested
    is the shape the skill records as having bitten three separate controls here
    ("ancestry is not identity"). ``is_relative_to`` is the right predicate for
    "under this prefix", so it is kept -- what is added is resolving both sides
    first, so a symlink into the prefix is read as being in it and a sibling
    directory whose name merely starts with the prefix is not.
    """
    prefix = _worktree_prefix()
    if prefix is None or not cwd:
        return None
    try:
        here = Path(cwd).resolve()
        root = prefix.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if here == root or root in here.parents:
        return root
    return None


def decide(payload: dict) -> tuple[str | None, list[str]]:
    """Return a refusal reason, or ``None`` to allow, plus stderr notices."""
    notices: list[str] = []

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None, notices
    command = tool_input.get("command")
    if not isinstance(command, str) or "gh" not in command:
        return None, notices

    env_override = _affirmative(os.environ.get(OVERRIDE))

    filing_segments = []
    inline_override = False
    for tokens in _segments(command):
        stripped, segment_override = _strip_leading_noise(tokens)
        inline_override = inline_override or segment_override
        if _is_gh_issue_create(stripped):
            filing_segments.append(stripped)

    if not filing_segments:
        return None, notices

    root = _inside_worktree_prefix(payload.get("cwd"))
    if root is None:
        # The main checkout, i.e. the coordinator. The skill says it may file,
        # and should say so plainly when it does.
        return None, notices

    if inline_override or env_override:
        source = "inline assignment" if inline_override else "the environment"
        notices.append(
            f"check-agent-filing: refusal disabled by {OVERRIDE}=1 from {source}. "
            "Filing an issue from an agent worktree; the coordinator owns this "
            "decision, so say in your report that you filed it and why."
        )
        return None, notices

    return (
        "check-agent-filing: refusing `gh issue create` from an agent worktree "
        f"({root}).\n"
        "\n"
        "Out-of-scope findings go to the coordinator as one line each in your "
        "final report, and the coordinator decides what gets filed. This is not "
        "a judgement about the finding: one run here filed 15 issues against 1 "
        "closed, and 8 more inside ninety seconds after filing was told to stop, "
        "and every one of those findings was real. Enumerating callers is a "
        "verification technique, not a licence to file.\n"
        "\n"
        "Two exceptions go to the coordinator immediately rather than waiting "
        "for your report: silent data loss or corruption, and a security "
        "control that does not fire.\n"
        "\n"
        f"If you are the coordinator and this is your decision, prefix the "
        f"command with {OVERRIDE}=1."
    ), notices


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return 0

    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0

    try:
        reason, notices = decide(payload)
    except Exception:  # noqa: BLE001 - a guard must not wedge the session
        return 0

    for line in notices:
        print(line, file=sys.stderr)

    if reason is None:
        return 0

    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
