"""Tests for the nested-checkout guard in scripts/srt/run.py.

`srt assess` walks whatever path it is given and has no `--exclude` option, so a
nested git checkout multiplies the scan: one checkov process per template per
copy, all concurrent. A tree holding 74 agent worktrees spawned 2,200 checkov
children and exhausted 123 GiB of RAM plus 8 GiB of swap before being killed
unfinished. These tests pin the guard that refuses such a scan, including the
orphaned-copy case (a directory under `.claude/worktrees/` that git no longer
lists still costs the scanner exactly as much as a registered one).
"""

import subprocess
import sys
from pathlib import Path

import pytest

SRT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRT_DIR))

from run import (  # noqa: E402
    abort_on_nested_checkouts,
    nested_checkouts,
)

pytestmark = pytest.mark.unit


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A committed git repo with no nested checkouts."""
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.invalid", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "a.txt").write_text("x\n")
    _git("add", ".", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


def test_clean_tree_has_no_nested_checkouts(repo):
    assert nested_checkouts(repo) == []


def test_clean_tree_does_not_abort(repo):
    abort_on_nested_checkouts(repo)  # must not raise SystemExit


def test_registered_worktree_is_detected(repo):
    _git("worktree", "add", "-q", ".claude/worktrees/agent-1", "-b", "agent1", cwd=repo)
    assert nested_checkouts(repo) == [".claude/worktrees/agent-1"]


def test_orphaned_copy_is_detected(repo):
    """A copy git no longer lists costs the scanner the same as a live one."""
    orphan = repo / ".claude" / "worktrees" / "orphan-1"
    orphan.mkdir(parents=True)
    (orphan / ".git").write_text("gitdir: /nonexistent\n")
    assert nested_checkouts(repo) == [".claude/worktrees/orphan-1"]


def test_directory_without_git_is_not_a_checkout(repo):
    """Only actual checkouts count -- a plain directory is not one."""
    plain = repo / ".claude" / "worktrees" / "notes"
    plain.mkdir(parents=True)
    (plain / "scratch.md").write_text("hi\n")
    assert nested_checkouts(repo) == []


def test_nested_checkout_aborts(repo):
    _git("worktree", "add", "-q", ".claude/worktrees/agent-1", "-b", "agent1", cwd=repo)
    with pytest.raises(SystemExit) as excinfo:
        abort_on_nested_checkouts(repo)
    assert excinfo.value.code == 1


def test_override_env_var_allows_the_scan(repo, monkeypatch):
    _git("worktree", "add", "-q", ".claude/worktrees/agent-1", "-b", "agent1", cwd=repo)
    monkeypatch.setenv("SRT_ALLOW_NESTED_CHECKOUTS", "1")
    abort_on_nested_checkouts(repo)  # must not raise SystemExit


def test_the_root_itself_is_never_reported(repo):
    """The tree being scanned is a checkout; only *nested* ones are a problem."""
    assert repo.as_posix() not in nested_checkouts(repo)
    assert "." not in nested_checkouts(repo)
