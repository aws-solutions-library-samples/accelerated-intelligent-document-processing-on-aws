# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`idp-feature-cli` — command-line entry point for the feature publisher."""

from __future__ import annotations

import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

from .manifest import ManifestError, load_manifest
from .publisher import FeaturePublisher
from .scaffold import ScaffoldError, ScaffoldOptions, scaffold_feature
from .seller_service import (
    DEFAULT_MARKETPLACE_REGION,
    SellerServiceError,
    build_activation_pointer,
    build_sam_deploy_command,
    build_trust_bundle,
    fetch_activations,
    fetch_signing_public_key,
    find_seller_service_dir,
    parse_product_registry,
    preflight,
    publish_activation_pointer,
    read_service_version,
    resolve_stack_output,
    run_command,
    utc_now_iso,
    verify_deployed_registry,
)

console = Console()


def _resolve_bucket(
    bucket_basename: Optional[str],
    region: str,
    *,
    make_public: bool = False,
) -> str:
    """Resolve a `--bucket-basename` to a full S3 bucket name.

    Mirrors `idp-cli`'s bucket semantics so the two CLIs behave identically:

      * An explicit basename has the region appended — ``my-artifacts`` in
        ``us-east-1`` becomes ``my-artifacts-us-east-1`` — and is used as-is.
      * When omitted, the per-account artifacts bucket
        ``idp-accelerator-artifacts-<account>-<region>`` is auto-generated and
        created if missing (via :func:`ensure_artifacts_bucket`).

    Exits the process with a helpful message if bucket creation fails.
    """
    if bucket_basename:
        return f"{bucket_basename}-{region}"
    from .pack import ensure_artifacts_bucket

    try:
        return ensure_artifacts_bucket(
            region=region, console=console, make_public=make_public
        )
    except RuntimeError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)


def _parse_parameters(parameters: Optional[str]) -> dict[str, str]:
    """Parse a `--parameters key=value,key2=value2` string into a dict.

    Mirrors `idp-cli deploy`'s parser: splits on commas that precede a
    ``key=`` token, so values may themselves contain commas (e.g. subnet
    lists). Returns an empty dict for ``None``/empty input.
    """
    if not parameters:
        return {}
    import re

    parsed: dict[str, str] = {}
    for match in re.finditer(
        r"([A-Za-z][A-Za-z0-9]*)=((?:(?![A-Za-z][A-Za-z0-9]*=).)*)",
        parameters,
    ):
        key = match.group(1).strip()
        value = match.group(2).strip().rstrip(",")
        parsed[key] = value
    return parsed


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="idp-feature-sdk")
def main() -> None:
    """Publish IDP Accelerator feature packages to a Marketplace-style S3 bucket."""


@main.command()
@click.argument(
    "project_dir", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
def validate(project_dir: Path) -> None:
    """Validate <PROJECT_DIR>/feature.yaml against the schema and linked files."""
    try:
        manifest = load_manifest(project_dir)
    except ManifestError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)
    console.print(
        f"[green]✓[/green] [bold]{manifest.featureId}[/bold] v{manifest.version} — "
        f"{manifest.displayName}"
    )
    if manifest.marketplace.productCode:
        console.print(f"  Marketplace productCode: {manifest.marketplace.productCode}")
    if manifest.capabilities:
        console.print(f"  capabilities: {', '.join(manifest.capabilities)}")


@main.command()
@click.argument(
    "project_dir", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
def build(project_dir: Path) -> None:
    """Build the UI bundle and run static validation. No uploads."""
    try:
        publisher = FeaturePublisher(project_dir, console=console)
        publisher.build()
    except (ManifestError, RuntimeError, ValueError) as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)


@main.command()
@click.argument(
    "project_dir", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "--bucket-basename",
    default=None,
    help="S3 bucket basename for artifacts — region is appended automatically "
    "(auto-generated as idp-accelerator-artifacts-<account>-<region> if not "
    "provided). Matches `idp-cli publish`.",
)
@click.option("--region", default="us-east-1", show_default=True)
@click.option(
    "--prefix",
    "s3_prefix",
    default="",
    help="Optional S3 key prefix under the bucket. Default (empty) yields the "
    "bare `extensions/<id>/...` layout that the catalog's `templateKey` "
    "expects; a non-empty prefix becomes `<prefix>/extensions/<id>/...`.",
)
@click.option(
    "--public",
    "make_public",
    is_flag=True,
    default=False,
    help="Upload objects with ACL=public-read. Required for Launch Stack URLs to "
    "work without additional bucket policy. Your feature bucket's ACL settings "
    "must permit this.",
)
@click.option(
    "--register-with-simulator",
    default=None,
    help="Also POST a CreateProduct call to the marketplace-simulator at this URL, "
    "so the feature flows through GetEntitlements locally. e.g. http://127.0.0.1:8080",
)
@click.option(
    "--simulator-product-code",
    default=None,
    help="productCode to register with the simulator. Defaults to 'prod-<featureId>'.",
)
def publish(
    project_dir: Path,
    bucket_basename: Optional[str],
    region: str,
    s3_prefix: str,
    make_public: bool,
    register_with_simulator: Optional[str],
    simulator_product_code: Optional[str],
) -> None:
    """Validate → build → upload → update latest.json. Prints a Launch Stack URL on success."""
    feature_bucket = _resolve_bucket(bucket_basename, region, make_public=make_public)
    try:
        publisher = FeaturePublisher(project_dir, console=console)
        result = publisher.publish(
            feature_bucket=feature_bucket,
            region=region,
            s3_prefix=s3_prefix,
            make_public=make_public,
            register_with_simulator=register_with_simulator,
            simulator_product_code=simulator_product_code,
        )
    except (ManifestError, RuntimeError, ValueError) as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    console.print()
    console.rule("[bold]Published[/bold]")
    console.print(f"  featureId:      {result.feature_id}")
    console.print(f"  version:        {result.version}")
    console.print(f"  template:       {result.template_url}")
    console.print(f"  ui bundle:      {result.bundle_url}")
    console.print(f"  manifest:       {result.manifest_url}")
    console.print(f"  latest.json:    {result.latest_json_url}")
    console.print()
    console.print("[bold]🚀 Launch Stack URL (placeholder MAINSTACKNAME):[/bold]")
    console.print(f"  {result.launch_url}")
    console.print()
    console.print(
        "[dim]In production this URL is generated by the main stack's "
        "getFeatureLaunchUrl resolver, which substitutes the real MainStackName and "
        "gates on the caller's Admin role.[/dim]"
    )


