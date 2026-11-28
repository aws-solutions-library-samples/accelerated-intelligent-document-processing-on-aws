# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""What the failure agent's session may run and read, measured on the real CLI.

``scripts/sdlc/failure_agent.py`` runs ``claude -p`` with the argv
:func:`failure_agent.claude_command` builds. Whether a Bash command is admitted
or refused is decided by the CLI's permission engine, so this test runs the CLI
rather than reading the rules.

No model is called and no AWS service is reached. The scripted Messages API from
``scripts/tests/test_ai_mr_review_session_scope.py`` answers each turn with the
next tool call from a fixed list, and the tool results the CLI sends back show
whether each call ran. ``aws`` is a stand-in script on ``PATH`` that records its
arguments, and ``git`` runs against a throwaway repository.

Runs whenever a usable CLI at the pinned version (``CLAUDE_CODE_NPM_SPEC``) is
present: ``FAILURE_AGENT_CLAUDE_BIN``, else the ``claude`` on ``PATH``. It skips,
saying why, otherwise, which is the case in CI. ``FAILURE_AGENT_PROBE_ANY_VERSION=1``
runs it against another version, which is how to check one before moving the pin.

Every case that must be refused has a control that must run, so a CLI that
refuses everything cannot pass.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))
sys.path.insert(0, str(_HERE.parents[2] / "tests"))

import failure_agent as fa  # noqa: E402
from test_ai_mr_review_session_scope import _Script  # noqa: E402

pytestmark = pytest.mark.unit


def _pinned_version() -> str:
    return fa.CLAUDE_CODE_NPM_SPEC.rsplit("@", 1)[-1]


@pytest.fixture(scope="module")
def cli() -> str:
    binary = os.environ.get("FAILURE_AGENT_CLAUDE_BIN") or ""
    if not binary:
        import shutil

        binary = shutil.which("claude") or ""
    if not binary:
        pytest.skip(
            "no `claude` CLI here; set FAILURE_AGENT_CLAUDE_BIN to an install of "
            "the pinned version to run this measurement"
        )
    version = subprocess.run(  # noqa: S603 - fixed argv
        [binary, "--version"],
        env={"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    found = re.search(r"\d+\.\d+\.\d+", version.stdout)
    if version.returncode != 0 or not found:
        pytest.skip(
            f"{binary} does not run with a minimal environment (exit "
            f"{version.returncode}); set FAILURE_AGENT_CLAUDE_BIN to a plain npm "
            "install"
        )
    pinned = _pinned_version()
    if (
        found.group(0) != pinned
        and os.environ.get("FAILURE_AGENT_PROBE_ANY_VERSION") != "1"
    ):
        pytest.skip(
            f"{binary} is {found.group(0)} and the pin is {pinned}; a measurement "
            f"of another version says nothing about the pin. Set "
            f"FAILURE_AGENT_CLAUDE_BIN to a {pinned} install, or "
            "FAILURE_AGENT_PROBE_ANY_VERSION=1 to measure this one"
        )
    return binary


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(  # noqa: S603, S607 - fixed argv
        ["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_the_session_runs_only_the_allowed_command_shapes(
    cli: str, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "inside.txt").write_text("CONTROLREAD\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "x", cwd=repo)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "build.log").write_text("LOGREAD\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_file = tmp_path / "aws-calls.txt"
    aws = bin_dir / "aws"
    aws.write_text(f'#!/bin/sh\necho "$@" >> {calls_file}\necho FAKEAWS\n')
    aws.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    out = tmp_path / "w"

    allowed = [
        ("aws read", "Bash", {"command": "aws sts get-caller-identity"}),
        ("git read", "Bash", {"command": "git log -1 --format=%H"}),
        ("log dir", "Read", {"file_path": str(log_dir / "build.log")}),
        ("checkout", "Read", {"file_path": str(repo / "inside.txt")}),
    ]
    refused = [
        ("aws --endpoint-url", "aws s3 ls --endpoint-url http://127.0.0.1:9"),
        ("aws --endpoint-url=", "aws s3 ls --endpoint-url=http://127.0.0.1:9"),
        ("git log --output=", f"git log -1 --output={out}1"),
        ("git log --output", f"git log -1 --output {out}2"),
        ("git show --output=", f"git show --output={out}3 HEAD"),
        ("git diff --output=", f"git diff --output={out}4"),
        ("chained", f"aws sts get-caller-identity; touch {out}5"),
        ("and-chained", f"aws sts get-caller-identity && touch {out}6"),
        ("redirected", f"aws sts get-caller-identity > {out}7"),
        ("substituted", f"aws s3 ls $(touch {out}8)"),
        ("piped", f"git log -1 | tee {out}9"),
        ("unlisted", "env"),
    ]
    calls = [{"name": tool, "input": args} for _, tool, args in allowed]
    calls += [{"name": "Bash", "input": {"command": c}} for _, c in refused]
    calls.append({"name": "Read", "input": {"file_path": "/etc/passwd"}})

    with _Script(calls) as script:
        env = {
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "ANTHROPIC_BASE_URL": script.url,
            "ANTHROPIC_API_KEY": "sk-scripted",
            "CI": "1",
        }
        result = subprocess.run(  # noqa: S603 - the agent's own argv
            fa.claude_command(cli, "scripted session", "scripted", str(log_dir)),
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    assert script.bodies, (
        f"the CLI made no request to the scripted endpoint (exit "
        f"{result.returncode}): {result.stderr[-600:]}"
    )
    results = script.results()

    def text(index: int) -> str:
        return results.get(f"toolu_{index:04d}", "")

    assert "FAKEAWS" in text(0), f"the allowed aws call was refused: {text(0)!r}"
    assert re.search(r"[0-9a-f]{40}", text(1)), f"git control refused: {text(1)!r}"
    assert "LOGREAD" in text(2), f"the log directory was refused: {text(2)!r}"
    assert "CONTROLREAD" in text(3), f"the checkout was refused: {text(3)!r}"

    ran = [
        label
        for offset, (label, _) in enumerate(refused)
        if "denied" not in text(len(allowed) + offset).lower()
    ]
    assert not ran, f"admitted: {ran}"
    assert "root:" not in text(len(calls) - 1), "/etc/passwd was served"
    assert calls_file.read_text().splitlines() == ["sts get-caller-identity"]
    assert not [p.name for p in tmp_path.iterdir() if p.name.startswith("w")]
