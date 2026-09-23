# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Prove `scripts/check_lint_debt.py` fails for each thing it claims to catch.

A ratchet nobody has watched fail is not a ratchet. `ruff.toml` excludes a named list
of files from `ruff check` and a longer one from `ruff format --check`
(`check_lint_debt.py --summary` prints both), so for those files the two
lint gates are silent by construction, and everything protecting them lives in
that script. Each test below drives one failure mode and asserts the message
names it — including the directions that a plain "does it still pass?" check
cannot distinguish: a listed file that *gained* a finding, a listed file that is
now clean and must be delisted, and `--write` being asked to *add* a file, which
would otherwise turn a red gate green in one command.

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


def _setup(
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
    walk=None,
    ignored=(),
    extra_toml="",
):
    """Point the module at a synthetic tree. Returns its baseline path."""
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
    # Default: ruff sees everything git tracks. Only the discovery-closure tests
    # override this, so the other fixtures are unaffected by that check.
    discovered = sorted(tracked) if walk is None else list(walk)
    monkeypatch.setattr(MODULE, "ruff_walk", lambda: discovered)
    monkeypatch.setattr(MODULE, "git_ignored", lambda paths: set(ignored) & set(paths))
    return baseline


def _harness(tmp_path, monkeypatch, **kwargs):
    """Run `check()` against a synthetic tree and return the failure list."""
    mark = kwargs.pop("high_water_mark", "auto")
    baseline_path = _setup(tmp_path, monkeypatch, **kwargs)
    if mark is not None:
        data = json.loads(baseline_path.read_text())
        if mark == "auto":
            mark = {
                "lintDebtFiles": len(data["lintDebt"]),
                "lintFindings": sum(sum(c.values()) for c in data["lintDebt"].values()),
                "formatDebtFiles": len(data["formatDebt"]),
            }
        data["highWaterMark"] = mark
        baseline_path.write_text(json.dumps(data))
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
# The list itself may only shrink. Without this, --write launders new findings
# into a permanent exclusion: the gate reddens on an unlisted file and says "do
# not add the file to lintDebt", and --write adds it anyway.
# --------------------------------------------------------------------------- #
def _write(tmp_path, monkeypatch, *, allow=None, mark=None, **kwargs):
    baseline_path = _setup(tmp_path, monkeypatch, **kwargs)
    data = json.loads(baseline_path.read_text())
    data["highWaterMark"] = mark or {
        "lintDebtFiles": 1,
        "lintFindings": 1,
        "formatDebtFiles": 0,
    }
    baseline_path.write_text(json.dumps(data))
    MODULE.write(allow)
    return json.loads(baseline_path.read_text())


def test_write_refuses_to_add_a_newly_dirty_file(tmp_path, monkeypatch):
    """The exact laundering path: one command turning a red gate green."""
    with pytest.raises(SystemExit) as excinfo:
        _write(
            tmp_path,
            monkeypatch,
            tracked=("pkg/a.py", "pkg/new.py"),
            findings={"pkg/a.py": {"I001": 1}, "pkg/new.py": {"F401": 3}},
            lint_debt={"pkg/a.py": {"I001": 1}},
        )
    message = str(excinfo.value)
    assert "refusing to write" in message
    assert "pkg/new.py" in message
    assert "lintDebtFiles: 1 → 2" in message