@main.command("publish-pack")
@click.argument(
    "project_dir", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "--bucket-basename",
    default=None,
    help="S3 bucket basename for the pack's published artifacts — region is "
    "appended automatically (auto-generated as "
    "idp-accelerator-artifacts-<account>-<region> and created if not "
    "provided). Matches `idp-cli` / `publish`. Private by default; pass "
    "--public to grant public read on the `packs/*`/`host/*` prefixes for "
    "CROSS-ACCOUNT deploys. Without --public the bucket is left untouched.",
)
@click.option(
    "--prefix",
    "artifacts_prefix",
    default="",
    help="Optional S3 key prefix under the artifacts bucket. Default (empty) "
    "yields the bare `extensions/<id>/...` layout — the SAME layout `publish` "
    "produces; a non-empty prefix becomes `<prefix>/extensions/<id>/...`.",
)
@click.option(
    "--host-template-url",
    required=True,
    help="Public HTTPS URL of the IDP accelerator main template (idp-main.yaml). "
    "Produced by `idp-cli publish`. Baked into the wrapper as a parameter "
    "default so deploy-pack doesn't need to specify it.",
)
@click.option("--region", default="us-east-1", show_default=True)
@click.option(
    "--public",
    "make_public",
    is_flag=True,
    default=False,
    help="Make the published artifacts world-readable on the `packs/*` and "
    "`host/*` prefixes (relaxes BlockPublicPolicy/RestrictPublicBuckets and "
    "applies a public-read bucket policy). ONLY needed to share artifacts for "
    "CROSS-ACCOUNT / Quick-Create pack deploys, where the deploying account "
    "fetches the wrapper template via anonymous HTTPS. Default: private "
    "(same-account). A pre-existing bucket's Block Public Access settings are "
    "never weakened unless this flag is set.",
)
def publish_pack_cmd(
    project_dir: Path,
    bucket_basename: Optional[str],
    artifacts_prefix: str,
    host_template_url: str,
    region: str,
    make_public: bool,
) -> None:
    """Publish a vertical-product pack as a single-template wrapper.

    Builds the feature, uploads all artifacts (template, ui-bundle,
    config preset, manifest, SAM Lambda zips), then bakes the artifact
    URLs into the pack's deploy.yaml as parameter defaults and uploads
    the result. Prints a Quick-Create URL and an `idp-feature-cli
    deploy-pack` command for one-click deploy.

    Private by default (secure, same-account). Pass --public to grant
    anonymous read for cross-account / Quick-Create deploys — mirrors the
    `publish` and `deploy-pack` commands.
    """
    from .pack import PackPublisher

    artifacts_bucket = _resolve_bucket(bucket_basename, region, make_public=make_public)
    try:
        publisher = PackPublisher(project_dir, console=console)
        result = publisher.publish(
            artifacts_bucket=artifacts_bucket,
            artifacts_prefix=artifacts_prefix,
            host_template_url=host_template_url,
            region=region,
            make_public=make_public,
        )
    except (ManifestError, RuntimeError, ValueError, FileNotFoundError) as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    console.print()
    console.rule(f"[bold]Published pack:[/bold] {result.feature_id} v{result.version}")
    console.print(
        f"  artifacts:        s3://{result.artifact_bucket}/{result.artifact_prefix}/"
    )
    console.print(f"  feature template: {result.feature_template_url}")
    console.print(f"  host template:    {result.host_template_url}")
    console.print(f"  wrapper template: {result.wrapper_template_url}")
    console.print()
    console.print("[bold]🚀 Quick-Create URL (one-click console deploy):[/bold]")
    console.print(f"  {result.quick_create_url}")
    console.print()
    console.print("[bold]Or deploy via CLI:[/bold]")
    console.print(f"  {result.deploy_command}")


