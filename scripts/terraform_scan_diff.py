#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Static security scan of the CloudFormation templates and of the Terraform
generated from them, and a diff of the two.

This is ``make terraform-scan-diff``, which ``make terraform-check`` runs. It is
the third way the translation is checked, beside ``terraform validate`` (the
module is well formed) and the live parity tier (the deployed properties
match): a deployment can work and match on every property both sides have and
still be softer than CloudFormation, because a setting that hardened the
CloudFormation resource did not survive translation. A static scanner with
rules for both languages sees that as a finding on the Terraform side with no
counterpart on the CloudFormation side.

It runs one pinned scanner, ``checkov`` (``CHECKOV_VERSION`` in the Makefile),
over:

- the CloudFormation side: the source templates (main and every nested one,
  which carry the ``# checkov:skip`` comments the security review recorded) and
  the same templates SAM-expanded, so every function is a plain
  ``AWS::Lambda::Function`` as the translator sees it. A result from either
  counts; a skip in the source wins;
- the Terraform side: the module the translator generates offline from the
  standard ``template.yaml``.

Each finding is reduced to (resource identity, check id). The identity is the
nested-stack path and logical id the translator's manifest records for every
Terraform address, so a bucket's ``aws_s3_bucket_versioning`` is the bucket.
Only checks ``checkov`` implements for both frameworks are compared. Then each
failed Terraform check is one of:

- **equal**: the CloudFormation side fails or skips the same check on the same
  resource;
- **softer**: the CloudFormation side passes it. The gate fails;
- **translator-added**: the address has no CloudFormation identity (the
  custom-resource bridge, the label stack). The gate fails;
- **not evaluated on the CloudFormation side**: ``checkov`` evaluates
  CloudFormation conditions at the parameters' defaults and skips a resource
  whose condition is false there, while the Terraform side evaluates the
  resource. Computed per finding from the resource's ``Condition``; counted and
  pinned, so a new one fails;
- **no counterpart**: anything else. The gate fails.

Every finding the gate would fail on must have an entry, with its reason, in
``scripts/terraform_scan_baseline.json``; an entry that matches nothing fails
too, as does a change in the pinned count. Findings on checks only the Terraform
framework has are counted and printed, never compared: they say nothing about
the translation.

What it does not prove: a scanner sees what its rules look at. A guardrail
wired to an empty string, which started this, is invisible to every ``checkov``
rule; the translator's completeness check is what catches that one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSLATOR_DIR = REPO_ROOT / "terraform" / "translator"
BASELINE = Path(__file__).resolve().with_name("terraform_scan_baseline.json")
CHECK_ID = re.compile(r"\b(CKV2?_AWS_\d+)\b")


class GateError(Exception):
    pass


# ------------------------------------------------------------------ checkov
def checkov_binary(version: str, cache: Path) -> Path:
    """``checkov`` at exactly ``version``, installed once into ``cache``.

    The path returned is absolute: CI passes the cache as a relative path
    (``.cache/idp-terraform-check`` on GitHub), and checkov is then run from
    other working directories, where a relative executable path names nothing.
    """
    venv = cache.resolve() / f"checkov-{version}"
    exe = venv / "bin" / "checkov"
    marker = venv / ".installed"
    if exe.is_file() and marker.is_file() and marker.read_text() == version:
        return exe
    if venv.exists():
        shutil.rmtree(venv)
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)  # nosec B603
    subprocess.run(  # nosec B603 - fixed argv, pinned version
        [
            str(venv / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            f"checkov=={version}",
        ],
        check=True,
    )
    marker.write_text(version)
    return exe


def run_checkov(exe: Path, args: list[str], cwd: Path) -> list[dict]:
    env = {
        **os.environ,
        "BC_SKIP_MAPPING": "TRUE",
        "CHECKOV_RUN_SCA_PACKAGE_SCAN_V2": "",
    }
    proc = subprocess.run(  # nosec B603 - fixed argv
        [str(exe), *args, "-o", "json", "--compact", "--skip-download"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # checkov exits 1 when it finds anything; only unparseable output is an error.
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise GateError(
            f"checkov produced no JSON (rc={proc.returncode}): {proc.stderr[-2000:]}"
        ) from e
    return data if isinstance(data, list) else [data]


def check_ids(exe: Path, framework: str) -> set[str]:
    out = subprocess.run(  # nosec B603 - fixed argv
        [str(exe), "--list", "--framework", framework],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return set(CHECK_ID.findall(out))


# --------------------------------------------------------------- translation
def translate(work: Path):
    """Translate the standard template offline. Returns (module dir, tree)."""
    sys.path.insert(0, str(TRANSLATOR_DIR))
    from idp_tf.awscc_schema import AwsccSchema
    from idp_tf.cli import VENDORED_SCHEMA, local_loader, main
    from idp_tf.expand import expand, load_template, offline_placeholders
    from idp_tf.translate import Translator

    out = work / "module"
    if main([str(REPO_ROOT / "template.yaml"), "--offline", "--out", str(out)]) != 0:
        raise GateError("the translator failed on template.yaml")
    template = expand(offline_placeholders(load_template(REPO_ROOT / "template.yaml")))
    tree = Translator(
        template, AwsccSchema(VENDORED_SCHEMA), nested_loader=local_loader(REPO_ROOT)
    )
    return out, tree


def source_templates(work: Path) -> tuple[dict[str, str], Path]:
    """``{repo-relative source template: stack path}``, and a directory of the
    same templates SAM-expanded, one ``<stack path>.json`` each."""
    sys.path.insert(0, str(TRANSLATOR_DIR))
    from idp_tf.expand import expand, load_template, offline_placeholders

    expanded = work / "cfn-expanded"
    expanded.mkdir()
    sources: dict[str, str] = {}

    def walk(path: Path, stack: str) -> None:
        sources[str(path.relative_to(REPO_ROOT))] = stack
        t = expand(offline_placeholders(load_template(path)))
        (expanded / f"{stack.replace('/', '__') or '_root'}.json").write_text(
            json.dumps(t, default=str)
        )
        for lid, res in t.get("Resources", {}).items():
            if res["Type"] != "AWS::CloudFormation::Stack":
                continue
            url = res["Properties"]["TemplateURL"].removeprefix("./")
            child = (
                REPO_ROOT
                / url.removesuffix("/.aws-sam/packaged.yaml")
                / "template.yaml"
            )
            walk(child, f"{stack}/{lid}" if stack else lid)

    walk(REPO_ROOT / "template.yaml", "")
    return sources, expanded


# ---------------------------------------------------------------- comparison
def _results(reports: list[dict], framework: str) -> list[tuple[str, dict]]:
    out = []
    for report in reports:
        if report.get("check_type") != framework:
            continue
        for kind in ("passed_checks", "failed_checks", "skipped_checks"):
            for r in report.get("results", {}).get(kind, []):
                out.append((kind.split("_")[0], r))
    return out


def cfn_status(
    source_reports: list[dict],
    expanded_reports: list[dict],
    sources: dict[str, str],
) -> dict[tuple[str, str, str], str]:
    """``{(stack, logical id, check): passed | failed | skipped}``."""
    rank = {"passed": 0, "failed": 1, "skipped": 2}
    status: dict[tuple[str, str, str], str] = {}

    def put(key, value):
        if key not in status or rank[value] > rank[status[key]]:
            status[key] = value

    for kind, r in _results(source_reports, "cloudformation"):
        stack = sources.get(r["file_path"].lstrip("/"))
        if stack is None:
            continue
        put((stack, r["resource"].split(".", 1)[1], r["check_id"]), kind)
    for kind, r in _results(expanded_reports, "cloudformation"):
        stem = Path(r["file_path"]).stem
        stack = "" if stem == "_root" else stem.replace("__", "/")
        put((stack, r["resource"].split(".", 1)[1], r["check_id"]), kind)
    return status


def tf_address(r: dict) -> str:
    """The module-qualified address of a Terraform finding, without instance keys."""
    address = r["resource"]
    if not address.startswith("module."):
        parts = Path(r["file_path"].lstrip("/")).parts
        prefix = ""
        i = 0
        while i + 1 < len(parts) and parts[i] == "modules":
            prefix += f"module.{parts[i + 1]}."
            i += 2
        address = prefix + address
    return re.sub(r"\[[^\]]*\]", "", address)


def classify(
    tf_reports: list[dict],
    status: dict[tuple[str, str, str], str],
    manifest: dict,
    conditional: set[tuple[str, str]],
    comparable: set[str],
) -> dict[str, list[dict]]:
    identity = {
        a: (e["stack"], e["logicalId"])
        for e in manifest["resources"]
        for a in e["addresses"]
    }
    out: dict[str, list[dict]] = {}
    for kind, r in _results(tf_reports, "terraform"):
        if kind != "failed":
            continue
        check = r["check_id"]
        address = tf_address(r)
        finding = {"check": check, "address": address, "name": r.get("check_name")}
        if check not in comparable:
            cls = "terraform-only check"
        elif address not in identity:
            cls = "translator-added"
            # The same generated file is emitted into every module that needs it.
            finding["resource"] = _bare(address)
        else:
            stack, lid = identity[address]
            finding["resource"] = f"{stack}/{lid}" if stack else lid
            cfn = status.get((stack, lid, check))
            if cfn in ("failed", "skipped"):
                cls = "equal"
            elif cfn == "passed":
                cls = "softer"
            elif (stack, lid) in conditional:
                cls = "not evaluated on the CloudFormation side"
            else:
                cls = "no counterpart"
        out.setdefault(cls, []).append(finding)
    return out


def _bare(address: str) -> str:
    """``module.a.module.b.aws_x.y`` -> ``aws_x.y``."""
    parts = address.split(".")
    return ".".join(parts[-2:])


def conditional_resources(tree) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()

    def walk(tr, stack: str) -> None:
        for lid, plan in tr.plans.items():
            if plan.condition:
                out.add((stack, lid))
            if plan.kind == "module":
                walk(tr.children[plan.name], f"{stack}/{lid}" if stack else lid)

    walk(tree, "")
    return out


GATED = ("softer", "translator-added", "no counterpart")


def verdict(classes: dict[str, list[dict]], baseline: dict, version: str) -> list[str]:
    """Failures: a gated finding with no baseline entry, an entry matching
    nothing, or a change in the pinned not-evaluated count."""
    problems = []
    if baseline.get("checkovVersion") != version:
        problems.append(
            f"the baseline was measured with checkov {baseline.get('checkovVersion')} "
            f"and the gate runs {version}; re-measure it with the pinned version"
        )
    entries = {(e["check"], e["resource"]): e for e in baseline.get("entries", [])}
    seen: set[tuple[str, str]] = set()
    for cls in GATED:
        for f in classes.get(cls, []):
            key = (f["check"], f["resource"])
            if key in entries:
                seen.add(key)
                continue
            problems.append(
                f"{cls}: {f['check']} ({f['name']}) fails on {f['address']} "
                f"[{f['resource']}]. Fix the translation, or add an entry with its "
                f"reason to {BASELINE.relative_to(REPO_ROOT)}"
            )
    for key in sorted(set(entries) - seen):
        problems.append(f"stale baseline entry, matches no finding: {key}")
    pinned = baseline.get("notEvaluatedCount")
    actual = len(classes.get("not evaluated on the CloudFormation side", []))
    if pinned != actual:
        problems.append(
            f"findings on resources the CloudFormation scan skips (condition false "
            f"at the defaults): {actual}, pinned at {pinned} in the baseline. Read "
            "the new ones (printed above) and update notEvaluatedCount"
        )
    return problems


def run(args: argparse.Namespace) -> int:
    baseline = json.loads(BASELINE.read_text())
    exe = checkov_binary(args.checkov_version, args.cache_dir)
    with tempfile.TemporaryDirectory(prefix="idp-tf-scan-") as tmp:
        work = Path(tmp)
        module, tree = translate(work)
        manifest = json.loads((module / "idp-terraform.json").read_text())
        sources, expanded = source_templates(work)
        file_args = [a for s in sources for a in ("-f", s)]
        cfn_src = run_checkov(
            exe, [*file_args, "--framework", "cloudformation"], REPO_ROOT
        )
        cfn_exp = run_checkov(
            exe, ["-d", str(expanded), "--framework", "cloudformation"], expanded
        )
        tf = run_checkov(
            exe,
            [
                "-d",
                str(module),
                "--framework",
                "terraform",
                "--download-external-modules",
                "false",
            ],
            module,
        )
        comparable = check_ids(exe, "cloudformation") & check_ids(exe, "terraform")
        status = cfn_status(cfn_src, cfn_exp, sources)
        classes = classify(
            tf, status, manifest, conditional_resources(tree), comparable
        )
    counts = Counter({k: len(v) for k, v in classes.items()})
    print(f"checkov {args.checkov_version}: failed Terraform checks by class:")
    for cls in (
        "equal",
        "softer",
        "translator-added",
        "no counterpart",
        "not evaluated on the CloudFormation side",
        "terraform-only check",
    ):
        print(f"  {cls:45} {counts.get(cls, 0)}")
    for f in classes.get("not evaluated on the CloudFormation side", []):
        print(f"  not evaluated: {f['check']} {f['address']}")
    if args.json:
        args.json.write_text(json.dumps(classes, indent=1, sort_keys=True) + "\n")
    problems = verdict(classes, baseline, args.checkov_version)
    for p in problems:
        print(f"FAIL {p}")
    if problems:
        print(f"terraform-scan-diff: {len(problems)} problem(s)")
        return 1
    print(
        "terraform-scan-diff: no Terraform finding without a CloudFormation "
        "counterpart beyond the explained baseline"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--checkov-version", required=True)
    ap.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("~/.cache/idp-terraform-check").expanduser(),
    )
    ap.add_argument("--json", type=Path, help="write the classified findings here")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except GateError as e:
        print(f"terraform-scan-diff: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
