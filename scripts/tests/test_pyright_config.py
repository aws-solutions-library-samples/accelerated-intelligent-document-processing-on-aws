# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assert every path in pyrightconfig.json's `include` array exists on disk.

`include` listed `idp_cli/idp_cli`, a path that has not existed since the CLI
moved to `lib/idp_cli_pkg/idp_cli`. basedpyright prints one line about the
missing directory and then contributes *nothing* for that entry, so it exits 0
over whatever remains. The result was that `lib/idp_cli_pkg/idp_cli/cli.py`
(6,791 lines) and the rest of the CLI package were type-checked by nothing at
all, while `make typecheck` looked like it was passing over the whole repo.

A stale include entry is invisible by construction: the gate gets quieter, not
louder, so nothing draws attention to it. These tests make the typo fail here
instead. `exclude` is checked two ways — a stale exclude silently stops
excluding (the less harmful direction, but still a lie about intent), and an
exclude entry that covers an include entry guts the gate from the other side,
because `exclude` wins.

Coverage of first-party source is **derived** from `lib/*/pyproject.toml`, not
listed in this file. An earlier version listed three paths in the test body,
which meant a newly added distribution was covered by nothing and the test
still passed — the same "control that enumerates the known instances" defect
this whole change set is about. Three distributions really were in that blind
spot (`idp_sdk`, `idp_feature_sdk`, `idp_mcp_connector_pkg`: 64 files, 28,958
lines). All are now in `include`; measured cost was 0 new errors and +18
warnings, so covering them was free.

Coverage of the *rest* of the tree is derived the same way, from `git ls-files`:
`test_every_tracked_python_file_is_type_checked` fails if any tracked `.py` file
falls outside `include` or is cancelled by `exclude`. That is the check the
six-entry `include` list could not have: it reached 432 of 1230 tracked files,
and the 798 it missed included every `lib/*/tests` suite and all of `scripts/`,
`nested/`, `feature-platform/`, `benchmarks/` and `samples/`. A carve-out from it
goes in `TYPECHECK_SCOPE_EXCLUSIONS` with a reason, and is asserted to still
shield something.

The **severities** are asserted too, not just the paths. Covering every file is
only half a gate: the other half is whether a finding in a covered file can fail
it. `reportCallIssue` and `reportReturnType` sat at "warning", so basedpyright
exited 0 over 36 `reportCallIssue` diagnostics that included six `idp_sdk` result
constructors which raised `TypeError` on every input — the type checker had found
all of it and the gate's exit code did not carry the answer. `_severity_findings`
below now closes that: a rule in `ENFORCED_ERROR_RULES` cannot be downgraded, and
the set of rules pinned to "none" cannot grow without an entry in
`DISABLED_RULE_EXEMPTIONS` explaining that member.

Covering every file and enforcing every severity still leaves a third question:
**can the checker see the types on the other side of a call?** It could not. With
`reportMissingImports` at "none" and no `extraPaths`, `idp_common` did not resolve,
so basedpyright knew nothing about the signature of anything in the shared library
and could produce no diagnostic about any call into it — across the boundary most of
this repository's code uses to reach its own core. `filesAnalyzed` was 1,314 either
way, so the gate read every file, matched `git ls-files` exactly, and proved far less
than that suggested. Eleven `reportCallIssue`/`reportOperatorIssue` errors were
sitting behind it, three of them statements that raise on every execution. Issue
#1109.

