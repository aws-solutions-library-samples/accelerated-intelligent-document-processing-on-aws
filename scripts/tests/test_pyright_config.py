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

`typeCheckingMode` (currently "basic") is still not asserted: it selects which
rules get a default at all rather than overriding the explicit ones below, so
pinning it would restate the rule list from a second direction.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from fnmatch import fnmatch
from pathlib import Path

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
# the config and fails if any rule sits in neither the enforced set nor a
# registered carve-out, which is what makes the two lists below trustworthy —
# a fourteenth rule quietly added at "none" is a failure, not an omission.
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
#: severity, with the reason for that root.
#:
#: A relaxation here is narrower than an `exclude` in two ways worth stating: the
#: files are still analysed, and the softened rules are still *reported*, just at
#: "warning". `test_a_relaxed_rule_is_still_reported` holds that second property —
#: relaxing to "none" through this mechanism would be an exclusion wearing a
#: severity's clothing, and would not show up in `DISABLED_RULE_EXEMPTIONS` either.
VENDORED_SEVERITY_EXEMPTIONS: dict[str, str] = {
    "feature-platform/pii-anonymizer/hook/vendor": (
        "A vendored third-party tree, re-copied file-for-file from upstream by "
        "its own resync script against the commit pinned in PROVENANCE.md. An "
        "inline `# pyright: ignore` written here would be silently dropped by the "
        "next resync, and correcting an upstream project's annotations in a "
        "vendored copy puts this repo's fix and upstream's source in conflict. "
        "The 5 diagnostics are upstream's to fix; `ruff.toml` carves the same "
        "tree out for the same reason."
    ),
}

#: Severities a rule may hold in this config. A value outside this set is a typo
#: that pyright accepts by falling back to its default, which is how a rule
#: silently stops meaning what the file says.
_VALID_SEVERITIES = frozenset({"none", "information", "warning", "error"})


def _rule_severities(config: dict) -> dict[str, str]:
    """Every repo-wide diagnostic rule setting in a config, by rule name."""
    return {
        key: value
        for key, value in config.items()
        if key.startswith("report") and isinstance(value, str)
    }


def _severity_findings(config: dict) -> list[str]:
    """Everything wrong with a config's rule severities, as reader-facing lines.

    A pure function over a parsed config so the tests below can assert it
    *rejects* a bad one. An assertion nobody has watched fail is a guess about
    what it checks — which is the failure this whole file is about, one level up.
    """
    findings: list[str] = []
    severities = _rule_severities(config)

    for rule, severity in sorted(severities.items()):
        if severity not in _VALID_SEVERITIES:
            findings.append(
                f"{rule} is set to {severity!r}, which is not one of "
                f"{sorted(_VALID_SEVERITIES)}. pyright falls back to its default "
                "for an unrecognised value, so this reads as a setting and is not one."
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
        if rule in ENFORCED_ERROR_RULES:
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


def test_there_are_rule_severities_to_check() -> None:
    """Anti-vacuity guard: an empty rule set would make the check above pass."""
    severities = _rule_severities(_config())
    assert len(severities) >= 15, (
        f"pyrightconfig.json declares only {len(severities)} diagnostic rule "
        f"severities ({sorted(severities)}). Either the config was gutted or the "
        "derivation in _rule_severities() is stale; the severity check above would "
        "be vacuous either way."
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
    """Per-root rule settings that are *weaker* than the repo-wide severity."""
    order = {"none": 0, "information": 1, "warning": 2, "error": 3}
    repo_wide = _rule_severities(config)
    out: dict[str, dict[str, str]] = {}
    for env in _execution_environments(config):
        root = env.get("root")
        if not isinstance(root, str):
            continue
        weaker = {
            rule: severity
            for rule, severity in _rule_severities(env).items()
            if order.get(severity, 3) < order.get(repo_wide.get(rule, "error"), 3)
        }
        if weaker:
            out[root] = weaker
    return out


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


#: Synthetic configs the severity check MUST reject, each named for what it is.
#: An assertion whose failure nobody has observed is a guess about what it checks.
_REJECTABLE_CONFIGS: dict[str, dict] = {
    "an enforced rule downgraded to warning": {"reportCallIssue": "warning"},
    "an enforced rule downgraded to none": {"reportReturnType": "none"},
    "an enforced rule dropped entirely": {"reportCallIssue": None},
    "a fifteenth rule turned off with no reason": {"reportIndexIssue": "none"},
    "a rule set to a value pyright does not know": {"reportCallIssue": "off"},
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
        if severity is None:
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
