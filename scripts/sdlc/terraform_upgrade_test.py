#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deploy one release of the Terraform module, seed it, upgrade it in place to
another release, prove nothing was lost, and tear it down.

WHAT IT PROVES
--------------
This is the Terraform counterpart of the CloudFormation in-place upgrade test
(``.claude/skills/test-upgrade.md``, release-validation tier E), and it answers the
one question a fresh deploy cannot: a customer who already runs release X and
points the module at release Y keeps their documents, configuration, users and
passwords, and the deployment still processes a document afterwards.

``terraform/translator/tests/test_upgrade_safety.py`` already checks the mechanisms
that make that work -- ``moved`` blocks for every renamed or relocated address, the
stateful address list, and the create-only properties a custom resource feeds -- but
it checks them against the translation, offline. Only a live run can show that the
plan Terraform actually produces against a deployed release replaces nothing, that
the buckets, tables, user pool and key come through the apply with the same physical
identities, and that the data in them is still there.

A refusal counts as a failure. ``idp-cli deploy --terraform`` refuses a plan that
would delete or replace a resource holding data or one CloudFormation retains, and
this run never passes ``--terraform-allow-replace-stateful``: if the upgrade is
refused, the phase fails and names the addresses, because that is exactly what a
customer would hit.

WHAT IT DOES NOT COVER
----------------------
* Upgrading a deployment made by the separate ``awslabs/genai-idp-terraform``
  repository: that is a migration, not an upgrade
  (``scripts/sdlc/terraform_migration_test.py``).
* The headless and GovCloud module variants. Both URLs may name a variant and the
  run records which, but the seeding and verification here assume the standard
  module's web API and Cognito pool, so a headless pair is refused.
* Skipping more than one release, a deployment older than this run, a hand-written
  root configuration, and a remote state backend. The deployment is the one
  ``idp-cli deploy --terraform`` writes, with its state in the work directory.
* The web UI, the knowledge base re-sync and the alerts subscription. An SRP
  sign-in proves the password; the UI is the UX tests' subject.

SAFETY
------
One deployment, one name. Everything the teardown deletes is either named in this
run's own Terraform state (recorded before the destroy) or starts with this run's
exact name prefix (``owned_prefixes``; never a substring, never case-folded).
Preflight runs the inventory the sweep and the leftover check use and refuses to
start when it finds anything, so the sweep cannot reach a deployment this run did
not create. No teardown runs unless this invocation's preflight passed (the name is
usable, the same as the work directory's run, unclaimed, right account) and the run
created something; ``--teardown-only`` checks the same. Passwords live only in
``<work-dir>/secrets.json``; every file the run writes, Terraform's state included,
is owner-only (umask 077).

USAGE
-----
    make stacktest-terraform-upgrade STACK_NAME=tfup1 \\
        ADMIN_EMAIL=you@example.com \\
        FROM_MODULE_URL=https://.../idp-terraform-<old>.zip \\
        TO_MODULE_URL=https://.../idp-terraform-<new>.zip

    python3 scripts/sdlc/terraform_upgrade_test.py --stack-name tfup1 \\
        --admin-email you@example.com \\
        --from-module-url <zip url> --to-module-url <zip url> \\
        --work-dir ~/scratch/tfup1 [--stop-after seed] [--resume] [--keep]

Budget about two and a half hours: the first apply is roughly half an hour, the
seed documents a few minutes each, the upgrade apply between ten minutes and half
an hour depending on how much the release changed, and the destroy about twenty
minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import terraform_migration_test as tmt  # noqa: E402

PhaseFailed = tmt.PhaseFailed

#: Where the module's resources sit in the root configuration ``idp-cli`` writes.
#: Spelled here rather than imported: ``idp_sdk._core`` is off limits outside the
#: SDK (ruff TID251), and this test reads the module's own files the way a customer
#: following docs/terraform.md would.
MODULE_PREFIX = "module.idp."
MANIFEST_NAME = "idp-terraform.json"

PHASES = (
    "preflight",
    "deploy-from",
    "seed",
    "snapshot-before",
    "upgrade",
    "verify",
    "destroy",
    "sweep",
    "leftover-check",
)

#: Phases after which the deployment's own resources are gone.
TEARDOWN_PHASES = ("destroy", "sweep", "leftover-check")

# There is deliberately no "resumed run may not continue past a failed phase" gate
# here, unlike the migration test's: every phase after ``verify`` is a teardown
# phase, and tearing down after a failed verification is the wanted behaviour, so
# such a gate could never fire. A control that cannot fire reads as protection that
# is not there.

#: The resource kinds whose physical identity an upgrade must preserve: the ones
#: that hold data. ``physical_names`` also reports roles and log groups, and those
#: are deliberately left out -- a release may rename either, and CloudFormation
#: replaces them on an update too, so a changed name there is not a data loss.
IDENTITY_KINDS = ("buckets", "tables", "user_pools", "kms_keys", "bda_projects")

#: Buckets whose objects are S3 server access logs rather than anything the
#: deployment stores. They are left out of the before/after snapshot for a timing
#: reason, not a trust one: S3 delivers access logs on its own schedule, minutes to
#: hours after the requests, so an object can land during the settle loop (which
#: waits for two identical snapshots) or between the two snapshots, and neither is
#: the upgrade's doing. The identity check still covers them -- a replaced logging
#: bucket is reported like any other -- and their contents are not customer data.
ACCESS_LOG_BUCKET_RESOURCES = ("logging_bucket",)

