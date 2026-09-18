# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for scripts/sdlc/check_branch_protection.py.

The script under test answers "do this repo's CI gates actually block a merge?"
by deriving the expected required-check list from ``.github/workflows/*.yml`` and
comparing it against the live GitHub API. These tests cover both halves —
parsing and assertion — **entirely offline**, using recorded API payloads, so
they run in CI with no token and no network.

Two behaviours are worth stating outright because getting either wrong makes the
script pass vacuously:

* ``on:`` is a YAML 1.1 boolean, so ``yaml.safe_load`` yields the key ``True``,
  not ``"on"``. Read only ``"on"`` and every workflow looks trigger-less, the
  expected list comes out empty, and nothing is ever reported as missing.
* a path-filtered ``pull_request`` trigger must NOT become a required check. Such
  a workflow does not run on a PR touching no matching path, so the check is
  never reported and a required one would sit pending forever, blocking every
  merge. This repo has two of those (``build-docs.yml``,
  ``generate-dep-manifest.yml``).

* the derivation must keep producing **all three** contexts that have to be
  required, not just the lint one. See ``MUST_BE_REQUIRED``: a guard covering one
  of the three let a plausible ``paths:`` filter drop both security gates out of
  the required set with the whole suite green.
* "cannot see" is not "not protected". The classic-protection endpoint needs
  repository admin and answers 404 without it, so a 404 alone cannot distinguish
  an unprotected branch from an invisible one. The tool resolves that with two
  further reads and reports a tri-state; these tests pin all four states.

The recorded payloads follow the shape of
``GET /repos/{owner}/{repo}/branches/{branch}/protection`` and
``GET /repos/{owner}/{repo}/rules/branches/{branch}``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "sdlc" / "check_branch_protection.py"


def _load_script():
    """Load the checker by path (scripts/sdlc isn't on sys.path).

    Registered in ``sys.modules`` *before* execution: the module defines
    ``@dataclass`` types, and dataclasses resolves ``cls.__module__`` through
    ``sys.modules``, so an unregistered module fails to import with an obscure
    ``AttributeError: 'NoneType' object has no attribute '__dict__'``.
    """
    spec = importlib.util.spec_from_file_location("check_branch_protection", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_script()

EXPECTED = ["Dependency Audit (SCA)", "Lint, Type Check, and Test"]

# Every check context that MUST come out of the derivation as required-eligible,
# mapped to a gate command that anchors it to real work.
#
# Naming all three is the whole point of this constant. The guard it replaces
# checked only the job running `make lint-cicd`, so adding a `paths:` filter to
# `security-checks.yml`'s `pull_request` trigger — a change a maintainer might
# plausibly make to save CI minutes — collapsed the derived required set to
# ['Lint, Type Check, and Test'] with every test in this file still green. Both
# security gates silently dropped out, and worse, the tool would then have
# reported them under `unknown_required_checks`, actively advising an
# administrator to UN-require the SRT scan and the dependency audit.
#
# The names are asserted directly because that is the string branch protection
# has to match; the gate command is asserted alongside so that keeping the name
# while gutting the job's work also fails.
MUST_BE_REQUIRED = {
    "Lint, Type Check, and Test": "make lint-cicd",
    "SRT Security Review": "make srt-scan",
    "Dependency Audit (SCA)": "scripts/security/dep_audit.py",
}

# The mirror image: contexts this repo does produce, but not on every pull
# request, so requiring one would leave it pending forever and block every merge.
# Deliberately NOT asserted as required-eligible.
MUST_STAY_ADVISORY = {
    "build": "build-docs.yml is paths-filtered",
    "Generate Dependency Manifests": "generate-dep-manifest.yml is paths-filtered",
    "Test Results": "action-created via check_name:, behind a conditional step",
}


def _fully_protected(contexts: list[str] | None = None) -> Dict[str, Any]:
    """A recorded protection payload with everything this repo should require.

    ``enforce_admins`` is enabled here. It used to be ``False``, which meant this
    fixture — the suite's own definition of "fully protected" — described a
    configuration where an administrator can push straight past every required
    check and the review requirement. That is not the target state.
    """
    return {
        "required_status_checks": {
            "strict": True,
            "contexts": list(EXPECTED if contexts is None else contexts),
            "checks": [
                {"context": name, "app_id": 15368}
                for name in (EXPECTED if contexts is None else contexts)
            ],
        },
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
            "required_approving_review_count": 1,
            "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []},
        },
        "enforce_admins": {"enabled": True},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "required_conversation_resolution": {"enabled": True},
    }


def _write_workflow(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(dedent(body), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Deriving the expected check list from workflow YAML
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_context_is_job_name_when_set_else_job_id(tmp_path: Path) -> None:
    """GitHub names the status check after the job's `name:`, else the job id."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        on:
          pull_request:
            branches: ["**"]
        jobs:
          named_job:
            name: Human Readable Name
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
          unnamed_job:
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
        """,
    )
    assert mod.expected_contexts(mod.discover_check_contexts(tmp_path)) == [
        "Human Readable Name",
        "unnamed_job",
    ]


@pytest.mark.unit
def test_on_key_parsed_as_yaml_boolean_is_handled(tmp_path: Path) -> None:
    """`on:` loads as the key True; missing that empties the expected list."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        on:
          pull_request:
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
        """,
    )
    contexts = mod.discover_check_contexts(tmp_path)
    assert mod.expected_contexts(contexts) == ["J"], (
        "a bare `pull_request:` trigger must still yield a required-eligible "
        "context — if this is empty, the True-vs-'on' key handling regressed and "
        "the script would report a pass against no expectation at all"
    )


@pytest.mark.unit
def test_quoted_on_key_is_also_handled(tmp_path: Path) -> None:
    """`"on":` loads as the string key; both spellings must work."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        "on":
          pull_request:
            branches: ["**"]
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
        """,
    )
    assert mod.expected_contexts(mod.discover_check_contexts(tmp_path)) == ["J"]


@pytest.mark.unit
@pytest.mark.parametrize("filter_key", ["paths", "paths-ignore"])
def test_path_filtered_pull_request_is_not_required_eligible(
    tmp_path: Path, filter_key: str
) -> None:
    """A required check that never reports blocks every merge forever."""
    _write_workflow(
        tmp_path,
        "w.yml",
        f"""
        name: W
        on:
          pull_request:
            {filter_key}:
              - 'docs/**'
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            steps: [{{run: "true"}}]
        """,
    )
    contexts = mod.discover_check_contexts(tmp_path)
    assert mod.expected_contexts(contexts) == []
    assert "pending forever" in contexts[0].reason


@pytest.mark.unit
def test_non_pull_request_workflow_is_not_required_eligible(tmp_path: Path) -> None:
    """A push/schedule-only job never reports on a PR, so it cannot be required."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        on:
          push:
            branches: [main]
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            steps: [{run: "true"}]
        """,
    )
    contexts = mod.discover_check_contexts(tmp_path)
    assert mod.expected_contexts(contexts) == []
    assert contexts[0].reason == "not triggered by pull_request"


@pytest.mark.unit
def test_matrix_job_is_excluded_because_context_is_suffixed(tmp_path: Path) -> None:
    """GitHub appends the matrix leg to the context, so it isn't static."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        on:
          pull_request:
            branches: ["**"]
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            strategy:
              matrix:
                python: ["3.12", "3.13"]
            steps: [{run: "true"}]
        """,
    )
    contexts = mod.discover_check_contexts(tmp_path)
    assert mod.expected_contexts(contexts) == []
    assert "matrix" in contexts[0].reason


@pytest.mark.unit
def test_gate_attribution_ignores_prose_that_looks_like_a_target(
    tmp_path: Path,
) -> None:
    """`apt-get install make curl` must not be reported as a gate `make curl`."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        on:
          pull_request:
            branches: ["**"]
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            steps:
              - run: apt-get install make curl -y
              - run: make lint-cicd
        """,
    )
    gates = mod.discover_check_contexts(tmp_path)[0].gates
    assert "make lint-cicd" in gates, "a real Makefile target must be attributed"
    assert "make curl" not in gates


@pytest.mark.unit
def test_unconditional_action_check_name_is_required_eligible(tmp_path: Path) -> None:
    """An action-created check that always reports can safely be required."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        on:
          pull_request:
            branches: ["**"]
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            steps:
              - uses: some/action@v1
                with:
                  check_name: Always Reports
        """,
    )
    contexts = mod.discover_check_contexts(tmp_path)
    assert mod.expected_contexts(contexts) == ["Always Reports", "J"]
    action = next(c for c in contexts if c.context == "Always Reports")
    assert action.created_by_action and action.required_eligible


@pytest.mark.unit
def test_conditional_action_check_name_is_advisory(tmp_path: Path) -> None:
    """A check run behind an `if:` is not created on every PR, so it can't be required."""
    _write_workflow(
        tmp_path,
        "w.yml",
        """
        name: W
        on:
          pull_request:
            branches: ["**"]
        jobs:
          j:
            name: J
            runs-on: ubuntu-latest
            steps:
              - uses: some/action@v1
                if: always() && hashFiles('r.xml') != ''
                with:
                  check_name: Sometimes Reports
        """,
    )
    contexts = mod.discover_check_contexts(tmp_path)
    assert mod.expected_contexts(contexts) == ["J"]
    action = next(c for c in contexts if c.context == "Sometimes Reports")
    assert action.created_by_action and not action.required_eligible
    assert "conditional" in action.reason


@pytest.mark.unit
def test_invalid_yaml_raises_rather_than_silently_reporting_no_jobs(
    tmp_path: Path,
) -> None:
    _write_workflow(tmp_path, "bad.yml", "name: W\n  bad: [indent\n")
    with pytest.raises(ValueError, match="invalid YAML"):
        mod.discover_check_contexts(tmp_path)


@pytest.mark.unit
def test_missing_workflows_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        mod.discover_check_contexts(tmp_path / "nope")


# --------------------------------------------------------------------------- #
# The real workflows in this repo (offline: reads files, no network)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_this_repos_workflows_yield_a_non_empty_expected_list() -> None:
    """Guards the vacuous-pass failure mode against the real workflow files."""
    contexts = mod.discover_check_contexts(mod.WORKFLOWS_DIR)
    expected = mod.expected_contexts(contexts)
    assert expected, (
        "no required-eligible check context derived from .github/workflows/ — "
        "the script would then have nothing to assert and would report a pass"
    )
    # The gates job must be in the list: it carries lint, typecheck and the unit
    # suites. Asserted via its gate commands, not its display name, so renaming
    # the job does not silently empty this test.
    lint_jobs = [c for c in contexts if "make lint-cicd" in c.gates]
    assert lint_jobs, "no workflow job runs `make lint-cicd`"
    assert all(c.required_eligible for c in lint_jobs), (
        "the job running `make lint-cicd` is not required-eligible; if its "
        "pull_request trigger gained a paths: filter, that gate now silently "
        "skips on PRs that touch no matching path"
    )


@pytest.mark.unit
def test_every_context_that_must_be_required_is_in_the_derived_required_set() -> None:
    """All three, not just the lint one — see MUST_BE_REQUIRED for why.

    This is the assertion that fails when a `paths:` filter is added to
    `security-checks.yml`, which is what previously slipped through green.
    """
    expected = set(mod.expected_contexts(mod.discover_check_contexts(mod.WORKFLOWS_DIR)))
    missing = sorted(set(MUST_BE_REQUIRED) - expected)
    assert not missing, (
        f"these contexts dropped out of the derived required set: {missing}. "
        f"Derived: {sorted(expected)}. A context that is not required-eligible is "
        f"one the tool will not ask to be required — and if it is already "
        f"required, the tool will report it as unknown and advise removing it."
    )


@pytest.mark.unit
@pytest.mark.parametrize(("context", "gate"), sorted(MUST_BE_REQUIRED.items()))
def test_context_that_must_be_required_is_eligible_and_runs_its_gate(
    context: str, gate: str
) -> None:
    """Each required context exists, is eligible, and still runs its gate.

    Two assertions rather than one: the name is what branch protection matches on,
    and the gate command is what makes requiring it worth anything. Renaming the
    job fails the first; keeping the name while removing the work fails the second.
    """
    matches = [
        c for c in mod.discover_check_contexts(mod.WORKFLOWS_DIR) if c.context == context
    ]
    assert matches, f"no workflow job produces the context {context!r}"
    for match in matches:
        assert match.required_eligible, (
            f"{context!r} is not required-eligible ({match.reason!r}). If its "
            f"pull_request trigger gained a paths: filter, this gate now skips "
            f"silently on PRs touching no matching path and the tool would advise "
            f"un-requiring it."
        )
        assert any(gate in attributed for attributed in match.gates), (
            f"{context!r} no longer runs {gate!r}; it is still required-eligible "
            f"but no longer gates what it is required for. Attributed: "
            f"{match.gates}"
        )


@pytest.mark.unit
def test_path_filtered_repo_workflows_are_reported_as_advisory_only() -> None:
    """build-docs and generate-dep-manifest are path-filtered on purpose."""
    contexts = mod.discover_check_contexts(mod.WORKFLOWS_DIR)
    advisory = {c.workflow for c in contexts if not c.required_eligible}
    assert {"build-docs.yml", "generate-dep-manifest.yml"} <= advisory


@pytest.mark.unit
@pytest.mark.parametrize(("context", "why"), sorted(MUST_STAY_ADVISORY.items()))
def test_context_that_must_stay_advisory_is_not_required_eligible(
    context: str, why: str
) -> None:
    """A required check that does not always report blocks every merge forever."""
    matches = [
        c for c in mod.discover_check_contexts(mod.WORKFLOWS_DIR) if c.context == context
    ]
    assert matches, f"the context {context!r} is no longer derived at all"
    for match in matches:
        assert not match.required_eligible, (
            f"{context!r} became required-eligible, but {why} — requiring it would "
            f"leave a check pending forever on every PR that does not trigger it"
        )


@pytest.mark.unit
def test_action_created_check_run_is_derived_from_check_name() -> None:
    """`Test Results` is a check run an action creates, not a job.

    No job-level parsing can find it, so `check_name:` is read off the step. It
    matters that it is derived at all: if it were not, and somebody required it,
    the tool would report it as a name nothing produces and advise removing it.
    """
    contexts = mod.discover_check_contexts(mod.WORKFLOWS_DIR)
    results = [c for c in contexts if c.context == "Test Results"]
    assert len(results) == 1, "expected exactly one `Test Results` context"
    assert results[0].created_by_action
    assert results[0].workflow == "developer-tests.yml"
    assert "conditional" in results[0].reason


# --------------------------------------------------------------------------- #
# Asserting against recorded API payloads
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_unprotected_branch_is_a_finding_that_names_the_issue() -> None:
    """The 404 case: what this repo looks like today."""
    findings = mod.evaluate(None, EXPECTED, "develop", status=404)
    assert [f.key for f in findings] == ["not_protected"]
    assert "#933" in findings[0].remedy
    for name in EXPECTED:
        assert name in findings[0].message, (
            "the finding must name the checks that should be required, so the "
            "reader can act on it without re-deriving the list"
        )


# --------------------------------------------------------------------------- #
# "cannot see" vs "not protected", and rulesets as the second mechanism
# --------------------------------------------------------------------------- #

# Recorded verbatim from GET /repos/{slug}/rules/branches/develop on
# 2026-09-18 with a token whose permissions are
# {"admin": false, "maintain": true, "pull": true, "push": true, "triage": true}.
# Every rule is inherited from the `amazon` enterprise and every one is
# REPOSITORY-scoped, so none of them protects the branch — the repository's five
# active rulesets are four target=repository and one target=tag. This payload is
# why a non-empty response must not be read as "protected".
REAL_BRANCH_RULES_RESPONSE = [
    {
        "type": "repository_visibility",
        "parameters": {"public": True, "internal": False, "private": True},
        "ruleset_source_type": "Enterprise",
        "ruleset_source": "amazon",
        "ruleset_id": 5369255,
    },
    {
        "type": "repository_visibility",
        "parameters": {"public": True, "internal": True, "private": False},
        "ruleset_source_type": "Enterprise",
        "ruleset_source": "amazon",
        "ruleset_id": 5369259,
    },
    {
        "type": "repository_delete",
        "ruleset_source_type": "Enterprise",
        "ruleset_source": "amazon",
        "ruleset_id": 14294791,
    },
    {
        "type": "repository_transfer",
        "ruleset_source_type": "Enterprise",
        "ruleset_source": "amazon",
        "ruleset_id": 14294817,
    },
]


@pytest.mark.unit
def test_repository_scoped_ruleset_rules_do_not_protect_a_branch() -> None:
    """The live response is non-empty yet protects no branch — see the payload note."""
    assert mod.branch_scoped_rules(REAL_BRANCH_RULES_RESPONSE) == []
    assert mod.ruleset_required_check_names(REAL_BRANCH_RULES_RESPONSE) == []


@pytest.mark.unit
def test_branch_scoped_rule_types_are_kept_including_unknown_ones() -> None:
    """Filtering is by the `repository_` prefix, so a new branch rule type counts."""
    rules = REAL_BRANCH_RULES_RESPONSE + [
        {"type": "pull_request"},
        {"type": "some_future_branch_rule"},
    ]
    assert [r["type"] for r in mod.branch_scoped_rules(rules)] == [
        "pull_request",
        "some_future_branch_rule",
    ]


@pytest.mark.unit
def test_verified_absent_is_distinguished_from_unverifiable() -> None:
    """A 404 without admin means "cannot see", not "not protected".

    Reporting the ambiguous case as absent would be a false all-clear in exactly
    the situation that matters most: right after somebody enables protection.
    """
    verified = mod.ProtectionState(
        state=mod.PROTECTION_VERIFIED_ABSENT,
        classic_status=404,
        admin_permission=False,
        protected_flag=False,
    )
    findings = mod.evaluate(None, EXPECTED, "develop", 404, state=verified)
    assert [f.key for f in findings] == ["not_protected"]
    assert "verified" in findings[0].message
    assert verified.protected is False

    unverifiable = mod.ProtectionState(
        state=mod.PROTECTION_UNVERIFIABLE,
        classic_status=404,
        admin_permission=False,
        protected_flag=None,
    )
    findings = mod.evaluate(None, EXPECTED, "develop", 404, state=unverifiable)
    assert [f.key for f in findings] == ["protection_unverifiable"]
    assert "NOT a clean bill of health" in findings[0].message
    assert unverifiable.protected is None, (
        "the tri-state must stay tri-state: collapsing unknown to False is the "
        "false all-clear this state exists to prevent"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("classic_status", "protected_flag", "rules", "expected_state"),
    [
        (404, False, [], "verified_absent"),
        (404, None, [], "unverifiable"),
        (404, True, [], "unverifiable"),
        (404, True, [{"type": "pull_request"}], "protected_by_ruleset"),
        (200, True, [], "protected_classic"),
    ],
)
def test_protection_state_classification(
    monkeypatch: pytest.MonkeyPatch,
    classic_status: int,
    protected_flag: bool | None,
    rules: list[dict],
    expected_state: str,
) -> None:
    """All four states, from the three reads that produce them."""
    classic = _fully_protected() if classic_status == 200 else None
    monkeypatch.setattr(
        mod, "fetch_protection", lambda *_a, **_k: (classic, classic_status)
    )
    monkeypatch.setattr(mod, "fetch_admin_permission", lambda *_a, **_k: False)
    monkeypatch.setattr(mod, "fetch_branch_summary", lambda *_a, **_k: protected_flag)
    monkeypatch.setattr(
        mod, "fetch_branch_rules", lambda *_a, **_k: (REAL_BRANCH_RULES_RESPONSE + rules, 200)
    )
    state = mod.resolve_protection_state(mod.DEFAULT_REPO, "develop", "t")  # noqa: S106
    assert state.state == expected_state


@pytest.mark.unit
def test_ruleset_protection_is_evaluated_not_just_reported() -> None:
    """A branch governed only by a ruleset must still be checked, not assumed fine."""
    bare = mod.ProtectionState(
        state=mod.PROTECTION_RULESET,
        classic_status=404,
        branch_rules=[{"type": "creation"}],
    )
    keys = {f.key for f in mod.evaluate(None, EXPECTED, "develop", 404, state=bare)}
    assert keys == {
        "ruleset_no_required_status_checks",
        "ruleset_no_pull_request_review",
        "ruleset_force_pushes_allowed",
        "ruleset_deletions_allowed",
    }


@pytest.mark.unit
def test_fully_configured_ruleset_has_no_findings() -> None:
    rules = [
        {"type": "pull_request"},
        {"type": "non_fast_forward"},
        {"type": "deletion"},
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": name, "integration_id": 15368} for name in EXPECTED
                ]
            },
        },
    ]
    state = mod.ProtectionState(
        state=mod.PROTECTION_RULESET,
        classic_status=404,
        branch_rules=rules,
        ruleset_required_checks=mod.ruleset_required_check_names(rules),
    )
    assert mod.evaluate(None, EXPECTED, "develop", 404, state=state) == []
    assert state.protected is True


@pytest.mark.unit
def test_ruleset_required_checks_count_towards_classic_protection() -> None:
    """Both mechanisms gate at once, so the union is what actually blocks a merge."""
    payload = _fully_protected(contexts=["Lint, Type Check, and Test"])
    state = mod.ProtectionState(
        state=mod.PROTECTION_CLASSIC,
        classic=payload,
        classic_status=200,
        ruleset_required_checks=["Dependency Audit (SCA)"],
    )
    assert mod.evaluate(payload, EXPECTED, "develop", 200, state=state) == []


@pytest.mark.unit
def test_required_but_conditional_check_is_not_reported_as_unknown() -> None:
    """`Test Results` is produced, just not always — do not advise removing it."""
    payload = _fully_protected(contexts=EXPECTED + ["Test Results"])
    findings = mod.evaluate(
        payload, EXPECTED, "develop", derived=EXPECTED + ["Test Results"]
    )
    assert [f.key for f in findings] == ["required_but_not_always_reported"]
    assert "do NOT" in findings[0].remedy


@pytest.mark.unit
def test_fully_configured_protection_has_no_findings() -> None:
    assert mod.evaluate(_fully_protected(), EXPECTED, "develop") == []


@pytest.mark.unit
def test_missing_required_check_is_reported_by_name() -> None:
    """The drift case: a job renamed, or a new gate never added to protection."""
    payload = _fully_protected(contexts=["Lint, Type Check, and Test"])
    findings = mod.evaluate(payload, EXPECTED, "develop")
    keys = [f.key for f in findings]
    assert keys == ["missing_required_checks"]
    assert "Dependency Audit (SCA)" in findings[0].message


@pytest.mark.unit
def test_required_check_no_workflow_produces_is_reported() -> None:
    """A stale required name never reports, so it blocks merges indefinitely."""
    payload = _fully_protected(contexts=EXPECTED + ["Renamed Away"])
    findings = mod.evaluate(payload, EXPECTED, "develop")
    assert [f.key for f in findings] == ["unknown_required_checks"]
    assert "Renamed Away" in findings[0].message


@pytest.mark.unit
def test_checks_read_from_either_response_shape() -> None:
    """The API returns both `checks:` and the deprecated flat `contexts:`."""
    checks_only = _fully_protected()
    checks_only["required_status_checks"]["contexts"] = []
    assert mod.evaluate(checks_only, EXPECTED, "develop") == []

    contexts_only = _fully_protected()
    contexts_only["required_status_checks"]["checks"] = []
    assert mod.evaluate(contexts_only, EXPECTED, "develop") == []


@pytest.mark.unit
def test_protection_with_no_required_checks_is_a_finding() -> None:
    """ "Protected" without required checks still does not gate a merge."""
    payload = _fully_protected()
    del payload["required_status_checks"]
    findings = mod.evaluate(payload, EXPECTED, "develop")
    assert "no_required_status_checks" in [f.key for f in findings]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutate", "expected_key"),
    [
        (
            lambda p: p["required_status_checks"].__setitem__("strict", False),
            "not_strict",
        ),
        (
            lambda p: p["required_pull_request_reviews"].__setitem__(
                "dismiss_stale_reviews", False
            ),
            "stale_reviews_kept",
        ),
        (
            lambda p: p["required_pull_request_reviews"].__setitem__(
                "required_approving_review_count", 0
            ),
            "no_approving_review",
        ),
        (
            lambda p: p.__setitem__("required_pull_request_reviews", None),
            "no_pull_request_reviews",
        ),
        (
            lambda p: p["allow_force_pushes"].__setitem__("enabled", True),
            "force_pushes_allowed",
        ),
        (
            lambda p: p["allow_deletions"].__setitem__("enabled", True),
            "deletions_allowed",
        ),
        # enforce_admins was fetched and never examined. With it off, an admin can
        # push straight past every setting above, so protection can read as
        # complete while remaining bypassable by whoever is most likely merging.
        (
            lambda p: p["enforce_admins"].__setitem__("enabled", False),
            "admins_not_enforced",
        ),
        (
            lambda p: p.__setitem__("enforce_admins", None),
            "admins_not_enforced",
        ),
        # A bypass list makes "1 approval required" mean nothing for its members.
        (
            lambda p: p["required_pull_request_reviews"][
                "bypass_pull_request_allowances"
            ].__setitem__("apps", [{"slug": "some-app"}]),
            "pull_request_review_bypass_allowed",
        ),
    ],
)
def test_each_weakened_setting_is_reported(mutate, expected_key: str) -> None:
    payload = _fully_protected()
    mutate(payload)
    assert expected_key in [f.key for f in mod.evaluate(payload, EXPECTED, "develop")]


# --------------------------------------------------------------------------- #
# Input validation and the offline/no-token contract
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("repo", ["not-a-slug", "owner/name/extra", "owner/na me"])
def test_invalid_repo_slug_is_rejected_before_any_request(repo: str) -> None:
    """The slug is interpolated into an API URL, so it is validated first."""
    with pytest.raises(ValueError, match="invalid repo slug"):
        mod.fetch_protection(repo, "develop", "unused-token")  # noqa: S106


@pytest.mark.unit
def test_invalid_branch_name_is_rejected_before_any_request() -> None:
    with pytest.raises(ValueError, match="invalid branch name"):
        mod.fetch_protection(mod.DEFAULT_REPO, "bad branch", "unused-token")  # noqa: S106


@pytest.mark.unit
def test_invalid_slug_is_rejected_by_every_endpoint_wrapper() -> None:
    """Validation must not be left behind on the newer reads."""
    with pytest.raises(ValueError, match="invalid repo slug"):
        mod.fetch_branch_summary("not-a-slug", "develop", "t")  # noqa: S106
    with pytest.raises(ValueError, match="invalid repo slug"):
        mod.fetch_branch_rules("not-a-slug", "develop", "t")  # noqa: S106
    with pytest.raises(ValueError, match="invalid repo slug"):
        mod.resolve_protection_state("not-a-slug", "develop", "t")  # noqa: S106


@pytest.mark.unit
def test_every_api_call_is_a_get_with_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool must stay read-only now that it reads four endpoints, not one.

    Asserted on the request objects it actually builds rather than by reading the
    source, so adding a write would fail here rather than pass review.
    """
    seen: list[Any] = []

    class _FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b"{}"

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def _urlopen(request: Any, timeout: int = 0) -> _FakeResponse:
        seen.append(request)
        return _FakeResponse()

    monkeypatch.setattr(mod.urllib.request, "urlopen", _urlopen)
    mod.resolve_protection_state(mod.DEFAULT_REPO, "develop", "t")  # noqa: S106

    assert len(seen) == 4, "expected the four documented reads"
    for request in seen:
        assert request.get_method() == "GET"
        assert request.data is None
        assert request.full_url.startswith("https://api.github.com/")


@pytest.mark.unit
def test_no_token_skips_cleanly_and_fail_on_skip_makes_it_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Offline default is exit 0; --fail-on-skip is the future blocking mode."""
    monkeypatch.setattr(mod, "resolve_token", lambda: None)

    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "no GitHub token" in out

    assert mod.main(["--fail-on-skip"]) == 2


@pytest.mark.unit
def test_unreachable_api_skips_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No network must not be reported as a protection failure."""
    monkeypatch.setattr(mod, "resolve_token", lambda: "t")  # noqa: S106

    def _boom(*_args: object, **_kwargs: object):
        raise mod.NetworkUnavailable("Name or service not known")

    monkeypatch.setattr(mod, "fetch_protection", _boom)
    assert mod.main([]) == 0
    assert "unreachable" in capsys.readouterr().out
    assert mod.main(["--fail-on-skip"]) == 2


@pytest.mark.unit
def test_main_exit_codes_and_json_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """1 on findings, 0 when protection is correct; --json stays machine-readable."""
    monkeypatch.setattr(mod, "resolve_token", lambda: "t")  # noqa: S106
    real_expected = mod.expected_contexts(
        mod.discover_check_contexts(mod.WORKFLOWS_DIR)
    )

    monkeypatch.setattr(
        mod,
        "resolve_protection_state",
        lambda *_a, **_k: mod.ProtectionState(
            state=mod.PROTECTION_VERIFIED_ABSENT,
            classic_status=404,
            admin_permission=False,
            protected_flag=False,
        ),
    )
    assert mod.main(["--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["protected"] is False
    assert report["protection_state"] == "verified_absent"
    assert report["protection_reads"]["branch_protected_flag"] is False
    assert report["issue"] == 933
    assert report["expected_required_checks"] == real_expected
    assert "Test Results" in report["action_created_contexts"]

    monkeypatch.setattr(
        mod,
        "resolve_protection_state",
        lambda *_a, **_k: mod.ProtectionState(
            state=mod.PROTECTION_CLASSIC,
            classic=_fully_protected(contexts=real_expected),
            classic_status=200,
            admin_permission=True,
            protected_flag=True,
        ),
    )
    assert mod.main([]) == 0
    assert "✅" in capsys.readouterr().out


@pytest.mark.unit
def test_json_reports_null_protected_when_the_state_is_unverifiable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`protected` must be null, not false, when the answer could not be read.

    A periodic run that emitted `false` here would report a clean "still
    unprotected, nothing changed" in the one case where something did change.
    """
    monkeypatch.setattr(mod, "resolve_token", lambda: "t")  # noqa: S106
    monkeypatch.setattr(
        mod,
        "resolve_protection_state",
        lambda *_a, **_k: mod.ProtectionState(
            state=mod.PROTECTION_UNVERIFIABLE,
            classic_status=404,
            admin_permission=False,
            protected_flag=None,
        ),
    )
    assert mod.main(["--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["protected"] is None
    assert report["protection_state"] == "unverifiable"
    assert [f["key"] for f in report["findings"]] == ["protection_unverifiable"]


@pytest.mark.unit
def test_empty_expectation_refuses_to_report_a_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty derived list means the parser broke, not that all is well."""
    monkeypatch.setattr(mod, "discover_check_contexts", lambda _dir: [])
    assert mod.main([]) == 3
    assert "refusing to report a pass" in capsys.readouterr().out


def _recipe(makefile: str, target: str) -> str:
    """The prerequisites plus recipe body of one make target.

    Sliced precisely — recipe lines are the TAB-indented ones directly after the
    target line. Slicing to the next ``##@`` section header (as
    ``test_ci_gate_parity.py`` does) would swallow every following target in the
    section, so an assertion about ``lint-cicd`` would match text belonging to a
    neighbouring target instead.
    """
    lines = makefile.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    body = [lines[start]]
    for line in lines[start + 1 :]:
        if line.startswith("\t") or not line.strip():
            body.append(line)
        else:
            break
    return "\n".join(body)


@pytest.mark.unit
def test_the_script_is_wired_into_the_makefile_but_not_into_lint() -> None:
    """It must be runnable, and must NOT be a blocking gate until #933 closes.

    Both halves matter. Without the target nobody can run it; inside ``lint-cicd``
    it would fail every branch for a condition no contributor can fix.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\ncheck-branch-protection:" in makefile
    assert "scripts/sdlc/check_branch_protection.py" in makefile

    for target in ("lint", "fastlint"):
        assert "check-branch-protection" not in _recipe(makefile, target), (
            f"check-branch-protection must not be a prerequisite of `{target}`"
        )

    assert "check-branch-protection" not in _recipe(makefile, "lint-cicd"), (
        "check-branch-protection needs network + a token and reports 'not "
        "protected' until issue #933 is closed; in lint-cicd it would red-line "
        "every branch. Re-enable it there only once #933 is closed."
    )

    parity = (REPO_ROOT / "scripts" / "tests" / "test_ci_gate_parity.py").read_text(
        encoding="utf-8"
    )
    assert "check-branch-protection" not in parity, (
        "adding this to SHARED_GATES would require it in both CIs, which is "
        "exactly what it must not be yet"
    )
