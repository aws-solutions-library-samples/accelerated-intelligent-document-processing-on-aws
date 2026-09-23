#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Block internal-only content, and CI suppression, from a public commit or PR body.

Registered as a ``PreToolUse`` hook on ``Bash`` in ``.claude/settings.json``. It
reads the hook payload on stdin, and when the command is one that *publishes*
text — ``git commit``, ``git tag``, ``gh pr create`` and friends — scans that
command for content that is never legitimate in this repository, which is public.

It refuses two different things, and the second is about the gates rather than
about the reader. A commit message carrying one of the directives in
:data:`CI_SUPPRESSION_DIRECTIVES` suppresses **every** gate on **both** CI
platforms — neither configuration opts into that and neither can switch it off in
YAML, because both honour the markers natively — and since no check is required on
this repository's branches (issue #933), the pull request does not merely merge with
a red check: it merges with nothing having run and nothing to see. That has already
happened here, and what reached ``develop`` through it broke the security gate for
every branch cut afterwards (#1072).

Why a hook and not a git hook: ``core.hooksPath`` on an Amazon-managed machine
points at git-defender, so a repo ``commit-msg`` hook or ``.githooks`` directory
is bypassed. This runs before the command is executed instead.

Why it matters more here than in most repositories: a merged commit message
cannot be edited, and force-pushing does not retract one, because GitHub keeps a
merged pull request's commits and diff view independently of any branch. The only
remedy after the fact is a GitHub Support request.

Scope is deliberately narrow — addresses, hostnames and identifiers, which are
mechanical. The judgment that goes with it, such as writing a message at summary
altitude rather than as an inventory of strings, lives in ``CLAUDE.md`` and
``.claude/skills/code-review.md``.

Exit codes: 0 to allow, 2 to block with the reason on stderr. Anything
unexpected — malformed payload, unreadable stdin — allows the command rather than
wedging the session, since this is a safety net and not the only control.
"""

from __future__ import annotations

import json
import re
import sys

# Addresses that are published on purpose: the AWS VDP intake named in
# SECURITY.md, the code-of-conduct alias in CONTRIBUTING.md, and the packaging
# placeholder in pyproject metadata.
ALLOWED_EMAILS = frozenset(
    {
        "aws-security@amazon.com",
        "opensource-codeofconduct@amazon.com",
        "noreply@amazon.com",
    }
)

# Public AWS hostnames that would otherwise be caught by a broad amazon.com or
# aws.dev rule. Matched as a whole host, so a subdomain is not covered by proxy.
ALLOWED_HOSTS = frozenset(
    {
        "aws.amazon.com",
        "docs.aws.amazon.com",
        "console.aws.amazon.com",
        "repost.aws.amazon.com",
        "amazon.com",
        "www.amazon.com",
    }
)

# The subdomain group is required, not cosmetic: without it an address on an
# internal host (someone@code.amazon.com) matches neither this pattern nor HOST,
# because HOST skips a match preceded by "@" to avoid double-reporting.
EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)*(?:amazon\.com|a2z\.com|amazon\.dev|aws\.dev)"
)

# Hosts that only exist behind Midway. Each alternative is anchored on a dot or a
# string start so that "myaws.dev.example.com" does not match.
HOST = re.compile(
    r"(?<![A-Za-z0-9.-])"
    r"(?:[A-Za-z0-9-]+\.)*"
    r"(?:"
    r"corp\.amazon\.com"
    r"|a2z\.com"
    r"|amazon\.dev"
    r"|aws\.dev"
    r"|quip-amazon\.com"
    r"|(?:code|w|sim|issues|tiny|phonetool|broadcast)\.amazon\.com"
    r")"
    r"(?![A-Za-z0-9-])"
)

# Internal identifiers. Each requires a boundary that a hex hash or a version
# string will not satisfy, because a false positive here blocks a real commit.
IDENTIFIERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("code review id", re.compile(r"(?<![A-Za-z0-9])CR-\d{6,}(?![A-Za-z0-9])")),
    ("SIM/ticket id", re.compile(r"(?<![A-Za-z0-9])[PV]\d{8,10}(?![A-Za-z0-9])")),
)

# Commands that publish text others will read. `git commit` covers `-m` and
# heredoc bodies alike, because both appear in the command string.
PUBLISHING = re.compile(
    r"\b(?:"
    r"git\s+(?:commit|tag|notes)"
    r"|gh\s+pr\s+(?:create|edit|comment|review)"
    r"|gh\s+issue\s+(?:create|edit|comment)"
    r"|gh\s+release\s+(?:create|edit)"
    r")\b"
)

# The commit-message directives that suppress CI, as each platform documents them.
# GitHub Actions skips a `push`- or `pull_request`-triggered run when the head commit
# message contains any of `[skip ci]`, `[ci skip]`, `[no ci]`, `[skip actions]` or
# `[actions skip]`; GitLab skips pipeline CREATION for `[skip ci]` and `[ci skip]`.
# The union is what has to be refused, since one platform honouring a directive is
# enough to take its gates out, and the hyphenated spellings are included because a
# reader typing one means the same thing and platforms have accepted both.
#
# Matched case-insensitively and anywhere in the command, which is where a `-m` body
# and a heredoc body both appear.
CI_SUPPRESSION_DIRECTIVES = (
    "[skip ci]",
    "[ci skip]",
    "[no ci]",
    "[skip actions]",
    "[actions skip]",
)

CI_SUPPRESSION = re.compile(
    r"\[\s*(?:skip[ \-_]ci|ci[ \-_]skip|no[ \-_]ci|skip[ \-_]actions|actions[ \-_]skip)"
    r"\s*\]",
    re.IGNORECASE,
)

# Per-command override, in the same family as ALLOW_SHARED_BRANCH in
# check_shared_branch.py: there is a legitimate case (a branch nobody will merge), and
# a guard with no way out gets removed rather than argued with.
#
# Read from the COMMAND TEXT rather than from this process's environment, because an
# inline `VAR=1 git commit ...` prefix never reaches the hook's environment — the hook
# runs before the command does. Anchored to a command boundary so that the same
# characters inside a commit message do not waive anything.
ALLOW_SKIP_CI = "ALLOW_SKIP_CI"
_OVERRIDE = re.compile(
    rf"(?:^|[;&|(\n]|&&|\|\|)\s*(?:\w+=\S+\s+)*{ALLOW_SKIP_CI}=(\S+)"
)
_AFFIRMATIVE = frozenset({"1", "true", "yes", "y", "on"})

CI_SUPPRESSION_REMEDY = (
    "That directive suppresses EVERY gate on BOTH CI platforms — lint, types, tests, "
    "the security scan and the dependency audit — and no check on this repository is "
    "a required status check (issue #933), so the pull request does not show red: it "
    "shows nothing, and a reviewer has no way to tell it apart from a clean run.\n"
    "Drop the directive. If the work genuinely does not need the gates, it still costs "
    "nothing to let them run.\n"
    f"To do it deliberately anyway, put the override in front of the command:\n"
    f"  {ALLOW_SKIP_CI}=1 <your command>\n"
    "Note that a commit already carrying one of these directives is reported by "
    "scripts/tests/test_no_skip_ci_markers.py on the NEXT pull request whose checks "
    "run, so the marker does not stay invisible — it just moves the discovery to "
    "somebody else."
)

REMEDY = (
    "This repository is public and a merged commit message cannot be edited; a "
    "force-push does not retract one either, because GitHub keeps a merged PR's "
    "commits and diff view independently of any branch.\n"
    "Rewrite the text without the internal reference — an external reader has no "
    "way to follow it anyway. If the string is legitimately public, add it to the "
    "allowlist in scripts/hooks/check_commit_text.py with a comment saying why.\n"
    "See the 'Commit messages and PR descriptions are published text' section of "
    "CLAUDE.md."
)


def is_publishing(command: str) -> bool:
    """True when the command writes text into git history or onto GitHub."""
    return bool(PUBLISHING.search(command))


def findings(command: str) -> list[str]:
    """Every internal reference in ``command``, as ``"kind: value"`` strings.

    Ordered and de-duplicated so the same address named three times reports once
    and the message stays readable.
    """
    hits: list[str] = []

    for match in EMAIL.finditer(command):
        if match.group(0).casefold() not in ALLOWED_EMAILS:
            hits.append(f"internal email address: {match.group(0)}")

    for match in HOST.finditer(command):
        host = match.group(0)
        if host.casefold() in ALLOWED_HOSTS:
            continue
        # An address already reported as an email would otherwise be reported a
        # second time as its own domain.
        start = match.start()
        if start and command[start - 1] == "@":
            continue
        hits.append(f"internal hostname: {host}")

    for label, pattern in IDENTIFIERS:
        for match in pattern.finditer(command):
            hits.append(f"{label}: {match.group(0)}")

    return list(dict.fromkeys(hits))


def ci_suppression(command: str) -> str | None:
    """The CI-suppressing directive in ``command``, or ``None``."""
    match = CI_SUPPRESSION.search(command)
    return match.group(0) if match else None


def override_set(command: str) -> bool:
    """Whether the command carries an affirmative ``ALLOW_SKIP_CI`` prefix.

    Only an affirmative value counts, so spelling out ``ALLOW_SKIP_CI=0`` leaves the
    check on rather than reading as permission — the same choice the shared-branch
    guard makes.
    """
    match = _OVERRIDE.search(command)
    return bool(match) and match.group(1).strip().strip("\"'").lower() in _AFFIRMATIVE


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return 0

    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0

    tool_input = payload.get("tool_input")
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str) or not is_publishing(command):
        return 0

    directive = ci_suppression(command)
    if directive:
        if override_set(command):
            # An override that is honoured says so, for the same reason the
            # shared-branch guard's does: a check believed on and actually off is
            # worse than no check.
            print(
                f"commit-text guard: {ALLOW_SKIP_CI} is set, so the CI-suppressing "
                f"directive {directive!r} in this command is not refused.",
                file=sys.stderr,
            )
        else:
            print(
                f"Blocked: this command would publish the CI-suppressing directive "
                f"{directive!r}.\n\n" + CI_SUPPRESSION_REMEDY,
                file=sys.stderr,
            )
            return 2

    hits = findings(command)
    if not hits:
        return 0

    print(
        "Blocked: this command would publish internal-only content.\n\n"
        + "\n".join(f"  - {hit}" for hit in hits)
        + "\n\n"
        + REMEDY,
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
