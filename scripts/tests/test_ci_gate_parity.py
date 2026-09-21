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

Integration tests are excluded on purpose: they need AWS credentials and stay
GitLab-only. See ``scripts/sdlc/docs/CI_TEST_COVERAGE.md``.

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

import json
import re
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]
GITLAB = REPO_ROOT / ".gitlab-ci.yml"
GITHUB_TESTS = REPO_ROOT / ".github/workflows/developer-tests.yml"
GITHUB_SECURITY = REPO_ROOT / ".github/workflows/security-checks.yml"
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
LIVE_STACK_SECTION_TARGET_COUNT = 19

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
        "Runs the integration-marked suites, which call live AWS. GitLab's "
        "`integration_tests` stage is the one deliberate CI asymmetry in this "
        "repo; test_integration_tests_stay_gitlab_only below pins it."
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
    return GITHUB_TESTS.read_text() + GITHUB_SECURITY.read_text()


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

    for path in (GITLAB, GITHUB_TESTS):
        text = path.read_text()
        assert f"cfn-lint=={version}" in text, (
            f"{path.name} does not pin cfn-lint=={version} (the Makefile's "
            f"CFN_LINT_VERSION). CI would then run a different linter than "
            f"`make cfn-lint` does locally."
        )


@pytest.mark.unit
def test_ruff_is_pinned_consistently() -> None:
    """The two CIs must pin the same ``ruff``, and the pin must be installable here.

    ``ruff``'s findings are version-dependent, and ``make check-lint-debt``
    compares a recorded per-file finding count against a live measurement — so
    two CI systems on different ``ruff`` releases would disagree about whether
    the baseline is current, and the disagreement would look like a code defect.
    ``lib/idp_common_pkg/pyproject.toml`` supplies ``ruff`` locally as a range, so
    the CI pin has to fall inside it or a contributor's ``make lint`` and CI are
    running different linters by construction.
    """
    pins = {}
    for path in (GITLAB, GITHUB_TESTS):
        found = re.findall(r"ruff==([0-9][0-9A-Za-z.\-]*)", path.read_text())
        assert found, f"{path.name} no longer pins a ruff version"
        assert len(set(found)) == 1, f"{path.name} pins several ruff versions: {found}"
        pins[path.name] = found[0]

    assert len(set(pins.values())) == 1, (
        f"the two CI configs pin different ruff versions: {pins}. `ruff check` and "
        "`make check-lint-debt` would then reach different verdicts depending on "
        "which CI a change was merged through."
    )
    version = next(iter(pins.values()))

    pyproject = (REPO_ROOT / "lib" / "idp_common_pkg" / "pyproject.toml").read_text()
    specifier = re.search(r'"ruff([^"]*)"', pyproject)
    assert specifier, "lib/idp_common_pkg/pyproject.toml no longer declares ruff"
    assert Version(version) in SpecifierSet(specifier.group(1)), (
        f"CI pins ruff=={version}, which is outside the range "
        f"{specifier.group(1)!r} that lib/idp_common_pkg installs locally. A "
        "developer's `make lint` would then run a different linter than CI."
    )


@pytest.mark.unit
def test_integration_tests_stay_gitlab_only() -> None:
    """Documents the ONE deliberate asymmetry, so it cannot drift unnoticed."""
    assert "integration_tests" in GITLAB.read_text()
    assert "integration_tests" not in _github_ci_text(), (
        "integration_tests appeared in GitHub CI. It needs AWS credentials; if "
        "that is now intended, update this test and CI_TEST_COVERAGE.md."
    )