`extraPaths` closes it in the configuration rather than in the environment, and that
choice is the load-bearing part. `PYTHONPATH` would have worked too and is worse:
the answer would depend on how the gate was invoked, and the machine this was found
on carries editable installs of `idp_common` and `idp_sdk` pointing at a **sibling
worktree** and at **another project** (#1094), so an environment-level fix can
type-check confidently against somebody else's copy of the library. Measured: an
`extraPaths` of `/home/ec2-user/projects/idp3/lib/idp_sdk` resolves `idp_sdk`
perfectly and says nothing about this tree.

So resolution is asserted four ways below, because "it resolves" is the weakest claim
of the four and is the one that passes in the broken configuration:

1. The top-level entries must be **relative and inside this repository**. pyright
   resolves them against the directory holding the config, so a relative entry
   cannot name another checkout — that makes "this tree" structural rather than
   lucky, and it is the assertion doing the real work. A symlink out of the tree is
   rejected separately, since the lexical argument cannot see one.
2. A live basedpyright probe must actually resolve all five first-party imports
   through the values the shipped config carries, in a throwaway project with
   `reportMissingImports` forced to "error" so the answer is reported. Its value is
   exercising the pinned basedpyright rather than re-reading the JSON.
3. basedpyright's **own reported search-path order** must make one of the
   **configured `extraPaths`** the first provider of each of the five, **in every
   reported environment**. Both emphasised parts were added after each weaker form
   was measured to pass while the gate was blind — see
   `_reported_search_path_blocks` and
   `test_first_party_imports_resolve_to_the_configured_package_roots`.
4. No `executionEnvironments` entry may declare its own `extraPaths`. One such entry
   reopens #1109 for a whole subtree, order-independently, and this is the cheap and
   total form of the statement (3) makes by measurement.

(3) and (4) overlap but neither subsumes the other: a per-root `extraPaths` is caught
by both, while an environment **root** that shadows a package root declares no
`extraPaths` at all and is caught only by (3).

All of these run with `PYTHONPATH` stripped from the subprocess. That is not
tidiness: basedpyright honours it, so a probe inheriting one measures the caller's
shell. Observed in both directions before it was fixed — with `PYTHONPATH` exported
the negative control resolved everything and failed, and the positive checks would
have passed over an empty `extraPaths`.

The **Python import** half of the same question — which tree `import idp_common`
reads under `pytest`, where a stale editable install genuinely does redirect — is
`scripts/tests/test_first_party_provenance.py`. The two are complementary: this
module asks basedpyright, that one asks the interpreter.

Changing a severity string is only the most obvious way to weaken a severity, and
the other four are each measured rather than reasoned about. `typeCheckingMode` is
pinned, because it governs the ~100 `report*` rules this config never names and
`"off"` takes a file whose one defect is `reportIndexIssue` from 1 error to 0.
Booleans are rejected, because basedpyright accepts `"reportIndexIssue": false`
and it silences the rule. Deleting a rule that defaults to "none" is caught by
`ENFORCED_WARNING_RULES` and by a rule count derived from the three authored lists
rather than a loose bound. And an `executionEnvironments` root is required to be a
strict descendant of an `include` entry, because `{"root": "."}` relaxes a rule for
the whole tree — defeating `ENFORCED_ERROR_RULES` through scope while every
severity in the file still reads "error".
"""

from __future__ import annotations

import json
import os
import posixpath
import subprocess
import tempfile
import tomllib
from fnmatch import fnmatch
from pathlib import Path

import gate_premises
import pytest
from setuptools import find_packages

REPO_ROOT = Path(__file__).resolve().parents[2]
PYRIGHT_CONFIG = REPO_ROOT / "pyrightconfig.json"
LIB_ROOT = REPO_ROOT / "lib"

pytestmark = pytest.mark.unit

# Top-level directory names a distribution may contain that are not shipped,
# importable source. Mirrors IGNORED_DIR_NAMES in test_package_discovery.py.
NON_SOURCE_TOP_LEVELS = {"tests", "test", "examples", "build", "dist", "docs"}

# Distributions deliberately NOT under a pyrightconfig `include` entry.
#
# This must stay EMPTY unless there is a reason worth writing down. The whole
# point of deriving the inventory below is that a newly added distribution is
# covered by default and dropping one is a visible decision, not an omission.
# Format: distribution directory name -> reason (and an issue reference).
#
# All five first-party distributions are covered as of this commit. The three
# that were previously outside `include` (idp_sdk, idp_feature_sdk,
# idp_mcp_connector_pkg — 64 files, 28,958 lines) were added after measuring
# that they contribute 0 basedpyright errors, so covering them cost nothing.
COVERAGE_EXEMPTIONS: dict[str, str] = {}


def _config() -> dict:
    return json.loads(PYRIGHT_CONFIG.read_text())


def _include_paths() -> list[str]:
    return list(_config().get("include", []))


def _pyprojects() -> list[Path]:
    """The source of truth for "what first-party distributions exist".

    Same glob `test_package_discovery.py` uses, deliberately: a control that
    enumerates the known instances in its own body stops being a control the
    moment a sixth distribution is added. This one derives the inventory, so a
    new `lib/<something>/pyproject.toml` is in scope automatically.
    """
    return sorted(LIB_ROOT.glob("*/pyproject.toml"))


def _importable_source_dirs(pyproject: Path) -> list[str]:
    """Repo-relative paths of the importable package dirs a distribution ships.

    Resolved from the distribution's own setuptools configuration rather than
    assumed from the directory name — they differ in practice
    (`lib/idp_mcp_connector_pkg` ships `idp_mcp_connector`, `lib/idp_cli_pkg`
    ships `idp_cli`).
    """
    cfg = tomllib.loads(pyproject.read_text())
    setuptools_cfg = cfg.get("tool", {}).get("setuptools", {})
    packages = setuptools_cfg.get("packages")
    pkg_dir = pyproject.parent

    tops: set[tuple[str, str]] = set()  # (where, top-level package name)
    if isinstance(packages, list):
        # Explicit list form: `packages = ["idp_cli"]`.
        tops = {(".", name.split(".")[0]) for name in packages}
    elif isinstance(packages, dict) and "find" in packages:
        spec = packages["find"]
        for root in spec.get("where", ["."]):
            found = find_packages(
                where=str(pkg_dir / root),
                include=spec.get("include", ["*"]),
                exclude=spec.get("exclude", []),
            )
            tops |= {(root, name.split(".")[0]) for name in found}

    out: list[str] = []
    for where, top in sorted(tops):
        if top in NON_SOURCE_TOP_LEVELS:
            continue
        target = (pkg_dir / where / top).resolve()
        if not target.is_dir():
            continue
        out.append(str(target.relative_to(REPO_ROOT)))
    return out


def test_pyright_config_exists() -> None:
    assert PYRIGHT_CONFIG.is_file(), f"missing {PYRIGHT_CONFIG}"


def test_include_is_non_empty() -> None:
    """An empty `include` would make basedpyright fall back to scanning the
    whole tree, which is not what this config intends."""
    assert _include_paths(), "pyrightconfig.json declares no `include` paths"


@pytest.mark.parametrize("rel", _include_paths())
def test_every_include_path_exists(rel: str) -> None:
    """A non-existent include entry silently removes files from the gate."""
    target = REPO_ROOT / rel
    assert target.exists(), (
        f"pyrightconfig.json `include` lists {rel!r}, which does not exist. "
        "basedpyright contributes nothing for a missing include entry, so this "
        "typo would silently drop those files from `make typecheck`. Update the "
        "path (or remove the entry if the code is gone)."
    )


@pytest.mark.parametrize("rel", _include_paths())
def test_every_include_path_contains_python(rel: str) -> None:
    """Existing but Python-free is the same failure wearing a disguise: the
    entry contributes no files, so it is not actually covering anything."""
    target = REPO_ROOT / rel
    if not target.exists():
        pytest.skip("covered by test_every_include_path_exists")
    if target.is_file():
        assert target.suffix == ".py", f"include entry {rel!r} is not a .py file"
        return
    assert next(target.rglob("*.py"), None) is not None, (
        f"pyrightconfig.json `include` lists {rel!r}, which exists but holds no "
        "*.py files, so it adds nothing to the typecheck set."
    )


def test_exclude_paths_that_look_concrete_exist() -> None:
    """Check the `exclude` entries that name a real directory.

    Glob-style entries (`**/build`, `patterns/*/src`) are skipped: they are
    patterns, and a pattern matching nothing today is legitimate.
    """
    stale = [
        rel
        for rel in _config().get("exclude", [])
        if not any(ch in rel for ch in "*?[")
        if not (REPO_ROOT / rel).exists()
    ]
    assert not stale, (
        f"pyrightconfig.json `exclude` names path(s) that do not exist: {stale}. "
        "A stale exclude no longer excludes anything — either the path moved (fix "
        "it) or the code is gone (drop the entry)."
    )


def _is_covered(rel: str, includes: list[str]) -> bool:
    return any(rel == inc or rel.startswith(inc.rstrip("/") + "/") for inc in includes)


def test_there_are_first_party_distributions_to_check() -> None:
    """Anti-vacuity guard for the derived inventory below.

    If `lib/` is restructured and the glob stops matching, the parametrised
    test would silently collect nothing and the file would still report green.
    """
    assert len(_pyprojects()) >= 5, (
        f"expected at least 5 first-party distributions under {LIB_ROOT}, found "
        f"{[str(p) for p in _pyprojects()]}. The coverage check below derives its "
        "inventory from this glob, so an empty result makes it vacuous."
    )


@pytest.mark.parametrize("pyproject", _pyprojects(), ids=lambda p: p.parent.name)
def test_first_party_distribution_is_typechecked(pyproject: Path) -> None:
    """Every first-party distribution's importable source falls under `include`.

    Derived from `lib/*/pyproject.toml`, NOT from a list written here. The
    earlier version of this test named three paths in its own body, so a newly
    added distribution was covered by nothing and the test still passed —
    exactly the failure mode this file exists to prevent, one level up. Three
    distributions (idp_sdk, idp_feature_sdk, idp_mcp_connector_pkg) were in that
    blind spot when it was found.

    To drop a distribution from the gate, add it to COVERAGE_EXEMPTIONS with a
    reason. That is a visible decision; an omission is not.
    """
    dist = pyproject.parent.name
    includes = _include_paths()
    source_dirs = _importable_source_dirs(pyproject)

    assert source_dirs, (
        f"{pyproject.relative_to(REPO_ROOT)}: could not resolve any importable "
        "package directory from its setuptools configuration. Either the layout "
        "changed or the derivation in _importable_source_dirs() is stale — fix "
        "that before trusting this test, because an empty result would otherwise "
        "pass silently."
    )

    uncovered = [rel for rel in source_dirs if not _is_covered(rel, includes)]

    if dist in COVERAGE_EXEMPTIONS:
        assert uncovered, (
            f"{dist!r} is listed in COVERAGE_EXEMPTIONS but is now fully covered "
            f"by pyrightconfig.json `include`. Delete the exemption — a stale one "
            "hides the next real gap."
        )
        pytest.skip(f"{dist}: exempt — {COVERAGE_EXEMPTIONS[dist]}")

    assert not uncovered, (
        f"{dist!r} ships first-party Python that no pyrightconfig.json `include` "
        f"entry covers: {uncovered} (current entries: {includes}).\n\n"
        "Add the path to `include` so it is type-checked. Measure the cost first "
        "with `basedpyright --venvpath <repo> <path>`; the three distributions "
        "added in this commit contributed 0 errors, so it is often free.\n\n"
        "If it genuinely must stay out of the gate, add an entry to "
        "COVERAGE_EXEMPTIONS in this file with a one-line reason and an issue "
        "reference, so the gap is a recorded decision rather than an omission."
    )


def test_non_first_party_include_entries_still_exist() -> None:
    """`src/lambda` is not a distribution, so the derived test above cannot see
    it. It is the parent stack's Lambda source and must stay covered."""
    assert _is_covered("src/lambda", _include_paths()), (
        "`src/lambda` (parent-stack Lambda handlers) is no longer covered by any "
        "pyrightconfig.json `include` entry. It holds the two "
        "`reportOperatorIssue` errors this gate was repaired to catch."
    )


#: Tracked Python that `include` is allowed not to cover. Each entry is a
#: pyrightconfig `exclude` pattern, and the reason has to be a property of the
#: files it matches rather than of the directory it happens to name.
#:
#: `notebooks/**/*.ipynb` shields 10 errors, and **6 of them are false positives
#: of a mechanism specific to notebooks**, which is what makes this a scope
#: decision rather than deferred work. basedpyright reads a notebook's cells in
#: document order and resolves names as it goes. Python does not: a global is
#: looked up when the function runs. So in the five `notebooks/misc/e2e-*`
#: notebooks, `s3_client` is assigned at column 0 in code-cell 2 and referenced
#: inside a function *defined* in code-cell 1 — flagged `reportUndefinedVariable`,
#: and correct at runtime, because cell 2 executes before anything calls that
#: function. The two `e2e-*` notebooks where the assignment and the first use sit
#: in the same cell are not flagged, which is the control for that explanation.
#: The sixth is the same shape one step along: a top-level `from PIL import Image`
#: in cell 1 plus a re-import in cell 7 leaves the name possibly-unbound on one
#: branch, so it is `reportUnboundVariable`.
#:
#: The other 4 (2 `reportOperatorIssue`, 2 `reportOptionalOperand`) are NOT
#: explained by that and have not been triaged, which is the honest residual here.
#: `scripts/lint_debt.json` records the same carve-out for ruff, with its own
#: premise.
TYPECHECK_SCOPE_EXCLUSIONS: dict[str, str] = {
    "notebooks/**/*.ipynb": (
        "basedpyright resolves a notebook's cells in document order while Python "
        "resolves globals at call time, so a name assigned in a later cell and "
        "used inside an earlier cell's function reads as undefined and is not; 6 "
        "of the 10 errors are that, 4 are untriaged"
    ),
}


#: Trees that hold Python only after a local build, and that basedpyright's walk
#: would otherwise read. One entry per tree, keyed by the directory, with the reason
#: for **that** directory — the pyrightconfig `exclude` pattern is derived from the
#: key below rather than authored, so the two cannot drift apart.
#:
#: basedpyright discovers files by walking the filesystem and has no notion of an
#: ignore file: there is no such setting in `pyrightconfig.json`, no such command-line
#: option, and the string does not occur anywhere in the 5,032 files the 1.32.1 npm
#: package ships. So an `exclude` entry is the only mechanism available, and each one
#: is a claim that has to be checked — which is what the tests below do, per entry.
#:
#: What makes this class of path safe to exclude is not that it is currently absent
#: from a clean checkout; it is that an ignore rule covers it, so a file under it
#: cannot become tracked without that rule being edited.
#: `gate_premises.vcs_ignored_build_output` measures both halves per member.
STAGED_BUILD_OUTPUT_EXEMPT: dict[str, str] = {
    "feature-platform/idp-data-generator/idp_common_pkg": (
        "the accelerator library copied into the AgentCore image build context by "
        "feature-platform/idp-data-generator/package_agent_source.sh at package "
        "time; ignored at feature-platform/idp-data-generator/.gitignore line 5. It "
        "is a snapshot of lib/idp_common_pkg, which the gate already reads at its "
        "tracked location, so checking the copy adds no coverage and pins the gate "
        "to whichever revision the last local build happened to stage"
    ),
    "feature-platform/idp-data-generator/bootstrap-processor/idp_common_pkg": (
        "the same library staged a second time, into the bootstrap-processor "
        "Lambda's CodeUri so the function can be packaged with it; ignored at "
        "feature-platform/idp-data-generator/.gitignore line 6. Same snapshot, same "
        "reasoning — and being a second copy is exactly why this is two entries "
        "rather than one pattern spanning both: each has its own ignore rule and "
        "its own reason to exist"
    ),
}


def _bare_directory_name(pattern: str) -> str | None:
    """The directory name in a `**/<name>` pattern, or None if it is not one.

    The seven cache and artifact directory names in `exclude` are bare on purpose —
    a `build/` or a `node_modules/` is build output wherever it sits — and they are
    the one group here with no per-member reason written down. They do not need one,
    because the claim is computable: :func:`gate_premises.vcs_ignored_build_output`
    asks whether an ignore rule covers the name and whether git tracks anything under
    it, and that is the whole of what "build output at any depth" means.

    Deriving membership this way rather than listing it is what stops `**/<name>`
    becoming the spelling that excludes anything without giving a reason. `**/build`
    and `**/notebooks` are the same shape; only one of them is ignored, so only one of
    them lands in this group and the other has to justify itself.
    """
    if not pattern.startswith("**/"):
        return None
    leaf = pattern.removeprefix("**/")
    return leaf if leaf and "*" not in leaf and "/" not in leaf else None


def _ignored_bare_directory_exclusions() -> list[str]:
    """`exclude` patterns that are a bare, ignored build-output directory name."""
    return [
        pattern
        for pattern in _config().get("exclude", [])
        if (leaf := _bare_directory_name(pattern))
        and gate_premises.vcs_ignored_build_output(leaf)[0]
    ]


@pytest.mark.parametrize(
    "pattern",
    [p for p in _config().get("exclude", []) if _bare_directory_name(p)],
)
def test_a_bare_directory_exclusion_is_ignored_build_output(pattern: str) -> None:
    """Per member: every bare `**/<name>` in `exclude` is covered by an ignore rule.

    `**/cdk.out` was not. Nothing tracked lived under it, so it cost no coverage
    today — but the durable half of this premise is the ignore rule, and without one
    a `cdk.out/` holding committed Python could appear and be excluded from
    `make typecheck` with no edit to any file a reviewer would look at. The remedy is
    an ignore rule, not a longer reason.
    """
    leaf = _bare_directory_name(pattern)
    assert leaf is not None
    holds, why = gate_premises.vcs_ignored_build_output(leaf)
    assert holds, (
        f"pyrightconfig.json `exclude` carries the bare directory name {pattern!r} as "
        f"build output at any depth, and that is not true of it: {why}. Add an ignore "
        "rule for it, or — if it holds code that ships — stop excluding it."
    )


#: Filename shapes another gate writes **into** the tree while it runs, keyed by the
#: `exclude` pattern, with the reason for that one shape. One entry, one artifact, one
#: reason — never a shared "generated files" carve-out, because the thing that makes
#: each of these safe is a specific ignore rule and a specific writer.
#:
#: These are distinct from :data:`STAGED_BUILD_OUTPUT_EXEMPT` in the axis they name.
#: That one excludes a *tree* a local build stages, and its members are directories. A
#: member here is a **filename glob**: `srt assess` nbconverts every notebook to
#: `<nb>-converted.py` *beside* the notebook, so the artifact has no fixed home and no
#: directory-keyed exclusion reaches it.
#:
#: The failure this closes is a gate that fails only while another gate is running.
#: `make srt-scan` takes ~15 minutes and the offline suite ~7, both read-only with
#: respect to tracked files, so overlapping them is the obvious thing to do — and doing
#: so made `test_the_typecheck_walk_reaches_no_ignored_python` fail for the duration and
#: pass afterwards with nothing changed in the tree. A red mark that depends on what
#: else was running is unreadable: the reader cannot tell it from a real finding without
#: re-running, which they will only do if they already suspect it. Both gates are right;
#: they interfered through the filesystem. In CI they are separate jobs and cannot
#: overlap, so this was local-only — and local gate runs are what branch decisions get
#: made on here. Issue #1176.
#:
#: `vcs_ignored_generated_filename` computes the premise per member, including the part
#: that stops this becoming a general escape hatch: the pattern's final component must
#: be a wildcard over filenames, so a bare directory name cannot be registered here and
#: the exclusion cannot widen as a tree grows.
GENERATED_ARTIFACT_EXCLUSIONS: dict[str, str] = {
    "**/*-converted.py": (
        "`srt assess` runs bandit over notebooks by nbconverting each one to "
        "`<nb>-converted.py` beside it, and deletes them when it finishes; ignored at "
        ".gitignore line 30. They exist only while a scan runs, they are a "
        "machine translation of a notebook this gate already covers as a notebook, "
        "and basedpyright would read them as first-party source — it walks the "
        "filesystem and has no ignore-file support"
    ),
}


@pytest.mark.parametrize("pattern", sorted(GENERATED_ARTIFACT_EXCLUSIONS))
def test_generated_artifact_exclusion_is_still_in_the_config(pattern: str) -> None:
    """Staleness: the reason must not outlive the exclusion it justifies."""
    assert pattern in _config().get("exclude", []), (
        f"GENERATED_ARTIFACT_EXCLUSIONS justifies {pattern!r}, which pyrightconfig.json "
        "`exclude` no longer contains. Drop the entry, or restore the pattern — a "
        "reason for a carve-out that is gone reads as a live decision and hides the "
        "next real gap."
    )


@pytest.mark.parametrize("pattern", sorted(GENERATED_ARTIFACT_EXCLUSIONS))
def test_generated_artifact_exclusion_premise_holds(pattern: str) -> None:
    """Per member: an ignore rule covers the shape, nothing tracked matches it, and
    the pattern cannot widen past a filename.

    The premise computed rather than asserted in prose. The direction that costs
    coverage is an exclusion that starts out over a generated artifact and ends up
    over committed code — `exclude` beats `include`, so a tracked `x-converted.py`
    would leave the gate quieter with every other test here still green.
    """
    holds, why = gate_premises.vcs_ignored_generated_filename(pattern)
    assert holds, (
        f"GENERATED_ARTIFACT_EXCLUSIONS excludes {pattern!r} from `make typecheck` as "
        f"an ignored generated artifact, and that is not true of it: {why}."
    )


def test_every_generated_artifact_exclusion_is_registered() -> None:
    """Universe closure over the `exclude` array: no pattern is unaccounted for.

    This is what stops the array being the quiet place a carve-out lands. Every entry
    in pyrightconfig's `exclude` must be categorised by exactly one of the four things
    that can justify one — a bare cache/build directory name, a staged build tree, a
    scope decision, or a generated filename shape — and an entry in **neither** set
    fails here rather than being read as obviously fine. `exclude` beats `include`, so
    an uncategorised entry is the cheapest way to remove a tree from the type gate.
    """
    categorised = (
        set(_ignored_bare_directory_exclusions())
        | {_staged_copy_pattern(rel) for rel in STAGED_BUILD_OUTPUT_EXEMPT}
        | set(TYPECHECK_SCOPE_EXCLUSIONS)
        | set(GENERATED_ARTIFACT_EXCLUSIONS)
    )
    unaccounted = [p for p in _config().get("exclude", []) if p not in categorised]
    assert not unaccounted, (
        f"pyrightconfig.json `exclude` holds {len(unaccounted)} pattern(s) that no "
        f"carve-out record in this file accounts for: {unaccounted}. `exclude` beats "
        "`include`, so each one silently removes files from `make typecheck`. Put it "
        "in STAGED_BUILD_OUTPUT_EXEMPT (a tree a local build stages), "
        "TYPECHECK_SCOPE_EXCLUSIONS (a scope decision about tracked files) or "
        "GENERATED_ARTIFACT_EXCLUSIONS (a filename another tool writes while it runs) "
        "— with the reason for that one entry. The fourth category, a bare "
        "`**/<directory>` name, is DERIVED rather than listed: it qualifies only if an "
        "ignore rule covers the name and git tracks nothing under it, which is what "
        "keeps `**/notebooks` from being categorised by having the same shape as "
        "`**/build`."
    )


def _staged_copy_pattern(rel: str) -> str:
    """The pyrightconfig `exclude` pattern that covers one staged tree.

    A trailing `/**` rather than the bare directory, for a reason that has nothing to
    do with glob semantics — both forms exclude the tree, measured against
    basedpyright 1.32.1. It is that `test_exclude_paths_that_look_concrete_exist`
    treats a slash-free, star-free entry as a concrete path and calls it stale when it
    is absent, which for build output is its normal state. A pattern is the honest
    spelling of "this may or may not be here".
    """
    return f"{rel}/**"


@pytest.mark.parametrize("rel", sorted(STAGED_BUILD_OUTPUT_EXEMPT))
def test_staged_build_output_is_still_excluded(rel: str) -> None:
    """The exclude entry this reason justifies must still be in the config.

    Staleness in the direction that matters: the reason outliving the exclusion reads
    as a live decision about a carve-out that no longer exists, and the next person
    believes the list describes the config.
    """
    pattern = _staged_copy_pattern(rel)
    assert pattern in _config().get("exclude", []), (
        f"STAGED_BUILD_OUTPUT_EXEMPT justifies {rel!r}, but pyrightconfig.json "
        f"`exclude` no longer contains {pattern!r}. Either restore the pattern or "
        "drop the entry — and if the tree is genuinely gone, drop both."
    )


@pytest.mark.parametrize("rel", sorted(STAGED_BUILD_OUTPUT_EXEMPT))
def test_staged_build_output_premise_holds(rel: str) -> None:
    """Per member: an ignore rule covers it and git tracks nothing under it.

    The premise, computed rather than asserted in prose. The failure this catches is
    an exclusion that starts out over build output and ends up over committed code,
    which is the direction that costs coverage: `exclude` beats `include`, so a
    tracked file appearing under one of these paths would leave the gate silently
    quieter with every other test here still green.
    """
    holds, why = gate_premises.vcs_ignored_build_output(rel)
    assert holds, (
        f"STAGED_BUILD_OUTPUT_EXEMPT excludes {rel!r} from `make typecheck` as "
        f"ignored build output, and that is not true of it: {why}. An exclusion over "
        "tracked code has to justify itself some other way — or, better, stop "
        "excluding it."
    )


@pytest.mark.parametrize("rel", sorted(STAGED_BUILD_OUTPUT_EXEMPT))
def test_staged_build_output_names_the_tree_that_holds_the_python(rel: str) -> None:
    """Non-vacuity, where it can be measured: the tree holds Python when present.

    This one is conditional by nature and says so rather than pretending otherwise.
    On a clean checkout the directory does not exist, and its absence is the normal
    state — so there is nothing to be non-vacuous about, and the staleness check above
    is the unconditional half. When the directory *is* present, an entry naming a
    tree with no `.py` in it is pointing at the wrong place: the errors are somewhere
    else and this pattern is shielding whatever next occupies the path.
    """
    target = REPO_ROOT / rel
    if not target.is_dir():
        pytest.skip(
            f"{rel} is absent, which is its state on any checkout where the "
            "idp-data-generator feature has not been packaged locally"
        )
    assert next(target.rglob("*.py"), None) is not None, (
        f"{rel} exists but holds no .py file, so excluding it removes nothing from "
        "`make typecheck`. Either the staging location moved — find it, because the "
        "walk is reading it — or this entry is pre-exempting a path for whatever "
        "lands there next."
    )


def _ignored_python() -> list[str]:
    """Every `.py` file an ignore rule covers, from git.

    Untracked-but-*not*-ignored files are deliberately not here. A file you have
    written and not yet committed is a file you want type-checked, and failing on it
    would make the gate's answer change at `git add` time.
    """
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--",
            "*.py",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(p for p in result.stdout.split("\0") if p)


def test_the_typecheck_walk_reaches_no_ignored_python() -> None:
    """Nothing an ignore rule covers may be inside the gate's walk.

    This is the class-level half, and it is the one that would have turned the
    incident this check was added for into a local test failure naming the directory
    instead of 20 `make typecheck` errors on everybody's machine at once.

    basedpyright discovers files by walking the filesystem, so `include` naming
    `feature-platform` reaches every staged copy, cache and build tree under it. The
    consequences run in both directions and both are bad. Type errors appear in code
    that is not part of this repository — a staged snapshot of `lib/idp_common_pkg`
    was one revision behind, so the gate reported 20 errors nobody could fix by
    editing a tracked file. And the answer depends on what you have built locally: the
    same commit is green on a clean checkout and in CI, red for anyone who has
    packaged the feature. A gate with that property cannot be used to decide anything.

    The remedy for a failure here is an `exclude` pattern plus a record of it, and
    **which record depends on what the artifact is keyed by**: a tree a local build
    stages goes in `STAGED_BUILD_OUTPUT_EXEMPT` as `<path>/**`, while something named by
    its filename wherever it lands — a tool writing `<x>-converted.py` beside each
    notebook — goes in `GENERATED_ARTIFACT_EXCLUSIONS` as a filename glob. The two are
    not interchangeable: `vcs_ignored_generated_filename` refuses a pattern whose final
    component is not a wildcard, so a `<path>/**` entry registered as a generated
    filename fails, and a filename glob registered as a staged tree has no directory to
    ask about. Neither is a wider `exclude`, and neither is a bare directory name outside
    the derived build-output category.
    """
    includes = _include_paths()
    reached = [
        rel
        for rel in _ignored_python()
        if _is_covered(rel, includes)
        if _excluded_by(rel) is None
    ]
    trees = sorted({str(Path(rel).parent) for rel in reached})
    assert not reached, (
        f"{len(reached)} ignored .py file(s) are inside basedpyright's walk, in "
        f"{len(trees)} director(ies):\n  "
        + "\n  ".join(trees[:10])
        + "\n\nThese are not part of this repository — an ignore rule covers them — "
        "and basedpyright has no way to know that: it has no ignore-file support, so "
        "`exclude` is the only mechanism. Add a pyrightconfig `exclude` pattern and "
        "register it in this file with the reason for that one entry, in whichever "
        "record matches what the artifact is keyed by: STAGED_BUILD_OUTPUT_EXEMPT for a "
        "tree a local build stages (as `<path>/**`), or GENERATED_ARTIFACT_EXCLUSIONS "
        "for a filename another tool writes wherever it runs (as a filename glob, e.g. "
        "`**/*-converted.py`). The two are not interchangeable — the generated-filename "
        "premise refuses a pattern whose final component is not a wildcard."
    )


def _tracked_python() -> list[str]:
    """Every tracked `.py` file, from git rather than a filesystem walk.

    `rglob` from the repo root would also walk `scratch/` and
    `.claude/worktrees/`, which routinely hold whole copies of this repository —
    the failure mode `test_repo_walk_guards_prune_local_work.py` exists for.
    """
    result = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(line for line in result.stdout.splitlines() if line)


def _excluded_by(rel: str) -> str | None:
    for pattern in _config().get("exclude", []):
        if pattern.startswith("**/"):
            if pattern[3:] in Path(rel).parts or fnmatch(rel, pattern):
                return pattern
        elif fnmatch(rel, pattern) or rel.startswith(pattern.rstrip("/") + "/"):
            return pattern
    return None


def test_there_is_tracked_python_to_check() -> None:
    """Anti-vacuity guard: an empty file list would make the check below pass."""
    assert len(_tracked_python()) > 500, (
        "git ls-files '*.py' returned "
        f"{len(_tracked_python())} paths, which is too few to be this repository. "
        "The coverage check below would be vacuous."
    )


def test_every_tracked_python_file_is_type_checked() -> None:
    """`include` must cover the whole tree, derived from git rather than listed.

    `include` named six paths and reached 432 of 1230 tracked `.py` files. The
    other 798 — every `lib/*/tests` suite, all 137 files under `scripts/`, the
    78 files under `nested/` (67 API resolvers, 9 Bedrock Knowledge Base
    custom-resource handlers, 2 under `multi-doc-discovery`), `feature-platform/`,
    `benchmarks/` and `samples/` — were type-checked by nothing except
    `make typecheck-pr`, and
    only for the files a given pull request happened to touch. Two
    `NameError`-class defects reached `develop` through that gap.

    This asserts the property rather than the list: a new top-level tree holding
    Python fails here instead of being silently uncovered, which is the failure
    the six-entry list could not detect. Broadening `include` cost 3 errors,
    all genuine (a `-> (str, str)` annotation, an `int` subscripted as a string,
    and `"literal" in __doc__` where `__doc__` is `str | None`).
    """
    includes = _include_paths()
    uncovered: list[str] = []
    for rel in _tracked_python():
        if not _is_covered(rel, includes):
            uncovered.append(f"{rel} (no include entry)")
            continue
        pattern = _excluded_by(rel)
        if pattern and pattern not in TYPECHECK_SCOPE_EXCLUSIONS:
            uncovered.append(f"{rel} (excluded by {pattern!r})")

    assert not uncovered, (
        f"{len(uncovered)} tracked .py file(s) are outside `make typecheck`:\n  "
        + "\n  ".join(uncovered[:20])
        + "\n\nAdd the top-level path to pyrightconfig.json `include` (measure the "
        "cost first: broadening it to the whole tree cost 3 errors), or, if the "
        "files genuinely must stay out, add the `exclude` pattern to "
        "TYPECHECK_SCOPE_EXCLUSIONS in this file with a reason that is a property "
        "of those files."
    )


@pytest.mark.parametrize("pattern", sorted(TYPECHECK_SCOPE_EXCLUSIONS))
def test_scope_exclusion_still_shields_something(pattern: str) -> None:
    """A carve-out that matches nothing is a stale claim, not a decision."""
    excludes = _config().get("exclude", [])
    assert pattern in excludes, (
        f"TYPECHECK_SCOPE_EXCLUSIONS names {pattern!r}, which is no longer a "
        "pyrightconfig.json `exclude` pattern. Drop the entry — a stale "
        "exemption hides the next real gap."
    )
    root = REPO_ROOT / pattern.split("/", 1)[0]
    suffix = pattern.rsplit("*", 1)[-1]
    assert next(root.rglob(f"*{suffix}"), None) is not None, (
        f"{pattern!r} matches no file under {root.name}/, so it shields nothing."
    )


def test_venv_is_not_pinned_in_the_config() -> None:
    """`venvPath`/`venv` made the gate's exit code mean nothing.

    They pointed at `./.venv`, so in any checkout without one — a `git worktree`,
    a fresh clone, a CI job that installed basedpyright from npm and nothing else
    — basedpyright printed one line about the missing directory and exited **3**
    regardless of findings. `make typecheck` therefore failed identically whether
    the tree had type errors or not.

    Dropping them is diagnostic-neutral: with a populated `.venv` present and with
    no `.venv` at all, the run reports the same 0 errors and 92 warnings — one
    `tqdm` stub-resolution warning trades places with one `reportReturnType` in the
    same file — because `reportMissingImports` and the `reportUnknown*` rules are
    already "none".

    The number 92 belongs to those two environments, not to `.venv` presence in
    general. An **empty** `.venv` (a bare `python3 -m venv .venv`, no packages) is a
    third and worse environment: basedpyright then resolves third-party imports to
    nothing and reports 4 errors / 287 warnings, so `make typecheck` fails there.
    That is a property of an unpopulated environment rather than of this config, and
    it is a second reason not to pin a `venvPath` at all — the pin made the gate's
    answer depend on a directory whose contents nothing here controls.
    """
    config = _config()
    present = sorted(key for key in ("venvPath", "venv") if key in config)
    assert not present, (
        f"pyrightconfig.json sets {present}, which makes basedpyright exit 3 in "
        "any checkout without that virtualenv — the same exit code for a config "
        "problem as for nothing at all. If a pinned environment is genuinely "
        "needed, pass --venvpath from the Makefile recipe so a missing one is a "
        "clear message rather than an opaque exit code."
    )


def test_no_exclude_entry_shadows_an_include_entry() -> None:
    """`exclude` beats `include`, so the gate can be gutted through either array.

    Adding `lib/idp_cli_pkg` to `exclude` while leaving the corrected `include`
    entry in place was measured to drop `filesAnalyzed` from 335 to 330 with
    every other test in this file still passing — the same silent narrowing as
    the stale include path, reintroduced from the other direction.

    This is a pure-config check: it does not prove a non-zero file count per
    entry (that would need a basedpyright invocation, which is far slower than
    the rest of this file). It catches an exclude entry that covers an include
    entry outright, which is the shape the mistake actually takes.
    """
    includes = _include_paths()
    excludes = list(_config().get("exclude", []))

    shadowed: list[str] = []
    for inc in includes:
        inc_norm = inc.rstrip("/")
        for exc in excludes:
            exc_norm = exc.rstrip("/")
            # An exclude entry shadows an include entry if it names the entry
            # itself or any ancestor directory of it.
            candidates = [inc_norm, *(str(p) for p in Path(inc_norm).parents)]
            if any(
                c == exc_norm or fnmatch(c, exc_norm) for c in candidates if c != "."
            ):
                shadowed.append(f"exclude {exc!r} shadows include {inc!r}")

    assert not shadowed, (
        "pyrightconfig.json `exclude` cancels out an `include` entry, so those "
        "files are silently dropped from the gate:\n  " + "\n  ".join(shadowed) + "\n"
        "`exclude` wins over `include` in pyright. Narrow the exclude pattern, or "
        "drop the include entry if it is genuinely not meant to be checked."
    )


# --------------------------------------------------------------------------- #
# First-party resolution
#
# The gate cannot report a bad call into `idp_common` unless it can resolve
# `idp_common`. It could not, and `reportMissingImports: "none"` meant nothing said
# so. The two properties asserted here are "the packages resolve" and "they resolve
# to THIS tree", and they are separate checks because satisfying the first with a
# foreign checkout is exactly the accident #1094 makes available.
#
# `reportMissingImports` stays at "none", and that is a measured decision rather
# than an inherited one. Raised to "warning" with `extraPaths` in place it reports
# 44 findings over 25 distinct modules, **none of them first-party**: about 38 are
# sibling-module imports inside script and Lambda trees that are not packages
# (`from index import ...`, `processors.pdf_image_processor`, `seed_data.stages`),
# which resolve at runtime because the handler's own directory is on `sys.path`;
# the remaining handful are genuinely uninstalled third-party distributions
# (`pdf2image`, `fastapi`, `mlflow`, `requests_aws4auth`). So the rule is unusable
# above "none" here for the reason DISABLED_RULE_EXEMPTIONS already gives, and the
# gap it leaves is not "third-party imports are unchecked" but "nothing reports an
# import that *should* have resolved and did not". That is precisely what the
# checks below cover, and why they are not redundant with the rule.
# --------------------------------------------------------------------------- #


#: The five first-party top-level import names, keyed to the distribution that
#: ships each. Derived below from `lib/*/pyproject.toml` rather than trusted from
#: here; this mapping only exists so the live probe has something to `import`.
def _first_party_package_roots() -> dict[str, str]:
    """`{top-level import name: repo-relative directory it must be imported from}`.

    Derived from each distribution's own setuptools configuration, so a sixth
    `lib/<something>/pyproject.toml` is in scope automatically and fails the
    agreement check below until its root is added to `extraPaths`. A list written
    here would be the "control that enumerates the known instances" defect this
    file exists to prevent.
    """
    roots: dict[str, str] = {}
    for pyproject in _pyprojects():
        for source_dir in _importable_source_dirs(pyproject):
            name = posixpath.basename(source_dir)
            roots[name] = posixpath.dirname(source_dir)
    return roots


def _extra_paths() -> list[str]:
    return list(_config().get("extraPaths", []))


def test_there_are_first_party_packages_for_the_resolution_checks() -> None:
    """Anti-vacuity: an empty derivation would make every check below pass."""
    roots = _first_party_package_roots()
    assert len(roots) >= 5, (
        f"derived only {len(roots)} first-party package root(s) ({roots}) from "
        f"{LIB_ROOT}/*/pyproject.toml. The resolution checks below would be vacuous."
    )


def test_first_party_packages_are_resolvable_from_the_config() -> None:
    """The invariant from #1109, stated as the conjunction that caused it.

    `reportMissingImports: "none"` is legitimate on its own (see the block comment
    above: above "none" the rule reports 44 findings on correct code). First-party
    packages failing to resolve is survivable on its own, because the rule would
    then say so on every affected line. **Together** they are a gate that reads
    every file in the repository and cannot produce a diagnostic about any call
    into the shared library, while reporting zero errors and a file count that
    matches `git ls-files` exactly. Nothing detected that combination.
    """
    config = _config()
    roots = _first_party_package_roots()
    extra = {posixpath.normpath(p) for p in _extra_paths()}

    unresolvable = sorted(
        f"{name} (ships from {root!r})"
        for name, root in roots.items()
        if posixpath.normpath(root) not in extra
    )
    if not unresolvable:
        return

    assert config.get("reportMissingImports") != "none", (
        "pyrightconfig.json has reportMissingImports at 'none' AND does not put "
        f"these first-party packages on the import path: {unresolvable}.\n\n"
        f"`extraPaths` currently lists {sorted(extra)}.\n\n"
        "That combination is what makes a green type gate meaningless: basedpyright "
        "reads every tracked file, resolves nothing across the first-party boundary, "
        "and is configured not to mention it — so no call into the shared library "
        "can produce a diagnostic. Eleven real errors, three of them statements that "
        "raise on every execution, sat behind it (#1109).\n\n"
        "Add the package root to `extraPaths`. Use a RELATIVE path: pyright resolves "
        "extraPaths against the directory holding this config, so a relative entry "
        "cannot name another checkout, and an absolute one can (#1094)."
    )


def test_every_extra_path_is_relative_and_inside_this_repository() -> None:
    """What makes resolution mean *this* tree rather than merely some tree.

    pyright resolves `extraPaths` against the directory holding the config, so a
    relative entry that does not climb out is the repository by construction. An
    absolute entry is not: this was found on a machine whose venv resolves
    `idp_common` to a sibling git worktree and `idp_sdk` to a different project
    entirely (#1094), and an `extraPaths` of `/home/ec2-user/projects/idp3/lib/idp_sdk`
    was measured to resolve `idp_sdk` cleanly while saying nothing whatsoever about
    the code in this checkout. Confident wrong answers are worse than silent ones,
    which is the whole reason this is configured rather than exported.

    `posixpath.normpath` is lexical, deliberately: `resolve()` would follow symlinks
    and make the verdict depend on the checkout, the same reasoning
    `_relaxation_scope_findings` uses for `executionEnvironments` roots.
    """
    findings: list[str] = []
    for raw in _extra_paths():
        if posixpath.isabs(raw):
            findings.append(
                f"{raw!r} is absolute. An absolute extraPath can name another "
                "checkout, and on this machine two stale editable installs do "
                "exactly that (#1094). Write it relative to the repo root."
            )
            continue
        normalised = posixpath.normpath(raw)
        if normalised == ".." or normalised.startswith("../"):
            findings.append(
                f"{raw!r} normalises to {normalised!r}, which climbs out of the "
                "repository. It would resolve first-party imports against a tree "
                "this repo does not control."
            )
            continue
        target = REPO_ROOT / normalised
        if not target.is_dir():
            findings.append(
                f"{raw!r} does not exist. pyright contributes nothing for a missing "
                "search path, so this silently stops resolving what it named — the "
                "same failure shape as a stale `include` entry."
            )
            continue
        # A relative entry that is a symlink out of the tree defeats the lexical
        # argument above: the string cannot name another checkout, but the path can
        # still reach one. Checked with an explicit message rather than being left to
        # surface as a `ValueError` from `relative_to` somewhere downstream.
        if not target.resolve().is_relative_to(REPO_ROOT.resolve()):
            findings.append(
                f"{raw!r} is relative but resolves to {target.resolve()}, outside "
                f"{REPO_ROOT.resolve()} — a symlink leaves the tree. The lexical "
                "check above cannot see that, so first-party imports would be "
                "type-checked against a tree this repo does not control (#1094)."
            )
    assert not findings, "pyrightconfig.json `extraPaths` problems:\n  " + "\n  ".join(
        findings
    )


def test_no_extra_path_is_stale() -> None:
    """An entry that is not a first-party package root is unexplained.

    `extraPaths` exists here for one purpose. An entry beyond that set either
    resolves something the repo has not declared it ships, or is left over from a
    distribution that moved — and in the second case the import it was added for is
    now silently unresolved again while this file stays green.
    """
    roots = {posixpath.normpath(r) for r in _first_party_package_roots().values()}
    stale = sorted(p for p in _extra_paths() if posixpath.normpath(p) not in roots)
    assert not stale, (
        f"pyrightconfig.json `extraPaths` lists {stale}, which no distribution under "
        f"{LIB_ROOT}/*/pyproject.toml ships from (roots: {sorted(roots)}). Drop the "
        "entry, or, if a new tree genuinely needs to be on the import path, say so "
        "in this test — an unexplained search path is how the next stale one hides."
    )


def _resolution_probe(extra_paths: list[str], modules: list[str]) -> list[str]:
    """Names basedpyright cannot resolve, given these search paths.

    A **live** measurement, not a restatement of the config. The config-level check
    above compares strings, and a string comparison is satisfied by an `extraPaths`
    entry that names the right directory while the package inside it has moved,
    been renamed, or lost its `__init__.py`. This actually asks the pinned
    basedpyright, with `reportMissingImports` forced to "error" in a throwaway
    project so the answer is reported rather than suppressed.

    Costs ~0.5s: one synthetic file, not the whole tree.
    """
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / "probe.py").write_text(
            "".join(f"import {name}\n" for name in sorted(modules))
        )
        (project / "pyrightconfig.json").write_text(
            json.dumps(
                {
                    "include": ["probe.py"],
                    # Absolute here, and only here: this config lives in a temp
                    # directory, so the relative entries the real one carries
                    # would resolve against the wrong root.
                    "extraPaths": [str(REPO_ROOT / p) for p in extra_paths],
                    "pythonVersion": _config().get("pythonVersion", "3.12"),
                    "typeCheckingMode": "basic",
                    "reportMissingImports": "error",
                    "reportMissingTypeStubs": "none",
                }
            )
        )
        result = subprocess.run(
            ["basedpyright", "--outputjson", "--project", str(project)],
            capture_output=True,
            text=True,
            cwd=project,
            env=_env_without_pythonpath(),
        )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:  # pragma: no cover - basedpyright not installed
        pytest.skip(f"basedpyright produced no JSON report: {result.stderr[:400]}")
    return sorted(
        diagnostic["message"]
        for diagnostic in report.get("generalDiagnostics", [])
        if diagnostic.get("rule") == "reportMissingImports"
    )


