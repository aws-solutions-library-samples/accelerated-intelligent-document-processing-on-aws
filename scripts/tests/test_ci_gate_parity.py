# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assert GitHub and GitLab run the same non-integration gates.

This repo has two CI systems, and gates have repeatedly existed on only one of
them — so a change merged through the *other* one skipped them silently:

* SRT and the dependency audit were GitLab-only until #827.
* ``make api-test-static`` and the service-role permission check were GitLab-only
  until #870.
* ``make cfn-lint`` and ``make validate-buildspec`` were in ``lint``/``fastlint``
  but not ``lint-cicd``, so they ran in **neither** CI.

Nothing detected any of those; each was found by hand, months later. This test is
the detector. It deliberately asserts on the *config files*, because the failure
mode is a config edit, not a code change.

⚠️ **Two different things are called "integration" here, and only one of them is
GitLab-only.** The GitLab ``integration_tests`` *stage* deploys a stack through
CodePipeline and is deliberately GitLab-only, because it needs AWS credentials.
The pytest ``integration`` *marker tier* — ``make test-integration`` — is a
different suite that stage never invokes, and it runs in **no** CI at all. The
two were conflated in this very docstring, which is how the tier came to be
uncovered while an exemption cited the stage as its coverage (#1307). See
:func:`test_the_integration_pytest_tier_runs_in_no_ci`, which measures the
absence, and ``scripts/sdlc/docs/CI_TEST_COVERAGE.md``.

**The structural weakness of a hardcoded list, and what is done about it.** The
first two incidents in that list — SRT and the dependency audit — were the ones
named above as the motivation, and neither appeared in :data:`SHARED_GATES`. Both
ran in both CIs, so deleting either side's step left this whole suite green. A list
of gates cannot see a gate that is missing from it, and it cannot see a gate present
in the ``Makefile`` and absent from *both* CIs at all, because such a gate has no
foothold in either config to be compared.

So membership in two directions is now derived rather than listed:

* :func:`gate_universe` reads the ``Makefile``'s check-shaped sections and requires
  every target in them to be either **reached by both CIs** or **registered in**
  :data:`GATES_DELIBERATELY_OUT_OF_CI` with a per-target reason. That is the
  universe-closure ratchet: a new gate cannot be added to the ``Makefile`` and left
  out of CI silently, which is the case a list of what *is* in CI cannot express.
* :data:`SHARED_GATES` stays an authored list, because parity is a claim about
  *specific* invocations (a gate can be reached through a different target name, an
  inline script path, or a container image step) and deriving it would mean guessing
  which lines in a CI config are gate invocations. It is ratcheted instead: every
  entry must be found in both CIs, every ``make`` entry must name a target that
  still exists, and the list may not be empty.

**What this module reads is text, and cannot tell a running gate from a present
one.** Every assertion here is a search over the ``Makefile`` and the two CI configs
with comments and ``echo`` lines removed. It therefore still passes if a gate is
*present but neutered* — ``make srt-scan || true`` swallows the failure, and a step
behind an ``if:`` that is never true never runs, and both read here as the gate
running in both CIs. Deciding whether a YAML condition can ever be true, or whether a
shell line's status reaches the job, is evaluation rather than reading; nothing in
this file attempts it. Reviewing a change to either CI config means looking at the
conditions, not just at whether the gate's name is present.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]
GITLAB = REPO_ROOT / ".gitlab-ci.yml"
GITHUB_TESTS = REPO_ROOT / ".github/workflows/developer-tests.yml"
GITHUB_SECURITY = REPO_ROOT / ".github/workflows/security-checks.yml"
#: Composite actions are part of the CI configuration — see :func:`_github_ci_text`.
GITHUB_ACTIONS_DIR = REPO_ROOT / ".github/actions"
MAKEFILE = REPO_ROOT / "Makefile"

# Gates that MUST run in both CIs. Each is a static, no-AWS check.
#
# `make srt-scan` and the dependency audit are the two incidents this module's
# docstring names as its motivation, and for a long time neither was listed here:
# deleting GitHub's SRT step left every test in this file green. The audit is listed
# by its script path because that is how both CIs invoke it — neither goes through
# `make dep-audit`.
#
# `make typecheck` is the WHOLE-TREE type gate. What ran before was
# `make typecheck-pr`, which narrows basedpyright to the files a PR changed and so
# cannot see a break the change caused in a file the diff did not touch.
SHARED_GATES = [
    "make lint-cicd",
    "make typecheck",
    "make api-test-static",
    "make test-cicd",
    "make test-packages-cicd",
    # The two halves of the coverage ratchet's reach. `coverage-all-cicd` is what
    # produces a report per tree; `check-coverage-debt-cicd` is the ratchet with
    # `--require-all-trees`, so a tree with no report is red BY NAME rather than
    # named-and-passed. Before both existed, eight of the nine trees were reported as
    # "not checked" and the gate exited 0 (#1256), which is why the pair is listed
    # here rather than left to the universe-closure check: parity is the property
    # that stops one CI keeping the reports and the other losing them.
    "make coverage-all-cicd",
    "make check-coverage-debt-cicd",
    "npx vitest run",
    "scripts/check_first_party_deps.py",
    "scripts/sdlc/validate_service_role_permissions.py",
    "make srt-scan",
    "scripts/security/dep_audit.py",
]

#: ``Makefile`` section headings every one of whose targets is scanned, because the
#: whole section is checks: they run a check, offline, against the checkout. Matched
#: as prefixes of the ``##@`` heading text.
#:
#: ⚠️ **This tuple alone is not the universe, and must not be read as one.** It names
#: 5 of the ``Makefile``'s 16 sections, and a section-scoped rule stating that the
#: other eleven "hold no checks" would be false for the section this module's own
#: subject lives in: **UI Development holds ``ui-lint``** — reached from
#: ``lint-cicd``, and the gate the ``--max-warnings 0`` change is about — and
#: ``ui-test``, whose ``npx vitest run`` form is in :data:`SHARED_GATES`. One
#: sentence covering eleven sections is exactly the shape of defect
#: ``scripts/tests/gate_exemptions.json`` exists to stop, so the scan does not rely
#: on it: :func:`gate_universe` adds any **check-shaped** target from an unscanned
#: section, by name (:data:`CHECK_SHAPED_NAME_PREFIXES`).
GATE_SECTION_PREFIXES = (
    "Code Quality",
    "Type Checking",
    "Tests — pytest",
    "Security (SRT)",
    "Dependencies",
)

#: The one section this module excludes *wholesale*, and the only one where a single
#: justification really is a property of every member: every target in "Stack tests"
#: deploys or talks to a live IDP stack, which its own heading says and which no
#: pull-request gate can do. They are mapped in ``docs/testing.md``, and that each one
#: appears there is enforced by ``scripts/tests/test_testing_doc.py``.
#:
#: Count-pinned by :func:`test_the_live_stack_section_exclusion_is_count_pinned`, so a
#: target added inside this excluded section is a deliberate edit here rather than a
#: silent extra member of a blanket exclusion.
LIVE_STACK_SECTION_PREFIXES = ("Stack tests",)

#: How many targets that wholesale exclusion covers today, audited when written.
LIVE_STACK_SECTION_TARGET_COUNT = 21

#: Name shapes that make a target a check wherever it lives. This is what stops
#: :data:`GATE_SECTION_PREFIXES` from being a section-scoped hole: a ``check-*`` or
#: ``validate-*`` target added to Deploy, or a ``*-lint`` added to UI Development, is
#: in the universe and must be classified like any other.
#:
#: Deliberately name-shaped rather than recipe-shaped. Reading a recipe to decide
#: whether it "checks something" is a guess about semantics, and a wrong guess here
#: produces a green gate; a name convention is crude but it is the convention this
#: ``Makefile`` actually follows, and the three targets it currently finds outside the
#: scanned sections are the proof it is not matching nothing.
CHECK_SHAPED_NAME_PREFIXES = ("check-", "validate-", "lint", "typecheck")
CHECK_SHAPED_NAME_SUFFIXES = ("-check", "-checks", "-lint", "-test", "-tests")

#: Targets in a gate section that are deliberately NOT reached by CI. One entry, one
#: target, one reason — never a shared justification, which is the defect class
#: ``scripts/tests/gate_exemptions.json`` exists to stop.
#:
#: This is the authored half of the universe-closure check in
#: :func:`test_every_gate_shaped_target_is_in_both_cis_or_registered`. Its ratchets
#: are in :func:`test_no_registered_target_is_actually_in_ci` (non-vacuity: an entry
#: that CI has since started running must be deleted, or it pre-exempts whatever next
#: takes the name) and :func:`test_every_registered_target_still_exists` (staleness).
GATES_DELIBERATELY_OUT_OF_CI = {
    # --- aggregates whose CI form is a different target -------------------------
    "lint": (
        "Developer aggregate. Its CI form is `lint-cicd`, and that lint-cicd is not "
        "the weaker of the two is asserted by "
        "test_lint_cicd_covers_what_local_lint_covers below."
    ),
    "fastlint": (
        "Developer aggregate: `lint` minus the UI targets, for a fast inner loop. "
        "Every prerequisite it has is also a prerequisite of `lint`."
    ),
    # --- auto-fixing forms; lint-cicd runs the checking form --------------------
    "ruff-lint": (
        "Runs `ruff check --fix`, which MUTATES the tree. A fixer cannot be a gate: "
        "in CI it would repair the ephemeral checkout and report it clean. "
        "lint-cicd runs `ruff check` instead — see CHECK_ONLY_EQUIVALENTS."
    ),
    "format": (
        "Runs `ruff format`, which MUTATES the tree, so the same applies. lint-cicd "
        "runs `ruff format --check` instead — see CHECK_ONLY_EQUIVALENTS."
    ),
    # --- reporting variants of a gate that does run ----------------------------
    "cfn-lint-warnings": (
        "`cfn-lint` with CFN_LINT_SHOW_WARNINGS=1: identical checks, verbose "
        "output. `cfn-lint` runs from lint-cicd, so the checking is gated."
    ),
    "typecheck-stats": (
        "`basedpyright --stats`: the same analysis as `typecheck` plus timing "
        "output. `typecheck` is the gate and runs in both CIs."
    ),
    "test-list": (
        "`scripts/run_all_tests.py --list`: prints the discovered test roots and "
        "runs nothing. The registry completeness it reports is itself asserted by "
        "scripts/tests/test_testing_doc.py."
    ),
    # --- narrower forms of a gate that does run --------------------------------
    "typecheck-pr": (
        "File-scoped basedpyright, for local latency. Deliberately not a gate: it "
        "cannot see a break the change caused in a file the diff did not select. "
        "Asserted absent from both CIs by "
        "scripts/sdlc/tests/test_typecheck_pr_changes.py."
    ),
    "ui-test": (
        "Runs the UI's Vitest suite, which both CIs DO run — as a bare "
        "`npx vitest run` (it is in SHARED_GATES) rather than through this target, "
        "because each CI installs the UI dependencies itself. So the suite is "
        "gated; this Makefile wrapper is the local spelling of it."
    ),
    "test-cli": (
        "`lib/idp_cli_pkg` alone, verbosely. The same suite runs in both CIs as the "
        "first step of `test-packages-cicd`."
    ),
    "test-config-library": (
        "`config_library/test_config_library.py` alone, verbosely. The same file "
        "runs in both CIs from `test-packages-cicd`."
    ),
    "test-hooks": (
        "`scripts/tests/test_check_commit_text.py` alone, verbosely. It sits under "
        "`scripts/tests`, which `test-packages-cicd` runs in full in both CIs."
    ),
    "test-capacity": (
        "`src/lambda/calculate_capacity` alone, verbosely. `make test` covers it "
        "via scripts/run_all_tests.py; no CI target does, which makes this a real "
        "residual and not a duplicate — see the `test` entry."
    ),
    "test-capacity-coverage": (
        "`test-capacity` plus a coverage report written to a local htmlcov/ "
        "directory. A coverage report is an artifact, not an assertion: this "
        "target's pass/fail is exactly `test-capacity`'s."
    ),
    "coverage": (
        "Measures idp_common coverage and prints the table worst-covered first. It "
        "asserts nothing by construction — it runs the package suite with the "
        "aggregate floor disabled (`COV_FLOOR=`), discards its output and ignores "
        "its exit status — so there is no verdict for a CI to carry. The ratchet it "
        "reports on does run in both CIs: the aggregate floor inside "
        "`test-cicd -C lib/idp_common_pkg`, and the per-file baseline via "
        "`make check-coverage-debt-cicd`, which both configurations invoke after the "
        "test steps because it reads the reports those steps write."
    ),
    "check-coverage-debt": (
        "The ratchet with no arguments, which is the LOCAL spelling. It names a tree "
        "with no report and exits 0, so on a checkout holding one tree's report it "
        "reports on that tree and passes — correct for a developer who measured one "
        "tree, and the exact state that left eight of nine trees advisory-but-passing "
        "in CI (#1256). Both CIs therefore reach `check-coverage-debt-cicd` instead, "
        "which adds `--require-all-trees` and is in SHARED_GATES. Registered here for "
        "this one target: the check runs in both CIs, under the other name."
    ),
    "coverage-table": (
        "Reprints the table from the last measurement without re-measuring. Reading "
        "a local report is not an assertion, and in CI there would be no report to "
        "read — the same vacuity the Makefile comment above the target records as "
        "the reason it is not wired into `lint` either."
    ),
    "coverage-all": (
        "Measures every tree and prints each one's figure. Like `coverage` above it has "
        "no threshold and no failure mode of its own -- it exits non-zero only if a "
        "tree's own tests fail, which the test gates already report -- so there is no "
        "verdict for CI to carry. What both CIs run is `coverage-all-cicd`, which is "
        "in SHARED_GATES: same producer, but with a concurrency budget and skipping "
        "the tree an earlier CI step already measured. This unbudgeted form "
        "is the local one."
    ),
    "coverage-summary": (
        "Prints the recorded per-tree figures out of scripts/coverage_debt.json without "
        "measuring anything. Reading a committed file is not an assertion. Registered "
        "separately from `coverage-all` because this registry is per-target by design: "
        "one entry, one target's worth of reason."
    ),
    "test-circuit-breaker": (
        "Three circuit-breaker test paths under src/lambda, verbosely. Same "
        "residual as `test-capacity`: covered by `make test`, not by a CI target."
    ),
    "dep-audit-fast": (
        "`dep-audit` reusing whatever is already in dist/manifests instead of "
        "regenerating it. A gate must not depend on a stale local artifact, so CI "
        "runs the regenerating form (scripts/security/dep_audit.py, in "
        "SHARED_GATES)."
    ),
    "dep-manifest": (
        "Generates dist/manifests/*.txt. It produces an input, and asserts "
        "nothing. The gate over its output is the dependency audit."
    ),
    # --- needs credentials, network, or a human -------------------------------
    "test-integration-all": (
        "Runs the integration-marked suites, which call live AWS. NO CI runs "
        "them — not GitLab either. GitLab's `integration_tests` stage is a "
        "different thing with a similar name: it triggers a CodePipeline stack "
        "deploy (scripts/sdlc/integration_test_deployment.py) and never invokes "
        "`pytest -m integration`. So the only thing that runs this tier is a "
        "person typing the target, which is how 14 of its 23 tests came to be "
        "failing for tooling reasons with nothing reporting it (#1307). That is "
        "a genuine residual, not a duplicate; it stays out of CI because it "
        "needs credentials GitHub has no OIDC role for, and because it bills "
        "Bedrock per run. `test_the_integration_pytest_tier_runs_in_no_ci` "
        "below measures the claim rather than asserting the stage name."
    ),
    "check-retired-models": (
        "Asks Bedrock whether a model this repo offers has been retired. Needs AWS "
        "credentials, and the answer changes without any code change — so as a "
        "blocking gate it would red-line branches for an external event."
    ),
    "check-branch-protection": (
        "Reads the live GitHub branch-protection setting. Needs a token with "
        "administration:read, which no CI token here has, and it reports `develop` "
        "unprotected for as long as that is the repository's actual setting — "
        "which it is, and which nobody working in the tree can change. What would "
        "make it gateable is the SETTING changing, not any work in this repo."
    ),
    "srt-fix": (
        "SRT's INTERACTIVE fix mode: it prompts, and edits files. Neither is "
        "possible in CI."
    ),
    "srt": (
        "The full local SRT workflow — clean, setup, scan, then optionally the "
        "interactive fixer. CI runs the non-interactive halves it composes, "
        "`srt-setup` and `srt-scan`, as separate steps."
    ),
    "srt-clean": (
        "Deletes gitignored build/temp directories so a LOCAL scan is not polluted "
        "by them. A CI checkout has no such leftovers, and a gate that deletes "
        "files is not one."
    ),
    "security-results": (
        "Curates a published snapshot into security/test-results/<version>/. It is "
        "a release deliverable driven by a human, and its live half needs a "
        "deployed stack (STACK_NAME)."
    ),
    # --- the honest residual --------------------------------------------------
    "test": (
        "Runs EVERY non-integration suite across ~64 test roots. Neither CI runs "
        "it; they run `test-cicd -C lib/idp_common_pkg` plus `test-packages-cicd`, "
        "which between them omit some roots `make test` reaches (the per-Lambda "
        "suites under src/lambda, for instance). That is a genuine coverage gap "
        "rather than a duplicate, and it is recorded as one in docs/testing.md. "
        "Closing it means adding roots to a CI target, not adding `make test` to "
        "CI: run_all_tests.py has no CI-safety contract and includes suites that "
        "are slow or environment-dependent."
    ),
}

#: Two of ``lint``'s prerequisites MUTATE the tree, so ``lint-cicd`` cannot invoke
#: them — it runs the check-only equivalent instead. This is a translation, not an
#: exemption: both halves still have to be present, and
#: :func:`test_lint_cicd_covers_what_local_lint_covers` accepts either spelling.
CHECK_ONLY_EQUIVALENTS = {
    "ruff-lint": "ruff check",
    "format": "ruff format --check",
}


def _github_ci_text() -> str:
    """Everything that makes up GitHub's CI configuration, as one blob.

    The **composite actions** are part of it, not a detail of it. GitHub Actions
    does not support YAML anchors, so when ``developer-tests.yml`` became four
    parallel jobs the shared setup had to move into
    ``.github/actions/*/action.yml`` — and that setup is where the ``ruff`` and
    ``cfn-lint`` pins now live. Reading only the workflow files reported both pins
    as missing from GitHub while they were present and working, which is the
    false-alarm direction: a parity guard that cries wolf gets muted.

    Globbed rather than listed so a second composite action is covered the day it
    is added.
    """
    parts = [GITHUB_TESTS.read_text(), GITHUB_SECURITY.read_text()]
    parts += [p.read_text() for p in sorted(GITHUB_ACTIONS_DIR.glob("*/action.yml"))]
    return "".join(parts)


def _pinned_tool_sources() -> list[tuple[str, str]]:
    """``(label, text)`` for each side's config, for the tool-pin assertions.

    The GitHub side is a BLOB rather than a single file for the reason
    :func:`_github_ci_text` gives: the pins live in a composite action now, so a
    per-file assertion reports a present, working pin as missing.
    """
    return [
        (GITLAB.name, GITLAB.read_text()),
        (".github/ (workflows + composite actions)", _github_ci_text()),
    ]


def _uncommented(text: str) -> str:
    """Drop whole-line comments and lines that only ``echo`` something.

    Both are places a gate's name appears without being invoked, and both were live
    false positives here. The ``Makefile``'s own error messages read "Please run
    'make ruff-lint' locally to fix these issues" — a sentence, inside an ``echo``,
    which a plain substring search reported as ``lint-cicd`` invoking the tree-
    mutating ``ruff-lint``. The CI configs likewise discuss gates in comments,
    including gates that have been removed.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.lstrip().lstrip("-").lstrip()
        if stripped.startswith(("#", "@#")):
            continue
        if stripped.lstrip("@").startswith("echo "):
            continue
        kept.append(line)
    return "\n".join(kept)


def _invokes(haystack: str, target: str) -> bool:
    """Does ``haystack`` invoke ``make <target>``?

    Word-boundary, not substring: ``make cfn-lint-RENAMED`` contains
    ``make cfn-lint``, so a plain ``in`` accepts a target that no longer exists.
    """
    return bool(re.search(rf"make {re.escape(target)}(?![A-Za-z0-9_.\-])", haystack))


def _makefile_targets() -> set[str]:
    """Every target defined in any Makefile a CI step could invoke.

    Not just the root one: CI runs ``make test-cicd -C lib/idp_common_pkg``, and that
    target is defined in the package's own Makefile. A staleness check that read only
    the root Makefile would report the repo's largest test gate as nonexistent.
    """
    sources = [
        MAKEFILE,
        *REPO_ROOT.glob("make/*.mk"),
        *REPO_ROOT.glob("lib/*/Makefile"),
    ]
    return {
        match.group(1)
        for path in sources
        if path.is_file()
        for match in re.finditer(
            r"^([A-Za-z][A-Za-z0-9_.-]*):(?!=)", path.read_text(), re.MULTILINE
        )
    }


def _is_check_shaped(target: str) -> bool:
    return target.startswith(CHECK_SHAPED_NAME_PREFIXES) or target.endswith(
        CHECK_SHAPED_NAME_SUFFIXES
    )


def _makefile_sections() -> dict[str, str]:
    """Every ``Makefile`` target mapped to the ``##@`` section it is defined under."""
    sections: dict[str, str] = {}
    section = ""
    for line in MAKEFILE.read_text().splitlines():
        if line.startswith("##@"):
            section = line[3:].strip()
            continue
        if line.startswith((".", "\t", " ", "#")):
            continue
        match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]*):(?!=)", line)
        if match:
            sections[match.group(1)] = section
    return sections


