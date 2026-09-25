#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Measure whether this repo's CI gates actually *block* a merge.

This repo has invested heavily in CI gates — ``make lint-cicd``, ``make
typecheck``, ``make cfn-lint``, ``make dep-audit``, the SRT security scan,
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
not per step. That matters here: ten of the twelve gates asserted by
``test_ci_gate_parity.py``'s ``SHARED_GATES`` are *steps* inside a **single**
job -- ``developer_tests`` in ``.github/workflows/developer-tests.yml`` -- and
GitHub can only require job-level contexts, never individual steps. So those
ten collapse to exactly **one** requireable context rather than one per gate.
The practical consequence is worth stating: because they share one
context they also share one red mark, so a required-check failure does not say
*which* of the eight failed — that needs the job log. The remaining two shared
gates, the SRT scan and the dependency audit, are jobs of their own in
``security-checks.yml`` and so carry a context each. This script prints which
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

Four shapes make a context conditional, and all four are checked
-----------------------------------------------------------------
``paths:``/``paths-ignore:`` is only one of them. A ``pull_request: branches:``
list that does not select the branch being checked has exactly the same effect,
which is why the branch is an input to the derivation rather than the workflow
being judged on its own — the same ``security-checks.yml`` is safely requireable
on ``develop`` and not on ``main`` if it is narrowed to ``branches: [main]``. A
``strategy.matrix`` makes the context name non-static, because GitHub suffixes it
per leg. And a **job-level** ``if:`` means the job does not run on every pull
request; GitHub reports a conditionally skipped job as *successful* rather than
leaving it pending, so requiring that context fails quietly in the other
direction — the gate passes without its work having run. None of these four
shapes other than ``paths:`` exists in this repository today, and the point of
checking them is that this tool's output is what somebody acts on if protection
is ever turned on — by which time the workflows will have moved on.

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
  the permission level this tool actually runs at. It also nests a ``protection``
  object carrying ``required_status_checks``, which is read too: if classic
  protection is ever enabled here, a non-admin run can compare the required-check
  list from this response instead of reporting ``unverifiable`` while holding the
  comparison data. That is a partial answer — the object says nothing about
  reviews, force-pushes, deletion or ``enforce_admins`` — so the other five
  questions are reported as **unread**, never as satisfied, and such a run still
  exits non-zero.
* ``GET /repos/{slug}/rules/branches/{branch}`` — the rules from every ruleset
  that applies to the branch, **including inherited organization and enterprise
  rulesets**, and also readable without admin. That makes it strictly more
  useful than the classic read here.

Measured on this repository (2026-09-21, token with ``admin: false, maintain:
true``): the classic read returns 404; ``branches/develop`` and ``branches/main``
both return ``"protected": false`` with ``required_status_checks`` of
``{checks: [], contexts: [], enforcement_level: "off"}``; and
``rules/branches/{branch}`` returns four rules, all inherited from the ``amazon``
enterprise and all *repository*-scoped (``repository_visibility`` ×2,
``repository_delete``, ``repository_transfer``). Of the repository's five active
rulesets, four have ``target=repository`` and one ``target=tag`` — none targets a
branch. So "no ruleset protects this branch" is a measurement here, not an error,
and the tool reaches a **verified** conclusion that the branch is unprotected
rather than an ambiguous one. The ambiguous ``unverifiable`` state is reserved for
when even the ``branches/{branch}`` read fails.

**Run it against both long-lived branches.** ``develop`` is what pull requests
target and is this script's default, but ``main`` is the repository's *default*
branch and the one releases are cut from, and it is equally unprotected. A single
run answers the question for one branch only.

Why this is opt-in and non-blocking
-----------------------------------
It needs network access and a token, and on this repository it reports "not
protected" on both of those branches. Wiring it into ``make lint-cicd`` would
red-line every branch for a condition nobody working in the tree can fix. So it
is not in ``lint-cicd`` and not in ``SHARED_GATES``, and
``scripts/tests/test_check_branch_protection.py`` fails if it is added to either,
or invoked from either CI configuration.

That absence of protection is a **known, accepted residual**, not an open task.
Enabling classic branch protection needs repository **admin**, which no
contributor and no CI token here has, so it cannot be done from the tree or from
tooling; the decision to stop pursuing it from inside the repository is recorded
in closed issue #933:
https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/933