def test_the_configured_extra_paths_really_do_resolve_the_first_party_imports() -> None:
    """Ask basedpyright, not the JSON file.

    This is the check that would have failed before #1109 was fixed, and the one
    that keeps failing if `extraPaths` is present but wrong.
    """
    unresolved = _resolution_probe(_extra_paths(), sorted(_first_party_package_roots()))
    assert not unresolved, (
        "basedpyright cannot resolve first-party imports through the `extraPaths` in "
        "pyrightconfig.json:\n  " + "\n  ".join(unresolved) + "\n\n"
        "Every call into an unresolved package is unchecked by `make typecheck`, and "
        '`reportMissingImports: "none"` means the run will not mention it — it will '
        "report 0 errors over the whole tree instead (#1109)."
    )


def _env_without_pythonpath() -> dict[str, str]:
    """The current environment with `PYTHONPATH` removed.

    Every basedpyright invocation below must run without one, and that is the point
    rather than tidiness. basedpyright *honours* `PYTHONPATH` — it is the whole
    mechanism behind #1109 — so a probe that inherits one measures the caller's
    shell instead of the configuration. Both directions were observed: the
    negative control below resolved all five packages and failed, and the positive
    check would have passed with `extraPaths` empty. A gate whose answer depends on
    how it was invoked is the defect being fixed, so the tests for it cannot have
    the same property.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _reported_search_path_blocks() -> list[list[Path]]:
    """Every ordered search-path list basedpyright reports, one per environment.

    `--verbose` prints one "Search paths:" block **per configured execution
    environment**, in config order, and the content does not depend on which file is
    passed. Reading only the first block is how an earlier version of this check was
    defeated: with two environments configured, the block it read belonged to the
    `feature-platform/pii-anonymizer/hook/vendor` entry rather than to the
    environment under which `src/`, `lib/`, `patterns/` and `scripts/` are actually
    analysed. It gave the right answer only because that environment happens to
    inherit the top-level `extraPaths` — so an `executionEnvironments` entry
    declaring its own `extraPaths` restored the whole #1109 condition for its
    subtree, measured at 2 errors to 0 on a planted call, with every test passing.

    Asked with a single file argument it costs ~0.8s while still loading the **real**
    `pyrightconfig.json`. `--verbose` and `--outputjson` are mutually exclusive,
    which is why this parses text.
    """
    probe_file = REPO_ROOT / "scripts" / "discover_model_limits.py"
    result = subprocess.run(
        ["basedpyright", "--verbose", str(probe_file)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
        env=_env_without_pythonpath(),
    )
    blocks: list[list[Path]] = []
    current: list[Path] | None = None
    for line in result.stdout.splitlines():
        if line.strip() == "Search paths:":
            current = []
            blocks.append(current)
            continue
        if current is not None:
            if not line.startswith("    ") or line.strip().endswith(":"):
                current = None
                continue
            current.append(Path(line.strip()))
    blocks = [block for block in blocks if block]
    if not blocks:  # pragma: no cover - basedpyright missing or output changed
        pytest.skip(
            "could not read any 'Search paths:' block from basedpyright --verbose; "
            f"stdout began {result.stdout[:200]!r}"
        )
    return blocks


def _first_provider(name: str, search_paths: list[Path]) -> Path | None:
    """The first search path that would satisfy `import <name>`, in order.

    A pure function over the path list so the non-vacuity test below can hand it a
    synthetic order with a foreign checkout in front, which is the arrangement that
    must fail and cannot be produced by editing this repo's config.
    """
    for parent in search_paths:
        for candidate in (
            parent / name / "__init__.py",
            parent / name / "__init__.pyi",
            parent / f"{name}.py",
            parent / f"{name}.pyi",
        ):
            if candidate.exists():
                return candidate
    return None


def test_first_party_imports_resolve_to_the_configured_package_roots() -> None:
    """Where they resolve, not merely that they resolve. The #1094 failure mode.

    Asserting resolution alone is satisfied by a foreign tree. This machine's
    environment resolves `idp_common` to a **sibling git worktree of this same
    repository** and `idp_sdk` to **another project** (#1094), and a sibling
    worktree is a near-copy at a different commit — so type-checking against it
    produces diagnostics that are confident and wrong, which is worse than the
    silence this change set removes and indistinguishable from a correct run.

    `exclude` cannot be the guard, because it bounds the **checked set** and not the
    **resolution path**: a tree `exclude` covers can still be imported from, with
    `filesAnalyzed` unchanged. Ordering is what settles it, and basedpyright puts
    `extraPaths` ahead of the interpreter's `site-packages`. This reads the order
    basedpyright reports rather than trusting that.

    Two things this asserts that a weaker form would not, both because a weaker form
    was measured to pass while the gate was blind:

    * **Every** reported block, not the first. See `_reported_search_path_blocks`:
      one block per execution environment, and the first belongs to the vendored
      subtree rather than to the environment that checks `src/` and `lib/`.
    * The provider must be one of the **configured `extraPaths`**, not merely
      somewhere inside `REPO_ROOT`. An `executionEnvironments` *root* is inserted
      **ahead of** `extraPaths` in the order, so an entry rooted at
      `feature-platform/idp-data-generator/idp_common_pkg` would make the stale
      staged copy that `exclude` covers the winning provider of `idp_common` for
      files under it — and that copy is inside the repository, so an
      `is_relative_to(REPO_ROOT)` test passes. Requiring the exact configured
      directory is what makes "inside the repo" mean "the library this repo ships".
      This is also the only one of the two resolution checks that sees that
      arrangement at all: it declares no `extraPaths`, so
      `test_no_execution_environment_declares_its_own_extra_paths` has nothing to
      look at and passes.

    ⚠️ **This is latent protection, not a currently exploitable hole — and saying so
    is the honest framing.** In the staged-copy arrangement above the planted wrong
    call still produces its error, because the shadowing root does not contain
    `src/lambda/`. The blindness would apply to files *under* the staged copy, and
    `exclude` already keeps those out of the checked set. So the useful property is
    that **this check fires on the dangerous arrangement before that arrangement can
    hide a diagnostic**, rather than that it is catching one today.

    The same distinction applies to the editable-install route. Measured on this
    machine: with the `lib/idp_common_pkg` entry removed, basedpyright resolves
    `idp_common` to **nothing** rather than to the sibling worktree, because the
    stale editable installs here are the modern `__editable___*_finder.py` kind and
    pyright cannot follow a `MetaPathFinder`. So that risk is live for `pytest` —
    which does follow it, and which `scripts/tests/test_first_party_provenance.py`
    covers — and not for this gate today. It is asserted anyway, because a
    `.pth`-style editable install (an older setuptools, or `setup.py develop`) puts a
    plain directory on `site-packages`' path and pyright does follow that.

    The message names the path resolution landed on, because the whole difficulty
    of #1094 is that the wrong answer looks exactly like the right one.
    """
    blocks = _reported_search_path_blocks()
    configured = {(REPO_ROOT / p).resolve() for p in _extra_paths()}
    findings: list[str] = []

    for index, search_paths in enumerate(blocks):
        for name in sorted(_first_party_package_roots()):
            provider = _first_provider(name, search_paths)
            if provider is None:
                findings.append(
                    f"environment {index}: {name} — no search path provides it"
                )
                continue
            try:
                providing_dir = provider.parent.parent.resolve()
            except OSError:  # pragma: no cover - unreadable path
                providing_dir = provider.parent.parent
            if providing_dir not in configured:
                findings.append(
                    f"environment {index}: {name} resolves to {provider}, served "
                    f"from {providing_dir}, which is not one of the configured "
                    f"`extraPaths` ({sorted(str(p) for p in configured)}). "
                    + (
                        "It is outside this repository entirely."
                        if not providing_dir.is_relative_to(REPO_ROOT)
                        else "It is inside the repository but is not the package "
                        "root this repo ships — a staged build copy, for instance."
                    )
                )

    assert not findings, (
        "basedpyright does not resolve first-party imports to this repository's own "
        "package roots:\n  "
        + "\n  ".join(findings)
        + "\n\n"
        + "\n\n".join(
            f"Environment {i} search paths, in resolution order:\n    "
            + "\n    ".join(str(p) for p in block)
            for i, block in enumerate(blocks)
        )
        + "\n\nA configured `extraPaths` entry must be the first thing providing each "
        "name, in every environment. Usual causes: an `executionEnvironments` entry "
        "with its own `extraPaths` or a root that shadows one, or a stale editable "
        "install left by `pip install -e` from another checkout (#1094)."
    )


def test_no_execution_environment_declares_its_own_extra_paths() -> None:
    """The config-level closure for the hole the search-path check exists to catch.

    One `executionEnvironments` entry is enough to reopen #1109 for a whole subtree:

        {"root": "src/lambda", "extraPaths": ["/tmp/foreign-idp"]}

    Measured — no severity change, no change to the top-level `extraPaths`: a planted
    wrong call into `idp_common` under `src/lambda` went from 2 errors to **0**,
    invisible again, with the entire rest of this module passing.

    This is asserted here as well as through the reported search paths because it is
    the cheap, total and order-independent form of the same statement. There is one
    entry today and it declares no `extraPaths`; if a per-root search path is ever
    genuinely needed, the top-level key is where it belongs, so that the check above
    governs it.
    """
    offenders = {
        env.get("root", "<no root>"): env["extraPaths"]
        for env in _execution_environments(_config())
        if "extraPaths" in env
    }
    assert not offenders, (
        f"pyrightconfig.json `executionEnvironments` declares per-root `extraPaths`: "
        f"{offenders}.\n\n"
        "A per-root `extraPaths` replaces the import path for that subtree, so it can "
        "point first-party imports at any tree at all while every severity and the "
        "top-level `extraPaths` stay correct — measured to take a planted wrong call "
        "from 2 errors to 0 (#1109). Put the path in the top-level `extraPaths`, "
        "where test_first_party_imports_resolve_to_the_configured_package_roots "
        "checks it."
    )


def test_resolution_outside_the_repository_is_detected() -> None:
    """Non-vacuity for the check above, driven the only way it can be.

    The failing arrangement cannot be produced by editing `pyrightconfig.json`,
    because `test_every_extra_path_is_relative_and_inside_this_repository` rejects
    an absolute entry before it gets that far. So `_first_provider` is given a
    synthetic order with a foreign directory in front — and the foreign directory
    used is a real one: `/home/ec2-user/projects/idp3`, the checkout #1094 found
    `idp_sdk` resolving to. If it is not present, a fabricated stand-in is used, so
    this test measures the ordering logic either way.
    """
    with tempfile.TemporaryDirectory() as tmp:
        foreign = Path(tmp) / "someone-elses-checkout"
        (foreign / "idp_common").mkdir(parents=True)
        (foreign / "idp_common" / "__init__.py").write_text("")

        in_repo = REPO_ROOT / "lib" / "idp_common_pkg"
        assert (in_repo / "idp_common" / "__init__.py").exists(), (
            "the in-repo control is missing, so neither ordering below means anything."
        )

        foreign_first = _first_provider("idp_common", [foreign, in_repo])
        repo_first = _first_provider("idp_common", [in_repo, foreign])

    assert foreign_first is not None and not foreign_first.is_relative_to(REPO_ROOT), (
        f"_first_provider returned {foreign_first} for a search order whose first "
        "entry is a foreign checkout. It is not reading the order, so the test "
        "above would pass with resolution pointing anywhere."
    )
    assert repo_first is not None and repo_first.is_relative_to(REPO_ROOT), (
        f"_first_provider returned {repo_first} when the in-repo path comes first. "
        "The control for the case above."
    )


def test_the_resolution_checks_ignore_the_callers_pythonpath(monkeypatch) -> None:
    """The checks above must answer about the config, not about the shell.

    Directly measured, not reasoned about: with `PYTHONPATH` exported to this
    repository's `lib/*` roots — which `scripts/run_all_tests.py` and anyone
    following the #1094 guidance does — the negative control resolved all five
    packages and failed. The positive checks had the mirror-image flaw: they would
    have passed over an empty `extraPaths`, which is the state this whole change set
    exists to reject.

    So `PYTHONPATH` is stripped from every basedpyright subprocess, and this pins
    it. A `dict(os.environ)` copied without the `pop` would reintroduce the
    dependency invisibly.
    """
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT / "lib" / "idp_common_pkg"))
    assert "PYTHONPATH" not in _env_without_pythonpath()

    # With a PYTHONPATH that would resolve everything, an empty `extraPaths` must
    # still be reported as resolving nothing.
    unresolved = _resolution_probe([], sorted(_first_party_package_roots()))
    assert len(unresolved) == len(_first_party_package_roots()), (
        f"with PYTHONPATH set and no extraPaths, only {len(unresolved)} of "
        f"{len(_first_party_package_roots())} first-party imports were reported "
        f"unresolvable: {unresolved}. The probe is inheriting the caller's "
        "PYTHONPATH, so it measures the environment rather than the configuration — "
        "the exact property #1109 is about."
    )

    # And the search-path reader must report the same order either way: every path
    # it lists has to come from the config, never from the environment.
    configured = {REPO_ROOT / p for p in _extra_paths()}
    from_environment = [
        path
        for block in _reported_search_path_blocks()
        for path in block
        if path.is_relative_to(REPO_ROOT / "lib") and path not in configured
    ]
    assert not from_environment, (
        f"basedpyright's search paths include {from_environment}, which "
        "`extraPaths` does not name — so they arrived from the exported PYTHONPATH. "
        "test_first_party_imports_resolve_inside_this_repository would then pass on "
        "this machine and fail in CI."
    )


def test_the_resolution_probe_reports_an_unresolvable_package() -> None:
    """Non-vacuity of the probe itself.

    A `_resolution_probe` that returned `[]` unconditionally — a renamed rule, a
    changed JSON shape, a basedpyright flag that stopped meaning what it meant —
    would satisfy the test above forever while resolution was broken. Measured with
    no search paths: all five first-party imports are reported.
    """
    unresolved = _resolution_probe([], sorted(_first_party_package_roots()))
    assert len(unresolved) == len(_first_party_package_roots()), (
        "with no `extraPaths` at all, basedpyright reported "
        f"{len(unresolved)} unresolvable first-party import(s) out of "
        f"{len(_first_party_package_roots())}: {unresolved}. The probe is not "
        "measuring what the test above relies on it measuring."
    )


# --------------------------------------------------------------------------- #
# Severity ratchet
#
# Every check above answers "is this file read by the gate?". None of them answer
# "can a finding in it fail the gate?", and that is a separate setting. With
# `reportCallIssue` and `reportReturnType` at "warning", `make typecheck` exited 0
# over 53 diagnostics in tracked first-party code, six of which were `idp_sdk`
# result constructors that raised `TypeError` on every input — each wrapped in a
# broad `except Exception` that reported them as plausible processing errors.
# Nothing read the type checker's answer because nothing had to.
#
# So the rules get the same treatment `include` got: the property is asserted and
# the judgement is authored. `_severity_findings` derives the rule universe from
# the config and fails if any rule sits in neither an enforced set nor a
# registered carve-out, which is what makes the lists below trustworthy —
# a fifteenth rule quietly added at "none" is a failure, not an omission.
#
# A severity can be weakened four ways, and only the obvious one is "change the
# value". The other three are each measured against basedpyright rather than
# assumed:
#
#   * `typeCheckingMode` is the only thing that sets the ~100 `report*` rules this
#     config never names, several of them error-class. With `"off"`, a file whose
#     sole defect is `reportIndexIssue` goes from 1 error to 0 while every rule
#     named below keeps its severity. So the mode is pinned too.
#   * A **boolean** is accepted in place of a severity: `"reportIndexIssue": false`
#     takes the same file from 1 error to 0. Booleans are rejected outright rather
#     than mapped, because `true` has no unambiguous severity and a `false` is a
#     silencing that no registry entry would be asked for.
#   * An `executionEnvironments` entry can relax a rule for a **path**, and a root
#     of `.` relaxes it for the whole tree. That defeats the enforced-error
#     guarantee through scope rather than through severity, so the root is bounded
#     and the rules it may relax are pinned.
#   * A rule can be **deleted** rather than downgraded. `reportImportCycles` and
#     `reportDuplicateImport` default to "none" in basic mode, so dropping either
#     line silences it. Both are named in `ENFORCED_WARNING_RULES`, which fails on
#     absence.
# --------------------------------------------------------------------------- #

#: Rules pinned to "error". A downgrade here has to be a visible edit to this set,
#: not a one-word change in a JSON file nothing reads.
#:
#: `reportUndefinedVariable` was already an error. `reportCallIssue` and
#: `reportReturnType` are errors as of the change that added this block: the tree
#: was brought to zero of both first, so promoting them red-lined nothing.
ENFORCED_ERROR_RULES: frozenset[str] = frozenset(
    {
        "reportUndefinedVariable",
        "reportCallIssue",
        "reportReturnType",
    }
)

#: Rules pinned to "warning": reported on every run, not gate-failing.
#:
#: Named here for one reason — both default to "none" under
#: `typeCheckingMode: "basic"`, so **deleting** either line silences the rule as
#: effectively as setting it to "none" would, and a deletion is what the
#: universe-closure check below cannot see (a rule the config does not mention is
#: not in its universe). Absence is therefore a failure.
ENFORCED_WARNING_RULES: frozenset[str] = frozenset(
    {
        "reportImportCycles",
        "reportDuplicateImport",
    }
)

#: The `typeCheckingMode` this config's rule set was chosen against.
#:
#: Pinned because it is the only setting that governs the roughly 100 `report*`
#: rules named nowhere in the file, and several of those are error-class. Lowering
#: it to "off" or "standard" silences or shifts all of them while every explicit
#: rule below keeps the severity it is written with, so nothing else here would
#: notice. Raising it is a deliberate change with a large diff and belongs in its
#: own commit.
ENFORCED_TYPE_CHECKING_MODE = "basic"

#: Rules pinned to "none" repo-wide, one reason per rule.
#:
#: These are the honest residuals of `typeCheckingMode: "basic"` over a tree with
#: no third-party stubs. Every one of them was measured to be unusable at
#: "warning" or above because of how this codebase reaches AWS — `boto3` clients
#: are untyped factories, so `reportUnknown*` and `reportAttributeAccessIssue`
#: fire on nearly every service call — rather than because the findings are
#: uninteresting. Each entry is a property of that rule, not of the set: a reason
#: shared by fourteen rules would be a reason for none of them.
#:
#: Paying one down means fixing its findings and deleting its entry here, in that
#: order. Adding one means writing the sentence, which is the point.
DISABLED_RULE_EXEMPTIONS: dict[str, str] = {
    "reportMissingImports": (
        "Lambda source trees import their bundled dependencies, which are not "
        "installed in the checkout, so this fires on correct code in every "
        "handler. Also load-bearing for the other rules: with imports "
        "unresolved, promoting it would bury the ones that find real defects."
    ),
    "reportMissingTypeStubs": (
        "No third-party stubs are vendored and none are installed by the lint "
        "environment, so this reports the absence of a package this repo has "
        "made no commitment to ship."
    ),
    "reportUnusedImport": (
        "ruff's F401 already covers this, per file and with an autofix; a second "
        "opinion on the same finding in a gate that cannot fix it is noise."
    ),
    "reportUnusedVariable": (
        "ruff's F841 covers it per file and with an autofix, and pyright's version "
        "additionally flags the deliberately-unused binding this tree uses in "
        "tuple unpacking (`result, _ = structured_output(...)`), which F841 "
        "exempts by name."
    ),
    "reportGeneralTypeIssues": (
        "A catch-all bucket for diagnostics that have no rule of their own, so "
        "its findings cannot be triaged as a class or ratcheted per rule."
    ),
    "reportOptionalCall": (
        "Optional callables here are lazily-initialised module handles assigned "
        "once at import and called thereafter; pyright cannot see the assignment "
        "order across a Lambda module's import-time setup."
    ),
    "reportOptionalMemberAccess": (
        "boto3 response dicts are typed as returning Optional members, so this "
        "fires on every `response['X']['Y']` chain against an AWS API that "
        "documents the key as always present."
    ),
    "reportOptionalSubscript": (
        "Same shape as reportOptionalMemberAccess, one syntax along: subscripting "
        "a boto3 response member that the service contract guarantees."
    ),
    "reportPrivateImportUsage": (
        "pydantic and strands re-export from private submodules without "
        "declaring them in `__all__`, so importing their documented public names "
        "reads as private access."
    ),
    "reportUnknownMemberType": (
        "boto3's `client()` is an untyped factory, so every attribute of every "
        "service client is Unknown. This fires once per AWS call in the tree."
    ),
    "reportUnknownArgumentType": (
        "The argument side of the same untyped-boto3 problem: values read out of "
        "an Unknown response and passed onward."
    ),
    "reportUnknownVariableType": (
        "The assignment side of the same untyped-boto3 problem: a local bound to a "
        "value read out of an Unknown response, which is most locals in the "
        "Lambda handlers and every `_core` processor in the SDK."
    ),
    "reportArgumentType": (
        "Left off while the Unknown* rules above are off: with boto3 values "
        "typed Unknown, this rule's true positives are indistinguishable from "
        "the Unknowns flowing into every call. Paying down the boto3 typing is "
        "the prerequisite, not a wider exemption."
    ),
    "reportAttributeAccessIssue": (
        "Fires on attributes of untyped boto3 clients and resources, the same "
        "root cause as the Unknown* rules."
    ),
}

#: Directory roots where `executionEnvironments` softens a rule below its repo-wide
#: severity: the rules it may soften, and why that root.
#:
#: `rules` is the pin that bounds the relaxation's breadth — a third rule added to
#: the same root is a failure, not an extension of an approved carve-out. The root
#: itself is bounded separately: a relaxation is required to name a strict
#: descendant of an `include` entry, because `{"root": "."}` relaxes a rule for the
#: entire tree and would defeat `ENFORCED_ERROR_RULES` through scope while every
#: severity in the file still reads "error".
#:
#: A relaxation is narrower than an `exclude` in two further ways worth stating:
#: the files are still analysed, and the softened rules are still *reported*, at
#: "warning". `test_a_relaxed_rule_is_still_reported` holds that second property —
#: relaxing to "none" here would be an exclusion wearing a severity's clothing, and
#: would not show up in `DISABLED_RULE_EXEMPTIONS` either.
VENDORED_SEVERITY_EXEMPTIONS: dict[str, dict] = {
    "feature-platform/pii-anonymizer/hook/vendor": {
        "rules": frozenset({"reportCallIssue", "reportReturnType"}),
        "reason": (
            "A vendored third-party tree, re-copied file-for-file from upstream by "
            "its own resync script against the commit pinned in PROVENANCE.md. An "
            "inline `# pyright: ignore` written here would be silently dropped by "
            "the next resync, and correcting an upstream project's annotations in "
            "a vendored copy puts this repo's fix and upstream's source in "
            "conflict. The 5 diagnostics are upstream's to fix; `ruff.toml` carves "
            "the same tree out of lint and format for the same reason."
        ),
    },
}

#: Severities a rule may hold in this config. A value outside this set is a typo
#: that pyright accepts by falling back to its default, which is how a rule
#: silently stops meaning what the file says.
_VALID_SEVERITIES = frozenset({"none", "information", "warning", "error"})

#: Weakest-to-strongest, for deciding whether a per-root setting is a relaxation.
_SEVERITY_ORDER = {"none": 0, "information": 1, "warning": 2, "error": 3}


def _rule_settings(config: dict) -> dict[str, object]:
    """Every `report*` key in a config, whatever type its value is.

    Deliberately not filtered to `str`: basedpyright also accepts a **boolean**,
    and `"reportIndexIssue": false` silences the rule. Filtering booleans out here
    would drop them from the universe the closure check derives, so a rule could
    be turned off in a form no registry entry is ever asked for.
    """
    return {key: value for key, value in config.items() if key.startswith("report")}


def _rule_severities(config: dict) -> dict[str, str]:
    """The `report*` keys whose value is a severity string."""
    return {
        key: value
        for key, value in _rule_settings(config).items()
        if isinstance(value, str)
    }


def _severity_findings(config: dict) -> list[str]:
    """Everything wrong with a config's rule severities, as reader-facing lines.

    A pure function over a parsed config so the tests below can assert it
    *rejects* a bad one. An assertion nobody has watched fail is a guess about
    what it checks — which is the failure this whole file is about, one level up.
    """
    findings: list[str] = []
    severities = _rule_severities(config)

    mode = config.get("typeCheckingMode")
    if mode != ENFORCED_TYPE_CHECKING_MODE:
        findings.append(
            f"typeCheckingMode is {mode!r}, not {ENFORCED_TYPE_CHECKING_MODE!r}. It "
            "is the only setting governing the ~100 `report*` rules this config "
            "never names, several of them error-class, so lowering it silences them "
            "while every rule written here keeps its severity — measured: a file "
            "whose one defect is reportIndexIssue goes from 1 error to 0 under "
            '"off". Change ENFORCED_TYPE_CHECKING_MODE deliberately if the mode is '
            "genuinely moving."
        )

    for rule, value in sorted(_rule_settings(config).items(), key=lambda kv: kv[0]):
        if isinstance(value, bool):
            findings.append(
                f"{rule} is set to the boolean {value!r} rather than a severity "
                "string. basedpyright accepts it — `false` silences the rule as "
                'completely as "none" does — but it carries no severity a reader '
                "or this gate can act on. Spell the severity: "
                f"{sorted(_VALID_SEVERITIES)}."
            )
        elif not isinstance(value, str):
            findings.append(
                f"{rule} is set to {value!r}, which is neither a severity string "
                "nor a boolean. pyright ignores it and falls back to the mode "
                "default, so this reads as a setting and is not one."
            )

    for rule, severity in sorted(severities.items()):
        if severity not in _VALID_SEVERITIES:
            findings.append(
                f"{rule} is set to {severity!r}, which is not one of "
                f"{sorted(_VALID_SEVERITIES)}. pyright falls back to its default "
                "for an unrecognised value, so this reads as a setting and is not one."
            )

    for rule in sorted(ENFORCED_WARNING_RULES):
        actual = severities.get(rule)
        if actual is None:
            findings.append(
                f"{rule} is in ENFORCED_WARNING_RULES but pyrightconfig.json no "
                'longer sets it. It defaults to "none" under '
                f"{ENFORCED_TYPE_CHECKING_MODE!r}, so deleting the line silences the "
                'rule exactly as setting it to "none" would — and a rule the config '
                "does not mention is outside the universe the closure check below "
                "derives, so nothing else here would see it."
            )
        elif _SEVERITY_ORDER.get(actual, 3) < _SEVERITY_ORDER["warning"]:
            findings.append(
                f"{rule} is pinned to at least 'warning' by ENFORCED_WARNING_RULES "
                f"but pyrightconfig.json sets it to {actual!r}."
            )

    for rule in sorted(ENFORCED_ERROR_RULES):
        actual = severities.get(rule)
        if actual is None:
            findings.append(
                f"{rule} is in ENFORCED_ERROR_RULES but pyrightconfig.json no longer "
                "sets it. Either re-pin it to 'error' or drop it from that set — "
                "leaving it implicit makes the severity depend on typeCheckingMode."
            )
        elif actual != "error":
            findings.append(
                f"{rule} is pinned to 'error' by ENFORCED_ERROR_RULES but "
                f"pyrightconfig.json sets it to {actual!r}. Below 'error' the gate "
                "exits 0 over its findings, which is how 53 diagnostics in tracked "
                "code — including six constructors that raised on every input — went "
                "unread. Fix the findings rather than the severity."
            )

    # Universe closure: a rule that is neither enforced nor registered has no
    # recorded decision behind it, whichever direction it drifted from.
    for rule, severity in sorted(severities.items()):
        if rule in ENFORCED_ERROR_RULES or rule in ENFORCED_WARNING_RULES:
            if rule in DISABLED_RULE_EXEMPTIONS:
                findings.append(
                    f"{rule} is in both ENFORCED_ERROR_RULES and "
                    "DISABLED_RULE_EXEMPTIONS. One of the two is stale."
                )
            continue
        if severity == "none" and rule not in DISABLED_RULE_EXEMPTIONS:
            findings.append(
                f"{rule} is turned off repo-wide with no entry in "
                "DISABLED_RULE_EXEMPTIONS. Turning a rule off is allowed; doing it "
                "without a sentence saying what is consequently unchecked is what "
                "lets the set grow one silent rule at a time. Add the entry, or "
                "raise the rule to 'warning' and leave it enabled."
            )
        if severity != "none" and rule in DISABLED_RULE_EXEMPTIONS:
            findings.append(
                f"{rule} has a DISABLED_RULE_EXEMPTIONS entry but is set to "
                f"{severity!r}, not 'none'. Delete the entry — a stale one reads as "
                "a live decision and hides the next real carve-out."
            )

    for rule in sorted(set(DISABLED_RULE_EXEMPTIONS) - set(severities)):
        findings.append(
            f"DISABLED_RULE_EXEMPTIONS names {rule}, which pyrightconfig.json does "
            "not set at all. Drop the entry."
        )

    return findings


def test_rule_severities_are_registered_and_not_downgraded() -> None:
    """The severity half of the gate, both directions."""
    findings = _severity_findings(_config())
    assert not findings, "pyrightconfig.json severity problems:\n  " + "\n  ".join(
        findings
    )


def test_the_rule_count_is_pinned_to_the_three_authored_lists() -> None:
    """Anti-vacuity, and a count pin that needs no magic number.

    The expected total is *derived* from the three lists above rather than
    written here, so it cannot go stale: a rule added to the config needs a place
    in one of them, and a rule deleted from the config leaves its list member
    dangling. A loose `>= 15` bound let `reportImportCycles` be deleted outright
    with every test green.
    """
    settings = _rule_settings(_config())
    expected = (
        set(ENFORCED_ERROR_RULES)
        | set(ENFORCED_WARNING_RULES)
        | set(DISABLED_RULE_EXEMPTIONS)
    )

    unaccounted = sorted(set(settings) - expected)
    missing = sorted(expected - set(settings))

    assert not unaccounted, (
        f"pyrightconfig.json sets {unaccounted}, which appear in none of "
        "ENFORCED_ERROR_RULES, ENFORCED_WARNING_RULES or DISABLED_RULE_EXEMPTIONS. "
        "Every rule the config names carries a decision; put each in the list that "
        "records it."
    )
    assert not missing, (
        f"{missing} are named in this file's enforced/disabled lists but "
        "pyrightconfig.json does not set them. A deleted line is a silenced rule "
        "for anything that defaults to 'none', so this is a weakening and not a "
        "tidy-up."
    )
    assert len(expected) >= 15, (
        f"only {len(expected)} rules are accounted for ({sorted(expected)}). Either "
        "the config was gutted or the derivation in _rule_settings() is stale; the "
        "severity checks would be vacuous either way."
    )


@pytest.mark.parametrize("rule", sorted(DISABLED_RULE_EXEMPTIONS))
def test_a_disabled_rule_has_a_substantive_reason(rule: str) -> None:
    """One member's worth of reason, not a shared gesture at it.

    A reason that could be pasted onto any of the fourteen is the defect this
    repo's exemption registry exists to catch: one justification attached to a
    set, where the justification is a property of individual members.
    """
    reason = DISABLED_RULE_EXEMPTIONS[rule]
    assert len(reason) >= 80, (
        f"{rule}'s reason is {len(reason)} characters. Say what this rule reports "
        "in THIS tree and why that is not actionable — not that it is noisy."
    )
    others = [r for r in DISABLED_RULE_EXEMPTIONS if r != rule]
    assert not any(DISABLED_RULE_EXEMPTIONS[other] == reason for other in others), (
        f"{rule}'s reason is character-for-character another rule's. If the reason "
        "really is shared, it is a reason about the tree and belongs in the block "
        "comment above; the per-rule entry has to say what this rule stops catching."
    )


def _execution_environments(config: dict) -> list[dict]:
    envs = config.get("executionEnvironments", [])
    return [env for env in envs if isinstance(env, dict)]


def _relaxations(config: dict) -> dict[str, dict[str, str]]:
    """Per-root rule settings that are *weaker* than the repo-wide severity.

    A boolean `false` counts as a relaxation to "none": it is how a rule gets
    silenced for a path without any severity string appearing in the file.
    """
    repo_wide = _rule_severities(config)
    out: dict[str, dict[str, str]] = {}
    for env in _execution_environments(config):
        root = env.get("root")
        if not isinstance(root, str):
            continue
        weaker: dict[str, str] = {}
        for rule, value in _rule_settings(env).items():
            severity = "none" if value is False else value
            if not isinstance(severity, str):
                continue
            here = _SEVERITY_ORDER.get(severity, 3)
            there = _SEVERITY_ORDER.get(repo_wide.get(rule, "error"), 3)
            if here < there:
                weaker[rule] = severity
        if weaker:
            out[root] = weaker
    return out


def _relaxation_scope_findings(config: dict) -> list[str]:
    """Roots whose breadth defeats the point of `ENFORCED_ERROR_RULES`.

    A relaxation is a *narrowing* device, but nothing in pyright stops
    `{"root": "."}`, which relaxes the rule for the whole tree and takes the run
    back to exit 0 while every severity in the file still reads "error". So a root
    is required to be a **strict descendant** of an `include` entry: inside the
    gate's scope, and smaller than it.

    The root is canonicalised with `posixpath.normpath` before either comparison,
    and that is load-bearing rather than tidiness. Comparing the raw string lets
    `"lib/.."` through — it is not `"."`, it is not an `include` entry, and it
    starts with `"lib/"` — while denoting the repository root, which was measured
    to downgrade a `reportCallIssue` in a file nowhere near `lib/` from error to
    warning, exactly as `{"root": "."}` does. `"lib/./"` reaches `lib` the same
    way. `normpath` is lexical (`"lib/.."` -> `"."`, `"lib/./"` -> `"lib"`), which
    is what is wanted: pyright resolves these relative to the config file, and a
    symlink-following `resolve()` would make the verdict depend on the checkout.
    """
    includes = [posixpath.normpath(inc) for inc in _include_paths()]
    findings: list[str] = []
    for root in sorted(_relaxations(config)):
        normalised = posixpath.normpath(root)
        if normalised in {"", ".", "/"} or normalised in includes:
            findings.append(
                f"executionEnvironments root {root!r} covers a whole `include` entry "
                "(or the entire tree). A relaxation at that breadth defeats "
                "ENFORCED_ERROR_RULES through scope while every severity in the file "
                "still reads 'error'. Name the specific subtree instead."
            )
            continue
        if not any(
            normalised.startswith(inc + "/") for inc in includes if inc not in {"", "."}
        ):
            findings.append(
                f"executionEnvironments root {root!r} is not inside any `include` "
                f"entry ({includes}). Either it relaxes a rule for files the gate "
                "does not read — dead configuration — or `include` has moved."
            )
    return findings


def test_every_per_root_relaxation_is_registered() -> None:
    """`executionEnvironments` is a second way to soften a rule, so it is a second
    exemption surface — and it is invisible to the repo-wide check above."""
    relaxed = _relaxations(_config())
    unregistered = sorted(set(relaxed) - set(VENDORED_SEVERITY_EXEMPTIONS))
    assert not unregistered, (
        f"pyrightconfig.json softens diagnostic rules for {unregistered} via "
        "`executionEnvironments` with no entry in VENDORED_SEVERITY_EXEMPTIONS. "
        "Per-root softening does not appear in the repo-wide severity block, so "
        "nothing else in this file would notice it."
    )


def test_no_relaxation_is_broad_enough_to_defeat_the_enforced_rules() -> None:
    """The scope bound, which is what stops one registry line from disarming the
    error severities for the entire tree."""
    findings = _relaxation_scope_findings(_config())
    assert not findings, "executionEnvironments scope problems:\n  " + "\n  ".join(
        findings
    )


@pytest.mark.parametrize("root", sorted(VENDORED_SEVERITY_EXEMPTIONS))
def test_a_registered_relaxation_still_shields_something(root: str) -> None:
    """Non-vacuity and staleness: the root must exist, hold Python, and still be
    the subject of a relaxation in the config."""
    relaxed = _relaxations(_config())
    assert root in relaxed, (
        f"VENDORED_SEVERITY_EXEMPTIONS names {root!r}, which pyrightconfig.json no "
        "longer relaxes any rule for. Drop the entry — a stale exemption reads as a "
        "live decision and pre-exempts whatever next occupies the path."
    )
    target = REPO_ROOT / root
    assert target.is_dir(), (
        f"VENDORED_SEVERITY_EXEMPTIONS names {root!r}, which is not a directory. "
        "A relaxation scoped to a path nobody can find shields nothing."
    )
    assert next(target.rglob("*.py"), None) is not None, (
        f"{root!r} holds no *.py files, so relaxing a diagnostic rule for it "
        "shields nothing and is dead configuration."
    )


@pytest.mark.parametrize("root", sorted(VENDORED_SEVERITY_EXEMPTIONS))
def test_a_relaxation_only_covers_the_rules_it_was_approved_for(root: str) -> None:
    """The breadth pin. A third rule at an approved root is a new decision.

    Without this, one registry line approved for two rules silently covers every
    rule anybody later adds to the same `executionEnvironments` entry — the
    "count-pinned" ratchet, applied to rules rather than to sites.
    """
    entry = VENDORED_SEVERITY_EXEMPTIONS[root]
    approved = entry["rules"]
    actual = frozenset(_relaxations(_config()).get(root, {}))

    assert actual <= approved, (
        f"{root!r} now relaxes {sorted(actual - approved)}, which "
        "VENDORED_SEVERITY_EXEMPTIONS does not approve for it. Widening a carve-out "
        "is a new decision: add the rule to that entry's `rules` and say in the "
        "reason why this root cannot satisfy it."
    )
    assert actual == approved, (
        f"{root!r} is approved to relax {sorted(approved - actual)} but no longer "
        "does. Drop the rule from the entry — a stale approval pre-exempts the next "
        "finding of that rule in this tree."
    )


@pytest.mark.parametrize("root", sorted(VENDORED_SEVERITY_EXEMPTIONS))
def test_a_registered_relaxation_has_a_substantive_reason(root: str) -> None:
    """Same standard as `DISABLED_RULE_EXEMPTIONS`: a reason about this member.

    The list without this check accepted an empty string, so a root could be
    approved by adding one blank line to a dict.
    """
    reason = VENDORED_SEVERITY_EXEMPTIONS[root].get("reason", "")
    assert isinstance(reason, str) and len(reason.strip()) >= 80, (
        f"{root!r} has a {len(reason.strip())}-character reason. Say what is true of "
        "THIS path that makes the rule unfixable here — not that it is noisy. A root "
        "this gate cannot check is approved on the strength of that sentence alone."
    )


@pytest.mark.parametrize("root", sorted(VENDORED_SEVERITY_EXEMPTIONS))
def test_a_relaxed_rule_is_still_reported(root: str) -> None:
    """A per-root relaxation may soften a rule, never silence it.

    "none" through this mechanism is an exclusion wearing a severity's clothing:
    the files stay in `filesAnalyzed` so every path check in this file still
    passes, and the rule is not in `DISABLED_RULE_EXEMPTIONS` either, so neither
    half of the gate would report it.
    """
    silenced = sorted(
        rule
        for rule, severity in _relaxations(_config()).get(root, {}).items()
        if severity == "none"
    )
    assert not silenced, (
        f"pyrightconfig.json sets {silenced} to 'none' for {root!r}. Relax to "
        "'warning' so the diagnostics are still printed, or, if the tree genuinely "
        "must leave the gate, add it to `exclude` and TYPECHECK_SCOPE_EXCLUSIONS "
        "where the coverage checks in this file can see it."
    )


#: Sentinel meaning "remove this key" in a mutation below.
_DELETE = object()

#: Synthetic configs the severity check MUST reject, each named for what it is.
#: An assertion whose failure nobody has observed is a guess about what it checks.
#:
#: The last four are ways of weakening the gate that do NOT change a severity
#: string, and every one of them passed the first version of this block:
#: lowering the mode silences the ~100 unnamed rules, a boolean silences a named
#: one, and a deletion silences anything that defaults to "none".
_REJECTABLE_CONFIGS: dict[str, dict] = {
    "an enforced rule downgraded to warning": {"reportCallIssue": "warning"},
    "an enforced rule downgraded to none": {"reportReturnType": "none"},
    "an enforced rule dropped entirely": {"reportCallIssue": _DELETE},
    "a fifteenth rule turned off with no reason": {"reportIndexIssue": "none"},
    "a rule set to a value pyright does not know": {"reportCallIssue": "off"},
    "typeCheckingMode lowered to off": {"typeCheckingMode": "off"},
    "typeCheckingMode dropped entirely": {"typeCheckingMode": _DELETE},
    "a rule silenced with a boolean instead of a severity": {
        "reportAssignmentType": False
    },
    "a rule enabled with a boolean instead of a severity": {"reportIndexIssue": True},
    "a warning-pinned rule deleted rather than downgraded": {
        "reportImportCycles": _DELETE
    },
    "a warning-pinned rule downgraded to none": {"reportDuplicateImport": "none"},
}

#: Synthetic configs the *scope* check must reject. Separate from the list above
#: because `executionEnvironments` weakens the gate without touching any repo-wide
#: severity, so `_severity_findings` is the wrong function to ask.
_REJECTABLE_SCOPES: dict[str, list] = {
    "a relaxation rooted at the repo root": [
        {"root": ".", "reportCallIssue": "warning"}
    ],
    "a relaxation rooted at an include entry": [
        {"root": "lib", "reportCallIssue": "warning"}
    ],
    "a relaxation rooted outside every include entry": [
        {"root": "no-such-tree/here", "reportCallIssue": "warning"}
    ],
    "a relaxation that silences rather than softens, via a boolean": [
        {"root": "lib/idp_sdk/idp_sdk", "reportCallIssue": False}
    ],
    # The two forms that reach the repo root, or an include entry, without
    # spelling either — the reason `_relaxation_scope_findings` canonicalises.
    # Measured: "lib/.." downgrades a reportCallIssue in a file under `src/` from
    # error to warning, identically to a root of ".".
    "a relaxation reaching the repo root through ..": [
        {"root": "lib/..", "reportCallIssue": "warning"}
    ],
    "a relaxation reaching an include entry through . and a trailing slash": [
        {"root": "lib/./", "reportCallIssue": "warning"}
    ],
    "a relaxation reaching an include entry through a deeper ..": [
        {"root": "lib/idp_sdk/..", "reportCallIssue": "warning"}
    ],
}


@pytest.mark.parametrize("case", sorted(_REJECTABLE_CONFIGS))
def test_the_severity_check_rejects_a_weakened_config(case: str) -> None:
    """Non-vacuity of the check itself, applied to the live config.

    Each case is the real `pyrightconfig.json` with one thing changed, so a
    refactor that makes `_severity_findings` silently return `[]` fails here
    rather than passing everywhere.
    """
    config = dict(_config())
    for rule, severity in _REJECTABLE_CONFIGS[case].items():
        if severity is _DELETE:
            config.pop(rule, None)
        else:
            config[rule] = severity

    assert _severity_findings(config), (
        f"_severity_findings() accepted a config with {case}. The checks above are "
        "then vacuous: they would pass over a gate that cannot fail."
    )


def test_the_severity_check_accepts_the_live_config() -> None:
    """The control for the case above: unmodified, the live config is clean.

    Without this, a `_severity_findings` that returned a finding for *every*
    config would satisfy every rejection case and prove nothing.
    """
    assert _severity_findings(_config()) == []


@pytest.mark.parametrize("case", sorted(_REJECTABLE_SCOPES))
def test_the_scope_check_rejects_a_broadened_relaxation(case: str) -> None:
    """Non-vacuity for the scope half, which `_severity_findings` cannot see.

    Each case leaves every repo-wide severity at "error" and weakens the gate
    purely through `executionEnvironments`. A root of `.` was measured to take the
    whole tree back to exit 0 with 66 tests still green.
    """
    config = dict(_config())
    config["executionEnvironments"] = _REJECTABLE_SCOPES[case]

    assert _severity_findings(config) == [], (
        f"{case} was caught by _severity_findings(), so this case is not exercising "
        "the scope check it was written for. Pick a mutation that leaves every "
        "repo-wide severity intact."
    )
    silenced = [
        rule
        for rules in _relaxations(config).values()
        for rule, severity in rules.items()
        if severity == "none"
    ]
    assert _relaxation_scope_findings(config) or silenced, (
        f"nothing rejected a config with {case}. The scope checks are then vacuous: "
        "one line in VENDORED_SEVERITY_EXEMPTIONS would disarm ENFORCED_ERROR_RULES "
        "for whatever path it named."
    )


def test_the_scope_check_accepts_the_live_config() -> None:
    """Control for the scope cases, same reason as the severity control."""
    assert _relaxation_scope_findings(_config()) == []
