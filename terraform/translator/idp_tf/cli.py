"""Command line: translate a packaged CloudFormation template to a module tree."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from . import completeness, moved, safety
from .assets import deterministic_zip
from .awscc_schema import AwsccSchema
from .expand import expand, load_template, offline_placeholders
from .static_files import VERSIONS_TF, bridge_tf, label_tf
from .translate import (
    LIFECYCLE_OVERRIDES,
    STABLE_CUSTOM_OUTPUTS,
    TranslationError,
    Translator,
    report_json,
)

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
    import yaml

    from idp_sdk import IDPClient  # noqa: PLC0415 - only the variants need it

    # The transformers read packaged templates, which use long-form intrinsics;
    # a source template uses short tags (!Ref, !Equals), so write it long-form.
    long_form = workdir / "template-long-form.yaml"
    long_form.write_text(yaml.safe_dump(load_template(template), sort_keys=False))
    out = workdir / f"template-{variant}.yaml"
    publish = IDPClient().publish
    if variant == "headless":
        result = publish.transform_template_headless(str(long_form), str(out))
    elif variant == "govcloud":
        # The gate validates the Terraform; CloudFormation's own lint of the
        # transformed template is the CloudFormation gate's job.
        result = publish.transform_template_govcloud(
            str(long_form), str(out), lint_region=None
        )
    else:
        raise TranslationError(f"unknown variant {variant!r}")
    ok = result.success
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


#: A published nested-template URL, path style. The region is optional: in
#: us-east-1 the publisher writes the global endpoint, ``https://s3.amazonaws.com``,
#: which names none.
S3_URL = re.compile(
    r"https://s3(?:[.-]([a-z0-9-]+))?\.amazonaws\.com[^/]*/([^/]+)/(.+)$"
)
#: The region the global endpoint serves.
S3_GLOBAL_ENDPOINT_REGION = "us-east-1"


def s3_loader(cache: Path) -> Callable[[str], dict[str, Any]]:
    """Fetch a nested template by its published S3 URL, then expand it."""

    def load(url: str) -> dict[str, Any]:
        import boto3

        m = S3_URL.match(url)
        if not m:
            raise TranslationError(f"cannot read nested TemplateURL {url}")
        region, bucket, key = m.groups()
        region = region or S3_GLOBAL_ENDPOINT_REGION
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


def write_module(
    tr: Translator, out: Path, files: dict[str, str], rendered: bool = False
) -> None:
    files["versions.tf"] = VERSIONS_TF.replace(
        "AWSCC_VERSION", tr.schema.version or "1.0.0"
    )
    if tr.custom_tokens:
        files["bridge.tf"] = bridge_tf(
            tr.permissions_boundary_variable(),
            kms_key=tr.bridge_kms_key(),
            access_log_bucket=tr.bridge_access_log_bucket(),
        )
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
        child_files = child.last_render if rendered else child.render()
        write_module(child, out / "modules" / name, child_files, rendered)


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


HEADER = re.compile(
    r'^(?:resource "([a-z0-9_]+)" "([a-z0-9_]+)"|module "([a-z0-9_]+)") \{', re.M
)
#: Version 2 adds statefulAddresses, resources, moves and customOutputEdges;
#: every version-1 key is unchanged.
MANIFEST_FORMAT_VERSION = 2


def tree_resources(tr: Translator, stack: str = "", prefix: str = "") -> list[dict]:
    """One manifest entry per CloudFormation resource in the tree: its identity
    (nested-stack path, logical id, type) and every address it became."""
    out = []
    for lid, plan in tr.plans.items():
        text = tr.emitted.get(lid, "")
        addresses = []
        for rtype, rname, mname in HEADER.findall(text):
            addresses.append(
                prefix + (f"module.{mname}" if mname else f"{rtype}.{rname}")
            )
        if not addresses and not plan.virtual:
            continue
        out.append(
            {
                "stack": stack,
                "logicalId": lid,
                "cfnType": plan.cfn_type,
                "address": prefix + plan.address,
                "addresses": sorted(set(addresses)),
            }
        )
        if plan.kind == "module":
            child_stack = f"{stack}/{lid}" if stack else lid
            out.extend(
                tree_resources(
                    tr.children[plan.name], child_stack, f"{prefix}module.{plan.name}."
                )
            )
    return out


def render_tree(tr: Translator) -> None:
    """Render every nested module (the root is rendered by the caller)."""
    for child in tr.children.values():
        child.render()
        render_tree(child)


def tree_checks(tr: Translator) -> tuple[list[str], list[str], list[safety.Edge]]:
    """The whole-tree checks: failures, declarations that matched nothing, and
    the custom-output edges to report.

    A declaration matching nothing is not a failure here, because a variant
    (headless, GovCloud) removes resources the standard template declares
    things about; ``tests/test_offline_templates.py`` fails on any for the
    standard template, which has every resource."""
    errors: list[str] = []
    stale_declarations: list[str] = []
    keys = resource_keys(tr)
    stale = set(LIFECYCLE_OVERRIDES) - keys
    if stale:
        stale_declarations.append(
            "LIFECYCLE_OVERRIDES names resources the template no longer has: "
            f"{sorted(stale)}"
        )
    missing_types = safety.unknown_create_only_types(tr)
    if missing_types:
        errors.append(
            f"{missing_types} not in schemas/create_only.json; run "
            "terraform/translator/scripts/refresh_create_only.py"
        )
    edges = safety.custom_output_edges(tr)
    for e in edges:
        if e.stateful and not e.handled:
            errors.append(
                "a stateful resource takes a custom resource's output as a "
                f"create-only property, so Terraform would replace it whenever "
                f"that custom resource is updated: {e.describe()}. Declare why the "
                "output is stable in STABLE_CUSTOM_OUTPUTS (translate.py), or "
                "change the template"
            )
    used = {(e.cfn_type, e.logical_id, e.prop.split("/", 1)[0]) for e in edges}
    stale_stable = sorted(set(STABLE_CUSTOM_OUTPUTS) - used)
    if stale_stable:
        stale_declarations.append(
            "STABLE_CUSTOM_OUTPUTS names properties no custom resource output "
            f"reaches: {stale_stable}"
        )
    missing, matched = completeness.check(tr)
    errors.extend(
        f"{m.describe()}. Map it, or declare it in completeness.NOT_APPLICABLE "
        "with a reason"
        for m in missing
    )
    unused = completeness.stale_declarations(matched)
    if unused:
        stale_declarations.append(
            f"completeness.NOT_APPLICABLE matches nothing: {unused}"
        )
    return errors, stale_declarations, edges


def load_manifest(source: str) -> dict:
    """A previous release's idp-terraform.json, from a path or an HTTPS URL."""
    if source.startswith("https://"):
        import urllib.request

        with urllib.request.urlopen(source, timeout=60) as resp:  # nosec B310 - https only
            return json.loads(resp.read())
    return json.loads(Path(source).read_text())


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
        "--previous-manifest",
        help="the previous release's idp-terraform.json (a path or an HTTPS URL); "
        "moved blocks are generated for every resource whose address changed",
    )
    ap.add_argument(
        "--renames",
        type=Path,
        default=moved.RENAMES_FILE,
        help="rename table for identities that cannot be inferred "
        "(default: terraform/translator/renames.yaml)",
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
        render_tree(tr)
        errors, stale_declarations, edges = tree_checks(tr)
        resources = tree_resources(tr)
        live = {a for e in resources for a in e["addresses"]}
        try:
            renames = moved.load_renames(args.renames)
        except ValueError as exc:
            raise TranslationError(str(exc)) from exc
        previous = (
            load_manifest(args.previous_manifest) if args.previous_manifest else {}
        )
        move = moved.plan_moves(previous, resources, renames)
        errors.extend(move.errors)
        moves, dropped = moved.carry_history(previous, move.moves, live)
        warnings = move.warnings + dropped
        if args.survey:
            errors = collect_errors(tr) + errors
            for e in errors:
                print(e)
            for d in stale_declarations:
                print(f"stale declaration: {d}")
            print(f"{len(errors)} failure(s); {tr.report.resource_kinds}")
            return 1 if errors else 0
        if errors:
            raise TranslationError("\n".join(errors))
        if moves:
            files["moved.tf"] = moved.moved_tf(moves)
        if args.label_template:
            if not args.version:
                ap.error("--label-template needs --version")
            desc = load_template(args.label_template)["Description"]
            files["label.tf"] = label_tf(desc, args.version, tr.label_parameters())
        write_module(tr, args.out, files, rendered=True)
        # Read by idp-cli: how CloudFormation parameter names map to variables,
        # which resources a delete keeps (CloudFormation DeletionPolicy), which a
        # plan may not delete or replace, and, for the next release's
        # translation, every resource's identity and the moves made so far.
        (args.out / "idp-terraform.json").write_text(
            json.dumps(
                {
                    "formatVersion": MANIFEST_FORMAT_VERSION,
                    "version": args.version,
                    "parameters": tr.parameter_map(),
                    "retainedAddresses": tr.retained_addresses(),
                    "statefulAddresses": safety.stateful_addresses(tr),
                    "customOutputEdges": [
                        {
                            "address": e.address,
                            "cfnType": e.cfn_type,
                            "property": e.prop,
                            "sources": list(e.sources),
                            "stateful": e.stateful,
                            "handled": e.handled,
                        }
                        for e in edges
                    ],
                    "moves": [{"from": a, "to": b} for a, b in moves],
                    "resources": resources,
                },
                indent=2,
            )
            + "\n"
        )
    except TranslationError as exc:
        print(f"translation failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.out} ({len(tr.children)} nested modules)")
    for w in tr.report.warnings + warnings + stale_declarations:
        print(f"warning: {w}")
    if moves:
        print(f"{len(moves)} moved block(s) in moved.tf")
    for e in edges:
        print(f"custom-output edge: {e.describe()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