**Nothing in this repository can substitute.** Enforcement is server-side by
construction: a merge taken through GitHub's own Merge button runs no code from
this tree, so no hook, script or gate here can turn a red check into a refused
merge. That is why the residual is reported rather than worked around.

So the condition for making this a required, blocking check is a **repository
setting changing**, by one of two routes — and it is worth separating them,
because they are not the same permission:

1. somebody with repository **admin** enables classic branch protection; or
2. an organization or enterprise owner publishes a **branch ruleset** targeting
   these branches. This repository already inherits five enterprise rulesets, so
   the mechanism is demonstrably available here; none of the five targets a
   branch. This route needs no repository admin at all, which is why "needs repo
   admin" is not the whole story.

Neither is actionable from this tree, and neither announces itself — running this
script is how either would be noticed. When one of them happens, add it to
``lint-cicd`` (or a small scheduled workflow) and pass ``--fail-on-skip`` so a
missing token becomes an error instead of a silent pass. Until then, drift in the
required-check list is invisible the moment somebody renames a job.

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

On this repository the steady-state result today is **exit 1** with a single
``not_protected`` finding, on ``develop`` and on ``main`` alike. That is the
expected answer, not a regression, and it is the reason this is not a gate.
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
# `develop` is what pull requests target, so it is the default here. It is NOT the
# repository's default branch — `main` is, and `main` is where releases are cut
# from — so answering the question for this repository takes two runs. Both are
# unprotected today; see the module docstring.
DEFAULT_BRANCH = "develop"
SHARED_BRANCHES = ("develop", "main")

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
# Protected, and the required-check list is readable from the branch *summary*
# even though the classic endpoint 404s for want of admin. This is the state a
# non-admin run lands in if classic protection is ever enabled here, and
# it exists so that such a run reports a verified answer to the question it can
# answer instead of collapsing to `unverifiable` — the comparison data is in a
# response the tool already made.
PROTECTION_SUMMARY = "protected_branch_summary"
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
    # The `protection` object nested in GET .../branches/{branch}, kept only when
    # informative — see _summary_protection.
    summary_protection: Optional[Dict[str, Any]] = None

    @property
    def summary_required_checks(self) -> List[str]:
        """Required contexts the branch summary declares, if any."""
        if not self.summary_protection:
            return []
        return _summary_required_check_names(self.summary_protection)

    def live_required_checks(self) -> List[str]:
        """Every required context, from all three readable sources.

        Classic protection, rulesets and the branch summary all gate at once, so
        the union is what actually blocks a merge. Reading fewer than all three
        would report a check as missing when it is in fact required.
        """
        names = set(self.ruleset_required_checks) | set(self.summary_required_checks)
        if self.classic:
            names |= set(_required_check_names(self.classic))
        return sorted(names)

    @property
    def protected(self) -> Optional[bool]:
        """True/False when known, None when it could not be determined."""
        if self.state in (PROTECTION_CLASSIC, PROTECTION_RULESET, PROTECTION_SUMMARY):
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


def _branch_pattern_to_regex(pattern: str) -> Optional[re.Pattern]:
    """One GitHub filter pattern as a regex, or None if it cannot be translated.

    In GitHub's filter-pattern syntax ``*`` and ``?`` stop at ``/`` while ``**``
    crosses it. ``+`` (one-or-more of the preceding character) and character
    ranges (``[...]``) are also part of the syntax and are deliberately **not**
    translated: returning ``None`` makes the caller give up and classify the
    context advisory, which is the safe direction. Guessing wrong in the other
    direction would advise requiring a check that never reports.
    """
    out = ["^"]
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern[i : i + 2] == "**":
            out.append(".*")
            i += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char in "+[]":
            return None
        else:
            out.append(re.escape(char))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def _branch_filter_admits(raw: Any, branch: str, *, exclude: bool) -> Optional[bool]:
    """Whether ``branch`` passes a ``branches:``/``branches-ignore:`` list.

    Returns True when the workflow does run on a pull request targeting
    ``branch``, False when it does not, and **None** when that could not be
    decided — an unsupported filter pattern, or a shape that is not a list of
    strings. The caller treats None like False, because an advisory check is
    harmless and a wrongly-required one wedges every merge.

    For ``branches:`` the patterns are evaluated in order and a leading ``!``
    negates, which is GitHub's documented precedence: a later matching pattern
    overrides an earlier one. ``branches-ignore:`` admits anything it does not
    match, and GitHub does not allow ``!`` there.
    """
    patterns = [raw] if isinstance(raw, str) else raw
    if not isinstance(patterns, list) or not patterns:
        return None

    if exclude:
        for item in patterns:
            regex = _branch_pattern_to_regex(str(item))
            if regex is None:
                return None
            if regex.match(branch):
                return False
        return True

    admitted = False
    for item in patterns:
        text = str(item)
        negated = text.startswith("!")
        regex = _branch_pattern_to_regex(text[1:] if negated else text)
        if regex is None:
            return None
        if regex.match(branch):
            admitted = not negated
    return admitted


