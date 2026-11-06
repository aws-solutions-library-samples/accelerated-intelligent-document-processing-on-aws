"""Command line: translate a packaged CloudFormation template to a module tree."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from .assets import deterministic_zip
from .awscc_schema import AwsccSchema
from .expand import expand, load_template
from .static_files import VERSIONS_TF, bridge_tf, label_tf
from .translate import LIFECYCLE_OVERRIDES, TranslationError, Translator, report_json

BRIDGE_SRC = Path(__file__).resolve().parents[2] / "bridge" / "index.py"
S3_URL = re.compile(r"https://s3[.-]([a-z0-9-]+)\.amazonaws\.com[^/]*/([^/]+)/(.+)$")


def s3_loader(cache: Path) -> Callable[[str], dict[str, Any]]:
    """Fetch a nested template by its published S3 URL, then expand it."""

    def load(url: str) -> dict[str, Any]:
        import boto3

        m = S3_URL.match(url)
        if not m:
            raise TranslationError(f"cannot read nested TemplateURL {url}")
        region, bucket, key = m.groups()
        target = cache / key.replace("/", "_")
        if not target.exists():
            cache.mkdir(parents=True, exist_ok=True)
            s3 = boto3.client("s3", region_name=region)
            s3.download_file(bucket, key, str(target))
        return expand(load_template(target))

    return load


def write_module(tr: Translator, out: Path, files: dict[str, str]) -> None:
    files["versions.tf"] = VERSIONS_TF
    if tr.custom_tokens:
        files["bridge.tf"] = bridge_tf()
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text)
    if tr.custom_tokens:
        # Source alongside for reading; the function deploys the zip.
        (out / "bridge").mkdir(exist_ok=True)
        shutil.copy(BRIDGE_SRC, out / "bridge" / "index.py")
        tr.assets["zips/cfn_bridge.zip"] = deterministic_zip(
            {"index.py": BRIDGE_SRC.read_bytes()}
        )
    for rel, data in tr.assets.items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(data)
    (out / "translation-report.json").write_text(report_json(tr))
    for name, child in tr.children.items():
        write_module(child, out / "modules" / name, child.render())


def resource_keys(tr: Translator) -> set[tuple[str, str]]:
    keys = {(p.cfn_type, p.logical_id) for p in tr.plans.values()}
    for child in tr.children.values():
        keys |= resource_keys(child)
    return keys


def collect_errors(tr: Translator) -> list[str]:
    errors = list(tr.errors)
    for name, child in tr.children.items():
        errors.extend(f"{name}/{e}" for e in collect_errors(child))
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="idp-tf")
    ap.add_argument("template", type=Path, help="packaged CloudFormation template")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--awscc-schema", type=Path, required=True)
    ap.add_argument("--mode", choices=["hybrid", "awscc"], default="hybrid")
    ap.add_argument(
        "--label-template",
        type=Path,
        help="published main template whose Description labels the deployment",
    )
    ap.add_argument("--version", help="published version for the label stack")
    ap.add_argument(
        "--nested-cache",
        type=Path,
        help="fetch nested templates by TemplateURL from S3 into this directory",
    )
    ap.add_argument(
        "--survey",
        action="store_true",
        help="collect every failure and print them instead of stopping",
    )
    args = ap.parse_args(argv)

    expanded = expand(load_template(args.template))
    loader = s3_loader(args.nested_cache) if args.nested_cache else None
    try:
        tr = Translator(
            expanded,
            AwsccSchema(args.awscc_schema),
            mode=args.mode,
            survey=args.survey,
            nested_loader=loader,
        )
        files = tr.render()
        if args.survey:
            # Children are rendered here so their emit-time failures count too.
            for child in tr.children.values():
                child.render()
            errors = collect_errors(tr)
            for e in errors:
                print(e)
            print(f"{len(errors)} failure(s); {tr.report.resource_kinds}")
            return 1 if errors else 0
        stale = set(LIFECYCLE_OVERRIDES) - resource_keys(tr)
        if stale:
            raise TranslationError(
                f"LIFECYCLE_OVERRIDES names resources the template no longer has: "
                f"{sorted(stale)}"
            )
        if args.label_template:
            if not args.version:
                ap.error("--label-template needs --version")
            desc = load_template(args.label_template)["Description"]
            files["label.tf"] = label_tf(desc, args.version)
        write_module(tr, args.out, files)
    except TranslationError as exc:
        print(f"translation failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.out} ({len(tr.children)} nested modules)")
    for w in tr.report.warnings:
        print(f"warning: {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