@main.command("deploy-pack")
@click.option(
    "--wrapper-url",
    default=None,
    help="Public HTTPS URL of the published wrapper template "
    "(printed by `idp-feature-cli publish-pack`). Use this to deploy a "
    "pack that has already been published. Mutually exclusive with "
    "--from-code.",
)
@click.option(
    "--from-code",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Path to a local pack project directory (containing feature.yaml). "
    "When set, the CLI publishes from this code first, then deploys the "
    "resulting wrapper. Mirrors `idp-cli deploy --from-code`. Mutually "
    "exclusive with --wrapper-url.",
)
@click.option(
    "--build",
    "build_target",
    type=click.Choice(["feature", "accelerator", "all"]),
    default="feature",
    show_default=True,
    help="What to build before deploy when --from-code is set. "
    "'feature' (default): publish only the pack, assuming the host "
    "accelerator template is already public-readable at "
    "--host-template-url. "
    "'accelerator': publish the IDP host accelerator from the same "
    "source dir; --host-template-url is auto-derived. "
    "'all': publish accelerator AND pack. "
    "Ignored when --wrapper-url is used.",
)
@click.option(
    "--bucket-basename",
    default=None,
    help="(Optional, used with --from-code) S3 bucket basename for published "
    "artifacts — region is appended automatically (matches `idp-cli deploy`). "
    "Auto-generated as `idp-accelerator-artifacts-<account-id>-<region>` and "
    "auto-created if not provided. With --build accelerator|all, host artifacts "
    "go under <bucket>/host/, pack feature artifacts under "
    "<bucket>/[<prefix>/]extensions/<id>/.",
)
@click.option(
    "--prefix",
    "artifacts_prefix",
    default="",
    help="Optional S3 key prefix under the artifacts bucket for the pack's "
    "artifacts. Default (empty) yields the bare `extensions/<id>/...` layout — "
    "the SAME layout `publish`/`deploy` use.",
)
@click.option(
    "--host-artifacts-prefix",
    default="host",
    show_default=True,
    help="Key prefix under the artifacts bucket for the host accelerator. "
    "Only used with --build accelerator|all.",
)
@click.option(
    "--host-template-url",
    default=None,
    help="HTTPS URL of the published host accelerator main template "
    "(idp-main.yaml). Required with --from-code --build feature; ignored "
    "with --build accelerator|all (URL is derived from the publish output).",
)
@click.option(
    "--source-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Source directory for the IDP host accelerator (the repo root "
    "with publish.py + Makefile + template.yaml). Auto-discovered "
    "by walking upward from --from-code; pass explicitly if your "
    "checkout is structured differently.",
)
@click.option(
    "--stack-name",
    required=True,
    help="Name for the wrapper CloudFormation stack.",
)
@click.option(
    "--admin-email",
    required=True,
    help="Email of the initial Admin user for the IDP host stack.",
)
@click.option(
    "--region",
    default="us-east-1",
    show_default=True,
    help="AWS region to deploy into. Must match the wrapper's region.",
)
@click.option(
    "--parameters",
    "extra_params",
    default=None,
    help="Extra wrapper parameters as key=value,key2=value2 (matches "
    "`idp-cli deploy`). Used to override values baked at publish time.",
)
@click.option(
    "--wait",
    is_flag=True,
    default=False,
    help="Wait for the wrapper stack to reach a terminal CloudFormation state "
    "before returning (matches `idp-cli deploy --wait`). Default: return "
    "immediately after submitting the create.",
)
@click.option(
    "--public",
    "make_public",
    is_flag=True,
    default=False,
    help="Make the auto-created artifacts bucket world-readable on the "
    "`packs/*` and `host/*` prefixes (disables Block Public Access for "
    "bucket policies). ONLY needed to share published artifacts for "
    "CROSS-ACCOUNT pack deploys. Default: private (same-account). A "
    "pre-existing bucket's Block Public Access settings are never "
    "weakened unless this flag is set.",
)
def deploy_pack_cmd(
    wrapper_url: Optional[str],
    from_code: Optional[Path],
    build_target: str,
    bucket_basename: Optional[str],
    artifacts_prefix: str,
    host_artifacts_prefix: str,
    host_template_url: Optional[str],
    source_dir: Optional[Path],
    stack_name: str,
    admin_email: str,
    region: str,
    extra_params: Optional[str],
    wait: bool,
    make_public: bool,
) -> None:
    """Deploy a vertical-product pack to a fresh CloudFormation stack.

    Two modes:

    \b
    1. Deploy a previously-published pack (default):

        idp-feature-cli deploy-pack \\
            --wrapper-url <url-from-publish-pack> \\
            --stack-name <name> \\
            --admin-email <email>

    \b
    2. Publish-then-deploy from local source:

        idp-feature-cli deploy-pack \\
            --from-code subscription-features/feature-platform/claims-pack \\
            --bucket-basename <public-bucket> \\
            --host-template-url <existing-host-url> \\
            --stack-name <name> \\
            --admin-email <email>

    Use `--build accelerator` or `--build all` to also republish the IDP
    host accelerator from --source-dir before publishing the pack
    (--host-template-url is then derived from the publish output).
    """
    from .pack import PackPublisher, deploy_pack, publish_host_accelerator

    # Mutex: --wrapper-url and --from-code are exclusive.
    if wrapper_url and from_code:
        console.print(
            "[red]✗ --wrapper-url and --from-code are mutually exclusive. "
            "Use --wrapper-url to deploy a published pack, or --from-code "
            "to publish-then-deploy from local source.[/red]"
        )
        sys.exit(1)
    if not wrapper_url and not from_code:
        console.print(
            "[red]✗ Pass either --wrapper-url (deploy a published pack) "
            "or --from-code (publish-then-deploy from local source).[/red]"
        )
        sys.exit(1)

    extras = _parse_parameters(extra_params)

    # ----- --from-code branch: publish first, derive wrapper_url -----
    if from_code:
        # Resolve the artifacts bucket the same way `idp-cli deploy --from-code`
        # does: an explicit basename gets the region appended; when omitted, the
        # per-account bucket is auto-generated and created if missing. A single
        # bucket can host both host-only deploys and pack deploys.
        artifacts_bucket = _resolve_bucket(
            bucket_basename, region, make_public=make_public
        )

        # Resolve source-dir for the host accelerator publish (when needed).
        if build_target in ("accelerator", "all"):
            if source_dir is None:
                # Walk upward from from_code to find the IDP host repo root.
                # Pack directories often have their own thin `publish.py`
                # wrapper, so a `publish.py` alone isn't enough — look for
                # `Makefile` AND `publish.py` AND `template.yaml` together,
                # the unique signature of the host accelerator repo root.
                p = from_code.resolve()
                while p != p.parent:
                    if (
                        (p / "publish.py").is_file()
                        and (p / "Makefile").is_file()
                        and (p / "template.yaml").is_file()
                    ):
                        source_dir = p
                        break
                    p = p.parent
                if source_dir is None:
                    console.print(
                        "[red]✗ --build accelerator|all needs --source-dir "
                        "(or the IDP repo root reachable upward from "
                        "--from-code; we look for publish.py + Makefile + "
                        "template.yaml together).[/red]"
                    )
                    sys.exit(1)
            console.rule("[bold]Publishing IDP host accelerator…[/bold]")
            try:
                host_template_url = publish_host_accelerator(
                    source_dir=source_dir,
                    artifacts_bucket=artifacts_bucket,
                    artifacts_prefix=host_artifacts_prefix,
                    region=region,
                    console=console,
                )
            except (RuntimeError, ValueError) as exc:
                console.print(f"[red]✗ Host accelerator publish failed: {exc}[/red]")
                sys.exit(1)
        elif not host_template_url:
            console.print(
                "[red]✗ --from-code --build feature requires "
                "--host-template-url (or use --build accelerator|all to "
                "republish the host).[/red]"
            )
            sys.exit(1)

        if build_target in ("feature", "all"):
            console.rule("[bold]Publishing pack…[/bold]")
            try:
                publisher = PackPublisher(from_code, console=console)
                result = publisher.publish(
                    artifacts_bucket=artifacts_bucket,
                    artifacts_prefix=artifacts_prefix,
                    host_template_url=host_template_url,
                    region=region,
                    make_public=make_public,
                )
                wrapper_url = result.wrapper_template_url
            except (ManifestError, RuntimeError, ValueError, FileNotFoundError) as exc:
                console.print(f"[red]✗ Pack publish failed: {exc}[/red]")
                sys.exit(1)
        else:
            # --build accelerator: pack wasn't republished. We still need a
            # wrapper URL — assume the pack is already published at the same
            # bucket/prefix using the *new* host URL. The simplest contract
            # is to refuse: if you're republishing the host, you almost
            # certainly want to also republish the pack against the new
            # host URL.
            console.print(
                "[red]✗ --build accelerator without --build all/feature "
                "leaves the pack pointing at the old host. Use --build all.[/red]"
            )
            sys.exit(1)

    # ----- Deploy -----
    assert wrapper_url is not None  # mutex check guaranteed this above
    console.rule("[bold]Deploying pack…[/bold]")
    try:
        arn = deploy_pack(
            wrapper_url=wrapper_url,
            stack_name=stack_name,
            admin_email=admin_email,
            region=region,
            extra_parameters=extras,
            wait=wait,
            console=console,
        )
    except RuntimeError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    console.print()
    console.print(f"[green]✓[/green] Stack ARN: {arn}")


