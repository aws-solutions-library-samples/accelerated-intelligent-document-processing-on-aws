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

1. the branch is protected at all;
2. the required status checks include **every** check context the workflows
   actually produce on a pull request;
3. stale approving reviews are dismissed on a new push;
4. force-pushes and branch deletion are blocked;
5. at least one approving review is required.

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
``test_ci_gate_parity.py``'s ``SHARED_GATES`` are *steps* inside three jobs, so
requiring them means requiring three contexts, not eight. This script prints
which gate commands each context covers so that correspondence is visible.

Two workflows are deliberately **excluded** from the required list:
``build-docs.yml`` and ``generate-dep-manifest.yml`` filter their
``pull_request`` trigger on ``paths:``. A path-filtered workflow does not run at
all on a PR that touches no matching path, so its check is never reported —
and a required check that is never reported sits **pending forever**, blocking
every merge. Requiring one of those would wedge the repository. They are listed
as advisory-only, with that reason.

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

Token: ``GITHUB_TOKEN`` or ``GH_TOKEN``, else ``gh auth token``. The token needs
read access to repository administration to see protection settings. It is only
ever placed in an Authorization header — never printed, logged, or written.

Exit codes:
    0 - protection is configured as required (or the check was skipped cleanly)
    1 - findings: not protected, or the required-check list has drifted
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


@dataclass
class Finding:
    """One thing that is not configured as it should be."""

    key: str
    message: str
    remedy: str


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


def fetch_protection(
    repo: str, branch: str, token: str
) -> Tuple[Optional[Dict[str, Any]], int]:
    """GET the branch's protection settings. Returns (payload_or_None, status).

    A 404 is the *expected* answer for an unprotected branch, not an error.
    """
    if not _SLUG_RE.match(repo):
        raise ValueError(f"invalid repo slug: {repo!r} (expected owner/name)")
    if not _BRANCH_RE.match(branch):
        raise ValueError(f"invalid branch name: {branch!r}")

    url = f"{API_ROOT}/repos/{repo}/branches/{branch}/protection"
    request = urllib.request.Request(  # nosec B310 - constant https:// base; repo/branch validated above
        url,
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
            # 404 = unprotected OR the token cannot read admin settings; both
            # are reported as "no protection visible", never as a pass.
            return None, exc.code
        raise
    except urllib.error.URLError as exc:
        raise NetworkUnavailable(str(exc.reason)) from exc


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


def evaluate(
    protection: Optional[Dict[str, Any]],
    expected: List[str],
    branch: str,
    status: int = 0,
) -> List[Finding]:
    """Compare live protection against what the workflows imply it should be."""
    if protection is None:
        return [
            Finding(
                key="not_protected",
                message=(
                    f"branch {branch!r} has no visible branch protection "
                    f"(GET .../branches/{branch}/protection returned {status}). "
                    f"Every CI gate is therefore advisory: a pull request can be "
                    f"merged with all checks red, and a direct push to {branch} "
                    f"runs no GitHub workflow at all. Expected required checks, "
                    f"derived from .github/workflows/: "
                    + (", ".join(expected) if expected else "(none derived!)")
                ),
                remedy=(
                    "Needs repository admin — tracked by issue #933. If the token "
                    "in use lacks administration:read, a protected branch also "
                    "reports 404, so confirm the token scope before concluding."
                ),
            )
        ]

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
        live = _required_check_names(protection)
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
        if stale:
            findings.append(
                Finding(
                    key="unknown_required_checks",
                    message=(
                        "these checks are required but no workflow job produces "
                        f"them, so they may never report and could block merges "
                        f"indefinitely: {', '.join(stale)}"
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


def print_report(
    repo: str,
    branch: str,
    contexts: List[CheckContext],
    findings: List[Finding],
) -> None:
    print(f"\nBranch protection check: {repo} @ {branch}")
    print("=" * 78)

    eligible = [c for c in contexts if c.required_eligible]
    advisory = [c for c in contexts if not c.required_eligible]

    print(
        f"\nDerived from .github/workflows/ ({len(contexts)} job(s) in "
        f"{len({c.workflow for c in contexts})} workflow file(s)).\n"
    )
    print(f"Should be REQUIRED status checks ({len(eligible)}):")
    for ctx in eligible:
        print(f"  • {ctx.context}")
        print(f"      from {ctx.workflow} :: job '{ctx.job_id}'")
        if ctx.gates:
            print(f"      covers: {', '.join(ctx.gates)}")
    if advisory:
        print(f"\nMust stay advisory ({len(advisory)}):")
        for ctx in advisory:
            print(f"  • {ctx.context}  (from {ctx.workflow} :: job '{ctx.job_id}')")
            print(f"      {ctx.reason}")

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
        "\n  This check needs network access and a GitHub token with "
        "administration:read.\n"
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
        protection, status = fetch_protection(args.repo, args.branch, token)
    except NetworkUnavailable as exc:
        return _skip("the GitHub API is unreachable", f"{exc}", args.fail_on_skip)
    except urllib.error.HTTPError as exc:
        print(f"❌ GitHub API error {exc.code} for {args.repo}@{args.branch}")
        return 3
    except ValueError as exc:
        print(f"❌ {exc}")
        return 3

    findings = evaluate(protection, expected, args.branch, status)

    if args.json:
        print(
            json.dumps(
                {
                    "repo": args.repo,
                    "branch": args.branch,
                    "protected": protection is not None,
                    "expected_required_checks": expected,
                    "live_required_checks": (
                        _required_check_names(protection) if protection else []
                    ),
                    "advisory_only": [
                        {"context": c.context, "reason": c.reason}
                        for c in contexts
                        if not c.required_eligible
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
        print_report(args.repo, args.branch, contexts, findings)

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
