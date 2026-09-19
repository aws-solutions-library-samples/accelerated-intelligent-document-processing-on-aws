#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Build-time generator for the HTTP API dispatcher's required-groups manifest.

WHY
---
Under AppSync, ``@aws_cognito_user_pools(cognito_groups: [...])`` gated a field
at the API layer *before* the resolver ran, and a field the caller's groups did
not satisfy was rejected there. The REST API that replaced AppSync authenticates
at the gateway and evaluates no groups, so authorization became opt-in per
resolver: an operation whose resolver forgets its check is reachable by any
authenticated user, and a NEW operation is open unless somebody remembers to add
one. That is the opposite default from the schema it replaced.

The dispatcher therefore enforces a group floor of its own (``authz.py``), and it
needs the required groups for every routable field at runtime. This script
precomputes them into ``api_rbac_manifest.json``, committed in the dispatcher's
CodeUri (so SAM bundles it) and read with stdlib ``json`` — no PyYAML in the
Lambda bundle and no SSM/DynamoDB lookup on the request path.

SOURCE OF TRUTH
---------------
``scripts/api_rbac_expectations.yaml`` — the file that already declares one entry
per routable operation and is already validated by ``scan_api_rbac.py``:

  * S1 fails if a routable op has no entry, or an entry names no routable op, so
    the manifest cannot miss an operation or carry a stale one;
  * S2 cross-checks each entry against the ``schema.graphql`` group directive;
  * ``scripts/test_api_rbac.py`` drives the deployed API as every group and
    asserts the same entries live.

Deriving the manifest from that file — rather than hand-maintaining a second
list next to it — is the point: a third copy of the policy is the defect this
repo has already been bitten by, and drift between copies is invisible until
somebody is either denied or let in.

