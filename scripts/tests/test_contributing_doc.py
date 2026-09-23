# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Keep ``CONTRIBUTING.md`` honest about the repository it describes.

``CONTRIBUTING.md`` is the one document a first-time contributor reads before
anything else, and almost everything in it is a checkable claim about the tree:
thirty-odd ``make`` target names, a set of file paths, the pinned ``cfn-lint``
version, the number of pytest roots, and four tool-version floors. Nothing in
``scripts/`` referenced the file before this test existed, so every one of those
claims could rot silently — the same class of gap as the CI-parity one that
``test_ci_gate_parity.py`` closes, and the docs one that
``test_testing_doc.py`` closes for ``docs/testing.md``.

Two rules shape what follows.

**Derive, do not enumerate.** Every expected value is read out of its source at
test time — the ``Makefile`` for the ``cfn-lint`` pin, ``run_all_tests.py`` for
the root count, ``check_prerequisites()`` for the SAM and Python floors, the two
``package.json`` files for the Node and npm floors, ``ruff`` itself for the
lint-coverage claims. A hardcoded expected-value list would be exactly the
defect this repository keeps rediscovering: a control that exists as an artifact
but is never consulted where the decision is made.

The corollary is that a claim only belongs in the document if it can survive
ordinary churn. Exact whole-repository file counts cannot: the first version of
this file asserted that ``CONTRIBUTING.md`` quoted the exact number of tracked
``.py`` files ``ruff`` examines and skips, which made every commit that adds a
``.py`` file anywhere a failing commit — adding *this* file broke it. Such
claims are stated as proportions and as absolute "every file under X" facts
instead, both still derived from ``ruff``.

**No network.** ``pytest scripts/tests`` runs in both CI systems with no
guarantee of egress, so nothing here resolves a URL. The consequence is that a
dead external link is still not caught; only in-tree paths are.

What this does *not* cover, stated plainly because the value of a guard is
knowing its edge: it does not check the ~112 advisory-warning figure for
``cfn-lint`` (running that gate costs ~25s and the document hedges the number),
it does not verify prose accuracy, and the setup-completeness assertion in
``test_documented_setup_names_every_venv_supplied_tool`` and
``test_documented_setup_names_every_npm_supplied_cli`` is deliberately
conservative: it only recognises a tool invoked as the *first token* of a recipe
line, so a tool invoked after a pipe or inside an ``if`` condition is missed.
Those two tests would have caught the two must-fix findings of the review that
prompted this file; they are not a general proof that the setup instructions are
complete.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPO_ROOT / "CONTRIBUTING.md"
ROOT_MAKEFILE = REPO_ROOT / "Makefile"
COMMON_MAKEFILE = REPO_ROOT / "lib" / "idp_common_pkg" / "Makefile"
PUBLISH_PY = REPO_ROOT / "lib" / "idp_sdk" / "idp_sdk" / "_core" / "publish.py"
RUFF_TOML = REPO_ROOT / "ruff.toml"

DOC = DOC_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Paths the document may legitimately name without them existing on disk.
#
# Keep every exemption visible and removable. An unexplained exemption is how a
# real gap gets waved through later.
# ---------------------------------------------------------------------------
#
# Paths the document names *because* they are absent. Each must stay absent: if
# one comes back, the sentence naming it has become wrong, so
# ``test_paths_expected_absent_are_still_absent`` fails and forces a decision.
PATHS_EXPECTED_ABSENT: dict[str, str] = {
    # ``SECURITY.md`` used to be listed here. It was created by issue #936 while
    # this document already linked it, so the entry existed to hold the link
    # exempt from the "every named path exists" check and the sentence hedged its
    # absence. Both are gone: the file is on ``develop``, the hedge has been
    # removed from the document, and the link is now covered by
    # ``test_documented_paths_exist`` like every other. It was deleted the
    # moment its reason expired, which is the point of writing the expiry
    # condition into the comment rather than into an issue.
    #
    # Named only in the note explaining that the three per-pattern directories
    # were merged into patterns/unified/. That note is the reason the rewrite
    # happened; if one of these reappears the note is the thing to fix.
    "patterns/pattern-1/": "removed; the document says so",
    "patterns/pattern-2/": "removed; the document says so",
    "patterns/pattern-3/": "removed; the document says so",
}

