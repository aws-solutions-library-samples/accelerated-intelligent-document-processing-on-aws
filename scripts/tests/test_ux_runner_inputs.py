# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/mr_inputs.py, the MR record the UX session runs on.

What matters is that the record describes the head that was fetched, measured
against its merge base, with a diff that holds ``src/ui`` and nothing else while
the changed-file list still holds everything (the caveats need it), and that a
record arriving from another process is validated rather than trusted. The
collect tests run real git in throwaway repositories under ``tmp_path``, with an
empty hooks directory, commit signing off, and git's global and system
configuration switched off.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import mr_inputs, ui_change  # noqa: E402

HEAD_SHA = "0123456789abcdef" * 2 + "01234567"
BASE_SHA = "fedcba9876543210" * 2 + "fedcba98"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _git(repo, "add", rel)
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _mr_repo(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, iid: int = 812
) -> tuple[Path, str, str]:
    """A repo whose MR head and target are reachable only through refs/ux-test."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    hooks = root / "empty-hooks"
    hooks.mkdir()
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "develop")
    _git(repo, "config", "core.hooksPath", str(hooks))
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.name", "Jane Doe")
    _git(repo, "config", "user.email", "jane@example.invalid")
    base = _commit(
        repo, {"src/ui/src/App.tsx": "v1\n", "lib/tool.py": "one = 1\n"}, "base"
    )
    _git(repo, "checkout", "--quiet", "-b", "feature")
    head = _commit(
        repo,
        {"src/ui/src/App.tsx": "v2\n", "lib/tool.py": "one = 2\n"},
        "Add page rotation",
    )
    _git(repo, "checkout", "--quiet", "develop")
    _commit(repo, {"src/ui/src/Later.tsx": "landed later\n"}, "later on develop")
    _git(repo, "update-ref", f"refs/merge-requests/{iid}/head", head)
    return repo, base, head


def _inputs(**overrides: Any) -> mr_inputs.MrInputs:
    values: dict[str, Any] = {
        "iid": 812,
        "title": "Add page rotation",
        "author": "jdoe",
        "web_url": "https://gitlab.example/x/-/merge_requests/812",
        "source_branch": "feature/rotate",
        "target_branch": "develop",
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "changed": (
            ui_change.ChangedFile("M", "src/ui/src/App.tsx"),
            ui_change.ChangedFile(
                "R100", "src/ui/src/B.tsx", old_path="src/ui/src/A.tsx"
            ),
        ),
        "ui_diff": "diff --git a/src/ui/src/App.tsx b/src/ui/src/App.tsx\n",
        "ui_diff_truncated": False,
        "ui_diff_bytes": 52,
        "title_from_git": False,
    }
    values.update(overrides)
    return mr_inputs.MrInputs(**values)


@pytest.mark.unit
def test_collect_reads_the_head_merge_base_and_a_ui_only_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = _mr_repo(tmp_path, monkeypatch)
    _git(repo, "update-ref", "refs/ux-test/812", head)
    _git(repo, "update-ref", "refs/ux-test/target-develop", "refs/heads/develop")
    calls: list[list[str]] = []
    real_popen = subprocess.Popen

    def recording_popen(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        calls.append(list(argv))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    inputs = mr_inputs.collect(repo, 812, "develop", fetch=False)

    assert inputs.head_sha == head
    assert inputs.base_sha == base
    assert [c.path for c in inputs.changed] == ["lib/tool.py", "src/ui/src/App.tsx"]
    assert "src/ui/src/App.tsx" in inputs.ui_diff
    assert "+v2" in inputs.ui_diff
    assert "lib/tool.py" not in inputs.ui_diff
    assert "Later.tsx" not in inputs.ui_diff
    assert inputs.title == "Add page rotation"
    assert inputs.author == "Jane Doe"
    assert inputs.title_from_git is True
    assert inputs.ui_diff_truncated is False
    assert inputs.ui_diff_bytes == len(inputs.ui_diff.encode())
    assert calls, "collect ran no git command"
    assert all(argv[:3] == ["git", "-C", str(repo)] for argv in calls), calls
    assert not any("fetch" in argv for argv in calls)


@pytest.mark.unit
def test_collect_fetches_into_its_own_ref_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = _mr_repo(tmp_path, monkeypatch)
    _git(repo, "remote", "add", "origin", str(repo))
    seen: list[tuple[str, ...]] = []

    def recording_git(repo_dir: Path, *args: str, timeout: float = 120.0) -> str:
        seen.append(args)
        return ui_change.git(repo_dir, *args, timeout=timeout)

    inputs = mr_inputs.collect(
        repo, 812, "develop", title="From the API", author="jdoe", git=recording_git
    )
    fetched = [args for args in seen if args[0] == "fetch"]
    assert fetched == [
        (
            "fetch",
            "--quiet",
            "origin",
            "+refs/merge-requests/812/head:refs/ux-test/812",
        ),
        (
            "fetch",
            "--quiet",
            "origin",
            "+refs/heads/develop:refs/ux-test/target-develop",
        ),
    ]
    assert not any("refs/ai-review" in " ".join(args) for args in seen)
    assert _git(repo, "rev-parse", "refs/ux-test/812").strip() == head
    assert inputs.head_sha == head
    assert inputs.base_sha == base
    assert inputs.title == "From the API"
    assert inputs.author == "jdoe"
    assert inputs.title_from_git is False
    assert not any(args[0] == "log" for args in seen)


@pytest.mark.unit
def test_collect_refuses_a_head_ref_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, _ = _mr_repo(tmp_path, monkeypatch)
    with pytest.raises(mr_inputs.InputsError, match="refs/ux-test/77"):
        mr_inputs.collect(repo, 77, "develop", fetch=False)


@pytest.mark.unit
def test_collect_refuses_a_bad_iid_or_branch_before_running_git(tmp_path: Path) -> None:
    def no_git(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("git must not run for an invalid request")

    with pytest.raises(mr_inputs.InputsError, match="iid"):
        mr_inputs.collect(tmp_path, 0, "develop", git=no_git)
    with pytest.raises(mr_inputs.InputsError, match="branch"):
        mr_inputs.collect(tmp_path, 812, "a:refs/heads/main", git=no_git)


@pytest.mark.unit
def test_cut_diff_cuts_on_a_line_boundary_and_says_so() -> None:
    diff = "line one\nline two\nline three\n"
    assert mr_inputs.cut_diff(diff, len(diff.encode())) == (diff, False)
    cut, truncated = mr_inputs.cut_diff(diff, 13)
    assert truncated is True
    assert cut == "line one"
    multibyte = "é" * 20 + "\n" + "ü" * 20 + "\n"
    cut, truncated = mr_inputs.cut_diff(multibyte, 50)
    assert truncated is True
    assert cut == "é" * 20
    assert len(cut.encode()) <= 50
    start = "line one\nline two\nline th"
    assert mr_inputs.cut_diff(start, 1000, partial=True) == ("line one\nline two", True)


@pytest.mark.unit
def test_collect_reads_the_diff_only_up_to_its_budget_and_counts_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diff is the MR author's and so is its size, so the precheck keeps only
    the budget's worth: the record says how big the whole was, and the part it
    carries ends on a line boundary."""
    repo, base, head = _mr_repo(tmp_path, monkeypatch)
    _git(repo, "update-ref", "refs/ux-test/812", head)
    _git(repo, "update-ref", "refs/ux-test/target-develop", "refs/heads/develop")
    whole = _git(
        repo,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames",
        f"{base}..{head}",
        "--",
        "src/ui",
    )
    budget = len(whole.encode()) - 4
    kept: list[int] = []

    def recording_prefix(
        repo_dir: Path, *args: str, keep: int, timeout: float = 120.0
    ) -> tuple[str, int]:
        kept.append(keep)
        return ui_change.git_prefix(repo_dir, *args, keep=keep, timeout=timeout)

    inputs = mr_inputs.collect(
        repo,
        812,
        "develop",
        fetch=False,
        max_diff_bytes=budget,
        git_prefix=recording_prefix,
    )
    assert kept == [budget]
    assert inputs.ui_diff_truncated is True
    assert inputs.ui_diff_bytes == len(whole.encode())
    assert len(inputs.ui_diff.encode()) <= budget
    assert whole.startswith(inputs.ui_diff + "\n")


