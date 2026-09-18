#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Block internal-only content from reaching a public commit message or PR body.

Registered as a ``PreToolUse`` hook on ``Bash`` in ``.claude/settings.json``. It
reads the hook payload on stdin, and when the command is one that *publishes*
text — ``git commit``, ``git tag``, ``gh pr create`` and friends — scans that
command for content that is never legitimate in this repository, which is public.

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