# Generated files: present after a build, absent in a fresh clone. Their presence
# or absence says nothing, so neither direction is asserted.
PATHS_BUILD_ARTIFACTS: dict[str, str] = {
    "src/ui/.checksum": "written by `make ui-lint`; gitignored, absent in a fresh clone",
}


# ---------------------------------------------------------------------------
# Makefile parsing
# ---------------------------------------------------------------------------
_RULE_RE = re.compile(r"^([^\t#=:]+?)\s*:(?!=)")


def _makefile_targets(makefile: Path) -> set[str]:
    """Every explicit target name declared in ``makefile``.

    Handles multi-target rules (``a b:``) and ignores variable assignments
    (``FOO := bar``) and recipe lines (which start with a tab).
    """
    targets: set[str] = set()
    for line in makefile.read_text(encoding="utf-8").splitlines():
        if line.startswith("\t") or not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _RULE_RE.match(line)
        if not match:
            continue
        lhs = match.group(1).strip()
        if lhs.startswith("."):  # .PHONY, .DEFAULT_GOAL and friends
            continue
        for name in lhs.split():
            if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
                targets.add(name)
    return targets


def _makefile_recipes(makefile: Path) -> dict[str, list[str]]:
    """Map each target to its recipe lines.

    Note that make recipes continue **across blank lines**; a parser that stops
    at the first blank line silently truncates them, which is how a reviewer of
    this document came to believe ``lint-cicd`` never invoked ``cfn-lint``.
    """
    recipes: dict[str, list[str]] = {}
    current: str | None = None
    for line in makefile.read_text(encoding="utf-8").splitlines():
        if line.startswith("\t"):
            if current:
                recipes.setdefault(current, []).append(line[1:])
            continue
        if not line.strip():
            continue  # a blank line does NOT end a recipe
        match = _RULE_RE.match(line)
        if match and not line.lstrip().startswith("#"):
            names = [
                n
                for n in match.group(1).strip().split()
                if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", n)
            ]
            current = names[0] if names else None
            if current:
                recipes.setdefault(current, [])
            continue
        current = None
    return recipes


def _recipe_first_tokens(makefile: Path) -> set[str]:
    """Bare command names that appear as the first token of a recipe line.

    Conservative on purpose: a name reached through a pipe, a ``$(...)``
    expansion or an ``if`` condition is not returned. Being conservative here
    only ever makes the tests that consume this *weaker*, never wrong.
    """
    tokens: set[str] = set()
    for lines in _makefile_recipes(makefile).values():
        for raw in lines:
            stripped = raw.strip().lstrip("@+-").strip()
            if not stripped or stripped.startswith("#"):
                continue
            first = stripped.split()[0]
            if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9._-]*", first):
                tokens.add(first)
    return tokens


# ---------------------------------------------------------------------------
# Extracting claims from the document
# ---------------------------------------------------------------------------
# `make(?!:)` skips make's own diagnostics, which the document quotes verbatim:
# `make: ruff: No such file or directory` is not an invocation of a target.
# `[ \t]+` rather than `\s+`: a newline must not be crossed, or a code span that
# is just `make` runs into the next span and reads its first word as a target.
_MAKE_INVOCATION_RE = re.compile(
    r"(?:(cd[ \t]+(\S+)[ \t]*&&[ \t]*))?\bmake(?!:)[ \t]+([^\n`|;]*)"
)
_MAKE_FLAG_RE = re.compile(r"^-")
_MAKE_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _command_text() -> str:
    """Only the parts of the document that are presented as commands.

    Restricting to fenced blocks and inline code spans matters: "make" is also an
    ordinary English verb, and scanning the prose turns "make it", "make such"
    and "make this" into imaginary targets.
    """
    chunks = _FENCE_RE.findall(DOC)
    chunks.extend(_BACKTICK_RE.findall(DOC))
    return "\n".join(chunks)


