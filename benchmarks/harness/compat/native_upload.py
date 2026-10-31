#!/usr/bin/env python3
"""Upload a benchmark config to a stack's ConfigurationTable in v0.5.16-NATIVE
format, bypassing idp-cli's forced v0.5->v0.6 migration.

Why this exists: `idp-cli config-upload` always runs migrate_v05_to_v06 on the
uploaded config, which converts the top-level `assessment` block into
`extraction.confidence` and DROPS `assessment`. A v0.5.16 stack's assessment
lambda reads `config.assessment.task_prompt` from the stored dict, so a migrated
config makes every doc FAIL with "Assessment task_prompt is required". This
writer stores the config verbatim (gzip Binary, _config_format=full) so the
top-level `assessment` block survives, matching how v0.5.16's own deploy seeds
managed configs.

The command-line entry point adds the pre-0.9 flat classification keys
(classification_flat.py) and refuses to write a config whose classification
settings the target release would drop (release_model_check.py), exactly as
``run_matrix.py --native-upload`` does. ``upload()`` itself writes what it is given.

Usage:
  AWS_PROFILE=default python3 native_upload.py --table <ConfigurationTable> \
      --version <name> --config-file <path.yaml> --release <tag, e.g. v0.6.12>
"""

import argparse
import datetime
import gzip
import json
import os
import sys

import boto3
import yaml

# Metadata fields kept as top-level DynamoDB attributes (everything else is
# compressed into _compressed_config), matching ConfigurationManager._compress_item.
META = {"Configuration", "IsActive", "Description", "Managed", "CreatedAt", "UpdatedAt"}


def upload(table, version, cfg, region="us-west-2", profile="default"):
    ddb = boto3.Session(profile_name=profile).client("dynamodb", region_name=region)
    # ensure v0.5.16 sees a "full" config (no lazy migration path)
    cfg = dict(cfg)
    cfg["_config_format"] = "full"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    config_json = json.dumps(cfg, default=str, separators=(",", ":"))
    compressed = gzip.compress(config_json.encode("utf-8"))
    item = {
        "Configuration": {"S": f"Config#{version}"},
        "_config_storage": {"S": "compressed"},
        "_compressed_config": {"B": compressed},
        "IsActive": {"BOOL": False},
        "Managed": {"BOOL": False},
        "Description": {"S": f"benchmark {version} (native v0.5.16 format)"},
        "CreatedAt": {"S": now},
        "UpdatedAt": {"S": now},
    }
    ddb.put_item(TableName=table, Item=item)
    return len(compressed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--config-file", required=True)
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--profile", default="default")
    ap.add_argument(
        "--release",
        required=True,
        help="git tag of the release the stack runs, e.g. v0.6.12",
    )
    a = ap.parse_args()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from classification_flat import CompatError, with_flat_classification
    from release_model_check import check_config, extract_release_lib

    with open(a.config_file) as fh:
        cfg = yaml.safe_load(fh)
    try:
        cfg, notes = with_flat_classification(cfg)
    except CompatError as exc:
        sys.exit(f"refusing to upload: {exc}")
    for note in notes:
        print(f"!! {note}")
    errors = check_config(cfg, extract_release_lib(a.release))
    if errors:
        sys.exit("refusing to upload:\n  " + "\n  ".join(errors))
    n = upload(a.table, a.version, cfg, a.region, a.profile)
    print(f"uploaded Config#{a.version} ({n} bytes compressed) -> {a.table}")


if __name__ == "__main__":
    main()
