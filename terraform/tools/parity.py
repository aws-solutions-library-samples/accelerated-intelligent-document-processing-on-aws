#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Compare every resource of a Terraform deployment with its CloudFormation twin.

This is ``make terraform-parity CFN_STACK=<name> TF_STACK=<name>``, the
release-time property-parity tier. Deploy the same release twice with the same
parameters, once with CloudFormation and once with ``idp-cli deploy
--terraform``, then run it. It reads both resource trees, the main stack and
every nested stack:

- the CloudFormation side through ``ListStackResources``, recursing into each
  ``AWS::CloudFormation::Stack``;
- the Terraform side through its label stack's ``TerraformResourceDirectory``,
  the CloudFormation-shaped list of every module's resources the module writes.

Resources are paired by nested-stack path and logical id. Each pair is read
through Cloud Control (``GetResource``) on both sides, and functions also
through the Lambda API (Cloud Control returns no code location), then
normalised: the names that necessarily differ (stack names, nested stack names,
every paired physical id) become placeholders, JSON text is parsed, lists are
sorted, tags each tool adds itself are dropped. What still differs is a
finding, unless ``parity_baseline.json`` explains it: one entry per (type,
property path), with its reason. An entry that explained nothing in the run is
reported as stale.

It exits 1 on any unexplained difference, on a resource present on one side
only, and on a stale entry. Custom resources are not compared (they are not
resources Cloud Control can read; their effects are). A type Cloud Control
cannot read is listed, not compared.

It only reads. Offline tests drive it with recorded responses
(``terraform/translator/tests/test_parity.py``).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

BASELINE = Path(__file__).resolve().with_name("parity_baseline.json")
DIRECTORY_OUTPUT = "TerraformResourceDirectory"
#: Properties that differ between any two deployments, whatever made them.
VOLATILE = {
    "Arn",
    "RoleId",
    "SnapStartResponse",
    "CreationTime",
    "LastModified",
    "RevisionId",
    "Version",
    "CodeSha256",
    "SourceKMSKeyArn",
}
#: Tags each tool adds on its own.
TOOL_TAG_PREFIXES = ("aws:cloudformation:", "idp:stack-name", "idp:prototype")
STACK_TYPE = "AWS::CloudFormation::Stack"


@dataclass(frozen=True)
class Resource:
    stack: str  # nested-stack path of logical ids, "" for the main stack
    logical_id: str
    cfn_type: str
    physical_id: str

    @property
    def identity(self) -> str:
        return f"{self.stack}/{self.logical_id}" if self.stack else self.logical_id


@dataclass
class Report:
    compared: int = 0
    differences: list[tuple[str, str, str]] = field(default_factory=list)
    explained: list[tuple[str, str, str]] = field(default_factory=list)
    only_cfn: list[str] = field(default_factory=list)
    only_tf: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    stale: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.differences or self.only_cfn or self.only_tf or self.stale)


# ------------------------------------------------------------- resource trees
def cfn_tree(cfn: Any, stack: str, path: str = "") -> tuple[list[Resource], dict]:
    """Every resource under a CloudFormation stack, and ``{path: stack name}``."""
    out: list[Resource] = []
    names = {path: stack.split("/")[1] if stack.startswith("arn:") else stack}
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
        for r in page["StackResourceSummaries"]:
            res = Resource(
                path,
                r["LogicalResourceId"],
                r["ResourceType"],
                r.get("PhysicalResourceId", ""),
            )
            out.append(res)
            if res.cfn_type == STACK_TYPE and res.physical_id:
                child = f"{path}/{res.logical_id}" if path else res.logical_id
                more, more_names = cfn_tree(cfn, res.physical_id, child)
                out.extend(more)
                names.update(more_names)
    return out, names


def tf_tree(cfn: Any, s3: Any, label: str) -> tuple[list[Resource], dict]:
    """Every resource of a Terraform deployment, from its resource directory."""
    stack = cfn.describe_stacks(StackName=label)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    url = outputs.get(DIRECTORY_OUTPUT)
    if not url:
        raise SystemExit(f"{label} has no {DIRECTORY_OUTPUT} output: not a label stack")
    bucket, _, key = url.removeprefix("s3://").partition("/")
    root = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())["root"]
    out: list[Resource] = []
    names = {"": label}

    def walk(node: dict, path: str, name: str) -> None:
        for e in node.get("resources", []):
            res = Resource(
                path,
                e["LogicalResourceId"],
                e["ResourceType"],
                e.get("PhysicalResourceId") or "",
            )
            out.append(res)
            child = (node.get("nested") or {}).get(res.logical_id)
            if res.cfn_type == STACK_TYPE and child:
                child_path = f"{path}/{res.logical_id}" if path else res.logical_id
                child_name = f"{name}-{res.logical_id}"
                names[child_path] = child_name
                walk(child, child_path, child_name)

    walk(root, "", label)
    return out, names


# --------------------------------------------------------------- normalising
def normalise(value: Any, subs: list[tuple[str, str]]) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in sorted(value.items()):
            if k in VOLATILE or v == "":
                continue
            if k == "Tags" and isinstance(v, list):
                v = [
                    t
                    for t in v
                    if not str(t.get("Key", "")).startswith(TOOL_TAG_PREFIXES)
                ]
                if not v:
                    continue
            out[k] = normalise(v, subs)
        return out
    if isinstance(value, list):
        items = [normalise(v, subs) for v in value]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(value, str):
        for old, new in subs:
            if old:
                value = value.replace(old, new)
        if value[:1] in "{[":
            try:
                return normalise(json.loads(value), subs)
            except ValueError:
                pass
        return value
    return value


