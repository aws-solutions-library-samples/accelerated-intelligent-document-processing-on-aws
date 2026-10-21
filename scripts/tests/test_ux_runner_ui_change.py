# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/ui_change.py, the MR UX test's change classifier.

Two jobs depend on this answer agreeing with itself: ``ux_test_build`` skips the
build on it, and ``ai_ux_test`` recomputes it with the pinned harness. So what is
pinned here is the classification rule (globs anchored the way the docstring
says, renames judged on both sides, deletions visible), the parser for git's
NUL-separated output, and the CLI against a real throwaway repository, with the
merge base rather than the target tip as the left side.

Every repository is created under ``tmp_path`` with an empty hooks directory and
commit signing off, and git's global and system configuration are switched off
for the test, so a developer's or a managed machine's git setup cannot change the
result.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import ui_change  # noqa: E402
from ux_runner.ui_change import ChangedFile  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _init_repo(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    hooks = root / "empty-hooks"
    hooks.mkdir()
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "develop")
    _git(repo, "config", "core.hooksPath", str(hooks))
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.name", "UX Test")
    _git(repo, "config", "user.email", "ux-test@example.invalid")
    return repo


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _git(repo, "add", rel)
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _branching_repo(
    root: Path, monkeypatch: pytest.MonkeyPatch, feature: dict[str, str]
) -> tuple[Path, str, str]:
    """develop, a feature branch off it, and develop moving on afterwards."""
    repo = _init_repo(root, monkeypatch)
    base = _commit(
        repo,
        {"src/ui/src/App.tsx": "v1\n", "README.md": "readme\n"},
        "base",
    )
    _git(repo, "checkout", "--quiet", "-b", "feature")
    head = _commit(repo, feature, "feature")
    _git(repo, "checkout", "--quiet", "develop")
    _commit(repo, {"src/ui/src/Later.tsx": "landed on develop later\n"}, "later")
    _git(repo, "checkout", "--quiet", "feature")
    return repo, base, head


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/__tests__/**", "src/ui/src/a/__tests__/b.tsx", True),
        ("**/__tests__/**", "__tests__/b.tsx", True),
        ("**/__tests__/**", "src/ui/src/a/not__tests__/b.tsx", False),
        ("**/*.test.*", "x.test.tsx", True),
        ("**/*.test.*", "src/ui/src/deep/x.test.tsx", True),
        ("**/*.test.*", "xtest.tsx", False),
        ("nested/**", "nested/a/b", True),
        ("nested/**", "nestedx", False),
        ("nested/**", "nested", False),
        (
            "src/ui/src/graphql/operations/**",
            "src/ui/src/graphql/operations/q.graphql",
            True,
        ),
        ("template.yaml", "template.yaml", True),
        ("template.yaml", "nested/template.yaml", False),
        ("src/ui/src/*.tsx", "src/ui/src/deep/App.tsx", False),
        ("src/ui/src/?pp.tsx", "src/ui/src/App.tsx", True),
        ("src/ui/src/a.b", "src/ui/src/aXb", False),
    ],
)
def test_glob_to_regex_anchors_both_ends_and_keeps_star_in_one_segment(
    pattern: str, path: str, expected: bool
) -> None:
    assert bool(ui_change.glob_to_regex(pattern).match(path)) is expected


@pytest.mark.unit
def test_a_change_made_only_of_tests_is_skipped_with_the_tests_reason() -> None:
    change = ui_change.classify(
        [
            ChangedFile("M", "src/ui/src/components/x/__tests__/X.test.tsx"),
            ChangedFile("A", "src/ui/src/components/y/Y.spec.ts"),
        ]
    )
    assert change.skip is True
    assert change.reason == "only tests changed under src/ui"
    assert change.user_visible == ()
    assert len(change.test_only) == 2


@pytest.mark.unit
def test_a_mix_of_tests_and_components_is_not_skipped() -> None:
    change = ui_change.classify(
        [
            ChangedFile("M", "src/ui/src/components/x/X.tsx"),
            ChangedFile("M", "src/ui/src/components/x/__tests__/X.test.tsx"),
        ]
    )
    assert change.skip is False
    assert change.reason == ""
    assert change.user_visible == ("src/ui/src/components/x/X.tsx",)
    assert change.test_only == ("src/ui/src/components/x/__tests__/X.test.tsx",)


