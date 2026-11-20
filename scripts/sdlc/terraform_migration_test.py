#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deploy a real ``awslabs/genai-idp-terraform`` deployment, migrate it in place onto
this repository's Terraform module, prove nothing was lost, and tear both down.

WHAT IT PROVES
--------------
``idp-cli terraform migrate-from-genai-idp-terraform`` moves an upstream
deployment's buckets, tables, KMS key and Cognito user pool under a new deployment
of this repository's module, phase by phase (``docs/terraform-migration.md``). Its
unit tests drive every phase from a sanitised state and plan. Only a live run can
show the things a fixture cannot: that the adoption plan really is free of
replacements, that users really do sign in with their old passwords afterwards,
that the old deployment really stops processing, and that destroying the old
deployment really leaves the adopted resources alone. This script is that run, made
repeatable:

1. clone one of the upstream releases ``UPSTREAM_RELEASES`` pins (and check the
   commit and ``sources/`` it resolves to; ``--upstream-tag rotate``, which the
   scheduled run passes, takes a different one each week), deploy its
   ``examples/unified-processor`` configuration as ``<old-name>``;
2. seed it as a user would: two documents processed (``samples/lending_package.pdf``
   and a W-2), two Cognito users with random passwords (an Admin, and an Author
   scoped to one configuration version, created through the old API), one
   configuration edit, an evaluation baseline and a test set;
3. snapshot every kept bucket and table, the pool's users and groups, each user's
   profile and the edited configuration;
4. run every phase of the command in the guide's order, each with ``--apply``;
5. verify: the snapshot is preserved, both users sign in to the NEW app client with
   their old passwords (SRP, unattended), each user's scope is unchanged, the
   configuration edit survived, ``idp-cli config-list`` shows every profile, and a
   new document is processed by the new deployment and not by the old one;
6. ``detach``, the old deployment's destroy plan, ``check-destroy``, the old
   deployment's destroy, a second sign-in check against what is left and a check
   that the Region's API Gateway CloudWatch role is the one it was before the
   destroy and still exists, then the new deployment's ``idp-cli delete
   --terraform`` and a sweep of what either retained, by name. Before the sweep the
   setting goes back to what it was when the run started (or, when that is refused,
   is cleared and a finding names the value to restore), and the run fails if it
   still leaves the setting naming a role that no longer exists.

Teardown runs in a ``finally``: a failed phase still destroys both deployments
(new first, then old, since at that point both states may name the shared
resources) and sweeps by name. ``--keep`` and ``--stop-after`` skip it.

WHAT IT DOES NOT PROVE
----------------------
* Other upstream example configurations (``bda-processor`` and the rest), upstream
  releases ``UPSTREAM_RELEASES`` does not pin (on any one run, any release but the
  one it took), or an upstream deployment that is months old (the expiry checks run
  against objects minutes old, so ``check-retention`` cannot find anything to
  refuse).
* The knowledge-base re-sync, the alerts subscription, or the browser sign-in (an
  SRP sign-in proves the password; ``~/spike``-style browser checks prove the UI).
* The back-out route in the guide.

SAFETY
------
Everything it deletes is either named in this run's own Terraform states (recorded
before each destroy) or starts with one of this run's two exact name prefixes
(``owned_prefixes``; never a substring, never case-folded). Preflight runs the very
inventory the sweep and the leftover check use, and refuses to start when it finds
anything, so the sweep cannot reach a resource this run did not create. No teardown
of any kind runs unless this invocation's preflight passed (names usable, the same
as the work directory's run, unclaimed, right account) and the run created
something; ``--teardown-only`` checks the same. Passwords live only in
``<work-dir>/secrets.json``; every file the run writes, Terraform's state included,
is owner-only (umask 077).

Running upstream runs third-party code: ``terraform apply`` executes upstream's
``local-exec`` provisioners (its layer, web-UI and agent builds call the AWS CLI with
the run's credentials). Upstream ships no provider lock and constrains providers with
``>=`` only; the driver installs ``terraform_migration_upstream.terraform.lock.hcl``,
the lock the passing run resolved, before ``init``, so the provider builds are pinned
and hash-checked. Upstream's commit and its ``sources/`` (a submodule or a vendored tree)
are pinned too, per release.

USAGE
-----
    make stacktest-terraform-migration OLD_NAME=gitf2 NEW_NAME=gitfnew2 \\
        TERRAFORM_MODULE_URL=https://.../idp-terraform-<version>.zip

    python3 scripts/sdlc/terraform_migration_test.py --old-name gitf2 \\
        --new-name gitfnew2 --terraform-module-url <zip url> \\
        --work-dir ~/scratch/gitf2 [--stop-after seed] [--resume] [--keep]

Measured on the passing run: 166 minutes. The upstream deploy took 18 and the
adoption apply 33; 76 were spent waiting on DynamoDB, which refuses to turn the
agent-jobs table's time-to-live off until about an hour after upstream enabled it,
and the module's new setting until about an hour after that (docs/terraform-migration.md,
Hazards); destroying both deployments is about 30.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import cognito_srp  # noqa: E402

UPSTREAM_REPO = "https://github.com/awslabs/genai-idp-terraform"


@dataclass(frozen=True)
class UpstreamRelease:
    """One upstream release the run can migrate from, with what its clone must
    resolve to. A tag can be moved; a clone resolving it anywhere else is refused,
    because the test would no longer be exercising what it was written against.

    ``sources`` is what ``git ls-tree <commit> sources`` must report: upstream's
    ``sources/`` holds this repository at a release, as a git submodule
    (``("commit", <sha>)``, initialised after the clone as upstream's README says)
    or, in earlier releases, as a vendored tree (``("tree", <sha>)``, which the
    pinned commit already fixes and which needs no initialising)."""

    tag: str
    commit: str
    sources: Tuple[str, str]


#: Every release the scheduled run rotates through, oldest first; the last is the
#: default for a run started by hand. A release belongs here only if the run can
#: exercise it unchanged: its ``examples/unified-processor`` declares every
#: variable upstream_tfvars sets (the seed phase needs the evaluation and test-set
#: features), every resource type it declares is one v0.6.9-tf.0 declares (the
#: release the command's resource kinds were derived from), and its provider
#: constraints admit UPSTREAM_LOCK_FILE. Releases before v0.6.4-tf.0 fail the
#: first test: their example has no ``enable_evaluation`` or ``enable_test_studio``.
UPSTREAM_RELEASES: Tuple[UpstreamRelease, ...] = (
    # The oldest release that passes the three checks above, read from its
    # files; it has not yet been through a live run. Vendors sources/ as a tree.
    UpstreamRelease(
        tag="v0.6.4-tf.0",
        commit="29b62a9ee0f0b53b26e4cc25e7d2bba629804a3b",
        sources=("tree", "bd0fb105f2a60ed807e02e112de4d71b1473de75"),
    ),
    # The newest release, and the one the passing run in the guide measured.
    UpstreamRelease(
        tag="v0.6.9-tf.0",
        commit="5fdac033b2376f16beddffc2a391422d18cf2292",
        sources=("commit", "325794a6ffe1c89bac41a0180c991e681eb70bc8"),
    ),
)
UPSTREAM_TAG = UPSTREAM_RELEASES[-1].tag
UPSTREAM_COMMIT = UPSTREAM_RELEASES[-1].commit
#: ``--upstream-tag rotate``: the release for this run's week (rotated_upstream_tag).
#: What the scheduled CI run passes. A resumed run keeps the release it recorded.
ROTATE_UPSTREAM_TAG = "rotate"
UPSTREAM_EXAMPLE = "examples/unified-processor"
#: Which old deployment the run migrates. ``base`` is the example with every
#: data-owning feature on and MCP off, which is what the command handles by
#: itself. ``mcp`` also turns on upstream's MCP integration, and ``mcp-datasets``
#: that and both Test Studio datasets. Each puts an ``aws_cloudformation_stack`` in
#: the old state that ``plan`` refuses, and the run then performs the manual
#: procedure in docs/terraform-migration.md ("Old deployments with a private
#: network, MCP or test datasets") before ``plan`` (phase ``pre-plan-features``).
#: ``mcp-datasets`` cannot pass at the pinned releases: upstream packages its
#: dataset deployers without their dependencies (``cfnresponse``, read from
#: modules/processing-environment-api/test-studio.tf), so their custom resources
#: never answer and both stacks stay CREATE_IN_PROGRESS past Terraform's 30-minute
#: timeout, which is how its one run (v0.6.9-tf.0) failed at upstream-deploy.
BASE_VARIANT = "base"
MCP_VARIANT = "mcp"
MCP_DATASETS_VARIANT = "mcp-datasets"
VARIANTS = (BASE_VARIANT, MCP_VARIANT, MCP_DATASETS_VARIANT)
MCP_VARIANTS = (MCP_VARIANT, MCP_DATASETS_VARIANT)
#: The override file that turns the two datasets on (datasets_override).
DATASETS_OVERRIDE_FILE = "zz_idp_ci_datasets_override.tf"
#: The upstream module call in every upstream example.
UPSTREAM_MODULE_CALL = "module.genai_idp_accelerator"
#: What the guide's MCP and test-dataset procedures destroy before ``plan``.
FEATURE_DESTROY_TARGETS = (
    f"{UPSTREAM_MODULE_CALL}.module.mcp_integration[0]",
    f"{UPSTREAM_MODULE_CALL}.module.processing_environment_api[0]"
    ".aws_cloudformation_stack.w2_dataset[0]",
    f"{UPSTREAM_MODULE_CALL}.module.processing_environment_api[0]"
    ".aws_cloudformation_stack.fcc_dataset[0]",
)
#: The two datasets: test-set id (the tracking-table key ``testset#<id>``) and
#: the test-set bucket prefix, the same in upstream's deployers and this
#: repository's (src/lambda/{w2,fcc}_dataset_deployer/index.py).
DATASETS = (("fake-w2", "fake-w2/"), ("realkie-fcc-verified", "realkie-fcc-verified/"))
#: The OAuth resource server upstream's MCP integration puts in the pool, which
#: the module creates too: Cognito refuses a second one with the same identifier.
MCP_RESOURCE_SERVER = "idp-mcp-connector"
#: Upstream ships no provider lock file, so its `>=` constraints would resolve to
#: whatever the registry has on the day. This is the lock the passing run
#: resolved, copied into the example before `init` so every run uses the same
#: provider builds (`init` verifies their hashes against it).
UPSTREAM_LOCK_FILE = HERE / "terraform_migration_upstream.terraform.lock.hcl"
#: Written beside every upstream ``aws_iam_role`` and ``aws_iam_policy`` the
#: example loads, when the run is given an IAM path and a permissions boundary
#: (CI always is). Terraform merges ``*_override.tf`` into the blocks it names, so
#: upstream's files are not edited and the pinned commit is what is deployed,
#: with two attributes added.
IAM_OVERRIDE_FILE = "zz_idp_ci_iam_override.tf"
_IAM_RESOURCE_RE = re.compile(
    r'^\s*resource\s+"(aws_iam_role|aws_iam_policy)"\s+"([^"]+)"', re.MULTILINE
)
#: What IAM accepts as a path, with the leading and trailing slash it requires.
_IAM_PATH_RE = re.compile(r"^/[A-Za-z0-9+=,.@_-]+/$")
#: ``--state-mirror s3://<bucket>/<prefix>/``: where the run keeps both Terraform
#: states (S3 backends), its context and the new root's configuration, so that a
#: later build can tear the run down after this one's work directory is gone.
_STATE_MIRROR_RE = re.compile(
    r"^s3://([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/((?:[A-Za-z0-9!_.*'()=-]+/)+)$"
)
#: The backend file in each root. The new root's is the name idp-cli keeps beside
#: the main.tf it writes, as on a Terraform nightly; the old one is prefixed so it
#: is plainly not one of upstream's files.
OLD_BACKEND_FILE = "zz_idp_ci_backend.tf"
NEW_BACKEND_FILE = "backend.tf"
MIRROR_CONTEXT = "context.json"
MIRROR_NEW_ROOT = "new-root/"


def parse_state_mirror(url: str) -> Tuple[str, str]:
    """``(bucket, prefix)`` of a ``s3://<bucket>/<prefix>/`` mirror URL."""
    match = _STATE_MIRROR_RE.match(url)
    if not match:
        raise ValueError(f"{url!r} is not s3://<bucket>/<prefix>/")
    return match.group(1), match.group(2)


def render_backend(bucket: str, key: str, region: str) -> str:
    """An S3 backend block, the shape codebuild_deployment.render_terraform_backend
    writes for a Terraform nightly: encrypted, with S3-native locking."""
    return "\n".join(
        [
            "# Written by scripts/sdlc/terraform_migration_test.py: this root's state,",
            "# kept in the CI bucket so a later build can destroy the deployment.",
            "terraform {",
            '  backend "s3" {',
            f"    bucket       = {json.dumps(bucket)}",
            f"    key          = {json.dumps(key)}",
            f"    region       = {json.dumps(region)}",
            "    encrypt      = true",
            "    use_lockfile = true",
            "  }",
            "}",
            "",
        ]
    )


def new_root_files(new_dir: Path) -> List[Path]:
    """The new root's own configuration: what idp-cli and the migration command
    wrote there, which is all a teardown needs beside the state. Never a state
    file, a plan or anything under ``.terraform``."""
    if not new_dir.is_dir():
        return []
    return sorted(
        f
        for f in new_dir.iterdir()
        if f.is_file()
        and (f.suffix in (".tf", ".tfvars") or f.name == ".terraform.lock.hcl")
        and "tfstate" not in f.name
    )


SAMPLE_DOCUMENTS = (
    "samples/lending_package.pdf",
    "samples/w2/W2_XL_input_clean_1000.pdf",
)
NEW_DOCUMENT = "samples/w2/W2_XL_input_clean_1002.pdf"
SEED_PREFIX = "seed/"
SCOPED_CONFIG_VERSION = "default"
FINAL_STATUSES = ("COMPLETED", "FAILED", "ABORTED")

#: Old resources the migration must keep, by (type, name) in the upstream
#: configuration. Owned by this test rather than read from the command's plan, so a
#: plan that silently dropped one of them fails here instead of shrinking the
#: snapshot it is compared with.
REQUIRED_KEPT = (
    ("aws_s3_bucket", "input_bucket"),
    ("aws_s3_bucket", "output_bucket"),
    ("aws_s3_bucket", "working_bucket"),
    ("aws_s3_bucket", "evaluation_baseline_bucket"),
    ("aws_s3_bucket", "test_sets"),
    ("aws_dynamodb_table", "tracking_table"),
    ("aws_dynamodb_table", "configuration_table"),
    ("aws_dynamodb_table", "users"),
    ("aws_kms_key", "encryption_key"),
    ("aws_cognito_user_pool", "user_pool"),
)
KEPT_VERDICTS = ("adopt", "adopt-after-fix")

#: Changes ``apply`` refuses by design that docs/terraform-migration.md tells every
#: migration to accept (its Hazards section). The test accepts these and nothing
#: else, so a refusal the guide does not document fails the run.
#: The pre-migration snapshot is retaken every SETTLE_SECONDS until two agree.
SETTLE_SECONDS = 180
SETTLE_ATTEMPTS = 8

#: How long after turning the old TTL off the adoption apply waits (the guide: at
#: least an hour), with a margin.
TTL_SETTLE_SECONDS = 65 * 60

GUIDE_ACCEPTED_CHANGES = ("module.idp.aws_dynamodb_table.agent_table:ttl-changed",)

#: The only change the migration is allowed to make to an existing item in a kept
#: table: ``fix-config`` removes these two counters from each ``Config#`` row.
CONFIG_COUNTERS = ("LatestRevision", "PublishedRevision")

PHASES = (
    "preflight",
    "upstream-clone",
    "upstream-deploy",
    "seed",
    "old-ttl-off",
    "pre-plan-features",
    "migrate-plan",
    "migrate-check-retention",
    "migrate-freeze",
    "snapshot-before",
    "migrate-fix-config",
    "migrate-adopt-user-pool",
    "ttl-settle",
    "migrate-apply-dry-run",
    "migrate-apply",
    "migrate-restore-config",
    "verify",
    "migrate-detach",
    "old-destroy-plan",
    "migrate-check-destroy",
    "old-destroy",
    "verify-after-old-destroy",
    "new-destroy",
    "sweep",
    "leftover-check",
)
#: Phases after which the old deployment's own resources are gone.
TEARDOWN_PHASES = ("old-destroy", "new-destroy", "sweep", "leftover-check")


class PhaseFailed(Exception):
    """A phase found something wrong. The message is the finding."""


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested offline in scripts/sdlc/tests)
# ---------------------------------------------------------------------------


