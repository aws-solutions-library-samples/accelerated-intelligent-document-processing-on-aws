#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Refuse credential-file reads and off-list fetches from an agent worktree.

Registered as a ``PreToolUse`` hook on ``Bash`` in ``.claude/settings.json``,
beside ``check_agent_filing.py``, and scoped the same way: it acts only when the
command runs from a worktree under ``$HOME/wt`` (or ``BACKLOG_WORKTREE_PREFIX``),
which is where fixer and reviewer agents work. The main checkout is never
refused.

Inside such a worktree it refuses a Bash command that

* names a local credential store: ``~/.aws``, ``~/.config/gh``, ``~/.netrc``
  (spelled with ``~``, ``$HOME``, ``${HOME}`` or the home path itself), or a
  process environment file, ``/proc/<anything>/environ``;
* runs ``curl``, ``wget``, ``nc``, ``ncat`` or ``netcat`` against a host other
  than GitHub's, the repository's own GitLab origin host, PyPI's or npm's, or
  against a host this hook cannot read off the command line (a URL in a
  variable, for instance).

The rule behind both: agents working the backlog have no use for the host's
credentials, and the fetches they need go to the code and package hosts above.
``fix-single-issue.md`` and ``work-the-backlog.md`` state that rule for agents;
this hook refuses the plain spellings of breaking it.

⚠️ **This is text matching on the command line. It is not a capability
boundary.** A script file, ``python3 -c``, ``bash -c`` with an assembled string,
variable indirection, an encoded path, another language's HTTP client, or a
``cd`` out of the worktree all reach the same files and hosts without spelling
them where this hook looks. The control that would actually work is not having
readable long-lived credentials on the host, which is outside this repository.
What this buys is the plain case, and a line in the transcript when it fires.

Override: ``ALLOW_WORKTREE_REACH=1``, read from an inline assignment on the
command and from the environment, the same as the sibling hooks. An honoured
override prints one line to stderr naming the check it disabled, because a
variable exported by a shell profile or a runner persists and would otherwise
turn the check off for every command, invisibly.

A command it cannot parse is allowed, for the reason every hook here gives: a
guard that wedges the session is worse than one that misses a case.

Run the tests with ``make test-hooks``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
from pathlib import Path

OVERRIDE = "ALLOW_WORKTREE_REACH"
AFFIRMATIVE = {"1", "true", "yes", "y", "on"}
SEGMENT_SEPARATORS = {"&&", "||", "|", ";", "&", "\n", "(", ")"}

#: Credential stores under the home directory, relative to it.
HOME_CREDENTIAL_PATHS = (".aws", ".config/gh", ".netrc")

#: ``/proc/<pid or self or a glob>/environ``, however the middle is spelled.
PROC_ENVIRON = re.compile(r"/proc/[^/\s'\"]*/+environ\b")

#: The commands whose destination is checked.
FETCHERS = {"curl", "wget", "nc", "ncat", "netcat"}

#: Hosts a fetch may reach, matched exactly. GitHub's web, API, raw-content and
#: archive hosts; PyPI's index and file host; npm's registry. The repository's
#: GitLab origin host is added at run time from ``git remote get-url origin``.
ALLOWED_FETCH_HOSTS = frozenset(
    {
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "www.npmjs.com",
    }
)

#: curl and wget options that take a separate value, so the next token is that
#: value and not a URL. Only the ones whose value could look like a host matter.
VALUE_OPTIONS = {
    "-o", "--output", "-H", "--header", "-d", "--data", "--data-raw",
    "--data-binary", "-u", "--user", "-A", "--user-agent", "-e", "--referer",
    "-X", "--request", "-w", "--write-out", "-O", "--output-document",
    "--resolve", "--connect-to", "-T", "--upload-file", "-b", "--cookie",
    "-c", "--cookie-jar", "-m", "--max-time", "--retry", "-x", "--proxy",
    "-K", "--config",
}  # fmt: skip


def _affirmative(value: str | None) -> bool:
    return value is not None and value.strip().lower() in AFFIRMATIVE


def _segments(command: str) -> list[list[str]]:
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
    return [segment for segment in segments if segment]


