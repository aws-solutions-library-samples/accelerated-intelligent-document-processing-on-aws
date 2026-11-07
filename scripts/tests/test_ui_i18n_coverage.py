# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pin what `scripts/ui_i18n_coverage.py` does, and above all what it never does: fail.

The script behind `make ui-i18n-coverage` runs the Node worker
(`src/ui/scripts/i18n/coverage.mjs`) over the UI's catalog directory and prints its
report. It is advisory by decision D17 of #1770, so the property these tests guard
hardest is that findings — missing, stale, invalid or dead translations — exit 0,
and only a worker that could not produce a report at all exits 2. The worker
invocation is replaced by a fake that records what it was asked to run, so nothing
here needs `node` and nothing can touch the real `src/ui`; one test at the end runs
the real script against the real tree when the UI's dependencies are installed,
and in CI they always are, so there it never skips.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Whether this is a CI run, which decides whether an absent Node toolchain is a
#: skip or a failure in the real-tree test. Same rule as ``IN_CI`` in
#: ``test_ui_test_discovery.py``.
IN_CI = bool(os.getenv("CI") or os.getenv("GITLAB_CI") or os.getenv("GITHUB_ACTIONS"))

#: What the worker prints for a catalog with one of everything, as its text report.
FINDINGS_TEXT = (
    "4 source keys in en.json\n"
    "\n"
    "fr-CA.json: 1 of 4 translated (25%)\n"
    "  missing (1): no entry, so the English is shown\n"
    "    nav.upload\n"
    "  stale (1): the English changed since the translation was made (sourceHash differs)\n"
    "    nav.count\n"
    "  invalid (1): the build rejects these, with the wording below\n"
    "    nav.greeting\n"
    "      fr-CA.json: nav.greeting: the translation is missing argument(s) {name}\n"
    "  dead (1): not in en.json\n"
    "    nav.gone\n"
)

#: The same report as the worker's ``--json`` output.
FINDINGS_JSON = (
    json.dumps(
        {
            "sourceKeys": 4,
            "locales": {
                "fr-CA": {
                    "translated": 1,
                    "missing": ["nav.upload"],
                    "stale": ["nav.count"],
                    "invalid": ["nav.greeting"],
                    "problems": [
                        "fr-CA.json: nav.greeting: the translation is missing "
                        "argument(s) {name}"
                    ],
                    "dead": ["nav.gone"],
                    "coveragePercent": 25,
                }
            },
        },
        indent=2,
    )
    + "\n"
)