def gate_universe() -> dict[str, str]:
    """Every check-shaped ``Makefile`` target, mapped to the section it sits in.

    Derived rather than listed, because the case a list cannot express is a gate
    that is in the ``Makefile`` and in **neither** CI: it has no foothold in either
    config for a parity comparison to find.

    Two routes in, because neither alone is sufficient:

    1. **Any** target in a section that is entirely checks
       (:data:`GATE_SECTION_PREFIXES`) — this catches a check whose name says
       nothing, such as ``cfn-lint-warnings`` or ``api-test-static``.
    2. A **check-shaped** target anywhere else (:func:`_is_check_shaped`), except in
       the live-stack section — this catches a check in a section that is mostly not
       checks, which route 1 structurally cannot see. ``ui-lint`` is the case that
       matters: it is the gate this module's ``--max-warnings 0`` assertion is about
       and it lives under "UI Development".

    The live-stack section is excluded by :data:`LIVE_STACK_SECTION_PREFIXES`, whose
    premise holds for every member and which is count-pinned rather than trusted.
    """
    universe: dict[str, str] = {}
    for target, section in _makefile_sections().items():
        if section.startswith(GATE_SECTION_PREFIXES):
            universe[target] = section
        elif section.startswith(LIVE_STACK_SECTION_PREFIXES):
            continue
        elif _is_check_shaped(target):
            universe[target] = section
    return universe


