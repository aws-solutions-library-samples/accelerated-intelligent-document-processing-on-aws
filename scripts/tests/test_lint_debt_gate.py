# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Prove `scripts/check_lint_debt.py` fails for each thing it claims to catch.

A ratchet nobody has watched fail is not a ratchet. `ruff.toml` excludes 85 files
from `ruff check` and 186 from `ruff format --check`, so for those files the two
lint gates are silent by construction, and everything protecting them lives in
that script. Each test below drives one failure mode and asserts the message
names it — including the two directions that a plain "does it still pass?" check
cannot distinguish: a listed file that *gained* a finding, and a listed file that
is now clean and must be delisted.

The failure modes are exercised against a synthetic tree rather than by editing
the real one, so a test cannot leave the repository dirty. One test at the end
runs the gate against the real tree, which is the same thing
`make check-lint-debt` does; it is here because `scripts/tests` runs in both CIs
while `make lint` runs in neither.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    """Load `scripts/check_lint_debt.py` by path (scripts/ is not a package)."""
    path = REPO_ROOT / "scripts" / "check_lint_debt.py"
    spec = importlib.util.spec_from_file_location("_check_lint_debt", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()

RUFF_TOML_TEMPLATE = """\
extend-exclude = [
# >>> GENERATED scope — edit scripts/lint_debt.json, then --write
{scope}\
# <<< GENERATED scope
]

[lint]
exclude = [
# >>> GENERATED lint-debt — edit scripts/lint_debt.json, then --write
{lint}\
# <<< GENERATED lint-debt
]

[format]
exclude = [
# >>> GENERATED format-debt — edit scripts/lint_debt.json, then --write
{fmt}\
# <<< GENERATED format-debt
]
"""


def _entries(paths) -> str:
    return "".join(f'    "{p}",\n' for p in sorted(paths))


def _harness(
    tmp_path,
    monkeypatch,
    *,
    tracked=("pkg/a.py",),
    findings=None,
    unformatted=(),
    scope=None,
    lint_debt=None,
    format_debt=(),
    toml_scope=None,
    toml_lint=None,
    toml_format=None,
    walk=(),
    ignored=(),
    extra_toml="",
):
    """Run `check()` against a synthetic tree and return the failure list."""
    from collections import Counter

    scope = {} if scope is None else scope
    lint_debt = {} if lint_debt is None else lint_debt
    findings = {} if findings is None else findings

    ruff_toml = tmp_path / "ruff.toml"
    ruff_toml.write_text(
        RUFF_TOML_TEMPLATE.format(
            scope=_entries(scope if toml_scope is None else toml_scope),
            lint=_entries(lint_debt if toml_lint is None else toml_lint),
            fmt=_entries(format_debt if toml_format is None else toml_format),
        )
        + extra_toml
    )
    baseline = tmp_path / "lint_debt.json"
    baseline.write_text(
        json.dumps(
            {"scope": scope, "lintDebt": lint_debt, "formatDebt": sorted(format_debt)}
        )
    )

    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "RUFF_TOML", ruff_toml)
    monkeypatch.setattr(MODULE, "BASELINE", baseline)
    monkeypatch.setattr(MODULE, "tracked_files", lambda: sorted(tracked))
    monkeypatch.setattr(
        MODULE,
        "measure",
        lambda: (
            {p: Counter(c) for p, c in findings.items()},
            set(unformatted),
        ),
    )
    monkeypatch.setattr(MODULE, "ruff_walk", lambda: list(walk))
    monkeypatch.setattr(MODULE, "git_ignored", lambda paths: set(ignored) & set(paths))

    report = MODULE.Report()
    MODULE.check(report)
    return report.failures


def _only(failures) -> str:
    assert len(failures) == 1, f"expected exactly one failure, got {failures}"
    return failures[0]


# --------------------------------------------------------------------------- #
# The clean case, so every test below is measuring the mutation and not noise
# --------------------------------------------------------------------------- #
def test_a_consistent_baseline_passes(tmp_path, monkeypatch):
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("pkg/a.py", "pkg/b.py"),
        findings={"pkg/a.py": {"I001": 1}},
        unformatted=("pkg/b.py",),
        lint_debt={"pkg/a.py": {"I001": 1}},
        format_debt=("pkg/b.py",),
    )
    assert failures == []


# --------------------------------------------------------------------------- #
# The ratchet: an excluded file may not accumulate
# --------------------------------------------------------------------------- #
def test_an_excluded_file_that_gains_a_finding_fails(tmp_path, monkeypatch):
    """The point of recording counts rather than just names."""
    message = _only(
        _harness(
            tmp_path,
            monkeypatch,
            tracked=("pkg/a.py",),
            findings={"pkg/a.py": {"I001": 1, "F401": 2}},
            lint_debt={"pkg/a.py": {"I001": 1}},
        )
    )
    assert "GAINED" in message
    assert "F401: was 0, now 2" in message


