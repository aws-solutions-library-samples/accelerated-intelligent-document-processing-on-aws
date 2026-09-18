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

USAGE
-----
  python3 scripts/sdlc/generate_api_rbac_manifest.py           # (re)write the JSON
  python3 scripts/sdlc/generate_api_rbac_manifest.py --check    # CI drift guard

EXIT CODES
----------
  0  wrote the manifest (default), or --check found no drift
  1  --check found drift (regenerated manifest differs from the committed file)
  2  usage / file-not-found / invalid expectations entry
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

# The two non-list policies the expectations file uses, carried through verbatim
# so the runtime does not have to reinterpret them:
#   ANY       — any authenticated Cognito caller (row/ownership scoping only)
#   IAM_ONLY  — backend/IAM principals only; every Cognito caller is rejected
SENTINELS = ("ANY", "IAM_ONLY")


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
    with path.open() as fh:
        return yaml.safe_load(fh)


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
            if groups not in SENTINELS:
                raise GeneratorError(
                    f"{field}: groups '{groups}' is not a list or one of {SENTINELS}"
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
            print(
                f"ERROR: committed manifest missing: {MANIFEST_OUT}\n"
                "Run: python3 scripts/sdlc/generate_api_rbac_manifest.py",
                file=sys.stderr,
            )
            return 1
        if MANIFEST_OUT.read_text() != rendered:
            print(
                "DRIFT: api_rbac_manifest.json is out of date with "
                "scripts/api_rbac_expectations.yaml.\n"
                "Regenerate: python3 scripts/sdlc/generate_api_rbac_manifest.py",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {MANIFEST_OUT.name} matches api_rbac_expectations.yaml "
              f"({count} operations)")
        return 0

    MANIFEST_OUT.write_text(rendered)
    print(f"Wrote {MANIFEST_OUT} ({count} operations)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
