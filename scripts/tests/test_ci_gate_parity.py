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
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GITLAB = REPO_ROOT / ".gitlab-ci.yml"
GITHUB_TESTS = REPO_ROOT / ".github/workflows/developer-tests.yml"
GITHUB_SECURITY = REPO_ROOT / ".github/workflows/security-checks.yml"
MAKEFILE = REPO_ROOT / "Makefile"

# Gates that MUST run in both CIs. Each is a static, no-AWS check.
SHARED_GATES = [
    "make lint-cicd",
    "make typecheck-pr",
    "make api-test-static",
    "make test-cicd",
    "make test-packages-cicd",
    "npx vitest run",
    "scripts/check_first_party_deps.py",
    "scripts/sdlc/validate_service_role_permissions.py",
]


def _github_ci_text() -> str:
    return GITHUB_TESTS.read_text() + GITHUB_SECURITY.read_text()


@pytest.mark.unit
@pytest.mark.parametrize("gate", SHARED_GATES)
def test_gate_runs_in_both_cis(gate: str) -> None:
    """A gate present on one side only is invisible to work merged via the other."""
    in_gitlab = gate in GITLAB.read_text()
    in_github = gate in _github_ci_text()

    assert in_gitlab and in_github, (
        f"gate {gate!r} runs in "
        f"{'GitLab' if in_gitlab else 'GitHub' if in_github else 'NEITHER'} only. "
        f"Work merged through the other CI skips it. Add it to both, or remove it "
        f"from SHARED_GATES with a reason (e.g. it needs AWS credentials)."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "gate",
    [
        "cfn-lint",
        "validate-buildspec",
        "check-arn-partitions",
        "check-filtered-scans",
        "check-data-plane-tags",
    ],
)
def test_lint_cicd_covers_what_local_lint_covers(gate: str) -> None:
    """``lint-cicd`` must not be a weaker gate than a developer's ``make lint``.

    ``cfn-lint`` and ``validate-buildspec`` were in ``lint``/``fastlint`` only, so
    CI ran neither — the gap this file exists to catch.
    """
    text = MAKEFILE.read_text()
    recipe_start = text.index("\nlint-cicd:")
    recipe_end = text.index("\n##@", recipe_start)
    recipe = text[recipe_start:recipe_end]

    assert gate in recipe, (
        f"'{gate}' is not reachable from `make lint-cicd`, so neither CI runs it "
        f"even though `make lint` does. Add it to lint-cicd."
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
def test_integration_tests_stay_gitlab_only() -> None:
    """Documents the ONE deliberate asymmetry, so it cannot drift unnoticed."""
    assert "integration_tests" in GITLAB.read_text()
    assert "integration_tests" not in _github_ci_text(), (
        "integration_tests appeared in GitHub CI. It needs AWS credentials; if "
        "that is now intended, update this test and CI_TEST_COVERAGE.md."
    )