def upstream_iam_overrides(
    upstream_root: Path, iam_path: str, boundary_arn: str
) -> Dict[Path, str]:
    """``{override file: content}`` putting every IAM role and managed policy the
    upstream example loads under ``iam_path``, and every role under
    ``boundary_arn``.

    Upstream sets no permissions boundary on any role and names many roles and
    policies from fixed strings (``ConfigurationResolverRole-<random>``) rather
    than from its prefix, so the CI deploy role, which may create only roles that
    carry the run's boundary and whose ARN starts with ``role/idp``, refuses them.
    With the boundary attached and an ``idp…`` path, ``role/<path><name>`` matches
    the role's existing grant: no IAM permission is widened, and the run's
    boundary still marks exactly the roles this run may write. Upstream builds no
    role or policy ARN from a name and the migration adopts no IAM resource
    (``RECREATE_SAFE_TYPES``), so neither attribute changes what is tested.

    Reads the module tree and the one example the run deploys; ``sources/`` (the
    accelerator, a submodule), other examples and test fixtures are not loaded by
    it and are skipped.
    """
    if not _IAM_PATH_RE.match(iam_path):
        raise ValueError(f"{iam_path!r} is not an IAM path")
    found: Dict[Path, List[Tuple[str, str]]] = {}
    for dirpath, dirnames, filenames in os.walk(upstream_root):
        rel = Path(dirpath).relative_to(upstream_root)
        if rel.parts and rel.parts[0] in ("sources", ".git"):
            dirnames[:] = []
            continue
        if rel.parts[:1] == ("examples",) and not (
            Path(UPSTREAM_EXAMPLE).is_relative_to(rel) or rel == Path(UPSTREAM_EXAMPLE)
        ):
            dirnames[:] = []
            continue
        if ".terraform" in rel.parts or "tests" in rel.parts:
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            if not name.endswith(".tf") or name.endswith("override.tf"):
                continue
            text = (Path(dirpath) / name).read_text(encoding="utf-8")
            found.setdefault(Path(dirpath), []).extend(_IAM_RESOURCE_RE.findall(text))
    files: Dict[Path, str] = {}
    for directory, blocks in sorted(found.items()):
        if not blocks:
            continue
        lines = [
            "# Written by scripts/sdlc/terraform_migration_test.py: puts upstream's IAM",
            "# under this run's path and permissions boundary. Not part of upstream.",
        ]
        for kind, name in blocks:
            lines += ["", f'resource "{kind}" "{name}" {{', f'  path = "{iam_path}"']
            if kind == "aws_iam_role":
                lines.append(f'  permissions_boundary = "{boundary_arn}"')
            lines.append("}")
        files[directory / IAM_OVERRIDE_FILE] = "\n".join(lines) + "\n"
    return files


def upstream_release(tag: str) -> UpstreamRelease:
    """The pinned release for ``tag``; ValueError for a tag UPSTREAM_RELEASES does
    not pin, since there would be no commit to hold the clone to."""
    for release in UPSTREAM_RELEASES:
        if release.tag == tag:
            return release
    raise ValueError(
        f"upstream tag {tag!r} is not one of the pinned releases "
        f"({', '.join(r.tag for r in UPSTREAM_RELEASES)}); add it to "
        "UPSTREAM_RELEASES with the commit and sources/ it resolves to"
    )


def rotated_upstream_tag(
    day: date, releases: Sequence[UpstreamRelease] = UPSTREAM_RELEASES
) -> str:
    """The release a scheduled run on ``day`` migrates from.

    One release per Monday-to-Sunday week, in list order, so runs a week apart
    take consecutive releases and every release is covered once in
    ``len(releases)`` weekly runs. A rerun the same week gets the same release,
    and ``--upstream-tag <that tag>`` reproduces it on any day. The date is read
    when the driver starts, so a schedule firing just before Monday 00:00 UTC
    can land either side of the boundary and repeat a release."""
    if not releases:
        raise ValueError("no upstream releases to rotate through")
    week = (day.toordinal() - 1) // 7  # date.min (ordinal 1) is a Monday
    return releases[week % len(releases)].tag


def resolve_upstream_tag(
    requested: Optional[str], stored: Optional[str], day: date
) -> Tuple[Optional[str], Optional[str]]:
    """``(tag, None)`` for the release this invocation uses, or ``(None, why)``.

    A work directory whose run recorded a release keeps it: its old deployment
    was cloned and deployed from that release, so a teardown or a resume on
    another one would plan the wrong configuration against its state. That holds
    for ``rotate`` (a run resumed in a later week, or reaped by a later build)
    and for no tag at all, and is refused for an explicit tag that differs. With
    nothing recorded, no tag means the newest release."""
    if stored:
        if requested not in (None, ROTATE_UPSTREAM_TAG, stored):
            return None, (
                f"the upstream tag is {requested!r} here but {stored!r} in this "
                "work directory's run"
            )
        tag = stored
    elif requested == ROTATE_UPSTREAM_TAG:
        tag = rotated_upstream_tag(day)
    else:
        tag = requested or UPSTREAM_TAG
    try:
        upstream_release(tag)
    except ValueError as e:
        return None, str(e)
    return tag, None


def iam_role_name(role_arn: str) -> str:
    """The role name in ``arn:<partition>:iam::<account>:role/<path/><name>``."""
    return role_arn.rsplit("/", 1)[-1]


def api_gateway_role_problems(
    before: str, after: str, after_exists: Optional[bool]
) -> List[str]:
    """What destroying the old deployment did to the Region's API Gateway
    CloudWatch role, given the setting before and after the destroy and whether
    the role ``after`` names still exists (None: could not be read).

    The setting is one per account and Region, shared by every REST API there, so
    a destroy that clears it, points it elsewhere or deletes the role it names
    stops API Gateway logging for all of them. Nothing to check when the Region
    had no role before the destroy."""
    if not before:
        return []
    if not after:
        return [
            f"the old destroy cleared the Region's API Gateway CloudWatch role "
            f"(it was {before})"
        ]
    if after != before:
        return [
            f"the old destroy changed the Region's API Gateway CloudWatch role "
            f"from {before} to {after}"
        ]
    if after_exists is False:
        return [
            f"the Region's API Gateway CloudWatch role is still {after}, but the "
            "old destroy deleted that role"
        ]
    if after_exists is None:
        return [
            f"could not read whether the API Gateway CloudWatch role {after} exists"
        ]
    return []


def api_gateway_role_left_problems(
    start: str, start_exists: Optional[bool], now: str, now_exists: Optional[bool]
) -> List[str]:
    """Whether the run leaves the Region's API Gateway CloudWatch role setting
    naming a role that no longer exists, given the setting when the run started
    and now (and whether each named role existed then). Not reported when the
    Region was already in exactly that state before the run."""
    if not now or now_exists is True:
        return []
    if now_exists is None:
        return [f"could not read whether the API Gateway CloudWatch role {now} exists"]
    if now == start and start_exists is False:
        return []
    return [
        f"the Region's API Gateway CloudWatch role setting names {now}, a role "
        f"that no longer exists (when the run started it was {start or 'unset'}); "
        "API Gateway logging in this Region fails until it is set to a role that "
        "exists"
    ]


def validate_names(old_name: str, new_name: str) -> List[str]:
    """Reasons the two deployment names cannot be used; empty when they can.

    Upstream's ``prefix`` and the module's ``stack_name`` both become part of
    resource names, and the leftover sweep matches by name, so neither may contain
    the other: a sweep for ``gitf2`` must not match ``gitf2new``'s resources while
    the second deployment is meant to outlive the first.
    """
    problems = []
    pattern = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
    # The old name is upstream's `prefix`: its longest IAM role name is the prefix
    # plus 51 characters (measured), and IAM caps role names at 64.
    for label, value, limit in (("old", old_name, 13), ("new", new_name, 25)):
        if not pattern.match(value):
            problems.append(
                f"the {label} name {value!r} must be lowercase letters, digits and "
                "hyphens, starting with a letter"
            )
        if len(value) > limit:
            problems.append(
                f"the {label} name {value!r} is longer than {limit} characters"
            )
    if old_name and new_name and (old_name in new_name or new_name in old_name):
        problems.append(
            f"{old_name!r} and {new_name!r} overlap; the sweep matches by name, so "
            "neither may contain the other"
        )
    return problems


def make_password(rng: Optional[secrets.SystemRandom] = None) -> str:
    """A 24-character password meeting any Cognito password policy: at least one
    upper, lower, digit and symbol, from Cognito's allowed symbol set."""
    rng = rng or secrets.SystemRandom()
    symbols = "^$*.[]{}()?-!@#%&/,><:;|_~"
    classes = (string.ascii_uppercase, string.ascii_lowercase, string.digits, symbols)
    chars = [rng.choice(c) for c in classes]
    pool = "".join(classes)
    chars += [rng.choice(pool) for _ in range(20)]
    rng.shuffle(chars)
    return "".join(chars)


def upstream_tfvars(
    old_name: str, run_id: str, region: str, variant: str = BASE_VARIANT
) -> str:
    """The upstream example's ``terraform.tfvars``: every feature that owns data
    the migration has to keep is on, so each kept resource kind exists to check.
    The ``mcp-datasets`` variant turns MCP on as well (the test datasets are not an
    input of the example; see :func:`datasets_override`)."""
    return (
        f'region      = "{region}"\n'
        f'prefix      = "{old_name}"\n'
        "admin_email = null\n\n"
        "create_bda_project          = true\n"
        "create_knowledge_base       = true\n"
        "create_discovery            = true\n"
        "chat_with_document_enabled  = true\n"
        "enable_evaluation           = true\n"
        "enable_test_studio          = true\n"
        "enable_agent_companion_chat = true\n"
        "enable_agent_analytics      = true\n"
        f"enable_mcp                  = {'true' if variant in MCP_VARIANTS else 'false'}\n"
        "enable_finetuning           = false\n\n"
        "rbac   = { enabled = true }\n"
        "web_ui = { enabled = true }\n\n"
        f'tags = {{ Project = "terraform-migration-test", Run = "{run_id}" }}\n'
    )


def _block_end(text: str, open_brace: int) -> int:
    """The index just past the ``}`` closing the ``{`` at ``open_brace``, skipping
    braces inside strings and ``#`` or ``//`` comments. ValueError when unclosed."""
    depth, i, n = 0, open_brace, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "#" or text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unclosed block")


def datasets_override(main_tf: str) -> str:
    """An override file for the upstream example that turns on both Test Studio
    datasets. The example passes the upstream module a literal ``api = { ... }``
    without the two flags, and Terraform's override files replace a module
    argument whole, so the override restates the example's own ``api`` expression,
    read from its ``main.tf``, with the flags added. Upstream's files stay as the
    pinned commit has them. ValueError when the example's layout has changed."""
    call = re.search(r'^module\s+"genai_idp_accelerator"\s*\{', main_tf, re.MULTILINE)
    if not call:
        raise ValueError('no module "genai_idp_accelerator" block')
    body = main_tf[call.end() - 1 : _block_end(main_tf, call.end() - 1)]
    api = re.search(r"^(\s*)api\s*=\s*\{", body, re.MULTILINE)
    if not api:
        raise ValueError("the module call has no literal api = { ... } argument")
    end = _block_end(body, api.end() - 1)
    expression = body[api.end() - 1 : end]
    if re.search(r"\benable_(w2|fcc)_dataset\b", expression):
        raise ValueError("the example already sets a dataset flag")
    indent = api.group(1).lstrip("\n")
    flags = (
        f"{indent}  enable_w2_dataset  = true\n{indent}  enable_fcc_dataset = true\n"
    )
    expression = expression[:-1].rstrip() + "\n" + flags + indent + "}"
    return (
        "# Written by scripts/sdlc/terraform_migration_test.py (variant\n"
        f"# {MCP_DATASETS_VARIANT}): the example's api argument with both Test Studio\n"
        "# datasets on.\n"
        'module "genai_idp_accelerator" {\n'
        f"{indent}api = {expression}\n"
        "}\n"
    )


def feature_destroy_problems(plan: Dict[str, Any]) -> List[str]:
    """Each change in the targeted destroy of the MCP integration and the dataset
    stacks that the guide's procedure does not allow: anything but a delete, and a
    delete of anything but a resource of a type the migration command recreates
    anyway, one of the dataset or gateway stacks, or the MCP integration's own
    resource server. An empty plan is a problem too: the procedure would then have
    destroyed nothing."""
    # The command's own list, so this check cannot drift from what `plan` recreates.
    from idp_sdk._core.terraform_migrate import RECREATE_SAFE_TYPES  # noqa: TID251

    mcp_module = FEATURE_DESTROY_TARGETS[0] + "."
    problems: List[str] = []
    deleted = 0
    for rc in plan.get("resource_changes", []):
        actions = (rc.get("change") or {}).get("actions", [])
        if actions == ["no-op"] or rc.get("mode") == "data":
            continue
        address, tf_type = rc.get("address", ""), rc.get("type", "")
        if actions != ["delete"]:
            problems.append(f"{address}: {'/'.join(actions)}, not a delete")
            continue
        deleted += 1
        if tf_type in RECREATE_SAFE_TYPES:
            continue
        if tf_type == "aws_cloudformation_stack" and (
            address in FEATURE_DESTROY_TARGETS or address.startswith(mcp_module)
        ):
            continue
        if tf_type == "aws_cognito_resource_server" and address.startswith(mcp_module):
            continue
        problems.append(f"{address}: deletes a {tf_type}")
    if not deleted:
        problems.append("the targeted destroy deletes nothing")
    return problems