def _ci_reaches(target: str) -> str | None:
    """How both CIs reach ``make <target>``, or None if they do not.

    Three routes, deliberately only one level deep — a recursive prerequisite walk
    would be the kind of engine whose own bugs produce a green gate:

    1. Both CI configs invoke ``make <target>`` directly.
    2. ``lint-cicd``'s recipe invokes it, and ``lint-cicd`` itself is in
       :data:`SHARED_GATES` and therefore asserted to run in both.
    3. Both CI configs name a script that the target's own recipe runs **with no
       options**. This is how the dependency audit is reached: both CIs call
       ``scripts/security/dep_audit.py`` rather than ``make dep-audit``.
    """
    gitlab = _uncommented(GITLAB.read_text())
    github = _uncommented(_github_ci_text())

    if _invokes(gitlab, target) and _invokes(github, target):
        return "invoked directly by both CIs"

    if _invokes(_uncommented(_lint_cicd_recipe()), target):
        return "invoked from `make lint-cicd`, which both CIs run"

    for script in _recipe_scripts(target):
        if script in gitlab and script in github:
            return f"both CIs run {script}, which is what this target runs"

    return None


def _recipe_scripts(target: str) -> set[str]:
    """Script paths ``target``'s recipe runs with NO options of its own.

    The option filter is what distinguishes a target from a *variant* of it.
    ``dep-audit`` runs ``scripts/security/dep_audit.py`` plainly and both CIs run
    that same script, so CI does cover it. ``dep-audit-fast`` runs the same script
    with ``--no-generate``, which skips the manifest regeneration the CI invocation
    performs — a different check, and reporting it as covered would be exactly the
    false reassurance this module is about.
    """
    found: set[str] = set()
    for line in _uncommented(_recipe(target)).splitlines():
        for match in re.finditer(r"scripts/[\w./-]+\.(?:py|sh)", line):
            if " --" in line[match.end() :] or line[match.end() :].strip().startswith(
                "--"
            ):
                continue
            found.add(match.group(0))
    return found


def _recipe(target: str) -> str:
    """The recipe lines of ``target`` — every line up to the next target or heading."""
    text = MAKEFILE.read_text()
    match = re.search(rf"^{re.escape(target)}:(?!=).*$", text, re.MULTILINE)
    if not match:
        return ""
    body_offset = match.end()
    following = re.search(
        r"^(?:[A-Za-z][A-Za-z0-9_.-]*:(?!=)|##@)", text[body_offset:], re.MULTILINE
    )
    end = body_offset + following.start() if following else len(text)
    return text[body_offset:end]


def _lint_cicd_recipe() -> str:
    """The body of the ``lint-cicd`` target, ending at the next target definition.

    A recipe line begins with a tab, so the first line matching ``^name:`` after
    the target's own header is the start of the next target. ``##@`` (a section
    heading) is also a terminator, for the case where ``lint-cicd`` is the last
    target in its section.
    """
    text = MAKEFILE.read_text()
    start = text.index("\nlint-cicd:") + 1
    body_offset = text.index("\n", start) + 1
    ends = [
        match.start() + body_offset
        for match in re.finditer(
            r"^(?:[A-Za-z0-9_.-]+:|##@)", text[body_offset:], re.MULTILINE
        )
    ]
    end = min(ends) if ends else len(text)
    return text[start:end]


@pytest.mark.unit
@pytest.mark.parametrize("gate", SHARED_GATES)
def test_gate_runs_in_both_cis(gate: str) -> None:
    """A gate present on one side only is invisible to work merged via the other.

    Comments are stripped first. Both configs discuss gates in prose — including
    ones that were deleted — so a raw substring search can find a gate in a config
    that does not run it.
    """
    in_gitlab = gate in _uncommented(GITLAB.read_text())
    in_github = gate in _uncommented(_github_ci_text())

    assert in_gitlab and in_github, (
        f"gate {gate!r} runs in "
        f"{'GitLab' if in_gitlab else 'GitHub' if in_github else 'NEITHER'} only. "
        f"Work merged through the other CI skips it. Add it to both, or remove it "
        f"from SHARED_GATES with a reason (e.g. it needs AWS credentials)."
    )


@pytest.mark.unit
def test_shared_gates_is_not_empty_and_names_real_targets() -> None:
    """Non-vacuity and staleness for the authored list.

    Two ways the list above could stop meaning anything without any test failing:
    it could be emptied, or an entry could name a ``make`` target that has been
    renamed — at which point the parity assertion is comparing a string that
    describes nothing.
    """
    assert len(SHARED_GATES) >= 8, (
        f"SHARED_GATES has shrunk to {len(SHARED_GATES)} entries. Gates are removed "
        "from CI parity only with a reason; shrinking this list is how parity stops "
        "being asserted."
    )

    targets = _makefile_targets()
    for gate in SHARED_GATES:
        if not gate.startswith("make "):
            continue
        name = gate.split()[1]
        assert name in targets, (
            f"SHARED_GATES names `make {name}`, which is not a target in the "
            f"Makefile. Both CIs may still contain the string, so the parity "
            f"assertion would pass while the gate does not exist."
        )


@pytest.mark.unit
def test_every_gate_shaped_target_is_in_both_cis_or_registered() -> None:
    """Universe closure: no check-shaped target may be unaccounted for.

    This is the blind spot a list of CI gates cannot cover. A target that is in the
    ``Makefile`` and in **neither** CI appears in neither config, so comparing the
    configs to each other finds nothing to report — they agree. ``make cfn-lint``
    and ``make validate-buildspec`` sat in exactly that position, in ``lint`` but
    not ``lint-cicd``, until a template error nearly reached deploy time.

    So the universe is derived from the ``Makefile`` and every member must be either
    reached by both CIs or registered in :data:`GATES_DELIBERATELY_OUT_OF_CI`. The
    consequence to expect: adding a check target to one of the gate sections fails
    this test until it is either wired into CI or given a reason.
    """
    universe = gate_universe()
    assert len(universe) > 20, (
        f"gate_universe() found only {len(universe)} targets, which is not a "
        f"plausible count for this Makefile — the section parsing has broken and "
        f"this whole check would pass vacuously. Found: {sorted(universe)}"
    )

    unaccounted = {
        target: section
        for target, section in universe.items()
        if _ci_reaches(target) is None and target not in GATES_DELIBERATELY_OUT_OF_CI
    }
    assert not unaccounted, (
        "these check-shaped Makefile targets run in neither CI and are not "
        f"registered as deliberately out of scope: {unaccounted}. Either wire the "
        "target into BOTH .gitlab-ci.yml and .github/workflows/, or add an entry to "
        "GATES_DELIBERATELY_OUT_OF_CI saying — for that one target — why not. A "
        "gate in neither CI is invisible to the parity comparison, which is why it "
        "has to be named here."
    )


@pytest.mark.unit
def test_the_name_shape_route_into_the_universe_matches_something() -> None:
    """Non-vacuity for :data:`CHECK_SHAPED_NAME_PREFIXES`.

    If the name-shape route ever matched nothing, ``GATE_SECTION_PREFIXES`` would
    silently become the whole universe again and a check outside the five scanned
    sections would be invisible — with every test here still green. So assert it is
    load-bearing, and name the three targets it currently carries.
    """
    scanned_sections = {
        target
        for target, section in _makefile_sections().items()
        if section.startswith(GATE_SECTION_PREFIXES)
    }
    by_name_shape = set(gate_universe()) - scanned_sections

    assert by_name_shape, (
        "no target reaches the universe by name shape, so gate_universe() is now "
        "just the five scanned sections. A check added to any other section would "
        "not be seen. Check CHECK_SHAPED_NAME_PREFIXES/SUFFIXES."
    )
    for expected in ("ui-lint", "ui-test", "codegen-check"):
        assert expected in by_name_shape, (
            f"{expected!r} no longer reaches the universe by name shape. It is a "
            f"check outside the scanned sections, so losing it means losing the "
            f"guarantee that it is classified at all."
        )


@pytest.mark.unit
def test_the_live_stack_section_exclusion_is_count_pinned() -> None:
    """Count-pinning for the one section excluded wholesale.

    Its premise — every member needs a live deployed stack — holds for every target
    there today, and the section heading says so. What a blanket exclusion cannot do
    is notice a *new* member that does not share the premise, so the count is pinned:
    adding a target to "Stack tests" is a deliberate edit here, which is the moment
    to check the premise still holds for it.
    """
    members = sorted(
        target
        for target, section in _makefile_sections().items()
        if section.startswith(LIVE_STACK_SECTION_PREFIXES)
    )
    assert members, (
        f"no Makefile section starts with {LIVE_STACK_SECTION_PREFIXES!r}, so this "
        "exclusion now covers nothing and the count below is meaningless."
    )
    assert len(members) == LIVE_STACK_SECTION_TARGET_COUNT, (
        f"the '{LIVE_STACK_SECTION_PREFIXES}' section now has {len(members)} targets, "
        f"not the {LIVE_STACK_SECTION_TARGET_COUNT} audited when this wholesale "
        f"exclusion was written. Confirm the new one really does need a live "
        f"deployed stack — if it does not, it belongs in a scanned section — then "
        f"update LIVE_STACK_SECTION_TARGET_COUNT. Members: {members}"
    )


@pytest.mark.unit
def test_no_registered_target_is_actually_in_ci() -> None:
    """Non-vacuity for the registry: a reason for a gate that now runs is dead.

    Left in place, it pre-exempts whatever next occupies the name — and it states
    something false about the tree, which is worse than stating nothing.
    """
    contradicted = {
        target: route
        for target in GATES_DELIBERATELY_OUT_OF_CI
        if (route := _ci_reaches(target)) is not None
    }
    assert not contradicted, (
        "GATES_DELIBERATELY_OUT_OF_CI says these targets are not in CI, but they "
        f"are: {contradicted}. Delete the entries — a stale exemption pre-exempts "
        "whatever next takes the name."
    )


@pytest.mark.unit
def test_every_registered_target_still_exists() -> None:
    """Staleness: an entry for a target that has been renamed or deleted fails."""
    universe = gate_universe()
    vanished = sorted(set(GATES_DELIBERATELY_OUT_OF_CI) - set(universe))
    assert not vanished, (
        f"GATES_DELIBERATELY_OUT_OF_CI has entries for {vanished}, which are not "
        f"targets in a gate section of the Makefile any more. Delete them; an entry "
        f"that matches nothing is indistinguishable from one that is working."
    )


@pytest.mark.unit
def test_every_registered_target_has_a_real_reason() -> None:
    """A reason answers for ONE target. An empty or boilerplate one does not."""
    for target, reason in GATES_DELIBERATELY_OUT_OF_CI.items():
        assert len(reason) > 60, (
            f"the reason for {target!r} is {len(reason)} characters. One entry, one "
            f"target's worth of reason: say what this target does and why CI cannot "
            f"or should not run it."
        )


def _lint_prerequisites() -> list[str]:
    """``lint``'s prerequisite targets, read from its own rule line.

    Derived rather than listed. The hardcoded list this replaced named 8 of the 12,
    so four of ``make lint``'s prerequisites could have been dropped from
    ``lint-cicd`` with every test here green.
    """
    match = re.search(r"^lint:([^#\n]*)", MAKEFILE.read_text(), re.MULTILINE)
    assert match, "the `lint` target no longer has a prerequisite line"
    return match.group(1).split()


@pytest.mark.unit
def test_lint_cicd_covers_what_local_lint_covers() -> None:
    """``lint-cicd`` must not be a weaker gate than a developer's ``make lint``.

    ``cfn-lint`` and ``validate-buildspec`` were in ``lint``/``fastlint`` only, so
    CI ran neither — the gap this file exists to catch.

    The slice this reads is the recipe and nothing else. It used to run from
    ``lint-cicd:`` to the next ``##@`` section heading, which is **320 lines** and
    contains the target *definitions* of ten other gates — so a gate name was found
    in the slice whether ``lint-cicd`` invoked it or not, and deleting an entire
    ``@if ! make <gate>`` block from the recipe left these tests passing. A control
    that cannot fail is not a control. It also asserts ``make <gate>`` rather than
    the bare name, and strips comments, so a mention in prose does not satisfy it —
    the recipe's own error messages say "Please run 'make ruff-lint' locally", and
    that sentence is not an invocation.
    """
    prerequisites = _lint_prerequisites()
    assert len(prerequisites) >= 10, (
        f"`lint` has only {len(prerequisites)} prerequisites ({prerequisites}), "
        f"which is fewer than this repo has gates — check the parsing before "
        f"trusting the result."
    )

    recipe = _uncommented(_lint_cicd_recipe())
    missing = []
    for gate in prerequisites:
        if _invokes(recipe, gate):
            continue
        equivalent = CHECK_ONLY_EQUIVALENTS.get(gate)
        if equivalent and equivalent in recipe:
            continue
        missing.append(gate)

    assert not missing, (
        f"these prerequisites of `make lint` are not reached from `make lint-cicd` "
        f"{missing}, so neither CI runs them even though a developer's `make lint` "
        f"does. Add them to lint-cicd, or — if the target mutates the tree and "
        f"cannot be a gate — register its check-only form in CHECK_ONLY_EQUIVALENTS."
    )