def diff(a: Any, b: Any, path: str = "") -> list[tuple[str, str]]:
    """(property path, description) per difference; ``a`` is CloudFormation."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            p = f"{path}.{k}"
            if k not in a:
                out.append((p, f"only in terraform = {json.dumps(b[k])[:200]}"))
            elif k not in b:
                out.append((p, f"only in cloudformation = {json.dumps(a[k])[:200]}"))
            else:
                out.extend(diff(a[k], b[k], p))
        return out
    if a != b:
        return [
            (
                path,
                f"cloudformation={json.dumps(a)[:200]} terraform={json.dumps(b)[:200]}",
            )
        ]
    return []


def explained_by(
    baseline: list[dict], cfn_type: str, path: str
) -> tuple[str, str] | None:
    for e in baseline:
        if e["type"] == cfn_type and (
            path == e["path"] or path.startswith(e["path"] + ".")
        ):
            return (e["type"], e["path"])
    return None


# ----------------------------------------------------------------- comparing
def read(cc: Any, cfn_type: str, ident: str) -> dict | None:
    try:
        resp = cc.get_resource(TypeName=cfn_type, Identifier=ident)
    except Exception as exc:  # noqa: BLE001 - unreadable types are listed
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("UnsupportedActionException", "TypeNotFoundException"):
            return None
        raise
    return json.loads(resp["ResourceDescription"]["Properties"])


def compare(
    cfn_side: tuple[list[Resource], dict],
    tf_side: tuple[list[Resource], dict],
    cc: Any,
    lam: Any,
    baseline: list[dict],
    log: Callable[[str], None] = print,
) -> Report:
    report = Report()
    cfn_res = {
        (r.stack, r.logical_id): r
        for r in cfn_side[0]
        if not r.cfn_type.startswith("Custom::")
        and r.cfn_type != "AWS::CloudFormation::CustomResource"
    }
    tf_res = {
        (r.stack, r.logical_id): r
        for r in tf_side[0]
        if not r.cfn_type.startswith("Custom::")
        and r.cfn_type != "AWS::CloudFormation::CustomResource"
    }
    report.only_cfn = sorted(cfn_res[k].identity for k in set(cfn_res) - set(tf_res))
    report.only_tf = sorted(tf_res[k].identity for k in set(tf_res) - set(cfn_res))
    pairs = [(cfn_res[k], tf_res[k]) for k in sorted(set(cfn_res) & set(tf_res))]
    subs: list[tuple[str, str]] = []
    for path in sorted(set(cfn_side[1]) | set(tf_side[1]), key=len, reverse=True):
        tag = f"<STACK:{path}>" if path else "<STACK>"
        subs.append((cfn_side[1].get(path, ""), tag))
        subs.append((tf_side[1].get(path, ""), tag))
    for a, b in pairs:
        subs.append((a.physical_id, f"<{a.identity}>"))
        subs.append((b.physical_id, f"<{a.identity}>"))
    subs.sort(key=lambda s: -len(s[0]))
    used: set[tuple[str, str]] = set()
    for a, b in pairs:
        if a.cfn_type == STACK_TYPE:
            continue
        if a.cfn_type != b.cfn_type:
            report.differences.append(
                (a.identity, "Type", f"{a.cfn_type} vs {b.cfn_type}")
            )
            continue
        left, right = (
            read(cc, a.cfn_type, a.physical_id),
            read(cc, b.cfn_type, b.physical_id),
        )
        if left is None or right is None:
            report.unreadable.append(f"{a.identity} ({a.cfn_type})")
            continue
        if a.cfn_type == "AWS::Lambda::Function":
            for side, name in ((left, a.physical_id), (right, b.physical_id)):
                code = lam.get_function(FunctionName=name)["Code"]
                side["Code"] = {
                    "ImageUri": code.get("ImageUri"),
                    "Location": "<s3>" if "Location" in code else None,
                }
        report.compared += 1
        found = diff(normalise(left, subs), normalise(right, subs))
        unexplained = []
        for prop, text in found:
            key = explained_by(baseline, a.cfn_type, prop)
            if key:
                used.add(key)
                report.explained.append((a.identity, prop, text))
            else:
                unexplained.append((a.identity, prop, text))
        report.differences.extend(unexplained)
        log(
            f"{a.identity} ({a.cfn_type}): "
            + ("same" if not unexplained else f"{len(unexplained)} difference(s)")
        )
        for _, prop, text in unexplained:
            log(f"    {prop}: {text}")
    report.stale = sorted({(e["type"], e["path"]) for e in baseline} - used)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cfn-stack", required=True)
    ap.add_argument("--tf-stack", required=True, help="the Terraform label stack")
    ap.add_argument("--region")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    args = ap.parse_args(argv)
    import boto3

    session = boto3.Session(region_name=args.region)
    cfn = session.client("cloudformation")
    baseline = json.loads(args.baseline.read_text())["entries"]
    report = compare(
        cfn_tree(cfn, args.cfn_stack),
        tf_tree(cfn, session.client("s3"), args.tf_stack),
        session.client("cloudcontrol"),
        session.client("lambda"),
        baseline,
    )
    print(
        f"\n{report.compared} resources compared; {len(report.differences)} "
        f"unexplained and {len(report.explained)} explained difference(s)"
    )
    for label, items in (
        ("only in CloudFormation", report.only_cfn),
        ("only in Terraform", report.only_tf),
        ("not readable through Cloud Control", report.unreadable),
        ("stale baseline entries", report.stale),
    ):
        if items:
            print(f"{label}: {len(items)}")
            for i in items:
                print(f"    {i}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