def state_instances(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every managed resource instance in a Terraform state, flattened to
    ``{address, type, name, module, attributes}``."""
    out = []
    for res in state.get("resources", []):
        if res.get("mode") != "managed":
            continue
        module = res.get("module", "")
        for inst in res.get("instances", []):
            key = inst.get("index_key")
            suffix = (
                ""
                if key is None
                else (f'["{key}"]' if isinstance(key, str) else f"[{key}]")
            )
            address = (
                f"{module}." if module else ""
            ) + f"{res['type']}.{res['name']}{suffix}"
            out.append(
                {
                    "address": address,
                    "type": res["type"],
                    "name": res["name"],
                    "module": module,
                    "attributes": inst.get("attributes") or {},
                }
            )
    return out


def physical_names(state: Dict[str, Any]) -> Dict[str, List[str]]:
    """The physical names of the resources the sweep knows how to delete, read from
    a state before it is destroyed. The sweep deletes only these, plus names
    carrying one of the run's two names."""
    names: Dict[str, List[str]] = {
        "buckets": [],
        "tables": [],
        "log_groups": [],
        "user_pools": [],
        "kms_keys": [],
        "bda_projects": [],
        "roles": [],
    }
    for inst in state_instances(state):
        a = inst["attributes"]
        t = inst["type"]
        if t in ("aws_s3_bucket",) and a.get("bucket"):
            names["buckets"].append(a["bucket"])
        elif t == "awscc_s3_bucket" and a.get("bucket_name"):
            names["buckets"].append(a["bucket_name"])
        elif t == "aws_dynamodb_table" and a.get("name"):
            names["tables"].append(a["name"])
        elif t == "awscc_dynamodb_table" and a.get("table_name"):
            names["tables"].append(a["table_name"])
        elif t == "aws_cloudwatch_log_group" and a.get("name"):
            names["log_groups"].append(a["name"])
        elif t == "awscc_logs_log_group" and a.get("log_group_name"):
            names["log_groups"].append(a["log_group_name"])
        elif t == "aws_cognito_user_pool" and a.get("id"):
            names["user_pools"].append(a["id"])
        elif t == "aws_kms_key" and a.get("key_id"):
            names["kms_keys"].append(a["key_id"])
        elif t == "awscc_kms_key" and a.get("key_id"):
            names["kms_keys"].append(a["key_id"])
        elif t == "awscc_bedrock_data_automation_project" and a.get("project_arn"):
            names["bda_projects"].append(a["project_arn"])
        elif t == "aws_iam_role" and a.get("name"):
            names["roles"].append(a["name"])
    return {k: sorted(set(v)) for k, v in names.items()}


def merge_names(*parts: Dict[str, List[str]]) -> Dict[str, List[str]]:
    out: Dict[str, set] = {}
    for part in parts:
        for k, v in (part or {}).items():
            out.setdefault(k, set()).update(v)
    return {k: sorted(v) for k, v in out.items()}


def required_kept_missing(old_state: Dict[str, Any], plan: Dict[str, Any]) -> List[str]:
    """Each REQUIRED_KEPT resource of the old state the migration plan does not
    adopt. An entry absent from the old state is reported too: the test seeded a
    deployment that should have one."""
    by_address = {r["old_address"]: r for r in plan.get("resources", [])}
    problems = []
    instances = state_instances(old_state)
    for tf_type, name in REQUIRED_KEPT:
        found = [i for i in instances if i["type"] == tf_type and i["name"] == name]
        if not found:
            problems.append(f"the old state has no {tf_type}.{name}")
            continue
        for inst in found:
            planned = by_address.get(inst["address"])
            verdict = planned and planned.get("verdict")
            if verdict not in KEPT_VERDICTS:
                problems.append(
                    f"{inst['address']} is not adopted (verdict {verdict!r})"
                )
    return problems


def kept_resources(plan: Dict[str, Any]) -> Dict[str, List[str]]:
    """The physical buckets and tables the migration plan keeps."""
    buckets, tables = set(), set()
    for r in plan.get("resources", []):
        if r.get("verdict") not in KEPT_VERDICTS or not r.get("physical_id"):
            continue
        if r["old_type"] in ("aws_s3_bucket", "awscc_s3_bucket"):
            buckets.add(r["physical_id"])
        elif r["old_type"] in ("aws_dynamodb_table", "awscc_dynamodb_table"):
            tables.add(r["physical_id"])
    return {"buckets": sorted(buckets), "tables": sorted(tables)}


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable by the owner only, from creation."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def log_tail(path: Path, lines: int = 30) -> str:
    """The last ``lines`` lines of a log, without terminal colour codes, so a CI
    job shows the actual error (an AccessDenied, say) rather than a path."""
    try:
        text = _ANSI.sub("", path.read_text(errors="replace"))
    except OSError:
        return ""
    return "\n".join(text.rstrip().splitlines()[-lines:])


def restored_profiles(changes: Sequence[str]) -> List[str]:
    """The profiles a ``restore-config`` result says it restored. Each change reads
    ``restore revision <n> of <profile> ...``; a change of any other shape is an
    error rather than a silent skip, since the profile list decides what verify
    allows."""
    out = []
    for change in changes:
        m = re.match(r"^restore revision \d+ of (\S+)( |$)", change)
        if not m:
            raise ValueError(f"unrecognised restore-config change: {change!r}")
        out.append(m.group(1))
    return out


def profile_listed(listing: str, profile: str) -> bool:
    """Whether ``config-list`` output names ``profile`` as a whole word, not as part
    of a longer profile name."""
    return bool(re.search(rf"(?<![\w-]){re.escape(profile)}(?![\w-])", listing))


def _json_default(value: Any) -> Any:
    import base64

    if isinstance(value, (bytes, bytearray)):
        return {"base64": base64.b64encode(bytes(value)).decode()}
    return str(value)


def _comparable(snap: Dict[str, Any]) -> str:
    return json.dumps(snap, sort_keys=True, default=str)


def item_key(item: Dict[str, Any], key_names: Sequence[str]) -> str:
    return json.dumps([item.get(k) for k in key_names], sort_keys=True)


def compare_objects(
    before: Dict[str, Dict[str, Any]], after: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Every object present before must be present after with the same ETag and
    size. Objects added since are counted, not judged."""
    missing = sorted(k for k in before if k not in after)
    changed = sorted(
        k
        for k in before
        if k in after
        and (
            before[k]["etag"] != after[k]["etag"]
            or before[k]["size"] != after[k]["size"]
        )
    )
    added = sum(1 for k in after if k not in before)
    return {
        "missing": missing,
        "changed": changed,
        "added": added,
        "before": len(before),
    }


#: Configuration-table rows every deploy of the module rewrites (the schema, the
#: pricing and the model limits it ships). Not user data, so a change is expected.
DEPLOYMENT_OWNED_CONFIG_ROWS = ("Schema", "DefaultPricing", "DefaultModelConfigLimits")


#: Attributes the module's ``backfill_gsi_attributes`` function
#: (src/lambda/backfill_gsi_attributes) adds to existing tracking-table rows so
#: that its indexes cover them: ``ItemType`` from the key prefix, the HITL index
#: flag, a test run's ``InitialEventTime``, and ``ConfidenceAlertCount`` computed
#: from the row's sections. Measured: upstream's ``run#`` rows carry neither
#: ``ItemType`` nor ``ConfidenceAlertCount``, and gained both after the adoption.
GSI_BACKFILL_ATTRIBUTES = (
    "ItemType",
    "ConfidenceAlertCount",
    "HITLPendingReview",
    "InitialEventTime",
)


def only_backfilled(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    """True when ``after`` is ``before`` plus GSI_BACKFILL_ATTRIBUTES it did not
    have: an attribute added, never one changed or removed."""
    if any(after.get(k) != v for k, v in before.items()):
        return False
    added = set(after) - set(before)
    return bool(added) and added <= set(GSI_BACKFILL_ATTRIBUTES)


def allowed_item_change(
    table_kind: str,
    before: Dict[str, Any],
    after: Dict[str, Any],
    restored: Iterable[str] = (),
) -> bool:
    """Whether a kept item's change is one the migration is meant to make.

    In the configuration table only:

    * ``fix-config`` removes CONFIG_COUNTERS from ``Config#`` rows, and nothing else
      about the row may differ;
    * a profile ``restore-config`` restored (``restored`` names them) has new
      content and counters by design; its content is checked through the API, so
      the row itself is not compared;
    * DEPLOYMENT_OWNED_CONFIG_ROWS are rewritten by every deploy.
    """
    if table_kind == "tracking_table":
        return only_backfilled(before, after)
    if table_kind != "configuration":
        return False
    config = (before.get("Configuration") or {}).get("S", "")
    if config in DEPLOYMENT_OWNED_CONFIG_ROWS:
        return True
    if not config.startswith("Config#"):
        return False
    if config[len("Config#") :] in set(restored):
        return True
    stripped = {k: v for k, v in before.items() if k not in CONFIG_COUNTERS}
    return stripped == after


def compare_items(
    table_kind: str,
    before: Dict[str, Dict[str, Any]],
    after: Dict[str, Dict[str, Any]],
    restored: Iterable[str] = (),
) -> Dict[str, Any]:
    """Items present before must be present after, unchanged except as
    :func:`allowed_item_change` allows. Items added since are counted."""
    missing = sorted(k for k in before if k not in after)
    changed, allowed = [], []
    for k, item in before.items():
        if k not in after or after[k] == item:
            continue
        if allowed_item_change(table_kind, item, after[k], restored):
            allowed.append(k)
        else:
            changed.append(
                {
                    "key": k,
                    "attributes": sorted(
                        a
                        for a in set(item) | set(after[k])
                        if item.get(a) != after[k].get(a)
                    ),
                }
            )
    added = sum(1 for k in after if k not in before)
    return {
        "missing": missing,
        "changed": changed,
        "allowedChanges": len(allowed),
        "added": added,
        "before": len(before),
    }


def table_kind(table_name: str, plan: Dict[str, Any]) -> str:
    for r in plan.get("resources", []):
        if r.get("physical_id") == table_name:
            if r["kind"] == "configuration_table":
                return "configuration"
            return r["kind"]
    return "unknown"


#: Profile fields a migration must not change. ``createdAt`` and ``status`` are
#: formatted by each deployment's own code, so they are not compared.
PROFILE_FIELDS = ("email", "persona", "allowedConfigVersions", "allowedTestSets")


def profile_differences(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    return [
        f"{f}: {before.get(f)!r} -> {after.get(f)!r}"
        for f in PROFILE_FIELDS
        if (before or {}).get(f) != (after or {}).get(f)
    ]


def owned_prefixes(run_name: str) -> Tuple[str, ...]:
    """The name prefixes a deployment called ``run_name`` gives what it creates:
    ``<name>-`` for buckets, tables, roles, pools and stacks, and the log-group
    paths Lambda, Step Functions, CodeBuild and the module use. Case-sensitive."""
    return (
        f"{run_name}-",
        f"/{run_name}/",
        f"/aws/lambda/{run_name}-",
        f"/aws/vendedlogs/states/{run_name}-",
        f"/aws/codebuild/{run_name}-",
    )


def owned_name(
    name: str, old_name: str, new_name: str, recorded: Iterable[str] = ()
) -> bool:
    """Whether the sweep may delete ``name``: recorded from this run's own states,
    or exactly one of the run's names, or starting with one of its
    :func:`owned_prefixes`. Never a substring match and never case-folded, so
    ``tfidp1-x`` is not ``idp1``'s and ``IDP1-x`` is not ``idp1``'s."""
    if name in set(recorded) or name in (old_name, new_name):
        return True
    return any(
        name.startswith(prefix)
        for run in (old_name, new_name)
        for prefix in owned_prefixes(run)
    )


#: Phases whose recorded result must be PASS before any later phase runs: a
#: resumed run never continues past a verification that failed, however the
#: context file came to list it as completed.
GATING_PHASES = ("verify",)


def stored_name_problems(
    ctx: Dict[str, Any],
    old_name: str,
    new_name: str,
    upstream_iam_path: Optional[str] = None,
    variant: str = BASE_VARIANT,
) -> List[str]:
    """Each run name given on the command line that differs from the one the work
    directory's run recorded. The upstream IAM path counts as a name: a resumed
    run under another path would plan every upstream role for replacement. So does
    the variant: a resumed run of another variant would apply a different old
    configuration over the one deployed. A run recorded before variants existed
    was the base one."""
    problems = []
    for key, given in (("oldName", old_name), ("newName", new_name)):
        stored = ctx.get(key)
        if stored and stored != given:
            problems.append(
                f"{key} is {given!r} here but {stored!r} in this work directory's run"
            )
    if "upstreamIamPath" in ctx and ctx["upstreamIamPath"] != upstream_iam_path:
        problems.append(
            f"the upstream IAM path is {upstream_iam_path!r} here but "
            f"{ctx['upstreamIamPath']!r} in this work directory's run"
        )
    stored_variant = ctx.get("variant", BASE_VARIANT) if "oldName" in ctx else variant
    if stored_variant != variant:
        problems.append(
            f"the variant is {variant!r} here but {stored_variant!r} in this work "
            "directory's run"
        )
    return problems


def resume_refusal(phase: str, ctx: Dict[str, Any]) -> Optional[str]:
    """Why ``phase`` must not run in this context, or None."""
    records = {p["phase"]: p for p in ctx.get("phases", [])}
    for gate in GATING_PHASES:
        if PHASES.index(phase) <= PHASES.index(gate):
            continue
        if phase in TEARDOWN_PHASES:
            continue  # tearing down is always allowed
        status = (records.get(gate) or {}).get("status")
        if status != "PASS":
            return (
                f"{gate} has not passed (recorded {status!r}); refusing to run {phase}"
            )
    return None


#: Resource types the test itself treats as kept, and the attribute naming each.
KEPT_DELETION_TYPES = {
    "aws_s3_bucket": ("bucket", "buckets"),
    "aws_dynamodb_table": ("name", "tables"),
    "aws_cognito_user_pool": ("id", "pools"),
}


def kept_deletions(
    destroy_plan: Dict[str, Any], targets: Dict[str, List[str]], old: Dict[str, Any]
) -> List[str]:
    """Each deletion in a destroy plan of a resource the test requires kept, matched
    by kind (a table named like a kept bucket is not a match)."""
    kept = {
        "buckets": set(targets.get("buckets", [])),
        "tables": set(targets.get("tables", [])),
        "pools": {old.get("user_pool_id")} - {None},
    }
    out = []
    for rc in destroy_plan.get("resource_changes", []):
        if "delete" not in (rc.get("change") or {}).get("actions", []):
            continue
        kind = KEPT_DELETION_TYPES.get(rc.get("type", ""))
        if not kind:
            continue
        name = ((rc["change"].get("before") or {}).get(kind[0])) or ""
        if name in kept[kind[1]]:
            out.append(f"{rc['address']} deletes {name}")
    return out


def managed_addresses(addresses: Iterable[str]) -> List[str]:
    """``terraform state list`` output without data sources, which a destroy
    leaves in no state and which hold nothing."""
    return [a for a in addresses if not a.startswith("data.") and ".data." not in a]


def summarise(result: Dict[str, Any]) -> str:
    """The human summary: the header, then the findings and leftovers, then one
    line per phase. Findings come first because chat posts keep only the start of
    the summary (the GitLab job cuts it at 2000 characters), and a phase table
    pushed the reason for a failure out of every post."""
    lines = [
        f"Terraform migration test: {result.get('status', 'UNKNOWN')}",
        f"  upstream {result.get('upstreamTag')} ({result.get('upstreamCommit', '')[:12]}), "
        f"old deployment {result.get('oldName')}, new deployment {result.get('newName')}, "
        f"region {result.get('region')}",
        f"  module {result.get('moduleUrl')}",
    ]
    findings = result.get("findings") or []
    if findings:
        lines += ["", "Findings:"] + [f"  - {f}" for f in findings]
    leftovers = result.get("leftovers")
    if leftovers is not None:
        lines += ["", f"Leftovers after teardown: {len(leftovers)}"] + [
            f"  - {x}" for x in leftovers
        ]
    lines += ["", "Phases:"]
    for p in result.get("phases", []):
        seconds = p.get("seconds")
        took = (
            f"{seconds / 60:6.1f} min"
            if isinstance(seconds, (int, float))
            else "       -  "
        )
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
    old_name: str
    new_name: str
    region: str
    work_dir: Path
    terraform_bin: str
    module_url: str
    upstream_repo: str = UPSTREAM_REPO
    #: A tag UPSTREAM_RELEASES pins, ROTATE_UPSTREAM_TAG, or None for the release
    #: the work directory's run recorded, else the newest (resolve_upstream_tag).
    upstream_tag: Optional[str] = None
    #: Overrides the release's pinned commit; None holds the clone to the pin.
    upstream_commit: Optional[str] = None
    assume_role_arn: Optional[str] = None
    keep: bool = False
    resume: bool = False
    stop_after: Optional[str] = None
    min_role_headroom: int = 400
    document_timeout: int = 2400
    accept_changes: Tuple[str, ...] = ()
    teardown_only: bool = False
    #: Seconds from the start of this invocation after which no forward phase
    #: starts, so that teardown fits inside an outer time limit (CI's build).
    deadline_seconds: Optional[int] = None
    expected_account: Optional[str] = None
    #: Both or neither: the IAM path and permissions boundary every upstream role
    #: is created with (see upstream_iam_overrides). CI passes both.
    upstream_iam_path: Optional[str] = None
    upstream_permissions_boundary_arn: Optional[str] = None
    #: ``s3://<bucket>/<prefix>/`` (see parse_state_mirror), or None for a run
    #: whose states stay in its work directory.
    state_mirror: Optional[str] = None
    #: One of VARIANTS: which old deployment the run migrates.
    variant: str = BASE_VARIANT


def first_party_pythonpath() -> str:
    """Every ``lib/*`` package root of this checkout, absolute (CLAUDE.md: a run
    measures the checkout it was started from only when the pin names them all)."""
    roots = sorted(str(p.parent) for p in (REPO_ROOT / "lib").glob("*/pyproject.toml"))
    return os.pathsep.join(roots)


class Run:
    def __init__(self, settings: Settings):
        self.s = settings
        self.work = settings.work_dir
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "logs").mkdir(exist_ok=True)
        (self.work / "phases").mkdir(exist_ok=True)
        self.context_path = self.work / "context.json"
        self.ctx: Dict[str, Any] = {}
        if settings.resume and self.context_path.is_file():
            self.ctx = json.loads(self.context_path.read_text())
        self.ctx.setdefault("completed", [])
        self.ctx.setdefault("phases", [])
        self.ctx.setdefault("findings", [])
        if settings.resume and self.ctx["findings"]:
            # Findings of an earlier attempt are history, not this attempt's verdict.
            self.ctx.setdefault("previousFindings", []).extend(self.ctx["findings"])
            self.ctx["findings"] = []
        self.ctx.setdefault(
            "runId", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        self.secrets_path = self.work / "secrets.json"
        self._boto_cache: Dict[str, Any] = {}
        self.started = time.monotonic()
        self.preflight_passed = False
        self._mirror_cleared = False
        #: Set by resolve_release, before any phase runs.
        self.release: Optional[UpstreamRelease] = None

    # -- plumbing --------------------------------------------------------------

    def resolve_release(self) -> Optional[str]:
        """Fix the upstream release this invocation uses (resolve_upstream_tag)
        and record it in the context; why it cannot, or None."""
        tag, problem = resolve_upstream_tag(
            self.s.upstream_tag,
            self.ctx.get("upstreamTag"),
            datetime.now(timezone.utc).date(),
        )
        if problem or tag is None:
            return problem or "no upstream release"
        self.release = upstream_release(tag)
        self.ctx["upstreamTag"] = tag
        return None

    @property
    def upstream(self) -> UpstreamRelease:
        if self.release is None:
            raise PhaseFailed("the upstream release was not resolved")
        return self.release

    @property
    def old_dir(self) -> Path:
        return self.work / "upstream" / UPSTREAM_EXAMPLE

    @property
    def new_dir(self) -> Path:
        return self.work / "new" / self.s.new_name

    def save(self) -> None:
        self.save_local()
        if self.s.state_mirror and not self._mirror_cleared:
            try:
                self.upload_mirror()
            except Exception as e:  # noqa: BLE001 - preflight proved it writable
                print(f"  ⚠ could not update the state mirror: {e}", flush=True)

    def save_local(self) -> None:
        tmp = self.context_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.ctx, indent=1, default=str))
        tmp.replace(self.context_path)

    # -- the state mirror --------------------------------------------------------------

    def upload_mirror(self) -> None:
        """Copy the context and the new root's configuration to the mirror. Never
        secrets.json: a teardown needs names, not passwords."""
        bucket, prefix = parse_state_mirror(self.s.state_mirror or "")
        s3 = self.boto("s3")
        s3.put_object(
            Bucket=bucket,
            Key=prefix + MIRROR_CONTEXT,
            Body=self.context_path.read_bytes(),
            ServerSideEncryption="AES256",
        )
        for f in new_root_files(self.new_dir):
            s3.put_object(
                Bucket=bucket,
                Key=prefix + MIRROR_NEW_ROOT + f.name,
                Body=f.read_bytes(),
                ServerSideEncryption="AES256",
            )

    def download_mirror_context(self) -> bool:
        """Load the context from the mirror into this work directory. False when
        the mirror holds none."""
        bucket, prefix = parse_state_mirror(self.s.state_mirror or "")
        try:
            body = (
                self.boto("s3")
                .get_object(Bucket=bucket, Key=prefix + MIRROR_CONTEXT)["Body"]
                .read()
            )
        except Exception as e:  # noqa: BLE001 - reported as a refusal
            print(f"  could not read the mirrored context: {e}", flush=True)
            return False
        self.context_path.write_bytes(body)
        self.ctx = json.loads(body)
        for key in ("completed", "phases", "findings"):
            self.ctx.setdefault(key, [])
        return True

    def download_new_root(self) -> List[str]:
        bucket, prefix = parse_state_mirror(self.s.state_mirror or "")
        s3 = self.boto("s3")
        names = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix + MIRROR_NEW_ROOT):
            for item in page.get("Contents", []):
                name = item["Key"][len(prefix + MIRROR_NEW_ROOT) :]
                if not name or "/" in name or "tfstate" in name:
                    continue
                self.new_dir.mkdir(parents=True, exist_ok=True)
                body = s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
                write_private(self.new_dir / name, body.decode())
                names.append(name)
        return names

    def clear_mirror(self) -> None:
        """Delete every object and version under the mirror prefix, the states
        included. Only once a teardown found nothing left: until then the mirror
        is what lets a later build try again."""
        bucket, prefix = parse_state_mirror(self.s.state_mirror or "")
        s3 = self.boto("s3")
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
                s3.delete_object(
                    Bucket=bucket, Key=item["Key"], VersionId=item["VersionId"]
                )
        self._mirror_cleared = True
        print(f"  state mirror s3://{bucket}/{prefix} removed", flush=True)

    def write_backend(self, directory: Path, filename: str, which: str) -> None:
        bucket, prefix = parse_state_mirror(self.s.state_mirror or "")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(
            render_backend(bucket, f"{prefix}{which}/terraform.tfstate", self.s.region)
        )

    def restore_for_teardown(self) -> List[str]:
        """Rebuild what the teardown phases read, for a work directory that is not
        the one the run used: the pinned upstream clone with the run's tfvars, IAM
        overrides, backend and provider lock, and the new root from the mirror;
        each initialised. Returns what was restored."""
        restored = []
        old_initialised = (self.old_dir / ".terraform").is_dir()
        if self.ctx.get("oldDeployStarted") and not old_initialised:
            if not (self.work / "upstream" / ".git").is_dir():
                self.phase_upstream_clone()
            self.prepare_old_root()
            self.terraform(
                self.old_dir, "init", "-input=false", log="restore-old-init.log"
            )
            restored.append("old root")
        if not (self.new_dir / "main.tf").is_file() and self.download_new_root():
            self.terraform(
                self.new_dir, "init", "-input=false", log="restore-new-init.log"
            )
            restored.append("new root")
        return restored

    def boto(self, service: str):
        if service not in self._boto_cache:
            import boto3

            self._boto_cache[service] = boto3.client(service, region_name=self.s.region)
        return self._boto_cache[service]

    def env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = first_party_pythonpath()
        env["AWS_REGION"] = self.s.region
        env["AWS_DEFAULT_REGION"] = self.s.region
        env["TF_IN_AUTOMATION"] = "1"
        env["TF_INPUT"] = "0"
        env["CHECKPOINT_DISABLE"] = "1"
        cache = self.work / ".terraform-plugin-cache"
        cache.mkdir(exist_ok=True)
        env["TF_PLUGIN_CACHE_DIR"] = str(cache)
        return env

    def sh(
        self,
        cmd: Sequence[str],
        log: str,
        cwd: Optional[Path] = None,
        timeout: int = 3 * 3600,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a command; stdout is returned, stderr appended to ``logs/<log>``."""
        log_path = self.work / "logs" / log
        with log_path.open("a") as err:
            err.write(
                f"\n$ {' '.join(cmd)}  # {datetime.now(timezone.utc).isoformat()}\n"
            )
            err.flush()
            proc = subprocess.run(
                list(cmd),
                cwd=str(cwd or self.work),
                env=self.env(),
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
                timeout=timeout,
                check=False,
            )
        if check and proc.returncode != 0:
            raise PhaseFailed(
                f"`{' '.join(cmd[:6])} ...` exited {proc.returncode}; see {log_path}"
            )
        return proc

    def terraform(
        self,
        directory: Path,
        *args: str,
        log: str,
        timeout: int = 3 * 3600,
        check: bool = True,
    ):
        """Terraform in ``directory``. Its human output goes to the log too."""
        log_path = self.work / "logs" / log
        with log_path.open("a") as out:
            out.write(
                f"\n$ terraform {' '.join(args)}  # {datetime.now(timezone.utc).isoformat()}\n"
            )
            out.flush()
            proc = subprocess.run(
                [self.s.terraform_bin, *args],
                cwd=str(directory),
                env=self.env(),
                stdout=out,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
        if check and proc.returncode != 0:
            raise PhaseFailed(
                f"terraform {args[0]} in {directory.name} exited {proc.returncode}; "
                f"see {log_path}; last lines:\n{log_tail(log_path)}"
            )
        return proc

    def terraform_capture(self, directory: Path, *args: str) -> str:
        proc = subprocess.run(
            [self.s.terraform_bin, *args],
            cwd=str(directory),
            env=self.env(),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise PhaseFailed(
                f"terraform {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()[-800:]}"
            )
        return proc.stdout

    def idp_cli(
        self, *args: str, log: str, timeout: int = 3 * 3600, check: bool = True
    ) -> Tuple[int, Any]:
        """``idp-cli`` from this checkout. Returns (exit code, parsed --json output
        or None)."""
        cmd = [sys.executable, "-m", "idp_cli.cli", *args]
        proc = self.sh(cmd, log, timeout=timeout, check=False)
        parsed = None
        text = (proc.stdout or "").strip()
        if "--json" in args and text:
            try:
                parsed = json.loads(text[text.index("{") :])
            except ValueError:
                parsed = None
        if check and proc.returncode != 0:
            detail = ""
            if isinstance(parsed, dict) and parsed.get("findings"):
                detail = ": " + "; ".join(parsed["findings"])[:1500]
            raise PhaseFailed(
                f"idp-cli {' '.join(args[:3])} exited {proc.returncode}{detail}; see logs/{log}"
            )
        return proc.returncode, parsed if parsed is not None else proc.stdout

    def migrate(
        self,
        phase: str,
        *args: str,
        log: Optional[str] = None,
        timeout: int = 3 * 3600,
        label: Optional[str] = None,
    ) -> Dict[str, Any]:
        """One phase of the migrate command, as the guide spells it, with --json."""
        common = ["--stack-name", self.s.new_name, "--region", self.s.region]
        code, out = self.idp_cli(
            "terraform",
            "migrate-from-genai-idp-terraform",
            phase,
            *common,
            *args,
            "--json",
            log=log or f"migrate-{label or phase}.log",
            timeout=timeout,
            check=False,
        )
        (self.work / "phases" / f"{label or phase}.json").write_text(
            json.dumps(out, indent=1, default=str)
            if isinstance(out, dict)
            else str(out)
        )
        if code != 0:
            findings = out.get("findings") if isinstance(out, dict) else None
            raise PhaseFailed(
                f"`migrate {phase}` exited {code}"
                + (
                    ": " + "; ".join(findings)[:2000]
                    if findings
                    else f"; see logs/{log or f'migrate-{phase}.log'}"
                )
            )
        return out if isinstance(out, dict) else {"raw": out}

    def finding(self, text: str) -> None:
        self.ctx["findings"].append(text)
        print(f"  ✗ {text}", flush=True)

    # -- the Region's API Gateway CloudWatch role ------------------------------

    def role_exists(self, role_arn: str) -> Optional[bool]:
        """Whether the IAM role ``role_arn`` names exists; None when that cannot
        be read."""
        try:
            self.boto("iam").get_role(RoleName=iam_role_name(role_arn))
            return True
        except Exception as e:  # noqa: BLE001 - classified below
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            return False if code == "NoSuchEntity" else None

    def api_gateway_role(self) -> Dict[str, Any]:
        """The Region's API Gateway CloudWatch role setting (``""`` when unset) and
        whether the role it names exists (None when unset or unreadable)."""
        arn = self.boto("apigateway").get_account().get("cloudwatchRoleArn") or ""
        return {"arn": arn, "exists": self.role_exists(arn) if arn else None}

    # -- secrets -----------------------------------------------------------------

    def secrets(self) -> Dict[str, Any]:
        if self.secrets_path.is_file():
            return json.loads(self.secrets_path.read_text())
        return {}

    def save_secrets(self, data: Dict[str, Any]) -> None:
        # write_private re-applies 0600 to a file that already existed.
        write_private(self.secrets_path, json.dumps(data))

    # -- phases ---------------------------------------------------------------------

    def identity_problems(self) -> List[str]:
        """Why this invocation must not act on this work directory: the names are
        unusable, differ from the ones the work directory's run used, or the
        credentials are for another account than it ran in."""
        problems = validate_names(self.s.old_name, self.s.new_name)
        problems += stored_name_problems(
            self.ctx,
            self.s.old_name,
            self.s.new_name,
            self.s.upstream_iam_path,
            self.s.variant,
        )
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

    def phase_preflight(self) -> str:
        self.preflight_passed = False
        problems = self.identity_problems()
        if problems:
            raise PhaseFailed("; ".join(problems))
        if self.s.variant in MCP_VARIANTS and not shutil.which("pip3"):
            # Upstream builds its AgentCore gateway manager with `pip3 install` in a
            # local-exec, whatever build.lambda_local says.
            raise PhaseFailed(
                "the mcp-datasets variant needs pip3 on PATH: upstream's MCP "
                "integration builds its gateway manager with it"
            )
        ident = {"Account": self._account}
        self.ctx["account"] = ident["Account"]
        if self.s.expected_account and ident["Account"] != self.s.expected_account:
            raise PhaseFailed(
                f"credentials are for account {ident['Account']}, not {self.s.expected_account}"
            )
        summary = self.boto("iam").get_account_summary()["SummaryMap"]
        headroom = summary["RolesQuota"] - summary["Roles"]
        if headroom < self.s.min_role_headroom:
            raise PhaseFailed(
                f"only {headroom} IAM roles below the account quota; the two deployments "
                f"need about {self.s.min_role_headroom}"
            )
        version = self.terraform_capture(self.work, "version", "-json")
        self.ctx["terraformVersion"] = json.loads(version)["terraform_version"]
        if self.s.state_mirror:
            # A run that cannot write its mirror could not be torn down by a later
            # build if this one died, so it creates nothing.
            self.save_local()
            try:
                self.upload_mirror()
            except Exception as e:  # noqa: BLE001
                raise PhaseFailed(
                    f"cannot write the state mirror {self.s.state_mirror}: {e}"
                ) from e
        if not self.ctx.get("oldDeployStarted"):
            clashes = self.existing_matches()
            if clashes:
                raise PhaseFailed(
                    "resources already carrying this run's names exist, so the sweep could "
                    "reach something this run did not create: "
                    + ", ".join(clashes[:10])
                )
        if "apiGatewayRoleAtStart" not in self.ctx:
            # Before upstream's deploy, which sets the Region's role to its own:
            # what leftover-check compares the end of the run against.
            try:
                self.ctx["apiGatewayRoleAtStart"] = self.api_gateway_role()
            except Exception as e:  # noqa: BLE001 - reported as the refusal
                raise PhaseFailed(
                    f"cannot read the Region's API Gateway account settings: {e}"
                ) from e
        self.preflight_passed = True
        return f"account ok, {headroom} roles of headroom, terraform {self.ctx['terraformVersion']}"

    def existing_matches(self) -> List[str]:
        """Everything already in the account that the sweep would select for this
        run's names: the same inventory the sweep and the leftover check use, so
        preflight cannot check fewer kinds than the sweep deletes."""
        return Sweeper(self, {}).inventory()

    def _user_pools(self) -> List[Dict[str, Any]]:
        out = []
        for page in (
            self.boto("cognito-idp")
            .get_paginator("list_user_pools")
            .paginate(MaxResults=60)
        ):
            out += page.get("UserPools", [])
        return out

    def phase_upstream_clone(self) -> str:
        target = self.work / "upstream"
        if not (target / ".git").is_dir():
            self.sh(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    self.upstream.tag,
                    self.s.upstream_repo,
                    str(target),
                ],
                "upstream-clone.log",
            )
        head = self.sh(
            ["git", "rev-parse", "HEAD"], "upstream-clone.log", cwd=target
        ).stdout.strip()
        release = self.upstream
        commit = self.s.upstream_commit or release.commit
        if head != commit:
            raise PhaseFailed(
                f"{release.tag} resolves to {head}, not the pinned {commit}; "
                "the tag has moved, so read the new commit before pinning it"
            )
        kind, sources = release.sources
        pinned = self.sh(
            ["git", "ls-tree", "HEAD", "sources"], "upstream-clone.log", cwd=target
        ).stdout.split()
        if pinned[1:3] != [kind, sources]:
            raise PhaseFailed(
                f"upstream has sources/ as {' '.join(pinned[1:3]) or 'nothing'}, "
                f"not the pinned {kind} {sources}"
            )
        if kind == "commit":
            # Upstream's README: `sources/` is a submodule, initialised after the
            # clone. A vendored tree is already checked out at the pinned commit.
            self.sh(
                ["git", "submodule", "update", "--init", "--depth", "1", "sources"],
                "upstream-clone.log",
                cwd=target,
                timeout=1800,
            )
            checked_out = self.sh(
                ["git", "rev-parse", "HEAD"],
                "upstream-clone.log",
                cwd=target / "sources",
            ).stdout.strip()
            if checked_out != sources:
                raise PhaseFailed(f"sources/ checked out {checked_out}, not {sources}")
        self.ctx["upstreamCommit"] = head
        return f"{release.tag} at {head[:12]}, sources/ {kind} {sources[:12]}"

    def prepare_old_root(self) -> str:
        """Write what the upstream example's root needs beside upstream's own
        files: the tfvars, the IAM overrides, the backend and the provider lock.
        The same for the deploy and for a teardown in a rebuilt work directory."""
        tfvars = upstream_tfvars(
            self.s.old_name, self.ctx["runId"], self.s.region, self.s.variant
        )
        (self.old_dir / "terraform.tfvars").write_text(tfvars)
        if self.s.variant == MCP_DATASETS_VARIANT:
            try:
                override = datasets_override((self.old_dir / "main.tf").read_text())
            except ValueError as e:
                raise PhaseFailed(
                    f"cannot turn the test datasets on in the upstream example: {e}"
                ) from e
            (self.old_dir / DATASETS_OVERRIDE_FILE).write_text(override)
        if self.s.state_mirror:
            self.write_backend(self.old_dir, OLD_BACKEND_FILE, "old")
        lock = self.old_dir / ".terraform.lock.hcl"
        if not lock.exists():
            shutil.copyfile(UPSTREAM_LOCK_FILE, lock)
        overridden = ""
        if self.s.upstream_iam_path:
            files = upstream_iam_overrides(
                self.work / "upstream",
                self.s.upstream_iam_path,
                self.s.upstream_permissions_boundary_arn or "",
            )
            if not files:
                raise PhaseFailed(
                    "found no upstream aws_iam_role to put under the run's boundary; "
                    "upstream's layout has changed"
                )
            for path, text in files.items():
                path.write_text(text)
            count = sum(
                text.count('resource "aws_iam_role"') for text in files.values()
            )
            overridden = f", {count} roles under {self.s.upstream_iam_path}"
        return overridden

    def phase_upstream_deploy(self) -> str:
        overridden = self.prepare_old_root()
        self.terraform(self.old_dir, "init", "-input=false", log="old-init.log")
        self.terraform(
            self.old_dir, "plan", "-input=false", "-out=old.tfplan", log="old-plan.log"
        )
        self.ctx["oldDeployStarted"] = True
        self.ctx["created"] = True
        self.save()
        self.terraform(
            self.old_dir, "apply", "-input=false", "old.tfplan", log="old-apply.log"
        )
        (self.old_dir / "old.tfplan").unlink(missing_ok=True)
        outputs = json.loads(self.terraform_capture(self.old_dir, "output", "-json"))
        self.ctx["old"] = outputs["e2e_stack"]["value"]
        write_private(self.work / "old-outputs.json", json.dumps(outputs, indent=1))
        return f"name prefix {self.ctx['old']['name_prefix']}{overridden}"

    # Seeding -----------------------------------------------------------------------

    def api(self, base: str, token: str, op: str, args: Dict[str, Any]) -> Any:
        """POST ``<base>/op/<op>``: the REST dispatcher both deployments share."""
        req = urllib.request.Request(
            f"{base}/op/{op}",
            data=json.dumps({"arguments": args}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": token},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:  # nosec B310 - the deployment's own API
                body = json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raise PhaseFailed(f"{op} answered HTTP {e.code}: {e.read()[:500]!r}") from e
        if isinstance(body, dict) and body.get("errors"):
            raise PhaseFailed(f"{op} answered {json.dumps(body['errors'])[:500]}")
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    def sign_in(
        self, pool_id: str, client_id: str, username: str, password: str
    ) -> str:
        result, _ = cognito_srp.srp_sign_in(
            self.boto("cognito-idp"), pool_id, client_id, username, password
        )
        return result["IdToken"]

    def wait_documents(
        self, table: str, keys: Sequence[str], since: Optional[str] = None
    ) -> Dict[str, str]:
        """Poll the tracking table until every key reaches a final status."""
        ddb = self.boto("dynamodb")
        deadline = time.monotonic() + self.s.document_timeout
        statuses: Dict[str, str] = {}
        while True:
            for key in keys:
                item = ddb.get_item(
                    TableName=table,
                    Key={"PK": {"S": f"doc#{key}"}, "SK": {"S": "none"}},
                    ConsistentRead=True,
                ).get("Item")
                statuses[key] = (
                    (item or {}).get("ObjectStatus", {}).get("S", "NOT_TRACKED")
                )
            if all(s in FINAL_STATUSES for s in statuses.values()):
                return statuses
            if time.monotonic() > deadline:
                raise PhaseFailed(
                    f"documents not finished after {self.s.document_timeout}s: {statuses}"
                )
            time.sleep(20)

    def phase_seed(self) -> str:
        old = self.ctx["old"]
        cognito = self.boto("cognito-idp")
        s3 = self.boto("s3")
        pool, client, api = (
            old["user_pool_id"],
            old["user_pool_client_id"],
            old["api_base_url"],
        )
        sec = self.secrets()
        users = sec.get("users") or [
            {
                "username": f"tfmig-{self.s.old_name}-admin@example.com",
                "persona": "Admin",
                "password": make_password(),
            },
            {
                "username": f"tfmig-{self.s.old_name}-author@example.com",
                "persona": "Author",
                "password": make_password(),
                "allowedConfigVersions": [SCOPED_CONFIG_VERSION],
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

        # The Author, through the old deployment's own API, so its users-table row
        # and scope are written by upstream's code in upstream's shape.
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
        groups = [
            g["GroupName"]
            for g in cognito.admin_list_groups_for_user(
                UserPoolId=pool, Username=author["username"]
            )["Groups"]
        ]
        if "Author" not in groups:
            raise PhaseFailed(f"createUser put the Author in {groups}, not Author")

        # Two documents, processed by the old deployment.
        keys = []
        for rel in SAMPLE_DOCUMENTS:
            key = SEED_PREFIX + Path(rel).name
            s3.upload_file(
                str(REPO_ROOT / rel),
                old["input_bucket"],
                key,
                ExtraArgs={"Metadata": {"config-version": SCOPED_CONFIG_VERSION}},
            )
            keys.append(key)
        statuses = self.wait_documents(old["documents_table"], keys)
        if any(v != "COMPLETED" for v in statuses.values()):
            raise PhaseFailed(
                f"the old deployment did not complete the seed documents: {statuses}"
            )

        # One configuration edit, through the API as the web UI makes it.
        marker = f"terraform-migration-test {self.ctx['runId']}"
        current = self.api(
            api, token, "getConfigVersion", {"versionName": SCOPED_CONFIG_VERSION}
        )
        custom = _json_field(current, "Custom") or {}
        custom["notes"] = marker
        updated = self.api(
            api,
            token,
            "updateConfiguration",
            {
                "versionName": SCOPED_CONFIG_VERSION,
                "customConfig": json.dumps(custom),
                "description": "edited by the Terraform migration test",
            },
        )
        if not _success(updated):
            raise PhaseFailed(
                f"updateConfiguration answered {json.dumps(updated)[:400]}"
            )
        reread = self.api(
            api, token, "getConfigVersion", {"versionName": SCOPED_CONFIG_VERSION}
        )
        if marker not in json.dumps(reread):
            raise PhaseFailed(
                "the configuration edit is not visible through getConfigVersion"
            )
        self.ctx["configMarker"] = marker

        # An evaluation baseline and a test set, through the API.
        self.api(api, token, "copyToBaseline", {"objectKey": keys[0]})
        baseline_bucket = self._old_bucket("evaluation_baseline_bucket")
        self._wait_prefix(baseline_bucket, keys[0] + "/", "baseline copy")
        test_set = self.api(
            api,
            token,
            "addTestSet",
            {
                "name": f"{self.s.old_name}-seed-set",
                "description": "Terraform migration test seed",
                "filePattern": SEED_PREFIX + "*",
                "bucketType": "input",
                "fileCount": len(keys),
            },
        )
        test_set_id = _find_value(test_set, "id")
        if not test_set_id:
            raise PhaseFailed(
                f"addTestSet returned no id: {json.dumps(test_set)[:300]}"
            )
        self._wait_prefix(
            self._old_bucket("test_sets"), f"{test_set_id}/", "test set copy"
        )
        self.ctx["testSetId"] = test_set_id

        # Each user's profile, as the old deployment reports it.
        profiles = {}
        for user in users:
            t = self.sign_in(pool, client, user["username"], user["password"])
            profiles[user["username"]] = (
                _find_value(self.api(api, t, "getMyProfile", {}), None) or {}
            )
        self.ctx["profilesBefore"] = profiles
        if (
            profiles[author["username"]].get("allowedConfigVersions")
            != author["allowedConfigVersions"]
        ):
            raise PhaseFailed(
                f"the Author's scope is {profiles[author['username']].get('allowedConfigVersions')!r} "
                "on the old deployment itself"
            )
        self.ctx["seedKeys"] = keys
        return f"{len(keys)} documents COMPLETED, 2 users, 1 config edit, baseline and test set {test_set_id}"

    def _old_bucket(self, name: str) -> str:
        state = self.old_state()
        for inst in state_instances(state):
            if inst["type"] == "aws_s3_bucket" and inst["name"] == name:
                return inst["attributes"]["bucket"]
        raise PhaseFailed(f"the old state has no aws_s3_bucket.{name}")

    def _wait_prefix(
        self, bucket: str, prefix: str, what: str, timeout: int = 900
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (
                self.boto("s3")
                .list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
                .get("KeyCount")
            ):
                return
            time.sleep(15)
        raise PhaseFailed(
            f"the {what} wrote nothing under s3://{bucket}/{prefix} in {timeout}s"
        )

    def old_state(self) -> Dict[str, Any]:
        return json.loads(self.terraform_capture(self.old_dir, "state", "pull") or "{}")

    # Snapshot -----------------------------------------------------------------------

    def snapshot_pool(self) -> str:
        """The user pool :meth:`snapshot` reads. Here it is the old deployment's,
        which the new one adopts; the upgrade test
        (``scripts/sdlc/terraform_upgrade_test.py``), which reuses this snapshot,
        reads its one deployment's instead."""
        return self.ctx["old"]["user_pool_id"]

    def snapshot(self, label: str) -> Dict[str, Any]:
        """Objects, items, users and groups of every bucket and table the old
        state holds that REQUIRED_KEPT or the plan keeps."""
        names = self.snapshot_targets()
        s3, ddb, cognito = (
            self.boto("s3"),
            self.boto("dynamodb"),
            self.boto("cognito-idp"),
        )
        snap: Dict[str, Any] = {"buckets": {}, "tables": {}, "users": {}, "groups": {}}
        for bucket in names["buckets"]:
            objects = {}
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                for o in page.get("Contents", []):
                    objects[o["Key"]] = {"etag": o["ETag"], "size": o["Size"]}
            snap["buckets"][bucket] = objects
        for table in names["tables"]:
            keys = [
                k["AttributeName"]
                for k in ddb.describe_table(TableName=table)["Table"]["KeySchema"]
            ]
            items = {}
            for page in ddb.get_paginator("scan").paginate(
                TableName=table, ConsistentRead=True
            ):
                for item in page.get("Items", []):
                    items[item_key(item, keys)] = item
            snap["tables"][table] = items
        pool = self.snapshot_pool()
        for page in cognito.get_paginator("list_users").paginate(UserPoolId=pool):
            for u in page["Users"]:
                sub = next(
                    (a["Value"] for a in u.get("Attributes", []) if a["Name"] == "sub"),
                    None,
                )
                snap["users"][u["Username"]] = {
                    "status": u["UserStatus"],
                    "sub": sub,
                    "enabled": u["Enabled"],
                }
        for group in ("Admin", "Author", "Reviewer", "Viewer"):
            members = []
            for page in cognito.get_paginator("list_users_in_group").paginate(
                UserPoolId=pool, GroupName=group
            ):
                members += [u["Username"] for u in page["Users"]]
            snap["groups"][group] = sorted(members)
        # Binary attributes as base64, so a snapshot read back from its file
        # compares equal to one taken in memory.
        snap = json.loads(json.dumps(snap, default=_json_default))
        write_private(self.work / f"snapshot-{label}.json", json.dumps(snap))
        return snap

    def required_targets(self) -> Dict[str, List[str]]:
        """The buckets and tables of the old state that REQUIRED_KEPT names."""
        state = self.old_state()
        write_private(self.work / "old-before.tfstate", json.dumps(state))
        self.ctx["oldNames"] = merge_names(
            self.ctx.get("oldNames") or {}, physical_names(state)
        )
        targets: Dict[str, set] = {"buckets": set(), "tables": set()}
        for inst in state_instances(state):
            key = (inst["type"], inst["name"])
            if key in REQUIRED_KEPT and inst["type"] == "aws_s3_bucket":
                targets["buckets"].add(inst["attributes"]["bucket"])
            if key in REQUIRED_KEPT and inst["type"] == "aws_dynamodb_table":
                targets["tables"].add(inst["attributes"]["name"])
        return {k: sorted(v) for k, v in targets.items()}

    def snapshot_targets(self) -> Dict[str, List[str]]:
        """Every bucket and table the migration keeps: REQUIRED_KEPT (the test's own
        list, so a plan that drops one cannot shrink the comparison) united with
        everything the plan adopts, once there is a plan."""
        if "snapshotTargets" not in self.ctx:
            targets = self.required_targets()
            plan_file = (
                self.new_dir / "genai-idp-terraform-migration" / "migration-plan.json"
            )
            if plan_file.is_file():
                kept = kept_resources(json.loads(plan_file.read_text()))
                targets = {
                    k: sorted(set(targets[k]) | set(kept.get(k, []))) for k in targets
                }
            self.ctx["snapshotTargets"] = targets
        return self.ctx["snapshotTargets"]

    def phase_snapshot_before(self) -> str:
        """Taken after ``freeze``, and only once the old deployment is quiet: it
        keeps writing to a COMPLETED document's rows and manifest for minutes
        (measured: evaluation after the baseline copy), and a snapshot taken during
        that reads those writes as changes the migration made."""
        self.snapshot_targets()
        previous = None
        for _attempt in range(SETTLE_ATTEMPTS):
            snap = self.snapshot("before")
            if previous is not None and _comparable(snap) == _comparable(previous):
                break
            previous = snap
            time.sleep(SETTLE_SECONDS)
        else:
            raise PhaseFailed(
                f"the old deployment's data kept changing for "
                f"{SETTLE_ATTEMPTS * SETTLE_SECONDS // 60} minutes after the freeze"
            )
        objects = sum(len(v) for v in snap["buckets"].values())
        items = sum(len(v) for v in snap["tables"].values())
        return f"{len(snap['buckets'])} buckets ({objects} objects), {len(snap['tables'])} tables ({items} items), {len(snap['users'])} users"

    # Migration phases -------------------------------------------------------------------

    # The guide's manual procedure for MCP and the test datasets --------------------

    def dataset_snapshot(self, bucket: str, table: str) -> Dict[str, Any]:
        """Each dataset's tracking-table record and every object under its prefix,
        with the time it was written, so that a re-download (identical content,
        hence an identical ETag) still shows."""
        ddb, s3 = self.boto("dynamodb"), self.boto("s3")
        out: Dict[str, Any] = {}
        for test_set, prefix in DATASETS:
            item = ddb.get_item(
                TableName=table,
                Key={"PK": {"S": f"testset#{test_set}"}, "SK": {"S": "metadata"}},
                ConsistentRead=True,
            ).get("Item")
            objects: Dict[str, List[str]] = {}
            for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=prefix
            ):
                for o in page.get("Contents", []):
                    objects[o["Key"]] = [o["ETag"], o["LastModified"].isoformat()]
            out[test_set] = {"record": item, "objects": objects}
        return out

    def dataset_problems(self, snap: Dict[str, Any]) -> List[str]:
        """Why a dataset snapshot shows nothing to keep."""
        problems = []
        for test_set, _prefix in DATASETS:
            record = snap[test_set]["record"]
            if not record:
                problems.append(f"no testset#{test_set} record")
            elif (record.get("status") or {}).get("S") == "FAILED":
                problems.append(f"testset#{test_set} is FAILED")
            if not snap[test_set]["objects"]:
                problems.append(f"no {test_set} objects")
        return problems

    def phase_pre_plan_features(self) -> str:
        """The guide's MCP and test-dataset procedures: one targeted destroy of the
        MCP integration and both dataset stacks in the old directory, checked as the
        guide says to check it, then checks that it removed the gateway and the
        resource server and left every dataset record and file as it was."""
        if self.s.variant not in MCP_VARIANTS:
            return f"nothing to do for the {self.s.variant} variant"
        with_datasets = self.s.variant == MCP_DATASETS_VARIANT
        targets = (
            FEATURE_DESTROY_TARGETS if with_datasets else FEATURE_DESTROY_TARGETS[:1]
        )
        insts = state_instances(self.old_state())
        stacks = {
            i["address"]: i["attributes"]
            for i in insts
            if i["type"] == "aws_cloudformation_stack"
        }
        gateway_stack = f"{FEATURE_DESTROY_TARGETS[0]}.aws_cloudformation_stack.agentcore_gateway[0]"
        wanted = {gateway_stack, *targets[1:]}
        if set(stacks) != wanted:
            raise PhaseFailed(
                "the old state's CloudFormation stacks are not the ones the variant "
                f"deploys: missing {sorted(wanted - set(stacks))}, "
                f"unexpected {sorted(set(stacks) - wanted)}"
            )
        gateway_id = (stacks[gateway_stack].get("outputs") or {}).get("GatewayId")
        if not gateway_id:
            raise PhaseFailed(f"{gateway_stack} records no GatewayId output")
        pool_id = self.ctx["old"]["user_pool_id"]
        cognito = self.boto("cognito-idp")
        cognito.describe_resource_server(
            UserPoolId=pool_id, Identifier=MCP_RESOURCE_SERVER
        )

        before: Dict[str, Any] = {}
        if with_datasets:
            bucket, table = self.dataset_location(insts)
            before = self.dataset_snapshot(bucket, table)
            empty = self.dataset_problems(before)
            if empty:
                raise PhaseFailed(
                    "the old deployment's datasets did not deploy, so the variant "
                    "would prove nothing about keeping them: " + "; ".join(empty)
                )
            write_private(
                self.work / "datasets-before.json", json.dumps(before, default=str)
            )
            self.ctx["datasets"] = {"bucket": bucket, "table": table}

        plan_file = "features-destroy.tfplan"
        self.terraform(
            self.old_dir,
            "plan",
            "-destroy",
            "-input=false",
            f"-out={plan_file}",
            *(f"-target={t}" for t in targets),
            log="old-features-destroy.log",
        )
        plan = json.loads(
            self.terraform_capture(self.old_dir, "show", "-json", plan_file)
        )
        destroyed = sorted(
            rc["address"]
            for rc in plan.get("resource_changes", [])
            if (rc.get("change") or {}).get("actions") == ["delete"]
        )
        self.ctx["featureDestroy"] = destroyed
        self.save()
        problems = feature_destroy_problems(plan)
        if problems:
            (self.old_dir / plan_file).unlink(missing_ok=True)
            raise PhaseFailed(
                "the targeted destroy goes beyond what the guide allows: "
                + "; ".join(problems)
            )
        self.terraform(
            self.old_dir,
            "apply",
            "-input=false",
            plan_file,
            log="old-features-destroy.log",
        )
        (self.old_dir / plan_file).unlink(missing_ok=True)

        findings: List[str] = []
        left = sorted(
            i["address"]
            for i in state_instances(self.old_state())
            if i["type"] == "aws_cloudformation_stack"
        )
        if left:
            findings.append(f"the old state still holds stacks {left}")
        cf = self.boto("cloudformation")
        for address, attrs in stacks.items():
            try:
                status = cf.describe_stacks(StackName=attrs["id"])["Stacks"][0][
                    "StackStatus"
                ]
            except Exception:  # noqa: BLE001 - gone
                status = "DELETE_COMPLETE"
            if status != "DELETE_COMPLETE":
                findings.append(f"stack {attrs.get('name')} is {status}")
        try:
            self.boto("bedrock-agentcore-control").get_gateway(
                gatewayIdentifier=gateway_id
            )
            findings.append(f"the old AgentCore gateway {gateway_id} still exists")
        except Exception as e:  # noqa: BLE001 - expected: not found
            if "ResourceNotFound" not in type(e).__name__ + str(e):
                findings.append(f"cannot read the old gateway {gateway_id}: {e}")
        try:
            cognito.describe_resource_server(
                UserPoolId=pool_id, Identifier=MCP_RESOURCE_SERVER
            )
            findings.append(
                f"the resource server {MCP_RESOURCE_SERVER} is still in the pool"
            )
        except cognito.exceptions.ResourceNotFoundException:
            pass
        after = self.dataset_snapshot(bucket, table) if with_datasets else {}
        for test_set in after:
            if after[test_set] != before[test_set]:
                findings.append(
                    f"dataset {test_set} changed: record "
                    f"{'same' if after[test_set]['record'] == before[test_set]['record'] else 'differs'}, "
                    f"{len(before[test_set]['objects'])} objects before, "
                    f"{len(after[test_set]['objects'])} after"
                )
        if findings:
            raise PhaseFailed("; ".join(findings))
        counts = {t: len(before[t]["objects"]) for t in before}
        return (
            f"destroyed {len(destroyed)} resources (the stacks, the gateway and the "
            "resource server are gone)"
            + (f"; datasets unchanged: {counts} objects" if with_datasets else "")
        )

    def dataset_location(self, insts: List[Dict[str, Any]]) -> Tuple[str, str]:
        """The test-set bucket and tracking table the old deployment's W-2 deployer
        was given. Upstream's Terraform names the bucket ``TEST_SET_BUCKET``
        (``test_studio_env_base`` in test-studio.tf); its deployers' own code reads
        ``TESTSET_BUCKET``, the name upstream's SAM template uses, so either is
        accepted."""
        deployer = next(
            (
                i
                for i in insts
                if i["type"] == "aws_lambda_function"
                and i["name"] == "w2_dataset_deployer"
            ),
            None,
        )
        if deployer is None:
            raise PhaseFailed("the old state has no w2_dataset_deployer function")
        env = (deployer["attributes"].get("environment") or [{}])[0].get(
            "variables"
        ) or {}
        bucket = env.get("TEST_SET_BUCKET") or env.get("TESTSET_BUCKET")
        table = env.get("TRACKING_TABLE")
        if not bucket or not table:
            raise PhaseFailed(
                "the w2_dataset_deployer function's environment names no test-set "
                f"bucket or tracking table (it has {sorted(env)})"
            )
        return bucket, table

    def verify_features(self, findings: List[str]) -> str:
        """For the mcp-datasets variant: the new deployment has its own gateway,
        and every dataset record and file is as it was before the targeted destroy
        (none rewritten, so the new deployment's deployers did not download them
        again)."""
        if self.s.variant not in MCP_VARIANTS:
            return ""
        name = f"{self.s.new_name}-analytics-gateway"
        control = self.boto("bedrock-agentcore-control")
        gateways = [
            g
            for page in control.get_paginator("list_gateways").paginate()
            for g in page.get("items", [])
            if g.get("name") == name
        ]
        if len(gateways) != 1:
            findings.append(f"{len(gateways)} AgentCore gateways named {name}, not 1")
        if self.s.variant != MCP_DATASETS_VARIANT:
            return f"; new gateway {name}"
        before = json.loads((self.work / "datasets-before.json").read_text())
        loc = self.ctx["datasets"]
        after = json.loads(
            json.dumps(self.dataset_snapshot(loc["bucket"], loc["table"]), default=str)
        )
        for test_set, _prefix in DATASETS:
            b, a = before[test_set], after[test_set]
            missing = [k for k in b["objects"] if k not in a["objects"]]
            rewritten = [
                k
                for k in b["objects"]
                if k in a["objects"] and a["objects"][k] != b["objects"][k]
            ]
            if missing or rewritten:
                findings.append(
                    f"dataset {test_set}: {len(missing)} objects missing, "
                    f"{len(rewritten)} rewritten"
                )
            if a["record"] is None:
                findings.append(f"the testset#{test_set} record is gone")
            elif a["record"].get("datasetVersion") != b["record"].get("datasetVersion"):
                findings.append(f"the testset#{test_set} record's version changed")
        return f"; new gateway {name}; datasets kept without a re-download"

    def phase_migrate_plan(self) -> str:
        old_state_file = self.work / "old.tfstate"
        write_private(
            old_state_file, self.terraform_capture(self.old_dir, "state", "pull")
        )
        admin = self.secrets()["users"][0]["username"]
        args = [
            "--old-state",
            str(old_state_file),
            "--admin-email",
            admin,
            "--terraform-module-url",
            self.s.module_url,
            "--terraform-dir",
            str(self.new_dir),
            "--terraform-bin",
            self.s.terraform_bin,
        ]
        if self.s.assume_role_arn:
            args += ["--terraform-assume-role-arn", self.s.assume_role_arn]
        parameters = []
        if self.s.upstream_permissions_boundary_arn:
            # The new deployment's roles need the run's boundary too: the deploy
            # role creates a role only when it carries that boundary, and the
            # module attaches PermissionsBoundaryArn to every role it creates.
            parameters.append(
                f"PermissionsBoundaryArn={self.s.upstream_permissions_boundary_arn}"
            )
        if self.s.variant in MCP_VARIANTS:
            parameters.append("EnableMCP=true")
        if parameters:
            args += ["--parameters", ",".join(parameters)]
        if self.s.state_mirror:
            # Before the command's own `init`, so the new state is in the bucket
            # from its first write.
            self.write_backend(self.new_dir, NEW_BACKEND_FILE, "new")
        self.migrate("plan", *args)
        plan = json.loads(
            (
                self.new_dir / "genai-idp-terraform-migration" / "migration-plan.json"
            ).read_text()
        )
        missing = required_kept_missing(json.loads(old_state_file.read_text()), plan)
        if missing:
            raise PhaseFailed(
                "the plan does not keep what the test requires: " + "; ".join(missing)
            )
        kept = kept_resources(plan)
        extra = {
            "buckets": sorted(
                set(kept["buckets"]) - set(self.required_targets()["buckets"])
            ),
            "tables": sorted(
                set(kept["tables"]) - set(self.required_targets()["tables"])
            ),
        }
        self.ctx["keptByPlan"] = kept
        verdicts: Dict[str, int] = {}
        for r in plan["resources"]:
            verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
        self.ctx["planVerdicts"] = verdicts
        self.ctx["bdaProjects"] = [
            r["physical_id"]
            for r in plan["resources"]
            if r["kind"] == "bda_project" and r.get("physical_id")
        ]
        self.ctx["reportingBucket"] = plan.get("parameters", {}).get(
            "ReportingBucketName"
        )
        return (
            f"{(self.new_dir / 'adopt.tf').read_text().count('import {')} import blocks; "
            f"verdicts {verdicts}; "
            f"kept beyond the required set: {extra}"
        )

    def phase_migrate_check_retention(self) -> str:
        out = self.migrate("check-retention", "--terraform-dir", str(self.new_dir))
        return f"{len(out.get('details', {}).get('buckets', []))} buckets checked"

    def phase_migrate_freeze(self) -> str:
        out = self.migrate(
            "freeze",
            "--terraform-dir",
            str(self.new_dir),
            "--apply",
            "--wait",
            "--timeout",
            "1800",
        )
        return "; ".join(out.get("changes") or ["rule already disabled"])

    def phase_migrate_fix_config(self) -> str:
        out = self.migrate(
            "fix-config", "--terraform-dir", str(self.new_dir), "--apply"
        )
        self.ctx["configBackup"] = out.get("backup_path")
        return f"{len(out.get('changes') or [])} rows fixed"

    def phase_migrate_adopt_user_pool(self) -> str:
        out = self.migrate(
            "adopt-user-pool",
            "--terraform-dir",
            str(self.new_dir),
            "--terraform-bin",
            self.s.terraform_bin,
            "--apply",
        )
        return f"{len(out.get('changes') or [])} changes"

    def phase_old_ttl_off(self) -> str:
        """The guide's hazard for the agent-jobs table: DynamoDB cannot move its
        time-to-live from ``expiresAfter`` to the module's ``ExpiresAfter`` while it
        is on. Turned off here, straight after seeding, so that the hour DynamoDB
        then refuses further TTL changes runs alongside the early phases rather than
        in front of the apply (``ttl-settle`` waits out what is left of it)."""
        tables = sorted(
            inst["attributes"]["name"]
            for inst in state_instances(self.old_state())
            if inst["type"] == "aws_dynamodb_table" and inst["name"] == "agent_jobs"
        )
        ddb = self.boto("dynamodb")
        for table in tables:
            desc = ddb.describe_time_to_live(TableName=table)["TimeToLiveDescription"]
            if desc.get("TimeToLiveStatus") == "ENABLED":
                self._disable_ttl(ddb, table, desc["AttributeName"])
                self.ctx["ttlDisabledAt"] = time.time()
        self.ctx["ttlTables"] = tables
        self.ctx.setdefault("ttlDisabledAt", time.time())
        return f"time-to-live turning off on {tables or 'no agent-jobs table'}"

    def _disable_ttl(self, ddb, table: str, attribute: str) -> None:
        """Turn TTL off, waiting out DynamoDB's change interval if it refuses.

        A deployment the test has just created enabled TTL on the table minutes
        ago, which counts as the first change of the interval, so the disable is
        refused ("modified multiple times within a fixed interval") until about an
        hour after the table was created. A customer's table is old enough never to
        hit this; the test's always does."""
        deadline = time.monotonic() + TTL_SETTLE_SECONDS + 300
        while True:
            try:
                ddb.update_time_to_live(
                    TableName=table,
                    TimeToLiveSpecification={
                        "Enabled": False,
                        "AttributeName": attribute,
                    },
                )
                return
            except ddb.exceptions.ClientError as e:
                if "multiple times" not in str(e) or time.monotonic() > deadline:
                    raise
                time.sleep(60)

    def phase_ttl_settle(self) -> str:
        """Wait until TTL reads DISABLED and DynamoDB's TTL change interval (about
        an hour after the disable, measured: an earlier apply failed with "modified
        multiple times within a fixed interval") has passed."""
        ddb = self.boto("dynamodb")
        tables = self.ctx.get("ttlTables") or []
        deadline = time.monotonic() + TTL_SETTLE_SECONDS
        for table in tables:
            while (
                ddb.describe_time_to_live(TableName=table)["TimeToLiveDescription"].get(
                    "TimeToLiveStatus"
                )
                != "DISABLED"
            ):
                if time.monotonic() > deadline:
                    raise PhaseFailed(f"time-to-live on {table} did not turn off")
                time.sleep(30)
        wait = (
            self.ctx.get("ttlDisabledAt", time.time())
            + TTL_SETTLE_SECONDS
            - time.time()
        )
        left = self.remaining_seconds()
        if tables and wait > 0 and left is not None and wait > left:
            raise PhaseFailed(
                f"DynamoDB's TTL interval needs {wait / 60:.0f} more minutes, past the "
                "run's deadline; tearing down"
            )
        if tables and wait > 0:
            print(
                f"  waiting {wait / 60:.0f} min for DynamoDB's TTL change interval",
                flush=True,
            )
            time.sleep(wait)
        return (
            f"waited {max(wait, 0) / 60:.0f} min" if tables else "nothing to wait for"
        )

    def acceptance_args(self) -> List[str]:
        """``--accept-change`` for each change the guide documents as expected, and
        for each one the operator passed; nothing else is accepted."""
        args: List[str] = []
        for change in (*GUIDE_ACCEPTED_CHANGES, *self.s.accept_changes):
            args += ["--accept-change", change]
        return args

    def phase_migrate_apply_dry_run(self) -> str:
        out = self.migrate(
            "apply",
            "--terraform-dir",
            str(self.new_dir),
            "--terraform-bin",
            self.s.terraform_bin,
            *self.acceptance_args(),
            label="apply-dry-run",
        )
        if out.get("applied"):
            raise PhaseFailed("the dry run reports it applied")
        review = (out.get("details") or {}).get("review") or []
        self.ctx["applyReview"] = review
        (self.work / "apply-review.txt").write_text("".join(r + "\n" for r in review))
        return (
            "; ".join(out.get("changes") or [])
            + f"; {len(review)} in-place changes to review"
        )

    def phase_migrate_apply(self) -> str:
        self.ctx["newApplyStarted"] = True
        self.save()
        out = self.migrate(
            "apply",
            "--terraform-dir",
            str(self.new_dir),
            "--terraform-bin",
            self.s.terraform_bin,
            *self.acceptance_args(),
            "--apply",
        )
        if not out.get("applied"):
            raise PhaseFailed("apply --apply reports nothing applied")
        (self.new_dir / "adopt.tf").replace(self.work / "adopt.tf.applied")
        return "; ".join(out.get("changes") or [])

    def phase_migrate_restore_config(self) -> str:
        out = self.migrate(
            "restore-config", "--terraform-dir", str(self.new_dir), "--apply"
        )
        try:
            restored = restored_profiles(out.get("changes") or [])
        except ValueError as e:
            raise PhaseFailed(str(e)) from e
        self.ctx["restoredProfiles"] = restored
        return f"restored {restored or 'nothing'}"

    # Verification --------------------------------------------------------------------------

    def new_handles(self) -> Dict[str, str]:
        cf = self.boto("cloudformation")
        outputs = {
            o["OutputKey"]: o["OutputValue"]
            for o in cf.describe_stacks(StackName=self.s.new_name)["Stacks"][0].get(
                "Outputs", []
            )
        }
        env = dict(
            line.split("=", 1)
            for line in outputs.get("WebUITestEnvFile", "").splitlines()
            if "=" in line
        )
        return {
            "pool": env.get("VITE_USER_POOL_ID", ""),
            "client": env.get("VITE_USER_POOL_CLIENT_ID", ""),
            "api": env.get("VITE_API_BASE_URL", ""),
            "stateMachine": outputs.get("StateMachineArn", ""),
            "inputBucket": outputs.get("S3InputBucketName", ""),
            "trackingTable": outputs.get("TrackingTableName", ""),
        }

    def check_users(self, handles: Dict[str, str], findings: List[str]) -> None:
        for user in self.secrets()["users"]:
            try:
                token = self.sign_in(
                    handles["pool"],
                    handles["client"],
                    user["username"],
                    user["password"],
                )
            except Exception as e:  # noqa: BLE001 - the finding is the failure
                findings.append(
                    f"{user['persona']} user cannot sign in to the new app client: {e}"
                )
                continue
            profile = (
                _find_value(self.api(handles["api"], token, "getMyProfile", {}), None)
                or {}
            )
            before = self.ctx["profilesBefore"].get(user["username"], {})
            diff = profile_differences(before, profile)
            if diff:
                findings.append(
                    f"{user['persona']} user's profile changed: " + "; ".join(diff)
                )

    def phase_verify(self) -> str:
        findings: List[str] = []
        handles = self.new_handles()
        self.ctx["new"] = handles
        if handles["pool"] != self.ctx["old"]["user_pool_id"]:
            findings.append(
                f"the new deployment uses pool {handles['pool']}, not the adopted one"
            )
        if (
            handles["inputBucket"]
            and handles["inputBucket"] != self.ctx["old"]["input_bucket"]
        ):
            findings.append(
                f"the new deployment's input bucket is {handles['inputBucket']}, not the adopted one"
            )

        before = json.loads((self.work / "snapshot-before.json").read_text())
        after = self.snapshot("after")
        plan = json.loads(
            (
                self.new_dir / "genai-idp-terraform-migration" / "migration-plan.json"
            ).read_text()
        )
        report: Dict[str, Any] = {"buckets": {}, "tables": {}}
        for bucket, objects in before["buckets"].items():
            cmp = compare_objects(objects, after["buckets"].get(bucket, {}))
            report["buckets"][bucket] = cmp
            if cmp["missing"] or cmp["changed"]:
                findings.append(
                    f"bucket {bucket}: {len(cmp['missing'])} objects missing, {len(cmp['changed'])} changed"
                )
        for table, items in before["tables"].items():
            cmp = compare_items(
                table_kind(table, plan),
                items,
                after["tables"].get(table, {}),
                self.ctx.get("restoredProfiles") or (),
            )
            report["tables"][table] = cmp
            if cmp["missing"] or cmp["changed"]:
                findings.append(
                    f"table {table}: {len(cmp['missing'])} items missing, {len(cmp['changed'])} changed "
                    f"({', '.join(sorted({a for c in cmp['changed'] for a in c['attributes']}))[:300]})"
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

        self.check_users(handles, findings)

        # The configuration edit, through the new API, and every profile listed.
        admin = self.secrets()["users"][0]
        token = self.sign_in(
            handles["pool"], handles["client"], admin["username"], admin["password"]
        )
        config = self.api(
            handles["api"],
            token,
            "getConfigVersion",
            {"versionName": SCOPED_CONFIG_VERSION},
        )
        if self.ctx["configMarker"] not in json.dumps(config):
            findings.append(
                "the configuration edit made on the old deployment is not in the new one"
            )
        profiles = sorted(
            json.loads(k)[0]["S"].split("#", 1)[1]
            for k in before["tables"].get(plan.get("configurationTable") or "", {})
            if json.loads(k)[0]["S"].startswith("Config#")
            and not json.loads(k)[0]["S"].startswith("Config#__")
        )
        _, listing = self.idp_cli(
            "config-list",
            "--stack-name",
            self.s.new_name,
            "--region",
            self.s.region,
            log="config-list.log",
            check=False,
        )
        missing_profiles = [p for p in profiles if not profile_listed(str(listing), p)]
        if missing_profiles:
            findings.append(f"idp-cli config-list does not show {missing_profiles}")

        # A new document: processed by the new deployment, not by the old one.
        key = f"migration-check/{self.ctx['runId']}-{Path(NEW_DOCUMENT).name}"
        started = datetime.now(timezone.utc)
        self.boto("s3").upload_file(
            str(REPO_ROOT / NEW_DOCUMENT), self.ctx["old"]["input_bucket"], key
        )
        statuses = self.wait_documents(
            handles["trackingTable"] or self.ctx["old"]["documents_table"], [key]
        )
        if statuses[key] != "COMPLETED":
            findings.append(f"the new document ended {statuses[key]}")
        time.sleep(30)
        new_runs = self._executions_since(handles["stateMachine"], started, key)
        old_runs = self._executions_since(
            self.ctx["old"]["state_machine_arn"], started, None
        )
        if not new_runs:
            findings.append(
                "the new deployment's state machine ran no execution for the new document"
            )
        if old_runs:
            findings.append(
                f"the OLD deployment's state machine ran {len(old_runs)} execution(s) after the migration"
            )
        self.ctx["newDocument"] = {
            "key": key,
            "status": statuses[key],
            "newRuns": len(new_runs),
            "oldRuns": len(old_runs),
        }

        features = self.verify_features(findings)
        if findings:
            raise PhaseFailed("; ".join(findings))
        added = sum(c["added"] for c in report["buckets"].values())
        return (
            f"snapshot preserved ({added} objects added since); both users sign in with old passwords and keep "
            f"their scope; config edit kept; new document {statuses[key]} by the new deployment only"
            + features
        )

    def _executions_since(
        self, arn: str, since: datetime, key: Optional[str]
    ) -> List[str]:
        sfn = self.boto("stepfunctions")
        found = []
        for page in sfn.get_paginator("list_executions").paginate(stateMachineArn=arn):
            for ex in page["executions"]:
                if ex["startDate"] < since:
                    continue
                if key is None:
                    found.append(ex["executionArn"])
                    continue
                detail = sfn.describe_execution(executionArn=ex["executionArn"])
                if key in (detail.get("input") or ""):
                    found.append(ex["executionArn"])
        return found

    def phase_migrate_detach(self) -> str:
        out = self.migrate(
            "detach",
            "--terraform-dir",
            str(self.new_dir),
            "--terraform-bin",
            self.s.terraform_bin,
            "--old-terraform-dir",
            str(self.old_dir),
            "--apply",
        )
        return f"{len(out.get('changes') or [])} addresses removed from the old state"

    def phase_old_destroy_plan(self) -> str:
        self.terraform(
            self.old_dir,
            "plan",
            "-destroy",
            "-input=false",
            "-out=destroy.tfplan",
            log="old-destroy-plan.log",
        )
        plan_json = self.terraform_capture(
            self.old_dir, "show", "-json", "destroy.tfplan"
        )
        (self.work / "destroy-plan.json").write_text(plan_json)
        deletes = sum(
            1
            for rc in json.loads(plan_json).get("resource_changes", [])
            if "delete" in rc["change"]["actions"]
        )
        return f"{deletes} resources to destroy"

    def phase_migrate_check_destroy(self) -> str:
        out = self.migrate(
            "check-destroy",
            "--terraform-dir",
            str(self.new_dir),
            "--destroy-plan",
            str(self.work / "destroy-plan.json"),
        )
        # The test's own check, independent of the command's classification: no
        # deletion in the plan is a kept bucket, table, key or pool of its kind.
        destroy = json.loads((self.work / "destroy-plan.json").read_text())
        own = kept_deletions(destroy, self.snapshot_targets(), self.ctx["old"])
        if own:
            raise PhaseFailed(
                "check-destroy passed, but the destroy plan deletes kept resources: "
                + "; ".join(own)
            )
        return (
            f"{out.get('details', {}).get('deletions', '?')} deletions, none of them "
            "kept resources (the command's check and the test's own agree)"
        )

    def phase_old_destroy(self) -> str:
        self.ctx["oldNames"] = merge_names(
            self.ctx.get("oldNames") or {}, physical_names(self.old_state())
        )
        if "apiGatewayRoleBeforeOldDestroy" not in self.ctx:
            # Once: a resumed destroy compares against the setting before the first
            # attempt, not against what a partial destroy left. None when it cannot
            # be read, which verify-after-old-destroy reports rather than passes.
            try:
                arn: Optional[str] = self.api_gateway_role()["arn"]
            except Exception as e:  # noqa: BLE001 - the destroy still runs
                print(f"  ⚠ cannot read the API Gateway account settings: {e}")
                arn = None
            self.ctx["apiGatewayRoleBeforeOldDestroy"] = arn
        self.save()
        self.terraform(
            self.old_dir,
            "apply",
            "-input=false",
            "destroy.tfplan",
            log="old-destroy.log",
        )
        remaining = self.terraform_capture(self.old_dir, "state", "list").split()
        self.ctx["oldDestroyed"] = True
        if remaining:
            raise PhaseFailed(
                f"the old state still lists {len(remaining)} resources after the destroy"
            )
        return "old deployment destroyed"

    def phase_verify_after_old_destroy(self) -> str:
        findings: List[str] = []
        handles = self.ctx["new"]
        s3, ddb = self.boto("s3"), self.boto("dynamodb")
        for bucket in self.ctx["snapshotTargets"]["buckets"]:
            try:
                s3.head_bucket(Bucket=bucket)
            except Exception as e:  # noqa: BLE001
                findings.append(
                    f"adopted bucket {bucket} is gone after the old destroy: {e}"
                )
        for table in self.ctx["snapshotTargets"]["tables"]:
            try:
                ddb.describe_table(TableName=table)
            except Exception as e:  # noqa: BLE001
                findings.append(
                    f"adopted table {table} is gone after the old destroy: {e}"
                )
        self.check_users(handles, findings)
        role = self.api_gateway_role()
        if self.ctx.get("apiGatewayRoleBeforeOldDestroy") is not None:
            findings += api_gateway_role_problems(
                self.ctx["apiGatewayRoleBeforeOldDestroy"], role["arn"], role["exists"]
            )
        else:
            findings.append(
                "the Region's API Gateway CloudWatch role was not recorded before "
                "the old destroy, so what the destroy did to it is unknown"
            )
        if findings:
            raise PhaseFailed("; ".join(findings))
        return (
            "adopted buckets and tables stand; both users still sign in; the "
            f"Region's API Gateway CloudWatch role is {role['arn'] or 'unset'}"
        )

    # Teardown ------------------------------------------------------------------------------

    def phase_new_destroy(self) -> str:
        if not (self.new_dir / "main.tf").is_file():
            return "no new deployment directory"
        try:
            state = json.loads(
                self.terraform_capture(self.new_dir, "state", "pull") or "{}"
            )
        except PhaseFailed:
            state = {}
        self.ctx["newNames"] = merge_names(
            self.ctx.get("newNames") or {}, physical_names(state)
        )
        self.save()
        args = [
            "delete",
            "--terraform",
            "--stack-name",
            self.s.new_name,
            "--region",
            self.s.region,
            "--terraform-dir",
            str(self.new_dir),
            "--terraform-bin",
            self.s.terraform_bin,
            "--force",
        ]
        if self.s.assume_role_arn:
            args += ["--terraform-assume-role-arn", self.s.assume_role_arn]
        self.idp_cli(*args, log="new-delete.log")
        self.ctx["newDestroyed"] = True
        return "idp-cli delete --terraform completed"

    def phase_sweep(self) -> str:
        names = merge_names(
            self.ctx.get("oldNames") or {}, self.ctx.get("newNames") or {}
        )
        if self.ctx.get("reportingBucket"):
            names["buckets"] = sorted(
                set(names.get("buckets", [])) | {self.ctx["reportingBucket"]}
            )
        names["bda_projects"] = sorted(
            set(names.get("bda_projects", [])) | set(self.ctx.get("bdaProjects") or [])
        )
        sweeper = Sweeper(self, names)
        self.hand_back_api_gateway_role(sweeper)
        deleted, errors = sweeper.sweep()
        self.ctx["swept"] = deleted
        if errors:
            raise PhaseFailed(
                f"{len(errors)} leftovers could not be deleted: "
                + "; ".join(errors[:10])
            )
        return f"{len(deleted)} retained resources deleted"

    def hand_back_api_gateway_role(self, sweeper: "Sweeper") -> None:
        """Before the sweep: if the Region's API Gateway CloudWatch role setting
        names a role the sweep would delete, set it back to what it was when the
        run started.

        Upstream's deploy points the setting at its own role, and the migration
        keeps both (destroying either would stop logging for every API in the
        Region), so at teardown the setting names a role carrying this run's name.
        Deleting that role while the setting still names it is the same harm.

        The start role is restored unless it was known not to exist; one whose
        existence could not be read is restored too, rather than cleared. A start
        that names a role the sweep selects was recorded after upstream's deploy
        (a context from before the start was recorded), so it says nothing about
        the Region before the run and the setting is cleared. When the restore is
        refused (the run's role may pass only roles under the ``idp`` prefix), the
        setting is cleared instead, which passes no role, and a finding names the
        value to restore: keeping the run's role would not help, because the
        build's IAM clean-up deletes every role under the run's boundary."""
        start = self.ctx.get("apiGatewayRoleAtStart")
        if start is None:
            return
        try:
            arn = self.api_gateway_role()["arn"]
        except Exception as e:  # noqa: BLE001 - leftover-check reports it again
            self.finding(f"sweep: cannot read the API Gateway account settings: {e}")
            return
        if not arn or not sweeper.owned(iam_role_name(arn), "roles"):
            return
        start_arn = start.get("arn") or ""
        if start_arn and sweeper.owned(iam_role_name(start_arn), "roles"):
            start_arn = ""
        target = start_arn if start.get("exists") is not False else ""
        problem = self.set_api_gateway_role(target)
        if problem is None:
            print(
                "  the Region's API Gateway CloudWatch role is back to "
                f"{target or 'unset'}",
                flush=True,
            )
            return
        restore = (
            "; restore it with `aws apigateway update-account --patch-operations "
            f"op=replace,path=/cloudwatchRoleArn,value={target}` as a principal "
            "that may pass that role"
        )
        cleared = self.set_api_gateway_role("") if target else problem
        if cleared is None:
            self.finding(
                "sweep: could not set the Region's API Gateway CloudWatch role back "
                f"to {target} ({problem}), so it is now unset and API Gateway "
                f"logging in the Region is off{restore}"
            )
            return
        self.finding(
            "sweep: could not set the Region's API Gateway CloudWatch role back to "
            f"{target or 'unset'} ({problem}) or clear it ({cleared}), so it still "
            f"names {iam_role_name(arn)}, which the sweep deletes"
            + (restore if target else "")
        )

    def set_api_gateway_role(self, arn: str) -> Optional[str]:
        """Set the Region's API Gateway CloudWatch role (``""`` clears it); why
        that was refused, or None."""
        try:
            self.boto("apigateway").update_account(
                patchOperations=[
                    {"op": "replace", "path": "/cloudwatchRoleArn", "value": arn}
                ]
            )
        except Exception as e:  # noqa: BLE001 - the caller reports it
            return str(e) or type(e).__name__
        return None

    def phase_leftover_check(self) -> str:
        names = merge_names(
            self.ctx.get("oldNames") or {}, self.ctx.get("newNames") or {}
        )
        left = Sweeper(self, names).inventory()
        # The inventory reaches only the kinds the sweep deletes, by name, and many
        # upstream resources carry neither run name (measured: a failed old destroy
        # left Lambda functions and roles the inventory did not see). A state that
        # still lists managed resources is the complete answer for everything else.
        for label, directory in (("old", self.old_dir), ("new", self.new_dir)):
            if not (directory / ".terraform").is_dir():
                continue
            try:
                listed = self.terraform_capture(directory, "state", "list").split()
            except PhaseFailed:
                listed = []
            managed = managed_addresses(listed)
            if managed:
                left.append(f"the {label} state still lists {len(managed)} resources")
        self.ctx["leftovers"] = left
        start = self.ctx.get("apiGatewayRoleAtStart")
        if start is not None:
            # A finding, not a leftover: no teardown can rebuild a deleted role, so
            # a later build's reap would retry it for nothing.
            try:
                now = self.api_gateway_role()
                problems = api_gateway_role_left_problems(
                    start["arn"], start["exists"], now["arn"], now["exists"]
                )
            except Exception as e:  # noqa: BLE001 - the leftover verdict still runs
                problems = [f"cannot read the API Gateway account settings: {e}"]
            for problem in problems:
                self.finding(f"leftover-check: {problem}")
        # A KMS key cannot be deleted at once; pending deletion is the end state.
        if left:
            raise PhaseFailed(
                f"{len(left)} resources still stand: " + ", ".join(left[:15])
            )
        return "nothing left"

    # -- driver ----------------------------------------------------------------------------

    def run_phase(self, name: str) -> None:
        fn: Callable[[], str] = getattr(self, "phase_" + name.replace("-", "_"))
        print(f"▶ {name}", flush=True)
        started = time.monotonic()
        record = {"phase": name, "startedAt": datetime.now(timezone.utc).isoformat()}
        try:
            detail = fn()
            record.update(status="PASS", detail=detail)
            self.ctx["completed"].append(name)
            print(f"  ✓ {name}: {detail}", flush=True)
        except PhaseFailed as e:
            record.update(status="FAIL", detail=str(e))
            self.finding(f"{name}: {e}")
            raise
        except Exception as e:  # noqa: BLE001 - recorded, then re-raised
            record.update(status="ERROR", detail=f"{type(e).__name__}: {e}")
            self.finding(f"{name}: {type(e).__name__}: {e}")
            (self.work / "logs" / f"{name}.traceback").write_text(
                traceback.format_exc()
            )
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 1)
            self.ctx["phases"] = [
                p for p in self.ctx["phases"] if p["phase"] != name
            ] + [record]
            self.save()

    def emergency_teardown(self) -> None:
        """After a failure: destroy what exists, new deployment first, then the old
        one (at this point both states may name the shared resources; whichever
        destroy runs second refreshes them as gone), then sweep by name."""
        print("⚠ a phase failed; tearing down", flush=True)
        if "new-destroy" not in self.ctx["completed"] and (
            self.ctx.get("newApplyStarted") or self._new_state_nonempty()
        ):
            self._attempt("new-destroy")
        if self.ctx.get("oldDeployStarted") and not self.ctx.get("oldDestroyed"):
            self._attempt("old-destroy-forced", self._old_destroy_forced)
        self._attempt("sweep")
        self._attempt("leftover-check")

    def _new_state_nonempty(self) -> bool:
        if not (self.new_dir / "main.tf").is_file():
            return False
        try:
            return bool(self.terraform_capture(self.new_dir, "state", "list").split())
        except PhaseFailed:
            return False

    def _old_destroy_forced(self) -> str:
        try:
            self.ctx["oldNames"] = merge_names(
                self.ctx.get("oldNames") or {}, physical_names(self.old_state())
            )
        except PhaseFailed:
            pass
        self.save()
        for attempt in (1, 2):
            # Before every attempt, not once: a bucket another of the run's
            # buckets logs into receives objects after it was emptied (measured:
            # DeleteBucket refused with BucketNotEmpty on the logs bucket).
            self._empty_old_buckets()
            self._forget_items_of_deleted_tables()
            proc = self.terraform(
                self.old_dir,
                "destroy",
                "-auto-approve",
                "-input=false",
                # After the new deployment's delete, refresh cannot read resources
                # tied to the adopted key, now pending deletion (measured: GetItem on
                # the old concurrency table failed with KMSInvalidStateException and
                # aborted the destroy, leaving most of the old deployment standing).
                "-refresh=false",
                log="old-destroy-forced.log",
                check=False,
            )
            if proc.returncode == 0:
                self.ctx["oldDestroyed"] = True
                return f"terraform destroy completed (attempt {attempt})"
        raise PhaseFailed(
            "terraform destroy of the old deployment failed twice; the sweep handles what is left"
        )

    def _empty_old_buckets(self) -> None:
        # Upstream sets force_destroy on some buckets only; its versioned test-set
        # bucket holds the seeded objects and refuses DeleteBucket (measured). These
        # are this run's own buckets, so empty them, every version, first.
        # Recorded from this run's own old state, so each one is this run's.
        for bucket in (self.ctx.get("oldNames") or {}).get("buckets", []):
            try:
                empty_bucket(self.boto("s3"), bucket)
            except Exception as e:  # noqa: BLE001 - the destroy reports what is left
                print(f"  could not empty {bucket}: {e}", flush=True)

    def _forget_items_of_deleted_tables(self) -> None:
        """Drop from the old state every table item whose table no longer exists.

        After a failed adoption the new deployment's delete has already destroyed
        the tables it adopted, and their KMS key, while the old state still lists
        their items; deleting an item of a deleted table fails every attempt
        (measured: the concurrency table's workflow_counter left the old state
        listing it and its neighbours after the destroy). An item cannot outlive
        its table, so there is nothing left to delete."""
        try:
            instances = state_instances(self.old_state())
        except PhaseFailed:
            return
        ddb = self.boto("dynamodb")
        gone: Dict[str, bool] = {}
        stale = []
        for inst in instances:
            if inst["type"] != "aws_dynamodb_table_item":
                continue
            table = inst["attributes"].get("table_name")
            if not table:
                continue
            if table not in gone:
                try:
                    ddb.describe_table(TableName=table)
                    gone[table] = False
                except Exception as e:  # noqa: BLE001 - only NotFound counts
                    gone[table] = "ResourceNotFound" in type(e).__name__ or (
                        "ResourceNotFoundException" in str(e)
                    )
            if gone[table]:
                stale.append(inst["address"])
        if stale:
            print(
                f"  forgetting {len(stale)} item(s) of deleted tables: {stale}",
                flush=True,
            )
            proc = self.terraform(
                self.old_dir, "state", "rm", *stale, log="old-state-rm.log", check=False
            )
            if proc.returncode != 0:
                print("  could not forget them; see logs/old-state-rm.log", flush=True)

    def _attempt(self, name: str, fn: Optional[Callable[[], str]] = None) -> None:
        if fn is not None:
            setattr(self, "phase_" + name.replace("-", "_"), fn)
        try:
            self.run_phase(name)
        except Exception:  # noqa: BLE001 - recorded by run_phase; keep tearing down
            pass

    def teardown_only(self) -> int:
        """Destroy what this work directory's run created and sweep, without any
        forward phase: for a run whose own teardown did not finish. Refuses unless
        the names and account match the work directory's run and it created
        something."""
        if self.s.state_mirror and not self.context_path.is_file():
            # A work directory rebuilt by a later build: the run's context is in
            # its mirror. The checks below hold it to the names given here.
            self.download_mirror_context()
        problems = self.identity_problems()
        if not self.ctx.get("created"):
            problems.append("the work directory's run created nothing")
        release_problem = self.resolve_release()
        if release_problem:
            problems.append(release_problem)
        if problems:
            print("✗ teardown refused: " + "; ".join(problems), flush=True)
            return 2
        self.preflight_passed = True
        self.ctx.pop("leftovers", None)
        if self.s.state_mirror:
            try:
                restored = self.restore_for_teardown()
                if restored:
                    print(f"  restored {', '.join(restored)}", flush=True)
            except Exception as e:  # noqa: BLE001 - the teardown reports what is left
                self.finding(f"restore for teardown: {e}")
        self.emergency_teardown()
        left = self.ctx.get("leftovers")
        # Its own files: a teardown-only pass must not rewrite the run's verdict.
        self.write_result(
            failed=bool(left) or left is None, stopped=False, name="teardown"
        )
        if left == [] and self.s.state_mirror:
            self.clear_mirror()
        return 0 if left == [] else 1

    def remaining_seconds(self) -> Optional[float]:
        if self.s.deadline_seconds is None:
            return None
        return self.s.deadline_seconds - (time.monotonic() - self.started)

    def past_deadline(self) -> bool:
        left = self.remaining_seconds()
        return left is not None and left <= 0

    def teardown_permitted(self) -> Optional[str]:
        """Why no teardown may run in this invocation, or None.

        A teardown deletes by the run's names, so it runs only when this
        invocation's preflight passed (the names are usable, consistent with the
        work directory, unclaimed by anything else, and in the right account) and
        the run has created something. A preflight that refused is refusing exactly
        the cases where a sweep would reach another deployment's resources."""
        if not getattr(self, "preflight_passed", False):
            return "preflight did not pass in this invocation"
        if not self.ctx.get("created"):
            return "this run created nothing"
        return None

    def execute(self) -> int:
        stored = stored_name_problems(
            self.ctx,
            self.s.old_name,
            self.s.new_name,
            self.s.upstream_iam_path,
            self.s.variant,
        )
        if stored:
            # Refuse before touching the context: overwriting its names would let a
            # later teardown sweep for the wrong deployment.
            print("✗ " + "; ".join(stored), flush=True)
            return 2
        refusal = self.resolve_release()
        if refusal:
            print(f"✗ {refusal}", flush=True)
            return 2
        print(f"upstream release {self.upstream.tag}", flush=True)
        self.ctx.pop("leftovers", None)
        self.ctx.update(
            oldName=self.s.old_name,
            newName=self.s.new_name,
            upstreamIamPath=self.s.upstream_iam_path,
            variant=self.s.variant,
            region=self.s.region,
            moduleUrl=self.s.module_url,
        )
        failed = False
        stopped = False
        try:
            for name in PHASES:
                if name in self.ctx["completed"] and name != "preflight":
                    continue
                refusal = resume_refusal(name, self.ctx)
                if refusal:
                    raise PhaseFailed(refusal)
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
            if (
                self.s.state_mirror
                and not self.s.keep
                and (
                    self.ctx.get("leftovers") == []
                    # Refused before creating anything: there is nothing for a
                    # later build to tear down, and the run's role, which a reap
                    # would assume, goes with the build's own `-iam` cleanup.
                    or not self.ctx.get("created")
                )
            ):
                try:
                    self.clear_mirror()
                except Exception as e:  # noqa: BLE001 - the verdict stands
                    print(f"⚠ could not remove the state mirror: {e}", flush=True)
        return 1 if failed or (self.ctx["findings"] and not stopped) else 0

    def write_result(self, failed: bool, stopped: bool, name: str = "result") -> None:
        status = (
            "FAIL"
            if failed or self.ctx["findings"]
            else ("STOPPED" if stopped else "PASS")
        )
        result = {
            "status": status,
            "runId": self.ctx["runId"],
            "oldName": self.s.old_name,
            "newName": self.s.new_name,
            "region": self.s.region,
            "moduleUrl": self.s.module_url,
            "upstreamRepo": self.s.upstream_repo,
            "upstreamTag": self.ctx.get("upstreamTag"),
            "upstreamCommit": self.ctx.get("upstreamCommit", ""),
            "terraformVersion": self.ctx.get("terraformVersion"),
            "phases": self.ctx["phases"],
            "findings": self.ctx["findings"],
            # An earlier attempt's findings: a run that passed only on a retry says so.
            "previousFindings": self.ctx.get("previousFindings") or [],
            "planVerdicts": self.ctx.get("planVerdicts"),
            "newDocument": self.ctx.get("newDocument"),
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
# The sweep: delete what both destroys left, by name
# ---------------------------------------------------------------------------


class Sweeper:
    """Find and delete resources this run created that outlived both destroys.

    A resource is in scope only when :func:`owned_name` says so: named in one of
    this run's Terraform states (recorded before each destroy), or starting with
    one of the run's exact name prefixes. Preflight runs the same inventory and
    refuses to start if anything is in scope already. The CI's ``<new>-iam``
    pre-stack and its roles are never in scope: the build created them before the
    driver ran, and the build deletes them.
    """

    def __init__(self, run: Run, recorded: Dict[str, List[str]]):
        self.owner = run
        self.recorded = recorded
        self.old, self.new = run.s.old_name, run.s.new_name

    def owned(self, name: str, kind: str) -> bool:
        if name.startswith(f"{self.new}-iam"):
            return False
        return owned_name(name, self.old, self.new, self.recorded.get(kind, []))

    def _buckets(self) -> List[str]:
        names = {
            b["Name"] for b in self.owner.boto("s3").list_buckets().get("Buckets", [])
        }
        return sorted(n for n in names if self.owned(n, "buckets"))

    def _tables(self) -> List[str]:
        out = []
        for page in self.owner.boto("dynamodb").get_paginator("list_tables").paginate():
            out += [t for t in page["TableNames"] if self.owned(t, "tables")]
        return out

    def _log_groups(self) -> List[str]:
        logs = self.owner.boto("logs")
        prefixes = set(owned_prefixes(self.old)) | set(owned_prefixes(self.new))
        found = set()
        for prefix in prefixes:
            for page in logs.get_paginator("describe_log_groups").paginate(
                logGroupNamePrefix=prefix
            ):
                found.update(
                    g["logGroupName"]
                    for g in page["logGroups"]
                    if self.owned(g["logGroupName"], "log_groups")
                )
        for name in self.recorded.get("log_groups", []):
            page = logs.describe_log_groups(logGroupNamePrefix=name)
            found.update(
                g["logGroupName"]
                for g in page["logGroups"]
                if g["logGroupName"] == name
            )
        return sorted(found)

    def _pools(self) -> List[Tuple[str, str]]:
        return [
            (p["Id"], p["Name"])
            for p in self.owner._user_pools()
            if p["Id"] in self.recorded.get("user_pools", [])
            or self.owned(p["Name"], "user_pools")
        ]

    def _roles(self) -> List[str]:
        out = []
        for page in self.owner.boto("iam").get_paginator("list_roles").paginate():
            out += [
                r["RoleName"]
                for r in page["Roles"]
                if self.owned(r["RoleName"], "roles")
            ]
        return out

    def _keys(self) -> List[str]:
        kms = self.owner.boto("kms")
        out = []
        for key in self.recorded.get("kms_keys", []):
            try:
                state = kms.describe_key(KeyId=key)["KeyMetadata"]["KeyState"]
            except Exception:  # noqa: BLE001 - NotFound
                continue
            if state not in ("PendingDeletion", "PendingReplicaDeletion"):
                out.append(key)
        return out

    def _bda(self) -> List[str]:
        bda = self.owner.boto("bedrock-data-automation")
        out = []
        for arn in self.recorded.get("bda_projects", []):
            try:
                bda.get_data_automation_project(projectArn=arn)
                out.append(arn)
            except Exception:  # noqa: BLE001 - ResourceNotFound
                continue
        return out

    def _stacks(self) -> List[str]:
        cf = self.owner.boto("cloudformation")
        out = []
        for name in (self.new,):
            try:
                stack = cf.describe_stacks(StackName=name)["Stacks"][0]
            except Exception:  # noqa: BLE001
                continue
            if stack["StackStatus"] != "DELETE_COMPLETE":
                out.append(name)
        return out

    def _gateways(self) -> List[Tuple[str, str]]:
        """AgentCore gateways (id, name) under the run's names: upstream's MCP
        integration names its gateway ``<prefix>-api-analytics-gateway`` and the
        module ``<stack>-analytics-gateway``. Only the mcp-datasets variant creates
        any, so the base variant does not need the service to be reachable."""
        if self.owner.s.variant not in MCP_VARIANTS:
            return []
        control = self.owner.boto("bedrock-agentcore-control")
        return sorted(
            (g["gatewayId"], g["name"])
            for page in control.get_paginator("list_gateways").paginate()
            for g in page.get("items", [])
            if self.owned(g.get("name", ""), "gateways")
        )

    def inventory(self) -> List[str]:
        return (
            [f"stack {n}" for n in self._stacks()]
            + [f"bucket {n}" for n in self._buckets()]
            + [f"table {n}" for n in self._tables()]
            + [f"log group {n}" for n in self._log_groups()]
            + [f"user pool {i} ({n})" for i, n in self._pools()]
            + [f"role {n}" for n in self._roles()]
            + [f"kms key {k}" for k in self._keys()]
            + [f"bda project {a}" for a in self._bda()]
            + [f"agentcore gateway {i} ({n})" for i, n in self._gateways()]
        )

    def sweep(self) -> Tuple[List[str], List[str]]:
        deleted: List[str] = []
        errors: List[str] = []

        def attempt(label: str, fn: Callable[[], None]) -> None:
            try:
                fn()
                deleted.append(label)
            except Exception as e:  # noqa: BLE001 - collected and reported
                errors.append(f"{label}: {e}")

        s3 = self.owner.boto("s3")
        ddb = self.owner.boto("dynamodb")
        logs = self.owner.boto("logs")
        cognito = self.owner.boto("cognito-idp")
        kms = self.owner.boto("kms")
        bda = self.owner.boto("bedrock-data-automation")
        for name in self._tables():

            def drop_table(n=name):
                ddb.update_table(TableName=n, DeletionProtectionEnabled=False)
                ddb.get_waiter("table_exists").wait(TableName=n)
                ddb.delete_table(TableName=n)

            attempt(f"table {name}", drop_table)
        for name in self._log_groups():
            attempt(
                f"log group {name}",
                lambda n=name: logs.delete_log_group(logGroupName=n),
            )
        buckets = sorted(self._buckets(), key=lambda n: ("log" in n, n))
        for name in buckets:
            attempt(f"bucket {name}", lambda n=name: empty_and_delete_bucket(s3, n))
        for pool_id, name in self._pools():

            def drop_pool(p=pool_id):
                desc = cognito.describe_user_pool(UserPoolId=p)["UserPool"]
                if desc.get("Domain"):
                    cognito.delete_user_pool_domain(Domain=desc["Domain"], UserPoolId=p)
                if desc.get("DeletionProtection") == "ACTIVE":
                    cognito.update_user_pool(
                        UserPoolId=p,
                        DeletionProtection="INACTIVE",
                        AutoVerifiedAttributes=desc.get("AutoVerifiedAttributes", []),
                    )
                cognito.delete_user_pool(UserPoolId=p)

            attempt(f"user pool {pool_id} ({name})", drop_pool)
        for arn in self._bda():
            attempt(
                f"bda project {arn}",
                lambda a=arn: bda.delete_data_automation_project(projectArn=a),
            )
        for gateway_id, name in self._gateways():
            attempt(
                f"agentcore gateway {gateway_id} ({name})",
                lambda g=gateway_id: delete_gateway(
                    self.owner.boto("bedrock-agentcore-control"), g
                ),
            )
        for key in self._keys():
            attempt(
                f"kms key {key} (scheduled)",
                lambda k=key: kms.schedule_key_deletion(KeyId=k, PendingWindowInDays=7),
            )
        for role in self._roles():
            attempt(
                f"role {role}", lambda r=role: delete_role(self.owner.boto("iam"), r)
            )
        return deleted, errors


def delete_gateway(control, gateway_id: str, timeout: int = 300) -> None:
    """Delete an AgentCore gateway: its targets first, which the service requires."""
    for page in control.get_paginator("list_gateway_targets").paginate(
        gatewayIdentifier=gateway_id
    ):
        for target in page.get("items", []):
            control.delete_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target["targetId"]
            )
    deadline = time.monotonic() + timeout
    while True:
        try:
            control.delete_gateway(gatewayIdentifier=gateway_id)
            return
        except control.exceptions.ValidationException:
            # Targets still deleting.
            if time.monotonic() > deadline:
                raise
            time.sleep(10)