@main.command("deploy")
@click.option(
    "--from-code",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Path to a local feature project directory (containing feature.yaml). "
    "When set, the CLI publishes from this code first (version-free layout, "
    "tokens baked) then deploys the resulting template. Mirrors "
    "`idp-cli deploy --from-code`. Mutually exclusive with --template-url.",
)
@click.option(
    "--template-url",
    default=None,
    help="HTTPS URL of an ALREADY-published, version-free feature template "
    "(`.../extensions/<id>/template.yaml`, printed by `publish`). Use this to "
    "deploy a feature without rebuilding/republishing it. Mutually exclusive "
    "with --from-code.",
)
@click.option(
    "--host-stack-name",
    required=True,
    help="Name of the running IDP main (host) stack to install this feature "
    "into. The feature template Fn::ImportValue's this stack's exports.",
)
@click.option(
    "--region",
    default=None,
    help="AWS region of the host stack (and where the feature stack is "
    "created). Defaults to the AWS session region "
    "(AWS_REGION / AWS_DEFAULT_REGION / profile), matching `idp-cli deploy`.",
)
@click.option(
    "--bucket-basename",
    default=None,
    help="S3 bucket the feature artifacts live in (and that the feature stack "
    "reads from). With --from-code, this is a basename — region is appended "
    "automatically (matches `idp-cli deploy`) — and defaults to the per-account "
    "artifacts bucket `idp-accelerator-artifacts-<account>-<region>`, auto-created "
    "if not provided. With --template-url, an explicit value is the literal "
    "bucket name (used as-is); otherwise the bucket is parsed from the URL host.",
)
@click.option(
    "--prefix",
    "s3_prefix",
    default="idp-cli",
    show_default=True,
    help="Key prefix under the feature bucket (used with --from-code). Final "
    "layout: <bucket>/<prefix>/extensions/<feature-id>/.",
)
@click.option(
    "--stack-name",
    default=None,
    help="Override the feature stack name. Defaults to "
    "`<host-stack-name>-feature-<feature-id>` — the SAME name a console install "
    "creates, so a CLI deploy UPDATES that stack rather than making a duplicate.",
)
@click.option(
    "--feature-display-name",
    default=None,
    help="Override the FeatureDisplayName parameter (defaults to the template's).",
)
@click.option("--log-level", default=None, help="Override the LogLevel parameter.")
@click.option(
    "--permissions-boundary-arn",
    default=None,
    help="Override the PermissionsBoundaryArn parameter.",
)
@click.option(
    "--public",
    "make_public",
    is_flag=True,
    default=False,
    help="Upload artifacts with ACL=public-read (used with --from-code; see "
    "`publish`).",
)
@click.option(
    "--wait",
    is_flag=True,
    default=False,
    help="Wait for the feature stack to reach a terminal CloudFormation state "
    "before returning (matches `idp-cli deploy --wait`). Default: return "
    "immediately after submitting the create/update.",
)
def deploy_cmd(
    from_code: Optional[Path],
    template_url: Optional[str],
    host_stack_name: str,
    region: Optional[str],
    bucket_basename: Optional[str],
    s3_prefix: str,
    stack_name: Optional[str],
    feature_display_name: Optional[str],
    log_level: Optional[str],
    permissions_boundary_arn: Optional[str],
    make_public: bool,
    wait: bool,
) -> None:
    """Install ONE feature into a running host stack.

    The per-extension analogue of `idp-cli deploy`. Two modes:

    \b
    1. Publish-then-deploy from local source (the fast inner loop):

        idp-feature-cli deploy --from-code ./my-feature \\
            --host-stack-name IDP-FeaturePlatform --region us-west-2

    \b
    2. Deploy an ALREADY-published template (no rebuild):

        idp-feature-cli deploy \\
            --template-url https://<bucket>.s3.<region>.amazonaws.com/extensions/<id>/template.yaml \\
            --host-stack-name IDP-FeaturePlatform

    Either way, the create-or-update targets the same stack a console install
    creates, so re-running it upgrades in place. The RegisterFeature custom
    resource in the template self-registers the feature and copies its UI bundle.
    """
    import boto3

    from .pack import _describe_one, create_or_update_stack

    # Mutex: exactly one source. Mirrors `deploy-pack` (--wrapper-url/--from-code).
    if from_code and template_url:
        console.print(
            "[red]✗ --from-code and --template-url are mutually exclusive. "
            "Use --from-code to publish-then-deploy from local source, or "
            "--template-url to deploy an already-published template.[/red]"
        )
        sys.exit(1)
    if not from_code and not template_url:
        console.print(
            "[red]✗ Pass either --from-code (publish-then-deploy from local "
            "source) or --template-url (deploy an already-published template).[/red]"
        )
        sys.exit(1)

    # Resolve region: explicit flag wins, else the AWS session region
    # (AWS_REGION / AWS_DEFAULT_REGION / profile), matching `idp-cli deploy`.
    if not region:
        region = boto3.session.Session().region_name
        if not region:
            console.print(
                "[red]✗ No region. Pass --region or set AWS_REGION / "
                "AWS_DEFAULT_REGION (or configure a profile region).[/red]"
            )
            sys.exit(1)

    cfn = boto3.client("cloudformation", region_name=region)

    # 1. Validate the host stack exists up front — the feature template
    #    Fn::ImportValue's its exports, so a missing/typo'd host fails opaquely.
    try:
        host = _describe_one(cfn, host_stack_name)
    except Exception as exc:  # botocore ClientError other than not-found
        console.print(f"[red]✗ Could not describe host stack: {exc}[/red]")
        sys.exit(1)
    if host is None:
        console.print(
            f"[red]✗ Host stack {host_stack_name!r} not found in {region}. "
            f"Pass an existing IDP main stack via --host-stack-name.[/red]"
        )
        sys.exit(1)

    # 2. Resolve (template_url, feature_bucket, feature_id, version) by mode.
    if from_code:
        # Resolve the publish target: explicit basename gets the region
        # appended; when omitted, auto-generate + create the per-account
        # bucket (matches `idp-cli deploy --from-code`).
        feature_bucket = _resolve_bucket(
            bucket_basename, region, make_public=make_public
        )

        # Publish the feature (reuse FeaturePublisher — version-free + tokens baked).
        console.rule("[bold]Publishing feature…[/bold]")
        try:
            publisher = FeaturePublisher(from_code, console=console)
            result = publisher.publish(
                feature_bucket=feature_bucket,
                region=region,
                s3_prefix=s3_prefix,
                make_public=make_public,
            )
        except (ManifestError, RuntimeError, ValueError) as exc:
            console.print(f"[red]✗ Publish failed: {exc}[/red]")
            sys.exit(1)
        template_url = result.template_url
        feature_id = result.feature_id
        version: Optional[str] = result.version

        # Zero-touch subscribe: if the host points at a marketplace simulator and
        # this feature declares a productCode, create + publish the product in the
        # simulator now so the admin can Subscribe immediately after install with
        # no manual seeding. Best-effort (see _seed_simulator_product).
        sim_endpoint = _host_stack_value(host, "FeaturePlatformSimulatorEndpoint")
        if sim_endpoint:
            _seed_simulator_product(
                simulator_endpoint=sim_endpoint,
                manifest=load_manifest(from_code),
                console=console,
            )
    else:
        # Deploy an already-published template — no publish, no SAM/Docker.
        # The feature stack still resolves its UI bundle / agent zip from the
        # FeatureBucket param + the BAKED FeatureArtifactPrefix, so a bucket is
        # required even though the template URL is explicit. Prefer the explicit
        # flag; else parse the bucket from the URL host (publish emits
        # https://<bucket>.s3.<region>.amazonaws.com/<key>).
        # In this mode --bucket-basename (if given) is the literal bucket name
        # backing the template URL, not a basename to suffix — an explicit value
        # wins, else parse it from the URL host.
        feature_id, url_bucket = _parse_published_template_url(template_url)
        feature_bucket = bucket_basename or url_bucket
        if not feature_bucket:
            console.print(
                "[red]✗ Could not determine the feature bucket from "
                f"{template_url!r}. Pass --bucket-basename explicitly.[/red]"
            )
            sys.exit(1)
        if not feature_id and not stack_name:
            console.print(
                "[red]✗ Could not parse the feature id from "
                f"{template_url!r} (expected .../extensions/<id>/template.yaml). "
                "Pass --stack-name explicitly.[/red]"
            )
            sys.exit(1)
        version = None

    feature_stack = stack_name or f"{host_stack_name}-feature-{feature_id}"

    # 3. Resolve parameters. FeatureArtifactPrefix + FeatureVersion are BAKED
    #    into the template (not params). MainStackName + FeatureBucket are part
    #    of every feature template's contract, so submit them unconditionally.
    params: list[dict[str, str]] = [
        {"ParameterKey": "MainStackName", "ParameterValue": host_stack_name},
        {"ParameterKey": "FeatureBucket", "ParameterValue": feature_bucket},
    ]

    # Optional overrides — gate each on the template actually declaring it, so
    # we never submit a param the template doesn't expose (a hard CFN
    # "Parameters do not exist in template" error). Only inspect the template
    # when at least one override was passed.
    optional = {
        "FeatureDisplayName": feature_display_name,
        "LogLevel": log_level,
        "PermissionsBoundaryArn": permissions_boundary_arn,
    }
    if any(v is not None for v in optional.values()):
        try:
            validation = cfn.validate_template(TemplateURL=template_url)
        except Exception as exc:
            console.print(f"[red]✗ Failed to validate feature template: {exc}[/red]")
            sys.exit(1)
        expected = {p["ParameterKey"] for p in validation.get("Parameters") or []}
        for key, value in optional.items():
            if value is not None and key in expected:
                params.append({"ParameterKey": key, "ParameterValue": value})

    # 4. Create-or-update the feature stack.
    console.rule(f"[bold]Deploying feature stack {feature_stack}…[/bold]")
    try:
        arn = create_or_update_stack(
            cfn=cfn,
            stack_name=feature_stack,
            template_url=template_url,
            parameters=params,
            wait=wait,
            console=console,
        )
    except RuntimeError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    console.print()
    console.rule("[bold]Deployed[/bold]")
    console.print(f"  featureId:      {feature_id or '(from --stack-name)'}")
    if version:
        console.print(f"  version:        {version}")
    console.print(f"  host stack:     {host_stack_name}")
    console.print(f"  feature stack:  {arn}")
    console.print(f"  template:       {template_url}")