def _pull_request_eligibility(pr: Any, branch: str) -> Tuple[bool, str]:
    """Whether a ``pull_request:`` trigger reports on every PR targeting ``branch``.

    Three ways it does not, each of which makes requiring the resulting context
    unsafe:

    * ``paths:``/``paths-ignore:`` — the workflow does not run at all on a PR
      touching no matching path, so the check is never reported;
    * ``branches:`` that does not select the branch being checked — likewise, and
      note that for ``pull_request`` these filter on the PR's **base** branch,
      which is exactly the branch whose protection is under examination;
    * ``branches-ignore:`` that excludes it.

    GitHub is explicit that a check belonging to a workflow skipped by path or
    branch filtering stays **pending**, and a pull request requiring it can never
    be merged. So each of these is reported advisory, with the reason.
    """
    if not isinstance(pr, dict):
        return True, ""

    if "paths" in pr or "paths-ignore" in pr:
        return False, (
            "pull_request trigger is path-filtered: on a PR touching no "
            "matching path the workflow never runs, so a required check "
            "would stay pending forever and block every merge"
        )

    for key, exclude in (("branches", False), ("branches-ignore", True)):
        if key not in pr:
            continue
        admits = _branch_filter_admits(pr[key], branch, exclude=exclude)
        if admits is True:
            continue
        if admits is False:
            return False, (
                f"pull_request trigger's {key}: filter does not select "
                f"{branch!r}, so the workflow never runs on a pull request "
                f"targeting it and a required check would stay pending forever "
                f"and block every merge"
            )
        return False, (
            f"pull_request trigger's {key}: filter could not be evaluated "
            f"against {branch!r} (filter-pattern syntax this tool does not "
            f"translate), so whether the workflow runs on a PR targeting it is "
            f"unknown; treated as advisory because a required check that never "
            f"reports blocks every merge"
        )

    return True, ""


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


def discover_check_contexts(
    workflows_dir: Path, branch: str = DEFAULT_BRANCH
) -> List[CheckContext]:
    """Parse workflow YAML into the check contexts a pull request produces.

    A context is the job's ``name:`` when set, else its job id — that is how
    GitHub names the status check, and therefore the string branch protection
    has to match.

    ``branch`` is the branch whose protection is being checked. It is needed
    because eligibility is **not** a property of the workflow alone: a
    ``pull_request: branches:`` filter that does not select this branch means the
    workflow never runs on a PR targeting it, so its context must not be
    required here even though it is perfectly requireable on another branch.
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
            eligible, reason = _pull_request_eligibility(
                triggers["pull_request"], branch
            )

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
            # A job-level `if:` means the job does not run on every pull request.
            # Its failure mode differs from a filtered *workflow* and is worth
            # stating precisely: GitHub reports a job skipped by a conditional as
            # succeeding rather than leaving it pending, so requiring such a
            # context does not wedge the branch — it does something quieter and
            # arguably worse, passing the gate on every PR that skips the job
            # without any of its work having run. Either way the context does not
            # mean what requiring it implies, so it is advisory.
            if job_eligible and job.get("if") is not None:
                job_eligible = False
                job_reason = (
                    "job-level `if:` condition, so the job does not run on every "
                    "pull request: requiring its context would either leave a "
                    "check pending or — because GitHub reports a conditionally "
                    "skipped job as successful — let the gate pass without having "
                    "run at all"
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


def fetch_branch_summary(
    repo: str, branch: str, token: str
) -> Tuple[Optional[bool], Optional[Dict[str, Any]]]:
    """The branch's ``protected`` flag **and** its nested ``protection`` object.

    Readable with plain ``pull`` access, unlike the classic-protection endpoint,
    so this is what lets the tool reach a *verified* conclusion at the permission
    level it normally runs with.

    Both halves are returned because the same response carries both, and throwing
    the second away is what would make this tool fail the moment protection is
    actually enabled: without admin the classic read 404s, so if the only thing
    kept from here were the boolean, a protected branch would be classified
    ``unverifiable`` while the required-check list needed to check it sat in a
    response already in hand. The nested object looks like::

        "protection": {
            "enabled": true,
            "required_status_checks": {
                "enforcement_level": "non_admins",
                "contexts": ["Lint, Type Check, and Test"],
                "checks": [{"context": "...", "app_id": 15368}]
            }
        }

    Note that it is **partial**: it carries the required-check list and nothing
    about reviews, ``enforce_admins``, force-pushes or deletions, so it can settle
    question 2 of the six and none of the others. Note also that ``protected`` is
    true for a branch governed only by a *ruleset*, in which case this nested
    object reports ``"enabled": false`` — the two fields answer different
    questions and ``_summary_protection`` only accepts the object when it is
    actually informative.
    """
    _validate(repo, branch)
    payload, _status = _api_get(f"repos/{repo}/branches/{branch}", token)
    if not isinstance(payload, dict) or "protected" not in payload:
        return None, None
    protection = payload.get("protection")
    return bool(payload.get("protected")), (
        protection if isinstance(protection, dict) else None
    )


def _summary_protection(summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The summary's ``protection`` object, but only when it says something.

    ``{"enabled": false, "required_status_checks": {"contexts": [], ...}}`` is
    what a branch governed solely by a ruleset returns, and reading that as
    "classic protection with no required checks" would invent a finding. So the
    object counts only when it is enabled or names at least one context.
    """
    if not isinstance(summary, dict):
        return None
    if summary.get("enabled"):
        return summary
    return summary if _summary_required_check_names(summary) else None