@pytest.mark.unit
def test_check_only_equivalents_are_all_real_lint_prerequisites() -> None:
    """Staleness for the translation map, which otherwise silently excuses a gate."""
    prerequisites = set(_lint_prerequisites())
    stray = sorted(set(CHECK_ONLY_EQUIVALENTS) - prerequisites)
    assert not stray, (
        f"CHECK_ONLY_EQUIVALENTS has entries for {stray}, which are not "
        f"prerequisites of `make lint`. An entry for a target that is not being "
        f"checked excuses nothing and hides the next thing that takes the name."
    )


@pytest.mark.unit
def test_the_ui_lint_gate_checks_rather_than_fixes() -> None:
    """A gate that repairs the tree it is checking has no failure mode.

    ``ui-lint`` is reached from ``make lint`` AND ``make lint-cicd``, and it used to
    run ``npm run lint -- --fix``. In CI that repaired the ephemeral checkout and
    then reported it clean, so ``prettier/prettier`` — configured ``'error'`` and
    entirely auto-fixable — could not fail. Separately, without
    ``--max-warnings 0`` every ``warn``-level rule was advisory forever.
    """
    recipe = _recipe("ui-lint")
    assert "npm run lint" in recipe, "`ui-lint` no longer runs the UI linter"
    assert "--fix" not in recipe, (
        "`ui-lint` passes --fix. It is the gate (both `make lint` and `make "
        "lint-cicd` reach it), so it must not mutate the tree it judges — in CI it "
        "would repair the checkout and report it clean. `make ui-lint-fix` is the "
        "fixer."
    )

    package_json = json.loads((REPO_ROOT / "src/ui/package.json").read_text())
    lint_script = package_json["scripts"]["lint"]
    assert "--max-warnings 0" in lint_script, (
        f"src/ui's `lint` script is {lint_script!r}, with no --max-warnings 0, so "
        f"every warn-level eslint rule is advisory and the gate cannot fail on one."
    )
    assert "--fix" not in lint_script, (
        f"src/ui's `lint` script is {lint_script!r} — the checking script must not "
        f"fix. `lint:fix` is the fixing one."
    )
    assert "--fix" in package_json["scripts"]["lint:fix"], (
        "src/ui has no `lint:fix` script that fixes, so removing --fix from the "
        "gate left contributors with no auto-fix entry point."
    )


@pytest.mark.unit
def test_the_ui_lint_skip_is_reported_as_a_skip() -> None:
    """A cache hit must not print a success marker.

    ``ui-lint`` caches on a checksum of ``src/ui`` and the skip branch printed a
    green ✅, so on a warm tree the target reported success for work that did not
    happen — both ``npm run lint`` and ``npm run typecheck``. That is the defect
    class this repository keeps filing against itself: a control that exists,
    reports success, and did not run. Issue #1152.

    This reads the two branches of the recipe's own conditional rather than the
    whole recipe, because the failure being guarded is precisely that one branch
    claims what the other earns.
    """
    recipe = _recipe("ui-lint")
    _, _, skip_branch = recipe.rpartition("else")
    assert skip_branch.strip(), (
        "`ui-lint`'s recipe no longer has an else branch, so this test is reading "
        "nothing. Re-derive the slice before trusting it."
    )
    assert "SKIP" in skip_branch.upper(), (
        f"`ui-lint`'s skip branch does not say it skipped: {skip_branch!r}. A reader "
        "scanning a long log needs the cache hit to be distinguishable from a pass."
    )
    assert "✅" not in skip_branch, (
        f"`ui-lint`'s skip branch prints a success marker: {skip_branch!r}. eslint "
        "and tsc did not run, so nothing was checked and nothing passed."
    )


