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

1. clone the pinned upstream tag (and check the commit it resolves to), deploy its
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
   deployment's destroy, a second sign-in check against what is left, then the new
   deployment's ``idp-cli delete --terraform`` and a sweep of what either retained,
   by name.

Teardown runs in a ``finally``: a failed phase still destroys both deployments
(new first, then old, since at that point both states may name the shared
resources) and sweeps by name. ``--keep`` and ``--stop-after`` skip it.

WHAT IT DOES NOT PROVE
----------------------
* Other upstream example configurations (``bda-processor`` and the rest), other
  upstream tags, or an upstream deployment that is months old (the expiry checks run
  against objects minutes old, so ``check-retention`` cannot find anything to
  refuse).
* The knowledge-base re-sync, the alerts subscription, or the browser sign-in (an
  SRP sign-in proves the password; ``~/spike``-style browser checks prove the UI).
* The back-out route in the guide.

SAFETY
------
Everything it deletes is either named in this run's own Terraform states (recorded
before each destroy) or carries one of this run's two names. Preflight refuses to
start when anything in the account already matches those names, so the sweep cannot
reach a resource this run did not create. Passwords live only in
``<work-dir>/secrets.json`` (mode 0600); the result JSON never holds one.

USAGE
-----
    make stacktest-terraform-migration OLD_NAME=gitf2 NEW_NAME=gitfnew2 \\
        TERRAFORM_MODULE_URL=https://.../idp-terraform-<version>.zip

    python3 scripts/sdlc/terraform_migration_test.py --old-name gitf2 \\
        --new-name gitfnew2 --terraform-module-url <zip url> \\
        --work-dir ~/scratch/gitf2 [--stop-after seed] [--resume] [--keep]

The upstream deploy and the adoption apply are each 20-40 minutes; a full run is
about two and a half hours.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import cognito_srp  # noqa: E402

UPSTREAM_REPO = "https://github.com/awslabs/genai-idp-terraform"
UPSTREAM_TAG = "v0.6.9-tf.0"
#: The commit the tag resolved to when this test was written. A tag can be moved;
#: a clone resolving it anywhere else is refused, because the test would no longer
#: be exercising what it was written against.
UPSTREAM_COMMIT = "5fdac033b2376f16beddffc2a391422d18cf2292"
#: Upstream's ``sources/`` is a git submodule holding this repository at a release
#: tag, and upstream's README has it initialised after the clone. Pinned for the
#: same reason as the commit above.
UPSTREAM_SOURCES_COMMIT = "325794a6ffe1c89bac41a0180c991e681eb70bc8"
UPSTREAM_EXAMPLE = "examples/unified-processor"

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

#: The only change the migration is allowed to make to an existing item in a kept
#: table: ``fix-config`` removes these two counters from each ``Config#`` row.
CONFIG_COUNTERS = ("LatestRevision", "PublishedRevision")