The one policy this script does not merely copy is ``ANY_GROUP`` ("authenticated
AND in at least one group this stack creates"), which is resolved here into the
concrete group names declared by ``AWS::Cognito::UserPoolGroup`` in
``template.yaml``. See the note beside the ``ANY_GROUP`` constant below for why
the vocabulary is read from the template on every build rather than spelled out
per operation, and why the expansion happens here rather than in the Lambda.

USAGE
-----
  python3 scripts/sdlc/generate_api_rbac_manifest.py           # (re)write the JSON
  python3 scripts/sdlc/generate_api_rbac_manifest.py --check    # CI drift guard

EXIT CODES
----------
  0  wrote the manifest (default), or --check found no drift
  1  --check found drift (regenerated manifest differs from the committed file)
  2  usage / file-not-found / invalid expectations entry

The 1-vs-2 split is the point: 1 means "the two files disagree, regenerate", which
is the routine signal a developer gets after editing the expectations file, while
2 means "something is missing or malformed", which regenerating will not fix. Both
are non-zero, so ``make api-test-static`` fails either way, but a script or a
human reading the code should not have to guess which condition it hit.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXPECTATIONS = REPO / "scripts" / "api_rbac_expectations.yaml"
ROOT_TEMPLATE = REPO / "template.yaml"
MANIFEST_OUT = (
    REPO
    / "nested"
    / "api-resolvers"
    / "src"
    / "lambda"
    / "http_api_dispatcher"
    / "api_rbac_manifest.json"
)

# Manifest schema version. The dispatcher refuses a manifest it does not
# understand rather than guessing at a shape it was not written for.
MANIFEST_VERSION = 1

# The two non-list policies that appear in the MANIFEST, carried through verbatim
# so the runtime does not have to reinterpret them:
#   ANY       — any authenticated Cognito caller (row/ownership scoping only)
#   IAM_ONLY  — backend/IAM principals only; every Cognito caller is rejected
SENTINELS = ("ANY", "IAM_ONLY")

# A third policy the EXPECTATIONS file may declare, which is resolved here and
# therefore never reaches the manifest:
#   ANY_GROUP — authenticated AND holding at least one of the groups this stack
#               creates. Expanded to the concrete list of
#               ``AWS::Cognito::UserPoolGroup`` names in ``template.yaml``.
#
# WHY A SENTINEL RATHER THAN WRITING THE FIVE GROUP NAMES IN THE YAML
# -------------------------------------------------------------------
# "At least one assigned group" is a statement about the group vocabulary, not
# about five particular names. Spelling the names out would be transparent but
# would silently stop covering a SIXTH group added to ``template.yaml`` later:
# the operation would keep naming five groups while the deployment had six, and
# nothing would say so. That is the "fix applied to the instance and not the
# class" defect this repository keeps hitting. The sentinel is evaluated against
# the template on every build, so a new group joins the set by construction.
#
# WHY IT IS EXPANDED HERE RATHER THAN CARRIED TO THE RUNTIME
# ----------------------------------------------------------
# The Lambda has no copy of ``template.yaml``, so it could not resolve the
# vocabulary itself — it could only implement the weaker "holds any group at
# all", which is a different policy. Expanding at build time means the runtime
# keeps exactly one comparison (list intersection, already fail-closed and
# already tested) and gains no new concept. It also means an ``ANY_GROUP``
# string reaching ``api_rbac_manifest.json`` is a build fault, and ``authz.py``
# treats it as one: it is not in the manifest's sentinel set, so the whole
# manifest is rejected and every operation is denied, rather than the unknown
# policy being read as permissive.
ANY_GROUP = "ANY_GROUP"

# Every policy string the expectations file may use. An unrecognised one is a
# hard error (exit 2), never a default.
EXPECTATION_SENTINELS = SENTINELS + (ANY_GROUP,)


class GeneratorError(Exception):
    """An expectations entry the manifest cannot be built from."""


def _read(path: Path) -> str:
    if not path.exists():
        print(f"ERROR: expected file not found: {path}", file=sys.stderr)
        sys.exit(2)
    return path.read_text()


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # PyYAML — repo tooling dependency, build-time only
    except ImportError:  # pragma: no cover
        print("ERROR: PyYAML is required (pip install pyyaml).", file=sys.stderr)
        sys.exit(2)
    # Read through _read so a missing expectations file exits 2 with the path
    # named, as the EXIT CODES block above promises, rather than raising an
    # unhandled FileNotFoundError and exiting 1 with a traceback.
    return yaml.safe_load(_read(path))


def cognito_group_names(template_text: str) -> set[str]:
    """Group names the stack actually creates (``AWS::Cognito::UserPoolGroup``).

    Used to reject a typo in the expectations file: a required group that is not
    a real Cognito group can never appear in a caller's claim, so the operation
    would be denied to everyone once the dispatcher enforces the manifest. That
    failure is indistinguishable from a deliberate lockdown, so catch it here.
    """
    names: set[str] = set()
    for m in re.finditer(r"Type:\s*AWS::Cognito::UserPoolGroup\b", template_text):
        # The literal GroupName inside this resource's block. A `!Ref` value
        # belongs to a group ATTACHMENT, not a group, and is skipped.
        gm = re.search(
            r"GroupName:\s*([A-Za-z]\w*)\s*$", template_text[m.end() :], re.M
        )
        if gm:
            names.add(gm.group(1))
    return names


def build_manifest(spec: dict, valid_groups: set[str] | None = None) -> dict:
    """Reduce the expectations file to {field: ["Group", ...] | "ANY" | "IAM_ONLY"}.

    Only the ``groups`` policy is carried over. Everything else in an entry
    (``kind``, ``enforced_in``, ``scope_checked``, ``args``, ``known_gap``, …)
    describes how the policy is tested or scoped, which is a build-time and
    test-time concern; the request path needs the group floor and nothing else.

    ``ANY_GROUP`` is the one policy that is *resolved* rather than copied: it
    becomes the sorted list of ``valid_groups``, i.e. the groups the stack
    actually creates. See the note beside ``ANY_GROUP`` above.
    """
    ops = (spec or {}).get("operations") or {}
    if not ops:
        raise GeneratorError("api_rbac_expectations.yaml declares no operations")

    operations: dict[str, object] = {}
    for field, entry in ops.items():
        if "groups" not in (entry or {}):
            raise GeneratorError(f"{field}: entry has no 'groups' key")
        groups = entry["groups"]
        if isinstance(groups, str):
            if groups == ANY_GROUP:
                # Resolved against the template, never against a list in this
                # script: the vocabulary is the stack's. An empty vocabulary
                # would expand to an empty list, which the runtime rejects as a
                # malformed policy and which reads as "denied to everyone" — so
                # refuse to emit it and name the cause instead.
                if not valid_groups:
                    raise GeneratorError(
                        f"{field}: groups '{ANY_GROUP}' means 'any group this "
                        "stack creates', but no AWS::Cognito::UserPoolGroup was "
                        f"found in {ROOT_TEMPLATE.name}, so it cannot be "
                        "resolved"
                    )
                operations[field] = sorted(valid_groups)
                continue
            if groups not in SENTINELS:
                raise GeneratorError(
                    f"{field}: groups '{groups}' is not a list or one of "
                    f"{EXPECTATION_SENTINELS}"
                )
            operations[field] = groups
            continue
        if not isinstance(groups, list) or not groups:
            raise GeneratorError(
                f"{field}: groups must be a non-empty list or one of {SENTINELS}"
            )
        unknown = {g for g in groups if not isinstance(g, str)}
        if unknown:
            raise GeneratorError(f"{field}: non-string group name(s) {unknown}")
        if valid_groups:
            bogus = sorted(set(groups) - valid_groups)
            if bogus:
                raise GeneratorError(
                    f"{field}: requires group(s) {bogus} that the stack does not "
                    f"create (known groups: {sorted(valid_groups)}) — an "
                    "unmatchable group denies the operation to everyone"
                )
        # Sorted so the rendered manifest is stable under reordering in the YAML.
        operations[field] = sorted(set(groups))

    return {
        "_comment": (
            "GENERATED FILE - do not edit. Required Cognito groups per API "
            "operation, enforced by the dispatcher (authz.py). Source of truth: "
            "scripts/api_rbac_expectations.yaml. Regenerate with "
            "scripts/sdlc/generate_api_rbac_manifest.py."
        ),
        "generated_from": "scripts/api_rbac_expectations.yaml",
        "operations": operations,
        "version": MANIFEST_VERSION,
    }


def _dump(manifest: dict) -> str:
    """Deterministic serialization (stable key order, trailing newline)."""
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def render() -> str:
    """The manifest text the committed file must equal."""
    spec = _load_yaml(EXPECTATIONS)
    valid_groups = cognito_group_names(_read(ROOT_TEMPLATE))
    return _dump(build_manifest(spec, valid_groups))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate the HTTP API dispatcher required-groups manifest."
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="regenerate in memory and fail (exit 1) if it differs from the "
        "committed api_rbac_manifest.json (CI drift guard)",
    )
    args = ap.parse_args()

    try:
        rendered = render()
    except GeneratorError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    count = len(json.loads(rendered)["operations"])

    if args.check:
        if not MANIFEST_OUT.exists():
            # 2, not 1: the committed manifest being absent is the
            # file-not-found class, not the drift class. See EXIT CODES above.
            print(
                f"ERROR: committed manifest missing: {MANIFEST_OUT}\n"
                "Run: python3 scripts/sdlc/generate_api_rbac_manifest.py",
                file=sys.stderr,
            )
            return 2
        if MANIFEST_OUT.read_text() != rendered:
            print(
                "DRIFT: api_rbac_manifest.json is out of date with "
                "scripts/api_rbac_expectations.yaml.\n"
                "Regenerate: python3 scripts/sdlc/generate_api_rbac_manifest.py",
                file=sys.stderr,
            )
            return 1
        print(
            f"OK: {MANIFEST_OUT.name} matches api_rbac_expectations.yaml "
            f"({count} operations)"
        )
        return 0

    MANIFEST_OUT.write_text(rendered)
    print(f"Wrote {MANIFEST_OUT} ({count} operations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