@pytest.mark.unit
def test_lint_cicd_cannot_skip_the_ui_lint() -> None:
    """The CI-equivalent target must not be able to take the checksum cache.

    ``make lint-cicd`` exited 0 on a warm tree having skipped both ``npm run lint``
    and ``npm run typecheck``. CI could never hit that skip — ``.checksum`` is
    gitignored, so a fresh checkout has no stored hash and the comparison always
    differs — which makes it exactly the asymmetry this module exists to catch: the
    local command documented as mirroring CI was the one gate in the set whose green
    mark could mean nothing. ``lint`` and ``fastlint`` keep the cache, which is what
    it was added for.

    Read from ``make -n`` rather than from the file, so that a variable expansion or
    a moved recipe line is measured as make sees it.
    """
    dry_run = subprocess.run(
        ["make", "-n", "lint-cicd"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert dry_run.returncode == 0, (
        f"`make -n lint-cicd` failed ({dry_run.returncode}), so this assertion is "
        f"reading nothing:\n{dry_run.stderr[-2000:]}"
    )
    invocations = [
        line
        for line in _uncommented(dry_run.stdout).splitlines()
        if re.search(r"\bmake\s+ui-lint\b", line)
    ]
    assert invocations, (
        "`make -n lint-cicd` shows no `make ui-lint` invocation at all, so the UI "
        "lint is not reached from the CI-equivalent target. That is a bigger gap "
        f"than the one this test is about. Recipe seen:\n{dry_run.stdout[-2000:]}"
    )
    unguarded = [line for line in invocations if "UI_LINT_NO_SKIP" not in line]
    assert not unguarded, (
        f"`lint-cicd` invokes ui-lint without UI_LINT_NO_SKIP=1: {unguarded}. On a "
        "tree whose src/ui/.checksum matches, that invocation skips eslint and tsc "
        "and the target still exits 0 — a green mark for a gate that did not run."
    )
    condition = _recipe("ui-lint").split("then", 1)[0]
    assert "UI_LINT_NO_SKIP" in condition, (
        "`lint-cicd` passes UI_LINT_NO_SKIP=1 but `ui-lint`'s own condition does not "
        f"read it, so the flag does nothing: {condition!r}"
    )


@pytest.mark.unit
def test_the_recipe_slice_stops_at_the_recipe() -> None:
    """Anti-vacuity guard for the slice the test above depends on.

    If the slice ever widens to include other target definitions again, every
    assertion above starts passing for the wrong reason, silently.
    """
    recipe = _lint_cicd_recipe()
    stray = [
        match.group(1)
        for match in re.finditer(r"^([A-Za-z0-9_.-]+):", recipe, re.MULTILINE)
        if match.group(1) != "lint-cicd"
    ]
    assert not stray, (
        f"the lint-cicd slice reaches the definitions of other targets {stray}, so "
        "`make <gate>` can be found in the slice without lint-cicd invoking it. "
        "Narrow _lint_cicd_recipe()."
    )
    assert 10 < len(recipe.splitlines()) < 120, (
        f"the lint-cicd slice is {len(recipe.splitlines())} lines, which is not a "
        "plausible length for one recipe — check _lint_cicd_recipe()."
    )


@pytest.mark.unit
def test_cfn_lint_is_pinned_consistently() -> None:
    """An unpinned linter on a blocking gate can red-line the branch unprompted.

    A new cfn-lint release that promotes any check to ERROR class would fail every
    build with no code change, so the version is pinned — and the two CI configs
    must agree with the Makefile or CI and local runs diverge.
    """
    makefile = MAKEFILE.read_text()
    marker = "CFN_LINT_VERSION := "
    assert marker in makefile, "CFN_LINT_VERSION is no longer declared in the Makefile"
    version = makefile.split(marker, 1)[1].split("\n", 1)[0].strip()

    for label, text in _pinned_tool_sources():
        assert f"cfn-lint=={version}" in text, (
            f"{label} does not pin cfn-lint=={version} (the Makefile's "
            f"CFN_LINT_VERSION). CI would then run a different linter than "
            f"`make cfn-lint` does locally."
        )


def _lint_debt_module():
    """``scripts/check_lint_debt.py``, loaded by path.

    By path rather than by import, because ``scripts`` is not a package here.
    """
    spec = importlib.util.spec_from_file_location(
        "_check_lint_debt_under_test", REPO_ROOT / "scripts" / "check_lint_debt.py"
    )
    assert spec and spec.loader, "scripts/check_lint_debt.py is not loadable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declared_version(marker: str) -> str:
    """The value of a ``NAME := value`` pin declared in the ``Makefile``.

    A trailing comment is stripped, because ``NC := \\033[0m  # No Color`` is the
    house style for these declarations and a pin written the same way would
    otherwise compare as ``"1.2.3 # bumped"`` and never match anything.
    """
    makefile = MAKEFILE.read_text()
    match = re.search(rf"^{re.escape(marker)}\s*:?=\s*(.*)$", makefile, re.MULTILINE)
    assert match, f"{marker} is no longer declared in the Makefile"
    return match.group(1).split("#", 1)[0].strip()


@pytest.mark.unit
def test_ruff_is_pinned_consistently() -> None:
    """The ``Makefile`` and both CIs must pin one ``ruff``, and it must be installable.

    ``ruff``'s findings are version-dependent, and ``make check-lint-debt``
    compares a recorded per-file finding count against a live measurement — so
    two CI systems on different ``ruff`` releases would disagree about whether
    the baseline is current, and the disagreement would look like a code defect.
    ``lib/idp_common_pkg/pyproject.toml`` supplies ``ruff`` locally as a range, so
    the pin has to fall inside it or a contributor's ``make lint`` and CI are
    running different linters by construction.

    ⚠️ **The ``Makefile`` is the side this was missing.** Both CIs pinned ruff
    while the ``Makefile`` declared nothing and its lint recipes invoked whatever
    ``ruff`` was first on ``PATH``, so the local gate and the CI gate were the
    same measurement only by coincidence (issue #1381). The two versions in play
    did agree on the tree at the time — the defect is that nothing constructed
    that agreement, and ``ruff format`` output changes between releases as a diff
    on a file nobody touched, which is the hardest failure shape to attribute.
    ``RUFF_VERSION`` is now the single source of truth and this asserts the other
    three places agree with it: the two CI configs, and the lookup in
    ``scripts/check_lint_debt.py`` that names the pin when a count was measured
    under a different linter.

    A declared pin that nothing installs is not a pin, so the ``Makefile`` is
    also asserted to *use* ``$(RUFF_VERSION)`` in its install recipes and to
    report a mismatch at every ruff invocation. That is the direction a mutation
    gets through: deleting the ``ruff==$(RUFF_VERSION)`` from ``setup`` leaves a
    declaration, a comment and three agreeing CI configs all in place.
    """
    version = _declared_version("RUFF_VERSION")
    assert re.fullmatch(r"[0-9][0-9A-Za-z.\-]*", version), (
        f"RUFF_VERSION is {version!r}, which is not a version literal. The "
        f"comparisons below are exact string matches, so a variable reference or "
        f"a range here would make every one of them vacuous."
    )

    pins = {}
    for label, text in _pinned_tool_sources():
        found = re.findall(r"ruff==([0-9][0-9A-Za-z.\-]*)", text)
        assert found, f"{label} no longer pins a ruff version"
        assert len(set(found)) == 1, f"{label} pins several ruff versions: {found}"
        pins[label] = found[0]

    assert len(set(pins.values())) == 1, (
        f"the two CI configs pin different ruff versions: {pins}. `ruff check` and "
        "`make check-lint-debt` would then reach different verdicts depending on "
        "which CI a change was merged through."
    )

    # Exact equality, not containment: `ruff==0.15.130` contains `0.15.13`, so a
    # substring test would accept a pin two minor releases away.
    drifted = {label: pin for label, pin in pins.items() if pin != version}
    assert not drifted, (
        f"these CI configs pin a ruff version other than the Makefile's "
        f"RUFF_VERSION ({version}): {drifted}. CI would then run a different "
        f"linter than `make lint` does locally, and a formatting difference "
        f"between the two arrives as a diff on a file the change never touched."
    )

    # Counted over _uncommented(), which drops `echo` lines. The version note's
    # own remedy line reads `pip install 'ruff==$(RUFF_VERSION)'` inside an echo,
    # so a raw count is 3 and `>= 2` then passes with BOTH real installs intact
    # or ONE of them deleted — a count pinned against a number a sentence can
    # satisfy. Measured: dropping the `setup` install survived this assertion
    # until the echo lines came out of it.
    installs = re.findall(
        r"ruff==\$\(RUFF_VERSION\)", _uncommented(MAKEFILE.read_text())
    )
    assert len(installs) >= 2, (
        f"the Makefile installs `ruff==$(RUFF_VERSION)` in {len(installs)} "
        f"place(s). Both `setup` and `setup-venv` must install the pin, or an "
        f"environment built through this Makefile gets whatever the pyproject "
        f"range resolves to on the day and the declaration above is decoration."
    )

    for target in ("ruff-lint", "format", "lint-cicd"):
        assert "$(ruff_version_note)" in _recipe(target), (
            f"`{target}` invokes ruff without the version note. The recipes call "
            f"ruff as a bare command, so the pin is advisory at invocation time — "
            f"a mismatch between the installed ruff and RUFF_VERSION is reported "
            f"rather than enforced, and removing the report makes it silent, "
            f"which is the state issue #1381 describes."
        )

    # The note must compare against the MARKER, not against a copy of its value.
    # A literal here reports correctly today and then reports nothing — or
    # reports on every run — from the first bump onwards, which is the same
    # coincidental-agreement defect one level down.
    note = re.search(
        r"^define ruff_version_note$(.*?)^endef$",
        MAKEFILE.read_text(),
        re.MULTILINE | re.DOTALL,
    )
    assert note, "the Makefile no longer defines the ruff_version_note recipe"
    # _uncommented() again, and for the third time in this test it is the echo
    # lines that matter: the note's own remedy line reads `pip install
    # 'ruff==$(RUFF_VERSION)'`, so a body-wide substring search finds the marker
    # whether the COMPARISON uses it or a hardcoded copy of its value. Measured:
    # rewriting the comparison as `!= "0.15.13"` survived until this dropped the
    # echoes.
    comparison = _uncommented(note.group(1))
    assert "$(RUFF_VERSION)" in comparison, (
        f"the ruff_version_note recipe compares the running ruff against "
        f"something other than $(RUFF_VERSION):\n{comparison.strip()}\n"
        f"A copy of today's value reports correctly until the pin is bumped and "
        f"then reports on every run, or on none."
    )

    # CALLED, not grepped for. A textual check that the lookup mentions
    # RUFF_VERSION passes on a lookup that raises or returns None — which is the
    # exact state it was in, reading a file the pin had moved out of. Asserting
    # the returned value is the only form that distinguishes the two.
    assert _lint_debt_module()._ci_ruff_pin() == version, (
        f"scripts/check_lint_debt.py's _ci_ruff_pin() returns "
        f"{_lint_debt_module()._ci_ruff_pin()!r}, not RUFF_VERSION ({version}). "
        f"It previously read the pin out of a CI workflow file, the pin moved "
        f"into a composite action, and the lookup silently resolved to None — so "
        f"`make check-lint-debt` reported a finding-count mismatch measured "
        f"under a different linter with no hint that was the cause."
    )

    pyproject = (REPO_ROOT / "lib" / "idp_common_pkg" / "pyproject.toml").read_text()
    specifier = re.search(r'"ruff([^"]*)"', pyproject)
    assert specifier, "lib/idp_common_pkg/pyproject.toml no longer declares ruff"
    assert Version(version) in SpecifierSet(specifier.group(1)), (
        f"RUFF_VERSION is {version}, which is outside the range "
        f"{specifier.group(1)!r} that lib/idp_common_pkg installs locally. A "
        "developer's `make lint` would then run a different linter than CI."
    )


@pytest.mark.unit
def test_the_lint_debt_pin_lookup_follows_the_marker(tmp_path: Path) -> None:
    """``_ci_ruff_pin()`` must FOLLOW ``RUFF_VERSION``, not agree with it today.

    Comparing the lookup's answer to the marker's current value cannot tell a
    lookup that reads the ``Makefile`` apart from one that returns a copy of
    today's value — both are correct until the pin is bumped, and the second then
    reports a stale version while claiming to name the pin. That is the same
    coincidental-agreement shape as the defect in #1381 itself, so it is measured
    the only way that distinguishes them: point the module at a throwaway tree
    declaring a *different* version and require the lookup to change its answer.
    """
    module = _lint_debt_module()
    (tmp_path / "Makefile").write_text(
        "# throwaway tree for this assertion only\nRUFF_VERSION := 9.99.99\n"
    )
    module.REPO_ROOT = tmp_path
    assert module._ci_ruff_pin() == "9.99.99", (
        f"_ci_ruff_pin() returned {module._ci_ruff_pin()!r} for a tree whose "
        f"Makefile declares RUFF_VERSION := 9.99.99, so it is not reading the "
        f"marker. A lookup that hardcodes the version names a stale pin from the "
        f"first bump onwards, which is worse than naming none."
    )


#: The file classes that *execute* a ruff install, for the closure check below.
#: Globs rather than a file list, so a second composite action or a new setup
#: script is covered the day it is added — a third location that disagrees with
#: the pin is the way this defect comes back, and it comes back in a file nobody
#: thought to name.
#:
#: ``*.sh`` is every tracked shell script in the tree rather than
#: ``scripts/setup/*.sh``, because the directory a script sits in is not what
#: makes it an install surface: a measured probe put an unpinned
#: ``pip3 install ruff`` in ``scripts/install_dev_tools.sh`` and the narrower
#: glob did not read it.
RUFF_INSTALL_SURFACE_GLOBS = (
    "Makefile",
    "make/*.mk",
    ".gitlab-ci.yml",
    ".github/workflows/*.y*ml",
    ".github/actions/*/action.yml",
    "*.sh",
)


def _install_surface_files() -> list[str]:
    """Repo-relative paths matching :data:`RUFF_INSTALL_SURFACE_GLOBS`.

    Discovered through ``git ls-files``, including files not yet committed
    (``--others --exclude-standard``), for the two reasons that idiom exists
    elsewhere in this repository: a filesystem walk reads build output, a sibling
    worktree and ``.venv``, producing findings CI cannot reproduce; and a universe
    restricted to *committed* files changes its answer at ``git add`` time, so a
    newly written setup script would be judged only after it was already in.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [
        path
        for path in tracked
        if any(PurePosixPath(path).match(glob) for glob in RUFF_INSTALL_SURFACE_GLOBS)
    ]


@pytest.mark.unit
def test_every_executing_ruff_install_pins_the_version() -> None:
    """Closure: nothing that *runs* a ruff install may leave it unpinned.

    Comparing the ``Makefile`` to the two CI configs cannot see a fourth place.
    ``scripts/setup/dev_setup_ubuntu.sh`` was exactly that — a
    ``pip3 install virtualenv typer ruff`` with no pin, building the environment
    a developer then runs ``make lint`` in, with every pin assertion above green
    because that file is in none of them.

    So the universe is derived from :data:`RUFF_INSTALL_SURFACE_GLOBS`, at every
    depth, and every member that installs ruff must pin ``RUFF_VERSION``.

    Two boundaries, stated so a green run is not over-read. ``echo``'d lines are
    dropped by :func:`_uncommented`, so a script *telling a human* to
    ``pip install ... ruff`` is not covered — nor are the equivalent lines in
    ``docs/``. Those are instructions rather than installs, and they leave
    ``cfn-lint`` unpinned in the same breath, so pinning one linter there and not
    the other would be a half-measure dressed as a gate. And the check reads
    files, not what a shell would do, so an install assembled from a variable is
    invisible to it.
    """
    version = _declared_version("RUFF_VERSION")
    # Boundary-anchored, not a substring: `ruff==0.15.13` IS a prefix of
    # `ruff==0.15.130`, so the obvious `f"ruff=={version}" in line` accepts a pin
    # a release away from the one it claims to be checking. Measured: that form
    # passed `'ruff==0.15.130'` in the Ubuntu setup script.
    pinned = re.compile(
        rf"ruff==(?:{re.escape(version)}|\$\(RUFF_VERSION\))(?![\w.\-])"
    )
    checked, unpinned = [], []
    for relative in _install_surface_files():
        path = REPO_ROOT / relative
        if not path.is_file():
            continue
        for line in _uncommented(path.read_text()).splitlines():
            # Case-insensitive and inside a word, so the installer variable `make
            # setup` uses (`$$SETUP_PIP install`) is read. Matching only the bare
            # words left that line unchecked, and the floor below then held only
            # because the Ubuntu setup script installed ruff too (#1758).
            if not re.search(r"(?i)\b(\w*pip\d*|uv pip)\b.*\binstall\b", line):
                continue
            if not re.search(r"(?<![\w.-])ruff(?![\w.-])", line):
                continue
            checked.append(relative)
            if not pinned.search(line):
                unpinned.append(f"{relative}: {line.strip()}")

    assert len(checked) >= 4, (
        f"only {len(checked)} ruff install line(s) were found ({checked}), which "
        f"is fewer than the Makefile's two plus one per CI config. The globs or "
        f"the line patterns have stopped matching and this check would pass "
        f"vacuously — the failure mode it exists to prevent."
    )
    assert not unpinned, (
        f"these lines install ruff without pinning RUFF_VERSION ({version}): "
        f"{unpinned}. Whatever they install is what `ruff check` and "
        f"`ruff format` then measure, and findings and formatting are both "
        f"properties of the linter version."
    )


IDP_COMMON_LOCK = REPO_ROOT / "lib" / "idp_common_pkg" / "uv.lock"
GITHUB_PYTHON_ENV = GITHUB_ACTIONS_DIR / "python-env" / "action.yml"
LOCK_CONSTRAINTS_REF = '-c "$LOCK_CONSTRAINTS"'


def _locked_version(name: str) -> str:
    """The version ``lib/idp_common_pkg/uv.lock`` records for ``name``."""
    import tomllib

    lock = tomllib.loads(IDP_COMMON_LOCK.read_text())
    versions = {p["version"] for p in lock["package"] if p["name"] == name}
    assert len(versions) == 1, f"uv.lock records {name} at {sorted(versions)}"
    return versions.pop()


def _ci_python_setups() -> list[tuple[str, list[str]]]:
    """``(label, commands)`` for the step each CI builds its Python environment in.

    GitLab's is the ``.python_setup`` anchor, one command per list item (a folded
    multi-line item is one command). GitHub's is the install step of the
    ``python-env`` composite action, one command per uncommented line.
    """
    gitlab = yaml.safe_load(GITLAB.read_text())[".python_setup"]
    action = yaml.safe_load(GITHUB_PYTHON_ENV.read_text())
    (install,) = [
        step["run"]
        for step in action["runs"]["steps"]
        if step.get("name") == "Install Python dependencies"
    ]
    github = [line.strip() for line in _uncommented(install).splitlines()]
    return [
        (GITLAB.name, gitlab),
        (str(GITHUB_PYTHON_ENV.relative_to(REPO_ROOT)), github),
    ]


@pytest.mark.unit
def test_the_lock_pins_the_ruff_the_gate_runs() -> None:
    """``uv.lock`` and ``RUFF_VERSION`` must name one ruff.

    Every install in both CIs is constrained to the lock (below), so a lock that
    pins another ruff makes the ``ruff==RUFF_VERSION`` install unsatisfiable —
    and an environment built from the lock alone lints with a ruff the gate does
    not run, which is how the two drifted apart (#1707). Bump them together:
    a Dependabot update of ruff in the lock fails here until ``RUFF_VERSION``
    follows it.
    """
    assert _locked_version("ruff") == _declared_version("RUFF_VERSION"), (
        f"lib/idp_common_pkg/uv.lock pins ruff {_locked_version('ruff')} but "
        f"RUFF_VERSION is {_declared_version('RUFF_VERSION')}. Re-lock with "
        f"`uv lock --upgrade-package ruff==<RUFF_VERSION>` in lib/idp_common_pkg, "
        f"or move RUFF_VERSION, so the lock and the gate agree."
    )


#: An install into the interpreter the job runs: uv's pip, a pip, or ``python -m
#: pip``. Whitespace-tolerant, because ``uv  pip install`` is the same command.
_INSTALL_COMMAND = re.compile(
    r"(?<![\w.-])(?:uv\s+pip|pip3?|python3?\s+-m\s+pip)\s+install(?![\w-])"
)
#: An install into the ENVIRONMENT after its setup step. A bare ``pip`` is left out
#: here on purpose: after GitLab activates a ``uv venv``, which has no pip, a bare
#: ``pip`` is the image's system pip, and installs outside the environment the
#: tests run in.
_ENV_INSTALL_COMMAND = re.compile(
    r"(?<![\w.-])(?:uv\s+pip|python3?\s+-m\s+pip)\s+install(?![\w-])"
)


def _subcommands(command: str) -> list[str]:
    """One shell command line split at ``&&``, ``||``, ``;`` and ``|``."""
    return [part.strip() for part in re.split(r"&&|\|\||;|\|", command) if part.strip()]


def _flatten(items) -> list[str]:
    """GitLab script lists with anchors expanded (an alias arrives as a nested list)."""
    flat: list[str] = []
    for item in items or []:
        flat.extend(_flatten(item) if isinstance(item, list) else [str(item)])
    return flat


@pytest.mark.unit
def test_every_ci_install_is_constrained_to_the_lock() -> None:
    """Both CIs install against ``uv.lock``, by the same mechanism (#1707).

    Unconstrained, the install resolved every dependency against the index on the
    day, and CI tested a different dependency set from the one the lock records.
    What is asserted, per CI: the constraints file is exported from the lock once,
    before the first install, and it is the file the ``Makefile`` names; every
    install in the environment step carries it, read per sub-command so one chained
    after ``&&`` is not hidden by the ``-c`` of the line it rides on — a tool
    installed first without it would leave a shared dependency at an unlocked
    version for the constrained pass to fight over; and no step after the
    environment step installs into the environment again.

    The boundary of that last check: it reads the literal install commands in the
    CI YAML and the one known reinstall (``make test-cicd`` in
    ``lib/idp_common_pkg``) spelled with the directory on the command line. An
    install made inside some other Makefile recipe (``make dev``, ``make setup``),
    a ``cd`` before the ``make``, ``uv pip sync`` / ``uv run --with``, or an
    ``--override`` beside the ``-c`` are not recognised. None exists in either CI
    today.
    """
    makefile = MAKEFILE.read_text()
    default = re.search(r"^LOCK_CONSTRAINTS \?= (.*)$", makefile, re.M)
    assert default, "the Makefile no longer declares LOCK_CONSTRAINTS"
    relative = default.group(1).strip().removeprefix("$(CURDIR)/")

    for label, commands in _ci_python_setups():
        parts = [part for command in commands for part in _subcommands(command)]
        # What runs before the environment exists (GitLab's `pip install uv` into
        # the image) installs the tooling, not the environment under test.
        venv = [i for i, c in enumerate(parts) if c.startswith("uv venv")]
        parts = parts[venv[-1] + 1 :] if venv else parts
        installs = [i for i, c in enumerate(parts) if _INSTALL_COMMAND.search(c)]
        assert installs, f"{label}: no install found; this check would be vacuous"
        exports = [i for i, c in enumerate(parts) if "LOCK_CONSTRAINTS=" in c]
        assert len(exports) == 1 and parts[exports[0]].rstrip('"').endswith(relative), (
            f"{label} must set LOCK_CONSTRAINTS exactly once, to .../{relative}; "
            f"found {[parts[i] for i in exports]}"
        )
        made = [i for i, c in enumerate(parts) if c == "make lock-constraints"]
        assert made, f"{label} no longer runs `make lock-constraints`"
        assert exports[0] < made[0] < installs[0], (
            f"{label}: the lock must be exported before the first install"
        )
        unconstrained = [
            parts[i]
            for i in installs
            if LOCK_CONSTRAINTS_REF not in re.sub(r"\s+", " ", parts[i])
        ]
        assert not unconstrained, (
            f"{label} installs without {LOCK_CONSTRAINTS_REF}: {unconstrained}"
        )

    # After the environment step: GitLab jobs that use `.python_setup`, and every
    # step of the two GitHub workflows (the composite action is not a step's `run`).
    setup = set(_flatten(yaml.safe_load(GITLAB.read_text())[".python_setup"]))
    later: list[tuple[str, str]] = []
    for name, body in yaml.safe_load(GITLAB.read_text()).items():
        if not isinstance(body, dict) or "script" not in body:
            continue
        lines = _flatten(body.get("before_script")) + _flatten(body.get("script"))
        if not setup <= set(lines):
            continue
        later += [
            (f"{GITLAB.name}:{name}", line) for line in lines if line not in setup
        ]
    for workflow in (GITHUB_TESTS, GITHUB_SECURITY):
        for name, steps in _github_job_steps(workflow).items():
            later += [(f"{workflow.name}:{name}", step) for step in steps]
    reinstalls = [
        (where, part)
        for where, text in later
        for line in _uncommented(text).splitlines()
        for part in _subcommands(line)
        if _ENV_INSTALL_COMMAND.search(part)
        or (
            re.search(
                r"make\b.*\btest-cicd\b.*lib/idp_common_pkg|make\b.*-C\s+lib/idp_common_pkg\b.*\btest-(unit-)?cicd\b",
                part,
            )
            and "SKIP_INSTALL=1" not in part
        )
    ]
    assert not reinstalls, (
        f"these install into the CI environment after its lock-constrained setup "
        f"step, so whatever they resolve is not the lock: {reinstalls}. Install in "
        f"the setup step, or pass SKIP_INSTALL=1 to lib/idp_common_pkg's targets."
    )

    assert "lock-constraints" in re.search(
        r"^install-first-party:(.*)$", makefile, re.M
    ).group(1), "install-first-party no longer depends on lock-constraints"
    assert re.search(
        r"\$\(PIP\) install -c \"\$\(LOCK_CONSTRAINTS\)\" \$\(FIRST_PARTY_EDITABLES\)",
        _uncommented(_recipe("install-first-party")),
    ), "install-first-party no longer installs with -c $(LOCK_CONSTRAINTS)"
    export = re.sub(r"\\\n\s*", " ", _uncommented(_recipe("lock-constraints")))
    assert re.search(r"uv export --locked\b", export), (
        "lock-constraints no longer exports with --locked, so a lock that has "
        "fallen behind pyproject.toml would be exported without complaint"
    )
    assert "--all-extras" in export and "--all-groups" in export, (
        "lock-constraints no longer exports every extra and group, so packages "
        "outside the narrower selection would install unconstrained"
    )
    assert not re.search(r"--only-|--no-dev\b|--extra\b|--group\b", export), (
        f"lock-constraints narrows the export: {export.strip()}"
    )


def _installer_segments(recipe: str) -> list[str]:
    """Each shell command in ``recipe`` that runs an installer's ``install``.

    The rule is about the installer, not about a spelling of it: a command counts
    when the word before ``install`` names pip in any form (``$$SETUP_PIP``,
    ``$(VENV_DIR)/bin/pip``, ``python -m pip``, ``uv pip``, ``pip3``). Bound: an
    installer reached through a variable whose name does not contain ``pip``, or a
    different subcommand (``uv sync``), is not recognised.
    """
    joined = re.sub(r"\\\n\s*", " ", _uncommented(recipe))
    segments = re.split(r"&&|\|\||;|\||\n", joined)
    found = []
    for segment in segments:
        words = segment.split()
        for i, word in enumerate(words[1:], start=1):
            if word == "install" and "pip" in words[i - 1].lower():
                found.append(" ".join(words))
                break
    return found


@pytest.mark.unit
@pytest.mark.parametrize("target", ["setup", "setup-venv"])
def test_the_setup_targets_install_against_the_lock(target: str) -> None:
    """``make setup`` and ``make setup-venv`` hold the versions CI tests (#1729).

    Both CIs install against ``lib/idp_common_pkg/uv.lock`` (#1707); the two setup
    targets did not, so a contributor's environment resolved afresh from the index
    and could differ from CI on any package. Each must export the lock first and
    pass it to every install. The one exemption is pip upgrading itself, which is
    the installer rather than part of the environment under test.
    """
    header = re.search(rf"^{re.escape(target)}:(.*)$", MAKEFILE.read_text(), re.M)
    assert header and "lock-constraints" in header.group(1).split("##")[0], (
        f"`{target}` no longer depends on lock-constraints, so the constraints "
        f"file it passes to -c may be absent or stale"
    )
    installs = _installer_segments(_recipe(target))
    # Non-vacuity: the recipe installs the first-party packages, the capacity test
    # requirements, cfn-lint and ruff. Finding none would mean the parse is wrong.
    assert len(installs) >= 4, f"`{target}`: only found installs {installs}"
    constrained = re.compile(
        r'(?:^|\s)(?:-c|--constraint)[\s=]"\$\(LOCK_CONSTRAINTS\)"'
    )
    self_upgrade = (["--upgrade", "pip"], ["-U", "pip"])
    unconstrained = [
        cmd
        for cmd in installs
        if not constrained.search(cmd)
        and cmd.split(" install ", 1)[-1].split() not in self_upgrade
    ]
    assert not unconstrained, (
        f'`{target}` installs without -c "$(LOCK_CONSTRAINTS)": {unconstrained}. '
        f"Whatever these resolve is not the version CI tests."
    )


def _first_party_requirements() -> list[tuple[str, str]]:
    """``(pyproject, requirement)`` for every requirement of every ``lib/*`` package.

    Runtime dependencies, every extra and every dependency group, so a requirement
    is counted whichever selection a CI or ``make setup`` happens to install.
    """
    import tomllib

    pairs = []
    for pyproject in sorted(REPO_ROOT.glob("lib/*/pyproject.toml")):
        data = tomllib.loads(pyproject.read_text())
        project = data["project"]
        reqs = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            reqs += extra
        for group in data.get("dependency-groups", {}).values():
            reqs += [r for r in group if isinstance(r, str)]  # skip include-group
        rel = str(pyproject.relative_to(REPO_ROOT))
        pairs += [(rel, r) for r in reqs]
    return pairs


@pytest.mark.unit
def test_every_third_party_requirement_of_a_first_party_package_is_locked() -> None:
    """Nothing a first-party package requires installs at an unlocked version (#1729).

    Both CIs and ``make setup`` install all five ``lib/*`` packages constrained to
    ``lib/idp_common_pkg/uv.lock``, and a constraint pins only a package the lock
    records. A third-party requirement of one of the OTHER four that the lock does
    not hold therefore resolves from the index on the day of the install — which is
    how ``semver`` and ``pytest-mock`` floated after #1707 while every lock check
    passed. The remedy is to name it in idp_common_pkg's ``first-party-siblings``
    dependency group and re-lock.

    Bound: this checks distribution names. An extra of a locked package that the
    lock never resolved (``moto[dynamodb]`` against a lock that requested other moto
    extras) is not checked; its dependencies happen to be locked today.
    """
    import tomllib

    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    first_party = {
        canonicalize_name(tomllib.loads(p.read_text())["project"]["name"])
        for p in REPO_ROOT.glob("lib/*/pyproject.toml")
    }
    assert len(first_party) >= 5, f"lib/*/pyproject.toml found only {first_party}"
    locked = {
        canonicalize_name(p["name"])
        for p in tomllib.loads(IDP_COMMON_LOCK.read_text())["package"]
    }
    requirements = _first_party_requirements()
    third_party = [
        (where, req)
        for where, req in requirements
        if canonicalize_name(Requirement(req).name) not in first_party
    ]
    # An empty universe would pass vacuously; the five packages require far more.
    assert len(third_party) > 50, f"only {len(third_party)} requirements were read"
    unlocked = [
        f"{where}: {req}"
        for where, req in third_party
        if canonicalize_name(Requirement(req).name) not in locked
    ]
    assert not unlocked, (
        "these first-party requirements name a distribution "
        "lib/idp_common_pkg/uv.lock does not record, so every lock-constrained "
        f"install resolves it from the index instead: {unlocked}. Add it to the "
        "`first-party-siblings` dependency group in lib/idp_common_pkg/pyproject.toml "
        "and run `uv lock` there."
    )


@pytest.mark.unit
def test_integration_tests_stay_gitlab_only() -> None:
    """Documents the ONE deliberate asymmetry, so it cannot drift unnoticed.

    ⚠️ This is about the GitLab **stage** of that name — a CodePipeline stack
    deploy — and nothing more. It does NOT say the pytest ``integration`` marker
    tier runs there; see the test below, which is the one that measures that.
    Reading this assertion as coverage of that tier is the mistake that let
    #1307 sit: a `test-integration-all` exemption cited this test as its ratchet,
    and a string check over a stage name cannot carry that claim.
    """
    assert "integration_tests" in GITLAB.read_text()
    assert "integration_tests" not in _github_ci_text(), (
        "integration_tests appeared in GitHub CI. It needs AWS credentials; if "
        "that is now intended, update this test and CI_TEST_COVERAGE.md."
    )


@pytest.mark.unit
def test_the_integration_pytest_tier_runs_in_no_ci() -> None:
    """The ``-m integration`` tier is run by no CI, and that must stay measured.

    The reason this is worth a test rather than a comment: the two things are
    easy to conflate by name. GitLab's `integration_tests` stage runs
    `scripts/sdlc/integration_test_deployment.py`, which deploys a stack through
    CodePipeline — it never invokes `pytest -m integration`, so the pytest tier
    was covered nowhere while the exemption for it said GitLab had it. 14 of its
    23 tests were failing on tooling faults, and the first thing to notice was a
    release validation months later (#1307).

    So: assert the absence directly, over every CI configuration and every
    buildspec. If a CI starts running the tier, this test fails and the
    `test-integration-all` exemption above must be rewritten — which is the
    point. It is an inverted ratchet: the exemption is only honest while this
    holds.

    ⚠️ **The universe is derived, not listed.** An absence measured over a
    hardcoded inventory is the failure mode the rest of this file exists to
    avoid: a workflow added later, or a buildspec outside the directory someone
    happened to glob, would invoke the tier and leave this green. Every
    `.github/workflows/*.y*ml` and every tracked `buildspec*.yml` is read,
    discovered at run time, so a workflow or buildspec added later is covered
    without being named.

    The glob is `*.y*ml` rather than `*.yml` because GitHub Actions reads both
    extensions, so a workflow added as `.yaml` would otherwise sit outside a
    universe this calls closed — and the non-vacuity guard below would not
    notice, because the other four files would still be found.
    """
    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
    buildspecs = sorted(
        REPO_ROOT / line
        for line in subprocess.run(
            ["git", "ls-files", "*buildspec*.yml"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )
    runners = [
        GITLAB,
        *workflows,
        REPO_ROOT / "scripts" / "sdlc" / "integration_test_deployment.py",
        *buildspecs,
    ]
    # Discovery returning nothing would make the assertion below vacuous, which
    # is how an absence check quietly stops checking anything.
    assert workflows, "no GitHub workflow discovered"
    assert buildspecs, "no buildspec discovered"
    for named in (GITHUB_TESTS, GITHUB_SECURITY):
        assert named in workflows, (
            f"{named.name} is not in the discovered workflow set, so discovery "
            "has stopped reaching the files this suite reads elsewhere"
        )

    present = [p for p in runners if p.exists()]
    assert len(present) == len(runners), (
        f"expected to read all of {[p.name for p in runners]}; missing "
        f"{[p.name for p in runners if not p.exists()]} — this test cannot "
        "report an absence it never looked for"
    )

    # Every spelling that starts the tier, not just the `make` one. The whole-tree
    # runner takes `--integration` (root Makefile's `test-integration-all` is
    # `run_all_tests.py --integration`), and a buildspec calling that script
    # directly is the natural way to run the tier from CI — it would have left this
    # check green while running every test the check is about. The marker selector
    # is listed in all three quotings because a shell or YAML author picks freely
    # between them and an unlisted one is an invisible hole, not a near miss.
    invocations = (
        "test-integration",
        "--integration",
        "-m integration",
        '-m "integration"',
        "-m 'integration'",
    )
    found = {
        p.relative_to(REPO_ROOT).as_posix(): [
            needle for needle in invocations if needle in _uncommented(p.read_text())
        ]
        for p in present
    }
    offenders = {path: hits for path, hits in found.items() if hits}
    assert not offenders, (
        "the pytest `integration` tier is now invoked from CI "
        f"({offenders}). That is a real improvement, but the "
        "`test-integration-all` entry in GATES_DELIBERATELY_OUT_OF_CI says the "
        "opposite — rewrite it, and delete this test or invert it."
    )


# --------------------------------------------------------------------------- #
# per-job toolchains: a suite's inputs have to reach the job that runs it,
# before the step that runs it
# --------------------------------------------------------------------------- #

#: The ``make`` targets through which a CI job reaches ``scripts/tests``, and so
#: ``scripts/tests/test_ui_test_discovery.py``, whose three calibrations consult the
#: installed Vitest. ``test-packages-cicd`` runs that suite directly;
#: ``coverage-all-cicd`` re-runs it in order to measure it, which drags the same
#: toolchain requirement into a job whose name and ``script:`` block say nothing
#: about it. Both spellings are listed because either one alone would leave a job
#: that runs the suite outside the universe below.
SCRIPTS_SUITE_TARGETS = ("test-packages-cicd", "coverage-all-cicd")

#: The composite-action input by which a GitHub job asks for the UI's npm
#: dependencies. GitHub Actions has no YAML anchors, so the install itself lives in
#: ``.github/actions/python-env/action.yml`` and the job's side of it is this input
#: — which is a string, and proves nothing on its own. That is why
#: :func:`test_the_github_ui_deps_input_actually_installs_the_dependencies` is a
#: separate assertion rather than a clause of the one before it.
UI_DEPS_INPUT = "ui-deps"


def _gitlab_job_commands() -> dict[str, list[str]]:
    """``{job name: its before_script + script}``, in order, merge keys resolved.

    A list rather than one blob because the order is part of what is asserted: an
    install that lands after the step it is installing for is not an install. And
    parsed rather than sliced out of the text by indentation, because a job's
    toolchain arrives through a merge key (``<<: *fast_check_job``) as readily as
    through its own lines, and a text slice cannot follow one.
    """
    loaded = yaml.safe_load(GITLAB.read_text(encoding="utf-8"))
    jobs: dict[str, list[str]] = {}
    for name, body in loaded.items():
        if not isinstance(body, dict) or "script" not in body:
            continue
        jobs[name] = [
            str(line)
            for line in list(body.get("before_script") or [])
            + list(body.get("script") or [])
        ]
    return jobs


def _github_job_steps(workflow: Path = GITHUB_TESTS) -> dict[str, list[str]]:
    """``{job name: one string per step}``, in order.

    A step contributes either its shell text or, when it ``uses`` a composite
    action **in this repository**, a rendering of the inputs it passed — because
    that is how a GitHub job asks for a toolchain. Reading only the shell would
    report the UI dependencies as missing from every job while they were being
    installed, which is the false-alarm direction :func:`_github_ci_text` was
    written about; reading only the inputs would accept one no action honours,
    which is the assertion after this one.
    """
    loaded = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    jobs: dict[str, list[str]] = {}
    for name, body in (loaded.get("jobs") or {}).items():
        rendered: list[str] = []
        for step in body.get("steps") or []:
            uses = str(step.get("uses", ""))
            if uses.startswith("./"):
                passed = " ".join(
                    f"{key}={value}" for key, value in (step.get("with") or {}).items()
                )
                rendered.append(f"uses {uses} with {passed}")
            else:
                rendered.append(str(step.get("run", "")))
        jobs[name] = rendered
    return jobs


def _installs_the_ui_deps(command: str) -> bool:
    """Does ``command`` install the UI's npm dependencies?

    Either spelling counts: ``npm ci`` run against ``src/ui``, or
    ``ui-deps=true`` passed to a composite action in this repository. One line has
    to carry both halves of the first, so a ``cd src/ui`` in one command and an
    ``npm ci`` in an unrelated one do not add up to an install. Run through
    :func:`_uncommented` first — a commented-out install reads to a substring
    search exactly like a live one, which is the first of the escapes
    ``scripts/tests/test_ui_test_discovery.py`` documents against itself.
    """
    for line in _uncommented(command).splitlines():
        # `src/ui` as a whole path element, so a neighbouring directory does not
        # read as the UI: `cd src/ui-staging && npm ci` contains `src/ui` as a
        # substring and installs this suite nothing. (`src/ui/` still matches.)
        if "npm ci" in line and re.search(r"src/ui(?![\w-])", line):
            return True
        if line.lstrip().startswith("uses ./") and f"{UI_DEPS_INPUT}=true" in line:
            return True
    return False


def _runs_the_scripts_suite(command: str) -> bool:
    return any(_invokes(_uncommented(command), t) for t in SCRIPTS_SUITE_TARGETS)


def _first_index(commands: list[str], predicate) -> int | None:
    return next((i for i, c in enumerate(commands) if predicate(c)), None)


def _ci_jobs_running_the_scripts_suite() -> dict[str, dict[str, list[str]]]:
    """``{config name: {job name: ordered commands}}`` for jobs running the suite.

    Derived from the ``make`` targets rather than listed by job name, so a third
    job that starts running the suite is covered the day it is added rather than
    the day somebody remembers a list.
    """
    out: dict[str, dict[str, list[str]]] = {}
    for label, jobs in (
        (GITLAB.name, _gitlab_job_commands()),
        (GITHUB_TESTS.name, _github_job_steps()),
    ):
        out[label] = {
            name: commands
            for name, commands in jobs.items()
            if any(_runs_the_scripts_suite(c) for c in commands)
        }
    return out


@pytest.mark.unit
def test_every_ci_job_running_this_suite_installs_the_ui_deps() -> None:
    """A job that runs ``scripts/tests`` installs the UI's npm dependencies first.

    ``scripts/tests/test_ui_test_discovery.py`` holds hand-written copies of
    Vitest's file-naming convention — the twelve extensions ``?(c|m)[jt]s?(x)``
    expands to, and ``defaultInclude`` itself — and those literals are the *input*
    to its gate assertions: the file set it calls the universe is derived from
    them. Three assertions calibrate them against the installed Vitest, and those
    three need ``src/ui/node_modules``.

    Until #1390 no job that ran that suite had it. ``node`` was on the runner for
    basedpyright and the UI's dependencies were installed in a different job, so
    the calibrations skipped — on **both** platforms, in all four jobs — and a skip
    exits 0. Nothing escalated, and the literals went unchecked against the tool
    they describe for as long as that lasted. A Vitest upgrade broadening its own
    convention is the live case: the gate would keep asserting against the stale
    expansion, pass, and quietly stop covering the new spelling, which is the exact
    failure that module was written to prevent.

    Both platforms at once, because giving one a toolchain and not the other is the
    asymmetry this module exists for, and it has already cost ``package_tests`` a
    red run once — see the toolchain notes in ``.gitlab-ci.yml``.

    **And in that order.** A ``cd src/ui && npm ci`` that sits after the step
    running the suite is a step that looks exactly like a fix and is not one: the
    install happens, the job's log shows it, and the suite that needed it has
    already skipped past. Presence alone cannot tell the two apart.
    """
    discovered = _ci_jobs_running_the_scripts_suite()

    # Both platforms are read, asserted by NAME rather than by iterating whatever
    # the dict happens to hold. A per-platform loop over the keys cannot see a
    # platform that is not a key: dropping one of the two pairs in
    # _ci_jobs_running_the_scripts_suite leaves this test green over the other
    # side alone, which is a parity test that has stopped comparing — measured as
    # a surviving mutant, not imagined.
    assert set(discovered) == {GITLAB.name, GITHUB_TESTS.name}, (
        f"this test read {sorted(discovered)}, not both CI configurations. Parity "
        "is the claim; one side alone cannot carry it."
    )

    # Non-vacuity, per platform: a side whose jobs are not found cannot have an
    # offender, so the loop below would pass by reading nothing.
    for platform, jobs in discovered.items():
        assert jobs, (
            f"no job in {platform} was found running any of {SCRIPTS_SUITE_TARGETS}. "
            "Either the suite has stopped running there — which "
            "test_gate_runs_in_both_cis is the gate for — or it is invoked in a form "
            "this discovery cannot see, in which case fix the discovery rather than "
            "the expectation: with an empty universe this test reports nothing."
        )

    missing: list[str] = []
    late: list[str] = []
    for platform, jobs in discovered.items():
        for name, commands in jobs.items():
            installs = _first_index(commands, _installs_the_ui_deps)
            runs = _first_index(commands, _runs_the_scripts_suite)
            if installs is None:
                missing.append(f"{platform}: job `{name}`")
            elif runs is not None and installs > runs:
                late.append(
                    f"{platform}: job `{name}` (installs at step {installs}, runs "
                    f"the suite at step {runs})"
                )

    assert not missing, (
        "these CI jobs run scripts/tests without installing the UI's npm "
        "dependencies, so test_ui_test_discovery.py's three calibrations cannot run "
        "there:\n  " + "\n  ".join(missing) + "\n\n"
        "On GitLab add `cd src/ui && npm ci --prefer-offline --no-audit && cd ../..` "
        "to the job's before_script, with the src/ui/node_modules cache block the "
        f'other jobs declare. On GitHub pass `{UI_DEPS_INPUT}: "true"` to the '
        "python-env composite action. That module now FAILS rather than skips when "
        "it finds them missing under CI, so such a job is red rather than quietly "
        "incomplete — but the remedy is the dependencies, not relaxing it back to a "
        "skip (#1390)."
    )
    assert not late, (
        "these CI jobs install the UI's npm dependencies only AFTER the step that "
        "runs scripts/tests, which is the same as not installing them as far as the "
        "suite is concerned:\n  " + "\n  ".join(late)
    )


def _ci_paths_module():
    """``scripts/srt/ci_paths.py``, loaded by path.

    By path rather than by import, because ``scripts/srt`` is not a package. The
    module is stdlib-only at import time — the parser this test is about is imported
    lazily inside it — so loading it here costs nothing and cannot depend on the
    thing being asserted.
    """
    spec = importlib.util.spec_from_file_location(
        "_ci_paths_for_parity", REPO_ROOT / "scripts" / "srt" / "ci_paths.py"
    )
    assert spec and spec.loader, "scripts/srt/ci_paths.py is not loadable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _installs_the_distribution(command: str, distribution: str) -> bool:
    """Does ``command`` ``pip install`` a distribution by that name?

    Per line, because a GitHub step's ``run`` is a whole shell script and a GitLab
    job's is one entry per line: a file-wide search would accept an install in a
    different step of a different job. Case-insensitively, because distribution
    names are compared case-insensitively by pip and ``pyyaml`` and ``PyYAML`` are
    the same package.
    """
    pattern = re.compile(rf"(?<![\w.-]){re.escape(distribution)}(?![\w.-])", re.I)
    for line in _uncommented(command).splitlines():
        if "pip install" in line and pattern.search(line):
            return True
    return False


def _ci_jobs_running_the_srt_gate() -> dict[str, dict[str, list[str]]]:
    """``{config name: {job name: ordered commands}}`` for jobs running the scan.

    Found by the ``make`` target rather than by job name, for the same reason the
    suite discovery above is: the job is renameable and a second one is addable.
    """
    out: dict[str, dict[str, list[str]]] = {}
    for label, jobs in (
        (GITLAB.name, _gitlab_job_commands()),
        (GITHUB_SECURITY.name, _github_job_steps(GITHUB_SECURITY)),
    ):
        out[label] = {
            name: commands
            for name, commands in jobs.items()
            if any(_invokes(_uncommented(c), "srt-scan") for c in commands)
        }
    return out


@pytest.mark.unit
def test_every_ci_job_running_the_srt_gate_installs_the_parser_it_needs() -> None:
    """The SRT gate's job installs the distributions its classification requires.

    ``scripts/srt/ci_paths.py`` reads a CodeBuild buildspec to learn which
    directories a deployment artifact is built from, and that read needs PyYAML.
    Neither job installed it. The parser was therefore absent on every CI run, the
    "cannot establish the packaged set" branch was taken every time, and the
    demotion that branch switches off is what keeps Bandit's identifier-name
    findings in test files out of the gate — so the security gate reported ten HIGH
    findings and exit 1 on every pull request for a day, for a missing
    ``pip install`` (#1428). The scan refuses outright on that condition now, which
    makes it legible; this is what keeps it from arising.

    **Both platforms, by name.** A gap between the two CIs is what this module
    exists for, and this is the shape it takes here: an environment dependency is
    the easiest thing to add on one side and forget on the other, and the symptom
    is not a missing step but a passing gate that has stopped classifying.

    The distributions come from ``REQUIRED_DISTRIBUTIONS`` in the module itself, so
    a second one added there has to reach both configurations rather than being
    silently required of neither.
    """
    required = _ci_paths_module().REQUIRED_DISTRIBUTIONS
    assert required, (
        "ci_paths.REQUIRED_DISTRIBUTIONS is empty, so the loop below asserts "
        "nothing. If the classification genuinely needs no third-party parser any "
        "more, delete this test rather than leaving it vacuous."
    )

    discovered = _ci_jobs_running_the_srt_gate()
    assert set(discovered) == {GITLAB.name, GITHUB_SECURITY.name}, (
        f"this test read {sorted(discovered)}, not both CI configurations. Parity "
        "is the claim; one side alone cannot carry it."
    )
    for platform, jobs in discovered.items():
        assert jobs, (
            f"no job in {platform} was found running `make srt-scan`. Either the "
            "gate has stopped running there — which test_gate_runs_in_both_cis is "
            "the gate for — or it is invoked in a form this discovery cannot see, "
            "in which case fix the discovery: with an empty universe this test "
            "reports nothing."
        )

    missing: list[str] = []
    late: list[str] = []
    for platform, jobs in discovered.items():
        for name, commands in jobs.items():
            scan = _first_index(
                commands, lambda c: _invokes(_uncommented(c), "srt-scan")
            )
            for distribution in required:
                installs = _first_index(
                    commands,
                    lambda c, d=distribution: _installs_the_distribution(c, d),
                )
                if installs is None:
                    missing.append(f"{platform}: job `{name}` needs {distribution}")
                elif scan is not None and installs > scan:
                    late.append(
                        f"{platform}: job `{name}` installs {distribution} at step "
                        f"{installs} but scans at step {scan}"
                    )

    assert not missing, (
        "these CI jobs run the SRT gate without installing a distribution its "
        "classification requires, so the gate runs degraded:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd the package to the job's existing `pip install` line in both "
        "configurations — one side alone is the defect this module is for."
    )
    assert not late, (
        "these CI jobs install the parser only after running the scan, which is "
        "the same as not installing it:\n  " + "\n  ".join(late)
    )


@pytest.mark.unit
def test_the_github_ui_deps_input_actually_installs_the_dependencies() -> None:
    """The input the assertion above accepts has to do something.

    ``ui-deps: "true"`` in a workflow is a string handed to a composite action. If
    no step in that action is conditioned on it, or the step it is conditioned on
    installs nothing, the assertion above would read the request and call it
    satisfied — a gate measuring an intention rather than an effect, which is the
    shape of defect this module's docstring is about.

    Composite actions are globbed rather than named, so the input may be honoured
    by whichever of them the jobs use.
    """
    actions = sorted(GITHUB_ACTIONS_DIR.glob("*/action.yml"))
    assert actions, (
        f"no composite action found under {GITHUB_ACTIONS_DIR.name}, so this test "
        "read nothing"
    )

    requested = sorted(
        name
        for name, commands in _github_job_steps().items()
        if any(
            c.lstrip().startswith("uses ./") and f"{UI_DEPS_INPUT}=true" in c
            for c in commands
        )
    )
    # The value the jobs actually pass, which the honouring condition below has to
    # be the condition FOR. Without this the check accepts any condition that
    # merely mentions the input: `if: inputs.ui-deps == 'yes'` names it, installs
    # nothing for a job passing "true", and survived this test as written. Nothing
    # here evaluates a GitHub expression — that is a language, not a string — but
    # requiring the passed value to appear in the condition is enough to tell a
    # live comparison from one that can never be true.
    passed_value = "true"
    # Deliberately conditional, and deliberately NOT guarded by `assert requested`.
    # The claim here is "an input that is passed must do something", which is
    # vacuous when nothing passes it — and in that state the assertion above is the
    # one that fails, because it requires every suite-running job to install the
    # dependencies by SOME route. Demanding a non-empty `requested` here would
    # instead demand that GitHub use the input route specifically, so an inline
    # `cd src/ui && npm ci` in the job — which installs them perfectly well, and is
    # how `ui_tests` does it — would be reported as a defect. The two assertions
    # together leave no gap: this one cannot be satisfied by an inert input, and
    # that one cannot be satisfied by no input at all.
    if not requested:
        return

    honouring: list[str] = []
    for action in actions:
        loaded = yaml.safe_load(action.read_text(encoding="utf-8"))
        # The input has to be DECLARED as well as tested. An undeclared input
        # passed through `with:` is a warning on GitHub rather than an error, and
        # `inputs.ui-deps` then evaluates to the empty string — so the condition
        # below is false, nothing is installed, and the workflow still runs. The
        # suite catches that at CI time now (it fails rather than skips), but an
        # offline gate that can see it should.
        if UI_DEPS_INPUT not in (loaded.get("inputs") or {}):
            continue
        for step in (loaded.get("runs") or {}).get("steps") or []:
            condition = str(step.get("if", ""))
            if (
                UI_DEPS_INPUT in condition
                and f"'{passed_value}'" in condition
                and _installs_the_ui_deps(str(step.get("run", "")))
            ):
                honouring.append(action.parent.name)
                break
    assert honouring, (
        f'{requested} pass `{UI_DEPS_INPUT}: "{passed_value}"`, but no composite '
        f"action under {GITHUB_ACTIONS_DIR.name} has a step whose `if:` tests that "
        f"input against '{passed_value}' AND runs `npm ci` in src/ui. The input is "
        "therefore inert, and those jobs run scripts/tests with no UI dependencies "
        "while appearing to ask for them."
    )