#: How the Terraform resource name of a table maps to the kind
#: ``tmt.compare_items`` knows, so a row an upgrade is meant to rewrite is not
#: reported as lost data.
TABLE_KINDS = {
    "configuration_table": "configuration",
    "tracking_table": "tracking_table",
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
#: ``idp-terraform-0.6.15.zip`` and ``idp-terraform-headless-0.6.15.zip``.
_MODULE_URL_RE = re.compile(
    r"idp-terraform-(?:(?P<variant>headless|govcloud)-)?(?P<version>[^/]+?)\.zip$"
)
_PLAN_LINE_RE = re.compile(
    r"^Plan: (?P<add>\d+) to add, (?P<change>\d+) to change, (?P<destroy>\d+) to destroy",
    re.MULTILINE,
)
_REFUSED_RE = re.compile(r"^\s+- (?P<address>module\.idp\.\S+)", re.MULTILINE)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def module_coordinates(url: str) -> Tuple[Optional[str], Optional[str]]:
    """``(version, variant)`` read from a module zip URL; ``(None, None)`` when the
    URL does not name one. The variant is None for the standard module."""
    match = _MODULE_URL_RE.search(url or "")
    if not match:
        return None, None
    return match.group("version"), match.group("variant")


def validate_stack_name(name: str) -> List[str]:
    """Reasons the deployment name cannot be used; empty when it can.

    It becomes ``stack_name``, which is part of every resource name the module
    creates, and the leftover sweep matches by that name. 25 characters is the
    module's own limit for a name it prefixes IAM roles with.
    """
    problems = []
    if not _NAME_RE.match(name or ""):
        problems.append(
            f"the stack name {name!r} must be lowercase letters, digits and "
            "hyphens, starting with a letter"
        )
    if len(name or "") > 25:
        problems.append(f"the stack name {name!r} is longer than 25 characters")
    return problems


def module_pair_problems(from_url: str, to_url: str) -> List[str]:
    """Why this pair of module URLs cannot be an upgrade."""
    problems = []
    if from_url == to_url:
        problems.append("the two module URLs are the same, so there is no upgrade")
    from_version, from_variant = module_coordinates(from_url)
    to_version, to_variant = module_coordinates(to_url)
    for label, url, version in (
        ("--from-module-url", from_url, from_version),
        ("--to-module-url", to_url, to_version),
    ):
        if version is None:
            problems.append(
                f"{label} {url!r} does not end in idp-terraform[-<variant>]-<version>.zip"
            )
    if from_variant != to_variant:
        problems.append(
            f"the modules are different variants ({from_variant} -> {to_variant}); "
            "an upgrade stays on one"
        )
    elif from_variant is not None:
        # Seeding and verification here go through the web API and the Cognito
        # pool, and the headless module has neither; the GovCloud module has both
        # but serves them through API Gateway, which this run has not been tried
        # against.
        problems.append(
            f"the {from_variant} variant is not covered by this test; it seeds and "
            "verifies through the standard module's API and user pool"
        )
    return problems


def stored_name_problems(ctx: Dict[str, Any], stack_name: str) -> List[str]:
    """Why this invocation's name differs from the one the work directory recorded.
    A teardown deletes by that name, so a mismatch must refuse rather than
    overwrite it."""
    stored = ctx.get("stackName")
    if stored and stored != stack_name:
        return [
            f"stackName is {stack_name!r} here but {stored!r} in this work "
            "directory's run"
        ]
    return []


def plan_summary(text: str) -> Optional[Dict[str, int]]:
    """The last ``Plan: N to add, M to change, K to destroy`` of a deploy log.

    The last one, not the first: ``idp-cli deploy --terraform`` plans once, and a
    log that holds an earlier plan too (a resumed work directory's log is appended
    to) is reporting this invocation's plan at the end.
    """
    matches = list(_PLAN_LINE_RE.finditer(text or ""))
    if not matches:
        return None
    last = matches[-1]
    return {
        "add": int(last.group("add")),
        "change": int(last.group("change")),
        "destroy": int(last.group("destroy")),
    }


def refused_addresses(text: str) -> List[str]:
    """The addresses the deploy guard listed when it refused a plan, so the phase's
    failure names them instead of only pointing at a log."""
    return sorted({m.group("address") for m in _REFUSED_RE.finditer(text or "")})


def read_manifest(directory: Path) -> Dict[str, Any]:
    """The ``idp-terraform.json`` of the module ``terraform init`` installed in
    ``directory``; empty when there is none to read."""
    modules_json = directory / ".terraform" / "modules" / "modules.json"
    if not modules_json.is_file():
        return {}
    try:
        entries = json.loads(modules_json.read_text()).get("Modules", [])
    except ValueError:
        return {}
    for mod in entries:
        if mod.get("Key") != "idp":
            continue
        path = directory / mod.get("Dir", "") / MANIFEST_NAME
        if path.is_file():
            try:
                return json.loads(path.read_text())
            except ValueError:
                return {}
    return {}


def identity_losses(
    before: Dict[str, List[str]], after: Dict[str, List[str]]
) -> List[str]:
    """Every data-holding resource present before the upgrade and gone after it.

    Matching is by physical name across the whole kind rather than by Terraform
    address, so a resource a ``moved`` block relocated is still found: its address
    changed and its identity did not. An address that gained a resource is not a
    finding; an identity that disappeared is a replacement, which loses its data.
    """
    losses = []
    for kind in IDENTITY_KINDS:
        gone = sorted(set(before.get(kind) or []) - set(after.get(kind) or []))
        losses += [
            f"{kind[:-1] if kind.endswith('s') else kind} {name}" for name in gone
        ]
    return losses


def table_kinds(state: Dict[str, Any]) -> Dict[str, str]:
    """Physical table name -> the kind ``tmt.compare_items`` understands, read from
    the deployment's own state rather than from a migration plan."""
    kinds = {}
    for inst in tmt.state_instances(state):
        if inst["type"] not in ("aws_dynamodb_table", "awscc_dynamodb_table"):
            continue
        name = inst["attributes"].get("name") or inst["attributes"].get("table_name")
        if name:
            kinds[name] = TABLE_KINDS.get(inst["name"], "unknown")
    return kinds


def summarise(result: Dict[str, Any]) -> str:
    """The run's verdict, findings first: a chat post or a job log keeps the start
    of a summary and cuts the rest."""
    lines = [f"Terraform upgrade test: {result['status']}"]
    lines.append(
        f"  deployment {result['stackName']} in {result['region']}, "
        f"{result.get('fromVersion') or '?'} -> {result.get('toVersion') or '?'}"
    )
    for url in ("fromModuleUrl", "toModuleUrl"):
        lines.append(f"  {url}: {result.get(url)}")
    if result.get("findings"):
        lines.append("\nFindings:")
        lines += [f"  ✗ {f}" for f in result["findings"]]
    if result.get("previousFindings"):
        lines.append(
            f"\n(an earlier attempt in this work directory reported "
            f"{len(result['previousFindings'])} finding(s))"
        )
    if result.get("upgradePlan"):
        p = result["upgradePlan"]
        lines.append(
            f"\nUpgrade plan: {p['add']} to add, {p['change']} to change, "
            f"{p['destroy']} to destroy"
        )
    if result.get("leftovers"):
        lines.append("\nLeftovers after teardown: " + ", ".join(result["leftovers"]))
    elif result.get("leftovers") == []:
        lines.append("\nLeftovers after teardown: 0")
    lines.append("\nPhases:")
    for p in result.get("phases", []):
        took = f"{p.get('seconds', 0) / 60:6.1f} min"
        lines.append(
            f"  {p['status']:<8} {took}  {p['phase']}"
            + (f"  - {p['detail']}" if p.get("detail") else "")
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    stack_name: str
    admin_email: str
    region: str
    work_dir: Path
    terraform_bin: str
    from_module_url: str
    to_module_url: str
    parameters: Optional[str] = None
    assume_role_arn: Optional[str] = None
    keep: bool = False
    resume: bool = False
    stop_after: Optional[str] = None
    teardown_only: bool = False
    expected_account: Optional[str] = None
    #: One deployment at a time, so it needs roughly half of what the migration
    #: test's two need.
    min_role_headroom: int = 200
    document_timeout: int = 2400
    #: Seconds from the start of this invocation after which no forward phase
    #: starts, so that the teardown fits inside an outer time limit (CI's build).
    deadline_seconds: Optional[int] = None
    #: Inherited plumbing reads this; this test keeps its state in its work
    #: directory, so there is no mirror and no flag for one.
    state_mirror: Optional[str] = field(default=None, init=False)

    # The inherited sweep and leftover check take the run's names from these two.
    # One deployment owns one name, so both are it.
    @property
    def old_name(self) -> str:
        return self.stack_name

    @property
    def new_name(self) -> str:
        return self.stack_name


class Run(tmt.Run):
    """One upgrade run.

    It inherits the migration test's plumbing -- the work directory and its context
    file, the subprocess wrappers for ``terraform`` and ``idp-cli``, the REST
    dispatcher client, the SRP sign-in, the tracking-table poll, the bucket, table,
    user and group snapshot, and the leftover sweep -- and adds the phases of an
    upgrade. There is deliberately one implementation of "make a deployment, seed
    it, snapshot it, sweep up after it" for both live Terraform tests.
    """

    @property
    def dir(self) -> Path:
        """The deployment directory ``idp-cli deploy --terraform`` writes and keeps
        the state in."""
        return self.work / "deployment" / self.s.stack_name

    # -- hooks the inherited plumbing calls ------------------------------------

    def snapshot_pool(self) -> str:
        return self.ctx.get("handles", {}).get("pool") or ""

    def snapshot_targets(self) -> Dict[str, List[str]]:
        """Every bucket and table of this deployment, read from its state. Recorded
        on the first call so the comparison after the upgrade covers exactly what
        was snapshotted before it, even if the upgrade added a bucket."""
        if "snapshotTargets" not in self.ctx:
            state = self.state()
            names = tmt.physical_names(state)
            logs = {
                inst["attributes"].get("bucket")
                for inst in tmt.state_instances(state)
                if inst["type"] == "aws_s3_bucket"
                and inst["name"] in ACCESS_LOG_BUCKET_RESOURCES
            }
            self.ctx["snapshotTargets"] = {
                "buckets": [b for b in (names.get("buckets") or []) if b not in logs],
                "tables": names.get("tables") or [],
            }
        return self.ctx["snapshotTargets"]

    def identity_problems(self) -> List[str]:
        """Why this invocation must not act on this work directory: the name is
        unusable, differs from the one the work directory's run used, or the
        credentials are for another account than it ran in."""
        problems = validate_stack_name(self.s.stack_name)
        problems += stored_name_problems(self.ctx, self.s.stack_name)
        account = self.boto("sts").get_caller_identity()["Account"]
        if self.ctx.get("account") and self.ctx["account"] != account:
            problems.append(
                f"credentials are for account {account}, but this work directory's "
                f"run was in {self.ctx['account']}"
            )
        if self.s.expected_account and account != self.s.expected_account:
            problems.append(
                f"credentials are for account {account}, not {self.s.expected_account}"
            )
        self._account = account
        return problems

    # -- plumbing of its own ---------------------------------------------------

    def state(self) -> Dict[str, Any]:
        if not (self.dir / "main.tf").is_file():
            return {}
        try:
            return json.loads(self.terraform_capture(self.dir, "state", "pull") or "{}")
        except PhaseFailed:
            return {}

    def record_names(self) -> Dict[str, List[str]]:
        """Merge this deployment's physical names into the context, so a teardown
        after a failed destroy still knows what to sweep."""
        self.ctx["names"] = tmt.merge_names(
            self.ctx.get("names") or {}, tmt.physical_names(self.state())
        )
        self.save()
        return self.ctx["names"]

    def bucket(self, resource_name: str) -> str:
        for inst in tmt.state_instances(self.state()):
            if inst["type"] == "aws_s3_bucket" and inst["name"] == resource_name:
                return inst["attributes"]["bucket"]
        raise PhaseFailed(f"the state holds no aws_s3_bucket.{resource_name}")

    def deploy(self, module_url: str, log: str, first: bool) -> Tuple[int, str]:
        """``idp-cli deploy --terraform`` against this deployment's directory.

        Never with ``--terraform-allow-replace-stateful``: a plan the guard refuses
        is the failure this test exists to find.
        """
        args = [
            "deploy",
            "--terraform",
            "--stack-name",
            self.s.stack_name,
            "--region",
            self.s.region,
            "--terraform-module-url",
            module_url,
            "--terraform-dir",
            str(self.dir),
            "--terraform-bin",
            self.s.terraform_bin,
        ]
        if first:
            # The upgrade passes neither: idp-cli remembers the values of the
            # previous deploy by CloudFormation parameter name, and an upgrade that
            # had to repeat them would not be testing that.
            args += ["--admin-email", self.s.admin_email]
            if self.s.parameters:
                args += ["--parameters", self.s.parameters]
        if self.s.assume_role_arn:
            args += ["--terraform-assume-role-arn", self.s.assume_role_arn]
        code, _ = self.idp_cli(*args, log=log, check=False)
        text = (self.work / "logs" / log).read_text(errors="replace")
        return code, text

    # -- phases ----------------------------------------------------------------

    def phase_preflight(self) -> str:
        self.preflight_passed = False
        problems = self.identity_problems()
        problems += module_pair_problems(self.s.from_module_url, self.s.to_module_url)
        if problems:
            raise PhaseFailed("; ".join(problems))
        self.ctx["account"] = self._account
        from_version, variant = module_coordinates(self.s.from_module_url)
        to_version, _ = module_coordinates(self.s.to_module_url)
        self.ctx.update(fromVersion=from_version, toVersion=to_version, variant=variant)
        summary = self.boto("iam").get_account_summary()["SummaryMap"]
        headroom = summary["RolesQuota"] - summary["Roles"]
        if headroom < self.s.min_role_headroom:
            raise PhaseFailed(
                f"only {headroom} IAM roles below the account quota; the deployment "
                f"needs about {self.s.min_role_headroom}"
            )
        version = self.terraform_capture(self.work, "version", "-json")
        self.ctx["terraformVersion"] = json.loads(version)["terraform_version"]
        if not self.ctx.get("deployStarted"):
            clashes = tmt.Sweeper(self, {}).inventory()
            if clashes:
                raise PhaseFailed(
                    "resources already carrying this run's name exist, so the sweep "
                    "could reach something this run did not create: "
                    + ", ".join(clashes[:10])
                )
        self.preflight_passed = True
        return (
            f"account ok, {headroom} roles of headroom, terraform "
            f"{self.ctx['terraformVersion']}, {from_version} -> {to_version}"
        )

    def phase_deploy_from(self) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        # Before the apply, not after: a partial apply still created resources, and
        # the teardown runs only for a run the context says created something.
        self.ctx["deployStarted"] = True
        self.ctx["created"] = True
        self.save()
        code, text = self.deploy(self.s.from_module_url, "deploy-from.log", first=True)
        if code != 0:
            raise PhaseFailed(
                f"the first deploy exited {code}; see logs/deploy-from.log; last "
                f"lines:\n{tmt.log_tail(self.work / 'logs' / 'deploy-from.log')}"
            )
        self.ctx["handles"] = self.new_handles()
        self.record_names()
        installed = read_manifest(self.dir).get("version")
        if installed and installed != self.ctx["fromVersion"]:
            raise PhaseFailed(
                f"the installed module reports version {installed}, not "
                f"{self.ctx['fromVersion']} as its URL says"
            )
        self.ctx["identityBefore"] = {
            k: v
            for k, v in (self.ctx.get("names") or {}).items()
            if k in IDENTITY_KINDS
        }
        plan = plan_summary(text)
        self.ctx["firstPlan"] = plan
        created = f"{plan['add']} resources" if plan else "the deployment"
        return f"{self.ctx['fromVersion']} deployed, {created} created"

    def phase_seed(self) -> str:
        handles = self.ctx["handles"]
        pool, client, api = handles["pool"], handles["client"], handles["api"]
        if not (pool and client and api):
            raise PhaseFailed(f"the deployment's outputs are incomplete: {handles}")
        cognito, s3 = self.boto("cognito-idp"), self.boto("s3")
        sec = self.secrets()
        users = sec.get("users") or [
            {
                "username": f"tfup-{self.s.stack_name}-admin@example.com",
                "persona": "Admin",
                "password": tmt.make_password(),
            },
            {
                "username": f"tfup-{self.s.stack_name}-author@example.com",
                "persona": "Author",
                "password": tmt.make_password(),
                "allowedConfigVersions": [tmt.SCOPED_CONFIG_VERSION],
            },
        ]
        self.save_secrets({"users": users})
        admin, author = users

        # The Admin, directly in Cognito: someone has to exist to call the API.
        try:
            cognito.admin_create_user(
                UserPoolId=pool,
                Username=admin["username"],
                UserAttributes=[
                    {"Name": "email", "Value": admin["username"]},
                    {"Name": "email_verified", "Value": "true"},
                ],
                MessageAction="SUPPRESS",
            )
        except cognito.exceptions.UsernameExistsException:
            pass
        cognito.admin_set_user_password(
            UserPoolId=pool,
            Username=admin["username"],
            Password=admin["password"],
            Permanent=True,
        )
        cognito.admin_add_user_to_group(
            UserPoolId=pool, Username=admin["username"], GroupName="Admin"
        )
        token = self.sign_in(pool, client, admin["username"], admin["password"])

        # The Author, through the deployment's own API, so the users-table row and
        # the scope are written by the product rather than by this test.
        try:
            cognito.admin_get_user(UserPoolId=pool, Username=author["username"])
        except cognito.exceptions.UserNotFoundException:
            self.api(
                api,
                token,
                "createUser",
                {
                    "email": author["username"],
                    "persona": author["persona"],
                    "allowedConfigVersions": author["allowedConfigVersions"],
                },
            )
        cognito.admin_set_user_password(
            UserPoolId=pool,
            Username=author["username"],
            Password=author["password"],
            Permanent=True,
        )

        # Documents, processed by the release being upgraded from.
        keys = []
        for rel in tmt.SAMPLE_DOCUMENTS:
            key = tmt.SEED_PREFIX + Path(rel).name
            s3.upload_file(
                str(REPO_ROOT / rel),
                handles["inputBucket"],
                key,
                ExtraArgs={"Metadata": {"config-version": tmt.SCOPED_CONFIG_VERSION}},
            )
            keys.append(key)
        statuses = self.wait_documents(handles["trackingTable"], keys)
        if any(v != "COMPLETED" for v in statuses.values()):
            raise PhaseFailed(
                f"the deployment did not complete the seed documents: {statuses}"
            )
        self.ctx["seedKeys"] = keys

        # One configuration edit, through the API as the web UI makes it.
        marker = f"terraform-upgrade-test {self.ctx['runId']}"
        current = self.api(
            api, token, "getConfigVersion", {"versionName": tmt.SCOPED_CONFIG_VERSION}
        )
        custom = tmt._json_field(current, "Custom") or {}
        custom["notes"] = marker
        updated = self.api(
            api,
            token,
            "updateConfiguration",
            {
                "versionName": tmt.SCOPED_CONFIG_VERSION,
                "customConfig": json.dumps(custom),
                "description": "edited by the Terraform upgrade test",
            },
        )
        if not tmt._success(updated):
            raise PhaseFailed(
                f"updateConfiguration answered {json.dumps(updated)[:400]}"
            )
        self.ctx["configMarker"] = marker

        # An evaluation baseline and a test set: both write to buckets of their own,
        # so the upgrade is checked against more than the input and output buckets.
        self.api(api, token, "copyToBaseline", {"objectKey": keys[0]})
        self._wait_prefix(
            self.bucket("evaluation_baseline_bucket"), keys[0] + "/", "baseline copy"
        )
        test_set = self.api(
            api,
            token,
            "addTestSet",
            {
                "name": f"{self.s.stack_name}-seed-set",
                "description": "Terraform upgrade test seed",
                "filePattern": tmt.SEED_PREFIX + "*",
                "bucketType": "input",
                "fileCount": len(keys),
            },
        )
        test_set_id = tmt._find_value(test_set, "id")
        if not test_set_id:
            raise PhaseFailed(
                f"addTestSet returned no id: {json.dumps(test_set)[:300]}"
            )
        self._wait_prefix(self.bucket("test_set_bucket"), f"{test_set_id}/", "test set")
        self.ctx["testSetId"] = test_set_id

        # Each user's profile, as the release being upgraded from reports it.
        profiles = {}
        for user in users:
            t = self.sign_in(pool, client, user["username"], user["password"])
            profiles[user["username"]] = (
                tmt._find_value(self.api(api, t, "getMyProfile", {}), None) or {}
            )
        self.ctx["profilesBefore"] = profiles
        if (
            profiles[author["username"]].get("allowedConfigVersions")
            != author["allowedConfigVersions"]
        ):
            raise PhaseFailed(
                "the Author's scope is "
                f"{profiles[author['username']].get('allowedConfigVersions')!r} "
                "on the deployment itself, before any upgrade"
            )
        return (
            f"{len(keys)} documents COMPLETED, 2 users, 1 config edit, baseline and "
            f"test set {test_set_id}"
        )

    def phase_snapshot_before(self) -> str:
        """Taken only once the deployment is quiet: it keeps writing to a COMPLETED
        document's rows and manifest for minutes after the status is final, and a
        snapshot taken during that reads those writes as changes the upgrade made.
        """
        self.snapshot_targets()
        previous = None
        for _attempt in range(tmt.SETTLE_ATTEMPTS):
            snap = self.snapshot("before")
            if previous is not None and tmt._comparable(snap) == tmt._comparable(
                previous
            ):
                break
            previous = snap
            time.sleep(tmt.SETTLE_SECONDS)
        else:
            raise PhaseFailed(
                "the deployment's data kept changing for "
                f"{tmt.SETTLE_ATTEMPTS * tmt.SETTLE_SECONDS // 60} minutes"
            )
        self.ctx["tableKinds"] = table_kinds(self.state())
        objects = sum(len(v) for v in snap["buckets"].values())
        items = sum(len(v) for v in snap["tables"].values())
        return (
            f"{len(snap['buckets'])} buckets ({objects} objects), "
            f"{len(snap['tables'])} tables ({items} items), {len(snap['users'])} users"
        )

    def phase_upgrade(self) -> str:
        code, text = self.deploy(self.s.to_module_url, "upgrade.log", first=False)
        plan = plan_summary(text)
        self.ctx["upgradePlan"] = plan
        self.save()
        if code != 0:
            refused = refused_addresses(text)
            detail = (
                "the deploy refused the plan for "
                + ", ".join(refused[:12])
                + (f" and {len(refused) - 12} more" if len(refused) > 12 else "")
                if refused
                else "see logs/upgrade.log; last lines:\n"
                + tmt.log_tail(self.work / "logs" / "upgrade.log")
            )
            raise PhaseFailed(f"the upgrade exited {code}: {detail}")
        self.record_names()
        installed = read_manifest(self.dir).get("version")
        if installed and installed != self.ctx["toVersion"]:
            raise PhaseFailed(
                f"after the upgrade the installed module reports version "
                f"{installed}, not {self.ctx['toVersion']}"
            )
        changed = (
            f"{plan['add']} to add, {plan['change']} to change, {plan['destroy']} to "
            "destroy"
            if plan
            else "plan summary not in the log"
        )
        return (
            f"{self.ctx['fromVersion']} -> {self.ctx['toVersion']} applied ({changed})"
        )

    def phase_verify(self) -> str:
        findings: List[str] = []
        handles = self.new_handles()
        before_handles = self.ctx["handles"]
        for key, label in (
            ("pool", "user pool"),
            ("inputBucket", "input bucket"),
            ("trackingTable", "tracking table"),
        ):
            if before_handles.get(key) and handles.get(key) != before_handles[key]:
                findings.append(
                    f"the {label} is {handles.get(key)!r} after the upgrade, not "
                    f"{before_handles[key]!r}"
                )

        # Every data-holding resource still has the identity it had before.
        after_identity = {
            k: v
            for k, v in tmt.physical_names(self.state()).items()
            if k in IDENTITY_KINDS
        }
        self.ctx["identityAfter"] = after_identity
        losses = identity_losses(self.ctx.get("identityBefore") or {}, after_identity)
        if losses:
            findings.append(
                f"{len(losses)} data-holding resource(s) were replaced or deleted: "
                + ", ".join(losses[:10])
            )

        # The label stack carries the new version, as docs/terraform.md says.
        stack = self.boto("cloudformation").describe_stacks(
            StackName=self.s.stack_name
        )["Stacks"][0]
        if self.ctx["toVersion"] not in (stack.get("Description") or ""):
            findings.append(
                f"the label stack's description does not name {self.ctx['toVersion']}: "
                f"{(stack.get('Description') or '')[:160]!r}"
            )

        # The data itself.
        before = json.loads((self.work / "snapshot-before.json").read_text())
        after = self.snapshot("after")
        kinds = self.ctx.get("tableKinds") or {}
        report: Dict[str, Any] = {"buckets": {}, "tables": {}}
        for bucket, objects in before["buckets"].items():
            cmp = tmt.compare_objects(objects, after["buckets"].get(bucket, {}))
            report["buckets"][bucket] = cmp
            if cmp["missing"] or cmp["changed"]:
                findings.append(
                    f"bucket {bucket}: {len(cmp['missing'])} objects missing, "
                    f"{len(cmp['changed'])} changed"
                )
        for table, items in before["tables"].items():
            cmp = tmt.compare_items(
                kinds.get(table, "unknown"), items, after["tables"].get(table, {})
            )
            report["tables"][table] = cmp
            if cmp["missing"] or cmp["changed"]:
                attributes = sorted(
                    {a for c in cmp["changed"] for a in c["attributes"]}
                )
                findings.append(
                    f"table {table}: {len(cmp['missing'])} items missing, "
                    f"{len(cmp['changed'])} changed ({', '.join(attributes)[:300]})"
                )
        if before["users"] != after["users"]:
            findings.append(
                f"pool users changed: {before['users']} -> {after['users']}"
            )
        if before["groups"] != after["groups"]:
            findings.append(
                f"group memberships changed: {before['groups']} -> {after['groups']}"
            )
        (self.work / "snapshot-compare.json").write_text(json.dumps(report, indent=1))

        # Both users sign in with the passwords they had, and keep their scope.
        self.check_users(handles, findings)

        # The configuration edit survived the upgrade's own config rewrite.
        admin = self.secrets()["users"][0]
        token = self.sign_in(
            handles["pool"], handles["client"], admin["username"], admin["password"]
        )
        config = self.api(
            handles["api"],
            token,
            "getConfigVersion",
            {"versionName": tmt.SCOPED_CONFIG_VERSION},
        )
        if self.ctx["configMarker"] not in json.dumps(config):
            findings.append(
                "the configuration edit made before the upgrade is not in the "
                "upgraded deployment"
            )

        # And it still processes a document.
        key = f"upgrade-check/{self.ctx['runId']}-{Path(tmt.NEW_DOCUMENT).name}"
        started = datetime.now(timezone.utc)
        self.boto("s3").upload_file(
            str(REPO_ROOT / tmt.NEW_DOCUMENT), handles["inputBucket"], key
        )
        statuses = self.wait_documents(handles["trackingTable"], [key])
        if statuses[key] != "COMPLETED":
            findings.append(
                f"the document uploaded after the upgrade ended {statuses[key]}"
            )
        time.sleep(30)
        runs = self._executions_since(handles["stateMachine"], started, key)
        if not runs:
            findings.append(
                "the upgraded deployment's state machine ran no execution for it"
            )
        self.ctx["newDocument"] = {
            "key": key,
            "status": statuses[key],
            "runs": len(runs),
        }

        if findings:
            raise PhaseFailed("; ".join(findings))
        added = sum(c["added"] for c in report["buckets"].values())
        return (
            f"every bucket, table, pool and key kept its identity; data preserved "
            f"({added} objects added since); both users sign in with their old "
            f"passwords; config edit kept; a new document reached {statuses[key]}"
        )

    # -- teardown --------------------------------------------------------------

    def phase_destroy(self) -> str:
        if not (self.dir / "main.tf").is_file():
            return "no deployment directory"
        self.record_names()
        args = [
            "delete",
            "--terraform",
            "--stack-name",
            self.s.stack_name,
            "--region",
            self.s.region,
            "--terraform-dir",
            str(self.dir),
            "--terraform-bin",
            self.s.terraform_bin,
            "--force",
        ]
        if self.s.assume_role_arn:
            args += ["--terraform-assume-role-arn", self.s.assume_role_arn]
        self.idp_cli(*args, log="delete.log")
        self.ctx["destroyed"] = True
        return "idp-cli delete --terraform completed"

    def phase_sweep(self) -> str:
        deleted, errors = tmt.Sweeper(self, self.ctx.get("names") or {}).sweep()
        self.ctx["swept"] = deleted
        if errors:
            raise PhaseFailed(
                f"{len(errors)} leftovers could not be deleted: "
                + "; ".join(errors[:10])
            )
        return f"{len(deleted)} retained resources deleted"

    def phase_leftover_check(self) -> str:
        left = tmt.Sweeper(self, self.ctx.get("names") or {}).inventory()
        if (self.dir / ".terraform").is_dir():
            try:
                listed = self.terraform_capture(self.dir, "state", "list").split()
            except PhaseFailed:
                listed = []
            managed = tmt.managed_addresses(listed)
            if managed:
                left.append(f"the state still lists {len(managed)} resources")
        self.ctx["leftovers"] = left
        if left:
            raise PhaseFailed(
                f"{len(left)} resources still stand: " + ", ".join(left[:15])
            )
        return "nothing left"

    def emergency_teardown(self) -> None:
        """After a failure: destroy what exists, then sweep by name."""
        print("⚠ a phase failed; tearing down", flush=True)
        if "destroy" not in self.ctx["completed"] and (
            self.ctx.get("deployStarted") or (self.dir / "main.tf").is_file()
        ):
            self._attempt("destroy")
        self._attempt("sweep")
        self._attempt("leftover-check")

    # -- driver ----------------------------------------------------------------

    def execute(self) -> int:
        stored = stored_name_problems(self.ctx, self.s.stack_name)
        if stored:
            # Refuse before touching the context: overwriting its name would let a
            # later teardown sweep for another deployment.
            print("✗ " + "; ".join(stored), flush=True)
            return 2
        self.ctx.pop("leftovers", None)
        self.ctx.update(
            stackName=self.s.stack_name,
            region=self.s.region,
            fromModuleUrl=self.s.from_module_url,
            toModuleUrl=self.s.to_module_url,
        )
        failed = False
        stopped = False
        try:
            for name in PHASES:
                if name in self.ctx["completed"] and name != "preflight":
                    continue
                if name not in TEARDOWN_PHASES and self.past_deadline():
                    raise PhaseFailed(
                        f"the run's deadline passed before {name}; tearing down so "
                        "that the teardown fits the outer time limit"
                    )
                self.run_phase(name)
                if self.s.stop_after == name:
                    stopped = True
                    print(
                        f"⏸ stopped after {name}; rerun with --resume to continue",
                        flush=True,
                    )
                    break
        except Exception:  # noqa: BLE001 - recorded by run_phase
            failed = True
        finally:
            if failed and not self.s.keep:
                refusal = self.teardown_permitted()
                if refusal:
                    print(f"⚠ no teardown: {refusal}", flush=True)
                else:
                    self.emergency_teardown()
            self.write_result(failed, stopped)
        return 1 if failed or (self.ctx["findings"] and not stopped) else 0

    def teardown_only(self) -> int:
        """Destroy what this work directory's run created and sweep, without any
        forward phase: for a run whose own teardown did not finish."""
        problems = self.identity_problems()
        if not self.ctx.get("created"):
            problems.append("the work directory's run created nothing")
        if problems:
            print("✗ teardown refused: " + "; ".join(problems), flush=True)
            return 2
        self.preflight_passed = True
        self.ctx.pop("leftovers", None)
        self.emergency_teardown()
        left = self.ctx.get("leftovers")
        # Its own files: a teardown-only pass must not rewrite the run's verdict.
        self.write_result(
            failed=bool(left) or left is None, stopped=False, name="teardown"
        )
        return 0 if left == [] else 1

    def write_result(self, failed: bool, stopped: bool, name: str = "result") -> None:
        status = (
            "FAIL"
            if failed or self.ctx["findings"]
            else ("STOPPED" if stopped else "PASS")
        )
        result = {
            "status": status,
            "runId": self.ctx["runId"],
            "stackName": self.s.stack_name,
            "region": self.s.region,
            "fromModuleUrl": self.s.from_module_url,
            "toModuleUrl": self.s.to_module_url,
            "fromVersion": self.ctx.get("fromVersion"),
            "toVersion": self.ctx.get("toVersion"),
            "terraformVersion": self.ctx.get("terraformVersion"),
            "firstPlan": self.ctx.get("firstPlan"),
            "upgradePlan": self.ctx.get("upgradePlan"),
            "newDocument": self.ctx.get("newDocument"),
            "phases": self.ctx["phases"],
            "findings": self.ctx["findings"],
            # An earlier attempt's findings: a run that passed only on a retry says so.
            "previousFindings": self.ctx.get("previousFindings") or [],
            "leftovers": self.ctx.get("leftovers"),
        }
        (self.work / f"{name}.json").write_text(
            json.dumps(result, indent=1, default=str)
        )
        text = summarise(result)
        summary_file = "summary.txt" if name == "result" else f"{name}-summary.txt"
        (self.work / summary_file).write_text(text)
        print("\n" + text, flush=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> Settings:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--stack-name", required=True, help="the deployment's name")
    p.add_argument("--admin-email", required=True, help="the first deploy's AdminEmail")
    p.add_argument("--region", default=os.environ.get("AWS_REGION") or "us-west-2")
    p.add_argument(
        "--from-module-url",
        required=True,
        help="the module zip of the release to deploy first",
    )
    p.add_argument(
        "--to-module-url",
        required=True,
        help="the module zip to upgrade that deployment to",
    )
    p.add_argument("--work-dir", help="default: ./scratch/terraform-upgrade/<stack>")
    p.add_argument(
        "--terraform-bin", default=os.environ.get("IDP_TERRAFORM_BIN") or "terraform"
    )
    p.add_argument(
        "--parameters",
        help='"K=V,K2=V2" by CloudFormation parameter name, for the first deploy; '
        "the upgrade deliberately repeats none of them",
    )
    p.add_argument(
        "--terraform-assume-role-arn", help="role both providers assume, if any"
    )
    p.add_argument("--expected-account", help="refuse to run in any other account")
    p.add_argument("--min-role-headroom", type=int, default=200)
    p.add_argument(
        "--deadline-seconds",
        type=int,
        help="start no forward phase after this many seconds (teardown still runs)",
    )
    p.add_argument(
        "--stop-after", choices=PHASES, help="stop (without teardown) after this phase"
    )
    p.add_argument("--keep", action="store_true", help="never tear down")
    p.add_argument(
        "--resume", action="store_true", help="continue from <work-dir>/context.json"
    )
    p.add_argument(
        "--teardown-only",
        action="store_true",
        help="with --resume: only destroy what the run created and sweep",
    )
    a = p.parse_args(argv)
    problems = validate_stack_name(a.stack_name) + module_pair_problems(
        a.from_module_url, a.to_module_url
    )
    if problems:
        p.error("; ".join(problems))
    work = (
        Path(a.work_dir).expanduser()
        if a.work_dir
        else REPO_ROOT / "scratch" / "terraform-upgrade" / a.stack_name
    )
    tf_bin = a.terraform_bin
    if os.sep in tf_bin:
        tf_bin = str(Path(tf_bin).expanduser().resolve())
    elif not shutil.which(tf_bin):
        p.error(f"{tf_bin} is not on PATH; pass --terraform-bin")
    return Settings(
        stack_name=a.stack_name,
        admin_email=a.admin_email,
        region=a.region,
        work_dir=work.resolve(),
        terraform_bin=tf_bin,
        from_module_url=a.from_module_url,
        to_module_url=a.to_module_url,
        parameters=a.parameters,
        assume_role_arn=a.terraform_assume_role_arn,
        keep=a.keep,
        resume=a.resume,
        stop_after=a.stop_after,
        teardown_only=a.teardown_only,
        expected_account=a.expected_account,
        min_role_headroom=a.min_role_headroom,
        deadline_seconds=a.deadline_seconds,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Terraform's own state file, the snapshots and every log are owner-only.
    os.umask(0o077)
    settings = parse_args(argv)
    run = Run(settings)
    return run.teardown_only() if settings.teardown_only else run.execute()


if __name__ == "__main__":
    sys.exit(main())
