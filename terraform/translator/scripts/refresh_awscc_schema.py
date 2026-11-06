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
    for template in TEMPLATE_VARIANTS:
        for cfn_type in cfn_types_in_tree(REPO / template):
            if cfn_type.count("::") != 2 or cfn_type.startswith("Custom::"):
                continue
            name = awscc_type_name(cfn_type)
            if name in resources:
                wanted.add(name)

    def strip(node):
        # Descriptions are prose the translator never reads; dropping them keeps
        # the file small and free of the documentation's example ARNs.
        if isinstance(node, dict):
            return {
                k: strip(v)
                for k, v in node.items()
                if k not in ("description", "description_kind", "deprecated")
            }
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

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