def test_a_higher_count_of_an_already_recorded_rule_fails(tmp_path, monkeypatch):
    """Not just a new rule: three F401s where two were audited is also new debt."""
    message = _only(
        _harness(
            tmp_path,
            monkeypatch,
            tracked=("pkg/a.py",),
            findings={"pkg/a.py": {"F401": 3}},
            lint_debt={"pkg/a.py": {"F401": 2}},
        )
    )
    assert "F401: was 2, now 3" in message


def test_an_excluded_file_that_became_clean_must_be_delisted(tmp_path, monkeypatch):
    """The ratchet turns one way: a paid-off file goes back under the gate."""
    message = _only(
        _harness(
            tmp_path,
            monkeypatch,
            tracked=("pkg/a.py",),
            findings={},
            lint_debt={"pkg/a.py": {"I001": 1}},
        )
    )
    assert "Delist it" in message


def test_a_formatted_file_must_be_delisted_from_format_debt(tmp_path, monkeypatch):
    message = _only(
        _harness(
            tmp_path,
            monkeypatch,
            tracked=("pkg/a.py",),
            unformatted=(),
            format_debt=("pkg/a.py",),
        )
    )
    assert "Delist it" in message


def test_a_listed_path_that_no_longer_exists_fails(tmp_path, monkeypatch):
    """A dead exclusion is how the `options` and `lib/get_config_pkg` entries
    survived in ruff.toml after the directories they named were deleted."""
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("pkg/b.py",),
        lint_debt={"pkg/gone.py": {"I001": 1}},
    )
    assert any("git does not track" in f for f in failures)


# --------------------------------------------------------------------------- #
# Issue #975 itself: a bare directory name matches at any depth
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("block", ["scope", "lint", "format"])
def test_a_bare_directory_name_fails_in_every_block(tmp_path, monkeypatch, block):
    kwargs = {"toml_scope": [], "toml_lint": [], "toml_format": []}
    kwargs[f"toml_{block}"] = ["src"]
    failures = _harness(tmp_path, monkeypatch, tracked=("src/a.py",), **kwargs)
    assert any("ANY path depth" in f for f in failures), failures


def test_a_root_anchored_path_is_accepted(tmp_path, monkeypatch):
    """The fix for the above is anchoring, so anchoring must not itself fail."""
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("src/a.py",),
        findings={"src/a.py": {"I001": 1}},
        lint_debt={"src/a.py": {"I001": 1}},
    )
    assert failures == []


# --------------------------------------------------------------------------- #
# ruff.toml and the baseline are two files and must not drift
# --------------------------------------------------------------------------- #
def test_ruff_toml_listing_a_file_the_baseline_does_not_fails(tmp_path, monkeypatch):
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("pkg/a.py", "pkg/b.py"),
        findings={"pkg/a.py": {"I001": 1}},
        lint_debt={"pkg/a.py": {"I001": 1}},
        toml_lint=["pkg/a.py", "pkg/b.py"],
    )
    assert any("disagree" in f and "pkg/b.py" in f for f in failures), failures


def test_the_baseline_listing_a_file_ruff_toml_does_not_fails(tmp_path, monkeypatch):
    """The dangerous direction: ruff would report the file and CI would go red."""
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("pkg/a.py",),
        findings={"pkg/a.py": {"I001": 1}},
        lint_debt={"pkg/a.py": {"I001": 1}},
        toml_lint=[],
    )
    assert any("disagree" in f for f in failures), failures


# --------------------------------------------------------------------------- #
# A finding that is neither fixed nor recorded
# --------------------------------------------------------------------------- #
def test_an_unlisted_file_with_findings_fails(tmp_path, monkeypatch):
    message = _only(
        _harness(
            tmp_path,
            monkeypatch,
            tracked=("pkg/a.py",),
            findings={"pkg/a.py": {"F821": 1}},
        )
    )
    assert "do not add the file" in message


def test_an_unlisted_unformatted_file_fails(tmp_path, monkeypatch):
    message = _only(
        _harness(
            tmp_path, monkeypatch, tracked=("pkg/a.py",), unformatted=("pkg/a.py",)
        )
    )
    assert "make format" in message


def test_a_scope_exclusion_covers_its_own_findings(tmp_path, monkeypatch):
    """A scope entry accounts for the findings under it, so they are not reported
    as unlisted. Without this the two mechanisms would contradict each other."""
    (tmp_path / "vendor" / "pii").mkdir(parents=True)
    (tmp_path / "vendor" / "pii" / "PROVENANCE.md").write_text("upstream abc123\n")
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("vendor/pii/PROVENANCE.md", "vendor/pii/x.py"),
        findings={"vendor/pii/x.py": {"E402": 8}},
        unformatted=("vendor/pii/x.py",),
        scope={
            "vendor/pii": {
                "premise": "vendored_with_provenance",
                "reason": "third-party source",
            }
        },
    )
    assert failures == []