@pytest.mark.unit
def test_a_rename_from_a_test_path_to_a_component_counts_as_visible() -> None:
    change = ui_change.classify(
        [
            ChangedFile(
                "R100",
                "src/ui/src/components/x/X.tsx",
                old_path="src/ui/src/components/x/__tests__/X.tsx",
            )
        ]
    )
    assert change.user_visible == ("src/ui/src/components/x/X.tsx",)
    assert change.skip is False


@pytest.mark.unit
def test_a_component_moved_out_of_the_ui_tree_is_still_a_ui_change() -> None:
    change = ui_change.classify(
        [ChangedFile("R090", "lib/moved/X.tsx", old_path="src/ui/src/X.tsx")]
    )
    assert change.ui_files == ("src/ui/src/X.tsx",)
    assert change.user_visible == ("src/ui/src/X.tsx",)


@pytest.mark.unit
def test_a_deleted_component_counts_as_visible() -> None:
    change = ui_change.classify([ChangedFile("D", "src/ui/src/components/Gone.tsx")])
    assert change.user_visible == ("src/ui/src/components/Gone.tsx",)
    assert change.skip is False


@pytest.mark.unit
def test_contract_and_backend_files_are_read_over_every_changed_path() -> None:
    change = ui_change.classify(
        [
            ChangedFile("M", "nested/api-resolvers/src/schema.graphql"),
            ChangedFile("M", "src/ui/src/graphql/operations/getDocument.graphql"),
            ChangedFile("M", "template.yaml"),
            ChangedFile("M", "lib/idp_common_pkg/idp_common/models.py"),
            ChangedFile("M", "docs/testing.md"),
        ]
    )
    assert change.api_contract == (
        "nested/api-resolvers/src/schema.graphql",
        "src/ui/src/graphql/operations/getDocument.graphql",
    )
    assert change.backend == (
        "nested/api-resolvers/src/schema.graphql",
        "template.yaml",
        "lib/idp_common_pkg/idp_common/models.py",
    )
    assert change.user_visible == ("src/ui/src/graphql/operations/getDocument.graphql",)


@pytest.mark.unit
def test_no_ui_change_gives_the_no_change_reason() -> None:
    change = ui_change.classify([ChangedFile("M", "template.yaml")])
    assert change.skip is True
    assert change.reason == "no change under src/ui"
    assert change.ui_files == ()
    assert change.backend == ("template.yaml",)


@pytest.mark.unit
def test_to_json_has_the_documented_keys_and_values() -> None:
    change = ui_change.classify([ChangedFile("M", "src/ui/src/App.tsx")])
    record = change.to_json(
        head_sha="a" * 40, base_sha="b" * 40, target_branch="develop"
    )
    assert list(record) == [
        "version",
        "head_sha",
        "base_sha",
        "target_branch",
        "ui_files",
        "user_visible",
        "test_only",
        "api_contract",
        "backend",
        "skip",
        "reason",
    ]
    assert record["version"] == 1
    assert record["user_visible"] == ["src/ui/src/App.tsx"]
    assert record["skip"] is False
    assert json.loads(json.dumps(record)) == record


@pytest.mark.unit
def test_parse_name_status_reads_renames_and_paths_with_spaces() -> None:
    raw = (
        "M\0src/ui/src/a b.tsx\0"
        "R100\0src/ui/src/old name.tsx\0src/ui/src/new name.tsx\0"
        "C075\0src/ui/src/orig.tsx\0src/ui/src/copy.tsx\0"
        "D\0gone.py\0"
    )
    assert ui_change.parse_name_status(raw) == [
        ChangedFile("M", "src/ui/src/a b.tsx"),
        ChangedFile(
            "R100", "src/ui/src/new name.tsx", old_path="src/ui/src/old name.tsx"
        ),
        ChangedFile("C075", "src/ui/src/copy.tsx", old_path="src/ui/src/orig.tsx"),
        ChangedFile("D", "gone.py"),
    ]


@pytest.mark.unit
def test_parse_name_status_refuses_a_truncated_rename() -> None:
    with pytest.raises(ui_change.UiChangeError, match="truncated"):
        ui_change.parse_name_status("R100\0only-the-old-path\0")