def _summary_required_check_names(summary: Dict[str, Any]) -> List[str]:
    """Required contexts declared by the branch summary's ``protection`` object."""
    block = summary.get("required_status_checks")
    if not isinstance(block, dict):
        return []
    names = {str(name) for name in block.get("contexts") or []}
    for check in block.get("checks") or []:
        if isinstance(check, dict) and check.get("context"):
            names.add(str(check["context"]))
    return sorted(names)


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
    protected_flag, summary = fetch_branch_summary(repo, branch, token)
    summary_protection = _summary_protection(summary)
    rules, rules_status = fetch_branch_rules(repo, branch, token)
    branch_rules = branch_scoped_rules(rules or [])

    if classic is not None:
        state = PROTECTION_CLASSIC
    elif branch_rules:
        state = PROTECTION_RULESET
    elif protected_flag is True and summary_protection is not None:
        # Protected, and the branch summary carries enough to check the
        # required-check list even though the classic endpoint 404s. Reporting
        # `unverifiable` here would be giving up on data already fetched.
        state = PROTECTION_SUMMARY
    elif protected_flag is False:
        # Verified absent: `branches/{branch}` is readable with pull access and
        # says the branch is not protected, and no ruleset rule governs it.
        state = PROTECTION_VERIFIED_ABSENT
    else:
        # Either the summary read failed outright, or it says protected while
        # carrying no usable detail and no ruleset rule explains it.
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
        summary_protection=summary_protection,
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


def _rule_params(rules: List[Dict[str, Any]], rule_type: str) -> Dict[str, Any]:
    """The ``parameters`` object of the first rule of ``rule_type``, or ``{}``."""
    for rule in rules:
        if rule.get("type") != rule_type:
            continue
        params = rule.get("parameters")
        return params if isinstance(params, dict) else {}
    return {}


