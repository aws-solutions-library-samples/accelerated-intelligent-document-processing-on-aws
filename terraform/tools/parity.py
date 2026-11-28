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
every paired physical id, and the name inside a paired ARN or queue URL) become
placeholders wherever they are a whole token of a value, JSON text is parsed,
lists are sorted, a KMS key ARN becomes its key id, an empty ``VpcConfig`` is
dropped, and tags each tool adds itself are dropped. What still differs is a
finding, unless ``parity_baseline.json`` explains it: one entry per (type,
property path), with its reason. An entry that explained nothing in the run is
reported as stale.

It exits 1 on any unexplained difference, on a resource present on one side
only, and on a stale entry. Custom resources are not compared (they are not
resources Cloud Control can read; their effects are). A type Cloud Control
cannot read is listed, not compared. A type whose Cloud Control identifier is
compound (``UserPoolId|GroupName`` for a user pool group, where CloudFormation's
physical id is the group name alone) is read with that identifier, built from
the type's primary identifier and the resource's template properties; one it
cannot be built for, or that Cloud Control refuses, is listed as unreadable.
A per-target policy (``AWS::SQS::QueuePolicy``, ``AWS::IAM::Policy``) has no
single Terraform resource, so it is compared through its targets: the queue's
``Policy`` attribute and the role's inline ``Policies``.

