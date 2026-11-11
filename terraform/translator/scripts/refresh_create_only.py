"""Regenerate the vendored create-only property list the translator reads.

CloudFormation's resource provider schemas mark the properties a resource
cannot change in place (``createOnlyProperties``): changing one replaces the
resource. The Terraform provider schema the translator also vendors does not
carry that information, so it is read here from the registry schemas that
``cfn-lint`` ships (the same pinned ``cfn-lint`` the CloudFormation gate uses,
``CFN_LINT_VERSION`` in the Makefile), for exactly the types the templates use:

    python terraform/translator/scripts/refresh_create_only.py

Run it when the translator reports a type missing from
``schemas/create_only.json``, or after moving ``cfn-lint`` to a newer release.
Commit the regenerated file. No AWS call is made.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from idp_tf.cli import TEMPLATE_VARIANTS, cfn_types_in_tree  # noqa: E402
from idp_tf.create_only import CREATE_ONLY_FILE  # noqa: E402

REGION = "us-east-1"


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    from importlib.metadata import version

    from cfnlint.schema import PROVIDER_SCHEMA_MANAGER

    wanted: set[str] = set()
    for template, variant in TEMPLATE_VARIANTS:
        for cfn_type in cfn_types_in_tree(REPO / template, variant):
            if (
                cfn_type.startswith("AWS::")
                and cfn_type != "AWS::CloudFormation::Stack"
            ):
                wanted.add(cfn_type)
    types: dict[str, list[str]] = {}
    for cfn_type in sorted(wanted):
        schema = PROVIDER_SCHEMA_MANAGER.get_resource_schema(REGION, cfn_type).schema
        types[cfn_type] = sorted(
            p.removeprefix("/properties/")
            for p in schema.get("createOnlyProperties", [])
        )
    doc = {
        "source": f"cfn-lint {version('cfn-lint')} provider schemas, {REGION}",
        "types": types,
    }
    CREATE_ONLY_FILE.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {CREATE_ONLY_FILE} with {len(types)} types")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