def empty_and_delete_bucket(s3, bucket: str) -> None:
    empty_bucket(s3, bucket)
    s3.delete_bucket(Bucket=bucket)


def empty_bucket(s3, bucket: str) -> None:
    """Delete every object version and delete marker in ``bucket``."""
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        batch = [
            {"Key": v["Key"], "VersionId": v["VersionId"]}
            for v in page.get("Versions", []) + page.get("DeleteMarkers", [])
        ]
        for i in range(0, len(batch), 1000):
            s3.delete_objects(
                Bucket=bucket, Delete={"Objects": batch[i : i + 1000], "Quiet": True}
            )


def delete_role(iam, role: str) -> None:
    for page in iam.get_paginator("list_attached_role_policies").paginate(
        RoleName=role
    ):
        for p in page["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
    for page in iam.get_paginator("list_role_policies").paginate(RoleName=role):
        for name in page["PolicyNames"]:
            iam.delete_role_policy(RoleName=role, PolicyName=name)
    for page in iam.get_paginator("list_instance_profiles_for_role").paginate(
        RoleName=role
    ):
        for prof in page["InstanceProfiles"]:
            iam.remove_role_from_instance_profile(
                InstanceProfileName=prof["InstanceProfileName"], RoleName=role
            )
    iam.delete_role(RoleName=role)


# ---------------------------------------------------------------------------
# API response helpers
# ---------------------------------------------------------------------------


def _json_field(response: Any, name: str) -> Any:
    """A JSON-valued field (AWSJSON arrives as a string) anywhere in a response."""
    value = _find_value(response, name)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _find_value(response: Any, name: Optional[str]) -> Any:
    """``name`` in the first dict of the response that has it; with ``name=None``,
    the response's single payload (the dispatcher wraps it in ``{op: payload}``)."""
    if name is None:
        if isinstance(response, dict) and len(response) == 1:
            only = next(iter(response.values()))
            if isinstance(only, dict):
                return only
        return response
    stack = [response]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            if name in cur:
                return cur[name]
            stack += list(cur.values())
        elif isinstance(cur, list):
            stack += cur
    return None


def _success(response: Any) -> bool:
    value = _find_value(response, "success")
    return value is True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> Settings:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--old-name", required=True, help="upstream deployment prefix (e.g. gitf2)"
    )
    p.add_argument(
        "--new-name", required=True, help="new deployment's stack name (e.g. gitfnew2)"
    )
    p.add_argument("--region", default=os.environ.get("AWS_REGION") or "us-west-2")
    p.add_argument(
        "--work-dir", help="default: ./scratch/terraform-migration/<old-name>"
    )
    p.add_argument(
        "--terraform-bin", default=os.environ.get("IDP_TERRAFORM_BIN") or "terraform"
    )
    p.add_argument(
        "--terraform-module-url", required=True, help="the module zip to migrate onto"
    )
    p.add_argument(
        "--terraform-assume-role-arn", help="role the new deployment's providers assume"
    )
    p.add_argument("--upstream-repo", default=UPSTREAM_REPO)
    p.add_argument(
        "--upstream-tag",
        choices=[r.tag for r in UPSTREAM_RELEASES] + [ROTATE_UPSTREAM_TAG],
        help=f"the release to migrate from; {ROTATE_UPSTREAM_TAG!r} takes this "
        "week's (the scheduled run's choice); default: the release a resumed run "
        f"recorded, else {UPSTREAM_TAG}",
    )
    p.add_argument(
        "--upstream-commit",
        help="override the release's pinned commit (default: the pin)",
    )
    p.add_argument("--expected-account", help="refuse to run in any other account")
    p.add_argument(
        "--upstream-iam-path",
        help="IAM path for every upstream role and policy (with the boundary below)",
    )
    p.add_argument(
        "--state-mirror",
        help="s3://<bucket>/<prefix>/ for both states, the context and the new root",
    )
    p.add_argument(
        "--upstream-permissions-boundary-arn",
        help="permissions boundary for every upstream role (with the path above)",
    )
    p.add_argument("--min-role-headroom", type=int, default=400)
    p.add_argument(
        "--variant",
        choices=VARIANTS,
        default=BASE_VARIANT,
        help=f"{MCP_VARIANT!r} also deploys upstream's MCP integration, and "
        f"{MCP_DATASETS_VARIANT!r} that and both Test Studio datasets; either runs the "
        "guide's manual procedure for them before plan",
    )
    p.add_argument(
        "--accept-change",
        action="append",
        default=[],
        metavar="ADDRESS:RULE",
        help="also accept this refused change in `apply` (beyond the guide's own)",
    )
    p.add_argument(
        "--deadline-seconds",
        type=int,
        help="start no forward phase after this many seconds (teardown still runs)",
    )
    p.add_argument(
        "--teardown-only",
        action="store_true",
        help="with --resume: only destroy what the run created and sweep",
    )
    p.add_argument("--keep", action="store_true", help="never tear down")
    p.add_argument(
        "--resume", action="store_true", help="continue from <work-dir>/context.json"
    )
    p.add_argument(
        "--stop-after", choices=PHASES, help="stop (without teardown) after this phase"
    )
    a = p.parse_args(argv)
    if bool(a.upstream_iam_path) != bool(a.upstream_permissions_boundary_arn):
        p.error(
            "--upstream-iam-path and --upstream-permissions-boundary-arn go together"
        )
    if a.state_mirror and not _STATE_MIRROR_RE.match(a.state_mirror):
        p.error(f"--state-mirror {a.state_mirror!r} is not s3://<bucket>/<prefix>/")
    if a.upstream_iam_path and not _IAM_PATH_RE.match(a.upstream_iam_path):
        p.error(f"--upstream-iam-path {a.upstream_iam_path!r} is not an IAM path")
    work = (
        Path(a.work_dir).expanduser()
        if a.work_dir
        else REPO_ROOT / "scratch" / "terraform-migration" / a.old_name
    )
    tf_bin = a.terraform_bin
    if os.sep in tf_bin:
        tf_bin = str(Path(tf_bin).expanduser().resolve())
    elif not shutil.which(tf_bin):
        p.error(f"{tf_bin} is not on PATH; pass --terraform-bin")
    return Settings(
        old_name=a.old_name,
        new_name=a.new_name,
        region=a.region,
        work_dir=work.resolve(),
        terraform_bin=tf_bin,
        module_url=a.terraform_module_url,
        upstream_repo=a.upstream_repo,
        upstream_tag=a.upstream_tag,
        upstream_commit=a.upstream_commit,
        assume_role_arn=a.terraform_assume_role_arn,
        keep=a.keep,
        resume=a.resume,
        stop_after=a.stop_after,
        min_role_headroom=a.min_role_headroom,
        expected_account=a.expected_account,
        accept_changes=tuple(a.accept_change),
        teardown_only=a.teardown_only,
        deadline_seconds=a.deadline_seconds,
        upstream_iam_path=a.upstream_iam_path,
        upstream_permissions_boundary_arn=a.upstream_permissions_boundary_arn,
        state_mirror=a.state_mirror,
        variant=a.variant,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Terraform's own state files, the snapshots and every log are owner-only.
    os.umask(0o077)
    settings = parse_args(argv)
    run = Run(settings)
    return run.teardown_only() if settings.teardown_only else run.execute()


if __name__ == "__main__":
    sys.exit(main())
