"""Compare a Terraform deployment of a translated template with its CloudFormation twin.

For every resource in the CloudFormation stack, read the live properties of
both copies through the Cloud Control API and diff them after replacing the
names that necessarily differ (stack names, physical ids) with placeholders.

Usage:
  python compare.py --cfn-stack NAME --tf-dir DIR --module module.tf \
      --report translation-report.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

import boto3

# Terraform attribute that holds the Cloud Control identifier, per tf type.
TF_IDENTIFIER = {
    "aws_iam_role": "name",
    "aws_lambda_function": "function_name",
    "aws_cloudwatch_log_group": "name",
    "aws_codebuild_project": "name",
}
# Read-only or volatile properties that differ between any two deployments.
IGNORE = {
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
# The Cloud Control read model returns no Code for a function; compare these
# through the Lambda API instead.
LAMBDA_EXTRA = ("PackageType", "ImageUri", "CodeSha256")


def tf_state(tf_dir: str) -> dict[str, Any]:
    out = subprocess.run(
        ["terraform", "show", "-json"],
        cwd=tf_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def tf_resources(state: dict[str, Any], module: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def walk(mod: dict[str, Any]) -> None:
        if mod.get("address") == module:
            for r in mod.get("resources", []):
                found[r["address"].removeprefix(module + ".")] = r
        for child in mod.get("child_modules", []):
            walk(child)

    walk(state["values"]["root_module"])
    return found


def cc_get(cc: Any, type_name: str, ident: str) -> dict[str, Any] | None:
    try:
        resp = cc.get_resource(TypeName=type_name, Identifier=ident)
    except cc.exceptions.UnsupportedActionException:
        return None
    except Exception as exc:  # noqa: BLE001 - report and carry on
        return {"__error__": str(exc)}
    return json.loads(resp["ResourceDescription"]["Properties"])


# Tags each tool adds on its own: CloudFormation's aws:cloudformation:* set and
# the prototype fixture's provider default_tags.
TOOL_TAG_PREFIXES = ("aws:cloudformation:", "idp:prototype")

# Explicit values one side stores where the other stores nothing, and which
# are the service's own default. Each entry is one (type, property path).
EXPLAINED_DEFAULTS = {
    (
        "AWS::CodeBuild::Project",
        ".Artifacts.OverrideArtifactName",
    ): "provider writes the service default false",
    (
        "AWS::CodeBuild::Project",
        ".LogsConfig",
    ): "provider writes the service default: CloudWatch on, S3 off",
}


def normalise(value: Any, subs: list[tuple[str, str]]) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in sorted(value.items()):
            if k in IGNORE or v == "":
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
        try:
            return sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
        except TypeError:
            return items
    if isinstance(value, str):
        for old, new in subs:
            value = value.replace(old, new)
        # Policy documents come back URL-decoded JSON strings from some types.
        if value[:1] in "{[":
            try:
                return normalise(json.loads(value), subs)
            except ValueError:
                pass
        return value
    return value


def diff(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: only in terraform = {json.dumps(b[k])[:200]}")
            elif k not in b:
                out.append(
                    f"{path}.{k}: only in cloudformation = {json.dumps(a[k])[:200]}"
                )
            else:
                out.extend(diff(a[k], b[k], f"{path}.{k}"))
        return out
    if a != b:
        return [
            f"{path}: cloudformation={json.dumps(a)[:200]} "
            f"terraform={json.dumps(b)[:200]}"
        ]
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfn-stack", required=True)
    ap.add_argument("--tf-stack-name", required=True)
    ap.add_argument("--tf-dir", required=True)
    ap.add_argument("--module", default="module.tf")
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    report = json.load(open(args.report))["resources"]
    cfn = boto3.client("cloudformation")
    cc = boto3.client("cloudcontrol")
    lam = boto3.client("lambda")
    tf = tf_resources(tf_state(args.tf_dir), args.module)

    pairs = []
    for page in cfn.get_paginator("list_stack_resources").paginate(
        StackName=args.cfn_stack
    ):
        for r in page["StackResourceSummaries"]:
            lid, rtype, phys = (
                r["LogicalResourceId"],
                r["ResourceType"],
                r.get("PhysicalResourceId", ""),
            )
            if rtype.startswith("Custom::"):
                continue
            entry = report.get(lid)
            if entry is None:
                print(f"{lid}: in the CloudFormation stack but not translated")
                continue
            res = tf.get(entry["address"])
            if res is None:
                print(f"{lid}: no Terraform resource at {entry['address']}")
                continue
            vals = res["values"]
            ident = vals.get(TF_IDENTIFIER.get(res["type"], "id"))
            pairs.append((lid, rtype, phys, ident))

    subs = [(args.cfn_stack, "<STACK>"), (args.tf_stack_name, "<STACK>")]
    for lid, _, phys, ident in pairs:
        subs.append((phys, f"<{lid}>"))
        subs.append((str(ident), f"<{lid}>"))
    subs.sort(key=lambda s: -len(s[0]))

    total = 0
    for lid, rtype, phys, ident in pairs:
        a = cc_get(cc, rtype, phys)
        b = cc_get(cc, rtype, str(ident))
        if a is None or b is None:
            print(f"{lid} ({rtype}): not readable through Cloud Control; skipped")
            continue
        if rtype == "AWS::Lambda::Function":
            for side, name in ((a, phys), (b, str(ident))):
                cfg = lam.get_function(FunctionName=name)
                side["Code"] = {
                    "ImageUri": cfg["Code"].get("ImageUri"),
                    "Location": "<s3>" if "Location" in cfg["Code"] else None,
                }
        found = [
            f
            for f in diff(normalise(a, subs), normalise(b, subs))
            if (rtype, f.split(":", 1)[0]) not in EXPLAINED_DEFAULTS
        ]
        total += len(found)
        status = "same" if not found else f"{len(found)} difference(s)"
        print(f"{lid} ({rtype}): {status}")
        for f in found:
            print(f"    {f}")
    print(f"\n{len(pairs)} resources compared, {total} differences")
    return 0


if __name__ == "__main__":
    sys.exit(main())
