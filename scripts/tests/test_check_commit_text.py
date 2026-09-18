# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the ``check-commit-text`` PreToolUse hook.

Two failure modes matter and they pull in opposite directions. A hook that misses
an internal address is useless; a hook that blocks a legitimate commit gets
disabled by the first person it inconveniences, which is worse than not having it.
So the negative cases here — public AWS URLs, version strings, git SHAs — are as
load-bearing as the positive ones.

The hook is also asserted to be *registered*: a correct script that no settings
file points at protects nothing, and that is exactly the kind of gap that goes
unnoticed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "scripts" / "hooks" / "check_commit_text.py"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"

sys.path.insert(0, str(HOOK.parent))

from check_commit_text import findings, is_publishing  # noqa: E402


def _run(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


# --------------------------------------------------------------------------- #
# what counts as publishing text
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'fix: thing'",
        "git commit -q -F - <<'MSG'\nsubject\nMSG",
        "git tag -a v0.6.9 -m 'release'",
        "gh pr create --base develop --title x --body y",
        "gh pr edit 992 --body x",
        "gh pr comment 992 --body x",
        "gh issue create --title x --body y",
        "gh release create v0.6.9 --notes x",
    ],
)
def test_publishing_commands_are_recognised(command: str) -> None:
    assert is_publishing(command)


@pytest.mark.unit
@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline -5",
        "git diff develop",
        "gh pr view 992 --json body",
        "gh pr list --state open",
        # Reading for internal references must not be mistaken for writing one.
        "git grep -nI '@amazon.com'",
        "rg 'code.amazon.com' docs/",
    ],
)
def test_read_only_commands_are_ignored(command: str) -> None:
    assert not is_publishing(command)


# --------------------------------------------------------------------------- #
# what gets caught
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("someone@amazon.com", "internal email address"),
        ("someone@a2z.com", "internal email address"),
        ("see code.amazon.com/packages/Foo", "internal hostname"),
        ("https://w.amazon.com/bin/view/Team", "internal hostname"),
        ("https://docs.hub.amazon.dev/brazil/", "internal hostname"),
        ("https://hub.cx.aws.dev/", "internal hostname"),
        ("https://quip-amazon.com/abc/Doc", "internal hostname"),
        ("https://issues.amazon.com/issues/foo", "internal hostname"),
        ("https://t.corp.amazon.com/V123", "internal hostname"),
        ("reviewed in CR-12345678", "code review id"),
        ("tracked as P123456789", "SIM/ticket id"),
    ],
)
def test_internal_references_are_reported(text: str, expected: str) -> None:
    hits = findings(f"git commit -m '{text}'")
    assert hits, f"{text!r} was not caught"
    assert any(h.startswith(expected) for h in hits), hits


@pytest.mark.unit
def test_each_reference_is_reported_once() -> None:
    """The same address three times is one finding, so the message stays short."""
    text = "a@amazon.com and a@amazon.com and a@amazon.com"
    assert len(findings(f"git commit -m '{text}'")) == 1


@pytest.mark.unit
def test_an_internal_email_is_not_double_reported_as_its_domain() -> None:
    hits = findings("git commit -m 'ping someone@code.amazon.com'")
    assert len(hits) == 1, hits
    assert hits[0].startswith("internal email address"), hits


# --------------------------------------------------------------------------- #
# what must NOT get caught
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        # The addresses this repository publishes on purpose.
        "report to aws-security@amazon.com",
        "contact opensource-codeofconduct@amazon.com",
        "authors = noreply@amazon.com",
        # Public AWS and third-party URLs.
        "see https://aws.amazon.com/professional-services/",
        "https://docs.aws.amazon.com/bedrock/latest/userguide/",
        "https://console.aws.amazon.com/cloudformation/home",
        "https://hackerone.com/aws_vdp",
        "https://github.com/aws-solutions-library-samples/foo/issues/936",
        "https://aws.amazon.com/marketplace/pp/prodview-guhlipxo6hpl2",
        # Things shaped like an identifier but not one.
        "bump version to v0.6.9",
        "fixes regression from 533a59417",
        "arn:aws:iam::123456789012:role/Foo",
        "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "P12 is a parameter name",
        "V1234567 is only seven digits",
    ],
)
def test_legitimate_text_is_not_blocked(text: str) -> None:
    assert findings(f"git commit -m '{text}'") == []


# --------------------------------------------------------------------------- #
# end-to-end through the hook protocol
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_hook_blocks_with_exit_code_two_and_explains() -> None:
    result = _run(_bash("git commit -m 'ask someone@amazon.com about it'"))
    assert result.returncode == 2, result.stderr
    assert "someone@amazon.com" in result.stderr
    assert "CLAUDE.md" in result.stderr


@pytest.mark.unit
def test_hook_allows_a_clean_commit() -> None:
    result = _run(_bash("git commit -m 'docs: trim the governance files'"))
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Read", "tool_input": {"file_path": "x@amazon.com"}},
        {"tool_name": "Bash", "tool_input": {"command": "git log someone@amazon.com"}},
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
    """A safety net that breaks the session is worse than one that misses a case."""
    assert _run(payload).returncode == 0


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


# --------------------------------------------------------------------------- #
# the hook is actually wired up
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_hook_is_registered_as_a_pretooluse_hook_on_bash() -> None:
    """A correct script nothing invokes protects nothing."""
    assert SETTINGS.is_file(), f"{SETTINGS} is missing; the hook is not registered"
    hooks = json.loads(SETTINGS.read_text(encoding="utf-8")).get("hooks", {})
    entries = hooks.get("PreToolUse", [])
    commands = [
        hook.get("command", "")
        for entry in entries
        if "Bash" in (entry.get("matcher") or "")
        for hook in entry.get("hooks", [])
    ]
    assert any("check_commit_text.py" in c for c in commands), (
        "no PreToolUse hook on Bash runs scripts/hooks/check_commit_text.py; "
        f"found {commands}"
    )