def _load_module():
    """Load `scripts/ui_i18n_coverage.py` by path (scripts/ is not a package).

    Registered in ``sys.modules`` before it executes: its dataclass resolves its
    postponed annotations through ``sys.modules[__module__]``.
    """
    path = REPO_ROOT / "scripts" / "ui_i18n_coverage.py"
    spec = importlib.util.spec_from_file_location("_ui_i18n_coverage", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def _tree(tmp_path: Path, *, toolchain: bool = True):
    """A synthetic checkout: the catalog directory, the worker and, optionally, the parser package."""
    layout = MODULE.Layout(tmp_path)
    layout.catalog_dir.mkdir(parents=True)
    (layout.catalog_dir / "en.json").write_text("{}\n", encoding="utf-8")
    layout.worker.parent.mkdir(parents=True)
    layout.worker.write_text("// stand-in\n", encoding="utf-8")
    if toolchain:
        layout.parser_package.mkdir(parents=True)
        (layout.parser_package / "package.json").write_text("{}\n", encoding="utf-8")
    return layout


def _fake_run(
    monkeypatch,
    *,
    stdout: str = FINDINGS_TEXT,
    stderr: str = "",
    returncode: int = 0,
    error: OSError | None = None,
) -> list[list[str]]:
    """Replace the module's subprocess runner; returns the list of commands it was given."""
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(list(command))
        if error is not None:
            raise error
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(MODULE, "run", run)
    return calls


def _without_ci(monkeypatch) -> None:
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(name, raising=False)


def _point_at(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    _without_ci(monkeypatch)


# --------------------------------------------------------------------------- #
# What is run: node by name, the worker and the catalog directory by path, flags as given
# --------------------------------------------------------------------------- #


def test_the_worker_runs_by_path_over_the_catalog_directory(tmp_path, monkeypatch):
    layout = _tree(tmp_path)
    _point_at(monkeypatch, tmp_path)
    calls = _fake_run(monkeypatch)
    assert MODULE.main([]) == 0
    assert calls == [["node", str(layout.worker), str(layout.catalog_dir)]]
    assert layout.worker == tmp_path / "src/ui/scripts/i18n/coverage.mjs"
    assert layout.catalog_dir == tmp_path / "src/ui/src/i18n"


@pytest.mark.parametrize(
    ("argv", "forwarded"),
    [
        (["--json"], ["--json"]),
        (["--locale", "fr-CA"], ["--locale", "fr-CA"]),
        (["--json", "--locale", "fr-CA"], ["--json", "--locale", "fr-CA"]),
    ],
)
def test_the_flags_are_forwarded_as_given(tmp_path, monkeypatch, argv, forwarded):
    layout = _tree(tmp_path)
    _point_at(monkeypatch, tmp_path)
    calls = _fake_run(monkeypatch, stdout=FINDINGS_JSON)
    assert MODULE.main(argv) == 0
    (command,) = calls
    assert command[:3] == ["node", str(layout.worker), str(layout.catalog_dir)]
    assert command[3:] == forwarded


def test_the_installed_node_runs_the_worker_and_nothing_else_is_invoked(
    tmp_path, monkeypatch
):
    """`node` by name, the worker and the catalog by path: no `npx`, no `npm`, no network."""
    layout = _tree(tmp_path)
    _point_at(monkeypatch, tmp_path)
    calls = _fake_run(monkeypatch)
    MODULE.main([])
    (command,) = calls
    assert command[0] == "node"
    assert Path(command[1]) == layout.worker
    assert Path(command[2]) == layout.catalog_dir
    assert not any(word in ("npx", "npm") for word in command)


# --------------------------------------------------------------------------- #
# The property that matters: findings exit 0; only a tool failure exits 2
# --------------------------------------------------------------------------- #


def test_findings_are_printed_as_they_are_and_exit_zero(tmp_path, monkeypatch, capsys):
    """Missing, stale, invalid and dead entries are the report, not a failure (D17)."""
    _tree(tmp_path)
    _point_at(monkeypatch, tmp_path)
    _fake_run(monkeypatch, stdout=FINDINGS_TEXT)
    assert MODULE.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == FINDINGS_TEXT
    assert captured.err == ""
    assert "❌" not in captured.out


def test_json_output_is_the_workers_json_and_nothing_else(
    tmp_path, monkeypatch, capsys
):
    """`make ui-i18n-coverage UI_I18N_COVERAGE_ARGS=--json` has to be parseable as it stands."""
    _tree(tmp_path)
    _point_at(monkeypatch, tmp_path)
    _fake_run(monkeypatch, stdout=FINDINGS_JSON)
    assert MODULE.main(["--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out == FINDINGS_JSON
    assert json.loads(captured.out)["locales"]["fr-CA"]["coveragePercent"] == 25
    assert captured.err == ""


def test_a_worker_that_cannot_report_exits_two_with_its_message(
    tmp_path, monkeypatch, capsys
):
    _tree(tmp_path)
    _point_at(monkeypatch, tmp_path)
    cause = "/x/src/ui/src/i18n/fr-CA.json: no such catalog\n"
    _fake_run(monkeypatch, stdout="", stderr=cause, returncode=2)
    assert MODULE.main(["--locale", "fr-CA"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(cause)
    assert (
        "❌ ui-i18n-coverage: no report could be produced (node exited 2)"
        in captured.err
    )
    assert "D17" in captured.err


def test_a_node_that_cannot_start_exits_two(tmp_path, monkeypatch, capsys):
    _tree(tmp_path)
    _point_at(monkeypatch, tmp_path)
    _fake_run(
        monkeypatch, error=FileNotFoundError(2, "No such file or directory", "node")
    )
    assert MODULE.main([]) == 2
    captured = capsys.readouterr()
    assert "❌ ui-i18n-coverage: node could not be started" in captured.err
    assert captured.out == ""


# --------------------------------------------------------------------------- #
# The toolchain guard: skip off CI, fail in CI, never a report
# --------------------------------------------------------------------------- #


def test_an_absent_toolchain_skips_off_ci_and_says_so(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, toolchain=False)
    _point_at(monkeypatch, tmp_path)
    calls = _fake_run(monkeypatch)
    assert MODULE.main([]) == 0
    captured = capsys.readouterr()
    assert (
        "⏭️  ui-i18n-coverage SKIPPED (not a pass: run make ui-lint first)"
        in captured.err
    )
    assert (
        "src/ui/node_modules/@formatjs/icu-messageformat-parser is missing"
        in captured.err
    )
    assert captured.out == "", (
        "a skip must write nothing to stdout: a caller piping the report (or --json) "
        f"must not receive a notice in its place:\n{captured.out}"
    )
    assert calls == []


@pytest.mark.parametrize("variable", ["CI", "GITHUB_ACTIONS", "GITLAB_CI"])
def test_an_absent_toolchain_fails_in_ci(tmp_path, monkeypatch, capsys, variable):
    _tree(tmp_path, toolchain=False)
    _point_at(monkeypatch, tmp_path)
    monkeypatch.setenv(variable, "true")
    calls = _fake_run(monkeypatch)
    assert MODULE.main([]) == 2
    out = capsys.readouterr().out
    assert "❌ ui-i18n-coverage" in out
    assert "src/ui/node_modules/@formatjs/icu-messageformat-parser is missing" in out
    assert "SKIPPED" not in out
    assert calls == []


# --------------------------------------------------------------------------- #
# The real tree
# --------------------------------------------------------------------------- #


def test_the_real_repository_reports_and_exits_zero(capsys):
    """What `make ui-i18n-coverage` runs, on the committed tree, in both output forms.

    Needs the UI's npm dependencies. Off CI an unbuilt UI is ordinary and the test
    skips; in CI every job running this suite installs them, so their absence is a
    broken job and fails (see `_require_node` in test_ui_test_discovery.py).
    """
    layout = MODULE.Layout(REPO_ROOT)
    if not layout.parser_package.is_dir():
        reason = (
            f"{layout.relative(layout.parser_package)} is not installed, so the "
            "worker cannot run (run `npm ci` in src/ui)."
        )
        if IN_CI:
            pytest.fail(
                f"{reason} In CI this is a job-configuration failure, not a reason "
                "to skip."
            )
        pytest.skip(reason)

    assert MODULE.main([]) == 0
    text = capsys.readouterr().out
    assert "source key" in text
    assert "en.json" in text

    assert MODULE.main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"sourceKeys", "locales"}
    assert report["sourceKeys"] == len(
        json.loads((layout.catalog_dir / "en.json").read_text(encoding="utf-8"))
    )
    for coverage in report["locales"].values():
        assert set(coverage) == {
            "translated",
            "missing",
            "stale",
            "invalid",
            "problems",
            "dead",
            "coveragePercent",
        }
