#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Measure whether this repo's CI gates actually *block* a merge.

This repo has invested heavily in CI gates — ``make lint-cicd``, ``make
typecheck-pr``, ``make cfn-lint``, ``make dep-audit``, the SRT security scan,
``make api-test-static``, the CI parity guard — and every one of them is
**advisory**. A GitHub pull request can be merged into ``develop`` with every
check red, and a direct push to ``develop`` runs no GitHub workflow at all,
because the workflows are ``pull_request``-only.

``CLAUDE.md`` and ``scripts/sdlc/docs/CI_TEST_COVERAGE.md`` both warned about
this in prose ("being visible is not being blocking"). A warning in a document
is not a measurement. This script is the measurement.

What it asserts, against the live GitHub API:

1. the branch is protected at all — by classic branch protection **or** by a
   ruleset (see "Two mechanisms" below);
2. the required status checks include **every** check context the workflows
   actually produce on a pull request;
3. stale approving reviews are dismissed on a new push;
4. force-pushes and branch deletion are blocked;
5. at least one approving review is required;
6. administrators are not exempt from all of the above (``enforce_admins``).

How the expected check list is derived
--------------------------------------
By **parsing** ``.github/workflows/*.yml``, never by hardcoding an inventory.
A hardcoded list would go stale the moment a workflow job is renamed — and a
stale inventory that still reports "pass" is worse than no check, which is a
defect class this repo has hit repeatedly (see the parity gaps listed in
``scripts/tests/test_ci_gate_parity.py``).

A GitHub Actions status-check *context* is the job's ``name:`` if it declares
one, otherwise the job **id**. So the mapping is per job, not per workflow and
not per step. That matters here: the eight gates asserted by
``test_ci_gate_parity.py``'s ``SHARED_GATES`` are all *steps* inside a **single**
job — ``developer_tests`` in ``.github/workflows/developer-tests.yml`` — and
GitHub can only require job-level contexts, never individual steps. So those
eight gates collapse to exactly **one** requireable context, not three and not
eight. The practical consequence is worth stating: because they share one
context they also share one red mark, so a required-check failure does not say
*which* of the eight failed — that needs the job log. This script prints which
gate commands each context covers so the correspondence is at least visible.

Check runs an action creates are **not** discoverable from job names
--------------------------------------------------------------------
A third-party action can create its own check run with a name of its choosing,
and that name is requireable exactly like a job context — but no amount of
job-level YAML parsing will find it. This script therefore also reads the
``check_name:`` input of each step, which is the convention the actions used here
follow. One such context exists today: ``Test Results``, created by
``EnricoMi/publish-unit-test-result-action`` in ``developer-tests.yml``. It is
reported as **advisory**, because its step is conditional (``if: always() &&
hashFiles(...) != ''``), so on a run that produces no test-results file the check
is never created — the same "required but never reported" hazard as a
path-filtered workflow. Actions that create a check run under a *default* name,
with no ``check_name:`` input to read, remain outside this derivation; there are
none in this repository today.

Two workflows are deliberately **excluded** from the required list:
``build-docs.yml`` and ``generate-dep-manifest.yml`` filter their
``pull_request`` trigger on ``paths:``. A path-filtered workflow does not run at
all on a PR that touches no matching path, so its check is never reported —
and a required check that is never reported sits **pending forever**, blocking
every merge. Requiring one of those would wedge the repository. They are listed
as advisory-only, with that reason.

Two mechanisms, and telling "cannot see" from "not protected"
------------------------------------------------------------
GitHub enforces branch rules through two independent mechanisms, and a branch can
be fully governed by a **ruleset** while classic branch protection reports
nothing at all. So three reads are made, not one:

* ``GET /repos/{slug}/branches/{branch}/protection`` — classic protection.
  Requires repository **admin** and returns **404**, not 403, when admin is
  absent, deliberately, so that it does not disclose whether protection exists.
  A 404 from this endpoint alone is therefore equally consistent with "not
  protected" and with "protected but invisible to me", and reporting it as the
  former would be a false all-clear in exactly the case that matters most —
  right after somebody enables protection.
* ``GET /repos/{slug}/branches/{branch}`` — carries a ``protected`` boolean and
  is readable with plain ``pull`` access. This is what settles the question at
  the permission level this tool actually runs at.
* ``GET /repos/{slug}/rules/branches/{branch}`` — the rules from every ruleset
  that applies to the branch, **including inherited organization and enterprise
  rulesets**, and also readable without admin. That makes it strictly more
  useful than the classic read here.

Measured on this repository (2026-09, token with ``admin: false, maintain:
true``): the classic read returns 404; ``branches/develop`` returns
``"protected": false``; and ``rules/branches/develop`` returns four rules, all
inherited from the ``amazon`` enterprise and all *repository*-scoped
(``repository_visibility`` ×2, ``repository_delete``, ``repository_transfer``).
Of the repository's five active rulesets, four have ``target=repository`` and one
``target=tag`` — none targets a branch. So "no ruleset protects this branch" is a
measurement here, not an error, and the tool reaches a **verified** conclusion
that ``develop`` is unprotected rather than an ambiguous one. The ambiguous
``unverifiable`` state is reserved for when even the ``branches/{branch}`` read
fails.

Why this is opt-in and non-blocking
-----------------------------------
It needs network access and a token, and it reports "not protected" until
GitHub issue #933 is closed — enabling branch protection needs repository
**admin**, which no contributor and no CI token here has. Wiring it into ``make
lint-cicd`` today would red-line every branch for a condition nobody working in
the tree can fix. So it is not in ``lint-cicd`` and not in ``SHARED_GATES``.

TODO(#933): once branch protection is enabled, this SHOULD become a required,
blocking check — add it to ``lint-cicd`` (or a small scheduled workflow) and run
it with ``--fail-on-skip`` so a missing token becomes an error instead of a
silent pass. Until then, drift in the required-check list is invisible again the
moment somebody renames a job.

All GitHub calls are **read-only** (``GET``). This script never writes
repository settings.

Usage:
    python3 scripts/sdlc/check_branch_protection.py
    python3 scripts/sdlc/check_branch_protection.py --branch main
    python3 scripts/sdlc/check_branch_protection.py --json

Token: ``GITHUB_TOKEN`` or ``GH_TOKEN``, else ``gh auth token``. ``pull`` access is
enough to reach a verified answer, via the ``branches/{branch}`` and
``rules/branches/{branch}`` reads; ``administration:read`` is needed only to see
the *detail* of classic protection settings. The token is only ever placed in an
Authorization header — never printed, logged, or written.

Exit codes:
    0 - protection is configured as required (or the check was skipped cleanly)
    1 - findings: not protected, protection state unverifiable, or the
        required-check list has drifted
    2 - the check could not run (no token / no network) and --fail-on-skip was given
    3 - usage or parsing error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess  # nosec B404 - only used to read a token from the `gh` CLI
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    print("Error: PyYAML is not installed.")
    print("Install it with: pip install pyyaml")
    sys.exit(3)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# The repository identity is a single constant, NOT an inventory — deriving it
# from the git remote would mean reading .git/config, whose `github` remote URL
# carries a credential. Override with --repo or $GITHUB_REPOSITORY.
DEFAULT_REPO = (
    "aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws"
)
DEFAULT_BRANCH = "develop"

API_ROOT = "https://api.github.com"
REQUEST_TIMEOUT = 20

# Validated before interpolation into an API URL.
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

# Gate commands worth attributing to a check context, so a reader can see which
# context covers which gate. Informational only — never asserted on, because the
# authoritative list of shared gates lives in test_ci_gate_parity.py.
_GATE_RE = re.compile(
    r"(make [a-z][a-z0-9-]*|python[0-9.]* scripts/[\w/.-]+\.py|npx vitest run)"
)

# `make <word>` also matches prose like `apt-get install make curl -y`, which
# would attribute a nonexistent gate "make curl" to a job. Resolve each match
# against the real Makefile targets instead of guessing — again derived, not a
# hardcoded list.
_TARGET_RE = re.compile(r"^([a-zA-Z0-9_.-]+):", re.MULTILINE)
_MAKEFILES = (
    REPO_ROOT / "Makefile",
    REPO_ROOT / "lib" / "idp_common_pkg" / "Makefile",
)


def _make_targets() -> Optional[frozenset]:
    """Known make targets, or None if no Makefile could be read."""
    targets: set = set()
    for path in _MAKEFILES:
        try:
            targets |= set(_TARGET_RE.findall(path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return frozenset(targets) or None


class NetworkUnavailable(RuntimeError):
    """Raised when the GitHub API is unreachable (offline, DNS, proxy)."""


@dataclass
class CheckContext:
    """One GitHub Actions status-check context produced by a workflow job."""

    context: str
    workflow: str
    job_id: str
    required_eligible: bool
    reason: str = ""
    gates: List[str] = field(default_factory=list)
    # True when the context is a check run an action creates (read from a step's
    # `check_name:`) rather than the job's own status check.
    created_by_action: bool = False


@dataclass
class Finding:
    """One thing that is not configured as it should be."""

    key: str
    message: str
    remedy: str


# Rule types from the ruleset endpoint that are repository-scoped, not
# branch-scoped — see branch_scoped_rules.
_REPOSITORY_SCOPED_RULE_PREFIX = "repository_"

# How much is actually known about the branch's protection. The distinction
# between the last two is the point: a 404 from the classic-protection endpoint
# without admin permission is equally consistent with "not protected" and
# "protected but invisible", and calling that a clean bill of health would be a
# false all-clear in exactly the case that matters most — just after somebody
# turns protection on.
PROTECTION_CLASSIC = "protected_classic"
PROTECTION_RULESET = "protected_by_ruleset"
PROTECTION_VERIFIED_ABSENT = "verified_absent"
PROTECTION_UNVERIFIABLE = "unverifiable"


@dataclass
class ProtectionState:
    """Everything the three read-only endpoints together say about a branch."""

    state: str
    classic: Optional[Dict[str, Any]] = None
    classic_status: int = 0
    admin_permission: Optional[bool] = None
    protected_flag: Optional[bool] = None
    branch_rules: List[Dict[str, Any]] = field(default_factory=list)
    branch_rules_status: int = 0
    ruleset_required_checks: List[str] = field(default_factory=list)

    @property
    def protected(self) -> Optional[bool]:
        """True/False when known, None when it could not be determined."""
        if self.state in (PROTECTION_CLASSIC, PROTECTION_RULESET):
            return True
        if self.state == PROTECTION_VERIFIED_ABSENT:
            return False
        return None


# --------------------------------------------------------------------------- #
# Deriving the expected required-check list from the workflows
# --------------------------------------------------------------------------- #


def _normalize_triggers(raw: Any) -> Dict[str, Any]:
    """Return the workflow's ``on:`` block as a dict of trigger -> config.

    ``on`` is a YAML 1.1 boolean, so ``yaml.safe_load`` parses the key ``on:``
    as Python ``True`` rather than the string ``"on"``. Miss that and every
    workflow looks like it has no triggers and the expected list comes out
    empty — i.e. the check passes vacuously. Both spellings are handled.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(item): None for item in raw}
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    return {}


def _action_check_names(job: Dict[str, Any]) -> List[Tuple[str, bool]]:
    """Check runs this job's steps create via an action, as (name, conditional).

    A third-party action can create a check run under any name, and that name is
    requireable exactly like a job context — but it is not a job, so job-level
    parsing never sees it. ``check_name:`` is the input the actions used here
    expose for it, so it is read directly off each step's ``with:`` block.

    ``conditional`` is True when the step carries an ``if:``. Such a check run is
    not created on every pull request, so it must not become a required context
    for the same reason a path-filtered workflow must not: a required check that
    does not report sits pending and blocks the merge.
    """
    found: List[Tuple[str, bool]] = []
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        params = step.get("with")
        if not isinstance(params, dict):
            continue
        name = params.get("check_name")
        if not name:
            continue
        entry = (str(name), step.get("if") is not None)
        if entry not in found:
            found.append(entry)
    return found


def _job_gates(job: Dict[str, Any], targets: Optional[frozenset] = None) -> List[str]:
    """Gate commands this job's steps run, in order, deduplicated."""
    if targets is None:
        targets = _make_targets()
    gates: List[str] = []
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for match in _GATE_RE.findall(str(step.get("run") or "")):
            if match.startswith("make ") and targets is not None:
                if match.split(" ", 1)[1] not in targets:
                    continue  # prose, not a target — see the note on _make_targets
            if match not in gates:
                gates.append(match)
    return gates


def discover_check_contexts(workflows_dir: Path) -> List[CheckContext]:
    """Parse workflow YAML into the check contexts a pull request produces.

    A context is the job's ``name:`` when set, else its job id — that is how
    GitHub names the status check, and therefore the string branch protection
    has to match.
    """
    if not workflows_dir.is_dir():
        raise FileNotFoundError(f"no workflows directory at {workflows_dir}")

    contexts: List[CheckContext] = []
    targets = _make_targets()
    paths = sorted(
        list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml"))
    )
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"{path.name}: invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            continue

        # `on:` parses as True — see _normalize_triggers.
        triggers = _normalize_triggers(data.get(True, data.get("on")))
        jobs = data.get("jobs") or {}
        if not isinstance(jobs, dict):
            continue

        if "pull_request" not in triggers:
            eligible, reason = False, "not triggered by pull_request"
        else:
            pr = triggers["pull_request"]
            filtered = isinstance(pr, dict) and ("paths" in pr or "paths-ignore" in pr)
            if filtered:
                eligible = False
                reason = (
                    "pull_request trigger is path-filtered: on a PR touching no "
                    "matching path the workflow never runs, so a required check "
                    "would stay pending forever and block every merge"
                )
            else:
                eligible, reason = True, ""

        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            job_eligible, job_reason = eligible, reason
            if job_eligible and isinstance(job.get("strategy"), dict):
                if "matrix" in job["strategy"]:
                    job_eligible = False
                    job_reason = (
                        "matrix job: GitHub suffixes the context per matrix leg, "
                        "so the required name cannot be derived statically"
                    )
            contexts.append(
                CheckContext(
                    context=str(job.get("name") or job_id),
                    workflow=path.name,
                    job_id=str(job_id),
                    required_eligible=job_eligible,
                    reason=job_reason,
                    gates=_job_gates(job, targets),
                )
            )
            # Check runs created by an action, discovered from `check_name:` —
            # see _action_check_names for why job parsing alone cannot see them.
            for check_name, conditional in _action_check_names(job):
                if conditional:
                    action_eligible = False
                    action_reason = (
                        "check run created by an action (check_name:) whose step "
                        "is conditional (if:), so it is not reported on every "
                        "pull request; a required check that does not report "
                        "sits pending forever and blocks every merge"
                    )
                else:
                    action_eligible, action_reason = job_eligible, job_reason
                contexts.append(
                    CheckContext(
                        context=check_name,
                        workflow=path.name,
                        job_id=str(job_id),
                        required_eligible=action_eligible,
                        reason=action_reason,
                        created_by_action=True,
                    )
                )
    return contexts


def expected_contexts(contexts: List[CheckContext]) -> List[str]:
    """The check names branch protection should require, sorted for stability."""
    return sorted(c.context for c in contexts if c.required_eligible)


# --------------------------------------------------------------------------- #
# Reading the live protection state (read-only)
# --------------------------------------------------------------------------- #


def resolve_token() -> Optional[str]:
    """Find a GitHub token. The value is never printed or written anywhere."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(var)
        if value:
            return value.strip()

    # Absolute path via which() so this is not a partial-path process start.
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        completed = subprocess.run(  # nosec B603 - absolute path, fixed argv, no shell
            [gh, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = completed.stdout.strip()
    return token or None


def _validate(repo: str, branch: Optional[str] = None) -> None:
    """Reject anything that must not be interpolated into an API URL."""
    if not _SLUG_RE.match(repo):
        raise ValueError(f"invalid repo slug: {repo!r} (expected owner/name)")
    if branch is not None and not _BRANCH_RE.match(branch):
        raise ValueError(f"invalid branch name: {branch!r}")


def _api_get(path: str, token: str) -> Tuple[Optional[Any], int]:
    """GET one API path. Returns (payload_or_None, status).

    ``None`` with a 403/404 status is a normal answer, not an error: for the
    classic-protection endpoint 404 means "unprotected **or** invisible at this
    permission level" (GitHub returns 404 rather than 403 there on purpose, so as
    not to disclose whether protection exists). Callers disambiguate; nothing
    here ever turns a 404 into a pass.

    Every call is a ``GET`` with no body. This script never writes settings.
    """
    request = urllib.request.Request(  # nosec B310 - constant https:// base; repo/branch validated by _validate
        f"{API_ROOT}/{path}",
        method="GET",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "idp-check-branch-protection",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # nosec B310 - see above
            return json.loads(response.read().decode("utf-8")), response.status
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return None, exc.code
        raise
    except urllib.error.URLError as exc:
        raise NetworkUnavailable(str(exc.reason)) from exc


def fetch_protection(
    repo: str, branch: str, token: str
) -> Tuple[Optional[Dict[str, Any]], int]:
    """GET classic branch protection. Returns (payload_or_None, status).

    Needs repository **admin**; without it the answer is 404 and is therefore
    ambiguous on its own. ``fetch_branch_summary`` settles it.
    """
    _validate(repo, branch)
    payload, status = _api_get(f"repos/{repo}/branches/{branch}/protection", token)
    return (payload if isinstance(payload, dict) else None), status


def fetch_admin_permission(repo: str, token: str) -> Optional[bool]:
    """Whether this token has repository admin, or None if that is unreadable.

    Read so that a 404 from the classic-protection endpoint can be attributed:
    without admin, 404 says nothing about whether protection exists.
    """
    payload, _status = _api_get(f"repos/{repo}", token)
    if not isinstance(payload, dict):
        return None
    permissions = payload.get("permissions")
    if not isinstance(permissions, dict):
        return None
    return bool(permissions.get("admin"))


def fetch_branch_summary(repo: str, branch: str, token: str) -> Optional[bool]:
    """The branch's ``protected`` boolean, or None if the branch is unreadable.

    Readable with plain ``pull`` access, unlike the classic-protection endpoint,
    so this is what lets the tool reach a *verified* conclusion at the permission
    level it normally runs with.
    """
    _validate(repo, branch)
    payload, _status = _api_get(f"repos/{repo}/branches/{branch}", token)
    if not isinstance(payload, dict) or "protected" not in payload:
        return None
    return bool(payload.get("protected"))


def fetch_branch_rules(
    repo: str, branch: str, token: str
) -> Tuple[Optional[List[Dict[str, Any]]], int]:
    """Rules from every ruleset that applies to the branch.

    Includes inherited organization and enterprise rulesets and is readable
    without admin, which makes it strictly more informative than the classic read
    at the permission level this tool typically runs with. A branch can be fully
    governed by a ruleset while classic protection reports nothing.
    """
    _validate(repo, branch)
    payload, status = _api_get(f"repos/{repo}/rules/branches/{branch}", token)
    if not isinstance(payload, list):
        return None, status
    return [rule for rule in payload if isinstance(rule, dict)], status


def branch_scoped_rules(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The subset of returned rules that actually govern this branch.

    The endpoint also returns *repository*-scoped rules, which say nothing about
    the branch: on this repository it returns only ``repository_visibility``,
    ``repository_delete`` and ``repository_transfer``, all inherited from the
    enterprise, even though no ruleset targets a branch. Treating a non-empty
    response as "protected" would therefore be wrong. Rule types are filtered by
    the ``repository_`` prefix rather than matched against an allowlist of branch
    rule types, so a branch rule type GitHub adds later is still counted.
    """
    return [
        rule
        for rule in rules
        if not str(rule.get("type", "")).startswith(_REPOSITORY_SCOPED_RULE_PREFIX)
    ]


def ruleset_required_check_names(rules: List[Dict[str, Any]]) -> List[str]:
    """Required status-check contexts declared by ``required_status_checks`` rules."""
    names: set = set()
    for rule in rules:
        if rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters")
        if not isinstance(params, dict):
            continue
        for check in params.get("required_status_checks") or []:
            if isinstance(check, dict) and check.get("context"):
                names.add(str(check["context"]))
    return sorted(names)


def resolve_protection_state(repo: str, branch: str, token: str) -> ProtectionState:
    """Read all three endpoints and classify what is actually known."""
    _validate(repo, branch)
    classic, classic_status = fetch_protection(repo, branch, token)
    admin = fetch_admin_permission(repo, token)
    protected_flag = fetch_branch_summary(repo, branch, token)
    rules, rules_status = fetch_branch_rules(repo, branch, token)
    branch_rules = branch_scoped_rules(rules or [])

    if classic is not None:
        state = PROTECTION_CLASSIC
    elif branch_rules:
        state = PROTECTION_RULESET
    elif protected_flag is False:
        # Verified absent: `branches/{branch}` is readable with pull access and
        # says the branch is not protected, and no ruleset rule governs it.
        state = PROTECTION_VERIFIED_ABSENT
    elif protected_flag is True:
        # Protected, but the detail is behind an endpoint this token cannot read.
        state = PROTECTION_UNVERIFIABLE
    else:
        state = PROTECTION_UNVERIFIABLE

    return ProtectionState(
        state=state,
        classic=classic,
        classic_status=classic_status,
        admin_permission=admin,
        protected_flag=protected_flag,
        branch_rules=branch_rules,
        branch_rules_status=rules_status,
        ruleset_required_checks=ruleset_required_check_names(branch_rules),
    )


# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #


def _required_check_names(protection: Dict[str, Any]) -> List[str]:
    """Required check names from either response shape.

    The API returns both the newer ``checks: [{context, app_id}]`` list and the
    deprecated flat ``contexts: [...]``. Read both so this does not depend on
    which one a given repository's settings populate.
    """
    block = protection.get("required_status_checks") or {}
    names = {str(name) for name in block.get("contexts") or []}
    for check in block.get("checks") or []:
        if isinstance(check, dict) and check.get("context"):
            names.add(str(check["context"]))
    return sorted(names)


def _expected_list(expected: List[str]) -> str:
    return ", ".join(expected) if expected else "(none derived!)"


def _evaluate_ruleset(
    state: ProtectionState, expected: List[str], branch: str
) -> List[Finding]:
    """Assert against ruleset rules when classic protection is not visible.

    Deliberately the same five questions the classic path asks, keyed distinctly
    so a report says which mechanism it read. Without this, a repository governed
    entirely by a ruleset would be reported as "protected" and never checked.
    """
    findings: List[Finding] = []
    types = {str(rule.get("type")) for rule in state.branch_rules}

    if "required_status_checks" not in types:
        findings.append(
            Finding(
                key="ruleset_no_required_status_checks",
                message=(
                    f"a ruleset governs {branch!r} but declares no "
                    f"required_status_checks rule, so the CI gates still do not "
                    f"block a merge"
                ),
                remedy=f"require these checks: {_expected_list(expected)}",
            )
        )
    else:
        live = state.ruleset_required_checks
        missing = [name for name in expected if name not in live]
        if missing:
            findings.append(
                Finding(
                    key="ruleset_missing_required_checks",
                    message=(
                        "these check contexts run on every pull request but the "
                        f"ruleset does NOT require them: {', '.join(missing)}"
                    ),
                    remedy="add them to the ruleset's required status checks",
                )
            )
        stale = [name for name in live if name not in expected]
        if stale:
            findings.append(
                Finding(
                    key="ruleset_unknown_required_checks",
                    message=(
                        "the ruleset requires these checks but no workflow job or "
                        "action produces them, so they may never report and could "
                        f"block merges indefinitely: {', '.join(stale)}"
                    ),
                    remedy=(
                        "remove them from the ruleset, or rename the workflow job "
                        "back to match"
                    ),
                )
            )

    if "pull_request" not in types:
        findings.append(
            Finding(
                key="ruleset_no_pull_request_review",
                message=(
                    f"the ruleset on {branch!r} has no pull_request rule, so no "
                    f"review is required and a commit can be pushed straight to it"
                ),
                remedy="add a pull_request rule requiring at least 1 approval",
            )
        )
    if "non_fast_forward" not in types:
        findings.append(
            Finding(
                key="ruleset_force_pushes_allowed",
                message=(
                    f"the ruleset on {branch!r} has no non_fast_forward rule, so "
                    f"history can be rewritten"
                ),
                remedy="add a non_fast_forward rule to block force pushes",
            )
        )
    if "deletion" not in types:
        findings.append(
            Finding(
                key="ruleset_deletions_allowed",
                message=f"the ruleset on {branch!r} has no deletion rule",
                remedy="add a deletion rule to block branch deletion",
            )
        )
    return findings


def evaluate(
    protection: Optional[Dict[str, Any]],
    expected: List[str],
    branch: str,
    status: int = 0,
    state: Optional[ProtectionState] = None,
    derived: Optional[List[str]] = None,
) -> List[Finding]:
    """Compare live protection against what the workflows imply it should be.

    ``derived`` is every context the workflows produce, advisory ones included, so
    a required-but-conditional check can be told apart from a required name
    nothing produces. Defaults to ``expected``.

    ``state`` carries what the three read-only endpoints together established.
    When it is omitted the ``protection`` payload is taken as the whole truth —
    that is the pure-function path used by the offline tests, where a missing
    payload really does mean "absent". ``main`` always passes a real state, so the
    "cannot see" case is never silently reported as "not protected".
    """
    if state is None:
        state = ProtectionState(
            state=PROTECTION_CLASSIC if protection else PROTECTION_VERIFIED_ABSENT,
            classic=protection,
            classic_status=status,
        )

    if state.state == PROTECTION_UNVERIFIABLE:
        seen = (
            "GET .../branches/{b}/protection returned {s}, and the fallback GET "
            ".../branches/{b} did not yield a `protected` flag either"
        ).format(b=branch, s=state.classic_status)
        return [
            Finding(
                key="protection_unverifiable",
                message=(
                    f"the protection state of {branch!r} could NOT be determined "
                    f"at this permission level (repository admin: "
                    f"{state.admin_permission}). {seen}. This is NOT a clean bill "
                    f"of health: the classic endpoint returns 404 rather than 403 "
                    f"without admin, so 'protected but invisible' and 'not "
                    f"protected' look identical from here. Expected required "
                    f"checks, derived from .github/workflows/: "
                    + _expected_list(expected)
                ),
                remedy=(
                    "re-run with a token that has administration:read, or at least "
                    "pull access to the branch, before drawing any conclusion"
                ),
            )
        ]

    if state.state == PROTECTION_VERIFIED_ABSENT:
        return [
            Finding(
                key="not_protected",
                message=(
                    f"branch {branch!r} is NOT protected — verified, not merely "
                    f"invisible: GET .../branches/{branch} reports "
                    f"'protected': false, no ruleset rule governs the branch, and "
                    f"GET .../branches/{branch}/protection returned "
                    f"{state.classic_status}. Every CI gate is therefore advisory: "
                    f"a pull request can be merged with all checks red, and a "
                    f"direct push to {branch} runs no GitHub workflow at all. "
                    f"Expected required checks, derived from .github/workflows/: "
                    + _expected_list(expected)
                ),
                remedy=(
                    "Needs repository admin — tracked by issue #933. Enabling it "
                    "via a ruleset works too; this check reads both mechanisms."
                ),
            )
        ]

    if state.state == PROTECTION_RULESET or protection is None:
        return _evaluate_ruleset(state, expected, branch)

    findings: List[Finding] = []
    block = protection.get("required_status_checks")

    if not block:
        findings.append(
            Finding(
                key="no_required_status_checks",
                message=(
                    "protection is enabled but requires NO status checks, so the "
                    "CI gates still do not block a merge"
                ),
                remedy=f"require these checks: {', '.join(expected) or '(none)'}",
            )
        )
    else:
        # A ruleset can require checks in addition to classic protection, and both
        # mechanisms gate simultaneously, so the union is what actually blocks.
        live = sorted(
            set(_required_check_names(protection)) | set(state.ruleset_required_checks)
        )
        missing = [name for name in expected if name not in live]
        if missing:
            findings.append(
                Finding(
                    key="missing_required_checks",
                    message=(
                        "these check contexts run on every pull request but are "
                        f"NOT required: {', '.join(missing)}"
                    ),
                    remedy="add them to the branch-protection required checks",
                )
            )
        stale = [name for name in live if name not in expected]
        # A required name this repo *does* produce, but only conditionally (a
        # path-filtered workflow, a matrix leg, an action-created check behind an
        # `if:`), is a different defect from a name nothing produces at all — and
        # advising an administrator to "remove" it would be wrong. Split them.
        known = set(derived or expected)
        conditional = [name for name in stale if name in known]
        unknown = [name for name in stale if name not in known]
        if conditional:
            findings.append(
                Finding(
                    key="required_but_not_always_reported",
                    message=(
                        "these checks are required, and this repo does produce "
                        "them, but not on every pull request (path-filtered "
                        "workflow, matrix leg, or a conditional action-created "
                        "check run), so they can sit pending and block a merge "
                        f"indefinitely: {', '.join(conditional)}"
                    ),
                    remedy=(
                        "un-require them, or make them unconditional — do NOT "
                        "remove the job, the check itself is wanted"
                    ),
                )
            )
        if unknown:
            findings.append(
                Finding(
                    key="unknown_required_checks",
                    message=(
                        "these checks are required but nothing in "
                        ".github/workflows/ produces them, so they may never "
                        "report and could block merges indefinitely: "
                        f"{', '.join(unknown)}"
                    ),
                    remedy=(
                        "remove them, or rename the workflow job back to match "
                        "(a renamed job silently stops being gated)"
                    ),
                )
            )
        if not block.get("strict"):
            findings.append(
                Finding(
                    key="not_strict",
                    message=(
                        "'require branches to be up to date before merging' is "
                        "off, so a PR can pass against a stale base"
                    ),
                    remedy="enable strict required status checks",
                )
            )

    reviews = protection.get("required_pull_request_reviews")
    if not reviews:
        findings.append(
            Finding(
                key="no_pull_request_reviews",
                message="no pull request review is required before merging",
                remedy="require at least 1 approving review",
            )
        )
    else:
        count = reviews.get("required_approving_review_count") or 0
        if count < 1:
            findings.append(
                Finding(
                    key="no_approving_review",
                    message=(
                        f"required approving review count is {count}; at least 1 "
                        f"approval should be required"
                    ),
                    remedy="set required approving reviews to 1 or more",
                )
            )
        if not reviews.get("dismiss_stale_reviews"):
            findings.append(
                Finding(
                    key="stale_reviews_kept",
                    message=(
                        "stale approvals are not dismissed on a new push, so an "
                        "approval of reviewed code carries over to code nobody "
                        "reviewed"
                    ),
                    remedy="enable 'dismiss stale pull request approvals'",
                )
            )
        # A bypass list lets the named users, teams or apps merge without the
        # approval the setting above appears to require, so an unread bypass list
        # makes "1 approval required" mean nothing for whoever is on it.
        bypass = reviews.get("bypass_pull_request_allowances") or {}
        allowed = [
            f"{len(bypass.get(kind) or [])} {kind}"
            for kind in ("users", "teams", "apps")
            if bypass.get(kind)
        ]
        if allowed:
            findings.append(
                Finding(
                    key="pull_request_review_bypass_allowed",
                    message=(
                        "these principals may merge without the required approval "
                        f"(bypass_pull_request_allowances): {', '.join(allowed)}"
                    ),
                    remedy=(
                        "empty the bypass list, or record why each entry needs to "
                        "merge unreviewed"
                    ),
                )
            )

    # enforce_admins was fetched but never examined before this. It is the setting
    # that decides whether everything above actually applies: with it off, an
    # administrator can push straight past every required check, so a branch can
    # report as fully protected while remaining bypassable by the people most
    # likely to be merging.
    if not (protection.get("enforce_admins") or {}).get("enabled"):
        findings.append(
            Finding(
                key="admins_not_enforced",
                message=(
                    f"'enforce_admins' is off, so administrators are exempt from "
                    f"every rule above and can push directly to {branch} past all "
                    f"required checks and the review requirement"
                ),
                remedy="enable 'Do not allow bypassing the above settings'",
            )
        )

    # `restrictions` (the push allowlist) is deliberately NOT asserted on. It
    # restricts who may push to the branch at all, which for this repository is
    # already covered by requiring a pull request: an empty restrictions block is
    # the normal, correct configuration for an open-source repo taking outside
    # contributions, so reporting its absence would be a false finding. It is
    # surfaced verbatim in --json for a reader who wants it.
    if (protection.get("allow_force_pushes") or {}).get("enabled"):
        findings.append(
            Finding(
                key="force_pushes_allowed",
                message=f"force pushes to {branch} are allowed, so history can be rewritten",
                remedy="disable force pushes",
            )
        )
    if (protection.get("allow_deletions") or {}).get("enabled"):
        findings.append(
            Finding(
                key="deletions_allowed",
                message=f"deletion of {branch} is allowed",
                remedy="disable branch deletion",
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


_STATE_LABEL = {
    PROTECTION_CLASSIC: "PROTECTED (classic branch protection)",
    PROTECTION_RULESET: "PROTECTED (by a ruleset; classic protection not visible)",
    PROTECTION_VERIFIED_ABSENT: "NOT PROTECTED (verified)",
    PROTECTION_UNVERIFIABLE: "UNVERIFIABLE at this permission level",
}


def print_report(
    repo: str,
    branch: str,
    contexts: List[CheckContext],
    findings: List[Finding],
    state: Optional[ProtectionState] = None,
) -> None:
    print(f"\nBranch protection check: {repo} @ {branch}")
    print("=" * 78)

    eligible = [c for c in contexts if c.required_eligible]
    advisory = [c for c in contexts if not c.required_eligible]

    print(
        f"\nDerived from .github/workflows/ ({len(contexts)} check context(s) "
        f"across {len({c.job_id for c in contexts})} job(s) in "
        f"{len({c.workflow for c in contexts})} workflow file(s)).\n"
    )
    print(f"Should be REQUIRED status checks ({len(eligible)}):")
    for ctx in eligible:
        print(f"  • {ctx.context}")
        print(f"      from {ctx.workflow} :: job '{ctx.job_id}'")
        if ctx.created_by_action:
            print("      created by an action (check_name:), not by the job itself")
        if ctx.gates:
            print(f"      covers: {', '.join(ctx.gates)}")
    if advisory:
        print(f"\nMust stay advisory ({len(advisory)}):")
        for ctx in advisory:
            print(f"  • {ctx.context}  (from {ctx.workflow} :: job '{ctx.job_id}')")
            print(f"      {ctx.reason}")

    if state is not None:
        print("\n" + "-" * 78)
        print(f"\nLive protection state: {_STATE_LABEL.get(state.state, state.state)}")
        print(
            f"  GET .../branches/{branch}/protection  -> {state.classic_status} "
            f"(needs repo admin; this token has admin={state.admin_permission})"
        )
        print(
            f"  GET .../branches/{branch}            -> "
            f"protected={state.protected_flag}"
        )
        rule_types = sorted({str(r.get("type")) for r in state.branch_rules})
        print(
            f"  GET .../rules/branches/{branch}      -> {state.branch_rules_status}, "
            + (
                f"branch-scoped rules: {', '.join(rule_types)}"
                if rule_types
                else "no branch-scoped rule from any ruleset "
                "(repository- and tag-scoped rules do not protect a branch)"
            )
        )
        if state.ruleset_required_checks:
            print(
                "  ruleset required checks: "
                + ", ".join(state.ruleset_required_checks)
            )

    print("\n" + "-" * 78)
    if not findings:
        print(f"\n✅ {branch} is protected and requires every pull-request check.")
        return

    print(f"\n❌ FINDINGS ({len(findings)}):\n")
    for i, finding in enumerate(findings, 1):
        print(f"  {i}. [{finding.key}] {finding.message}")
        print(f"     → {finding.remedy}\n")
    print(
        "Enabling branch protection requires repository ADMIN, which contributor\n"
        "and CI tokens here do not have. GitHub issue #933 tracks enabling it:\n"
        "  https://github.com/aws-solutions-library-samples/"
        "accelerated-intelligent-document-processing-on-aws/issues/933\n"
        "This check is opt-in and gates nothing today; it should become a\n"
        "required, blocking check once #933 is closed."
    )


def _skip(reason: str, detail: str, fail_on_skip: bool) -> int:
    print(f"\nBranch protection check: SKIPPED — {reason}")
    print(f"  {detail}")
    print(
        "\n  This check needs network access and a GitHub token. `pull` access is\n"
        "  enough to reach a verified answer; administration:read additionally\n"
        "  reveals the detail of classic protection settings.\n"
        "  It is opt-in by design and gates nothing, so a skip is not a failure.\n"
        "  Pass --fail-on-skip to make an unrunnable check an error instead "
        "(do that\n  once issue #933 is closed and this becomes a blocking gate)."
    )
    return 2 if fail_on_skip else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Assert that GitHub branch protection actually requires the CI "
            "checks the workflows produce. Read-only; never writes settings."
        )
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO,
        help="owner/name (default: this repository)",
    )
    parser.add_argument(
        "--branch", default=DEFAULT_BRANCH, help="branch to check (default: develop)"
    )
    parser.add_argument(
        "--fail-on-skip",
        action="store_true",
        help="exit 2 when the check cannot run, instead of 0",
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    args = parser.parse_args(argv)

    try:
        contexts = discover_check_contexts(WORKFLOWS_DIR)
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ could not derive the expected check list: {exc}")
        return 3
    expected = expected_contexts(contexts)

    if not expected:
        print(
            "❌ no pull-request check contexts derived from "
            f"{WORKFLOWS_DIR.relative_to(REPO_ROOT)} — refusing to report a pass "
            "from an empty expectation."
        )
        return 3

    token = resolve_token()
    if not token:
        return _skip(
            "no GitHub token available",
            "Set GITHUB_TOKEN or GH_TOKEN, or run `gh auth login`.",
            args.fail_on_skip,
        )

    try:
        state = resolve_protection_state(args.repo, args.branch, token)
    except NetworkUnavailable as exc:
        return _skip("the GitHub API is unreachable", f"{exc}", args.fail_on_skip)
    except urllib.error.HTTPError as exc:
        print(f"❌ GitHub API error {exc.code} for {args.repo}@{args.branch}")
        return 3
    except ValueError as exc:
        print(f"❌ {exc}")
        return 3

    findings = evaluate(
        state.classic,
        expected,
        args.branch,
        state.classic_status,
        state=state,
        derived=[c.context for c in contexts],
    )

    if args.json:
        live = set(state.ruleset_required_checks)
        if state.classic:
            live |= set(_required_check_names(state.classic))
        print(
            json.dumps(
                {
                    "repo": args.repo,
                    "branch": args.branch,
                    # Tri-state on purpose: null means "could not be determined at
                    # this permission level", which is NOT the same as false. See
                    # protection_state for which read produced the answer.
                    "protected": state.protected,
                    "protection_state": state.state,
                    "protection_reads": {
                        "classic_protection_status": state.classic_status,
                        "branch_protected_flag": state.protected_flag,
                        "branch_rules_status": state.branch_rules_status,
                        "token_has_admin": state.admin_permission,
                    },
                    "classic_protection": state.classic,
                    "branch_scoped_ruleset_rule_types": sorted(
                        {str(r.get("type")) for r in state.branch_rules}
                    ),
                    "expected_required_checks": expected,
                    "live_required_checks": sorted(live),
                    "advisory_only": [
                        {"context": c.context, "reason": c.reason}
                        for c in contexts
                        if not c.required_eligible
                    ],
                    "action_created_contexts": [
                        c.context for c in contexts if c.created_by_action
                    ],
                    "findings": [
                        {"key": f.key, "message": f.message, "remedy": f.remedy}
                        for f in findings
                    ],
                    "issue": 933,
                },
                indent=2,
            )
        )
    else:
        print_report(args.repo, args.branch, contexts, findings, state)

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