It only reads. Offline tests drive it with recorded responses
(``terraform/translator/tests/test_parity.py``).
"""

from __future__ import annotations

import argparse
import json
import re
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
    "PolicyId",
    "CreatedAt",
    "UpdatedAt",
    "CreateDate",
    "UpdateDate",
    "LastModifiedTime",
}
#: Tags each tool adds on its own.
TOOL_TAG_PREFIXES = ("aws:cloudformation:", "idp:stack-name", "idp:prototype")
STACK_TYPE = "AWS::CloudFormation::Stack"
#: Per-target policy types: Terraform writes one resource per target and no
#: directory entry, so each is compared through what it is applied to. The value
#: names where that comparison happens.
PER_TARGET_POLICY_TYPES = {
    "AWS::IAM::Policy": "the inline Policies Cloud Control reads with each role",
    "AWS::SQS::QueuePolicy": "the Policy attribute read with each queue",
}
#: Cloud Control error codes that mean "this type, or this identifier, cannot be
#: read": the resource is listed as unreadable, and the run goes on.
UNREADABLE_CODES = (
    "UnsupportedActionException",
    "TypeNotFoundException",
    "ValidationException",
    "InvalidRequestException",
)
#: The shortest name read out of an ARN or URL that is substituted on its own;
#: below this a name is too likely to be an ordinary word.
MIN_ALIAS_LENGTH = 8
KMS_KEY_ARN = re.compile(r"^arn:[^:]+:kms:[^:]*:[^:]*:key/(.+)$")


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
    #: Per-target policies, compared through their targets (PER_TARGET_POLICY_TYPES).
    through_target: list[str] = field(default_factory=list)
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
class Substitutions:
    """Names that necessarily differ, each replaced by its placeholder wherever
    it is a whole token of a value: not preceded or followed by a letter or
    digit. Every name is replaced in one pass, longest first, so a placeholder
    is never rewritten and a short name (a group called ``Author``) does not
    rewrite the inside of a longer word (``Authorization``)."""

    def __init__(self, pairs: list[tuple[str, str]]):
        self.mapping: dict[str, str] = {}
        for old, new in pairs:
            if old:
                self.mapping.setdefault(old, new)
        names = sorted(self.mapping, key=len, reverse=True)
        self.pattern = (
            re.compile(
                r"(?<![A-Za-z0-9])(?:"
                + "|".join(re.escape(n) for n in names)
                + r")(?![A-Za-z0-9])"
            )
            if names
            else None
        )

    def apply(self, value: str) -> str:
        if self.pattern is None:
            return value
        return self.pattern.sub(lambda m: self.mapping[m.group(0)], value)


def aliases(physical_id: str) -> list[str]:
    """The other spellings of a physical id that appear in values: the name at
    the end of an ARN or URL (a queue's URL holds the name its ARN and its
    ``QueueName`` carry), and each part of a compound ``a|b`` id."""
    out: list[str] = []
    if physical_id.startswith(("arn:", "https://")):
        out.append(re.split(r"[/:]", physical_id.rstrip("/"))[-1])
    if "|" in physical_id:
        out.extend(physical_id.split("|"))
    return [a for a in out if len(a) >= MIN_ALIAS_LENGTH and a != physical_id]


def _is_tag_property(key: str) -> bool:
    return key == "Tags" or key.endswith("Tags")


def _without_tool_tags(tags: Any) -> Any:
    """A tag property (``Tags``, ``UserPoolTags``, ...) without the tags a tool
    adds itself, in either shape Cloud Control returns: a list of Key/Value
    pairs or a key-to-value map."""
    if isinstance(tags, list):
        return [
            t
            for t in tags
            if not (
                isinstance(t, dict)
                and str(t.get("Key", "")).startswith(TOOL_TAG_PREFIXES)
            )
        ]
    if isinstance(tags, dict):
        return {
            k: v for k, v in tags.items() if not str(k).startswith(TOOL_TAG_PREFIXES)
        }
    return tags


def _is_empty_vpc_config(value: Any) -> bool:
    """A ``VpcConfig`` with no subnets and no security groups is no VPC: one
    tool reports it with empty lists, the other omits it."""
    return isinstance(value, dict) and not any(value.values())


def normalise(value: Any, subs: Substitutions | list[tuple[str, str]]) -> Any:
    if not isinstance(subs, Substitutions):
        subs = Substitutions(subs)
    if isinstance(value, dict):
        out = {}
        for k, v in sorted(value.items()):
            if k in VOLATILE or v == "":
                continue
            if _is_tag_property(k):
                v = _without_tool_tags(v)
                if v in ([], {}):
                    continue
            if k == "VpcConfig" and _is_empty_vpc_config(v):
                continue
            out[k] = normalise(v, subs)
        return out
    if isinstance(value, list):
        items = [normalise(v, subs) for v in value]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(value, str):
        value = subs.apply(value)
        key = KMS_KEY_ARN.match(value)
        if key:
            # A key ARN and its key id name the same key; which one a property
            # holds depends on how it was written, not on the key.
            return key.group(1)
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
    if isinstance(a, list) and isinstance(b, list) and a != b:
        only_a = [x for x in a if x not in b]
        only_b = [x for x in b if x not in a]
        if only_a or only_b:
            # The elements that differ rather than both whole lists, which a
            # truncated line would otherwise cut before the difference.
            return [
                (
                    path,
                    f"only in cloudformation={json.dumps(only_a)[:200]} "
                    f"only in terraform={json.dumps(only_b)[:200]}",
                )
            ]
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
def _error_code(exc: Exception) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def read(cc: Any, cfn_type: str, ident: str | None) -> dict | None:
    """The resource's properties through Cloud Control, or None when the type or
    the identifier is not one it can read."""
    if not ident:
        return None
    try:
        resp = cc.get_resource(TypeName=cfn_type, Identifier=ident)
    except Exception as exc:  # noqa: BLE001 - unreadable types are listed
        if _error_code(exc) in UNREADABLE_CODES:
            return None
        raise
    return json.loads(resp["ResourceDescription"]["Properties"])


class Identifiers:
    """Cloud Control identifiers for one side of the comparison.

    Most types are read by the physical id CloudFormation reports. A type whose
    primary identifier has several properties is read by those properties'
    values joined with ``|``, in the schema's order (``DescribeType``). Each
    value comes from the resource's properties in the CloudFormation template,
    resolved against the same side's resources: a literal, a ``Ref`` to a
    resource or (through the parent stack) to a nested stack parameter, or a
    ``GetAtt`` read through Cloud Control. One property the template does not
    set (an id the service assigns) is the physical id. The Terraform side uses
    the CloudFormation twin's templates, which are the same release's.
    """

    def __init__(
        self,
        cfn: Any,
        cc: Any,
        side: tuple[list[Resource], dict],
        templates: Callable[[str], dict],
    ):
        self.cfn, self.cc = cfn, cc
        self.by_key = {(r.stack, r.logical_id): r for r in side[0]}
        self.templates = templates
        self._schemas: dict[str, list[str] | None] = {}

    def primary(self, cfn_type: str) -> list[str] | None:
        if cfn_type not in self._schemas:
            try:
                schema = json.loads(
                    self.cfn.describe_type(Type="RESOURCE", TypeName=cfn_type)["Schema"]
                )
                found = [
                    p.removeprefix("/properties/")
                    for p in schema.get("primaryIdentifier", [])
                ]
            except Exception:  # noqa: BLE001 - an unknown schema reads as before
                found = None
            self._schemas[cfn_type] = found
        return self._schemas[cfn_type]

    def resolve(self, stack: str, value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if not isinstance(value, dict) or len(value) != 1:
            return None
        if "Ref" in value:
            name = value["Ref"]
            res = self.by_key.get((stack, name))
            if res is not None:
                return res.physical_id or None
            if not stack:
                return None
            parent, _, child = stack.rpartition("/")
            nested = self.templates(parent).get("Resources", {}).get(child, {})
            passed = nested.get("Properties", {}).get("Parameters", {})
            return self.resolve(parent, passed[name]) if name in passed else None
        if "Fn::GetAtt" in value:
            target = value["Fn::GetAtt"]
            if isinstance(target, str):
                target = target.split(".", 1)
            if not isinstance(target, list) or len(target) != 2:
                return None
            res = self.by_key.get((stack, target[0]))
            if res is None or res.cfn_type == STACK_TYPE:
                return None
            props = read(self.cc, res.cfn_type, res.physical_id) or {}
            found = props.get(target[1])
            return found if isinstance(found, str) else None
        return None

    def identifier(self, res: Resource) -> str | None:
        names = self.primary(res.cfn_type)
        if not names or len(names) == 1:
            return res.physical_id
        if res.physical_id.count("|") == len(names) - 1:
            return res.physical_id
        if any("/" in n for n in names):
            return None
        template = self.templates(res.stack).get("Resources", {}).get(res.logical_id)
        props = (template or {}).get("Properties", {})
        values = [
            self.resolve(res.stack, props[n]) if n in props else None for n in names
        ]
        missing = [i for i, v in enumerate(values) if v is None]
        if len(missing) > 1:
            return None
        if missing:
            values[missing[0]] = res.physical_id
        return "|".join(str(v) for v in values)


def cfn_templates(cfn: Any, names: dict) -> Callable[[str], dict]:
    """``path -> template`` for the CloudFormation side, read once per stack;
    an empty template when one cannot be read."""
    cache: dict[str, dict] = {}

    def template(path: str) -> dict:
        if path not in cache:
            body: Any = {}
            try:
                body = cfn.get_template(
                    StackName=names[path], TemplateStage="Processed"
                )["TemplateBody"]
                if isinstance(body, str):
                    body = json.loads(body)
            except Exception:  # noqa: BLE001 - YAML, missing or denied
                body = {}
            cache[path] = body if isinstance(body, dict) else {}
        return cache[path]

    return template


def queue_policy(sqs: Any, url: str) -> str | None:
    """A queue's ``Policy`` attribute: Cloud Control's queue omits it."""
    return (
        sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["Policy"])
        .get("Attributes", {})
        .get("Policy")
    )


def compare(
    cfn_side: tuple[list[Resource], dict],
    tf_side: tuple[list[Resource], dict],
    cc: Any,
    lam: Any,
    baseline: list[dict],
    log: Callable[[str], None] = print,
    cfn: Any = None,
    sqs: Any = None,
) -> Report:
    """Compare the two sides. ``cfn`` (a CloudFormation client) is what builds
    compound Cloud Control identifiers; without it every resource is read by
    its physical id. ``sqs`` reads each queue's policy, which is how an
    ``AWS::SQS::QueuePolicy`` is compared; without it a queue policy is a
    resource only in CloudFormation."""
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
    through_target = {"AWS::IAM::Policy"} | (
        {"AWS::SQS::QueuePolicy"} if sqs else set()
    )
    per_target = [
        cfn_res[k]
        for k in sorted(set(cfn_res) - set(tf_res))
        if cfn_res[k].cfn_type in through_target
    ]
    report.only_cfn = sorted(
        cfn_res[k].identity
        for k in set(cfn_res) - set(tf_res)
        if cfn_res[k].cfn_type not in through_target
    )
    report.only_tf = sorted(tf_res[k].identity for k in set(tf_res) - set(cfn_res))
    pairs = [(cfn_res[k], tf_res[k]) for k in sorted(set(cfn_res) & set(tf_res))]
    subs: list[tuple[str, str]] = []
    for path in sorted(set(cfn_side[1]) | set(tf_side[1]), key=len, reverse=True):
        tag = f"<STACK:{path}>" if path else "<STACK>"
        subs.append((cfn_side[1].get(path, ""), tag))
        subs.append((tf_side[1].get(path, ""), tag))
    # Every resource both sides hold names a placeholder, custom resources
    # included: they are not compared, but their ids (a knowledge base's, say)
    # appear in the properties of resources that are.
    cfn_all = {(r.stack, r.logical_id): r for r in cfn_side[0]}
    tf_all = {(r.stack, r.logical_id): r for r in tf_side[0]}
    both = sorted(set(cfn_all) & set(tf_all))
    for k in both:
        a, b = cfn_all[k], tf_all[k]
        subs.append((a.physical_id, f"<{a.identity}>"))
        subs.append((b.physical_id, f"<{a.identity}>"))
    for k in both:
        a, b = cfn_all[k], tf_all[k]
        if a.cfn_type == STACK_TYPE:
            continue  # a stack ARN ends in its id, or in "terraform"
        subs.extend((alias, f"<{a.identity}>") for alias in aliases(a.physical_id))
        subs.extend((alias, f"<{a.identity}>") for alias in aliases(b.physical_id))
    names = Substitutions(subs)
    ids_cfn = ids_tf = None
    if cfn is not None:
        templates = cfn_templates(cfn, cfn_side[1])
        ids_cfn = Identifiers(cfn, cc, cfn_side, templates)
        ids_tf = Identifiers(cfn, cc, tf_side, templates)

    def ident(side: Identifiers | None, res: Resource) -> str | None:
        return side.identifier(res) if side else res.physical_id

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
            read(cc, a.cfn_type, ident(ids_cfn, a)),
            read(cc, b.cfn_type, ident(ids_tf, b)),
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
        if a.cfn_type == "AWS::SQS::Queue" and sqs is not None:
            try:
                policies = (
                    queue_policy(sqs, a.physical_id),
                    queue_policy(sqs, b.physical_id),
                )
            except Exception:  # noqa: BLE001 - listed, the rest still compared
                report.unreadable.append(f"{a.identity} (AWS::SQS::Queue Policy)")
                through_target.discard("AWS::SQS::QueuePolicy")
            else:
                for side, policy in zip((left, right), policies):
                    if policy:
                        side["Policy"] = policy
        report.compared += 1
        found = diff(normalise(left, names), normalise(right, names))
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
    # A queue policy counts as compared only if every queue's policy was read.
    for res in per_target:
        if res.cfn_type in through_target:
            report.through_target.append(
                f"{res.identity} ({res.cfn_type}: "
                f"{PER_TARGET_POLICY_TYPES[res.cfn_type]})"
            )
        else:
            report.only_cfn.append(res.identity)
    report.only_cfn.sort()
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
        cfn=cfn,
        sqs=session.client("sqs"),
    )
    print(
        f"\n{report.compared} resources compared; {len(report.differences)} "
        f"unexplained and {len(report.explained)} explained difference(s)"
    )
    for label, items in (
        ("only in CloudFormation", report.only_cfn),
        ("only in Terraform", report.only_tf),
        ("compared through their targets", report.through_target),
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