@pytest.mark.unit
@pytest.mark.parametrize("name", ["develop", "release/0.6", "feature/x_y-1.2"])
def test_check_branch_accepts_ordinary_branch_names(name: str) -> None:
    assert ui_change.check_branch(name) == name


@pytest.mark.unit
@pytest.mark.parametrize(
    "name", ["", "a:refs/heads/main", "../x", "x..y", "has space", "-flag", "x.lock"]
)
def test_check_branch_refuses_names_that_would_change_the_refspec(name: str) -> None:
    with pytest.raises(ui_change.UiChangeError):
        ui_change.check_branch(name)


@pytest.mark.unit
def test_main_writes_the_record_against_the_merge_base_using_git_dash_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = _branching_repo(
        tmp_path,
        monkeypatch,
        {
            "src/ui/src/App.tsx": "v2\n",
            "src/ui/src/__tests__/App.test.tsx": "test\n",
            "template.yaml": "x: 1\n",
        },
    )
    calls: list[list[str]] = []
    real_popen = subprocess.Popen

    def recording_popen(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        calls.append(list(argv))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    out = tmp_path / "out" / "ui-change.json"
    code = ui_change.main(
        [
            "--out",
            str(out),
            "--repo-dir",
            str(repo),
            "--target-branch",
            "develop",
            "--no-fetch",
        ]
    )

    assert code == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert set(record) == {
        "version",
        "head_sha",
        "base_sha",
        "target_branch",
        "ui_files",
        "user_visible",
        "test_only",
        "api_contract",
        "backend",
        "skip",
        "reason",
    }
    assert record["head_sha"] == head
    assert record["base_sha"] == base
    assert record["target_branch"] == "develop"
    assert record["user_visible"] == ["src/ui/src/App.tsx"]
    assert record["test_only"] == ["src/ui/src/__tests__/App.test.tsx"]
    assert record["backend"] == ["template.yaml"]
    assert "src/ui/src/Later.tsx" not in record["ui_files"]
    assert record["skip"] is False
    assert calls, "main ran no git command"
    assert all(argv[:3] == ["git", "-C", str(repo)] for argv in calls), calls
    assert not any("fetch" in argv for argv in calls)
    assert "ui-change: 1 user-visible file(s), skip=false" in capsys.readouterr().out


@pytest.mark.unit
def test_main_reports_skip_for_a_test_only_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = _branching_repo(
        tmp_path, monkeypatch, {"src/ui/src/__tests__/App.test.tsx": "test\n"}
    )
    out = tmp_path / "ui-change.json"
    code = ui_change.main(["--out", str(out), "--repo-dir", str(repo), "--no-fetch"])
    record = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0
    assert record["skip"] is True
    assert record["reason"] == "only tests changed under src/ui"
    assert "skip=true" in capsys.readouterr().out


@pytest.mark.unit
def test_main_fetches_the_target_into_the_ux_test_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, _ = _branching_repo(tmp_path, monkeypatch, {"src/ui/src/App.tsx": "v2\n"})
    _git(repo, "remote", "add", "origin", str(repo))
    out = tmp_path / "ui-change.json"
    code = ui_change.main(["--out", str(out), "--repo-dir", str(repo)])
    develop = _git(repo, "rev-parse", "refs/heads/develop").strip()
    fetched = _git(repo, "rev-parse", "refs/ux-test/target-develop").strip()
    assert code == 0
    assert fetched == develop
    assert json.loads(out.read_text(encoding="utf-8"))["user_visible"] == [
        "src/ui/src/App.tsx"
    ]


@pytest.mark.unit
def test_main_fails_loudly_when_git_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = _branching_repo(tmp_path, monkeypatch, {"src/ui/src/App.tsx": "v2\n"})
    out = tmp_path / "ui-change.json"
    code = ui_change.main(
        [
            "--out",
            str(out),
            "--repo-dir",
            str(repo),
            "--target-branch",
            "no-such-branch",
            "--no-fetch",
        ]
    )
    assert code == 1
    assert capsys.readouterr().out.startswith("FAILED: ")
    assert not out.exists()


@pytest.mark.unit
def test_ui_change_imports_nothing_from_the_package_or_beyond_the_stdlib() -> None:
    """ux_test_build runs it by path on a bare python3, so it must stand alone."""
    import ast

    source = (SCRIPTS / "ux_runner" / "ui_change.py").read_text(encoding="utf-8")
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}, roots