def _parse_published_template_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """Parse a published feature template URL into (feature_id, bucket).

    Recognises the version-free layout `.../extensions/<id>/template.yaml` and
    the virtual-hosted S3 URL form `https://<bucket>.s3.<region>.amazonaws.com/`
    that `publish` emits. Either element is None when it can't be determined
    (the caller falls back to an explicit flag).
    """
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    host = parsed.netloc.split(":", 1)[0]
    segments = [unquote(s) for s in parsed.path.split("/") if s]

    # bucket: virtual-hosted style `<bucket>.s3[.<region>].amazonaws.com` keeps
    # the bucket before the first `.s3` label; path-style
    # `s3[.<region>].amazonaws.com/<bucket>/<key>` puts it in the first path
    # segment.
    bucket: Optional[str] = None
    if host.startswith("s3.") or host.startswith("s3-"):
        if segments:
            bucket = segments[0]
            segments = segments[1:]
    else:
        marker = host.find(".s3.")
        if marker == -1:
            marker = host.find(".s3-")
        if marker > 0:
            bucket = host[:marker]

    # feature_id: the segment after `extensions/` in the key path.
    feature_id: Optional[str] = None
    if "extensions" in segments:
        idx = segments.index("extensions")
        if idx + 1 < len(segments):
            feature_id = segments[idx + 1]

    return feature_id, bucket


def _host_stack_value(host: dict, key: str) -> Optional[str]:
    """Read a Parameter or Output value named `key` from a describe-stacks dict.
    Returns None (or "" treated as None) when absent."""
    for coll in (host.get("Parameters") or [], host.get("Outputs") or []):
        for item in coll:
            if item.get("ParameterKey") == key or item.get("OutputKey") == key:
                val = item.get("ParameterValue") or item.get("OutputValue")
                return val or None
    return None


def _seed_simulator_product(
    *,
    simulator_endpoint: str,
    manifest,
    console: Console,
) -> None:
    """Create + publish the feature's product in the marketplace simulator so a
    deployed feature can be subscribed to with no manual `curl`. Best-effort and
    idempotent: an unreachable simulator or an already-existing product is logged,
    not fatal — deploy must not fail because the simulator is offline.

    Uses the feature manifest's marketplace block (productCode, displayName,
    pricingModel, dimensions) with sensible defaults so a plain feature seeds a
    free product with one `cap_units` dimension.
    """
    import json as _json
    import ssl
    import urllib.error
    import urllib.request

    product_code = manifest.marketplace.productCode
    if not product_code:
        return  # not a marketplace feature — nothing to seed

    base = simulator_endpoint.rstrip("/")
    pricing = manifest.marketplace.pricingModel or "free"
    dimensions = manifest.marketplace.dimensions or [
        {"apiName": "cap_units", "displayName": "Capacity", "category": "Units"}
    ]
    # nip.io / self-signed simulator certs — tolerate like `curl -k`, scoped here.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def _post(path: str, payload: Optional[dict]) -> int:
        data = _json.dumps(payload).encode("utf-8") if payload is not None else b""
        req = urllib.request.Request(
            f"{base}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:  # nosec B310 - operator-supplied simulator base URL in a local dev CLI, not request input
            return resp.status

    console.rule("[bold]Seeding marketplace simulator…[/bold]")
    try:
        _post(
            "/admin/products",
            {
                "productCode": product_code,
                "name": manifest.displayName,
                "pricingModel": pricing,
                "dimensions": dimensions,
            },
        )
        console.log(f"[green]✓[/green] Created simulator product {product_code}")
    except urllib.error.HTTPError as exc:
        # 409/400 "already exists" is fine — idempotent re-deploy.
        if exc.code in (400, 409):
            console.log(
                f"[dim]Product {product_code} already exists in simulator — reusing.[/dim]"
            )
        else:
            console.log(
                f"[yellow]![/yellow] Could not create simulator product "
                f"{product_code}: {exc}. Seed it manually if Subscribe fails."
            )
            return
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        console.log(
            f"[yellow]![/yellow] Simulator at {base} unreachable ({exc}); skipping "
            f"product seed. Seed it manually if Subscribe fails."
        )
        return

    # Publish locks pricing/dimensions (like real Marketplace). Idempotent.
    try:
        _post(f"/admin/products/{product_code}/publish", None)
        console.log(f"[green]✓[/green] Published simulator product {product_code}")
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 409):
            console.log(f"[dim]Product {product_code} already published.[/dim]")
        else:
            console.log(
                f"[yellow]![/yellow] Could not publish simulator product "
                f"{product_code}: {exc}."
            )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        console.log(
            f"[yellow]![/yellow] Could not publish simulator product "
            f"{product_code}: {exc}."
        )