def _documented_make_invocations() -> set[tuple[str, str]]:
    """``(makefile-relative-dir, target)`` pairs the document tells you to run.

    Resolves the two ways the document points at a non-root Makefile:
    ``make <target> -C <dir>`` and ``cd <dir> && make <target>``.
    """
    text = _command_text()
    found: set[tuple[str, str]] = set()
    for cd_dir, _, tail in (
        (m.group(2), m.group(1), m.group(3)) for m in _MAKE_INVOCATION_RE.finditer(text)
    ):
        # Named `args`, not `tokens`: Bandit's B105 (hardcoded_password_string)
        # fires on any `==` comparison against a literal where the identifier is
        # called `token`, and `arg == "-C"` below tripped the SRT gate.
        args = tail.split()
        directory = cd_dir or "."
        target: str | None = None
        i = 0
        while i < len(args):
            arg = args[i]
            if ":" in arg:
                break  # a colon means this is prose or a diagnostic, not a command
            if arg == "-C":
                directory = args[i + 1] if i + 1 < len(args) else directory
                i += 2
                continue
            if _MAKE_FLAG_RE.match(arg) or _MAKE_ASSIGNMENT_RE.match(arg):
                i += 1
                continue
            # Accept upper case too. Every real target here is lower case, but a
            # lower-case-only pattern silently *ignores* a mistyped target such as
            # `make lintCicd`, which is exactly the drift being guarded against.
            if target is None and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", arg):
                target = arg
            i += 1
        if target:
            found.add((directory, target))
    return found


_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")


def _looks_like_repo_path(candidate: str) -> bool:
    """True when a backticked string is plausibly a path into this repository.

    Rejects the many backticked strings in the document that are *not* paths:
    shell fragments, config keys, extras syntax (``lib/idp_common_pkg[core]``),
    CloudFormation pseudo-parameters, rule codes and TOML snippets.
    """
    if not candidate or any(c in candidate for c in " \t\"'[]{}$*<>|=()"):
        return False
    if candidate.startswith(("../", "./", "/", "~", "http")):
        return False
    if candidate.startswith((".venv/", "node_modules/", "build/", "dist/")):
        return False
    if ":" in candidate:  # arn:..., key: value
        return False
    # Require an interior slash. A bare filename cannot be told apart from a
    # generic reference — "each with its own `conftest.py`", "a `requirements.txt`"
    # — so it carries no signal, and a trailing-slash-only string like `feature/`
    # is a branch-name prefix rather than a directory. The cost is that root-level
    # files named only in backticks (`template.yaml`, `VERSION`) are not covered
    # here; markdown links to them still are.
    return "/" in candidate.rstrip("/")


def _documented_paths() -> set[str]:
    """Every in-tree path the document names, from links and from backticks."""
    paths: set[str] = set()
    for target in _LINK_RE.findall(DOC):
        target = target.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        paths.add(target)
    for candidate in _BACKTICK_RE.findall(DOC):
        candidate = candidate.strip()
        if _looks_like_repo_path(candidate):
            paths.add(candidate)
    return paths


# ---------------------------------------------------------------------------
# Deriving the numbers
# ---------------------------------------------------------------------------
def _doc_numbers(pattern: str) -> list[str]:
    return re.findall(pattern, DOC)


