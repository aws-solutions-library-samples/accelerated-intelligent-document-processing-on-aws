"""Regenerate the vendored awscc schema subset the translator reads.

The full provider schema is about 17 MB; the translator needs only the resource
types the templates use. This script reads a full schema (the output of
``terraform providers schema -json`` for a pinned ``hashicorp/awscc``), finds the
types the templates need by translating them offline, and writes just those.

    terraform -chdir=<dir with a versions.tf pinning awscc> init
    terraform -chdir=<dir> providers schema -json > /tmp/awscc-full.json
    python terraform/translator/scripts/refresh_awscc_schema.py \\
        /tmp/awscc-full.json --version <awscc version>

Run it when the translator reports a type missing from the vendored subset, or
to move to a newer provider. Commit the regenerated file and the version.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from idp_tf.awscc_schema import awscc_type_name  # noqa: E402
from idp_tf.cli import TEMPLATE_VARIANTS, cfn_types_in_tree  # noqa: E402

OUT = HERE / "schemas" / "awscc.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("full_schema", type=Path)
    ap.add_argument("--version", required=True, help="the awscc provider version")
    args = ap.parse_args()
    full = json.loads(args.full_schema.read_text())
    key = next(k for k in full["provider_schemas"] if k.endswith("/awscc"))
    resources = full["provider_schemas"][key]["resource_schemas"]
    wanted = set()
    for template, variant in TEMPLATE_VARIANTS:
        for cfn_type in cfn_types_in_tree(REPO / template, variant):
            if cfn_type.count("::") != 2 or cfn_type.startswith("Custom::"):
                continue
            name = awscc_type_name(cfn_type)
            if name in resources:
                wanted.add(name)

    doc_keys = ("description", "description_kind", "deprecated")

    def strip_spec(spec):
        """An attribute or block spec, without its documentation fields. Only
        the spec's own keys are dropped: an attribute can itself be named
        `description`, and that must stay."""
        out = {k: v for k, v in spec.items() if k not in doc_keys}
        if "nested_type" in out:
            out["nested_type"] = strip_block(out["nested_type"])
        return out

    def strip_block(block):
        out = {k: v for k, v in block.items() if k not in doc_keys}
        if "attributes" in out:
            out["attributes"] = {n: strip_spec(a) for n, a in out["attributes"].items()}
        if "block_types" in out:
            out["block_types"] = {
                n: dict(strip_spec(b), block=strip_block(b.get("block", {})))
                for n, b in out["block_types"].items()
            }
        return out

    def strip(resource):
        return dict(resource, block=strip_block(resource.get("block", {})))

    subset = {
        "format_version": full.get("format_version", "1.0"),
        "awscc_version": args.version,
        "provider_schemas": {
            key: {"resource_schemas": {n: strip(resources[n]) for n in sorted(wanted)}}
        },
    }
    OUT.write_text(json.dumps(subset, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT} with {len(wanted)} resource types for awscc {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