def test_write_refuses_to_add_a_newly_unformatted_file(tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        _write(
            tmp_path,
            monkeypatch,
            tracked=("pkg/a.py", "pkg/new.py"),
            findings={"pkg/a.py": {"I001": 1}},
            unformatted=("pkg/new.py",),
            lint_debt={"pkg/a.py": {"I001": 1}},
        )
    assert "formatDebtFiles: 0 → 1" in str(excinfo.value)


def test_write_tells_you_to_name_the_path_when_reformatting(tmp_path, monkeypatch):
    """Bare `ruff format` honours the exclusion, so it skips the file being fixed.

    Discovered the hard way: this script's own file landed in `[format] exclude`
    and then bare `ruff format` would not touch it.
    """
    with pytest.raises(SystemExit) as excinfo:
        _write(
            tmp_path,
            monkeypatch,
            tracked=("pkg/a.py", "pkg/new.py"),
            findings={"pkg/a.py": {"I001": 1}},
            unformatted=("pkg/new.py",),
            lint_debt={"pkg/a.py": {"I001": 1}},
        )
    assert "ruff format <that path>" in str(excinfo.value)


def test_write_allows_growth_only_with_a_recorded_reason(tmp_path, monkeypatch):
    data = _write(
        tmp_path,
        monkeypatch,
        allow="a merge brought in files another branch never formatted",
        tracked=("pkg/a.py", "pkg/new.py"),
        findings={"pkg/a.py": {"I001": 1}, "pkg/new.py": {"F401": 3}},
        lint_debt={"pkg/a.py": {"I001": 1}},
    )
    assert data["newDebtJustifications"] == [
        "a merge brought in files another branch never formatted"
    ]
    assert data["highWaterMark"]["lintDebtFiles"] == 2


def test_write_lowers_the_mark_when_the_debt_shrinks(tmp_path, monkeypatch):
    """The ratchet turns one way: paying debt down must tighten the limit."""
    data = _write(
        tmp_path,
        monkeypatch,
        mark={"lintDebtFiles": 5, "lintFindings": 40, "formatDebtFiles": 9},
        tracked=("pkg/a.py",),
        findings={"pkg/a.py": {"I001": 1}},
        lint_debt={"pkg/a.py": {"I001": 1}},
    )
    assert data["highWaterMark"] == {
        "lintDebtFiles": 1,
        "lintFindings": 1,
        "formatDebtFiles": 0,
    }


def test_a_hand_grown_list_without_a_matching_mark_fails(tmp_path, monkeypatch):
    """Growth has to show up as a diff on three numbered lines, not 200 quiet ones."""
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("pkg/a.py", "pkg/b.py"),
        findings={"pkg/a.py": {"I001": 1}, "pkg/b.py": {"F401": 1}},
        lint_debt={"pkg/a.py": {"I001": 1}, "pkg/b.py": {"F401": 1}},
        high_water_mark={"lintDebtFiles": 1, "lintFindings": 1, "formatDebtFiles": 0},
    )
    assert any("highWaterMark" in f for f in failures), failures


def test_a_missing_mark_fails(tmp_path, monkeypatch):
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("pkg/a.py",),
        findings={"pkg/a.py": {"I001": 1}},
        lint_debt={"pkg/a.py": {"I001": 1}},
        high_water_mark=None,
    )
    assert any("no highWaterMark" in f for f in failures), failures


# --------------------------------------------------------------------------- #
# --explain: the probe that cannot lie. Every ruff-native probe misreports at
# least one class of file, which is the reason #975 survived inspection.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path,expected",
    [
        (
            "pkg/lintdebt.py",
            ["ruff check        : NOT read", "ruff format       : read"],
        ),
        (
            "pkg/fmtdebt.py",
            ["ruff check        : read", "ruff format       : NOT read"],
        ),
        ("pkg/clean.py", ["ruff check        : read", "ruff format       : read"]),
        ("vendor/pii/x.py", ["scope exclusion", "third-party source"]),
    ],
)
def test_explain_classifies_each_kind_of_exclusion(
    tmp_path, monkeypatch, capsys, path, expected
):
    (tmp_path / "vendor" / "pii").mkdir(parents=True)
    (tmp_path / "vendor" / "pii" / "PROVENANCE.md").write_text("upstream abc123\n")
    _setup(
        tmp_path,
        monkeypatch,
        tracked=(
            "pkg/lintdebt.py",
            "pkg/fmtdebt.py",
            "pkg/clean.py",
            "vendor/pii/PROVENANCE.md",
            "vendor/pii/x.py",
        ),
        findings={"pkg/lintdebt.py": {"I001": 1}},
        unformatted=("pkg/fmtdebt.py",),
        lint_debt={"pkg/lintdebt.py": {"I001": 1}},
        format_debt=("pkg/fmtdebt.py",),
        scope={
            "vendor/pii": {
                "premise": "vendored_with_provenance",
                "reason": "third-party source",
            }
        },
    )
    assert MODULE.explain(path) == 0
    out = capsys.readouterr().out
    for fragment in expected:
        assert fragment in out, out