# --------------------------------------------------------------------------- #
# Premises. An exemption's stated reason has to be true of what it covers.
# --------------------------------------------------------------------------- #
def test_a_vendored_exclusion_without_provenance_fails(tmp_path, monkeypatch):
    """ "Kept byte-for-byte to ease re-sync" is only a reason while something
    records what it is synced against."""
    (tmp_path / "vendor" / "pii").mkdir(parents=True)
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("vendor/pii/x.py",),
        findings={"vendor/pii/x.py": {"E402": 1}},
        scope={
            "vendor/pii": {
                "premise": "vendored_with_provenance",
                "reason": "third-party source, kept byte-for-byte",
            }
        },
    )
    assert any("premise" in f and "PROVENANCE.md" in f for f in failures), failures


def test_a_notebook_exclusion_that_also_catches_python_fails(tmp_path, monkeypatch):
    """The defect this repository keeps producing: one reason attached to a set,
    false for a member of it. "Notebook cells share a namespace" says nothing
    about a .py file, so the pattern must not reach one."""
    (tmp_path / "notebooks").mkdir()
    (tmp_path / "notebooks" / "_validate_notebooks.py").write_text("")
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("examples/nb/a.ipynb", "examples/nb/helper.py"),
        scope={
            "examples/nb": {
                "premise": "only_notebooks_and_they_have_their_own_gate",
                "reason": "notebook cells share a namespace",
            }
        },
    )
    assert any("non-notebook files" in f for f in failures), failures


def test_the_notebook_exclusion_needs_the_notebook_harness_to_exist(
    tmp_path, monkeypatch
):
    """The reason is "checked elsewhere", so elsewhere has to still be there."""
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("notebooks/a.ipynb",),
        scope={
            "**/*.ipynb": {
                "premise": "only_notebooks_and_they_have_their_own_gate",
                "reason": "notebook cells share a namespace",
            }
        },
    )
    assert any("_validate_notebooks.py is gone" in f for f in failures), failures


def test_an_unknown_premise_name_fails(tmp_path, monkeypatch):
    """A premise nobody implemented is prose, and prose is what this replaces."""
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("vendor/pii/x.py",),
        scope={"vendor/pii": {"premise": "it_is_fine", "reason": "trust me"}},
    )
    assert any("not implemented" in f for f in failures), failures


def test_a_scope_exclusion_that_shields_nothing_fails(tmp_path, monkeypatch):
    (tmp_path / "vendor" / "pii").mkdir(parents=True)
    (tmp_path / "vendor" / "pii" / "PROVENANCE.md").write_text("upstream abc123\n")
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("vendor/pii/PROVENANCE.md", "elsewhere/a.py"),
        scope={
            "vendor/pii": {
                "premise": "vendored_with_provenance",
                "reason": "third-party source",
            }
        },
    )
    assert any("shields no tracked file" in f for f in failures), failures


# --------------------------------------------------------------------------- #
# The measurement must stay possible
# --------------------------------------------------------------------------- #
def test_force_exclude_would_blind_the_measurement(tmp_path, monkeypatch):
    """`force-exclude = true` makes ruff honour exclusions for named paths too,
    so the gate would measure nothing and report everything as unchanged."""
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("pkg/a.py",),
        extra_toml="\nforce-exclude = true\n",
    )
    assert any("force-exclude" in f for f in failures), failures


def test_ruff_reaching_a_gitignored_path_fails(tmp_path, monkeypatch):
    """`make lint` lets ruff walk. A stale `.aws-sam/` or a worktree under
    `.claude/` gives findings CI cannot reproduce — 157 of them, once."""
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("pkg/a.py",),
        walk=("pkg/a.py", ".claude/worktrees/x/src/lambda/index.py"),
        ignored=(".claude/worktrees/x/src/lambda/index.py",),
    )
    assert any("gitignored path" in f for f in failures), failures


# --------------------------------------------------------------------------- #
# And the real tree
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    __import__("shutil").which("ruff") is None,
    reason="ruff is not on PATH (see the venv-activation note in CONTRIBUTING.md)",
)
def test_the_real_repository_satisfies_the_gate():
    """Same assertion as `make check-lint-debt`, run where both CIs will see it.

    `make lint` runs in neither CI; `pytest scripts/tests` runs in both.
    """
    module = _load_module()
    report = module.Report()
    module.check(report)
    assert not report.failures, "\n\n".join(report.failures)
