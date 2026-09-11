#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Migrate a stack's evaluation baselines to the multi-instance shape (#715).

Turning on ``x-aws-idp-multi-instance: true`` for a class changes the shape of
that class's ``inference_result`` from a flat record to
``{"instances": [ … ]}``. Evaluation compares a prediction against a stored
baseline **of the same shape**, so a wrapped prediction against a flat baseline
scores every field as missing-on-one-side: the class's accuracy collapses to ~0
with no error anywhere. That is the one way this feature can break a working
deployment, so migrate the baselines in the same change as the flag.

    # See what would change (default — nothing is written)
    python3 scripts/migrate_multi_instance_baselines.py --stack-name MyStack

    # Apply
    python3 scripts/migrate_multi_instance_baselines.py --stack-name MyStack --apply

    # Roll the flag back off again
    python3 scripts/migrate_multi_instance_baselines.py --stack-name MyStack \
        --direction unwrap --apply

**It migrates SHAPE, not CONTENT.** Wrapping a one-record baseline gives
``instances`` of length 1, which is correct ground truth only if the document
really contains one document of that class. The reason to turn the flag on is that
some documents contain several — and those extra records were never in the
baseline, because the old pipeline could not extract them. Adding them is
authoring work no tool can do. Every migrated document is listed so that work is
visible now rather than discovered later as a mysterious recall drop.

Safe to re-run: the transform is idempotent, so an interrupted run can simply be
repeated. Set ``--backup-suffix`` to keep a copy of each original object.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError as _exc:  # pragma: no cover - operational script
    raise SystemExit(
        f"boto3 is required to run this script (pip install boto3): {_exc}"
    ) from _exc

sys.path.insert(
    0,
    str(
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "lib"
        / "idp_common_pkg"
    ),
)

from idp_common.evaluation.baseline_migration import (  # noqa: E402
    baseline_instance_count,
    multi_instance_class_labels,
    section_class_label,
    unwrap_baseline_result,
    wrap_baseline_result,
)


def stack_outputs(stack_name: str, region: str | None) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=region)
    stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


def _configuration_table(stack_name: str, region: str | None) -> str:
    """Physical name of the stack's ``ConfigurationTable``.

    ``describe_stack_resource`` rather than ``list_stack_resources``: the latter
    returns at most 100 resources per page and the main template has close to 300,
    so an unpaginated scan looked fine on a toy stack and then raised a bare
    ``StopIteration`` on a real one — on the DEFAULT invocation of the very script
    that mitigates this feature's one deployment-breaking risk.
    """
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        resource = cfn.describe_stack_resource(
            StackName=stack_name, LogicalResourceId="ConfigurationTable"
        )
    except ClientError as exc:
        raise SystemExit(
            f"Could not find the ConfigurationTable resource in stack "
            f"{stack_name!r}: {exc}. Pass --class-label to skip the config lookup "
            f"and name the classes to migrate explicitly."
        ) from exc
    return resource["StackResourceDetail"]["PhysicalResourceId"]


def load_classes(
    stack_name: str, region: str | None, config_profile: str | None
) -> list[dict[str, Any]]:
    """Read the stack's document classes from its Configuration table."""
    from idp_common.config.configuration_manager import ConfigurationManager

    manager = ConfigurationManager(table_name=_configuration_table(stack_name, region))
    config = (
        manager.get_merged_config(config_profile)
        if config_profile
        else manager.get_merged_config()
    )
    return config.get("classes") or []


