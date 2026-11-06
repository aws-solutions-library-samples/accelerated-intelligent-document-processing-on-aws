"""Command line: translate a packaged CloudFormation template to a module tree."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from .assets import deterministic_zip
from .awscc_schema import AwsccSchema
from .expand import expand, load_template, offline_placeholders
from .static_files import VERSIONS_TF, bridge_tf, label_tf
from .translate import LIFECYCLE_OVERRIDES, TranslationError, Translator, report_json

BRIDGE_SRC = Path(__file__).resolve().parents[2] / "bridge" / "index.py"
VENDORED_SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "awscc.json"
# (main template relative to the repository root, variant) pairs the translator
# must handle. A variant is produced by the SDK's own template transformer, the
# same one `idp-cli publish --headless/--govcloud` uses.
TEMPLATE_VARIANTS = (
    ("template.yaml", None),
    ("template.yaml", "headless"),
    ("template.yaml", "govcloud"),
)


def apply_variant(template: Path, variant: str | None, workdir: Path) -> Path:
    """Write the variant of a main template into workdir and return its path."""
    if variant is None:
        return template
    from idp_sdk._core.template_transform import (  # noqa: PLC0415 - optional
        GovCloudTemplateTransformer,
        HeadlessTemplateTransformer,
    )

    import yaml

    # The transformers read packaged templates, which use long-form intrinsics;
    # a source template uses short tags (!Ref, !Equals), so write it long-form.
    long_form = workdir / "template-long-form.yaml"
    long_form.write_text(yaml.safe_dump(load_template(template), sort_keys=False))
    out = workdir / f"template-{variant}.yaml"
    if variant == "headless":
        ok = HeadlessTemplateTransformer().transform(str(long_form), str(out))
    elif variant == "govcloud":
        ok = GovCloudTemplateTransformer().transform(str(long_form), str(out))
    else:
        raise TranslationError(f"unknown variant {variant!r}")
    if not ok:
        raise TranslationError(f"the {variant} transform failed for {template}")
    return out


def cfn_types_in_tree(template: Path, variant: str | None = None) -> set[str]:
    """Every resource type in a template and its nested templates (offline)."""
    import tempfile

    root = template.resolve().parent
    loader = local_loader(root)
    types: set[str] = set()
    with tempfile.TemporaryDirectory() as tmp:
        source = apply_variant(template, variant, Path(tmp))
        pending = [expand(offline_placeholders(load_template(source)))]
    while pending:
        t = pending.pop()
        for res in t.get("Resources", {}).values():
            types.add(res["Type"])
            if res["Type"] == "AWS::CloudFormation::Stack":
                url = (res.get("Properties") or {}).get("TemplateURL")
                if isinstance(url, str):
                    pending.append(loader(url))
    return types


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


def local_loader(root: Path) -> Callable[[str], dict[str, Any]]:
    """Resolve a nested stack's local TemplateURL to its source template.

    In the source templates a nested stack is
    ``./<dir>/.aws-sam/packaged.yaml``; its source is ``<dir>/template.yaml``.
    """

    def load(url: str) -> dict[str, Any]:
        rel = url.removeprefix("./")
        if not rel.endswith("/.aws-sam/packaged.yaml"):
            raise TranslationError(f"cannot resolve nested TemplateURL {url} offline")
        source = root / rel.removesuffix("/.aws-sam/packaged.yaml") / "template.yaml"
        if not source.exists():
            raise TranslationError(f"nested template {source} not found")
        return expand(offline_placeholders(load_template(source)))

    return load


def write_module(tr: Translator, out: Path, files: dict[str, str]) -> None:
    files["versions.tf"] = VERSIONS_TF.replace(
        "AWSCC_VERSION", tr.schema.version or "1.0.0"
    )
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
    ap.add_argument(
        "--awscc-schema",
        type=Path,
        default=VENDORED_SCHEMA,
        help="awscc provider schema (default: the vendored subset)",
    )
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
        "--offline",
        action="store_true",
        help="translate source templates from the checkout (the per-PR gate): "
        "local code paths become placeholders and nested stacks resolve locally",
    )
    ap.add_argument(
        "--variant",
        choices=["headless", "govcloud"],
        help="translate this variant of the main template (offline mode only); "
        "it is produced by the SDK's template transformer",
    )
    ap.add_argument(
        "--survey",
        action="store_true",
        help="collect every failure and print them instead of stopping",
    )
    args = ap.parse_args(argv)

    import tempfile

    variant_dir = tempfile.TemporaryDirectory()
    template_path = args.template
    if args.variant:
        if not args.offline:
            ap.error("--variant needs --offline; publish produces the variants itself")
        template_path = apply_variant(
            args.template, args.variant, Path(variant_dir.name)
        )
    source = load_template(template_path)
    if args.offline:
        source = offline_placeholders(source)
        loader = local_loader(args.template.resolve().parent)
    elif args.nested_cache:
        loader = s3_loader(args.nested_cache)
    else:
        loader = None
    expanded = expand(source)
    try:
        tr = Translator(
            expanded,
            AwsccSchema(args.awscc_schema),
            mode=args.mode,
            survey=args.survey,
            nested_loader=loader,
        )
        tr.directory_readers = args.label_template is not None
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
            files["label.tf"] = label_tf(desc, args.version, tr.label_parameters())
        write_module(tr, args.out, files)
        # Read by idp-cli: how CloudFormation parameter names map to variables,
        # and which resources a delete keeps (CloudFormation DeletionPolicy).
        (args.out / "idp-terraform.json").write_text(
            json.dumps(
                {
                    "formatVersion": 1,
                    "version": args.version,
                    "parameters": tr.parameter_map(),
                    "retainedAddresses": tr.retained_addresses(),
                },
                indent=2,
            )
            + "\n"
        )
    except TranslationError as exc:
        print(f"translation failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.out} ({len(tr.children)} nested modules)")
    for w in tr.report.warnings:
        print(f"warning: {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