def _fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str) -> None:
    """Put a ``git`` first on ``PATH`` that runs ``program`` with this Python."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    (bin_dir / "fake_git.py").write_text(program, encoding="utf-8")
    launcher = bin_dir / "git"
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_git.py"}" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


class _Cancelled(BaseException):
    """What a job cancel raises in the main thread, as ``processes.Terminated`` is."""


@pytest.mark.unit
@pytest.mark.parametrize(
    "program",
    [
        "import time\ntime.sleep(60)\n",
        "import os, time\nos.close(1)\nos.close(2)\ntime.sleep(60)\n",
    ],
    ids=["holding-its-pipes", "after-closing-its-pipes"],
)
def test_git_that_hangs_or_cannot_start_is_a_ui_change_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str
) -> None:
    _fake_git(tmp_path, monkeypatch, program)
    started = time.monotonic()
    with pytest.raises(ui_change.UiChangeError, match="did not finish within 1s"):
        ui_change.git(tmp_path, "status", timeout=1)
    assert time.monotonic() - started < 30
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    with pytest.raises(ui_change.UiChangeError, match="could not be started"):
        ui_change.git(tmp_path, "status")


@pytest.mark.unit
def test_git_is_killed_when_the_wait_for_it_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job cancel arrives as an exception in the main thread while it waits for
    git, and git goes with it rather than running on, as ``subprocess.run`` would
    have killed it."""
    pid_file = tmp_path / "git.pid"
    _fake_git(
        tmp_path,
        monkeypatch,
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
    )

    def cancel(signum: int, frame: object) -> None:
        raise _Cancelled

    previous = signal.signal(signal.SIGALRM, cancel)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(_Cancelled):
            ui_change.git(tmp_path, "fetch", timeout=30)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text(encoding="utf-8")), 0)


@pytest.mark.unit
def test_git_runs_without_the_jobs_review_token_personas_or_aws_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The precheck's git children are the orchestrator's only ones with an
    inherited environment, so they inherit everything but those three families;
    the job token stays for a runner's credential helper."""
    _fake_git(
        tmp_path,
        monkeypatch,
        "import json, os, sys\nsys.stdout.write(json.dumps(dict(os.environ)))\n",
    )
    for name, value in {
        "GITLAB_REVIEW_TOKEN": "review-value",
        "UX_TEST_PERSONAS": "personas-value",
        "UX_TEST_UI_ENV": "ui-env-value",
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-value",
        "AWS_SESSION_TOKEN": "aws-session-value",
        "CI_JOB_TOKEN": "job-value",
        "HOME": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)
    seen = json.loads(ui_change.git(tmp_path, "rev-parse", "HEAD"))
    assert not [n for n in seen if n.startswith(("AWS_", "GITLAB_", "UX_TEST_"))]
    assert seen["CI_JOB_TOKEN"] == "job-value"
    assert seen["HOME"] == str(tmp_path)
    assert seen["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.unit
def test_git_output_is_kept_only_up_to_a_bound_however_much_git_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An MR decides how much its diff prints, and the precheck that reads it holds
    the review token and the personas. ``git_prefix`` keeps the start and counts
    the rest as it arrives, so this process never holds the flood; ``git`` refuses
    a command that overruns its bound rather than answer from a cut."""
    flood = 32 * 1024 * 1024
    _fake_git(
        tmp_path,
        monkeypatch,
        "import os, sys\n"
        f"for _ in range({flood // 65536}):\n"
        "    os.write(1, b'x' * 65535 + b'\\n')\n"
        "    os.write(2, b'e' * 65536)\n",
    )
    tracemalloc.start()
    try:
        text, size = ui_change.git_prefix(tmp_path, "diff", keep=4096)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (len(text), size) == (4096, flood)
    assert set(text) == {"x"}
    assert peak < flood // 8, peak
    monkeypatch.setattr(ui_change, "MAX_OUTPUT_BYTES", 4096)
    with pytest.raises(ui_change.UiChangeError, match=f"printed {flood} bytes"):
        ui_change.git(tmp_path, "diff", "--name-status")