@main.command("show-schema")
def show_schema() -> None:
    """Print the feature.yaml JSON schema to stdout."""
    schema_path = files("idp_feature_sdk.schemas").joinpath(
        "feature-manifest.schema.json"
    )
    print(schema_path.read_text(encoding="utf-8"))


@main.command("init")
@click.argument("project_dir", type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--feature-id",
    required=True,
    help="DNS-safe slug, e.g. 'docs-by-status'. Used as the S3 prefix, "
    "Cognito session-tag value, and `window.IdpFeatures.register()` key.",
)
@click.option(
    "--display-name",
    required=True,
    help="Human-readable name shown in the IDP nav and on the feature page, "
    "e.g. 'Docs By Status'.",
)
@click.option(
    "--version",
    "version",
    default="0.1.0",
    show_default=True,
    help="Initial SemVer for the feature.",
)
def init_cmd(
    project_dir: Path, feature_id: str, display_name: str, version: str
) -> None:
    """Scaffold a new feature project from the bundled feature-template/.

    Copies the template into <PROJECT_DIR> and substitutes the placeholder
    featureId / displayName / version literals throughout (feature.yaml,
    template.yaml, package.json, entry.tsx, App.tsx, handler.py, README.md).
    Skips node_modules/, dist/, __pycache__/. Refuses to overwrite an
    existing directory.
    """
    try:
        created = scaffold_feature(
            ScaffoldOptions(
                project_dir=project_dir,
                feature_id=feature_id,
                display_name=display_name,
                version=version,
            )
        )
    except ScaffoldError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    console.print(
        f"[green]✓[/green] Scaffolded [bold]{feature_id}[/bold] v{version} → {created}"
    )
    console.print()
    console.print("  Next steps:")
    console.print(f"    cd {created}")
    console.print(
        "    # customise feature-api/handler.py, feature-ui/src/App.tsx, template.yaml"
    )
    console.print("    cd feature-ui && npm install && cd ..")
    console.print("    idp-feature-cli validate .")
    console.print("    idp-feature-cli build .")
    console.print("    idp-feature-cli publish . --bucket-basename <bucket>")


# ---------------------------------------------------------------------------
# Seller Entitlement Service — deployed by an extension SELLER into their own
# AWS Marketplace seller account. See
# feature-platform/seller-entitlement-service/README.md.
#
# This lives in `idp-feature-cli` rather than a Makefile target because the
# audience is extension authors/sellers (the same people who run `publish` and
# `deploy`), and because the preflight is a safety guard whose silent failure
# mode is "every customer locked out" — that deserves unit tests, which a shell
# snippet in a Makefile does not get.
# ---------------------------------------------------------------------------


@main.group("seller-service")
def seller_service_group() -> None:
    """Manage the seller-side entitlement service for paid extensions."""


def _seller_clients(region: str):
    """boto3 STS + Marketplace Catalog clients. Imported lazily so the rest of
    the CLI works without botocore installed."""
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - dependency always present
        raise SellerServiceError(
            "boto3 is required for seller-service commands."
        ) from exc
    return boto3.client("sts"), boto3.client("marketplace-catalog", region_name=region)


def _run_preflight(
    product_registry: str,
    seller_account_id: Optional[str],
    skip_ownership_check: bool,
    region: str,
):
    product_ids = parse_product_registry(product_registry)
    sts_client, catalog_client = _seller_clients(region)
    result = preflight(
        product_ids=product_ids,
        sts_client=sts_client,
        catalog_client=catalog_client,
        expected_account_id=seller_account_id,
        skip_ownership_check=skip_ownership_check,
    )
    console.print(
        f"[green]✓[/green] Credentials resolve to account [bold]{result.account_id}[/bold]"
    )
    console.print(f"  {result.caller_arn}")
    if result.ownership_verified:
        owned = {p.entity_id: p for p in result.owned}
        for pid in result.product_ids:
            p = owned[pid]
            console.print(
                f"[green]✓[/green] {pid} owned by this account — "
                f"'{p.name}' ({p.visibility})"
            )
    else:
        console.print(
            "[yellow]![/yellow] Ownership check skipped. If this account does not "
            "own the products, every activation will be refused and every customer "
            "locked out — silently. You are asserting the account is correct."
        )
    return result


_registry_option = click.option(
    "--product-registry",
    required=True,
    help=(
        'JSON map of productId -> settings, e.g. \'{"prod-abc":{"productCode":"xyz",'
        '"allowFreeTier":true}}\'. productId is the SaaS product ENTITY id '
        "(prod-...), not the product code."
    ),
)
_seller_account_option = click.option(
    "--seller-account-id",
    default=None,
    help="Assert the caller is exactly this AWS account before proceeding.",
)
_skip_ownership_option = click.option(
    "--skip-ownership-check",
    is_flag=True,
    help=(
        "Skip the product-ownership check (use only when the deploying role "
        "lacks aws-marketplace:ListEntities and you are certain the account is "
        "correct)."
    ),
)
_mp_region_option = click.option(
    "--region",
    default=DEFAULT_MARKETPLACE_REGION,
    show_default=True,
    help="Region for AWS Marketplace APIs and the deployed stack.",
)