def _cfn_lint_pin() -> str:
    match = re.search(
        r"^CFN_LINT_VERSION\s*:=\s*(\S+)",
        ROOT_MAKEFILE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, "CFN_LINT_VERSION is no longer assigned in the root Makefile"
    return match.group(1)


def _run_root_count() -> int:
    """The number ``make test-list`` prints, derived from the same registry."""
    spec = importlib.util.spec_from_file_location(
        "_contributing_doc_run_all_tests", REPO_ROOT / "scripts" / "run_all_tests.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return len([r for r in module.RUN_ROOTS if (REPO_ROOT / r).exists()])
    finally:
        sys.modules.pop(spec.name, None)


def _publish_floor(variable: str) -> str:
    match = re.search(
        rf'^\s*{re.escape(variable)}\s*=\s*"([^"]+)"',
        PUBLISH_PY.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, f"{variable} is no longer set in publish.py's check_prerequisites()"
    return match.group(1)


def _engines(package_json: Path) -> dict[str, str]:
    return json.loads(package_json.read_text(encoding="utf-8")).get("engines", {})


def _ruff_python_files() -> set[str]:
    """Repo-relative ``.py`` files ``ruff`` would actually examine."""
    result = subprocess.run(
        ["ruff", "check", "--show-files", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    files: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.endswith(".py"):
            continue  # `--show-files` also lists the pyproject.toml config sources
        try:
            files.add(str(Path(line).resolve().relative_to(REPO_ROOT)))
        except ValueError:
            continue
    return files


def _tracked_python_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


# ===========================================================================
# 1. Every `make` target the document cites exists in the Makefile it names
# ===========================================================================
@pytest.mark.unit
def test_documented_make_targets_exist() -> None:
    """A renamed or deleted target must not keep being recommended."""
    known: dict[str, set[str]] = {
        ".": _makefile_targets(ROOT_MAKEFILE),
        "lib/idp_common_pkg": _makefile_targets(COMMON_MAKEFILE),
    }
    missing: list[str] = []
    for directory, target in sorted(_documented_make_invocations()):
        makefile_dir = directory.rstrip("/")
        if makefile_dir not in known:
            candidate = REPO_ROOT / makefile_dir / "Makefile"
            if not candidate.exists():
                missing.append(f"{directory}: no Makefile there (target {target!r})")
                continue
            known[makefile_dir] = _makefile_targets(candidate)
        if target not in known[makefile_dir]:
            where = "root Makefile" if makefile_dir == "." else f"{makefile_dir}/Makefile"
            missing.append(f"make {target} — not a target in the {where}")
    assert not missing, "CONTRIBUTING.md names make targets that do not exist:\n" + "\n".join(
        f"  - {m}" for m in missing
    )


# ===========================================================================
# 2. Every in-tree path the document cites exists
# ===========================================================================
@pytest.mark.unit
def test_documented_paths_exist() -> None:
    """A moved or deleted file must not keep being pointed at.

    ``CONTRIBUTING.md`` was rewritten in the first place because it described
    directories (``patterns/pattern-1/`` and siblings) that had been removed.
    """
    exempt = set(PATHS_EXPECTED_ABSENT) | set(PATHS_BUILD_ARTIFACTS)
    missing: list[str] = []
    for candidate in sorted(_documented_paths()):
        if candidate in exempt or candidate.rstrip("/") in exempt:
            continue
        if not (REPO_ROOT / candidate).exists():
            missing.append(candidate)
    assert not missing, "CONTRIBUTING.md points at paths that do not exist:\n" + "\n".join(
        f"  - {m}" for m in missing
    )


@pytest.mark.unit
def test_paths_expected_absent_are_still_absent() -> None:
    """Every deliberate exemption must still be earning its place.

    An exemption that outlives its reason is indistinguishable from a hole. Each
    of these is named by the document *as absent* — the three
    ``patterns/pattern-N/`` directories, because they were merged into
    ``patterns/unified/``. If one exists, the sentence that names it is now wrong
    and the exemption is now hiding a real check.

    This has already fired once for real. ``SECURITY.md`` was exempt while it was
    being written under issue #936, and the moment it landed on ``develop`` this
    test failed and named it, which is what forced both the hedge in
    ``CONTRIBUTING.md`` and the exemption itself to be removed rather than
    quietly left behind as a permanently unchecked link.
    """
    stale = [
        f"{name} — exempted because: {reason}"
        for name, reason in PATHS_EXPECTED_ABSENT.items()
        if (REPO_ROOT / name).exists()
    ]
    assert not stale, (
        "These paths now exist. Update the sentence in CONTRIBUTING.md that names "
        "them as absent, then delete their PATHS_EXPECTED_ABSENT entries:\n"
        + "\n".join(f"  - {s}" for s in stale)
    )


# ===========================================================================
# 3. The numbers the document quotes, each read from its own source
# ===========================================================================
@pytest.mark.unit
def test_cfn_lint_pin_matches_makefile() -> None:
    pin = _cfn_lint_pin()
    assert pin in DOC, (
        f"CONTRIBUTING.md does not mention the pinned cfn-lint version {pin} "
        "(CFN_LINT_VERSION in the root Makefile). It quotes the pin in the "
        "cfn-lint bullet and in the venv-activation paragraph; update both."
    )


# test_test_root_count_matches_registry was here, and it was a mistake. It
# asserted that CONTRIBUTING.md's "the tests live in N separate roots" matched
# the length of RUN_ROOTS in scripts/run_all_tests.py, deriving N at test time
# so the prose could not drift.
#
# The mechanism is right and the target was wrong. Deriving a number is worth
# doing when the NUMBER is the claim a reader acts on -- how many resource
# policies deny non-TLS requests, how many alarms reach a subscribed topic --
# because a stale one there misleads about the security posture. This number was
# scenery. The sentence's load-bearing content is that there are many roots, one
# per package and per Lambda directory, each with its own conftest.py, so a bare
# pytest from the root collides; the count adds nothing a reader can act on.
#
# What it cost was real: registering one more test suite -- a routine and
# desirable act -- failed a blocking gate for a purely cosmetic reason, which
# teaches people to read gate failures as bureaucracy and erodes the gates that
# matter. The prose now says "many separate roots" and there is nothing to keep
# in step. Do not reinstate this without a reason a reader would act on.

@pytest.mark.unit
def test_prerequisite_floors_match_their_sources() -> None:
    """The prerequisites table must agree with the code and configs it cites."""
    sam_floor = _publish_floor("min_sam_version")
    python_floor = _publish_floor("min_python_version")
    root_engines = _engines(REPO_ROOT / "package.json")
    ui_engines = _engines(REPO_ROOT / "src" / "ui" / "package.json")

    expected = {
        "SAM CLI floor (publish.py check_prerequisites)": sam_floor,
        "Python floor (publish.py check_prerequisites)": python_floor,
        "Node floor (root package.json engines)": root_engines["node"].lstrip(">=~^"),
        "npm floor (root package.json engines)": root_engines["npm"].lstrip(">=~^"),
        "UI npm floor (src/ui/package.json engines)": ui_engines["npm"].lstrip(">=~^"),
    }
    missing = {label: value for label, value in expected.items() if value not in DOC}
    assert not missing, (
        "CONTRIBUTING.md's prerequisites table no longer quotes these values:\n"
        + "\n".join(f"  - {label}: expected {value!r}" for label, value in missing.items())
    )
    assert root_engines["node"] == ui_engines["node"], (
        "The two package.json files now disagree on the Node floor; the document "
        "states a single value for both and needs updating."
    )


@pytest.mark.unit
def test_ruff_exclusion_figures_match_the_baseline() -> None:
    """The document's two exclusion counts, read from the baseline that sets them.

    These are exact counts on purpose, which an earlier version of this check
    could not afford: the figures then were "how many tracked .py files does ruff
    skip", which every commit adding a .py file changed, so pinning them would
    have red-lined unrelated branches. They are now the sizes of two named lists
    in ``scripts/lint_debt.json``, and those change only when somebody runs
    ``check_lint_debt.py --write`` — a deliberate act, by whoever is also in a
    position to update this sentence.
    """
    baseline = json.loads(
        (REPO_ROOT / "scripts" / "lint_debt.json").read_text(encoding="utf-8")
    )
    lint_files = len(baseline["lintDebt"])
    lint_findings = sum(sum(c.values()) for c in baseline["lintDebt"].values())
    format_files = len(baseline["formatDebt"])

    assert lint_files and format_files, (
        "scripts/lint_debt.json records no debt at all. If that is genuine, the "
        "paragraph in CONTRIBUTING.md about excluded files should go rather than "
        "quote zeroes."
    )
    for label, value in (
        ("[lint] exclude file count", lint_files),
        ("pre-existing lint findings", lint_findings),
        ("[format] exclude file count", format_files),
    ):
        assert str(value) in DOC, (
            f"CONTRIBUTING.md no longer quotes the {label} ({value}). "
            "scripts/lint_debt.json is the source of truth; update the paragraph "
            "under 'Before every commit' to match it."
        )


@pytest.mark.unit
def test_ruff_reads_every_tracked_python_file_except_the_scope_exclusions() -> None:
    """The document's central claim, measured: a new file anywhere is linted.

    ``ruff`` discovers files by walking, so the claim is about the walk and not
    about the per-file debt lists (a file on those is still *discovered*; the
    ``[lint] exclude`` array is what silences it, and ``make check-lint-debt``
    re-measures those). What must hold here is that no directory is skipped
    wholesale any more, apart from the ``scope`` entries — because a directory
    exclusion is what makes a *newly added* file silently uncovered.
    """
    if shutil.which("ruff") is None:
        pytest.skip("ruff is not on PATH (see the venv-activation note in CONTRIBUTING.md)")

    baseline = json.loads(
        (REPO_ROOT / "scripts" / "lint_debt.json").read_text(encoding="utf-8")
    )

    def in_scope(rel: str) -> bool:
        for pattern in baseline["scope"]:
            if pattern.startswith("**/*."):
                if rel.endswith(pattern[4:]):
                    return True
            elif rel == pattern or rel.startswith(pattern.rstrip("/") + "/"):
                return True
        return False

    tracked = _tracked_python_files()
    assert tracked, "git ls-files '*.py' returned nothing; the measurement is vacuous"
    undiscovered = sorted(
        rel for rel in tracked - _ruff_python_files() if not in_scope(rel)
    )
    assert not undiscovered, (
        f"`ruff` does not even look at {len(undiscovered)} tracked .py file(s), and "
        f"they are not covered by a scripts/lint_debt.json `scope` entry, so a new "
        f"file added beside them would be silently unlinted too: "
        f"{undiscovered[:10]}\n\nThis is the shape of issue #975. Check ruff.toml's "
        "top-level `exclude` for a directory name that should not be there."
    )


@pytest.mark.unit
def test_the_document_does_not_still_describe_a_whole_tree_as_unlinted() -> None:
    """The claim that used to be here, kept out rather than simply deleted.

    The document told a reader that a clean ``ruff check`` on anything under
    ``src/`` or ``scripts/`` meant the file had not been examined. That is now
    false — ``ruff`` reads both trees — and a false reassurance in the other
    direction is worse than the original gap, because it tells a contributor to
    ignore a real result.
    """
    for claim in ("roughly a third", "every file under `scripts/`"):
        assert claim not in DOC, (
            f"CONTRIBUTING.md still contains {claim!r}, which described the "
            "directory-wide exclusions removed in issue #975."
        )


@pytest.mark.unit
def test_the_documented_probe_is_the_one_that_works() -> None:
    """A misleading probe is the stated reason #975 survived inspection.

    The document used to name `ruff check --force-exclude <file>` as the way to
    tell whether a file is linted. That was correct while every exclusion was a
    *discovery* exclusion (`exclude` / `extend-exclude`), and it is not correct for
    the per-file `[lint] exclude` / `[format] exclude` arrays, which filter after
    discovery. So the document must point at the baseline instead.
    """
    assert "check_lint_debt.py --explain" in DOC, (
        "CONTRIBUTING.md no longer names `check_lint_debt.py --explain` as the way "
        "to find out whether a file is linted. Every ruff-native probe misreports "
        "at least one class of file — see the measurement in the test below."
    )
    assert "--explain" in _script_options(), (
        "scripts/check_lint_debt.py no longer offers --explain, which "
        "CONTRIBUTING.md tells contributors to use."
    )


def _script_options() -> str:
    return (REPO_ROOT / "scripts" / "check_lint_debt.py").read_text(encoding="utf-8")


@pytest.mark.unit
def test_force_exclude_really_does_misreport_a_per_file_exclusion() -> None:
    """Measure the trap rather than asserting it from memory.

    If a future `ruff` release makes `--force-exclude` honour `lint.exclude`, this
    fails and the warning in CONTRIBUTING.md can be simplified — which is the point
    of measuring it instead of describing it.
    """
    if shutil.which("ruff") is None:
        pytest.skip("ruff is not on PATH (see the venv-activation note in CONTRIBUTING.md)")
    baseline = json.loads(
        (REPO_ROOT / "scripts" / "lint_debt.json").read_text(encoding="utf-8")
    )
    lint_excluded = sorted(baseline["lintDebt"])
    assert lint_excluded, "no lint-excluded files, so this measurement is vacuous"
    probe = lint_excluded[0]

    result = subprocess.run(
        ["ruff", "check", "--no-fix", "--force-exclude", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and "All checks passed" in result.stdout, (
        f"`ruff check --force-exclude {probe}` now reports something for a file in "
        "[lint] exclude. If it has started honouring lint.exclude, it is a valid "
        "probe again — update the warning in CONTRIBUTING.md and the two skill "
        f"files.\nexit={result.returncode}\n{result.stdout}"
    )

    # And the same file, asked without the flag: the findings the exclusion hides.
    unforced = subprocess.run(
        ["ruff", "check", "--no-fix", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unforced.returncode == 1, (
        f"{probe} is recorded in lintDebt as carrying findings, but `ruff check "
        f"{probe}` (which bypasses the exclusions) reports none. The baseline is "
        "stale: run `python3 scripts/check_lint_debt.py --write`."
    )


@pytest.mark.unit
def test_no_ruff_exclusion_is_a_bare_directory_name() -> None:
    """The defect of issue #975, asserted on the config rather than the prose.

    ``ruff.toml`` excluded ``notebooks``, ``options``, ``patterns``, ``src`` and
    ``scripts``. A pattern with no ``/`` matches at ANY path depth, so ``src``
    also excluded ``nested/*/src`` and ``patterns/*/src`` — 442 of 1230 tracked
    ``.py`` files in total, and nobody had counted the second-order ones.

    ``make check-lint-debt`` asserts the same thing over the generated blocks.
    This is here as well because it needs no tooling and covers every exclusion
    array in the file, generated or not, including ``per-file-ignores``.
    """
    ruff_toml = RUFF_TOML.read_text(encoding="utf-8")
    offenders: list[str] = []
    for key in ("extend-exclude", "exclude"):
        for match in re.finditer(
            rf"^{re.escape(key)}\s*=\s*\[(.*?)^\]", ruff_toml, re.DOTALL | re.MULTILINE
        ):
            block = match.group(1)
            # The top-level `exclude` array is deliberately bare names: a
            # `build/` or `.venv/` at any depth IS build output. It is
            # identified by containing `.git`, which no source path would.
            if '".git"' in block:
                continue
            offenders += [e for e in re.findall(r'"([^"]*)"', block) if "/" not in e]

    assert not offenders, (
        f"ruff.toml excludes bare directory name(s) {offenders}, which ruff "
        "matches at any path depth rather than at the repository root. Name the "
        "path from the root (see issue #975), or, if an any-depth match is "
        "genuinely wanted, put it in the top-level `exclude` array with the "
        "build-output entries."
    )


@pytest.mark.unit
def test_ruff_line_length_is_not_an_enforced_check() -> None:
    """The document's claim that 88 columns is a formatter setting, not a rule.

    If ``E501`` is ever selected the claim becomes wrong in the reader's favour,
    and the paragraph must be removed rather than left to mislead in reverse.
    """
    ruff_toml = RUFF_TOML.read_text(encoding="utf-8")
    select_match = re.search(r"^select\s*=\s*(\[[^\]]*\])", ruff_toml, re.MULTILINE)
    extend_match = re.search(r"^extend-select\s*=\s*(\[[^\]]*\])", ruff_toml, re.MULTILINE)
    selected = (select_match.group(1) if select_match else "") + (
        extend_match.group(1) if extend_match else ""
    )
    e501_selected = "E501" in selected or '"E"' in selected or "'E'" in selected
    claims_not_enforced = "`E501` is not in the `[lint] select` list" in DOC
    assert e501_selected != claims_not_enforced, (
        "ruff.toml and CONTRIBUTING.md disagree about line-length enforcement: "
        f"E501 selected={e501_selected}, document claims it is not enforced="
        f"{claims_not_enforced}."
    )


# ===========================================================================
# 4. The setup instructions name every tool the documented gates need
#
# This is the group that would have caught the two must-fix findings: a setup
# sequence that produced `ruff: No such file or directory`, and a type-check
# section that named no way to obtain basedpyright.
# ===========================================================================
def _setup_section() -> str:
    """The prose a reader follows to get a working environment."""
    match = re.search(
        r"^### Prerequisites$(.*?)^## Repository layout$", DOC, re.MULTILINE | re.DOTALL
    )
    assert match, "CONTRIBUTING.md's setup section could not be located"
    return match.group(1)


def _venv_supplied_console_scripts() -> set[str]:
    """Tools ``make setup-venv`` puts in ``.venv/bin`` — derived, not listed.

    Two sources, both read at test time: the literal distributions the setup
    recipes ``pip install`` by name, and the dependencies of the
    ``lib/idp_common_pkg`` extras that ``FIRST_PARTY_EDITABLES`` requests.
    """
    makefile = ROOT_MAKEFILE.read_text(encoding="utf-8")
    names: set[str] = set()
    for spec in re.findall(r"pip install\s+([A-Za-z][A-Za-z0-9._-]*)==", makefile):
        names.add(spec)
    pyproject = (REPO_ROOT / "lib" / "idp_common_pkg" / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    for spec in re.findall(r'^\s*"([A-Za-z][A-Za-z0-9._-]*)(?:\[[^\]]*\])?[<>=!~]', pyproject, re.MULTILINE):
        names.add(spec)
    return names


@pytest.mark.unit
def test_documented_setup_names_every_venv_supplied_tool() -> None:
    """If a gate recipe calls a venv tool by bare name, say to activate the venv.

    The condition is derived from the ``Makefile``, so the assertion retires by
    itself the moment the ``Makefile`` starts putting ``.venv/bin`` on ``PATH``.
    """
    makefile = ROOT_MAKEFILE.read_text(encoding="utf-8")
    venv_dir = "$(VENV_DIR)/bin"
    makefile_exports_path = bool(
        re.search(r"^\s*(export\s+)?PATH\s*[:+]?=", makefile, re.MULTILINE)
    ) or (venv_dir in makefile and re.search(r"PATH\s*=.*VENV_DIR", makefile))
    if makefile_exports_path:
        pytest.skip("the Makefile now puts .venv/bin on PATH; activation is no longer needed")

    bare = _recipe_first_tokens(ROOT_MAKEFILE) & _venv_supplied_console_scripts()
    if not bare:
        pytest.skip("no venv-supplied tool is invoked as a bare command any more")

    activation = "source .venv/bin/activate"
    assert activation in DOC, (
        f"The Makefile invokes {sorted(bare)} as bare command(s) and never adds "
        f"$(VENV_DIR)/bin to PATH, so `make setup-venv` alone leaves those gates "
        f"failing with Error 127. CONTRIBUTING.md must tell the reader to run "
        f"`{activation}`."
    )
    setup = _setup_section()
    assert activation in setup, (
        f"`{activation}` is mentioned somewhere in CONTRIBUTING.md but not in the "
        "setup section, which is where a reader following the instructions will be."
    )


@pytest.mark.unit
def test_documented_setup_names_every_npm_supplied_cli() -> None:
    """A gate tool that comes from npm must have its install command documented.

    ``basedpyright`` is the live case: a devDependency of the root
    ``package.json`` that neither ``make setup`` nor ``make setup-venv``
    installs, invoked as a bare command by three ``make`` targets.
    """
    dev_deps = set(
        json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
        .get("devDependencies", {})
        .keys()
    )
    bare = _recipe_first_tokens(ROOT_MAKEFILE) & dev_deps
    if not bare:
        pytest.skip("no root-package.json devDependency is invoked as a bare command")

    setup = _setup_section()
    problems: list[str] = []
    for tool in sorted(bare):
        if f"npm install -g {tool}" not in DOC:
            problems.append(f"{tool}: CONTRIBUTING.md never gives an install command")
        elif tool not in setup:
            problems.append(f"{tool}: named in the document but not in the setup section")
    assert not problems, (
        "The Makefile invokes these npm-supplied tools as bare commands, so a "
        "contributor who ran only `make setup`/`make setup-venv` does not have "
        "them:\n" + "\n".join(f"  - {p}" for p in problems)
    )