def _inline_override(segments: list[list[str]]) -> bool:
    for tokens in segments:
        for token in tokens:
            name, sep, value = token.partition("=")
            if not sep or not name.replace("_", "").isalnum():
                break
            if name == OVERRIDE and _affirmative(value):
                return True
    return False


def _worktree_root(cwd: str | None) -> Path | None:
    configured = os.environ.get("BACKLOG_WORKTREE_PREFIX")
    home = os.environ.get("HOME")
    prefix = Path(configured) if configured else (Path(home) / "wt" if home else None)
    if prefix is None or not cwd:
        return None
    try:
        here = Path(cwd).resolve()
        root = prefix.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return root if here == root or root in here.parents else None


def _credential_paths(command: str, home: str) -> list[str]:
    """Credential stores the command text names, in any of its spellings."""
    found: list[str] = []
    homes = [
        re.escape(spelling) for spelling in ("~", "$HOME", "${HOME}", home) if spelling
    ]
    for relative in HOME_CREDENTIAL_PATHS:
        pattern = (
            r"(?:(?<![\w/.~$])|(?<=[=:]))(?:"
            + "|".join(homes)
            + r")/+"
            + re.escape(relative).replace("/", "/+")
            + r"(?![\w.-])"
        )
        if re.search(pattern, command):
            found.append(f"~/{relative}")
    if PROC_ENVIRON.search(command):
        found.append("/proc/*/environ")
    return found


def _origin_host(cwd: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603, S607 - fixed argv
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    url = result.stdout.strip()
    match = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([A-Za-z0-9.-]+)[:/]", url)
    return match.group(1).lower() if match else ""


def _fetch_hosts(tokens: list[str]) -> tuple[str, list[str]] | None:
    """The fetcher a segment runs and the hosts it names, or ``None``."""
    for index, token in enumerate(tokens):
        name = token.rsplit("/", 1)[-1]
        if name in FETCHERS:
            break
    else:
        return None
    hosts: list[str] = []
    rest = tokens[index + 1 :]
    skip = False
    for token in rest:
        if skip:
            skip = False
            continue
        if token in VALUE_OPTIONS:
            skip = True
            continue
        if token.startswith("-"):
            continue
        if "://" in token:
            hosts.append((urllib.parse.urlsplit(token).hostname or "?").lower())
        elif name in {"nc", "ncat", "netcat"}:
            if not token.isdigit():
                hosts.append(token.lower())
        else:
            # curl and wget take a scheme-less URL too: host[:port][/path].
            hosts.append(re.split(r"[:/]", token, maxsplit=1)[0].lower() or "?")
    return name, hosts or ["?"]


def decide(payload: dict) -> tuple[str | None, list[str]]:
    """Return a refusal reason, or ``None`` to allow, plus stderr notices."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None, []
    command = tool_input.get("command")
    cwd = payload.get("cwd")
    if not isinstance(command, str) or _worktree_root(cwd) is None:
        return None, []

    segments = _segments(command)
    problems = [
        f"names {path}"
        for path in _credential_paths(command, os.environ.get("HOME", ""))
    ]
    allowed = set(ALLOWED_FETCH_HOSTS)
    origin = _origin_host(str(cwd))
    if origin:
        allowed |= {origin, origin.removeprefix("ssh.")}
    for tokens in segments:
        fetch = _fetch_hosts(tokens)
        if fetch is None:
            continue
        name, hosts = fetch
        for host in hosts:
            if host not in allowed:
                shown = "a host it cannot read" if host == "?" else host
                problems.append(f"runs {name} against {shown}")

    if not problems:
        return None, []
    if _inline_override(segments) or _affirmative(os.environ.get(OVERRIDE)):
        source = (
            "inline assignment" if _inline_override(segments) else "the environment"
        )
        return None, [
            f"check-worktree-reach: refusal disabled by {OVERRIDE}=1 from {source} "
            f"for a command that {'; '.join(problems)}."
        ]
    return (
        "check-worktree-reach: refusing this command in an agent worktree: it "
        + "; ".join(problems)
        + ".\n\nAgents working the backlog hold no AWS, GitHub or other host "
        "credentials, and fetch only from GitHub, this repository's GitLab host, "
        "PyPI and npm. If the work genuinely needs this, stop and ask the "
        f"coordinator; to run it deliberately, prefix the command with {OVERRIDE}=1."
    ), []


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