@pytest.mark.unit
def test_a_diff_cut_short_by_the_read_is_cut_on_a_line_boundary_however_short(
    tmp_path: Path,
) -> None:
    """The start of a longer diff can decode to fewer bytes than were read, so the
    record trusts the count of what git printed rather than the length of the
    text it kept."""

    def git(repo_dir: Path, *args: str, timeout: float = 120.0) -> str:
        return {
            "rev-parse": HEAD_SHA + "\n",
            "merge-base": BASE_SHA + "\n",
            "diff": "M\0src/ui/src/App.tsx\0",
            "log": "Add page rotation\0Jane Doe\n",
        }[args[0]]

    def git_prefix(
        repo_dir: Path, *args: str, keep: int, timeout: float = 120.0
    ) -> tuple[str, int]:
        return "diff --git a b\n+one\n+tw", 5_000_000

    inputs = mr_inputs.collect(
        tmp_path,
        812,
        "develop",
        fetch=False,
        max_diff_bytes=1000,
        git=git,
        git_prefix=git_prefix,
    )
    assert inputs.ui_diff == "diff --git a b\n+one"
    assert inputs.ui_diff_truncated is True
    assert inputs.ui_diff_bytes == 5_000_000


@pytest.mark.unit
def test_the_record_round_trips_through_json_and_disk(tmp_path: Path) -> None:
    inputs = _inputs()
    record = inputs.to_json()
    assert record["version"] == 1
    assert record["changed"][1] == {
        "status": "R100",
        "path": "src/ui/src/B.tsx",
        "old_path": "src/ui/src/A.tsx",
    }
    assert mr_inputs.MrInputs.from_json(json.loads(json.dumps(record))) == inputs
    path = mr_inputs.write(inputs, tmp_path / "ux-tests" / "inputs.json")
    assert mr_inputs.read(path) == inputs


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("head_sha", "0123456"),
        ("base_sha", "G" * 40),
        ("iid", "812"),
        ("iid", True),
        ("iid", 0),
        ("changed", [{"status": "M", "path": "x"}]),
        ("changed", "src/ui/src/App.tsx"),
        ("ui_diff_truncated", "no"),
        ("ui_diff_bytes", -1),
        ("version", 2),
    ],
)
def test_from_json_refuses_a_record_of_the_wrong_shape(key: str, value: object) -> None:
    record = _inputs().to_json()
    record[key] = value
    with pytest.raises(mr_inputs.InputsError):
        mr_inputs.MrInputs.from_json(record)


@pytest.mark.unit
def test_from_json_bounds_the_title() -> None:
    record = _inputs().to_json()
    record["title"] = "x" * 1000
    assert mr_inputs.MrInputs.from_json(record).title == "x" * mr_inputs.TITLE_CHARS


@pytest.mark.unit
def test_read_reports_unparseable_json_as_an_inputs_error(tmp_path: Path) -> None:
    path = tmp_path / "inputs.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(mr_inputs.InputsError, match="cannot read"):
        mr_inputs.read(path)