def _evaluate_ruleset(
    state: ProtectionState, expected: List[str], branch: str
) -> List[Finding]:
    """Assert against ruleset rules when classic protection is not visible.

    Asks the same six questions the classic path asks, keyed distinctly so a
    report says which mechanism it read. Without this, a repository governed
    entirely by a ruleset would be reported as "protected" and never checked.

    Asking only whether each *rule type* is present is not enough, and used to be
    all this did. A ``pull_request`` rule with
    ``required_approving_review_count: 0`` and
    ``dismiss_stale_reviews_on_push: false`` is a rule that requires no review and
    carries stale approvals forward, and it produced no finding at all — so a
    repository migrating from classic protection to a ruleset and setting
    approvals to zero got a clean bill of health from a tool whose whole purpose
    is to catch that. The rule *parameters* are therefore read too, and each maps
    to the classic finding it mirrors:

    ================================  =========================
    ruleset parameter                 classic equivalent
    ================================  =========================
    required_approving_review_count   no_approving_review
    dismiss_stale_reviews_on_push     stale_reviews_kept
    strict_required_status_checks_..  not_strict
    ================================  =========================

    One classic assertion has no ruleset counterpart here: bypass actors are a
    property of the *ruleset*, not of the rules that
    ``GET /repos/{slug}/rules/branches/{branch}`` returns, so a bypass list
    equivalent to ``bypass_pull_request_allowances`` cannot be seen from this
    read. Nothing is asserted about it rather than asserting it vacuously.
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
        # `missing` is computed against every mechanism that gates, so a check
        # required elsewhere is not falsely reported as unrequired. `stale` stays
        # scoped to the ruleset's own list, because its message names the ruleset
        # as the thing to edit and must not blame it for somebody else's entry.
        live = state.live_required_checks()
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
        stale = [name for name in state.ruleset_required_checks if name not in expected]
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
        # The ruleset spelling of classic protection's `strict` flag. Live shape,
        # recorded from home-assistant/core's `dev` branch: the flag sits in the
        # `required_status_checks` rule's parameters alongside the context list.
        checks_params = _rule_params(state.branch_rules, "required_status_checks")
        if not checks_params.get("strict_required_status_checks_policy"):
            findings.append(
                Finding(
                    key="ruleset_not_strict",
                    message=(
                        f"the ruleset on {branch!r} does not set "
                        f"strict_required_status_checks_policy, so a pull request "
                        f"can pass against a stale base and merge a combination "
                        f"neither side tested"
                    ),
                    remedy=(
                        "enable 'require branches to be up to date before merging' "
                        "on the ruleset's required status checks"
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
    else:
        # Present-but-toothless is the case that used to slip through: the rule
        # exists, so the type check above passes, while its parameters say no
        # approval is needed and stale approvals carry over.
        review_params = _rule_params(state.branch_rules, "pull_request")
        count = review_params.get("required_approving_review_count") or 0
        if count < 1:
            findings.append(
                Finding(
                    key="ruleset_no_approving_review",
                    message=(
                        f"the ruleset on {branch!r} has a pull_request rule but its "
                        f"required_approving_review_count is {count}, so a pull "
                        f"request can be merged with no approval at all"
                    ),
                    remedy=(
                        "set the ruleset's required_approving_review_count to 1 or more"
                    ),
                )
            )
        if not review_params.get("dismiss_stale_reviews_on_push"):
            findings.append(
                Finding(
                    key="ruleset_stale_reviews_kept",
                    message=(
                        f"the ruleset on {branch!r} does not set "
                        f"dismiss_stale_reviews_on_push, so an approval of reviewed "
                        f"code carries over to code nobody reviewed"
                    ),
                    remedy=(
                        "enable dismiss_stale_reviews_on_push on the ruleset's "
                        "pull_request rule"
                    ),
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


def _evaluate_branch_summary(
    state: ProtectionState, expected: List[str], branch: str, derived: List[str]
) -> List[Finding]:
    """Assert what the branch *summary* can settle, and say what it cannot.

    This is the non-admin path on a branch that *is* protected. The summary's
    nested ``protection``
    object carries the required-check list and nothing else, so exactly one of the
    six questions is answerable here — and answering it is the point, because the
    alternative this replaces was reporting ``protection_unverifiable`` while
    holding the comparison data.

    The remaining five settings are reported as unread rather than as satisfied.
    That keeps the exit code honest: a run that cannot see ``enforce_admins``
    must not print a clean bill of health, because ``enforce_admins: false``
    exempts exactly the people most likely to be merging.
    """
    findings = _compare_required_checks(state.live_required_checks(), expected, derived)
    findings.append(
        Finding(
            key="protection_detail_unreadable",
            message=(
                f"{branch!r} IS protected and its required-check list was read "
                f"from GET .../branches/{branch} (classic endpoint returned "
                f"{state.classic_status}; this token has admin="
                f"{state.admin_permission}), but that response carries only the "
                f"required-check list. Whether an approving review is required, "
                f"whether stale approvals are dismissed, whether force-pushes and "
                f"deletion are blocked, and whether enforce_admins is on could NOT "
                f"be read at this permission level — they are unverified, not "
                f"verified-good"
            ),
            remedy=(
                "re-run with a token that has administration:read to check the "
                "remaining five settings"
            ),
        )
    )
    return findings


def _compare_required_checks(
    live: List[str], expected: List[str], derived: Optional[List[str]] = None
) -> List[Finding]:
    """The required-check list comparison, shared by the classic and summary paths.

    ``live`` is the union of every mechanism that gates, so a check required by
    one of them is never reported as unrequired. ``_evaluate_ruleset`` keeps its
    own copy of this comparison because its messages name the ruleset as the thing
    to edit and its `stale` list is deliberately scoped to the ruleset's own
    entries.
    """
    findings: List[Finding] = []
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
    # A required name this repo *does* produce, but only conditionally, is a
    # different defect from a name nothing produces at all — and advising an
    # administrator to "remove" it would be wrong. Split them.
    known = set(derived if derived is not None else expected)
    conditional = [name for name in stale if name in known]
    unknown = [name for name in stale if name not in known]
    if conditional:
        findings.append(
            Finding(
                key="required_but_not_always_reported",
                message=(
                    "these checks are required, and this repo does produce them, "
                    "but not on every pull request (path- or branch-filtered "
                    "workflow, matrix leg, a job behind an `if:`, or a conditional "
                    "action-created check run), so they can sit pending and block "
                    f"a merge indefinitely: {', '.join(conditional)}"
                ),
                remedy=(
                    "un-require them, or make them unconditional — do NOT remove "
                    "the job, the check itself is wanted"
                ),
            )
        )
    if unknown:
        findings.append(
            Finding(
                key="unknown_required_checks",
                message=(
                    "these checks are required but nothing in .github/workflows/ "
                    "produces them, so they may never report and could block "
                    f"merges indefinitely: {', '.join(unknown)}"
                ),
                remedy=(
                    "remove them, or rename the workflow job back to match "
                    "(a renamed job silently stops being gated)"
                ),
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
                    f"direct push to {branch} runs no GATE workflow — the lint, "
                    f"test and security workflows are pull_request-only. (The two "
                    f"that do trigger on push are the path-filtered docs and "
                    f"dependency-manifest publishers, which are not gates.) "
                    f"Expected required checks, derived from .github/workflows/: "
                    + _expected_list(expected)
                ),
                remedy=(
                    "Not actionable from this repository, and recorded as an "
                    "accepted residual in closed issue #933: enabling classic "
                    "protection needs repository ADMIN, which no contributor and "
                    "no CI token here has. A branch ruleset published by an "
                    "organization or enterprise owner is a second route and needs "
                    "no repository admin; this check reads both mechanisms."
                ),
            )
        ]

    if state.state == PROTECTION_SUMMARY:
        return _evaluate_branch_summary(
            state, expected, branch, derived if derived is not None else expected
        )

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
        # All three mechanisms gate simultaneously, so the union of what each one
        # requires is what actually blocks a merge. `state.live_required_checks()`
        # folds in the branch summary too, which is the source a non-admin run has
        # when the classic read 404s.
        # Unioned with this payload explicitly rather than relying on `state`
        # carrying it: `evaluate` accepts `protection` and `state` as separate
        # arguments, so a caller can pass a payload the state does not hold, and
        # falling back to one *or* the other would then drop the other's names.
        live = sorted(
            set(state.live_required_checks()) | set(_required_check_names(protection))
        )
        findings.extend(_compare_required_checks(live, expected, derived))
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
    PROTECTION_SUMMARY: (
        "PROTECTED (per the branch summary; classic settings need administration:read)"
    ),
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
            + (
                ", nested protection.required_status_checks: "
                + (", ".join(state.summary_required_checks) or "(none)")
                if state.summary_protection
                else ""
            )
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
                "  ruleset required checks: " + ", ".join(state.ruleset_required_checks)
            )

    # Stated in the report, not just in a source comment: a reader who sees a
    # clean result on a protected branch would otherwise reasonably conclude the
    # push allowlist had been checked and found adequate. It is deliberately not
    # asserted on, because an empty one is correct for a repository taking outside
    # contributions, so its absence is not a finding — but "not asserted" and
    # "asserted and fine" must not look the same.
    print(
        "\nNot asserted: `restrictions` (the push allowlist). An empty one is the\n"
        "normal, correct configuration for an open-source repo taking outside\n"
        "contributions — requiring a pull request already covers it — so reporting\n"
        "its absence would be a false finding. Raw value is in --json under\n"
        "`classic_protection.restrictions`."
    )

    # One run answers for one branch. Both long-lived branches matter here and
    # only one of them is this script's default, which is how `main` — the
    # repository's default branch, and the one releases are cut from — went
    # unexamined while `develop` was being measured.
    unread = [name for name in SHARED_BRANCHES if name != branch]
    if unread:
        print(
            "\nNot read by this run: "
            + ", ".join(unread)
            + ". Each long-lived branch carries its own\nprotection setting, so re-run "
            "with --branch <name> for the rest."
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
        "These findings are NOT actionable from inside this repository, and that\n"
        "is a known, accepted residual rather than an open task. Enabling classic\n"
        "branch protection needs repository ADMIN, which no contributor and no CI\n"
        "token here has; the decision is recorded in closed issue #933:\n"
        "  https://github.com/aws-solutions-library-samples/"
        "accelerated-intelligent-document-processing-on-aws/issues/933\n"
        "A branch ruleset published by an organization or enterprise owner reaches\n"
        "the same outcome and needs no repository admin — this check reads both\n"
        "mechanisms, so either would show up here.\n"
        "Nothing in this repository can substitute: enforcement is server-side, so\n"
        "a merge taken through GitHub's Merge button runs no code from this tree.\n"
        "This check is opt-in and gates nothing. The condition for making it a\n"
        "required, blocking check is one of those two settings actually changing."
    )


def _skip(reason: str, detail: str, fail_on_skip: bool) -> int:
    print(f"\nBranch protection check: SKIPPED — {reason}")
    print(f"  {detail}")
    print(
        "\n  This check needs network access and a GitHub token. `pull` access is\n"
        "  enough to reach a verified answer, and enough to compare the required-\n"
        "  check list once protection exists (GET .../branches/<branch> carries a\n"
        "  nested protection.required_status_checks object at that scope).\n"
        "  administration:read is what the other five assertions need; without it\n"
        "  they are reported as unread, not as satisfied.\n"
        "  It is opt-in by design and gates nothing, so a skip is not a failure.\n"
        "  Pass --fail-on-skip to make an unrunnable check an error instead — do\n"
        "  that once protection has actually been enabled and this is a blocking\n"
        "  gate, where a silent pass from a missing token is the worst answer of\n"
        "  the three."
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
        contexts = discover_check_contexts(WORKFLOWS_DIR, args.branch)
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
                    # The `protection` object nested in GET .../branches/{branch}.
                    # Partial by design — it carries the required-check list and
                    # nothing about reviews, enforce_admins, force-push or
                    # deletion, which is why the summary path still reports those
                    # five as unread rather than as satisfied.
                    "branch_summary_protection": state.summary_protection,
                    "classic_protection": state.classic,
                    "branch_scoped_ruleset_rule_types": sorted(
                        {str(r.get("type")) for r in state.branch_rules}
                    ),
                    "expected_required_checks": expected,
                    "live_required_checks": state.live_required_checks(),
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
                    # The issue is cited as a DECISION RECORD, not as open work:
                    # it is closed as not-planned because enabling protection is
                    # out of this repository's reach. What would make this check
                    # blocking is a repository *setting* changing, which is a
                    # different thing from an issue state — so that condition is
                    # stated here rather than left implied by the issue number.
                    "decision_record": {
                        "issue": 933,
                        "state": "closed",
                        "state_reason": "not_planned",
                        "url": (
                            "https://github.com/aws-solutions-library-samples/"
                            "accelerated-intelligent-document-processing-on-aws"
                            "/issues/933"
                        ),
                        "blocking_gate_trigger": (
                            "somebody with repository admin enables branch "
                            "protection, or an organization or enterprise owner "
                            "publishes a branch ruleset targeting this branch"
                        ),
                        # Deliberately a structural statement rather than an
                        # inventory of guards. A consumer reads these values as
                        # fact, so naming an artifact here would assert that it
                        # exists; this says why no artifact in the tree could
                        # close the gap whatever is added.
                        "enforcement_is_server_side_only": (
                            "a merge taken through GitHub's Merge button runs no "
                            "code from this repository, so nothing in the tree can "
                            "make a red check block one"
                        ),
                    },
                    "shared_branches": list(SHARED_BRANCHES),
                    "branches_not_read_by_this_run": [
                        name for name in SHARED_BRANCHES if name != args.branch
                    ],
                },
                indent=2,
            )
        )
    else:
        print_report(args.repo, args.branch, contexts, findings, state)

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