PHASES = (
    "preflight",
    "upstream-clone",
    "upstream-deploy",
    "seed",
    "snapshot-before",
    "migrate-plan",
    "migrate-check-retention",
    "migrate-freeze",
    "migrate-fix-config",
    "migrate-adopt-user-pool",
    "migrate-apply-dry-run",
    "migrate-apply",
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


def validate_names(old_name: str, new_name: str) -> List[str]:
    """Reasons the two deployment names cannot be used; empty when they can.

    Upstream's ``prefix`` and the module's ``stack_name`` both become part of
    resource names, and the leftover sweep matches by name, so neither may contain
    the other: a sweep for ``gitf2`` must not match ``gitf2new``'s resources while
    the second deployment is meant to outlive the first.
    """
    problems = []
    pattern = re.compile(r"^[a-z][a-z0-9]*$")
    for label, value, limit in (("old", old_name, 12), ("new", new_name, 25)):
        if not pattern.match(value):
            problems.append(
                f"the {label} name {value!r} must be lowercase letters and digits"
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


def upstream_tfvars(old_name: str, run_id: str, region: str) -> str:
    """The upstream example's ``terraform.tfvars``: every feature that owns data
    the migration has to keep is on, so each kept resource kind exists to check."""
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
        "enable_mcp                  = false\n"
        "enable_finetuning           = false\n\n"
        "rbac   = { enabled = true }\n"
        "web_ui = { enabled = true }\n\n"
        f'tags = {{ Project = "terraform-migration-test", Run = "{run_id}" }}\n'
    )


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


def allowed_item_change(
    table_kind: str, before: Dict[str, Any], after: Dict[str, Any]
) -> bool:
    """Whether a kept item's change is one the migration is meant to make.

    Exactly one is: ``fix-config`` removes CONFIG_COUNTERS from ``Config#`` rows of
    the configuration table, and nothing else about the row may differ. A row the
    new deployment rewrites afterwards (its first deploy seeds a revision history
    from the row's content) is reported, because its content is checked separately
    through the API rather than byte for byte.
    """
    if table_kind != "configuration":
        return False
    config = (before.get("Configuration") or {}).get("S", "")
    if not config.startswith("Config#"):
        return False
    stripped = {k: v for k, v in before.items() if k not in CONFIG_COUNTERS}
    return stripped == after


def compare_items(
    table_kind: str,
    before: Dict[str, Dict[str, Any]],
    after: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Items present before must be present after, unchanged except as
    :func:`allowed_item_change` allows. Items added since are counted."""
    missing = sorted(k for k in before if k not in after)
    changed, allowed = [], []
    for k, item in before.items():
        if k not in after or after[k] == item:
            continue
        if allowed_item_change(table_kind, item, after[k]):
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


def owned_name(
    name: str, old_name: str, new_name: str, recorded: Iterable[str] = ()
) -> bool:
    """Whether the sweep may delete ``name``: recorded from this run's states, or
    carrying one of the run's two names as a delimited token."""
    if name in set(recorded):
        return True
    token = re.compile(
        rf"(^|[^a-z0-9])({re.escape(old_name)}|{re.escape(new_name)})([^a-z0-9]|$)",
        re.I,
    )
    return bool(token.search(name))


def summarise(result: Dict[str, Any]) -> str:
    """The human summary: one line per phase, then the findings."""
    lines = [
        f"Terraform migration test: {result.get('status', 'UNKNOWN')}",
        f"  upstream {result.get('upstreamTag')} ({result.get('upstreamCommit', '')[:12]}), "
        f"old deployment {result.get('oldName')}, new deployment {result.get('newName')}, "
        f"region {result.get('region')}",
        f"  module {result.get('moduleUrl')}",
        "",
    ]
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
    findings = result.get("findings") or []
    if findings:
        lines += ["", "Findings:"] + [f"  - {f}" for f in findings]
    leftovers = result.get("leftovers")
    if leftovers is not None:
        lines += ["", f"Leftovers after teardown: {len(leftovers)}"] + [
            f"  - {x}" for x in leftovers
        ]
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
    upstream_tag: str = UPSTREAM_TAG
    upstream_commit: str = UPSTREAM_COMMIT
    assume_role_arn: Optional[str] = None
    keep: bool = False
    resume: bool = False
    stop_after: Optional[str] = None
    min_role_headroom: int = 400
    document_timeout: int = 2400
    expected_account: Optional[str] = None


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

    # -- plumbing --------------------------------------------------------------

    @property
    def old_dir(self) -> Path:
        return self.work / "upstream" / UPSTREAM_EXAMPLE

    @property
    def new_dir(self) -> Path:
        return self.work / "new" / self.s.new_name

    def save(self) -> None:
        tmp = self.context_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.ctx, indent=1, default=str))
        tmp.replace(self.context_path)

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
                f"terraform {args[0]} in {directory.name} exited {proc.returncode}; see {log_path}"
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
        self, phase: str, *args: str, log: Optional[str] = None, timeout: int = 3 * 3600
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
            log=log or f"migrate-{phase}.log",
            timeout=timeout,
            check=False,
        )
        (self.work / "phases" / f"{phase}.json").write_text(
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

    # -- secrets -----------------------------------------------------------------

    def secrets(self) -> Dict[str, Any]:
        if self.secrets_path.is_file():
            return json.loads(self.secrets_path.read_text())
        return {}

    def save_secrets(self, data: Dict[str, Any]) -> None:
        fd = os.open(self.secrets_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)

    # -- phases ---------------------------------------------------------------------

    def phase_preflight(self) -> str:
        problems = validate_names(self.s.old_name, self.s.new_name)
        if problems:
            raise PhaseFailed("; ".join(problems))
        ident = self.boto("sts").get_caller_identity()
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
        if not self.ctx.get("oldDeployStarted"):
            clashes = self.existing_matches()
            if clashes:
                raise PhaseFailed(
                    "resources already carrying this run's names exist, so the sweep could "
                    "reach something this run did not create: "
                    + ", ".join(clashes[:10])
                )
        return f"account ok, {headroom} roles of headroom, terraform {self.ctx['terraformVersion']}"

    def existing_matches(self) -> List[str]:
        found = []
        cf = self.boto("cloudformation")
        try:
            stack = cf.describe_stacks(StackName=self.s.new_name)["Stacks"][0]
            if stack["StackStatus"] != "DELETE_COMPLETE":
                found.append(f"stack {self.s.new_name}")
        except Exception:  # noqa: BLE001 - ValidationError: no such stack
            pass
        for b in self.boto("s3").list_buckets().get("Buckets", []):
            if owned_name(b["Name"], self.s.old_name, self.s.new_name):
                found.append(f"bucket {b['Name']}")
        for page in self.boto("dynamodb").get_paginator("list_tables").paginate():
            found += [
                f"table {t}"
                for t in page["TableNames"]
                if owned_name(t, self.s.old_name, self.s.new_name)
            ]
        for pool in self._user_pools():
            if owned_name(pool["Name"], self.s.old_name, self.s.new_name):
                found.append(f"user pool {pool['Name']}")
        return found

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
                    self.s.upstream_tag,
                    self.s.upstream_repo,
                    str(target),
                ],
                "upstream-clone.log",
            )
        head = self.sh(
            ["git", "rev-parse", "HEAD"], "upstream-clone.log", cwd=target
        ).stdout.strip()
        if head != self.s.upstream_commit:
            raise PhaseFailed(
                f"{self.s.upstream_tag} resolves to {head}, not the pinned {self.s.upstream_commit}; "
                "the tag has moved, so read the new commit before pinning it"
            )
        pinned = self.sh(
            ["git", "ls-tree", "HEAD", "sources"], "upstream-clone.log", cwd=target
        ).stdout.split()
        if len(pinned) < 3 or pinned[2] != UPSTREAM_SOURCES_COMMIT:
            raise PhaseFailed(
                f"upstream pins sources/ at {pinned[2:3]}, not {UPSTREAM_SOURCES_COMMIT}"
            )
        # Upstream's README: `sources/` is a submodule, initialised after the clone.
        self.sh(
            ["git", "submodule", "update", "--init", "--depth", "1", "sources"],
            "upstream-clone.log",
            cwd=target,
            timeout=1800,
        )
        checked_out = self.sh(
            ["git", "rev-parse", "HEAD"], "upstream-clone.log", cwd=target / "sources"
        ).stdout.strip()
        if checked_out != UPSTREAM_SOURCES_COMMIT:
            raise PhaseFailed(
                f"sources/ checked out {checked_out}, not {UPSTREAM_SOURCES_COMMIT}"
            )
        self.ctx["upstreamCommit"] = head
        return (
            f"{self.s.upstream_tag} at {head[:12]}, sources/ at "
            f"{UPSTREAM_SOURCES_COMMIT[:12]}"
        )

    def phase_upstream_deploy(self) -> str:
        tfvars = upstream_tfvars(self.s.old_name, self.ctx["runId"], self.s.region)
        (self.old_dir / "terraform.tfvars").write_text(tfvars)
        self.terraform(self.old_dir, "init", "-input=false", log="old-init.log")
        self.terraform(
            self.old_dir, "plan", "-input=false", "-out=old.tfplan", log="old-plan.log"
        )
        self.ctx["oldDeployStarted"] = True
        self.save()
        self.terraform(
            self.old_dir, "apply", "-input=false", "old.tfplan", log="old-apply.log"
        )
        (self.old_dir / "old.tfplan").unlink(missing_ok=True)
        outputs = json.loads(self.terraform_capture(self.old_dir, "output", "-json"))
        self.ctx["old"] = outputs["e2e_stack"]["value"]
        (self.work / "old-outputs.json").write_text(json.dumps(outputs, indent=1))
        return f"name prefix {self.ctx['old']['name_prefix']}"

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

    def snapshot(self, label: str) -> Dict[str, Any]:
        """Objects, items, users and groups of every bucket and table the old
        state holds that REQUIRED_KEPT or the plan keeps."""
        names = self.ctx["snapshotTargets"]
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
        pool = self.ctx["old"]["user_pool_id"]
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
        (self.work / f"snapshot-{label}.json").write_text(json.dumps(snap, default=str))
        return snap

    def phase_snapshot_before(self) -> str:
        state = self.old_state()
        (self.work / "old-before.tfstate").write_text(json.dumps(state))
        targets = {"buckets": set(), "tables": set()}
        for inst in state_instances(state):
            key = (inst["type"], inst["name"])
            if key in REQUIRED_KEPT and inst["type"] == "aws_s3_bucket":
                targets["buckets"].add(inst["attributes"]["bucket"])
            if key in REQUIRED_KEPT and inst["type"] == "aws_dynamodb_table":
                targets["tables"].add(inst["attributes"]["name"])
        self.ctx["snapshotTargets"] = {k: sorted(v) for k, v in targets.items()}
        self.ctx["oldNames"] = physical_names(state)
        snap = self.snapshot("before")
        objects = sum(len(v) for v in snap["buckets"].values())
        items = sum(len(v) for v in snap["tables"].values())
        return f"{len(snap['buckets'])} buckets ({objects} objects), {len(snap['tables'])} tables ({items} items), {len(snap['users'])} users"

    # Migration phases -------------------------------------------------------------------

    def phase_migrate_plan(self) -> str:
        old_state_file = self.work / "old.tfstate"
        old_state_file.write_text(self.terraform_capture(self.old_dir, "state", "pull"))
        os.chmod(old_state_file, 0o600)
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
        out = self.migrate("plan", *args)
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
                set(kept["buckets"]) - set(self.ctx["snapshotTargets"]["buckets"])
            ),
            "tables": sorted(
                set(kept["tables"]) - set(self.ctx["snapshotTargets"]["tables"])
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
            f"{out.get('import_count', out.get('importCount', '?'))} imports; verdicts {verdicts}; "
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

    def phase_migrate_apply_dry_run(self) -> str:
        out = self.migrate(
            "apply",
            "--terraform-dir",
            str(self.new_dir),
            "--terraform-bin",
            self.s.terraform_bin,
        )
        if out.get("applied"):
            raise PhaseFailed("the dry run reports it applied")
        return "; ".join(out.get("changes") or [])

    def phase_migrate_apply(self) -> str:
        self.ctx["newApplyStarted"] = True
        self.save()
        out = self.migrate(
            "apply",
            "--terraform-dir",
            str(self.new_dir),
            "--terraform-bin",
            self.s.terraform_bin,
            "--apply",
        )
        if not out.get("applied"):
            raise PhaseFailed("apply --apply reports nothing applied")
        (self.new_dir / "adopt.tf").replace(self.work / "adopt.tf.applied")
        return "; ".join(out.get("changes") or [])

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
                table_kind(table, plan), items, after["tables"].get(table, {})
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
        missing_profiles = [p for p in profiles if p not in str(listing)]
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

        if findings:
            raise PhaseFailed("; ".join(findings))
        added = sum(c["added"] for c in report["buckets"].values())
        return (
            f"snapshot preserved ({added} objects added since); both users sign in with old passwords and keep "
            f"their scope; config edit kept; new document {statuses[key]} by the new deployment only"
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
        return f"{out.get('details', {}).get('deletions', '?')} deletions, none of them kept resources"

    def phase_old_destroy(self) -> str:
        self.ctx["oldNames"] = merge_names(
            self.ctx.get("oldNames") or {}, physical_names(self.old_state())
        )
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
        if findings:
            raise PhaseFailed("; ".join(findings))
        return "adopted buckets and tables stand; both users still sign in"

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
        deleted, errors = sweeper.run()
        self.ctx["swept"] = deleted
        if errors:
            raise PhaseFailed(
                f"{len(errors)} leftovers could not be deleted: "
                + "; ".join(errors[:10])
            )
        return f"{len(deleted)} retained resources deleted"

    def phase_leftover_check(self) -> str:
        names = merge_names(
            self.ctx.get("oldNames") or {}, self.ctx.get("newNames") or {}
        )
        left = Sweeper(self, names).inventory()
        self.ctx["leftovers"] = left
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
            proc = self.terraform(
                self.old_dir,
                "destroy",
                "-auto-approve",
                "-input=false",
                log="old-destroy-forced.log",
                check=False,
            )
            if proc.returncode == 0:
                self.ctx["oldDestroyed"] = True
                return f"terraform destroy completed (attempt {attempt})"
        raise PhaseFailed(
            "terraform destroy of the old deployment failed twice; the sweep handles what is left"
        )

    def _attempt(self, name: str, fn: Optional[Callable[[], str]] = None) -> None:
        if fn is not None:
            setattr(self, "phase_" + name.replace("-", "_"), fn)
        try:
            self.run_phase(name)
        except Exception:  # noqa: BLE001 - recorded by run_phase; keep tearing down
            pass

    def execute(self) -> int:
        self.ctx.update(
            oldName=self.s.old_name,
            newName=self.s.new_name,
            region=self.s.region,
            moduleUrl=self.s.module_url,
            upstreamTag=self.s.upstream_tag,
        )
        failed = False
        stopped = False
        try:
            for name in PHASES:
                if name in self.ctx["completed"] and name != "preflight":
                    continue
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
                self.emergency_teardown()
            self.write_result(failed, stopped)
        return 1 if failed or (self.ctx["findings"] and not stopped) else 0

    def write_result(self, failed: bool, stopped: bool) -> None:
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
            "upstreamTag": self.s.upstream_tag,
            "upstreamCommit": self.ctx.get("upstreamCommit", ""),
            "terraformVersion": self.ctx.get("terraformVersion"),
            "phases": self.ctx["phases"],
            "findings": self.ctx["findings"],
            "planVerdicts": self.ctx.get("planVerdicts"),
            "newDocument": self.ctx.get("newDocument"),
            "leftovers": self.ctx.get("leftovers"),
        }
        (self.work / "result.json").write_text(
            json.dumps(result, indent=1, default=str)
        )
        text = summarise(result)
        (self.work / "summary.txt").write_text(text)
        print("\n" + text, flush=True)


# ---------------------------------------------------------------------------
# The sweep: delete what both destroys left, by name
# ---------------------------------------------------------------------------


class Sweeper:
    """Find and delete resources this run created that outlived both destroys.

    A resource is in scope only when :func:`owned_name` says so: named in one of
    this run's Terraform states (recorded before each destroy), or carrying one of
    the run's two names as a token. Preflight refused to start if anything in scope
    already existed.
    """

    def __init__(self, run: Run, recorded: Dict[str, List[str]]):
        self.run = run
        self.recorded = recorded
        self.old, self.new = run.s.old_name, run.s.new_name

    def owned(self, name: str, kind: str) -> bool:
        return owned_name(name, self.old, self.new, self.recorded.get(kind, []))

    def _buckets(self) -> List[str]:
        names = {
            b["Name"] for b in self.run.boto("s3").list_buckets().get("Buckets", [])
        }
        return sorted(n for n in names if self.owned(n, "buckets"))

    def _tables(self) -> List[str]:
        out = []
        for page in self.run.boto("dynamodb").get_paginator("list_tables").paginate():
            out += [t for t in page["TableNames"] if self.owned(t, "tables")]
        return out

    def _log_groups(self) -> List[str]:
        logs = self.run.boto("logs")
        prefixes = {
            f"/aws/lambda/{self.old}-",
            f"/aws/vendedlogs/states/{self.old}-",
            f"/aws/codebuild/{self.old}-",
            f"/{self.new}/",
            f"/aws/lambda/{self.new}-",
            f"/aws/vendedlogs/states/{self.new}-",
            f"/aws/codebuild/{self.new}-",
            f"{self.new}-",
        }
        found = set()
        for prefix in prefixes:
            for page in logs.get_paginator("describe_log_groups").paginate(
                logGroupNamePrefix=prefix
            ):
                found.update(g["logGroupName"] for g in page["logGroups"])
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
            for p in self.run._user_pools()
            if p["Id"] in self.recorded.get("user_pools", [])
            or self.owned(p["Name"], "user_pools")
        ]

    def _roles(self) -> List[str]:
        out = []
        for page in self.run.boto("iam").get_paginator("list_roles").paginate():
            out += [
                r["RoleName"]
                for r in page["Roles"]
                if r["RoleName"].startswith((f"{self.old}-", f"{self.new}-"))
                or r["RoleName"] in self.recorded.get("roles", [])
            ]
        return out

    def _keys(self) -> List[str]:
        kms = self.run.boto("kms")
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
        bda = self.run.boto("bedrock-data-automation")
        out = []
        for arn in self.recorded.get("bda_projects", []):
            try:
                bda.get_data_automation_project(projectArn=arn)
                out.append(arn)
            except Exception:  # noqa: BLE001 - ResourceNotFound
                continue
        return out

    def _stacks(self) -> List[str]:
        cf = self.run.boto("cloudformation")
        out = []
        for name in (self.new, f"{self.new}-iam"):
            try:
                stack = cf.describe_stacks(StackName=name)["Stacks"][0]
            except Exception:  # noqa: BLE001
                continue
            if stack["StackStatus"] != "DELETE_COMPLETE":
                out.append(name)
        return out

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
        )

    def run(self) -> Tuple[List[str], List[str]]:
        deleted: List[str] = []
        errors: List[str] = []

        def attempt(label: str, fn: Callable[[], None]) -> None:
            try:
                fn()
                deleted.append(label)
            except Exception as e:  # noqa: BLE001 - collected and reported
                errors.append(f"{label}: {e}")

        s3 = self.run.boto("s3")
        ddb = self.run.boto("dynamodb")
        logs = self.run.boto("logs")
        cognito = self.run.boto("cognito-idp")
        kms = self.run.boto("kms")
        bda = self.run.boto("bedrock-data-automation")
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
        for key in self._keys():
            attempt(
                f"kms key {key} (scheduled)",
                lambda k=key: kms.schedule_key_deletion(KeyId=k, PendingWindowInDays=7),
            )
        for role in self._roles():
            attempt(f"role {role}", lambda r=role: delete_role(self.run.boto("iam"), r))
        return deleted, errors


def empty_and_delete_bucket(s3, bucket: str) -> None:
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
    s3.delete_bucket(Bucket=bucket)


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
    p.add_argument("--upstream-tag", default=UPSTREAM_TAG)
    p.add_argument("--upstream-commit", default=UPSTREAM_COMMIT)
    p.add_argument("--expected-account", help="refuse to run in any other account")
    p.add_argument("--min-role-headroom", type=int, default=400)
    p.add_argument("--keep", action="store_true", help="never tear down")
    p.add_argument(
        "--resume", action="store_true", help="continue from <work-dir>/context.json"
    )
    p.add_argument(
        "--stop-after", choices=PHASES, help="stop (without teardown) after this phase"
    )
    a = p.parse_args(argv)
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
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    return Run(parse_args(argv)).execute()


if __name__ == "__main__":
    sys.exit(main())