def iter_section_results(s3, bucket: str, prefix: str):
    """Yield every ``.../sections/<n>/result.json`` key under ``prefix``."""
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/result.json") and "/sections/" in key:
                yield key


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate evaluation baselines to/from the multi-instance shape."
    )
    parser.add_argument("--stack-name", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--config-profile",
        default=None,
        help="Configuration profile whose classes decide what is migrated "
        "(default: the active one).",
    )
    parser.add_argument(
        "--baseline-bucket",
        default=None,
        help="Override the stack's EvaluationBaseline bucket.",
    )
    parser.add_argument(
        "--prefix", default="", help="Only migrate baselines under this key prefix."
    )
    parser.add_argument(
        "--class-label",
        action="append",
        default=[],
        help="Migrate ONLY this class (repeatable). Default: every class flagged "
        "x-aws-idp-multi-instance in the config.",
    )
    parser.add_argument(
        "--direction",
        choices=("wrap", "unwrap"),
        default="wrap",
        help="wrap = flat -> instances[] (turning the flag ON). "
        "unwrap = instances[] -> flat (rolling it back).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without it this is a dry run and nothing changes.",
    )
    parser.add_argument(
        "--backup-suffix",
        default=None,
        help="Copy each original object to <key><suffix> before overwriting "
        '(e.g. ".pre-multi-instance").',
    )
    args = parser.parse_args()

    s3 = boto3.client("s3", region_name=args.region)

    bucket = args.baseline_bucket
    if not bucket:
        outputs = stack_outputs(args.stack_name, args.region)
        bucket = outputs.get("S3EvaluationBaselineBucketName") or outputs.get(
            "EvaluationBaselineBucketName"
        )
    if not bucket:
        print(
            "Could not resolve the evaluation baseline bucket; pass "
            "--baseline-bucket explicitly.",
            file=sys.stderr,
        )
        return 2

    if args.class_label:
        labels = {label.lower() for label in args.class_label}
    else:
        labels = multi_instance_class_labels(
            load_classes(args.stack_name, args.region, args.config_profile)
        )
    if not labels:
        print(
            "No class in this configuration sets x-aws-idp-multi-instance, so "
            "there is nothing to migrate. Set the flag first, or name a class "
            "with --class-label."
        )
        return 0

    print(f"baseline bucket : s3://{bucket}/{args.prefix}")
    print(f"direction       : {args.direction}")
    print(f"classes         : {', '.join(sorted(labels))}")
    print(f"mode            : {'APPLY' if args.apply else 'DRY RUN (nothing written)'}")
    print()

    transform = (
        wrap_baseline_result if args.direction == "wrap" else unwrap_baseline_result
    )

    scanned = skipped_class = changed = unchanged = refused = 0
    single_record: list[str] = []

    failed = 0
    for key in iter_section_results(s3, bucket, args.prefix):
        scanned += 1
        # One AccessDenied or throttle must not abort the run with a traceback:
        # the transform is idempotent, so the right behaviour is to report the
        # object, keep going, and exit non-zero at the end.
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except ClientError as exc:
            failed += 1
            print(f"  ERROR reading         {key}: {exc}")
            continue
        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            print(f"  SKIP (not JSON)      {key}")
            continue

        label = section_class_label(result)
        if not label or label.lower() not in labels:
            skipped_class += 1
            continue

        migrated, did_change = transform(result)
        if not did_change:
            if (
                args.direction == "unwrap"
                and (baseline_instance_count(result) or 0) > 1
            ):
                refused += 1
                print(
                    f"  REFUSED ({baseline_instance_count(result)} records — "
                    f"flattening would discard ground truth) {key}"
                )
            else:
                unchanged += 1
            continue

        print(f"  {'MIGRATE' if args.apply else 'WOULD MIGRATE'} {key}")

        if not args.apply:
            changed += 1
            if args.direction == "wrap":
                single_record.append(key)
            continue

        try:
            if args.backup_suffix:
                s3.copy_object(
                    Bucket=bucket,
                    Key=f"{key}{args.backup_suffix}",
                    CopySource={"Bucket": bucket, "Key": key},
                )
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=json.dumps(migrated, indent=2).encode("utf-8"),
                ContentType="application/json",
            )
        except ClientError as exc:
            failed += 1
            print(f"  ERROR writing         {key}: {exc}")
            continue

        # Counted only AFTER the write actually landed. The counters used to be
        # incremented up-front and rolled back in the error handler, and the
        # rollback popped `single_record` unconditionally — which raises
        # IndexError on `--direction unwrap`, because nothing is ever appended
        # there. So a single AccessDenied during a rollback crashed with a
        # traceback: exactly the failure this error handling was added to remove,
        # on the path where an operator is already having a bad day.
        #
        # Counting after the fact also makes the printed list mean "written",
        # which is what a reader needs it to mean.
        changed += 1
        if args.direction == "wrap":
            single_record.append(key)

    print()
    print(f"scanned                 : {scanned}")
    print(f"skipped (other class)   : {skipped_class}")
    print(f"migrated                : {changed}")
    print(f"already correct         : {unchanged}")
    if refused:
        print(f"refused (multi-record)  : {refused}")
    if failed:
        print(f"FAILED (S3 errors)      : {failed}")

    if args.direction == "wrap" and single_record:
        print()
        print(
            "IMPORTANT — shape only. Each of these baselines now asserts exactly "
            "ONE record. Any document that actually contains several needs the "
            "extra records ADDED to its `instances` list, or evaluation will "
            "score the newly-extracted records as false positives:"
        )
        for key in single_record[:50]:
            print(f"  {key}")
        if len(single_record) > 50:
            print(f"  … and {len(single_record) - 50} more")

    if not args.apply and changed:
        print()
        print("Dry run — nothing was written. Re-run with --apply.")
    if failed:
        print()
        print(
            f"{failed} object(s) could not be processed. The transform is "
            f"idempotent, so re-running after fixing permissions is safe."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