@seller_service_group.command("preflight")
@_registry_option
@_seller_account_option
@_skip_ownership_option
@_mp_region_option
def seller_service_preflight_cmd(
    product_registry: str,
    seller_account_id: Optional[str],
    skip_ownership_check: bool,
    region: str,
) -> None:
    """Check that the current credentials are the SELLER for these products.

    Read-only. Run this before `deploy` (which runs it automatically) or any time
    you want to confirm which account you are pointed at.
    """
    try:
        _run_preflight(
            product_registry, seller_account_id, skip_ownership_check, region
        )
    except SellerServiceError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)
    console.print("[green]✓ Preflight passed.[/green]")


@seller_service_group.command("deploy")
@_registry_option
@_seller_account_option
@_skip_ownership_option
@_mp_region_option
@click.option(
    "--stack-name",
    default="idp-seller-entitlement",
    show_default=True,
    help="CloudFormation stack name in the seller account.",
)
@click.option(
    "--allowed-accounts",
    default="",
    help=(
        "Comma-separated buyer accounts that receive a token WITHOUT a "
        "subscription check. For your own test deployments only — every entry is "
        "an account getting your paid product for free."
    ),
)
@click.option(
    "--token-ttl-seconds",
    type=int,
    default=None,
    help="Activation token lifetime (template default: 3600).",
)
@click.option("--guided", is_flag=True, help="Pass --guided to `sam deploy`.")
@click.option(
    "--yes", is_flag=True, help="Skip the confirmation prompt after preflight."
)
def seller_service_deploy_cmd(
    product_registry: str,
    seller_account_id: Optional[str],
    skip_ownership_check: bool,
    region: str,
    stack_name: str,
    allowed_accounts: str,
    token_ttl_seconds: Optional[int],
    guided: bool,
    yes: bool,
) -> None:
    """Preflight, then deploy the Seller Entitlement Service to this account.

    Deploys into the account the current credentials resolve to — so the
    preflight runs first and refuses if that account does not own the products
    being registered.
    """
    try:
        service_dir = find_seller_service_dir()
        if service_dir is None:
            raise SellerServiceError(
                "Could not find feature-platform/seller-entitlement-service/. "
                "Run this from a checkout of the IDP Accelerator repository "
                "(the template and Lambda source live there, as with "
                "`idp-feature-cli init`)."
            )

        result = _run_preflight(
            product_registry, seller_account_id, skip_ownership_check, region
        )

        version = read_service_version(service_dir)
        console.print()
        console.print("  About to deploy the Seller Entitlement Service:")
        console.print(f"    account    {result.account_id}")
        console.print(f"    region     {region}")
        console.print(f"    stack      {stack_name}")
        if version:
            console.print(f"    version    {version}")
        if allowed_accounts:
            console.print(
                f"    [yellow]allow-list {allowed_accounts} "
                f"(these accounts skip the subscription check)[/yellow]"
            )
        console.print()

        if not yes and not click.confirm("Proceed?", default=False):
            console.print("Aborted.")
            sys.exit(1)

        run_command(["sam", "build"], cwd=service_dir)
        run_command(
            build_sam_deploy_command(
                service_dir=service_dir,
                stack_name=stack_name,
                region=region,
                product_registry_json=product_registry,
                allowed_accounts=allowed_accounts,
                token_ttl_seconds=token_ttl_seconds,
                guided=guided,
            ),
            cwd=service_dir,
        )

        # Verify the registry actually reached the function. A mangled registry
        # produces an endpoint that looks healthy and refuses every customer.
        import boto3

        deployed = verify_deployed_registry(
            cfn_client=boto3.client("cloudformation", region_name=region),
            lambda_client=boto3.client("lambda", region_name=region),
            stack_name=stack_name,
            expected_product_ids=result.product_ids,
        )
        console.print(
            f"[green]✓[/green] Deployed registry serves "
            f"{len(deployed)} product(s): {', '.join(sorted(deployed))}"
        )
    except SellerServiceError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    console.print()
    console.print("[green]✓ Seller Entitlement Service deployed.[/green]")
    console.print("  Next steps:")
    console.print(
        f"    aws cloudformation describe-stacks --stack-name {stack_name} "
        f"--region {region} --query 'Stacks[0].Outputs' --output table"
    )
    console.print(
        "    # bake ActivationEndpoint + the public key into your extension; see"
    )
    console.print(
        "    # feature-platform/seller-entitlement-service/README.md "
        "'Buyer-side integration contract'"
    )


@seller_service_group.command("export-trust-bundle")
@_mp_region_option
@click.option(
    "--stack-name",
    default="idp-seller-entitlement",
    show_default=True,
    help="Seller-service stack to export from.",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Write activation-trust.json + activation-public-key.pem here. Omit to "
    "print the JSON to stdout.",
)
def seller_service_export_trust_bundle_cmd(
    region: str,
    stack_name: str,
    output_dir: Optional[Path],
) -> None:
    """Export the material an extension bakes in at build time.

    Emits the activation endpoint, the `kid` (the signing key ARN — byte-identical
    to the token's `kid` claim), the signing algorithm, and the **public**
    verification key as PEM.

    Run this once per extension release and embed the result. The public key is
    safe to ship in public artifacts: it verifies tokens, it cannot mint them. It
    belongs embedded rather than fetched at runtime — a key fetched from the
    artifact bucket would make that bucket a forgery trust root, which is exactly
    why `activation.json` carries the endpoint and nothing else.
    """
    try:
        import boto3

        cfn = boto3.client("cloudformation", region_name=region)
        endpoint = resolve_stack_output(cfn, stack_name, "ActivationEndpoint")
        key_arn = resolve_stack_output(cfn, stack_name, "TokenSigningKeyArn")
        try:
            service_version = resolve_stack_output(cfn, stack_name, "ServiceVersion")
        except SellerServiceError:
            service_version = ""

        der = fetch_signing_public_key(boto3.client("kms", region_name=region), key_arn)
        bundle = build_trust_bundle(
            activation_endpoint=endpoint,
            kid=key_arn,
            public_key_der=der,
            service_version=service_version,
            exported_at=utc_now_iso(),
        )
    except SellerServiceError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    if output_dir is None:
        # Plain print, not console.print: this is machine-readable output that a
        # release script will pipe, so it must not pick up Rich markup or wrapping.
        print(json.dumps(bundle, indent=2, sort_keys=True))
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = output_dir / "activation-trust.json"
    pem_path = output_dir / "activation-public-key.pem"
    bundle_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8"
    )
    pem_path.write_text(bundle["publicKeyPem"], encoding="utf-8")
    console.print(f"[green]✓[/green] {bundle_path}")
    console.print(f"[green]✓[/green] {pem_path}")
    console.print(f"  endpoint {bundle['activationEndpoint']}")
    console.print(f"  kid      {bundle['kid']}")
    console.print(f"  alg      {bundle['signingAlgorithm']}")