def test_explain_refuses_an_untracked_path(tmp_path, monkeypatch, capsys):
    """An answer about a file no gate can see must not look like a verdict."""
    _setup(tmp_path, monkeypatch, tracked=("pkg/a.py",))
    assert MODULE.explain("pkg/nope.py") == 2
    assert "not tracked by git" in capsys.readouterr().out


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


def test_a_source_tree_missing_from_ruffs_walk_fails(tmp_path, monkeypatch):
    """#975 is re-openable through the TOP-LEVEL `exclude` array, which the
    bare-name check cannot see (that array is bare names on purpose, for build
    output). Measured on the real tree: adding "scripts" back to it drops the walk
    from 1202 to 1066 files while `ruff check` still prints "All checks passed!".
    This asserts the property instead of the names, so it covers a form nobody has
    thought of yet."""
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("pkg/a.py", "pkg/unseen.py"),
        walk=("pkg/a.py",),
    )
    assert any("does not look at" in f and "pkg/unseen.py" in f for f in failures), (
        failures
    )


def test_a_scope_covered_file_may_be_absent_from_the_walk(tmp_path, monkeypatch):
    """The other side of the check above: a scope exclusion legitimately removes
    files from discovery, so it must not be reported as a coverage hole."""
    (tmp_path / "vendor" / "pii").mkdir(parents=True)
    (tmp_path / "vendor" / "pii" / "PROVENANCE.md").write_text("upstream abc123\n")
    failures = _harness(
        tmp_path,
        monkeypatch,
        tracked=("pkg/a.py", "vendor/pii/PROVENANCE.md", "vendor/pii/x.py"),
        walk=("pkg/a.py",),
        scope={
            "vendor/pii": {
                "premise": "vendored_with_provenance",
                "reason": "third-party source",
            }
        },
    )
    assert failures == [], failures


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
# The measurement must be able to see every file it is given
# --------------------------------------------------------------------------- #
def test_a_finding_in_a_path_containing_a_space_is_attributed(monkeypatch):
    """A space in a path must not make a file's findings invisible to the ratchet.

    The first version of the parser matched the path as "anything but whitespace
    or a colon", so such a file's findings simply did not match: bare
    `ruff check` would fail on it while the ratchet counted zero. No tracked file
    has a space today, which is exactly why this is a test and not a bug report.
    """
    concise = (
        "pkg/with space.py:3:1: I001 Import block is un-sorted\n"
        "pkg/plain.py:9:5: F401 `os` imported but unused\n"
        "pkg/nb.ipynb:cell 3:12:1: E402 Module level import not at top of file\n"
    )
    monkeypatch.setattr(
        MODULE,
        "tracked_files",
        lambda: ["pkg/with space.py", "pkg/plain.py", "pkg/nb.ipynb"],
    )
    monkeypatch.setattr(
        MODULE,
        "_run_ruff",
        lambda args, paths: concise if args[0] == "check" else "",
    )
    findings, _ = MODULE.measure()
    assert findings["pkg/with space.py"] == {"I001": 1}
    assert findings["pkg/plain.py"] == {"F401": 1}
    assert findings["pkg/nb.ipynb"] == {"E402": 1}


def test_a_finding_it_cannot_attribute_is_a_hard_error(monkeypatch):
    """Silently dropping an unparseable line is how the ratchet would under-count."""
    monkeypatch.setattr(MODULE, "tracked_files", lambda: ["pkg/a.py"])
    monkeypatch.setattr(
        MODULE,
        "_run_ruff",
        lambda args, paths: (
            "somewhere/else.py:1:1: F401 x\n" if args[0] == "check" else ""
        ),
    )
    with pytest.raises(SystemExit) as excinfo:
        MODULE.measure()
    assert "could not attribute" in str(excinfo.value)


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
