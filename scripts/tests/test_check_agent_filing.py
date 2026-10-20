# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the ``gh issue create`` guard in ``scripts/hooks/check_agent_filing.py``.

The guard's whole value is the discrimination it makes -- an agent worktree is
refused, the main checkout is not -- so the tests are written around that
boundary in both directions rather than around the refusal alone. A guard that
refused everything would pass a one-sided suite and would stop the coordinator
from filing, which the skill says is its decision to make.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "check_agent_filing.py"


def run_hook(
    command: str,
    cwd: str,
    env_extra: dict[str, str] | None = None,
    home: str = "/home/runner",
) -> subprocess.CompletedProcess[str]:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": cwd,
    }
    env = {"HOME": home, "PATH": "/usr/bin:/bin"}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


AGENT_CWD = "/home/runner/wt/2026-10-20-1/fix-ocr"
MAIN_CWD = "/home/runner/projects/idp1"


# --------------------------------------------------------------------------
# The refusal
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "gh issue create --title x --body y",
        # --repo on either side of the subcommand: gh registers it on the root
        # command, so both spellings are valid and both must be read.
        "gh --repo owner/name issue create --title x",
        "gh -R owner/name issue create --title x",
        "gh issue create --repo owner/name --title x",
        # An option that takes a separate value must not have its value read as
        # the subcommand.
        "gh --hostname github.com issue create --title x",
        # Not the first command in the call.
        "pytest -q && gh issue create --title x",
        # Behind a shell keyword, which is an ordinary thing to type.
        "if pytest -q; then gh issue create --title x; fi",
    ],
)
def test_filing_from_an_agent_worktree_is_refused(command: str) -> None:
    result = run_hook(command, AGENT_CWD)
    assert result.returncode == 2, result.stderr
    assert "refusing `gh issue create`" in result.stderr


def test_the_refusal_names_the_report_route_and_the_two_exceptions() -> None:
    """The message has to be actionable, or the agent works around it.

    It must say where findings go instead, and it must name the two categories
    that bypass the report -- otherwise the rule reads as "stay silent", which is
    the failure mode the skill warns about explicitly.
    """
    result = run_hook("gh issue create --title x", AGENT_CWD)
    assert "final report" in result.stderr
    assert "silent data loss" in result.stderr
    assert "security control that does not fire" in result.stderr


# --------------------------------------------------------------------------
# The other direction: what must NOT be refused
# --------------------------------------------------------------------------


def test_the_coordinator_may_file_from_the_main_checkout() -> None:
    result = run_hook("gh issue create --title x", MAIN_CWD)
    assert result.returncode == 0
    assert result.stderr == ""


@pytest.mark.parametrize(
    "command",
    [
        # Reading issues is the thing every fixer is told to do.
        "gh issue view 1146 --comments",
        "gh issue list --repo owner/name --state open --limit 500",
        # Opening the PR is the agent's job.
        "gh pr create --base backlog/staging --title x",
        "gh pr checks 1197",
        # A commit message that merely mentions the words.
        "git commit -m 'gh issue create is refused from a worktree'",
        # Another tool entirely.
        "pytest -q -n 4 tests/unit/ocr",
    ],
)
def test_everything_other_than_filing_is_allowed_from_a_worktree(
    command: str,
) -> None:
    result = run_hook(command, AGENT_CWD)
    assert result.returncode == 0, result.stderr


def test_a_sibling_directory_sharing_the_prefix_string_is_not_inside_it() -> None:
    """``/home/runner/wtx`` is not under ``/home/runner/wt``.

    A prefix test written as a string comparison gets this wrong, and the skill
    records "ancestry is not identity" as having bitten three separate controls
    here. The failure direction would be a false refusal in an unrelated tree.
    """
    result = run_hook("gh issue create --title x", "/home/runner/wtx/project")
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------
# The override, and the notice that makes it visible
# --------------------------------------------------------------------------


def test_an_inline_override_allows_the_command_and_says_so() -> None:
    result = run_hook("ALLOW_AGENT_FILING=1 gh issue create --title x", AGENT_CWD)
    assert result.returncode == 0
    assert "refusal disabled by ALLOW_AGENT_FILING=1" in result.stderr
    assert "inline assignment" in result.stderr


def test_an_environment_override_allows_the_command_and_says_so() -> None:
    """The environment form is the one that persists, so it must announce itself.

    A variable exported by a shell profile or a runner turns the check off for
    every command in that environment, which is invisible by construction. The
    stderr line is the only thing that makes it visible.
    """
    result = run_hook(
        "gh issue create --title x", AGENT_CWD, {"ALLOW_AGENT_FILING": "1"}
    )
    assert result.returncode == 0
    assert "refusal disabled by ALLOW_AGENT_FILING=1" in result.stderr
    assert "the environment" in result.stderr


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_a_non_affirmative_override_leaves_the_guard_on(value: str) -> None:
    result = run_hook(
        "gh issue create --title x", AGENT_CWD, {"ALLOW_AGENT_FILING": value}
    )
    assert result.returncode == 2, result.stderr


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "y", "on", " 1 "])
def test_affirmative_spellings_are_honoured(value: str) -> None:
    result = run_hook(
        "gh issue create --title x", AGENT_CWD, {"ALLOW_AGENT_FILING": value}
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------
# Configuration and failing open
# --------------------------------------------------------------------------


def test_the_prefix_is_configurable() -> None:
    result = run_hook(
        "gh issue create --title x",
        "/srv/agents/run-1/fix-ocr",
        {"BACKLOG_WORKTREE_PREFIX": "/srv/agents"},
    )
    assert result.returncode == 2, result.stderr


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        '{"tool_name": "Read", "tool_input": {"file_path": "x"}}',
        '{"tool_name": "Bash"}',
        '{"tool_name": "Bash", "tool_input": "a string"}',
        "[]",
    ],
)
def test_an_unreadable_payload_allows_the_command(payload: str) -> None:
    """A guard that cannot read its input allows the command.

    Wedging the session is worse than missing a case, which is the choice both
    sibling hooks make.
    """
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env={"HOME": "/home/runner", "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode == 0


def test_an_unlexable_command_allows_it() -> None:
    result = run_hook('gh issue create --title "unterminated', AGENT_CWD)
    assert result.returncode == 0


def test_a_missing_cwd_allows_the_command() -> None:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "gh issue create --title x"},
    }
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"HOME": "/home/runner", "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode == 0