@seller_service_group.command("publish-endpoint")
@_mp_region_option
@click.option(
    "--stack-name",
    default="idp-seller-entitlement",
    show_default=True,
    help="Seller-service stack to read the ActivationEndpoint from.",
)
@click.option(
    "--feature-id",
    "feature_ids",
    multiple=True,
    required=True,
    help="Extension featureId to publish the pointer for. Repeatable — pass every "
    "extension served by this endpoint.",
)
@click.option(
    "--bucket-basename",
    required=True,
    help="Artifact bucket basename; the region is appended, matching "
    "`idp-feature-cli publish` (e.g. 'aws-ml-blog' -> 'aws-ml-blog-us-east-1').",
)
@click.option(
    "--artifact-regions",
    required=True,
    help="Comma-separated regions to publish into — EVERY region the extension is "
    "offered in, since an extension reads the pointer from its own regional bucket.",
)
@click.option(
    "--prefix",
    "s3_prefix",
    default="",
    help="S3 key prefix under the bucket, matching the catalog's templateKey "
    "(e.g. 'artifacts/genai-idp-mp').",
)
@click.option(
    "--private",
    is_flag=True,
    help="Do NOT set public-read. The pointer is read by extensions in arbitrary "
    "buyer accounts, so it normally must be public — like the template beside it.",
)
def seller_service_publish_endpoint_cmd(
    region: str,
    stack_name: str,
    feature_ids: tuple,
    bucket_basename: str,
    artifact_regions: str,
    s3_prefix: str,
    private: bool,
) -> None:
    """Publish the activation endpoint as a pointer file next to latest.json.

    This is the indirection layer for the endpoint URL. The URL embeds an
    API-Gateway-assigned API id, so replacing the API would otherwise
    permanently break every already-installed extension — those run in customer
    accounts the seller cannot reach. Extensions read this pointer instead.

    Carries no key material by design: the public verification key stays embedded
    in the extension, so a tampered pointer can only cause a fail-closed
    activation, never a forged entitlement.
    """
    try:
        import boto3

        cfn = boto3.client("cloudformation", region_name=region)
        endpoint = resolve_stack_output(cfn, stack_name, "ActivationEndpoint")
        try:
            key_id = resolve_stack_output(cfn, stack_name, "TokenSigningKeyArn")
        except SellerServiceError:
            key_id = ""
        try:
            service_version = resolve_stack_output(cfn, stack_name, "ServiceVersion")
        except SellerServiceError:
            service_version = ""

        document = build_activation_pointer(
            activation_endpoint=endpoint,
            signing_key_id=key_id,
            service_version=service_version,
            published_at=utc_now_iso(),
        )

        targets = [r.strip() for r in artifact_regions.split(",") if r.strip()]
        if not targets:
            raise SellerServiceError("--artifact-regions listed no regions")

        console.print(f"  endpoint: {endpoint}")
        console.print(f"  features: {', '.join(feature_ids)}")
        console.print()
        for artifact_region in targets:
            bucket = f"{bucket_basename}-{artifact_region}"
            written = publish_activation_pointer(
                s3_client=boto3.client("s3", region_name=artifact_region),
                bucket=bucket,
                feature_ids=list(feature_ids),
                document=document,
                s3_prefix=s3_prefix,
                make_public=not private,
            )
            for key in written:
                console.print(f"[green]✓[/green] s3://{bucket}/{key}")
    except SellerServiceError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    console.print()
    console.print(
        "[green]✓ Activation pointer published.[/green] Extensions should read it "
        "at activation time and fall back to their embedded default if unreachable."
    )


@seller_service_group.command("activations")
@_mp_region_option
@click.option(
    "--stack-name",
    default="idp-seller-entitlement",
    show_default=True,
    help="Seller-service stack to read the roster from.",
)
@click.option("--product-id", default=None, help="Only this product (uses the GSI).")
@click.option("--buyer-account-id", default=None, help="Only this buyer account.")
@click.option(
    "--outcome",
    type=click.Choice(["granted", "refused"]),
    default=None,
    help="Only attempts whose LAST outcome was this.",
)
@click.option(
    "--since",
    default=None,
    help="Only attempts on/after this ISO timestamp, e.g. 2026-08-01.",
)
@click.option(
    "--table-name",
    default=None,
    help="Read this table directly, skipping stack lookup.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def seller_service_activations_cmd(
    region: str,
    stack_name: str,
    product_id: Optional[str],
    buyer_account_id: Optional[str],
    outcome: Optional[str],
    since: Optional[str],
    table_name: Optional[str],
    as_json: bool,
) -> None:
    """Show which buyer accounts have activated (or been refused) which products.

    Reads the seller service's activation roster — one row per (buyer account,
    product) with first/last seen, attempt counts, and the last outcome. Run this
    with credentials for your SELLER account.

    The roster is the durable record: the Lambda and API access logs hold the
    per-request forensic detail but age out with LogRetentionInDays.
    """
    try:
        import boto3

        if not table_name:
            table_name = resolve_stack_output(
                boto3.client("cloudformation", region_name=region),
                stack_name,
                "ActivationsTableName",
            )
        records = fetch_activations(
            dynamodb_resource=boto3.resource("dynamodb", region_name=region),
            table_name=table_name,
            product_id=product_id,
            buyer_account_id=buyer_account_id,
            outcome=outcome,
            since=since,
        )
    except SellerServiceError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        sys.exit(1)

    if as_json:
        import dataclasses
        import json as _json

        click.echo(_json.dumps([dataclasses.asdict(r) for r in records], indent=2))
        return

    if not records:
        console.print(
            "No activation attempts recorded yet"
            + (f" for {product_id}" if product_id else "")
            + "."
        )
        return

    from rich.table import Table

    table = Table(title=f"Activation roster ({len(records)} account/product pairs)")
    table.add_column("Buyer account")
    table.add_column("Product")
    table.add_column("Last outcome")
    table.add_column("Attempts", justify="right")
    table.add_column("Granted", justify="right")
    table.add_column("Tier")
    table.add_column("First seen")
    table.add_column("Last seen")
    for r in records:
        colour = "green" if r.last_outcome == "granted" else "red"
        table.add_row(
            r.buyer_account_id,
            r.product_id,
            f"[{colour}]{r.last_outcome}[/{colour}]",
            str(r.attempt_count),
            str(r.granted_count),
            "free" if r.free_tier else "paid",
            r.first_attempt_at[:19],
            r.last_attempt_at[:19],
        )
    console.print(table)

    refused = [r for r in records if r.last_outcome == "refused"]
    if refused:
        console.print()
        console.print(
            f"[yellow]{len(refused)} account/product pair(s) last refused.[/yellow] "
            "Most recent reasons:"
        )
        for r in refused[:5]:
            console.print(
                f"  {r.buyer_account_id} / {r.product_id}: {r.detail or '(no detail)'}"
            )


if __name__ == "__main__":
    main()
